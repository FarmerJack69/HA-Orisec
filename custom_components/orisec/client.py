"""Async client for an Orisec panel over any of three links.

- ``udp``    : the CM2 comms module on the LAN (one frame per datagram)
- ``serial`` : the panel's micro-USB port (STM32 virtual COM port, e.g. /dev/ttyACM0)
- ``tcp``    : a serial-to-network bridge (ESP32 stream server, ser2net) wired to a COM port

All three carry the same plain frames. One request is in flight at a time. A request
that gets no reply returns None, which callers must treat as "unknown", never as zeros.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import struct
import termios

from . import protocol as p
from .const import REPLY_SETTLE, REPLY_TIMEOUT

_LOGGER = logging.getLogger(__name__)

TRANSPORT_UDP = "udp"
TRANSPORT_SERIAL = "serial"
TRANSPORT_TCP = "tcp"


class OrisecError(Exception):
    """Communication problem with the panel."""


class OrisecPinError(OrisecError):
    """A user code given for one action was refused by the panel."""


class OrisecAuthError(OrisecError):
    """The panel rejected the code."""


# --- transports -------------------------------------------------------------


def check_reply(blocks: list[p.Block]) -> None:
    """Raise if the panel answered a command with an error block."""
    for b in blocks:
        if b.cmd == p.CMD_ERROR:
            code = b.data[0] if b.data else None
            raise OrisecError(f"Panel refused the command (error {code})")


class _Transport:
    """Delivers whole frames into ``self.frames``."""

    def __init__(self) -> None:
        self.frames: asyncio.Queue[bytes] = asyncio.Queue()
        self.error: Exception | None = None

    def discard_pending(self) -> None:
        while not self.frames.empty():
            self.frames.get_nowait()
        split = getattr(self, "_split", None)
        if split is not None:
            split.reset()

    async def open(self) -> None: ...
    def send(self, frame: bytes) -> None: ...
    def close(self) -> None: ...

    @property
    def is_open(self) -> bool:
        return False


class _UdpTransport(_Transport, asyncio.DatagramProtocol):
    def __init__(self, host: str, port: int) -> None:
        _Transport.__init__(self)
        self._addr = (host, port)
        self._t: asyncio.DatagramTransport | None = None

    async def open(self) -> None:
        loop = asyncio.get_running_loop()
        self._t, _ = await loop.create_datagram_endpoint(lambda: self, remote_addr=self._addr)

    def datagram_received(self, data: bytes, addr) -> None:  # type: ignore[override]
        self.frames.put_nowait(data)

    def error_received(self, exc: Exception) -> None:
        self.error = exc

    def send(self, frame: bytes) -> None:
        assert self._t is not None
        self._t.sendto(frame)

    def close(self) -> None:
        if self._t is not None:
            self._t.close()
        self._t = None

    @property
    def is_open(self) -> bool:
        return self._t is not None and not self._t.is_closing()


class _FrameSplitter:
    """Cuts a byte stream into frames using the length prefix and CRC.

    Scans every offset for a complete frame with a valid CRC, so stray bytes that
    happen to look like a length can't stall it.
    """

    MAX_FRAME = 4096

    def __init__(self, out: asyncio.Queue[bytes]) -> None:
        self._buf = bytearray()
        self._out = out

    def reset(self) -> None:
        self._buf.clear()

    def feed(self, data: bytes) -> None:
        self._buf += data
        while True:
            found = False
            for i in range(len(self._buf) - 1):
                (length,) = struct.unpack_from("<H", self._buf, i)
                if length < 12 or length > self.MAX_FRAME or i + length > len(self._buf):
                    continue
                frame = bytes(self._buf[i : i + length])
                if p.crc16(frame[:-2]) == struct.unpack_from("<H", frame, length - 2)[0]:
                    self._out.put_nowait(frame)
                    del self._buf[: i + length]
                    found = True
                    break
            if not found:
                break
        if len(self._buf) > self.MAX_FRAME:
            del self._buf[: len(self._buf) - self.MAX_FRAME]


_BAUD_CONSTANTS = {
    9600: termios.B9600,
    19200: termios.B19200,
    38400: termios.B38400,
    57600: termios.B57600,
    115200: termios.B115200,
}


class _SerialTransport(_Transport):
    """Raw tty. USB CDC-ACM ignores the baud rate; a real UART (TTL adaptor,
    serial comport) needs it to match the panel's port."""

    def __init__(self, device: str, baud: int = 115200) -> None:
        super().__init__()
        self._device = device
        self._baud = _BAUD_CONSTANTS.get(baud, termios.B115200)
        self._fd: int | None = None
        self._split = _FrameSplitter(self.frames)

    async def open(self) -> None:
        fd = os.open(self._device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        attrs = termios.tcgetattr(fd)
        # cfmakeraw equivalent
        attrs[0] &= ~(termios.IGNBRK | termios.BRKINT | termios.PARMRK | termios.ISTRIP
                      | termios.INLCR | termios.IGNCR | termios.ICRNL | termios.IXON)
        attrs[1] &= ~termios.OPOST
        attrs[2] &= ~(termios.CSIZE | termios.PARENB)
        attrs[2] |= termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[3] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG | termios.IEXTEN)
        attrs[4] = attrs[5] = self._baud
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        termios.tcflush(fd, termios.TCIOFLUSH)
        self._fd = fd
        asyncio.get_running_loop().add_reader(fd, self._readable)

    def _readable(self) -> None:
        try:
            data = os.read(self._fd, 4096)  # type: ignore[arg-type]
        except BlockingIOError:
            return
        except OSError as err:
            self.error = err
            self.close()
            return
        if data:
            self._split.feed(data)

    def send(self, frame: bytes) -> None:
        assert self._fd is not None
        view = memoryview(frame)
        while view:
            try:
                n = os.write(self._fd, view)
            except BlockingIOError:
                continue
            view = view[n:]

    def close(self) -> None:
        if self._fd is not None:
            with contextlib.suppress(RuntimeError):
                asyncio.get_running_loop().remove_reader(self._fd)
            with contextlib.suppress(OSError):
                os.close(self._fd)
        self._fd = None

    @property
    def is_open(self) -> bool:
        return self._fd is not None


