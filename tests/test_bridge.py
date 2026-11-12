"""Picking the serial bridge's entities from an ESPHome device."""

import importlib.util
import pathlib
import sys

_path = pathlib.Path(__file__).parents[1] / "custom_components" / "orisec" / "bridge.py"
_spec = importlib.util.spec_from_file_location("orisec_bridge", _path)
bridge = importlib.util.module_from_spec(_spec)
sys.modules["orisec_bridge"] = bridge
_spec.loader.exec_module(bridge)
E = bridge.EntityInfo


def test_picks_the_bridge_yaml_entities():
    ents = [
        E("binary_sensor.b_status", "binary_sensor", "connectivity", "Status"),
        E("binary_sensor.b_link", "binary_sensor", "connectivity", "Panel link connected"),
        E("sensor.b_wifi", "sensor", "signal_strength", "WiFi signal"),
        E("sensor.b_up", "sensor", "duration", "Uptime"),
        E("sensor.b_clients", "sensor", None, "Panel link clients"),
        E("sensor.b_ip", "sensor", None, "IP address"),
        E("button.b_restart", "button", "restart", "Restart"),
    ]
    assert bridge.pick_bridge_entities(ents) == {
        "wifi": "sensor.b_wifi", "uptime": "sensor.b_up",
        "link": "binary_sensor.b_link", "clients": "sensor.b_clients",
    }


def test_status_is_never_the_link_and_fallbacks_work():
    ents = [
        E("binary_sensor.x_status", "binary_sensor", "connectivity", "Status"),
        E("sensor.x_rssi", "sensor", "signal_strength", "RSSI"),
        E("sensor.x_runtime", "sensor", "duration", "Running for"),
    ]
    assert bridge.pick_bridge_entities(ents) == {"wifi": "sensor.x_rssi", "uptime": "sensor.x_runtime"}


def test_nothing_found():
    assert bridge.pick_bridge_entities([]) == {}
