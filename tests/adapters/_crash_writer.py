"""Subprocess entry point for the kill -9 crash-consistency proxy.

Not a test on its own — spawned by ``test_kill_minus_9_crash_proxy`` in
``test_store.py``. Writes events through the real ``Store.append`` path and
prints each event's ``msg_id`` to stdout, flushed, immediately after
``append()`` returns: the same point at which a real caller would be allowed
to emit an ACK on the radio (S9). The parent test reads a random number of
these lines before sending SIGKILL, so it knows exactly which events were
"acknowledged" before this process died.
"""

from __future__ import annotations

import sys

from feeder_station.adapters.store import Store
from feeder_station.domain import payloads
from feeder_station.domain.events import StationEvent


def main() -> None:
    db_path, count_s = sys.argv[1], sys.argv[2]
    count = int(count_s)

    store = Store(db_path)
    for msg_id in range(count):
        event = StationEvent(
            source="node",
            node_id=1,
            msg_type=payloads.HEARTBEAT,
            msg_id=msg_id,
            node_epoch=None,
            tag=None,
            fields={"n": msg_id},
            payload=b"",
        )
        store.append(event, received_epoch=float(msg_id), clock_trust="unset")
        print(msg_id, flush=True)


if __name__ == "__main__":
    main()
