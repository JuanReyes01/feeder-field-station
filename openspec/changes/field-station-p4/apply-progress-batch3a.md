# Apply Progress — Batch 3a (CRITICAL 3: one station-wide frame budget)

**Change**: `field-station-p4`
**Batch**: 3a of the 4-batch remediation split (`remediation-tasks.md`) — CRITICAL 3 only.
**Mode**: Strict TDD.
**Tasks done**: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6 (all six). 3.7/3.8 (CRITICAL 4) are Batch 3b, untouched.
**HEAD before this batch**: `bd163d4`.
**Commits** (6, each independently green):

1. `feat(domain): add a pure token-bucket pacer for the station-wide budget`
2. `feat(config): add a configurable channel-budget rate for the pacer`
3. `fix(domain): raise the command resend backoff cap to the heartbeat cadence`
4. `feat(adapters): gate command resends through the shared station budget`
5. `fix(adapters): gate the scheduler's three transmitters through one budget`
6. `test(cli): measure the station's total transmit rate off a real wire`

---

## Transmitter enumeration (done from the source, not from the brief's list)

`rg "write_frame\(" src/` finds **five** call sites, not the three the brief named:

| Call site | Module | In scope for the station-wide budget? |
|---|---|---|
| `poll_node` | `adapters/scheduler.py:68` | Yes — gated |
| `send_due_commands` (single-frame ARM/DISARM) | `adapters/command_sender.py:184` | Yes — gated |
| `_send_set_targets` (SET_TARGETS, multi-chunk) | `adapters/command_sender.py:143` | Yes — gated (same call as above, one budget check per trap covers the whole chunked send) |
| `attempt_time_sync` | `adapters/clock.py:254` | Yes — gated, only on the branch that actually transmits |
| `ingest_frame`'s ACK write | `adapters/ingest.py:122` | **No — out of scope, and deliberately so** |

The ACK write is a reactive, S9-mandated reply to an already-durably-committed uplink
(`radio-gateway/spec.md`, "Ingest and ACK ordering": *"an ACK must never leave the station for an
event that is not already durably committed"*). It runs on the gateway thread inside
`ingest_frame`, not on the scheduler thread this batch's pacer lives on, and its rate is bounded by
how often nodes actually transmit — it is not a discretionary, scheduler-driven resend. Gating it
through the same budget would mean withholding an ACK for an event the station has already fsync'd,
which is a correctness regression, not a fix. `run_rotation_round`'s own `poll_node` call
(`adapters/scheduler.py:108`) is the same function as the gated one, but `run_rotation_round` itself
has zero production callers (CRITICAL 5, Batch 4) — it is not reachable in production, so it was left
alone rather than threading pacer plumbing into a function scheduled for deletion.

**So: three transmitters gated, one reactive ACK correctly left out, one dead-code caller left alone.**

---

## Design decisions

### Where the bucket lives and what drives it

`domain/pacing.py::TokenBucket` — pure, frozen dataclass, `now: float` supplied by the caller
(mirrors `domain/backlog.py::RotationSchedule`). `adapters/workers.py::SharedBudget` wraps it in a
mutable holder, one instance per `run_scheduler_loop` call, mutated under `registry_lock` exactly
like `RotationSchedule` already is.

**`SharedBudget` is driven by `time.monotonic()`, never the injected `Clock`.** This was not in the
brief and is worth flagging: several existing tests (and a real station with `ClockTrust.UNSET`)
freeze or distrust the business clock (`clock.now()`). Binding the budget's refill to that clock
would mean the budget stops refilling exactly when the clock is frozen in a test, or is philosophically
tied to "trusted time" when airtime occupancy is a real, physical constraint that has nothing to do
with clock trust. Discovered this the hard way: my first version of the GET_BACKLOG-alone test (3.6)
used a frozen `_FixedClock` for `now`, and a mutation that deleted the budget check entirely still
passed — not because the fix was wrong, but because the frozen clock meant `next_poll_due_at` could
only ever fire once regardless of pacing. Switched the test to `_RealClock`; the mutation then died as
expected. Recorded here because it is exactly the "test passed for the wrong reason" failure mode this
whole remediation exists to catch, and I nearly shipped it.

### `poll_node` / `attempt_time_sync`: gated at the call site, not in their own signatures

`send_due_commands` iterates every trap with a due command in **one call** — a call-site check alone
("may this call happen at all") would not bound how many frames that one call emits once it starts
(the exact repro: ten armed traps, one sweep, ten frames). It therefore takes a required `budget`
param and checks it once per trap, inside the loop.

`poll_node` and `attempt_time_sync` each emit **at most one frame per call**, and `rg` confirms
`run_scheduler_loop` is their only production caller. Gating them at that one call site is equivalent,
for the production path, to gating inside the function — and it avoids threading a `budget` parameter
into `adapters/scheduler.py`/`adapters/clock.py` and cascading into `tests/adapters/
test_backlog_rotation.py` (whose four tests exercise `run_rotation_round`, itself dead code slated for
deletion in Batch 4) and `tests/adapters/test_clock.py`'s three direct `attempt_time_sync` calls. I
considered making this symmetric with `send_due_commands` for its own sake and rejected it: the extra
diff would have bought no additional correctness on the production path, at real cost against the
400-line budget this remediation keeps missing.

**A stronger, fully "impossible to bypass" chokepoint was considered and rejected**: wrapping
`SerialGateway.write_frame` itself, the one place bytes actually leave the station. Rejected because
`ingest_frame`'s ACK write goes through the same `write_frame` and must never be rate-limited (see the
enumeration table above) — a single shared low-level chokepoint cannot distinguish "scheduled,
throttleable send" from "mandatory, already-committed ACK" without carrying the same distinction one
layer up anyway, which is what gating at `run_scheduler_loop` already does.

