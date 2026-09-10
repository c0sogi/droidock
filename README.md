# Droidock

A Python library and interactive CLI that remembers Android connection settings on your PC.
It groups USB and wireless connections by device serial number, discovers changing addresses,
and verifies the device identity before updating a saved profile.

```powershell
uv tool install droidock
droidock
```

To run without a persistent CLI installation, use `uvx droidock`. To use the Python API in another project,
run `uv add droidock`, then `from droidock import ConnectionManager`.

The package prefers the ADB executable included in `adbutils`, so a separate Android SDK or ADB installation
is not required on supported platforms. If no bundled executable is available for your OS, configure an ADB
path or provide ADB on `PATH`. The distribution has been verified on Windows x64.

## First connection

Use Tab to switch tables, the arrow keys to select a row, and Enter to open its actions.
Press A to add a device or F2 for settings and other tools. For a numbered menu, run `uv run droidock --plain`.

The overview scans once on startup and keeps that snapshot while you browse menus or edit saved preferences.
Press R/F5 or select **Scan again** to refresh it. Opening **Add a device** reuses that snapshot; its address
picker also offers **Scan again**. Selecting a wireless service tries its displayed addresses first. If they
fail, Droidock discovers that same service again and retries changed addresses. Successful pairing refreshes
discovery to find the connection port. Registration and connection actions refresh ADB connection status.
**Back** and **Exit** do not start discovery. Use `uv run droidock --version` to check the
installed version; restart an already running CLI after updating it.

Nearby services can advertise both IPv4 and IPv6 addresses. The table and selection menu show one entry per
service name and purpose, with all its addresses retained. Selecting a wireless connection tries IPv4 first,
then another advertised address if the connection fails (up to three addresses). A discovery entry is not yet a
saved device: registration still requires a responding device with a verified serial number.

1. Select **Add a device**.
2. For USB, enable USB debugging on the device and authorize this PC before registering it.
3. For wireless access, join the same network, enable **Wireless debugging**, and open
   **Pair device with pairing code** on the device.
4. Select the pairing address in the CLI and enter the six-digit code. The code is hidden and is not saved.
5. After pairing, connect to the device's **current connection address**. The pairing port and connection port differ.
6. Give the device a name. The first registered device becomes the default, with automatic connection enabled.

The interactive menu attempts to reconnect saved devices on subsequent launches. To keep checking connections
while the program runs:

```powershell
uv run droidock watch
```

`watch` periodically discovers and reconnects enabled devices until you stop it. It does not install an OS startup
entry or background service. Change per-device preferences under **Manage saved devices**, and startup behavior
under **Connection settings**. Disconnecting a saved device also disables its automatic connection preference.
These settings control this tool's connection attempts; other applications and a shared ADB server can manage
connections independently.

## Device overview

The interactive overview and `droidock devices` always show two tables, including when they are empty:

**📱 Devices** uses a cyan bordered table; **📡 Wireless services** uses magenta headings and horizontal
rules. The icons and different borders distinguish the tables even without color.

- **Devices** combines saved profiles and detected ADB connections. It shows the device name and serial,
  whether it is saved, its connection status, USB or wireless addresses, and automatic connection preference.
  Matching USB and wireless connections share one row. Conflicting identities remain separate. Connections
  whose identity cannot be read show **Unverified** in the Saved column.
- **Wireless services** lists discovered connection and pairing services, grouping their IPv4 and IPv6
  addresses. A service shows **🟢 Connected** only when a responding wireless ADB connection matches its
  current address or full ADB service name. Otherwise it shows **🔎 Discovered**, which does not establish
  whether a connection exists. USB connections, remembered addresses, and serial-like text in service names
  do not mark a wireless service connected. Pairing services remain Discovered.

Device states include **🟢 Connected**, **🟡 Authorization required**, and **⚪ Not connected**. Status text
is always included alongside the icon. Saved devices remain visible when offline, with historical addresses
labeled **Last wireless**. Rendering these tables does not trigger discovery, connect devices, or save profiles.
Use **Scan again** to refresh the view and **Add a device** to register a device. JSON output is unchanged.

