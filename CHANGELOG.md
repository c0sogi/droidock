# Changelog

## 0.1.7

- Add `droidock mirror`: low-latency screen mirroring through scrcpy. It chooses the only, default, or named
  device, offers a list with `--pick` or when the choice is ambiguous, and is available as **Mirror the screen**
  in the interactive menu.
- Install the pinned scrcpy 5.0.1 on first use into the user cache after checking its published SHA-256 digest;
  `droidock scrcpy` shows or prepares it. `DROIDOCK_SCRCPY_PATH` or scrcpy 3.0+ on `PATH` is used without a
  download. scrcpy runs with Droidock's ADB executable and server port.
- Size the stream to the measured link, restart at a lower quality when video queues on a network link, and
  reconnect after a disconnection. On Windows a restarted window keeps its position and size.
- Mirror the left eye of headsets with side-by-side displays, without forwarding input, unless `--eye` or
  `--control` says otherwise.
- Library API: `MirrorSession`, `MirrorOptions`, `MirrorQuality`, `QUALITY_LEVELS`, `AdbLinkProbe`,
  `LinkMonitor`, `DisplayInfo`, and `ScrcpyInstaller`.

## 0.1.6

- Add verified standalone APK installation and application launch and stop: `Deployment`, `droidock install`,
  `launch`, `stop`, and an interactive **Install an APK** action.

## 0.1.5

- Choose **Connect without saving** or **Connect and save this device** in wireless service, manual address,
  pairing, and Tailscale connection flows. Temporary connections remain visible and can be registered later.
- Add `droidock connect --no-save`, including human-readable connection details and a transport JSON result.
  Ordinary `connect` commands retain their existing save behavior and result format.
- Add `remember=False` to `connect_endpoint()` and `connect_endpoints()`; identity checks and address fallback
  remain active without creating or updating profiles. Add `scan(update_saved=False)` for observation without
  updating saved connection details.
- Skip alias prompts for temporary connections. Cancel before connecting without creating a profile.
- Preserve ADB pairing credentials and existing saved-device preferences independently of this choice.
