"""Network devices (12510): replies read on a CP50 on 10 Nov 2026, compared with
what the panel reported (keypad 1.45, expander 2.08, sounders 2.34 / 3.00)."""

from orisec_protocol import Q_DEVICES, Block, device_kind, parse_device

KEYPAD = "080000003c00000000022d015401e007500d000000000000000000000a00030003002a830000e3040000"
EXPANDER = "08000c003c0000000007080200001402400e00000000000000000000010000000000d68aff0006050000"
SOUNDER_1 = "080000803c00000000042202a0006006080da409000000001c070100000000000000a99500004a050000"
SOUNDER_2 = "080000803c0000000004000348005806fc0ca0090000000018070100030001000100039d0000a1040000"


def dev(slot, hexdata):
    return parse_device(Block(Q_DEVICES, slot, 1, bytes.fromhex(hexdata)))


def test_keypad():
    d = dev(1, KEYPAD)
    assert (d.kind, d.address, d.name, d.model, d.software) == ("keypad", 1, "TK-1250 1,1", "TK-1250", "1.45")
    assert (d.light_raw, d.light) == (340, 10)  # hall light had been on: 10 % expected
    assert d.voltage_raw == 3408 and 12.5 < d.voltage < 12.7
    assert d.current == 0


def test_expander():
    d = dev(11, EXPANDER)
    assert (d.kind, d.address, d.name, d.model, d.software) == ("expander", 1, "W-XP-R 1,1", "W-XP-R", "2.08")
    assert d.wireless
    assert d.voltage == round(3648 / 270, 2) == 13.51  # expected 13.50 V


def test_sounders():
    a, b = dev(27, SOUNDER_1), dev(28, SOUNDER_2)
    assert (a.name, a.software, b.name, b.software) == ("Bell 1,1", "2.34", "Bell 1,2", "3.00")
    assert (a.kind, a.model, a.wireless) == ("sounder", "Bell", False)
    assert (a.light_raw, a.light, b.light_raw, b.light) == (160, 4, 72, 2)
    assert a.current == 246.8 and b.current == 246.4  # expected 246 mA


def test_light_percent():
    # Raw values read on 10 Nov against the expected %.
    for raw, pct in ((84, 2), (340, 10), (64, 1), (132, 4), (176, 5)):
        assert dev(1, KEYPAD[:24] + f"{raw & 0xFF:02x}{raw >> 8:02x}" + KEYPAD[28:]).light == pct


def test_empty_and_slots():
    assert parse_device(Block(Q_DEVICES, 5, 1, bytes(42))) is None
    assert parse_device(Block(Q_DEVICES, 2, 1, bytes(3) + b"\x80" + bytes(38))) is None
    assert device_kind(10) == ("keypad", 10, "Keypad 1,10")
    assert device_kind(26) == ("expander", 16, "Expander 1,16")
    assert device_kind(30) == ("sounder", 4, "Sounder 1,4")
    # The type byte wins over the slot range; the address still comes from the slot.
    assert device_kind(1, 0x02) == ("keypad", 1, "TK-1250 1,1")
    assert device_kind(12, 0x02) == ("keypad", 12, "TK-1250 (slot 12)")
    assert device_kind(5, 0x99) == ("keypad", 5, "Keypad 1,5")  # unknown type: slot range decides
    assert device_kind(40) == ("device", 40, "Device (slot 40)")


def test_models():
    """A CP60 with RK-720 keypads and wired ZX-10 expanders (20 Nov 2026)."""
    assert device_kind(1, 0x10) == ("keypad", 1, "RK-720/920 1,1")
    assert device_kind(2, 0x10) == ("keypad", 2, "RK-720/920 1,2")
    assert device_kind(11, 0x03) == ("expander", 1, "ZX-10 1,1")
    assert device_kind(12, 0x05) == ("expander", 2, "PZX-10 1,2")
    assert device_kind(13, 0x0E) == ("expander", 3, "ROX-10 1,3")
    assert device_kind(14, 0x11) == ("expander", 4, "W-XP2 1,4")
    assert device_kind(27, 0x0D) == ("sounder", 1, "INT-CS 1,1")
    assert device_kind(3, 0x08) == ("keypad", 3, "Int.Prox 1,3")  # kind not known: slot decides
    zx = dev(11, EXPANDER[:18] + "03" + EXPANDER[20:])
    assert (zx.name, zx.model, zx.wireless) == ("ZX-10 1,1", "ZX-10", False)
    rk = dev(1, KEYPAD[:18] + "10" + KEYPAD[20:])
    assert (rk.kind, rk.name, rk.model, rk.wireless) == ("keypad", "RK-720/920 1,1", "RK-720/920", False)
    assert dev(1, KEYPAD[:18] + "99" + KEYPAD[20:]).model == "Type 0x99"


def test_sounder_battery():
    assert dev(27, SOUNDER_1).battery == 6.74 and dev(28, SOUNDER_2).battery == 6.73  # expected 6.75 / 6.73


def test_panel_power():
    """540 read with mains on and off, 10 Nov; expected (mains off) 12.08 V,
    battery 12.51 V, 445 mA, aux 132 mA, and (mains back) 13.47 V, 13.15 V."""
    from orisec_protocol import Q_PANEL_POWER, parse_panel_power

    off = bytes.fromhex("fe0f0e00fe0f0700fe0f0700fe0f0600fe0f0000fe0fb80cfe0f2b0dfe0fda0ffe0ffe0ffe0ffe0f640076003a02a700550a00000000000000000000")
    on = bytes.fromhex("fe0f0e00fe0f0700fe0f0800fe0f0600fe0f0000fe0f320efe0fd70dfe0fdf0ffe0ffe0ffe0ffe0f2b0024028703c900540a00000000000000000000")
    a = parse_panel_power([Block(Q_PANEL_POWER, 1, 30, off)])
    assert (a.supply, a.battery) == (12.06, 12.49)
    assert abs(a.current - 445) < 15 and abs(a.aux_current - 132) < 5
    b = parse_panel_power([Block(Q_PANEL_POWER, 1, 30, on)])
    assert (b.supply, b.battery) == (13.46, 13.12)
    assert parse_panel_power([]) is None


def test_comms_counts():
    """Comms counts (18:36): poll errors 0/0/1/1, CRC errors 10,3,3 / 1,0,0 / 0,0,0 / 3,1,1."""
    k, e, a, b = dev(1, KEYPAD), dev(11, EXPANDER), dev(27, SOUNDER_1), dev(28, SOUNDER_2)
    assert [d.poll_errors for d in (k, e, a, b)] == [0, 0, 1, 1]
    assert [d.crc for d in (k, e, a, b)] == [(10, 3, 3), (1, 0, 0), (0, 0, 0), (3, 1, 1)]
    assert k.crc_errors == 16 and k.rx_packets == 33578
