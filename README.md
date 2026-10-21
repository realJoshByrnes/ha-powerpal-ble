# Powerpal (Bluetooth) for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![Validate](https://github.com/realJoshByrnes/ha-powerpal-ble/actions/workflows/validate.yml/badge.svg)](https://github.com/realJoshByrnes/ha-powerpal-ble/actions/workflows/validate.yml)
[![Tests](https://github.com/realJoshByrnes/ha-powerpal-ble/actions/workflows/tests.yml/badge.svg)](https://github.com/realJoshByrnes/ha-powerpal-ble/actions/workflows/tests.yml)

Talk to your **Bluetooth Powerpal** directly from Home Assistant. Live power, total energy and battery, with full history for the Energy dashboard. No cloud account, no phone app, no internet.

> **Enjoying it? Please star the repo.** It helps other Powerpal owners find it. Forks, issues and pull requests are very welcome (see [Contributing](#contributing)).

## Why use this instead of a cloud/Wi-Fi integration?

The other Powerpal integrations for Home Assistant read your data from the Powerpal web service. That only works if your phone has already uploaded it. This one talks to the Powerpal itself over Bluetooth.

| | This integration | Cloud-based integrations |
|---|---|---|
| Where data comes from | The Powerpal, over Bluetooth | Powerpal's web service |
| Needs a Powerpal account | No | Yes |
| Needs internet | No | Yes |
| Needs the phone app running | No | Yes, the app uploads the readings |
| Keeps working if the service changes or shuts down | Yes | No |
| Your readings leave your home | Never, unless you opt in | Always |
| Full per-minute resolution | Yes | Whatever the service holds |
| Power, energy, battery sensors | Yes | Varies |
| Energy dashboard history (backfilled) | Yes, up to 90 days | Varies |

In practice:

- **It works on its own.** Pair it once, then Home Assistant polls the device every 5 minutes and downloads whatever it stored.
- **Nothing goes missing.** The Powerpal stores readings on the device, so a Home Assistant outage or a weak Bluetooth link just means the next poll catches up. Nothing is estimated.
- **It is private by default.** Nothing is sent anywhere. The optional web service sync is off and you have to turn it on yourself.
- **It is not tied to a vendor's servers.** The Powerpal service has changed before and could change again.
- **It handles meter changes.** Flashes per kWh is a timeline, so changing meters or moving house does not rewrite your old energy.

## Three ways to get your history

1. **From the device (always on).** The Powerpal stores its own readings. Home Assistant downloads them over Bluetooth and imports them, including history from before you installed this.
2. **From the Powerpal web service (optional, off by default).** Pulls in readings the phone app uploaded earlier, up to 90 days. Anything read from the device itself always wins over the service's copy.
3. **To the Powerpal web service (optional, off by default).** Sends your readings to your Powerpal account, which is what the phone app normally does. Once Home Assistant does it instead, the Powerpal app and website stay current and work away from home. Without it, the app only sees data when your phone is near the device.

Options 2 and 3 contact powerpal.net with the device's serial number and hardware ID, and 3 writes to your account. Both are switched on in the integration's **Configure** menu.

> **Important: unpair your phone from the Powerpal once this is installed.** The device accepts only one connection at a time, so the phone app and Home Assistant would keep knocking each other off. Remove the Powerpal from the phone's Bluetooth settings (and keep the app from connecting to it). If you want the app to stay useful, turn on upload (option 3) and it will show the data Home Assistant uploads.

## What you get

For each Powerpal:

| Entity | Description |
|---|---|
| Power (W) | Live power from the time between meter flashes |
| Energy (kWh) | Total energy, usable in the Energy dashboard |
| Battery (%) | Device battery |
| Update now (button) | Poll immediately instead of waiting for the next 5 minute poll |
| LED sensitivity (config) | The meter sensor sensitivity, 0 to 14. It is stored on the device and written to it |
| Pairing code and hardware UUID (config) | Shown for reference |

Hourly energy statistics are imported for up to 90 days, so the Energy dashboard has history from day one. The per-minute log is kept for 90 days.

## Supported devices

- **Supported:** the Bluetooth Powerpal (the small sensor that clips onto your meter's LED and talks to the phone app, advertising as `Powerpal xxxxxxxx`).
- **Not supported:** the Wi-Fi **Powerpal Pro**. It is detected and ignored on purpose.

## Requirements

- Home Assistant **2025.4 or newer** with the Bluetooth integration set up.
- A Bluetooth adapter on the Home Assistant machine that can reach the Powerpal. A USB adapter in your Home Assistant host is the tested setup (see below).
- The 6-digit **pairing code** printed on the Powerpal.
- Your meter's **flashes per kWh**, printed on the meter (commonly 1000 or 3200).

## Does it work through an ESP32 Bluetooth proxy?

**Short answer: unproven. It might, and I would like to find out.**

- **Tested and working:** a local Bluetooth adapter on Home Assistant OS.
- **Untested:** ESPHome Bluetooth proxies (ESP32). The integration uses Home Assistant's Bluetooth stack, so it will see the Powerpal through a proxy, and the code falls back gracefully if the proxy can't pair. But the one-time bonding step is where proxies are most likely to fail.

Things to know before you try a proxy:

1. The Powerpal needs a **bonded** connection. On a local adapter the integration handles this itself by talking to BlueZ over D-Bus. Through a proxy that route does not exist, so bonding relies on your proxy's firmware and ESPHome version supporting pairing for **active connections**.
2. The proxy must be running with **active connections enabled** (the default for the standard proxy configuration), because the Powerpal is read by connecting to it, not from advertisements.
3. The Powerpal accepts **one connection at a time**, so unpair your phone from it.
4. A good trick if bonding fails through a proxy: pair once with a local adapter (even a cheap USB one, or temporarily put the Powerpal next to the host), and see whether a proxy then works for the regular polling.

If you try it, please [open an issue](https://github.com/realJoshByrnes/ha-powerpal-ble/issues) saying what you used (ESPHome version, board, whether pairing and polling worked) so this section can be updated.

## Installation

### HACS (recommended)

1. In HACS, open the menu and choose **Custom repositories**.
2. Add `https://github.com/realJoshByrnes/ha-powerpal-ble` with the category **Integration**.
3. Install **Powerpal (Bluetooth)** and restart Home Assistant.

### Manual

Copy `custom_components/powerpal_ble` into your Home Assistant `config/custom_components/` folder and restart.

## Setup

1. Close the Powerpal phone app and unpair your phone from the Powerpal (the device only accepts one connection).
2. Take the Powerpal near your Home Assistant Bluetooth adapter for the first setup if the signal is weak. It can go back on the meter afterwards.
3. Go to **Settings > Devices & services**. The Powerpal should be discovered automatically; otherwise **Add integration > Powerpal (Bluetooth)**.
4. Enter the 6-digit pairing code from the device.
5. Wait while it bonds. A weak signal can take several attempts, and the dialog shows progress.
6. Confirm flashes per kWh and the LED sensitivity.
7. Add the **Energy** sensor to **Settings > Dashboards > Energy**.

To change flashes per kWh later (new meter, moved house), open the integration's **Configure** menu. The change applies from the date and time you pick, and energy already recorded keeps the value it was counted under.

## How it works

The integration connects briefly (every 5 minutes, and sooner after a failed poll), unlocks the device with the pairing code, reads the current state, downloads any readings it has not seen yet, and disconnects. Connecting is the slow, battery-costly part, so it stays connected only as long as needed. Readings are stored per minute, and hourly statistics are derived from that.

The protocol library lives in [custom_components/powerpal_ble/ble](custom_components/powerpal_ble/ble) and uses only `bleak`, with no Home Assistant imports, so it can be reused in other projects.

## Troubleshooting

- **Bonding keeps failing:** make sure the phone is unpaired and the app closed, move the Powerpal closer, and retry. Raise the log level with:
  ```yaml
  logger:
    logs:
      custom_components.powerpal_ble: debug
  ```
- **"Unlock failed: check the pairing code":** the code is the 6 digits printed on the device, not the serial number.
- **Energy looks off by a constant factor:** your flashes per kWh is wrong. Fix it from the Configure menu with the correct start date.
- **No devices found:** check that Home Assistant's Bluetooth integration works and that the Powerpal is in range.
- **Another Powerpal integration is installed:** this one uses the domain `powerpal_ble`, so it can coexist with cloud integrations (which use `powerpal`).

## Status

Used daily on Home Assistant OS with a local USB Bluetooth adapter. Discovery, bonding, unlock, live data, history backfill and the Energy dashboard import are working. Not yet verified: ESPHome Bluetooth proxies, other Linux hosts, and a long soak over weeks, so reports are very helpful.

## Contributing

This is a small, independent project and help makes it better. All of these are welcome:

- **Star the repo** so other Powerpal owners can find it.
- **Try it and report back**, especially with an **ESP32 Bluetooth proxy**, a non-HAOS host, or different firmware versions.
- **Open issues** with debug logs (remove any pairing code or hardware UUID first).
- **Send pull requests.** Run the tests with:
  ```bash
  pip install -r requirements_test.txt
  python -m pytest
  ```
  The protocol library tests need no hardware or Home Assistant.
- **Fork it.** The MIT licence lets you build on it freely. If you add support for another Powerpal model, I would love to hear about it.

Protocol and web service details are in [docs/](docs/README.md).

`scripts/deploy.ps1` copies the integration to a mounted Home Assistant config share for testing.

## Disclaimer

This is an unofficial community project. It is not affiliated with or endorsed by Powerpal. "Powerpal" is a trademark of its owner. It exists for interoperability with Home Assistant. Use at your own risk.

## Licence

[MIT](LICENSE)