class _TcpTransport(_Transport):
    def __init__(self, host: str, port: int) -> None:
        super().__init__()
        self._addr = (host, port)
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None
        self._split = _FrameSplitter(self.frames)

    async def open(self) -> None:
        reader, self._writer = await asyncio.wait_for(asyncio.open_connection(*self._addr), REPLY_TIMEOUT)
        self._task = asyncio.get_running_loop().create_task(self._read(reader))

    async def _read(self, reader: asyncio.StreamReader) -> None:
        try:
            while data := await reader.read(4096):
                self._split.feed(data)
        except OSError as err:
            self.error = err
        self.close()

    def send(self, frame: bytes) -> None:
        assert self._writer is not None
        self._writer.write(frame)

    def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
        self._writer = None
        if self._task is not None and not self._task.done() and self._task is not asyncio.current_task():
            self._task.cancel()

    @property
    def is_open(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()


# --- client -----------------------------------------------------------------


class OrisecClient:
    def __init__(
        self,
        password: str,
        *,
        transport: str = TRANSPORT_UDP,
        host: str | None = None,
        port: int | None = None,
        device: str | None = None,
        baud: int = 115200,
    ) -> None:
        self._password = password
        self.transport_type = transport
        self.host = host
        self.port = port
        self.device = device
        self.baud = baud
        self._t: _Transport | None = None
        self._lock = asyncio.Lock()
        self.login_info: p.LoginInfo | None = None
        # Set when switching back to the connection's own user failed.
        self.relogin_needed = False

    @property
    def label(self) -> str:
        if self.transport_type == TRANSPORT_SERIAL:
            return f"USB {self.device}"
        if self.transport_type == TRANSPORT_TCP:
            return f"bridge {self.host}:{self.port}"
        return f"{self.host}:{self.port}"

    def _make(self) -> _Transport:
        if self.transport_type == TRANSPORT_SERIAL:
            return _SerialTransport(self.device or "", self.baud)
        if self.transport_type == TRANSPORT_TCP:
            return _TcpTransport(self.host or "", self.port or 0)
        return _UdpTransport(self.host or "", self.port or p.DEFAULT_PORT)

    async def _ensure_open(self) -> _Transport:
        if self._t is None or not self._t.is_open:
            t = self._make()
            try:
                await t.open()
            except (OSError, TimeoutError) as err:
                raise OrisecError(f"Cannot open {self.label}: {err}") from err
            self._t = t
        return self._t

    async def close(self) -> None:
        if self._t is not None:
            self._t.close()
        self._t = None
        self.login_info = None

    async def request(self, frame: bytes) -> list[p.Block] | None:
        """Send one frame and return the reply blocks, or None if the panel is silent."""
        async with self._lock:
            return await self._request(frame)

    async def _request(self, frame: bytes) -> list[p.Block] | None:
        """request() without taking the lock; the caller must hold it.

        Over the CM2 a reply can come in several datagrams, so it waits REPLY_SETTLE
        for more after the first. Over USB or the bridge it stops as soon as every
        command asked for has answered (or the panel sent an error), which saves
        that wait on almost every request.
        """
        t = await self._ensure_open()
        q = t.frames
        t.discard_pending()
        t.error = None
        want: set[int] = set()
        if self.transport_type in (TRANSPORT_TCP, TRANSPORT_SERIAL):
            with contextlib.suppress(p.FrameError):
                want = {b.cmd for b in p.parse_frame(frame)}
        t.send(frame)
        try:
            first = await asyncio.wait_for(q.get(), REPLY_TIMEOUT)
        except TimeoutError:
            if t.error:
                _LOGGER.debug("Error on %s: %s", self.label, t.error)
            return None
        blocks: list[p.Block] = []
        seen: set[int] = set()

        def take(r: bytes) -> None:
            try:
                got = p.parse_frame(r)
            except p.FrameError:
                return  # unsolicited/other-format packets (e.g. CM2 pushes) are ignored
            blocks.extend(got)
            seen.update(b.cmd for b in got)

        def complete() -> bool:
            return bool(want) and (want <= seen or p.CMD_ERROR in seen)

        take(first)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + REPLY_SETTLE
        while not complete() and (left := deadline - loop.time()) > 0:
            try:
                take(await asyncio.wait_for(q.get(), left))
            except TimeoutError:
                break
        return blocks

    async def login(self) -> p.LoginInfo:
        blocks = await self.request(p.build_login(self._password))
        if blocks is None:
            raise OrisecError(f"No reply from panel via {self.label} during login")
        info = p.parse_login(blocks)
        if not info.ok:
            if info.error == 3:
                raise OrisecAuthError("Panel rejected the code")
            raise OrisecError(f"Login failed (panel error {info.error})")
        self.login_info = info
        return info

    async def try_user_login(self, code: str) -> tuple[p.LoginInfo, p.LoginInfo]:
        """Log in with another user's code, then straight back in with the
        connection's own code, holding the lock so nothing else runs in between.

        Diagnostic for per-user arming. Returns (other user's login, restored login).
        The code is never logged or stored.
        """
        if not code.isdigit() or not 4 <= len(code) <= 8:
            raise OrisecPinError("A PIN is 4 to 8 digits")
        async with self._lock:
            other = p.parse_login(await self._request(p.build_login(code)) or [])
            back = p.parse_login(await self._request(p.build_login(self._password)) or [])
            if back.ok:
                self.login_info = back
        return other, back

    async def as_user(self, code: str, frame: bytes) -> list[p.Block]:
        """Send one command logged in as the user with `code`, so the panel applies
        that user's permissions and logs their name; then log back in with the
        connection's own code. The lock is held throughout, so no poll runs as
        the other user. The code is never logged or stored.
        """
        if not code.isdigit() or not 4 <= len(code) <= 8:
            raise OrisecPinError("A PIN is 4 to 8 digits")
        async with self._lock:
            other = p.parse_login(await self._request(p.build_login(code)) or [])
            reply: list[p.Block] | None = None
            if other.ok:
                reply = await self._request(frame)
            back = p.parse_login(await self._request(p.build_login(self._password)) or [])
            if back.ok:
                self.login_info = back
            else:
                self.relogin_needed = True
        if not other.ok:
            if other.error in (3, 4):
                raise OrisecPinError("PIN refused by the panel")
            raise OrisecError(f"Login with PIN failed (panel error {other.error})")
        if reply is None:
            raise OrisecError("Panel did not acknowledge the command")
        check_reply(reply)
        return reply

    async def read(self, reads: list[tuple[int, int, int]]) -> list[p.Block] | None:
        return await self.request(p.build_query(reads))

    async def press_key(self, key: str) -> bool:
        return await self.request(p.build_keypad_char(key)) is not None

    async def arm_key(self, key: int, area_mask: int) -> bool:
        return await self.request(p.build_arm_key(key, area_mask)) is not None

    async def omit_zone(self, zone: int, omit: bool) -> bool:
        return await self.request(p.build_omit_zone(zone, omit)) is not None

    async def set_clock(self, dt) -> None:
        """Set the panel clock to ``dt``, one parameter per frame.

        Holds the lock so no poll runs between the six writes. Raises if any
        write is refused or not acknowledged.
        """
        async with self._lock:
            for param, frame in zip(p.CLOCK_PARAMS, p.build_clock_writes(dt), strict=True):
                reply = await self._request(frame)
                if reply is None:
                    raise OrisecError(f"No reply setting the clock (parameter {param})")
                check_reply(reply)
                if not p.is_write_ack(reply, param):
                    raise OrisecError(f"Panel did not accept the clock (parameter {param})")

    async def read_event(self, index: int) -> p.Event | None:
        """One event-log entry (1 = newest); None if empty or no reply."""
        blocks = await self.request(p.build_query([(p.CMD_EVENT_LOG, index, 1)]))
        for b in blocks or []:
            if b.cmd == p.CMD_EVENT_LOG and b.start == index:
                return p.parse_event(index, b.data)
        return None
