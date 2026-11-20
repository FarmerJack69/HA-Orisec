"""Polling coordinator for an Orisec panel."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import protocol as p
from .client import OrisecClient, OrisecError
from .const import (
    CLOCK_SYNC_MIN_GAP,
    CONF_CLOCK_SYNC,
    CONF_CLOCK_TOLERANCE,
    CONF_POLL_INTERVAL,
    CONF_TRANSPORT,
    DEFAULT_CLOCK_SYNC,
    DEFAULT_CLOCK_TOLERANCE,
    DOMAIN,
    EVENT_ORISEC,
    UNAVAILABLE_AFTER_SECONDS,
    ZONE_NAME_BATCH,
    poll_seconds,
)

_LOGGER = logging.getLogger(__name__)
# Polls the clock offset is the median of (smooths the 1 s jitter between the clocks).
CLOCK_SAMPLES = 31
# Extra reads run on timers (seconds), not every N polls, so a faster poll only
# speeds up the main status read. At most one of the rotating extras runs per poll.
EVENTS_EVERY_S = 2.0  # panel log check
FLAGS_EVERY_S = 30.0  # per-zone 1150 setting (rarely changes)
EXTRAS_EVERY_S = {  # name -> seconds between runs
    "zone_stats": 5.0,
    "panel_power": 20.0,
    "wireless": 5.0,
    "device": 5.0,
}
RTT_SAMPLES = 600  # main-read round trips kept for the poll stats
ZONE_STATS_BATCH = 16  # zones per request
WIRELESS_BATCH = 4  # zones per 10810 request when probing (44 bytes each)
DEVICE_BATCH = 4  # slots per 12510 request when probing (42 bytes each)
# The panel's clock lands about 2 s behind the time written , so write
# slightly ahead.
CLOCK_LEAD = 2.0


@dataclass
class Zone:
    number: int  # 1-based
    name: str
    zone_type: int
    areas: int

    @property
    def type_name(self) -> str:
        return p.ZONE_TYPE_NAMES.get(self.zone_type, f"Type {self.zone_type}")

    def in_area(self, area: int) -> bool:
        return bool(self.areas >> (area - 1) & 1)

    @property
    def area_list(self) -> list[int]:
        return [a for a in range(1, 9) if self.in_area(a)]

    @property
    def is_sensor(self) -> bool:
        return self.zone_type != p.ZONE_TYPE_NOT_USED and self.zone_type not in p.ZONE_TYPES_KEYSWITCH


@dataclass
class PanelConfig:
    login: p.LoginInfo
    zones: list[Zone] = field(default_factory=list)
    area_names: list[str] = field(default_factory=list)
    part_names: list[str] = field(default_factory=list)
    zone_flags: list[int] = field(default_factory=list)
    wireless_zones: list[int] = field(default_factory=list)  # zones with a learnt wireless device
    device_slots: list[int] = field(default_factory=list)  # 12510 slots with a network device

    @property
    def last_sensor_zone(self) -> int:
        """Highest zone number that has a zone sensor (0 if none)."""
        return max((z.number for z in self.zones if z.is_sensor), default=0)

    @property
    def user_areas(self) -> list[int]:
        """Areas the user code can use that have at least one zone in them
        (like the official app, empty areas are left out)."""
        mask = self.login.user_area or ((1 << max(self.login.max_areas, 1)) - 1)
        allowed = [a for a in range(1, max(self.login.max_areas, 1) + 1) if mask >> (a - 1) & 1]
        used = [a for a in allowed if any(
            z.zone_type != p.ZONE_TYPE_NOT_USED and z.in_area(a) for z in self.zones)]
        return used or allowed[:1]

    def part_name(self, part: int) -> str:
        if 0 < part <= len(self.part_names) and self.part_names[part - 1]:
            return self.part_names[part - 1]
        return f"Part {part}"

    def part_available(self, part: int, area: int | None = None, flags: list[int] | None = None) -> bool:
        """A part set is offered if the panel allows it and, for an area, it
        actually omits at least one zone in that area (otherwise it would just
        be a full set under another name)."""
        if not self.login.part_arm_mask >> (part - 1) & 1:
            return False
        flags = flags or self.zone_flags
        if area is None or not flags:
            return True
        bit = (p.ZONE_OMIT_PART_1, p.ZONE_OMIT_PART_2, p.ZONE_OMIT_PART_3)[part - 1]
        return any(
            z.zone_type != p.ZONE_TYPE_NOT_USED and z.in_area(area)
            and z.number - 1 < len(flags) and flags[z.number - 1] & bit
            for z in self.zones
        )


@dataclass
class PanelData:
    snapshot: p.PanelSnapshot
    zone_flag: list[int]
    # Panel clock minus HA's clock in seconds (median of recent polls), or None.
    clock_offset: int | None = None
    panel_clock: datetime | None = None
    zone_stats_at: datetime | None = None  # UTC time snapshot.zone_timers were read

    @property
    def time(self) -> tuple[int, int, int] | None:
        """Panel time (h, m, s) for the keypad display."""
        c = self.panel_clock
        return (c.hour, c.minute, c.second) if c else None


class OrisecCoordinator(DataUpdateCoordinator[PanelData]):
    config: PanelConfig

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: OrisecClient) -> None:
        seconds = poll_seconds(entry.data.get(CONF_TRANSPORT), entry.options.get(CONF_POLL_INTERVAL))
        # HA's scheduler times each refresh from the start of the current whole second,
        # so below 1 s it polls back to back for most of every second (20+ polls/s over
        # USB). Sub-second intervals use our own loop instead (start_polling).
        self._interval = seconds
        self._own_loop = seconds < 1
        super().__init__(
            hass, _LOGGER, name=f"{DOMAIN} {client.label}",
            update_interval=None if self._own_loop else timedelta(seconds=seconds), config_entry=entry,
        )
        # Last arm key HA sent per area, so Quick Arm can repeat the same set type.
        self.exit_key: dict[int, tuple[int, float]] = {}  # area -> (key, monotonic time)
        # Missed polls tolerated before going unavailable, so the grace period
        # stays about the same whatever the poll interval.
        self._max_misses = max(2, math.ceil(UNAVAILABLE_AFTER_SECONDS / seconds))
        self.client = client
        self._misses = 0
        self._poll = 0
        self._flag: list[int] = []
        # Monotonic time each timed read is next due (0 = straight away).
        self._due: dict[str, float] = {"events": 0.0, "flags": 0.0, **dict.fromkeys(EXTRAS_EVERY_S, 0.0)}
        # Poll statistics (diagnostics), for judging how fast the link can poll.
        self._rtt: deque[float] = deque(maxlen=RTT_SAMPLES)
        self._timed_task: asyncio.Task | None = None
        self._stats = {"polls": 0, "misses": 0, "relogins": 0, "started": time.monotonic()}
        self._logged_in = False
        # New panel log entries and zones going active are passed to these
        # (notifications), and fired on the bus as "orisec_event" for automations.
        self.event_listeners: list = []
        self._last_event: p.Event | None = None
        self._events_primed = False
        self._offsets: list[int] = []
        self._zone_timers: list[int] = []
        self._zone_counts: list[int] = []
        self._zone_stats_at: datetime | None = None
        self.wireless: dict[int, p.WirelessInfo] = {}
        self._wireless_next = 0
        self.devices: dict[int, p.DeviceInfo] = {}
        self.panel_power: p.PanelPower | None = None
        self._device_next = 0
        self._clock_synced_at = -1e9  # monotonic time of the last automatic sync attempt
        self._clock_task: asyncio.Task | None = None

    async def async_load_config(self) -> None:
        """Log in and read the static configuration (names, types)."""
        info = await self.client.login()
        self._logged_in = True
        max_zones = info.max_zones or 20
        max_areas = info.max_areas or 1
        cfg = PanelConfig(login=info)

        blocks = await self.client.read([
            (p.Q_ZONE_TYPES, 1, max_zones),
            (p.Q_ZONE_AREAS, 1, min(max_zones, 100)),
            (p.Q_AREA_TEXTS, 1, max_areas),
            (p.Q_PART_ARM_TEXTS, 1, 3),
            (p.Q_ZONE_FLAG, 1, max_zones),
        ]) or []
        types = p.parse_bytes(blocks, p.Q_ZONE_TYPES)
        areas = p.parse_bytes(blocks, p.Q_ZONE_AREAS)
        cfg.area_names = p.parse_strings(blocks, p.Q_AREA_TEXTS)
        cfg.part_names = p.parse_strings(blocks, p.Q_PART_ARM_TEXTS)
        cfg.zone_flags = p.parse_snapshot(blocks).zone_flag

        names: list[str] = []
        for start in range(1, max_zones + 1, ZONE_NAME_BATCH):
            count = min(ZONE_NAME_BATCH, max_zones - start + 1)
            reply = await self.client.read([(p.Q_ZONE_TEXTS, start, count)]) or []
            batch = p.parse_strings(reply, p.Q_ZONE_TEXTS)
            names.extend((batch + [""] * count)[:count])
            await asyncio.sleep(0.1)

        for z in range(max_zones):
            cfg.zones.append(Zone(
                number=z + 1,
                name=names[z] if z < len(names) and names[z] else f"Zone {z + 1}",
                zone_type=types[z] if z < len(types) else p.ZONE_TYPE_NOT_USED,
                areas=areas[z] if z < len(areas) else 0,
            ))
        self.config = cfg
        await self._probe_wireless()
        await self._probe_devices()
        await self._read_panel_power()

    async def _probe_wireless(self) -> None:
        """Find the zones with a wireless device (10810 is all zeros for wired zones)."""
        zones = [z.number for z in self.config.zones if z.is_sensor]
        found: list[int] = []
        try:
            for i in range(0, len(zones), WIRELESS_BATCH):
                batch = zones[i : i + WIRELESS_BATCH]
                blocks = await self.client.read([(p.Q_WIRELESS, z, 1) for z in batch]) or []
                for b in blocks:
                    if b.cmd == p.Q_WIRELESS and (w := p.parse_wireless(b)) is not None:
                        found.append(w.zone)
                        self.wireless[w.zone] = w
                await asyncio.sleep(0.1)
        except Exception:  # noqa: BLE001 - extras; never block setup
            _LOGGER.debug("Wireless probe failed", exc_info=True)
        self.config.wireless_zones = sorted(set(found))

    async def _read_wireless_next(self) -> None:
        """Refresh one wireless zone (round robin), so a full pass takes about a minute."""
        zones = self.config.wireless_zones
        if not zones:
            return
        zone = zones[self._wireless_next % len(zones)]
        self._wireless_next += 1
        try:
            blocks = await self.client.read([(p.Q_WIRELESS, zone, 1)]) or []
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Wireless read failed", exc_info=True)
            return
        for b in blocks:
            if b.cmd == p.Q_WIRELESS and b.start == zone and (w := p.parse_wireless(b)) is not None:
                self.wireless[zone] = w

    async def _probe_devices(self) -> None:
        """Find the keypads, expanders and sounders on the panel's network (12510)."""
        found: list[int] = []
        try:
            for start in range(1, p.DEVICE_SLOTS + 1, DEVICE_BATCH):
                slots = range(start, min(start + DEVICE_BATCH, p.DEVICE_SLOTS + 1))
                blocks = await self.client.read([(p.Q_DEVICES, s, 1) for s in slots]) or []
                for b in blocks:
                    if b.cmd == p.Q_DEVICES and (d := p.parse_device(b)) is not None:
                        found.append(d.slot)
                        self.devices[d.slot] = d
                await asyncio.sleep(0.1)
        except Exception:  # noqa: BLE001 - extras; never block setup
            _LOGGER.debug("Network device probe failed", exc_info=True)
        self.config.device_slots = sorted(set(found))

    async def _read_device_next(self) -> None:
        """Refresh one network device (round robin). The panel itself only
        updates the light level every minute or two, so this is plenty."""
        slots = self.config.device_slots
        if not slots:
            return
        slot = slots[self._device_next % len(slots)]
        self._device_next += 1
        try:
            blocks = await self.client.read([(p.Q_DEVICES, slot, 1)]) or []
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Network device read failed", exc_info=True)
            return
        for b in blocks:
            if b.cmd == p.Q_DEVICES and b.start == slot and (d := p.parse_device(b)) is not None:
                self.devices[slot] = d

    async def _read_panel_power(self) -> None:
        """Panel supply / battery voltage and currents (540)."""
        try:
            blocks = await self.client.read([(p.Q_PANEL_POWER, 1, p.PANEL_POWER_COUNT)]) or []
        except Exception:  # noqa: BLE001 - extras; never break polling
            _LOGGER.debug("Panel power read failed", exc_info=True)
            return
        if (pw := p.parse_panel_power(blocks)) is not None:
            self.panel_power = pw

    def _take_due(self, name: str, every: float, now: float) -> bool:
        """True (and schedule the next run) if the timed read `name` is due."""
        if now < self._due[name]:
            return False
        self._due[name] = now + every
        return True

    async def _timed_reads(self) -> None:
        """Panel log check and at most one rotating extra read, when due."""
        mono = time.monotonic()
        if self._take_due("events", EVENTS_EVERY_S, mono):
            try:
                await self._check_events()
            except Exception:  # noqa: BLE001 - never let the log watcher break polling
                _LOGGER.debug("Event log check failed", exc_info=True)
        for name, every in EXTRAS_EVERY_S.items():
            if self._take_due(name, every, mono):
                await self._run_extra(name)
                break  # at most one extra per poll keeps the gap between polls short

    async def _run_extra(self, name: str) -> None:
        if name == "zone_stats":
            await self._read_zone_stats()
        elif name == "wireless":
            await self._read_wireless_next()
        elif name == "device":
            await self._read_device_next()
        elif name == "panel_power":
            await self._read_panel_power()

    def poll_stats(self) -> dict:
        """How the polling is going, for diagnostics (testing faster poll rates)."""
        rtt = sorted(self._rtt)
        up = max(time.monotonic() - self._stats["started"], 1e-9)

        def ms(x: float) -> int:
            return round(x * 1000)

        return {
            "transport": self.client.transport_type,
            "poll_interval_s": self._interval,
            "uptime_s": round(up),
            "polls": self._stats["polls"],
            "polls_per_s": round(self._stats["polls"] / up, 2),
            "misses": self._stats["misses"],
            "relogins": self._stats["relogins"],
            "miss_rate_pct": round(100 * self._stats["misses"] / max(self._stats["polls"], 1), 2),
            "rtt_samples": len(rtt),
            "rtt_ms_median": ms(rtt[len(rtt) // 2]) if rtt else None,
            "rtt_ms_p95": ms(rtt[int(len(rtt) * 0.95)]) if rtt else None,
            "rtt_ms_max": ms(rtt[-1]) if rtt else None,
        }

    def start_polling(self) -> None:
        """Start the sub-second poll loop (after the first refresh); HA polls otherwise."""
        if self._own_loop:
            self.config_entry.async_create_background_task(self.hass, self._poll_loop(), "orisec poll loop")

    async def _poll_loop(self) -> None:
        # Wait the interval after each poll finishes; async_refresh logs and
        # publishes failures itself, so the loop keeps going through them.
        while True:
            await asyncio.sleep(self._interval)
            await self.async_refresh()

    async def _async_update_data(self) -> PanelData:
        if self.client.relogin_needed:
            self.client.relogin_needed = False
            self._logged_in = False
        try:
            if not self._logged_in:
                await self.client.login()
                self._logged_in = True
        except OrisecError as err:  # includes a refused code
            raise UpdateFailed(str(err)) from err

        reads = [
            (p.Q_SYS_STATE, 1, p.SYS_STATE_COUNT),
            (p.Q_ZONE_STATUS, 1, len(self.config.zones)),
            (p.CMD_LCD, 1, 1),
            *((q, 1, 1) for q in p.CLOCK_PARAMS),
        ]

        mono = time.monotonic()
        if self._take_due("flags", FLAGS_EVERY_S, mono):
            reads.append((p.Q_ZONE_FLAG, 1, len(self.config.zones)))
        self._poll += 1
        self._stats["polls"] += 1

        sent = time.monotonic()
        blocks = await self.client.read(reads)
        now = dt_util.now().replace(tzinfo=None, microsecond=0)
        if blocks is None or not blocks:
            self._misses += 1
            self._stats["misses"] += 1
            _LOGGER.debug("No reply from panel (%d in a row)", self._misses)
            if self._misses >= self._max_misses:
                # Force a fresh login on the next attempt; the panel's comms may have restarted.
                self._logged_in = False
                self._stats["relogins"] += 1
                await self.client.close()
                raise UpdateFailed(
                    f"Panel via {self.client.label} is not responding"
                )
            if self.data is None:
                raise UpdateFailed("No reply from panel")
            return self.data  # one missed poll: keep last good data briefly
        self._rtt.append(time.monotonic() - sent)

        snap = p.parse_snapshot(blocks)
        if p.Q_SYS_STATE not in snap.seen:
            # Usually an error reply ("not logged in") after the panel restarted its comms.
            self._misses += 1
            self._stats["misses"] += 1
            self._stats["relogins"] += 1
            self._logged_in = False
            raise UpdateFailed("Panel reply had no system state; will log in again")
        self._misses = 0
        if snap.zone_flag:
            self._flag = snap.zone_flag
        if self.data is not None:
            self._zone_edges(self.data.snapshot.zone_status, snap.zone_status)
        # The log check and extras run after this poll's data is published, so they
        # never hold up the screen; they delay the next poll a little instead.
        if self._timed_task is None or self._timed_task.done():
            self._timed_task = self.config_entry.async_create_background_task(
                self.hass, self._timed_reads(), "orisec timed reads"
            )
        snap.zone_timers = self._zone_timers
        snap.zone_counts = self._zone_counts
        clock = p.parse_clock(blocks)
        if clock is not None:
            self._offsets.append(round((clock - now).total_seconds()))
            del self._offsets[:-CLOCK_SAMPLES]
        offset = sorted(self._offsets)[len(self._offsets) // 2] if self._offsets else None
        self._maybe_auto_sync(snap, offset)
        return PanelData(
            snapshot=snap, zone_flag=self._flag,
            clock_offset=offset, panel_clock=clock, zone_stats_at=self._zone_stats_at,
        )

    async def _read_zone_stats(self) -> None:
        """Per-zone timer (10120) and count (10140), in their own small requests:
        added to the main poll they made the reply too big and the panel went silent."""
        n = self.config.last_sensor_zone
        timers: list[int] = []
        counts: list[int] = []
        try:
            for start in range(1, n + 1, ZONE_STATS_BATCH):
                count = min(ZONE_STATS_BATCH, n - start + 1)
                blocks = await self.client.read([(p.Q_ZONE_TIMERS, start, count), (p.Q_ZONE_COUNTS, start, count)])
                snap = p.parse_snapshot(blocks or [])
                if len(snap.zone_timers) < count or len(snap.zone_counts) < count:
                    return  # keep the last good values
                timers += snap.zone_timers[:count]
                counts += snap.zone_counts[:count]
        except Exception:  # noqa: BLE001 - extras; never break polling
            _LOGGER.debug("Zone timer/count read failed", exc_info=True)
            return
        self._zone_timers, self._zone_counts = timers, counts
        self._zone_stats_at = dt_util.utcnow()

    # --- clock ---------------------------------------------------------------

    def _maybe_auto_sync(self, snap: p.PanelSnapshot, offset: int | None) -> None:
        opts = self.config_entry.options
        if not opts.get(CONF_CLOCK_SYNC, DEFAULT_CLOCK_SYNC) or offset is None:
            return
        if len(self._offsets) < 10 or abs(offset) < opts.get(CONF_CLOCK_TOLERANCE, DEFAULT_CLOCK_TOLERANCE):
            return
        if self._clock_task is not None and not self._clock_task.done():
            return
        now = time.monotonic()
        if now - self._clock_synced_at < CLOCK_SYNC_MIN_GAP:
            return
        busy = {"arming", "pending", "triggered"}
        if any(snap.area(a).state in busy or snap.area(a).alarm for a in self.config.user_areas):
            return
        self._clock_synced_at = now
        _LOGGER.info("Panel clock is %+d s from Home Assistant's; setting it", offset)

        async def run() -> None:
            try:
                await self.async_sync_clock()
            except Exception as err:  # noqa: BLE001 - try again after the gap
                _LOGGER.warning("Automatic panel clock sync failed: %s", err)

        self._clock_task = self.config_entry.async_create_background_task(self.hass, run(), "orisec clock sync")

    async def async_sync_clock(self, when: datetime | None = None) -> None:
        """Set the panel clock to HA's local time (or ``when``).

        The six parameters are written one by one, so stay clear of a minute
        rollover between the hour/minute/second writes.
        """
        if when is None:
            while not 1 <= dt_util.now().second <= 54:
                await asyncio.sleep(0.5)
            # Each write takes ~0.1-0.2 s; aim for the moment the seconds land.
            when = (dt_util.now() + timedelta(seconds=CLOCK_LEAD)).replace(tzinfo=None, microsecond=0)
        await self.client.set_clock(when)
        self._offsets.clear()
        await self.async_request_refresh()

    # --- events --------------------------------------------------------------

    def area_names(self) -> dict[int, str]:
        return {i + 1: n for i, n in enumerate(self.config.area_names) if n}

    def _emit(self, data: dict) -> None:
        data = {"config_entry_id": self.config_entry.entry_id, **data}
        self.hass.bus.async_fire(EVENT_ORISEC, data)
        for cb in list(self.event_listeners):
            try:
                cb(data)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Orisec event listener failed")

    async def _check_events(self) -> None:
        """Pass on entries added to the panel log since the last check.

        Reads the newest entry; if it changed, reads back until the last one
        seen (up to 20), then passes them on oldest first. The first check after
        setup only takes a baseline, so a restart doesn't replay old events."""
        newest = await self.client.read_event(1)
        if newest is None:
            return
        if not self._events_primed:
            self._last_event, self._events_primed = newest, True
            return
        if p.same_event(newest, self._last_event):
            return
        new = [newest]
        for n in range(2, 21):
            ev = await self.client.read_event(n)
            if ev is None or p.same_event(ev, self._last_event):
                break
            new.append(ev)
        self._last_event = newest
        names = self.area_names()
        for ev in reversed(new):
            self._emit({
                "kind": "log",
                "code": ev.code,
                "category": p.event_category(ev.code),
                "text": ev.text,
                "area": ev.area,
                "areas": p.event_areas(ev, names),
                "date": ev.date,
                "time": ev.time,
            })

    def _zone_edges(self, before: list[int], after: list[int]) -> None:
        """A zone going active (the app's per-zone Notify)."""
        for z in self.config.zones:
            i = z.number - 1
            if i >= len(after) or i >= len(before) or not z.is_sensor:
                continue
            if after[i] & p.ZONE_ACTIVE and not before[i] & p.ZONE_ACTIVE:
                self._emit({
                    "kind": "zone",
                    "code": None,
                    "category": "zones",
                    "text": f"Zone {z.number} {z.name} Active",
                    "zone": z.number,
                    "zone_name": z.name,
                    "area": "",
                    "areas": z.area_list,
                })

    # --- actions -------------------------------------------------------------

    async def async_press_keys(self, keys: list[str]) -> None:
        for key in keys:
            if not await self.client.press_key(key):
                raise OrisecError("Panel did not acknowledge the key press")
            await asyncio.sleep(0.15)
        await self.async_request_refresh()

    def can_omit(self, zone: int) -> bool:
        """The zone's 1150 setting lets the user omit it (as the app's Omit button)."""
        flags = (self.data.zone_flag if self.data else None) or self.config.zone_flags
        return 0 < zone <= len(flags) and bool(flags[zone - 1] & p.ZONE_OMIT_MANUAL)

    def is_omitted(self, zone: int) -> bool:
        """Manually omitted (the only kind the omit command changes)."""
        st = self.data.snapshot.zone_status if self.data else []
        return 0 < zone <= len(st) and bool(st[zone - 1] & p.ZONE_OMITTED)

    def zone_area_set(self, zone: int) -> bool:
        """Any of the zone's areas is not disarmed."""
        z = next((z for z in self.config.zones if z.number == zone), None)
        sys = self.data.snapshot.sys_state if self.data else []
        if z is None or len(sys) <= p.SYS_OPEN:
            return False
        return any(not (sys[p.SYS_OPEN] >> (a - 1) & 1) for a in z.area_list)

    async def async_omit_zone(self, zone: int, omit: bool | None = None, code: str | None = None) -> None:
        """Omit or reinstate a zone (None toggles), then confirm from the zone status.

        With `code`, it's done as that panel user (their permissions, their name in the log)."""
        if omit is None:
            omit = not self.is_omitted(zone)
        if code:
            await self.client.as_user(code, p.build_omit_zone(zone, omit))
        elif not await self.client.omit_zone(zone, omit):
            raise OrisecError("Panel did not acknowledge the omit command")
        await asyncio.sleep(0.3)
        await self.async_refresh()
        if self.is_omitted(zone) != omit:
            what = "omit" if omit else "reinstate"
            if self.zone_area_set(zone):
                raise OrisecError(f"Can't {what} zone {zone} while its area is armed")
            raise OrisecError(f"Panel did not {what} zone {zone} (it may not be allowed to be omitted)")

    async def async_read_events(self, start: int = 1, count: int = 10) -> list[p.Event]:
        """Read `count` event-log entries from `start` (1 = newest), one per request.

        Reading from 1 makes the panel fix the starting point, so later pages
        (start 11, 21, ...) continue from the same place."""
        events: list[p.Event] = []
        for n in range(start, start + count):
            ev = await self.client.read_event(n)
            if ev is None:
                break
            events.append(ev)
        return events

    def area_state(self, area: int) -> str | None:
        return self.data.snapshot.area(area).state if self.data else None

    async def async_arm(self, key: int, area: int, code: str | None = None) -> None:
        """Send an arm / disarm / reset key for one area.

        With `code`, it's sent as that panel user, so the panel applies their
        permissions and logs their name. The panel acks a command it ignores
        (e.g. an area that user can't use), so with a code the result is checked
        from the area state afterwards."""
        before = self.area_state(area)
        frame = p.build_arm_key(key, 1 << (area - 1))
        if code:
            await self.client.as_user(code, frame)
        elif not await self.client.arm_key(key, 1 << (area - 1)):
            raise OrisecError("Panel did not acknowledge the command")
        if key in (p.ARM_FULL, p.ARM_PART_1, p.ARM_PART_2, p.ARM_PART_3):
            self.exit_key[area] = (key, time.monotonic())
        elif key == p.DISARM:
            self.exit_key.pop(area, None)
        if not code:
            await asyncio.sleep(0.5)
            await self.async_request_refresh()
            return
        # Confirm it took effect (only for arm/disarm from a known starting state).
        expect = None
        if key == p.DISARM and before not in (None, "disarmed"):
            expect = lambda s: s == "disarmed"  # noqa: E731
        elif key != p.DISARM and key != p.RESET and before == "disarmed":
            expect = lambda s: s != "disarmed"  # noqa: E731
        for _ in range(3):
            await asyncio.sleep(0.5)
            await self.async_refresh()
            if expect is None or expect(self.area_state(area) or ""):
                return
        if expect is not None:
            if area in self.exit_key and self.exit_key[area][0] == key:
                self.exit_key.pop(area, None)
            raise OrisecError("The panel didn't carry it out (that user may not be allowed to, or the area isn't ready)")
