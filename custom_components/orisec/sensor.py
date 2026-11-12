"""Keypad display sensor."""

from __future__ import annotations

import logging
from dataclasses import asdict

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, MAJOR_VERSION, MINOR_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import protocol as p
from .bridge import find_bridge
from .client import TRANSPORT_TCP
from .const import CONF_TRANSPORT, DOMAIN, MANUFACTURER
from .coordinator import OrisecCoordinator
from .entity import OrisecEntity, panel_device_info

_LOGGER = logging.getLogger(__name__)
# HA 2026.8 replaced DeviceInfo's via_device (identifiers) with via_device_id;
# the old key stops working in 2027.8. hacs.json still allows older HA.
_VIA_DEVICE_ID = (MAJOR_VERSION, MINOR_VERSION) >= (2026, 8)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    coordinator: OrisecCoordinator = entry.runtime_data
    entities: list[SensorEntity] = [OrisecDisplay(coordinator), OrisecClockOffset(coordinator)]
    names = {z.number: z.name for z in coordinator.config.zones}
    for zone in coordinator.config.wireless_zones:
        w = coordinator.wireless.get(zone)
        for kind in _WIRELESS_KINDS:
            if kind == "signal2" and (w is None or w.signal2 is None):
                continue
            entities.append(OrisecWirelessSensor(coordinator, zone, names.get(zone, f"Zone {zone}"), kind))
    if coordinator.config.device_slots:
        # Network devices hang off the panel's device, so register that first
        # and link them to it by its device id.
        panel = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id, **panel_device_info(coordinator))
        for slot in coordinator.config.device_slots:
            d = coordinator.devices[slot]
            for kind in (*p.DEVICE_READINGS.get(d.kind, ("voltage",)), "poll_errors", "crc_errors"):
                entities.append(OrisecDeviceSensor(coordinator, d, kind, panel.id))
    if coordinator.panel_power is not None:
        entities += [OrisecPanelPowerSensor(coordinator, kind) for kind in _PANEL_POWER_KINDS]
    add(entities)


class OrisecDisplay(OrisecEntity, SensorEntity):
    """State is the display text; attributes carry the layout for the keypad card."""

    _attr_name = "Keypad display"
    _attr_icon = "mdi:dialpad"
    # The layout changes with the clock every poll; keep it out of the database.
    _unrecorded_attributes = frozenset({"layout", "lines", "devices"})

    def __init__(self, coordinator: OrisecCoordinator) -> None:
        super().__init__(coordinator, "display")
        self._reported: set[str] = set()
        self._bridge: dict | None = None
        self._bridge_at = -1e9

    def _lines(self) -> list[p.LcdLine]:
        d = self.coordinator.data
        lines = p.decode_lcd(d.snapshot.lcd, d.time)
        for seg in lines[0].segments:
            if seg.icon.startswith("?") and seg.icon not in self._reported:
                self._reported.add(seg.icon)
                _LOGGER.warning(
                    "Keypad display shows a status icon this integration doesn't know yet "
                    "(byte 0x%s). Please report what the wired keypad shows next to it.",
                    seg.icon[1:],
                )
        return lines

    @property
    def native_value(self) -> str:
        # Without the clock, so the state only changes when the screen does.
        lines = p.decode_lcd(self.coordinator.data.snapshot.lcd)
        text = " | ".join(ln.text for ln in lines if ln.text)
        return text[:255]

    def _bridge_info(self) -> dict | None:
        """The ESPHome bridge's entities, looked up at most once a minute."""
        data = self.coordinator.config_entry.data
        if data.get(CONF_TRANSPORT) != TRANSPORT_TCP:
            return None
        now = self.hass.loop.time()
        if now - self._bridge_at >= 60:
            self._bridge_at = now
            try:
                self._bridge = find_bridge(self.hass, data.get(CONF_HOST))
            except Exception:  # registry shapes differ between HA versions; never break the display
                _LOGGER.debug("Bridge lookup failed", exc_info=True)
                self._bridge = None
        return self._bridge

    @property
    def extra_state_attributes(self) -> dict:
        lines = self._lines()
        attrs = {
            "lines": [ln.text for ln in lines],
            "layout": [
                {"segments": [asdict(s) for s in ln.segments], "progress": ln.progress}
                for ln in lines
            ],
            "panel_entry_id": self.coordinator.config_entry.entry_id,
        }
        bridge = self._bridge_info()
        if bridge:
            attrs["bridge"] = bridge
        devices = self._devices()
        if devices:
            attrs["devices"] = devices
        return attrs

    def _devices(self) -> list[dict]:
        """Network devices for the card's System list."""
        out = []
        pw = self.coordinator.panel_power
        if pw is not None:
            login = self.coordinator.config.login
            out.append({
                "name": "Panel", "kind": "panel", "address": 0,
                "model": p.PANEL_MODELS.get(login.panel_id or 0, f"CP{login.panel_id}" if login.panel_id else "Panel"),
                "software": login.version,
                "light": None, "voltage": pw.supply, "battery": pw.battery,
                "current": pw.current, "aux_current": pw.aux_current,
            })
        for slot in self.coordinator.config.device_slots:
            d = self.coordinator.devices.get(slot)
            if d is None:
                continue
            readings = p.DEVICE_READINGS.get(d.kind, ())
            out.append({
                "name": d.name, "kind": d.kind, "address": d.address, "model": d.model,
                "software": d.software,
                "light": d.light if "light" in readings else None,
                "voltage": d.voltage,
                "current": d.current if "current" in readings else None,
                "battery": d.battery if "battery" in readings else None,
                "poll_errors": d.poll_errors, "crc_errors": d.crc_errors,
            })
        return out