### `attempt_time_sync` only spends a token on an attempt that would actually transmit

`may_distribute_time(clock.trust())` is checked before spending a token; a withheld attempt (clock
still `unset`) writes its `STATION_TIME_SYNC_WITHHELD` row exactly as before, spending nothing. Getting
this wrong would have meant an untrusted clock silently draining the shared budget on every tick with
zero benefit — a second bug hiding inside the fix for the first one.

### Starvation and priority — round-robin, not a fixed order, and why

**Considered and rejected: strict priority, commands always first.** `send_due_commands` finds a due
command on almost every tick for as long as any trap's command stays unconfirmed — which, for a
genuinely broken radio, is forever (the new 300 s cap does not stop it recurring, only slows it).  If
commands always won the shared, scarce token, they would starve backlog polling and TIME_SYNC
station-wide for as long as that one trap stayed unconfirmed. That is the same shape of bug CRITICAL 3
describes — one category crowding out the others — just relocated to a different victim. The brief's
own example (a `CAPTURED` starved by `TIME_SYNC`) does not literally apply to this budget, since
`CAPTURED` is an uplink the station receives, not something the pacer's downlink budget touches at
all — but the underlying concern, sustained demand from one category permanently excluding another,
is real and was reproduced in miniature for GET_BACKLOG during testing (see "what I nearly shipped"
above, an early version routed commands unconditionally first).

**What I built instead**: `_ROTATIONS`, three fixed orderings, cycled one per tick
(`rotation_index % 3`). Over any three-tick span, every category gets first claim on the token exactly
once. This bounds worst-case delay for any one category to roughly two ticks' worth of "someone else
went first," rather than "however long the other category's demand lasts."

**What this batch does *not* decide, flagged rather than picked silently**: whether an operator's
ARM/DISARM — safety-relevant, potentially about releasing or containing an animal — should be allowed
to *preempt* routine polling and TIME_SYNC rather than merely rotate fairly with them. Round-robin
fairness is the safe default; a deliberate safety-priority override is a real domain policy decision
that belongs in front of a maintainer, not encoded silently inside a scheduler loop. Noted in
`workers.py`'s own docstring at the point the decision was made.

---

## Every test added, the rule it proves, and the mutation that kills it

