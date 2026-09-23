# Apply progress — Batch 2c (Batch 2 close-out: CRITICAL 1, WARNING 1/2/3/4/5, SUGGESTION 1)

**Change**: `field-station-p4`
**Source**: `openspec/changes/field-station-p4/verify-report-batch2.md` — closing CRITICAL 1, WARNING 1,
WARNING 2, WARNING 3, WARNING 4, WARNING 5, SUGGESTION 1, and correcting two inaccurate records.
**This finishes Batch 2. It is not a new batch of the original remediation plan.**
**Mode**: Strict TDD (RED confirmed-failing before every GREEN where production code changed; the
test-only findings — CRITICAL 1's parametrize, WARNING 3's second log site — passed immediately
against already-correct code, so mutation testing stood in for RED, per the brief's own instruction).
**Starting commit**: `1c59681`.
**Out of scope, not touched**: CRITICAL 3/4/5/I (Batches 3-5), `adapters/workers.py`,
`adapters/scheduler.py`, `domain/backlog.py`.

## Why this batch exists

The fifth consecutive round of the same diagnosis: *the code is right, the test proves the example
rather than the rule.* Twice, a test written to prove a rule turned out to be vacuous against the very
mutation it was meant to catch. Every fix below either closes that gap or (CRITICAL 1's DISARM half)
was a genuine, previously-unknown defect found *because* the gap was closed properly instead of
patched around.

## 1. CRITICAL 1 — the roster rule, parametrized over every operator command

**Rule**: the roster rehydration walk includes every node with *any* operator-sourced row — `ARM`,
`DISARM`, `SET_TARGETS` — not only the `ARM` example the original test happened to issue.

`test_a_command_to_a_never_heard_from_node_survives_a_restart` (`tests/test_cli.py`) is now
parametrized over `POST /arm`, `POST /disarm`, `PUT /targets` — the three entries in
`Store._OPERATOR_COMMAND_MSG_TYPES`, read from the code rather than assumed.

**Unplanned discovery, fixed in the same commit**: parametrizing over `DISARM` immediately failed —
for a reason CRITICAL 1's own roster union does not touch. `_reported_state`'s `KeyError` fallback
(`ReportedState(armed=False, ...)`) happened to keep a pending `ARM` correctly pending (any
non-`ARMED` reading does) but silently *satisfied* a pending `DISARM` for a never-heard node on
rehydration — DD15's own `satisfied_by` check discarded the command before the roster fix ever got a
chance to matter. `_reported_state` now returns `None` when nothing is known about a node (no trap
entry, or a trap entry whose `state` is still `None`), and `_rehydrate_desired_command` never calls
`satisfied_by` when that happens — an unconfirmed command stays pending until a real report confirms
it, never until the station simply runs out of information.

**Mutations, re-run against final HEAD**:
```
M9  (union -> source='operator' AND msg_type=256 (ARM))          -> disarm, set_targets FAIL. KILLED.
M28 (union -> source='operator' AND msg_type=258 (SET_TARGETS))  -> arm, disarm FAIL. KILLED.
```

## 2. WARNING 5 — the ordering test now has teeth

**Rule**: the state race's `max(facts, key=lambda fact: fact.seq)` must survive a station wall clock
that steps *backward* between two appends (`received_epoch`, `Clock.now()`, the same clock
`clock_trust.py` exists because it is unsynced at boot) — not just survive the node's own older
`node_epoch`.

The FAULT row's `received_epoch` in `test_a_backlog_replayed_report_does_not_win_the_state_race_by_
the_nodes_own_older_clock` changed from `2060.0` (later than the HEARTBEAT's `2000.0`, so `key=seq`
and `key=at` always agreed) to `1000.0` (earlier — the clock-step case). Added an assertion on
`trap.last_seen` too. `store.py`'s WARNING A comment gained a correction explaining why `seq`, not
`received_epoch`, is the key that survives this. `apply-progress-batch2a.md`'s claim that
`received_epoch` is "monotonic with seq in every single-writer ingestion fixture" is false — corrected
in place, not rewritten.

**Mutation, re-run against final HEAD**:
```
M8 (key=fact.seq -> key=fact.at) -> AssertionError: ARMED is not FAULT. KILLED.
```

## 3. WARNING 1 — the module's other not-readable path now counts

**Rule**: a row that cannot be read is a row that cannot be read, whether the code expresses that by
raising or by returning `None`.

`_rehydrate_heartbeat` and `_rehydrate_capture` each had an `isinstance` guard that returned `None`
silently for a present-but-wrong-typed `state`/`state_after` field — never raising, never counted in
`rehydration_errors`, never flagging `state_may_be_stale`. Both now raise `TypeError` into the same
`except Exception` guard every other unreadable row already goes through, instead of adding a second
flag-setting site (the fix direction the report itself preferred). Two new tests
(`tests/adapters/test_store.py`) reproduce the report's own measured cases B and C. One pre-existing
test's `rehydration_errors` assertion shifted from `1` to `2` — see "Shifted test" below.

## 4. WARNING 2 — the flag now reaches the operator, pinned both ways

**Rule**: a stale-flagged trap must render visibly differently from a confident one, and the field must
be pinned so neither "always false" nor "always true" can ship silently.

`static/index.html`'s `renderTrap` now renders a badge (`⚠ puede no ser el último reporte de esta
trampa`, italic, `.stale-badge`) keyed on `t.state_may_be_stale`, independent of the `reachable`-keyed
`⚠ SIN CONTACTO` badge. Pinned two ways: a structural test (`tests/test_app.py`) asserting the actual
ternary is present in the shipped JS (a bare substring check would not catch "always emit the badge"
or "never emit it"), and a real `/api/traps` boot (`tests/test_cli.py`) with one genuinely stale node
and one genuinely healthy node in the same response.

**Mutations, re-run against final HEAD**:
```
M15 (view() emits "state_may_be_stale": False unconditionally) -> OverflowError inside rehydrate
    (the stale node's FAULT row is genuinely unreadable and the test now depends on the flag
    reaching the trap that earned it) -> test FAILED. KILLED.
M26 (a_state_row_was_unreadable = True unconditionally)          -> same failure. KILLED.
Badge-unconditional mutation (index.html)                        -> structural test FAILED. KILLED.
```

## 5. WARNING 3 — the two remaining log sites, pinned

**Rule**: every skip/drop log site is pinned, not only the two the earlier fixture happened to drive.

`rehydrate()`'s state-race loop and `_rehydrate_desired_command` each have their own `try/except`
around an identical log message — the earlier fixture only ever drove the first. A new test corrupts
an `OPERATOR_ARM` row's `received_epoch` (`OverflowError`) so only `_rehydrate_desired_command` raises
for that node. `ingest.py`'s drop log catches `FrameError`/`PayloadError`/`UninterpretableFrame` in one
`except` — the earlier fixture only ever drove `FrameError`. A new test sends a syntactically valid
frame envelope with a too-short `HEARTBEAT` payload, raising `PayloadError` inside `interpret()`.

**Mutations, re-run against final HEAD**:
```
M10 (downgrade only _rehydrate_desired_command's skip log to logger.debug) -> 0 == 1. KILLED.
M11 (log FrameError at WARNING, everything else at DEBUG)                  -> 0 == 1. KILLED.
```

## 6. WARNING 4 — the partial roster, the third case

**Rule**: a roster that comes back short must say so, not read identically to a healthy boot.

`Store.roster_nodes_expected` (`SELECT COUNT(DISTINCT node_id) FROM event` — the source-agnostic
ground truth, run inside the same guard as the roster query) and `roster_nodes_rehydrated` (`len(
_known_node_ids())`) are surfaced in `/health` and folded into `ok` when the second is less than the
first. New test: three nodes seeded, node 2's `node_cursor` row deleted directly while its `HEARTBEAT`
row survives in `event` — reproduces the report's own measured case exactly.

**Mutation, re-run against final HEAD**:
```
roster_incomplete forced to False -> "assert True is False" (ok read healthy). KILLED.
(roster_nodes_expected/rehydrated themselves are unmutated and already assert 3/2 independently.)
```

## 7. SUGGESTION 1 — `docs/deploy.md`

Two sentences added beside the existing `curl` block: `/health` always answers HTTP 200 (parse the
body, never trust the status code alone), and `ok: false` covers several conditions of which only a
dead worker thread is restart-fixable — a down link or a lost/incomplete roster are not, and an
automatic restart wired to `ok: false` alone will loop.

## 8. Records corrected

- `remediation-tasks.md` task 2.6: correction note — CRITICAL 1's code was correct, its proof was not
  (the ARM-only example), plus the DISARM discovery and fix.
- `remediation-tasks.md` task 2.9: correction note — the WARNING-C half (`state_may_be_stale`)
  overstated what shipped (WARNING 1/2), plus the roster-count half (WARNING 4).
- `remediation-tasks.md` task 2.10: correction note — "per log site" closed 2 of 3 (WARNING 3).
- `apply-progress-batch2a.md`: correction note on the false "received_epoch is monotonic with seq"
  claim (WARNING 5).
- `apply-progress-batch2b.md`: correction note — the `roster_lost` test's assertion change was an
  **inversion** (`ok is True` → `ok is False`), not "three assertions instead of two." Justified on
  the merits; the characterization was wrong.

All five corrections follow the same convention tasks 1.2/1.4 already established: the original text
stays visible, a bolded `**Correction (...)**` note is appended in place, nothing is silently rewritten.

## Existing test whose behaviour shifted, with spec citation

`tests/test_cli.py::test_a_node_with_no_readable_state_source_shows_unknown_not_idle` —
**before**: `health["rehydration_errors"] == 1` (only the heartbeat's out-of-range `state` byte
counted). **After**: `== 2` (the `CAPTURED` row's missing `state_after` now also raises and counts,
per WARNING 1). **Spec citation**: `event-store/spec.md` "Registry rehydration at boot" requires the
rebuild to happen "without depending on the node re-sending anything" — a row the station cannot read
is exactly the row that requirement is about, and undercounting it (as the pre-fix behaviour did)
hides how much of a node's history the station actually lost. Both of this node's two rows are
genuinely unreadable; the previous count reported only one of them.

## Verification

```
uv run pytest         -> 757 passed (748 baseline + 9 new: 2 CRITICAL-1 parametrize cases,
                          2 WARNING-1 tests, 1 WARNING-3 store.py test, 1 WARNING-3 ingest.py test,
                          1 WARNING-2 structural test, 1 WARNING-2 API test, 1 WARNING-4 test)
uv run ruff check .   -> All checks passed
uv run ruff format --check .  -> exit 1, 6 files -- identical set to baseline 504d18e, zero new drift
uv run mypy src       -> Success: no issues found in 22 source files
```

Every mutation named in the brief re-run against the final tree, in the real package root
(`feeder_station.adapters.store.__file__` confirmed under this repo's `src/` before the round),
reverted with the Edit tool (not `git checkout` on a multi-concern file — see "process note" below),
`git status --porcelain` confirmed to show only the two pre-existing untracked verify-report files
after every single one. All eight died: M9, M28, M8, M15, M26, the badge-unconditional mutation, M10,
M11, and the partial-roster `roster_incomplete` mutation.

## Process note: commit reconstruction

`store.py`, `test_cli.py`, `test_store.py`, `app.py` and `remediation-tasks.md` each carry edits for
more than one finding in this batch. To keep commits small and per-concern (rather than one giant
commit or misattributed whole-file commits), these five files were reset to `1c59681` after the full
implementation was verified green, then their edits were re-applied in commit order and diffed byte-
for-byte against the verified-final versions before each commit (confirmed identical). Six commits
total, each independently green (full suite + ruff + mypy run after every one):

```
fix(store): prove CRITICAL 1's roster rule for every operator command
fix(store): give the state-race ordering test real teeth (WARNING 5)
fix(store): count an unreadable state field as unreadable (WARNING 1)
fix(store,ingest): pin the two log sites WARNING K(a) left open (WARNING 3)
feat(ui): render the possibly-stale flag and pin it both ways (WARNING 2)
feat(store): surface a partial roster instead of reading healthy (WARNING 4)
```

## Budget: exceeded, disclosed

**Under 350 changed lines was the target. Actual: 684 (635 insertions + 49 deletions), git diff --stat
1c59681..HEAD.** This is now six for six.

Composition:

| Category | Files | Lines |
|---|---|---|
| Production code | `store.py` (+108/-18), `app.py` (+20/-1), `index.html` (+9/-0) | 156 |
| Tests | `test_cli.py` (+175/-13), `test_store.py` (+197/-17), `test_ingest.py` (+40/-0), `test_app.py` (+22/-0) | 464 |
| Docs / records | `deploy.md` (+8/-0), `remediation-tasks.md` (+33/-0), `apply-progress-batch2a.md` (+15/-0), `apply-progress-batch2b.md` (+8/-0) | 64 |
| **Total** | | **684** |

Not trimmed, per the brief's own instruction: every test above is mutation-killed (see the sections
above), every finding carries the citation trail this change's history has demanded since round one,
and the CRITICAL-1 DISARM discovery was not optional once found. The genuine lever available for next
time is scope, not prose: seven findings (one CRITICAL, five WARNING, one SUGGESTION) plus two record
corrections in one close-out round is more than a single ~350-line slice can hold once every finding
requires its own mutation-proof test and citation. Worth the same design-time conversation
verify-report-batch2.md's own SUGGESTION 6 already asked for, before Batch 3's forecast is trusted.
