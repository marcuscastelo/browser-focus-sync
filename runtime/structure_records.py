#!/usr/bin/env python3
"""Read and apply Zen sidebar records through temporary Marionette control.

state   digests of every projected record, plus every id that still exists
export  full records for the given ids
apply   opened tabs, changed structure and structure deletions in one batch

The caller holds the bridge lease; this process only talks to the port.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from marionette_driver.errors import MarionetteException
from marionette_driver.marionette import Marionette

MAX_RECORDS = 5000

PRELUDE = """
const done = arguments[arguments.length - 1];
const { ZenSpacesSyncModel, recordDigest } = ChromeUtils.importESModule(
  "resource:///modules/zen/ZenSpacesSyncModel.sys.mjs");
const { ZenSessionStore } = ChromeUtils.importESModule(
  "resource:///modules/zen/ZenSessionManager.sys.mjs");
// Firefox moved the session store modules; resolve before mutating anything.
const sessionModule = name => {
  try {
    return ChromeUtils.importESModule(`moz-src:///browser/components/sessionstore/${name}.sys.mjs`);
  } catch (_) {
    return ChromeUtils.importESModule(`resource:///modules/sessionstore/${name}.sys.mjs`);
  }
};
const { SessionSaver } = sessionModule("SessionSaver");
const win = Services.wm.getMostRecentWindow("navigator:browser");
// Collect now: projections follow the stored sidebar, which otherwise lags the
// window by the session save interval.
const collect = async () => {
  await SessionSaver.run();
  ZenSpacesSyncModel.invalidate();
};
const digests = () => {
  const result = {};
  for (const [id, projected] of ZenSpacesSyncModel.projections()) {
    result[id] = [projected.kind, recordDigest(projected.kind, projected.data)];
  }
  return result;
};
const presentIds = () => {
  const sidebar = ZenSessionStore.getSidebarData() || {};
  return [
    ...(sidebar.tabs || []).map(tab => tab.zenSyncId),
    ...(sidebar.folders || []).map(folder => folder.id),
    ...(sidebar.splitViewData || []).map(split => split.groupId),
    ...win.gZenWorkspaces.getWorkspaces().map(space => space.uuid),
  ].filter(Boolean);
};
const exportRecord = id => {
  const projected = ZenSpacesSyncModel.projectRecord(id);
  if (!projected) return null;
  const data = { ...projected.data };
  if (typeof data.icon === "string" && data.icon.startsWith("data:") && data.icon.length > 100000) {
    data.icon = null;
  }
  return { id, cleartext: { id, kind: projected.kind, data } };
};
"""

STATE = PRELUDE + """
(async () => {
  await win.gZenWorkspaces.promiseInitialized;
  await collect();
  return { ok: true, records: digests(), presentIds: presentIds() };
})().then(done, error => done({ ok: false, error: String(error) }));
"""

EXPORT = PRELUDE + """
const [ids] = arguments;
(async () => {
  await win.gZenWorkspaces.promiseInitialized;
  await collect();
  const records = ids.map(exportRecord).filter(Boolean);
  const exported = new Set(records.map(record => record.id));
  return { ok: true, records, missing: ids.filter(id => !exported.has(id)) };
})().then(done, error => done({ ok: false, error: String(error) }));
"""

APPLY = PRELUDE + """
const [payload] = arguments;
const { ZenSpacesSyncApplier } = ChromeUtils.importESModule(
  "resource:///modules/zen/ZenSpacesSyncApplier.sys.mjs");
const tabRecords = payload.tabRecords || [];
const records = payload.records || [];
const closing = new Set(payload.closingTabIds || []);
const exists = record => {
  const kind = record.cleartext.kind;
  if (kind === "space") {
    return win.gZenWorkspaces.getWorkspaces().some(space => space.uuid === record.id);
  }
  if (kind === "tab" || kind === "folder" || kind === "split") {
    return !!win.document.getElementById(record.id);
  }
  return true;
};
const inFolder = (tab, folderId) => {
  for (let group = tab.group; group; group = group.group) {
    if (group.id === folderId) return true;
  }
  return false;
};
// Tabs a structure deletion would take with it and this handoff does not close.
const survivors = (kind, id) => win.gBrowser.tabs.filter(tab =>
  !tab.hasAttribute("zen-empty-tab") && !closing.has(tab.id) &&
  (kind === "folder"
    ? inFolder(tab, id)
    : !tab.hasAttribute("zen-essential") && tab.getAttribute("zen-workspace-id") === id));