In a supported terminal, both tables are selectable and scroll independently within a separate terminal screen.
The selected row's serial, connection details, or service addresses appear below the tables. Long rows are
shortened to fit the window; Enter opens their actions and details. The column headings and controls stay visible
while scrolling, and the tables resize with the terminal.

| Key | Action |
| --- | --- |
| Tab / Shift+Tab | Switch between Devices and Wireless services. |
| Up / Down | Select the previous or next row. |
| Page Up / Page Down | Move by one visible page. |
| Home / End | Select the first or last row. |
| Enter | Manage a saved device, save an unregistered connection, or connect/pair a service. |
| R / F5 | Scan again. |
| A | Add a device, including manual addresses. |
| F2 | Open settings, diagnostics, Tailscale, and other actions. |
| Q / Esc / Ctrl+C | Exit. |

Mouse clicks select rows and the mouse wheel scrolls the table under the pointer in supported terminals.
Returning to the overview clears
the previous page, so repeated scans and actions do not accumulate in terminal history. Results such as
connection details, diagnostics, and errors stay visible until you press Enter. Exiting, including Ctrl+C
at the main menu, restores the previous terminal screen. `--plain`, redirected output, and terminals without screen-control support
keep the normal scrolling output; standalone commands are unchanged.

## Tailscale devices

Open **Tailscale devices** in the main menu. This reads the installed Tailscale client's device list, with Android
devices first and both IP address families shown in one row. Tailscale must be installed, running, and signed in
on this PC; it is optional for the rest of Droidock. Set `DROIDOCK_TAILSCALE_PATH` if its executable cannot be found.
The device list is cached until you select **Refresh device list**. Opening a device menu or selecting **Back**
does not search ports.

1. Select the device, then **Find ADB port and connect**.
2. The search first checks ports from current and saved connections at the selected IP. If none answers as ADB,
   it searches the other TCP ports on that one address, skipping the peer's advertised Tailscale API ports.
3. A progress bar shows checked ports, percentage, elapsed time, and **ETA**. ETA estimates the time to check the
   remaining range at the observed rate; it starts with “estimating” and can change with network conditions.
   The search stops immediately when ADB responds, or at the five-minute time limit. A partial search keeps its
   actual count instead of displaying 100%. Press **Ctrl+C** during the search to return to the device menu.
4. Droidock connects through ADB, verifies the physical device serial, and lets you save or edit its name.
   An existing profile for that serial keeps its ID and alias. Finding an ADB port does not grant authorization;
   first-time wireless pairing still requires the device's pairing code.

IPv4 is selected first. Use **Select another device address** to try IPv6, or **Enter a known connection port**
to connect without searching. An online Tailscale status alone does not establish ADB access. Keep Wireless
debugging enabled, and ensure Tailscale access rules allow the connection.

Successful endpoints are saved on this PC and used by normal automatic connection attempts. Startup, **Scan again**,
and `watch` do not run broad Tailscale port searches. If the saved port stops working, reopen the Tailscale menu
to find its current port. Local mDNS remains part of ordinary discovery; Tailscale device enumeration supplies
peer addresses, not Android's changing debugging port.

The same functionality is available without terminal dependencies:

```python
import asyncio

from droidock import AdbPortScanner, ConnectionManager, TailscaleClient, preferred_adb_ports

manager = ConnectionManager()
peers = TailscaleClient().peers()  # List devices without probing their ports.
peer = next(p for p in peers if p.name == "Office XR")  # The caller selects one device.
host = peer.addresses[0]
saved = [e for d in manager.store.read().devices for e in d.endpoints]
result = asyncio.run(
    AdbPortScanner().scan(
        host,
        preferred_ports=preferred_adb_ports(host, saved),
        excluded_ports=peer.peer_api_ports,
    )
)
if result.endpoint:
    device = manager.connect_endpoint(result.endpoint)  # Verify serial before saving.
```

