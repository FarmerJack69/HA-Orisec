"""Per-user alarm notifications and event categories (Home Assistant stubbed out)."""

import asyncio
import importlib.util
import pathlib
import re
import sys
import types

_DIR = pathlib.Path(__file__).parents[1] / "custom_components" / "orisec"
_PKG = "orisec_notify_pkg"


def _stub_ha():
    ha = types.ModuleType("homeassistant")
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    core.callback = lambda f: f
    helpers = types.ModuleType("homeassistant.helpers")
    storage = types.ModuleType("homeassistant.helpers.storage")

    class Store:
        saved = None

        def __init__(self, *a):
            pass

        async def async_load(self):
            return None

        async def async_save(self, data):
            Store.saved = data

    storage.Store = Store
    util = types.ModuleType("homeassistant.util")
    util.slugify = lambda s: re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")
    for name, mod in {"homeassistant": ha, "homeassistant.core": core, "homeassistant.helpers": helpers,
                      "homeassistant.helpers.storage": storage, "homeassistant.util": util}.items():
        sys.modules.setdefault(name, mod)


def _load():
    _stub_ha()
    pkg = types.ModuleType(_PKG)
    pkg.__path__ = [str(_DIR)]
    sys.modules[_PKG] = pkg
    mods = {}
    for name in ("protocol", "const", "notifications"):
        spec = importlib.util.spec_from_file_location(f"{_PKG}.{name}", _DIR / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"{_PKG}.{name}"] = mod
        spec.loader.exec_module(mod)
        mods[name] = mod
    return mods


M = _load()
p, n = M["protocol"], M["notifications"]


# --- categories ----------------------------------------------------------------

def test_categories_cover_codes_seen():
    assert p.event_category(0) == "alarms"  # Intruder Alarm
    assert p.event_category(1) == "restore"
    assert p.event_category(14) == "priority"  # PA Alarm
    assert p.event_category(64) == "priority"  # Duress
    assert p.event_category(59) == "priority"  # Confirmed Alarm (confirmed = priority)
    assert p.event_category(60) == "priority"  # Confirmed PA Alarm
    assert p.event_category(28) == "tampers"
    assert p.event_category(92) == "faults"  # AC Failed
    assert p.event_category(84) == "arming"  # System Disarmed
    assert p.event_category(81) == "arming"  # part set (text is the part-set name)
    assert p.event_category(122) == "test"  # Walktest Started
    # Never notify: remote access, user n, engineer on/off site.
    for code in (61, 62, 98, 99, None):
        assert p.event_category(code) is None


def test_no_code_in_two_categories():
    seen = [c for codes in p._CAT_CODES.values() for c in codes]
    assert len(seen) == len(set(seen))


def test_event_areas():
    names = {1: "Home", 2: "Area 2"}
    ev = p.parse_event(1, b"System Disarmed Alex |Home Thu 05/11/26 - 07:46.25\r\x54")
    assert p.event_areas(ev, names) == [1]
    ev = p.parse_event(1, b"Remote Access 1 Alex 1,2 Thu 05/11/26 - 07:46.25\r\x3d")
    assert p.event_areas(ev, names) == [1, 2]
    ev = p.parse_event(1, b"AC Failed Thu 05/11/26 - 07:46.25\r\x5c")
    assert p.event_areas(ev, names) == []


# --- preferences -----------------------------------------------------------------

def test_clean_defaults_off():
    c = n.clean({})
    assert c["enabled"] is False and "alarms" in c["categories"] and c["zones"] == []
    c = n.clean({"categories": ["alarms", "bogus"], "areas": ["2", 1, 1], "junk": 1})
    assert c["categories"] == ["alarms"] and c["areas"] == [1, 2] and "junk" not in c


def test_wants():
    prefs = n.clean({"enabled": True, "categories": ["alarms", "zones"], "areas": [1], "zones": [5]})
    assert n.wants(prefs, {"category": "alarms", "areas": [1]})
    assert not n.wants(prefs, {"category": "alarms", "areas": [2]})  # other area
    assert n.wants(prefs, {"category": "alarms", "areas": []})  # panel-wide
    assert not n.wants(prefs, {"category": "arming", "areas": [1]})
    assert not n.wants(prefs, {"category": None, "areas": [1]})
    assert n.wants(prefs, {"category": "zones", "zone": 5, "areas": [1]})
    assert not n.wants(prefs, {"category": "zones", "zone": 2, "areas": [1]})
    assert not n.wants({**prefs, "enabled": False}, {"category": "alarms", "areas": [1]})


# --- sending ---------------------------------------------------------------------

