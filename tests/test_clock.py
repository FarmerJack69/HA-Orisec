"""Panel clock: the write frames match the reference frames for setting a CP50's
time and date (17:59:22 on 09/11/26), and the clock parses back."""

from datetime import datetime

import pytest

from orisec_protocol import (
    CLOCK_PARAMS,
    Block,
    build_clock_writes,
    build_frame,
    is_write_ack,
    parse_clock,
    parse_frame,
)

EXPECTED_WRITES = [
    "0d009623010001000100113e06",
    "0d00a0230100010001003b9704",
    "0d00aa2301000100010016f928",
    "0d00b4230100010001000904ff",
    "0d00be230100010001000be706",
    "0d00c8230100010001001a3a52",
]
EXPECTED_ACKS = [
    "0c009623010001000000d042",
    "0c00a023010001000000ebfb",
    "0c00aa23010001000000a2b7",
    "0c00be23010001000000433a",
    "0c00c823010001000000fcc3",
]


def test_writes_match_reference():
    frames = build_clock_writes(datetime(2026, 11, 9, 17, 59, 22))
    assert [f.hex() for f in frames] == EXPECTED_WRITES


def test_acks():
    for h in EXPECTED_ACKS:
        blocks = parse_frame(bytes.fromhex(h))
        assert is_write_ack(blocks, blocks[0].cmd)
    assert not is_write_ack([Block(9110, 1, 1, b"\x11")], 9110)
    assert not is_write_ack([Block(9120, 1, 1, b"")], 9110)


def test_parse_clock():
    vals = (18, 8, 28, 9, 11, 26)  # read from the panel 9 Nov 18:08
    blocks = parse_frame(build_frame([Block(q, 1, 1, bytes([v])) for q, v in zip(CLOCK_PARAMS, vals, strict=True)]))
    assert parse_clock(blocks) == datetime(2026, 11, 9, 18, 8, 28)
    assert parse_clock(blocks[:5]) is None
    bad = [Block(q, 1, 1, bytes([v])) for q, v in zip(CLOCK_PARAMS, (25, 0, 0, 1, 1, 26), strict=True)]
    assert parse_clock(bad) is None


def test_year_range():
    with pytest.raises(ValueError):
        build_clock_writes(datetime(2100, 1, 1))


def test_zone_timers_and_counts():
    """Read on the CP50 9 Nov 18:16; expected Front Door 0:36.47, Bedroom 5:50.30,
    keyswitch 12:17.20, Doorbell 2 23:18.45 and counts 91, 485, 1236, 488, 145, 13, 3, 280."""
    from orisec_protocol import Q_ZONE_COUNTS, Q_ZONE_TIMERS, parse_snapshot

    timers = bytes.fromhex(
        "e7560000de1d000022000000641300002c3603007e0000006aa15b006aa15b00"
        "d6c006006aa15b006aa15b006aa15b00022d27000a210000"
    )
    counts = bytes.fromhex("5b00e501d5040000e8019100000000000d0000000000000003001801")
    snap = parse_snapshot(parse_frame(build_frame([
        Block(Q_ZONE_TIMERS, 1, 14, timers), Block(Q_ZONE_COUNTS, 1, 14, counts),
    ])))
    assert len(snap.zone_timers) == 14 and len(snap.zone_counts) == 14
    assert snap.zone_timers[0] == 22247  # 37 min 4.7 s
    assert snap.zone_timers[4] // 10 == 5 * 3600 + 50 * 60 + 47  # Bedroom PIR
    assert snap.zone_timers[8] // 10 == 12 * 3600 + 17 * 60 + 38  # keyswitch
    assert snap.zone_timers[12] // 10 == 2 * 86400 + 23 * 3600 + 19 * 60 + 2  # Doorbell
    assert snap.zone_counts == [91, 485, 1237, 0, 488, 145, 0, 0, 13, 0, 0, 0, 3, 280]


def test_wireless():
    """10810 replies read on the CP50 9 Nov; expected Front Door W-MC-C 4716002B0050,
    2.8 V, 61 %; Hall DT W-DT-200 with two signals; wired zones are all zeros."""
    from orisec_protocol import Q_WIRELESS, parse_wireless

    front = bytes.fromhex("012801817300e4344716002b00501f1c440500000000000000000000000011000001000a0000000000150000")
    hall = bytes.fromhex("01280e81919440a3470a3b0070001c1e470800000000000000000000000004000007000a0000000000151c00")
    wired = bytes(41) + bytes.fromhex("150000")
    w = parse_wireless(Block(Q_WIRELESS, 1, 1, front))
    assert (w.zone, w.signal, w.signal2, w.serial, w.device, w.battery, w.temperature, w.last_polled) == (
        1, 61, None, "4716002B0050", "W-MC-C", 2.8, 14.0, 17)
    h = parse_wireless(Block(Q_WIRELESS, 14, 1, hall))
    assert (h.signal, h.signal2, h.device, h.battery, h.temperature) == (76, 78, "W-DT-200", 3.0, 15.5)
    assert parse_wireless(Block(Q_WIRELESS, 9, 1, wired)) is None
    for raw, pct in ((0xC5, 100), (0xBD, 99), (0x8B, 73), (0x7A, 64), (0xA0, 84), (0x97, 79), (0x8D, 74), (0xA2, 85), (0x88, 72)):
        from orisec_protocol import signal_percent
        assert signal_percent(raw) == pct
