#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys

from marionette_driver.errors import MarionetteException
from marionette_driver.marionette import Marionette


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ids-json", required=True)
    parser.add_argument("--port", type=int, default=2828)
    parser.add_argument("--allow-excluded", action="store_true")
    args = parser.parse_args()
    try:
        ids = json.loads(args.ids_json)
        if not isinstance(ids, list) or len(ids) > 5000 or not all(isinstance(item, str) for item in ids):
            raise ValueError("expected at most 5000 tab IDs")
        ids = list(dict.fromkeys(ids))
    except (ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 1

    client = Marionette(host="127.0.0.1", port=args.port, socket_timeout=30)
    try:
        client.start_session()
        client.set_context(client.CONTEXT_CHROME)
        result = client.execute_script(
            """
            const { ZenSpacesSyncModel } = ChromeUtils.importESModule(
              "resource:///modules/zen/ZenSpacesSyncModel.sys.mjs"
            );
            ZenSpacesSyncModel.invalidate();
            const { ZenSessionStore } = ChromeUtils.importESModule(
              "resource:///modules/zen/ZenSessionManager.sys.mjs");
            const sidebar = ZenSessionStore.getSidebarData() || {};
            // Zen's own empty tabs (folder placeholders, the new-tab surface) are never
            // synced; report them apart so they do not count as failed exports.
            const structural = arguments[0].filter(
              id => (sidebar.tabs || []).some(t => t.zenSyncId === id && t.zenIsEmpty));
            const exported = arguments[0].map(id => {
              let projected = ZenSpacesSyncModel.projectRecord(id);
              if (!projected) {
                const t = (sidebar.tabs || []).find(t => t.zenSyncId === id);
                const entries = t?.entries || [];
                const entry = entries[Math.max(0, Math.min(entries.length - 1, (t?.index || entries.length) - 1))];
                if (t && !t.zenIsEmpty && !t.zenIsGlance && !t.zenLiveFolderItemId && (!entries.length || entry?.url === "about:blank")) {
                  projected = {kind: "tab", data: {
                    tabId: id, url: "about:blank", title: entry?.title || "",
                    containerGuid: ZenSpacesSyncModel.guidForContextId(t.userContextId, {create:true}),
                    essential: !!t.zenEssential, pinned: !!t.pinned,
                    workspaceUuid: t.zenWorkspace || null, folderId: null
                  }};
                }
              }
              if (!projected || projected.kind !== "tab") {
                return null;
              }
              const data = { ...projected.data };
              if (typeof data.icon === "string" && data.icon.startsWith("data:") && data.icon.length > 100000) {
                data.icon = null;
              }
              return { id, cleartext: { id, kind: projected.kind, data } };
            }).filter(Boolean);
            return { records: exported, structural };
            """,
            script_args=[ids],
        )
    except (OSError, MarionetteException) as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 1
    finally:
        try:
            client.delete_session()
        except Exception:
            pass
    return report(ids, result, allow_excluded=args.allow_excluded)


def report(ids: list[str], result: dict, *, allow_excluded: bool = False) -> int:
    records = result["records"]
    structural = set(result["structural"]) - {r["id"] for r in records}
    missing = set(ids) - {r["id"] for r in records} - structural
    if missing and not allow_excluded:
        print(json.dumps({"ok": False, "error": "not every opened tab could be exported", "requested": len(ids), "exported": len(records), "structural": len(structural)}))
        return 1
    print(json.dumps({"ok": True, "records": records, "structuralIds": sorted(structural), "excludedIds": sorted(missing | structural)}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
