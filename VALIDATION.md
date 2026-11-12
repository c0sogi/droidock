# Droidock validation

## 0.1.7 screen mirroring - 2026-11-12

- Windows 11, Python 3.12: 354 tests passed in the checkout and again against the installed wheel from outside it.
  Ruff lint and format, isort, Pyright (zero errors), `uv lock --check`, and strict Twine checks passed.
  `scripts/verify_wheel.py` passed with an empty `PATH`, now including mirroring imports without terminal
  packages, a pinned scrcpy archive for this computer, and `mirror`/`scrcpy` help.
- New tests cover the scrcpy installer (verified download and reuse, digest mismatch, archive paths outside the
  folder, tar archives, download retries, a broken managed copy, configured and `PATH` executables with version
  checks, unsupported platforms), starting-quality selection, scrcpy arguments, the link probe against a loopback
  server speaking the ADB smart-socket protocol, monitor thresholds and frame-flow gating, restarts, reconnection,
  start failures, keys, the ADB environment, headset detection, CLI options, and the device list.
- Physical device: Samsung Galaxy XR (Android 14) over wireless debugging on Wi-Fi, from Windows 11.
  - The pinned scrcpy 5.0.1 win64 archive downloaded and matched its published SHA-256 digest.
  - The 7104x3840 side-by-side display and XR features were detected; scrcpy ran with `--crop=3552:3840:0:0`
    and `--no-control`.
  - Within minutes the link measured between 6 and 26 Mb/s with 20-46 ms round trips, and the starting quality
    followed (low, light, balanced, sharp).
  - A quality change restarted scrcpy in about 2.5 s; the new window returned to the position and size the
    previous window had been moved to.
  - With the headset not worn, no frames arrived and ADB round trips rose to 700-900 ms from Wi-Fi power saving
    alone. Before frame-flow gating this lowered the quality falsely; with gating, a 25-second session kept its
    starting quality.
  - Quitting removed the ADB reverse tunnel and the device-side scrcpy server.
- Not verified: the mirrored picture while the headset is worn, a restart caused by real network congestion
  (exercised with test doubles), and mirroring on macOS and Linux.

## 0.1.6 APK deployment - 2026-09-11

