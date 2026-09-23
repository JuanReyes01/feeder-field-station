# Trap Commands Specification

## Purpose

Arm/disarm and target-list delivery, expressed strictly through the S4 discipline: a command changes
what the station is asking for, never what it displays as true. Delivered-vs-pending is read off the
trap's own reports, not asserted by the station.

## Requirements

### Requirement: Commands set pending state only (S4)

Issuing `ARM` or `DISARM` MUST set (or clear) a pending flag in the registry and MUST NOT change the
trap's reported `state`. The trap's displayed state MUST always be whatever its own most recent
`HEARTBEAT` or `HELLO` reported.

**Traces:** S1, S4

#### Scenario: ARM does not assert armed
- GIVEN a trap last reported `idle`
- WHEN the operator issues `ARM`
- THEN the registry shows `state=idle` (unchanged) and `arm_pending=true`, until a subsequent `HEARTBEAT` reports `state=armed`, at which point `arm_pending` clears

#### Scenario: DISARM mirrors the same discipline
- GIVEN a trap last reported `armed`
- WHEN the operator issues `DISARM`
- THEN the registry continues to show `state=armed` until a `HEARTBEAT` reports otherwise

### Requirement: Target-list delivery with version reconciliation

The system MUST resend `SET_TARGETS` (split across frames by `seq`/`seq_total` when the list exceeds
one frame) until the node's `HEARTBEAT` reports the `targets_version` the station holds for it. The
system MUST distinguish delivered from pending by comparing the trap-reported `targets_version` to
the station-held version, and MUST NOT assume delivery from having sent the frame.

**Traces:** S2

#### Scenario: edit is pending until confirmed
- GIVEN an operator edits a trap's target list, incrementing the held version
- WHEN no `HEARTBEAT` has yet reported the new version
- THEN the UI-facing view shows the edit as pending, not delivered

#### Scenario: resend continues until the version is confirmed
- GIVEN `SET_TARGETS` sent for version N, and a `HEARTBEAT` still reporting version N-1
- WHEN the resend interval elapses
- THEN `SET_TARGETS` for version N is sent again

#### Scenario: split list is not marked delivered until the version matches
- GIVEN a target list split into two `SET_TARGETS` frames sharing one `targets_version`
- WHEN a later `HEARTBEAT` reports that version
- THEN delivery is marked from the version match alone — the station does not track partial-frame delivery separately, matching the node's own all-or-nothing application
