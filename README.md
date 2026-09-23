# browser-focus-sync

Move between computers without manually clicking **Sync now**.

An experimental focus-handoff coordinator for **Zen Browser / Twilight on Linux
KDE and macOS**, using SSH (optionally over Tailscale). Linux coordinates; the Mac
agent observes local input activity. On a handoff, tab additions and closures are
transferred in order, with temporary in-process Marionette control.

**This is an extracted working prototype, not a universal browser sync product.**
Chrome, Brave, Opera, Safari and vanilla Firefox are not supported. The browser
adapter calls private Zen APIs; browser upgrades can break it. The source
deployment was exercised with Twilight 1.22/1.23-era builds. The generalized
installer/configuration is new: validate it with disposable profiles first.

## Scope

| Works toward | Does not do |
| --- | --- |
| Open/close handoff between one Linux and one Mac | Arbitrary peers or concurrent multi-writer merging |
| Preserve pending edits across focus changes | Initial automatic union of divergent profiles |
| Resume baselines across browser process changes | Live mirroring of every navigation or scroll position |
| Turn Marionette off after operations | Guarantee cleanup if the process is killed mid-operation |
| Optional native Spaces/folder sync | Password, cookie, extension-state or login migration |

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
migration; this repository is not an in-place updater.

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
after a normal restart; it will **not** automatically restart your browser. On
Mac you can opt into one backed-up restart with `mac.allow_restart = true`, or
repeat the manual bootstrap instead.

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
