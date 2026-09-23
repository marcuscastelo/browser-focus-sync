# Security and data safety

This is experimental browser automation, not a browser extension or a backup
system. Test with disposable profiles before using your everyday session.

- Marionette has privileged access to the entire browser profile. Bind it to
  loopback only. Never expose port 2828, including over Tailscale. Only SSH should
  cross the network. Other processes running under your account are trusted.
- The injected bridge stays loaded inside the browser, but the Marionette
  listener and automation state should turn off after each operation. If they
  do not, stop the agents and restart the browser normally. A killed Python
  process may prevent cleanup; there is no independent control-lease watchdog yet.
- The coordinator socket lives in a private 0700 directory with mode 0600.
  SSH uses your own keys and host-key verification. Do not disable verification.
- The configured remote command runs through SSH's remote shell. Configuration
  is trusted code: do not copy another person's remote command blindly.
- Tab URLs can contain credentials or private identifiers. Runtime inventories,
  logs, baselines, backups, records, profiles, cookies and keys must stay outside
  the repository. Logs may contain browser errors and sensitive details.
- No code migrates passwords or cookies. Importing a tab does not import its login.
- Initial setup refuses mismatched ID inventories. That is a guard, not proof of
  identical content: the same ID can have different navigation history or URL on
  each device. Review those differences and container mappings yourself.
- Closure propagation intentionally deletes tabs in the other browser. Browser
  restart mismatch suppresses deletion inference for that handoff, but this is
  not a formal conflict-free replication protocol. Take independent backups.
- Native structural sync is off by default. Enabling it uses Mozilla Sync and
  inherits Zen's conflict resolution, including deletions. The tombstone guard
  is not a universal no-data-loss guarantee.

Report vulnerabilities privately through GitHub private vulnerability reporting
if enabled. Otherwise contact the maintainer privately; do not post tokens,
profile dumps, URLs, cookies or session files in a public issue.
