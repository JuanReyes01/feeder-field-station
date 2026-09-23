# Verification Report — field station P4

**Change**: `field-station-p4`
**Phase**: `sdd-verify`. Date: 2026-09-23.
**Mode**: Strict TDD. Artifact store: `openspec`.
**Posture**: adversarial — validated against `specs/*/spec.md` and `docs/requirements.md`, not against
the test suite. A green suite was treated as the claim under attack, not as the conclusion.

**Verdict: FAIL** — 5 CRITICAL, 11 WARNING, 9 SUGGESTION.

The suite is green and the components are, individually, good. The failures are all of one shape and
it is the same shape apply already found four times: **a component that is correct in isolation with
nothing connecting it to the running station**, invisible because the covering test assembles the
wiring by hand. Three more instances of that family are below, plus one latent bug that becomes live
the moment the first is fixed, plus one measured protocol-budget violation.

---

## Completeness

| Metric | Value |
|--------|-------|
| Tasks total | 138 |
| Tasks complete (`[x]`) | 138 |
| Tasks incomplete | 0 |
| Slices | 13 |

Three tasks are marked complete whose code does not do what the task says (see CRITICAL 1, 3, 5):
**10.6**, **5.5/5.6**, and **9.9/9.10**.

## Build & Tests Execution

**Tests**: ✅ `uv run pytest` → **200 passed**, 2 warnings, 3.48 s, exit 0.

**Lint**: ✅ `uv run ruff check .` → `All checks passed!`

**Format**: ⚠️ `uv run ruff format --check .` → **6 files would be reformatted**
(`openspec/changes/field-station-p4/design.md`, `src/feeder_station/domain/payloads.py`,
`tests/adapters/test_ingest.py`, `tests/adapters/test_serial_gateway.py`,
`tests/domain/test_events.py`, `tests/domain/test_traps.py`). Pre-existing drift, disclosed in
tasks.md slice 13 (which counts it as 5; it is 6).

**Types**: ✅ `uv run mypy src` → `Success: no issues found in 22 source files`.
⚠️ `uv run mypy tests` → 95 errors in 22 files (the agreed gate is `src`; recorded, not counted).

**Build**: ✅ `uv build` → `feeder_field_station-0.1.0.tar.gz` + `-py3-none-any.whl`.

**Coverage**: ➖ no coverage tool configured in `pyproject.toml`. Not a failure.

---

## Spec Compliance Matrix

41 scenarios across 8 specs. **36 compliant, 5 not.**

