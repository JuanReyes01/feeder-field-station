# Verification Report — field-station-p4, Batch 2 close-out (CRITICAL A re-verify + Batch 2b)

**Scope**: CRITICAL A's closure (`576e640`, `d05f7d5`) re-verified, plus Batch 2b tasks
**2.5, 2.6, 2.7, 2.9, 2.10** (`9fb28bb`, `69375ae`, `ac1cc49`, `d34a87c`, `1c59681`).
Diff `0fa597d..HEAD` — 7 commits, 1162 insertions / 33 deletions (487 of them code+tests).
**Artifact store**: openspec. **Mode**: Strict TDD.
**Out of scope, deliberately not reported**: CRITICAL 3, 4, 5, I (Batches 3-5).

**Verdict: FAIL** — 1 CRITICAL, 5 WARNING, 7 SUGGESTION.

Every claim below was re-derived from the repository. Both apply records were treated as hypotheses:
their four mutations were reproduced, **26 more were invented (30 total, 20 killed, 10 survived)**,
the CRITICAL A table was re-measured from real wire bytes through a real `cli.build_app` boot, every
RED claim was replayed against the pre-batch tree, and the implementer's explicit request to
re-derive rather than trust the A3 structural argument was honoured — **and it does not hold.**

---

## Executive verdicts (the six the brief asked for)

| Question | Verdict |
|---|---|
| **Is CRITICAL A closed?** | **YES.** Re-measured independently: all four `state_after` values agree live-vs-rehydrated, with and without a preceding HEARTBEAT, through real wire bytes and a real boot. Both halves of the fix are mutation-killed (M12, M13). The extended test genuinely asserts both sides. |
| **Is CRITICAL 6 closed?** | **CODE YES, PROOF NO.** The union query is correct for every operator `msg_type` today. But **M9 — narrowing the union to `msg_type = OPERATOR_ARM` — SURVIVES all 748 tests.** The rule the task states ("every node with *any* row") is not what the suite proves; it proves the ARM example. `trap-commands`' "resend continues until version confirmed, across a restart, for a never-heard node" is verified only in a throwaway script in the apply record, by no test. **CRITICAL 1 below.** |
| **Is WARNING K(a) closed?** | **PARTIALLY.** Both named mutations are killed, and three invented variants (drop the node id, drop the traceback, drop the exception text) are killed too — the pins have real teeth. But K(a)'s stated close was *"one `caplog` assertion per log site"*: there are three sites and two are pinned. **M10** (downgrade only `_rehydrate_desired_command`'s skip log) and **M11** (log at WARNING with the exception only for `FrameError`, DEBUG for `PayloadError`/`UninterpretableFrame`) both **SURVIVE**. |
| **Is WARNING K(b) closed?** | **YES for the two cases it names**, measured independently. `roster_lost` genuinely resets across repeated `rehydrate()` calls (3-pass probe). A **third** case — the roster query succeeds but returns a *truncated* list — is still indistinguishable from a healthy boot: `ok: true`, `roster_lost: false`, `rehydration_errors: 0`, nodes silently absent. WARNING K's own text offered "a count of nodes expected vs rehydrated" as one of three closes; that is the one that would have caught it. **WARNING 4.** |
| **Is WARNING J's residual (WARNING C) closed?** | **NO.** Three independent reasons: the flag never reaches the operator (`static/index.html` does not read it — a stale-flagged `armed` trap is byte-identical on screen to a confident one); it is unpinned at the API layer (**M15** and **M26** both survive, so "always false" *and* "always true" ship green); and it keys on `except`, missing the module's *other* not-readable path — a newest row whose `state`/`state_after` is present but non-`int` yields `state=ARMED`, `state_may_be_stale=False`, `rehydration_errors=0`. **WARNING 1 and 2.** |
| **Is `ok: false` for a lost roster correct?** | **YES, correct as implemented; the crash-loop hazard is real in principle but not reachable in this repo today, and is not new.** Full argument below. `docs/deploy.md` **should** carry a warning — SUGGESTION 1. |
| **May Batch 2 be pushed?** | **Not as a CRITICAL 6 closure.** One parametrize axis closes CRITICAL 1 (est. ~6 test lines). See the final section. |
| **May Batch 3 start?** | **Yes, now.** Batch 3 is the frame-budget/scheduler work (`workers.py`, `scheduler.py`, `backlog.py`); it does not touch `_known_node_ids`, `/health`, `rehydrate()`'s state race, or `traps.py`. No overlap with any finding here. |

---

## Gates (each exit status checked directly — nothing piped into `head`/`tail`)