(async () => {
  await win.gZenWorkspaces.promiseInitialized;
  await collect();
  const before = digests();
  // An update for something this side no longer has is not a creation:
  // resurrecting it would undo a deletion made here.
  const skipped = records.filter(record => record.op === "update" && !exists(record)).map(r => r.id);
  const batch = records.filter(record => !skipped.includes(record.id));
  // Native Spaces records omit ordinary about:blank tabs; create them directly.
  for (const record of tabRecords) {
    const d = record.cleartext.data;
    if (d.url !== "about:blank" || win.document.getElementById(record.id)) continue;
    const context = d.containerGuid ? ZenSpacesSyncModel.contextIdForGuid(d.containerGuid) : 0;
    if (context === null) throw new Error("Unknown container for blank tab");
    const tab = win.gBrowser.addTrustedTab("about:blank", {
      inBackground: true, skipAnimation: true, skipRoute: true, userContextId: context
    });
    tab.id = record.id;
    if (d.workspaceUuid) win.gZenWorkspaces.moveTabToWorkspace(tab, d.workspaceUuid);
  }
  // Closed tabs go first, as tombstones, so the digests collected below already
  // describe the spaces and folders without them.
  const failed = await ZenSpacesSyncApplier.applyBatch([
    ...[...closing].map(id => ({ id, deleted: true })),
    ...tabRecords.filter(record => record.cleartext.data.url !== "about:blank"),
    ...batch,
  ]);
  // Tab removal is animated; wait until the closed tabs are really gone.
  for (let i = 0; i < 60 && [...closing].some(id => win.document.getElementById(id)); i++) {
    await new Promise(resolve => win.setTimeout(resolve, 50));
  }
  const deleted = [];
  const kept = [];
  for (const id of payload.deletedIds || []) {
    const el = win.document.getElementById(id);
    const spaces = win.gZenWorkspaces.getWorkspaces();
    if (el?.isZenFolder) {
      if (survivors("folder", id).length) { kept.push(id); continue; }
      // ZenFolder.delete() animates and runs beforeunload prompts nobody is
      // there to answer; every tab in it was already closed on the other side.
      for (const item of el.allItemsRecursive) {
        if (item.hasAttribute?.("zen-empty-tab")) win.gBrowser.removeTab(item, { animate: false });
      }
      await win.gBrowser.removeTabGroup(el, { animate: false, skipPermitUnload: true });
      deleted.push(id);
    } else if (el?.hasAttribute?.("split-view-group")) {
      // Unsplitting keeps every tab.
      const index = win.gZenViewSplitter._data.findIndex(group => group.groupId === id);
      if (index >= 0) win.gZenViewSplitter.removeGroup(index);
      deleted.push(id);
    } else if (spaces.some(space => space.uuid === id)) {
      if (spaces.length <= 1 || survivors("space", id).length) { kept.push(id); continue; }
      await win.gZenWorkspaces.removeWorkspace(id);
      deleted.push(id);
    } else {
      deleted.push(id);
    }
  }
  // Unsplitting is animated; collect once the groups are really gone.
  for (let i = 0; i < 40 && deleted.some(id => win.document.getElementById(id)); i++) {
    await new Promise(resolve => win.setTimeout(resolve, 50));
  }
  await collect();
  const present = new Set(presentIds());
  const tabIds = [...tabRecords.map(r => r.id), ...closing];
  const tabFailed = tabIds.filter(id => failed.includes(id));
  const missing = tabRecords.map(r => r.id).filter(id => !present.has(id));
  const remaining = [...closing].filter(id => present.has(id));
  return {
    // Opened and closed tabs must land; structure that could not be applied is
    // reported but does not block the handoff.
    ok: tabFailed.length === 0 && missing.length === 0 && remaining.length === 0,
    requested: tabRecords.length + records.length,
    failed,
    missing,
    remaining,
    skipped,
    deleted,
    kept,
    before,
    records: digests(),
    presentIds: [...present],
  };
})().then(done, error => done({ ok: false, error: String(error) }));
"""


def run_script(script: str, args: list, port: int, timeout: int) -> dict:
    client = Marionette(host="127.0.0.1", port=port, socket_timeout=timeout + 30)
    try:
        client.start_session()
        client.set_context(client.CONTEXT_CHROME)
        client.timeout.script = timeout
        return client.execute_async_script(script, script_args=args)
    finally:
        try:
            client.delete_session()
        except Exception:
            pass


def load_ids(text: str) -> list[str]:
    ids = json.loads(text)
    if not isinstance(ids, list) or len(ids) > MAX_RECORDS or not all(isinstance(item, str) for item in ids):
        raise ValueError(f"expected at most {MAX_RECORDS} record IDs")
    return list(dict.fromkeys(ids))


def valid_record(record: object, kinds: set[str] | None = None) -> bool:
    if not isinstance(record, dict) or not isinstance(record.get("id"), str):
        return False
    cleartext = record.get("cleartext")
    if not isinstance(cleartext, dict) or not isinstance(cleartext.get("data"), dict):
        return False
    return kinds is None or cleartext.get("kind") in kinds


def load_payload(path: Path) -> dict:
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        raise ValueError("expected a JSON object")
    tab_records = payload.get("tabRecords", [])
    records = payload.get("records", [])
    if not isinstance(tab_records, list) or not isinstance(records, list):
        raise ValueError("tabRecords and records must be lists")
    if len(tab_records) + len(records) > MAX_RECORDS:
        raise ValueError(f"expected at most {MAX_RECORDS} records")
    if not all(valid_record(record, {"tab"}) for record in tab_records):
        raise ValueError("invalid tab record")
    if not all(valid_record(record) and record.get("op") in {"create", "update"} for record in records):
        raise ValueError("invalid structure record")
    for key in ("deletedIds", "closingTabIds"):
        value = payload.get(key, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"{key} must be a list of strings")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["state", "export", "apply"])
    parser.add_argument("--ids-json")
    parser.add_argument("--payload-file")
    parser.add_argument("--port", type=int, default=2828)
    args = parser.parse_args()
    try:
        if args.action == "state":
            result = run_script(STATE, [], args.port, 60)
        elif args.action == "export":
            result = run_script(EXPORT, [load_ids(args.ids_json or "[]")], args.port, 60)
        else:
            if not args.payload_file:
                raise ValueError("--payload-file is required")
            result = run_script(APPLY, [load_payload(Path(args.payload_file))], args.port, 90)
    except (OSError, ValueError, json.JSONDecodeError, MarionetteException) as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0 if isinstance(result, dict) and result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