Async applications can await `scan()` directly. Its optional `on_progress` callback receives `PortScanProgress`
values, including `eta_seconds`. Pass a `threading.Event` as `stop`, or cancel the async task, to close all active
probes. `PortScanStatus` distinguishes discovery, no response, timeout, and cancellation. Callers can set concurrency,
timeouts, or an explicit port range. Peer names and IP addresses are discovery hints, not persistent Android identities;
pass `expected=manager.device(saved_name)` when connecting on behalf of a specific saved device.

## Saved connection settings

`droidock settings` shows the storage path. On Windows, the default is usually
`%LOCALAPPDATA%\droidock\state.json`.

An explicit `--data-dir` or `DeviceStore(directory)` takes priority over `DROIDOCK_HOME`, followed by the default
configuration directory. All stores use the same schema, so CLI and Python applications can share saved profiles
or select separate directories.

The store contains:

- A persistent local device ID, name, verified serial number, manufacturer, and model.
- Wireless identifiers read from the connected device, recent connection addresses, and the last-seen timestamp.
- The default device and each device's automatic connection preference.
- The ADB executable path, server port, discovery duration, command timeout, and connection attempt limit.

Saved IP addresses and ports are connection candidates. **A different device serial number never replaces the
saved identity.** Discovery combines ADB's results with independent mDNS discovery, which looks for device
advertisements on the local network. After connecting to a new address, the manager checks the command response
and serial number before updating the profile.

Writes use file locking and atomic replacement. An invalid or corrupted store is reported and preserved instead
of being silently reset. ADB manages its authorization keys in the PC user's `.android` directory. This tool does
not copy those keys or store pairing codes. Deleting a saved profile does not revoke pairing authorization on Android.

To keep separate records for another application, choose a different directory:

```powershell
uv run droidock --data-dir C:\MyAppData\android devices
# Alternatively, set the DROIDOCK_HOME environment variable.
```

Set `DROIDOCK_ADB_PATH` to override the bundled executable. The saved `adb_path` setting takes priority over this
environment variable. Set `DROIDOCK_TAILSCALE_PATH` to select a Tailscale executable, or pass its path directly to
`TailscaleClient(executable=...)`. The public base exception is `DroidockError`.

## CLI commands

Interactive device details and diagnostics use labeled tables, readable statuses, and suggested next steps.
Result commands also display tables or short messages by default. For scripts, append `--json` to `devices`,
`register`, `connect`, `pair`, `auto-connect`, `diagnose`, `settings`, `profile`, or `disconnect`.
For example, `uv run droidock settings --json` preserves the structured field names and values.
JSON pairing requires an explicit address and `--code-stdin`, so prompts do not appear in the JSON output.

| Command | Behavior |
|---|---|
| `droidock` | Attempt startup connections and open the interactive menu. |
| `droidock --version` | Show the installed version without loading profiles or starting ADB. |
| `droidock devices --json` | List profiles, current connections, and wireless services without requesting reconnection. |
| `droidock register USB_SERIAL --name "Office XR"` | Register an already connected device. |
| `droidock pair IP:PAIRING_PORT` | Pair using a hidden code prompt. |
| `droidock connect --endpoint IP:CONNECT_PORT --name "Office XR"` | Connect, verify identity, and register the device. |
| `droidock connect "Office XR"` | Discover and reconnect a saved device. |
| `droidock connect --name "Lab tablet"` | Select one connected or discovered device, verify it, and save its name. |
| `droidock connect "Office XR" --endpoint IP:PORT` | Connect to an explicit address and verify it matches the saved device. |
| `droidock auto-connect` | Try once for each device with automatic connection enabled. |
| `droidock watch --interval 5` | Maintain connections while running; stop with Ctrl+C. |
| `droidock profile "Office XR" --default` | Select the default device. |
| `droidock profile "Office XR" --no-auto-connect` | Disable automatic connection for this device. |
| `droidock disconnect "Office XR"` | Disconnect verified wireless connections and disable automatic connection. |
| `droidock diagnose` | Show ADB, server, and device diagnostics with connection guidance. |
| `droidock restart-server` | Confirm a local ADB server restart, then reconnect saved devices with automatic connection enabled. |
| `droidock settings` | Show connection settings and the storage path. |
| `droidock settings adb_path auto` | Prefer bundled ADB. |
| `droidock forget "Office XR"` | Confirm and delete the saved profile from this PC. |

