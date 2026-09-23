"""Serial gateway: device resolution, reconnect backoff, and link state.

Protocol-fake tier (DD16). Every scenario here drives a fake ``SerialPort``
implementing the ``read``/``write``/``close`` protocol and injects ``sleep``,
so the full backoff schedule (1, 2, 4, 8, 16, 30 s, capped) runs in
microseconds — a test that actually waited 30 seconds is a test nobody runs.
A "closed loopback" (design.md's own phrasing, radio-gateway spec.md) is
simulated here by the fake raising ``serial.SerialException`` on read or
open, mirroring what pyserial itself raises when a real device is unplugged
mid-operation. This tier proves the state machine — resolution order,
backoff schedule, link-state bookkeeping — not real bytes moving through a
real port; see ``test_serial_gateway_pty.py`` for that half, and its own
docstring for what even a real pty still cannot prove.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import serial

from feeder_station.adapters.serial_gateway import (
    DeviceConfig,
    SerialGateway,
    SerialPort,
    resolve_device,
)


class _FakePort:
    """A minimal ``SerialPort`` double: bytes in, bytes out, no OS involved."""

    def __init__(self, *, to_read: bytes = b"") -> None:
        self._to_read = bytearray(to_read)
        self.written: list[bytes] = []
        self.closed = False

    def read(self, size: int) -> bytes:
        chunk = bytes(self._to_read[:size])
        del self._to_read[: len(chunk)]
        return chunk

    def write(self, data: bytes) -> int:
        self.written.append(bytes(data))
        return len(data)

    def close(self) -> None:
        self.closed = True


@dataclass
class _ListedPort:
    device: str
    vid: int | None
    pid: int | None


BY_ID_ENTRIES = [("usb-Silicon_Labs_CP2102-if00", "/dev/ttyUSB3")]
LIST_PORTS = [_ListedPort("/dev/ttyUSB7", vid=0x10C4, pid=0xEA60)]


def _resolve(config: DeviceConfig) -> str | None:
    return resolve_device(config, by_id_entries=lambda: BY_ID_ENTRIES, list_ports=lambda: LIST_PORTS)


# --- 5.1/5.2: device resolution order -----------------------------------------


def test_resolves_explicit_path_then_by_id_then_vid_pid_then_down() -> None:
    full_config = DeviceConfig(
        explicit_path="/dev/ttyACM0",
        by_id_substring="CP2102",
        vid=0x10C4,
        pid=0xEA60,
    )

    # Step 1 wins even when every other step would also match.
    assert _resolve(full_config) == "/dev/ttyACM0"

    # Step 1 absent: step 2 (by-id substring) wins over step 3.
    no_explicit = replace(full_config, explicit_path=None)
    assert _resolve(no_explicit) == "/dev/ttyUSB3"

    # Step 2 configured but its substring matches nothing: falls through to
    # step 3, it does not stop at "configured but not found".
    by_id_no_match = replace(no_explicit, by_id_substring="not-a-real-device")
    assert _resolve(by_id_no_match) == "/dev/ttyUSB7"

    # Steps 1 and 2 both absent: step 3 (VID:PID) wins.
    no_by_id = replace(no_explicit, by_id_substring=None)
    assert _resolve(no_by_id) == "/dev/ttyUSB7"

    # Nothing configured or nothing matches at all: step 4, the link is down.
    nothing_left = replace(no_by_id, vid=None, pid=None)
    assert _resolve(nothing_left) is None


# --- 5.3/5.4: reconnect with backoff -------------------------------------------


def test_reconnects_with_backoff_after_serial_exception_and_re_resolves_every_attempt() -> None:
    # The by-id symlink resolves to a different target on the third lookup —
    # standing in for a device that came back under a new name (design.md:
    # "a replugged USB-CDC device is not guaranteed to return under the same
    # name"). If the gateway cached the first resolution it would keep
    # retrying the dead path forever instead of noticing the new one.
    by_id_lookups: list[list[tuple[str, str]]] = [
        [("usb-node", "/dev/ttyUSB0")],
        [("usb-node", "/dev/ttyUSB0")],
        [("usb-node", "/dev/ttyUSB5")],
    ]
    calls = {"n": 0}

    def by_id_entries() -> list[tuple[str, str]]:
        entries = by_id_lookups[min(calls["n"], len(by_id_lookups) - 1)]
        calls["n"] += 1
        return entries

    opened: list[str] = []

    def open_port(device: str) -> SerialPort:
        opened.append(device)
        if device != "/dev/ttyUSB5":
            raise serial.SerialException("no such device")
        return _FakePort()

    sleeps: list[float] = []
    gateway = SerialGateway(
        DeviceConfig(by_id_substring="usb-"),
        open_port=open_port,
        sleep=sleeps.append,
        by_id_entries=by_id_entries,
        list_ports=list,
    )

    assert gateway.ensure_connected() is True

    assert opened == ["/dev/ttyUSB0", "/dev/ttyUSB0", "/dev/ttyUSB5"]
    assert calls["n"] == 3, "device resolution must re-run on every attempt, not be cached"
    assert sleeps == [1.0, 2.0]
    assert gateway.link_state.connected is True
    assert gateway.link_state.device == "/dev/ttyUSB5"


def test_backoff_is_capped_at_30_seconds_once_the_schedule_is_exhausted() -> None:
    attempts = {"n": 0}

    def open_port(device: str) -> SerialPort:
        attempts["n"] += 1
        if attempts["n"] <= 7:
            raise serial.SerialException("still gone")
        return _FakePort()

    sleeps: list[float] = []
    gateway = SerialGateway(
        DeviceConfig(explicit_path="/dev/ttyUSB0"),
        open_port=open_port,
        sleep=sleeps.append,
    )

    assert gateway.ensure_connected() is True
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]


# --- 5.5/5.6: link state, including a dropped link -----------------------------


def test_link_drop_surfaces_in_health() -> None:
    """A "closed loopback" (radio-gateway spec.md's own scenario wording) is
    simulated by the fake port's ``read`` raising ``SerialException`` — what
    pyserial itself raises when the underlying device vanishes mid-read.
    """
    port = _FakePort(to_read=b"\x01\x02\x00")
    gateway = SerialGateway(
        DeviceConfig(explicit_path="/dev/ttyUSB0"),
        open_port=lambda _device: port,
    )
    assert gateway.ensure_connected() is True
    assert gateway.link_state.connected is True

    def read_then_raise(size: int) -> bytes:
        raise serial.SerialException("device reports readiness to read but returned no data")

    port.read = read_then_raise  # type: ignore[method-assign]

    frames = gateway.read_frames()

    assert frames == []
    state = gateway.link_state
    assert state.connected is False
    assert state.device is None
    assert state.last_error == "device reports readiness to read but returned no data"


def test_link_state_counts_frames_in_and_out_and_decode_errors() -> None:
    from feeder_station.domain import frames as domain_frames
    from feeder_station.domain.serial_framing import DELIMITER, cobs_encode

    raw = domain_frames.encode(domain_frames.Frame(msg_type=domain_frames.PING, payload=b"hi"))
    wire = cobs_encode(raw) + bytes([DELIMITER])
    # A byte sequence that cannot decode: a length code claiming more bytes
    # than the block has, immediately followed by the delimiter.
    corrupt = bytes([0xFA, 0x01, 0x02, 0x03, DELIMITER])

    port = _FakePort(to_read=corrupt + wire)
    gateway = SerialGateway(DeviceConfig(explicit_path="/dev/ttyUSB0"), open_port=lambda _d: port)
    assert gateway.ensure_connected() is True

    decoded = gateway.read_frames(size=1024)
    assert decoded == [raw]

    state = gateway.link_state
    assert state.frames_in == 1
    assert state.decode_errors == 1

    assert gateway.write_frame(raw) is True
    assert port.written == [wire]
    assert gateway.link_state.frames_out == 1
