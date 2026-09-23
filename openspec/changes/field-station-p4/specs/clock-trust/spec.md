# Clock Trust Specification

## Purpose

The station's own clock-trust model (unset / RTC / NTP-disciplined) and the `TIME_SYNC` policy this
implies. D7 governs: `TIME_SYNC` carries only `epoch`, no confidence and no refusal frame, so
"refuses to answer" means withholding the downlink entirely.

## Requirements

### Requirement: Clock trust states

The system MUST track its own clock as exactly one of: `unset`, `rtc` (time from the DS3231, not yet
NTP-corrected this boot), or `ntp` (disciplined by NTP at least once since boot). The system MUST
expose this state in `/health`.

**Traces:** S12, S15

#### Scenario: no RTC read and no NTP sync yields unset
- GIVEN a fresh boot where the RTC read fails and NTP has not yet synced
- WHEN `/health` is queried
- THEN clock state is `unset`

### Requirement: Withhold TIME_SYNC when untrusted (S12, D7)

While the station's clock state is `unset`, the system MUST NOT send a `TIME_SYNC` downlink to any
node. The system MUST log each withheld send and MUST show "clock unset" in the operator UI. This is
the only form S12's refusal can take, because the protocol's `TIME_SYNC` has no confidence field and
no refusal frame (D7) — the node simply sees silence where a sync would have been.

**Traces:** S12, D7

#### Scenario: TIME_SYNC scheduling skips a node while unset
- GIVEN the station's clock state is `unset`
- WHEN a node would otherwise be due for `TIME_SYNC`
- THEN no `TIME_SYNC` frame is sent, and a withheld-sync log entry is recorded

#### Scenario: TIME_SYNC resumes once trusted
- GIVEN the station's clock state transitions from `unset` to `rtc` or `ntp`
- WHEN a node is next due for `TIME_SYNC`
- THEN the frame is sent

### Requirement: RTC read/write and fallback on failure

The system MUST read the DS3231 at boot to establish clock state, and MUST fall back to `unset` —
never to a system-clock guess — when that read fails or the RTC has never been set.

**Traces:** S15

#### Scenario: RTC read failure yields unset, not a guessed time
- GIVEN a fake RTC adapter that raises on read
- WHEN the station boots
- THEN clock state is `unset`
- **Hardware gate:** whether the physical DS3231 actually holds time across an unpowered hour (S15's literal criterion) cannot be proven in CI; CI proves only that a read failure degrades to `unset` rather than to a wrong time