| Spec | Scenario | Covering test | Result |
|------|----------|---------------|--------|
| wire-codec | decodes a HEARTBEAT payload | `test_payloads.py::test_decodes_a_heartbeat_payload` | ✅ |
| wire-codec | rejects a truncated CAPTURED payload | `test_payloads.py::test_rejects_a_truncated_captured_payload` | ✅ |
| wire-codec | encodes a 40-tag list across two frames | `test_payloads.py::test_encodes_a_40_tag_target_list_across_two_frames` | ✅ |
| wire-codec | encodes TIME_SYNC with no refusal field | `test_payloads.py::test_encodes_time_sync_with_no_refusal_field` | ✅ |
| wire-codec | round-trips a maximum-size frame | `test_serial_framing.py::test_round_trips_a_maximum_size_frame` | ✅ |
| wire-codec | resyncs after a corrupted frame | `test_serial_framing.py::test_resyncs_after_a_corrupted_frame` | ✅ |
| event-store | write completes before ACK is possible | `test_store.py::test_write_and_fsync_precede_acknowledgement` | ✅ |
| event-store | crash-consistency proxy | `test_store.py::test_kill_minus_9_crash_proxy` | ✅ |
| event-store | msg_id wraps without dropping/duplicating | `test_store.py::test_msg_id_wraps_without_dropping_or_duplicating` | ✅ |
| event-store | repeat within the window is deduplicated | `test_store.py::test_repeat_within_the_window_is_deduplicated` | ✅ |
| event-store | unacknowledged capture survives a restart | `test_store.py::test_unacknowledged_capture_survives_a_restart` | ✅ (store level) |
| event-store | cursor query returns events oldest-first | `test_store.py::test_cursor_query_returns_events_oldest_first` | ✅ |
| **event-store** | **registry reflects last known state after restart** | `test_store.py::test_registry_reflects_last_known_state_after_restart` — calls `rehydrate()` itself | ❌ **UNTESTED at "at startup"; VIOLATED — CRITICAL 1** |
| radio-gateway | replugged device found under a new node name | `test_serial_gateway_pty.py::test_replugged_device_is_found_under_a_new_node_name` | ✅ |
| **radio-gateway** | **link drop surfaces in `/health`** | `test_serial_gateway.py::test_link_drop_surfaces_in_health` — never requests `/health` | ❌ **UNTESTED; VIOLATED — CRITICAL 5** |
| radio-gateway | ACK withheld on write failure | `test_ingest.py::test_ack_withheld_on_write_failure` | ✅ |
| radio-gateway | registry updates after the write, not before | `test_ingest.py::test_registry_updates_after_the_write_not_before` | ✅ |
| radio-gateway | silent node is marked unreachable | `test_app.py::test_silent_node_is_marked_unreachable_within_15_minutes` | ✅ |
| radio-gateway | one silent node does not block the rotation | `test_backlog_rotation.py::test_run_rotation_round_visits_every_node_and_retries_the_silent_one` | ⚠️ PARTIAL — budget resets every tick (CRITICAL 4) |
| trap-commands | ARM does not assert armed | `test_traps.py::test_arming_is_pending_until_the_trap_itself_reports_it` | ✅ |
| trap-commands | DISARM mirrors the same discipline | `test_traps.py::test_disarm_pending_clears_when_the_trap_reports_not_armed` | ✅ |
| trap-commands | edit is pending until confirmed | `test_traps.py::test_edit_is_pending_until_confirmed` | ✅ |
| trap-commands | resend continues until version confirmed | `test_traps.py::test_resend_continues_until_version_confirmed` | ⚠️ PARTIAL — lost across a restart (CRITICAL 1) |
| trap-commands | split list not delivered until version matches | `test_traps.py::test_split_list_is_not_marked_delivered_until_version_matches` | ✅ |
| clock-trust | no RTC read and no NTP sync yields unset | `test_clock_trust.py::test_no_rtc_read_and_no_ntp_sync_yields_unset` + `test_app.py::test_health_reports_clock_unset` | ✅ |
| clock-trust | TIME_SYNC scheduling skips a node while unset | `test_clock.py::test_time_sync_scheduling_skips_a_node_while_unset` | ✅ |
| **clock-trust** | **TIME_SYNC resumes once trusted** | `test_clock.py::test_time_sync_resumes_once_trusted` — no transition is exercised | ❌ **UNTESTED; VIOLATED — CRITICAL 3** |
| clock-trust | RTC read failure yields unset | `test_clock.py::test_rtc_read_failure_yields_unset_not_a_guessed_time` | ✅ |
| operator-ui | full task list completes on one page | `test_app.py::test_arm_disarm_edit_targets_acknowledge_routes_all_return_2xx` | ✅ (route level; DOM gate disclosed) |
| **operator-ui** | **capture from hours ago is still visible** | `test_app.py::test_unacknowledged_capture_is_present_after_a_simulated_restart` — rehydrates by hand | ❌ **VIOLATED across a real restart — CRITICAL 1** |
| operator-ui | unreachable trap is marked, not hidden | `test_app.py::test_unreachable_trap_is_visually_distinguished_in_the_view_payload` | ✅ |
| operator-ui | unset clock shows a banner | `test_app.py::test_clock_unset_banner_present_when_health_reports_unset` | ✅ |
| data-export | export contains only witnessed event types | `test_export.py::test_export_contains_only_station_witnessed_event_types` | ⚠️ PARTIAL — see WARNING 2 |
| data-export | export is labelled as the station's own record | `test_export.py::test_export_is_labelled_as_the_stations_own_record` | ✅ |
| data-export | export under concurrent writes omits nothing | `test_export.py::test_export_under_concurrent_writes_omits_nothing_acknowledged` | ✅ |
| data-export | plain file copy is not the export path | `test_export.py::test_plain_file_copy_is_not_the_export_path` | ✅ |
| data-export | documented step produces a complete copy | `test_export.py::test_documented_copy_step_produces_a_complete_openable_copy` + `docs/deploy.md` | ✅ |
| station-deployment | process restarts after being killed | `test_deploy_unit.py::test_process_restarts_after_being_killed` | ✅ (CI proxy) |
| station-deployment | unit file has no internet-waiting dependency | `test_deploy_unit.py::test_unit_file_has_restart_always_and_no_internet_wait_dependency` | ✅ |
| station-deployment | service starts with no route to the internet | `test_deploy_unit.py::test_service_starts_with_no_route_to_the_internet` | ✅ |
| **station-deployment** | **runbook names the tested image** | (none found) | ❌ **UNTESTED; unmet — WARNING 8 (honestly disclosed)** |

**Compliance summary: 36/41 scenarios compliant.**

### Requirements with no test at all

Named as asked:

1. **clock-trust → "TIME_SYNC resumes once trusted"** — `test_time_sync_resumes_once_trusted`
   constructs a clock that is *already* `RTC` and calls `attempt_time_sync` once. No transition from
   `unset` exists anywhere in the test. It is indistinguishable from
   `test_a_valid_rtc_read_with_no_ntp_yields_rtc` plus a send.
