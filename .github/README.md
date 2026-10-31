# Operations fork CI

The maintained branch is `current-ops`. The deployment target is Linux CLI and
the Telegram gateway.

- **CI** is intentionally disabled in this fork's GitHub Actions settings.
  If re-enabled, pushes and pull requests run `scripts/ci/run_ops_tests.sh`, Python lint and the
  existing lockfile, repository-boundary and applicable security checks.
- The operations suite covers the fork's install/update channel, stash recovery,
  restart handoff and receipts, reboot approval, Telegram approval buttons and
  credential redaction. It delegates to `scripts/run_tests.sh` for isolation.
- If full CI validation is needed after merging an official release, explicitly
  re-enable **CI** and manually run it on `current-ops` with **full** selected. This includes the full Python suite, Python E2E, macOS and
  Windows tests, JS/TS checks, Desktop core E2E, Rust and documentation checks.
  A failed full run must be investigated before calling the release validated.
- **Nix flake check** and **Install & Update E2E** are manual workflows. The
  latter requires release tags in this repository; it does not fetch or publish
  upstream tags automatically. For routine fork update coverage, use the
  operations suite instead.
- Contributor attribution is not a gate for this fork.
- **Canary Release** is disabled in this fork's GitHub Actions settings and its
  entry job is restricted to the upstream repository. Fork upgrades follow
  `current-ops`; they do not require canary tags or GitHub releases.
- **Desktop Bundled Release**, **Stable Release**, **Stable Release Publication**
  and **Deploy Site** are also disabled in GitHub Actions. Their entry jobs and
  jobs that bypass dependency skipping are restricted to the upstream repository.
  These publishing/deployment workflows are not needed for this fork's Linux
  CLI/gateway deployments.
- Docker's standalone publishing and Sandbox image publishing retain upstream
  repository guards. Build/test workflows are distinct from release publishing.
- The upstream-disabled experimental Desktop E2E remains disabled. The known
  tmux resize/scrollback failure remains visible in manual full Python E2E;
  moving it out of daily CI is not a fix for that failure.

Local verification: `bash scripts/ci/run_ops_tests.sh`.
