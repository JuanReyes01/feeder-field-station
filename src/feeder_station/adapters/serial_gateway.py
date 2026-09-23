"""The serial transport to the ESP32 USB-CDC modem (S5, S11).

Pure COBS framing lives in ``domain/serial_framing.py``; everything that can
fail against a real device — device resolution, the reconnect loop, and link
state — lives here (design.md section 4, DD9).

**Device resolution, in order, and re-run on every reconnect attempt, never
cached across one:** a replugged USB-CDC device is not guaranteed to come
back under the same ``/dev/ttyUSBn`` name.

1. An explicit configured path.
2. ``/dev/serial/by-id/*`` matching a configured substring.
3. VID:PID through ``serial.tools.list_ports``.
4. Nothing matched: the link is down. The gateway never pretends otherwise.

**Reconnect.** A ``SerialException`` on open, read, or write closes the port,
marks the link down, and backs off: 1, 2, 4, 8, 16, 30 seconds, capped at 30.
``sleep`` and ``open_port`` are both injected (DD9's own reasoning: the
blocking read is pyserial's supported shape, not ``pyserial-asyncio`` or
``loop.add_reader``), so the whole state machine runs in milliseconds under
test instead of real seconds.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import serial
import serial.tools.list_ports

from feeder_station.domain.serial_framing import DELIMITER, FrameStream, cobs_encode

# exploration.md and messages.md do not pin a baud rate for the ESP32's
# USB-CDC link; a virtual COM port ignores it in practice, but pyserial's API
# still requires a value. Documented assumption, same spirit as
# domain/payloads.py's own unpinned-width notes.
DEFAULT_BAUDRATE = 115_200

# design.md section 4: "sleep 1, 2, 4, 8, 16, 30... seconds capped at 30".
# Index past the end of this tuple reuses its last element, which is the cap.
BACKOFF_SCHEDULE_S: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)


class SerialPort(Protocol):
    """The subset of pyserial's ``Serial`` the gateway actually uses."""

    def read(self, size: int) -> bytes: ...
    def write(self, data: bytes) -> int | None: ...
    def close(self) -> None: ...


class ListedPort(Protocol):
    """The subset of ``serial.tools.list_ports_common.ListPortInfo`` used here."""

    device: str
    vid: int | None
    pid: int | None


@dataclass(frozen=True)
class DeviceConfig:
    """What the operator told the station about finding its own radio.

    Every field is optional; an all-``None`` config resolves to nothing, and
    the gateway reports the link as down (step 4) rather than guessing.
    """

    explicit_path: str | None = None
    by_id_substring: str | None = None
    vid: int | None = None
    pid: int | None = None


def _default_by_id_entries(
    by_id_dir: Path = Path("/dev/serial/by-id"),
) -> list[tuple[str, str]]:
    """List ``(symlink name, resolved target)`` pairs under ``by_id_dir``.

    An empty list — never an exception — when the directory does not exist,
    which is the normal case on a machine with no USB-serial device attached
    at all.
    """
    if not by_id_dir.is_dir():
        return []
    entries: list[tuple[str, str]] = []
    for entry in sorted(by_id_dir.iterdir()):
        try:
            target = str(entry.resolve())
        except OSError:
            continue
        entries.append((entry.name, target))
    return entries


def _default_list_ports() -> list[ListedPort]:
    return list(serial.tools.list_ports.comports())


def resolve_device(
    config: DeviceConfig,
    *,
    by_id_entries: Callable[[], Iterable[tuple[str, str]]] = _default_by_id_entries,
    list_ports: Callable[[], Iterable[ListedPort]] = _default_list_ports,
) -> str | None:
    """The 4-step device-resolution order, evaluated fresh every call.

    Returns the resolved device path, or ``None`` when nothing matches
    (step 4 — the link is down).
    """
    if config.explicit_path is not None:
        return config.explicit_path

    if config.by_id_substring is not None:
        for name, target in by_id_entries():
            if config.by_id_substring in name:
                return target

    if config.vid is not None and config.pid is not None:
        for port in list_ports():
            if port.vid == config.vid and port.pid == config.pid:
                return port.device

    return None


def _default_open_port(device: str) -> SerialPort:
    return serial.Serial(device, baudrate=DEFAULT_BAUDRATE, timeout=1.0)


@dataclass(frozen=True)
class LinkState:
    """Everything /health and the UI need to show about the radio link.

    Never partial: a caller reads one consistent snapshot rather than
    several fields that could have changed between reads.
    """

    connected: bool
    device: str | None
    since: float | None
    frames_in: int
    frames_out: int
    decode_errors: int
    last_error: str | None


