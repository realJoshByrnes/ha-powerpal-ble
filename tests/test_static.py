"""Static checks over the Home Assistant-facing code, which no other test imports.

A refactor once left code reading `self._ppk` after it was removed. It compiled, every other test
passed, and it would have crashed the integration on load. These checks find that class of mistake
without needing Home Assistant: names that are used but never defined.
"""

import ast
import io
import pathlib

from pyflakes import api, reporter

PACKAGE = pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "powerpal_ble"

# D-Bus type strings that dbus_fast requires as annotations (not real names).
DBUS_TYPES = {"o", "u", "s", "q"}


def test_no_undefined_or_unused_names_in_the_integration():
    problems = []
    for path in sorted(PACKAGE.rglob("*.py")):
        out = io.StringIO()
        api.check(path.read_text(), str(path), reporter.Reporter(out, out))
        for line in out.getvalue().splitlines():
            if path.name == "agent.py" and any(f"undefined name '{t}'" in line for t in DBUS_TYPES):
                continue
            problems.append(line)
    assert not problems, "\n".join(problems)


# Attributes the Home Assistant base classes provide.
INHERITED_COMMON = {
    "hass", "config_entry", "context", "flow_id", "handler", "source", "unique_id", "data",
    "_abort_if_unique_id_configured",
}
INHERITED_COORDINATOR = INHERITED_COMMON | {
    "update_interval", "last_update_success", "logger", "name", "last_exception",
    "always_update",
}


def _undefined_self_attributes(path, class_names, inherited):
    tree = ast.parse(path.read_text())
    bad = {}
    for cls in (n for n in tree.body if isinstance(n, ast.ClassDef) and n.name in class_names):
        defined = set(inherited)
        for node in cls.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defined.add(node.name)
            elif isinstance(node, ast.Assign):
                defined |= {t.id for t in node.targets if isinstance(t, ast.Name)}
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                defined.add(node.target.id)
        reads = {}
        for node in ast.walk(cls):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
                if isinstance(node.ctx, ast.Store):
                    defined.add(node.attr)
                else:
                    reads.setdefault(node.attr, node.lineno)
        for attr, line in reads.items():
            # methods inherited from Home Assistant: async_show_form, async_create_entry, ...
            if attr not in defined and not attr.startswith(("async_", "_async_")):
                bad[f"{cls.name}.{attr}"] = line
    return bad


def test_coordinator_reads_only_attributes_it_defines():
    bad = _undefined_self_attributes(PACKAGE / "coordinator.py", {"PowerpalCoordinator"}, INHERITED_COORDINATOR)
    assert not bad, f"read but never defined: {bad}"


def test_flows_read_only_attributes_they_define():
    bad = _undefined_self_attributes(
        PACKAGE / "config_flow.py", {"PowerpalConfigFlow", "PowerpalOptionsFlow"}, INHERITED_COMMON
    )
    assert not bad, f"read but never defined: {bad}"


def test_entities_read_only_attributes_they_define():
    inherited = INHERITED_COMMON | {"coordinator", "entity_description", "platform"}
    for name, classes in {
        "number.py": {"PowerpalLedSensitivity"},
        "text.py": {"PowerpalSecret"},
        "button.py": {"PowerpalUpdateButton"},
        "sensor.py": {"PowerpalSensor"},
        "entity.py": {"PowerpalEntity"},
    }.items():
        bad = _undefined_self_attributes(PACKAGE / name, classes, inherited)
        assert not bad, f"{name}: read but never defined: {bad}"


def test_the_single_value_factor_is_really_gone():
    text = (PACKAGE / "coordinator.py").read_text()
    assert "self._ppk" not in text and 'saved.get("ppk")' not in text
