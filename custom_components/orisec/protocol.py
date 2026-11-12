"""Orisec ControlPlus local UDP protocol.

Pure Python with no Home Assistant imports, so it can be tested and reused on its own.
See docs/protocol.md for the details.

Frame (plain form, as accepted and answered by the panel):

    [total_len u16][block][block]...[crc u16]

    block = [cmd u16][start u16][count u16][data_len u16][data ...]

All integers are little-endian. ``total_len`` covers the whole frame including the CRC.
CRC-16: table polynomial 0x1021, start value 0xFFFF, byte-wise update
``crc = TABLE[(b ^ crc) & 0xFF] ^ (crc >> 8)``, over every byte before the CRC.

Requests are blocks with ``data_len`` 0 (a read of ``count`` items from ``start``)
or with data (a command). Replies use the same block layout.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field

DEFAULT_PORT = 20202

# --- command / query IDs ---------------------------------------------------

CMD_PASSWORD = 1
CMD_PANEL_INFO = 2
CMD_PANEL_STATE = 3
CMD_CONTROL_OUTPUT = 6  # control: zone omit (op 3); other ops drive outputs, never sent
CMD_LCD = 9
CMD_KEYPAD_CHAR = 10
CMD_ARM_KEY = 11
CMD_EVENT_LOG = 12
CMD_SYS_CONFIG = 20
CMD_ERROR = 99

Q_ZONE_TYPES = 1110
Q_MAX_ZONES = 1114
Q_ZONE_TEXTS = 1120
Q_ZONE_WIRING = 1130
Q_ZONE_AREAS = 1140
Q_MAX_AREAS = 1141
Q_ZONE_FLAG = 1150  # per-zone OMIT attribute bitfield (see ZONE_OMIT_* / part_arm_omits)
Q_AREA_TEXTS = 2110
Q_AREA_ARM_MODE = 2120
Q_REM_OUTPUT_TEXTS = 3520
Q_MAX_REM_OUTPUTS = 3524
Q_PART_ARM_TEXTS = 3540
Q_PANEL_TIME_H = 9110
Q_PANEL_TIME_M = 9120
Q_PANEL_TIME_S = 9130
Q_PANEL_DAY = 9140
Q_PANEL_MONTH = 9150
Q_PANEL_YEAR = 9160  # two digits
# Panel clock, one byte each, in the order they are written when setting time and date.
CLOCK_PARAMS = (Q_PANEL_TIME_H, Q_PANEL_TIME_M, Q_PANEL_TIME_S, Q_PANEL_DAY, Q_PANEL_MONTH, Q_PANEL_YEAR)
Q_ZONE_STATUS = 10110
Q_ZONE_TIMERS = 10120  # u32 per zone: tenths of a second since the zone last changed (Active/Healthy)
Q_WIRELESS = 10810  # per zone, read one zone per block: wireless device details (see parse_wireless)
Q_ZONE_COUNTS = 10140  # u16 per zone: activation count (the keypad's "Count"; reset from the keypad)
# 12510 (Q_DEVICES): per network device slot, see parse_device
Q_SYS_STATE = 10320
Q_REM_OUTPUT_STATE = 10340

# --- system state array (Q_SYS_STATE). Each entry is a per-area bitmask.
# Indices are array positions: a read with start=S puts data[i] at S - 1 + i.

SYS_AC_FAULT = 1
SYS_OPEN = 3  # disarmed
SYS_JUST_DISARMED = 4
SYS_ARMED = 5
SYS_PART_ARMED = 6
SYS_ARMED_ALARM = 7
SYS_BELL = 8
SYS_STROBE = 9
SYS_ALARM = 10
SYS_CONFIRMED_ALARM = 11
SYS_ALARM_MEMORY = 13  # seen set with ALARM after disarm, until reset (meaning inferred)
SYS_FIRE = 14
SYS_PA = 15
SYS_TAMPER = 19
SYS_READY = 20
SYS_TROUBLE = 21
SYS_BYPASS = 23
SYS_BATTERY_FAULT = 31
SYS_IN_EXIT = 40
SYS_IN_ENTRY = 41
SYS_IN_ALARM = 43
SYS_CALL_ENGINEER = 48
SYS_ARMING_FAILED = 59
SYS_PART_1 = 61
SYS_PART_2 = 62
SYS_PART_3 = 63
SYS_STATE_COUNT = 65

# --- direct arm/disarm (CMD_ARM_KEY) ---------------------------------------

ARM_FULL = 0
ARM_PART_1 = 1
ARM_PART_2 = 2
ARM_PART_3 = 3
DISARM = 4
RESET = 99
PANIC = 112

# --- zone status word (Q_ZONE_STATUS, u16 per zone) -------------------------

ZONE_ACTIVE = 0x0001
ZONE_TAMPER = 0x0002
ZONE_OMITTED = 0x0040  # confirmed by omitting zones from the keypad (manual omit)
ZONE_PART_OMITTED = 0x0020  # left out by the part set in use (confirmed: zone 5 in Bedtime Arm)
ZONE_ALARMED = 0x0100  # zone caused an alarm; keypad shows "Zone n: Alarmed" until reset

# --- per-zone OMIT attribute bitfield (Q_ZONE_FLAG = 1150) -------------------
# The panel's *programmed* omit rules for each zone (not live status).
ZONE_OMIT_PART_1 = 0x0001  # omitted when Part Arm 1 is selected
ZONE_OMIT_PART_2 = 0x0002  # omitted when Part Arm 2 is selected
ZONE_OMIT_PART_3 = 0x0004  # omitted when Part Arm 3 is selected
ZONE_OMIT_MANUAL = 0x0008  # user may manually omit when arming
ZONE_OMIT_AUTO_EXIT = 0x0010  # auto-omit at end of exit if still active
ZONE_OMIT_OMIT_KEY = 0x0020  # omitted while an 'Omit Key' zone is active
ZONE_OMIT_WIRELESS_NOPOLL = 0x0040  # wireless: no fault if device hasn't polled
ZONE_OMIT_EXIT_WHEN_READY = 0x0080  # ignored when arming if 'Exit When Ready' on
ZONE_OMIT_MULTI_HOLDOFF = 0x0100  # 'Multi Hold Off' (30 s)
ZONE_OMIT_TEMP_PKEY = 0x0200  # temporary omit via PART key when part set

_PART_OMIT_BITS = (
    (1, ZONE_OMIT_PART_1),
    (2, ZONE_OMIT_PART_2),
    (3, ZONE_OMIT_PART_3),
)


def part_arm_omits(flag: int) -> list[int]:
    """Return the part-set numbers a zone is programmed to be omitted from."""

    return [part for part, bit in _PART_OMIT_BITS if flag & bit]

# --- zone types (Q_ZONE_TYPES) ----------------------------------------------

ZONE_TYPE_NAMES: dict[int, str] = {
    0: "Not Used", 1: "Final Exit 1", 2: "Final Exit 2", 3: "Entry Route",
    4: "Intruder", 5: "Perimeter", 6: "Fire", 7: "PA Silent", 8: "PA Audible",
    9: "PA Confirmed", 10: "PA Confirmed Silent", 11: "Disarmed PA Silent",
    12: "Disarmed PA Audible", 13: "Medical", 14: "24 Hour", 15: "24 Hour (Int)",
    16: "Tamper", 17: "Exit Terminator", 18: "Arm/Disarm Key", 19: "Part 1 Key",
    20: "Part 2 Key", 21: "Part 3 Key", 22: "Omit Key", 23: "Security Key",
    24: "Auxiliary", 25: "Warning", 26: "Log/Monitor", 27: "Fault", 28: "Counter",
    29: "Flood 24 Hour", 30: "CO 24 Hour", 31: "Disarm Only Key", 32: "Wireless Tamper",
    33: "RTE / Door Bell", 34: "Orisec VP", 35: "Arm Only Key",
    36: "Fire Alarm Log", 37: "Fire Fault Log",
}
ZONE_TYPE_NOT_USED = 0
# Keyswitch-style inputs: not useful as sensors.
ZONE_TYPES_KEYSWITCH = frozenset({18, 19, 20, 21, 22, 23, 31, 35})

# --- virtual keypad characters (CMD_KEYPAD_CHAR) ----------------------------


class Key:
    ENTER = "Y"
    CLEAR = "N"
    FULL_ARM = "F"
    PART_ARM = "P"
    AREA = "A"
    OMIT = "O"
    UP = "u"
    DOWN = "d"
    LEFT = "l"
    RIGHT = "r"
    CHIME = "C"
    BACK = "R"


KEY_NAMES: dict[str, str] = {
    **{d: d for d in "0123456789"},
    "enter": Key.ENTER, "clear": Key.CLEAR, "full_arm": Key.FULL_ARM,
    "part_arm": Key.PART_ARM, "area": Key.AREA, "omit": Key.OMIT,
    "up": Key.UP, "down": Key.DOWN, "left": Key.LEFT, "right": Key.RIGHT,
    "chime": Key.CHIME, "back": Key.BACK,
}
VALID_KEY_CODES = frozenset(KEY_NAMES.values())


# --- CRC --------------------------------------------------------------------

def _make_table() -> list[int]:
    table = []
    for i in range(256):
        c = i << 8
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) & 0xFFFF if c & 0x8000 else (c << 1) & 0xFFFF
        table.append(c)
    return table


_CRC_TABLE = _make_table()


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc = _CRC_TABLE[(b ^ crc) & 0xFF] ^ (crc >> 8)
    return crc & 0xFFFF


# --- frames -----------------------------------------------------------------

@dataclass
class Block:
    cmd: int
    start: int = 1
    count: int = 1
    data: bytes = b""


def build_frame(blocks: list[Block]) -> bytes:
    body = b"".join(
        struct.pack("<HHHH", b.cmd, b.start, b.count, len(b.data)) + b.data for b in blocks
    )
    total = 2 + len(body) + 2
    head = struct.pack("<H", total) + body
    return head + struct.pack("<H", crc16(head))


class FrameError(ValueError):
    pass


def parse_frame(frame: bytes) -> list[Block]:
    if len(frame) < 4:
        raise FrameError("frame too short")
    (total,) = struct.unpack_from("<H", frame, 0)
    if total != len(frame):
        raise FrameError(f"length mismatch: header {total}, actual {len(frame)}")
    if crc16(frame[:-2]) != struct.unpack_from("<H", frame, total - 2)[0]:
        raise FrameError("bad CRC")
    blocks = []
    pos, end = 2, total - 2
    while pos + 8 <= end:
        cmd, start, count, dlen = struct.unpack_from("<HHHH", frame, pos)
        pos += 8
        if pos + dlen > end:
            raise FrameError("truncated block")
        blocks.append(Block(cmd, start, count, frame[pos : pos + dlen]))
        pos += dlen
    return blocks


# --- request builders -------------------------------------------------------

def build_login(password: str) -> bytes:
    """Password plus panel info, panel state and system config in one frame."""
    if not password.isdigit():
        raise ValueError("password must be digits")
    return build_frame([
        Block(CMD_PASSWORD, 1, 1, password.encode("ascii")),
        Block(CMD_PANEL_INFO),
        Block(CMD_PANEL_STATE),
        Block(CMD_SYS_CONFIG),
    ])


def build_query(reads: list[tuple[int, int, int]]) -> bytes:
    """Multi-read: [(query_id, start, count), ...]."""
    return build_frame([Block(cmd, start, count) for cmd, start, count in reads])


def build_arm_key(key: int, area_mask: int) -> bytes:
    """Direct arm/disarm: ARM_FULL, ARM_PART_1..3, DISARM, RESET."""
    if key == PANIC:
        raise ValueError("panic is deliberately not supported here")
    data = bytes([key]) + (area_mask & 0xFFFFFF).to_bytes(3, "little")
    return build_frame([Block(CMD_ARM_KEY, 1, 1, data)])


# --- panel clock -------------------------------------------------------------
#
# The clock is six one-byte parameters (CLOCK_PARAMS), written one per frame,
# hour first. The panel acks each with the same block and no data.


def build_clock_writes(dt) -> list[bytes]:
    """Frames that set the panel clock to ``dt`` (a datetime, panel local time)."""
    if not 2000 <= dt.year <= 2099:
        raise ValueError("year out of range")
    values = (dt.hour, dt.minute, dt.second, dt.day, dt.month, dt.year % 100)
    return [build_frame([Block(q, 1, 1, bytes([v]))]) for q, v in zip(CLOCK_PARAMS, values, strict=True)]


def is_write_ack(blocks: list[Block], param: int) -> bool:
    return any(b.cmd == param and not b.data for b in blocks)


def parse_clock(blocks: list[Block]):
    """The panel clock as a naive datetime, or None if a part is missing or invalid."""
    from datetime import datetime

    vals = []
    for q in CLOCK_PARAMS:
        b = parse_bytes(blocks, q)
        if not b:
            return None
        vals.append(b[0])
    h, m, s, d, mo, y = vals
    try:
        return datetime(2000 + y, mo, d, h, m, s)
    except ValueError:
        return None


# --- zone omit / reinstate (CMD_CONTROL_OUTPUT, op type 3) ------------------

CONTROL_OP_OMIT = 3
OMIT_REINSTATE = 0
OMIT_OMIT = 1


def build_omit_zone(zone: int, omit: bool) -> bytes:
    """Omit (True) or reinstate (False) one zone, as the official app does.

    Data is ``[zone][0][state][op]`` with a 1-based zone, state 1 omit / 0
    reinstate and op 3. The panel acks even when it ignores the request (zone
    not allowed to be omitted, or its area armed), so re-read 10110 to confirm.
    Only op 3 is ever built here; the other ops of this command drive outputs.
    """
    if not 1 <= zone <= 250:
        raise ValueError(f"zone {zone} out of range")
    data = bytes([zone, 0, OMIT_OMIT if omit else OMIT_REINSTATE, CONTROL_OP_OMIT])
    return build_frame([Block(CMD_CONTROL_OUTPUT, 1, 1, data)])


# --- event log (CMD_EVENT_LOG) ---------------------------------------------
#
# Read [12, n, 1]: n = 1 is the newest entry (reading n = 1 also fixes the
# starting point for n > 1, so pages stay stable while new events arrive).
# Read one entry per request: several in one frame overwrite each other.
# The panel sends each entry as finished text, then CR and the event code:
#   "Zone 2 Omitted Lounge PIR |Home Wed 04/11/26 - 23:23.41" 0x0D 0x1A

_EVENT_TAIL = re.compile(r"^(?P<area>.*?)\s*(?P<day>Mon|Tue|Wed|Thu|Fri|Sat|Sun) (?P<date>\d\d/\d\d/\d\d) - (?P<time>\d\d:\d\d\.\d\d)\s*$")


@dataclass
class Event:
    index: int  # 1 = newest
    code: int | None
    text: str  # what happened, e.g. "Zone 2 Omitted Lounge PIR"
    area: str  # e.g. "Home" (may be empty)
    day: str  # "Wed"
    date: str  # "04/11/26" (dd/mm/yy)
    time: str  # "23:23.41"
    raw: str


def parse_event(index: int, data: bytes) -> Event | None:
    """One event-log entry, or None if the slot is empty."""
    code = None
    body = data
    cr = data.rfind(b"\r")
    if cr != -1:
        if cr + 1 < len(data):
            code = data[cr + 1]
        body = data[:cr]
    raw = body.split(b"\0", 1)[0].decode("latin-1").strip()
    if not raw or raw.startswith("No Event"):
        return None
    text, bar, rest = raw.partition("|")
    if not bar:
        # Several areas: the panel lists their numbers in the text and has no
        # "|area" part, e.g. "Remote Access 1 Alex 1,2 Thu 05/11/26 - 07:46.25" (as the app shows it).
        m = _EVENT_TAIL.match(raw)
        if m and m["area"].strip():
            return Event(index, code, m["area"].strip(), "", m["day"], m["date"], m["time"], raw)
    m = _EVENT_TAIL.match(rest)
    if m:
        return Event(index, code, text.strip(), m["area"].strip(), m["day"], m["date"], m["time"], raw)
    return Event(index, code, text.strip() or raw, rest.strip(), "", "", "", raw)


def build_keypad_char(key: str) -> bytes:
    """One virtual keypad key press (raw code like ``"R"`` or a name like ``"back"``)."""
    code = KEY_NAMES.get(key, key)
    if code not in VALID_KEY_CODES:
        raise ValueError(f"unknown key: {key!r}")
    return build_frame([Block(CMD_KEYPAD_CHAR, 1, 1, code.encode("ascii"))])


# --- reply parsing ----------------------------------------------------------

def _strings(data: bytes) -> list[str]:
    parts = data.split(b"\x00")
    if parts and parts[-1] == b"":
        parts = parts[:-1]
    return [p.decode("ascii", errors="replace").strip() for p in parts]


@dataclass
class LoginInfo:
    ok: bool = False
    error: int | None = None
    version: str | None = None
    panel_id: int | None = None
    serial: str | None = None
    max_zones: int = 0
    max_areas: int = 0
    max_rem_outputs: int = 0
    part_arm_mask: int = 7
    user_number: int = 0
    user_area: int = 0


PANEL_MODELS = {40: "WCP40", 110: "CP10", 120: "CP20", 140: "CP40", 160: "EP100", 200: "CP200"}


def parse_login(blocks: list[Block]) -> LoginInfo:
    info = LoginInfo()
    for b in blocks:
        if b.cmd == CMD_PASSWORD:
            info.ok = len(b.data) == 0
            if b.data:
                info.error = b.data[0]
        elif b.cmd == CMD_ERROR and b.data:
            info.error = b.data[0]
        elif b.cmd == CMD_PANEL_INFO and len(b.data) >= 6:
            info.version = f"{b.data[0]}.{b.data[1]:02d}"
            info.panel_id = b.data[5]
        elif b.cmd == CMD_PANEL_STATE and len(b.data) >= 10:
            info.user_number = struct.unpack_from("<H", b.data, 6)[0]
            if len(b.data) > 14:
                info.user_area = struct.unpack_from("<I", b.data, 8)[0]
            else:
                info.user_area = struct.unpack_from("<H", b.data, 8)[0]
        elif b.cmd == CMD_SYS_CONFIG and b.data:
            i = 0
            while i + 1 < len(b.data):
                tag, length = b.data[i], b.data[i + 1]
                val = b.data[i + 2 : i + 2 + length]
                i += 2 + length
                if tag == 90 and val:
                    info.max_zones = val[0]
                elif tag == 65 and val:
                    info.max_areas = val[0]
                elif tag == 83 and val:
                    info.serial = val.decode("ascii", errors="replace").rstrip("\x00")
                elif tag == 86 and val:
                    info.max_rem_outputs = val[0]
                    if len(val) > 2:
                        info.part_arm_mask = val[2]
    return info


@dataclass
class AreaState:
    area: int  # 1-based
    state: str  # disarmed, arming, pending, armed_away, armed_home, armed_night, armed_vacation, triggered
    part: int | None
    ready: bool
    alarm: bool
    bell: bool
    strobe: bool
    tamper: bool
    engineer: bool = False  # "Engineer Required": e.g. a confirmed alarm with engineer reset


@dataclass
class PanelSnapshot:
    sys_state: list[int] = field(default_factory=lambda: [0] * (SYS_STATE_COUNT + 1))
    zone_status: list[int] = field(default_factory=list)
    zone_flag: list[int] = field(default_factory=list)
    lcd: bytes = b""
    zone_timers: list[int] = field(default_factory=list)  # deciseconds since last change
    zone_counts: list[int] = field(default_factory=list)
    seen: set[int] = field(default_factory=set)  # query IDs present in the reply

    def sys(self, index: int, area: int) -> bool:
        return bool(self.sys_state[index] >> (area - 1) & 1)

    def area(self, area: int, part_names: tuple[str, str, str] = ("", "", "")) -> AreaState:
        part = None
        if self.sys(SYS_PART_ARMED, area):
            part = next((n for n, i in ((1, SYS_PART_1), (2, SYS_PART_2), (3, SYS_PART_3)) if self.sys(i, area)), 1)
        alarm = self.sys(SYS_ALARM, area) or self.sys(SYS_IN_ALARM, area)
        # The alarm flags stay latched after a disarm until reset. Once the area is
        # open (disarmed) it is "disarmed, alarm active", as the official app shows it.
        if alarm and not self.sys(SYS_OPEN, area):
            state = "triggered"
        elif self.sys(SYS_ARMED, area):
            state = {None: "armed_away", 1: "armed_home", 2: "armed_night", 3: "armed_vacation"}[part]
        elif self.sys(SYS_IN_EXIT, area):
            state = "arming"
        elif self.sys(SYS_IN_ENTRY, area):
            state = "pending"
        else:
            state = "disarmed"
        return AreaState(
            area=area, state=state, part=part,
            ready=self.sys(SYS_READY, area), alarm=alarm,
            bell=self.sys(SYS_BELL, area), strobe=self.sys(SYS_STROBE, area),
            tamper=self.sys(SYS_TAMPER, area),
            engineer=self.sys(SYS_CALL_ENGINEER, area),
        )


def parse_snapshot(blocks: list[Block]) -> PanelSnapshot:
    snap = PanelSnapshot()
    for b in blocks:
        if not b.data:
            continue
        snap.seen.add(b.cmd)
        if b.cmd == Q_SYS_STATE:
            for i, v in enumerate(b.data):
                idx = b.start - 1 + i
                if 0 <= idx <= SYS_STATE_COUNT:
                    snap.sys_state[idx] = v
        elif b.cmd == Q_ZONE_STATUS:
            snap.zone_status = [
                int.from_bytes(b.data[i : i + 2], "little") for i in range(0, len(b.data) - 1, 2)
            ]
        elif b.cmd == Q_ZONE_FLAG:
            # The panel may send 1 or 2 bytes per zone; derive the width from
            # the echoed count so the bitfield lines up either way.
            n = b.count or len(b.data)
            width = max(1, len(b.data) // n) if n else 1
            if width >= 2:
                snap.zone_flag = [
                    int.from_bytes(b.data[i : i + width], "little")
                    for i in range(0, len(b.data) - width + 1, width)
                ]
            else:
                snap.zone_flag = list(b.data)
        elif b.cmd == CMD_LCD:
            snap.lcd = b.data
        elif b.cmd == Q_ZONE_TIMERS:
            snap.zone_timers = [
                int.from_bytes(b.data[i : i + 4], "little") for i in range(0, len(b.data) - 3, 4)
            ]
        elif b.cmd == Q_ZONE_COUNTS:
            snap.zone_counts = [
                int.from_bytes(b.data[i : i + 2], "little") for i in range(0, len(b.data) - 1, 2)
            ]
    return snap


def parse_strings(blocks: list[Block], query: int) -> list[str]:
    for b in blocks:
        if b.cmd == query and b.data:
            return _strings(b.data)
    return []


def parse_bytes(blocks: list[Block], query: int) -> bytes:
    for b in blocks:
        if b.cmd == query:
            return b.data
    return b""


# --- wireless zone details (Q_WIRELESS) --------------------------------------
#
# Read [10810, zone, 1]: 44 bytes about the wireless device on that zone.
# All zeros for a wired zone.
#   [0] 1 when a wireless device is learnt   [2] zone   [3] 0x81
#   [4] signal (raw)   [5] second signal (two-way devices such as the W-DT-200)
#   [8:14] serial   [14] device type   [15] battery, 0.1 V
#   [16] temperature, (raw - 40) / 2 degC   [30] minutes since last polled
# Signal % = raw * 100 / 190, capped at 100 (matches the keypad).

WIRELESS_DEVICES = {
    0x15: "W-IR-200",
    0x16: "W-DT-300",
    0x1C: "W-DT-200",
    0x1F: "W-MC-C",
    0x21: "W-PA",
    0x29: "W-SD-MC-C",
    0x2A: "W-SD-C",
}


@dataclass
class WirelessInfo:
    zone: int
    signal: int  # %
    signal2: int | None  # %, two-way devices only
    serial: str
    device_type: int
    battery: float  # V
    temperature: float  # degC
    last_polled: int  # minutes

    @property
    def device(self) -> str:
        return WIRELESS_DEVICES.get(self.device_type, f"Type 0x{self.device_type:02X}")


def signal_percent(raw: int) -> int:
    return min(100, round(raw * 100 / 190))


# --- bus devices (Q_DEVICES) -------------------------------------------------
#
# Read [12510, slot, 1]: 42 bytes about one device on the panel's network.
# All zeros for an empty slot.
# Slot = address + offset: keypads 1-10, wireless expanders 11-26, external
# sounders 27-30 (seen on a CP50: keypad 1 = slot 1, expander 1 = 11,
# sounders 1 and 2 = 27 and 28; the range ends are inferred).
#   [0] 0x08 when a device is there   [9] device type
#   [10] software minor, [11] major (0x2D 0x01 = 1.45)
#   [12:14] light, u16; % = raw // 33 (keypads and sounders)
#   [14:16] temperature, raw (conversion not known yet)
#   [16:18] supply voltage, u16; V ~= raw / 270
#   [18:20] current, u16, 0.1 mA (sounders)
#   [24:26] battery voltage, u16, V ~= raw / 270 (sounders' own battery)
#   [26:28] poll errors   [28:34] CRC errors, three u16 counts
#   [34:36] Rx packets, u16 (wraps at 65535)   [38:40] maybe cable delay (not decoded)

Q_DEVICES = 12510
DEVICE_SLOTS = 30
DEVICE_KINDS = (  # (kind, first slot, last slot, name)
    ("keypad", 1, 10, "Keypad"),
    ("expander", 11, 26, "Wireless expander"),
    ("sounder", 27, 30, "External sounder"),
)
DEVICE_MODELS = {0x02: "TK-650/1250", 0x04: "External sounder", 0x07: "W-XP-R"}
DEVICE_TYPE_KINDS = {0x02: "keypad", 0x04: "sounder", 0x07: "expander"}
# The readings that apply to each kind (the others are meaningless for it).
DEVICE_READINGS = {
    "keypad": ("light", "voltage"),
    "expander": ("voltage",),
    "sounder": ("light", "voltage", "current", "battery"),
}


def device_kind(slot: int, device_type: int | None = None) -> tuple[str, int, str]:
    """(kind, address, name) for a 12510 slot.

    The kind comes from the device type byte when it's a known one, so a
    device is never named after the wrong kind even if the slot ranges
    (inferred on a CP50) turn out different; the address comes from the slot."""
    by_slot = next((k for k in DEVICE_KINDS if k[1] <= slot <= k[2]), None)
    kind = DEVICE_TYPE_KINDS.get(device_type) or (by_slot[0] if by_slot else "device")
    info = next((k for k in DEVICE_KINDS if k[0] == kind), None)
    if info is None:
        return "device", slot, f"Device {slot}"
    if by_slot is not None and by_slot[0] == kind:
        address = slot - info[1] + 1
        return kind, address, f"{info[3]} {address}"
    return kind, slot, f"{info[3]} (slot {slot})"  # type and slot range disagree


@dataclass
class DeviceInfo:
    slot: int
    device_type: int
    software: str
    light_raw: int
    temperature_raw: int
    voltage_raw: int
    current_raw: int
    battery_raw: int = 0
    poll_errors: int = 0
    crc: tuple[int, int, int] = (0, 0, 0)
    rx_packets: int = 0

    @property
    def crc_errors(self) -> int:
        return sum(self.crc)

    @property
    def kind(self) -> str:
        return device_kind(self.slot, self.device_type)[0]

    @property
    def address(self) -> int:
        return device_kind(self.slot, self.device_type)[1]

    @property
    def name(self) -> str:
        return device_kind(self.slot, self.device_type)[2]

    @property
    def model(self) -> str:
        return DEVICE_MODELS.get(self.device_type, f"Type 0x{self.device_type:02X}")

    @property
    def light(self) -> int:
        """Light level, %."""
        return min(100, self.light_raw // 33)

    @property
    def voltage(self) -> float:
        return round(self.voltage_raw / 270, 2)

    @property
    def current(self) -> float:
        """mA."""
        return self.current_raw / 10

    @property
    def battery(self) -> float:
        return round(self.battery_raw / 270, 2)


def parse_device(block: Block) -> DeviceInfo | None:
    """One network device's readings, or None for an empty slot."""
    x = block.data
    if len(x) < 20 or not x[0]:
        return None
    u16 = lambda i: x[i] | x[i + 1] << 8  # noqa: E731
    return DeviceInfo(
        slot=block.start,
        device_type=x[9],
        software=f"{x[11]}.{x[10]:02d}",
        light_raw=u16(12),
        temperature_raw=u16(14),
        voltage_raw=u16(16),
        current_raw=u16(18),
        battery_raw=u16(24) if len(x) >= 26 else 0,
        poll_errors=u16(26) if len(x) >= 28 else 0,
        crc=(u16(28), u16(30), u16(32)) if len(x) >= 34 else (0, 0, 0),
        rx_packets=u16(34) if len(x) >= 36 else 0,
    )