class SerialGateway:
    """Owns the one open serial port, its reconnect loop, and its link state.

    ``open_port`` and ``sleep`` are both injected so the reconnect/backoff
    state machine can be driven in a test without a real device or a real
    clock (design.md section 4).
    """

    def __init__(
        self,
        config: DeviceConfig,
        *,
        open_port: Callable[[str], SerialPort] = _default_open_port,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        by_id_entries: Callable[[], Iterable[tuple[str, str]]] = _default_by_id_entries,
        list_ports: Callable[[], Iterable[ListedPort]] = _default_list_ports,
    ) -> None:
        self._config = config
        self._open_port = open_port
        self._sleep = sleep
        self._clock = clock
        self._by_id_entries = by_id_entries
        self._list_ports = list_ports

        self._port: SerialPort | None = None
        self._frame_stream = FrameStream()
        self._device: str | None = None
        self._since: float | None = None
        self._frames_in = 0
        self._frames_out = 0
        self._last_error: str | None = None
        # design.md section 7's concurrency table, "Serial writes" row: one
        # lock around `port.write()` -- slice 12 is the first slice that
        # ever gives this gateway two callers (the gateway thread's own ACK
        # writes and the scheduler thread's GET_BACKLOG/command writes); a
        # queue was rejected there for the same reason it is not the answer
        # here either (it would turn "sent after the commit" into "queued
        # after the commit"). Only `write_frame` needs this: reads happen on
        # the gateway thread alone.
        self._write_lock = threading.Lock()

    @property
    def link_state(self) -> LinkState:
        return LinkState(
            connected=self._port is not None,
            device=self._device,
            since=self._since,
            frames_in=self._frames_in,
            frames_out=self._frames_out,
            decode_errors=self._frame_stream.decode_errors,
            last_error=self._last_error,
        )

    def resolve(self) -> str | None:
        """Run the 4-step resolution fresh — never cache it across calls."""
        return resolve_device(
            self._config, by_id_entries=self._by_id_entries, list_ports=self._list_ports
        )

    def connect_once(self) -> bool:
        """Resolve and open the port exactly once. No sleep, no retry."""
        device = self.resolve()
        if device is None:
            self._disconnect("no serial device matched the configured resolution rules")
            return False
        try:
            port = self._open_port(device)
        except serial.SerialException as exc:
            self._disconnect(str(exc))
            return False

        self._port = port
        self._device = device
        self._since = self._clock()
        self._last_error = None
        return True

    def _disconnect(self, error: str | None) -> None:
        if self._port is not None:
            try:
                self._port.close()
            except serial.SerialException:
                pass
        self._port = None
        self._device = None
        self._since = None
        if error is not None:
            self._last_error = error

    def ensure_connected(self, *, stop: Callable[[], bool] = lambda: False) -> bool:
        """Block, retrying with capped exponential backoff, until connected.

        Returns ``False`` only when ``stop()`` becomes true before a
        connection succeeds — by default this call retries forever, exactly
        as a long-running gateway thread should.
        """
        if self._port is not None:
            return True

        attempt = 0
        while not stop():
            if self.connect_once():
                return True
            delay = BACKOFF_SCHEDULE_S[min(attempt, len(BACKOFF_SCHEDULE_S) - 1)]
            self._sleep(delay)
            attempt += 1
        return False

    def read_frames(self, *, size: int = 4096) -> list[bytes]:
        """Read up to ``size`` bytes and return whatever frames they complete.

        A ``SerialException`` here is exactly what design.md's reconnect
        rule is about: close, mark down, and let the caller's next
        ``ensure_connected()`` re-resolve and retry. Nothing is raised
        upward — the link going down is reported through ``link_state``,
        never as an exception the ingest loop must catch.
        """
        if self._port is None:
            return []
        try:
            chunk = self._port.read(size)
        except serial.SerialException as exc:
            self._disconnect(str(exc))
            return []
        if not chunk:
            return []
        blocks = self._frame_stream.feed(chunk)
        self._frames_in += len(blocks)
        return blocks

    def write_frame(self, raw_frame: bytes) -> bool:
        """COBS-encode and write one frame. ``False`` when nothing is connected
        or the write itself drops the link — never raises.

        Holds ``self._write_lock`` for the whole call: two threads writing
        to the same port at once could interleave their bytes into one
        corrupt block on the wire, which no amount of COBS framing on the
        receiving end could recover from.
        """
        with self._write_lock:
            if self._port is None:
                return False
            block = cobs_encode(raw_frame) + bytes([DELIMITER])
            try:
                self._port.write(block)
            except serial.SerialException as exc:
                self._disconnect(str(exc))
                return False
            self._frames_out += 1
            return True

    def close(self) -> None:
        """Close the port, if any, without recording it as a link error."""
        self._disconnect(None)
