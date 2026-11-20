"""Protocol tests built from real CP50 frames.

Key frames are single key presses, so they contain no codes.
Status vectors are system-state blocks from real arm/disarm cycles.
"""

import struct

import pytest

from orisec_protocol import (
    ARM_PART_1,
    CMD_KEYPAD_CHAR,
    decode_lcd,
    CMD_LCD,
    DISARM,
    PANIC,
    Q_SYS_STATE,
    Q_ZONE_STATUS,
    Block,
    FrameError,
    build_arm_key,
    build_frame,
    build_keypad_char,
    build_login,
    build_query,
    parse_frame,
    parse_login,
    parse_snapshot,
    parse_strings,
)

# key -> exact frame for a single key press (shown unwrapped)
KEY_FRAMES = {
    "0": "0d000a0001000100010030b7a5",
    "1": "0d000a000100010001003196b5",
    "2": "0d000a0001000100010032f585",
    "3": "0d000a0001000100010033d495",
    "4": "0d000a000100010001003433e5",
    "5": "0d000a000100010001003512f5",
    "6": "0d000a000100010001003671c5",
    "7": "0d000a000100010001003750d5",
    "8": "0d000a0001000100010038bf24",
    "9": "0d000a00010001000100399e34",
    "A": "0d000a000100010001004101cb",
    "F": "0d000a0001000100010046e6bb",
    "N": "0d000a000100010001004eee3a",
    "O": "0d000a000100010001004fcf2a",
    "P": "0d000a000100010001005011c9",
    "R": "0d000a000100010001005253e9",
    "Y": "0d000a00010001000100593858",
    "d": "0d000a0001000100010064c6bf",
    "l": "0d000a000100010001006cce3e",
    "r": "0d000a000100010001007231cd",
    "u": "0d000a0001000100010075d6bd",
}

# Reference polls, unwrapped (plain frames).
REF_STATUS_POLL = "1400090001000100000050280300280000008e6c"
REF_ZONE_POLL = "24007e27010032000000882701003200000092270100320000009c27010019000000fd5a"


@pytest.mark.parametrize("key,frame_hex", KEY_FRAMES.items())
def test_key_frame_matches_byte_for_byte(key, frame_hex):
    assert build_keypad_char(key).hex() == frame_hex


@pytest.mark.parametrize("key,frame_hex", KEY_FRAMES.items())
def test_key_frame_parses_with_valid_crc(key, frame_hex):
    (block,) = parse_frame(bytes.fromhex(frame_hex))
    assert block.cmd == CMD_KEYPAD_CHAR
    assert block.data == key.encode()


def test_reference_polls_rebuild_exactly():
    assert build_query([(CMD_LCD, 1, 1), (Q_SYS_STATE, 3, 40)]).hex() == REF_STATUS_POLL
    assert build_query(
        [(Q_ZONE_STATUS, 1, 50), (10120, 1, 50), (10130, 1, 50), (10140, 1, 25)]
    ).hex() == REF_ZONE_POLL


@pytest.mark.parametrize("name,code", [("back", "R"), ("enter", "Y"), ("clear", "N"), ("up", "u"), ("chime", "C")])
def test_friendly_key_names(name, code):
    assert build_keypad_char(name) == build_keypad_char(code)


def test_unknown_key_rejected():
    with pytest.raises(ValueError):
        build_keypad_char("panic")


def test_bad_crc_rejected():
    frame = bytearray(build_keypad_char("1"))
    frame[-1] ^= 0xFF
    with pytest.raises(FrameError):
        parse_frame(bytes(frame))


def test_login_frame():
    blocks = parse_frame(build_login("1234"))
    assert [b.cmd for b in blocks] == [1, 2, 3, 20]
    assert blocks[0].data == b"1234"
    with pytest.raises(ValueError):
        build_login("12a4")


def test_login_reply():
    sys_config = bytes([90, 1, 50, 65, 1, 5, 83, 4]) + b"TEST" + bytes([86, 3, 20, 0, 3])
    panel_state = bytes(6) + struct.pack("<H", 1) + struct.pack("<H", 0x0001) + bytes(2)
    reply = build_frame([
        Block(1, 1, 1, b""),
        Block(2, 1, 1, bytes([9, 10, 0, 0, 0, 140])),
        Block(3, 1, 1, panel_state),
        Block(20, 1, 1, sys_config),
    ])
    info = parse_login(parse_frame(reply))
    assert info.ok
    assert (info.version, info.max_zones, info.max_areas, info.serial) == ("9.10", 50, 5, "TEST")
    assert info.user_area == 1 and info.part_arm_mask == 3


def test_login_rejected():
    info = parse_login(parse_frame(build_frame([Block(1, 1, 1, bytes([3]))])))
    assert not info.ok and info.error == 3


