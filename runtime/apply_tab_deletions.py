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
    args = parser.parse_args()
    try:
        ids = json.loads(args.ids_json)
        if not isinstance(ids, list) or len(ids) > 5000 or not all(isinstance(item, str) for item in ids):
            raise ValueError("expected at most 5000 tab IDs")
        ids = list(dict.fromkeys(ids))
    except (ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 1

    client = Marionette(host="127.0.0.1", port=args.port, socket_timeout=10)
    try:
        client.start_session()
        client.set_context(client.CONTEXT_CHROME)
        client.timeout.script = 90
        result = client.execute_async_script(
            """
              const done = arguments[arguments.length - 1];
              const ids = arguments[0];
              const { ZenSpacesSyncApplier } = ChromeUtils.importESModule(
                "resource:///modules/zen/ZenSpacesSyncApplier.sys.mjs"
              );
              const { ZenSessionStore } = ChromeUtils.importESModule(
                "resource:///modules/zen/ZenSessionManager.sys.mjs"
              );
              const before = ZenSessionStore.getSidebarData()?.tabs?.length || 0;
              ZenSpacesSyncApplier.applyBatch(ids.map(id => ({ id, deleted: true }))).then(
                failed => {
                  const deleted = new Set(ids);
                  const sidebar = ZenSessionStore.getSidebarData() || {};
                  const tabs = (sidebar.tabs || []).filter(tab => !deleted.has(tab.zenSyncId));
                  if (tabs.length !== (sidebar.tabs || []).length) {
                    ZenSessionStore.saveState({
                      windows: [{
                        tabs,
                        folders: sidebar.folders || [],
                        splitViewData: sidebar.splitViewData || [],
                        groups: sidebar.groups || [],
                        spaces: sidebar.spaces || [],
                        isPopup: false,
                        isTaskbarTab: false,
                        isZenUnsynced: false,
                      }],
                    });
                  }
                  const after = ZenSessionStore.getSidebarData()?.tabs?.length || 0;
                  const remaining = (ZenSessionStore.getSidebarData()?.tabs || [])
                    .map(tab => tab.zenSyncId)
                    .filter(id => deleted.has(id));
                  done({
                    ok: failed.length === 0 && remaining.length === 0,
                    requested: ids.length,
                    failed,
                    remaining,
                    before,
                    after,
                  });
                },
                error => done({ ok: false, error: String(error), before })
              );
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
    print(json.dumps(result, separators=(",", ":")))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