Prefix commands with `uv run` when working in the project. From another directory:

```powershell
uv run --project C:\Projects\droidock droidock
```

### Explicit server recovery (0.1.3)

Choose **Restart ADB server and reconnect** when the ADB server or its discovery
state appears stuck. The menu explains that other apps sharing the selected local
server port will briefly lose their connections, then asks for confirmation.
Saved profiles, aliases, default selection, and pairing credentials are kept.
After restarting, Droidock verifies the server response and makes one bounded
connection attempt for each saved device with automatic connection enabled.
Disabled profiles are skipped; individual failures are shown without discarding
successful reconnections. The menu then refreshes discovery explicitly.

This action is never triggered by startup, scans, or ordinary connection retries.
It restarts only the configured local server port and uses the selected ADB
executable; it does not terminate every ADB process on the PC. Each stop/start
command has a timeout of at least 20 seconds (or the configured command timeout
if longer). A restart cannot remove multicast restrictions imposed by the network.

```powershell
uv run droidock restart-server
uv run droidock restart-server --yes --json
```

`--json` requires `--yes`. Exit code 1 means that the server restart or at least one
reconnection failed. A successful restart with partial connection failures has
`server_restarted: true` and per-device `errors` in its JSON result.

Applications can expose the same explicit action without terminal dependencies:

```python
from droidock import ConnectionManager

manager = ConnectionManager()
# Call only after the user requests recovery; this interrupts other server clients.
report = manager.restart_server()
# Use manager.restart_server(reconnect=False) to restart without reconnecting profiles.
```

Custom backends may implement the optional `ServerControlBackend` protocol.
Unsupported backends raise `DroidockError` with code `unsupported_operation`.

For scripted pairing, use `pair IP:PORT --code-stdin` and send the code through standard input. Successful pairing
confirms the trust exchange; a subsequent connection must still verify the device response. The default command
timeout is 5 seconds, pairing has a 30-second timeout, and reconnection allows up to 3 attempts by default.
Each attempt tries at most 3 addresses, preferring current discovery results over saved addresses.

## Use the library in another project

Applications use the same `ConnectionManager` as the CLI. The core has no terminal prompts or output and does not
import Typer or Questionary. Importing the package does not start ADB or connect to devices.

During local development, add this package from a sibling project:

```powershell
uv add ../droidock
```

```python
from droidock import ConnectionManager

# Share the profiles registered through the CLI.
manager = ConnectionManager()
transport = manager.ensure_connected("Office XR")
result = manager.run(["shell", "getprop", "ro.product.model"], device=transport)
print(result.stdout.strip())
```

Use a separate store when your application should manage its own profiles:

```python
from droidock import ConnectionManager, DeviceStore

manager = ConnectionManager(DeviceStore("./my-app-data/android"))
```

