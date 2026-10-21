"""Poll the device: connect, read live state, fetch new history, disconnect."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from homeassistant.components.bluetooth import (
    async_ble_device_from_address,
    async_last_service_info,
)
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .ble import PowerpalClient, PowerpalError, PowerpalState, live_watts, reading_wh
from .cloud import CloudAuthError, CloudError, PowerpalCloud, find_token, token_candidates
from .cloud_sync import CloudSync
from .const import (
    CONF_CLOUD_DOWNLOAD,
    CONF_CLOUD_TOKEN,
    CONF_CLOUD_UPLOAD,
    CONF_HARDWARE_UUID,
    CONF_LED_APPLY,
    CONF_LED_SENSITIVITY,
    CONF_PAIRING_CODE,
    CONF_PULSES_PER_KWH,
    CONF_SERIAL,
    DOMAIN,
    UPDATE_INTERVAL_MINUTES,
)
from .energy import MinuteLog
from .factor_timeline import FactorTimeline

_LOGGER = logging.getLogger(__name__)

# After a failed poll retry sooner, with backoff, then fall back to the normal interval. A failed
# connect costs the Powerpal's battery almost nothing (it advertises regardless); time spent
# *connected* is what costs, so a dead link is not hammered.
RETRY_BACKOFF_SECONDS = (60, 120)
STALE_AFTER_SECONDS = 15 * 60  # keep the last values this long without a successful poll

# History: the first poll imports up to BACKFILL_DAYS of what the device has stored. Chunks are
# large and fetched back to back on one connection (connecting is what is slow and unreliable on
# a weak link, not downloading); if a backlog still remains, poll again quickly.
BACKFILL_DAYS = 90  # what the Powerpal app itself imports on first use
CHUNK_MINUTES = 1440  # measured: 360 readings took ~7 s including the connect; connecting is the slow part
MAX_CHUNKS_PER_POLL = 4  # bounds how long one session stays connected
CATCH_UP_SECONDS = 60
EMPTY_WINDOWS_BEFORE_SKIP = 3  # a window with no readings is skipped after this many tries

# The minute log is the primary store. Minutes older than this are folded into a running base
# (their hourly statistics have long been imported). 90 days is about 130,000 readings, at most
# ~0.5 MB on disk.
RETENTION_DAYS = 90
# Rewriting that file on every 5-minute poll would be ~150 MB/day. Save at most this often (and
# on shutdown). A skipped save costs nothing: the log and last_timestamp are saved together, so
# the missing minutes are simply downloaded again, and re-adding a minute is harmless.
SAVE_EVERY_SECONDS = 30 * 60

# Optional web-service features (both off by default); the logic is in cloud_sync.py. Cloud work
# does not need Bluetooth, so while there is a cloud backlog it runs on its own short timer instead
# of making extra Bluetooth polls (every poll is a connection to a battery-powered device).
CLOUD_FOLLOWUP_SECONDS = 30
CLOUD_RETRY_SECONDS = 6 * 3600  # after the service rejects every credential


def _resolve_serial(hass: HomeAssistant, entry: ConfigEntry) -> str:
    """Identity for entities, known without connecting (setup must never wait on Bluetooth)."""
    if serial := entry.data.get(CONF_SERIAL):
        return serial
    # Older entries: reuse the identity of the device already registered for this entry.
    for device in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id):
        for domain, identifier in device.identifiers:
            if domain == DOMAIN:
                return identifier
    # Last resort: the advertised name is "Powerpal <serial-hex>".
    info = async_last_service_info(hass, entry.data[CONF_ADDRESS], connectable=True)
    if info and (match := re.search(r"([0-9a-fA-F]{8})\s*$", info.name or "")):
        return match.group(1).upper()
    return entry.data[CONF_ADDRESS].replace(":", "")


@dataclass
class PowerpalData:
    state: PowerpalState
    watts: float
    total_kwh: float


class PowerpalCoordinator(DataUpdateCoordinator[PowerpalData]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES),
        )
        self._store: Store = Store(hass, 1, f"{DOMAIN}_{entry.entry_id}")
        self._last_timestamp = 0  # newest reading we have counted (unix s)
        self._log = MinuteLog()  # primary storage: one (timestamp, pulses) per minute
        self._pending_hours: set[int] = set()  # hours changed but not yet written to statistics
        self._empty_windows = 0
        self._failures = 0
        self._last_success = time.monotonic()
        self.last_poll_error: str | None = None
        self._lock = asyncio.Lock()  # one Bluetooth session at a time
        self.serial = _resolve_serial(hass, entry)
        # Flashes per kWh over time (see factor_timeline.py). The value entered at setup is the
        # first entry; later changes are dated edits, so old pulses keep their own factor.
        self._timeline = FactorTimeline(initial=entry.data[CONF_PULSES_PER_KWH])
        self._options = dict(entry.options)  # tells an options change from other entry updates
        self._sync = CloudSync(self._log, self._wh)
        self._followup_unsub = None  # timer for cloud work between polls
        self._raw_uuid: bytes | None = None  # read over BLE, only until the credential is derived
        self._cloud_retry_at = 0.0
        self._last_save = float("-inf")  # monotonic time of the last scheduled save (first one always runs)

    def _wh(self, timestamp: int, pulses: int) -> float:
        """Energy of one minute: the factor depends on when the pulses were counted."""
        return reading_wh(pulses, self._timeline.factor_at(timestamp))

    @property
    def statistic_id(self) -> str:
        return f"{DOMAIN}:{self.serial.lower()}_energy"

    @property
    def power_statistic_id(self) -> str:
        return f"{DOMAIN}:{self.serial.lower()}_power"

    async def async_load(self) -> None:
        saved = await self._store.async_load()
        if saved and "runs" in saved:
            self._last_timestamp = saved["last_timestamp"]
            self._log = MinuteLog.from_dict(saved)
            # The timeline must be restored first: it decides what every stored minute is worth.
            if "ppk_timeline" in saved:
                self._timeline = FactorTimeline.from_dict(saved["ppk_timeline"])
            if "power_stats" not in saved:
                # Hourly power statistics are new: write them for all the history we hold.
                self._pending_hours |= set(self._log.hourly_wh(self._wh))
            self._sync = CloudSync(
                self._log,
                self._wh,
                saved.get("cloud_cursor", 0),
                saved.get("cloud_checked", 0),
                saved.get("upload_cursor", 0),
            )

    def _data_to_save(self) -> dict:
        return {
            "last_timestamp": self._last_timestamp,
            "ppk_timeline": self._timeline.to_dict(),
            "power_stats": True,
            **self._sync.to_dict(),
            **self._log.to_dict(),
        }

    def _maybe_save(self, backlog: bool) -> None:
        now = time.monotonic()
        if backlog or now - self._last_save >= SAVE_EVERY_SECONDS:
            self._last_save = now
            self._store.async_delay_save(self._data_to_save, 5)

    async def async_flush(self) -> None:
        """Write the log now (shutdown, or the entry is being removed)."""
        await self._store.async_save(self._data_to_save())

    async def _async_update_data(self) -> PowerpalData:
        async with self._lock:
            return await self._poll()

    async def _poll(self) -> PowerpalData:
        cfg = self.config_entry.data
        device = async_ble_device_from_address(self.hass, cfg[CONF_ADDRESS], connectable=True)
        if device is None:
            return self._handle_failure(PowerpalError("Powerpal not in Bluetooth range"))
        ppk = self._timeline.current  # live power uses the factor in force now
        try:
            async with PowerpalClient(device, cfg[CONF_PAIRING_CODE], connect_attempts=3) as client:
                state = await client.read_state()
                if state.device_time == 0:
                    await client.set_clock()  # device rebooted; stored timestamps were unreliable
                state = await self._apply_pending_led(client, state)
                more = await self._sync_history(client, state)
                if self._raw_uuid is None and (
                    (self._cloud_wanted and not self._token) or not cfg.get(CONF_HARDWARE_UUID)
                ):
                    try:
                        self._raw_uuid = await client.read_hardware_uuid()
                    except Exception as err:  # noqa: BLE001  (optional feature: never fail a poll)
                        _LOGGER.debug("Could not read the hardware UUID: %r", err)
        except Exception as err:  # noqa: BLE001
            return self._handle_failure(err)
        self._failures = 0
        self._last_success = time.monotonic()
        self.last_poll_error = None
        self._persist_hardware_uuid()
        await self._cloud_step()  # before the import, so merged hours are written in the same go
        self.update_interval = (
            timedelta(seconds=CATCH_UP_SECONDS)
            if more
            else timedelta(minutes=UPDATE_INTERVAL_MINUTES)
        )
        self._update_device_registry(state)
        self._import_statistics()
        # Only after the import: pruned hours are final.
        self._log.prune(self._last_timestamp, RETENTION_DAYS * 86400, self._wh)
        self._maybe_save(backlog=more)
        self._schedule_cloud_followup()
        return PowerpalData(
            state=state,
            watts=live_watts(state.live_interval_ms, ppk),
            total_kwh=self._log.total_wh(self._wh) / 1000,
        )

    async def _apply_pending_led(self, client: PowerpalClient, state: PowerpalState) -> PowerpalState:
        """If the user changed LED sensitivity in the setup form, write it to the device once."""
        cfg = self.config_entry.data
        if not cfg.get(CONF_LED_APPLY):
            return state
        wanted = cfg.get(CONF_LED_SENSITIVITY)
        try:
            if wanted is not None and state.led_sensitivity != wanted:
                state = replace(state, led_sensitivity=await client.write_led_sensitivity(wanted))
        except Exception as err:  # noqa: BLE001  (try again on the next poll)
            _LOGGER.debug("Could not apply the LED sensitivity yet: %r", err)
            return state
        self.hass.config_entries.async_update_entry(
            self.config_entry, data={k: v for k, v in cfg.items() if k != CONF_LED_APPLY}
        )
        return state

    async def async_set_led_sensitivity(self, value: int) -> None:
        """Write LED sensitivity to the device now, and read it back."""
        async with self._lock:
            device = async_ble_device_from_address(
                self.hass, self.config_entry.data[CONF_ADDRESS], connectable=True
            )
            if device is None:
                raise HomeAssistantError("The Powerpal is not in Bluetooth range")
            try:
                async with PowerpalClient(
                    device, self.config_entry.data[CONF_PAIRING_CODE], connect_attempts=3
                ) as client:
                    actual = await client.write_led_sensitivity(value)
            except PowerpalError as err:
                raise HomeAssistantError(f"Could not set the LED sensitivity: {err}") from err
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data={**self.config_entry.data, CONF_LED_SENSITIVITY: actual},
            )
            if self.data is not None:
                self.async_set_updated_data(
                    replace(self.data, state=replace(self.data.state, led_sensitivity=actual))
                )
        if actual != value:
            raise HomeAssistantError(f"The Powerpal reports {actual} after setting {value}")

    def _persist_hardware_uuid(self) -> None:
        """Remember the hardware UUID (for display), and note which byte order the service accepts."""
        if self._raw_uuid is None:
            return
        if not self.config_entry.data.get(CONF_HARDWARE_UUID):
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data={**self.config_entry.data, CONF_HARDWARE_UUID: self._raw_uuid.hex()},
            )
        if self._token:
            forms = token_candidates(self._raw_uuid)
            _LOGGER.debug(
                "Credential form that the service accepts: %s",
                forms.index(self._token) if self._token in forms else "none of the candidates",
            )

    @property
    def timeline(self) -> FactorTimeline:
        return self._timeline

    @property
    def oldest_reading(self) -> int:
        """Timestamp of the oldest minute we hold (0 if none)."""
        return next(self._log.readings(), (0, 0))[0]

    @property
    def earliest_editable(self) -> int:
        """Older minutes are folded into a running total and cannot be recomputed."""
        if not self._last_timestamp:
            return 0
        return self._log.hour_of(self._last_timestamp - RETENTION_DAYS * 86400)

    def preview(self, new: FactorTimeline, days: int = 7) -> tuple[float, float]:
        """kWh over the last `days` days under the current timeline and under `new`."""
        since = (self._last_timestamp or 0) - days * 86400
        old_kwh = self._log.wh_since(since, self._wh) / 1000
        new_kwh = self._log.wh_since(since, lambda ts, p: reading_wh(p, new.factor_at(ts))) / 1000
        return old_kwh, new_kwh

    async def async_set_timeline(self, new: FactorTimeline) -> None:
        """Install a reviewed timeline and rewrite only the hours it changes.

        The change is dated, so energy before it keeps the factor it was counted under. Statistics
        are rewritten from the first affected hour on (their cumulative sums shift from there).
        """
        async with self._lock:
            changed = self._timeline.changed_from(new)
            self._timeline = new
            if changed is not None:
                first_hour = self._log.hour_of(changed)
                self._pending_hours |= {
                    hour for hour in self._log.hourly_wh(self._wh) if hour >= first_hour
                }
                self._import_statistics()
            await self._store.async_save(self._data_to_save())
            if self.data is not None:
                self.async_set_updated_data(
                    replace(
                        self.data,
                        total_kwh=self._log.total_wh(self._wh) / 1000,
                        watts=live_watts(self.data.state.live_interval_ms, new.current),
                    )
                )

    @property
    def options_changed(self) -> bool:
        return dict(self.config_entry.options) != self._options

    @property
    def _cloud_download(self) -> bool:
        return bool(self.config_entry.options.get(CONF_CLOUD_DOWNLOAD, False))

    @property
    def _cloud_upload(self) -> bool:
        return bool(self.config_entry.options.get(CONF_CLOUD_UPLOAD, False))

    @property
    def _cloud_wanted(self) -> bool:
        return self._cloud_download or self._cloud_upload

    @property
    def _token(self) -> str | None:
        return self.config_entry.data.get(CONF_CLOUD_TOKEN)

    def _cloud_backlog(self) -> bool:
        if not (self._cloud_wanted and self._token):
            return False
        return (
            self._cloud_download and self._sync.download_backlog(int(time.time()))
        ) or (self._cloud_upload and self._sync.upload_backlog(self._last_timestamp))

    async def _cloud_client(self) -> PowerpalCloud | None:
        """A client for the web service, deriving and storing the credential on first use."""
        session = async_get_clientsession(self.hass)
        token = self._token
        if not token:
            if self._raw_uuid is None or time.monotonic() < self._cloud_retry_at:
                return None
            token = await find_token(session, self.serial, self._raw_uuid, int(time.time()))
            self._raw_uuid = None
            if token is None:
                self._cloud_retry_at = time.monotonic() + CLOUD_RETRY_SECONDS
                _LOGGER.warning("The Powerpal web service did not accept this device's credential")
                return None
            self.hass.config_entries.async_update_entry(
                self.config_entry, data={**self.config_entry.data, CONF_CLOUD_TOKEN: token}
            )
        return PowerpalCloud(session, self.serial, token)

    async def _cloud_step(self) -> None:
        """Download and/or upload, per the options. Never fails a poll."""
        if not self._cloud_wanted:
            return
        try:
            cloud = await self._cloud_client()
            if cloud is None:
                return
            if self._cloud_download:
                not_before = 0
                if self._last_timestamp:
                    not_before = self._log.hour_of(self._last_timestamp - RETENTION_DAYS * 86400)
                added = await self._sync.download(
                    cloud, int(time.time()), BACKFILL_DAYS, not_before, self._pending_hours
                )
                if added:
                    _LOGGER.debug("Merged %d minutes from the web service", added)
            if self._cloud_upload:
                posted = await self._sync.upload(cloud, self._last_timestamp, time.monotonic())
                if posted:
                    _LOGGER.debug("Uploaded %d readings to the web service", posted)
        except CloudAuthError:
            _LOGGER.warning("The Powerpal web service rejected the stored credential; will re-derive it")
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data={k: v for k, v in self.config_entry.data.items() if k != CONF_CLOUD_TOKEN},
            )
        except CloudError as err:
            _LOGGER.debug("Web service sync failed, will retry: %s", err)

    def _schedule_cloud_followup(self) -> None:
        """Keep working through a cloud backlog between polls, without touching Bluetooth."""
        if self._followup_unsub is not None or not self._cloud_backlog():
            return
        delay = float(CLOUD_FOLLOWUP_SECONDS)
        if not (self._cloud_download and self._sync.download_backlog(int(time.time()))):
            # Only uploading is left: it is held back for 5 minutes after each upload, but a
            # window with nothing to send needs no such wait.
            delay = max(delay, self._sync.seconds_until_next_post(time.monotonic()))
        self._followup_unsub = async_call_later(self.hass, delay, self._cloud_followup)

    async def _cloud_followup(self, _now: object) -> None:
        self._followup_unsub = None
        async with self._lock:
            await self._cloud_step()
            self._import_statistics()
            self._maybe_save(backlog=False)
        self._schedule_cloud_followup()

    async def async_shutdown(self) -> None:
        if self._followup_unsub is not None:
            self._followup_unsub()
            self._followup_unsub = None
        await super().async_shutdown()

    def _update_device_registry(self, state: PowerpalState) -> None:
        if state.serial != self.serial:
            _LOGGER.warning("Device serial %s differs from entity identity %s", state.serial, self.serial)
        elif not self.config_entry.data.get(CONF_SERIAL):
            self.hass.config_entries.async_update_entry(
                self.config_entry, data={**self.config_entry.data, CONF_SERIAL: state.serial}
            )
        registry = dr.async_get(self.hass)
        for device in dr.async_entries_for_config_entry(registry, self.config_entry.entry_id):
            if (DOMAIN, self.serial) in device.identifiers and device.sw_version != state.firmware:
                registry.async_update_device(device.id, sw_version=state.firmware)

    def _advert_hint(self) -> str:
        info = async_last_service_info(self.hass, self.config_entry.data[CONF_ADDRESS], connectable=True)
        if info is None:
            return "no advertisement heard"
        return f"last advertisement {int(time.monotonic() - info.time)}s ago at {info.rssi} dBm"

    def _handle_failure(self, err: Exception) -> PowerpalData:
        """Keep the last data through failed polls (weak link) and retry sooner.

        Nothing is lost by a missed poll: readings stay on the device and are fetched next time.
        """
        self._failures += 1
        # Say whether the adapter can still hear the device: "can't hear it" means out of range
        # or off, "hears it but can't connect" means busy (another connection) or interference.
        detail = f"{err or type(err).__name__} [{self._advert_hint()}]"
        self.last_poll_error = detail
        backoff = RETRY_BACKOFF_SECONDS[min(self._failures - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
        if self._failures > len(RETRY_BACKOFF_SECONDS):
            self.update_interval = timedelta(minutes=UPDATE_INTERVAL_MINUTES)
        else:
            self.update_interval = timedelta(seconds=backoff)
        stale = time.monotonic() - self._last_success > STALE_AFTER_SECONDS
        if self.data is not None and not stale:
            _LOGGER.debug(
                "Poll failed (%d in a row), keeping last values, next try in %s: %s",
                self._failures,
                self.update_interval,
                detail,
            )
            return self.data
        raise UpdateFailed(detail) from err

    async def _sync_history(self, client: PowerpalClient, state: PowerpalState) -> bool:
        """Download missing history, chunk after chunk on this connection. True if more remains."""
        for _ in range(MAX_CHUNKS_PER_POLL):
            more, count = await self._sync_chunk(client, state)
            if not more:
                return False
            if count == 0:
                return True  # a flaky link or a gap: stop and let the next poll retry
        return True

    async def _sync_chunk(self, client: PowerpalClient, state: PowerpalState) -> tuple[bool, int]:
        """Download one chunk. Returns (more remains, readings received)."""
        if not state.first_record or not state.last_record:
            return False, 0  # the device's buffer is empty
        if not self._last_timestamp:
            # First run: begin BACKFILL_DAYS back (or at the device's oldest reading).
            first = max(state.first_record, state.last_record - BACKFILL_DAYS * 86400)
            self._last_timestamp = first - 60
            _LOGGER.debug("First sync, backfilling from %s", datetime.fromtimestamp(first, UTC))
        start = max(self._last_timestamp + 60, state.first_record)
        if start > state.last_record:
            return False, 0
        end = min(start + CHUNK_MINUTES * 60 - 60, state.last_record)

        count = 0

        def _add(reading) -> None:
            nonlocal count
            if reading.timestamp > self._last_timestamp:
                self._pending_hours.add(self._log.add(reading.timestamp, reading.pulses))
                self._last_timestamp = reading.timestamp
                count += 1

        # Counted as they arrive, so a dropped link keeps the progress made so far.
        await client.download_history(start, end, on_reading=_add)
        _LOGGER.debug(
            "History %s .. %s: %d readings (device holds %s .. %s)",
            datetime.fromtimestamp(start, UTC),
            datetime.fromtimestamp(end, UTC),
            count,
            datetime.fromtimestamp(state.first_record, UTC),
            datetime.fromtimestamp(state.last_record, UTC),
        )
        if count == 0:
            # Either a real gap in the device's records or a flaky link; skip it only after
            # several empty tries so a bad link cannot silently lose data.
            self._empty_windows += 1
            if self._empty_windows >= EMPTY_WINDOWS_BEFORE_SKIP:
                self._last_timestamp = end
                self._empty_windows = 0
        else:
            self._empty_windows = 0
        return self._last_timestamp < state.last_record, count

    def _import_statistics(self) -> None:
        """Write the hours that changed to Home Assistant's long-term statistics."""
        if not self._pending_hours:
            return
        rows = self._log.rows(min(self._pending_hours), self._wh)
        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"Powerpal {self.serial} energy",
            source=DOMAIN,
            statistic_id=self.statistic_id,
            unit_class="energy",
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        )
        statistics = [
            StatisticData(start=datetime.fromtimestamp(hour, UTC), state=wh / 1000, sum=wh / 1000)
            for hour, wh in rows
        ]
        async_add_external_statistics(self.hass, metadata, statistics)
        _LOGGER.debug("Imported %d hourly statistics for %s", len(statistics), self.statistic_id)

        # Power as hourly mean, min and max in watts: the lowest and highest demand in each hour.
        # (Min and max need a power unit, which an energy statistic cannot carry.)
        power = self._log.hourly_power(self._wh, only=self._pending_hours)
        async_add_external_statistics(
            self.hass,
            StatisticMetaData(
                mean_type=StatisticMeanType.ARITHMETIC,
                has_sum=False,
                name=f"Powerpal {self.serial} power",
                source=DOMAIN,
                statistic_id=self.power_statistic_id,
                unit_class="power",
                unit_of_measurement=UnitOfPower.WATT,
            ),
            [
                StatisticData(start=datetime.fromtimestamp(hour, UTC), mean=mean, min=low, max=high)
                for hour, (mean, low, high) in sorted(power.items())
            ],
        )

        self._pending_hours.clear()
