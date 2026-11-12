"""Find the ESPHome serial bridge (if any) that this entry connects through.

The serial-bridge transport talks TCP to an ESP32 running ESPHome. When that ESP32
is also adopted in Home Assistant, its diagnostic entities (Wi-Fi signal, uptime,
the stream server's "connected" sensor) can be shown on the keypad card. Nothing
here is required: with no matching ESPHome device the card simply hides the section.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class EntityInfo:
    entity_id: str
    domain: str
    device_class: str | None
    name: str


def pick_bridge_entities(entities: list[EntityInfo]) -> dict[str, str]:
    """Choose the wifi / uptime / link entities among one ESPHome device's entities."""
    out: dict[str, str] = {}

    def lname(e: EntityInfo) -> str:
        return (e.name or "").lower()

    sensors = [e for e in entities if e.domain == "sensor"]
    binaries = [e for e in entities if e.domain == "binary_sensor"]

    wifi = [e for e in sensors if e.device_class == "signal_strength"]
    wifi.sort(key=lambda e: 0 if "wifi" in lname(e).replace("-", "") else 1)
    if wifi:
        out["wifi"] = wifi[0].entity_id

    up = [e for e in sensors if "uptime" in lname(e)]
    if not up:
        up = [e for e in sensors if e.device_class == "duration"]
    if up:
        out["uptime"] = up[0].entity_id

    # The stream server's sensor, not ESPHome's own "Status" (both are connectivity).
    link = [e for e in binaries if "status" not in lname(e)
            and ("connected" in lname(e) or "link" in lname(e) or "client" in lname(e))]
    if link:
        out["link"] = link[0].entity_id

    clients = [e for e in sensors if "client" in lname(e) or "connection" in lname(e)]
    if clients:
        out["clients"] = clients[0].entity_id
    return out


def find_bridge(hass, host: str | None) -> dict | None:
    """Return {'device_id', 'name', 'wifi', 'uptime', 'link', 'clients'} or None."""
    if not host:
        return None
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    host = host.strip().lower()
    entry = next((e for e in hass.config_entries.async_entries("esphome")
                  if str(e.data.get("host", "")).strip().lower() == host), None)
    if entry is None:
        return None
    devs = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    if not devs:
        return None
    dev = devs[0]
    ents = [
        EntityInfo(e.entity_id, e.domain, e.device_class or e.original_device_class,
                   e.original_name or e.name or "")
        for e in er.async_entries_for_device(er.async_get(hass), dev.id)
        if not e.disabled_by
    ]
    found = pick_bridge_entities(ents)
    if not found:
        return None
    return {"device_id": dev.id, "name": dev.name_by_user or dev.name or entry.title, **found}
