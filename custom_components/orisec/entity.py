"""Base entity for Orisec."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import protocol as p
from .const import DOMAIN, MANUFACTURER
from .coordinator import OrisecCoordinator


def panel_serial(coordinator: OrisecCoordinator) -> str:
    """The panel's identifier: its serial, or the link label if it gave none."""
    return coordinator.config.login.serial or coordinator.client.label


def panel_device_info(coordinator: OrisecCoordinator) -> DeviceInfo:
    """The panel's own device, which every other Orisec device hangs off."""
    info = coordinator.config.login
    model = p.PANEL_MODELS.get(info.panel_id or 0, f"CP{info.panel_id}" if info.panel_id else "Alarm panel")
    return DeviceInfo(
        identifiers={(DOMAIN, panel_serial(coordinator))},
        manufacturer=MANUFACTURER,
        model=model,
        name=f"Orisec {model}",
        sw_version=info.version,
        serial_number=info.serial,
    )


class OrisecEntity(CoordinatorEntity[OrisecCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: OrisecCoordinator, key: str) -> None:
        super().__init__(coordinator)
        serial = panel_serial(coordinator)
        self._serial = serial
        self._attr_unique_id = f"{serial}_{key}"
        self._attr_device_info = panel_device_info(coordinator)