# --- panel power (Q_PANEL_POWER) ---------------------------------------------
#
# Read [540, 1, 30]: 30 u16 analogue readings, worked out
# with mains on and off (CP50, 10 Nov 2026):
#   [11] supply rail (panel and aux), V = raw / 270
#   [13] battery, V = raw / 270
#   [22] panel current, mA ~= raw * 0.79     [23] aux current, mA ~= raw * 0.79
#   [21] probably battery charge current (dropped from ~520 to ~120 with mains
#   off). Temperature is in here somewhere, not decoded.

Q_PANEL_POWER = 540
PANEL_POWER_COUNT = 30
CURRENT_SCALE = 0.79  # fitted to 8 reference readings (0.75-0.82; never read at the same instant)


@dataclass
class PanelPower:
    supply: float  # V
    battery: float  # V
    current: float  # mA
    aux_current: float  # mA


def parse_panel_power(blocks: list[Block]) -> PanelPower | None:
    x = parse_bytes(blocks, Q_PANEL_POWER)
    if len(x) < 48:
        return None
    v = [x[i] | x[i + 1] << 8 for i in range(0, 48, 2)]
    return PanelPower(
        supply=round(v[11] / 270, 2),
        battery=round(v[13] / 270, 2),
        current=round(v[22] * CURRENT_SCALE),
        aux_current=round(v[23] * CURRENT_SCALE),
    )