2. **radio-gateway → "link drop surfaces in `/health`"** — `test_link_drop_surfaces_in_health`
   (`tests/adapters/test_serial_gateway.py:175-192`) asserts `gateway.link_state.connected is False`.
   It never constructs an app and never issues a request. `/health` has no link field to assert on.
3. **event-store → "Registry rehydration at boot"** — every covering test calls `store.rehydrate()`
   itself. No test boots the app and asserts the registry was rebuilt.
4. **station-deployment → "runbook names the tested image"** — no test, and `docs/deploy.md` states
   the image is still an open question.

---

## CRITICAL

### CRITICAL 1 — `Store.rehydrate()` has no production caller; the station boots blind

**Where**: `src/feeder_station/app.py:203-275` (`create_app`), `src/feeder_station/cli.py:60-84`
(`build_app`). `src/feeder_station/adapters/store.py:430` defines `rehydrate`.

`rg 'rehydrate' src/` returns only its own definition. The only callers in the repository are
`tests/test_app.py:341`, `tests/test_app.py:628`, `tests/test_app.py:640`,
`tests/adapters/test_store.py:217,246,282`. `create_app` builds
`Registry(silence_after=...)` and never replays the log into it; `cli.build_app` — the real
`ExecStart` path in `deploy/feeder-station.service` — passes `store=` and `gateway=` and no registry.

**Proven through the production entry point**, not inferred. A store holding one heartbeat and one
unacknowledged capture for node 7:

```
REAL BOOT  (cli.build_app -> create_app) -> GET /api/traps = []
TEST PATH  (hand-called store.rehydrate) -> capture = {'tag': '0384199C87EC06', ...}
```

**Which requirements this violates**
- `event-store/spec.md`, "Registry rehydration at boot": *"At startup, the system MUST rebuild
  in-memory trap state (last known reported state, `targets_version`, unacknowledged captures) from
  stored events, without depending on the node re-sending anything."* Nothing rebuilds anything.
- `operator-ui/spec.md`: *"An unacknowledged capture MUST remain visible across page reloads **and
  station restarts** until acknowledged."*
- **S3**, and it is S3's whole second half: *"still there for anyone who opens the interface hours
  later."* `docs/requirements.md` spends a section ("A capture is a state, not an event") on exactly
  this. The row survives in SQLite; nothing puts it back on the page.
- **S1/S2**: `Trap.pending_command` is not rebuilt either, so an ARM or a `SET_TARGETS` issued before
  a restart is never resent. This is the superset of the gap the apply agent flagged (design.md §6's
  third replay step): step 3 is missing *and* steps 1 and 2 never execute in production.
- **S7**: the station restarts by itself, into amnesia.

It also silences the scheduler: `run_scheduler_loop` (`adapters/workers.py:165`) derives `node_ids`
from `registry.traps()`, which is empty after a restart — so no backlog rotation, no command resend
and no `TIME_SYNC` happen until a node spontaneously transmits.

**Why the tests did not catch it**: `tests/test_app.py:339-343` reopens the store, calls
`reopened.rehydrate(registry)`, and then passes that already-populated registry into
`create_app(registry)`. Its docstring says *"Confirms slice 4's `Store.rehydrate()` feeds
`/api/traps` correctly"*. It does not — the test feeds it. Task **10.6** ("GREEN: confirm slice 4's
rehydration path feeds `/api/traps` correctly at startup") is marked `[x]` against code that has no
such path.

**Fix**: call `store.rehydrate(app.state.registry)` in `create_app` before the lifespan yields (it
must run before the HTTP server accepts — design.md §6 says so explicitly), and add a test that boots
through `cli.build_app` and asserts `/api/traps` without ever calling `rehydrate` itself.

---

### CRITICAL 2 — `rehydrate()` regresses reported state and `last_seen` when the capture predates the heartbeat

**Where**: `src/feeder_station/adapters/store.py:448-450` and `475-488`; `domain/traps.py:142-147`.

`rehydrate` runs `_rehydrate_heartbeat` then `_rehydrate_capture` unconditionally, and
`Registry.captured()` overwrites **both** `trap.state = TrapState.FIRED` **and**
`trap.last_seen = at`. When the latest `CAPTURED` is older than the latest `HEARTBEAT`, the capture
replay stamps the trap with the *older* fact.

Measured, with a capture at 03:00 and a heartbeat at 08:58 reporting `idle`, evaluated at 09:00:

```
after rehydrate -> state     = 'fired'   (the node's own latest report was 'idle')
                   last_seen = 2026-09-23T03:00:00+00:00
                   reachable = False      <-- a healthy trap that beat 2 minutes ago
```

**Violates S4** — *"all sourced from the trap's own heartbeat, never from what the station last
commanded"*, and `trap-commands/spec.md`'s *"The trap's displayed state MUST always be whatever its
own most recent `HEARTBEAT` or `HELLO` reported."* Here it is whatever the older `CAPTURED` said.
**Violates S11** — a trap heard from two minutes ago is reported unreachable, i.e. a false fault,
after every restart.

