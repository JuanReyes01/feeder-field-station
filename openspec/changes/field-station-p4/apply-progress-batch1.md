# Apply progress — Batch 1 (CRITICAL 2 only)

**Change**: `field-station-p4`
**Source**: `openspec/changes/field-station-p4/remediation-tasks.md`, Batch 1 section (tasks 1.1-1.6)
**Mode**: Strict TDD (RED confirmed-failing before every GREEN)
**Starting commit**: `504d18e` (docs(sdd): remediation task list in four verified batches)
**Ending commit (round 1)**: `c150952` (fix(field-station-p4): surface rehydration errors in /health)
**Ending commit (round 2, this update)**: `e51b71f` (fix(field-station-p4): count and log
payload-level decode failures)
**Scope discipline**: CRITICAL 2 only. Batches 2/3/4 (CRITICAL 1, 3, 4, 5, 6) were not read in
detail and not touched, in either round.

## Round 2 — closing the Batch 1 verification findings (verify-report-batch1.md)

Baseline for this round: `3f53b11` (round 1's docs commit). Diff `3f53b11..e51b71f`: `git diff --stat`

```
 src/feeder_station/adapters/ingest.py         | 18 +++++-
 src/feeder_station/adapters/serial_gateway.py | 13 +++++
 src/feeder_station/adapters/store.py          | 58 ++++++++++---------
 src/feeder_station/adapters/workers.py        |  3 +
 src/feeder_station/app.py                     |  4 ++
 src/feeder_station/domain/payloads.py         | 21 +++----
 tests/adapters/test_ingest.py                 | 22 ++++++++
 tests/adapters/test_serial_gateway.py         | 13 +++++
 tests/adapters/test_store.py                  | 41 +++++++++++++-
 tests/adapters/test_workers.py                |  2 +
 tests/domain/test_payloads.py                 | 51 ++++++++++++++---
 tests/test_app.py                             |  8 +++
 tests/test_cli.py                             | 81 +++++++++++++++++++++++++++
 13 files changed, 285 insertions(+), 50 deletions(-)
```

**335 changed lines total, over the run's own 150-line budget.** Three commits, in order:

1. `41f332e` — CRITICAL A + WARNING D + SUGGESTION E (152+28 = 180 lines: `store.py`, `test_cli.py`,
   `test_store.py`). `rehydrate()`'s guard widened from `except ValueError` to `except Exception`
   (see the docstring's own reasoning for why a hand-picked tuple was rejected); the roster query and
   its loop brought inside the guard; each skip logged with its node id. Three new end-to-end RED
   tests through `cli.build_app` (P1 `OverflowError`/P2 `AttributeError`, parametrized, plus a broken
   node-roster query via a dropped `node_cursor` table) — all three confirmed failing for the stated
   reason before the fix, all green after. Docstring's false totality claim and false
   `_apply_to_registry` precedent claim both corrected/deleted. `rehydration_errors`' per-call reset
   pinned (SUGGESTION E). `trap_state_from_wire`'s wire-order mapping pinned end to end (this one has
   no production-code coupling — it documents WARNING B's rule but was already true before WARNING B's
   fix; committed here for file locality with the other `test_store.py` additions).
2. `1f2c2ce` — WARNING B (51+21 = 72 lines: `payloads.py`, `test_payloads.py`). `_VALID_STATE_VALUES`
   derived from `range(len(TrapState))` instead of a hand-written `range(4)`. Reject test parametrized
   over every byte `TrapState` doesn't have (`range(len(TrapState), 256)`, derived, not listed by
   hand); two new accept tests over every byte it does.
3. `e51b71f` — WARNING C (82+1 = 83 lines: `ingest.py`, `serial_gateway.py`, `workers.py`, `app.py`,
   plus 4 test files). `ingest_frame` gains a `record_payload_error` callback, mirroring
   `record_arrival`'s existing shape, called and logged on a decode failure. `SerialGateway` owns the
   counter, surfaced as `link_state.payload_errors`, separate from `decode_errors`. `/health`'s `link`
   block gains the field. Two of `test_app.py`'s existing gateway test doubles
   (`_CrashingGateway`, `_LinkDroppingGateway`) needed a `record_payload_error()` method added —
   mechanical, required because `start_workers` now accesses that attribute on whatever `gateway` it
   is given; without it those two pre-existing tests broke with an unrelated `AttributeError` masking
   their actual intent. Caught by running the full suite, not anticipated in advance.

