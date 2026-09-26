"""The TCP gateway: nodes dial out to one listening socket, over a real
loopback socket -- not only a fake -- because the failure modes this module
exists for (two byte streams sharing framing state, an outbound frame
reaching the wrong socket, a connection the OS has not noticed is dead) are
exactly the ones a fake has no way to reproduce.

Every scenario below drives a real ``socket.socket`` client against a real
``NetworkGateway`` bound to ``127.0.0.1:0`` (an OS-assigned ephemeral port,
never a fixed one -- so this suite can run concurrently with itself and
never collides with whatever else is listening on the machine running it).
"""

from __future__ import annotations

import logging
import socket
import time
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from feeder_station.adapters.network_gateway import NetworkGateway, NetworkGatewayConfig
from feeder_station.domain import frames
from feeder_station.domain.serial_framing import DELIMITER, cobs_encode

_DEADLINE_S = 2.0
_POLL_S = 0.01


def _wait_until(predicate, *, deadline_s: float = _DEADLINE_S) -> bool:
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(_POLL_S)
    return predicate()


@contextmanager
def _gateway(**kwargs: object) -> Iterator[NetworkGateway]:
    gateway = NetworkGateway(NetworkGatewayConfig(host="127.0.0.1", port=0), **kwargs)  # type: ignore[arg-type]
    assert gateway.ensure_connected() is True
    try:
        yield gateway
    finally:
        gateway.close()


def _connect(gateway: NetworkGateway) -> socket.socket:
    port = gateway.bound_port
    assert port is not None
    client = socket.create_connection(("127.0.0.1", port), timeout=_DEADLINE_S)
    return client


def _send_frame(client: socket.socket, *, node_id: int, msg_type: int, payload: bytes = b"") -> None:
    frame = frames.encode(frames.Frame(msg_type=msg_type, node_id=node_id, payload=payload))
    client.sendall(cobs_encode(frame) + bytes([DELIMITER]))


def _drain(gateway: NetworkGateway, *, count: int, deadline_s: float = _DEADLINE_S) -> list[bytes]:
    blocks: list[bytes] = []
    deadline = time.monotonic() + deadline_s
    while len(blocks) < count and time.monotonic() < deadline:
        blocks.extend(gateway.read_frames())
    return blocks


# --- Per-connection isolation: the bug this gateway exists to prevent -------


