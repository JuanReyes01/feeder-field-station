# Apply Progress — field-station-p4, Batch 2e

**Scope**: closes BEHAVIOUR 1, 2, 3 from `verify-report-batch2d.md` (BUCKET A), plus two record
corrections and one explicit decision (the badge). BEHAVIOUR 4 and SUGGESTION 5 are explicitly **not**
touched — deferred to Batch 3 as a joint design decision, per the caller's brief. CRITICAL 3/4/5/I remain
Batches 3-5, untouched.

**Artifact store**: openspec. **Mode**: Strict TDD (RED confirmed failing for the stated reason, then
GREEN, for every production change below).

**HEAD before this batch**: `1bd6f10`. **Commits landed**: 5, on `main`, none pushed.

```
fae5c5a fix(store): reject a negative wire state byte in trap_state_from_wire
3a680c2 fix(store): treat a corrupt heartbeat battery/version field as unreadable
c923dce fix(store): route the desired-tags read through the unreadable guard
ecba013 docs(store): correct _unreadable's stale not-readable call-site count
32845cb docs(ui): fix the stale-badge comment and record the badge verification gap
```

Each commit is independently green: `uv run pytest`, `uv run ruff check .`, `uv run mypy src` were run
and confirmed exit 0 after every single commit above, not only at the end.

---

## Habit check: enumerated from source, not from the previous report's list

Before writing any fix, re-derived the not-readable expressions directly from `store.py` (`rg
"_unreadable\(" src/feeder_station/adapters/store.py` plus a manual walk of every `isinstance`/`is None`
guard in the rehydration path). Result: **eight** not-readable expressions, five raising, three silent —
independently matching the report's own Q2 table exactly, not copied from it. The three silent ones are
BEHAVIOUR 1 (`store.py:813-815`, the desired-tags read) and BEHAVIOUR 3's two fields
(`store.py:728/729`, `battery_mv`/`targets_version`). BEHAVIOUR 2 (`trap_state_from_wire`'s negative
indexing) is a distinct root cause, not one of the eight — confirmed separately by reading
`trap_state_from_wire`'s full body and testing negative values by hand before writing any test.

---

## Tasks completed

- [x] BEHAVIOUR 1 — `_rehydrate_desired_command`'s desired-tags read (`store.py:813-815`, pre-batch line
  numbers) now raises through `_unreadable()` on an unreadable `tags` list, instead of silently skipping
  the restore with no `else`.
- [x] BEHAVIOUR 2 — `trap_state_from_wire` rejects negative values instead of resolving them via Python's
  own negative indexing into `_WIRE_STATE_ORDER`.
- [x] BEHAVIOUR 3 — `_rehydrate_heartbeat`'s `battery_mv`/`targets_version` now raise through
  `_unreadable()` on a non-int value, instead of coercing to `None`.
- [x] PROOF 4 (closed on the way past, as the brief asked) — `trap_state_from_wire(4)` and
  `trap_state_from_wire(-1)` are now both pinned explicitly, closing the "fifth `_WIRE_STATE_ORDER`
  entry" gap.
- [x] Record correction 1 — `_unreadable`'s docstring no longer claims "five call sites"; re-derived to
  eight, and rewritten to state the rule (not only a count), since the count is what went stale four
  rounds running.
- [x] Record correction 2 — `index.html`'s stale-badge comment ("doubt, italic") now matches the CSS
  (`font-weight: 700`, no italic, same weight as `.no-contact-badge`) shipped in the same commit that
  originally introduced both.
- [x] Explicit decision — the badge-rendering verification gap (PROOF 1) is now recorded in
  `specs/operator-ui/spec.md` and `tests/test_app.py`'s module docstring, rather than left implied by a
  green regex-based test. Decision: **no DOM harness** (see below).
- [x] `remediation-tasks.md` task 2.9 — corrected in place to record "five of eight closed" before this
  batch and "eight of eight" after, rather than left ticked clean on the narrower claim.
