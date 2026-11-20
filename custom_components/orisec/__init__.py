"""Orisec alarm panel integration (local UDP)."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import voluptuous as vol

from homeassistant.components import frontend
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, CONF_HOST, CONF_PASSWORD, CONF_PORT, Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady, HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util

from . import protocol as p
from .client import TRANSPORT_UDP, OrisecAuthError, OrisecClient, OrisecError, OrisecPinError
from .const import (
    ATTR_KEY, ATTR_KEYS, ATTR_OMIT, CARD_URL, CONF_BAUD, CONF_DEVICE, CONF_TRANSPORT, DEFAULT_BAUD, DOMAIN,
    SERVICE_OMIT_ZONE, SERVICE_SEND_KEY,
)
from .coordinator import OrisecCoordinator
from .notifications import Notifier, phones

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.ALARM_CONTROL_PANEL, Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type OrisecConfigEntry = ConfigEntry[OrisecCoordinator]

_KEY = vol.In(sorted(p.KEY_NAMES) + sorted(p.VALID_KEY_CODES))
SEND_KEY_SCHEMA = vol.Schema(
    {
        vol.Optional("config_entry_id"): cv.string,
        vol.Exclusive(ATTR_KEY, "key"): _KEY,
        vol.Exclusive(ATTR_KEYS, "key"): vol.All(cv.ensure_list, [_KEY]),
    }
)

_CODE = vol.All(cv.string, vol.Match(r"^\d{4,8}$", msg="A PIN is 4 to 8 digits"))

OMIT_ZONE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
        vol.Optional(ATTR_OMIT): cv.boolean,
        vol.Optional("code"): _CODE,
    }
)

RESET_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
        vol.Optional("code"): _CODE,
    }
)

# Diagnostic: raw reads only (data_len 0), never commands. For working out
# undocumented queries such as the event log.
READ_RAW_SCHEMA = vol.Schema(
    {
        vol.Optional("config_entry_id"): cv.string,
        vol.Required("reads"): vol.All(
            cv.ensure_list,
            [vol.All(cv.ensure_list, vol.Length(min=3, max=3), [vol.All(vol.Coerce(int), vol.Range(min=1, max=65535))])],
            vol.Length(min=1, max=10),
        ),
    }
)

GET_EVENTS_SCHEMA = vol.Schema(
    {
        vol.Optional("config_entry_id"): cv.string,
        vol.Optional("start", default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=1280)),
        vol.Optional("count", default=10): vol.All(vol.Coerce(int), vol.Range(min=1, max=50)),
    }
)


SET_CLOCK_SCHEMA = vol.Schema(
    {
        vol.Optional("config_entry_id"): cv.string,
        vol.Optional("datetime"): cv.datetime,
    }
)

TEST_USER_LOGIN_SCHEMA = vol.Schema(
    {
        vol.Optional("config_entry_id"): cv.string,
        vol.Required("code"): vol.All(cv.string, vol.Match(r"^\d{4,8}$")),
    }
)


_PREFS = {
    vol.Optional("enabled"): cv.boolean,
    vol.Optional("categories"): vol.All(cv.ensure_list, [vol.In(sorted(p.NOTIFY_CATEGORIES))]),
    vol.Optional("areas"): vol.All(cv.ensure_list, [vol.All(vol.Coerce(int), vol.Range(min=1, max=8))]),
    vol.Optional("zones"): vol.All(cv.ensure_list, [vol.All(vol.Coerce(int), vol.Range(min=1, max=250))]),
    vol.Optional("devices"): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional("critical"): cv.boolean,
    vol.Optional("url"): vol.Any(None, vol.All(cv.string, vol.Length(max=200))),
}
GET_NOTIFY_SCHEMA = vol.Schema({vol.Optional("config_entry_id"): cv.string, vol.Optional("user_id"): cv.string})
SET_NOTIFY_SCHEMA = vol.Schema({vol.Optional("config_entry_id"): cv.string, vol.Optional("user_id"): cv.string, **_PREFS})
_PREFS_KEYS = {str(k) for k in _PREFS}


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    await hass.http.async_register_static_paths([
        StaticPathConfig(CARD_URL, str(Path(__file__).parent / "frontend" / "orisec-keypad-card.js"), False)
    ])
    # The version in the URL makes browsers fetch the new card after an update
    # (the plain URL can stay cached). No dashboard resource needed.
    version = (await async_get_integration(hass, DOMAIN)).version
    frontend.add_extra_js_url(hass, f"{CARD_URL}?v={version}")

    def panel(call: ServiceCall) -> OrisecCoordinator:
        """The panel a service call is for: the only one loaded, or `config_entry_id`."""
        entries = list(hass.config_entries.async_loaded_entries(DOMAIN))
        if eid := call.data.get("config_entry_id"):
            entries = [e for e in entries if e.entry_id == eid]
        if len(entries) != 1:
            raise ServiceValidationError("Specify config_entry_id (none or several panels loaded)")
        return entries[0].runtime_data

    async def send_key(call: ServiceCall) -> None:
        keys = call.data.get(ATTR_KEYS) or ([call.data[ATTR_KEY]] if ATTR_KEY in call.data else [])
        if not keys:
            raise ServiceValidationError("Give 'key' or 'keys'")
        coord = panel(call)
        try:
            await coord.async_press_keys(keys)
        except OrisecError as err:
            raise HomeAssistantError(str(err)) from err

    hass.services.async_register(DOMAIN, SERVICE_SEND_KEY, send_key, schema=SEND_KEY_SCHEMA)

    async def omit_zone(call: ServiceCall) -> None:
        """Omit (omit: true), reinstate (omit: false) or toggle (no omit) zones."""
        reg = er.async_get(hass)
        for eid in call.data[ATTR_ENTITY_ID]:
            ent = reg.async_get(eid)
            entry = hass.config_entries.async_get_entry(ent.config_entry_id) if ent and ent.config_entry_id else None
            state = hass.states.get(eid)
            zone = state.attributes.get("zone") if state else None
            if entry is None or entry.domain != DOMAIN or not zone or not eid.startswith("binary_sensor."):
                raise ServiceValidationError(f"{eid} is not an Orisec zone")
            coordinator: OrisecCoordinator = entry.runtime_data
            if not coordinator.can_omit(zone) and not coordinator.is_omitted(zone):
                raise ServiceValidationError(f"Zone {zone} is not set up to be omitted by the user")
            try:
                await coordinator.async_omit_zone(zone, call.data.get(ATTR_OMIT), call.data.get("code"))
            except OrisecPinError as err:
                raise ServiceValidationError(str(err)) from err
            except OrisecError as err:
                raise HomeAssistantError(str(err)) from err

    hass.services.async_register(DOMAIN, SERVICE_OMIT_ZONE, omit_zone, schema=OMIT_ZONE_SCHEMA)

    async def reset(call: ServiceCall) -> None:
        """Reset areas (as the Reset buttons), optionally as the panel user with `code`.

        Takes the area's Reset button or its alarm panel entity."""
        reg = er.async_get(hass)
        for eid in call.data[ATTR_ENTITY_ID]:
            ent = reg.async_get(eid)
            entry = hass.config_entries.async_get_entry(ent.config_entry_id) if ent and ent.config_entry_id else None
            state = hass.states.get(eid)
            area = state.attributes.get("area") if state else None
            if entry is None or entry.domain != DOMAIN or not area:
                raise ServiceValidationError(f"{eid} is not an Orisec area or Reset button")
            try:
                await entry.runtime_data.async_arm(p.RESET, area, call.data.get("code"))
            except OrisecPinError as err:
                raise ServiceValidationError(str(err)) from err
            except OrisecError as err:
                raise HomeAssistantError(str(err)) from err

    hass.services.async_register(DOMAIN, "reset", reset, schema=RESET_SCHEMA)

    async def read_raw(call: ServiceCall) -> ServiceResponse:
        coord = panel(call)
        reads = [tuple(r) for r in call.data["reads"]]
        blocks = await coord.client.read(reads)
        if blocks is None:
            raise HomeAssistantError("No reply from panel")
        return {"blocks": [
            {"cmd": b.cmd, "start": b.start, "count": b.count, "data": b.data.hex()} for b in blocks
        ]}

    async def get_events(call: ServiceCall) -> ServiceResponse:
        coord = panel(call)
        try:
            events = await coord.async_read_events(call.data["start"], call.data["count"])
        except OrisecError as err:
            raise HomeAssistantError(str(err)) from err
        return {"events": [
            {"index": e.index, "code": e.code, "text": e.text, "area": e.area,
             "day": e.day, "date": e.date, "time": e.time} for e in events
        ]}

    hass.services.async_register(
        DOMAIN, "get_events", get_events, schema=GET_EVENTS_SCHEMA, supports_response=SupportsResponse.ONLY
    )

    async def set_clock(call: ServiceCall) -> None:
        """Set the panel clock to HA's local time, or to the given date and time."""
        coord = panel(call)
        when = call.data.get("datetime")
        if when is not None:
            if when.tzinfo is not None:
                when = dt_util.as_local(when)
            when = when.replace(tzinfo=None, microsecond=0)
        try:
            await coord.async_sync_clock(when)
        except (OrisecError, ValueError) as err:
            raise HomeAssistantError(str(err)) from err

    hass.services.async_register(DOMAIN, "set_clock", set_clock, schema=SET_CLOCK_SCHEMA)

    async def test_user_login(call: ServiceCall) -> ServiceResponse:
        """Diagnostic: log in as another panel user, then back. Never arms anything."""
        coord = panel(call)
        try:
            other, back = await coord.client.try_user_login(call.data["code"])
        except OrisecError as err:
            raise HomeAssistantError(str(err)) from err
        return {
            "accepted": other.ok,
            "error": other.error,
            "user_number": other.user_number if other.ok else None,
            "user_area_mask": other.user_area if other.ok else None,
            "restored": back.ok,
            "restored_user_number": back.user_number if back.ok else None,
            "restored_user_area_mask": back.user_area if back.ok else None,
        }

    hass.services.async_register(
        DOMAIN, "test_user_login", test_user_login, schema=TEST_USER_LOGIN_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )

    hass.services.async_register(
        DOMAIN, "read_raw", read_raw, schema=READ_RAW_SCHEMA, supports_response=SupportsResponse.ONLY
    )

    async def _notify_target(call: ServiceCall) -> tuple[OrisecCoordinator, str, bool]:
        """The panel and the HA user whose notification settings to use.

        Without user_id it's the user making the call. Only admins may look at
        or change someone else's."""
        coord = panel(call)
        caller = call.context.user_id
        target = call.data.get("user_id") or caller
        if not target:
            raise ServiceValidationError("No HA user: give user_id")
        cu = await hass.auth.async_get_user(caller) if caller else None
        admin = caller is None or bool(cu and cu.is_admin)  # None = called by HA itself (automation)
        if target != caller and not admin:
            raise ServiceValidationError("Only an admin can change another user's alarm notifications")
        if await hass.auth.async_get_user(target) is None:
            raise ServiceValidationError(f"No HA user {target}")
        return coord, target, admin

    async def _notify_view(coord: OrisecCoordinator, user_id: str, prefs: dict, admin: bool) -> dict:
        user = await hass.auth.async_get_user(user_id)
        zones = [{"zone": z.number, "name": z.name, "areas": z.area_list}
                 for z in coord.config.zones if z.is_sensor]
        others = []
        if admin:  # admins can pick any person's HA user to set up
            for u in await hass.auth.async_get_users():
                if u.is_active and not u.system_generated:
                    others.append({"user_id": u.id, "name": u.name or u.id,
                                   "set_up": u.id in coord.notifier.users()})
        return {
            "user_id": user_id,
            "user_name": user.name if user else "",
            "settings": prefs,
            "phones": [{k: v for k, v in ph.items() if k != "service"} for ph in phones(hass, user_id)],
            "categories": p.NOTIFY_CATEGORIES,
            "areas": [{"area": a, "name": coord.area_names().get(a, f"Area {a}")} for a in coord.config.user_areas],
            "zones": zones,
            "users": others,
        }

    async def get_notify_settings(call: ServiceCall) -> ServiceResponse:
        coord, uid, admin = await _notify_target(call)
        return await _notify_view(coord, uid, coord.notifier.get(uid), admin)

    async def set_notify_settings(call: ServiceCall) -> ServiceResponse:
        coord, uid, admin = await _notify_target(call)
        prefs = {k: v for k, v in call.data.items() if k in _PREFS_KEYS}
        saved = await coord.notifier.async_set(uid, prefs)
        return await _notify_view(coord, uid, saved, admin)

    hass.services.async_register(
        DOMAIN, "get_notify_settings", get_notify_settings, schema=GET_NOTIFY_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, "set_notify_settings", set_notify_settings, schema=SET_NOTIFY_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


def client_from_data(data) -> OrisecClient:
    return OrisecClient(
        data[CONF_PASSWORD],
        transport=data.get(CONF_TRANSPORT, TRANSPORT_UDP),
        host=data.get(CONF_HOST),
        port=data.get(CONF_PORT),
        device=data.get(CONF_DEVICE),
        baud=data.get(CONF_BAUD, DEFAULT_BAUD),
    )


async def async_setup_entry(hass: HomeAssistant, entry: OrisecConfigEntry) -> bool:
    if CONF_HOST not in entry.data and "panel_ip" in entry.data:
        # Entry created by the older community integration: same panel, different key names.
        data = {k: v for k, v in entry.data.items() if k not in ("panel_ip", "panel_port")}
        data[CONF_HOST] = entry.data["panel_ip"]
        data[CONF_PORT] = entry.data.get("panel_port", p.DEFAULT_PORT)
        hass.config_entries.async_update_entry(entry, data=data)
    client = client_from_data(entry.data)
    coordinator = OrisecCoordinator(hass, entry, client)
    try:
        await coordinator.async_load_config()
    except OrisecAuthError as err:
        await client.close()
        raise ConfigEntryAuthFailed(str(err)) from err
    except OrisecError as err:
        await client.close()
        raise ConfigEntryNotReady(str(err)) from err
    await coordinator.async_config_entry_first_refresh()
    coordinator.start_polling()
    _remove_stale_areas(hass, entry, coordinator)
    names = coordinator.area_names()
    notifier = Notifier(hass, entry.entry_id, names.get(1) or entry.title)
    await notifier.async_load()
    coordinator.notifier = notifier
    coordinator.event_listeners.append(notifier.handle)
    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _remove_stale_areas(hass: HomeAssistant, entry: OrisecConfigEntry, coordinator: OrisecCoordinator) -> None:
    """Drop entities for areas the user code can no longer use (or that have no
    zones), and for zones no longer in use, instead of leaving them unavailable. They come back by themselves if
    the area is used again."""
    keep = set(coordinator.config.user_areas)
    reg = er.async_get(hass)
    for ent in er.async_entries_for_config_entry(reg, entry.entry_id):
        if ent.domain not in ("alarm_control_panel", "button") or not ent.unique_id:
            continue
        m = re.search(r"_area_(\d+)(_reset)?$", ent.unique_id)
        if m and int(m.group(1)) not in keep:
            _LOGGER.info("Removing %s: area %s is no longer in use", ent.entity_id, m.group(1))
            reg.async_remove(ent.entity_id)
    # Zones removed or set to Not Used on the panel (and keyswitch zones, which get no sensor).
    zones = {z.number for z in coordinator.config.zones if z.is_sensor}
    for ent in er.async_entries_for_config_entry(reg, entry.entry_id):
        if ent.domain != "binary_sensor" or not ent.unique_id:
            continue
        m = re.search(r"_zone_(\d+)$", ent.unique_id)
        if m and int(m.group(1)) not in zones:
            _LOGGER.info("Removing %s: zone %s is no longer in use", ent.entity_id, m.group(1))
            reg.async_remove(ent.entity_id)


async def _options_updated(hass: HomeAssistant, entry: OrisecConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: OrisecConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.client.close()
    return ok
