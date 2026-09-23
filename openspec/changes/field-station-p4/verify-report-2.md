# Verification Report #2 — field station P4 (re-verification of commit `21be80c`)

**Change**: `field-station-p4`
**Phase**: `sdd-verify` (second pass). Date: 2026-09-23.
**Mode**: Strict TDD. Artifact store: `openspec`.
**Baseline**: `openspec/changes/field-station-p4/verify-report.md` (FAIL — 5 CRITICAL, 11 WARNING,
9 SUGGESTION), left untouched as the record of what was wrong.
**Under test**: `21be80c` *fix(field-station-p4): wire rehydration at boot and stop saturating the
radio* — 892 insertions / 76 deletions across 13 files.

**Posture**: adversarial, and deliberately doubly so. The same class of agent wrote both the defects
and their fixes, so nothing here was accepted from a diff, a test name, or a docstring. Every one of
the five original CRITICALs was re-checked by **running the station the way `cli.build_app` does** —
real `build_app`, real `SerialGateway` over a real `pty.openpty()` device, real worker threads, real
HTTP requests — and observing what the station actually did. Every number below was measured on this
machine, not read off an assertion.

**Verdict: FAIL** — **6 CRITICAL, 13 WARNING, 13 SUGGESTION.**

Three of the five original CRITICALs are genuinely, behaviourally fixed. Two are partially fixed.
And the fix pass introduced four new CRITICALs of its own, three of which are the *same defect,
re-created*: CRITICAL 2's state regression was moved rather than removed, CRITICAL 4's airtime
violation was reduced rather than closed, and CRITICAL 1/5's "correct component nobody calls" family
gained a new member in `run_rotation_round`. One is new and worse than anything in the first report:
**a single corrupt byte on the radio now makes the station permanently unbootable.**

---

## Verdict per original CRITICAL

| # | Original finding | Verdict | Evidence |
|---|---|---|---|
| 1 | `Store.rehydrate()` has no production caller | **FIXED** (one hole) | Real `build_app` restart: roster, capture, pending ARM and `desired_tags` all survive. Hole: a node the station has never *heard from* is still dropped → **NEW CRITICAL 6** |
| 2 | Capture replay regresses `state`/`last_seen` | **PARTIALLY FIXED** | The named ordering is fixed. The mirror ordering, capture-only, and `FAULT` all regress now → **NEW CRITICAL 1** |
| 3 | `TIME_SYNC` never resumes once trusted | **FIXED** | Real `run_scheduler_loop`, clock flipped `unset→ntp` mid-run: all 3 nodes resynced. Cost: an unbounded withheld-log → **NEW CRITICAL 4** |
| 4 | Scheduler saturates the radio | **PARTIALLY FIXED** | Rotation persists across ticks ✅, retry budget not reset ✅, `GET_BACKLOG` paced to 0.0033 f/s ✅. Station total still **11.68 % occupancy** → **NEW CRITICAL 3**; `run_rotation_round` now dead → **NEW CRITICAL 5** |
| 5 | `/health` never reports link state | **FIXED** | Real HTTP `GET /health` against a real pty that disappears: `connected:false`, `ok:false`, full link block, 0.21 s |

---

## Completeness

| Metric | Value |
|--------|-------|
| Tasks total | 138 |
| Tasks complete (`[x]`) | 138 |
| Tasks incomplete | 0 |
| Tasks added by the fix pass | **0** |

`tasks.md` was deliberately not touched by `21be80c` (the apply agent's own `apply-progress` record
says so). The consequence is that the tasks artifact now **describes code that no longer exists**:
slice 12's Done line still claims *"All four orphaned drivers (gateway reader, `run_rotation_round`,
`due_for_resend`/`sent`, `attempt_time_sync`) actually run, proven end to end"* — `run_rotation_round`
has not run in production since `21be80c`. See WARNING 14.

Three tasks the first report called out as marked-done-against-wrong-code are now honestly done:
**5.5/5.6** (`/health` link fields), **9.9/9.10** (`TIME_SYNC` resumes), **10.6** (rehydration feeds
`/api/traps` at startup). That is real progress and should be recorded as such.

## Build & Tests Execution

**Tests**: ✅ `uv run pytest` → **208 passed**, 2 warnings, 4.83 s, exit 0. (200 baseline + 8 new.)

**Lint**: ✅ `uv run ruff check .` → `All checks passed!`

**Format**: ⚠️ `uv run ruff format --check .` → **6 files would be reformatted, 59 already formatted**.
Unchanged pre-existing baseline; still the same six files the first report named.

**Types**: ✅ `uv run mypy src` → `Success: no issues found in 22 source files`.
⚠️ `uv run mypy tests` → **100 errors** (was 95; the +5 are `type: ignore[arg-type]` on the new
gateway doubles). The agreed gate is `src`; recorded, not counted.

**Build**: ✅ `uv build` → `feeder_field_station-0.1.0.tar.gz` + `-py3-none-any.whl`.

**Coverage**: ➖ still no coverage tool configured. Not a failure.

---

## Part 1 — is each of the five really fixed, behaviourally?

Everything in this section was run against `src/` at `21be80c`. Harnesses are in the session
scratchpad; each is described precisely enough to re-derive.

### 1. Does a capture, a pending command and the trap roster survive a restart? — **YES**

Boot 1: `cli.build_app(["--db-path", db, "--serial-device", /dev/pts/N])`, entered through
`with TestClient(app)` so the real ASGI lifespan runs and the real gateway + scheduler threads start.
Two `HEARTBEAT` frames and one `CAPTURED` frame were COBS-framed and written to the pty master, i.e.
they arrived through `SerialGateway.read_frames()` → `ingest_frame()`, the production ingest path.
Then `POST /arm 9`, `PUT /targets 7`, `POST /arm 42`. Boot 1 shut down (lifespan `finally`,
`store.close()`).

Boot 2: a fresh `build_app` on the same `station.db`. Nothing anywhere called `store.rehydrate()`.

```
VERDICT roster      : SURVIVED   (nodes 7 and 9 both present)
VERDICT capture n7  : SURVIVED   (tag 0384199C87EC06)
VERDICT pending n9  : SURVIVED   (arm)
VERDICT targets n7  : SURVIVED   (['0384199C87EC06'], pending=set_targets)
VERDICT pending n42 : LOST       -- node never heard from, no node_cursor row
```

CRITICAL 1 is genuinely fixed. `create_app`'s lifespan calls `store.rehydrate(app.state.registry)`
*before* the worker threads start and before `yield`, which is the correct order for both reasons
design.md §6 gives. The one surviving hole is CRITICAL 6 below.

