"""Per-user alarm notifications, like the official app's per-phone settings.

Each HA user picks which kinds of panel event they're told about (the app's
categories), for which areas, and which zones notify when they go active.
Notifications go to that user's phones via the companion app's notify
services. Settings are stored per config entry and per HA user.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import slugify

from . import protocol as p
from .const import NOTIFY_STORE

_LOGGER = logging.getLogger(__name__)

# Categories that can be sent as critical alerts (sound through silent / DND).
URGENT = {"priority", "alarms"}
ZONE_REPEAT_SECONDS = 60  # one notification per zone per user in this window
# Icon and background colour per category, shown in place of the HA logo (iOS
# companion app: notification_icon / color; Android uses them for the small icon
# and accent colour). Disarm events get an open padlock.
ICONS: dict[str, tuple[str, str]] = {
    "priority": ("mdi:alarm-light", "#C62828"),
    "alarms": ("mdi:shield-alert", "#D32F2F"),
    "tampers": ("mdi:shield-lock-open", "#E65100"),
    "faults": ("mdi:alert", "#F57C00"),
    "arming": ("mdi:shield-lock", "#2E7D32"),
    "test": ("mdi:clipboard-check-outline", "#6A1B9A"),
    "restore": ("mdi:shield-check", "#1565C0"),
    "custom": ("mdi:remote", "#455A64"),
    "chains": ("mdi:link-variant", "#455A64"),
    "zones": ("mdi:motion-sensor", "#1E88E5"),
}
DISARM_CODES = {84, 88, 90, 166, 193}  # System/Auto/Remote Disarmed, Disarm after alarm, Vehicle Disarmed


def icon_for(category: str, code: int | None) -> tuple[str, str]:
    """(mdi icon, background colour) for a notification."""
    if category == "arming" and code in DISARM_CODES:
        return "mdi:shield-off-outline", "#546E7A"
    return ICONS.get(category, ("mdi:shield-home", "#1E88E5"))


DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "categories": ["priority", "alarms", "tampers", "faults", "arming"],
    "areas": [],  # empty = all areas
    "zones": [],  # zones that notify when they go active
    "devices": [],  # mobile_app config entry ids; empty = all of the user's phones
    "critical": True,
    "url": "",  # dashboard path to open when tapped
}


def clean(prefs: dict[str, Any]) -> dict[str, Any]:
    out = {**DEFAULTS, **{k: v for k, v in (prefs or {}).items() if k in DEFAULTS}}
    out["categories"] = [c for c in out["categories"] if c in p.NOTIFY_CATEGORIES]
    out["areas"] = sorted({int(a) for a in out["areas"]})
    out["zones"] = sorted({int(z) for z in out["zones"]})
    out["devices"] = [str(d) for d in out["devices"]]
    out["enabled"] = bool(out["enabled"])
    out["critical"] = bool(out["critical"])
    out["url"] = str(out["url"] or "")
    return out


def wants(prefs: dict[str, Any], event: dict[str, Any]) -> bool:
    """Does this user's setting cover the event?"""
    if not prefs.get("enabled"):
        return False
    cat = event.get("category")
    if not cat or cat not in prefs.get("categories", []):
        return False
    if cat == "zones" and event.get("zone") not in prefs.get("zones", []):
        return False
    areas = prefs.get("areas") or []
    ev_areas = event.get("areas") or []
    if areas and ev_areas and not set(areas) & set(ev_areas):
        return False
    return True


def phones(hass: HomeAssistant, user_id: str) -> list[dict[str, str]]:
    """The user's companion-app devices that have a notify service."""
    out = []
    for e in hass.config_entries.async_entries("mobile_app"):
        if e.data.get("user_id") != user_id:
            continue
        name = e.data.get("device_name") or e.title
        svc = f"mobile_app_{slugify(name)}"
        if hass.services.has_service("notify", svc):
            out.append({"id": e.entry_id, "name": name, "service": svc,
                        "os": str(e.data.get("os_name") or "")})
    return out


class Notifier:
    def __init__(self, hass: HomeAssistant, entry_id: str, title: str) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.title = title
        self._store: Store = Store(hass, 1, NOTIFY_STORE)
        self._all: dict[str, dict[str, dict[str, Any]]] = {}
        self._zone_sent: dict[tuple[str, int], float] = {}

    async def async_load(self) -> None:
        self._all = await self._store.async_load() or {}

    def get(self, user_id: str) -> dict[str, Any]:
        return clean(self._all.get(self.entry_id, {}).get(user_id, {}))

    def users(self) -> dict[str, dict[str, Any]]:
        return {u: clean(v) for u, v in self._all.get(self.entry_id, {}).items()}

    async def async_set(self, user_id: str, prefs: dict[str, Any]) -> dict[str, Any]:
        merged = clean({**self.get(user_id), **prefs})
        self._all.setdefault(self.entry_id, {})[user_id] = merged
        await self._store.async_save(self._all)
        return merged

    @callback
    def handle(self, event: dict[str, Any]) -> None:
        for user_id, prefs in self.users().items():
            if not wants(prefs, event):
                continue
            if event.get("category") == "zones":
                key = (user_id, int(event.get("zone") or 0))
                now = time.monotonic()
                if now - self._zone_sent.get(key, -1e9) < ZONE_REPEAT_SECONDS:
                    continue
                self._zone_sent[key] = now
            for ph in phones(self.hass, user_id):
                if prefs["devices"] and ph["id"] not in prefs["devices"]:
                    continue
                self.hass.async_create_task(self._send(ph, prefs, event))

    async def _send(self, phone: dict[str, str], prefs: dict[str, Any], event: dict[str, Any]) -> None:
        cat = event.get("category") or ""
        where = event.get("area") or self.title
        when = event.get("time", "")
        message = event.get("text", "")
        if when:
            message += f" ({when[:5]})"
        icon, colour = icon_for(cat, event.get("code"))
        data: dict[str, Any] = {
            "group": "orisec", "tag": f"orisec-{cat}-{time.time_ns()}",
            "notification_icon": icon, "notification_icon_color": "#FFFFFF", "color": colour,
        }
        if prefs.get("url"):
            data["url"] = prefs["url"]  # iOS
            data["clickAction"] = prefs["url"]  # Android
        if cat in URGENT and prefs.get("critical"):
            if phone["os"].lower().startswith("ios") or phone["os"].lower() in ("ipados", "macos"):
                data["push"] = {"sound": {"name": "default", "critical": 1, "volume": 1.0},
                                "interruption-level": "critical"}
            else:
                data.update({"ttl": 0, "priority": "high", "channel": "alarm_stream"})
        try:
            await self.hass.services.async_call(
                "notify", phone["service"],
                {"title": f"{p.NOTIFY_CATEGORIES.get(cat, 'Alarm')}: {where}", "message": message, "data": data},
                blocking=False,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Couldn't send the alarm notification to %s", phone["name"], exc_info=True)