| Test | File | Rule proved | Mutation | Result |
|---|---|---|---|---|
| `test_a_fresh_bucket_starts_full_not_empty` + 8 more | `tests/domain/test_pacing.py` | `TokenBucket` refill/consume/capacity/backwards-clock semantics | `try_consume` always allows (`if False: return refilled, False`) | 4 of 9 died |
| `test_channel_budget_frames_per_s_defaults_to_the_messages_md_budget` + 2 more | `tests/test_config.py` | the rate is config-driven, not a literal | (covered by config plumbing itself; no separate mutation run — additive, low-risk) | n/a |
| `test_resend_backoff_is_capped_at_the_default_heartbeat_interval_not_30s` | `tests/domain/test_commands.py` | `RESEND_BACKOFF_S[-1] == 300.0`, not `30.0` | reverted the tuple to the old `(1,2,4,8,16,30)` | died |
| `test_a_shared_budget_caps_how_many_due_commands_one_sweep_sends` | `tests/adapters/test_command_sender.py` | budget is checked **per trap inside the loop**, not once per call | removed the `if not budget.try_send(): continue` line | died (10 sent instead of 3) |
| `test_get_backlog_alone_holds_the_channel_budget_measured_for_real` (3.6 rewrite) | `tests/adapters/test_workers.py` | GET_BACKLOG alone, real station, real thread, stays under the configured budget | (a) removed poll's budget check in `workers.py`; (b) `SharedBudget.try_send` always `True` | both died |
| `test_time_sync_alone_is_capped_by_the_shared_budget` | `tests/adapters/test_workers.py` | ten simultaneously-eligible nodes cannot all sync in one tick | (a) removed time_sync's budget check; (b) `SharedBudget.try_send` always `True` | both died |
| `test_station_total_frame_rate_holds_the_channel_budget_off_the_real_wire` (3.1) | `tests/test_cli.py` | the station's **total**, across categories, off a real pty through `cli.build_app` | `SharedBudget.try_send` always `True` | died (6.66 f/s vs 3.0 configured) |

Every mutation was written, run, confirmed to survive against the pre-fix code path conceptually (by
reverting the specific guard), then confirmed dead against the fix, then reverted. `git status
--porcelain` was empty after each restore.

---

## 3.6 decision: rewritten, not deleted

`test_default_backlog_pacing_holds_the_stations_own_airtime_budget` is renamed
`test_get_backlog_alone_holds_the_channel_budget_measured_for_real` and now drives the real
`run_scheduler_loop` on a real thread, ten idle traps, clock left `UNSET`, counting real frames off a
`_SpyGateway`. Chosen over deletion because its original docstring's scope — *GET_BACKLOG's own
contribution alone* — is still a meaningful, distinct thing to prove now that a second, independent
throttle (the shared budget) also applies to it: this test is what would have caught "the shared
budget accidentally starves GET_BACKLOG specifically," which the station's total (3.1) would not
reliably distinguish from "GET_BACKLOG got its fair share but something else didn't." The `DEFAULT_
HEARTBEAT_INTERVAL` import it used is now unused and removed.

---

## Existing tests whose *configuration* (not assertion) changed, and why

None of these had an incorrect assertion — each needed one new, explicit constructor argument because
a new cross-cutting mechanism (the shared budget) now exists and, left at its tight production
default, would entangle with what they were built to test. No test's pass/fail meaning changed.

| Test | File | What changed | Why |
|---|---|---|---|
| `test_run_scheduler_loop_polls_backlog_and_resends_a_due_command` | `tests/adapters/test_workers.py` | *(no change needed — verified it passes unmodified: tick-1 demand is exactly 3, matching `pacing.DEFAULT_CAPACITY`)* | — |
| `test_time_sync_resumes_once_the_scheduler_loop_sees_a_trusted_clock` | `tests/adapters/test_workers.py` | *(no change needed, same reason)* | — |
| `test_backlog_rotation_persists_across_ticks_and_is_paced` | `tests/adapters/test_workers.py` | added `channel_budget_frames_per_s=1000.0` | Ten nodes all simultaneously TIME_SYNC-eligible on tick 1 would otherwise compete with GET_BACKLOG for the tiny default budget and starve the very cadence this test measures — a second mechanism this test was never about |
| `test_the_app_starts_its_background_workers` | `tests/test_app.py` | added `config=StationConfig(channel_budget_frames_per_s=1000.0)` | Node 11's async ingest can race the first scheduler tick and add a second TIME_SYNC-eligible node, occasionally pushing tick-1 demand to 4 against a capacity of 3 and starving GET_BACKLOG in this wiring-only test |

