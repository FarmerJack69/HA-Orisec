"""Per-area Reset buttons (clears an alarm/tamper condition, like the app's Reset)."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import protocol as p
from .client import OrisecError
from .coordinator import OrisecCoordinator
from .entity import OrisecEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    coordinator: OrisecCoordinator = entry.runtime_data
    add([*(OrisecResetButton(coordinator, area) for area in coordinator.config.user_areas),
         OrisecSyncClockButton(coordinator)])


class OrisecResetButton(OrisecEntity, ButtonEntity):
    _attr_icon = "mdi:restore"

    def __init__(self, coordinator: OrisecCoordinator, area: int) -> None:
        super().__init__(coordinator, f"area_{area}_reset")
        self._area = area
        cfg = coordinator.config
        name = cfg.area_names[area - 1] if area - 1 < len(cfg.area_names) else ""
        self._attr_name = "Reset" if len(cfg.user_areas) == 1 else f"{name or f'Area {area}'} reset"

    @property
    def extra_state_attributes(self) -> dict:
        return {"area": self._area}

    async def async_press(self) -> None:
        try:
            await self.coordinator.async_arm(p.RESET, self._area)
        except OrisecError as err:
            raise HomeAssistantError(str(err)) from err


class OrisecSyncClockButton(OrisecEntity, ButtonEntity):
    """Set the panel's time and date to HA's."""

    _attr_name = "Sync panel clock"
    _attr_icon = "mdi:clock-check-outline"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: OrisecCoordinator) -> None:
        super().__init__(coordinator, "sync_clock")

    async def async_press(self) -> None:
        try:
            await self.coordinator.async_sync_clock()
        except OrisecError as err:
            raise HomeAssistantError(str(err)) from err
