# Event Store Specification

## Purpose

Durable, restart-safe storage for everything a node reports: the append log itself, deduplication
under a wrapping wire `msg_id` (D5), the backlog cursor query, and rehydrating in-memory trap state
at boot. SQLite, WAL, `synchronous=FULL` per the proposal's D2.

## Requirements

### Requirement: Write and fsync precede acknowledgement

The system MUST persist an inbound event, including an fsync of that write, before any ACK for it is
sent on the radio. This is the first half of S9's binding ordering: decode → **write + fsync** → ACK
on the radio → update registry → publish SSE. Only these first two steps are S9-critical; the rest
may be best effort.

**Traces:** S9

#### Scenario: write completes before ACK is possible
- GIVEN a decoded, ack-required frame
- WHEN it is handed to the store
- THEN the store's write, including fsync, completes and returns before the caller may emit an ACK

#### Scenario: crash-consistency proxy (hardware gate named explicitly)
- GIVEN a harness driving frames through the real write path
- WHEN the process is killed (`kill -9`) at randomized points and reopened
- THEN `PRAGMA integrity_check` reports `ok` and every event the harness saw acknowledged is present
- **Hardware gate:** this proves the code's crash-consistency, not whether the SD card's controller honours flush — that needs a physical power-pull rig that does not exist yet, recorded as an outstanding gate

### Requirement: Own sequence, bounded-window dedup (D5)

The system MUST assign every stored event its own ever-increasing sequence number, independent of
the wire `msg_id`. The system MUST NOT treat the wire `msg_id` as globally unique or monotonic, and
MUST compare it against prior values only within a bounded recent window per `node_id` for
deduplication.

**Traces:** S9, D5

#### Scenario: msg_id wraps without dropping or duplicating
- GIVEN a node whose `msg_id` wraps from `0xFFFF` to `0x0000`
- WHEN the next event arrives
- THEN it is stored with a new store sequence number, is not treated as a duplicate of an earlier low `msg_id`, and is not dropped

#### Scenario: repeat within the window is deduplicated
- GIVEN an event already stored for `(node_id, msg_id)`
- WHEN the same `(node_id, msg_id)` arrives again within the bounded window
- THEN it is recognized as a duplicate and stored once

### Requirement: Capture persists until acknowledged (S3)

A stored capture MUST remain queryable as unacknowledged across restarts until an operator
acknowledges it. Acknowledgement MUST be a store write, not merely an in-memory flag.

**Traces:** S3

#### Scenario: unacknowledged capture survives a restart
- GIVEN a capture stored and not yet acknowledged
- WHEN the process restarts
- THEN the capture is still reported unacknowledged

### Requirement: Backlog cursor query

The system MUST answer "events for node X with store sequence greater than Y", ordered by store
sequence, to serve `GET_BACKLOG` polling.

**Traces:** S5, S9

#### Scenario: cursor query returns events oldest-first
- GIVEN events stored for a node at sequences 10, 11, 12
- WHEN queried with cursor 10
- THEN events 11 and 12 are returned, in that order

### Requirement: Registry rehydration at boot

At startup, the system MUST rebuild in-memory trap state (last known reported state,
`targets_version`, unacknowledged captures) from stored events, without depending on the node
re-sending anything.

**Traces:** S7, S3

#### Scenario: registry reflects last known state after restart
- GIVEN a store holding a prior heartbeat and an unacknowledged capture for a node
- WHEN the process starts
- THEN the in-memory registry reports that state and that unacknowledged capture before any new frame arrives