- Released on PyPI and GitHub as 0.1.6 from commit `2cd611d`.
- [Release CI](https://github.com/c0sogi/droidock/actions/runs/34493912188): all six jobs passed,
  covering Windows, Linux, and macOS with Python 3.11 and 3.12. Each job passed 296 tests,
  Ruff lint/format, isort, Pyright, and package building.
- Windows installed-wheel validation passed the same 296 tests. Outside the checkout,
  CLI/settings and bundled ADB resolution passed with an empty PATH; core imports do not load CLI modules.
- Both distributions passed strict Twine checks, and their PyPI SHA-256 digests match the reviewed files.
- Added standalone APK installation, verification, launch/stop, interactive install and CLI commands.
- Tests cover selected-device routing, USB/wireless strategies, lost acknowledgements,
  corrupt transfers, installed-hash mismatch, signature/version/parsing rejection without uninstall,
  cleanup failure, launcher errors, path quoting, unreadable APKs, and previews without execution.
- CLI and Python API use the same deployment implementation and existing connection manager.
- Physical-device APK installation was not revalidated for this release.

## 0.1.3 explicit server recovery - 2026-09-10

- Added the interactive restart action, `restart-server` command, and optional server-control API.
- Windows Python 3.11 and 3.12: 218 tests passed on each, including 15 new recovery cases.
- Ruff, formatting, isort, and Pyright passed.
- Tests cover port selection, timeout/failure boundaries, post-start server verification,
  confirmation/cancellation, opt-in reconnection, profile preservation, and partial failures.
- The running shared ADB server was not restarted; physical-device recovery remains unverified.
- Included in release 0.1.3; the profile storage format and existing device IDs are unchanged.

## 0.1.2 library extensions — 2026-09-10

This section records local validation of the 0.1.2 library extensions.
The existing profile storage format and profile IDs are retained.

| Check | Result |
|---|---|
| Windows, Python 3.12.12: `uv run --no-sync pytest -q` | 203 passed, including 57 new cases. |
| Windows, Python 3.11.15: `uv run --isolated --python 3.11 --locked pytest -q` | 203 passed. |
| Ruff, Ruff format, isort, Pyright | Passed; Pyright reported zero errors and warnings. |
| `uv lock --check` | Passed. |
| Source distribution and wheel build with `--no-sources` | Passed. |
| Isolated wheel verifier outside the checkout, empty `PATH` | Passed; bundled ADB resolved. |

New tests exercise unregistered acquisition, caller-defined criteria, saved-address resolution, unavailable
defaults, disabled automatic connection, USB/wireless grouping, conflicting identifiers, nonpersistent use of
unidentified connections, grouped pairing, public command dispatch, alternate addresses, and discovery failure.
They also verify real interface prefix handling using controlled interface data, bounded IPv4 enumeration,
explicit scoped IPv6 queries, DNS address records, provider composition, command output/error contracts,
redaction, and CLI acquisition through the shared library workflow.

The new connection and network cases use test backends, mocked interfaces, and controlled socket responses.
They do not establish physical-device recovery, live unicast mDNS support on a particular device, or APK
installation. Cross-platform CI results are available on the repository's Actions page for the release commit.

## Historical 0.1.1 baseline

Release: **0.1.1**. Date: **2026-09-09**.
Local environment: Windows x64, Python 3.12.12, uv, and adbutils 2.12.0.

### Automated checks for 0.1.1

| Command | Result |
|---|---|
| `uv run pytest -q` | 146 passed. |
| `uv run ruff check .` | Passed, including import ordering. |
| `uv run ruff format --check .` | Passed. |
| `uv run isort --check-only --diff .` | Passed. |
| `uv run pyright` | Zero errors and warnings in source, tests, scripts, and examples. |
| `uv lock --check` | Lock file is current. |
| `uv build --no-sources` | Built the source distribution and the wheel from it. |
| Strict Twine validation | Both release files passed. |
| Isolated wheel installation | Passed from outside the checkout with an empty `PATH`. |

The GitHub workflow runs these checks and builds the package on Windows and Ubuntu with Python 3.11 and 3.12.
Its results are available on the repository's Actions page. Test backends and loopback servers exercise:

- Persistent device IDs, aliases, connection preferences, and default selection across process restarts.
- USB and wireless connections to one physical identity, changing addresses, and rejection of unrelated devices.
- Grouped IPv4/IPv6 advertisements, separate pairing/connection purposes, bounded retries, and fallback addresses.
- Atomic storage, concurrent writes, corrupted data, invalid settings, and unwritable paths.
- English menus, readable tables, JSON output, arrow-key exit, and navigation without repeated discovery.
- Tailscale device enumeration, optional executable configuration, missing installation, and command timeout.
- ADB protocol recognition using actual loopback TCP servers, including fragmented, invalid, and missing replies.
- Port-search concurrency and time limits, progress/ETA, early completion, and cancellation cleanup.
- Selected-peer connection, saved identity verification, and returning to the menu after cancelling a search.

## Package installation

Build and validate the distribution from a source checkout:

```powershell
uv build --no-sources
uvx --from twine twine check --strict dist/droidock-0.1.3-py3-none-any.whl dist/droidock-0.1.3.tar.gz
```

Run `scripts/verify_wheel.py` from outside the checkout with only the built wheel installed:

```powershell
uv run --isolated --no-project --with C:/Projects/droidock/dist/droidock-0.1.3-py3-none-any.whl python C:/Projects/droidock/scripts/verify_wheel.py
```

The verifier checks distribution metadata, the `droidock` entry point, public API imports without terminal
packages, cancelled discovery without socket I/O, English help, persisted settings, and the bundled ADB executable
with an empty `PATH`. The normal check does not restart the shared ADB server. The optional `--cold-start` mode
starts and stops its own temporary server on a separate port.

The package has no local file or workspace-path dependencies. Development environments,
diagnostic captures, saved device profiles, and credentials are excluded from source control and release files.

## Physical-device checks

On 2026-09-09, a Samsung Galaxy XR was enumerated through Tailscale and accessed through ADB using existing PC
authorization. The device returned its physical serial and model through `shell getprop`. The release source was
checked again after the final package naming and successfully inspected that connected device.

The interactive Tailscale device list displayed IPv4 and IPv6 addresses in one row. Searching a known connection
port finished within one second. With no cached ports, the search displayed progress and ETA, found ADB in about
50 seconds, and stopped after 11,713 of 65,534 port checks. Connection and serial verification succeeded, and a
name was saved in a separate validation store. No test profiles were added to the default store.

Ctrl+C during port discovery returned to the device menu, which remained usable for retry and Back/Exit.
An ordinary native terminal run exited with code 0. The PTY command wrapper reported status 1 after an injected
interrupt, so interrupted runs are not counted as exit-code-0 checks.

These physical checks do not establish first-time wireless pairing, USB-driver setup, automatic recovery after
a real port or IP change, or integration with another project's APK deployment flow. Those require separate
end-to-end checks. Exact device identifiers and diagnostic captures remain local.
