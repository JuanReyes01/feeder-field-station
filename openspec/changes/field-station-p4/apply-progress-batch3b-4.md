# Apply Progress — Batch 3b (CRITICAL 4) + BEHAVIOUR 4/SUGGESTION 5

**Change**: `field-station-p4`
**Mode**: Strict TDD.
**HEAD before this run**: `f2c2f19`.
**Status**: Parts 1 and 2 done, both green, independently committed. **Part 3 (Batch 4, CRITICAL 5)
was NOT started** — stopped at the 500-line budget before touching it. See "Budget" below.

**Commits** (2):
1. `fix(adapters): cap the withheld TIME_SYNC write rate to the heartbeat cadence` (Part 1)
2. `fix(domain): make CAPTURED satisfy commands, keep FAULT from assuming state` (Part 2)

---

## Part 1 — CRITICAL 4: the withheld-TIME_SYNC write volume (tasks 3.7, 3.8)

**Root cause**: `run_scheduler_loop`'s `time_sync` branch made every still-unsynced node "due" on
every tick. `attempt_time_sync` unconditionally writes a `STATION_TIME_SYNC_WITHHELD` row before
returning `False` (clock-trust/spec.md's own "MUST log each withheld send") — correct per-attempt,
unbounded per-second with an unset clock and a multi-node roster.

**Fix**: took the task file's first named option. A per-node `last_withheld_attempt_at:
dict[int, float]`, driven by `time.monotonic()` (not the injected `Clock` — several tests and a real
`unset` station legitimately freeze or distrust it, the same reasoning `SharedBudget` already
established for its own refill clock in batch 3a). Gates only the *withheld* branch
(`may_distribute_time(clock.trust())` is `False`): an unsynced node is attempted, and logged, at most
once per `config.heartbeat_interval`. The *would-send* branch (clock trusted) is untouched — no new
gate, no interaction with `SharedBudget` — so `test_time_sync_resumes_once_the_scheduler_loop_sees_a_
trusted_clock`'s claim about resuming stays correct and needed no change. `domain/pacing.py`'s
`TokenBucket` was deliberately **not** reused here: it is a *rate* limiter (tokens/second, shared
across categories); this is a *per-node cadence* gate, the same shape `next_poll_due_at` already uses
for GET_BACKLOG — a second pacing primitive was not needed.

**Test added**: `tests/adapters/test_workers.py::test_withheld_time_sync_rows_are_bounded_by_the_
heartbeat_cadence_not_the_tick_rate`, parametrized over `node_count` (3, 10) x `window_s` (0.4, 0.8) —
4 cases. Property-shaped per the brief's own requirement: an upper bound on rows/second
(`2.0 * node_count / heartbeat_interval_s`) that must hold regardless of roster size or how long the
loop has run, not "at least one row exists" (that claim is `test_time_sync_resumes_...`'s, untouched).

**Mutation**: disabled the cadence gate (`if False and last_attempt is not None and (...)`). All 4
parametrized cases died (measured 899-911 rows/s against a ~133/s bound). Restored; `git status
--porcelain` empty after.

**Repro re-run** (10 traps, unset clock, production `tick_s=1.0`, `heartbeat_interval` at its
5-minute default, 10 s window):
```
STATION_TIME_SYNC_WITHHELD rows written: 10 in 10.0s = 1.000 rows/s
```
Bounded, not the pre-fix 10.0 rows/s (each of the 10 nodes gets its one immediate attempt at boot;
none is due again inside the 10 s window since the next attempt per node is not due for another
~290 s).

---

## Part 2 — BEHAVIOUR 4 / SUGGESTION 5: the `satisfied_by` asymmetry

**Enumeration done from source**: `rg 'satisfied_by' src/feeder_station` finds exactly 2 production
call sites before this fix — `Registry.heartbeat()` (traps.py) and `_rehydrate_desired_command`
(store.py) — confirming the report's claim that `captured()`/`fault()` never called it at all.

**Decision, checked against the specs before relying on it** (as instructed):
- `docs/requirements.md:30`, S4: *"armed state ... all sourced from the trap's own heartbeat, never
  from what the station last commanded."* `domain/traps.py`'s own module docstring states the same
  rule as *"S4 — state is reported, never assumed."*
- `src/feeder_station/domain/payloads.py::decode_captured` decodes a real `state_after` byte (pinned,
  validated range) — a genuinely reported fact, the same standing CRITICAL A already gave it for
  display. **So CAPTURED now satisfies a command**, via `Registry.captured()` running `satisfied_by`
  exactly like `heartbeat()` does.
- `src/feeder_station/domain/payloads.py::decode_fault` decodes only `code`/`detail` — **no state
  field at all**. `Registry.fault()` sets `trap.state = TrapState.FAULT` as this module's own
  convention for display, not a reported fact. Treating "something is wrong" as "not armed" is
  precisely the assumption S4 rules out — a faulted trap may still be physically armed. **So FAULT
  deliberately does NOT satisfy a command** — `fault()` is intentionally left without a `satisfied_by`
  call, and its docstring now says so explicitly.

This reading held up against the specs (no spec line contradicts it), so it was implemented as read
rather than pushed back on.

**Live-side fix**: `Registry.captured()` (`domain/traps.py`) now runs the same DD15 `satisfied_by`
check `heartbeat()` already runs, using `state is TrapState.ARMED` from the reported `state_after`.
`fault()` is unchanged in behaviour; its docstring now states the omission is deliberate.

**Rehydration-side fix**: `_ReportedFact` (`adapters/store.py`) gained a `msg_type` field, set at each
of the three `_rehydrate_heartbeat`/`_rehydrate_capture`/`_rehydrate_fault` call sites.
`rehydrate()`'s state-race loop now threads `winner_msg_type` through to `_rehydrate_desired_command`,
which nulls out `reported` (treating it exactly like "nothing is known about this node yet") whenever
the winning state-bearing row was a FAULT — mirroring `fault()`'s own live-side omission. HEARTBEAT and
CAPTURED wins are unaffected: they already ran (and, for CAPTURED, still run) the same `satisfied_by`
check the live path now also runs.

**Fixed the class of bug, not the instance**: `tests/test_cli.py`'s five-ordering property test
compared exactly `("state", "last_seen", "reachable")` — a hand-picked tuple `pending_command` was
never added to, across six verification rounds, which is exactly why this hid. The comparison axis is
now built by `_compared_view_fields()`, enumerating every field name off `Trap`'s own
`dataclasses.fields()` and asserting every one has a `view()`-key mapping — `assert not missing` fails
the whole test module at *collection* time if a future `Trap` field has no entry. Verified by mutation:
removing the `pending_command` entry made `tests/test_cli.py` fail to collect at all
(`AssertionError: Trap field(s) {'pending_command'} have no view()-key mapping`). Restored.

**Tests added**:
- `tests/domain/test_traps.py::test_disarm_pending_clears_when_a_captured_reports_not_armed` — proves
  a CAPTURED with a not-armed `state_after` satisfies a pending DISARM. Mutation: disabled the
  `satisfied_by` call inside `captured()` — died (`pending_command` stayed `DISARM`, not `None`).
- `tests/domain/test_traps.py::test_a_fault_report_does_not_satisfy_a_pending_disarm` — pins that a
  FAULT report leaves a pending DISARM untouched. This one was already true before any change here
  (not a new RED-to-GREEN cycle — disclosed rather than hidden, per this run's own instructions);
  proven load-bearing by mutation: adding a `satisfied_by` call to `fault()` made it die
  (`pending_command` went from `DISARM` to `None`).
- `tests/test_cli.py::test_a_pending_command_agrees_live_vs_rehydrated_for_every_report_type` — the
  18-case property (2 pending commands x 9 report shapes: 4 HEARTBEAT states, 4 CAPTURED
  `state_after`s, 1 FAULT). Each case asserts both live and rehydrated pending-command values equal
  `_behaviour_4_expected_pending()`'s explicit statement of the rule, not merely each other. Mutation:
  disabled the FAULT gate in `_rehydrate_desired_command` — died on exactly `fault/disarm`, the report's
  own sharpest-diverging case (`rehydrated=None` vs `live='disarm'`).

**Existing test that needed no change, but whose scope silently widened**:
`test_rehydrated_registry_matches_the_live_registry_for_every_ordering` (the five-ordering property
test) — its assertion loop now iterates `_COMPARED_VIEW_FIELDS` instead of a 3-item tuple, so it
started comparing 12 view keys instead of 3, on the same five logs, with no change to the logs
themselves. All five cases still pass unmodified — none of their five logs exercises an operator
command, so `pending_command`/`desired_targets_version` are trivially `None` on both sides in every
case; the wider axis proved nothing new for *this* test, but keeps a future field from silently
dropping off it again.

**18-combination repro, re-run, real values**:
```
pending  report           LIVE       REHYDRATED agree?
arm      heartbeat_idle   arm        arm        True
arm      heartbeat_armed  None       None       True
arm      heartbeat_fired  arm        arm        True
arm      heartbeat_fault  arm        arm        True
arm      captured_idle    arm        arm        True
arm      captured_armed   None       None       True
arm      captured_fired   arm        arm        True
arm      captured_fault   arm        arm        True
arm      fault            arm        arm        True
disarm   heartbeat_idle   None       None       True
disarm   heartbeat_armed  disarm     disarm     True
disarm   heartbeat_fired  None       None       True
disarm   heartbeat_fault  None       None       True
disarm   captured_idle    None       None       True
disarm   captured_armed   disarm     disarm     True
disarm   captured_fired   None       None       True
disarm   captured_fault   None       None       True
disarm   fault            disarm     disarm     True

18/18 agree
```
**Pre-fix baseline, verified by actually re-running the same script against the genuine pre-fix
`traps.py`/`store.py`** (temporarily restored from `f2c2f19` in the real package root, run, then put
back — not derived from the report's table by assumption):
```
arm   captured_armed   LIVE=arm     REHYDRATED=None    *** diverges
disarm captured_idle   LIVE=disarm  REHYDRATED=None    *** diverges
disarm captured_fired  LIVE=disarm  REHYDRATED=None    *** diverges
disarm captured_fault  LIVE=disarm  REHYDRATED=None    *** diverges
disarm fault           LIVE=disarm  REHYDRATED=None    *** diverges

13/18 agree pre-fix -> 18/18 agree post-fix
```
Exactly the report's own 5-of-18 count and exactly its own listed rows (`verify-report-batch2d.md`'s
table), confirmed empirically rather than trusted from either that report or this run's own earlier
derivation.

---

## Part 3 (Batch 4, CRITICAL 5) — NOT STARTED

Stopped here per this run's explicit budget instruction ("Under 500 changed lines for all three
parts... If you approach it, stop and report rather than pressing on"). Parts 1+2 alone:

```
git diff --stat f2c2f19 HEAD -- src/ tests/
 6 files changed, 453 insertions(+), 7 deletions(-)   (460 changed lines, code+tests only)

git diff --stat f2c2f19 HEAD   (including this file's own remediation-tasks.md edits)
 7 files changed, 487 insertions(+), 9 deletions(-)   (496 changed lines, everything)
```

496/500 already spent (or 460/500 counting only `src/`+`tests/`, matching batch 3a's own accounting
convention). Batch 4's own per-batch forecast (`remediation-tasks.md`'s Review Workload Forecast
table) estimated CRITICAL 5 alone at **150-250 changed lines** — deleting `run_rotation_round` and its
four-test file, writing a new production-loop poll-order test, editing an existing assertion (4.3), and
a docstring fix (4.5). Doing any part of that now would put the combined total at roughly 610-750
lines, well past the 500-line ceiling this run was explicitly told not to exceed, on a remediation
whose own history is nine consecutive rounds of budget overrun.

**`remediation-tasks.md` tasks 4.1-4.5 remain unchecked (`[ ]`).** Nothing in `Batch 4`'s section was
touched: `run_rotation_round`, `poll_node`'s re-export, `tests/adapters/test_backlog_rotation.py`, and
`tests/adapters/test_workers.py::test_backlog_rotation_persists_across_ticks_and_is_paced` are all
exactly as they were at `f2c2f19`.

---

## Verification run (Parts 1+2 only)

- `uv run pytest` → **810 passed**, 0 failed (~16 s). (786 baseline + 4 (3.7) + 2 (domain) + 18
  (behaviour 4 property) = 810.)
- `uv run ruff check .` → `All checks passed!`
- `uv run ruff format --check .` → **7 files** would be reformatted — the same 7 pre-existing files.
  Verified independently against `504d18e` (not trusted from a prior batch's number): a `git worktree
  add --detach` at `504d18e` shows **6** files unformatted (`design.md`, `payloads.py`,
  `test_ingest.py`, `test_serial_gateway.py`, `test_events.py`, `test_traps.py`); the 7th
  (`verify-report-batch2d.md`) was added in a later commit (`bd163d4`), before this run started. **Zero
  new drift** from this run's own changes.
- `uv run mypy src` → `Success: no issues found in 23 source files`.

## TDD Cycle Evidence

| Task/Test | RED | GREEN | REFACTOR |
|---|---|---|---|
| 3.7/3.8 (withheld TIME_SYNC cadence) | 4 parametrized cases written, run against unmodified `workers.py`, failed (899-911 rows/s vs ~133/s bound), confirmed | `last_withheld_attempt_at` cadence gate added, all 4 green | Mutation (`if False and ...`) re-confirmed the RED reason; restored |
| `captured()` satisfies a command | RED written and run, failed (`pending_command` stayed `DISARM`) | `satisfied_by` call added to `captured()`, green | Mutation disabling the call re-killed it; restored |
| `fault()` does not satisfy a command | Written already-passing (disclosed, not new RED — the omission predates this run) | N/A | Mutation *adding* `satisfied_by` to `fault()` killed it, proving the pin is load-bearing; restored |
| BEHAVIOUR 4 18-case property | Written, run against domain fix alone (store.py untouched): 17/18 passed, `fault/disarm` failed (`rehydrated=None` vs `live='disarm'`), confirmed | `_ReportedFact.msg_type` + FAULT gate added to `_rehydrate_desired_command`, 18/18 green | Mutation disabling the gate re-killed exactly `fault/disarm`; restored |
| Field-enumeration guard | N/A (a refactor of an already-passing test's comparison axis) | Five-ordering test still green after the axis widened from 3 to 12 keys | Mutation (removed `pending_command` from the mapping) failed the whole module at collection, as designed; restored |

## Issues found, not fixed (this run)

- **Batch 4 (CRITICAL 5)** — see "Part 3" above. Deliberately not started; budget.
- **Batch 5 (CRITICAL I, corrupt `station.db` bricking `Store.__init__`)** — explicitly descoped by
  the user for this run (prototype, not yet deployed). Left in `remediation-tasks.md` as a recorded,
  unscheduled item, untouched.
- Nothing else was found in scope during this run that was not fixed.
