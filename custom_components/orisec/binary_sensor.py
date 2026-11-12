"""Zone and system binary sensors."""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from . import protocol as p
from .coordinator import OrisecCoordinator, Zone
from .entity import OrisecEntity



def _icons(zone: Zone) -> tuple[str, str]:
    """(healthy icon, active icon) hinting at what the zone is. States use Orisec wording."""
    t = zone.zone_type
    name = zone.name.lower()
    if t == 6:
        return "mdi:smoke-detector-variant", "mdi:smoke-detector-variant-alert"
    if t == 29:
        return "mdi:water-off", "mdi:water-alert"
    if t == 30:
        return "mdi:molecule-co", "mdi:molecule-co"
    if t in (7, 8, 9, 10, 11, 12) or "panic" in name:
        return "mdi:alarm-light-outline", "mdi:alarm-light"
    if t == 33 or "doorbell" in name or "door bell" in name:
        return "mdi:doorbell", "mdi:bell-ring"
    if "window" in name:
        return "mdi:window-closed-variant", "mdi:window-open-variant"
    if "gate" in name:
        return "mdi:gate", "mdi:gate-open"
    if "door" in name or "dc" in name.split() or t in (1, 2, 17):
        return "mdi:door-closed", "mdi:door-open"
    if t in (13, 14, 15, 24, 25, 26, 27):
        return "mdi:shield-check-outline", "mdi:alert-circle"
    return "mdi:motion-sensor-off", "mdi:motion-sensor"


# (key, system state position, device class, name)
_SYSTEM = (
    ("ac_fault", p.SYS_AC_FAULT, BinarySensorDeviceClass.PROBLEM, "Mains fault"),
    ("battery_fault", p.SYS_BATTERY_FAULT, BinarySensorDeviceClass.PROBLEM, "Battery fault"),
    ("tamper", p.SYS_TAMPER, BinarySensorDeviceClass.TAMPER, "Tamper"),
    ("trouble", p.SYS_TROUBLE, BinarySensorDeviceClass.PROBLEM, "Trouble"),
    ("call_engineer", p.SYS_CALL_ENGINEER, BinarySensorDeviceClass.PROBLEM, "Engineer required"),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    coordinator: OrisecCoordinator = entry.runtime_data
    entities: list[BinarySensorEntity] = [
        OrisecZoneSensor(coordinator, z) for z in coordinator.config.zones if z.is_sensor
    ]
    entities += [OrisecSystemSensor(coordinator, *s) for s in _SYSTEM]
    add(entities)


class OrisecZoneSensor(OrisecEntity, BinarySensorEntity, RestoreEntity):
    """A zone. `since` is when it last really went active or clear; it survives restarts
    (HA's own last_changed resets to the restart time, which made every zone look recent)."""

    def __init__(self, coordinator: OrisecCoordinator, zone: Zone) -> None:
        super().__init__(coordinator, f"zone_{zone.number}")
        self._zone = zone
        self._since: str | None = None
        self._was_on: bool | None = None
        self._attr_name = zone.name
        # No device class: HA would word states from a guessed type (Closed, Clear...).
        # The translation key gives the panel's own terms: Healthy / Active.
        self._attr_translation_key = "zone"
        self._icon_off, self._icon_on = _icons(zone)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        on = self.is_on
        last = await self.async_get_last_state()
        if last is not None and last.state == ("on" if on else "off"):
            # Same state as before the restart: keep when it really changed.
            self._since = last.attributes.get("since") or last.last_changed.isoformat()
        else:
            self._since = dt_util.utcnow().isoformat()
        self._update_since(False)
        self._was_on = on

    def _panel_since(self) -> datetime | None:
        """When the zone last changed, from the panel's own timer (10120), if read."""
        d = self.coordinator.data
        idx = self._zone.number - 1
        if d is None or d.zone_stats_at is None or not 0 <= idx < len(d.snapshot.zone_timers):
            return None
        return (d.zone_stats_at - timedelta(seconds=d.snapshot.zone_timers[idx] / 10)).replace(microsecond=0)

    def _update_since(self, changed: bool) -> None:
        panel = self._panel_since()
        if panel is not None:
            # The panel's timer is the truth (it counts while HA is off too); only move
            # `since` when it really moved, so poll jitter doesn't rewrite the attribute.
            current = dt_util.parse_datetime(self._since) if self._since else None
            if current is None or abs((panel - current).total_seconds()) > 2:
                self._since = panel.isoformat()
        elif changed:
            self._since = dt_util.utcnow().isoformat()

    def _handle_coordinator_update(self) -> None:
        on = self.is_on
        self._update_since(self._was_on is not None and on != self._was_on)
        self._was_on = on
        super()._handle_coordinator_update()

    def _word(self) -> int:
        st = self.coordinator.data.snapshot.zone_status
        return st[self._zone.number - 1] if self._zone.number - 1 < len(st) else 0

    @property
    def is_on(self) -> bool:
        return bool(self._word() & p.ZONE_ACTIVE)

    @property
    def icon(self) -> str:
        return self._icon_on if self.is_on else self._icon_off

    @property
    def extra_state_attributes(self) -> dict:
        flags = self.coordinator.data.zone_flag
        idx = self._zone.number - 1
        flag = flags[idx] if 0 <= idx < len(flags) else None
        attrs = {
            "zone": self._zone.number,
            "zone_type": self._zone.type_name,
            "areas": self._zone.area_list,
            "areas_raw": self._zone.areas,
            "tamper": bool(self._word() & p.ZONE_TAMPER),
            "omitted": bool(self._word() & (p.ZONE_OMITTED | p.ZONE_PART_OMITTED)),
            "manual_omitted": bool(self._word() & p.ZONE_OMITTED),
            "part_omitted": bool(self._word() & p.ZONE_PART_OMITTED),
            "alarmed": bool(self._word() & p.ZONE_ALARMED),
            "raw_status": f"0x{self._word():04x}",
        }
        if self._since:
            attrs["since"] = self._since
        counts = self.coordinator.data.snapshot.zone_counts
        if 0 <= idx < len(counts):
            attrs["activations"] = counts[idx]
        if (w := self.coordinator.wireless.get(self._zone.number)) is not None:
            attrs["wireless_device"] = w.device
            attrs["signal"] = w.signal
            if w.signal2 is not None:
                attrs["signal2"] = w.signal2
        if flag is not None:
            attrs["part_arm_omit"] = p.part_arm_omits(flag)
            attrs["zone_flag"] = f"0x{flag:02x}"
            attrs["can_omit"] = bool(flag & p.ZONE_OMIT_MANUAL)
        return attrs


class OrisecSystemSensor(OrisecEntity, BinarySensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, key, position, device_class, name) -> None:
        super().__init__(coordinator, key)
        self._pos = position
        self._attr_device_class = device_class
        self._attr_name = name

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.data.snapshot.sys_state[self._pos])