- [x] `remediation-tasks.md` task 2.10 — left untouched, per the brief ("may be ticked clean — it was
  verified exhaustive").

**Not touched, deliberately** (per the brief): `traps.py::captured`/`fault`, `workers.py`,
`scheduler.py`, `backlog.py`, `commands.py`. BEHAVIOUR 4 and SUGGESTION 5 remain open, to be decided
together in Batch 3.

---

## TDD Cycle Evidence

| Fix | RED (test written first, confirmed failing for the stated reason) | GREEN | REFACTOR |
|---|---|---|---|
| BEHAVIOUR 1 | `test_a_corrupt_desired_tags_list_counts_as_unreadable_even_when_a_later_arm_wins` — failed `assert reopened.rehydration_errors == 1` (`0 == 1`) before the fix | Routed `store.py:813-815` through `_unreadable()` | Comment explains the two-site read and why this one was missed before |
| BEHAVIOUR 2 | `test_trap_state_from_wire_rejects_a_negative_value` — failed `DID NOT RAISE ValueError` before the fix; `test_a_negative_stored_state_byte_counts_as_unreadable_not_a_fabricated_idle` — failed `assert 7 not in {7}` before the fix | Added `if value < 0: raise IndexError(value)` inside `trap_state_from_wire`'s existing `try` | `test_trap_state_from_wire_rejects_the_first_out_of_range_high_value` added as a regression pin (already correct pre-batch; not a RED case) |
| BEHAVIOUR 3 | `test_a_corrupt_heartbeat_numeric_field_counts_as_unreadable_not_a_fabricated_none[battery_mv/targets_version]` — failed `assert 7 not in {7}` before the fix | Both fields now raise through `_unreadable()` before `restore_heartbeat_fields` is called | `_rehydrate_heartbeat`'s docstring updated to describe the widened atomic-row contract |

All three RED failures were captured and confirmed failing for the stated reason (not an unrelated
error) before any production line was written; see the "RED-before-fix" run below.

---

## RED-before-fix (verbatim, run before any production code changed)

```
$ uv run pytest tests/adapters/test_store.py -k "corrupt_desired_tags_list or negative_value or first_out_of_range_high_value or negative_stored_state_byte or corrupt_heartbeat_numeric_field" -v
...
FAILED ...test_a_corrupt_desired_tags_list_counts_as_unreadable_even_when_a_later_arm_wins
FAILED ...test_trap_state_from_wire_rejects_a_negative_value
FAILED ...test_a_negative_stored_state_byte_counts_as_unreadable_not_a_fabricated_idle
FAILED ...test_a_corrupt_heartbeat_numeric_field_counts_as_unreadable_not_a_fabricated_none[battery_mv]
FAILED ...test_a_corrupt_heartbeat_numeric_field_counts_as_unreadable_not_a_fabricated_none[targets_version]
5 failed, 1 passed, 27 deselected in 0.09s
```

(`test_trap_state_from_wire_rejects_the_first_out_of_range_high_value` passed pre-fix — it pins an
already-correct boundary, per PROOF 4's own framing, not a BEHAVIOUR fix.)

## GREEN (after all three fixes)

```
$ uv run pytest
769 passed, 2 warnings in 5.02s
```

769 = 763 (Batch 2d baseline) + 6 new test cases (3 BEHAVIOUR-2 + 2 BEHAVIOUR-3 parametrized + 1
BEHAVIOUR-1).

## Gates, final state

| Gate | Exit | Result |
|---|---|---|
| `uv run pytest` | 0 | 769 passed |
| `uv run ruff check .` | 0 | All checks passed |
| `uv run ruff format --check .` | 1 | 7 files — same 6-file baseline drift verified against `504d18e` in `verify-report-batch2d.md`, plus one untracked report file (`verify-report-batch2d.md` itself) never touched by this batch. **Zero new drift** from any file this batch edited. |
| `uv run mypy src` | 0 | no issues in 22 source files |

---

## Mutation kills (required probes, re-run after landing, tree restored clean after each)

Mutated in the real package root (`feeder_station.adapters.store.__file__` confirmed under this repo's
`src/` before the first mutation), each restored inside a scripted `finally`-equivalent, `git status
--porcelain` diffed against the pre-mutation baseline after every single one.

**W-dt** (revert BEHAVIOUR 1 — `:813-815` back to silent skip):
```
FAILED test_a_corrupt_desired_tags_list_counts_as_unreadable_even_when_a_later_arm_wins
  assert reopened.rehydration_errors == 1   ->   assert 0 == 1
1 failed, 32 deselected
```
Tree clean after restore: confirmed.

**W-neg** (revert BEHAVIOUR 2 — drop the `value < 0` guard):
```
FAILED test_trap_state_from_wire_rejects_a_negative_value
FAILED test_a_negative_stored_state_byte_counts_as_unreadable_not_a_fabricated_idle
  assert 7 not in {trap.node_id for trap in registry.traps()}   ->   assert 7 not in {7}
2 failed, 31 deselected
```
Tree clean after restore: confirmed.

**W-bat** (`else None` -> `else 0`, exactly as named in the brief — fabricates a dead battery for a
corrupt field, applied against the pre-2e shape):
```
FAILED test_a_corrupt_heartbeat_numeric_field_counts_as_unreadable_not_a_fabricated_none[battery_mv]
FAILED test_a_corrupt_heartbeat_numeric_field_counts_as_unreadable_not_a_fabricated_none[targets_version]
2 failed, 767 passed, 2 warnings in 5.78s   (full suite run under this mutation)
```
Confirms no pre-existing test outside this batch's own new tests ever caught this — matches the report's
own finding that W-bat "passes all 763 tests" before this batch. Tree clean after restore: confirmed.

**M22** (a fifth `_WIRE_STATE_ORDER` entry — PROOF 4):
```
FAILED test_trap_state_from_wire_rejects_the_first_out_of_range_high_value
  Failed: DID NOT RAISE <class 'ValueError'>
1 failed, 32 deselected
```
Tree clean after restore: confirmed.

All four required probes died. Tree confirmed clean (`git status --porcelain` matching the pre-mutation
baseline exactly) after every single mutation, not only at the end.

---

## The real not-readable count, derived from source (not carried over)

```
$ rg -n "_unreadable\(" src/feeder_station/adapters/store.py
734:            _unreadable("HEARTBEAT state", state_value)
747:            _unreadable("HEARTBEAT battery_mv", battery_mv)
752:            _unreadable("HEARTBEAT targets_version", targets_version)
785:            _unreadable("CAPTURED tag", captured.tag)
800:            _unreadable("CAPTURED state_after", state_after)
844:                _unreadable("SET_TARGETS tags", latest_targets.fields.get("tags"))
865:                _unreadable("SET_TARGETS targets_version", version)
894:                _unreadable("SET_TARGETS tags", latest.fields.get("tags"))
```

**Eight call sites, all eight now raise.** Before this batch: eight not-readable expressions existed,
five raised (`:734`≈old`:721`, `:785`≈old`:764`, `:800`≈old`:779`, `:865`≈old`:835`, `:894`≈old`:864`),
three did not (`:747`/`:752` coerced to `None`; `:844` had no `else` at all). `_unreadable`'s own
docstring is rewritten to state the rule rather than rely on this number alone — a hand-maintained count
is exactly what went stale here across four consecutive verification rounds (Q7, `verify-report-
batch2d.md`).

---

## The badge decision, and why

**Checked what is actually available before assuming**, per the brief: `node --version` resolves
(`v25.9.0`, this machine's global Node install), but the project itself has **no** JS/DOM test
toolchain — no `package.json`, nothing in `pyproject.toml`'s `[dependency-groups] dev` list, no CI config
in the repository at all. Building a DOM harness for `renderTrap` would mean introducing a new,
project-wide runtime dependency (Node, and whatever DOM-shim/test-runner it needs) to pin one badge's
rendered output, in a project that has otherwise been deliberately minimal (SQLite + FastAPI, no build
step for the frontend, no bundler).

**Decision: no DOM harness. Recorded explicitly instead** (not left as a green regex implying coverage it
doesn't have): `specs/operator-ui/spec.md`'s "Displayed state is reported state" requirement now has a
verification note naming the three mutations (B3, B4, CSS-hide) a regex cannot see and stating outright
that `renderTrap`'s actual rendered output — content, placement, visibility — is unverified at this
layer. `tests/test_app.py`'s module docstring cross-references the same note. Nothing about the existing
regex test was weakened or removed; the badge's *data layer* (`Registry.view()`, `GET /api/traps`) stays
fully pinned both ways, as it already was.

---

## Line-budget composition breakdown (over budget — flagged, not trimmed)

```
$ git diff --stat 1bd6f10..HEAD
 openspec/changes/field-station-p4/remediation-tasks.md |  15 ++
 openspec/changes/field-station-p4/specs/operator-ui/spec.md | 16 ++
 src/feeder_station/adapters/store.py                   |  97 ++++++++----
 src/feeder_station/static/index.html                   |  13 +-
 tests/adapters/test_store.py                           | 166 +++++++++++++++++++++
 tests/test_app.py                                       |  14 ++
 6 files changed, 289 insertions(+), 32 deletions(-)
```

**Total: 321 changed lines. Budget: 250. Over by 71 lines (28%).**

Composition, derived from a line-by-line read of every hunk (not estimated):

| Category | Lines | Files |
|---|---|---|
| **Production behaviour code** (the actual `if`/`raise` logic for BEHAVIOUR 1/2/3) | **15** | `store.py` |
| Inline comments explaining those 3 fixes at their call sites | 21 | `store.py` |
| Docstring rewrites (`_rehydrate_heartbeat`'s atomicity note + `_unreadable`'s record correction) | 61 | `store.py` |
| `index.html` comment-only fix (record correction 2; zero CSS/JS logic changed) | 13 | `index.html` |
| New tests (6 test functions/cases, RED-then-GREEN, full `Store`/`StationEvent` fixtures matching this file's existing style) | 166 | `test_store.py` |
| Badge-decision record (module docstring only; zero test code changed) | 14 | `test_app.py` |
| Spec/task record corrections (the two explicitly-requested corrections + the badge decision note) | 31 | `remediation-tasks.md`, `operator-ui/spec.md` |
| **Total** | **321** | |

**Production code landed exactly where predicted** — 15 lines, inside the brief's own "~10-20 lines"
estimate for BEHAVIOUR 1+2+3 combined, matching the report's own measured "about ten production lines"
finding almost exactly. **The overage is entirely in prose the brief itself asked for**: two explicit
record corrections (`_unreadable`'s stale count, the CSS/comment mismatch), one explicit decision write-up
(the badge), and tests written in this file's own established, verbose style (real `Store`, real
`StationEvent`, real `rehydrate()` calls — no shortcuts). Per the brief's own instruction ("if you exceed
it, flag it with a composition breakdown rather than trimming tests or citations"), nothing was cut to
fit the number. If the budget must be met going forward, the two candidates that could plausibly shrink
without losing anything load-bearing are: (a) the `_unreadable` docstring correction (61 lines including
the `_rehydrate_heartbeat` atomicity note — could be split into a shorter statement-of-rule plus a
one-line pointer to this apply-progress file for the historical count), and (b) the operator-ui spec note
(16 lines) could move to a shorter cross-reference if `verify-report-batch2d.md` is treated as the
permanent record instead of duplicating its content into the spec. Neither was cut here without being
asked.

---

## Status

All BEHAVIOUR 1/2/3 tasks complete. 769/769 tests passing (763 baseline + 6 new). Both record corrections
and the badge decision are recorded. `remediation-tasks.md` task 2.9 corrected; 2.10 left as-is per the
brief. This is the last batch of the Batch 2 series — Batch 2 (2a through 2e) is now, to the best of this
session's own re-derivation from source, free of open BEHAVIOUR defects within its stated scope.
BEHAVIOUR 4 / SUGGESTION 5 (the `satisfied_by` asymmetry) and CRITICAL 3/4/5/I remain for Batch 3 onward,
untouched by this batch, as instructed.

**Ready for**: `sdd-verify` (a fresh, independent re-verification of Batch 2e specifically, and of
whether Batch 2 as a whole — 2a through 2e — is now a complete, defensible close-out).