def parse_wireless(block: Block) -> WirelessInfo | None:
    """One zone's wireless details, or None for a wired / empty zone."""
    x = block.data
    if len(x) < 31 or x[0] == 0 or not any(x[8:14]):
        return None
    return WirelessInfo(
        zone=block.start,
        signal=signal_percent(x[4]),
        signal2=signal_percent(x[5]) if x[5] else None,
        serial=x[8:14].hex().upper(),
        device_type=x[14],
        battery=x[15] / 10,
        temperature=(x[16] - 40) / 2,
        last_polled=x[30],
    )


# --- LCD --------------------------------------------------------------------

# Icon glyphs (byte values 127..173) mapped to plain Unicode stand-ins.
LCD_ICONS = {
    127: "\u25c0", 131: "\u2715", 155: ":", 156: ".", 159: "\u2612", 160: "\u2610", 162: " ",
    163: "\u25b2", 164: "\u25bc", 165: "\u25b6", 166: "\u25c0", 170: "\u2713",  # 170: tick (the keypad shows \u2713, e.g. "\u2713 to confirm")
    172: "\u25bc", 173: "\u25b2",
}


# Status icons at the left of the top line. The panel sends them for a while
# after a user/engineer logs off; the chime note stays while chime is on.
# Meanings from how the panel picks each byte; drawn by the card as HA icons.
LCD_STATUS_ICONS = {
    0x8B: "mdi:power-plug", 0xF0: "mdi:power-plug-off",  # mains OK / failed
    0x8A: "mdi:battery", 0xF1: "mdi:battery-low", 0xF2: "mdi:battery-alert-variant-outline",  # battery OK / low / fault
    0x91: "mdi:signal-cellular-1", 0x92: "mdi:signal-cellular-2",  # GSM bars
    0x93: "mdi:signal-cellular-3", 0x94: "mdi:signal",
    0xF4: "mdi:signal-off",  # GSM no signal / not registered
    0xFA: "mdi:email-outline|mdi:sim-alert",  # SIM/messaging warning: the keypad alternates envelope and SIM card
    0x87: "mdi:lan", 0xF9: "mdi:lan-disconnect",  # Ethernet connected / not
    0x8E: "mdi:wifi-strength-3", 0x8F: "mdi:wifi-strength-2", 0x90: "mdi:wifi-strength-1",  # Wi-Fi strong/medium/weak
    0xF3: "mdi:wifi-strength-off-outline", 0x88: "mdi:wifi",  # Wi-Fi down / other Wi-Fi state
    0x8D: "mdi:music-note",  # chime on (confirmed: gone when chime is off)
    0x8C: "mdi:white-balance-sunny",  # sun/light, per area, shown after an alarm (keypad photo 5 Nov)
}
# Other icon bytes the panel can put on the top line, meaning not known yet.
# Shown by the card as "?" with the byte, and logged, so they can be reported.
LCD_UNKNOWN_STATUS = frozenset({0x99, 0x9A, 0xF5, 0xF6, 0xF7, 0xF8, 0xFB, 0xFC})