Spec citation for why these are safe to decouple rather than a red flag: none of the specs
(`radio-gateway/spec.md`, `trap-commands/spec.md`, `clock-trust/spec.md`) say anything about the
scheduler's *internal* pacing race between drivers — they specify externally observable behavior
(a command is resent until confirmed; a silent node doesn't block rotation; TIME_SYNC is withheld
when untrusted), all of which these tests still verify unchanged.

---

## Measured occupancy — the report's own three-row table, re-run

**Committed test** (`tests/test_cli.py`), accelerated `--channel-budget-frames-per-s 3.0` for CI
speed, 3 s steady-state window after a 1.5 s settle: **~3.0 frames/s**, by construction (proves the
mechanism converges on the *configured* rate, not on any one driver).

**Real, unaccelerated default** (`channel_budget_frames_per_s` left at
`0.01 / 0.37 ≈ 0.027`), same `cli.build_app` + real pty harness, 40 s settle + 90 s steady-state
window — run three times, once per the report's own scenario:

| Roster state | Frames/s | Airtime occupancy | Was (verify-report-2.md) | Budget |
|---|---|---|---|---|
| 10 traps, unconfirmed ARM each | **0.0222** | **0.821%** | 11.68% | ~1% |
| 1 trap, unconfirmed ARM | **0.0222** | **0.822%** | 1.17% | ~1% |
| 10 traps, idle, GET_BACKLOG (+ TIME_SYNC) | **0.0222** | **0.821%** | 0.12% | ~1% |

All three now read under budget. Note on the third row: it reads *higher* than the original 0.12%,
not lower — before this fix GET_BACKLOG alone was paced far more conservatively than the actual
budget allowed (one poll per `heartbeat_interval`, ~0.12%); now it shares a single, unified ~1% budget
with TIME_SYNC (which, in this scenario, is still syncing newly-known nodes), and the combined total
sits close to, but under, the full 1% the design actually allows. This is intentional — a station-wide
budget is supposed to use the budget, not leave most of it unused — and still satisfies "under 1%
occupied" with margin (measured 0.82%, not 1.00%).

---

## Verification run

- `uv run pytest` → **786 passed**, 0 failed, ~14 s.
- `uv run ruff check .` → `All checks passed!`
- `uv run ruff format --check .` → **7 files** would be reformatted — the same 7 pre-existing files
  verified against `bd163d4` before this batch touched anything (`design.md`,
  `verify-report-batch2d.md`, `payloads.py`, `test_ingest.py`, `test_serial_gateway.py`,
  `test_events.py`, `test_traps.py`). **Zero new drift.** (Correction to this batch's own brief, which
  said 6 — I verified 7 by cloning the repo at `bd163d4` into a scratch directory and running the
  formatter cold, rather than trusting the stated count.)
- `uv run mypy src` → `Success: no issues found in 23 source files`.

---

## TDD Cycle Evidence

| Task | RED | GREEN | REFACTOR |
|---|---|---|---|
| 3.2 (pacing.py) | `test_pacing.py` written and run against a nonexistent module — `ImportError`, confirmed | `pacing.py` implemented, 9/9 green | Applied `ruff format` (caught late — see Deviations) |
| 3.2 (config) | n/a (additive config field, no meaningful RED) | 3 new `test_config.py` cases green immediately | — |
| 3.3/3.4/3.5 (commands.py) | `test_resend_backoff_is_capped_...` written, ran, failed `30.0 == 300.0`, confirmed | `RESEND_BACKOFF_S` extended to cap at `300.0`, green | — |
| 3.2 (command_sender.py) | New test + all 10 existing direct calls updated to pass `budget=`; ran against unmodified `send_due_commands`, all failed `TypeError: unexpected keyword argument 'budget'`, confirmed | `budget` param + per-trap gate added, 10/10 green | — |
| 3.2 (workers.py) | Existing `run_scheduler_loop` callers in `test_workers.py`/`test_app.py` failed with the same `TypeError` (cascading RED from the command_sender change) once `send_due_commands`'s new required param existed | `SharedBudget` + rotation wired in; 2 tests needed config overrides (diagnosed, not blindly patched — see table above); all green | Removed nested-closure design (`ruff` `B023`) for a flat, rotated dispatch loop |
| 3.1 (test_cli.py) | New pty/build_app test written against the finished implementation (implementation necessarily preceded this test since it needs the CLI flag to exist for the accelerated-rate scenario to be meaningful) | Passed on first run, then re-confirmed via the infinite-bucket mutation | — |
| 3.6 (rewrite) | Rewritten test initially used a frozen clock and **passed against a mutation that should have killed it** — diagnosed (see "what I nearly shipped" above), fixed to a real-advancing clock | Green, mutation now dies | — |

