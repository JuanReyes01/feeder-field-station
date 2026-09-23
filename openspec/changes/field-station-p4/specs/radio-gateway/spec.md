# Radio Gateway Specification

## Purpose

The serial transport to the ESP32 USB-CDC modem: device resolution, reconnect with backoff, the
ingest-and-ACK ordering that makes S9 real end to end, fault/silence detection, and backlog rotation
across the node roster. All I/O lives here — the impure counterpart to `wire-codec`.

## Requirements

### Requirement: Device resolution and reconnect

The system MUST resolve the serial device by `/dev/serial/by-id` or VID:PID rather than a bare
`/dev/ttyUSBn` path, and MUST attempt reconnection with backoff when the link raises
`SerialException` or otherwise drops.

**Traces:** S5, S11

#### Scenario: replugged device is found under a new node name
- GIVEN a pty loopback standing in for the serial device
- WHEN the device path changes but the by-id symlink resolves to the same target
- THEN the gateway reconnects to it without operator intervention
- **Hardware gate:** whether `/dev/serial/by-id` stays stable in the field depends on the ESP32 board's USB-UART chip, not provable in CI; CI proves the resolve-by-symlink logic against the loopback

#### Scenario: link drop surfaces in /health
- GIVEN an open connection to a pty loopback
- WHEN the loopback is closed to simulate an unplug
- THEN `/health` reports link state down within the next poll interval, and the gateway does not pretend the link is up

### Requirement: Ingest and ACK ordering (S9)

For every inbound frame requiring acknowledgement, the system MUST perform, in order: decode →
write + fsync (via `event-store`) → ACK on the radio → update the in-memory registry → publish an
SSE event. If the write step raises, the system MUST NOT send an ACK.

**Traces:** S9, S3, S4, S11

#### Scenario: ACK withheld on write failure
- GIVEN a store write that raises for a given frame
- WHEN the gateway processes that frame
- THEN no ACK is sent on the radio for it

#### Scenario: registry updates after the write, not before
- GIVEN a valid `CAPTURED` frame
- WHEN it is ingested
- THEN the store write is observably complete before the registry reflects the capture

### Requirement: FAULT and silence handling

On a `FAULT` uplink, the system MUST update the affected trap's reported state to fault. When no
frame has been received from a node within the configured silence interval, the system MUST mark
that trap unreachable rather than leave its last reported state unchanged.

**Traces:** S11, S4

#### Scenario: silent node is marked unreachable
- GIVEN a node last heard from more than `silence_after` ago
- WHEN the registry view is computed
- THEN that trap is reported unreachable, and its last-known state is shown as untrustworthy per S4 — never silently as still-armed-and-fine

### Requirement: Backlog rotation across nodes

The system MUST poll each known node in rotation with `GET_BACKLOG`, using that node's
last-acknowledged store sequence as the cursor, and MUST skip a node that does not respond within
its retry budget and continue the rotation.

**Traces:** S5, S9

#### Scenario: one silent node does not block the rotation
- GIVEN ten simulated nodes, one of which never responds
- WHEN a full rotation runs
- THEN the other nine are polled and commanded without the rotation stalling on the silent one