### 2. Does rehydration still regress `state` / `last_seen`? — **YES, in the opposite direction**

Both orderings were driven through `cli.build_app`, and for each the **rehydrated** registry was
compared against the **live** registry built by replaying the identical event log through
`adapters/ingest.py::_apply_to_registry` — i.e. what the station itself would be showing had it not
restarted. Identical log, two paths, one answer expected.

```
A. capture 6h OLD, heartbeat 2min NEW   (the ordering the first report named)
     state      live=idle     rehydrated=idle                             AGREE

B. heartbeat 6h OLD, capture 2min NEW   (the MIRROR ordering)
     state      live=fired    rehydrated=idle                          <-- DIVERGES
     last_seen  live=16:12:17 rehydrated=10:14:17                      <-- DIVERGES
     reachable  live=True     rehydrated=False                         <-- DIVERGES

C. capture 30s ago, no heartbeat at all
     state      live=fired    rehydrated=idle                          <-- DIVERGES
     last_seen  live=16:13:47 rehydrated=None                          <-- DIVERGES
     reachable  live=True     rehydrated=False                         <-- DIVERGES

D. heartbeat, then FAULT (the node's own latest report)
     state      live=fault    rehydrated=idle                          <-- DIVERGES
     last_seen  live=16:13:17 rehydrated=16:04:17                      <-- DIVERGES
```

