"""Alarm panel entities (one per area the user code can control)."""

from __future__ import annotations

import time

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import protocol as p
from .client import OrisecError, OrisecPinError
from .coordinator import OrisecCoordinator
from .entity import OrisecEntity

_STATE = {
    "disarmed": AlarmControlPanelState.DISARMED,
    "arming": AlarmControlPanelState.ARMING,
    "pending": AlarmControlPanelState.PENDING,
    "armed_away": AlarmControlPanelState.ARMED_AWAY,
    "armed_home": AlarmControlPanelState.ARMED_HOME,
    "armed_night": AlarmControlPanelState.ARMED_NIGHT,
    "armed_vacation": AlarmControlPanelState.ARMED_VACATION,
    "triggered": AlarmControlPanelState.TRIGGERED,
}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    coordinator: OrisecCoordinator = entry.runtime_data
    add(OrisecAlarm(coordinator, area) for area in coordinator.config.user_areas)


_PART_FEATURE = {
    1: AlarmControlPanelEntityFeature.ARM_HOME,
    2: AlarmControlPanelEntityFeature.ARM_NIGHT,
    3: AlarmControlPanelEntityFeature.ARM_VACATION,
}
_PART_SERVICE = {1: "arm_home", 2: "arm_night", 3: "arm_vacation"}


class OrisecAlarm(OrisecEntity, AlarmControlPanelEntity):
    # No code is required: the connection is already logged in. An optional code
    # (e.g. from the card's PIN pad) makes the panel act as that user instead.
    _attr_code_arm_required = False
    _attr_code_format = None

    def __init__(self, coordinator: OrisecCoordinator, area: int) -> None:
        super().__init__(coordinator, f"area_{area}")
        self._area = area
        cfg = coordinator.config
        name = cfg.area_names[area - 1] if area - 1 < len(cfg.area_names) else ""
        self._attr_name = name or (None if len(cfg.user_areas) == 1 else f"Area {area}")

    def _parts(self) -> list[int]:
        cfg = self.coordinator.config
        flags = self.coordinator.data.zone_flag if self.coordinator.data else None
        return [n for n in (1, 2, 3) if cfg.part_available(n, self._area, flags)]

    @property
    def supported_features(self) -> AlarmControlPanelEntityFeature:
        features = AlarmControlPanelEntityFeature.ARM_AWAY
        for part in self._parts():
            features |= _PART_FEATURE[part]
        return features

    @property
    def alarm_state(self) -> AlarmControlPanelState | None:
        return _STATE[self.coordinator.data.snapshot.area(self._area).state]

    @property
    def extra_state_attributes(self) -> dict:
        a = self.coordinator.data.snapshot.area(self._area)
        cfg = self.coordinator.config
        return {
            "area": self._area,
            "ready": a.ready,
            "alarm_active": a.alarm,
            "part_set": cfg.part_name(a.part) if a.part else None,
            "bell": a.bell,
            "strobe": a.strobe,
            "engineer_required": a.engineer,
            "part_set_names": {_PART_SERVICE[n]: cfg.part_name(n) for n in self._parts()},
            "quick_arm": self._quick_arm(a),
        }

    def _quick_arm(self, a: p.AreaState) -> str | None:
        """During exit, the service that repeats the set type in progress (skips the exit timer).

        The panel arms immediately when the same arm command arrives again during
        exit. Prefer the panel's own part bits; fall back to what HA sent. None when
        unknown (e.g. set from a keypad), so we never change the set type.
        """
        sent = self.coordinator.exit_key.get(self._area)
        if a.state != "arming":
            # Forget once the exit has ended (allow a moment for the panel to start it).
            if sent and time.monotonic() - sent[1] > 5:
                self.coordinator.exit_key.pop(self._area, None)
            return None
        if a.part:
            return f"alarm_{_PART_SERVICE[a.part]}"
        key = sent[0] if sent else None
        if key == p.ARM_FULL:
            return "alarm_arm_away"
        if key in _PART_SERVICE:
            return f"alarm_{_PART_SERVICE[key]}"
        return None

    async def _send(self, key: int, code: str | None) -> None:
        try:
            await self.coordinator.async_arm(key, self._area, code or None)
        except OrisecPinError as err:
            raise ServiceValidationError(str(err)) from err
        except OrisecError as err:
            raise HomeAssistantError(str(err)) from err

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        await self._send(p.DISARM, code)

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        await self._send(p.ARM_FULL, code)

    async def async_alarm_arm_home(self, code: str | None = None) -> None:
        await self._send(p.ARM_PART_1, code)

    async def async_alarm_arm_night(self, code: str | None = None) -> None:
        await self._send(p.ARM_PART_2, code)

    async def async_alarm_arm_vacation(self, code: str | None = None) -> None:
        await self._send(p.ARM_PART_3, code)
