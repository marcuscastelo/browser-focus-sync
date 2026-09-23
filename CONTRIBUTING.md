# Contributing

Use synthetic fixtures and mocked control for tests. Never attach a real browser
profile, session file, inventory, log, cookie database, key or token to an issue
or pull request. Keep local configuration outside the checkout.

Run the commands in README's Development section. Any new handoff behavior should
cover additions, legitimate closures, failed delivery, stale acknowledgements,
browser restart, and control cleanup. Unit tests are not a replacement for
opt-in end-to-end tests using disposable profiles on both operating systems.

Do not weaken missing-baseline guards, bypass host-key validation, expose
Marionette on the network, or solve drift by restoring an older session over
current data. Make destructive behavior explicit and back up first.

The public repository is separate from any personal installed deployment. A
source edit must not silently deploy itself, start services, or manipulate a
contributor's browser. Migration/update tooling is future work.
