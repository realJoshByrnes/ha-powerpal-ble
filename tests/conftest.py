"""Load the BLE library standalone as `powerpal_ble`, without importing Home Assistant."""

import importlib.util
import pathlib
import sys

pkg = pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "powerpal_ble" / "ble"
spec = importlib.util.spec_from_file_location(
    "powerpal_ble", pkg / "__init__.py", submodule_search_locations=[str(pkg)]
)
mod = importlib.util.module_from_spec(spec)
sys.modules["powerpal_ble"] = mod
spec.loader.exec_module(mod)