This is latent today only because CRITICAL 1 means `rehydrate` never runs in production. Fixing
CRITICAL 1 alone ships this regression. They must be fixed together.

**Fix**: `_rehydrate_capture` should restore the capture without touching `state`/`last_seen` (a
`Registry` method that sets only `trap.capture`), leaving the reported state to the heartbeat replay.

---

### CRITICAL 3 — `TIME_SYNC` is never retried after being withheld, so it never resumes once trusted

**Where**: `src/feeder_station/adapters/workers.py:162, 186-189`.

```python
synced_node_ids: set[int] = set()
...
for node_id in node_ids:
    if node_id not in synced_node_ids:
        attempt_time_sync(node_id, clock=clock, store=store, gateway=gateway)
        synced_node_ids.add(node_id)
```

`attempt_time_sync` returns `bool` — whether the frame was actually sent. **The return value is
discarded and `synced_node_ids.add(node_id)` runs unconditionally**, so a *withheld* attempt marks
the node as synced for the entire life of the thread.

Sequence: the station boots after a power cut with no internet — the exact scenario S12 exists for —
so `clock.trust()` is `unset`. On the first tick every node is withheld and added to
`synced_node_ids`. NTP comes up twenty minutes later and `trust()` flips to `ntp`. **No node is ever
offered `TIME_SYNC` again.** Every trap's PCF8523 stays unset for the rest of the station's uptime,
and the whole time chain `docs/requirements.md` describes (NTP → station RTC → `TIME_SYNC` → node) is
severed.

**Violates `clock-trust/spec.md`, "Withhold TIME_SYNC when untrusted" → scenario "TIME_SYNC resumes
once trusted"**: *"GIVEN the station's clock state transitions from `unset` to `rtc` or `ntp`, WHEN a
node is next due for `TIME_SYNC`, THEN the frame is sent."* It is not. Traces **S12, S15, D7**.

Measured: over 3 s at a 1 s tick with 10 traps, exactly 10 `TIME_SYNC` (`0x85`) frames were written —
one per node, then never again.

**Why the test did not catch it**: `test_time_sync_resumes_once_trusted`
(`tests/adapters/test_clock.py:108-128`) builds a fresh `_FixedClock(ClockTrust.RTC)` and calls
`attempt_time_sync` once. There is no `unset` state, no transition, and no scheduler. It tests that
a trusted clock sends — which `test_a_valid_rtc_read_with_no_ntp_yields_rtc` already implies — not
that a withheld sync ever resumes.

**Fix**: only record the node as synced when `attempt_time_sync` returns `True`, and add a test that
drives `run_scheduler_loop` across a clock that flips `unset → ntp` and asserts a `TIME_SYNC` frame
appears after the flip. The "once per node per thread lifetime" cadence itself is also an invented
policy (see WARNING 10) and should be decided deliberately.

---

### CRITICAL 4 — the scheduler runs a full rotation every tick, saturating the radio channel (S5)

**Where**: `src/feeder_station/adapters/workers.py:163-190`;
`src/feeder_station/adapters/scheduler.py:100` (`schedule = backlog.start(node_ids)`).

`run_rotation_round` internally loops until **every** node in the roster has settled, and it creates
a **fresh** `RotationSchedule` on every call (`backlog.start(node_ids)`). `run_scheduler_loop` calls
it on **every 1 s tick**. So `domain/backlog.py`'s round-robin state — whose entire purpose is
"whose turn is next" — never survives a tick. Every node's turn is every tick, and the retry budget
resets every second.

Measured on this machine, `retry_budget=3`, `tick_s=1.0`, 10 known traps:

| Roster state | Frames/s | `GET_BACKLOG`/s |
|---|---|---|
| all 10 reachable | 13.3 | 10 |
| none reachable (any roster past `silence_after`) | **40.0** | **30** |

`docs/protocol/messages.md` puts a ~50-byte frame at **0.37 s of airtime at Long Fast** and states
the design budget as *"ten nodes at a couple of hundred events a day keep the channel under 1%
occupied."* 10 `GET_BACKLOG`/s is ≈370% of channel capacity; 30/s is ≈1110%. The station transmits
continuously and no node can be heard.

**Violates S5** (*"Ten traps report and are commanded without the operator waiting on the UI"* — at
this duty cycle they cannot report at all) and contradicts `messages.md`'s own delivery model, which
makes the scheduled turn the **recovery** path, not a 1 Hz sweep: *"A node that does not answer is
skipped and tried next round."* Here it is retried three times a second forever.

It also weakens `radio-gateway/spec.md`'s *"MUST skip a node that does not respond within its retry
budget and continue the rotation"* to a within-one-round guarantee only.