def test_arm_key_frames():
    (b,) = parse_frame(build_arm_key(ARM_PART_1, 0x01))
    assert (b.cmd, b.data) == (11, bytes([1, 1, 0, 0]))
    (b,) = parse_frame(build_arm_key(DISARM, 0x1F))
    assert b.data == bytes([4, 0x1F, 0, 0])
    with pytest.raises(ValueError):
        build_arm_key(PANIC, 1)


# System-state entries 3..42 read from the panel (polled with start=3, count=40).
SYS_VECTORS = {
    "disarmed": ("001f000000000000000000000000000000001f0000000000000000000000000000001f0000000000", "disarmed"),
    # Read after disarm with the alarm still latched: area open, so disarmed (alarm active).
    "alarm": ("001f000000000001010000000000000000001f0000000000000000000000000000001f0000000000", "disarmed"),
    "exit_time": ("001f000000000000000000000000000000001e0000000000000000000000000000001f0000000100", "arming"),
    "full_armed": ("001e000100010000000000000000000000001e0000000000000000000000000000001f0100000001", "armed_away"),
    "part_armed": ("001e000101010000000000000000000000001f0000000000010000000000000000001f0100000000", "armed_home"),
}


def _snapshot(sys_hex: str, start: int = 3):
    data = bytes.fromhex(sys_hex)
    return parse_snapshot(parse_frame(build_frame([Block(Q_SYS_STATE, start, len(data), data)])))


@pytest.mark.parametrize("name", SYS_VECTORS)
def test_area_state_from_real_sys_state(name):
    hexstr, expected = SYS_VECTORS[name]
    snap = _snapshot(hexstr)
    assert snap.area(1).state == expected
    for a in range(2, 6):
        assert snap.area(a).state == "disarmed"


def test_alarm_latched_after_disarm():
    a = _snapshot(SYS_VECTORS["alarm"][0]).area(1)
    assert (a.state, a.alarm) == ("disarmed", True)


def test_alarm_while_armed_is_triggered():
    data = bytearray(65)
    for idx in (5, 10, 43):  # armed, alarm, in alarm; area not open
        data[idx] = 0x01
    a = _snapshot(data.hex(), start=1).area(1)
    assert (a.state, a.alarm) == ("triggered", True)


def test_ready_flag():
    assert _snapshot(SYS_VECTORS["disarmed"][0]).area(1).ready
    assert not _snapshot(SYS_VECTORS["exit_time"][0]).area(1).ready


def test_part_set_identified():
    data = bytearray(65)
    for idx in (5, 6, 62):  # armed, part armed, part 2
        data[idx] = 0x01  # start=1 puts data[i] at position i
    snap = _snapshot(data.hex(), start=1)
    a = snap.area(1)
    assert (a.state, a.part) == ("armed_night", 2)


def test_zone_status_and_flag():
    status = b"".join(v.to_bytes(2, "little") for v in [0x0801, 0x0000, 0x0002, 0x0001])
    # zone 4 is programmed to omit in Part Arm 1 (bit 0); zone 1 in Part Arm 1+2.
    snap = parse_snapshot(parse_frame(build_frame([
        Block(Q_ZONE_STATUS, 1, 4, status),
        Block(1150, 1, 4, bytes([0x03, 0, 0, 0x01])),
    ])))
    assert snap.zone_status == [0x0801, 0, 0x0002, 0x0001]
    assert snap.zone_flag == [0x03, 0, 0, 0x01]


def test_zone_flag_u16_width():
    # Two bytes per zone: bedroom (zone 4 here) = Omit PS1 (0x01) + manual (0x08).
    data = b"".join(v.to_bytes(2, "little") for v in [0x0000, 0x0000, 0x0000, 0x0009])
    snap = parse_snapshot(parse_frame(build_frame([
        Block(1150, 1, 4, data),
    ])))
    assert snap.zone_flag == [0, 0, 0, 0x09]
    from orisec_protocol import part_arm_omits
    assert part_arm_omits(snap.zone_flag[3]) == [1]


def test_part_arm_omits():
    from orisec_protocol import part_arm_omits
    assert part_arm_omits(0x00) == []
    assert part_arm_omits(0x01) == [1]
    assert part_arm_omits(0x03) == [1, 2]
    assert part_arm_omits(0x07) == [1, 2, 3]
    # High attribute bits must not leak into the part list.
    assert part_arm_omits(0x08 | 0x40) == []


def test_zone_type_names_9_29():
    from orisec_protocol import ZONE_TYPE_NAMES, ZONE_TYPES_KEYSWITCH
    assert ZONE_TYPE_NAMES[18] == "Arm/Disarm Key"
    assert ZONE_TYPE_NAMES[31] == "Disarm Only Key"
    assert ZONE_TYPE_NAMES[35] == "Arm Only Key"
    assert ZONE_TYPE_NAMES[36] == "Fire Alarm Log"
    assert 35 in ZONE_TYPES_KEYSWITCH


