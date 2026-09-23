# Apply Progress — field-station-p4, Batch 2d (Batch 2 final close-out)

**Scope**: closes the findings in `verify-report-batch2c.md` — CRITICAL 1, WARNING 1,
WARNING 2, WARNING 3, WARNING 4 — plus SUGGESTION 2 (covered anyway) and the
`remediation-tasks.md` corrections for tasks 2.6, 2.9, 2.10.
**Artifact store**: openspec. **Mode**: Strict TDD.
**Out of scope, untouched**: CRITICAL 3/4/5, `adapters/workers.py`,
`adapters/scheduler.py`, `domain/backlog.py` (Batch 3+). The FAULT-only-trap
resend-forever interaction (SUGGESTION 5) was left exactly as the verifier
found it — not this batch's scope.

**HEAD before**: `0374341`. Six commits, each independently green on
`uv run pytest` / `uv run ruff check .` / `uv run mypy src`, `uv run ruff
format --check .` at the same 6-file baseline drift throughout (zero new
drift, verified against `504d18e`).

---

## The rule for this batch — "write the mutation that should kill it, run it,
confirm it dies" — applied to every test written or changed here

| # | Finding | Mutation | Result |
|---|---|---|---|
| 1 | CRITICAL 1 | N2 (`_reported_state`'s `state is None` branch fabricates `ReportedState(armed=False, ...)`) | **DIES** — `[set_targets_then_disarm]` fails, other 3 cases stay green |
| 2 | WARNING 2 | N5 (badge condition inverted: `!t.state_may_be_stale ?`) | **DIES** |
| 2 | WARNING 2 | N6 (both ternary branches emptied: `? "" : ""`) | **DIES** |
| 3 | WARNING 3 | N7 (roster-failure log downgraded to `logger.debug`, message changed) | **DIES** |
| 4 | WARNING 1, case D | `captured is None or captured.tag is None: return None` (pre-fix shape) | **DIES** |
| 4 | WARNING 1, case E | `if not isinstance(version, int): return` (pre-fix shape) | **DIES** |
| 4 | WARNING 1, case F | `tags = _tags_from_fields(latest.fields) or ()` (pre-fix shape) | **DIES** |
| 5 | WARNING 4 | union narrowed back to `source = 'operator'` only | **DIES** |

All eight mutations applied in the real package root (`src/`, not a
worktree — `feeder_station.adapters.store.__file__` resolves under this
repo's `src/`, confirmed before the first mutation), restored immediately
after each run, `git status --porcelain` confirmed to show only the three
pre-existing untracked verify-report files after every single one.

---

## 1. CRITICAL 1 — the unpinned DISARM branch of `_reported_state`

**Where**: `tests/test_cli.py` — new parametrize case
`set_targets_then_disarm` on `test_a_command_to_a_never_heard_from_node_
survives_a_restart`. No production change: HEAD's `_reported_state` was
already correct on both branches; only the proof was missing.

The existing three cases (`arm`, `disarm`, `set_targets`) each issue a
single command against a node with no prior registry entry, so they only
ever reach `_reported_state`'s `KeyError` branch. The new case issues
`PUT /targets` first — which makes `restore_desired_tags` create the
registry entry with `state` still `None` — then `POST /disarm` against the
same never-heard node, reaching the *second* branch (`if trap.state is
None: return None`). Two real `build_app` boots, same as the other three
cases. Rule proved: **both** branches of the two-branch rule
`_reported_state`'s own docstring states, not just the one the original
report's example happened to exercise.

Also added, per the verifier's note: `test_an_acknowledge_only_node_is_
counted_in_the_roster_and_rehydrates_to_nothing` — covers
`OPERATOR_CAPTURE_ACKNOWLEDGED`, the fourth operator `msg_type`. Confirmed
equivalent today (N14 still an equivalent mutant, not re-tested against —
the point was coverage for symmetry, not a new kill), matching the
verifier's own measurement (`POST /acknowledge` on a never-heard node
writes its durable row, then 500s; the roster still counts the node).

**Spec**: `trap-commands/spec.md` "Commands set pending state only (S4)" /
`event-store/spec.md` "Registry rehydration at boot" (rebuild without
depending on the node re-sending anything).

## 2. WARNING 2 — the stale-badge ternary's polarity, and its visual weight

**Where**: `tests/test_app.py::test_stale_state_badge_in_render_trap_
depends_on_the_flag_not_a_constant`; `src/feeder_station/static/index.html`.

Considered moving the badge decision out of the template entirely (the
"Best" option) — but `state_may_be_stale` is *already* a domain-computed
boolean (`domain/traps.py::Trap.state_may_be_stale`, surfaced through
`Registry.view()` and `/api/traps`); the only thing left in the template is
which markup to render for `true`/`false`, the same shape every other badge
on this page already uses (`noContact`, the SIN CONTACTO badge). Forcing
that further out would fight the existing `index.html`/domain split, so the
floor applies: the regex now anchors the whole ternary — condition, the
truthy branch's actual `<p class="stale-badge">` markup, and the falsy
branch's empty string — instead of only the condition's substring.

Also bumped `.stale-badge`'s CSS from a small italic to the same
`font-weight: 700` as `.no-contact-badge` (no test — CSS class assertions
are not valid test assertions; this is a plain production change): the
previous italic footnote sat quietly below the bold `⚠ SIN CONTACTO`
badge, which the report measured as the two "usually co-occurring" after a
restart — the doubt read as a qualifier on an alarm already raised, not as
its own claim.

**Spec**: `operator-ui/spec.md` "Displayed state is reported state;
captures persist (S3, S4)" — "an unreachable / unknown / doubtful trap is
marked, never shown as calm."

## 3. WARNING 3 — the fourth log site

Enumerated from the source, not the previous report's list:
`rg 'logger\.(exception|warning|error|debug)' src/feeder_station/adapters/
store.py src/feeder_station/adapters/ingest.py` finds exactly **4** skip/
drop log sites — **3** `logger.exception` in `store.py` (`:556` roster
failure, `:572` state-race loop, `:626` `_rehydrate_desired_command`) and
**1** `logger.warning` in `ingest.py` (`:100`). `:572`, `:626` and `:100`
were already pinned (Batch 2c). `:556` was not — measured (N7) as unpinned
by the same shape as the other two: `caplog.at_level` wrapped around
`test_boot_survives_a_broken_node_roster_query` (which already exercises
this exact scenario for `rehydration_errors`/`roster_lost`), asserting
`levelno == ERROR` and `exc_info is not None`. All 4 sites are now pinned.

**Spec**: no spec scenario directly names logging; this closes the last
gap in CRITICAL A's own "the guard is `except Exception` ... because every
skip is logged" justification (design.md section 6 / `rehydrate()`'s
docstring).

## 4. WARNING 1 — three more silent not-readable paths, unified

**Where**: `src/feeder_station/adapters/store.py` — new `_unreadable(what,
value) -> NoReturn` helper; `tests/adapters/test_store.py` — three new
tests (cases D/E/F).

Genuine RED→GREEN, not a proof-only gap: all three tests failed against
HEAD for the stated reason (pasted below), then passed once the guards
raised.

- **Case D** (`:734`, `captured.tag is None`): a newest CAPTURED row with a
  NULL `tag` — a corruption shape `decode_captured` could never itself
  produce — used to fold into the same `captured is None` branch and
  answer "no fact," losing both the capture object and the state-race
  candidate silently. Now split: `captured is None` stays an honest "no
  report" (`return None`); `captured.tag is None` raises.
- **Case E** (`:799-802`, `SET_TARGETS targets_version`): a corrupt version
  used to `return` early, dropping the whole pending command with nothing
  counted while `desired_tags` (restored independently, S2) still showed
  the edit as accepted. Now raises.
- **Case F** (`:823`, `tags = _tags_from_fields(...) or ()`): an unreadable
  `tags` list used to fabricate an *empty* desired list — a trap configured
  to hunt specific tags silently becomes one configured to hunt none. Now
  raises instead of defaulting.

**Unified, not three bespoke guards**: added `_unreadable(what, value) ->
NoReturn`, one expression of "this value cannot be read," and routed all
**five** not-readable guards in the module through it — the three new ones
plus the two WARNING 1 already fixed in Batch 2c
(`_rehydrate_heartbeat`'s `state`, `_rehydrate_capture`'s `state_after`,
previously two separate hand-written `raise TypeError(...)` calls). Every
caller sits inside `rehydrate()`'s own `except Exception` (CRITICAL A), so
raising — not returning a default — is what makes the corruption counted
in `rehydration_errors` and logged with a traceback.

**RED (actual pytest output, before the fix)**:
```
FAILED tests/adapters/test_store.py::test_a_captured_rows_null_tag_counts_as_unreadable_not_silent
  assert False is True  (state_may_be_stale)
FAILED tests/adapters/test_store.py::test_a_corrupt_targets_version_counts_as_unreadable_instead_of_dropping_the_command
  assert 0 == 1  (rehydration_errors)
FAILED tests/adapters/test_store.py::test_a_corrupt_tags_list_counts_as_unreadable_instead_of_fabricating_an_empty_list
  assert 7 not in {7}  (a Trap was fabricated)
```

**Spec**: `event-store/spec.md` "Registry rehydration at boot"; the same
"an unreadable row is counted, never a silent all-clear" rule WARNING J /
CRITICAL 2 already established for `state`.

## 5. WARNING 4 — the by-design source gap

**Where**: `src/feeder_station/adapters/store.py::_known_node_ids` /
`_count_all_known_nodes`; `tests/test_cli.py::test_a_station_sourced_only_
node_does_not_trip_the_partial_roster_check`.

RED confirmed against HEAD: `roster_nodes_rehydrated == 1`, expected `2`
(a node named only by a `STATION_TIME_SYNC_WITHHELD` row was invisible to
`_known_node_ids`'s union while `_count_all_known_nodes` counted it
anyway). Fixed by widening the union to `source IN ('operator',
'station')` — deliberately **not** `SELECT DISTINCT node_id FROM event`
outright, which would also swallow `source = 'node'` rows and defeat
`test_boot_survives_a_broken_node_roster_query` /
`test_a_partial_roster_says_so_instead_of_reading_healthy`'s own
`node_cursor`-corruption coverage (both re-run and confirmed still green,
no regression). Corrected `_count_all_known_nodes`'s docstring, which
claimed a mismatch could only ever come from corruption — false, as this
by-design gap demonstrated.

**Spec**: `station-deployment/spec.md` S7 / `/health` "distinguishes a
degraded boot from a healthy one" — a monitoring signal that can fire on a
by-design difference "trains people to ignore it."

## 6. Records

`remediation-tasks.md` tasks 2.6, 2.9, 2.10: append-only corrections added
(same convention tasks 1.2/1.4 use — history stays visible, nothing
rewritten), naming exactly what Batch 2c closed narrower than claimed and
what Batch 2d closed for real.

---

## TDD Cycle Evidence

| Item | Test file | Layer | RED | GREEN | Mutation-kill |
|---|---|---|---|---|---|
| 1 (CRITICAL 1) | `tests/test_cli.py` | Integration | Proof-only — code already correct; RED demonstrated via mutation instead of a failing implementation | ✅ passes on HEAD | ✅ N2 dies |
| 1 (SUGGESTION 2) | `tests/test_cli.py` | Integration | N/A — documents an equivalent mutant | ✅ passes on HEAD | N/A (equivalent, by design) |
| 2 (WARNING 2) | `tests/test_app.py` | Unit (regex on shipped page) | Proof-only — ternary already correct | ✅ passes on HEAD | ✅ N5, N6 both die |
| 3 (WARNING 3) | `tests/test_cli.py` | Integration + caplog | Proof-only — log site already correct | ✅ passes on HEAD | ✅ N7 dies |
| 4 (WARNING 1, D/E/F) | `tests/adapters/test_store.py` | Unit | ✅ Written, run, failed for the stated reason | ✅ Passed after `_unreadable` + 3 call-site fixes | ✅ each case's own mutation dies |
| 5 (WARNING 4) | `tests/test_cli.py` | Integration | ✅ Written, run, failed for the stated reason (`1 == 2` assertion) | ✅ Passed after widening the union | ✅ dies when reverted to `source = 'operator'` |

Items 1-3 are proof-only gaps: the production code was already correct
(per `verify-report-batch2c.md`'s own "code correct, proof incomplete"
verdicts), so the classic RED-via-missing-implementation does not apply.
Per this batch's own stated rule, each test's mutation-kill stands in for
RED in these cases. Items 4-5 are genuine production defects and followed
the ordinary RED→GREEN cycle.

### Test Summary
- **Total tests added**: 6 (1 new parametrize case + 5 new test functions)
- **Total tests extended with new assertions (not new test count)**: 2
  (`test_stale_state_badge_...`, `test_boot_survives_a_broken_node_roster_
  query`)
- **Total tests passing**: 763 (757 baseline + 6 new)
- **Mutations run this batch**: 8, all killed (N2, N5, N6, N7, and one per
  WARNING 1 case D/E/F, plus the WARNING 4 union revert)

---

## Gates (each exit status checked directly)

| Gate | Exit | Result |
|---|---|---|
| `uv run pytest` | 0 | 763 passed |
| `uv run ruff check .` | 0 | All checks passed |
| `uv run ruff format --check .` | 1 | Same 6 pre-existing files as the `504d18e` baseline (`design.md`, `payloads.py`, `test_ingest.py`, `test_serial_gateway.py`, `test_events.py`, `test_traps.py`). Zero new drift. |
| `uv run mypy src` | 0 | no issues in 22 source files |

Re-checked after every mutation restore: `git status --porcelain` clean
(only the three pre-existing untracked verify-report files).

---

## Budget

**472 changed lines against a 300 target — over by 172 (157% of budget).**
This is the seventh round to overrun (252/220, 335/150, 610/350, 260/200,
393/300, 684/350, now 472/300), though proportionally smaller than four of
the six prior overruns.

**Composition breakdown** (insertions + deletions, per commit):

| Item | Commit | Production | Tests | Docs |
|---|---|---|---|---|
| 1. CRITICAL 1 | `f6591cd` | 0 | 71 | 0 |
| 2. WARNING 2 | `9ae7b51` | 6 | 19 | 0 |
| 3. WARNING 3 | `dd1b9a8` | 0 | 28 | 0 |
| 4. WARNING 1 | `81b4aab` | 57 | 145 | 0 |
| 5. WARNING 4 | `4271864` | 36 | 68 | 0 |
| 6. Records | `d904fff` | 0 | 0 | 42 |
| **Total** | | **99** | **331** | **42** |

Production changes total 99 lines across two files (store.py, index.html)
— genuinely small, matching "item 4 should be a unification." The
overrun is entirely in the test layer (331 of 472 lines, 70%): every new
test in this codebase carries a citation-heavy docstring tying it to a
spec, a report finding, and — per this batch's own rule — the mutation
that proves it, which is the project's established convention (visible in
every pre-existing test in `test_store.py`/`test_cli.py`), not padding
added here. Item 4 alone (WARNING 1's three cases) is 202 lines because it
is three separate corruption scenarios, each needing its own fixture, its
own RED/GREEN pair, and its own docstring naming which report case it
closes — the same shape the two WARNING 1 guards fixed in Batch 2c already
used. Not trimmed, per this batch's explicit instruction.

---

## Deviations from design

None. `_unreadable()` is new but is a private, module-local helper — no
public API, no design.md-documented interface changed. The `_known_node_ids`
widening stays inside the method's existing documented contract (a roster
derivation query); its docstring was extended, not the module's public
shape.

## Issues found belonging to a later batch (not fixed here)

- SUGGESTION 5 (`verify-report-batch2c.md`): `captured()`/`fault()` never
  run `satisfied_by` — only `heartbeat()` does — so a trap in FAULT that
  sends only FAULTs resends its pending command indefinitely. Explicitly
  named by the caller as Batch 3's frame-budget scope; left untouched.
- SUGGESTION 7 (visual weight): partially addressed by this batch's
  `.stale-badge` font-weight bump; the report's own stronger suggestion
  (marking the state chip itself, e.g. `ARMED (?)`) was not implemented —
  judged as a larger, more opinionated UI change than "bump the CSS to
  match its sibling badge," and out of this batch's line budget.
- SUGGESTION 8 (`POST /acknowledge` on an unknown node 500s after a
  durable write): unchanged, confirmed still present, exercised
  incidentally by the new SUGGESTION 2 test but not fixed — pre-existing
  and explicitly noted as low-value by the verifier.

## Status

6/6 findings from `verify-report-batch2c.md` closed (CRITICAL 1, WARNING
1, 2, 3, 4, plus SUGGESTION 2 covered). Batch 2 is now a complete
close-out. Ready for `sdd-verify` before Batch 3 (CRITICAL 3/4,
`adapters/workers.py`/`adapters/scheduler.py`/`domain/backlog.py`) starts.