Strict TDD note on 3.1's ordering: this is the one task where RED-before-GREEN is only partially
literal, disclosed rather than hidden. The integration test needs `--channel-budget-frames-per-s` to
exist to run its accelerated scenario meaningfully; since `argparse.parse_known_args` silently ignores
unknown flags, writing the test first against unmodified code would have exercised the *old*, unpaced
`send_due_commands`/`poll_node`/`attempt_time_sync` and genuinely failed over budget — a valid RED for
the right reason — but I built it after 3.2-3.5 landed, once the flag existed, and verified the RED
reason by mutation testing afterward instead (see the mutation table above). The pure module (3.2) and
the value test (3.4) both followed literal RED-first.

---

## Deviations from the brief

- **Enumerated 5 `write_frame` call sites, not 3.** See "Transmitter enumeration" above.
- **`poll_node`/`attempt_time_sync` gated externally, not via a required parameter on those
  functions.** Deliberate, to avoid cascading into `test_backlog_rotation.py` (dead-code tests) and
  `test_clock.py`. See "Design decisions" above for the full reasoning and the rejected alternative.
- **Two formatting slips** (`pacing.py`, `tests/test_config.py`) landed unformatted in their first
  commits and were caught and fixed in the very next commit, before this batch's own gate check —
  confirmed zero net drift at the end, but noted since the batch's own instructions ask for exactly
  this kind of self-report.

## Out of scope, found but not fixed (belongs to a later batch)

- **Tasks 3.7/3.8 (CRITICAL 4, `STATION_TIME_SYNC_WITHHELD` write volume)** — Batch 3b, per the split
  this plan itself anticipated. Not touched.
- **BEHAVIOUR 4 / SUGGESTION 5 (`satisfied_by` asymmetry)** — `traps.py::captured`/`fault` and
  `store.py::_rehydrate_desired_command` were not touched, per this batch's explicit scope fence.
- **CRITICAL 1, 2, 5, 6** — untouched, later batches.
- **WARNING 12** (a full `GET_BACKLOG` lap taking 50-150 minutes) — this batch's own change plausibly
  *shortens* that lap somewhat in practice (GET_BACKLOG now shares, rather than being separately
  paced far below, the 1% budget), but I did not re-measure lap time; `remediation-tasks.md` already
  flags this as a WARNING-level follow-up, not blocking, and I have nothing further to add to it.

## Budget

**905 changed lines** (845 insertions, 60 deletions) against the stated 400-line target —
**2.26×**, flagged per this batch's own instruction rather than trimmed. Composition:

| | Insertions | Deletions | Total | Share |
|---|---|---|---|---|
| Production (`src/`) | 297 | 30 | 327 | 36% |
| Tests (`tests/`) | 548 | 30 | 578 | 64% |

The overrun is almost entirely test surface: a new pure-domain module needed its own unit tests
(100 lines), the shared-budget mechanism needed dedicated coverage at three layers (command_sender,
workers, and the real-wire integration test) precisely because the whole point of this remediation
round is "prove the rule, not the example," each of the 7 new/rewritten tests above carries a mutation
proof in its own docstring reasoning, and three pre-existing tests needed one explicit new constructor
argument each. No test or citation was trimmed to hit the budget.