class OrisecClockOffset(OrisecEntity, SensorEntity):
    """How far the panel clock is from HA's (positive = panel ahead)."""

    _attr_name = "Panel clock offset"
    _attr_icon = "mdi:clock-alert-outline"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _unrecorded_attributes = frozenset({"panel_time"})

    def __init__(self, coordinator: OrisecCoordinator) -> None:
        super().__init__(coordinator, "clock_offset")

    @property
    def native_value(self) -> int | None:
        return self.coordinator.data.clock_offset if self.coordinator.data else None

    @property
    def extra_state_attributes(self) -> dict:
        clock = self.coordinator.data.panel_clock if self.coordinator.data else None
        return {"panel_time": clock.isoformat() if clock else None}


# kind: (name, unit, device class, diagnostic, icon)
_WIRELESS_KINDS = {
    "temperature": ("temperature", UnitOfTemperature.CELSIUS, SensorDeviceClass.TEMPERATURE, False, None),
    "battery": ("battery", UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE, True, "mdi:battery"),
    "signal": ("signal", PERCENTAGE, None, True, "mdi:signal"),
    "signal2": ("signal 2", PERCENTAGE, None, True, "mdi:signal"),
    "last_polled": ("last polled", UnitOfTime.MINUTES, SensorDeviceClass.DURATION, True, "mdi:timer-sand"),
}


