# Verification Report — final pass, `field-station-p4`

**Under test**: `9bc82c9`. **Scope**: `bd163d4..HEAD` — Batch 3a (station-wide pacer), Batch 3b
(TIME_SYNC volume + the `satisfied_by` asymmetry), Batch 4 (dead rotation primitive), plus a
regression check on CRITICAL 1, 2, 6 and A. Strict TDD, artifact store `openspec`. 2026-09-23.

Everything below was derived from the source and measured on this machine. Nothing was accepted from
an apply record, a test name or a docstring. 34 mutations were applied in the real package root
(`feeder_station.__file__` confirmed resolving to `src/feeder_station/` via the editable install), each
restored, `git status --porcelain` empty after every one.

**Verdict: FAIL — 1 CRITICAL BEHAVIOUR, 1 WARNING BEHAVIOUR, 4 PROOF gaps, 5 WARNINGs.**
Seven of eight findings under re-verification are genuinely closed. The eighth is CRITICAL 3.

| # | Finding | Verdict |
|---|---|---|
| CRITICAL 1 | rehydration regresses state for a `CAPTURED`/`FAULT` | **CLOSED**, no regression |
| CRITICAL 2 | one corrupt `HEARTBEAT.state` byte bricks the boot | **CLOSED**, no regression |
| CRITICAL 3 | station transmits over the airtime budget | **NOT CLOSED** — BEHAVIOUR 1 |
| CRITICAL 4 | unset clock writes 864,000 fsync'd rows/day | **CLOSED** |
| CRITICAL 5 | `run_rotation_round` dead; spec scenario untested live | **CLOSED** |
| CRITICAL 6 | command to a never-heard node lost on restart | **CLOSED**, no regression |
| CRITICAL A | `except ValueError` too narrow; roster query unguarded | **CLOSED**, no regression |
| BEHAVIOUR 4 | the `satisfied_by` asymmetry | **CLOSED**, 18/18 confirmed independently |

CRITICAL I is recorded at `remediation-tasks.md:599` with zero checkboxes — descoped and unscheduled,
as agreed. Nothing further.

## Gates (each exit status checked directly)

`uv run pytest` **0** — 808 passed, 17.4 s · `ruff check .` **0** · `mypy src` **0** — 23 files ·
`uv build` **0** · `ruff format --check .` **1** — 7 files, **zero new drift**.

Format baseline established independently: a `git worktree` at `504d18e`, run cold through this repo's
own ruff (`pyproject.toml` is byte-identical at `504d18e` and `HEAD`), lists **6** files. `HEAD` lists
those 6 plus `verify-report-batch2d.md`, which `git log --diff-filter=A` places at `bd163d4` — before
this change's scope.

---

## BEHAVIOUR 1 (CRITICAL) — one operator action routes around the station-wide budget and restores the pre-fix occupancy

**Where**: `adapters/command_sender.py:195` spends one token per **trap**; `:157` then writes **every
chunk** unconditionally (`results = [gateway.write_frame(raw) for raw in wire_frames]`). No bound on
list length anywhere — `app.py:68-85`'s `TargetsRequest` validates each tag's hex format and nothing
else.

**Input → outcome, measured off a real pty through `cli.build_app` at production defaults** (no
`--channel-budget-frames-per-s` override, configured budget 0.027027 f/s = 1.000%):

```
PUT /api/traps/7/targets {"tags":[...270 valid tags...]}  ->  200 OK
  first 6 s        10 SET_TARGETS frames on the wire for 1 budget token
  180 s window     0.2887 f/s = 10.683% occupancy   (5 tokens spent, 50 frames emitted)

10 traps x 270 tags, 60 s settle + 240 s steady window
                   0.1791 f/s =  6.627% occupancy   (4 tokens spent, 40 frames emitted)
```

The pre-fix figure this whole remediation exists to close was **11.68%**.

`payloads.TAGS_PER_SET_TARGETS_FRAME` is 27, so the bypass opens at **28 tags**, not 270: 28 buy 2
frames per token, 100 buy 4, 1000 buy 38. Asymptotically, once the backoff reaches its 300 s cap, one
trap with a 270-tag list sustains 10 frames / 300 s = 1.23% alone; ten such traps are bounded by the
token grant rate instead and sustain ≈10%.