Case A is fixed. Cases B, C and D are broken **by the fix**: under the old code B and C were correct
(`captured()` stamped the capture's own fact) and only A was wrong. The fix traded one wrong ordering
for three. See CRITICAL 1.

### 3. Does `TIME_SYNC` resume for every node after the clock becomes trusted? — **YES**

The production `run_scheduler_loop` was driven on a real thread with a real `Store`, three known
traps, and a `Clock` whose `trust()` starts `UNSET` and is flipped to `NTP` mid-run. Only the two
hardware ports (clock probes, serial write) are doubles; the loop is the shipped one.

```
PHASE 1 -- clock UNSET, 3 s:      TIME_SYNC frames sent: 0                   (correct, S12/D7)
PHASE 2 -- flipped UNSET -> NTP:  TIME_SYNC delivered to nodes: [1, 2, 3]
VERDICT CRITICAL 3: FIXED -- every node resynced
```

Every node, not just the first. Genuinely fixed. The bill for it is CRITICAL 4.

### 4. Scheduler frames/s and airtime occupancy — **still over budget by 11.7×**

`docs/protocol/messages.md`: *"A visit event is ~50 bytes on the wire, 0.37 s of airtime at Long
Fast; ten nodes at a couple of hundred events a day keep the channel under 1% occupied."*
1 % of the channel at 0.37 s/frame is **0.027 frames/s**.

**(a) Rotation persistence and retry budget — both correct.** Production `run_scheduler_loop`,
`heartbeat_interval=0.5 s`, `retry_budget=3`, 3 silent nodes:

```
GET_BACKLOG poll order: [1, 1, 1, 2, 2, 2, 3, 3, 3]
```

Exactly `retry_budget` attempts per node before advancing, accumulated **across ticks**, then the
rotation moves on. Both halves of CRITICAL 4's mechanism are fixed: `RotationSchedule` survives the
tick and the retry count is not reset.

**(b) `GET_BACKLOG` rate — within budget.** Real `build_app` boot, 10 traps, default config:
one `GET_BACKLOG` at the first tick, then nothing for `heartbeat_interval`. That is **0.0033 f/s =
0.12 % occupancy**, matching the fix's own claim. ✅

**(c) Station total transmit rate — 11.68 % occupancy, measured.** Real `build_app` boot, real pty,
10 traps, all armed, none confirming; the 1/2/4/8/16 s backoff burst was allowed to pass, then 95 s
were measured off the wire:

```
STEADY STATE: 10 traps holding an unconfirmed ARM, window 95.0s
   ARM             30 =  0.3158 frames/s
   TOTAL           30 =  0.3158 frames/s
   airtime occupancy = 11.68%     (messages.md budget: under 1%)
```

`send_due_commands` is still called on **every 1 s tick** with no pacing beyond
`Command.due_for_resend`'s 30 s cap. One trap alone is 1/30 s = 0.0316 f/s = **1.17 %** — a single
unconfirmed command already exceeds the whole channel budget. See CRITICAL 3.

### 5. Does `/health` report a dropped link through a real HTTP request? — **YES**

Real `build_app`, real pty, real `GET /health` through `TestClient`. The "unplug" is closing the pty
master fd, which makes the device node genuinely disappear.

```
LINK UP   -> ok=True   link.connected=True  device=/dev/pts/2  frames_in=1
   {"connected":true,"device":"/dev/pts/2","since":...,"frames_in":1,"frames_out":0,
    "decode_errors":0,"last_error":null}
   ...radio unplugged...
LINK DOWN -> detected after 0.21s
   ok=False  link.connected=False
   last_error="[Errno 2] could not open port /dev/pts/2: ... No such file or directory"
   workers: gateway alive=true, scheduler alive=true
VERDICT CRITICAL 5: FIXED
```

All seven fields design.md §4 names are present, `connected` folds into `ok`, and a station with no
gateway configured reports `link: null` / `ok: true` rather than a fabricated `connected: true`.
This one is fixed properly, including the operational point the first report actually cared about:
`ok` is now `false` with a dead radio and two healthy threads.

---

## Part 2 — what the fixes broke

### Did rehydration-at-boot interact badly with the workers or the registry lock?

**Ordering: correct.** `store.rehydrate()` runs before `start_workers()` and before `yield`, so no
thread exists yet and no lock is needed. The rehydrated roster is visible to the scheduler's very
first tick — confirmed: in boot 2 above, the scheduler immediately began resending the rehydrated
pending ARM. No deadlock, no race, in any run.

**Lock contention: measured, acceptable on this hardware.** A contending thread taking
`registry_lock` every 20 ms while the scheduler ran its full tick (10 traps, unset clock, i.e. the
worst case — 10 fsync'd store writes per tick inside the lock):
`494 samples, max 0.7 ms, p95 0.0 ms, mean 0.02 ms`. On this machine's NVMe. On the Pi's SD card an
fsync is one to two orders of magnitude slower, and that same tick would hold the lock for
50–500 ms — see WARNING 4, which the fix made materially worse rather than better.

**New failure mode: rehydration has no error containment.** See CRITICAL 2. This is the single most
serious finding in this report.

### Is anything else now unreachable in production, or left half-wired?

The "does this symbol have any non-test caller" sweep was re-run across `src/` — AST-based this
time, not `rg` — collecting every function, class and upper-case constant defined under
`src/feeder_station/`, then counting `Name`/`Attribute`/`alias` loads across `src/` and separately
across `tests/`, plus a literal scan of `static/index.html` for anything the UI reaches.

| Symbol | Defined | Callers in `src/` | Callers in `tests/` |
|---|---|---|---|
| **`run_rotation_round`** | `adapters/scheduler.py:71` | **0 — newly dead** | 4 |
| `Store.events_since` | `adapters/store.py:423` | 0 | 4 |
| `Store.integrity_check` | `adapters/store.py:282` | 0 | **0 — now dead everywhere** |
| `Registry.unacknowledged_captures` | `domain/traps.py:192` | 0 | 2 |
| `payloads.encode_get_state` / `GET_STATE` | `domain/payloads.py:222/37` | 0 | 1 |

`StationConfig.heartbeat_interval`, dead in the first report, is now the scheduler's pacing input —
resolved. Everything else in SUGGESTION 4 stands, and `run_rotation_round` joins it: **the fix for
CRITICAL 4 created a new instance of the exact defect family CRITICAL 1 and 5 were** (see
CRITICAL 5). `FIRE`/`PING`/`FIRE_FAILED`/`RELEASE_STATE`/`BROADCAST_NODE_ID`/`FLAG_RETRANSMIT` are
protocol constants the station documents as seen-in-passing, not defects. `_tags_are_well_formed`
(pydantic validator) and `export` (route decorator) are framework-registered — false positives.

### Can `Registry.restore_capture` / `restore_desired_tags` be called from a route by mistake?

Not today — no caller outside `adapters/store.py::rehydrate`. But both are plain public methods on
the same `Registry` object every route already holds as `app.state.registry`, they sit immediately
below `captured()` in the file, and both `self._traps.setdefault(node_id, Trap(...))`. A route
calling `restore_capture` by mistake would silently *create* a trap the station has never heard from
and bypass the `state`/`last_seen` update that makes a live capture visible. Low likelihood, silent
failure mode. SUGGESTION 10.

### Is the new `last_frame_arrival` dict shared safely across threads?

**Yes.** Writes happen through `record_arrival=last_frame_arrival.__setitem__`, called inside
`ingest_frame` which `run_gateway_loop` invokes inside `with registry_lock`. Reads happen in
`run_scheduler_loop` inside `with registry_lock`. Same lock, both sides, consistently — and the
underlying `dict.__setitem__`/`dict.get` on `int` keys are atomic under the GIL regardless. It is
keyed by `node_id`, so it is bounded by roster size and cannot grow without limit. One real problem
remains, and it is not a race: the parameter defaults to `None`, which silently degrades the loop.
See WARNING 13.

---

## CRITICAL

### CRITICAL 1 — rehydration regresses the reported state whenever the node's latest report is a `CAPTURED` or a `FAULT` (the mirror of the old CRITICAL 2)

**Where**: `src/feeder_station/adapters/store.py:468-512` (`rehydrate`, `_rehydrate_heartbeat`,
`_rehydrate_capture`); `src/feeder_station/domain/traps.py:149-167` (`Registry.restore_capture`).

The fix reads the S4 rule as *"`state`/`last_seen` are the heartbeat replay's fact alone"*
(`store.py:449-450`, `traps.py:153-156`). That is the misreading. S4's fact is the trap's **most
recent report** — and a `CAPTURED` and a `FAULT` are reports, made by the node, about itself. The
live path already treats them that way (`ingest.py:119-123` → `registry.captured()` /
`registry.fault()`, both of which set `state` and `last_seen`). Rehydration now does not. The same
event log therefore produces two different answers depending on whether the station has restarted:

| Scenario | Live registry | Rehydrated registry |
|---|---|---|
| HB 6 h old, `CAPTURED` 2 min ago | `fired`, `reachable: true` | **`idle`, `reachable: false`** |
| `CAPTURED` 30 s ago, no HB ever | `fired`, `reachable: true` | **`idle`, `last_seen: null`, `reachable: false`** |
| HB 10 min old, `FAULT` 1 min ago | `fault`, `last_seen` = fault | **`idle`, `last_seen` = the old HB** |

`FAULT` is not replayed at all — `rehydrate` only ever consults `latest(HEARTBEAT)`,
`latest(CAPTURED)` and the operator rows.

**Which requirements this violates**
- `event-store/spec.md`, "Registry rehydration at boot": *"At startup, the system MUST rebuild
  in-memory trap state (**last known reported state**, `targets_version`, unacknowledged captures)."*
  In all three rows above the last known reported state is not what is rebuilt.
- `radio-gateway/spec.md`, "FAULT and silence handling": *"On a `FAULT` uplink, the system MUST
  update the affected trap's reported state to fault."* Across a restart it does not.
- **S4** and `trap-commands/spec.md`'s *"The trap's displayed state MUST always be whatever its own
  most recent `HEARTBEAT` or `HELLO` reported"* — here it is an older heartbeat, while a newer report
  exists in the log.
- **S11**, and it is the *same* false-fault the original CRITICAL 2 named, just triggered by the
  opposite ordering: a trap heard from two minutes ago comes back `reachable: false`.
- **S3**, in the direction that matters most. Row 1 is a trap that **fired and is holding an animal**,
  shown as `idle` and unreachable. The capture card still renders — so the page contradicts itself,
  one row saying "capture, tag 0384199C87EC06" and the state chip beside it saying "idle". The first
  report's CRITICAL 2 was a false *alarm*; this is a false *all-clear*, on the one event the whole
  system exists to surface.

**How long the wrong answer lasts**: row 1 until the trap's next heartbeat (up to
`heartbeat_interval`, 5 min by default) — but indefinitely if the capture is the last thing the trap
ever manages to send, which is exactly the trap-broke-while-firing case. Rows 2 and 3 are unbounded.

**Why the test did not catch it**: `tests/test_cli.py::test_an_older_capture_does_not_regress_a_newer_
heartbeats_state` fixes the capture at `now - 6 h` and the heartbeat at `now - 120 s` — only ordering
A. No test anywhere puts a `CAPTURED` or a `FAULT` after the latest `HEARTBEAT` and rehydrates.
Worse, `tests/adapters/test_store.py::test_registry_reflects_last_known_state_after_restart` had its
assertion *changed* by this fix from `TrapState.FIRED` to `TrapState.ARMED` — the apply record calls
the old assertion "the CRITICAL 2 bug encoded as a passing assertion", which is true for ordering A
and is precisely what hid ordering B.

**Fix**: rehydrate `state`/`last_seen` from the node's most recent **state-bearing** row by store
`seq` — `MAX(seq) WHERE msg_type IN (HEARTBEAT, CAPTURED, FAULT)` — and apply that one row's fact,
then restore the unacknowledged capture separately with `restore_capture` as now. `state_after`
(WARNING 7) is the right source for a replayed `CAPTURED`'s state. Add a test per ordering, and one
that asserts *live and rehydrated agree for the same log*, which is the invariant that actually
holds all four cases at once.

---

### CRITICAL 2 — one corrupt `HEARTBEAT.state` byte now makes the station permanently unbootable

**Where**: `src/feeder_station/app.py:227-228` (the new lifespan call);
`src/feeder_station/adapters/store.py:217-221` (`trap_state_from_wire`), `:488-494`;
`deploy/feeder-station.service` (`Restart=always`, `RestartSec=2`).

`trap_state_from_wire` raises `ValueError` for any `state` byte outside 0–3. Nothing validates that
byte before it is stored: `frames.decode` checks only the header and the declared length, COBS is
framing and **not a checksum** (`domain/serial_framing.py:24` says so in as many words), the frame
carries no CRC, and `payloads.decode_heartbeat` reads `payload[0]` as-is. `interpret()` accepts it.
`ingest_frame` then calls `store.append()` **before** `_apply_to_registry` — so the bad row is
fsync'd to disk first and only then does the raise happen.

Before `21be80c` that raise escaped into `run_supervised`, which killed the gateway thread and
recorded it: the HTTP interface stayed up, the UI stayed up, and `/health` reported
`workers.gateway.alive: false`. Contained, visible, recoverable.

Now `store.rehydrate()` runs inside `create_app`'s lifespan with no `try`. Reproduced end to end:

```
interpret() accepts a HEARTBEAT with state=7 : {'state': 7, 'battery_mv': 4000, ...}
row is durably stored (this is what ingest_frame does BEFORE touching the registry)

Now boot the station through cli.build_app on that store:
   STATION FAILED TO BOOT: ValueError HEARTBEAT state byte 7 has no known TrapState
   File ".../adapters/store.py", line 221, in trap_state_from_wire
```

The ASGI startup raises, uvicorn exits, systemd restarts after 2 s, rehydration reads the same row,
raises again. **An infinite crash loop, self-perpetuating, with no `/health`, no UI, and no way for a
biologist in the field to recover without hand-editing SQLite.**

**Which requirements this violates**
- **S7** — *"Starts by itself. A power cut must not need a person."* The station now has a durable
  state from which it can never start by itself again.
- `station-deployment/spec.md`, "Starts by itself after a power cut": the unit's own comment argues
  *"the whole reason `Restart=always` is safe here rather than a corruption generator is S9's own
  fsync discipline."* That argument covers a torn write. It does not cover a well-formed row the
  boot path refuses to read, which is what this is.
- **S9's inversion**: durability is now a liability. The more reliably the station stores, the more
  permanently it is bricked.

**Trigger realism**: the protocol has no integrity check below ESP-NOW, the module docstring says so,
and `read_frames`'s own `decode_errors` counter exists because corrupt blocks are expected. One
undetected bit-flip in one payload byte is enough. The same shape exists for `_tags_from_fields`'s
`bytes.fromhex` on a malformed stored tag.

**Fix**: two independent guards, both needed. (1) Make rehydration total — skip and count a row it
cannot interpret rather than propagate, surfacing the count in `/health`, the same best-effort
discipline `_apply_to_registry` already gets for the live path. (2) Validate `state`/`state_after` at
the decode boundary so an out-of-range byte is dropped like any other undecodable frame and never
reaches the store at all. A boot that cannot read one row must still be a boot.

---

### CRITICAL 3 — the station still transmits at 11.68 % airtime occupancy; only `GET_BACKLOG` was paced

**Where**: `src/feeder_station/adapters/workers.py:233-239` (`send_due_commands` called every tick);
`src/feeder_station/domain/commands.py:34, 76-85` (`RESEND_BACKOFF_S` capped at 30 s).

CRITICAL 4's fix paced exactly one of the scheduler's three transmitters. `send_due_commands` is
still invoked on every 1 s tick, unpaced; its only throttle is each `Command`'s own backoff, which
caps at 30 s and then repeats forever, because a command clears only when the trap's own heartbeat
confirms it (DD15 — correct, and not in question here).

Measured through the production boot path, off the wire, after the initial burst had settled:

| Roster state | Frames/s | Airtime occupancy | Budget |
|---|---|---|---|
| 10 traps, unconfirmed ARM each | **0.3158** | **11.68 %** | ~1 % |
| 1 trap, unconfirmed ARM | 0.0316 | **1.17 %** | ~1 % |
| 10 traps, idle, paced `GET_BACKLOG` only | 0.0033 | 0.12 % | ~1 % |

**A single trap holding one unconfirmed command already exceeds the entire channel budget**, and ten
exceed it by 11.7×, continuously, for as long as the command stays unconfirmed — which is forever if
the trap is out of range or its radio is dead.

**Which requirements this violates**
- `docs/protocol/messages.md`'s stated design budget, *"keep the channel under 1 % occupied"*, and
  its delivery model: the scheduled turn is *"the recovery path"*, not a continuous transmitter.
- **S5** — *"Ten traps report and are commanded without the operator waiting."* This is the first
  report's own deadlock argument, unchanged and still live: the resends saturate the channel that the
  confirming heartbeats need to arrive on, so the condition that would stop the resends is the one
  the resends prevent.

**Why the tests did not catch it**: `test_default_backlog_pacing_holds_the_stations_own_airtime_budget`
(`tests/adapters/test_workers.py`) is the only test that names the budget, and it **measures nothing**
— it computes `1/DEFAULT_HEARTBEAT_INTERVAL.total_seconds() * 0.37 < 0.01` from two module constants,
never constructs a station, never counts a frame. It is arithmetic asserting arithmetic. Its own
docstring scopes it honestly to *"backlog polling's own contribution"*; nothing anywhere bounds the
station's **total** transmit rate, which is the only quantity the channel budget is about. See
WARNING 9.

**Fix**: pace the whole transmitter, not one of its three sources — a single station-wide frame
budget (a token bucket, or one downlink per tick at most) that `send_due_commands`, `poll_node` and
`attempt_time_sync` all draw from, so no future driver can bypass it either. Raise the 30 s resend
cap toward `heartbeat_interval`, since a command cannot possibly be confirmed faster than the trap
heartbeats. Add a test that measures the station's real total frames/s through `build_app` and
asserts the budget.

---

### CRITICAL 4 — an unset clock now writes 864,000 fsync'd rows a day; the CRITICAL 3 fix removed the only bound on the withheld log

**Where**: `src/feeder_station/adapters/workers.py:240-247`;
`src/feeder_station/adapters/clock.py:237-249` (`attempt_time_sync` writes a
`STATION_TIME_SYNC_WITHHELD` row before returning `False`).

Before the fix, `synced_node_ids.add(node_id)` ran unconditionally — the bug, but also the bound:
exactly one withheld row per node, ever. The fix correctly stopped marking a withheld node as synced,
and put nothing in its place. Every unsynced node is therefore re-attempted on **every tick**, and
every withheld attempt writes one `synchronous=FULL` row. Measured at production settings
(`tick_s=1.0`, 10 traps, clock `unset`):

```
STATION_TIME_SYNC_WITHHELD rows written: 100 in 10.0s  = 10.0 fsync'd rows/s
extrapolated: 36,000 rows/hour, 864,000 rows/day
station.db after checkpoint+close: 218 bytes/row -> ~189 MB/day of growth
```

design.md §2's own sizing says the store grows *"~350 MB/year"*. This is **~189 MB per day** — 197×
the design's annual budget, every day — plus 864,000 fsyncs/day against a Raspberry Pi SD card.

The trigger is not an edge case: it is the S12 headline scenario. *"With no internet and no RTC after
a power cut"* the clock is `unset`, and it stays `unset` for as long as the site has no route out —
which `docs/requirements.md` describes as normal. The station survives the power cut exactly as S7
requires and then fills its own card.

**Which requirements this violates**
- `clock-trust/spec.md` requires the station *"MUST log each withheld send."* It does. The defect is
  that the scheduler now makes every node "due" once per second, so "each withheld send" is
  unbounded. The spec pins the log, not the cadence; the cadence is the implementation's to choose
  and it chose 1 Hz per node by omission.
- **S9**, indirectly but seriously: a full card cannot commit, and *"never loses an event"* depends
  on that commit.
- **S7** — the station stops starting by itself once the filesystem is full.

It also makes WARNING 4 materially worse: those ten fsyncs happen **inside `registry_lock`**, once
per tick, blocking `/api/traps` and frame ingestion for as long as the SD card takes.

**Why the test did not catch it**: `test_time_sync_resumes_once_the_scheduler_loop_sees_a_trusted_clock`
uses one node and asserts `store.latest(3, STATION_TIME_SYNC_WITHHELD) is not None` — presence, never
count. Nothing bounds withheld-row volume anywhere.

**Fix**: give the withheld path its own retry cadence — attempt `TIME_SYNC` for an unsynced node at
most once per `heartbeat_interval`, or log the withholding once per node per transition into `unset`
rather than once per attempt. Either preserves CRITICAL 3's actual fix (the node stays *eligible*)
without the 1 Hz write amplification. Add a test that asserts an upper bound on rows/s with an unset
clock.

---

### CRITICAL 5 — `run_rotation_round` now has no production caller; `radio-gateway`'s silent-node scenario is covered only by tests of dead code

**Where**: `src/feeder_station/adapters/scheduler.py:71-116`;
`tests/adapters/test_backlog_rotation.py` (4 tests).

The AST sweep across `src/` finds zero callers. The fix's own docstring states it plainly —
*"`run_rotation_round` itself is untouched and still fully tested … this loop simply no longer calls
it"* — and the renamed pty test repeats it. The disclosure is honest. The consequence is not
acceptable: this is the **same defect family** as the first report's CRITICAL 1 and CRITICAL 5, a
component that is correct in isolation with nothing connecting it to the running station, re-created
by the fix for those very findings. 46 lines of adapter code ship in the wheel and never execute.

The concrete cost is traceability. `radio-gateway/spec.md`'s scenario *"one silent node does not
block the rotation"* was mapped in the first report to
`test_run_rotation_round_visits_every_node_and_retries_the_silent_one`. That test now exercises a
function the station never calls. **Nothing tests the live scheduler for this scenario**: the
replacement, `test_backlog_rotation_persists_across_ticks_and_is_paced`, counts frames and asserts
`frames_per_second < 15` — it never inspects the poll order, never checks that a silent node is
skipped after its budget, and never checks that the schedule persisted, despite its name.

I verified the behaviour myself and **the production loop is correct** — poll order
`[1,1,1,2,2,2,3,3,3]` at `retry_budget=3`. So this is a coverage and dead-code finding, not a
behavioural one. It is CRITICAL because the skill's gate is explicit (a spec scenario with no passing
covering test against the production path is CRITICAL `UNTESTED`) and because leaving it unfixed is
how the first three CRITICALs happened.

