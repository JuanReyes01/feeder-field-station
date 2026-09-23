# Proposal — field station P4

Phase: `sdd-propose`. Date: 2026-09-23. Builds on `exploration.md`, whose findings are taken as
verified and are not re-derived here.

**What this change is:** turn the current in-memory prototype into the real P4 field station —
durable event store, USB-serial radio gateway, and an operator interface a biologist can use from a
phone. Twelve reviewable slices, each one PR with a green `uv run pytest`.

---

## Intent

The station today is a pure wire-frame codec, a pure in-memory trap registry and a FastAPI shell.
It has no storage, no radio and no way to start by itself. Every requirement that matters in the
field — S9 never losing an event, S7 starting after a power cut, S1-S5 actually reaching a trap —
is unimplemented, because the parts that touch the world do not exist yet.

P4 is also the phase that unblocks the roadmap: P5's range survey needs a station to survey
*towards*, and no Feather can be retired from a feeder until the station that replaces its
collection path is real.

**Success looks like:** a Pi that boots into serving without a keyboard, acknowledges nothing it has
not fsynced, shows ten traps' reported state on a phone, and hands its record to a USB drive in one
step.

## Scope

### In scope

- Durable event store (SQLite, WAL, `synchronous=FULL`) with dedup, backlog cursors and restart recovery.
- COBS framing and a reconnecting pyserial gateway to the ESP32 modem.
- Uplink and downlink payload codecs for the full v1 catalogue.
- Command path: arm/disarm, versioned target lists with delivered-vs-pending.
- Clock trust: DS3231 + NTP + withholding `TIME_SYNC` when untrusted.
- Phone-first operator UI, capture-as-state, fault and silence visibility.
- Event CSV export and whole-record copy; systemd auto-start and an offline-safe boot order.

### Out of scope

- **LoRa and the range survey** — P5. The gateway sits behind an interface so the transport can change.
- **Node firmware**: `station_link` and the gateway ESP32 image are upstream work in `feeder-node-firmware`.
- **Widening `msg_id` on the wire** — a protocol version bump and a reflash of every deployed node.
- **User accounts** (requirements open question 4) and **retention/deletion policy** (open question 5).
- **Audible alerting** (buzzer or page sound), remote access from outside the site, and any analysis feature.
- **A power-loss test rig.** It does not exist; see the honesty note below.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| D1 | **S6 is the station's own event export.** The station exports the visits, captures and trap-state changes *it witnessed*, as CSV. It never synthesises a raw per-reading file. | By design ruling 2 the radio carries collapsed events, so the station structurally cannot reproduce a per-reading file — and `messages.md` has no bulk file transfer message with which to fetch one. The node's SD card stays the scientific record, collected by hand, exactly as the design's network diagram draws it: two arrows into the analysis app, not one. **`docs/requirements.md` S6 must be amended to say this** (slice 11). |
| D2 | **SQLite (WAL, `synchronous=FULL`) writes; DuckDB queries; CSV interchanges.** | Only `synchronous=FULL` gives S9's guarantee — NORMAL can lose the last committed transaction on power loss — and WAL costs one fsync of a small append per commit instead of several. DuckDB attaches to that same file read-only (`INSTALL sqlite; LOAD sqlite; ATTACH … (TYPE SQLITE)`, verified on this machine) and is **never in the write path**: its own docs describe caching in RAM "rather than going back and forth to disk", the opposite of what S9 demands. |
| D3 | **Postgres rejected.** | A server that must be up before the app is a new S7 failure mode, it multiplies SD-card writes, and it carries real operational burden — for under 1 MB/day. |
| D4 | **COBS framing is required on the USB-serial link.** | Our frame has no delimiter and no checksum. ESP-NOW preserves packet boundaries; USB-CDC delivers a byte stream, so one lost byte desynchronises the parser until the port is reopened. COBS is self-synchronising at the next delimiter for ~1 byte of overhead. |
| D5 | **The wire `msg_id` is not a key.** It is 2 bytes and wraps in ~327 days at the design's own 200-events-a-day rate — inside one field season, with R7 keeping nodes awake permanently. The store owns its own ever-increasing sequence; the wire value is compared only inside a bounded recent window. | A schema that assumes global uniqueness or monotonicity silently drops events after the wrap. Fixing it later means rewriting stored history. |
| D6 | **S14's export must take a consistent snapshot** — backup API, `VACUUM INTO`, or a forced `PRAGMA wal_checkpoint(TRUNCATE)` — never a plain copy of a live `station.db`. | Under WAL, acknowledged rows can live only in `station.db-wal`. Copying the main file while running silently omits exactly the events S9 promised. This belongs in the spec, not in the judgement of whoever writes the export. |
| D7 | **S12's refusal is withholding the downlink.** | `TIME_SYNC` carries only `epoch`: there is no confidence field and no refusal frame. The station logs the refusal and shows "clock unset" in its own UI; the node sees silence. Recorded as a protocol gap for a future version. |

## Capabilities

### New capabilities

