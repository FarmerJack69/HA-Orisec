"""Poll interval limits per link: USB / serial bridge may go fast, the CM2 never below 1 s."""

import importlib.util
import pathlib

_path = pathlib.Path(__file__).parents[1] / "custom_components" / "orisec" / "const.py"
_spec = importlib.util.spec_from_file_location("orisec_const", _path)
const = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(const)


def test_limits_per_link():
    assert const.poll_limits("serial") == (0.2, 1)
    assert const.poll_limits("tcp") == (0.2, 1)
    assert const.poll_limits("udp") == (1.0, 2)
    assert const.poll_limits(None) == (1.0, 2)  # entries from before the link choice are CM2


def test_defaults_when_unset():
    assert const.poll_seconds("tcp", None) == 1
    assert const.poll_seconds("udp", None) == 2


def test_saved_value_kept_within_limits():
    assert const.poll_seconds("tcp", 0.2) == 0.2
    assert const.poll_seconds("serial", 5) == 5


def test_fast_value_raised_on_cm2():
    # e.g. a bridge entry at 0.2 s switched to the CM2 with Reconfigure
    assert const.poll_seconds("udp", 0.2) == 1.0