Applications can use the verified connection address for installation, file transfer, and other ADB operations.
The package has no dependency on an application workspace or a particular device model.
See [examples/integrate.py](https://github.com/c0sogi/droidock/blob/main/examples/integrate.py) for a runnable integration example.

### Device acquisition and selection

`ensure_connected(selector=None, ...)` owns selection, discovery, connection verification, and registration.
Callers supply requirements and consume the returned `Transport`; they do not need to inspect the profile store
or implement reconnection branches. `resolve()` and `connect()` retain their saved-profile contract and share
the same workflow internally.

```python
from droidock import ConnectionManager, DeviceCriteria, SelectionError

manager = ConnectionManager()
try:
    transport = manager.ensure_connected(
        criteria=DeviceCriteria(models=("Example tablet", "Example phone")),
        name="Test device",
    )
    # Operation order and application-specific checks belong to the caller.
    manager.run(["install", "-r", "application.apk"], device=transport, timeout=180)
except SelectionError as error:
    print(error.code, error.candidates)  # IDs/addresses for a GUI, CLI, or service response.
```

- Selectors accept saved names, profile IDs, physical serials, current ADB transport addresses, and historical
  saved endpoints. A historical endpoint selects its saved identity and searches for its current address.
  `endpoint=...` explicitly overrides the connection address; with a selector it must match that device.
- Without a selector, a matching default profile takes priority, followed by a single current device, a single
  eligible saved profile, or a single discovered service. Unavailable defaults never select another device.
- Disabled automatic connection blocks implicit reconnection. Explicit selection may reconnect that profile.
- `DeviceCriteria` accepts `models`, `manufacturers`, `serials`, and an optional `predicate(Identity)`.
  String matching is case-insensitive and exact. No model names or manufacturer rules are built in.
  Criteria are checked again after connection, before saving. Discovery does not necessarily advertise model
  details, so multiple unknown services are returned as an ambiguity instead of connecting to each to guess.
- `device_groups(criteria=..., prefer_usb=True)` lists responding connections without discovery or profile
  writes. USB and wireless observations are grouped using their reported identities; conflicting model,
  manufacturer, Wi-Fi GUID, or multiple wired observations remain separate with a `conflict` explanation.
  Unidentified connections also remain separate. `group_transports()` applies the same rule to a supplied list.
- `remember=False` performs no profile writes or renaming. It also permits explicit use of a responding
  connection with no readable device serial. Such a connection is not treated as a persistent identity.
  `reconnect=False` only selects existing connections; `discover=False` permits saved/explicit addresses
  without discovery. `prefer_usb=False` prefers a wireless route within one device group.
- Connection attempts remain bounded (1–5 rounds, at most 3 candidate addresses per round). Current discovery
  precedes saved addresses; a discovery failure still permits saved addresses. Identity, criteria, and storage
  errors stop immediately. Already-connected devices do not trigger discovery.

### Service selection and command execution

`select_service(ServiceKind.PAIRING, selector=None)` selects one advertised service by purpose and optional
instance/address, retaining all IPv4/IPv6 addresses. Missing and ambiguous results use distinct error codes.
`pair_discovered(code, selector=None)` uses only pairing services and tries an alternate address on a transport
failure. The code must be supplied by the caller; the library never prompts or prints it.
`select_service(groups, kind, selector)` is also available as a pure function for an existing snapshot.

`ConnectionManager.run(arguments, device=..., timeout=..., check=True)` executes through the same backend and
server as discovery/connection. Pass an acquired `Transport` to avoid selecting again. Custom backends can opt
into the separate `CommandBackend` protocol; existing connection-only backends do not need new methods.

`AdbBackend.run(arguments, serial=..., input_text=..., cwd=..., timeout=..., check=True)` exposes direct command
execution. `CommandResult` contains `returncode`, `stdout`, `stderr`, and combined `output`. Nonzero exits raise
`CommandError` with the structured `result`; `check=False` returns that result. Timeout failures use code
`timeout`. Positive finite timeouts may exceed the default for long installations or file transfers.
Arguments are passed without a shell, stdin text is redacted from captured output, and remote-server environment
overrides are removed without modifying the parent environment. For streaming subprocesses, use
`AdbBackend.command(arguments, serial=...)` together with `AdbBackend.environment()`.

### Optional network discovery

Default discovery remains ADB plus multicast mDNS, with IPv4 and IPv6 support. Applications can add bounded
unicast queries when multicast discovery is unavailable:

```python
from droidock import CompositeDiscovery, ConnectionManager, MdnsDiscovery, UnicastDiscovery

manager = ConnectionManager(
    discovery=CompositeDiscovery(
        MdnsDiscovery(),
        UnicastDiscovery(include_local_networks=True, max_hosts=512),
        fallback_only=True,
    )
)
transport = manager.ensure_connected()
```

`UnicastDiscovery` accepts explicit IP `targets`, explicit CIDR `networks`, and actual local IPv4 interface
networks. It reads their real prefix lengths instead of guessing `/24`. `candidate_hosts()` previews the plan
without sending packets. Oversized networks are skipped with warnings rather than silently truncated; the
combined target count is bounded by `max_hosts`. IPv6 uses multicast or explicit scoped IP addresses, never
subnet enumeration. SRV ports and advertised address records are used; service names are not identity proof.

`CompositeDiscovery` merges providers concurrently, or tries fallbacks in sequence within a shared time budget.
All providers must honor the `discover(seconds)` contract. A custom `Discovery` can supply services from another
environment. Tailscale peers can supply explicit IP targets, but unicast mDNS only works if those peers respond
to UDP 5353. This feature does not automatically scan Tailscale TCP ports; the existing explicit Tailscale scan
remains available separately.

Extension points:

- `ServiceKind.CONNECT` and `ServiceKind.PAIRING`: typed service purposes on `Service` and `ServiceGroup`.
  Python callers should pass these enum members. Existing string inputs are normalized at runtime; unsupported
  values raise `ValueError`. JSON continues to use `"connect"` and `"pairing"`, with unchanged field names.
- `Snapshot.service_groups` or `group_services(services)`: obtain one entry per advertised service with all its
  addresses. `Snapshot.services` and `devices --json` retain the individual address records.
- `connect_endpoints(endpoints, name=...)`: connect through a selected service's addresses and save a verified
  device. Pass `expected=record` to require an existing device identity; profile and identity errors stop fallback.
- `Backend`: replace ADB queries, connection, pairing, and identity inspection.
- `Discovery`: add discovery for a different network environment.
- `DeviceStore(directory)`: isolate storage; subclass it to replace the storage implementation.
- `on_event(ConnectionEvent)`: send discovery, connection, and failure events to your application's UI or logs.
- `watch(stop=threading.Event())`: let the calling application stop its reconnection loop.
- `DroidockError.code`: handle failures by error category.

Another connected Android device never satisfies a request for the selected device. Missing serial numbers and
conflicting model details for the same serial prevent automatic registration or merging. Serial matching is not a
cryptographic defense against cloned identifiers; authentication uses Android's ADB authorization and pairing.

## Connection troubleshooting

A powered-on device may still be unavailable to ADB. Wireless debugging may be disabled, the network may block
discovery advertisements, or USB authorization or drivers may be missing. In those cases, update the device settings
or enter its current connection address manually. Windows USB connections may require device-specific drivers.

The tool reuses a compatible running ADB server and does not automatically stop the shared server. A protocol version
conflict produces guidance to select a compatible ADB executable or a separate local server port.

## Development and validation

Use English for project documentation, CLI messages, comments, docstrings, and examples. User-provided device names
remain unchanged. Use `uv` to manage the Python environment and dependencies.

```powershell
uv sync
uv run ruff check .
uv run ruff format --check .
uv run isort --check-only --diff .
uv run pyright
uv run pytest -q
uv lock --check
uv build --no-sources
```

Ruff includes import-order checks (`I`), and isort is also installed as a development dependency for a separate
check. Both use a line length of 110; isort uses the `black` profile. Pyright covers `src`, `tests`, `scripts`,
and `examples`.

Tests cover identity persistence after address changes, reassigned addresses, USB/wireless duplicates, requested
device verification, connection preferences, pairing secrets, storage corruption and concurrent writes, and CLI
behavior. See [VALIDATION.md](https://github.com/c0sogi/droidock/blob/main/VALIDATION.md) for executed checks, isolated wheel installation, and physical-device
verification limits.

External API references:
[adbutils](https://github.com/openatx/adbutils),
[ADB commands](https://android.googlesource.com/platform/packages/modules/adb/+/HEAD/docs/user/adb.1.md),
[wireless ADB](https://android.googlesource.com/platform/packages/modules/adb/+/HEAD/docs/dev/adb_wifi.md),
[ADB protocol](https://android.googlesource.com/platform/packages/modules/adb/+/HEAD/docs/dev/protocol.md),
[Tailscale CLI](https://tailscale.com/docs/reference/tailscale-cli#status),
[Zeroconf](https://python-zeroconf.readthedocs.io/en/latest/api.html),
[isort configuration](https://isort.readthedocs.io/en/latest/configuration/black_compatibility.html),
[Ruff formatter](https://docs.astral.sh/ruff/formatter/).
