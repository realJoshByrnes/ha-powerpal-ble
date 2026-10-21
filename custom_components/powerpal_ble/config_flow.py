from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.util import dt as dt_util

from .ble import LinkError, PowerpalClient, WrongCodeError
from .factor_timeline import FOREVER
from .ble.const import (
    LED_SENSITIVITY_DEFAULT,
    LED_SENSITIVITY_MAX,
    LED_SENSITIVITY_MIN,
    PRO_NAME_PREFIX,
    SERVICE_UUID,
    SERVICE_UUID_SHORT,
)
from .const import (
    CONF_CLOUD_DOWNLOAD,
    CONF_CLOUD_UPLOAD,
    CONF_HARDWARE_UUID,
    CONF_LED_APPLY,
    CONF_LED_SENSITIVITY,
    CONF_PAIRING_CODE,
    CONF_PULSES_PER_KWH,
    CONF_SERIAL,
    DEFAULT_PULSES_PER_KWH,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

MAX_BOND_ATTEMPTS = 20
ATTEMPT_TIMEOUT = 30  # seconds per attempt: one ~10 s connect, then bond + unlock
RETRY_DELAY = 1  # seconds between attempts

PAIRING_SCHEMA = vol.Schema(
    {vol.Required(CONF_PAIRING_CODE): vol.All(int, vol.Range(min=0, max=999999))}
)


async def _optional(awaitable):
    try:
        return await awaitable
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Optional device read failed: %r", err)
        return None


def _is_pro(info: BluetoothServiceInfoBleak) -> bool:
    return (info.name or "").startswith(PRO_NAME_PREFIX)


def _local(timestamp: int) -> str:
    return dt_util.as_local(dt_util.utc_from_timestamp(timestamp)).strftime("%d %b %Y %H:%M")


def _to_timestamp(value: str) -> int:
    """A date and time chosen in the form (Home Assistant's time zone) as a unix timestamp."""
    parsed = dt_util.parse_datetime(value)
    if parsed is None:
        raise ValueError("not a date and time")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_util.get_default_time_zone())
    return int(parsed.timestamp())


