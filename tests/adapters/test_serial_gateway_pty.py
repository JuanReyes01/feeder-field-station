"""Serial gateway against a real port. Real pty tier (DD16).

``pty.openpty()`` gives the gateway a genuine ``/dev/pts/N`` path that real
pyserial opens, reads from, and writes to — no ``SerialPort`` fake stands in
anywhere in this module. That proves real bytes survive the actual pyserial
stack: COBS framing, resync past injected corruption, and a frame split
across two separate OS-level reads.

**What a pty still cannot prove, stated plainly (design.md, "What cannot be
verified without hardware"):** a pty is not a USB-CDC device. It has no DTR
reset pulse, no hot-unplug errno of its own, and none of a real UART's
buffering behaviour. The "replug" scenario below closes one pty pair,
repoints a `/dev/serial/by-id`-style symlink at a second freshly-opened pty,
and lets the gateway's own reconnect loop discover that during backoff — an
honest simulation of "the device came back under a new name," not a test of
an actual USB hot-unplug. The real hardware gate — whether `/dev/serial/
by-id` is actually stable for the ESP32 board's own USB-UART chip — is
named in design.md and stays outstanding until run against real hardware.
"""

from __future__ import annotations

import contextlib
import os
import pty
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import serial

from feeder_station.adapters.serial_gateway import DeviceConfig, SerialGateway
from feeder_station.domain import frames
from feeder_station.domain.serial_framing import DELIMITER, cobs_encode

_TEST_TIMEOUT_S = 0.3


def _open_real_port(device: str) -> serial.Serial:
    return serial.Serial(device, baudrate=115_200, timeout=_TEST_TIMEOUT_S)


@contextmanager
def _pty_pair() -> Iterator[tuple[int, str]]:
    """Yield ``(master_fd, slave_path)``. Closing the master fd is what
    later makes the slave path stop existing at all — see the replug test.
    """
    master_fd, slave_fd = pty.openpty()
    slave_name = os.ttyname(slave_fd)
    os.close(slave_fd)  # pyserial reopens the same path by name
    try:
        yield master_fd, slave_name
    finally:
        with contextlib.suppress(OSError):
            os.close(master_fd)


def test_real_bytes_survive_real_pyserial() -> None:
    with _pty_pair() as (master_fd, slave_name):
        gateway = SerialGateway(DeviceConfig(explicit_path=slave_name), open_port=_open_real_port)
        try:
            assert gateway.ensure_connected() is True

            good_a = frames.encode(frames.Frame(msg_type=frames.PING, payload=b"first"))
            good_b = frames.encode(frames.Frame(msg_type=frames.FIRED, payload=b"second"))
            corrupt = bytes([0xFA, 0x01, 0x02, 0x03])  # guaranteed CobsError
            wire = (
                corrupt
                + bytes([DELIMITER])
                + cobs_encode(good_a)
                + bytes([DELIMITER])
                + cobs_encode(good_b)
                + bytes([DELIMITER])
            )

            # A frame split across two separate reads of the real device —
            # nothing guarantees an OS-buffered chunk lines up with a frame
            # boundary, so the split point deliberately lands mid-block.
            split_at = len(wire) // 2
            os.write(master_fd, wire[:split_at])
            decoded = gateway.read_frames(size=split_at)
            os.write(master_fd, wire[split_at:])
            decoded += gateway.read_frames(size=len(wire) - split_at)

            assert decoded == [good_a, good_b]
            state = gateway.link_state
            assert state.decode_errors == 1
            assert state.frames_in == 2

            # The write half: what the gateway puts on the wire is exactly
            # what domain/serial_framing.py would produce by hand.
            assert gateway.write_frame(good_a) is True
            arrived = os.read(master_fd, 128)
            assert arrived == cobs_encode(good_a) + bytes([DELIMITER])
            assert gateway.link_state.frames_out == 1
        finally:
            gateway.close()


def test_replugged_device_is_found_under_a_new_node_name(tmp_path: Path) -> None:
    """5.9/5.10 — hardware gate named in this module's own docstring above.

    The by-id symlink is repointed at a second, freshly-opened pty while the
    gateway is mid-backoff on the first (already-dead) one. The gateway must
    re-resolve the symlink on its very next attempt rather than keep retrying
    the path it first resolved at boot.
    """
    by_id_dir = tmp_path / "by-id"
    by_id_dir.mkdir()
    symlink = by_id_dir / "usb-esp32-if00"

    # Both ptys are allocated while both are still open, so they are
    # guaranteed distinct nodes — devpts reuses a freed minor number, which
    # would otherwise make "found under a new name" trivially true by
    # accident rather than by the gateway's own re-resolution.
    dead_master_fd, dead_slave_fd = pty.openpty()
    dead_slave_name = os.ttyname(dead_slave_fd)
    os.close(dead_slave_fd)

    new_master_fd, new_slave_fd = pty.openpty()
    new_slave_name = os.ttyname(new_slave_fd)
    os.close(new_slave_fd)

    assert new_slave_name != dead_slave_name

    symlink.symlink_to(dead_slave_name)
    os.close(dead_master_fd)
    # With no fd left referencing it, the kernel has torn the dead pty pair
    # down: the symlink now points at a path that no longer exists — what
    # "the device path changed" looks like from the gateway's point of view.

    try:
        swapped = {"done": False}

        def sleep_and_replug(_delay: float) -> None:
            # Stands in for backoff: by the time the gateway wakes up, the
            # symlink has been repointed at the live pty — the moment a
            # real replug's udev rule would have rewritten it.
            if not swapped["done"]:
                symlink.unlink()
                symlink.symlink_to(new_slave_name)
                swapped["done"] = True

        gateway = SerialGateway(
            DeviceConfig(by_id_substring="usb-esp32"),
            open_port=_open_real_port,
            sleep=sleep_and_replug,
            by_id_entries=lambda: [
                (entry.name, str(entry.resolve())) for entry in by_id_dir.iterdir()
            ],
        )
        try:
            assert gateway.ensure_connected() is True
            assert gateway.link_state.device == new_slave_name
            assert swapped["done"] is True

            # And the reconnected link carries real bytes, not just a path.
            good = frames.encode(frames.Frame(msg_type=frames.PING, payload=b"back"))
            os.write(new_master_fd, cobs_encode(good) + bytes([DELIMITER]))
            assert gateway.read_frames(size=len(cobs_encode(good)) + 1) == [good]
        finally:
            gateway.close()
    finally:
        with contextlib.suppress(OSError):
            os.close(new_master_fd)
