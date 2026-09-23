# Tasks — field station P4

Phase: `sdd-tasks`. Reads `proposal.md` (D1-D7) and `design.md` (DD1-DD16) — neither is re-opened.
Every task traces to a scenario in `specs/{capability}/spec.md`. Strict TDD: RED (failing test) before
GREEN (passing code) before REFACTOR (only when duplication or a naming smell appears).

Blanket gate, every slice, not repeated per-task below: `uv run pytest`, `ruff`, `mypy --strict` clean;
station still runs; no slice depends on a later one.

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ≈3,500 (range 2,900-4,300), 12 slices |
| 400-line budget risk | High |
| Chained PRs recommended | Yes |
| Suggested split | PR 1 → PR 2 → … → PR 12, `stacked-to-main` |
| Delivery strategy | already resolved by the user |
| Chain strategy | stacked-to-main |

Decision needed before apply: No
Chained PRs recommended: Yes
Chain strategy: stacked-to-main
400-line budget risk: High

### Suggested Work Units

| PR | Slice | Est. lines | Depends on | Parallelizable with |
|----|-------|-----------|------------|----------------------|
| 1 | wire-uplink-codec | ~260 | none | 2 (after), 3, 9 |
| 2 | wire-downlink-codec | ~240 | 1 | 3, 9 |
| 3 | serial-cobs-framing | ~200 | none | 1, 2, 9 |
| 4 | durable-event-store | ~420 | 1 | — |
| 5 | serial-gateway-adapter | ~330 | 3 | 4 |
| 6 | ingest-and-ack-ordering | ~350 | 4, 5 | — |
| 7 | backlog-rotation | ~280 | 6 | 8 |
| 8 | trap-commands | ~380 | 6 | 7 |
| 9 | clock-trust | ~320 | none | 1, 2, 3 |
| 10 | operator-ui | ~400 | 8, 9 | — |
| 11 | data-export | ~300 | 4 | 10, 12 |
| 12 | auto-start-deployment | ~220 | 4, 5 (CLI args only) | 10, 11 |

**Named early, not a footnote:** the `domain/` purity architecture test (task 1.1) lands in PR 1, before
any other domain module exists, so no later slice can quietly import `sqlite3` or `datetime.now`.

---

## Slice 1 — `wire-uplink-codec` (PR 1)

Traces: S3, S4, S11. Files: `domain/payloads.py` (new), `tests/domain/test_architecture_purity.py`
(new), `tests/domain/test_payloads.py` (new).

- [x] 1.1 RED: `test_architecture_purity.py` walks `domain/`, fails on import of `fastapi`, `serial`,
      `sqlite3`, `socket`, `pathlib`, `os`, or use of `datetime.now` — lands before any other new module
- [x] 1.2 GREEN: confirm it passes today (only `frames.py`/`traps.py` exist); fix any offending import
- [x] 1.3 RED: `test_decodes_a_heartbeat_payload` — byte vector shared with `components/link_frame`
- [x] 1.4 GREEN: decode `HEARTBEAT` in `domain/payloads.py`
- [x] 1.5 RED: decode vectors for `HELLO`, `VISIT`, `CAPTURED`, `FAULT`, `ACK`
- [x] 1.6 GREEN: implement each decoder
- [x] 1.7 RED: `test_rejects_a_truncated_captured_payload` — decode error, no partial value
- [x] 1.8 GREEN: length validation raising a decode error before any field is read
- [x] 1.9 REFACTOR: shared length-check helper across decoders

Done: all uplink scenarios green against firmware byte vectors; purity test passes.
PR boundary: `domain/payloads.py` (uplink half) + the two test files. No wiring elsewhere.
Hardware gate: none — pure byte vectors.

## Slice 2 — `wire-downlink-codec` (PR 2, needs 1)

Traces: S1, S2, S5, S12.

