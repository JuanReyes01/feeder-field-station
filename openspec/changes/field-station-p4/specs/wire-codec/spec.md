# Wire Codec Specification

## Purpose

Byte-exact encoding and decoding of the v1 protocol's uplink and downlink payload catalogue
(`docs/protocol/messages.md`), plus COBS framing so frames parse safely from the unbounded byte
stream a USB-serial link delivers. Pure `domain/` code: no FastAPI, no sockets, no files, no clocks.

## Requirements

### Requirement: Uplink payload decoding

The system MUST decode `HELLO`, `HEARTBEAT`, `VISIT`, `CAPTURED`, `FAULT` and `ACK` payloads into
structured values per their field layout in `messages.md`, and MUST raise a decode error when a
payload's declared length does not match its type's layout. A decoded `CAPTURED` yields the tag,
epoch and `state_after` as reported by the node — the codec does not decide whether this capture has
been acknowledged; that is `event-store`'s concern (S3).

**Traces:** S3, S4, S11

#### Scenario: decodes a HEARTBEAT payload
- GIVEN a well-formed `HEARTBEAT` payload
- WHEN it is decoded
- THEN the result exposes `state`, `battery_mv`, `clock_epoch`, `clock_source`, `targets_version`, `reads_today`, `last_read_epoch`

#### Scenario: rejects a truncated CAPTURED payload
- GIVEN a `CAPTURED` payload shorter than its fixed layout requires
- WHEN it is decoded
- THEN a decode error is raised and no partial value is returned

### Requirement: Downlink payload encoding

The system MUST encode `ARM`, `DISARM`, `SET_TARGETS`, `GET_STATE`, `TIME_SYNC` and `GET_BACKLOG`
payloads per `messages.md`. For a target list exceeding one frame's 200-byte payload capacity
(27 tags), the system MUST split it across multiple frames sharing one `targets_version`, using
`seq`/`seq_total`, in order.

**Traces:** S1, S2, S5, S12

#### Scenario: encodes a 40-tag target list across two frames
- GIVEN a target list of 40 tags and a `targets_version`
- WHEN it is encoded
- THEN two payloads are produced, `seq=0/seq_total=2` and `seq=1/seq_total=2`, each ≤ 200 bytes, sharing the same `targets_version`

#### Scenario: encodes TIME_SYNC with no refusal field
- GIVEN an epoch value
- WHEN `TIME_SYNC` is encoded
- THEN the payload is exactly the 4-byte little-endian epoch — no confidence or refusal field, matching the protocol as written (D7)

### Requirement: COBS framing and resynchronization

The system MUST encode a frame into a COBS-delimited byte sequence and decode a COBS-delimited byte
stream back into frames. After a corrupted or truncated frame, the decoder MUST resynchronize at the
next delimiter and continue decoding subsequent well-formed frames without decoding garbage as a
frame.

**Traces:** S9 (link integrity only — durable-write ordering belongs to `event-store`)

#### Scenario: round-trips a maximum-size frame
- GIVEN a frame with a 200-byte payload
- WHEN it is COBS-encoded then decoded
- THEN the result equals the original frame

#### Scenario: resyncs after a corrupted frame
- GIVEN a byte stream containing one corrupted frame followed by one well-formed frame
- WHEN the stream is decoded
- THEN the corrupted frame is reported as an error and the well-formed frame that follows still decodes correctly

**All acceptance criteria are runnable in CI:** fixed byte vectors shared with the firmware's own
test suite (`components/link_frame`) prove uplink/downlink correctness with no hardware; COBS
round-trip and resync are pure functions with no I/O.
