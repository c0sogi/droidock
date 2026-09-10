# Changelog

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
