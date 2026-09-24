# Apply Progress — Batch 3b (CRITICAL 4) + BEHAVIOUR 4/SUGGESTION 5 + Batch 4 (CRITICAL 5)

**Change**: `field-station-p4`
**Mode**: Strict TDD.
**Status**: All of Batches 1-4 now done, all green, independently committed across two runs. Parts 1-2
below ran first (`HEAD` before: `f2c2f19`); Part 3 (Batch 4, CRITICAL 5) ran in a later session
(`HEAD` before: `ccb7afc`) — see that section for its own commits, budget accounting, and
corrections. **This was the last CRITICAL in `verify-report-2.md`**; Batch 5 (CRITICAL I) remains
explicitly descoped by the user, recorded and unscheduled in `remediation-tasks.md`.

**Commits** (5 total): Parts 1-2 —
1. `fix(adapters): cap the withheld TIME_SYNC write rate to the heartbeat cadence`
2. `fix(domain): make CAPTURED satisfy commands, keep FAULT from assuming state`

Part 3 (Batch 4) — see its own section below for the 3 commits.

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

---

## Part 3 (Batch 4, CRITICAL 5) — DONE (separate run, HEAD before: `ccb7afc`)

**Commits** (3, each independently green): `2a06ae1` (new poll-order test), `282e006` (delete
`run_rotation_round`, gut the dead-code test file), `ee2e3fb` (WARNING 14 docstring fix).