- [x] 2.1 RED: encode vectors for `ARM`, `DISARM`, `GET_STATE`, `GET_BACKLOG`
- [x] 2.2 GREEN: implement those encoders
- [x] 2.3 RED: `test_encodes_time_sync_with_no_refusal_field` — exactly 4-byte LE epoch (D7)
- [x] 2.4 GREEN: implement `encode_time_sync`
- [x] 2.5 RED: `test_encodes_a_40_tag_target_list_across_two_frames` — `seq=0/2`, `seq=1/2`, ≤200 B each
- [x] 2.6 GREEN: implement `SET_TARGETS` chunking at 27 tags/frame, one shared `targets_version`
- [x] 2.7 REFACTOR: extract chunking helper if slice 7's rotation logic would otherwise duplicate it

Done: all downlink scenarios green; a chunked `SET_TARGETS` round-trips through slice 1's decoder.
PR boundary: downlink additions to `domain/payloads.py` + tests. No adapter wiring.
Hardware gate: none.

## Slice 3 — `serial-cobs-framing` (PR 3, independent)

Traces: S9 (link integrity only). Files: `domain/serial_framing.py`, `tests/domain/test_serial_framing.py`.

- [x] 3.1 RED: `test_round_trips_a_maximum_size_frame` — 200-byte payload
- [x] 3.2 GREEN: implement `cobs_encode`/`cobs_decode`
- [x] 3.3 RED: `test_resyncs_after_a_corrupted_frame` — corrupted frame, then a well-formed one
- [x] 3.4 GREEN: implement `FrameStream`, a byte-in/blocks-out state machine resyncing at the next `0x00`
- [x] 3.5 REFACTOR: consolidate the resync scan; docstring names the invariant it holds

Done: round-trip and resync scenarios green; byte vectors shared with firmware's COBS tests.
PR boundary: `domain/serial_framing.py` + tests only.
Hardware gate: none — pure state machine.

## Slice 4 — `durable-event-store` (PR 4, needs 1)

Traces: S9, S3, S5 (all 5 event-store requirements). Files: `config.py` (new), `domain/events.py`
(new), `adapters/store.py` (new), `tests/test_config.py`, `tests/domain/test_events.py`,
`tests/adapters/test_store.py` (new dir).

- [x] 4.1 RED: `test_config.py::test_heartbeat_interval_is_5_minutes_silence_after_is_15_minutes` —
      **user decision:** heartbeat every 5 min, unreachable after 15 min (three missed beats); values
      live in `StationConfig`, never as literals
- [x] 4.2 GREEN: implement frozen `StationConfig` in `config.py` from argv/`FEEDER_STATION_*` env
- [x] 4.3 RED: `test_events.py::test_interpret_produces_a_station_event_from_a_captured_frame`
- [x] 4.4 GREEN: `StationEvent` + `interpret(frame) -> StationEvent` in `domain/events.py`
- [x] 4.5 RED: `test_store.py::test_every_opened_connection_has_the_full_pragma_set` — WAL,
      `synchronous=FULL`, `busy_timeout=5000`, `foreign_keys=ON`, `journal_size_limit`, `trusted_schema=OFF`
- [x] 4.6 GREEN: `Store.__init__` creates schema + applies PRAGMAs on every connection open
- [x] 4.7 RED: `test_write_and_fsync_precede_acknowledgement` — write returns before an ACK may be emitted
- [x] 4.8 GREEN: `Store.append()` — one `BEGIN IMMEDIATE` transaction, insert `event` + upsert `node_cursor`
- [x] 4.9 RED: `test_msg_id_wraps_without_dropping_or_duplicating` — drive `0xFFFF → 0x0000` (D5)
- [x] 4.10 GREEN: bounded-window dedup query, `W=32768` per node (DD6)
- [x] 4.11 RED: `test_repeat_within_the_window_is_deduplicated` — stored once, `duplicate_of` set (DD8)
- [x] 4.12 GREEN: wire dedup result into `Appended.duplicate_of`; caller still ACKs
- [x] 4.13 RED: `test_unacknowledged_capture_survives_a_restart` — close, reopen, still unacknowledged
- [x] 4.14 GREEN: `Store.latest(node_id, msg_type)` off `event_by_type(node_id, msg_type, seq DESC)`
- [x] 4.15 RED: `test_cursor_query_returns_events_oldest_first`
- [x] 4.16 GREEN: `Store.cursor(node_id)` + recency-not-maximum `node_cursor` write (DD7)
- [x] 4.17 RED: `test_registry_reflects_last_known_state_after_restart` — rehydration entry point
- [x] 4.18 GREEN: `Store.rehydrate() -> ...` feeding `traps.rehydrate()` per design §6 (heartbeat →
      capture-if-unacked → desired command state, in that order; reachability never rehydrated)
