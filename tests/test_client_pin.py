"""OrisecClient.as_user: act as another panel user, then switch back (no Home Assistant needed)."""

import asyncio
import importlib.util
import pathlib
import sys
import types

import pytest

_DIR = pathlib.Path(__file__).parents[1] / "custom_components" / "orisec"
_PKG = "orisec_client_pkg"


def _load():
    pkg = types.ModuleType(_PKG)
    pkg.__path__ = [str(_DIR)]
    sys.modules[_PKG] = pkg
    mods = {}
    for name in ("protocol", "const", "client"):
        spec = importlib.util.spec_from_file_location(f"{_PKG}.{name}", _DIR / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"{_PKG}.{name}"] = mod
        spec.loader.exec_module(mod)
        mods[name] = mod
    return mods


M = _load()
p, client = M["protocol"], M["client"]

CONN_CODE = "1111"   # placeholders, never real codes
OTHER_CODE = "2222"


def login_reply(ok: bool, error: int | None = None, user: int = 1) -> list:
    pw = p.Block(p.CMD_PASSWORD, 1, 1, b"" if ok else bytes([error or 3]))
    state = p.Block(p.CMD_PANEL_STATE, 1, 1, bytes(6) + user.to_bytes(2, "little") + (1).to_bytes(2, "little"))
    return [pw, state]


class Fake(client.OrisecClient):
    """Answers frames from a script instead of a transport."""

    def __init__(self, replies):
        super().__init__(CONN_CODE, transport="tcp", host="h", port=1)
        self.sent: list[bytes] = []
        self._replies = list(replies)

    async def _request(self, frame):
        self.sent.append(frame)
        return self._replies.pop(0)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_as_user_logs_in_sends_and_switches_back():
    arm = p.build_arm_key(p.DISARM, 1)
    c = Fake([login_reply(True, user=2), [p.Block(p.CMD_ARM_KEY, 1, 1)], login_reply(True, user=1)])
    run(c.as_user(OTHER_CODE, arm))
    assert c.sent == [p.build_login(OTHER_CODE), arm, p.build_login(CONN_CODE)]
    assert c.login_info.user_number == 1
    assert not c.relogin_needed


@pytest.mark.parametrize("err", [3, 4])
def test_refused_pin_sends_nothing_and_still_switches_back(err):
    arm = p.build_arm_key(p.ARM_FULL, 1)
    c = Fake([login_reply(False, error=err), login_reply(True)])
    with pytest.raises(client.OrisecPinError):
        run(c.as_user(OTHER_CODE, arm))
    assert arm not in c.sent
    assert c.sent[-1] == p.build_login(CONN_CODE)


def test_failed_switch_back_flags_relogin():
    c = Fake([login_reply(True, user=2), [p.Block(p.CMD_ARM_KEY, 1, 1)], None])
    run(c.as_user(OTHER_CODE, p.build_arm_key(p.DISARM, 1)))
    assert c.relogin_needed


def test_error_block_reply_raises():
    c = Fake([login_reply(True, user=2), [p.Block(p.CMD_ERROR, 1, 1, bytes([6]))], login_reply(True)])
    with pytest.raises(client.OrisecError, match="refused the command"):
        run(c.as_user(OTHER_CODE, p.build_arm_key(p.DISARM, 1)))


@pytest.mark.parametrize("bad", ["12", "123456789", "12a4", ""])
def test_bad_pin_format_never_sent(bad):
    c = Fake([])
    with pytest.raises(client.OrisecPinError):
        run(c.as_user(bad, p.build_arm_key(p.DISARM, 1)))
    assert c.sent == []


class _QueueTransport(client._Transport):
    """Hands out scripted reply frames, one per send, after a tiny delay each."""

    def __init__(self, frames):
        super().__init__()
        self._script = list(frames)

    async def open(self):
        pass

    def send(self, frame):
        loop = asyncio.get_running_loop()
        for i, r in enumerate(self._script.pop(0)):
            loop.call_later(0.01 * (i + 1), self.frames.put_nowait, r)

    @property
    def is_open(self):
        return True


def _timed_request(transport_type, script, frame):
    c = client.OrisecClient(CONN_CODE, transport=transport_type, host="h", port=1)
    c._t = _QueueTransport(script)

    async def go():
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        blocks = await c.request(frame)
        return blocks, loop.time() - t0

    return run(go())


def test_stream_reply_returns_once_every_command_answered():
    frame = p.build_query([(p.Q_SYS_STATE, 1, 2), (p.CMD_LCD, 1, 1)])
    reply = p.build_frame([p.Block(p.Q_SYS_STATE, 1, 2, b"\x00\x00"), p.Block(p.CMD_LCD, 1, 1, b"x")])
    blocks, took = _timed_request("tcp", [[reply]], frame)
    assert {b.cmd for b in blocks} == {p.Q_SYS_STATE, p.CMD_LCD}
    assert took < client.REPLY_SETTLE  # no settle wait


def test_stream_reply_split_over_frames_waits_for_the_rest():
    frame = p.build_query([(p.Q_SYS_STATE, 1, 2), (p.CMD_LCD, 1, 1)])
    a = p.build_frame([p.Block(p.Q_SYS_STATE, 1, 2, b"\x00\x00")])
    b = p.build_frame([p.Block(p.CMD_LCD, 1, 1, b"x")])
    blocks, took = _timed_request("tcp", [[a, b]], frame)
    assert {blk.cmd for blk in blocks} == {p.Q_SYS_STATE, p.CMD_LCD}
    assert took < client.REPLY_SETTLE


def test_udp_still_waits_the_settle_time():
    frame = p.build_query([(p.Q_SYS_STATE, 1, 2)])
    reply = p.build_frame([p.Block(p.Q_SYS_STATE, 1, 2, b"\x00\x00")])
    blocks, took = _timed_request("udp", [[reply]], frame)
    assert len(blocks) == 1 and took >= client.REPLY_SETTLE
