# browser-focus-sync

Move between computers without manually clicking **Sync now**.

An experimental focus-handoff coordinator for **Zen Browser / Twilight on Linux
KDE and macOS**, using SSH (optionally over Tailscale). Linux coordinates; the Mac
agent observes local input activity. On a handoff, tab additions and closures and
the sidebar structure (Spaces, folders, split views, order, pinning, a tab's place
and URL) are transferred, with temporary in-process Marionette control.

**This is an extracted working prototype, not a universal browser sync product.**
Chrome, Brave, Opera, Safari and vanilla Firefox are not supported. The browser
adapter calls private Zen APIs; browser upgrades can break it. The source
deployment was exercised with Twilight 1.22/1.23-era builds. The generalized
installer/configuration is new: validate it with disposable profiles first.

## Scope

| Works toward | Does not do |
| --- | --- |
| Legacy handoff between one Linux and one Mac; opt-in three-participant protocol below | Arbitrary peers or conflict-free concurrent merging |
| Preserve pending edits across focus changes | Initial automatic union of divergent profiles |
| Resume baselines across browser process changes | Live mirroring of navigation history or scroll position |
| Turn Marionette off after operations | Guarantee cleanup if the process is killed mid-operation |
| Spaces, folders, split views, order and tab placement | Password, cookie, extension-state or login migration |

"Focus" means recent **device input**, not the frontmost browser window. The
current defaults are 30 seconds of Linux inactivity and 8 seconds on macOS.
There is polling and SSH latency; this is not instantaneous tab mirroring.

## Safety first

1. Back up both complete profiles while the browsers are closed. Backups contain
   credentials and browsing history: keep them private and outside the checkout.
2. Use explicit profile paths from `about:profiles`. Only one relevant browser
   process/profile per host is supported. Do not point at the wrong channel.
3. Both profiles must already share tab, Space, folder and container identities.
   Same-looking tabs with different IDs are **not** an aligned baseline. The
   installer does not import profiles or fix drift; it refuses to seed mismatched
   inventories.
4. Never run this alongside another focus-sync daemon managing the same profiles.
   Do not copy old runtime state or restore an old session over newer user edits.
5. Read [SECURITY.md](SECURITY.md). No system can promise zero data loss here.

Native structural sync and automatic Mac restarts are **off by default** in the
public configuration. Tab handoffs still work without native structural sync;
create matching Spaces/containers/folders before relying on them. If you opt
into structural sync, sign in to the same Mozilla account on both devices and
accept native Zen conflict-resolution behavior.

## Requirements

- Python 3.11+ and the dependencies in `requirements.txt` on both hosts.
- Linux: KDE idle events via Qt 6 + KF6 IdleTime, a C++ compiler, and CMake.
  On Arch, the build packages are `base-devel cmake extra-cmake-modules qt6-base kidletime`.
- macOS: a GUI login session, Twilight installed, and permission for Terminal/the
  agent to control Twilight if opting into automated restarts.
- Mac → Linux SSH key authentication, including a verified SSH host key.
  Tailscale supplies connectivity only; it does not replace SSH authentication.
- Port 2828 available on loopback only; no other automation competing for it.

## Install (on each machine)

```sh
git clone https://github.com/marcuscastelo/browser-focus-sync.git
cd browser-focus-sync
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python manage.py init-config
```

Edit `~/.config/browser-focus-sync/config.toml`. Set the local profile path and
browser executable; on the Mac set `linux_host` to your SSH alias. The remote
command defaults to the Linux installation's default paths. If Linux uses a
custom config location, prefix `remote_ctl` with
`env BROWSER_FOCUS_SYNC_CONFIG=/absolute/path/to/config.toml`.

```sh
.venv/bin/python manage.py doctor
.venv/bin/python manage.py install --dry-run
.venv/bin/python manage.py install
```

Installation copies **only an allowlist of source files**, creates a separate
venv, builds the Linux idle helper, and writes a user-service definition. It
**does not start services, restart browsers, modify browser preferences or create
baselines**. It refuses existing installation/service destinations. For an
existing personal deployment, keep it running until you schedule a separate
migration; the `install` command is not an in-place updater.