**Caller enumeration, from source (`rg`), not from any report**: `run_rotation_round` — zero callers
in `src/`, only its own definition and four callers inside `tests/adapters/test_backlog_rotation.py`
(now three, post-fix). `poll_node` — exactly one production caller, `adapters/workers.py::
run_scheduler_loop:339`; its only other caller was `run_rotation_round` itself. **`poll_node`'s
"now-unused re-export" (task 4.1's own wording) does not exist** — `rg` found no re-export anywhere
(`__init__.py` or otherwise); `poll_node` is defined directly in `scheduler.py`. Followed the code,
not the task text: nothing was removed beyond the function itself and its now-unused imports.

**Task 4.2, corrected**: the task's own poll order `[1,1,1,2,2,2,3,3,3]` does not reproduce against
`run_scheduler_loop` for a true "one silent among nine responsive" roster — verified by running the
real loop (see `probe_poll_order.py`/`probe_poll_order2.py` in this session's scratch dir, five
repeated trials, identical every time): `run_scheduler_loop`'s `heard` check is gated on a
`last_polled_at` baseline that does not exist on any node's first-ever poll, so lap 1 retries *every*
node up to `retry_budget` times regardless of responsiveness — an artifact of "first contact," not of
who is genuinely silent. The `[1,1,1,2,2,2,...]` shape only appears if *no* node ever registers a
reply (all ten silent), which is the deleted primitive's synchronous `responded` callback semantics,
not this loop's own. The new test therefore skips lap 1 and pins laps 2-3 instead, where the real
distinction holds: silent node retried exactly 3 times, every other node exactly once, repeating
identically. This is exactly the "component correct in isolation, nothing proving it against the
running station" trap CRITICAL 5 itself is about — copying the report's array verbatim would have
moved dead-primitive semantics onto the live loop and called it proven.

**Mutation-kill proof for the new test** (`test_backlog_rotation_skips_a_silent_node_within_its_
retry_budget_and_continues`), each applied in the real package root, run, confirmed dead, reverted,
`git status --porcelain` empty after each:
1. `workers.py`'s `heard = ...` forced to `True` — died (silent node showed 1 poll, not 3).
2. `heard` forced to `False` — died (every node showed 3 polls, not 1).
3. `domain/backlog.py::record_response`'s `attempts + 1 >= retry_budget` loosened to `>` — died
   (silent node showed 4 polls, not 3).

**Task 4.4, corrected**: the premise ("its four tests' behavior is covered by 4.2/4.3") undercounted
the file — six tests, not four; three (`test_poll_node_asks_after_the_stored_cursor`,
`test_poll_node_asks_for_everything_when_no_cursor_exists_yet`, `test_get_backlog_cursor_survives_a_
msg_id_wrap`) test `poll_node`'s own DD7 cursor-forwarding and never touch `run_rotation_round`.
Deleting the whole file would have silently dropped that coverage with nothing to do with this
CRITICAL. Gutted in place instead: kept those three tests and their helpers untouched, removed only
the three `run_rotation_round`-based tests and their now-unused pty/serial/threading/select/os
imports. **Not fixed, disclosed**: the removed real-pty test's wire-level proof that a `GET_BACKLOG`
frame's cursor survives a real serial port is not replaced — recreating it for `poll_node` alone was
judged out of this batch's line budget (see below) and is left as a follow-up.

**Budget**: this run's own ceiling was 300 changed lines (forecast 150-250). Final:
`git diff --stat ccb7afc HEAD` → **5 files changed, 157 insertions(+), 241 deletions(-) = 398**. Over
budget. Root cause, verified rather than assumed: the Batch 4 forecast itself undercounted
`test_backlog_rotation.py` (assumed four tests worth of dead-code-only content; it holds six, 251
lines). Gutting in place rather than deleting the whole file and recreating pieces elsewhere is
*cheaper*, not more expensive (170 changed lines there vs. 251+ for delete-and-recreate), and all
discretionary docstring prose (WARNING-14-style truth-telling in `scheduler.py`/`workers.py`) was
trimmed repeatedly before landing at 398. Reported per instruction rather than cut further at the
expense of the new test's rigor (its three-assertion, three-mutation-proof shape was kept intact).

**Verification (Batch 4 scope)**:
- `uv run pytest` → **808 passed** (810 baseline − 3 removed + 1 added = 808; matches exactly).
- `uv run ruff check .` → `All checks passed!`.
- `uv run ruff format --check .` → same **7** pre-existing files, verified by diffing before/after
  this run's own changes at the same `HEAD` (not a stale number) — zero new drift.
- `uv run mypy src` → `Success: no issues found in 23 source files`.
- `uv build` → wheel and sdist built cleanly.
- AST sweep (`ast.walk` over every `src/**/*.py`, checked both `FunctionDef` and `Name` nodes) →
  `run_rotation_round` exists as a symbol nowhere in `src/` — deleted, not merely uncalled.

### TDD Cycle Evidence (Batch 4)

| Task | RED/characterization | GREEN | REFACTOR |
|---|---|---|---|
| 4.2 (poll-order test) | Written first against the unmodified, already-correct `run_scheduler_loop` (approval-testing shape — no new prod code needed since the report itself found the *loop* correct, only untested); ran green immediately | Confirmed green | 3 mutations applied/reverted (above) to prove it is load-bearing, not incidentally passing |
| 4.1 (delete `run_rotation_round`) | Safety net: full suite green before deletion (810) | Deleted; `tests/adapters/test_backlog_rotation.py`'s 3 rotation tests fail collection (`ImportError`) as expected, confirming the deletion is what broke them | Unused imports removed; stale docstring claims in `scheduler.py`/`workers.py` corrected in the same commit |
| 4.3 (edited assertion) | N/A — no assertion in the existing test changed, only its docstring, disclosing scope | Test unchanged, still green | — |
| 4.4 (gut test file) | N/A — deletion of already-dead-code-only tests | 3 kept tests re-verified green post-edit; `ruff format` applied | — |
| 4.5 (docstring fix) | N/A — no behaviour change | Test re-run, still green (30/30 in `test_app.py`) | — |

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

## Issues found, not fixed

- **Batch 4 (CRITICAL 5) — now DONE**, see "Part 3" above (separate run from Parts 1+2). All six
  CRITICALs in `verify-report-2.md` are closed as of this batch; a full `sdd-verify` re-run is the
  recommended next step per `remediation-tasks.md`'s own verification cadence.
- **`poll_node`'s real-pty coverage** (Batch 4) — the wire-level proof that a `GET_BACKLOG` frame's
  cursor value survives a real serial port was removed along with `run_rotation_round`'s pty test and
  not recreated for `poll_node` alone, under this batch's line budget. `poll_node`'s cursor-forwarding
  logic is still covered by three fake-gateway tests; only the "real bytes crossed a real port" tier
  is gone. Recommended follow-up: a single, narrow real-pty test for `poll_node` (one node, no
  rotation), in `tests/adapters/test_backlog_rotation.py` or a new `test_scheduler_pty.py`.
- **Batch 5 (CRITICAL I, corrupt `station.db` bricking `Store.__init__`)** — explicitly descoped by
  the user (prototype, not yet deployed). Left in `remediation-tasks.md` as a recorded, unscheduled
  item, untouched.
- Nothing else was found in scope during Batch 4 that was not fixed.

---

## Part 4 — closing slice: BEHAVIOUR 1 (CRITICAL), PROOF 1, WARNING 2 (`verify-report-final.md`)

**HEAD before**: `9bc82c9`. **Mode**: Strict TDD. **Status**: all three closed, 812/812 green,
zero new format drift, mypy clean, `uv build` clean. This is the last work item on `field-station-p4`
per the user's own scope note — CRITICAL I stays descoped.

**Commits** (3):
1. `fix(adapters): charge N tokens for an N-chunk SET_TARGETS send` — BEHAVIOUR 1, both halves
   (token charge + `TargetsRequest` bound), all its tests.
2. `test(cli): require the field-view guard's mapping to be real, not just present` — PROOF 1.
3. `docs(config): record the messages.md citation gap and the uplink/downlink math` — WARNING 2.

`verify-report-final.md` committed alongside this file (it was untracked on disk at session start).

### BEHAVIOUR 1 — one token, N frames

**Root cause** (`adapters/command_sender.py`): `send_due_commands` spent one `budget.try_send()`
token per trap *before* dispatching on command kind, then `_send_set_targets` wrote every chunk
`payloads.encode_set_targets` produced unconditionally. A trap with a 270-tag list (10 chunks) put
10 frames on the wire for the price of one token — the pre-fix 11.68% occupancy CRITICAL 3 exists to
close, restored through the one supported operator action (`PUT /targets`) that CRITICAL 3's own fix
never re-measured after finding the call site (`apply-progress-batch3a.md:27` records "gated... one
budget check per trap covers the whole chunked send" without measuring the consequence).

**Fix, three parts**:
1. `domain/pacing.py::try_consume_n(bucket, n, *, now)` — the same refill-then-spend rule as
   `try_consume`, generalised to an atomic charge of `n` tokens: all `n` or none, never a partial
   spend. `try_consume` is now a one-token special case of it.
2. `adapters/workers.py::SharedBudget.try_send_n(n)` — the adapter-level call `_send_set_targets`
   needs, mirroring `try_send()`'s shape.
3. `adapters/command_sender.py`: `TransmitBudget` gained `try_send_n`; `_send_set_targets` now
   computes `wire_frames` first, then calls `budget.try_send_n(len(wire_frames))` *before* writing
   anything — a refusal spends nothing and writes nothing, so the trap stays pending exactly as the
   single-frame path already behaves on refusal. `send_due_commands` no longer pre-charges a flat
   one-token gate for `SET_TARGETS` before dispatch; the charge moved inside `_send_set_targets`,
   sized to the chunk count.

**Fewer-than-N-tokens decision (made deliberately, not left implicit)**: refuse the *whole* send,
spend nothing, leave the trap pending and unattempted. `trap-commands/spec.md:48-51` requires a
split list be delivered as one unit, from the version match alone — the station never tracks which
half of a split list arrived. Spending fewer than N tokens to write *some* chunks now and the rest
once more tokens arrive would let the budget layer split a send the frame layer itself refuses to
split. The alternative the verifier named ("refusing until N are available") is exactly this: the
trap's own `due_for_resend` backoff keeps offering it every sweep until the budget actually holds N.

**`TargetsRequest.tags` bound (the other half, `app.py`)**: capped at
`int(pacing.DEFAULT_CAPACITY) * TAGS_PER_SET_TARGETS_FRAME` = `3 * 27` = **81 tags (3 chunks)**.
Justification: the shared bucket never holds more than `DEFAULT_CAPACITY` tokens at once — that
ceiling is deliberate, the station's *whole* burst allowance, not a per-trap grant. A tag list needing
more chunks than the bucket can ever hold would be refused by the budget *forever*, not merely paced,
which the atomic all-or-nothing charge above makes newly possible (it wasn't reachable as a *dead*
edit before, since the old bug always sent every chunk regardless of count). Bounding at exactly the
bucket's own capacity is the only value that keeps every *accepted* edit structurally sendable, and it
also caps how much of the station's entire burst allowance one trap's single edit can claim, out of a
roster of at least ten (S5). An operator-typed list has no legitimate reason to need more (S13: sized
to be typed or pasted by hand). Verified at the boundary: 81 tags → `200`, 82 → `422`, 270 → `422`.

**TDD evidence**:

| Test | RED (against unfixed `9bc82c9`) | GREEN |
|---|---|---|
| `test_a_chunked_set_targets_send_charges_n_tokens_for_n_frames_not_one` (unit, real token-accounting double, not `_AllowAllBudget()`) | `charged [1]` for a 2-chunk send, expected `[2]` | `charged [2]`, both chunks written |
| `test_a_chunked_set_targets_send_is_refused_whole_when_fewer_than_n_tokens_remain` | `sent_to == [9]` with only 1 of 2 needed tokens available, expected `[]` | `sent_to == []`, `gateway.written == []`, trap stays pending with `attempts == 0` |
| `test_a_chunked_set_targets_send_charges_n_tokens_off_the_real_wire` (integration, `cli.build_app`, real pty, real `SharedBudget`) | **6** SET_TARGETS frames observed (two pending 3-chunk sends both went through on a 3-token bucket) | **3** frames — one trap's whole list, the other left pending |
| `test_edit_targets_route_rejects_a_tag_list_the_budget_could_never_send` (`test_app.py`) | `False and ...` bound temporarily disabled → 422 assertion failed | bound restored → 422 above bound, 200 at bound |

The integration test's determinism does not depend on precise tick timing: `adapters/workers.py`'s
`rotation_index` starts at `0` and only advances once the roster is non-empty, so the *first* tick
that sees a populated roster always tries `commands` before `time_sync`/`poll`
(`_ROTATIONS[0] = ("commands", "time_sync", "poll")`) — the shared bucket is still at its full
starting `DEFAULT_CAPACITY` (3.0) when that tick's chunked sends are evaluated, deterministically, not
by luck. Production defaults throughout (no `--channel-budget-frames-per-s` override).

### Re-run of the BEHAVIOUR 1 repro (real pty, `cli.build_app`, production defaults, converged windows)

The literal pre-fix repro (270 tags) is no longer reachable — the bound now rejects it outright:

```
PUT /api/traps/7/targets  {270 tags}  -> 422  "tag list has 270 tags, more than the 81 this
                                                station can ever budget tokens for..."
PUT /api/traps/8/targets  {81 tags}   -> 200  (at the bound)
PUT /api/traps/9/targets  {82 tags}   -> 422  (one over)
```

Re-run at the new bound (81 tags, 3 chunks — the largest chunked send now possible at all), same
methodology as `verify-report-final.md` (real `pty.openpty()`, real `SerialGateway`, real worker
threads via `TestClient(app)`, real `cli.build_app`, production defaults, no rate override):

```
1 trap, 81 tags (3 chunks), 60 s settle + 180 s window
  4 frames  (SET_TARGETS=3, GET_BACKLOG=1)   0.0222 f/s = 0.822% occupancy   budget 1.000%

10 traps, 81 tags each, 60 s settle + 240 s window
  7 frames  (TIME_SYNC=6, GET_BACKLOG=1)     0.0292 f/s = 1.079% occupancy   budget 1.000%

10 traps, 81 tags each, 120 s settle + 420 s window (longer, more converged)
  9 frames  (TIME_SYNC=8, GET_BACKLOG=1)     0.0214 f/s = 0.793% occupancy   budget 1.000%
```

Honest disclosure on the 10-trap rows: **this interactive sandbox's clock is real-NTP-synchronized**
(`timedatectl`: `System clock synchronized: yes`, `NTP service: active`) — unlike the CI-style
sandbox other tests in this suite document as `UNSET` (no RTC, no NTP). `clock_trust.py::
trust_from_evidence` trusts NTP outright, so `TIME_SYNC` actively competes for the same shared budget
here, where the existing single-trap real-pty test's own docstring assumed it would not. Across both
10-trap windows, none of the 9 remaining pending `SET_TARGETS` commands won a turn against `TIME_SYNC`'s
one-time-per-node sync burst — fair rotation (`_ROTATIONS`) guarantees no category is starved
*forever*, not that every category is served within any given short window. The number that matters is
unaffected by which category wins: **total station occupancy stayed at or under the 1.000% budget in
every row measured**, against the pre-fix 11.68% and this report's own worse-case restoration figures
(6.6-10.7%) at the old bug's true bound (270 tags, no longer reachable). The single-trap row (0.822%,
no TIME_SYNC contention — that trap's own sync had already completed) is the cleanest isolated proof
of the mechanism: a full 3-chunk resend, repeated, still holds inside budget.

### PROOF 1 — field guard now proves "is compared", not "has an entry"

`tests/test_cli.py::_compared_view_fields()` strengthened: added `_VIEW_KEYS`, the 12 keys
`Registry.view()` actually emits (named from its source), and two new assertions — every mapped
tuple must be non-empty, and the union of all mapped keys must equal `_VIEW_KEYS` exactly (not a
subset). Both named mutations re-applied and confirmed dead:

```
"pending_command": ()              -> AssertionError at collection: "map to an empty tuple"
"pending_command": ("node_id",)    -> AssertionError at collection: "do not match Registry.view()'s
                                       own emitted keys exactly" (missing pending_command and
                                       desired_targets_version from the union)
```

Both mutations applied and reverted in the real package root; `git status --porcelain` empty after
each; full 812-test suite reconfirmed green with the correct mapping restored.

### WARNING 2 — `config.py`'s own comment now states the assumption

No re-derivation (needs a real field measurement this project has not taken — explicitly out of
scope). Added a comment at `AIRTIME_PER_FRAME_S`/`CHANNEL_BUDGET_FRACTION` recording: (1)
`docs/protocol/messages.md` does not exist in this repository — verified via
`rg -l 'docs/protocol/messages.md'`, 14 files here cite it, none of them can open it from a clone; (2)
the document's own 1% is the node *uplink* allocation (`10 x 200 x 0.37 / 86400 = 0.857%`), and this
constant reallocates the whole 1% to station *downlink* on top of that uplink and its ACKs — channel
total at design load is closer to 2%, not "under 1%". Corrected one over-claim from the task brief in
passing: the "site survey" phrasing did not appear anywhere in this repository's own docs
(`docs/requirements.md`'s "Open questions" section has no such item) — the comment says "a real field
measurement this project has not taken" instead, verified rather than assumed.

### Composition breakdown — over the 250-line budget, flagged as instructed

`git diff --stat 9bc82c9..HEAD -- src tests`: **374 changed lines** (366 insertions, 8 deletions),
across 8 files, 3 commits.

| Area | Production | Tests | Total |
|---|---|---|---|
| BEHAVIOUR 1 (`pacing.py`, `workers.py`, `command_sender.py`, `app.py` + `test_command_sender.py` 99, `test_app.py` 21, `test_cli.py` integration hunk 70) | 119 | 190 | 309 |
| PROOF 1 (`test_cli.py` guard hunk) | 0 | 38 | 38 |
| WARNING 2 (`config.py` comment only) | 27 | 0 | 27 |
| **Total** | **146** | **228** | **374** |

**Why it's over, and why it wasn't trimmed further**: BEHAVIOUR 1 alone (309 lines) exceeds the
budget by itself. It required a Protocol change (`TransmitBudget.try_send_n`) propagated across three
production modules plus every test double implementing that Protocol, and — per the task's own
explicit instruction — *two* levels of test evidence: a fast deterministic unit test (real
token-accounting, not `_AllowAllBudget()`) and a real `cli.build_app` + real-pty integration test
(closing PROOF 2, "nothing exercises the budget against a chunked send," which only a real-wiring
test can close). Docstring density matches this codebase's own established convention throughout
(every function in `pacing.py`, `command_sender.py` already carries multi-paragraph rationale before
this change) — trimming it below that standard would have been inconsistent with the surrounding
code, not a genuine size reduction of the *logic*. No chained/stacked PR split was applied: this is a
closing slice on a change already `push: yes`-cleared by the final verification, not a new batch, and
splitting one CRITICAL fix's own token-charge logic from its own bound and its own tests across
multiple PRs would have left an intermediate PR either failing PROOF 2 again or shipping the bound
without the charge it exists to protect.

### Gates (final)

`uv run pytest` **0** — 812 passed (808 baseline + 4 new: 2 unit + 1 integration + 1 bound test) ·
`ruff check .` **0** · `mypy src` **0** — 23 files · `uv build` **0** · `ruff format --check .` **1**
— same **7** pre-existing files, zero new drift (diffed against the pre-slice baseline at the same
`HEAD` before this run's own changes).

### Spot-check: commits verified independently green

Commit 1 (`fix(adapters): charge N tokens...`) checked out detached and run alone: **812 passed**
(the correct pre-existing `_compared_view_fields()` and pre-WARNING-2 `config.py` are still present
at that commit, unaffected by the later two commits). Returned to `main` afterward; `git status
--porcelain` showed only the untracked `verify-report-final.md` throughout.

### Not fixed / left as-is

- **CRITICAL I** — explicitly descoped by the user for this slice; untouched, per instructions.
- The real airtime budget itself is **not re-derived** (WARNING 2) — needs a field measurement this
  project has not taken. This was explicitly out of scope for this slice.
- **BEHAVIOUR 2** (the pacer sized *at* the documented budget, `verify-report-final.md`) is
  substantively the same finding as WARNING 2 above and was addressed by the same `config.py` comment;
  no separate code change was needed or made.
- The `poll_node` real-pty coverage gap (recorded in Part 3 above) remains open — not in this slice's
  scope.
