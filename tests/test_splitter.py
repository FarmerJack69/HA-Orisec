"""Stream framing tests for the USB and serial-bridge links."""

import asyncio
import importlib.util
import pathlib
import sys
import types

from orisec_protocol import Q_SYS_STATE, build_query

_dir = pathlib.Path(__file__).parents[1] / "custom_components" / "orisec"
_pkg = types.ModuleType("orisec_pkg")
_pkg.__path__ = [str(_dir)]
sys.modules["orisec_pkg"] = _pkg
for _name in ("const", "protocol", "client"):
    _spec = importlib.util.spec_from_file_location(f"orisec_pkg.{_name}", _dir / f"{_name}.py")
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[f"orisec_pkg.{_name}"] = _mod
    _spec.loader.exec_module(_mod)
_FrameSplitter = sys.modules["orisec_pkg.client"]._FrameSplitter

FRAME = build_query([(Q_SYS_STATE, 1, 65)])


def _run(chunks):
    q: asyncio.Queue = asyncio.Queue()
    s = _FrameSplitter(q)
    for c in chunks:
        s.feed(c)
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def test_whole_frame():
    assert _run([FRAME]) == [FRAME]


def test_byte_by_byte():
    assert _run([bytes([b]) for b in FRAME]) == [FRAME]


def test_two_frames_in_one_read():
    assert _run([FRAME + FRAME]) == [FRAME, FRAME]


def test_junk_before_frame():
    assert _run([b"\x00\xff\x13" + FRAME]) == [FRAME]


def test_junk_that_looks_like_a_long_length():
    # 0x0c13 claims a 3091 byte frame; the real frame must still come through.
    assert _run([b"\x13\x0c\x00", FRAME[:5], FRAME[5:]]) == [FRAME]


def test_corrupt_frame_is_dropped():
    bad = bytearray(FRAME)
    bad[-1] ^= 0xFF
    assert _run([bytes(bad) + FRAME]) == [FRAME]


def test_reset_discards_partial():
    q: asyncio.Queue = asyncio.Queue()
    s = _FrameSplitter(q)
    s.feed(FRAME[:6])
    s.reset()
    s.feed(FRAME)
    assert q.qsize() == 1