@dataclass
class LcdSegment:
    text: str
    x: int = 0  # cursor position in panel units (0..127)
    align: str = "left"  # left, center, right
    inverted: bool = False
    size: int = 0  # 0..3 (3 = small)
    cont: bool = False  # follows straight on from the previous segment (style change mid-text)
    icon: str = ""  # a status icon (mdi name) instead of text


@dataclass
class LcdLine:
    segments: list[LcdSegment] = field(default_factory=list)
    progress: tuple[int, int, int] | None = None  # (bar width, max, value)

    @property
    def text(self) -> str:
        return " ".join(s.text.strip() for s in self.segments if s.text.strip())


def decode_lcd(stream: bytes, time: tuple[int, int, int] | None = None) -> list[LcdLine]:
    """Decode the LCD command stream into 4 lines of positioned text segments.

    Control bytes: 0x11 next line, 0x12-0x15 select line 1-4, 0x05/0x06 normal/
    inverted, 0x08 x set cursor, 0x09 cursor home, 0x0A centre, 0x0B right,
    0x0C panel time, 0x0E w m v progress bar, 0x16-0x19 font size,
    0x07/0x10/0x1A skip one argument byte, 0x20-0x7D text, 0x7F-0xAD icons.
    """
    lines = [LcdLine() for _ in range(4)]
    # Text before any line select goes on line 1 (e.g. the "Call ARC / to reset
    # system" screen starts with 0x0A centre, no 0x12).
    cur = 0
    inverted = False
    size = 0
    seg: LcdSegment | None = None

    def flush() -> None:
        nonlocal seg
        if seg is not None and (seg.text or seg.icon) and cur >= 0:
            lines[cur].segments.append(seg)
        seg = None

    def start(x: int = 0, align: str = "left") -> None:
        nonlocal seg
        flush()
        seg = LcdSegment("", x, align, inverted, size)

    def add(ch: str) -> None:
        nonlocal seg
        if cur < 0:
            return
        if seg is None or seg.icon or seg.inverted != inverted or seg.size != size:
            # A style change mid-text (e.g. the highlighted letter in the character
            # picker) carries on where the previous text ended, not at its start.
            cont = seg is not None and bool(seg.text or seg.icon)
            x = seg.x if seg is not None else 0
            align = seg.align if seg is not None else "left"
            flush()
            seg = LcdSegment("", x, align, inverted, size, cont)
        seg.text += ch

    def add_icon(name: str) -> None:
        nonlocal seg
        if cur < 0:
            return
        cont = seg is not None and bool(seg.text or seg.icon)
        x = seg.x if seg is not None else 0
        align = seg.align if seg is not None else "left"
        flush()
        seg = LcdSegment("", x, align, inverted, size, cont, name)

    i = 0
    while i < len(stream):
        b = stream[i]
        if b == 0x11 or 0x12 <= b <= 0x15:
            flush()
            cur = min(cur + 1, 3) if b == 0x11 else b - 0x12
            lines[cur] = LcdLine()
            inverted, size = False, 0
        elif b == 0x05:
            inverted = False
        elif b == 0x06:
            inverted = True
        elif b == 0x08 and i + 1 < len(stream):
            i += 1
            start(stream[i])
        elif b == 0x09:
            start(0)
        elif b == 0x0A:
            start(seg.x if seg else 0, "center")
        elif b == 0x0B:
            start(seg.x if seg else 0, "right")
        elif b == 0x0C:
            if time is not None:
                for ch in f"{time[0]:02d}:{time[1]:02d}.{time[2]:02d}":
                    add(ch)
        elif b == 0x0E:
            if cur >= 0 and i + 3 < len(stream):
                flush()
                lines[cur].progress = (stream[i + 1], stream[i + 2] or 1, stream[i + 3])
            i += 3
        elif b in (0x07, 0x10, 0x1A):
            i += 1
        elif 0x16 <= b <= 0x19:
            size = b - 0x16
        elif b in LCD_STATUS_ICONS:
            add_icon(LCD_STATUS_ICONS[b])
        elif cur == 0 and (b in LCD_UNKNOWN_STATUS or (b >= 0x80 and b not in LCD_ICONS)):
            add_icon(f"?{b:02X}")
        elif 0x20 <= b <= 0x7D:
            add(chr(b))
        elif 0x7F <= b <= 0xAD:
            icon = LCD_ICONS.get(b, "")
            if icon:
                add(icon)
        i += 1
    flush()
    return lines