**Why the tests did not catch it**: every rotation test calls `run_rotation_round` **once**
(`test_backlog_rotation.py:113, 143`) and asserts the poll sequence. No test bounds frames per unit
time. `test_run_scheduler_loop_polls_backlog_and_resends_a_due_command`
(`tests/adapters/test_workers.py:213-254`) runs the real loop at `tick_s=0.01` for up to 2 s and
asserts only that `GET_BACKLOG` and `DISARM` *appear* in the written set — it emits several hundred
frames and checks none of it.

**Fix**: hold one `RotationSchedule` across ticks in `run_scheduler_loop` and advance it by one node
per tick (or per `heartbeat_interval`), instead of rebuilding it and sweeping the whole roster each
second. Add a test that asserts an upper bound on frames written per unit time.

---

### CRITICAL 5 — `/health` never reports link state; `SerialGateway.link_state` has no production caller

**Where**: `src/feeder_station/app.py:277-296` (`health()`);
`src/feeder_station/adapters/serial_gateway.py:194-204` (`link_state`).

`/health` returns exactly `ok`, `clock`, `clock_trust`, `workers`. `app.state.gateway` is stored at
`app.py:263` and never read again. `rg '\blink_state\b'` finds the property definition and **nine
test references — and nothing in `src/`.**

**Violates `radio-gateway/spec.md`, "Device resolution and reconnect" → scenario "link drop surfaces
in /health"**: *"THEN `/health` reports link state down within the next poll interval, and the
gateway does not pretend the link is up."* Also design.md §4 (*"**Link state**, surfaced whole in
`/health` and in the UI: `connected`, `device`, `since`, `frames_in`, `frames_out`, `decode_errors`,
`last_error`"*), proposal slice 5 (*"link state in `/health`"*), and the proposal's own risk
mitigation for a renamed serial device (*"surface link state in `/health` and the UI"*).

Operationally: unplug the radio and every trap silently drifts to `reachable: false` with nothing
anywhere saying the *station's own* link is down. `workers.gateway.alive` stays `true` — the thread
is alive and healthily reconnecting forever — so `/health` reports `ok: true` with a dead radio.
That is precisely the "the gateway never pretends the link is up" rule, inverted.

**Why the test did not catch it**: `test_link_drop_surfaces_in_health`
(`tests/adapters/test_serial_gateway.py:175-192`) is named for `/health` and never touches it; it
asserts on the adapter object. Tasks **5.5/5.6** are marked `[x]` and the slice's Done line claims
"`/health` exposes every link field."

**Fix**: add a `link` block to `health()` from `app.state.gateway.link_state`, fold `connected` into
`ok`, render it in `index.html`, and move the covering test to `tests/test_app.py` where it issues a
real request.

---

## WARNING

**WARNING 1 — `ARM.expires_epoch` distributes a time the station may not trust (S12), and invents a
duration the protocol does not pin.**
`adapters/command_sender.py:86-88` computes `expires_epoch = int(now.timestamp() + arm_duration_s)`
with no clock-trust gate. D7/S12 withhold `TIME_SYNC` when `trust()` is `unset`, but `ARM` carries an
absolute epoch too — an unset-clock station arms a trap with an expiry near 1970, which the node
reads as long past. `messages.md` defines `expires_epoch (4, **0 = until disarmed**)`, so the
protocol's own answer for "no meaningful expiry" already exists and is unused. The 24 h
`DEFAULT_ARM_DURATION` (`config.py:43`) is the station's invention, correctly labelled as an
assumption in its comment but never reconciled with `0`. *Fix*: send `0` when
`may_distribute_time(clock.trust())` is false, and raise the 24 h default with the client.

**WARNING 2 — the export presents commanded intentions as "trap-state changes" (S4).**
`adapters/export.py:59-65` sets `EXPORTABLE_MSG_TYPES = (VISIT, CAPTURED, FAULT, OPERATOR_ARM,
OPERATOR_DISARM)`. `OPERATOR_ARM`/`OPERATOR_DISARM` are what an operator *asked for*; a trap may
never have armed. Meanwhile the actual reported arm/disarm transitions live only in `HEARTBEAT`,
which is excluded. So the CSV exports the command and omits the report — the exact inversion S4
forbids, in the file a biologist takes away. The `source` column mitigates it for a careful reader.
*Fix*: either drop the operator rows from the "trap-state-change" set, or derive real state changes
from consecutive `HEARTBEAT.state` values. Either way the spec wording ("trap-state changes") should
be made explicit about which msg_types it means.