An existing deployment can instead run the pinned `runtime/` sources from its
existing Python environment. Set `[paths] data_dir` and `state_dir` to its
current state directories, and set `socket_path` to its existing coordinator
socket. Preserve its baselines and browser profile. Stop both old agents before
changing their service definitions; restart them with the new configuration and
verify a test handoff in both directions. Never seed a fresh baseline during an
in-place migration.

An interrupted install may leave a partial destination. Inspect it and move it
aside (rather than blindly deleting it) before retrying. Keep runtime data out
of the Git checkout.

## Prepare the browsers

In the selected profiles' `about:config`, set:

```text
services.sync.engine.spaces = false
zen.spaces-sync.normal-tabs = true
remote.prefs.recommended = false
```

The first prevents an unscheduled native Spaces merge from competing with this
coordinator. The last prevents Marionette's recommended test preferences from
changing normal browsing behavior. These are browser preferences, not settings
in the project's TOML file. Remember their previous values if you want to undo
the setup later.

For the **initial control bridge**, fully quit the selected browser yourself,
then launch it with your actual executable and profile:

```sh
# Linux example; replace the profile path.
/opt/zen-twilight-bin/zen-bin --profile '/absolute/path/to/profile' \
  --restore-last-session --marionette --remote-allow-system-access

# macOS example; replace the profile path.
open -a Twilight --args --profile '/absolute/path/to/profile' \
  --restore-last-session --marionette --remote-allow-system-access
```

Immediately run this in another terminal on that machine:

```sh
.venv/bin/python manage.py bootstrap-control
```

It installs the in-process bridge and turns Marionette off. It must report
success; do not leave the browser in automation mode if setup fails. Browser
restarts remove the injected bridge. On Linux repeat the explicit bootstrap
after a normal restart; it will **not** automatically restart your browser.
After updating the bridge script, swap it into the running browser with
`.venv/bin/python manage.py reinstall-bridge`; it takes the lease, so it waits for
a handoff in progress.

On Mac the agent never restarts the browser on its own either:
- When the browser restarts itself (an update, `about:restart`) after being opened
  with the flags above, it inherits `MOZ_MARIONETTE` and
  `MOZ_REMOTE_ALLOW_SYSTEM_ACCESS`; the agent installs the bridge in place and turns
  Marionette off within seconds.
- When it was opened without them (Dock, a link, after login), nothing can install
  the bridge without a restart. The first handoff that needs control shows one
  notification per browser process and waits. Restart it when convenient with
  `.venv/bin/python runtime/mac_agent.py reopen`: the running agent quits and reopens
  the browser between handoffs, with a session snapshot and a check that every tab
  came back, and prints the result. Set `mac.reopen_command` to the command shown in
  the notification.
- `mac.allow_restart = true` opts back into one backed-up restart during a handoff.

## Establish an initial baseline

Keep both browsers open and don't edit tabs during this step. Services must still
be stopped. Take ID-only inventories, one per machine, and exchange them through
SSH. Write them somewhere private outside the repository:

```sh
.venv/bin/python manage.py inventory --out /absolute/private/path/local-inventory.json
# Copy the other machine's inventory here as peer-inventory.json using scp.
.venv/bin/python manage.py baseline \
  --peer-inventory /absolute/private/path/peer-inventory.json \
  --confirm-backed-up-and-aligned
```

Run the baseline command on **both** machines using each other's inventory. It
requires equal tab/Space/folder ID sets, and refuses to overwrite existing
baselines or coordinator state. It does not compare every page's content or
container mapping. Check those separately. If it reports drift, stop here: keep
both backups and reconcile intentionally. There is no safe "force baseline"
switch.

## Start / stop

Linux:

```sh
systemctl --user daemon-reload
systemctl --user enable --now browser-focus-sync.service
# Stop:
systemctl --user disable --now browser-focus-sync.service
```

macOS:

```sh
launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/io.github.browser-focus-sync.plist"
# Stop:
launchctl bootout "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/io.github.browser-focus-sync.plist"
```

