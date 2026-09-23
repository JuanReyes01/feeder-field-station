# Data Export Specification

## Purpose

The station's own event record, exportable to removable media in one documented step. Scoped by D1:
the station exports what it witnessed (visits, captures, trap-state changes) as CSV — it
structurally cannot produce a per-reading raw-ingest file, because the radio never carries
individual reads (design ruling 2). Any export MUST be a consistent snapshot (D6), never a plain
copy of a live WAL-mode database.

## Requirements

### Requirement: Export is the station's own witnessed events (D1)

The system MUST export the visits, captures and trap-state changes stored in `event-store`, as CSV.
The system MUST NOT attempt to synthesize a per-reading raw-ingest CSV, and the export MUST be
labelled — in its header and in the operator UI — as the station's own record, distinct from the
node's SD-card record.

**Traces:** S6 (re-scoped by D1), D1

#### Scenario: export contains only station-witnessed event types
- GIVEN a store holding visit, capture and trap-state-change events
- WHEN an export is produced
- THEN every row is one of those event types, and none is a synthesized individual-read row

#### Scenario: export is labelled as the station's own record
- GIVEN a completed export file
- WHEN its header is inspected
- THEN it identifies itself as the station's event export, not the raw-ingest format

### Requirement: Consistent snapshot (D6)

Any export MUST read a consistent snapshot of the store, taken via the SQLite backup API,
`VACUUM INTO`, or a forced `PRAGMA wal_checkpoint(TRUNCATE)` immediately before copying. The system
MUST NOT read a plain filesystem copy of a live `station.db` while the station is running.

**Traces:** S14, D6

#### Scenario: export under concurrent writes omits nothing acknowledged
- GIVEN a writer thread continuously appending acknowledged events during export
- WHEN the export completes
- THEN every event acknowledged before the export started is present in the export

#### Scenario: plain file copy is not the export path
- GIVEN the station running under WAL
- WHEN the export mechanism is invoked
- THEN it does not read `station.db` directly without first checkpointing or using the backup API

### Requirement: Whole-record copy in one documented step

The system MUST provide, or its runbook MUST document, a single operator action that copies the
complete record — the consistent-snapshot database and/or its CSV export — to a USB drive or another
machine.

**Traces:** S14

#### Scenario: documented step produces a complete, openable copy
- GIVEN the documented copy step
- WHEN an operator follows it
- THEN the resulting file opens and contains the full record as of that snapshot
- **Note:** CI proves the copy mechanism is mechanically correct and complete; that a biologist with no training can follow the written step (S14's usability half) is a field/usability gate, not CI-provable