- `wire-codec`: uplink and downlink payload codecs plus COBS framing for the serial link.
- `event-store`: durable append, dedup, backlog cursors, restart recovery.
- `radio-gateway`: serial transport, device resolution, reconnect, ingest and ACK ordering, backlog rotation.
- `trap-commands`: arm/disarm and versioned target-list delivery.
- `clock-trust`: RTC/NTP trust model and `TIME_SYNC` policy.
- `operator-ui`: phone-first interface, capture-as-state, fault and silence visibility.
- `data-export`: station event CSV and whole-record copy.
- `station-deployment`: auto-start, offline-safe boot, runbook.

### Modified capabilities

None. `openspec/specs/` is empty; this change establishes the first specs.

## Approach

Keep the architecture the repo already has: **pure `domain/` with no FastAPI, no sockets, no files
and no clocks; all I/O in `adapters/`.** Everything byte-exact or rule-shaped is pure and tested
with fixed vectors; everything that can fail in the world is an adapter tested against a fake.

The one ordering that is not negotiable, straight from S9:

```
frame in → decode → write + fsync → ACK on the radio → update registry → publish SSE
```

Only the first two steps are S9-critical. The rest may be best effort — and that is what makes
`Restart=always` safe rather than a substitute for durability.

## Delivery slices

Each slice is one PR, leaves `uv run pytest` green, and leaves the station runnable.

| # | Slice | Delivers | Satisfies | Est. lines |
|---|-------|----------|-----------|-----------|
| 1 | `wire-uplink-codec` | `domain/payloads.py` uplink half: HELLO, HEARTBEAT, VISIT, CAPTURED, FAULT, ACK; byte vectors shared with the firmware suite | S3, S4, S11 | ~260 |
| 2 | `wire-downlink-codec` | Downlink half: ARM, DISARM, SET_TARGETS (with `seq`/`seq_total`), GET_STATE, TIME_SYNC, GET_BACKLOG | S1, S2, S5, S12 | ~240 |
| 3 | `serial-cobs-framing` | `domain/serial_framing.py`: COBS encode/decode and stream resynchronisation after corruption | S9 (link integrity) | ~200 |
| 4 | `durable-event-store` | `adapters/store.py`: WAL + `synchronous=FULL`, own monotonic `seq`, bounded-window dedup (D5), cursor query, registry rehydration at boot, `kill -9` crash-consistency proxy | S9, S3, S5 | ~420 |
| 5 | `serial-gateway-adapter` | `adapters/serial_gateway.py`: `/dev/serial/by-id` or VID:PID resolution, backoff reconnect, pty loopback tests, link state in `/health` | S5, S11 | ~330 |
| 6 | `ingest-and-ack-ordering` | Wires the S9 ordering end to end; FAULT → fault state; silence → unreachable; SSE publish | S9, S3, S4, S11 | ~350 |
| 7 | `backlog-rotation` | `GET_BACKLOG` cursor poller across ten nodes, skip-and-retry for a silent node | S5, S9 | ~280 |
| 8 | `trap-commands` | Arm/disarm and `SET_TARGETS`, resent until the node's heartbeat reports the expected `targets_version`; pending vs delivered in the registry | S1, S2 | ~380 |
| 9 | `clock-trust` | `adapters/clock.py`: trust states (unset / RTC / NTP-disciplined), withholding `TIME_SYNC` (D7), clock status in `/health`; DS3231 overlay + `hwclock` wiring doc | S12, S15, S8 | ~320 |
| 10 | `operator-ui` | Phone-first UI: arm/disarm, target-list editor, capture acknowledge, fault and unreachable, clock-unset banner; nothing fetched from a CDN | S3, S4, S10, S11, S13 | ~400 |
| 11 | `data-export` | `adapters/export.py`: consistent snapshot per D6, event CSV per D1, one documented operator step; amends `docs/requirements.md` S6 | S6, S14 | ~300 |
| 12 | `auto-start-deployment` | `feeder-station.service` (`Restart=always`, `After=network.target`, never `Requires=network-online.target`), install path and runbook | S7, S8 | ~220 |

Dependencies run left to right: 1-3 are pure and independent; 4 needs 1; 5 needs 3; 6 needs 4+5; 7
and 8 need 6; 10 needs 8+9; 11 needs 4.

## Review Workload Forecast

- Estimated changed lines, whole change: **≈3,500** (range 2,900-4,300), across 12 slices.
- Largest single slice: `durable-event-store`, ~420 — at the budget, not over it.
- **Chained PRs recommended: Yes**
- **400-line budget risk: High**
- **Decision needed before apply: Yes** — the chain strategy must be chosen before slice 1.
  Recommendation: **`stacked-to-main`**. Every slice is additive and leaves the station running, so
  nothing needs to wait behind an integration branch, and a 12-deep feature chain would rebase
  constantly for no review benefit.

## Affected areas

