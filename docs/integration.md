# How the integration works

A tour of the code for contributors. For the wire formats see [bluetooth.md](bluetooth.md) and [http-service.md](http-service.md).

## Layout

```
custom_components/powerpal_ble/
  ble/                  Protocol library: bleak only, no Home Assistant imports
    client.py           PowerpalClient: connect, bond, unlock, read state, download history
    agent.py            BlueZ helpers: pairable adapter, Just Works agent, forget stale device
    const.py            UUIDs and limits
    conversion.py       Pulses and intervals to Wh and watts
    models.py           Reading, PowerpalState
  coordinator.py        Polling, backfill, retries, statistics import
  energy.py             MinuteLog: the per-minute store (pure Python)
  factor_timeline.py    Flashes-per-kWh as a timeline (pure Python)
  cloud.py              Web service client (aiohttp, no Home Assistant imports)
  cloud_sync.py         Download/upload logic over the minute log (pure Python)
  config_flow.py        Discovery, bonding screen, settings, options
  sensor.py button.py number.py text.py entity.py    Entities
tests/                  No hardware and no Home Assistant needed
```

Keeping the protocol, storage, timeline and sync logic free of Home Assistant imports is deliberate: it is what makes them testable here and reusable elsewhere.

## Polling cycle

Every 5 minutes (sooner after a failure), the coordinator:

1. Connects with `PowerpalClient` (bond if needed, then unlock).
2. Reads state: serial, firmware, battery, live interval, LED sensitivity, first/last record.
3. Downloads readings it has not seen, in chunks of one day, up to four chunks per connection.
4. Disconnects.
5. Updates the minute log, then writes hourly statistics for the hours that changed.

Connecting is slow and unreliable on a weak link, while downloading is fast, so the integration connects as seldom and as briefly as it can. A failed connect costs the device almost nothing; time spent connected is what drains its battery. After a failed poll it retries after 60 s, then 120 s, then returns to the normal interval.

Setup never waits for the device. The first poll runs in the background once Home Assistant has finished starting.

## Storage

- **The minute log is the primary store**: one `(timestamp, pulses)` per minute, kept for 90 days (around 130,000 readings, roughly half a megabyte on disk). Older minutes are folded into a running total.
- **Hourly statistics are derived** from the log and imported as an external statistic `powerpal_ble:<serial>_energy`, which is what the Energy dashboard reads.
- Saves are throttled to once every 30 minutes and happen on shutdown. A skipped save only means some minutes are downloaded again, and re-adding a minute is harmless.

## Flashes per kWh as a timeline

Flashes per kWh is stored as a timeline of `(from, value)` changes. Changing it for a new meter applies from a chosen moment, so energy already counted keeps the factor it was counted under. By default a change lasts until the next existing change, so correcting one period never erases a real meter swap. Changing the timeline rewrites the hourly statistics from the first affected hour onwards.

## Optional web service sync

Both directions are off by default (options flow). See [http-service.md](http-service.md) for the requests.

- **Download** imports up to 90 days on first use, a few windows at a time, then tops up the last three days once a day. A downloaded minute only fills a gap; a device value is never overwritten.
- **Upload** reconciles window by window against the service and posts only what is missing, at most 1000 readings per request and one request every 5 minutes.
- Cloud work needs no Bluetooth, so a backlog runs on its own short timer rather than causing extra Bluetooth connections.
- The credential (hardware UUID) is read over Bluetooth during setup and stored in the config entry. It is never logged.

## Bonding in the config flow

The config flow runs bonding as a progress screen that retries up to 20 times, because on a weak link several attempts may be needed. Home Assistant only continues a finished progress step when the next step id differs from the current one, so attempts alternate between two steps (`bond_a` and `bond_b`).

Errors are classified so the flow knows what to do:

| Error | Meaning | Action |
|---|---|---|
| `LinkError` | Could not connect or keep the link | Retry |
| `BondingError` | Connected but bonding failed | Retry (stale device is removed first) |
| `WrongCodeError` | Bonded, but the device stayed locked | Ask for the code again |

## Running the tests

```bash
pip install -r requirements_test.txt
python -m pytest
```

- `test_library.py`, `test_client.py`: the protocol library, with a fake Bluetooth client.
- `test_energy.py`, `test_factor_timeline.py`: storage and timeline.
- `test_cloud.py`, `test_cloud_sync.py`: the web service client and sync, with a fake service.
- `test_static.py`: checks the Home Assistant facing files for undefined names and unused imports, since no test imports them.

Anything that touches Home Assistant (coordinator, config flow, entities) is currently covered only by `test_static.py` and by running it on a real instance. Tests for those are very welcome.

## Deploying to a test instance

`scripts/deploy.ps1 <config path>` copies the integration into a mounted Home Assistant config folder. Code only reloads after a Home Assistant restart. Enable debug logging with:

```yaml
logger:
  logs:
    custom_components.powerpal_ble: debug
```
