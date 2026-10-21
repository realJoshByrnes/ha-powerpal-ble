"""BlueZ helpers for bonding with the Powerpal (Just Works, like the Android app).

On HA OS (BlueZ 5.86), with bluetoothctl: with the adapter pairable, a
NoInputNoOutput agent registered and a freshly discovered device, Just Works bonding
succeeds (no passkey is ever requested). Bleak's BlueZ backend registers no agent, the
adapter is not pairable by default, and a device object that has already failed to bond
keeps failing, hence these helpers.

Everything is best effort: when there is no system bus, no BlueZ or we are not on Linux
(dbus_fast only imports there), pairing is attempted without them.

NOTE: no `from __future__ import annotations` here; dbus_fast reads the annotations as
D-Bus type strings.
"""

import logging
from contextlib import asynccontextmanager

_LOGGER = logging.getLogger(__name__)

AGENT_PATH = "/net/powerpal/agent"
CAPABILITY = "NoInputNoOutput"
_BLUEZ = "org.bluez"


def _make_agent():
    # dbus_fast only imports on Linux (it needs socket.CMSG_LEN), so import lazily.
    from dbus_fast.service import ServiceInterface, method

    class _JustWorksAgent(ServiceInterface):
        def __init__(self) -> None:
            super().__init__("org.bluez.Agent1")

        @method()
        def Release(self):  # noqa: N802
            pass

        @method()
        def RequestConfirmation(self, device: "o", passkey: "u"):  # noqa: N802, F821
            pass  # returning without error accepts

        @method()
        def RequestAuthorization(self, device: "o"):  # noqa: N802, F821
            pass

        @method()
        def AuthorizeService(self, device: "o", uuid: "s"):  # noqa: N802, F821
            pass

        @method()
        def Cancel(self):  # noqa: N802
            pass

    return _JustWorksAgent()


async def _connect_bus():
    from dbus_fast import BusType
    from dbus_fast.aio import MessageBus

    return await MessageBus(bus_type=BusType.SYSTEM).connect()


async def _managed_objects(bus) -> dict:
    introspection = await bus.introspect(_BLUEZ, "/")
    proxy = bus.get_proxy_object(_BLUEZ, "/", introspection)
    return await proxy.get_interface("org.freedesktop.DBus.ObjectManager").call_get_managed_objects()


async def _iface(bus, path: str, name: str):
    introspection = await bus.introspect(_BLUEZ, path)
    return bus.get_proxy_object(_BLUEZ, path, introspection).get_interface(name)


@asynccontextmanager
async def pairing_agent():
    """Make adapters pairable and register a default Just Works agent for the block."""
    bus = None
    manager = None
    registered = False
    try:
        bus = await _connect_bus()
        for path, ifaces in (await _managed_objects(bus)).items():
            if "org.bluez.Adapter1" in ifaces:
                adapter = await _iface(bus, path, "org.bluez.Adapter1")
                await adapter.set_pairable(True)
        bus.export(AGENT_PATH, _make_agent())
        manager = await _iface(bus, "/org/bluez", "org.bluez.AgentManager1")
        await manager.call_register_agent(AGENT_PATH, CAPABILITY)
        registered = True
        await manager.call_request_default_agent(AGENT_PATH)
        _LOGGER.debug("Adapters pairable, Just Works agent registered")
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("BlueZ agent setup failed (%r); pairing without it", err)
    try:
        yield
    finally:
        if bus is not None:
            if registered:
                try:
                    await manager.call_unregister_agent(AGENT_PATH)
                except Exception:  # noqa: BLE001
                    pass
            bus.disconnect()


async def forget_device(address: str) -> bool:
    """Remove BlueZ's entry for `address` so the next attempt starts from a fresh object."""
    bus = None
    try:
        bus = await _connect_bus()
        for path, ifaces in (await _managed_objects(bus)).items():
            dev = ifaces.get("org.bluez.Device1")
            if dev and dev["Address"].value.upper() == address.upper():
                adapter_path = dev["Adapter"].value
                adapter = await _iface(bus, adapter_path, "org.bluez.Adapter1")
                await adapter.call_remove_device(path)
                _LOGGER.debug("Removed stale BlueZ device %s", path)
                return True
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("forget_device failed: %r", err)
    finally:
        if bus is not None:
            bus.disconnect()
    return False