After starting both, create one disposable tab on Linux, switch input to the Mac,
and verify it arrives. Close that test tab and return to Linux; verify the closure.
Repeat in reverse. Do not test by closing valuable tabs.

Status on Linux (default installation):

```sh
~/.local/share/browser-focus-sync/venv/bin/python \
  ~/.local/share/browser-focus-sync/runtime/focusctl.py status
journalctl --user -u browser-focus-sync.service -n 30
```

Mac logs: `~/.local/share/browser-focus-sync/agent.log`. Redact logs before sharing.

Chrome control sessions on macOS use Gecko's native `moz:windowless` capability
and attach to the most recent browser window with `focus: false`. Ordinary
Marionette session initialization calls `window.focus()` even for read-only
scripts. This policy covers the bridge installer, structural handoffs and the
shared client used by browser tools. An explicitly requested focus operation
inside a script still uses normal focus behavior.

Session metadata (time, caller basename, PID, platform and port; no arguments,
URLs or scripts) is recorded in
`~/.local/state/browser-focus-sync/control-sessions.jsonl`, rotating at 1 MB.
The headless macOS regression is `tests/background_browser.py RUNTIME EXECUTABLE`
(run with the project's Python environment); it creates and removes its own
profile and port. It measures native focus calls, checks both clients and
cleanup after errors. Never run that test against an existing browser profile.

Gecko rejects `moz:windowless` on Linux. Ordinary session startup can also take
focus on KWin. `focusmanager.testmode` is unsuitable as a replacement: emulated
activation leaves the internal active window different from the compositor,
breaking subsequent explicit `window.focus()`. This Linux limitation remains
unresolved; do not enable that preference in the user's browser as a workaround.
For a full removal, stop/unload both agents, restart both browsers normally to
remove the in-process bridges, and restore the three preferences you changed.
Keep backups/state until you have verified your sessions; removal is deliberately
not an automatic destructive command.

## Design and limitations

- Linux owns the coordinator socket and handoff generation. Mac input transitions
  request handoffs through SSH. Stale acknowledgements are rejected.
- Baselines represent acknowledged tab IDs, not just a browser PID. A PID change
  allows additions but suppresses deletion inference until a successful handoff.
- Mac refocus while it already owns the handoff does not reset its baseline and
  silently consume pending edits.
- Tab operations use Zen's private model/applier modules. Temporary control is
  released in `finally`; the low-frequency bridge timer remains resident.
- Other programs that need the browser (route appliers, a tab MCP) use
  `runtime/bridge_client.py`, standard library only and Python 3.9 compatible.
  Its `Lease` holds an exclusive lock on `<data_dir>/<platform>-control.lock`
  from acquire to release, turns Marionette on only when it was off, and turns it
  back off only if the request is still its own. It never restarts the browser
  or installs a bridge: without a bridge it refuses (`bridge_unavailable`), and a
  restart stays a separate path in `mac_agent`. While held,
  `<platform>-control.holder.json` names the holder (`pid`, `label`, `since` in ms);
  `bridge_client.holder()` reads it for diagnostics.
- Structure travels as Zen's own sidebar records (`ZenSpacesSyncModel`
  projections, applied with `ZenSpacesSyncApplier`) compared against each side's
  digests from the last handoff (`runtime/structure.py`,
  `runtime/structure_records.py`); Mozilla Sync and its server state are not used.
  After a browser restart only new records travel. Only Spaces, folders and split
  views are deleted as structure, and a receiver keeps a Space or folder that
  still holds tabs the handoff does not close. An update for something the
  receiver no longer has is skipped, not recreated. On a simultaneous edit of the
  same record the Mac's version wins. A changed URL only retargets an unloaded
  tab; nothing is loaded and a loaded tab is never navigated.
- Native Spaces sync is separate and opt-in; bulk deletion guards do not make it
  a conflict-free merge engine. Simultaneous editing and large handoff payloads
  still need hardening. Missing/corrupt baselines fail closed, not auto-reseeded.
- No initial merge tool, URL/history conflict resolver, upgrade installer,
  independent control watchdog, or multi-profile router is included yet.

