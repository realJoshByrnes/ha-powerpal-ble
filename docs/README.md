# Developer documentation

How the Powerpal works, written for people who want to extend this integration or build their own client.

| Document | Covers |
|---|---|
| [bluetooth.md](bluetooth.md) | Discovery, bonding, the unlock step, every GATT characteristic, history download, unit conversion |
| [http-service.md](http-service.md) | The Powerpal web service: authentication, reading upload and download, and what personal data it holds |
| [integration.md](integration.md) | How this integration uses both, and the design decisions behind it |

## The big picture

```
 Meter LED  -->  Powerpal sensor  <-- Bluetooth LE -->  Phone app / Home Assistant
 (flashes)       counts flashes,                              |
                 stores 1 reading                             | HTTPS (optional)
                 per minute                                   v
                                                       Powerpal web service
                                                       readings, pricing, account
```

- The Powerpal is a **Bluetooth Low Energy pulse counter**. It sits on the electricity meter's LED and counts flashes ("pulses").
- It stores **one reading per minute**: a unix timestamp and a pulse count. It keeps a long on-device history (months), so a client that was away can catch up.
- A flash is a fixed amount of energy: `1000 / flashes_per_kWh` Wh. **Flashes per kWh is not stored on the device.** It is printed on the meter (commonly 1000 or 3200) and kept in the phone app / web account, so a client must be told it.
- The **web service** stores uploaded readings, prices them against the account's tariff and holds the account's personal details. Its only credential is the device's hardware UUID.

## Scope

Only the Bluetooth Powerpal is covered. The Wi-Fi **Powerpal Pro** (advertises as `Powerpal Pro <id>`) is a different product and is not documented or supported here.

## Conventions

- All multi-byte values on the wire are **little-endian**.
- Timestamps are **unix seconds, UTC**.
- Serial numbers are `uint32`, shown as 8 digit uppercase hex (`000A1B2C`). In web service URLs use **lowercase** hex (`000a1b2c`).
- Examples use placeholders such as `<hardware-uuid>`. Never publish a real hardware UUID, pairing code or account response (see [http-service.md](http-service.md#what-private-data-the-service-holds)).
