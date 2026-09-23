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
    parser.add_argument("--port", type=int, default=2828)
    parser.add_argument("--reason", default="focus-handoff")
    parser.add_argument("--if-spaces-pending", action="store_true")
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--max-tombstones", type=int, default=25)
    parser.add_argument("--allow-tombstones-file")
    args = parser.parse_args()
    if not args.inspect and not cfg.get("sync", "allow_native_structure_sync", False):
        print(json.dumps({"ok": False, "error": "native structural sync is disabled in configuration"}))
        return 1

    allowed_tombstones: list[str] = []
    if args.allow_tombstones_file:
        try:
            payload = json.loads(Path(args.allow_tombstones_file).read_text())
            if not isinstance(payload, list) or not all(isinstance(item, str) for item in payload):
                raise ValueError("expected a JSON array of record IDs")
            allowed_tombstones = payload
        except (OSError, ValueError, json.JSONDecodeError) as error:
            print(json.dumps({"ok": False, "error": f"invalid tombstone authorization: {error}"}))
            return 1

    client = Marionette(host="127.0.0.1", port=args.port, socket_timeout=120)
    try:
        client.start_session()
        client.set_context(client.CONTEXT_CHROME)
        client.timeout.script = 90
        result = client.execute_async_script(
            """
              const done = arguments[arguments.length - 1];
              const [reason, onlyIfSpacesPending, inspect, maxTombstones, allowedTombstones] = arguments;
              const accountConfigPrefs = [
                "identity.fxaccounts.remote.root",
                "identity.fxaccounts.auth.uri",
                "identity.fxaccounts.remote.oauth.uri",
                "identity.fxaccounts.remote.profile.uri",
                "identity.fxaccounts.remote.pairing.uri",
                "identity.sync.tokenserver.uri",
              ];
              const invalidAccountConfig = accountConfigPrefs.some(pref =>
                Services.prefs.prefHasUserValue(pref) &&
                Services.prefs.getStringPref(pref).includes("{server}")
              );
              if (invalidAccountConfig) {
                const { FxAccountsConfig } = ChromeUtils.importESModule(
                  "resource://gre/modules/FxAccountsConfig.sys.mjs"
                );
                FxAccountsConfig.resetConfigURLs();
              }
              const authUriPref = "identity.fxaccounts.auth.uri";
              const { Weave } = ChromeUtils.importESModule(
                "resource://services-sync/main.sys.mjs"
              );
              const { ZenSpacesSyncModel } = ChromeUtils.importESModule(
                "resource:///modules/zen/ZenSpacesSyncModel.sys.mjs"
              );
              const { ZenSessionStore } = ChromeUtils.importESModule(
                "resource:///modules/zen/ZenSessionManager.sys.mjs"
              );
              const counts = () => {
                const sidebar = ZenSessionStore.getSidebarData() || {};
                return {
                  tabs: sidebar.tabs?.length || 0,
                  spaces: sidebar.spaces?.length || 0,
                  folders: sidebar.folders?.length || 0,
                  groups: sidebar.groups?.length || 0,
                  records: Object.keys(ZenSpacesSyncModel.getAllRecordIds()).length,
                  syncLogin: String(Weave.Status.login),
                  syncService: String(Weave.Status.service),
                  syncSpaces: String(Weave.Status.engines?.spaces ?? "success.status_ok"),
                  syncLocked: Weave.Service.locked,
                  authUri: Services.prefs.getStringPref(authUriPref),
                  spacesEnabled: Weave.Service.engineManager.get("spaces")?.enabled ?? false,
                };
              };
              const syncResult = extra => ({
                ok: !String(Weave.Status.login).startsWith("error.") &&
                  String(Weave.Status.service) === "success.status_ok" &&
                  !String(Weave.Status.engines?.spaces ?? "").startsWith("error."),
                ...extra,
                ...counts(),
              });
              if (inspect) {
                ZenSpacesSyncModel.invalidate();
                const sidebar = ZenSessionStore.getSidebarData() || {};
                const changes = ZenSpacesSyncModel.computeChangedIDs();
                done({
                  ok: true,
                  ...counts(),
                  tabIds: (sidebar.tabs || [])
                    .map(tab => tab.zenSyncId)
                    .filter(Boolean),
                  tombstoneIds: Object.keys(changes).filter(
                    id => !ZenSpacesSyncModel.projectRecord(id)
                  ),
                });
                return;
              }
              const finish = result => {
                Services.prefs.setBoolPref("services.sync.engine.spaces", false);
                Weave.Service.engineManager.decline(["spaces"]);
                done(result);
              };
              const enableSpaces = async () => {
                Weave.Service.engineManager.undecline(["spaces"]);
                Services.prefs.setBoolPref("services.sync.engine.spaces", true);
                for (let attempt = 0; attempt < 40; attempt++) {
                  if (Weave.Service.engineManager.get("spaces")?.enabled) {
                    return;
                  }
                  await new Promise(resolve => setTimeout(resolve, 25));
                }
                throw new Error("Spaces engine did not enable");
              };
              const waitForUnlocked = async () => {
                for (let attempt = 0; attempt < 1200; attempt++) {
                  if (!Weave.Service.locked) {
                    return;
                  }
                  await new Promise(resolve => setTimeout(resolve, 50));
                }
                throw new Error("Firefox Sync remained busy for 60 seconds");
              };
              const runSpacesSync = async () => {
                await waitForUnlocked();
                await enableSpaces();
                await Weave.Service.sync({ engines: ["spaces"], why: reason });
              };
              // Focus handoff must read the browser state that exists now,
              // not a projection cached before the user opened or closed tabs.
              ZenSpacesSyncModel.invalidate();
              if (onlyIfSpacesPending) {
                if (!ZenSpacesSyncModel.hasPendingChanges()) {
                  finish({ ok: true, skipped: true, ...counts() });
                  return;
                }
              }
              const changes = ZenSpacesSyncModel.computeChangedIDs();
              const tombstones = Object.keys(changes).filter(
                id => !ZenSpacesSyncModel.projectRecord(id)
              );
              const allowed = new Set(allowedTombstones);
              const exactAuthorization = tombstones.length > 0 &&
                tombstones.every(id => allowed.has(id));
              if (tombstones.length > maxTombstones && !exactAuthorization) {
                finish({
                  ok: false,
                  error: `refusing ${tombstones.length} automatic deletions`,
                  tombstones: tombstones.length,
                  authorizedTombstones: allowed.size,
                  ...counts(),
                });
                return;
              }
              runSpacesSync().then(
                () => finish(syncResult({
                  pending: ZenSpacesSyncModel.hasPendingChanges(),
                  tombstones: tombstones.length,
                })),
                error => finish({ ok: false, error: String(error), ...counts() })
              );
            """,
            script_args=[
                args.reason,
                args.if_spaces_pending,
                args.inspect,
                args.max_tombstones,
                allowed_tombstones,
            ],
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