- [x] 4.19 RED: `test_kill_minus_9_crash_proxy` — subprocess writes frames, `kill -9` at randomized
      points, reopen — **CI proxy, not the hardware gate itself**
- [x] 4.20 GREEN: crash-proxy harness; assert `PRAGMA integrity_check` = `ok` and every acknowledged
      event present
- [x] 4.21 REFACTOR: one function applies the full PRAGMA set on every connection, reused by slice 11's
      export connection

Done: all 5 event-store scenarios green; crash-proxy passes; no connection opens without the full
PRAGMA set.
PR boundary: `config.py`, `domain/events.py`, `adapters/store.py`, tests. Not wired into `app.py` yet.
Hardware gate: **whether the SD card's controller actually honours flush (S9's literal criterion)
cannot be proven in CI.** CI proxy: 4.19/4.20's `kill -9` test proves code-level crash-consistency only.

## Slice 5 — `serial-gateway-adapter` (PR 5, needs 3)

Traces: S5, S11 (device resolution + reconnect). Files: `adapters/serial_gateway.py` (new),
`tests/adapters/test_serial_gateway.py` (Protocol fake), `tests/adapters/test_serial_gateway_pty.py`
(real pty, DD16). `pyproject.toml`: add `pyserial`, `types-pyserial` (dev).

- [x] 5.1 RED: `test_resolves_explicit_path_then_by_id_then_vid_pid_then_down` (Protocol fake)
- [x] 5.2 GREEN: implement the 4-step device-resolution order in `adapters/serial_gateway.py`
- [x] 5.3 RED: `test_reconnects_with_backoff_after_serial_exception` — injected `sleep`, backoff
      `1,2,4,8,16,30…` capped, re-resolves from step 1 on every attempt
- [x] 5.4 GREEN: implement the reconnect loop with injected `sleep`/`open_port`
- [x] 5.5 RED: `test_link_drop_surfaces_in_health` — closed loopback → link state down
- [x] 5.6 GREEN: track `connected/device/since/frames_in/frames_out/decode_errors/last_error`
- [x] 5.7 RED (pty tier, DD16): `test_serial_gateway_pty.py::test_real_bytes_survive_real_pyserial`
- [x] 5.8 GREEN: fix any framing/partial-read edge case the real pty exposes
- [x] 5.9 RED: `test_replugged_device_is_found_under_a_new_node_name` (pty + by-id symlink) —
      **hardware gate named**
- [x] 5.10 GREEN: confirm resolve-by-symlink retries on every reconnect attempt

Done: both test tiers green; `pyproject.toml` carries the new runtime + dev dependency; `/health`
exposes every link field.
PR boundary: `adapters/serial_gateway.py`, both test tiers, dependency bump. Not yet wired to ingest.
Hardware gate: **whether `/dev/serial/by-id` is actually stable depends on the ESP32 board's
USB-UART chip (CP2102 vs CH340) — not provable in CI.** CI proxy: pty loopback proves the
resolve-by-symlink logic and reconnect state machine only (5.9/5.10).

## Slice 6 — `ingest-and-ack-ordering` (PR 6, needs 4+5) — DD11-blocking