**Fix**: either delete `run_rotation_round` and move its four tests onto `run_scheduler_loop`, or
call it from the loop as the "catch-up sweep" primitive its docstring claims it is. Then assert the
poll *order* from the production loop, not just the frame count.

---

### CRITICAL 6 — an operator command to a node the station has never heard from is still silently lost on restart

**Where**: `src/feeder_station/adapters/store.py:473-475` (`_known_node_ids` reads `node_cursor`);
`:350-363` (`node_cursor` is only ever written `if event.source == "node"`).

`rehydrate` iterates `SELECT node_id FROM node_cursor`, and that table is populated exclusively by
node-sourced events. A trap the station has issued a command to but never received a frame from has
no row there, so none of the three replay steps ever run for it. Measured, in the same real-boot
restart as Part 1.1:

```
boot1 POST /arm 42 (never heard) -> {'node_id': 42, 'pending_command': 'arm'}
boot1 /api/traps                 -> node 42 present, pending_command: arm
boot2 /api/traps                 -> node 42 ABSENT
VERDICT pending n42 : LOST -- node never heard from, no node_cursor row
```

The `OPERATOR_ARM` row is durably in `event`; nothing reads it. The live station *does* act on such a
command — `send_due_commands` iterates `registry.traps()` and transmits ARM frames to node 42 — so
this is not a node the station considers imaginary. It is only the restart that forgets.