**This is the fix's own stated rule, not a new subject.** `remediation-tasks.md:422` set it as *"the
station's total transmit rate stays under the budget… measured off the wire"*; task 3.2's language was
*"no future driver can bypass it either."* `apply-progress-batch3a.md:27` names this exact call site
and records the decision — *"gated (same call as above, one budget check per trap covers the whole
chunked send)"*. The site was found; the consequence of the decision was never measured.

**Not a design conflict.** `trap-commands/spec.md:48-51` requires a split list be sent as one unit and
delivered on the version match alone, so chunks must not be split across sweeps. Charging the bucket
*N* tokens for an *N*-chunk send — or refusing until *N* are available — satisfies both.

**Why no test caught it**: `tests/adapters/test_command_sender.py:344`, the only split-list test,
passes `_AllowAllBudget()` explicitly. The budget test at `:381` uses ten single-frame `ARM`s. Nothing
exercises the budget against a chunked send.

## BEHAVIOUR 2 (WARNING) — the pacer is sized *at* the documented budget, and that 1% is `messages.md`'s **uplink** allocation

**Where**: `config.py:52-54`. Two things, pointing opposite ways, neither recorded:

1. `docs/protocol/messages.md:103-104`: *"A visit event is ~50 bytes on the wire, 0.37 s of airtime at
   Long Fast; ten nodes at a couple of hundred events a day keep the channel under 1% occupied."* That
   sentence's own arithmetic is `10 × 200 × 0.37 / 86400 = 0.857%` — it describes the **node uplink**.
   The implementation reallocates the whole 1% to **station downlink**, on top of that uplink and on
   top of one ACK per ack-required uplink. Channel total at design load ≈ 2%, not under 1%.
2. Offsetting it: 0.37 s is a *~50-byte* frame. The station's own paced downlinks are 12–16 bytes, so
   the bucket over-charges its own traffic — except `SET_TARGETS` at ~200 bytes, which is exactly what
   BEHAVIOUR 1 lets through 10 at a time.

**And the reported margin does not exist where it matters.** `apply-progress-batch3a.md:189-193`
reports 0.821% / 0.822% / 0.821% — a 90 s window quantising 2 frames into 0.0222 f/s. Re-measured to
convergence, the saturating case sits on the configured refill rate exactly (0.986–1.000%): there is
no 0.18% of headroom, the sustained figure *is* the budget. The other two cases are in fact far
*better* than reported (0.247% and 0.123%), so all three of the implementer's numbers are artifacts —
one optimistic, two pessimistic.

Neither point violates a spec: no spec under `specs/` states an airtime number at all. Recorded so the
next reader does not inherit a margin that was never there.

---

## Measured: the three-row occupancy table, re-run

Real `pty.openpty()`, real `SerialGateway`, real worker threads via `with TestClient(app)`, real
`cli.build_app`, **production defaults**; frames counted by raw `os.read` + COBS decode.