Traces: S9, S3, S4, S11. Files: `adapters/ingest.py` (new), `app.py` (modified:
`Broadcaster.publish_threadsafe`), `domain/traps.py` (modified: `Registry.fault()`), `tests/adapters/test_ingest.py`.

- [x] 6.1 RED: `test_app.py::test_publish_threadsafe_is_safe_from_a_background_thread` — drives
      `Queue.put_nowait` from a real thread under load; **this is the DD11 test, explicit, not a
      footnote:** today's `Broadcaster.publish` is called straight from a non-loop thread and
      `asyncio.Queue` is documented NOT thread-safe
- [x] 6.2 GREEN: `Broadcaster` captures the running loop at lifespan startup;
      `publish_threadsafe(event)` routes through `loop.call_soon_threadsafe(self.publish, event)`;
      **ingest MUST NOT call the gateway until this lands**
- [x] 6.3 RED: `test_ack_withheld_on_write_failure` — store raises → no ACK written to the port
- [x] 6.4 GREEN: `adapters/ingest.py::ingest_frame()` — decode → `store.append()` (fsync) → ACK write
      → registry update → `publish_threadsafe()`, in that literal order; ACK only after commit returns
- [x] 6.5 RED: `test_registry_updates_after_the_write_not_before` — ordering assertion via a spy store
- [x] 6.6 GREEN: confirm ordering holds (should already pass from 6.4)
- [x] 6.7 RED: `test_fault_uplink_updates_reported_state_to_fault`
- [x] 6.8 GREEN: `Registry.fault(node_id, at)` sets `TrapState.FAULT` (enum already exists)
- [x] 6.9 RED: `test_silent_node_is_marked_unreachable_within_15_minutes` — uses `config.silence_after`
      (task 4.1/4.2), not the old hardcoded `timedelta(minutes=5)` in `app.py`
- [x] 6.10 GREEN: replace `app.py`'s hardcoded `SILENCE_AFTER` with `config.silence_after`; wire
      `ingest.py`'s view path to it
- [x] 6.11 REFACTOR: `adapters/ingest.py` module docstring: "the only place that knows the S9 ordering"

Done: `ingest_frame()` is the single call site wiring gateway→store→ACK→registry→SSE in that order;
6.1's thread-safety test passes under real contention; FAULT/silence scenarios pass.
PR boundary: `adapters/ingest.py`, `Broadcaster.publish_threadsafe`, `Registry.fault()`, tests. First
point the gateway is wired to anything — gated on 6.1/6.2 landing first.
Hardware gate: none new — builds on slices 4/5's proxies.

## Slice 7 — `backlog-rotation` (PR 7, needs 6; parallel with 8)

Traces: S5, S9. Files: `domain/backlog.py` (new, pure), adapter wiring in `adapters/ingest.py` or a
new `adapters/scheduler.py`, `tests/domain/test_backlog.py`, `tests/adapters/test_backlog_rotation.py`.

- [x] 7.1 RED: `test_rotation_visits_each_node_once_per_round`
- [x] 7.2 GREEN: pure round-robin rotation in `domain/backlog.py`
- [x] 7.3 RED: `test_one_silent_node_does_not_block_the_rotation` — 10 nodes, one never responds
- [x] 7.4 GREEN: skip-and-retry-next-round logic, retry budget from `config.py`
- [x] 7.5 RED: `test_ten_simulated_nodes_over_pty_stays_responsive` (S5 scale scenario)
- [x] 7.6 GREEN: wire rotation to `GET_BACKLOG` encode (slice 2) + gateway write (slice 5) + store
      cursor query (slice 4)
- [x] 7.7 REFACTOR: retry-budget constant moves into `config.py` if not already there

Done: 10-node rotation scenario passes without stalling; skip-and-retry scenario passes.
PR boundary: `domain/backlog.py` + adapter wiring + tests.
Hardware gate: none — pty simulates the 10 nodes; true multi-node field scale is not re-claimed here.

## Slice 8 — `trap-commands` (PR 8, needs 6; parallel with 7) — DD15-blocking

