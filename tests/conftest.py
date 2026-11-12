"""Load the protocol module directly, without importing Home Assistant."""

import importlib.util
import pathlib
import sys

_path = pathlib.Path(__file__).parents[1] / "custom_components" / "orisec" / "protocol.py"
_spec = importlib.util.spec_from_file_location("orisec_protocol", _path)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["orisec_protocol"] = _mod
_spec.loader.exec_module(_mod)