def test_zone_names():
    blocks = parse_frame(build_frame([Block(1120, 1, 3, b"Front Door DC\x00Lounge PIR\x00\x00")]))
    assert parse_strings(blocks, 1120) == ["Front Door DC", "Lounge PIR", ""]


def test_lcd_lines():
    stream = (
        bytes.fromhex("128d084719") + b"02 Nov" + bytes.fromhex("160b0c090911170a")
        + b"Enter Passcode:" + bytes.fromhex("0911020a") + b"***" + bytes.fromhex("09")
    )
    assert [ln.text for ln in decode_lcd(stream) if ln.text] == ["02 Nov", "Enter Passcode:", "***"]


def test_lcd_layout_home_screen():
    from orisec_protocol import decode_lcd

    stream = (
        bytes.fromhex("128d084719") + b"02 Nov" + bytes.fromhex("160b0c0913")
        + bytes.fromhex("0a0914170a") + b"Test Site" + bytes.fromhex("091509")
    )
    lines = decode_lcd(stream, time=(18, 32, 52))
    assert lines[0].text == "02 Nov 18:32.52"
    right = [s for s in lines[0].segments if s.align == "right"][0]
    assert right.text == "18:32.52" and right.clock
    assert not any(seg.clock for seg in decode_lcd(stream)[0].segments)  # no time given: no clock
    small = [s for s in lines[0].segments if s.text == "02 Nov"][0]
    assert (small.x, small.size) == (0x47, 3)
    assert lines[2].text == "Test Site" and lines[2].segments[0].align == "center"


def test_zone_omitted_bit():
    from orisec_protocol import ZONE_ACTIVE, ZONE_OMITTED

    # Read from the panel after omitting the lounge and kitchen PIRs.
    snap = parse_snapshot(parse_frame(build_frame([
        Block(Q_ZONE_STATUS, 1, 3, bytes.fromhex("000040004000")),
    ])))
    assert [bool(w & ZONE_OMITTED) for w in snap.zone_status] == [False, True, True]
    assert not any(w & ZONE_ACTIVE for w in snap.zone_status)


import orisec_protocol as _pm  # noqa: E402

def test_build_omit_zone_layout():
    frame = _pm.build_omit_zone(5, True)
    blocks = _pm.parse_frame(frame)
    assert len(blocks) == 1
    b = blocks[0]
    assert (b.cmd, b.start, b.count) == (_pm.CMD_CONTROL_OUTPUT, 1, 1)
    assert b.data == bytes([5, 0, 1, 3])


def test_build_reinstate_zone_layout():
    b = _pm.parse_frame(_pm.build_omit_zone(38, False))[0]
    assert b.data == bytes([38, 0, 0, 3])


def test_build_omit_zone_rejects_bad_zone():
    for z in (0, -1, 251):
        with pytest.raises(ValueError):
            _pm.build_omit_zone(z, True)


def test_build_omit_zone_only_uses_omit_op():
    # Other ops of this command drive outputs; the builder must never produce them.
    for z in (1, 50):
        for omit in (True, False):
            assert _pm.parse_frame(_pm.build_omit_zone(z, omit))[0].data[3] == _pm.CONTROL_OP_OMIT



def _ev(text: str, code: int) -> bytes:
    return text.encode("latin-1") + b"\r" + bytes([code])


def test_parse_event_remote_access():
    e = _pm.parse_event(1, _ev("Remote Access 1 Alex |Home Wed 04/11/26 - 23:39.07", 0x3D))
    assert (e.index, e.code, e.text, e.area) == (1, 61, "Remote Access 1 Alex", "Home")
    assert (e.day, e.date, e.time) == ("Wed", "04/11/26", "23:39.07")


def test_parse_event_zone_omitted():
    e = _pm.parse_event(3, _ev("Zone 2 Omitted Lounge PIR |Home Wed 04/11/26 - 23:23.41", 0x1A))
    assert (e.code, e.text, e.area, e.time) == (26, "Zone 2 Omitted Lounge PIR", "Home", "23:23.41")


def test_parse_event_empty_slot():
    assert _pm.parse_event(1280, b"") is None
    assert _pm.parse_event(5, b"No Event Stored\r\x00") is None


def test_parse_event_unexpected_format_keeps_text():
    e = _pm.parse_event(1, b"Something odd")
    assert e.text == "Something odd" and e.code is None


def test_parse_event_several_areas_no_bar():
    e = _pm.parse_event(1, _ev("Remote Access 1 Alex 1,2 Thu 05/11/26 - 07:46.25", 0x3D))
    assert (e.text, e.area, e.day, e.date, e.time) == ("Remote Access 1 Alex 1,2", "", "Thu", "05/11/26", "07:46.25")