**Which requirements this violates**
- `event-store/spec.md`, "Registry rehydration at boot": *"without depending on the node re-sending
  anything"* — here it depends on the node having sent something, ever.
- **S1/S2**, and this is the unfixed remainder of the first report's CRITICAL 1, which named exactly
  this: *"an ARM or a `SET_TARGETS` issued before a restart is never resent."* Fixed for known nodes,
  still broken for new ones.
- design.md §6 step 3 as written applies to *"each `node_id` in `node_cursor`"*, so the design shares
  the defect — the code is faithful to a design that is wrong here.

**Realistic path**: commissioning. An operator configures target lists for a batch of traps before or
while they are being deployed, the station restarts (or systemd restarts it), and the whole
configuration is gone with no error and no trace in the UI.

**Fix**: derive the roster from `SELECT DISTINCT node_id FROM event`, or union `node_cursor` with the
distinct node ids of operator rows. Amend design.md §6 to match. Add a test that commands an unknown
node, restarts through `build_app`, and asserts the command is still pending.

---

## WARNING

Nine of the original eleven still stand. WARNING 9 and WARNING 10 are resolved. Four are new.

**WARNING 1 — `ARM.expires_epoch` distributes a time the station may not trust (S12). STANDS,
unchanged.** `adapters/command_sender.py:86-88` still computes
`int(now.timestamp() + arm_duration_s)` with no clock-trust gate, while `messages.md` already defines
`expires_epoch (4, 0 = until disarmed)`. An `unset`-clock station withholds `TIME_SYNC` (correctly,
and now reliably, per CRITICAL 3's fix) and in the same tick transmits an `ARM` carrying an absolute
epoch derived from an untrusted clock. *Fix*: send `0` when `may_distribute_time(clock.trust())` is
false; reconcile the invented 24 h `DEFAULT_ARM_DURATION` with `0` and with the client.

**WARNING 2 — the export presents commanded intentions as "trap-state changes" (S4). STANDS,
unchanged.** `adapters/export.py:59-65` still exports `OPERATOR_ARM`/`OPERATOR_DISARM` while
excluding `HEARTBEAT`, so the CSV a biologist takes away carries the command and omits the report.

**WARNING 3 — the UI labels the desired tag list "Objetivos actuales" (S4). STANDS, unchanged.**
`static/index.html:137` still renders `Trap.desired_tags` as *"Objetivos actuales"*. Verified live:
`PUT /api/traps/44/targets` on a node that has never reported anything immediately renders
`targets: ['0384199C87EC06']`. The station is asserting what a trap is hunting on the strength of
nothing but its own request. *Fix*: relabel to "solicitados (v N)" and mark confirmed only when
`targets_version == desired_targets_version`.

**WARNING 4 — the registry lock is held across all serial I/O and store writes. STANDS, and the fix
made it worse.** `adapters/workers.py:212-247` still wraps the whole tick — `poll_node`,
`send_due_commands`, every `attempt_time_sync` — in `with registry_lock`. CRITICAL 4 adds up to ten
`synchronous=FULL` store writes *inside* that lock on every tick with an unset clock. Measured on
this machine's NVMe the contention is benign (max 0.7 ms over 494 samples); on the target SD card,
where an fsync is 5–50 ms, the same tick holds the lock for 50–500 ms and both `/api/traps` and frame
ingestion queue behind it. *Fix*: snapshot `node_ids` under the lock, release, then do the I/O.

**WARNING 5 — `POST /api/traps/{id}/acknowledge` on an unknown node raises `KeyError` → 500. STANDS.**
Re-verified against the current build:

```
POST /api/traps/42/acknowledge (unknown node) -> 500
POST /api/traps/42/arm                        -> 200
POST /api/traps/43/disarm                     -> 200
PUT  /api/traps/44/targets                    -> 200
```

`domain/traps.py:189-190` still indexes `self._traps[node_id]` directly while `_set_pending` uses
`setdefault`. The realistic trigger the first report named (empty registry after a restart) is
narrowed by CRITICAL 1's fix, but the route still 500s and the store row is already written by then,
so the operator is told an acknowledgement failed that actually succeeded. **S13.**

**WARNING 6 — reachability *is* rehydrated, contradicting design.md §6. STANDS, unchanged.**
`store.py:488-494` still calls `registry.heartbeat(..., at=heartbeat.received_epoch)`, which restores
`last_seen` and therefore reachability, against design.md:303's flat *"Reachability is not
rehydrated."* Decide one way and record it.

**WARNING 7 — `CAPTURED.state_after` is decoded, stored, and then discarded. STANDS, and it is now
load-bearing.** `rg state_after src/` still finds it only inside `payloads.py`. This is no longer a
nit: `state_after` is precisely the field a correct `_rehydrate_capture` needs in order to fix
CRITICAL 1 without guessing.

**WARNING 8 — `station-deployment`'s "runbook names the tested image" is unmet and untested. STANDS,
unchanged.** `docs/deploy.md:24-28` still says the image *"is the literal open question this runbook
exists to eventually close."* Honest, and blocked on a hardware/ops decision, not on code.

**WARNING 9 (was: three tests assert less than their names promise) — RESOLVED, and replaced.** All
three were genuinely dealt with: `test_link_drop_surfaces_in_health` now issues a real `/health`
request from `tests/test_app.py`, `test_time_sync_resumes_once_trusted` now drives a real
`unset → rtc` transition, and the pty test was renamed with an honest docstring rather than
over-claimed. Credit where due. **But two of the replacement tests have the same defect:**
- `test_default_backlog_pacing_holds_the_stations_own_airtime_budget` measures nothing — it asserts
  `1/300 * 0.37 < 0.01`, arithmetic on two constants, and is the only test that names the budget
  (CRITICAL 3).
- `test_backlog_rotation_persists_across_ticks_and_is_paced` never checks persistence. It counts
  `GET_BACKLOG` frames and asserts `< 15/s` against an expectation of 10/s; the poll order, the
  retry accumulation and the schedule's survival — the three things its name promises — are not
  asserted anywhere.

**WARNING 10 (was: two invented adapter policies) — RESOLVED.** `_recently_reachable` is deleted and
replaced by `last_frame_arrival`, which answers the right question ("did this node send anything
since we last polled it") and is correctly thread-guarded. The `TIME_SYNC` cadence is resolved for
the *sent* case. The *withheld* case became CRITICAL 4.

**WARNING 11 — Strict TDD evidence is still per-change, not per-slice. STANDS.** The
`apply-progress` topic key was upserted again (obs #4987, revision 11), so slices 1–12 remain
overwritten and RED-was-genuinely-red is still unverifiable for most of the change. The fix pass's
own evidence is good and checkable — RED confirmed per fix via `git stash push -- <src files>`, each
new test named with its failure mode — and I confirmed all eight new tests exist and pass. Format
drift is still **6** files (tasks.md and the apply record both still say 5). `mypy tests` is now
**100** errors, up from 95.

**WARNING 12 — a full `GET_BACKLOG` lap now takes 50 to 150 minutes, and nothing records that
decision.** `workers.py:231` sets `next_poll_due_at = now + config.heartbeat_interval` **globally**,
not per node, so the whole roster shares one poll every 5 minutes. Confirmed empirically
(`heartbeat_interval=0.5 s`, 3 nodes → exactly 1 poll per 0.5 s, lap = 4.5 s at `retry_budget=3`).
For the specified 10-trap roster that is a **50-minute lap when every node answers and 150 minutes
when none do**. `messages.md` says *"A node that was unreachable for hours is caught up by the next
turn"* — that turn is now up to 2.5 hours away. The pacing is the right *shape* (it is the only
roster-size-independent way to hold the budget) and the number may well be acceptable, but it was
chosen inside a bug fix, is recorded only in a code comment, and contradicts design.md §7's
"scheduler thread — 1 s tick: backlog rotation". It needs to be a decision, not a side effect.

**WARNING 13 — `run_scheduler_loop(last_frame_arrival=None)` degrades silently.**
`workers.py:138, 208`: the parameter defaults to `None` and the loop then builds its own private
empty dict. Nothing ever populates it, so `heard` is permanently `False`, every node burns its full
`retry_budget` on every turn forever, and the lap time triples. `start_workers` passes it correctly,
so production is fine — but **both new scheduler tests omit it**, which means both of them measure
the degraded mode rather than the shipped one. A parameter whose default silently changes the
behaviour under test is the wrong default. *Fix*: make it required, as `run_gateway_loop` already
does.

**WARNING 14 — design.md, tasks.md and three docstrings now describe code that no longer exists.**
- design.md:293 still says step 2 calls `registry.captured(...)`; it calls `restore_capture(...)`.
- design.md:303 "Reachability is not rehydrated" — still contradicted (WARNING 6).
- design.md §7's diagram still labels the scheduler "1 s tick · backlog rotation".
- design.md:288 still refers to a `traps.rehydrate()` that has never existed.
- tasks.md:384-385 still claims *"All four orphaned drivers (gateway reader, `run_rotation_round`, …)
  actually run, proven end to end"*.
- `tests/test_app.py:446-456`'s docstring still claims the test proves the app "runs a rotation
  round (slice 7)" and that `attempt_time_sync` "fires every tick here since the clock is trusted".
  Neither is true after `21be80c`.

The change's own convention is to disclose rather than hide; these are the places where the record
now actively misleads the next reader. A verification pass that trusts them reaches the wrong
conclusion — which is close to what happened here.

**WARNING 15 — the fix shipped as one 892-line commit, 2.2× the 400-line review budget.** Disclosed
in the apply record rather than hidden, and CRITICAL 1+2 genuinely had to ship together — but 892
lines rewiring the boot path, the scheduler, the store and `/health` in one reviewable unit is how
four new CRITICALs crossed review unnoticed. The remediation for this report should be split by
CRITICAL.

---

## SUGGESTION

All nine from the first report still stand. Four are new.

1. **`domain/payloads.py`'s docstring is still stale** — it still says `messages.md` *"pins some
   field widths explicitly … and leaves others unstated"*; `messages.md` now pins every width and
   records that they came from this module.
2. **`decode_heartbeat`'s comment still says `clock_source` is a "small enum (0-2)"**
   (`payloads.py:108`), anticipating a GPS source `messages.md` has since removed.
3. **`adapters/export.py:48-58`'s comment is still stale** — it still says *"nothing in `app.py`
   today calls `store.append()` for an operator's arm/disarm … left for whichever slice owns
   `app.py`'s command handlers."* Slice 13 did that.
4. **Dead production code** — `Store.events_since`, `Registry.unacknowledged_captures`,
   `payloads.encode_get_state`/`GET_STATE`, and `Capture.acknowledged` never set `True`.
   `StationConfig.heartbeat_interval` is **resolved** (now the scheduler's pacing input).
   `Store.integrity_check` is now dead in `tests/` too, not just `src/`.
   `run_rotation_round` is promoted to CRITICAL 5.
5. **`Trap.is_reachable`'s 5-minute default** (`traps.py:58-62`) is still optional; make it required
   so no future direct caller silently gets 5 minutes instead of the configured 15.
6. **design.md §2's `quick_check` at boot, "Reported in `/health`"** — still not implemented. The
   `link` block is the natural place for it now that `/health` has one.
7. **No `py.typed` marker** in `src/feeder_station/`; the wheel still ships untyped.
8. **`RELEASE_EPOCH_FLOOR`'s comment still disagrees with its value** (`clock_trust.py:47-49`):
   comment says 2026-09-23, value is 2025-01-01.
9. **`Registry.command_arm`/`command_disarm` still take an unused `at` parameter** (`traps.py:109-113`).
10. **`Registry.restore_capture` / `restore_desired_tags` are indistinguishable from the live API.**
    Both are public on the object every route holds, both sit next to `captured()`, and both
    `setdefault` a `Trap` into existence — a mistaken route call would silently create a ghost trap
    and skip the `state`/`last_seen` update. No caller today; consider naming or a guard.
11. **A rehydrated `Command` returns with `attempts=0, last_sent_at=None`**, so `due_for_resend` is
    immediately true and the 1/2/4/8/16/30 s backoff restarts from scratch on every boot. Combined
    with `Restart=always`, a station in any restart loop re-bursts the whole ramp each time.
12. **A roster change resets the rotation to position 0.** `workers.py:217-218` rebuilds
    `backlog.start(node_ids)` whenever `schedule.node_ids != node_ids`, discarding both position and
    attempts. While the roster is still growing (first boot on a fresh store, nodes appearing one
    heartbeat at a time), nodes at the tail can be starved. Self-healing once the roster settles.
13. **The UI link banner shows only `connected` and `last_error`.** design.md §4 says link state is
    *"surfaced whole in `/health` **and in the UI**"*; `device`, `since`, `frames_in`, `frames_out`
    and `decode_errors` reach `/health` but not the page.

---

## Spec Compliance Matrix

41 scenarios across 8 specs. **34 compliant, 7 not** (was 36/41).

Only rows whose status changed, or that a new CRITICAL touches, are listed; every other row is
unchanged from the first report and still ✅.

| Spec | Scenario | Status now | Change |
|------|----------|-----------|--------|
| event-store | registry reflects last known state after restart | ❌ **VIOLATED** | rehydration now runs at boot (old cause fixed) but rebuilds the wrong state — **CRITICAL 1**; and skips command-only nodes — **CRITICAL 6** |
| radio-gateway | link drop surfaces in `/health` | ✅ **COMPLIANT** | fixed and verified over real HTTP — was ❌ |
| radio-gateway | one silent node does not block the rotation | ❌ **UNTESTED on the production path** | behaviour verified correct by measurement; covering test now targets dead code — **CRITICAL 5** — was ⚠️ |
| radio-gateway | FAULT updates reported state to fault | ⚠️ **PARTIAL** | holds live; lost across a restart — **CRITICAL 1** — was ✅ |
| clock-trust | TIME_SYNC resumes once trusted | ✅ **COMPLIANT** | fixed and verified through the real loop — was ❌ |
| clock-trust | TIME_SYNC scheduling skips a node while unset | ⚠️ **PARTIAL** | still withholds correctly; the withheld *log* is now unbounded — **CRITICAL 4** — was ✅ |
| trap-commands | resend continues until version confirmed | ⚠️ **PARTIAL** | survives a restart now; still lost for a never-heard node — **CRITICAL 6** — was ⚠️ |
| operator-ui | capture from hours ago is still visible | ⚠️ **PARTIAL** | the capture survives a real restart ✅; the row beside it reads `idle`/unreachable, not *"exactly as if seen live"* — **CRITICAL 1** — was ❌ |
| station-deployment | process restarts after being killed | ⚠️ **PARTIAL** | the CI proxy still passes; `Restart=always` is now weaponisable into a permanent crash loop — **CRITICAL 2** — was ✅ |
| station-deployment | runbook names the tested image | ❌ **unmet** | unchanged — WARNING 8 |
| data-export | export contains only witnessed event types | ⚠️ **PARTIAL** | unchanged — WARNING 2 |

**Compliance summary: 34/41.** Three scenarios were repaired (link drop, TIME_SYNC resume,
operator-ui capture persistence — the last only partially); five regressed or were newly exposed.

### Requirements with no test at all

1. **The station's total transmit rate against the channel budget.** The only test naming it is
   arithmetic on constants (CRITICAL 3, WARNING 9).
2. **Withheld-`TIME_SYNC` log volume.** Presence is asserted; count is not (CRITICAL 4).
3. **Rehydration where the latest report is a `CAPTURED` or a `FAULT`** (CRITICAL 1).
4. **Rehydration of a commanded-but-never-heard node** (CRITICAL 6).
5. **Boot against a store the boot path cannot read** (CRITICAL 2).
6. **`radio-gateway` "one silent node does not block the rotation" on the production scheduler**
   (CRITICAL 5).
7. **`station-deployment` "runbook names the tested image"** — unchanged, blocked on ops.

---

## Ordering audit (S9) — re-checked

**S9 still holds on every path that writes**, including the paths the fix touched. `ingest_frame`'s
new `record_arrival(...)` call sits *before* `store.append()` — which is correct and deliberate: it
is a best-effort in-memory fact, never acknowledged to anyone, and moving it after the write would
make the scheduler's `heard` answer depend on a durability it does not need. No `try`/`except` was
added around `append`. `_record_operator_event` still precedes every registry mutation and publish on
all four operator routes. `attempt_time_sync` still writes its withheld row before returning.
No path acknowledges, confirms or publishes before a durable write.

One new interaction worth naming: `store.rehydrate()` now runs on the asyncio event-loop thread
during startup, opening a thread-local SQLite connection there. It is before `yield`, so nothing is
being served and nothing is blocked that matters — and `store.close()` closes every connection it has
ever handed out, including that one. Correct, but it is the first time the loop thread touches SQLite
and it is worth knowing.

## Assertion quality

The fix pass improved this materially: three over-claiming tests were repaired or honestly renamed,
and the four new `test_cli.py` tests drive the genuine production boot path (`build_app` +
`with TestClient(app)`, which is the only way the ASGI lifespan runs at all — a real and non-obvious
discovery, correctly recorded in the apply notes).

Against that, two of the replacement tests reintroduce the same defect (WARNING 9), and one existing
assertion was *changed to match the bug*: `test_registry_reflects_last_known_state_after_restart`'s
`FIRED → ARMED` edit is correct for ordering A and is exactly what allowed ordering B to ship
(CRITICAL 1). An assertion edited to make a fix pass deserves the same scrutiny as the fix.

**Assertion quality: 1 CRITICAL (CRITICAL 3's budget test), 2 WARNING** — all already counted above.

## Test layer distribution

| Layer | Tests | Change |
|-------|-------|--------|
| Pure/unit (`tests/domain/`) | 63 | — |
| Adapter (`tests/adapters/`) | 65 | +3 |
| App/route (`test_app.py`, `test_cli.py`, `test_config.py`, `test_deploy_unit.py`) | 80 | +5 |
| **Total** | **208** | **+8** |

## Hardware gates — outstanding, never counted as green

Carried forward unchanged and still correctly disclosed throughout: SD-card flush honesty (S9's
literal criterion); `/dev/serial/by-id` stability (CP2102 vs CH340, board unidentified); a pty is not
a USB-CDC device; the physical DS3231 across an unpowered hour (S15); a literal power-pull (S7); the
target Pi OS image; and any true end-to-end exchange, which still needs a `station_link` component
and a gateway ESP32 image that do not exist. `adapters/command_sender.py`'s module docstring still
states plainly that no real trap has ever received a frame. That honesty survived the fix pass intact
and remains a real strength of this change.

Note that CRITICAL 4 and WARNING 4 both land hardest exactly where CI cannot reach: SD-card fsync
latency and endurance. They should be treated as more severe than this machine's measurements make
them look, not less.

## Verdict

**FAIL** — 6 CRITICAL, 13 WARNING, 13 SUGGESTION. 34/41 spec scenarios compliant;
208/208 tests pass; `ruff check`, `mypy src` and `uv build` clean.

Do not archive. Return to `sdd-apply`.

Three of the five original CRITICALs are properly fixed and should not be reopened: rehydration runs
at boot (1), `TIME_SYNC` resumes for every node (3), and `/health` reports a dropped link (5). The
rotation half of (4) — persistence across ticks, retry budget not reset, `GET_BACKLOG` pacing — is
fixed too.

What went wrong is narrower than the count suggests and worth stating exactly, because it is the same
mistake three times: **each fix was validated against the failing example rather than against the
rule.** CRITICAL 2 was fixed for the ordering the report printed, not for the invariant "rehydrated
state equals live state". CRITICAL 4 was fixed for the transmitter the report measured, not for the
channel budget. CRITICAL 3 was fixed for the frame that should be sent, not for the attempt that is
made. And CRITICAL 1's wiring was added without asking what happens when the thing being wired in
raises. A green suite followed in every case, because the tests were written from the same examples.

Suggested order, since these are coupled: **CRITICAL 2 first** (it is the only one that can brick a
deployed station and it is cheap to contain), then **CRITICAL 1 + 6 together** (both are `rehydrate`,
and both need the roster and the state source reconsidered at once), then **CRITICAL 3 + 4 together**
(both are the scheduler's unpaced transmitters, and a single station-wide budget closes both), then
**CRITICAL 5** (delete or re-wire, then move the coverage). Split them into separate reviewable units
this time — WARNING 15.
