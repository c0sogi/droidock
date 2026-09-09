# Droidock validation

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

## Published baseline

Release: **0.1.1**. Date: **2026-09-09**.
Local environment: Windows x64, Python 3.12.12, uv, and adbutils 2.12.0.

## Automated checks

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
uvx --from twine twine check --strict dist/droidock-0.1.1-py3-none-any.whl dist/droidock-0.1.1.tar.gz
```

Run `scripts/verify_wheel.py` from outside the checkout with only the built wheel installed:

```powershell
uv run --isolated --no-project --with C:/Projects/droidock/dist/droidock-0.1.1-py3-none-any.whl python C:/Projects/droidock/scripts/verify_wheel.py
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