class FakeEntry:
    def __init__(self, eid, user, name, os_name):
        self.entry_id, self.title = eid, name
        self.data = {"user_id": user, "device_name": name, "os_name": os_name}


class FakeHass:
    def __init__(self):
        self.calls = []
        self.tasks = []
        self.config_entries = types.SimpleNamespace(async_entries=lambda d: [
            FakeEntry("m1", "u1", "Alex iPhone", "iOS"), FakeEntry("m2", "u2", "Pixel", "Android")])
        hass = self

        class Services:
            def has_service(self, domain, svc):
                return svc in ("mobile_app_alex_iphone", "mobile_app_pixel")

            async def async_call(self, domain, svc, data, blocking=False):
                hass.calls.append((svc, data))

        self.services = Services()

    def async_create_task(self, coro):
        self.tasks.append(coro)


def _run(hass):
    async def go():
        for t in hass.tasks:
            await t
    asyncio.run(go())


def test_notifier_sends_to_the_right_people():
    hass = FakeHass()
    nt = n.Notifier(hass, "E", "Home")
    nt._all = {"E": {
        "u1": {"enabled": True, "categories": ["alarms", "arming", "zones"], "zones": [2]},
        "u2": {"enabled": True, "categories": ["arming"], "areas": [2]},
    }}
    nt.handle({"category": "alarms", "text": "Intruder Alarm Zone 2 Lounge PIR", "area": "Home",
               "areas": [1], "time": "23:23.41"})
    _run(hass)
    assert len(hass.calls) == 1
    svc, data = hass.calls[0]
    assert svc == "mobile_app_alex_iphone"
    assert data["title"] == "Alarms: Home" and data["message"].endswith("(23:23)")
    assert data["data"]["push"]["sound"]["critical"] == 1  # iOS critical alert
    assert data["data"]["notification_icon"] == "mdi:shield-alert" and data["data"]["color"] == "#D32F2F"

    hass.calls.clear(); hass.tasks.clear()
    nt.handle({"category": "arming", "text": "System Armed", "area": "", "areas": [2], "time": ""})
    _run(hass)
    assert {c[0] for c in hass.calls} == {"mobile_app_alex_iphone", "mobile_app_pixel"}


def test_zone_notifications_throttled():
    hass = FakeHass()
    nt = n.Notifier(hass, "E", "Home")
    nt._all = {"E": {"u1": {"enabled": True, "categories": ["zones"], "zones": [2]}}}
    ev = {"category": "zones", "zone": 2, "text": "Zone 2 Lounge PIR Active", "areas": [1]}
    nt.handle(ev); nt.handle(ev)
    nt.handle({**ev, "zone": 3})  # not chosen
    _run(hass)
    assert len(hass.calls) == 1


def test_android_critical_uses_alarm_stream():
    hass = FakeHass()
    nt = n.Notifier(hass, "E", "Home")
    nt._all = {"E": {"u2": {"enabled": True, "categories": ["priority"]}}}
    nt.handle({"category": "priority", "text": "Fire Alarm", "areas": []})
    _run(hass)
    assert hass.calls[0][1]["data"]["channel"] == "alarm_stream"


def test_set_saves():
    hass = FakeHass()
    nt = n.Notifier(hass, "E", "Home")
    out = asyncio.run(nt.async_set("u1", {"enabled": True, "zones": [5, 2]}))
    assert out["zones"] == [2, 5] and nt.get("u1")["enabled"] is True


def test_alarm_follow_on_lines_dont_notify():
    # As the official app: one alarm gives "Intruder Alarm" then "Disarm after alarm", nothing else.
    for code in (56, 57, 134, 154):
        assert p.event_category(code) is None
    assert p.event_category(166) == "arming"  # Disarm after alarm: not a critical alert


def test_exit_started_doesnt_notify():
    # As the app: arming sends only "System Armed" / the part-set line, not "Exit Started".
    for code in (36, 38, 40, 46, 63, 65, 66, 71, 72, 73, 74, 76, 77):
        assert p.event_category(code) is None
    for code in (78, 79, 80, 81, 82, 83, 84, 166):  # armed, keyswitch/quick/part armed, disarmed
        assert p.event_category(code) == "arming"
    assert p.event_category(75) == "faults"  # Exit Failed by Zone


def test_icons():
    assert n.icon_for("arming", 78) == ("mdi:shield-lock", "#2E7D32")
    assert n.icon_for("arming", 84)[0] == "mdi:shield-off-outline"
    assert n.icon_for("alarms", 0)[0] == "mdi:shield-alert"
    for cat in p.NOTIFY_CATEGORIES:
        assert cat in n.ICONS