Traces: S1, S2. Files: `domain/commands.py` (new), `domain/traps.py` (modified), `app.py` (modified:
disarm route), `tests/domain/test_commands.py`, `tests/domain/test_traps.py` (updated).

- [ ] 8.1 RED: `test_commands.py::test_arm_is_satisfied_by_reported_state_armed`
- [ ] 8.2 GREEN: `Command` (ARM/DISARM/SET_TARGETS) + `satisfied_by(heartbeat)` in `domain/commands.py`
- [ ] 8.3 RED: `test_disarm_is_satisfied_by_reported_state_not_armed` — **the DD15 gap, explicit:**
      today's `Registry.heartbeat()` clears `arm_pending` only when state is `ARMED`, so it cannot
      express a pending disarm; this test fails against the current model before the fix lands
- [ ] 8.4 GREEN: replace `Trap.arm_pending: bool` with `Trap.pending_command: Command | None`;
      `Registry.command_arm/command_disarm` set it, every `heartbeat()` call checks `satisfied_by()`
- [ ] 8.5 RED: `test_set_targets_is_satisfied_only_when_reported_version_matches_desired`
- [ ] 8.6 GREEN: `SET_TARGETS` command + version-comparison in `Registry`
- [ ] 8.7 RED: `test_edit_is_pending_until_confirmed`, `test_resend_continues_until_version_confirmed`
- [ ] 8.8 GREEN: resend scheduling reusing slice 5's backoff pattern
- [ ] 8.9 RED: `test_split_list_is_not_marked_delivered_until_version_matches` — chunked `SET_TARGETS`
      (slice 2), all-or-nothing delivery
- [ ] 8.10 GREEN: confirm delivery marks from version match alone; no partial-frame tracking
- [ ] 8.11 REFACTOR: migrate `test_traps.py::test_arming_is_pending_until_the_trap_itself_reports_it`
      to the new `Command` model without losing its intent

Done: all trap-commands scenarios pass; `arm_pending: bool` fully replaced; existing traps tests
updated, not broken.
PR boundary: `domain/commands.py`, `domain/traps.py`, minimal `app.py` disarm route, updated tests.
Target-list editor UI itself is slice 10's.
Hardware gate: none.

## Slice 9 — `clock-trust` (PR 9, independent)

Traces: S12, S15, S8. Files: `domain/clock_trust.py` (new, pure), `adapters/clock.py` (new),
`tests/domain/test_clock_trust.py`, `tests/adapters/test_clock.py`.

- [ ] 9.1 RED: `test_no_rtc_read_and_no_ntp_sync_yields_unset`
- [ ] 9.2 GREEN: `ClockTrust` states (`unset`/`rtc`/`ntp`) in `domain/clock_trust.py`
- [ ] 9.3 RED: `test_epoch_below_release_floor_is_treated_as_unset` (1970 / 2000-01-01 floor)
- [ ] 9.4 GREEN: implement the floor-epoch check
- [ ] 9.5 RED: `test_clock.py::test_rtc_read_failure_yields_unset_not_a_guessed_time` (fake RTC raises)
      — **hardware gate named**
- [ ] 9.6 GREEN: `adapters/clock.py::Clock` reads `/dev/rtc0` via an injectable fake, degrades to
      `unset` on any failure
- [ ] 9.7 RED: `test_time_sync_scheduling_skips_a_node_while_unset` + a withheld-log-row assertion
- [ ] 9.8 GREEN: `may_distribute_time()` gate in `domain/clock_trust.py`; the command sender checks it
      immediately before emitting `TIME_SYNC` and writes a `station`-source withheld row via slice 4's
      `Store.append` (D7 — no refusal frame exists on the wire)
- [ ] 9.9 RED: `test_time_sync_resumes_once_trusted`
- [ ] 9.10 GREEN: confirm resumption once state flips
- [ ] 9.11 REFACTOR: `adjtimex` ctypes probe with a `timedatectl show -p NTPSynchronized` fallback;
      record in `docs/hardware/` which one was verified on the shipped image

