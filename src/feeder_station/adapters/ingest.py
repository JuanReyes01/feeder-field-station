"""Wires the radio gateway to the event store, the registry and the browsers.

**The only place that knows the S9 ordering** (design.md, section 1): decode
-> write + fsync (``event-store``) -> ACK on the radio -> update the
in-memory registry -> publish an SSE event, in that literal order
(design.md, section 3; radio-gateway spec.md's "Ingest and ACK ordering"
requirement).

Only the first two steps are S9-critical: an ACK must never leave the
station for an event that is not already durably committed.
``store.append()`` does not return until its own ``BEGIN IMMEDIATE`` /
``COMMIT`` transaction has completed, fsync included (``adapters/store.py``).
If it raises, ``ingest_frame`` never reaches the ACK write below it — nothing
downstream of the raise runs, and the caller sees the exception. There is no
``try``/``except`` around that call on purpose.

Registry update and SSE publish are best-effort (design.md's failure table,
steps 8-9 of the sequence diagram): a stale registry entry is corrected by
the node's next heartbeat (S4), and a missed push is corrected by the next
page load (S3) — neither is retried from here.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from feeder_station.adapters.store import trap_state_from_wire
from feeder_station.domain import frames, payloads
from feeder_station.domain.events import (
    StationEvent,
    UninterpretableFrame,
    interpret,
    interpret_reads,
)
from feeder_station.domain.frames import FLAG_ACK_REQUIRED, Frame, FrameError
from feeder_station.domain.payloads import PayloadError
from feeder_station.domain.traps import Registry


class EventStore(Protocol):
    """The two calls this module needs from ``adapters/store.py::Store``.

    A narrow Protocol rather than the concrete class, so a test can prove
    the S9 ordering with a plain spy that never touches SQLite — DD16's own
    "Protocol fake" reasoning, applied one layer up the stack.
    """

    def append(self, event: StationEvent, *, received_epoch: float, clock_trust: str) -> object: ...

    def append_batch(
        self, events: Sequence[StationEvent], *, received_epoch: float, clock_trust: str
    ) -> object: ...


class AckPort(Protocol):
    """The one call this module needs from ``SerialGateway``."""

    def write_frame(self, raw_frame: bytes) -> bool: ...


Publish = Callable[[dict[str, Any]], None]
RecordArrival = Callable[[int, float], None]
RecordPayloadError = Callable[[], None]

logger = logging.getLogger(__name__)


def _no_op_record_arrival(node_id: int, received_epoch: float) -> None:
    return None


def _no_op_record_payload_error() -> None:
    return None


def ingest_frame(
    raw_frame: bytes,
    *,
    store: EventStore,
    ack_port: AckPort,
    registry: Registry,
    publish: Publish,
    received_epoch: float,
    clock_trust: str,
    record_arrival: RecordArrival = _no_op_record_arrival,
    record_payload_error: RecordPayloadError = _no_op_record_payload_error,
) -> None:
    """Carry one raw, already-COBS-decoded frame through the S9 ordering.

    ``raw_frame`` is one block ``SerialGateway.read_frames()`` has already
    delimited and COBS-decoded; what happens here is everything design.md's
    sequence diagram bundles as steps 1 through 9.
    """
    try:
        frame = frames.decode(raw_frame)
        # READS (0x06) is the one uplink message that is not one event per
        # frame: interpret_reads() returns one StationEvent per read, all
        # sharing this frame's own node_id/msg_id (domain/events.py's own
        # docstring has the full argument for why this is a separate
        # function rather than a change to interpret()'s return type).
        # Every other message type keeps interpret()'s existing single-event
        # path completely untouched.
        events: tuple[StationEvent, ...] = (
            interpret_reads(frame) if frame.msg_type == payloads.READS else (interpret(frame),)
        )
    except (FrameError, PayloadError, UninterpretableFrame) as exc:
        # Steps 1-3 failure: nothing was acknowledged, so S9 is intact by
        # construction. The node already assumes an unreliable link and
        # retries; there is nothing more honest to do than drop this one.
        # WARNING C (sdd-verify field-station-p4 batch 1): counted and
        # logged here, separately from FrameStream's COBS-level
        # `decode_errors` one layer below, so the two causes stay
        # distinguishable -- a trap whose every heartbeat lands here used
        # to look identical to one that had simply gone silent.
        logger.warning("ingest: dropped an undecodable frame: %s", exc)
        record_payload_error()
        return

    # CRITICAL 4 (sdd-verify, field-station-p4): the scheduler's own "did
    # this node answer" question (adapters/workers.py) needs a fact only
    # the gateway thread can honestly supply — it is the only place a
    # frame ever really arrives. Best-effort, like the registry update and
    # SSE publish below: recording it is never S9-critical. Every event in
    # `events` names the same node (one frame), so the first is enough.
    record_arrival(events[0].node_id, received_epoch)

    # Steps 4-5: write + fsync. See the module docstring for why nothing
    # catches an exception from this call. A READS batch's N reads share
    # one wire msg_id and must be deduped as the one message they are, not
    # once per read -- store.append_batch() runs that check exactly once
    # per call; see its own docstring for what a per-event append() loop
    # here would silently do to 16 of 17 reads.
    if frame.msg_type == payloads.READS:
        store.append_batch(events, received_epoch=received_epoch, clock_trust=clock_trust)
    else:
        store.append(events[0], received_epoch=received_epoch, clock_trust=clock_trust)

    # Steps 6-7: ACK on the radio, only for frames that asked for one. One
    # frame, one ACK, regardless of how many events it decoded to.
    if frame.flags & FLAG_ACK_REQUIRED:
        ack_frame = Frame(
            msg_type=payloads.ACK,
            node_id=frame.node_id,
            payload=payloads.encode_ack(acked_msg_id=frame.msg_id, status=0),
        )
        ack_port.write_frame(frames.encode(ack_frame))

    # Step 8: registry update. Best-effort — see the module docstring.
    # `_apply_to_registry` already no-ops for any msg_type it does not
    # recognise (READS carries no trap state), so looping it costs nothing
    # extra for the common single-event case and stays correct for a batch
    # without a special case here.
    at = datetime.fromtimestamp(received_epoch, tz=UTC)
    for event in events:
        _apply_to_registry(registry, event, at)

    # Step 9: SSE publish. Best-effort — see the module docstring. Once per
    # frame, not once per event: a 17-read batch is one thing that changed,
    # not seventeen identical notifications.
    publish({"type": "traps_changed", "node_id": events[0].node_id})


def _apply_to_registry(registry: Registry, event: StationEvent, at: datetime) -> None:
    if event.msg_type == payloads.HEARTBEAT:
        _apply_heartbeat(registry, event, at)
    elif event.msg_type == payloads.CAPTURED:
        _apply_captured(registry, event, at)
    elif event.msg_type == payloads.FAULT:
        # docs/pending.md, "A command the node refuses is retried forever":
        # `code` is what lets `Registry.fault()` tell a named refusal from
        # a generic hardware fault -- the same defensive `isinstance` guard
        # `_apply_heartbeat`/`_apply_captured` already apply to their own
        # `fields` reads, since this dict is JSON round-tripped through the
        # store and nothing here re-proves it is still an int.
        code = event.fields.get("code")
        registry.fault(event.node_id, at=at, code=code if isinstance(code, int) else None)
    # HELLO, VISIT and a node's own ACK carry nothing this slice's registry
    # model represents yet — the same "not every event type touches the
    # registry" precedent adapters/store.py's own rehydrate() already sets.


def _apply_captured(registry: Registry, event: StationEvent, at: datetime) -> None:
    """CRITICAL A / WARNING 7 (sdd-verify field-station-p4 batch 2a):
    ``state_after`` was decoded but discarded here — ``registry.captured()``
    hardcoded ``FIRED`` instead of reading it. The ``isinstance`` guard
    mirrors :func:`_apply_heartbeat`'s own defensive check; ``decode_captured``
    already validates ``state_after`` into range, so it should not trigger.
    """
    if event.tag is None:
        return
    state_after = event.fields.get("state_after")
    if not isinstance(state_after, int):
        return
    registry.captured(
        node_id=event.node_id,
        tag=event.tag,
        state=trap_state_from_wire(state_after),
        at=at,
    )


def _apply_heartbeat(registry: Registry, event: StationEvent, at: datetime) -> None:
    state_value = event.fields.get("state")
    if not isinstance(state_value, int):
        return
    battery_mv = event.fields.get("battery_mv")
    targets_version = event.fields.get("targets_version")
    registry.heartbeat(
        node_id=event.node_id,
        state=trap_state_from_wire(state_value),
        at=at,
        battery_mv=battery_mv if isinstance(battery_mv, int) else None,
        targets_version=targets_version if isinstance(targets_version, int) else None,
    )