| Gate | Exit | Result |
|---|---|---|
| `uv run pytest` | **0** | 748 passed (742 after CRITICAL A's fix + 6 new) |
| `uv run ruff check .` | **0** | All checks passed |
| `uv run ruff format --check .` | **1** | 6 files — **identical set to baseline `504d18e`**, verified by running the repo's own `ruff` against a `504d18e` worktree and diffing the file lists. **Zero new drift.** |
| `uv run mypy src` | **0** | no issues in 22 source files |

Baseline and HEAD file lists are identical: `openspec/changes/field-station-p4/design.md`,
`src/feeder_station/domain/payloads.py`, `tests/adapters/test_ingest.py`,
`tests/adapters/test_serial_gateway.py`, `tests/domain/test_events.py`, `tests/domain/test_traps.py`.

**Per-commit greenness** (verified, not trusted — `src`+`tests` checked out at each sha, suite run,
tree restored):

```
576e640 (CRITICAL A, live path)            -> 742 passed
d05f7d5 (docs: 2a label correction)        -> 742 passed
9fb28bb (CRITICAL 6, roster union)         -> 743 passed
69375ae (WARNING K(b), /health.roster_lost)-> 744 passed
ac1cc49 (WARNING C, state_may_be_stale)    -> 746 passed
d34a87c (WARNING K(a), caplog pins)        -> 748 passed
1c59681 (docs: ticks + apply record)       -> 748 passed
```

Monotonic, every commit independently green. A bisect through this stack is safe.

---

## TDD compliance (Strict TDD active) — RED replayed, not taken on trust

`src/` checked out at `d05f7d5` (pre-Batch-2b) with HEAD's tests:

```
5 failed, 743 passed
FAILED test_store.py::test_a_skipped_newer_row_flags_the_older_survivor_as_possibly_stale
FAILED test_store.py::test_a_fresh_live_report_clears_the_possibly_stale_flag
FAILED test_cli.py::test_boot_survives_a_broken_node_roster_query
FAILED test_cli.py::test_one_bad_row_does_not_look_like_a_lost_roster
FAILED test_cli.py::test_a_command_to_a_never_heard_from_node_survives_a_restart
```

Every production-driven task's named test genuinely fails against the pre-batch tree. The two
`caplog` tests (2.10) **pass** pre-batch — exactly as the apply record discloses, and correct for a
coverage-only task. That disclosure checks out.

| Task | Boxed | Verified | Note |
|---|---|---|---|
| 2.5 RED never-heard-node test | `[x]` | Yes | Real two-boot `build_app` harness; RED replayed |
| 2.6 GREEN roster union | `[x]` | **Code yes, rule unproven** | See CRITICAL 1 |
| 2.7 design.md line ~288 | `[x]` | Yes | Exactly the one line in scope; no other `design.md` line touched |
| 2.9 K(b) `/health.roster_lost` | `[x]` | Yes for the two named cases | Third case open — WARNING 4 |
| 2.9 WARNING C `state_may_be_stale` | `[x]` | **No** | WARNING 1, WARNING 2 |
| 2.10 K(a) `caplog` pins | `[x]` | Partially | 2 of 3 log sites — WARNING 3 |

`remediation-tasks.md`'s diff has **zero removed lines** — pure addition (ticks + one-line notes). No
prose was silently edited to match the code. Same for `design.md`: one hunk, the scoped line only.

---

## CRITICAL A, re-measured from scratch

Not the implementer's script. `CAPTURED` frames were built as **real wire payloads**, pushed through
`frames.encode` → `frames.decode` → `events.interpret`, applied live via `ingest._apply_to_registry`,
appended to a real `Store`, and read back through a real `cli.build_app` boot + ASGI lifespan +
`GET /api/traps`. `capture` and `state_may_be_stale` compared as well as `state`.

```
CAPTURED alone                              HEARTBEAT(armed) then CAPTURED
state_after=0  live idle  / rehyd idle  AGREE    live idle  / rehyd idle  AGREE
state_after=1  live armed / rehyd armed AGREE    live armed / rehyd armed AGREE
state_after=2  live fired / rehyd fired AGREE    live fired / rehyd fired AGREE
state_after=3  live fault / rehyd fault AGREE    live fault / rehyd fault AGREE
(capture object and state_may_be_stale AGREE in all eight)
```

Batch 2a's cases F, G and H are all closed. `/health` reads `ok: true`, `rehydration_errors: 0`,
`roster_lost: false` throughout.

**The extended test is real coverage, not a restatement.**
`tests/adapters/test_store.py:312-349` now builds a second, live `Registry`, applies the same
`StationEvent` through `_apply_to_registry`, and asserts `live_trap.state is TrapState.ARMED`
alongside the pre-existing rehydrated assertion. Mutation **M12** (`Registry.captured()` hardcodes
`FIRED` again) is killed by that new assertion *and* by three of the four new parametrized cases;
**M13** (`_rehydrate_capture` hardcodes `FIRED`) is killed by the same four. Both halves are pinned
independently, and the parametrize table
(`test_cli.py::test_rehydrated_registry_matches_the_live_registry_for_every_captured_state_after`,
4 cases) closes batch 2a's WARNING B on the content axis — `state_after=2`'s coincidence can no
longer hide a hardcode.

### The A3 structural argument — re-derived, and it does **not** hold

The implementer asked for this rather than trusting it. Doing so:

**Facts confirmed.** `_ReportedFact.at` is `_epoch_to_datetime(row.received_epoch)` at all three
construction sites and nowhere else (`store.py:646`, `:689`, `:702`). `received_epoch` is always the
station's own clock — `clock.now().timestamp()` at `workers.py:123` and `app.py:188`, never
`node_epoch`. `node_epoch` is written (`events.py::_node_epoch_of`) and read only by
`adapters/export.py`'s CSV; the state race never touches it. So "`at` is never sourced from
`node_epoch`" is **TRUE**.

**The monotonicity claim is FALSE.** `at` is monotonic with `seq` only while the station's wall clock
moves forward. `Clock.now()` is `system_now()` (`clock.py:186`) — and `clock_trust.py` exists
precisely because that clock is *unsynced at boot* and gets **stepped** by chrony/timesyncd. A
backward step between two appends breaks the order. Measured, on an ordinary two-row fixture with no
code change:

```
seq=1 HEARTBEAT armed  received_epoch=2000.0   (clock unsynced, reads high)
seq=2 FAULT            received_epoch=1000.0   (chrony steps the clock back)

HEAD   (key=seq): live FAULT / rehydrated FAULT           AGREE
M8     (key=at) : live FAULT / rehydrated ARMED           DIVERGES  (last_seen diverges too)
```

So `key=seq` is not merely one of two defensible rules — it is the rule that keeps the live and
rehydrated paths equal, which is CRITICAL 1's own invariant. **A test with real teeth is available
for the price of changing one number in a fixture.**

**The new ordering test is vacuous against the surviving mutation.**
`test_a_backlog_replayed_report_does_not_win_the_state_race_by_the_nodes_own_older_clock`
(`test_store.py:350-416`) gives its HEARTBEAT `received_epoch=2000.0`/`seq 1` and its FAULT
`received_epoch=2060.0`/`seq 2` — **both orderings agree**, so `key=seq` and `key=at` produce the
same answer and **M8 SURVIVES all 748 tests**, exactly as reported. The test varies only
`node_epoch`, a field the race never reads. It documents the decision (valuable, and the docstring is
excellent) and it would catch a future switch to `node_epoch`; it pins nothing about the code as
written. This is the recurring diagnosis once more: the test encodes the *story* (a backlog replay)
rather than the *rule* (arrival order wins).

**Ordering-decision facts, both verified.** `domain/clock_trust.py` exists and is exercised by
`tests/domain/test_clock_trust.py`; `payloads.decode_fault` (`payloads.py:198-205`) returns
`Fault(code, detail)` with **no epoch field at all**, and `_node_epoch_of` (`events.py:93-100`)
returns `None` for it. **Implication for FAULT's `last_seen`**: a `node_epoch`-based rule could not
rank a FAULT against a HEARTBEAT at all — FAULT would need a fallback, and any fallback reintroduces
the arrival-order rule for exactly the report type a trap in trouble sends. The decision is
well-founded. Only its test is not.

---

## Mutation testing — 30 mutations in the real package root

Method gotcha honoured: every mutation applied in the real package root
(`feeder_station.adapters.store.__file__` asserted under this repo's `src/` before the first run),
never in a worktree. Each reverted with `git checkout --`; `git status --porcelain` asserted equal to
the single expected untracked file after **every** mutation. Final tree: clean.

### Killed (20)

| # | Mutation | Killed by |
|---|---|---|
| M1 | roster query back to `node_cursor` alone *(implementer's #1)* | `test_a_command_to_a_never_heard_from_node_survives_a_restart` — **reproduces their claim exactly** |
| M2 | delete `self.roster_lost = True` *(implementer's #2)* | `test_boot_survives_a_broken_node_roster_query` — **reproduced** |
| M3 | `rehydrate` skip log → `logger.debug("rehydrate: skipped a row")`, both sites *(#3a)* | `test_a_skipped_row_is_logged_at_error_with_the_node_id_and_a_traceback` — **reproduced** |
| M4 | `ingest` drop log → `logger.debug("ingest: dropped a frame")` *(#3b)* | `test_a_dropped_frame_is_logged_at_warning_with_the_exception` — **reproduced** |
| **M5** | **log at ERROR with a traceback but drop the node id** | the same store test (`"7" in getMessage()`) |
| **M6** | **log the node id at ERROR but drop the traceback (`logger.error`, no `exc_info`)** | the same store test (`record.exc_info is not None`) |
| **M7** | **ingest: log `type(exc).__name__` instead of the exception's own text** | the ingest test (`"shorter than"`) |
| M12 | `Registry.captured()` hardcodes `FIRED` (undo CRITICAL A, live half) | 4 tests |
| M13 | `_rehydrate_capture` hardcodes `FIRED` (undo CRITICAL A, rehydrated half) | the same 4 |
| M14 | `mark_state_possibly_stale` never called | `test_a_skipped_newer_row_flags_the_older_survivor_as_possibly_stale` |
| M16 | `heartbeat()` stops clearing `state_may_be_stale` | `test_a_fresh_live_report_clears_the_possibly_stale_flag` |
| M17 | `roster_ok` not folded into `ok` | `test_boot_survives_a_broken_node_roster_query` |
| M19 | `except Exception` → `except ValueError` in the state-step guard | 3 tests (Batch 1 guards intact) |
| M20 | roster query moved back outside the guard | `test_boot_survives_a_broken_node_roster_query` |
| M21 | `rehydration_errors` not reset per pass | `test_rehydration_errors_resets_each_call_not_accumulating` |
| M22 | `_VALID_STATE_VALUES` widened to `range(5)` | both wire-byte-4 tests (HEARTBEAT + CAPTURED) |
| M23 | `view()`: `None` state falls back to `"idle"` (undo WARNING J) | 2 tests, incl. the new never-heard-node test |
| M24 | roster → `SELECT DISTINCT node_id FROM event` (the option the implementer rejected) | `test_boot_survives_a_broken_node_roster_query` — **their diagnosis was accurate** |
| M27 | `/health` drops the `roster_lost` field | both K(b) tests |
| M28 | union restricted to `msg_type = OPERATOR_SET_TARGETS` (excludes ARM) | the never-heard-node test |

### Survived (10)

| # | Mutation | Meaning |
|---|---|---|
| **M8** | `max(facts, key=seq)` → `key=at` | The state-race ordering key is still unpinned. See above — and it is **distinguishable** with an ordinary fixture. |
| **M9** | union restricted to `source='operator' AND msg_type = OPERATOR_ARM` | **CRITICAL 1** — the roster test proves the ARM example, not the rule. |
| **M10** | downgrade only `_rehydrate_desired_command`'s skip log | K(a) pins 2 of 3 log sites. |
| **M11** | ingest logs correctly for `FrameError`, `logger.debug` for `PayloadError`/`UninterpretableFrame` | K(a)'s ingest pin covers 1 of 3 drop causes. |
| **M15** | `view()` emits `"state_may_be_stale": False` unconditionally | The flag is asserted nowhere above the `Registry` unit level. |
| **M18** | delete `self.roster_lost = False` from the top of `rehydrate()` | The per-pass reset is real in the code but **pinned by nobody**. |
| M25 | `_apply_captured`: non-`int` `state_after` → assume `2` instead of dropping | The live defensive branch is untested. |
| **M26** | `a_state_row_was_unreadable = True` unconditionally | **Every** trap would report `state_may_be_stale: true` and the suite stays green. |
| M29 | `restore_reported_state` also clears `state_may_be_stale` | The sticky-across-rehydrate behaviour is unpinned in either direction. |
| M30 | a tagless `CAPTURED` raises a `FAULT` instead of being dropped | Live branch untested (low value; unreachable from a valid decode). |

---

## CRITICAL

### CRITICAL 1 — CRITICAL 6's rule is not proven: an implementation that handles only operator `ARM` rows passes all 748 tests

**Where**: `src/feeder_station/adapters/store.py:615-622` (`_known_node_ids`);
`tests/test_cli.py:696-737` (`test_a_command_to_a_never_heard_from_node_survives_a_restart`).

**The rule, as the batch itself states it** — `remediation-tasks.md:238-240`: *"the roster
rehydration walks must include every node with an operator-sourced row"*; `apply-progress-batch2b.md`
line 27: *"**Rule proved**: the rehydration roster walk includes every node with *any* row in the
store … not 'the report's own example survives.'"*

**Measured**: narrowing the union to `... WHERE source = 'operator' AND msg_type = 256`
(`OPERATOR_ARM`) — i.e. an implementation that handles exactly the one command the test issues —
**passes all 748 tests, exit 0** (M9). The mirror mutation restricted to `SET_TARGETS` (M28) *is*
killed, which proves the point precisely: the suite is sensitive to the ARM example and blind to
everything else.

**Concrete failure scenario.** An operator opens the UI, expands *Editar lista de objetivos* for trap
77 — a trap freshly installed in the field whose radio has not yet reached the station — and saves a
tag list. `PUT /api/traps/77/targets` writes an `OPERATOR_SET_TARGETS` row and the UI shows the edit
as pending. The station is power-cycled that night. Under a roster derived from ARM rows alone, node
77 is not in the boot-2 roster, `_rehydrate_desired_command` never runs for it, and the pending
target list is gone — silently, with `ok: true`, `rehydration_errors: 0`, `roster_lost: false`, and
no row on the page to notice. That is
`specs/trap-commands/spec.md:29-34`'s *"MUST resend `SET_TARGETS` … until the node's `HEARTBEAT`
reports the `targets_version`"* broken across exactly the restart
`specs/event-store/spec.md:76-78` says must not depend on the node re-sending anything.

**Status of that scenario in the suite: UNTESTED.** The apply record verifies it by hand
(`apply-progress-batch2b.md`, the `PUT /targets 77` spot-check) and records the output — but a
throwaway script is not a regression guard. `test_an_unconfirmed_set_targets_command_survives_a_restart`
covers a node that *has* been heard from, so `node_cursor` carries it regardless.

**Why the code is nevertheless correct today**: the union is `source = 'operator'` with no
`msg_type` predicate, so all four operator types are covered — verified directly
(`OPERATOR_ARM` → `[66]`, `OPERATOR_SET_TARGETS` → `[66]`, `OPERATOR_CAPTURE_ACKNOWLEDGED` → `[66]`).
This is a proof defect, not a behaviour defect. It is also the **fifth consecutive appearance of the
same diagnosis** on this change, which is why it is rated CRITICAL rather than WARNING: the batch's
own headline claim ("rule proved, not example") is the thing that is false.

**Fix direction** (not applied): parametrize the never-heard-node test over the operator route that
issues each command — `POST /arm`, `POST /disarm`, `PUT /targets` — asserting the pending command and
`desired_targets_version` survive boot 2 in each case. ~6 test lines. M9 and M28 both die.

---

## WARNING

### WARNING 1 — `state_may_be_stale` keys on `except`, and misses the module's *other* not-readable path: a silent, uncounted false all-clear

**Where**: `src/feeder_station/adapters/store.py:553-559` (the flag is set only in the `except`
branch); `:641-650` and `:683-685` (the two `isinstance(...)` guards that return `None` *without*
raising).

`rehydrate()` has two ways a state-bearing row fails to produce a fact. One raises → counted, logged,
flagged. The other — the field is present but not an `int` — returns `None` → **not counted, not
logged, not flagged.** WARNING C's own rule is *"a fresher report existed and could not be read"*;
the fix implements *"a fresher row raised an exception."*

**Measured** (three probes, real `Store`, real `rehydrate`):

```
A. newest FAULT row unreadable (received_epoch=1e20, OverflowError)
     state=ARMED  state_may_be_stale=True   rehydration_errors=1     <- the documented case, works

B. newest CAPTURED row has state_after="CORRUPT" (valid JSON, wrong type)
     state=ARMED  state_may_be_stale=False  rehydration_errors=0     <- SILENT

C. newest HEARTBEAT row has state=null, older CAPTURED wins
     state=IDLE   state_may_be_stale=False  rehydration_errors=0     <- SILENT
```

Case B is strictly worse than the case WARNING C named: there, at least `/health`'s
`rehydration_errors` ticked. Here nothing does. The operator sees `TRAMPA 7 · ARMED`, `ok: true`,
`rehydration_errors: 0` — a confident, calm reading for a trap whose actual last word the station
could not read. That is verify-report-2.md's own *"a false all-clear, on the one event the whole
system exists to surface"*, one corruption shape over.

Reachability is narrower than case A (it needs the `fields` JSON cell to stay parseable while a value
changes type), but it is inside this remediation's declared threat model — the `isinstance` guards
exist in the source *because* the author already considered exactly this row shape.

**Fix direction**: have the two `isinstance` guards feed the same `a_state_row_was_unreadable` flag
(and `rehydration_errors`) instead of returning `None` silently — the distinction the code currently
draws between "raised" and "unusable" is not a distinction WARNING C cares about.

### WARNING 2 — the possibly-stale signal never reaches an operator, and is unconstrained in both directions

**Where**: `src/feeder_station/static/index.html:122-158` (`renderTrap`);
`src/feeder_station/domain/traps.py:301-307` (`view()`); `tests/adapters/test_store.py:670,682,686`
(the only three assertions that ever read the flag — all at the `Registry` unit level).

`renderTrap` reads `node_id`, `reachable`, `battery_mv`, `targets_version`, `last_seen`, `state`,
`targets`, `pending_command`. It does **not** read `state_may_be_stale`. The rendered HTML for a
stale-flagged trap is therefore byte-identical to that of a confident one. The `⚠ SIN CONTACTO` badge
is keyed on `reachable`, which after a restart is computed from the *winning row's* `received_epoch`
— so a trap whose last readable report was 5 minutes ago renders `TRAMPA 7 · ARMED` with no badge,
no styling change, nothing. **A stale-flagged `armed` trap and a confident `armed` trap look exactly
the same at a glance.**

The API layer is equally unconstrained: **M15** (`view()` emits `False` unconditionally) and
**M26** (`a_state_row_was_unreadable = True` unconditionally, so *every* trap is flagged) **both
survive all 748 tests**. The field could be deleted, or turned into a blanket false positive on every
trap in the field, and CI would not notice.

WARNING C's own close language (`remediation-tasks.md:279`, carried from task 2.8) is *"a state the
interface can render as unknown beats a default that renders as safe."* The interface renders
nothing. Task 2.9's tick for this half overstates what shipped.

**Fix direction**: one `renderTrap` line (a badge beside the chip, e.g. `⚠ puede no ser el último
reporte`) plus one `/api/traps`-level assertion in `test_cli.py` — M15 and M26 both die.

### WARNING 3 — WARNING K(a) closed 2 of 3 log sites; both remaining sites measured unpinned

**Where**: `src/feeder_station/adapters/store.py:594` (the `_rehydrate_desired_command` skip log);
`src/feeder_station/adapters/ingest.py:91-100` (one log site, three caught exception types).

K(a)'s stated close (`verify-report-batch1-round2.md:351-353`) is *"one `caplog` assertion **per log
site**"*.

- **M10 SURVIVES**: downgrading *only* `rehydrate()`'s second skip log — the one that fires when a
  node's pending-command replay fails — to `logger.debug("rehydrate: skipped a row")` leaves 748
  green. That is the branch that hides a lost `ARM`/`SET_TARGETS`, which is CRITICAL 6's own subject
  matter.
- **M11 SURVIVES**: `if isinstance(exc, FrameError): logger.warning(... exc) else: logger.debug(...)`
  leaves 748 green. The ingest test drives a 3-byte frame, i.e. `FrameError` only; the
  `PayloadError` and `UninterpretableFrame` drop paths — the ones a wire-format disagreement with real
  firmware produces — are unpinned.

The pins that exist are genuinely strong (M3, M5, M6, M7 all killed — level, node id, traceback and
exception text are each independently asserted). The gap is coverage breadth, not assertion quality.
Two more `caplog` tests close it.

### WARNING 4 — the partial roster: the third case, still indistinguishable from a healthy boot

**Where**: `src/feeder_station/adapters/store.py:538-544`.

`roster_lost` is all-or-nothing: it is set only when the roster *query itself* raises. A query that
succeeds while returning fewer rows than it should — a `node_cursor` row lost to a partial write, or
one node's rows landing outside the `UNION`'s reach — is neither "one bad row" nor "roster lost", and
nothing reports it.

**Measured** (three healthy nodes seeded, node 2's `node_cursor` row deleted, its `HEARTBEAT` row
left intact):

```
ok=True   roster_lost=False   rehydration_errors=0   /api/traps -> [1, 3]
```

Node 2 vanishes with every signal reading healthy. This is WARNING K(b)'s own complaint — *"the
`/api/traps` count is the only tell, and only to someone who knows how many traps there should be"* —
reproduced exactly, one layer in. WARNING K offered three closes; the batch took two
(`roster_unreadable: true`, folding into `ok`) and left the third (*"a count of nodes expected vs
rehydrated"*), which is the one that covers this case.

Also confirmed: `roster_lost` **does** reset correctly across repeated `rehydrate()` calls
(healthy → `False`, table dropped → `True`, table restored → `False`), so the reset claim is true —
but **M18 shows no test pins it**.

### WARNING 5 — the state-race ordering key is an undefended decision (M8 survives), and the test written to defend it cannot

Covered in full under *The A3 structural argument* above. Summarised here so it is not lost:
`max(facts, key=seq)` → `key=at` survives all 748; the new test's two rows agree under both keys; and
a two-row fixture with a backward `received_epoch` (the station's own clock being stepped — the
condition `clock_trust.py` exists for) makes the mutant diverge live-vs-rehydrated, reopening
CRITICAL 1. The decision is right; it is one fixture number away from being proven, and is currently
proven by nothing.

---

## The `ok: false` question — explicit verdict

### Is anything in this repo consuming `ok` to restart or remove the station?

**No.** Exhaustively checked:

| Candidate | Finding |
|---|---|
| `deploy/feeder-station.service` | `Restart=always`, `RestartSec=2`, `TimeoutStopSec=10`, `KillSignal=SIGTERM`. **No `WatchdogSec`, no `ExecStartPost`, no health poll.** systemd restarts on *process exit*, and has no way to read a JSON body. |
| Dockerfile / `HEALTHCHECK` / compose | None exist (`fd` over the whole repo: the only config files are `deploy/feeder-station.service`, `openspec/config.yaml`, `pyproject.toml`). |
| CI | No `.github/` at all. |
| Any script | None. |
| `docs/deploy.md` | Step 4 is a human running `curl http://<pi>:8000/health`. The runbook's only guidance about the body is *"A healthy response's `workers.gateway.enabled` is `true` only once a real serial device is configured."* |
| `static/index.html` | Fetches `/health` (line 67) and reads **only** `clock_trust` (line 79) and `link` (line 91). It never reads `ok`, and never reads `roster_lost`. (`response.ok` at line 181 is the `fetch` Response's own property, unrelated.) |

`ok` is read by tests and by nothing else: 9 assertions across `tests/test_cli.py` and
`tests/test_app.py`. It is not load-bearing for the UI, the SSE stream, or the export path.

### Does `/health` return a non-200 when `ok` is false?

**No — always 200.** Measured directly against a genuinely lost roster:

```
GET /health     -> HTTP 200   {'ok': False, 'roster_lost': True, 'rehydration_errors': 1}
GET /           -> HTTP 200
GET /api/traps  -> HTTP 200   []
```

This cuts both ways, and both ways matter for the runbook:

1. **It defuses the crash loop.** Every off-the-shelf probe that would restart or remove a service —
   Docker `HEALTHCHECK` (exit status), Kubernetes `httpGet` (status code), systemd watchdog (ping,
   not HTTP), nearly every uptime monitor's default — acts on the HTTP status, which stays 200. Only
   a consumer written specifically to parse the body and branch on `ok` would ever act.
2. **It also means the signal is easy to miss.** The same naive wiring that cannot loop also cannot
   *see* `roster_lost`. Someone who wires a status-code probe and calls monitoring done will get a
   green light on an empty registry forever.

### Is `ok: false` correct in principle?

**Yes.** Arguing it both ways, and then deciding:

**Against** — a station with a lost roster is *degraded, not dead*. It is serving `/health` and the
UI, accepting live frames, appending to the store, ACKing on the radio, and re-populating the
registry from every frame that arrives. Calling that "not ok" invites a supervisor to kill something
that is still doing most of its job, and a restart provably does not fix it: the store is still
whatever it was. `roster_lost → ok: false → restart → roster_lost` is CRITICAL 2's crash loop
relocated to the health signal.

**For** — and this is decisive:

1. **The hazard is not new, and `roster_lost` is not the worst case of it.** `ok` in this repo
   already means *"is this station fully functional right now"*, not *"is this process alive"*:
   `ok = workers_ok and link_ok and roster_ok` (`app.py:358`). `link_ok` already goes false when a
   *configured* radio is unplugged — also unfixable by a restart. Any consumer wired to restart on
   `ok: false` was already in a loop on an unplugged antenna before this batch. `roster_lost` widens
   an existing class; it does not create one.
2. **It was one of the finding's own sanctioned closes.** WARNING K(b) (`verify-report-batch1-round2.md:351-353`)
   says: *"either a distinct signal for the roster branch … **or** folding it into `ok`."* The
   implementer did both. That is not an unlicensed invention.
3. **The alternative loses the one consumer that exists.** `docs/deploy.md` trains a person to
   `curl /health` and read it. A distinct field alone, with `ok: true` beside it, is precisely the
   "silent empty station claiming `ok: true`" that WARNING K(b) was raised about.
4. **`roster_lost` is its own field**, so a body-parsing consumer can distinguish "restart might help"
   (`workers_ok` false — a dead thread) from "restart will not help" (`roster_lost` true). The
   information needed to *avoid* the loop is present in the response.
5. **This repo's real restart loop is elsewhere and already open.** `verify-report-batch1-round2.md`'s
   CRITICAL I: a corrupt `station.db` raises out of `Store.__init__` inside `cli.build_app`, so the
   process exits and `Restart=always` + `RestartSec=2` loops it every two seconds forever, with no
   `/health` to consult. That is the crash loop this deployment actually has, it is process-exit
   driven, and `ok` does not feed it. Batch 5 owns it.

**Verdict: `ok: false` is correct as implemented; the crash-loop path is hypothetical today (nothing
consumes `ok` at all) and would be a pre-existing hazard rather than a new one if something did.**
Rated **SUGGESTION**, not WARNING — but `docs/deploy.md` **should** carry the warning, because it is
the only document that tells anyone to read `ok`, and the next person wiring monitoring will read it.
See SUGGESTION 1 for the exact text.

---

## Regressions — none

| Property | Pinned by | Status |
|---|---|---|
| `rehydrate()` totality (`except Exception`, not a type whitelist) | M19 killed (3 tests) | intact |
| Roster query inside the guard | M20, M24 killed | intact |
| `rehydration_errors` reset per pass | M21 killed | intact |
| `payloads._VALID_STATE_VALUES == range(len(TrapState))`, wire byte 4 rejected on both HEARTBEAT and CAPTURED | M22 killed by both tests | intact — nullable `state` still has not widened the range |
| Nullable `state` honest through every consumer | M23 killed (2 tests) | intact — re-measured below |
| CRITICAL A, both halves | M12, M13 killed (4 tests each) | intact |
| Five-ordering property test | 5 cases collected and green; the 4-case `state_after` table now closes its content axis (M13 dies on 3 of 4) | still meaningful, and strictly stronger than at Batch 2a |

**A never-heard node renders honestly through every consumer** (question 6), re-derived:

| Consumer | Behaviour | Verdict |
|---|---|---|
| `GET /api/traps` | `{"node_id": 99, "state": null, "state_may_be_stale": false, "reachable": false, "last_seen": null, "pending_command": "arm", …}` | honest; pinned by `test_a_command_to_a_never_heard_from_node_survives_a_restart` (`assert … ["state"] is None`), killed by M23 |
| SSE `GET /api/events` | payloads are `{"type": "traps_changed"}` / `{"type": "traps_changed", "node_id": …}` — never carry state | nothing to fabricate |
| `static/index.html:131` | `t.state != null ? t.state : "desconocido"` → `TRAMPA 99 · DESCONOCIDO`, plus `no-contact` styling and the `⚠ SIN CONTACTO` badge (`reachable:false`) | distinct and prominent |
| `adapters/export.py::export_events_csv` | selects raw `event` columns; never touches `Trap.state` | unaffected |
| `Registry.view()` sort | by `node_id` only | no odd ordering |

No path fabricates `idle`. WARNING J remains closed.

**The self-reported near-miss (question 12) — verified repaired.**
`tests/adapters/test_store.py::test_rehydrate_skips_and_counts_an_unreadable_row_without_blocking_other_nodes`
was extracted by AST at `0fa597d` and at `HEAD` and compared: **ASTs are identical**. All three of its
assertions (`rehydration_errors == 1`, node 7 absent from the roster, node 9 still `ARMED`) are
intact and still kill M19. The truncated-anchor `Edit` was genuinely caught and repaired before the
commit.

**The shifted test (question 10) — verified, with one correction to the record.**
`git diff 0fa597d..HEAD -- tests/` contains exactly **13 removed lines**, examined individually:
two `_OrderedRegistry.captured` signature lines, one `from datetime import timedelta` import, two
other import lines, three `registry.captured(...)` call-site updates for the new `state` argument,
four comment lines (WARNING D's correction, itself accurate — I re-measured that a trap for node 7
*is* now created), and **`assert body["ok"] is True`**. That last one is the only pre-existing
assertion that changed, as claimed.

The spec citation holds: `specs/event-store/spec.md:76-78` does require the registry to be rebuilt
*"without depending on the node re-sending anything"*, and a registry that never got built does not
meet it. **But the apply record's characterisation is wrong**: calling the change *"strictly stricter
than before (three assertions instead of two)"* describes an addition. What happened is an
**inversion** — `ok is True` became `ok is False`. The new behaviour is justified (see the `ok`
section), but an inverted assertion is a behaviour change and should be recorded as one. On a change
whose entire history is inaccurate records, that phrasing is worth correcting.

---

## SUGGESTION

1. **`docs/deploy.md` should say which `ok: false` conditions a restart fixes.** Step 4 is the only
   place anything tells a human to read `ok`, and the next person wiring monitoring will start there.
   Two sentences, beside the existing `curl` block: (a) `/health` always answers **200**; `ok` lives
   in the body, so a status-code-only probe will never see a degraded station — parse the body;
   (b) `ok: false` covers three different conditions and only one of them is restart-fixable — a dead
   worker thread (`workers.*.alive: false`) usually is; a down link (`link.connected: false`) and a
   lost roster (`roster_lost: true`) are **not**, and wiring either to an automatic restart produces a
   loop. This is the cheapest possible insurance against re-creating CRITICAL 2's shape one layer up,
   and costs nothing today.
2. **The roster query still misses `source = 'station'`.** `Source` is
   `Literal["node", "operator", "station"]` (`events.py:28`) and `clock.py:238-248` writes
   `STATION_TIME_SYNC_WITHHELD` rows that name a `node_id`; `_known_node_ids` covers `node_cursor` +
   `source = 'operator'` only. Verified: a store whose only row is a station-sourced one returns
   `[]` from `_known_node_ids`. **Currently unreachable in production** — `attempt_time_sync` is only
   ever called for `node_ids` already drawn from the registry (`workers.py:215`), so a station-sourced
   row never names a node the union has not already found, and nothing in `rehydrate()` reads one.
   But `_known_node_ids`' own docstring and `apply-progress-batch2b.md`'s "every node with *any* row"
   both overstate what the query does. Either widen to `source != 'node'` or correct the two claims.
3. **`state_may_be_stale` is sticky across `rehydrate()` calls, while `roster_lost` resets.**
   `restore_reported_state` does not clear the flag, so a second `rehydrate()` on the same `Registry`
   keeps it set even after the offending row is repaired (measured: pass 2 reads `state=FAULT`,
   `rehydration_errors=0`, `state_may_be_stale=True`). Harmless today — production rehydrates once
   per boot into a fresh `Registry` — but it is the exact asymmetry task 2.9 designed away for
   `roster_lost`, and **Batch 5 (CRITICAL I) is the batch most likely to introduce a retry/recovery
   rehydrate**. One line in `restore_reported_state`, or a reset alongside `roster_lost`. M29 shows
   nothing pins either choice.
4. **Pin `roster_lost`'s per-pass reset.** M18 survives. `test_rehydration_errors_resets_each_call_not_accumulating`
   is the obvious home — one extra assertion.
5. **`_apply_captured`'s defensive branch is untested** (M25): a non-`int` `state_after` currently
   drops the registry update silently, and changing it to assume `FIRED` passes everything. Same
   family as WARNING 1, on the live path. Unreachable from a valid decode (`decode_captured` validates),
   so genuinely a SUGGESTION — but if WARNING 1 is fixed, fix both sides together.
6. **Batch 2b ran 31% over its 300-line budget** (393 changed lines, ~75% tests), disclosed in the
   apply record rather than pressed through silently — the right call. The record also notes this is
   the fifth consecutive round over target (252/220, 335/150, 610/350, 260/200, 393/300). That
   pattern is now data, not noise: the per-slice forecast in `remediation-tasks.md` is systematically
   low for a change whose every task demands a mutation-killing test plus a citation trail. Worth one
   design-time conversation before Batch 3's forecast is trusted.
7. **Correct `apply-progress-batch2b.md`'s "strictly stricter"** to "inverted, and justified by
   `event-store/spec.md`'s rehydration requirement" — see the question-10 section.

---

## Spec compliance movement (Batch 2 scope only)

| Capability | Scenario | Before | After | Evidence |
|---|---|---|---|---|
| event-store | registry reflects last known state after restart | ⚠️ PARTIAL | ✅ **MET** | 5 orderings × 4 `state_after` values, all AGREE, re-measured through real wire bytes + a real boot; M12/M13 pin both paths |
| event-store | rebuild without depending on the node re-sending | ❌ VIOLATED (CRITICAL 6) | ⚠️ **PARTIAL** | Correct in code for every operator row type; **untested for anything but `ARM`** (M9) — CRITICAL 1 |
| trap-commands | resend continues until version confirmed, across a restart | ⚠️ PARTIAL | ⚠️ **PARTIAL** | Holds when measured by hand; no test covers a never-heard node with `SET_TARGETS` — CRITICAL 1 |
| trap-commands | displayed state is the trap's own most recent report (S4) | ⚠️ PARTIAL | ⚠️ **PARTIAL** | Holds by arrival order, live and rehydrated agreeing; the key itself is unpinned — WARNING 5 |
| operator-ui | an unreachable / unknown trap is marked, never shown as calm | ⚠️ PARTIAL | ⚠️ **PARTIAL** | `state: null` → `DESCONOCIDO` is honest and pinned; a *possibly stale* state is invisible on the page — WARNING 2 |
| station-deployment | `/health` distinguishes a degraded boot from a healthy one | ❌ VIOLATED | ⚠️ **PARTIAL** | The two named cases are distinguishable and pinned; a truncated roster still reads healthy — WARNING 4 |

---

## Final verdict

**FAIL** — 1 CRITICAL, 5 WARNING, 7 SUGGESTION.

What is genuinely good here, said plainly: **CRITICAL A is closed, properly.** It was closed on both
paths at once, with a parametrized table over the content axis that kills the hardcode from either
side — the fix batch 2a asked for, executed exactly. The `caplog` pins are strong where they exist
(level, node id, traceback and exception text each independently asserted; three invented bypass
mutations all died). Every Batch 1 guard survived re-probing. Every commit is independently green and
the counts climb monotonically. Every RED claim replays. `roster_lost` really does reset per pass. The
`design.md` and `remediation-tasks.md` edits are exactly in scope, with zero removed lines. The
implementer disclosed its own near-miss and its own budget overrun, and the near-miss really was
repaired — AST-identical. The ordering decision in `store.py:566-579` is well-reasoned and its two
supporting facts (`clock_trust.py`'s existence, `decode_fault`'s missing epoch) are both true.

And the diagnosis recurs, for the fifth time, in three places at once:

- **CRITICAL 6's test proves the ARM example, not "every node with any row"** — a roster restricted to
  `ARM` passes all 748 (M9).
- **WARNING C's fix keys on the failing example's mechanism** (`except`) rather than the rule ("a
  newer state row could not be read") — the `isinstance` path produces a silent, uncounted false
  all-clear, and the signal never reaches the page at all.
- **WARNING A's new test encodes the story** (a backlog replay ordered by `node_epoch`) rather than
  the rule (arrival order wins) — so the mutation that actually survives, `key=at`, is untouched, and
  the "structurally undetectable" argument is disproven by a two-row fixture.

In each case the production code is right and the proof is not. That is a better failure than the
previous four rounds — none of these is a live defect on a well-formed log — but it is the same
failure, and the apply record's headline ("**Rule proved**: … not 'the report's own example
survives'") is the specific claim that does not hold.

**May Batch 2 be pushed?** **Not as a CRITICAL 6 closure.** The remedy is small and belongs in this
stack: parametrize the never-heard-node test over `POST /arm` / `POST /disarm` / `PUT /targets`
(~6 lines, kills M9 and M28). If the team would rather push the seven commits now, they must land with
CRITICAL 6 recorded as **PARTIAL — code correct, rule untested** in `remediation-tasks.md` and in the
Spec Compliance Matrix, and with task 2.9's WARNING-C half **un-ticked**: the flag it added is not
visible to any operator and is unconstrained in both directions (M15 and M26 both survive).

**May Batch 3 start?** **Yes, now.** Batch 3 is CRITICAL 3 + CRITICAL 4 — the station-wide frame
budget, in `adapters/workers.py`, `adapters/scheduler.py` and `domain/backlog.py`. It touches none of
`_known_node_ids`, `/health`, `rehydrate()`'s state race, or `domain/traps.py`. Carry WARNING 1 and
WARNING 2 forward as a single item — "make the possibly-stale signal cover both unreadable paths and
render it on the page" — rather than letting them close narrower than the hole a third time.