**WARNING 3 — the UI labels the desired tag list "Objetivos actuales" (current targets) (S4).**
`static/index.html:113-115, 121` renders `t.targets` — which is `Trap.desired_tags`, set by
`PUT /targets` — as *"Objetivos actuales"*. The node never reports its tag list; the protocol carries
only `targets_version`. `operator-ui/spec.md` requires the interface render *"strictly from the
registry's view — trap-reported data, never what was last commanded."* The adjacent pending line
(`describePending`, line 140) softens it, but the label asserts a fact the station cannot know.
`tests/test_app.py:710-716` PUTs a list and immediately asserts `row["targets"]` equals it with no
heartbeat — the test encodes the misreading. *Fix*: relabel to "Objetivos solicitados (v N)" and mark
it confirmed only when `targets_version == desired_targets_version`.

**WARNING 4 — the registry lock is held across all serial I/O and store queries (design §7
deviation; S5/S10).**
`adapters/workers.py:164-189` wraps the entire scheduler tick — `run_rotation_round`,
`send_due_commands`, every `attempt_time_sync` — in `with registry_lock:`. design.md §7's concurrency
table scopes that lock to `Registry` access only (*"Plain dicts mutated from three threads"*). Every
HTTP route (`app.py:300, 306, 315, 329, 349`) and the gateway thread's `ingest_frame`
(`workers.py:101`) contend on it. At the measured 30-40 writes per tick the UI and frame ingestion
both queue behind the scheduler's serial I/O. No deadlock (the lock order `registry → port` is
consistent), but it is latency S5/S10 pay for. *Fix*: take a snapshot of `node_ids` under the lock,
release it, then do the I/O.

**WARNING 5 — `POST /api/traps/{id}/acknowledge` on an unknown node raises `KeyError` → 500.**
`domain/traps.py:159-160`: `self._traps[node_id].capture = None`. Reproduced:
`POST /api/traps/42/acknowledge` on a fresh app → `KeyError: 42`. `arm` and `disarm` return 200
because `_set_pending` uses `setdefault`. The realistic path is a phone with the page still open
after a station restart: the registry is empty (CRITICAL 1), the operator taps "Atender" and gets a
500. Note `_record_operator_event` has already written the store row by then, so the acknowledgement
is durable but the operator is told it failed. **S13.** *Fix*: make `acknowledge` tolerant of an
unknown node, as `_set_pending` already is.

**WARNING 6 — reachability *is* rehydrated, contradicting design.md §6.**
design.md §6 states plainly: *"Reachability is not rehydrated. Every trap starts unreachable until it
beats again, because after an outage the station genuinely does not know — which is what S11 asks
for."* `store.py:467-473` calls `registry.heartbeat(..., at=heartbeat.received_epoch)`, which sets
`last_seen` and therefore restores reachability from a pre-restart heartbeat. The implementation is
arguably *more* accurate than the design's rule, but the deviation is undocumented. *Fix*: decide one
way and record it; if the implementation wins, amend design.md §6.

**WARNING 7 — `CAPTURED.state_after` is decoded, stored, and then discarded.**
`adapters/ingest.py:107-108` → `registry.captured()` → `traps.py:144` hard-codes
`trap.state = TrapState.FIRED`, ignoring the node's own `state_after` byte.
`wire-codec/spec.md` requires the decoder yield *"`state_after` as reported by the node"* — it does —
but no consumer uses it. A node reporting `CAPTURED` with `state_after = 3` (fault) is displayed as
`fired`. Small S4 nit; `rg state_after src/` shows the field is read nowhere outside `payloads.py`.

**WARNING 8 — `station-deployment` "runbook names the tested image" is unmet and untested.**
The scenario requires the runbook *"states the exact OS image and version the install steps were
verified against."* `docs/deploy.md:25-29` instead says *"This is the literal open question this
runbook exists to eventually close. Nothing below has been installed on a real Pi boot as of this
writing."* That is the honest answer and far better than a fabricated one, but the requirement as
written is not satisfied and nothing tests it. **Blocked on a hardware/ops decision, not on code.**

**WARNING 9 — three tests assert materially less than their names promise.**
- `test_link_drop_surfaces_in_health` — never requests `/health` (CRITICAL 5).
- `test_time_sync_resumes_once_trusted` — exercises no transition (CRITICAL 3).
- `test_ten_simulated_nodes_over_pty_stays_responsive` (`test_backlog_rotation.py:169`) — S5's
  criterion is *"without the operator waiting on the UI"*; the test never constructs an app, never
  requests `/api/traps`, and asserts only that `run_rotation_round` returns. "Responsive" is not
  measured. This is also the exact configuration in which WARNING 4 would show.

**WARNING 10 — two adapter-level policies were invented rather than derived, as the apply agent
flagged. Adjudicated here.**
- **`_recently_reachable` (`workers.py:113-130`) is not sound** as an answer to
  `run_rotation_round`'s `responded` contract. `responded` means "did this node answer *this poll*";
  the function answers "has this node been heard from within `silence_after` (15 min)". The two
  differ in the case that matters: a genuinely silent node returns `False` forever, so it consumes
  its full retry budget on *every* tick — the direct cause of CRITICAL 4's 30 frames/s. The
  reasoning in its docstring (a `GET_BACKLOG` reply is asynchronous) is correct; the conclusion is
  not. The right shape is to record, on the gateway thread, the last tick in which a frame arrived
  per node, and have `responded` compare against *this round*, not against a 15-minute window.