def test_two_connections_get_independent_frame_streams() -> None:
    """MUTATION TARGET: sharing one ``FrameStream`` across connections --
    e.g. constructing it once in ``__init__`` and handing the same instance
    to every ``_Connection`` -- must fail this test. Node A leaves a
    dangling, never-delimited partial block sitting in its own buffer;
    node B then sends one complete, well-formed frame on a *separate*
    socket. If the two connections shared framing state, B's own delimiter
    would flush A's leftover bytes fused with B's, and COBS would refuse to
    decode the corrupted result -- B's genuinely correct frame must still
    decode standalone.
    """
    with _gateway() as gateway:
        client_a = _connect(gateway)
        client_b = _connect(gateway)
        try:
            good_b = frames.encode(frames.Frame(msg_type=frames.PING, node_id=222, payload=b"clean"))
            partial_a = cobs_encode(
                frames.encode(frames.Frame(msg_type=frames.PING, node_id=111, payload=b"x" * 40))
            )
            # A: half of an encoded block, no delimiter yet -- a dangling buffer.
            client_a.sendall(partial_a[: len(partial_a) // 2])
            # B: one complete, independent block on its own connection.
            client_b.sendall(cobs_encode(good_b) + bytes([DELIMITER]))

            decoded = _drain(gateway, count=1)
            assert decoded == [good_b]
        finally:
            client_a.close()
            client_b.close()


def test_an_oversized_block_on_one_connection_never_blocks_the_other() -> None:
    """A block that never finds its delimiter within the framing layer's
    own ``max_block_bytes`` is dropped and the reader resynchronises at the
    next ``0x00`` -- proven here to hold per connection, not station-wide:
    node A's runaway block must never prevent node B's own frames from
    decoding.
    """
    with _gateway() as gateway:
        client_a = _connect(gateway)
        client_b = _connect(gateway)
        try:
            # Comfortably larger than domain/serial_framing.py's own
            # MAX_ENCODED_BLOCK_BYTES, with no 0x00 anywhere in it.
            runaway = bytes(b for b in range(1, 255)) * 20
            client_a.sendall(runaway)

            good_b = frames.encode(frames.Frame(msg_type=frames.PING, node_id=44, payload=b"still-fine"))
            client_b.sendall(cobs_encode(good_b) + bytes([DELIMITER]))

            decoded = _drain(gateway, count=1)
            assert decoded == [good_b]
        finally:
            client_a.close()
            client_b.close()


# --- Outbound routing: node_id -> the connection it arrived on -------------


def test_write_frame_routes_to_the_connection_the_node_arrived_on() -> None:
    """MUTATION TARGET: routing an outbound write to the *first* connection
    found (or the most recently accepted one) instead of the one the
    matching ``node_id`` actually identified itself on must fail this test.

    Deliberately addresses node 20 -- client B, accepted *second* -- not
    client A: a mutation that routes to "whichever connection comes first
    in the list" would send this to client A instead and pass by accident
    if the target were node 10, the same trap this test itself fell into
    on a first draft.
    """
    with _gateway() as gateway:
        client_a = _connect(gateway)
        client_b = _connect(gateway)
        try:
            _send_frame(client_a, node_id=10, msg_type=frames.PING)
            _send_frame(client_b, node_id=20, msg_type=frames.PING)
            _drain(gateway, count=2)
            assert _wait_until(lambda: len(gateway.link_state.nodes) == 2)

            outbound = frames.encode(frames.Frame(msg_type=frames.FIRED, node_id=20, payload=b"ack"))
            assert gateway.write_frame(outbound) is True

            client_b.settimeout(_DEADLINE_S)
            arrived = client_b.recv(4096)
            assert arrived == cobs_encode(outbound) + bytes([DELIMITER])

            # Nothing at all reached client A -- the wrong socket.
            client_a.settimeout(0.05)
            with pytest.raises(TimeoutError):
                client_a.recv(4096)
        finally:
            client_a.close()
            client_b.close()


def test_write_frame_is_honest_when_no_connection_exists_for_that_node() -> None:
    """Never pretend a write succeeded for a node with no live connection --
    the exact honesty ``SerialGateway.write_frame`` already gives an
    unconnected port.
    """
    with _gateway() as gateway:
        outbound = frames.encode(frames.Frame(msg_type=frames.FIRED, node_id=999, payload=b""))
        assert gateway.write_frame(outbound) is False


# --- /health must not flatten many nodes into one boolean ------------------


def test_health_reports_a_node_as_disconnected_once_its_socket_closes() -> None:
    """MUTATION TARGET: computing ``connected`` from "this node_id has ever
    been seen" instead of "this node's connection is live right now" must
    fail this test -- the exact flattening defect (docs/pending.md's
    closing section) this gateway's whole ``/health`` shape exists to
    avoid.
    """
    with _gateway() as gateway:
        client_a = _connect(gateway)
        client_b = _connect(gateway)
        _send_frame(client_a, node_id=1, msg_type=frames.PING)
        _send_frame(client_b, node_id=2, msg_type=frames.PING)
        _drain(gateway, count=2)
        assert _wait_until(lambda: len(gateway.link_state.nodes) == 2)

        client_b.close()
        assert _wait_until(
            lambda: any(
                n.node_id == 2 and n.connected is False for n in gateway.link_state.nodes
            )
        )
        state = gateway.link_state
        by_id = {n.node_id: n for n in state.nodes}
        assert by_id[1].connected is True
        assert by_id[2].connected is False
        client_a.close()


def test_a_node_that_connects_and_sends_nothing_is_not_reported_as_a_known_node() -> None:
    """No frame ever arrived, so there is no ``node_id`` to report one under
    -- honest silence (``unidentified_connections``), never a fabricated
    entry.
    """
    with _gateway() as gateway:
        client = _connect(gateway)
        try:
            assert _wait_until(lambda: gateway.link_state.unidentified_connections == 1)
            assert gateway.link_state.nodes == ()
        finally:
            client.close()


def test_a_silent_connection_past_the_read_timeout_is_retired() -> None:
    """A half-open connection the OS itself has not noticed: nothing ever
    arrives, and a plain blocking ``recv()`` would hang the reader thread
    forever. The read timeout (test override; production uses
    ``StationConfig.silence_after``, S11's own reachability window) turns
    that indefinite hang into a bounded, reported fact instead.
    """
    with _gateway(read_timeout_s=0.05) as gateway:
        client = _connect(gateway)
        try:
            assert _wait_until(lambda: gateway.link_state.unidentified_connections == 0)
        finally:
            client.close()


# --- Reconnect: the station must not keep writing into a dead old socket ---


def test_a_reconnecting_node_supersedes_its_old_connection() -> None:
    """The exact "node reconnecting while the station still holds its old
    socket" scenario: node 7 opens a second connection without the first
    ever being closed by anything. The old socket must stop being the
    write target -- an outbound frame addressed to node 7 must reach only
    the new connection.
    """
    with _gateway() as gateway:
        old_client = _connect(gateway)
        _send_frame(old_client, node_id=7, msg_type=frames.PING)
        _drain(gateway, count=1)
        assert _wait_until(lambda: len(gateway.link_state.nodes) == 1)

        new_client = _connect(gateway)
        _send_frame(new_client, node_id=7, msg_type=frames.PING)
        _drain(gateway, count=1)
        assert _wait_until(
            lambda: len(gateway.link_state.nodes) == 1 and gateway.link_state.nodes[0].connected
        )

        outbound = frames.encode(frames.Frame(msg_type=frames.FIRED, node_id=7, payload=b"x"))
        assert gateway.write_frame(outbound) is True

        new_client.settimeout(_DEADLINE_S)
        assert new_client.recv(4096) == cobs_encode(outbound) + bytes([DELIMITER])

        # The station closed the stale socket rather than leaving it open
        # and unwritable forever.
        old_client.settimeout(0.05)

        def _old_client_closed() -> bool:
            try:
                return old_client.recv(4096) == b""
            except TimeoutError:
                return False

        assert _wait_until(_old_client_closed)

        old_client.close()
        new_client.close()


# --- Reported live on real hardware (site Wi-Fi, 2026-09-26): a node ACK'd
# every ~5s went to `connected: false` within ~2s of its own ACK, every time,
# and the recorded reason ("no bytes for 900s") could not possibly be true
# for a connection that lived seconds. Both bugs traced to the same file. ---


def test_write_frame_does_not_corrupt_the_read_timeout_of_the_same_connection() -> None:
    """MUTATION TARGET: ``write_frame`` calling ``settimeout()`` on the same
    socket object the reader thread blocks on, with its own much shorter
    write timeout, silently shortens a *later* ``recv()`` call too --
    ``socket.settimeout`` is per socket *object*, not per call, so it is
    not scoped to the one ``sendall()`` that needed it. Measured live: a
    node ACK'd on every heartbeat kept getting retired within
    ``write_timeout_s`` of its own ACK, never anywhere near the configured
    ``read_timeout_s``. Read and write must use independent socket objects
    (``socket.dup()``) so one side's timeout can never leak into the
    other's.

    Five send-ack-wait cycles, not one: whether a given ``write_frame``
    call's ``settimeout()`` lands while the reader thread's ``recv()`` is
    already blocked (unaffected -- the deadline for an in-flight call is
    fixed when it starts) or between two of its calls (affected -- the
    *next* ``recv()`` picks up whatever timeout is current when it starts)
    is a genuine scheduling race. Repeating the cycle removes the
    dependence on winning that race exactly once; the live failure did not
    need to either; it recurred every single time over hours on site.
    """
    with _gateway(read_timeout_s=1.0, write_timeout_s=0.05) as gateway:
        client = _connect(gateway)
        try:
            for _ in range(5):
                _send_frame(client, node_id=7, msg_type=frames.PING)
                assert _wait_until(lambda: len(gateway.link_state.nodes) == 1)

                # The exact action that corrupted the timeout live: one
                # write, simulating the ACK `ingest_frame` sends for every
                # inbound frame that asks for one.
                outbound = frames.encode(
                    frames.Frame(msg_type=frames.FIRED, node_id=7, payload=b"ack")
                )
                assert gateway.write_frame(outbound) is True
                client.settimeout(_DEADLINE_S)
                assert client.recv(4096) == cobs_encode(outbound) + bytes([DELIMITER])

                # Comfortably past write_timeout_s (0.05s) and comfortably
                # short of read_timeout_s (1.0s): a real node's own gap
                # between heartbeats, which must never be long enough to
                # retire the connection on its own.
                time.sleep(0.2)

            assert gateway.link_state.nodes[0].connected is True
        finally:
            client.close()


def test_a_node_reads_connected_right_after_a_frame_arrives_despite_reconnect_churn() -> None:
    """MUTATION TARGET: a self-retired connection's ``node_id`` slot never
    being replaced/refreshed by a later, genuinely live connection for the
    same node -- ``/health`` must never be able to say a node is
    unreachable in the instant after a frame from it was just ingested.

    Reproduces the live sequence exactly: a connection dies *on its own*
    (never superseded -- the firmware's own redial races the station's own
    retirement, so `_associate`'s stale-detection alone is not the only
    path that must stay correct) before the node's replacement connection
    ever arrives, then the replacement connects and sends a frame. The one
    entry this station has ever kept for this node must read connected
    immediately once that frame lands -- not a stale, orphaned row left
    over from the connection that died first.
    """
    with _gateway(read_timeout_s=0.1) as gateway:
        dying_client = _connect(gateway)
        _send_frame(dying_client, node_id=9, msg_type=frames.PING)
        _drain(gateway, count=1)
        assert _wait_until(lambda: len(gateway.link_state.nodes) == 1)

        # Let the first connection die *on its own* -- no supersede, no
        # `_associate` involved yet. The station must notice on its own
        # read-timeout schedule, exactly as a genuinely silent node would.
        assert _wait_until(lambda: gateway.link_state.nodes[0].connected is False)

        # Only now does the replacement arrive -- after the first is
        # already gone, the shape the live redial-churn bug actually hit.
        fresh_client = _connect(gateway)
        try:
            _send_frame(fresh_client, node_id=9, msg_type=frames.PING)
            _drain(gateway, count=1)

            assert _wait_until(
                lambda: len(gateway.link_state.nodes) == 1
                and gateway.link_state.nodes[0].connected is True
            ), "a frame just arrived from node 9, yet /health does not read it as connected"
        finally:
            dying_client.close()
            fresh_client.close()


def test_a_superseded_connection_is_retired_with_the_supersede_reason_not_a_timeout(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """MUTATION TARGET: reporting whatever ``recv()`` happened to raise or
    return when a connection is force-closed by ``_associate``'s own
    supersede path, instead of the actual cause -- a connection closed by
    the station itself, seconds into its life, must never be logged with
    the same sentence a connection that genuinely went silent for the
    whole configured read timeout gets.
    """
    with (
        _gateway(read_timeout_s=5.0) as gateway,
        caplog.at_level(logging.INFO, logger="feeder_station.adapters.network_gateway"),
    ):
        old_client = _connect(gateway)
        _send_frame(old_client, node_id=7, msg_type=frames.PING)
        _drain(gateway, count=1)
        assert _wait_until(lambda: len(gateway.link_state.nodes) == 1)

        new_client = _connect(gateway)
        try:
            _send_frame(new_client, node_id=7, msg_type=frames.PING)
            _drain(gateway, count=1)
            assert _wait_until(
                lambda: gateway.link_state.nodes[0].connected is True
            )

            def _old_connection_retired_logged() -> bool:
                return any("retired" in r.getMessage() for r in caplog.records)

            assert _wait_until(_old_connection_retired_logged)
        finally:
            old_client.close()
            new_client.close()

    retire_records = [r for r in caplog.records if "retired" in r.getMessage()]
    assert retire_records, "the superseded connection's retirement was never logged at all"
    messages = " | ".join(r.getMessage() for r in retire_records)
    assert "no bytes for" not in messages, (
        f"a connection the station itself closed to supersede it was logged with the "
        f"silence-timeout's own reason: {messages!r}"
    )
    assert any("supersede" in r.getMessage() or "reconnect" in r.getMessage() for r in retire_records)


# --- Bind resolution and backoff, the same shape as SerialGateway's own ----


def test_a_port_already_in_use_is_retried_with_backoff_then_succeeds() -> None:
    blocker = socket.create_server(("127.0.0.1", 0))
    busy_port = blocker.getsockname()[1]
    delays: list[float] = []

    def fake_sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) == 1:
            blocker.close()  # free the port during the gateway's own backoff

    gateway = NetworkGateway(
        NetworkGatewayConfig(host="127.0.0.1", port=busy_port), sleep=fake_sleep
    )
    try:
        assert gateway.ensure_connected() is True
        assert delays == [1.0]
    finally:
        gateway.close()


def test_two_nodes_connecting_at_once_are_both_isolated_and_routable() -> None:
    with _gateway() as gateway:
        client_a = _connect(gateway)
        client_b = _connect(gateway)
        try:
            _send_frame(client_a, node_id=101, msg_type=frames.PING)
            _send_frame(client_b, node_id=202, msg_type=frames.PING)
            decoded = _drain(gateway, count=2)
            assert len(decoded) == 2
            assert _wait_until(lambda: len(gateway.link_state.nodes) == 2)
            ids = {n.node_id for n in gateway.link_state.nodes}
            assert ids == {101, 202}
        finally:
            client_a.close()
            client_b.close()
