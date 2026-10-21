# Bluetooth protocol

Everything needed to talk to a Bluetooth Powerpal directly. The reference implementation is [custom_components/powerpal_ble/ble](../custom_components/powerpal_ble/ble), which depends only on `bleak`.

- [Discovery](#discovery)
- [Connecting: two gates](#connecting-two-gates)
- [GATT reference](#gatt-reference)
- [Reading live power](#reading-live-power)
- [Downloading history](#downloading-history)
- [Unit conversion](#unit-conversion)
- [The device clock](#the-device-clock)
- [Reading batch size](#reading-batch-size)
- [Settings](#settings)
- [Gotchas](#gotchas)

## Discovery

A Powerpal advertises one or both of these service UUIDs:

| UUID | Notes |
|---|---|
| `59daabcd-12f4-25a6-7d4f-55961dce4205` | The main (custom) service |
| `0000fcd7-0000-1000-8000-00805f9b34fb` | A 16-bit vendor UUID (`FCD7`) |

Match on **either**. The local name, when present, is `Powerpal <serial-hex>`, for example `Powerpal 000a1b2c`. Not every advertisement carries the name, and some carry only `FCD7`, so do not rely on it.

The Wi-Fi Powerpal Pro advertises as `Powerpal Pro <id>`. Skip names starting with `Powerpal Pro`.

Other services on the device:

| Service | Purpose |
|---|---|
| `0000180A` Device Information | Firmware revision (`2A26`) |
| `0000180F` Battery | Battery level (`2A19`) |
| `0000FE59` | Nordic Secure DFU (firmware update). Not needed for normal use |

The device accepts **one connection at a time**. If a phone is connected, nobody else can connect.

## Connecting: two gates

Reading data needs two separate things. Both are required, and they fail in different ways.

### Gate 1: bonding (link layer)

Until the host is bonded, any read of a `59DA....` characteristic fails with **ATT error 5, Insufficient Authentication**. The standard battery and firmware characteristics are readable without bonding.

Bonding is **Just Works**. No passkey is ever requested, and the 6-digit pairing code is *not* used at this layer. The host only needs to accept.

Bonding persists, so it is done once per host.

**Linux / BlueZ.** Plain `BleakClient.pair()` fails with `AuthenticationFailed`, after which the device drops the link. Bonding works reliably once all three of these hold:

1. The adapter is **pairable** (`Pairable: no` is the BlueZ default).
2. A **`NoInputNoOutput` agent** is registered on D-Bus and set as default.
3. The device is a **freshly discovered** BlueZ object. A device object that has already failed to bond keeps failing; remove it (`RemoveDevice`) and let it be rediscovered.

[ble/agent.py](../custom_components/powerpal_ble/ble/agent.py) implements this. After a failed bond, `PowerpalClient` removes the stale device so the next attempt starts clean.

**Windows** shows a "set up your Powerpal" prompt and bonds without a passkey. **Android** does it implicitly on connect.

A bonded device may stop advertising while the host holds the connection, so connect by address rather than scanning.

### Gate 2: the unlock (application layer)

After bonding, reads of `59DA....` characteristics fail with **ATT error 2, Read Not Permitted** until the pairing code is written:

```
write  59DA0011  =  uint32 little-endian pairing code     (write with response)
```

The pairing code is the 6-digit number printed on the device, for example `123456` is written as `40 E2 01 00`.

- This must be repeated **on every new connection**. It does not persist.
- A **wrong code is still accepted** by the write (no error). The only symptom is that reads stay locked. To validate a code, write it and then read the serial (`59DA0010`); if that read fails, the code is wrong.
- Writing the right code afterwards unlocks as normal.

### Connection sequence

```
connect
bond                              (once per host; see gate 1)
write 59DA0011 = pairing code     (every connection)
read  59DA0010                    (serial; fails here if the code is wrong)
... read state, download history ...
disconnect
```

If the link drops right after bonding, reconnect and carry on from the unlock.

## GATT reference

Custom characteristics have the form `59DA00NN-12F4-25A6-7D4F-55961DCE4205`.

| ID | Name | Type | Access | Meaning |
|---|---|---|---|---|
| `59DA0001` | measurement | `uint32` timestamp, `uint16` pulses (+ padding) | Notify | One stored reading, streamed during a history download |
| `59DA0002` | measurement access | `uint32` start, `uint32` end | Write | Requests readings between two timestamps and starts the stream |
| `59DA0003` | pulse | `uint32` | Read, Notify | Milliseconds between the last two meter flashes |
| `59DA0004` | time | `uint32` | Read, Write | Device clock, unix seconds (see [the clock](#the-device-clock)) |
| `59DA0005` | first record | `uint32` earliest, `uint32` latest | Read | Timestamps of the oldest and newest stored readings |
| `59DA0007` | (unknown) | | Read | Not used by the app; reads as `00000000` |
| `59DA0008` | LED sensitivity | `uint8` | Read, Write | Sensor sensitivity, 0 to 14, default 9 |
| `59DA0009` | hardware UUID | 16 bytes | Read, Write | Identifies the device to the web service. **Secret** |
| `59DA0010` | serial number | `uint32` | Read | Shown as 8 digit uppercase hex |
| `59DA0011` | pairing code | `uint32` | Write | The unlock (see [gate 2](#gate-2-the-unlock-application-layer)) |
| `59DA0012` | millis since pulse | `uint32` | Read | Milliseconds since the last flash |
| `59DA0013` | reading batch size | `uint32` | Read, Write | Minutes per delivery batch (see [below](#reading-batch-size)) |
| `59DA0014` | transmit power | `int8` | Read, Write | BLE TX power in dBm |

Standard characteristics:

| UUID | Service | Meaning |
|---|---|---|
| `00002A19-0000-1000-8000-00805F9B34FB` | `180F` | Battery percent, `uint8` |
| `00002A26-0000-1000-8000-00805F9B34FB` | `180A` | Firmware revision, UTF-8, `<firmware>_<bootloader>`, for example `1.0.4_3` |

The constants live in [ble/const.py](../custom_components/powerpal_ble/ble/const.py).

## Reading live power

Two ways to get the instantaneous power:

- **Notifications:** subscribe to `59DA0003`. Each notification is the number of milliseconds between the last two flashes.
- **On connect:** read both `59DA0003` (last interval) and `59DA0012` (time since the last flash), and use the **larger** of the two. That makes power decay towards zero when flashes stop (for example when solar is exporting), instead of freezing at the last value.

Convert with [the formulas below](#unit-conversion).

## Downloading history

The device stores one reading per minute and can hold months. To fetch a range:

1. Read `59DA0005` to get `(earliest, latest)`. If both are empty there is nothing stored.
2. Choose `start = max(last_timestamp_you_have, earliest)` and `end = latest`. If `start >= end` there is nothing new.
3. **Subscribe to notifications on `59DA0001` first.**
4. Write `start` and `end` as two little-endian `uint32` (8 bytes) to `59DA0002`.
5. The device sends **one notification per reading**, in ascending order. Both `start` and `end` are inclusive, and readings fall on exact minute boundaries.
6. Stop when you receive the reading whose timestamp equals `end`.

### Notification layout

```
offset  size  field
0       4     timestamp  (uint32, unix seconds)
4       2     pulses     (uint16)
6       ...   constant padding: identical on every reading, ignore it
```

On the wire a notification is 20 bytes (default 23-byte ATT MTU minus 3). Reading `59DA0001` directly returns 23 bytes. Only the first 6 bytes mean anything.

Example (hex): `A0 2D 57 65 31 00 ...` is timestamp `0x65572DA0` (unix 1700212128) with `0x0031` = 49 pulses.

### Robustness

- Ignore any reading with `timestamp > end`.
- **Add a timeout.** If the reading stamped exactly `end` never arrives, the stream just goes quiet. This implementation ends the download after **30 seconds of silence**.
- Throughput is high. A day (1440 readings) downloads in seconds, and connecting is the slow part, so fetch large ranges on one connection. This integration downloads up to a day at a time and up to four chunks per connection.
- Re-adding a minute is harmless, so overlapping downloads are safe.

## Unit conversion

`flashes_per_kwh` is the meter's setting (the app default is 1000). One flash is `1000 / flashes_per_kwh` Wh.

**Stored reading** (one minute):

```
wh    = min(pulses * 1000 / flashes_per_kwh, 416.6667)     # capped at 25 kW
watts = wh * 60
```

**Live interval** (from `59DA0003` / `59DA0012`):

```
seconds = interval_ms / 1000
watts   = 3600 * 1000 / (seconds * flashes_per_kwh)        # capped at 25 kW
```

Example: 1000 flashes per kWh and a flash every 3.6 s is exactly 1000 W. 49 pulses in a minute is 49 Wh, about 2.9 kW.

Code: [ble/conversion.py](../custom_components/powerpal_ble/ble/conversion.py).

## The device clock

`59DA0004` holds the time stored readings are stamped from.

- Reading it returns **the last value written**, not a running clock.
- A value of **0** means the device has lost power and rebooted. Readings from before the host set the clock again are unreliable.
- The phone app writes the current unix time on every connect. A client should write it after seeing `0`, and may refresh it occasionally to limit drift.
- **Do not write 0.** The app uses that to put the hardware into a deep-sleep factory-reset state when you "forget" a device.

## Reading batch size

`59DA0013` is the number of minutes the device accumulates before delivering readings. The phone app sets it to 1 ("high resolution") while in the foreground or charging, and to the user's upload interval otherwise to save phone battery. It does not change the one-minute storage resolution, and history downloads still return every minute. A mains-powered host can leave it at 1.

## Settings

- **LED sensitivity** (`59DA0008`): write 0 to 14 and read it back. Raise it if flashes are being missed.
- **Flashes per kWh:** not on the device (see the [overview](README.md#the-big-picture)).
- **Firmware:** `2A26`, split on `_` into firmware and bootloader versions.

## Gotchas

- **Only one connection.** Unpair the phone from the Powerpal, or it and Home Assistant will fight over the link.
- **Weak links.** Connecting either works within a few seconds or not at all, so use a short per-attempt timeout and retry rather than one long wait. Time spent *connected* costs the device's battery; failed connects cost almost nothing.
- **`59DA0004` is not a clock.** See above.
- **Wrong code looks like success.** See gate 2.
- **Bluetooth proxies.** Bonding needs the Just Works exchange above. On BlueZ this integration drives it over D-Bus, which a remote proxy does not offer, so proxy support depends on the proxy firmware's pairing support. If `pair()` raises but the link stays up, try the unlock anyway.
- **Repeated wrong codes.** One wrong attempt caused no lockout; whether repeated attempts lock the device out has not been tested.