- **The `TIME_SYNC` cadence is unsound as implemented** — see CRITICAL 3. "Once per node per thread
  lifetime" is defensible *for a successful sync*; applying it to a withheld one is the bug.

**WARNING 11 — Strict TDD evidence is complete for slice 13 only; earlier slices are unverifiable
from the record.**
*Corrected during verification*: a TDD Cycle Evidence table **does** exist — in the Engram
`apply-progress` artifact (obs #4987, project `cicg-feeder-project`), not in any openspec file. Its
seven rows for slice 13 were cross-checked against reality and all hold: every named RED test file
exists, every named test passes, and the `_FieldsRow.fields` `@property` fix is present with
`mypy src` clean. `tasks.md` carries `RED:`/`GREEN:`/`REFACTOR:` labels for all 138 tasks and every
named test file exists and passes.

What is missing is a *per-slice* evidence table for slices 1-12 — obs #4986 covers slice 4 and #4987
covers slice 13; the topic key was upserted, so the intermediate slices' evidence has been
overwritten. RED-was-genuinely-red is therefore unverifiable for most of the change. The substance is
strong, but three tasks are demonstrably marked `[x]` against a RED test that does not exercise what
the task claims — **5.5/5.6, 9.9/9.10, 10.6** (CRITICAL 5, 3, 1). That is the concrete cost of the
missing per-slice record.

Also: obs #4987 and `tasks.md` both report the format drift as **5** files; it is **6**
(`ruff format --check --diff` lists `design.md`, `payloads.py`, `test_ingest.py`,
`test_serial_gateway.py`, `test_events.py`, `test_traps.py` — the prose names six and counts five).

---

## SUGGESTION

1. **`domain/payloads.py`'s docstring and inline comments are now stale.** The module says
   *"`messages.md` pins some field widths explicitly ... and leaves others unstated"* and marks ~14
   fields as "assumption". `messages.md` now pins **every** width ("Widths are pinned below and are
   part of the contract"), and its own note records that these widths came *from this module*. The
   bytes agree exactly — verified field by field for all 6 uplink and 7 downlink types — but the
   commentary no longer describes who owns them. Rewrite the docstring to say the widths are now
   contractual.
2. **`decode_heartbeat`'s comment says `clock_source` is a "small enum (0-2)"** (`payloads.py:108`),
   i.e. it still anticipates `2 = GPS`. `messages.md` now says *"`clock_source`: 0 unset, 1 RTC."*
   design.md's open question 4 ("`messages.md` still defines `clock_source: 2 = GPS`") is resolved
   and should be struck.
3. **`adapters/export.py:51-58`'s comment is stale** — it says *"nothing in `app.py` today calls
   `store.append()` for an operator's arm/disarm ... left for whichever slice owns `app.py`'s command
   handlers."* Slice 13 did exactly that. Delete the paragraph.
4. **Dead production code.** No caller in `src/` for: `Store.events_since` (`store.py:416`),
   `Store.integrity_check` (`store.py:275`), `Registry.unacknowledged_captures` (`traps.py:162`),
   `payloads.encode_get_state`, `StationConfig.heartbeat_interval`. Also `Capture.acknowledged`
   (`traps.py:36`) is never set to `True` — `Registry.acknowledge` nulls the whole capture — so
   `unacknowledged_captures` would return every capture if it were ever called.
5. **`Trap.is_reachable`'s 5-minute default** (`traps.py:61`) is the literal that task 6.9/6.10 set
   out to remove. `Registry` always passes `self._silence_after`, so it is unreachable today, but any
   direct caller silently gets 5 minutes instead of the configured 15. Make the parameter required.
6. **design.md §2 specifies `quick_check` at boot, "Reported in `/health`"** — not implemented.
   Fold it in alongside the link state (CRITICAL 5).
7. **No `py.typed` marker** in `src/feeder_station/`. The built wheel ships untyped; `mypy tests`
   reports *"module is installed, but missing library stubs or py.typed marker"* for every
   `feeder_station` import. One empty file fixes it.
8. **`RELEASE_EPOCH_FLOOR`'s comment disagrees with its value** (`domain/clock_trust.py:44-49`): the
   comment says *"fixed at the start of the year this change shipped (2026-09-23)"*, the value is
   `1_735_689_600` = 2025-01-01. Harmless (still well above every unset-RTC default) but confusing.
9. **`Registry.command_arm`/`command_disarm` take an unused `at` parameter** (`traps.py:109-113`).

---

## Ordering audit (S9) — requested explicitly

**S9 holds on every path that writes.** Checked exhaustively, not only `adapters/ingest.py`:

| Path | Order | Verdict |
|---|---|---|
| `adapters/ingest.py:84-100` | `decode → store.append (fsync) → ACK → registry → publish` | ✅ literal, no `try/except` around `append` |
| `app.py::arm` (307-311) | `_record_operator_event → registry → publish` | ✅ |
| `app.py::disarm` (317-321) | same | ✅ |
| `app.py::acknowledge` (330-337) | `store write → registry → publish` | ✅ (event-store's "MUST be a store write" is met) |
| `app.py::set_targets` (361-374) | `store write → registry → publish` | ✅ |
| `adapters/clock.py::attempt_time_sync` (237-249) | withheld row written before returning | ✅ |
| `adapters/command_sender.py` | no durable write; `sent()` advances only after a successful write | ✅ |

**No path acknowledges, confirms or publishes before a durable write.** Duplicates are ACKed, not
dropped (DD8) — `ingest.py` ACKs regardless of `Appended.duplicate_of`, correct. One note: a
duplicate still runs `_apply_to_registry` with the *retransmission's* `received_epoch`, so a replayed
old frame refreshes `last_seen`. Benign, but worth a comment.

**S4 audit**: no path writes a reported state that nobody reported, *except* the two named above —
CRITICAL 2 (capture replay overwrites the heartbeat's state) and WARNING 3 (the UI's "Objetivos
actuales"). WARNING 2 is the same failure in the exported file. `Registry.heartbeat` /
`Command.satisfied_by` (DD15) are correct: ARM by `armed`, DISARM by `not armed`, SET_TARGETS by
version equality, and nothing clears a pending command from having sent a frame.

**S3 audit**: the capture survives in the store on every route (verified above) and is correctly
suppressed after acknowledgement (`_rehydrate_capture` compares `seq`). It does **not** survive into
the registry on any real restart — CRITICAL 1.

## Protocol/document agreement — requested explicitly

`domain/payloads.py` vs `docs/protocol/messages.md`: **the bytes agree exactly.** HELLO 8, HEARTBEAT
16, VISIT 15, CAPTURED 12, FAULT ≥1, ACK 3; ARM 4, DISARM 0, SET_TARGETS header 7, GET_STATE 0,
TIME_SYNC 4, GET_BACKLOG 4. `TAGS_PER_SET_TARGETS_FRAME` computes 27, matching the doc's own
`(200-7)//7 = 27` derivation. Only the *commentary* has drifted — SUGGESTION 1 and 2.

## Assertion quality

No tautologies, no ghost loops, no assertion-without-production-call, no mock-heavy files. Every
empty-collection assertion has a non-empty companion. `tests/domain/test_architecture_purity.py` is
particularly good: AST-based rather than import-based, parametrized per module, so a banned import
fails even if it would import cleanly at runtime.

The defects are not trivial assertions but **scope**: three tests assert less than their names claim
(WARNING 9), and two tests establish their own wiring and then verify it (CRITICAL 1's two
`rehydrate` tests). **Assertion quality: 0 CRITICAL, 5 WARNING** — all five already counted above.

## Test layer distribution

| Layer | Tests | Notes |
|-------|-------|-------|
| Pure/unit (`tests/domain/`) | 63 | byte vectors, table-driven, AST purity guard |
| Adapter (`tests/adapters/`) | 62 | Protocol fakes, real pty (DD16), `kill -9` subprocess proxy, threads |
| App/route (`tests/test_app.py`, `test_cli.py`, `test_config.py`, `test_deploy_unit.py`) | 75 | `TestClient`, subprocess restart harness |
| **Total** | **200** | |

## Hardware gates — outstanding, never counted as green

Carried forward unchanged and correctly disclosed throughout: SD-card flush honesty (S9's literal
criterion); `/dev/serial/by-id` stability (CP2102 vs CH340, board unidentified); a pty is not a
USB-CDC device; the physical DS3231 across an unpowered hour (S15); a literal power-pull (S7); the
target Pi OS image; and any true end-to-end exchange, which needs `station_link` and a gateway ESP32
image that do not exist. `adapters/command_sender.py`'s module docstring states plainly that no real
trap has ever received a frame. That honesty is a genuine strength of this change and should be
preserved through the fixes.

## Verdict

**FAIL** — 5 CRITICAL, 11 WARNING, 9 SUGGESTION. 36/41 spec scenarios compliant; 200/200 tests pass;
`ruff check`, `mypy src` and `uv build` clean.

Do not archive. Return to `sdd-apply` for CRITICAL 1-5. CRITICAL 1 and 2 must be fixed in the same
change — fixing 1 alone introduces 2 as a live S4/S11 regression.
