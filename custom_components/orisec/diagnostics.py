"""Diagnostics download (Settings → Devices → Orisec → Download diagnostics)."""

from __future__ import annotations

from dataclasses import asdict

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from . import protocol as p

# Testers post these in public issues: keep codes, addresses and the serial out.
REDACT = {CONF_PASSWORD, CONF_HOST, "serial"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry) -> dict:
    c = entry.runtime_data
    data = c.data
    out = {
        "entry": async_redact_data(dict(entry.data), REDACT),
        "panel": async_redact_data(asdict(c.config.login), REDACT),
        "areas": ["**REDACTED**" if n else n for n in c.config.area_names],  # often the address
        "part_sets": c.config.part_names,
        "zones": [asdict(z) | {"type_name": z.type_name, "is_sensor": z.is_sensor} for z in c.config.zones],
        "last_update_success": c.last_update_success,
    }
    if data is not None:
        out["system_state"] = data.snapshot.sys_state
        out["zone_status"] = [f"0x{w:04x}" for w in data.snapshot.zone_status]
        out["zone_flag"] = data.zone_flag
        out["display"] = [ln.text for ln in p.decode_lcd(data.snapshot.lcd, data.time)]
        out["areas_state"] = [asdict(data.snapshot.area(a)) for a in c.config.user_areas]
    return out