class PowerpalOptionsFlow(OptionsFlow):
    """Optional features (off by default) and the flashes-per-kWh timeline."""

    def __init__(self) -> None:
        self._new_timeline = None

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["flashes", "web_service"])

    async def async_step_web_service(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data={**self.config_entry.options, **user_input})
        return self.async_show_form(
            step_id="web_service",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_CLOUD_DOWNLOAD,
                        default=self.config_entry.options.get(CONF_CLOUD_DOWNLOAD, False),
                    ): bool,
                    vol.Required(
                        CONF_CLOUD_UPLOAD,
                        default=self.config_entry.options.get(CONF_CLOUD_UPLOAD, False),
                    ): bool,
                }
            ),
        )

    async def async_step_flashes(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Change flashes per kWh from a chosen date and time.

        The value is a timeline, so a new meter (or a move) changes it from that moment on and
        energy counted earlier keeps the factor it was counted under. By default a change lasts
        until the next existing change, so correcting one period never erases real meter swaps.
        """
        coordinator = getattr(self.config_entry, "runtime_data", None)
        if coordinator is None:
            return self.async_abort(reason="not_loaded")
        timeline = coordinator.timeline
        origin = coordinator.oldest_reading
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                start = _to_timestamp(user_input["from"])
                if user_input.get("through_end"):
                    until = FOREVER
                elif user_input.get("until"):
                    until = _to_timestamp(user_input["until"])
                else:
                    until = None
                if start > int(time.time()) + 60:
                    errors["base"] = "start_in_future"
                elif coordinator.earliest_editable and start < coordinator.earliest_editable:
                    errors["base"] = "too_old"
                else:
                    new = timeline.with_value(user_input["factor"], start, until, origin=origin)
                    if timeline.changed_from(new) is None:
                        return self.async_create_entry(data=self.config_entry.options)  # no change
                    self._new_timeline = new
                    return await self.async_step_confirm()
            except ValueError:
                errors["base"] = "bad_range"
        return self.async_show_form(
            step_id="flashes",
            data_schema=vol.Schema(
                {
                    vol.Required("factor", default=timeline.current): vol.All(int, vol.Range(min=1)),
                    vol.Required(
                        "from", default=dt_util.now().strftime("%Y-%m-%d %H:%M:%S")
                    ): selector.DateTimeSelector(),
                    vol.Optional("until"): selector.DateTimeSelector(),
                    vol.Optional("through_end", default=False): bool,
                }
            ),
            description_placeholders={
                "timeline": "\n".join(f"- {line}" for line in timeline.describe(_local, origin)),
                "oldest": _local(coordinator.earliest_editable) if coordinator.earliest_editable else "the first reading",
            },
            errors=errors,
        )

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        coordinator = self.config_entry.runtime_data
        new = self._new_timeline
        if user_input is not None:
            await coordinator.async_set_timeline(new)
            return self.async_create_entry(data=self.config_entry.options)
        old = coordinator.timeline
        origin = coordinator.oldest_reading
        lines = []
        for change in old.changes_to(new):
            begin = _local(change.start) if change.start else (_local(origin) if origin else "the beginning")
            span = f"{begin} onwards" if change.end >= FOREVER else f"{begin} to {_local(change.end)}"
            lines.append(f"- {span}: {change.old} to {change.new}")
        removed = old.removed_boundaries(new)
        if removed:
            lines.append(
                "This ERASES the recorded meter change(s) at: " + ", ".join(_local(t) for t, _ in removed)
            )
        before, after = coordinator.preview(new)
        lines.append(f"The last 7 days of energy would change from {before:.1f} kWh to {after:.1f} kWh.")
        first = old.changed_from(new)
        lines.append(f"Hourly statistics are rewritten from {_local(first)} onwards; earlier hours are untouched.")
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={"summary": "\n".join(lines)},
        )


class PowerpalConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> PowerpalOptionsFlow:
        return PowerpalOptionsFlow()

    def __init__(self) -> None:
        self._discovered: dict[str, BluetoothServiceInfoBleak] = {}
        self._address: str | None = None
        self._settings: dict[str, Any] = {}
        self._serial: str | None = None
        self._led: int | None = None  # LED sensitivity read from the device
        self._uuid_raw: bytes | None = None  # hardware UUID read from the device
        self._attempt = 0
        self._last_error = "none yet"
        self._bond_task: asyncio.Task[None] | None = None
        self._form_error: str | None = None

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        if _is_pro(discovery_info):
            return self.async_abort(reason="unsupported_device")
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._address = discovery_info.address
        self.context["title_placeholders"] = {"name": discovery_info.name or "Powerpal"}
        return await self.async_step_configure()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(self._address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return await self.async_step_configure()

        current = self._async_current_ids()
        for info in async_discovered_service_info(self.hass, connectable=True):
            uuids = {u.lower() for u in info.service_uuids}
            if (
                info.address not in current
                and not _is_pro(info)
                and uuids & {SERVICE_UUID, SERVICE_UUID_SHORT}
            ):
                self._discovered[info.address] = info
        if not self._discovered:
            return self.async_abort(reason="no_devices_found")
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): vol.In(
                        {a: f"{i.name or 'Powerpal'} ({a})" for a, i in self._discovered.items()}
                    )
                }
            ),
        )

    async def async_step_configure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._settings = {CONF_PAIRING_CODE: user_input[CONF_PAIRING_CODE]}
            self._attempt = 0
            self._last_error = "none yet"
            self._form_error = None
            return await self.async_step_bond_a()
        errors = {"base": self._form_error} if self._form_error else {}
        return self.async_show_form(
            step_id="configure", data_schema=PAIRING_SCHEMA, errors=errors
        )

    async def _async_try_bond(self) -> None:
        """One connect + bond + unlock attempt. Raises on failure."""
        if self._attempt > 1:
            await asyncio.sleep(RETRY_DELAY)
        device = async_ble_device_from_address(self.hass, self._address, connectable=True)
        if device is None:
            raise LinkError("device not currently visible to Bluetooth")
        async with asyncio.timeout(ATTEMPT_TIMEOUT):
            async with PowerpalClient(device, self._settings[CONF_PAIRING_CODE]) as client:
                self._serial = await client.read_serial()
                # Optional extras: a failure here must not fail the bond.
                self._led = await _optional(client.read_led_sensitivity())
                self._uuid_raw = await _optional(client.read_hardware_uuid())

    # HA only continues a finished progress step, and refreshes the browser, when the next
    # step id differs from the current one (data_entry_flow.py). Retrying "bond" -> "bond"
    # stalls after the first attempt, so attempts alternate between two step ids.
    async def async_step_bond_a(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_bond_step("bond_a", "bond_b")

    async def async_step_bond_b(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_bond_step("bond_b", "bond_a")

    async def _async_bond_step(self, step_id: str, retry_step_id: str) -> ConfigFlowResult:
        """Run up to MAX_BOND_ATTEMPTS attempts, one per progress screen."""
        if self._bond_task is None:
            self._attempt += 1
            self._bond_task = self.hass.async_create_task(self._async_try_bond())

        if not self._bond_task.done():
            return self.async_show_progress(
                step_id=step_id,
                progress_action="bonding",
                description_placeholders={
                    "attempt": str(self._attempt),
                    "max": str(MAX_BOND_ATTEMPTS),
                    "last_error": self._last_error,
                },
                progress_task=self._bond_task,
            )

        task, self._bond_task = self._bond_task, None
        try:
            task.result()
        except WrongCodeError:
            _LOGGER.warning("Wrong pairing code for %s", self._address)
            self._form_error = "invalid_auth"
            return self.async_show_progress_done(next_step_id="configure")
        except (LinkError, TimeoutError, asyncio.CancelledError) as err:
            self._last_error = str(err) or type(err).__name__
            _LOGGER.debug(
                "Bonding attempt %d/%d failed: %s", self._attempt, MAX_BOND_ATTEMPTS, self._last_error
            )
            if self._attempt >= MAX_BOND_ATTEMPTS:
                return self.async_show_progress_done(next_step_id="bond_failed")
            return self.async_show_progress_done(next_step_id=retry_step_id)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error bonding with %s", self._address)
            self._last_error = "unexpected error, see the log"
            return self.async_show_progress_done(next_step_id="bond_failed")
        return self.async_show_progress_done(next_step_id="settings")

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm the settings, with what the device reported already filled in.

        LED sensitivity is stored on the device, so it is read. Flashes per kWh is NOT stored on
        the device (it is printed on the meter and kept in the phone app/cloud account), so it
        starts at the usual 1000 and has to be confirmed by the user.
        """
        if user_input is not None:
            data = {
                CONF_ADDRESS: self._address,
                CONF_SERIAL: self._serial,
                CONF_PAIRING_CODE: self._settings[CONF_PAIRING_CODE],
                CONF_PULSES_PER_KWH: user_input[CONF_PULSES_PER_KWH],
                CONF_LED_SENSITIVITY: user_input[CONF_LED_SENSITIVITY],
            }
            if self._uuid_raw:
                data[CONF_HARDWARE_UUID] = self._uuid_raw.hex()
            if self._led is not None and user_input[CONF_LED_SENSITIVITY] != self._led:
                data[CONF_LED_APPLY] = True  # the user changed it: write it to the device once
            return self.async_create_entry(title="Powerpal", data=data)
        led = self._led if self._led is not None else LED_SENSITIVITY_DEFAULT
        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PULSES_PER_KWH, default=DEFAULT_PULSES_PER_KWH): vol.All(
                        int, vol.Range(min=1)
                    ),
                    vol.Required(CONF_LED_SENSITIVITY, default=led): vol.All(
                        int, vol.Range(min=LED_SENSITIVITY_MIN, max=LED_SENSITIVITY_MAX)
                    ),
                }
            ),
            description_placeholders={
                "led_source": "read from your Powerpal"
                if self._led is not None
                else "the default, because it could not be read from the Powerpal"
            },
        )

    async def async_step_bond_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_abort(
            reason="bonding_failed",
            description_placeholders={
                "max": str(MAX_BOND_ATTEMPTS),
                "last_error": self._last_error,
            },
        )

    def async_remove(self) -> None:
        if self._bond_task and not self._bond_task.done():
            self._bond_task.cancel()
