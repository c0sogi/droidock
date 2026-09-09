# Droidock validation

Release: **0.1.0**. Date: **2026-09-09**.
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
uvx --from twine twine check --strict dist/droidock-0.1.0-py3-none-any.whl dist/droidock-0.1.0.tar.gz
```

Run `scripts/verify_wheel.py` from outside the checkout with only the built wheel installed:

```powershell
uv run --isolated --no-project --with C:/Projects/droidock/dist/droidock-0.1.0-py3-none-any.whl python C:/Projects/droidock/scripts/verify_wheel.py
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