# --- event categories (for notifications) -----------------------------------
#
# The official app lets each phone pick which kinds of event it's told about.
# Every event code the panel logs, sorted into the app's categories. Codes not
# listed never notify: remote/engineer access, user housekeeping, time changes,
# the steps on the way to a set or unset (36/38/40/46 key active, 63/65/66 access
# by code/tag/remote, 71-73 Exit Started, 74 Exit Stopped, 76 Entry Started,
# 77 Entry Timeout): Arming sends only the result, "System Armed" / the part set /
# "System Disarmed", as the app. 75 Exit Failed by Zone is a Fault (set failed);
# and the follow-on lines every alarm produces (56 Alarm Active, 57 Bells Active,
# 134 Alarm Aborted, 154 Alarms Reset), which the official app doesn't send
# either (compared side by side on a real alarm, 5 Nov 2026).

NOTIFY_CATEGORIES = {
    "priority": "Priority",  # PA, duress, fire, medical, CO, confirmed alarms
    "alarms": "Alarms",
    "tampers": "Tampers",
    "faults": "Faults",
    "arming": "Arming",
    "test": "Test",
    "restore": "Restore",
    "custom": "Custom",  # timers, remote controls, keyfob buttons, camera triggers
    "chains": "Chains",  # no panel event known to belong here yet
    "zones": "Zones",  # a chosen zone going active (the app's per-zone Notify)
}

