# Operations fork CI

The maintained branch is `current-ops`. The deployment target is Linux CLI and
the Telegram gateway.

- Pushes and pull requests run `scripts/ci/run_ops_tests.sh`, Python lint and the
  existing lockfile, repository-boundary and applicable security checks.
- The operations suite covers the fork's install/update channel, stash recovery,
  restart handoff and receipts, reboot approval, Telegram approval buttons and
  credential redaction. It delegates to `scripts/run_tests.sh` for isolation.
- After merging an official release, manually run **CI** on `current-ops` with
  **full** selected. This includes the full Python suite, Python E2E, macOS and
  Windows tests, JS/TS checks, Desktop core E2E, Rust and documentation checks.
  A failed full run must be investigated before calling the release validated.
- **Nix flake check** and **Install & Update E2E** are manual workflows. The
  latter requires release tags in this repository; it does not fetch or publish
  upstream tags automatically. For routine fork update coverage, use the
  operations suite instead.
- Contributor attribution is not a gate for this fork. Official publishing,
  website deployment and bot auto-merge jobs retain their upstream restrictions.
- The upstream-disabled experimental Desktop E2E remains disabled. The known
  tmux resize/scrollback failure remains visible in manual full Python E2E;
  moving it out of daily CI is not a fix for that failure.

Local verification: `bash scripts/ci/run_ops_tests.sh`.