## Development

```sh
PYTHONPATH=runtime .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q runtime manage.py tests
cmake -S native -B build
cmake --build build
```

Tests use synthetic data and mocks; CI never opens real browsers or contacts your
SSH hosts. The extracted source's real-machine tests are not equivalent to
cross-version end-to-end coverage of this new configurable distribution.

Source layout: `runtime/` agents and Zen adapter, `native/` KDE idle helper,
`manage.py` explicit setup, `tests/` safety regressions. No personal profile,
session, cookie, hostname, backup, credential or historical one-off recovery
script is part of this repository.

MIT licensed. Mozilla/Zen and the Python dependencies are separate projects with
their own licenses; their source is not vendored here.

## Opt-in three-participant runtime (protocol 2)

Protocol 2 uses **one Sirius coordinator** and two instances of the existing
`mac_agent.py`: Polaris uses the Mac adapter; workstation uses `linux_control`.
It polls every ten seconds, including while idle, so offline edits are exported
before queued remote edits are received. This is serialized replication through
the existing export/apply bridge, rather than exclusive focus ownership. No new
module, native Spaces Sync or second Linux coordinator is involved.

All three configurations must set:

```toml
[sync]
multiparticipant = true
participant_id = "sirius" # polaris or workstation on the corresponding host
allow_native_structure_sync = false
```

Keep `services.sync.engine.spaces = false` in all browsers. Each host retains its
own private data/state directories and explicit local profile/executable section:
`[linux]` for Sirius/workstation, `[mac]` for Polaris. Both agents currently read
SSH `linux_host` and `remote_ctl` from `[mac]`, including workstation; these name
the **same Sirius** protocol client. No real host aliases belong in this repo.
The existing installer does not select the workstation role: configure its
launcher separately, after validating disposable profiles. Do not start the
installer's Linux coordinator there.

Initialization is explicit, with all daemons stopped and edits paused:

1. Back up all three complete profiles and existing runtime state privately.
2. Preserve the established Sirius/Polaris baseline. Align workstation IDs and
   full record digests intentionally; there is no automatic union or force seed.
   Each local baseline must have `tabIds`, `identity` and `records` digests. A
   legacy baseline without digests must be reconciled before enabling protocol 2.
   `linux-active-baseline.json` remains Sirius's baseline;
   `active-baseline.json` remains each agent's baseline. Never copy another
   process's identity into a baseline or reseed to bypass an error.
3. Bootstrap the existing control bridge on each disposable browser as described
   above. Start only `runtime/coordinator.py` on Sirius with protocol 2 enabled.
4. Run `runtime/mac_agent.py enroll` locally on each remote participant. Enrollment
   refuses existing participants and requires exact baseline IDs and digests
   equal to Sirius's baseline. Keep edits paused until both enrollments finish.
5. Start `runtime/mac_agent.py` once on Polaris and once on workstation. Each
   daemon holds a lifetime exclusive lock in its configured profile, in addition
   to the existing operation leases. All daemons must use this version for that
   lock to protect against duplicate roles. Old binaries cannot honor the lock.

Protocol messages have `protocol: 2`, `participantId`, `event` and `requestId`.
The coordinator rejects legacy mutation events in this mode. `focusctl.py -`
accepts JSON through stdin over the existing SSH route; `status` reports enrolled
participants and an uncertain-application block. Protocol 1 remains the default
when `multiparticipant` is absent/false, using its existing state files.

Sirius exports local changes before applying a peer's changes under one global
lock/lease. It queues both sets of changes for other participants. It keeps the
exact reply until an explicit participant ACK; retries with the same request ID
return that reply, and reusing an ID with different content is rejected. ACKs
carry the participant's observed baseline. Closures are restricted to its
acknowledged IDs; browser identity changes suppress inferred deletions and
updates of existing records. Third-party deliveries update that participant's
baseline when applied, so they remain visible to subsequent comparisons.
Concurrent edits to distinct records survive; for the same record, the later
serialized peer edit wins over older queued edits. Existing applier rules still
apply (loaded tabs are not navigated; updates do not resurrect absent records;
nonempty folders/Spaces may be retained). This is not a CRDT or history sync.

