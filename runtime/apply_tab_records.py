#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import bfs_config as cfg

from marionette_driver.errors import MarionetteException
from marionette_driver.marionette import Marionette


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--records-file", required=True)
    parser.add_argument("--port", type=int, default=2828)
    args = parser.parse_args()
    try:
        records = json.loads(Path(args.records_file).read_text())
        if not isinstance(records, list) or len(records) > 5000:
            raise ValueError("expected at most 5000 tab records")
        if not all(
            isinstance(record, dict)
            and isinstance(record.get("id"), str)
            and record.get("cleartext", {}).get("kind") == "tab"
            for record in records
        ):
            raise ValueError("invalid tab record")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 1
    if not records:
        print(json.dumps({"ok": True, "requested": 0, "added": 0}))
        return 0

    client = Marionette(host="127.0.0.1", port=args.port, socket_timeout=120)
    try:
        client.start_session()
        client.set_context(client.CONTEXT_CHROME)
        client.timeout.script = 90
        result = client.execute_async_script(
            """
            const done = arguments[arguments.length - 1];
            const records = arguments[0];
            const { ZenSpacesSyncApplier } = ChromeUtils.importESModule(
              "resource:///modules/zen/ZenSpacesSyncApplier.sys.mjs"
            );
            // Resolve imports before mutating: Firefox changed this URI.
            let SessionStore;
            try {
              ({ SessionStore } = ChromeUtils.importESModule(
                "moz-src:///browser/components/sessionstore/SessionStore.sys.mjs"));
            } catch (_) {
              ({ SessionStore } = ChromeUtils.importESModule(
                "resource:///modules/sessionstore/SessionStore.sys.mjs"));
            }
            Promise.resolve().then(async () => {
              // Native Spaces Sync omits ordinary about:blank tabs. Preserve
              // them in our direct handoff without touching folder placeholders.
              const { ZenSpacesSyncModel } = ChromeUtils.importESModule(
                "resource:///modules/zen/ZenSpacesSyncModel.sys.mjs");
              const win = Services.wm.getMostRecentWindow("navigator:browser");
              for (const record of records) {
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
              return ZenSpacesSyncApplier.applyBatch(records.filter(r => r.cleartext.data.url !== "about:blank"));
            }).then(failed => {
              const { ZenSessionStore } = ChromeUtils.importESModule(
                "resource:///modules/zen/ZenSessionManager.sys.mjs"
              );
              const win = Services.wm.getMostRecentWindow("navigator:browser");
              const ids = new Set(records.map(record => record.id));
              const sidebar = ZenSessionStore.getSidebarData() || {};
              const added = [...ids].map(id => win.document.getElementById(id))
                .filter(tab => win.gBrowser.isTab(tab))
                .map(tab => JSON.parse(SessionStore.getTabState(tab)));
              const tabs = [
                ...(sidebar.tabs || []).filter(tab => !ids.has(tab.zenSyncId)),
                ...added,
              ];
              ZenSessionStore.saveState({ windows: [{
                tabs,
                folders: sidebar.folders || [],
                splitViewData: sidebar.splitViewData || [],
                groups: sidebar.groups || [],
                spaces: sidebar.spaces || [],
                isPopup: false,
                isTaskbarTab: false,
                isZenUnsynced: false,
              }] });
              const present = new Set((ZenSessionStore.getSidebarData()?.tabs || []).map(tab => tab.zenSyncId));
              const missing = [...ids].filter(id => !present.has(id));
              done({
                ok: failed.length === 0 && missing.length === 0,
                requested: records.length,
                added: added.length,
                failed,
                missing,
              });
            }).catch(error => done({ ok: false, error: String(error) }));
            """,
            script_args=[records],
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
