# Apply progress — Batch 1 (CRITICAL 2 only)

**Change**: `field-station-p4`
**Source**: `openspec/changes/field-station-p4/remediation-tasks.md`, Batch 1 section (tasks 1.1-1.6)
**Mode**: Strict TDD (RED confirmed-failing before every GREEN)
**Starting commit**: `504d18e` (docs(sdd): remediation task list in four verified batches)
**Ending commit**: `c150952` (fix(field-station-p4): surface rehydration errors in /health)
**Scope discipline**: CRITICAL 2 only. Batches 2/3/4 (CRITICAL 1, 3, 4, 5, 6) were not read in
detail and not touched.

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
2. `77fad51` — rehydrate containment (1.3 RED written here, made green by commit 3; 1.4 GREEN, 1.6
   REFACTOR; store-level containment test added and proven green standalone)
3. `c150952` — health surfacing (1.5 GREEN; 1.3's end-to-end test turns green here)

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

6/6 Batch 1 tasks complete (1.1-1.6). Batches 2, 3, 4 not started — not read beyond their section
headers, per the run's explicit scope boundary. Ready for the Batch 1 verification gate before
Batch 2 begins.
