"""The station's own event record, copied to removable media in one
documented step (D1, D6, S14).

Two rulings from ``openspec/changes/field-station-p4/proposal.md`` shape
this module and nothing else does:

**D1 — the station exports what it witnessed, not what the analysis app
wants.** The radio only ever carries the collapsed events
``domain/events.py`` already models — a visit, a capture, a trap reporting
its own state — never an individual reader pass, because
``docs/protocol/messages.md`` has no message for one.
:func:`export_events_csv` therefore filters to those types; it does not,
and structurally cannot, synthesise the per-reading raw-ingest file
``feeder-analysis`` wants. The node's own SD card stays the scientific
record, collected by hand — this export is the station's own record,
labelled as such.

**D6 — an export is a consistent snapshot, never a plain copy of a live WAL
database.** Under WAL, a row this station already acknowledged to a trap
can live only in ``station.db-wal``, uncheckpointed. Copying ``station.db``
itself while the station is running can silently omit exactly the event S9
promised was durable. :meth:`~feeder_station.adapters.store.Store.snapshot_into`
uses ``VACUUM INTO`` instead: one statement, one read transaction, a
compacted copy that misses nothing committed at the moment it starts
(DD13). This module never reads the live store's own file directly — only
that snapshot.
"""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path
from sqlite3 import Connection

from feeder_station.adapters.store import Store, open_connection
from feeder_station.domain import payloads
from feeder_station.domain.events import OPERATOR_ARM, OPERATOR_DISARM

# D1: what the station witnessed and may honestly export — a visit, a
# capture, and the trap-state changes it can actually observe: a node's own
# FAULT report, or an operator's arm/disarm command. ``HEARTBEAT`` is
# deliberately excluded: design.md's DD2 already treats it as a continuous
# *report* of current state sent on every contact, not a *change*, and
# including every heartbeat would flood a biologist's export with
# near-duplicate rows for no new fact.
#
# specs/data-export/spec.md names "trap-state changes" without pinning
# exact msg_types; the choice above is this slice's disambiguation of that
# wording, flagged for verify. It also surfaces a real gap in earlier
# slices worth noting here: nothing in ``app.py`` today calls
# ``store.append()`` for an operator's arm/disarm/set-targets/acknowledge
# action, so OPERATOR_ARM/OPERATOR_DISARM rows do not yet exist in
# practice — only a node's own FAULT report currently produces a
# trap-state-change row. Wiring that persistence is outside slice 11's file
# list (``adapters/export.py``, ``docs/requirements.md``,
# ``tests/adapters/test_export.py``) and is left for whichever slice owns
# ``app.py``'s command handlers.
EXPORTABLE_MSG_TYPES: tuple[int, ...] = (
    payloads.VISIT,
    payloads.CAPTURED,
    payloads.FAULT,
    OPERATOR_ARM,
    OPERATOR_DISARM,
)

# DD13's README text, verbatim: provenance lives here and in the folder
# name, never as a `#` comment in the CSV that would break a naive reader.
README_TEXT = (
    "This is the station's own record of what it witnessed.\n"
    "The scientific record is the node's SD card.\n"
)

_CSV_HEADER = (
    "seq",
    "source",
    "node_id",
    "msg_type",
    "msg_id",
    "received_utc",
    "clock_trust",
    "node_utc",
    "tag_hex",
    "fields_json",
)


def _epoch_to_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


def export_events_csv(conn: Connection, csv_path: Path) -> None:
    """Scan ``event``, filtered to D1's witnessed types, oldest first, to CSV.

    Takes a plain ``sqlite3.Connection`` rather than a ``Store`` so it can
    run equally against a small fixture in a unit test, or — the only path
    production ever takes — a ``VACUUM INTO`` snapshot already sitting on
    disk (:func:`export_station`).
    """
    placeholders = ",".join("?" for _ in EXPORTABLE_MSG_TYPES)
    rows = conn.execute(
        f"""
        SELECT seq, source, node_id, msg_type, msg_id, received_epoch,
               clock_trust, node_epoch, tag, fields
          FROM event
         WHERE msg_type IN ({placeholders})
         ORDER BY seq ASC
        """,
        EXPORTABLE_MSG_TYPES,
    ).fetchall()

    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(_CSV_HEADER)
        for (
            seq,
            source,
            node_id,
            msg_type,
            msg_id,
            received_epoch,
            clock_trust,
            node_epoch,
            tag,
            fields_json,
        ) in rows:
            writer.writerow(
                (
                    seq,
                    source,
                    node_id,
                    msg_type,
                    msg_id if msg_id is not None else "",
                    _epoch_to_iso(received_epoch),
                    clock_trust,
                    _epoch_to_iso(node_epoch) if node_epoch is not None else "",
                    tag.hex() if tag is not None else "",
                    fields_json,
                )
            )


def export_station(store: Store, dest_root: str | Path, *, now: datetime | None = None) -> Path:
    """Assemble one ``station-export-{timestamp}/`` folder (DD13): a
    ``VACUUM INTO`` snapshot of the whole store, its D1-filtered CSV, and a
    README — the single documented step S14 asks an operator to follow.

    ``dest_root`` is wherever the runbook points this — a mounted USB
    drive, another machine's shared folder. The database copy inside the
    folder is the *complete* record, unfiltered, exactly as
    ``Store.snapshot_into`` produced it (S14: "the whole record");
    ``events.csv`` is D1's filtered, human-readable view of that same
    snapshot. Only the snapshot is ever read back — never the live store's
    own connection or file (D6, task 11.8).
    """
    timestamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    folder = Path(dest_root) / f"station-export-{timestamp}"
    folder.mkdir(parents=True)

    db_path = folder / "station.db"
    store.snapshot_into(db_path)

    snapshot_conn = open_connection(db_path)
    try:
        export_events_csv(snapshot_conn, folder / "events.csv")
    finally:
        snapshot_conn.close()

    (folder / "README.txt").write_text(README_TEXT, encoding="utf-8")

    return folder