Done: all 3 clock-trust scenarios pass; `/health` exposes clock state; a withheld `TIME_SYNC` writes
a store row.
PR boundary: `domain/clock_trust.py`, `adapters/clock.py`, `/health` field, tests, `docs/hardware/`.
Hardware gate: **whether the physical DS3231 holds time across an unpowered hour (S15's literal
criterion) cannot be proven in CI.** CI proxy: 9.5/9.6's fake-RTC-raises test proves only that a read
failure degrades to `unset`, never to a guessed time.

## Slice 10 — `operator-ui` (PR 10, needs 8+9)

Traces: S3, S4, S8, S10, S11, S12, S13. Files: `static/index.html` (new — moved out of `app.py`'s
`PAGE` string per DD1), `app.py` (modified), `pyproject.toml` (package non-Python files).

- [ ] 10.1 RED: `test_app.py::test_index_serves_static_html_with_no_external_asset_reference`
- [ ] 10.2 GREEN: move `PAGE` into `src/feeder_station/static/index.html`; `app.py`'s `/` route reads
      it; add hatchling package-data config
- [ ] 10.3 RED: `test_arm_disarm_edit_targets_acknowledge_routes_all_return_2xx` (route-level check —
      touch-DOM interaction is not pytest's domain)
- [ ] 10.4 GREEN: add `/api/traps/{id}/disarm`, `/api/traps/{id}/targets` (PUT) calling slice 8's
      `Registry`/`Command` methods
- [ ] 10.5 RED: `test_unacknowledged_capture_is_present_after_a_simulated_restart` (rehydrate a fake
      store, assert `/api/traps` shows it)
- [ ] 10.6 GREEN: confirm slice 4's rehydration path feeds `/api/traps` correctly at startup
- [ ] 10.7 RED: `test_unreachable_trap_is_visually_distinguished_in_the_view_payload`
- [ ] 10.8 GREEN: extend `index.html` template if the existing `.unreachable`/`.capture` classes need
      a new field
- [ ] 10.9 RED: `test_clock_unset_banner_present_when_health_reports_unset`
- [ ] 10.10 GREEN: wire slice 9's `/health` clock field into a banner fetch in `index.html`
- [ ] 10.11 REFACTOR: assert (in a test) no external asset URL anywhere in `static/index.html`

Done: all 3 operator-ui scenarios pass; page stays self-contained; every S1/S2/S3/S4/S8/S10/S11/S12/S13
UI-visible behavior has a route-level test.
PR boundary: `static/index.html`, `app.py` route additions, packaging change, tests.
Hardware gate: **"operable by a biologist with no manual" (S13) and true phone-touch usability are not
pytest-provable.** CI proxy: route-existence + payload-shape tests (10.1-10.10); usability named as a
field gate in the verify report.

## Slice 11 — `data-export` (PR 11, needs 4; parallel with 10, 12)

Traces: S6 (re-scoped, D1), S14, D6. Files: `adapters/export.py` (new), `docs/requirements.md`
(amended), `tests/adapters/test_export.py`.

- [ ] 11.1 RED: `test_export_contains_only_station_witnessed_event_types`
- [ ] 11.2 GREEN: `export_events_csv()` scans `event`, filters to visit/capture/trap-state-change types
- [ ] 11.3 RED: `test_export_is_labelled_as_the_stations_own_record` — header/README text
- [ ] 11.4 GREEN: write `README.txt` + CSV header per DD13's folder layout
- [ ] 11.5 RED: `test_export_under_concurrent_writes_omits_nothing_acknowledged` (D6, writer thread
      appending during export)
- [ ] 11.6 GREEN: `Store.snapshot_into(path)` via `VACUUM INTO`; startup assertion
      `sqlite3.sqlite_version_info >= (3, 27)`
- [ ] 11.7 RED: `test_plain_file_copy_is_not_the_export_path`
- [ ] 11.8 GREEN: confirm export only ever reads the `VACUUM INTO` copy, never the live file
- [ ] 11.9 RED: `test_documented_copy_step_produces_a_complete_openable_copy`
- [ ] 11.10 GREEN: assemble `station-export-{timestamp}/` (db + CSV + README); add the CLI/UI trigger
- [ ] 11.11 Docs (no test): amend `docs/requirements.md` S6 per D1 — station's own export, not a
      per-reading raw file

Done: all 3 data-export scenarios pass, including the concurrent-write snapshot; `docs/requirements.md`
S6 amended.
PR boundary: `adapters/export.py`, docs amendment, tests, export trigger.
Hardware gate: **whether a biologist can follow the copy step unaided (S14's usability half) is not
CI-provable.** CI proxy: 11.1-11.10's mechanical-correctness scenarios; usability named outstanding.

## Slice 12 — `auto-start-deployment` (PR 12, needs 4+5 for CLI args only; parallel with 10, 11)

Traces: S7, S8. Files: `deploy/feeder-station.service` (new), `docs/deploy.md` (new), `cli.py`
(modified), `tests/test_deploy_unit.py`.

- [ ] 12.1 RED: `test_unit_file_has_restart_always_and_no_internet_wait_dependency` — parses the
      `.service` file; asserts `Restart=always`, `After=network.target`, and no
      `network-online.target` in `Requires=`/`Wants=`/`After=`
- [ ] 12.2 GREEN: write `deploy/feeder-station.service`
- [ ] 12.3 RED: `test_process_restarts_after_being_killed` — subprocess harness kills the process,
      asserts `/health` responds again within a bounded delay — **CI proxy, hardware gate named below**
- [ ] 12.4 GREEN: confirm `cli.py`/lifespan exits cleanly on SIGTERM so `Restart=always` restarts a
      sane process, not a corrupt one
- [ ] 12.5 RED: `test_service_starts_with_no_route_to_the_internet` — mocked DNS/network failure,
      `/health` and `/` still respond
- [ ] 12.6 GREEN: confirm no startup path makes an outbound network call
- [ ] 12.7 Docs (no test): `docs/deploy.md` runbook naming install steps and the exact Pi OS image +
      version tested (or flags it explicitly as still open, per design's open question)
- [ ] 12.8 GREEN: `cli.py` args — `--db-path`, `--serial-device`, `--no-gateway` (rollback kill switch)

Done: unit-file, killed-process-restart, and no-internet scenarios pass; runbook exists and names the
tested image or flags it open.
PR boundary: `deploy/feeder-station.service`, `docs/deploy.md`, `cli.py` args, tests.
Hardware gate: **a literal power-pull on the physical Pi (S7's literal criterion) is not CI-provable.**
CI proxy: 12.3's killed-process-then-restart test; named outstanding in the verify report.

---

## Cross-cutting call-outs (carried from design, not footnotes)

- **DD11** (slice 6, 6.1/6.2): `Broadcaster.publish` today calls `Queue.put_nowait` from a non-loop
  thread on a documented-not-thread-safe `asyncio.Queue`. Ingest is not wired to the gateway until
  `publish_threadsafe` lands and its own thread-contention test passes.
- **DD15** (slice 8, 8.3/8.4): `traps.py` cannot express a pending disarm today. The RED test at 8.3
  is written to fail against the *current* model before `Command.satisfied_by(heartbeat)` replaces it.
- **S11 timings** (slice 4, 4.1/4.2; used at 6.9/6.10): heartbeat every 5 minutes, unreachable after
  15 minutes (three missed beats) — both live in `StationConfig`, never as a literal in `app.py`.
- **Purity guard** (slice 1, 1.1): lands in PR 1, before slices 4-12 add any adapter code, so no later
  slice can quietly import something `domain/` must never touch.

## Next

`sdd-apply`, starting PR 1. Each PR is stacked on the previous one's merge to main.