| Roster state | Window | Frames/s | Occupancy | Was (report #2) | Budget |
|---|---|---|---|---|---|
| 10 traps, unconfirmed ARM | 45 s settle + 185 s | 0.0270 | 1.000 % | 11.68 % | 1.000 % |
| **10 traps, unconfirmed ARM** | **300 s settle + 600 s** | **0.0267** | **0.986 %** | 11.68 % | 1.000 % |
| 1 trap, unconfirmed ARM | 45 s settle + 185 s | 0.0270 | 1.000 % | 1.17 % | 1.000 % |
| **1 trap, unconfirmed ARM** | **300 s settle + 600 s** | **0.0067** | **0.247 %** | 1.17 % | 1.000 % |
| 10 traps, idle | 45 s settle + 185 s | 0.0270 | 0.999 % | 0.12 % | 1.000 % |
| **10 traps, idle** | **300 s settle + 600 s** | **0.0033** | **0.123 %** | 0.12 % | 1.000 % |

**The long-window rows are the steady states; every short-window row is a transient — and so was every
row the implementer reported.** Three different constraints bind, which is why one number does not
describe all three cases:

- *10 traps unconfirmed* is **budget-bound**: demand exceeds the token rate forever, so the downlink
  converges on exactly its configured budget. 0.986–1.000% is the worst case and the number that
  matters. All three categories transmit (ARM 8, TIME_SYNC 6, GET_BACKLOG 2 in 600 s) — no starvation.
- *1 trap unconfirmed* is **backoff-bound**: `RESEND_BACKOFF_S`'s new 300 s cap (task 3.3) is slower
  than the token rate, so it settles at **0.247%** (2 ARM + 2 GET_BACKLOG in 600 s), not the 1.000% a
  short window shows. Raising the cap toward `heartbeat_interval` did real work here — this row is the
  one verify-report-2 called *"a single unconfirmed command already exceeds the whole channel budget"*,
  and it is now 4× under it.
- *10 traps idle* settles at **0.123%, `GET_BACKLOG` only** — one poll per `heartbeat_interval`,
  unchanged from before the fix. The 0.999% at 185 s is the **boot transient**: 4 `TIME_SYNC` + 1
  `GET_BACKLOG`, the one-off roster sweep, which takes ~370 s for ten nodes at 0.027 f/s. So
  `apply-progress-batch3a.md:195-201`'s claim that this row rose *"from 0.12% to 0.821%"* and that
  "a station-wide budget is supposed to use the budget" describes the transient, not the steady state.
  An idle station is as quiet as it ever was.

**The 11.7× reduction is real and the mechanism works.** BEHAVIOUR 1 routes around it.

### Transmitters, re-enumerated from source

`rg "write_frame\(" src/` — five real call sites (the other five hits are `Protocol` declarations and
`SerialGateway`'s own implementation):

| Call site | Gated? | Verdict |
|---|---|---|
| `scheduler.py:71` `poll_node` | yes, `workers.py:333` | correct |
| `clock.py:254` `attempt_time_sync` | yes, `workers.py:393`, only on the transmitting branch | correct |
| `command_sender.py:209` single-frame ARM/DISARM | yes, `:195`, per trap inside the loop | correct |
| `command_sender.py:157` SET_TARGETS multi-chunk | **1 token, N frames** | **BEHAVIOUR 1** |
| `ingest.py:122` ACK | no — deliberate | **correct; gating it would be a regression** |

The ACK exclusion holds under scrutiny. It fires only for `FLAG_ACK_REQUIRED` and only after
`store.append()` has fsync'd (`ingest.py:113-122`), so gating it would violate `radio-gateway/spec.md`
"Ingest and ACK ordering". Worse, `messages.md:124-125` says a sender *"is retried until it sees an
ACK"* — withholding one makes the node retransmit the whole event, **increasing** channel use. Its
rate is bounded by the node's own transmit rate, which the station cannot pace anyway.

### Starvation — round-robin holds, including under adversarial resonance

`workers.py:51-55` cycles three fixed orderings, one per tick. Probed with all three categories
saturating (10 traps each holding a permanently unconfirmed ARM, trusted clock), 4 s windows:

```
refill = 3.3 ticks   total 30.49 f/s (budget 30.0)   ARM 29  TIME_SYNC 10  GET_BACKLOG 83   starved: none
refill = 3.0 ticks   total 33.99 f/s (budget 33.3)   ARM 26  TIME_SYNC 10  GET_BACKLOG 100  starved: none
refill = 6.0 ticks   total 17.24 f/s (budget 16.7)   ARM 24  TIME_SYNC 10  GET_BACKLOG 35   starved: none
```

Rows 2–3 are deliberately adversarial: a refill period that is an exact multiple of the 3-cycle
rotation is the one arrangement that could lock first-claim to one category forever. It does not,
because `stop.wait(tick_s)` jitters and because `send_due_commands` can take several tokens in one
tick. No category starved in any configuration, and the 600 s real-wire run above shows all three
transmitting at the production default too (ARM 8, TIME_SYNC 6, GET_BACKLOG 2). A single trap with a
permanently unconfirmed command cannot starve `GET_BACKLOG` or `TIME_SYNC`.

**Can a trap's `CAPTURED` be delayed by routine station traffic?** Not by the budget: `CAPTURED` is an
uplink and the pacer gates downlink only, so the station never withholds it. But uplink and downlink
**do** share one channel (`messages.md:12`, "LoRa in the field"; open question 4, *"it costs airtime on
a shared channel"*), and LoRa is half-duplex. A `CAPTURED` sent while the station is transmitting
collides, with probability ≈ the station's own occupancy — **~1% per attempt now, ~12% before this
change**. `messages.md:126-128` bounds the consequence: a `CAPTURED` *"is retried until acknowledged"*
and *"is also repeated in every HEARTBEAT through state = fired"*. So the cost is one node-side retry
interval, not a lost alert, and this change cuts that risk ~12×. BEHAVIOUR 1 puts it back to ~7–11%
for as long as a large target list stays unconfirmed.

---

## CRITICAL 4 — closed, and it is a property, not one measurement

The report's own repro, re-run against the production `run_scheduler_loop` (production `tick_s=1.0`,
`heartbeat_interval` at its 5-minute default, unset clock):

```
10 traps, 10.0 s  ->  10 rows  =  1.000 rows/s     (was 10.0)
10 traps, 30.0 s  ->  10 rows  =  0.333 rows/s
```

1.000 rows/s is exact. The bound holds **regardless of roster size and tick count** — at `tick_s=0.01`
(hundreds of ticks per window) and `heartbeat_interval=0.15 s`:

| nodes | 0.4 s | 0.8 s | 1.6 s | 3.2 s | asymptote `n/hb` |
|---|---|---|---|---|---|
| 1 | 7.49 | 7.49 | 6.87 | 6.56 | 6.67 |
| 3 | 22.46 | 22.46 | 20.61 | 19.68 | 20.00 |
| 10 | 74.84 | 74.91 | 68.71 | 65.61 | 66.67 |
| 50 | 373.87 | 374.40 | 343.56 | 328.03 | 333.33 |

The rate is a function of `node_count / heartbeat_interval`, converging on it from above (the boot
burst is one extra attempt per node), never of the tick rate. At production settings that is
10/300 s = 0.033 rows/s ≈ 2,880 rows/day ≈ 0.6 MB/day — inside design.md §2's ~350 MB/year sizing,
against 189 MB/day before.

**The MUST-log obligation is intact, not traded away.** `clock-trust/spec.md:27` requires *"MUST log
each withheld send"*; `clock.py:237-249` writes the `STATION_TIME_SYNC_WITHHELD` row unconditionally on
the withheld branch and is untouched by this batch. The gate at `workers.py:381-392` changes how often
the function is *called*, never whether a call logs.

## BEHAVIOUR 4 / the `satisfied_by` rule — correct, and the spec's alternative reading named

**The decision holds.** `payloads.decode_captured` decodes a real, range-validated `state_after`;
`decode_fault` decodes `code`/`detail` and no state field at all. A `CAPTURED` carries a reported fact,
a `FAULT` does not, and inferring "not armed" from "something is wrong" is the assumption
`docs/requirements.md:30` (S4) rules out — a faulted trap may still be physically armed.

**Where the spec supports a different answer, plainly.** `trap-commands/spec.md:14-15` says *"The
trap's displayed state MUST always be whatever its own most recent `HEARTBEAT` or `HELLO` reported"*,
and its scenario at `:22` clears `arm_pending` *"until a subsequent `HEARTBEAT` reports state=armed"*.
S4 likewise says *"sourced from the trap's own heartbeat."* A strict-literal reading of either would
have **`CAPTURED` satisfy nothing** — both enumerate HEARTBEAT and HELLO, not CAPTURED. The change
departs from that literal reading deliberately and consistently: verify-report-2's CRITICAL 1 required
`CAPTURED` be treated as a report for *display*, CRITICAL A made `state_after` authoritative, and
`messages.md:30` states the governing principle — *"The node is the authority on its own state."*
Making satisfaction agree with display completes that coherently. It is the spec sentence that is now
stale and should be amended to name `CAPTURED`, rather than left to be read against the code.

**The 18-combination matrix, re-run independently.** I drove the **live** side through
`ingest.py::_apply_to_registry` — the real production path, not the direct `Registry` calls the
committed test uses — and the **rehydrated** side through a real `build_app` boot:

```
18/18 agree live-vs-rehydrated;  18/18 match the stated rule   (`state` agreed in all 18 too)
```

**Pre-fix baseline verified independently** by restoring `traps.py` + `store.py` from `6e34062^` into
the real package root: **13/18**, diverging on exactly `arm/captured_armed`, `disarm/captured_idle`,
`disarm/captured_fired`, `disarm/captured_fault`, `disarm/fault` — exactly the five rows reported.

**The consequence is real and bounded.** A FAULT-only trap resends forever. At the 300 s backoff cap
that is 1 frame / 300 s = 0.12% per such trap, drawn from the shared budget and rotating fairly — it
slows the other categories, it cannot exclude them. **What the operator sees**: the trap renders
`state: "fault"` with `pending_command: "arm"` (or `"disarm"`) indefinitely. Nothing in the UI
distinguishes "still in flight" from "can never be confirmed because this trap only reports FAULT".
Honest but unhelpful; worth a follow-up.

## CRITICAL 5 — closed; the implementer's poll-order reasoning is a genuine insight

`run_rotation_round` exists nowhere in `src/` as a symbol (prose only). `poll_node` survives with
exactly one production caller, `workers.py:339`.

**The disputed claim, verified empirically** by running the live `run_scheduler_loop` with a spy
gateway that simulates a reply from every responsive node:

```
3 nodes, node 2 silent  ->  lap1 runs (1,2) (2,3) (3,2)   lap2+ (1,1) (2,3) (3,1)
3 nodes, ALL silent     ->  [1,1,1, 2,2,2, 3,3,3, ...]    <- the task's own array, exactly
10 nodes, node 5 silent ->  lap1: 2 polls each except node 5 (3);  lap2+: silent 3, others 1
```

The reasoning is correct and the code is the reason: `workers.py:337` computes
`heard = since is not None and ...`, and on a node's first-ever poll `last_polled_at.get(node_id)` is
`None`, so `heard` is `False` regardless of responsiveness. `[1,1,1,2,2,2,3,3,3]` only appears when no
node ever registers a reply — the deleted primitive's synchronous `responded` semantics, not this
loop's. Copying the array verbatim would have moved dead-primitive semantics onto the live loop and
called it proven. **Insight, not rationalisation.**

The committed test asserts lap 1's node *ids* only (the first-contact transient, correctly skipped) and
laps 2–3 exactly: silent node 3 polls, every other node 1, repeating — `radio-gateway/spec.md`'s *"one
silent node does not block the rotation"*, proven against the production path for the first time.

The three tests kept in `test_backlog_rotation.py` all call `poll_node` directly — live production
code, DD7's `msg_id`-wrap regression included. **Disclosed loss confirmed**: the real-pty tier went
with `test_run_rotation_round_completes_quickly_over_a_real_pty` and was not recreated;
`poll_node`'s cursor forwarding is now proven only against a `_FakeGateway`. Disclosed correctly at
`apply-progress-batch3b-4.md:305-310`; it should be recreated. Also fair:
`test_run_rotation_round_terminates_with_a_single_silent_node` went with the primitive, and that
hazard is structurally gone — `run_scheduler_loop` polls one node per tick with no inner sweep.

---

## PROOF gaps (the code is right; the mutation survives)

**PROOF 1 — the field guard proves "has an entry", not "is compared."** `tests/test_cli.py:245-249`.
Two mutations survive the whole 808-test suite:

```
map "pending_command" -> ()             808 passed   SURVIVED
map "pending_command" -> ("node_id",)   808 passed   SURVIVED
```

`_compared_view_fields()` checks only `trap_field_names - set(_TRAP_FIELD_TO_VIEW_KEYS)`, so an empty
or wrong tuple satisfies it while silently dropping the field from the axis — the exact six-round blind
spot the guard exists to prevent, one keystroke away. **The current mapping is correct**: all 9 `Trap`
fields mapped, none vacuously, and the 12 mapped view keys are exactly the 12 `Registry.view()` emits.
*Fix*: assert the union of mapped keys equals `view()`'s own key set.

**The guard's headline claim is true.** Adding `verify_probe_field: int | None = None` to `Trap` makes
the whole suite fail at **collection** —
`AssertionError: Trap field(s) {'verify_probe_field'} have no view()-key mapping`. Removing
`pending_command` from the mapping does the same.

**PROOF 2 — nothing constrains a chunked `SET_TARGETS` against the budget** (the proof side of
BEHAVIOUR 1): the only split-list test passes `_AllowAllBudget()`.

**PROOF 3 — rotation fairness is pinned only incidentally.** Collapsing `_ROTATIONS` to a fixed
commands-first priority is killed by exactly one test, via its *secondary* assertion
(`assert payloads.GET_BACKLOG in observed_types` in `test_station_total_frame_rate_...`). Real
coverage, but no test is named for or about starvation.

**PROOF 4 — the 18-case matrix builds its live side from direct `Registry` calls**, not
`ingest._apply_to_registry`, so `ingest -> captured(state_after) -> satisfied_by` is not pinned end to
end for `pending_command`. I closed this by re-running through the real path (18/18); the suite does
not.

## Mutations — 34 in the real package root, all restored clean

All KILLED except the two named in PROOF 1. Failure counts in parentheses.

Pacer: M1 `try_consume` always allows (7) · M2 `try_send` always `True` (3) · M3 per-trap check removed
(2) · M4 poll's check removed (2) · M5 TIME_SYNC's check removed (1) · M16 `DEFAULT_CAPACITY`→100 (3) ·
M17 default budget ×100 (1) · M30 bucket starts empty (6) · M31 bucket rebuilt per tick (2) ·
M15 `_ROTATIONS` collapsed to fixed priority (1).
Commands/TIME_SYNC: M6 `RESEND_BACKOFF_S` back to 30 s (1) · M7 withheld cadence gate disabled (4) ·
M19 cadence source → 0 s (4) · M32 `would_send` gate removed (5).
`satisfied_by`: M8 `captured()`'s call disabled (5) · M9 call **added** to `fault()` (2) ·
M10 FAULT gate in `_rehydrate_desired_command` disabled (1) · M18 `captured()` hardcodes `armed=False` (2).
Rotation: M12 `heard`→`True` (1) · M13 `heard`→`False` (1) · M14 retry boundary `>=`→`>` (2).
Field guard: M20 new `Trap` field → collection error · M21 mapping removed → collection error ·
**M22 `()` → SURVIVED** · **M23 wrong key → SURVIVED**.
Batch 1/2 regressions: M24 `except Exception`→`except ValueError` (4) · M25 `rehydration_errors` reset
removed (1) · M26 `_reported_state` fabricates `armed=False` (1) · M27 negative-index rejection removed
(2) · M28 `_known_node_ids` back to `node_cursor` only (10) · M29 `Trap.state` defaults to `IDLE` (5) ·
M33 `battery_mv` guard back to silent `None` (1) · M34 `mark_state_possibly_stale` never called (5) ·
M35 `roster_lost` never set (1).

Plus one full pre-fix restore of `traps.py`+`store.py` from `6e34062^` (the 13/18 baseline), restored.

## Batches 1 and 2 — regression check only

All intact, each proven by a killed mutation: `rehydrate()`'s totality (M24) — **all 8 `_unreadable()`
call sites are inside `rehydrate()`'s three guards**, AST-verified (`_rehydrate_heartbeat` ×3,
`_rehydrate_capture` ×2, `_rehydrate_desired_command` ×3; `_rehydrate_fault` reads only structural
fields); `rehydration_errors` reset and accuracy (M25, M33); negative-index rejection (M27); roster
derivation (M28, 10 failures); live-equals-rehydrated for all four `state_after` values (18/18 in my
own run); nullable `state` and the stale flag (M29, M34); degraded-boot signalling (M35). Nothing in
Batches 3a/3b/4 broke any of them.

## Existing tests — `git diff bd163d4..HEAD -- tests/`

**No assertion's expected value was changed to accommodate a fix.** Every removed/modified line was
examined:

- `test_default_backlog_pacing_holds_the_stations_own_airtime_budget` — deleted assertions were
  `1/300 * 0.37 < 0.01`, arithmetic on two constants. Rewritten (not deleted) to drive the real loop
  and count real frames. A strict improvement.
- The three `run_rotation_round` tests — removed with the primitive; poll-order coverage moved onto the
  live loop and is *stronger*. Real-pty tier is a disclosed, real loss.
- `test_the_app_starts_its_background_workers` — the `GET_BACKLOG` **assertion is unchanged**; only its
  failure message was corrected (task 4.5).
- The five-ordering property test — axis widened from a 3-item tuple to 12 enumerated view keys.
  Widened, never weakened.
- Ten `send_due_commands(...)` call sites gained `budget=`; four tests gained
  `channel_budget_frames_per_s=1000.0`. Configuration, not assertions — no pass/fail meaning changed
  (see WARNING 4).

**Assertion quality**: no tautologies, no orphan empty checks, no smoke tests, no mock-heavy files. One
near-miss: `test_channel_budget_frames_per_s_defaults_to_the_messages_md_budget` asserts
`pytest.approx(0.01 / 0.37)` — the same arithmetic as the constant it checks. Load-bearing anyway
(M17 kills it), but it restates a derivation rather than citing the document.

## WARNING

1. **`adapters/workers.py:7` still lists `run_rotation_round` as one of the four wired drivers** — a
   WARNING 14 residual *created* by Batch 4's deletion. Task 4.5 fixed the `test_app.py` docstring and
   not this one, which is the production module's own header.
2. **Ten new citations of `docs/protocol/messages.md`, a path that does not exist in this repository.**
   It lives in the parent monorepo at `/home/juanc/code/cicg/docs/protocol/messages.md`;
   `git log --all -- 'docs/protocol/*'` here is empty. Fourteen files now cite an unresolvable path,
   seven added by this change. `config.py:45-54` is the station's most load-bearing magic number and
   its source cannot be opened from a clone.
3. **WARNING 13 stands** — `run_scheduler_loop(last_frame_arrival=None)` (`workers.py:183`) still
   defaults to `None` and builds a private empty dict, so `heard` is permanently `False`. The new
   poll-order test passes it; four other scheduler tests still omit it.
4. **Four tests now run at `channel_budget_frames_per_s=1000.0`** — effectively unpaced. Disclosed and
   defensible (each isolates a different mechanism), but the wiring- and rotation-level tests no longer
   exercise the shipped pacing at all.
5. **`synced_node_ids` and `last_withheld_attempt_at` are thread-lifetime**, so every restart re-bursts
   one withheld row and one TIME_SYNC attempt per node. Bounded and benign now that CRITICAL 2 rules
   out a crash loop; record alongside SUGGESTION 11.

## Task completeness

Batch 3 (3.1–3.8 + BEHAVIOUR 4) and Batch 4 (4.1–4.5): **14/14 `[x]`**, each verified against the code
— `domain/pacing.py` exists, `channel_budget_frames_per_s` is plumbed through argv/env/default,
`RESEND_BACKOFF_S` caps at 300.0, all three scheduler transmitters draw from one `SharedBudget`, 3.6
rewritten, the withheld cadence gate present, `captured()`/`fault()` carry the decided rule,
`run_rotation_round` gone, the poll-order test targets the live loop, `test_app.py`'s docstring fixed.
Batch 5 carries no checkboxes — recorded, unscheduled, untouched.

## Verdict

**FAIL.** One BEHAVIOUR defect at CRITICAL severity remains: **BEHAVIOUR 1**, the multi-chunk
`SET_TARGETS` budget bypass — reachable through the supported HTTP route with no corruption and no
exotic setup, measured at **6.6–10.7% airtime occupancy against a 1.000% budget**, which is the pre-fix
number CRITICAL 3 was raised about.

**Do not archive.** CRITICAL 3 is closed for the case it was raised about and open as the rule the
remediation set out to prove.

**Push: yes, and I would.** Every commit in `bd163d4..HEAD` is a strict improvement on what is already
on `origin/main`; all five gates are green with zero new format drift; seven of eight findings are
genuinely closed with independent measurement behind each. BEHAVIOUR 1 is a pre-existing gap this
change failed to close, not a regression it introduced — holding the push does not make the station
safer, and it leaves five real fixes unshipped. Push, then open BEHAVIOUR 1 as the next unit of work.

One process note, because it is the same lesson three rounds running: the enumeration that found
BEHAVIOUR 1 is the one `apply-progress-batch3a.md:27` already did. The call site *was* found. What was
missing was a measurement of the decision taken about it.
