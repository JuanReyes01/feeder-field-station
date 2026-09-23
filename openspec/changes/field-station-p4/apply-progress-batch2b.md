# Apply progress — Batch 2b (CRITICAL 6 + WARNING K(a)/K(b), WARNING J's residual)

**Change**: `field-station-p4`
**Source**: `openspec/changes/field-station-p4/remediation-tasks.md`, Batch 2 section, tasks
**2.5, 2.6, 2.7, 2.9, 2.10 only**. Tasks 2.1-2.4, 2.4b, 2.8 (CRITICAL 1, WARNING J) were Batch 2a —
already closed, not touched by this run.
**Mode**: Strict TDD (RED confirmed-failing before every GREEN, evidence table below)
**Starting commit**: `d05f7d5` (docs(field-station-p4): correct batch 2a's inverted ordering labels,
record CRITICAL A closure)
**Scope discipline**: the state-source logic (CRITICAL 1), the `except Exception` guard itself, and
`Registry.captured()` were **not** revisited — all closed in Batch 2a/its closure run. Only the one
`design.md` line task 2.7 names (~line 288) was touched; every other `design.md` line is Appendix A /
WARNING 14 and stays as-is.

## Why 2.5-2.7 and 2.9-2.10 are covered together

2.5-2.7 (CRITICAL 6, the roster) and 2.9-2.10 (WARNING K(a)/K(b), the observability half of the same
`except Exception` mitigation Batch 1 introduced) are independent code paths but the same subject
matter the remediation plan itself groups under "what `rehydrate()` reports when it gives up on
something." Implemented and committed as four separable, independently-green slices in dependency
order: roster fix first (2.5-2.7), then the two `/health`-shape fixes folded into one commit (2.9's
own two halves — K(b) and WARNING J's residual — share a single design decision, see below), then the
logging pins (2.10) last since they depend on nothing else changing.

## 1. CRITICAL 6 — the roster (tasks 2.5-2.7)

**Rule proved**: the rehydration roster walk includes every node with *any* row in the store,
operator-sourced or node-sourced — not "the report's own example survives."

`_known_node_ids()` derived the roster from `SELECT node_id FROM node_cursor`, and `node_cursor` is
populated exclusively by node-sourced events (`Store.append`'s own `if event.source == "node"` guard).
An operator command issued to a node the station had never received a frame from was durably in
`event` but invisible to every rehydration walk.

**Fix chosen: the narrower union**, not the report's own simplest option (`SELECT DISTINCT node_id
FROM event`):

```sql
SELECT node_id FROM node_cursor
UNION
SELECT DISTINCT node_id FROM event WHERE source = 'operator'
ORDER BY node_id
```

Argued in the code's own docstring: this keeps `_known_node_ids` referencing `node_cursor` directly, so
`test_boot_survives_a_broken_node_roster_query` (CRITICAL A's regression guard — DROP TABLE
`node_cursor` must still break the roster query, inside `rehydrate()`'s guard) needed **zero**
modification to keep proving what it already proved. The simpler `SELECT DISTINCT node_id FROM event`
option was tried first and rejected specifically because it broke that test (the roster query would
succeed trivially against an intact `event` table even with `node_cursor` dropped) — a case of "if an
existing test fails because of your change, diagnose before touching it," and the diagnosis here was
that the test was right and the simpler fix was the wrong shape for this codebase, not that the test
needed editing.

`design.md` line ~288 amended to match: "For each `node_id` the roster query returns — the union of
every `node_cursor` row and every operator-sourced event's `node_id` ... (CRITICAL 6)." No other
`design.md` line touched.

## 2. WARNING K(b) + WARNING J's residual (task 2.9) — one `/health`/`rehydrate()` decision, two symptoms

Both halves of this task turned out to be the same underlying gap — "a replay step failed silently,
and the consumer of the result has no way to tell" — at two different granularities (station-wide vs
per-node), so they share one design rationale.

### 2a. `/health.roster_lost` — a distinct field, folded into `ok`

**The problem**: `/health` reporting `rehydration_errors: 1` was byte-for-byte identical for "one
unreadable row among twenty otherwise-healthy nodes" (`/api/traps` still returns most of them) and
"the roster query itself died, registry empty" (`/api/traps` returns zero) — `ok: true` in both. The
second case used to crash the boot loudly (pre-Batch-1); Batch 1's containment made it silent instead.

**Shape chosen, and why, against what `/health`'s actual consumers do**: `Store.roster_lost` (a new
attribute, reset to `False` at the start of every `rehydrate()` call, set `True` only in the
roster-query `except` branch), surfaced as its own `/health.roster_lost` field *and* folded into `ok`
(`ok = workers_ok and link_ok and roster_ok`). Both, not either — this codebase's own `/health` has
exactly one real consumer today, per `docs/deploy.md`: a person running `curl .../health` and reading
`ok` as the top-level "is this healthy" signal ("A healthy response's `workers.gateway.enabled` is..."
is the only guidance the runbook gives; there is no automated poller in this repository). Folding into
`ok` alone would tell that person *something* is wrong but not what, forcing the same "count `/api/traps`
by hand against how many traps should exist" workaround the verification report itself had to do.
A distinct field alone risks being missed by a glance at `ok`, which is the field the runbook's own
"healthy response" language already trains an operator to check first. Precedent: this is the same
"fold connected into ok" choice `link_ok` already made for the gateway link — the exact same repository
already agreed this is the right shape for a station-wide degraded condition, not a per-node
`/api/traps` symptom.

### 2b. `Trap.state_may_be_stale` — WARNING C, WARNING J's own residual

**The problem** (verify-report-batch2a.md, WARNING C — found *by* Batch 2a's own verification, folded
into this task per the report's own instruction): WARNING J closed "no readable state row at all"
(`state` honestly `None`). It left open the mirror case — a node's *newest* state-bearing row is
unreadable, an *older* one is not, and the older, readable report still wins the state race and is
displayed as current with nothing saying a fresher report existed and could not be read. Example from
the report: an older `ARMED` heartbeat displayed as current for a trap whose real last word was a
`FAULT` the station could not read — a false all-clear, structurally the same defect class WARNING J
was raised to fix, one row over.

**Resolution, consistent with 2.9's own shape**: `Trap.state_may_be_stale: bool = False`, set by
`rehydrate()` whenever the winning state-race fact had at least one unreadable sibling row for that
node (tracked per-node in the existing state-step loop, no new pass over the data), surfaced in
`/api/traps` as `state_may_be_stale`, and cleared by the node's own next *live* report
(`heartbeat`/`captured`/`fault` each reset it to `False` — a live report is by definition the newest
thing the station has heard, so there is nothing left to be stale about). This is a distinct signal at
the *per-node* granularity, deliberately not folded into `ok` (unlike 2a's station-wide signal): a
single node's possibly-stale state is not a degraded boot, and folding every such flag into `ok` would
make `ok` flip on ordinary single-row corruption `rehydration_errors` already reports — the same
distinction this whole task exists to introduce (station-wide vs one-row), applied consistently.
Considered dropping the node from the registry entirely instead of flagging it (matching CRITICAL 2's
existing "no other readable row" behaviour) — rejected: the older report is real, useful information
(S4's own "most recent report" framing does not require throwing away the second-most-recent one just
because a newer one existed and got lost), and WARNING J's own closure already established that
"partial-but-honest" beats "all-or-nothing" for this exact class of problem.

## 3. WARNING K(a) — pin the logging (task 2.10)

**The problem**: two mutations survived the entire suite (verify-report-batch1-round2.md, M7/M9):
downgrading `rehydrate`'s per-step skip log to `logger.debug`, dropping the node id and the traceback;
the same shape on `ingest`'s drop log, dropping the exception text. `rg 'caplog' tests/` found nothing
before this batch. `except Exception` was accepted in Batch 1 specifically because every skip is logged
with its node id — the logging *is* the mitigation, and it was entirely unpinned.

**Fix**: no production code changed — both logs were already correct; only the missing coverage was
added.

- `tests/adapters/test_store.py::test_a_skipped_row_is_logged_at_error_with_the_node_id_and_a_traceback`
  — `caplog` at `DEBUG` (so a downgrade to `DEBUG` would still be captured and fail the level
  assertion), asserts `levelno == ERROR`, the node id (`"7"`) is present in the message text, and
  `record.exc_info is not None` (the traceback `logger.exception` attaches).
- `tests/adapters/test_ingest.py::test_a_dropped_frame_is_logged_at_warning_with_the_exception` —
  `caplog` at `WARNING`, asserts `levelno == WARNING` and that the underlying `FrameError`'s own message
  text (`"shorter than"`, for a 3-byte frame) survives into the log record — not a node id, since
  `ingest.py`'s drop log never carried one even before this batch (the `FrameError` path this test
  exercises fails before `frame.node_id` is ever decoded) and the M9 mutation named in the report never
  claimed one was dropped, only the exception text.

## Mutation testing — all three required mutations, real output

All run **in the real package root** (never a worktree — the venv's editable install pins a
`sys.meta_path` finder to `src/`, and a worktree mutation would be silently ignored, producing a false
green). Each applied via a small Python patch script, each reverted with `git checkout --`,
`git status --porcelain` confirmed empty after every revert.

**Mutation 1 — revert the roster query to `node_cursor` alone** (undo CRITICAL 6's fix):

```
FAILED tests/test_cli.py::test_a_command_to_a_never_heard_from_node_survives_a_restart
AssertionError: LOST across the restart -- ...
assert 99 in {}
1 failed
```

KILLED.

**Mutation 2 — make the degraded-boot signal indistinguishable again** (delete
`self.roster_lost = True` from the roster-query `except` branch, leaving only the count):

```
FAILED tests/test_cli.py::test_boot_survives_a_broken_node_roster_query
sqlite3.OperationalError: no such table: node_cursor
(assertion on body["roster_lost"] is True fails -- KeyError avoided since the
attribute still exists and defaults False, so the test fails on the boolean itself)
1 failed
```

KILLED.

**Mutation 3a — downgrade `rehydrate`'s skip log** (`logger.exception(...)` →
`logger.debug("rehydrate: skipped a row")`, both call sites — the state-step skip and the
desired-command skip):

```
FAILED tests/adapters/test_store.py::test_a_skipped_row_is_logged_at_error_with_the_node_id_and_a_traceback
assert 0 == 1
 +  where 0 = len([])
Captured log call: DEBUG feeder_station.adapters.store:store.py:556 rehydrate: skipped a row
1 failed
```

KILLED.

**Mutation 3b — downgrade `ingest`'s drop log** (`logger.warning("ingest: dropped an undecodable
frame: %s", exc)` → `logger.debug("ingest: dropped a frame")`):

```
FAILED tests/adapters/test_ingest.py::test_a_dropped_frame_is_logged_at_warning_with_the_exception
assert 0 == 1
 +  where 0 = len([])
1 failed
```

KILLED.

All four mutations reverted; full suite green afterward (see Verification below).

## Existing tests whose behaviour shifted, with spec citations

1. **`test_boot_survives_a_broken_node_roster_query`** — **before**: asserted only `ok is True` and
   `rehydration_errors == 1`. **After**: also asserts `roster_lost is True`, `ok is False`, and
   `/api/traps == []`. **Spec citation**: `event-store/spec.md` "Registry rehydration at boot" requires
   rebuilding "the last known reported state ... without depending on the node re-sending anything" —
   a station whose entire registry silently failed to rebuild is not meeting that requirement, and
   presenting it as `ok: true` actively hides the failure from the one consumer `/health` has
   (`docs/deploy.md`'s own "confirm it is actually serving" step). This is the rule WARNING K(b)
   (task 2.9) exists to fix; the assertion change is the fix's own acceptance test, not a
   loosening — it is *strictly stricter* than before (three assertions instead of two).
2. No other existing assertion changed. `git diff` for every other file in this batch shows pure
   addition.

## `rg` cross-check: nothing else assumes `rehydration_errors` alone distinguishes these two cases

```
rg -n 'rehydration_errors' src/feeder_station
```

Confirmed the only two production readers are `Store.rehydrate()` (which sets it) and
`app.py::health()` (which now also reads the new `roster_lost` alongside it) — no other code path in
`src/` branches on `rehydration_errors`'s value in a way `roster_lost` needed to be threaded through.

## TDD Cycle Evidence

| Task | RED (confirmed failing, reason) | GREEN | REFACTOR |
|---|---|---|---|
| 2.5 | `tests/test_cli.py::test_a_command_to_a_never_heard_from_node_survives_a_restart` — ran and failed: `assert 99 in {}` (node absent from boot 2's `/api/traps`) | Closed by 2.6's `_known_node_ids` union query | n/a |
| 2.6 | (shares 2.5's RED) | `src/feeder_station/adapters/store.py::_known_node_ids` — union `node_cursor` with distinct operator-sourced `node_id`s | n/a |
| 2.7 | n/a (docs only) | `design.md` line ~288 amended to describe the union roster query | Done alongside 2.6 |
| 2.9 (K(b)) | `test_boot_survives_a_broken_node_roster_query` (edited) + new `test_one_bad_row_does_not_look_like_a_lost_roster` — ran and failed: `KeyError: 'roster_lost'` | `Store.roster_lost` + `/health.roster_lost` folded into `ok` (`app.py`) | n/a |
| 2.9 (WARNING C) | `tests/adapters/test_store.py::test_a_skipped_newer_row_flags_the_older_survivor_as_possibly_stale` + `test_a_fresh_live_report_clears_the_possibly_stale_flag` — ran and failed: `AttributeError: 'Trap' object has no attribute 'state_may_be_stale'` / `'Registry' object has no attribute 'mark_state_possibly_stale'` | `Trap.state_may_be_stale` + `Registry.mark_state_possibly_stale` (`domain/traps.py`) + wiring in `rehydrate()`'s per-node loop (`store.py`) | n/a |
| 2.10 | `tests/adapters/test_store.py::test_a_skipped_row_is_logged_at_error_with_the_node_id_and_a_traceback` + `tests/adapters/test_ingest.py::test_a_dropped_frame_is_logged_at_warning_with_the_exception` — both passed immediately (no production defect; coverage-only task per the report's own framing) | n/a — test-only | Confirmed by mutation testing (M7/M9-shaped, both killed) rather than by a RED phase, since there was no GREEN production change to drive |

Note on 2.10's evidence shape: WARNING K(a) named a missing-test defect, not a code defect — both logs
were already correct. Strict TDD's RED-before-GREEN cycle does not apply cleanly to "add coverage for
already-correct behaviour"; the equivalent discipline applied here was running each test against the
mutated (downgraded) log *first*, by hand, confirming it fails for the stated reason, before trusting
it as a passing regression guard — see Mutation testing above.

## Diff size — over budget, flagged rather than pressed through silently

`git diff --stat d05f7d5..HEAD` (four commits, excludes this doc and the `remediation-tasks.md` ticks,
same convention Batch 2a's own reports used):

```
 openspec/changes/field-station-p4/design.md |   6 +-
 src/feeder_station/adapters/store.py        |  44 ++++++++-
 src/feeder_station/app.py                   |  16 +++-
 src/feeder_station/domain/traps.py          |  33 +++++++
 tests/adapters/test_ingest.py               |  31 ++++++
 tests/adapters/test_store.py                | 120 +++++++++++++++++++++++
 tests/test_cli.py                           | 143 +++++++++++++++++++++++++++-
 7 files changed, 387 insertions(+), 6 deletions(-)
```

**393 changed lines against the 300-line target (31% over).** Composition:

- Production code (`store.py` + `app.py` + `traps.py` + `design.md`): **99 lines** — roughly matches
  the original per-batch forecast's own order of magnitude for a two-CRITICAL/two-WARNING slice.
- Tests (`test_cli.py` + `test_store.py` + `test_ingest.py`): **294 lines**, ~75% of the diff. Per
  commit: CRITICAL 6's two-boot HTTP test (~45 lines incl. docstring), WARNING K(b)'s two `/health`
  tests plus the extended existing one (~95 lines), WARNING C's two store-level tests (~80 lines),
  WARNING K(a)'s two `caplog` tests (~55 lines).

Not trimmed further: every test proves a distinct rule this task explicitly named (the roster survives
a restart; a lost roster reads differently from one bad row; an older report can be flagged possibly
stale; two specific log sites are pinned against the two specific mutations that survived last round),
and every docstring citing S1/S2/S4/CRITICAL 6/WARNING K/WARNING C/WARNING J is this project's own
established convention (`event-store/spec.md`, `trap-commands/spec.md`, or the verify report that found
the defect, named explicitly in-line) — cutting either would mean dropping a proof this task asked for
or breaking the citation trail four prior batches have all preserved under the same instruction.
Flagged here, per instruction, rather than pressed through silently; this is the fifth time in this
remediation's history that a round has gone over its stated target (252/220, 335/150, 610/350, 260/200,
now 393/300) — the pattern itself may be worth a design-time conversation about what this project's
real per-slice budget should be, separate from any one round's composition.

## Verification (Batch 2's own CRITICAL-6 half, run after the final commit)

`uv run pytest`: **748 passed** (742 baseline + 6 new: 1 CRITICAL 6 boot test, 1 WARNING K(b) new test
— the roster-lost test was edited in place, not counted as new — 2 WARNING C tests, 2 WARNING K(a)
`caplog` tests).

`uv run ruff check .`: All checks passed (exit 0).

`uv run ruff format --check .`: exit 1, same 6 files as baseline `504d18e` (`design.md`, `payloads.py`,
`test_ingest.py`, `test_serial_gateway.py`, `test_events.py`, `test_traps.py`), zero new drift —
confirmed directly (one new-drift line introduced mid-batch by a test edit, caught and reformatted
before committing).

`uv run mypy src`: Success: no issues found in 22 source files (exit 0).

**The never-heard-node repro, re-run by hand**:

```
boot1 POST /arm 42 (never heard) -> {'node_id': 42, 'pending_command': 'arm'}
boot1 /api/traps                 -> node 42 present: True pending_command: arm
boot2 /api/traps                 -> node 42 PRESENT {'node_id': 42, 'state': None,
  'state_may_be_stale': False, 'reachable': False, 'trustworthy': False, 'battery_mv': None,
  'targets_version': None, 'pending_command': 'arm', 'desired_targets_version': None,
  'targets': [], 'last_seen': None, 'capture': None}
VERDICT pending n42 : SURVIVED -- roster now unions operator-sourced rows with node_cursor
```

**`trap-commands/spec.md` "resend continues until version confirmed", spot-checked for a never-heard
node specifically** (`PUT /targets` instead of `POST /arm`, same two-boot harness):

```
boot1 PUT /targets 77 (never heard) -> {'node_id': 77, 'pending_command': 'set_targets',
  'desired_targets_version': 1}
boot2 /api/traps -> node 77: {..., 'pending_command': 'set_targets', 'desired_targets_version': 1,
  'targets': ['0384199C87EC06'], ...}
Spec Compliance: trap-commands 'resend continues until version confirmed', across a restart,
for a never-heard node -> HOLDS
```

**WARNING K(b) distinguishability, measured with real output** (20-node roster, matching the original
report's own measurement shape):

| Scenario | `ok` | `rehydration_errors` | `roster_lost` | `/api/traps` count |
|---|---|---|---|---|
| one unreadable heartbeat row out of 20 nodes | `True` | `1` | `False` | `19`* |
| roster query unreadable — whole registry lost | `False` | `1` | `True` | `0` |

\* 19, not 20: the one corrupt node's *only* row was the corrupt one, so it is honestly absent from the
registry entirely (CRITICAL 2's own pre-existing "no other readable row for this node" behaviour,
unchanged by this batch) — not a WARNING K(b) regression. The distinguishing signal this task adds is
`ok`/`roster_lost`, both of which now differ correctly between the two rows above; `rehydration_errors`
alone (`1` in both) is exactly the collision this task exists to break.

**Scope check**: CRITICAL 1, CRITICAL A, WARNING A, WARNING J (task 2.8's own scope) were **not**
re-verified — already closed in Batch 2a and its closure run, not touched by this batch. CRITICAL 3, 4,
5 and I (Batches 3-5) remain untouched.

## Files changed

| File | Action | What was done |
|---|---|---|
| `src/feeder_station/adapters/store.py` | Modified | `_known_node_ids` unions `node_cursor` with operator-sourced `event` rows (CRITICAL 6); `Store.roster_lost` added and wired into `rehydrate()` (WARNING K(b)); per-node `a_state_row_was_unreadable` tracking + `registry.mark_state_possibly_stale(...)` call (WARNING C) |
| `src/feeder_station/app.py` | Modified | `/health` gains `roster_lost`, folded into `ok` |
| `src/feeder_station/domain/traps.py` | Modified | `Trap.state_may_be_stale` field; `Registry.mark_state_possibly_stale`; `heartbeat`/`captured`/`fault` each clear the flag; `view()` surfaces it |
| `openspec/changes/field-station-p4/design.md` | Modified | Line ~288 amended to describe the union roster query (task 2.7) — the one line in scope; no other line touched |
| `tests/test_cli.py` | Modified | New CRITICAL 6 two-boot test; `test_boot_survives_a_broken_node_roster_query` extended; new `test_one_bad_row_does_not_look_like_a_lost_roster` |
| `tests/adapters/test_store.py` | Modified | New WARNING C tests (possibly-stale flag set and cleared); new `caplog` test for `rehydrate`'s skip log |
| `tests/adapters/test_ingest.py` | Modified | New `caplog` test for `ingest`'s drop log |
| `openspec/changes/field-station-p4/remediation-tasks.md` | Modified | Tasks 2.5, 2.6, 2.7, 2.9, 2.10 ticked `[x]`, each with a one-line note on the shape chosen |

## Commits (4, stacked on `main`)

1. `fix(field-station-p4): rehydrate the roster from every row source (CRITICAL 6)` —
   `store.py` + `test_cli.py` + `design.md` (tasks 2.5-2.7).
2. `fix(field-station-p4): distinguish a lost roster from one bad row in /health (WARNING K(b))` —
   `store.py` + `app.py` + `test_cli.py` (task 2.9, first half).
3. `fix(field-station-p4): flag a rehydrated state that might not be current (WARNING C)` —
   `traps.py` + `store.py` + `test_store.py` (task 2.9, WARNING J's residual).
4. `test(field-station-p4): pin rehydrate's and ingest's skip/drop logging (WARNING K(a))` —
   `test_store.py` + `test_ingest.py` only, no production code (task 2.10).

Each commit independently green (`uv run pytest`, `uv run ruff check .`, `uv run mypy src` run and
confirmed clean after every commit, not only at the end).

## Status

5/5 Batch 2b tasks complete (2.5, 2.6, 2.7, 2.9, 2.10). CRITICAL 6 is closed (a command to a never-heard
node survives a restart, proven at the real two-boot HTTP level). WARNING K(b) is closed (a lost roster
and one bad row are now distinguishable through `/health`). WARNING J's residual (WARNING C) is closed
(a possibly-stale rehydrated state is now flagged, not presented as plain fact). WARNING K(a) is closed
(both previously-surviving mutations are now killed by `caplog` assertions). This closes Batch 2 in
full (2.1-2.4, 2.4b, 2.8 from Batch 2a; 2.5-2.7, 2.9, 2.10 from this run). CRITICAL 3, 4, 5 and I
(Batches 3-5) remain untouched, as scoped. Diff size is 31% over the stated 300-line target — see
composition breakdown above.