Failures do not acknowledge a transfer. `participant-pending.json` retains the
exact exported request or pending ACK across agent restarts. Before applying,
Sirius writes `participant-apply-intent.json`; an agent receiving changes writes
`participant-receive-intent.json`. A failed/partial apply, baseline persistence
failure or crash in that interval blocks further inference. **Do not delete an
intent merely to retry.** Stop all daemons, preserve state/profile backups,
inspect the saved request/reply and actual resulting inventories/digests, and
reconcile the affected baselines and queues explicitly before clearing the
intent. There is deliberately no automated reseed/recovery command. Atomic file
replacement covers process interruption; power-loss durability is not guaranteed.
Queues have no retention limit; a long-offline peer may exceed the existing
16 MiB transport limit and require offline reconciliation. Sirius must be online
for replication; real-browser cross-version validation remains required.

Reversible rollout: validate in three disposable profiles first (addition,
closure and folder move in each direction, offline edits, lost reply/ACK and
restart); stop all old daemons before switching launch commands. Keep the pinned
old version and original state untouched as rollback evidence. To roll back,
stop all three protocol-2 processes, back up their current profiles/state, drain
or explicitly reconcile pending changes, and restore the pair's launch commands
with protocol 2 disabled. Do not restore stale profiles/baselines over newer
edits. Leave workstation stopped until its divergence is reconciled. This source
change installs or starts nothing on real machines.

### Exact Linux role configuration and bootstrap commands

Example **Sirius** config (replace paths outside Git):

```toml
[paths]
data_dir = "/absolute/private/sirius-data"
state_dir = "/absolute/private/sirius-state"
socket_path = "/absolute/private/sirius-state/coordinator.sock"
[linux]
executable = "/absolute/browser/zen-bin"
profile = "/absolute/private/sirius-profile"
[sync]
multiparticipant = true
participant_id = "sirius"
allow_native_structure_sync = false
```

Example **workstation** config:

```toml
[paths]
data_dir = "/absolute/private/workstation-data"
state_dir = "/absolute/private/workstation-state"
[linux]
executable = "/absolute/browser/zen-bin"
profile = "/absolute/private/workstation-profile"
[mac] # SSH transport settings are shared with the existing Mac agent
linux_host = "coordinator-ssh-alias"
remote_ctl = "env BROWSER_FOCUS_SYNC_CONFIG=/absolute/private/sirius.toml /absolute/venv/bin/python /absolute/source/runtime/focusctl.py"
[sync]
multiparticipant = true
participant_id = "workstation"
allow_native_structure_sync = false
```

Polaris uses its existing `[mac]` profile/executable/transport, private paths and
`participant_id = "polaris"`. Set `BROWSER_FOCUS_SYNC_CONFIG` in every service's
environment; use absolute paths for Python and source. The source can run with
an existing venv containing the pinned requirements. In the following commands,
`$BFS_PYTHON` and `$BFS_SOURCE` are those paths, and the config environment is
already set for the local host:

```sh
# Each Linux browser: manually launch selected profile with the documented flags.
"$BFS_PYTHON" "$BFS_SOURCE/manage.py" --platform linux bootstrap-control
# Polaris uses --platform mac instead.

# All hosts, with daemons stopped and profiles intentionally aligned:
umask 077
"$BFS_PYTHON" "$BFS_SOURCE/runtime/mac_agent.py" snapshot > /private/local-snapshot.json
# Exchange ID/digest snapshots privately; compare all three, no edits meanwhile.
# ONLY for a host with NO baseline/state: explicitly create its first baseline.
"$BFS_PYTHON" "$BFS_SOURCE/runtime/mac_agent.py" initialize /private/peer-snapshot.json

# Sirius only (service ExecStart):
"$BFS_PYTHON" "$BFS_SOURCE/runtime/coordinator.py"
# Then once on each remote, before starting its service:
"$BFS_PYTHON" "$BFS_SOURCE/runtime/mac_agent.py" enroll
# Polaris and workstation only (their separate service ExecStart):
"$BFS_PYTHON" "$BFS_SOURCE/runtime/mac_agent.py"
```