_CAT_CODES = {
    "priority": (10, 12, 14, 16, 18, 20, 44, 59, 60, 64, 141, 142, 143, 151, 164, 200, 202),
    "alarms": (0, 2, 4, 6, 8, 22, 24, 42, 50, 54, 58, 132, 133, 150),
    "tampers": (28, 48, 67, 68, 69, 108, 110, 112, 114, 135, 153, 174, 190),
    "faults": (30, 32, 34, 52, 75, 85, 86, 91, 92, 94, 96, 100, 102, 104, 106, 116, 118, 120, 137, 139, 144,
               148, 157, 159, 167, 169, 173, 175, 176, 177, 178, 179, 187, 197),
    "arming": (26, 27, 78, 79, 80, 81, 82, 83, 84, 87, 88, 89, 90, 146, 147, 155, 166, 192, 193),
    "test": (122, 123, 124, 125, 126, 127, 130, 131, 156, 161, 162, 172),
    "restore": (1, 3, 5, 7, 9, 11, 13, 15, 17, 19, 21, 23, 25, 29, 31, 33, 35, 37, 39, 41, 43, 45, 47, 49,
                51, 53, 55, 93, 95, 101, 103, 105, 107, 109, 111, 113, 115, 117, 119, 121, 136, 138, 140,
                145, 149, 152, 158, 160, 165, 168, 170, 180, 181, 182, 183, 184, 185, 188, 198, 201, 203),
    "custom": (128, 129, 194, 195, 196, 199),
}
EVENT_CATEGORY: dict[int, str] = {c: cat for cat, codes in _CAT_CODES.items() for c in codes}

_AREA_LIST = re.compile(r"\s(\d(?:,\d)*)$")


def event_category(code: int | None) -> str | None:
    """The app's notification category for an event code, or None (never notify)."""
    return EVENT_CATEGORY.get(code) if code is not None else None


def event_areas(ev: Event, area_names: dict[int, str]) -> list[int]:
    """Areas an event belongs to: by the area name after '|', or the trailing
    area numbers of a multi-area entry ("... Alex 1,2"). Empty = panel-wide."""
    if ev.area:
        hit = [n for n, name in area_names.items() if name and name == ev.area]
        if hit:
            return hit
    m = _AREA_LIST.search(ev.text)
    if m:
        return [int(x) for x in m.group(1).split(",")]
    return []


def same_event(a: Event | None, b: Event | None) -> bool:
    return a is not None and b is not None and a.raw == b.raw and a.code == b.code
