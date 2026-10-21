# Powerpal web service (HTTP)

The Powerpal phone app uploads readings to a web service, which prices them against the account's tariff and serves them back. This integration can optionally download from it and upload to it (both off by default). The reference code is [cloud.py](../custom_components/powerpal_ble/cloud.py) and [cloud_sync.py](../custom_components/powerpal_ble/cloud_sync.py).

> **Privacy warning.** The service is keyed only by the device serial number and hardware UUID, and it stores the account holder's **name, email, phone number, address, meter identifier (NMI) and electricity tariff**. Anyone holding the hardware UUID can read and change that account's data. See [What private data the service holds](#what-private-data-the-service-holds).

- [Hosts](#hosts)
- [Authentication](#authentication)
- [Upload readings](#upload-readings)
- [Download readings](#download-readings)
- [Device and account endpoints](#device-and-account-endpoints)
- [What private data the service holds](#what-private-data-the-service-holds)
- [Limits and behaviour](#limits-and-behaviour)
- [How the app schedules uploads](#how-the-app-schedules-uploads)

## Hosts

| Service | Base URL |
|---|---|
| Meter readings | `https://readings.powerpal.net` |
| Devices / account | `https://devices.powerpal.net` |
| Messages | `https://messages.powerpal.net` |
| Advice | `https://advice.powerpal.net` |
| IoT gateway | `https://iot.powerpal.net` (`wss://iot.powerpal.net/ws`) |

Each has a staging twin under `*.staging.powerpal.net`. This integration only uses the readings service.

## Authentication

There is **no login, token exchange or bearer scheme**. Every request carries the device's hardware UUID, exactly as a string, in the `Authorization` header:

```
Authorization: <hardware-uuid>
```

- The hardware UUID is the 16 bytes read from Bluetooth characteristic `59DA0009` (see [bluetooth.md](bluetooth.md#gatt-reference)), written in the usual `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` form.
- The serial number goes in the URL path in **lowercase hex**, for example `000a1b2c`. Uppercase returns `403`.
- Nothing else identifies the caller. The UUID is effectively a password for the account. Treat it as a secret: do not log it, put it in a URL, paste it in an issue or commit it.
- The byte order of the 16 bytes as a UUID string is not obvious, so this integration tries the plausible forms (as-is, reversed, and Microsoft little-endian field order) and keeps the one the service accepts. See `token_candidates` and `find_token` in [cloud.py](../custom_components/powerpal_ble/cloud.py).
- A wrong or unknown credential returns `401` or `403`.

## Upload readings

```http
POST /api/v1/meter_reading/000a1b2c HTTP/1.1
Host: readings.powerpal.net
Authorization: <hardware-uuid>
Accept: application/json
Content-Type: application/json
Content-Encoding: gzip

<gzip-compressed JSON body>
```

The **body must be gzip-compressed**, matching the `Content-Encoding: gzip` header. A plain JSON body is rejected with `400 "Invalid gzipped content"` and nothing is stored.

Decompressed body: an array of readings. A reading taken over Bluetooth has only three fields:

```json
[
  { "timestamp": 1700212128, "pulses": 49, "watt_hours": 49 },
  { "timestamp": 1700212188, "pulses": 51, "watt_hours": 51 }
]
```

| Field | Meaning |
|---|---|
| `timestamp` | Unix seconds, UTC, on a minute boundary |
| `pulses` | Flashes counted in that minute |
| `watt_hours` | `pulses * 1000 / flashes_per_kwh`, capped at 416.6667 |

Do not send price fields. **The server does all the pricing.**

Response (`200`): the stored readings, now priced.

```json
{
  "readings": [
    {
      "timestamp": 1700212128,
      "pulses": 49,
      "watt_hours": 49.0,
      "cost": 0.0123,
      "usage_cost": 0.0123,
      "fixed_cost": 0.0,
      "cost_per_kwh": 0.251,
      "is_peak": false,
      "is_final": true,
      "samples": 1
    }
  ]
}
```

Status `>= 400` is an error. The app sends **at most 1000 readings per request**.

Python equivalent:

```python
import gzip, json, requests

payload = [{"timestamp": 1700212128, "pulses": 49, "watt_hours": 49}]
requests.post(
    "https://readings.powerpal.net/api/v1/meter_reading/000a1b2c",
    data=gzip.compress(json.dumps(payload, separators=(",", ":")).encode()),
    headers={
        "Authorization": "<hardware-uuid>",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Content-Encoding": "gzip",
    },
)
```

This **writes to the user's account**. It becomes visible in their app and website.

## Download readings

```http
GET /api/v1/meter_reading/000a1b2c?start=1700000000&end=1700604800 HTTP/1.1
Host: readings.powerpal.net
Authorization: <hardware-uuid>
Accept: application/json
```

`start` and `end` are unix seconds. The response is a JSON array, oldest first:

```json
[
  {
    "timestamp": 1700000040,
    "pulses": 48,
    "watt_hours": 48.0,
    "cost": 0.0120,
    "usage_cost": 0.0120,
    "fixed_cost": 0.0,
    "cost_per_kwh": 0.251,
    "is_peak": false,
    "is_final": true,
    "samples": 1
  }
]
```

| Field | Meaning |
|---|---|
| `timestamp` | Unix seconds |
| `pulses` | Flashes in the interval |
| `watt_hours` | Energy for the interval. Equals `pulses * 1000 / flashes_per_kwh` |
| `cost`, `usage_cost`, `fixed_cost`, `cost_per_kwh` | Pricing from the account's tariff |
| `is_peak`, `is_final` | Tariff flags |
| `samples` | 1 for a per-minute reading |

The pulse counts match what the device itself reports for the same minutes. The service only holds what a phone (or this integration) uploaded, so it **lags the device**: a window recently read over Bluetooth can be empty on the service.

```bash
curl -H "Authorization: <hardware-uuid>" \
  "https://readings.powerpal.net/api/v1/meter_reading/000a1b2c?start=1700000000&end=1700604800"
```

## Device and account endpoints

These live on `devices.powerpal.net` with the same `Authorization` header. This integration does not use them, but they matter because of what they return.

| Method and path | Purpose |
|---|---|
| `GET /api/v1/energy_bridges/{serial}` | The device and **account profile** (see below) |
| `POST /api/v1/energy_bridges/{serial}/checkin` | Reports the phone and device state; the response is the profile, plus a `firmware_update` offer when the reported firmware is out of date |
| `POST /api/v1/energy_bridges/{serial}/factory_reset` | Resets the device record (the app calls this when you forget a device) |
| `POST /api/v1/energy_bridges/{serial}/pairing_code_recovery` | Sends the pairing code to the registered email or mobile |
| `GET /api/v1/device/{serial}` | First and last reading timestamps; `404` for a device with no uploaded data |

Other paths exist (`/api/v1/config/`, `/price/`, `/state/`, `/push_tokens/`, `/content/...`, `/logs`, `/gateways/...`) but are not needed for a local integration.

### Check-in body

```json
{
  "firmware_version": "1.0.4_3",
  "battery_percent_remaining": 87,
  "os": "android",
  "os_version": "14",
  "app_version": "2.1.3",
  "phone_id": "<phone id>",
  "phone_model": "<phone model>",
  "phone_name": "<phone name>",
  "is_bluetooth_enabled": true,
  "app_opened_at": 1700212128,
  "hardware_rebooted_at": null
}
```

A minimal body is accepted. When the reported `firmware_version` is older than the newest release, the profile includes a `firmware_update` object with download URLs for the firmware and bootloader packages (Nordic DFU zips); otherwise it is `null`.

## What private data the service holds

`GET /api/v1/energy_bridges/{serial}` with nothing but the hardware UUID returns the whole account profile. The shape below is illustrative, with invented values:

```json
{
  "serial_number": "000a1b2c",
  "pulses_per_kwh": 1000,
  "led_sensitivity": 9,
  "installed_at": "2024-01-01T00:00:00Z",
  "factory_reset_at": "2024-01-01T00:00:00Z",
  "os": "android",
  "app_version": "2.1.3",
  "timezone": "Australia/Sydney",

  "name": "Jane Citizen",
  "email": "jane@example.com",
  "phone": "+61 400 000 000",
  "address": "1 Example Street, Sampleville",
  "nmi": "0000000000",

  "tariff": {
    "retailer": "Example Energy",
    "rates": [ { "name": "Peak", "cents_per_kwh": 45.0, "from": "15:00", "to": "21:00" } ]
  },

  "firmware_update": null
}
```

The device fields are harmless. The rest is **personal information**:

- Full name, email address and phone number.
- Service address and the **NMI** (the national meter identifier).
- The retailer and time-of-use tariff.
- Phone identifiers from check-ins (model, name, ID).
- The user's **usage history**, minute by minute, which reveals when they are home or away.

Consequences for developers:

1. **The hardware UUID is the only key.** There is no per-user token. Someone with the UUID can read this profile, read and write readings, trigger a factory reset and request pairing code recovery.
2. **Never log, commit or post** a hardware UUID, a profile response or a readings dump. Scrub them from issues and test fixtures. Tests in this repository use invented data.
3. **Keep the optional sync opt-in.** Turning it on makes Home Assistant send the serial and hardware UUID to powerpal.net and, for upload, write to the user's account. The options screen says so in plain words.
4. The UUID is also readable over Bluetooth by any host that is bonded and has the pairing code, so physical access to the device plus its printed code is enough to reach the account.

## Limits and behaviour

- **Row limit.** A `GET` returns at most **50,000 rows**, oldest first, **still with status 200**. A window that is too large silently loses the newest readings. Use windows of 7 days (about 10,000 rows). This integration treats a full response as an error.
- **Upload size.** At most 1000 readings per request.
- **Pricing is server side.** Uploads carry no cost fields; the response and later downloads do.
- **Depth.** The app imports up to 90 days when setting up a device. This integration does the same.
- **Merging.** Per-minute values from the service agree with the device's pulse counts. When both exist, prefer the device. This integration only uses the service to fill gaps.

## How the app schedules uploads

The phone app uploads unsent readings when all of these hold:

- the network is connected,
- no upload was made in the last **300 seconds**, and
- the newest unsent reading has changed.

Whether it uploads *now* depends on state: in the foreground or on charge it sends every new reading; in the background it waits until a full batch of `cloudSaveIntervalMinutes` readings (a setting held in the account) has built up. After a successful upload it advances a cursor to the last timestamp sent.

This integration mirrors the limits (one request per 5 minutes, at most 1000 readings), and reconciles by asking the service which minutes it already has, then posting only the missing ones, so nothing is sent twice and real gaps get filled.