| Area | Impact | What changes |
|------|--------|--------------|
| `src/feeder_station/domain/payloads.py` | New | Payload codecs, both directions |
| `src/feeder_station/domain/serial_framing.py` | New | COBS |
| `src/feeder_station/domain/traps.py` | Modified | Disarm, target-list versioning, fault, rehydration from the store |
| `src/feeder_station/adapters/` | New | `store.py`, `serial_gateway.py`, `clock.py`, `export.py` |
| `src/feeder_station/app.py` | Modified | Store-backed endpoints, command routes, export route, clock status |
| `src/feeder_station/cli.py` | Modified | Store path, serial device, gateway lifecycle |
| `pyproject.toml` | Modified | `pyserial` runtime dependency; DuckDB for the query/export path |
| `deploy/feeder-station.service`, `docs/deploy.md` | New | Auto-start and runbook |
| `docs/requirements.md` | Modified | S6 wording amended per D1 |
| `docs/hardware/` | Modified | Station DS3231 wiring and clock setup |

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| **No `station_link` and no gateway ESP32 image** — there is no true end-to-end test | High (it is true today) | Test against a pty loopback and byte vectors shared with the firmware suite. End-to-end is a **hardware gate**, not a CI gate, and is named as such in the verify report |
| **The SD card lies about flushing**, so S9 cannot be proven in CI | Medium | Two tiers: a CI crash proxy (`kill -9` at randomised points, then `PRAGMA integrity_check` plus "every acknowledged event is present"), and a named physical gate that does not exist yet. Recommend a UPS, or a USB SSD for `station.db` |
| **`msg_id` wrap corrupts dedup or the cursor** | Medium | D5. A test drives the counter through `0xFFFF → 0x0000` and asserts nothing is dropped and nothing is deduplicated wrongly |
| **WAL export omits acknowledged events** | Medium | D6, plus a test that snapshots under concurrent writes and asserts every acknowledged event is in the copy |
| **Serial device renames on replug and the gateway goes quiet** | Medium | Resolve by `/dev/serial/by-id` or VID:PID, reconnect with backoff, and surface link state in `/health` and the UI. The gateway never pretends the link is up |
| **The client disagrees with D1's re-scope of S6** | Medium | The export is labelled as the station's own record in the UI and in the file header; the node's SD card remains the scientific record. Raise the amendment with the client alongside slice 11 |
| **Unknown target Pi OS image** (`chrony` vs `ntp`, `/boot/firmware` vs `/boot`) | Medium | Keep the unit file image-agnostic; anything image-specific lives in the runbook, which records the exact image and version it was tested on |
| **A 12-slice chain drifts from integration** | Low | Every slice leaves the app runnable and the suite green; `stacked-to-main` keeps the chain shallow |

### What cannot be verified without hardware

Honest list, so nobody mistakes a green suite for a working station: the SD card's real behaviour
under power loss (S9's written criterion); S15's "left for an hour, still knows the time"; S7's
physical power-pull; the ESP32 modem's USB-UART chip, which decides whether `/dev/serial/by-id` is
stable; and any true end-to-end exchange with a node, which needs firmware that is not written yet.

## Rollback plan

- **Per slice.** Each PR is self-contained and revertible on its own; none depends on a later one to
  leave the suite green. No node firmware changes, so a rollback never requires reflashing a trap.
- **The store.** `station.db` is a single file. Schema changes ship additively (new tables or
  nullable columns), so an older binary opens a newer file. To reset: stop the service, move the
  file aside — never delete it, it is the record — and restart; the station rebuilds an empty store.
- **The gateway.** Runnable with the serial gateway disabled by configuration, which returns the
  station to today's behaviour: UI and SSE, no radio.
- **Deployment.** `systemctl disable --now feeder-station` removes auto-start without touching data.

## Dependencies

- `pyserial` (new runtime dependency); DuckDB for the query/export path — whether the export reads
  through DuckDB or stdlib `sqlite3` is a design-phase call, but either way it reads a consistent
  snapshot (D6), never the live WAL.
- Upstream, and blocking end-to-end only: `station_link` in `feeder-node-firmware`, and a gateway
  ESP32 image, neither of which exists.
- Hardware: a DS3231 wired to the station's I²C, and a decision on the target Raspberry Pi OS image.

## Success criteria

- [ ] S1/S2: a trap arms and takes a new target list from the UI; the UI shows delivered vs pending from the trap's own reported `targets_version`.
- [ ] S3: a capture appears at once in every open browser **and** is still there after a restart.
- [ ] S4/S11: every field shown per trap comes from its heartbeat; a switched-off trap is marked unreachable within the agreed interval.
- [ ] S5: ten simulated nodes report and are commanded without the UI stalling.
- [ ] S6/S14: an operator copies the whole record in one documented step, and the event CSV is a consistent snapshot per D6.
- [ ] S7/S8: the service starts on boot with no keyboard and with no route to the internet.
- [ ] S9: the CI crash proxy passes — integrity check `ok`, and every acknowledged event present. The physical power-pull gate is recorded as outstanding.
- [ ] S10/S13: every operator task completes from a phone browser, by someone who has not read a manual.
- [ ] S12/S15: with the clock never set, the station withholds `TIME_SYNC` and says so; with the RTC fitted, it knows the time after an hour unpowered.
- [ ] `uv run pytest`, `ruff` and `mypy --strict` clean on every slice.

## Next

`sdd-spec` and `sdd-design` — they can run in parallel. Both read this proposal; neither may
re-open D1-D7.