def test_lcd_style_change_mid_text_continues():
    # Character picker: "56789" then the highlighted " A " then "BCDE" on one line.
    from orisec_protocol import decode_lcd
    stream = bytes([0x13]) + b"56789" + bytes([0x06]) + b" A " + bytes([0x05]) + b"BCDE"
    segs = decode_lcd(stream)[1].segments
    assert [(s.text, s.inverted, s.cont) for s in segs] == [("56789", False, False), (" A ", True, True), ("BCDE", False, True)]


def test_lcd_positioned_segment_is_not_a_continuation():
    from orisec_protocol import decode_lcd
    stream = bytes([0x12]) + b"Zone" + bytes([0x08, 100]) + b"4"
    segs = decode_lcd(stream)[0].segments
    assert [(s.text, s.x, s.cont) for s in segs] == [("Zone", 0, False), ("4", 100, False)]


def test_lcd_tick_icon():
    from orisec_protocol import decode_lcd
    assert decode_lcd(bytes([0x12, 170]) + b" to confirm")[0].text == "✓ to confirm"


def test_lcd_status_icons():
    # Top line after a logoff: mains OK, battery OK, GSM no signal, SIM warning, Ethernet, chime, then the date.
    stream = bytes.fromhex(
        "128b8af4fa878d0847193035204e6f76160b0c09130a0914170a"
        "486f6d6509150900"
    )
    from orisec_protocol import decode_lcd
    lines = decode_lcd(stream, (17, 29, 5))
    top = lines[0].segments
    icons = [s.icon for s in top if s.icon]
    assert icons == ["mdi:power-plug", "mdi:battery", "mdi:signal-off", "mdi:email-outline|mdi:sim-alert", "mdi:lan", "mdi:music-note"]
    assert top[0].x == 0 and not top[0].cont and all(s.cont for s in top[1:6])
    assert top[6].text == "05 Nov" and top[6].x == 71 and not top[6].cont
    # Icons don't leak into the text (the sensor state).
    assert lines[0].text.startswith("05 Nov")
    assert lines[2].text == "Home"


def test_lcd_chime_only():
    stream = bytes.fromhex("128d0847193035204e6f76")
    from orisec_protocol import decode_lcd
    top = decode_lcd(stream)[0].segments
    assert top[0].icon == "mdi:music-note" and top[1].text == "05 Nov"


def test_lcd_status_wifi_and_unknown():
    from orisec_protocol import decode_lcd
    # mains fail, battery low, Wi-Fi medium, an unknown byte 0xF6, chime; then the date
    stream = bytes([0x12, 0xF0, 0xF1, 0x8F, 0xF6, 0x8D, 0x08, 0x47]) + b"05 Nov"
    top = decode_lcd(stream)[0].segments
    assert [s.icon for s in top[:5]] == [
        "mdi:power-plug-off", "mdi:battery-low", "mdi:wifi-strength-2", "?F6", "mdi:music-note",
    ]
    assert top[5].text == "05 Nov"


def test_lcd_unknown_only_on_top_line():
    from orisec_protocol import decode_lcd
    # An unmapped high byte on another line is still just dropped, not shown as "?".
    lines = decode_lcd(bytes([0x13, 0x41, 0xF6, 0x42]))
    assert all(not s.icon for s in lines[1].segments)


def test_engineer_required_flag():
    from orisec_protocol import PanelSnapshot, SYS_CALL_ENGINEER, SYS_OPEN
    snap = PanelSnapshot()
    snap.sys_state[SYS_OPEN] = 0x1F
    snap.sys_state[SYS_CALL_ENGINEER] = 0x01  # area 1, as seen after a confirmed alarm (5 Nov)
    assert snap.area(1).engineer and not snap.area(2).engineer


def test_lcd_reset_screen():
    from orisec_protocol import decode_lcd
    # As built by the panel after a confirmed alarm with engineer reset (5 Nov):
    stream = (b"\x0aCall ARC\x09\x13\x0ato reset system\x09"
              b"\x15\x06 \xaa=Exit\x0b\x83=User Menu \x09\x00")
    lines = decode_lcd(stream)
    assert lines[0].text == "Call ARC"
    assert lines[1].text == "to reset system"
    assert "✓=Exit" in lines[3].text and "✕=User Menu" in lines[3].text


def test_lcd_clock_mid_text_keeps_following_text():
    from orisec_protocol import decode_lcd

    stream = bytes([0x12]) + b"At " + bytes([0x0C]) + b" ok"
    segs = decode_lcd(stream, time=(9, 5, 7))[0].segments
    assert [(s.text, s.clock, s.cont) for s in segs] == [("At ", False, False), ("09:05.07", True, True), (" ok", False, True)]