class OrisecWirelessSensor(OrisecEntity, SensorEntity):
    """A wireless zone's battery, temperature, signal or last poll."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: OrisecCoordinator, zone: int, zone_name: str, kind: str) -> None:
        super().__init__(coordinator, f"zone_{zone}_{kind}")
        self._zone = zone
        self._kind = kind
        label, unit, dclass, diagnostic, icon = _WIRELESS_KINDS[kind]
        self._attr_name = f"{zone_name} {label}"
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = dclass
        if diagnostic:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        if icon:
            self._attr_icon = icon
        if kind == "battery":
            self._attr_suggested_display_precision = 1

    @property
    def native_value(self):
        w = self.coordinator.wireless.get(self._zone)
        return None if w is None else getattr(w, self._kind)

    @property
    def extra_state_attributes(self) -> dict:
        w = self.coordinator.wireless.get(self._zone)
        if w is None or self._kind != "signal":
            return {"zone": self._zone}
        return {"zone": self._zone, "device": w.device, "serial": w.serial}


# kind: (name, unit, device class, diagnostic, icon)
_DEVICE_KINDS = {
    "light": ("light level", PERCENTAGE, None, False, "mdi:brightness-6"),
    "voltage": ("voltage", UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE, True, None),
    "current": ("current", UnitOfElectricCurrent.MILLIAMPERE, SensorDeviceClass.CURRENT, True, None),
    "battery": ("battery voltage", UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE, True, "mdi:battery"),
    "poll_errors": ("poll errors", None, None, True, "mdi:lan-disconnect"),
    "crc_errors": ("CRC errors", None, None, True, "mdi:alert-circle-check-outline"),
}


class OrisecDeviceSensor(OrisecEntity, SensorEntity):
    """A keypad's, expander's or sounder's light level, voltage or current.
    Each network device is its own HA device under the panel."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: OrisecCoordinator, device: p.DeviceInfo, kind: str, panel_device_id: str) -> None:
        super().__init__(coordinator, f"device_{device.slot}_{kind}")
        self._slot = device.slot
        self._kind = kind
        label, unit, dclass, diagnostic, icon = _DEVICE_KINDS[kind]
        self._attr_name = label[0].upper() + label[1:]
        if kind in ("poll_errors", "crc_errors"):
            # Counts the panel keeps (they restart from 0 when it restarts).
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = dclass
        if diagnostic:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        if icon:
            self._attr_icon = icon
        if kind in ("voltage", "battery"):
            self._attr_suggested_display_precision = 2
        elif kind == "current":
            self._attr_suggested_display_precision = 0
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{self._serial}_device_{device.slot}")},
            manufacturer=MANUFACTURER,
            model=device.model,
            name=f"Orisec {device.name}",
            sw_version=device.software,
        )
        if _VIA_DEVICE_ID:
            self._attr_device_info["via_device_id"] = panel_device_id
        else:  # Home Assistant before 2026.8
            self._attr_device_info["via_device"] = (DOMAIN, self._serial)

    @property
    def native_value(self):
        d = self.coordinator.devices.get(self._slot)
        return None if d is None else getattr(d, self._kind)

    @property
    def extra_state_attributes(self) -> dict:
        d = self.coordinator.devices.get(self._slot)
        attrs = {"address": d.address if d else None}
        if d is not None and self._kind == "light":
            attrs["raw"] = d.light_raw
        if d is not None and self._kind == "voltage":
            attrs["temperature_raw"] = d.temperature_raw  # conversion not known yet
        if d is not None and self._kind == "crc_errors":
            attrs["counts"] = list(d.crc)  # the three CRC error counts
            attrs["rx_packets"] = d.rx_packets
        return attrs


# kind: (name, attribute of PanelPower, unit, device class, icon)
_PANEL_POWER_KINDS = {
    "supply": ("Supply voltage", UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE, "mdi:flash"),
    "battery": ("Battery voltage", UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE, "mdi:car-battery"),
    "current": ("Current", UnitOfElectricCurrent.MILLIAMPERE, SensorDeviceClass.CURRENT, None),
    "aux_current": ("Aux current", UnitOfElectricCurrent.MILLIAMPERE, SensorDeviceClass.CURRENT, None),
}


class OrisecPanelPowerSensor(OrisecEntity, SensorEntity):
    """The panel's supply and battery voltage and currents ."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: OrisecCoordinator, kind: str) -> None:
        super().__init__(coordinator, f"panel_{kind}")
        self._kind = kind
        name, unit, dclass, icon = _PANEL_POWER_KINDS[kind]
        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = dclass
        if icon:
            self._attr_icon = icon
        self._attr_suggested_display_precision = 2 if unit == UnitOfElectricPotential.VOLT else 0

    @property
    def native_value(self):
        pw = self.coordinator.panel_power
        return None if pw is None else getattr(pw, self._kind)
