"""Base entity for Orisec."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import protocol as p
from .const import DOMAIN, MANUFACTURER
from .coordinator import OrisecCoordinator


class OrisecEntity(CoordinatorEntity[OrisecCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: OrisecCoordinator, key: str) -> None:
        super().__init__(coordinator)
        info = coordinator.config.login
        serial = info.serial or coordinator.client.label
        self._serial = serial
        self._attr_unique_id = f"{serial}_{key}"
        model = p.PANEL_MODELS.get(info.panel_id or 0, f"CP{info.panel_id}" if info.panel_id else "Alarm panel")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            manufacturer=MANUFACTURER,
            model=model,
            name=f"Orisec {model}",
            sw_version=info.version,
            serial_number=info.serial,
        )