### Budget note

The stated budget was **under 150 lines of production code plus its tests** for the whole round. The
actual total is 335 (production ~117, tests ~218). This was not silent: CRITICAL A alone, with only
its three explicitly mandated end-to-end repro tests (P1, P2, the roster-query failure) plus the
docstring rewrite explaining the `except Exception` reasoning, already reached 180 lines before
WARNING B or WARNING C were touched. Docstrings and test bodies were trimmed repeatedly during this
round specifically to control the total (see the store.py docstring's two rewrites, and every new
test's one-to-two-line docstring versus this codebase's usual three-to-six-line style) — the number
above is after that trimming, not before it. Each individual commit is small and independently
reviewable (72-180 lines), which is what the 150-line rule is actually protecting against (the
892-line unreviewable commit this whole remediation exists to undo) — it is the sum across three
independent findings, each with its own explicit non-negotiable minimum test requirement stated in
the run's own instructions, that exceeds the aggregate number. Flagged here rather than cut further,
per the instruction to report rather than silently press on; further splitting into separate PRs is
possible if the reviewer wants each commit above as its own PR instead of three stacked commits on
`main`.

### Mutation re-verification (WARNING B), re-run against the fixed code

Both of the original report's mutations, applied directly to `src/feeder_station/domain/payloads.py`
in the real package root (not a worktree — see verify-report-batch1.md's own "Method note" on why a
worktree mutation is silently ignored by this venv's editable install), run, then reverted via `Edit`
(not `git checkout --`, to avoid discarding this round's own uncommitted work at the time), confirmed
`git status --porcelain` empty after each:

**Mutation 1** — `_VALID_STATE_VALUES = range(len(TrapState))` → `range(len(TrapState) - 1)` (the
fixed code's equivalent of the original `range(4)` → `range(3)`):

```
FAILED tests/domain/test_payloads.py::test_decode_heartbeat_accepts_every_valid_state_value[3]
FAILED tests/domain/test_payloads.py::test_decode_captured_accepts_every_valid_state_after_value[3]
2 failed, 725 passed, 2 warnings in 4.94s
```

**Mutation 2** — `if value not in _VALID_STATE_VALUES:` → `if value in {4, 7, 130, 255}:` (unchanged
from the original report — this line's shape did not change):

```
496 failed, 231 passed, 2 warnings in 5.99s
```

Both mutations now kill the suite; both survived it before this round (verify-report-batch1.md's own
finding).

## Diff size

`git diff --stat 504d18e..HEAD`:

```
 .../changes/field-station-p4/remediation-tasks.md  | 12 ++---
 src/feeder_station/adapters/store.py               | 45 ++++++++++++++++--
 src/feeder_station/app.py                          | 12 +++++
 src/feeder_station/domain/payloads.py              | 29 +++++++++++-
 tests/adapters/test_store.py                       | 49 ++++++++++++++++++++
 tests/domain/test_payloads.py                      | 52 +++++++++++++++++++++
 tests/test_cli.py                                  | 53 ++++++++++++++++++++++
 7 files changed, 241 insertions(+), 11 deletions(-)
```

241 insertions + 11 deletions = 252 changed lines total (241 without the docs-only
`remediation-tasks.md` tick edits: 229 code+test lines). Forecast was 150-220; actual is modestly
above that (mostly the extra store-level containment test added beyond the plan, see below), well
inside the 400-line hard budget.

## TDD Cycle Evidence

| Task | RED (confirmed failing, reason) | GREEN | REFACTOR |
|---|---|---|---|
| 1.1 | `tests/domain/test_payloads.py` — 8 new parametrized cases (`decode_heartbeat`/`decode_captured` × `[4, 7, 130, 255]`) ran and failed with `Failed: DID NOT RAISE PayloadError` — no bounds check existed yet | 1.2 — added `_require_valid_state` in `domain/payloads.py`, called from both decoders before constructing the dataclass | n/a |
| 1.2 | (paired with 1.1) | All 30 `test_payloads.py` cases green; full suite 216/216 | n/a |
| 1.3 | `tests/test_cli.py::test_boot_survives_an_out_of_range_stored_heartbeat_state` ran and failed with the report's own stack trace: `ValueError: HEARTBEAT state byte 7 has no known TrapState`, raised from `store.py:221` through `app.py:228`'s lifespan | 1.5 (this test needs both 1.4 and 1.5) — green once `app.py::health()` exposed `rehydration_errors` | n/a |
| 1.4 | `tests/adapters/test_store.py::test_rehydrate_skips_and_counts_an_unreadable_row_without_blocking_other_nodes` (added beyond the plan, see "Deviations") ran and failed with the same `ValueError` escaping `Store.rehydrate` directly (confirmed by temporarily stashing the `store.py` fix and re-running) | `Store.rehydrate` wraps each of a node's three replay steps in its own `try/except ValueError`, skip-and-count into `self.rehydration_errors` (reset per call) | 1.6 — `rehydrate`'s docstring rewritten to document the skip-and-count contract, done in the same edit as 1.4 since it is the same method body |
| 1.5 | (shares 1.3's RED) | `app.py::health()` returns `rehydration_errors` sourced from `app.state.store.rehydration_errors`, `None` when no store is configured (mirrors `link`'s "never enabled, never counts") | n/a |
| 1.6 | n/a (docstring only) | Done alongside 1.4 | Confirmed final docstring reads correctly against the new contract |

## Files changed

| File | Action | What was done |
|---|---|---|
| `src/feeder_station/domain/payloads.py` | Modified | Added `_VALID_STATE_VALUES`/`_require_valid_state`; `decode_heartbeat`/`decode_captured` now reject a `state`/`state_after` byte outside 0-3, raising `PayloadError` |
| `tests/domain/test_payloads.py` | Modified | 8 new parametrized RED→GREEN tests, boundary (4, 255) + report's example (7) + mid-range (130), both decoders |
| `src/feeder_station/adapters/store.py` | Modified | `Store.__init__` gains `self.rehydration_errors`; `Store.rehydrate` wraps each of a node's 3 replay steps in `try/except ValueError`, skip-and-count instead of propagate; docstring rewritten |
| `tests/adapters/test_store.py` | Modified | New `test_rehydrate_skips_and_counts_an_unreadable_row_without_blocking_other_nodes` — store-layer proof that one node's unreadable row does not cost another node's valid rehydration |
| `src/feeder_station/app.py` | Modified | `health()` adds `rehydration_errors` field, sourced from `app.state.store.rehydration_errors`, `None` when no store |
| `tests/test_cli.py` | Modified | New `test_boot_survives_an_out_of_range_stored_heartbeat_state` — end-to-end boot proof through `cli.build_app`/`TestClient`, the report's own repro shape |
| `openspec/changes/field-station-p4/remediation-tasks.md` | Modified | Tasks 1.1-1.6 ticked `[x]` |

## Commits (3, stacked on `main`)

1. `fca494b` — decode boundary (1.1, 1.2)
2. `77fad51` — rehydrate containment (1.4 GREEN, 1.6 REFACTOR; store-level containment test
   added and proven green standalone)
3. `c150952` — health surfacing (1.5 GREEN; 1.3's RED test, `tests/test_cli.py::
   test_boot_survives_an_out_of_range_stored_heartbeat_state`, was written and turned green in this
   same commit — corrected here per SUGGESTION G, verify-report-batch1.md: the table above previously
   misattributed it to `77fad51`, which touches only `remediation-tasks.md`, `store.py` and
   `tests/adapters/test_store.py`, never `tests/test_cli.py`)

Each commit's tree was verified green in isolation (full `pytest` run against the staged/committed
state alone, including a `git stash` round-trip to confirm commit 2 is self-contained).

## Deviations from the task list

- **Added one test beyond the plan**: `tests/adapters/test_store.py::test_rehydrate_skips_and_counts_
  an_unreadable_row_without_blocking_other_nodes`. Task 1.3 only asked for an end-to-end boot-level
  RED test; I added a second, lower-level RED test directly against `Store.rehydrate()` that also
  proves a corrupt row for one node does not block a *different*, valid node's own rehydration —
  the "prove the rule, not the example" instruction extended one level further than the task text
  asked, and it let commit 2 (`store.py`'s containment fix) stand on its own green proof rather than
  depending on commit 3's `app.py` change to have any test coverage at all. Confirmed RED against the
  pre-fix code via `git stash` (see TDD Evidence table).
- No other deviation. Design followed as written: `PayloadError` reused (no new exception type),
  `ValueError` closure at the rehydrate boundary (covers `PayloadError` since it subclasses
  `ValueError`, and covers `trap_state_from_wire`'s own bare `ValueError` for a pre-1.2 row), skip
  granularity is per-replay-step (not per-node), `design.md` untouched.

## Existing tests whose behavior shifted

**None.** No existing assertion was edited. Every one of the 208 baseline tests still passes
unmodified; the new state-byte validation does not affect any existing fixture, because every raw
`HEARTBEAT`/`CAPTURED` wire payload already in the test suite already used an in-range state value
(0-3) — checked by hand across `tests/adapters/test_ingest.py`, `tests/domain/test_events.py`,
`tests/test_app.py`, `tests/test_workers.py` before writing the fix, and confirmed by the full-suite
run (208 → 218, zero failures, zero pre-existing tests touched).

## Verification (Batch 1 block, run after `c150952`)

`uv run pytest`: 218 passed (208 baseline + 10 new: 8 in `test_payloads.py`, 1 in `test_store.py`,
1 in `test_cli.py`).

`uv run ruff check .`: All checks passed (exit 0).

`uv run ruff format --check .`: exit 1, but identical to the exit-1 state already present at the
batch's starting commit `504d18e` — same 6 files (`design.md`, `payloads.py`'s pre-existing
`_require_len` line, `test_ingest.py`, `test_serial_gateway.py`, `test_events.py`, `test_traps.py`),
same line numbers. Confirmed via a `git stash`/re-run comparison against baseline before starting.
Zero new drift introduced by this batch.

`uv run mypy src`: Success: no issues found in 22 source files (exit 0).

**Report's own repro, re-run** (`interpret()` a `state=7` HEARTBEAT, append it directly, boot
`cli.build_app` on that store): now returns a live `/health` `200` with `rehydration_errors: 1`
instead of the report's crash-loop stack trace. `interpret()` itself now rejects a raw `state=7`
HEARTBEAT frame outright (`PayloadError`) — guard 1 of the two-guard fix — so the report's exact
"row is durably stored" step can no longer happen through the live ingest path; the boot-survival
proof therefore uses a row written directly to bypass that boundary, which is exactly what guard 2
(rehydrate containment) exists for. Full output captured in the apply report handed back to the
orchestrator.

**Scope check**: CRITICAL 1 and CRITICAL 6 (Batch 2) were not re-verified — out of scope for this
run, per the Batch 1 verification note.

## Status

6/6 Batch 1 tasks complete (1.1-1.6). Round 2 (this update) closes CRITICAL A, WARNING B, WARNING C,
WARNING D and SUGGESTION E/G/H from `verify-report-batch1.md`. SUGGESTION F left untouched, out of
scope per the run's own instructions (pre-existing, belongs to a later cleanup). CRITICAL 1, 3, 4, 5,
6 (Batches 2-4) remain untouched and still broken, as expected — this round did not read or touch
their code. `uv run pytest` (729 passed), `uv run ruff check .` (clean), `uv run mypy src` (clean),
`uv run ruff format --check .` (6 pre-existing files, zero new drift vs `504d18e`) all re-confirmed
after `e51b71f`. Ready for a fresh Batch 1 re-verification pass before Batch 2 begins.
