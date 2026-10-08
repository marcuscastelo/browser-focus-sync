"""Structure handoff: which Zen records changed since the last handoff.

Zen projects its sidebar into records (``ZenSpacesSyncModel.projections()``):
spaces with their ordered children, folders, tabs with placement and URL,
split views, the space/essentials layout and containers. Each side keeps the
digest of every record as of the last handoff in its baseline; a handoff sends
the records whose digest changed and the structure that disappeared. The native
Firefox Sync engine and its server-side state are not involved.

Rules that keep this from destroying anything:
- after a browser restart only records that did not exist before are sent;
  nothing is modified or deleted on the other side from a restored session;
- a baseline without digests (older version) only establishes digests;
- only spaces, folders and split views are deleted as structure; tabs keep
  their own close rule, and containers are never deleted;
- the receiver refuses to delete a space or folder that still holds tabs the
  handoff does not close.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import lz4.block

DELETABLE_KINDS = frozenset({"space", "folder", "split"})
# Tab fields that place a tab or name what it shows; selection and scroll are not structure.
TAB_FIELDS = ("zenSyncId", "zenWorkspace", "groupId", "pinned", "zenEssential",
              "userContextId", "zenStaticLabel", "zenIsEmpty")


def read_session(profile: Path) -> dict:
    payload = (profile / "zen-sessions.jsonlz4").read_bytes()
    return json.loads(lz4.block.decompress(payload[8:]))


def current_url(tab: dict) -> str | None:
    entries = tab.get("entries") or []
    if not entries:
        return None
    index = min(max((tab.get("index") or len(entries)) - 1, 0), len(entries) - 1)
    return (entries[index] or {}).get("url")


def fingerprint(session: dict) -> str:
    """Cheap change detector over the stored session, read without browser control.

    It covers every input of the projections the handoff compares (structure,
    tab order, placement and URL), so an unchanged fingerprint means there is
    nothing to send and control is not needed.
    """
    tabs = [[tab.get(key) for key in TAB_FIELDS] + [current_url(tab)]
            for tab in session.get("tabs", [])]
    structure = {key: session.get(key, []) for key in ("spaces", "folders", "groups", "splitViewData")}
    encoded = json.dumps({"tabs": tabs, "structure": structure}, sort_keys=True,
                         separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def stored_fingerprint(profile: Path) -> str:
    return fingerprint(read_session(profile))


def valid_digests(value: object) -> dict[str, list[str]] | None:
    """``{id: [kind, digest]}`` as stored in baselines and returned by the browser."""
    if not isinstance(value, dict):
        return None
    for key, item in value.items():
        if (not isinstance(key, str) or not isinstance(item, list) or len(item) != 2
                or not all(isinstance(part, str) for part in item)):
            return None
    return value


def plan(
    baseline: dict[str, list[str]] | None,
    current: dict[str, list[str]],
    present_ids: set[str],
    *,
    same_browser: bool,
    skip_ids: set[str] = frozenset(),
) -> tuple[list[str], list[str]]:
    """Return (ids to send, structure ids to delete) for one side of a handoff.

    ``skip_ids`` are travelling elsewhere (opened tabs) or were already decided
    by the other side in this handoff.
    """
    if baseline is None:
        return [], []
    changed = []
    for record_id, (_kind, digest) in current.items():
        if record_id in skip_ids:
            continue
        previous = baseline.get(record_id)
        if previous is None or (same_browser and previous[1] != digest):
            changed.append(record_id)
    deleted = []
    if same_browser:
        for record_id, (kind, _digest) in baseline.items():
            if (kind in DELETABLE_KINDS and record_id not in current
                    and record_id not in present_ids and record_id not in skip_ids):
                deleted.append(record_id)
    return sorted(changed), sorted(deleted)


def record_ids(records: list[dict[str, object]]) -> set[str]:
    ids = set()
    for record in records:
        identifier = record.get("id")
        if not isinstance(identifier, str):
            cleartext = record.get("cleartext")
            identifier = cleartext.get("id") if isinstance(cleartext, dict) else None
        if isinstance(identifier, str):
            ids.add(identifier)
    return ids


def outgoing(exported: list[dict[str, object]], baseline: dict[str, list[str]] | None) -> list[dict[str, object]]:
    """Mark each record as a creation or an update of something both sides had."""
    return [dict(record, op="update" if baseline and record.get("id") in baseline else "create")
            for record in exported]


def received(
    base: dict[str, list[str]] | None,
    report: dict[str, object],
    sent: list[dict[str, object]],
    closed_tab_ids: list[str] | set[str],
) -> dict[str, list[str]] | None:
    """Receiver baseline after applying ``sent``, from the browser's apply report.

    The receiver stores its own digests of what it now has, so a record the
    browser could not reproduce exactly is not echoed back, and changes the
    apply caused (a space's order after a tab arrived) are absorbed. Only
    records this side edited and has not sent yet stay pending.
    """
    after = valid_digests(report.get("records"))
    if after is None:
        return base
    if base is None:
        return after
    before = valid_digests(report.get("before")) or {}
    sent_ids = record_ids(sent)
    result = dict(base)
    for key in set(before) | set(after):
        if key not in sent_ids and before.get(key) != base.get(key):
            continue  # a local edit still waiting for its own handoff
        if key in after:
            result[key] = after[key]
        else:
            result.pop(key, None)
    for key in set(report.get("deleted") or []) | set(closed_tab_ids):
        result.pop(key, None)
    return result


def summary(report: dict[str, object]) -> str:
    """One log line without titles or URLs."""
    if not report.get("ok") and report.get("error"):
        return f"ok=False error={report['error']!r}"
    parts = [f"ok={bool(report.get('ok'))}", f"requested={report.get('requested', 0)}"]
    for key in ("failed", "missing", "remaining", "skipped", "deleted", "kept"):
        value = report.get(key) or []
        if value:
            parts.append(f"{key}={','.join(map(str, value))}")
    return " ".join(parts)