`snapshot` inspects live IDs/digests using the existing lease. `initialize`
compares them exactly to the private peer snapshot, writes the role's baseline,
and refuses any existing baseline, pending transfer, intent or coordinator state.
For an established Sirius/Polaris deployment, **do not run initialize**: preserve
and inspect their acknowledged baselines. If they lack digests or are divergent,
stop for explicit reconciliation; initialization cannot upgrade them silently.

A formatted/fresh machine first needs its identities and structure restored,
using the existing `export_tab_records.py` and `structure_records.py export/apply`
under the existing control lease, or a deliberately aligned profile restoration.
`initialize` and `enroll` only validate alignment; they do not restore structure.
An export/apply payload is private browser data and must never enter Git. Verify
live snapshots after restoration and before creating a genuinely absent baseline.
There is no automatic fresh-machine restore command in this runtime; existing
baselines always take the refusal path, even after formatting/reinstalling the
browser. Stop rather than using initialization to acknowledge a divergent session.

### Bridge lifetime on current Gecko

The original window timer remains in use. Both adapters omit the obsolete
`RecommendedPreferences.restoreAllPreferences()` call: current Gecko removed it,
and its exception prevented the off status from being written after Marionette
had already stopped. `remote.prefs.recommended=false` prevents automation prefs
from being applied. Temporary control must stop both the listener and
`webdriverActive` after each operation. `tests/bridge_lifetime.py runtime` is a
manual Linux proof against a fresh headless Twilight: it installs the real bridge,
ends the installer session, and checks three acquire/release cycles.

`tests/three_browser.py runtime` exercises the real coordinator and participant
cycle against three disposable Linux browsers, adapting only host/port/process
boundaries. It tests additions from every host, closure and an offline pending
request followed by concurrent additions. Set `BFS_LAB_ROOT` to a private disk
directory if the system temporary directory is a small tmpfs. Neither test opens
a real user profile. They do not replace cross-platform rollout validation.

For a formatted machine whose participant ID already exists at the coordinator,
restore and align first, initialize its genuinely absent local baseline, then run
`mac_agent.py reenroll` instead of `enroll`. This explicit recovery requires IDs
and record digests exactly equal to the coordinator baseline, refuses uncertain
apply state, and archives the previous peer queue/reply privately before replacing
it. It does not import data or waive alignment, and never runs automatically.
Keep another participant online to flush pending coordinator edits before comparing
snapshots; an unacknowledged local change is not an aligned recovery baseline.

Protocol-2 tab inventories use Zen's projected page identities: native folder
placeholders, Glance, live-folder virtual items and empty surfaces without a page
identity remain local. They are preserved, never inferred as shared closures.
Their IDs differ between hosts and current browser versions, so comparing them
as shared pages would create false closures and block enrollment. Legacy
protocol-1 inventories and its blank-tab exporter keep their previous behavior.
Initial alignment compares projected IDs and digests, never these local UI IDs.

Quiet protocol-2 polls compare stored fingerprints and IDs and exchange empty
ACKs without creating a browser-control session. Empty batches are not fanned
out. Incoming mutations or a changed local snapshot still use the same lease and
live exporter. A fingerprint changed during collection is never cached as
acknowledged, so later local edits remain pending for the next cycle.

The quiet change detector observes Zen's saved session file; native UI saves are
asynchronous. The disposable transport test explicitly flushes the native
snapshot after mutations, so its bounded cycles test replication rather than
the browser's save timer. Changes made while a request is offline remain pending
even if the saved file has not caught up yet; receive-side live comparisons keep
them protected.

Protocol-2 record digests omit ordinary fetched tab favicons (the native applier
keeps a loaded page's cache) while retaining static user icons. Exported records
still carry the favicon for best-effort rendering. Parent child-order records
include only IDs that have native projections, so local empty surfaces do not
make otherwise identical shared trees appear different across browser versions.
Workspace/folder icons, page identity, pinning, placement and shared order remain
part of the digest. Protocol-1 records and digests are unchanged.
