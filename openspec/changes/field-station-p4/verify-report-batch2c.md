# Verification Report — field-station-p4, Batch 2c (Batch 2 close-out)

**Scope**: `1c59681..HEAD` — 7 commits (`ae6a462..0374341`), 684 changed lines
(635 insertions / 49 deletions). Closes verify-report-batch2.md's CRITICAL 1,
WARNING 1-5 and SUGGESTION 1, plus five record corrections.
**Artifact store**: openspec. **Mode**: Strict TDD.
**Out of scope, deliberately not reported**: CRITICAL 3, 4, 5, I (Batches 3-5).

**Verdict: FAIL** — 1 CRITICAL, 4 WARNING, 8 SUGGESTION.

Everything below was re-derived from the repository. The apply record was treated
as a hypothesis: all nine mutations it names were reproduced, **29 more were
invented (38 total, 30 killed, 8 survived)**, every claimed closure was probed
against the live code, and the new `_reported_state` semantics were walked
exhaustively over all three `CommandKind`s. The code is correct everywhere I
could reach it. **Two of the seven closures are narrower than the rule they
state, and one of those two is reachable through two ordinary operator clicks
with no corruption at all.**

---

## Executive verdicts

| Question | Verdict |
|---|---|
| **CRITICAL 1 (roster rule)** | **CLOSED.** M9 and M28 both die. A fourth operator `msg_type` exists and is uncovered, but behaviourally equivalent — SUGGESTION 2. |
| **WARNING 5 (ordering key)** | **CLOSED.** M8 dies. The `node_epoch` axis is still guarded, the stepped-clock fixture is physically coherent. |
| **WARNING 1 (unreadable = unreadable)** | **PARTIALLY CLOSED.** The two named `isinstance` guards raise, are counted, are flagged, are pinned, and stay inside the guard. **Three more "not readable" expressions are still silent — WARNING 1 below.** |
| **WARNING 2 (the stale badge)** | **PARTIALLY CLOSED.** The `/api/traps` layer is genuinely pinned both ways. **The UI pin constrains neither polarity nor content: the inverted badge ships green — WARNING 2 below.** |
| **WARNING 3 (log sites)** | **PARTIALLY CLOSED.** M10 and M11 die. `store.py:556`, the fourth log site, is unpinned — WARNING 3 below. |
| **WARNING 4 (partial roster)** | **CLOSED, with a new latent false alarm.** No false alarm for the honest-skip case (measured: 20 nodes, one corrupt, `ok: true`). But the check compares two queries that differ *by design* on `source = 'station'` — WARNING 4 below. |
| **SUGGESTION 1 (`docs/deploy.md`)** | **CLOSED.** Both sentences accurate; `/health` re-measured as always 200. |
| **The `_reported_state` semantic change** | **Correct in every branch, half proven.** See its own section — **CRITICAL 1**. |
| **May Batch 2 be pushed?** | **Not as a complete close-out.** ~10 test lines short. See the final section. |
| **May Batch 3 start?** | **Yes, now.** No overlap. One interaction to carry forward. |

---

## Gates (each exit status checked directly — nothing piped into `head`/`tail`)

| Gate | Exit | Result |
|---|---|---|
| `uv run pytest` | **0** | 757 passed (748 baseline + 9 new) |
| `uv run ruff check .` | **0** | All checks passed |
| `uv run ruff format --check .` | **1** | 6 files — **file list byte-identical to a real `504d18e` checkout**, diffed directly. **Zero new drift.** |
| `uv run mypy src` | **0** | no issues in 22 source files |

Baseline verified by adding a `504d18e` worktree, running this repo's own `ruff`
against it, and diffing the two file lists: identical.
`design.md`, `payloads.py`, `test_ingest.py`, `test_serial_gateway.py`,
`test_events.py`, `test_traps.py`. `test_ingest.py` was modified by this batch and
its reported drift is still the pre-existing byte-list at line 96, not new code.

### Per-commit greenness — the `git checkout --` incident, audited (question 9)

`src` + `tests` checked out at each sha in the real tree (never a worktree — see
*Method*), full suite + `ruff check` + `mypy src` run at each, tree restored:

```
ae6a462  pytest=0 (750 passed)  ruff=0  mypy=0   CRITICAL 1, roster rule
1ee240e  pytest=0 (750 passed)  ruff=0  mypy=0   WARNING 5, ordering teeth
50341c4  pytest=0 (752 passed)  ruff=0  mypy=0   WARNING 1, unreadable counts
76972a0  pytest=0 (754 passed)  ruff=0  mypy=0   WARNING 3, two log sites
ed9a273  pytest=0 (756 passed)  ruff=0  mypy=0   WARNING 2, the badge
b6a08ec  pytest=0 (757 passed)  ruff=0  mypy=0   WARNING 4, partial roster
0374341  pytest=0 (757 passed)  ruff=0  mypy=0   docs
```

Monotonic (750, 750, 752, 754, 756, 757, 757), every commit independently green on
all three gates, bisect-safe. The `+9 new tests` arithmetic checks out exactly
(2 parametrize cases, 2 WARNING 1, 2 WARNING 3, 2 WARNING 2, 1 WARNING 4).
**Nothing was lost to the reported `git checkout --` incident**: every fix the
apply record describes is present in the final tree, each in the commit it claims,
and the reconstruction did not leave a commit that only passes with its successors.

---

## The `_reported_state` semantic change — explicit verdict

**The change is correct. Half of it is proven.**

`_reported_state` (`src/feeder_station/adapters/store.py:827-853`) now returns
`ReportedState | None`, and `_rehydrate_desired_command` (`:804-814`) skips
`satisfied_by` entirely when it is `None`.

### All three kinds × every reading — measured, not argued

```
reading                     ARM                 DISARM              SET_TARGETS(v=5)
None (nothing known)        pending (skipped)   pending (skipped)   pending (skipped)
armed=False, tv=None        pending             SATISFIED -> drop   pending
armed=True,  tv=None        SATISFIED -> drop   pending             pending
armed=False, tv=5           pending             SATISFIED -> drop   SATISFIED -> drop
armed=False, tv=4           pending             SATISFIED -> drop   pending
```

`None` is handled correctly for **all three** kinds, not only the two the new test
exercises: for `ARM` and `SET_TARGETS` the old fabricated `armed=False, tv=None`
happened to reach the same answer (an unconfirmed version never equals an `int`),
so the change is behaviour-neutral for them and a genuine fix for `DISARM` alone —
exactly as the record claims. `_reported_state` was measured directly against all
four registry shapes (no entry / entry with `state=None` / `ARMED` / `IDLE`+`tv=5`)
and answers correctly in each.

### The live path cannot fabricate an equivalent `ReportedState` — verified

`Registry.heartbeat()` (`domain/traps.py:132-137`) is the only other
`satisfied_by` caller. Its `ReportedState` is built from the method's own `state`
parameter, which is always a really-reported state decoded from a real HEARTBEAT
(`ingest.py:170-176`). **A never-heard node cannot reach it by definition** —
calling `heartbeat()` means the node was heard. The one residual doubt,
`trap.targets_version` surviving a HEARTBEAT that reports no version
(`traps.py:124-125` only overwrites when not `None`), cannot wrongly satisfy
anything: `PUT /targets` computes `next_version = (current or 0) + 1`
(`app.py:442`), so a pending version never equals an already-reported one.

### No command can get stuck forever *because of this change* — verified

The `None` path only *withholds* a check until a real report arrives; it never
installs a command that nothing can clear. Measured on a live `Registry`:

```
pending ARM    + heartbeat(ARMED) -> CLEARED      pending DISARM + heartbeat(IDLE)  -> CLEARED
pending ARM    + heartbeat(IDLE)  -> pending      pending DISARM + heartbeat(ARMED) -> pending
pending ARM    + captured(IDLE)   -> pending      pending DISARM + captured(IDLE)   -> pending
pending ARM    + fault()          -> pending      pending DISARM + fault()          -> pending
```

The `traps.py::heartbeat` comment's own hazard ("no single shared 'clear on ARMED'
rule that could quietly leave a pending disarm stuck forever") is intact: DD15's
per-kind rule resolves both directions on the next HEARTBEAT. The pre-existing gap
that `captured()`/`fault()` never run the check is **unchanged by this diff**, but
this diff does widen the population exposed to it — see SUGGESTION 5.

**And then: CRITICAL 1.**

---

## CRITICAL

### CRITICAL 1 — the second half of `_reported_state`'s new `None` semantics is pinned by nothing, and reverting it silently drops a pending `DISARM` after two ordinary operator clicks

**Where**: `src/feeder_station/adapters/store.py:851-852` (`if trap.state is None:
return None`); `tests/test_cli.py:841-850`
(`test_a_command_to_a_never_heard_from_node_survives_a_restart`, the parametrize
that is supposed to prove it).

The batch states the rule at `apply-progress-batch2c.md:36-38`: *"`_reported_state`
now returns `None` when nothing is known about a node (**no trap entry, or a trap
entry whose `state` is still `None`**)."* Two branches. The function has both.
**Only the first is pinned.**

**Measured (mutation N2)** — revert *only* the second branch, leaving the `KeyError`
branch as shipped:

```python
    if trap.state is None:
        return ReportedState(armed=False, targets_version=trap.targets_version)
```

**All 757 tests pass, exit 0.** The parametrize case that kills the `KeyError`
revert (N1, N3, N4 — all killed) never reaches this line, because `POST /disarm`
on a never-heard node creates no registry entry before `_reported_state` runs.

**Concrete failure scenario — no corruption required.** An operator working trap 77,
a unit installed in the field whose radio has not yet reached the station:
expands *Editar lista de objetivos*, saves a tag list (`PUT /api/traps/77/targets`),
then presses **Desarmar** (`POST /api/traps/77/disarm`). The `SET_TARGETS` row makes
`_rehydrate_desired_command` call `restore_desired_tags` first
(`store.py:782-785`), which creates a registry entry with `state` still `None`;
the newer `DISARM` row then wins the `latest` scan and is checked against that
entry. Measured, boot 1 → boot 2 through two real `build_app` boots:

```
HEAD           boot2 node 77: state=None  pending_command='disarm'  targets=['0384199C87EC06']
N2 mutant      boot2 node 77: state=None  pending_command=None      targets=['0384199C87EC06']
               (health identical in both: ok=True, roster_lost=False, rehydration_errors=0)
```

The disarm is gone. The page still shows the target list, so the row looks
attended-to; `/health` reads perfectly healthy. That is
`specs/trap-commands/spec.md`'s resend-until-reported requirement broken across
exactly the restart `specs/event-store/spec.md:76-78` says must not depend on the
node re-sending anything — **the same defect this batch was written to fix, on the
branch it did not pin.**

**Why the code is nevertheless correct today**: HEAD returns `None` on both
branches, verified by direct call and by the two-boot probe above. **This is a
proof defect, not a behaviour defect.**

**Why CRITICAL and not WARNING**: the previous round rated the structurally
identical finding CRITICAL on the ground that *"the batch's own headline claim is
the thing that is false."* The same applies verbatim here, and this branch is
**strictly more reachable** than the one that is covered — it needs no unreached
node and no corrupt row, only two operator actions in sequence. This is the sixth
consecutive round of *the code is right, the test proves the example rather than
the rule*, and it recurs inside the very fix that discovery produced.

**Fix direction** (not applied): one more parametrize case, or one extra step in
the existing `set_targets` case — issue `PUT /targets` then `POST /disarm` against
the same never-heard node and assert `pending_command == "disarm"` after boot 2.
~4 test lines. N2 dies.

---

## WARNING

### WARNING 1 — "a row that cannot be read is a row that cannot be read" is still not the rule the module implements: three more silent, uncounted paths

**Where**: `src/feeder_station/adapters/store.py:734` (`captured.tag is None`);
`:799-802` (`SET_TARGETS` `targets_version` not an `int`); `:823`
(`_tags_from_fields(...) or ()`).

The fix is real for the two guards it names (N8 and N9 both die; the new `raise`s
are inside the state-step guard at `:569-575`, so **Batch 1's totality is not
reopened** — M19, narrowing that guard, still dies). But the stated rule —
`apply-progress-batch2c.md:69-70`, *"whether the code expresses that by raising or
by returning `None`"* — has three more expressions in the same module, all still
silent. Measured on a real `Store` through a real `rehydrate`:

```
D  newest CAPTURED row, tag NULL (corrupt BLOB)
     state=ARMED  state_may_be_stale=False  rehydration_errors=0   <- SILENT
E  operator SET_TARGETS row, targets_version="CORRUPT"
     pending_command=None  desired_tags=(the tags)  rehydration_errors=0   <- SILENT
F  operator SET_TARGETS row, tags="CORRUPT" (version fine)
     pending_command=SET_TARGETS(v=5)  desired_tags=()  rehydration_errors=0   <- SILENT
```

**Case D is WARNING 1's own case B, one field over.** `_rehydrate_capture` returns
`None` at `:734` *before* it ever reaches the `state_after` guard this batch fixed:
a newest CAPTURED row whose `tag` cannot be read loses both the capture object (S3)
and its state-race candidate, the older HEARTBEAT wins, and the operator reads
`TRAMPA 7 · ARMED`, no stale badge, `ok: true`, `rehydration_errors: 0`. A real
CAPTURED row always carries a tag (`decode_captured` requires one), so
`tag is None` means exactly *unreadable* — the same corruption class the
`isinstance` guards exist for. **This is verify-report-batch2.md's own "a false
all-clear, on the one event the whole system exists to surface", reproduced after
the fix that was supposed to close it.**

**Case E is CRITICAL 6's subject matter.** A corrupt `targets_version` makes
`_rehydrate_desired_command` `return` at `:801-802`, discarding the node's entire
pending command — not counted, not logged, not flagged — while `desired_tags` is
already restored, so the page shows the edit as if it had been confirmed.

**Case F fabricates.** `tags = _tags_from_fields(latest.fields) or ()` at `:823`
turns an unreadable tag list into an *empty desired list* — the exact "invent the
calmest-looking value" shape WARNING J exists to forbid. The wire is protected
(`command_sender._stored_tags` refuses to send), so the pending command simply
never resolves, and the page reads `Objetivos actuales: ninguno`.

**Fix direction**: route `:734`, `:801` and `:823` through the same `raise` the two
fixed guards now use. The previous round asked for exactly this: *"rather than
letting them close narrower than the hole a third time."*

### WARNING 2 — the badge's only test constrains neither its polarity nor its content: the inverted badge ships green

**Where**: `tests/test_app.py:406-410`;
`src/feeder_station/static/index.html:149`.

The API half is genuinely closed — M15 (`view()` emits `False` always) and M26
(flag every trap) both die against
`tests/test_cli.py:670` `test_state_may_be_stale_is_true_only_for_the_node_that_earned_it`,
which boots a real store with one genuinely stale node and one genuinely healthy
node. That is a strong test.

The UI half is a single source regex:

```python
assert re.search(r"t\.state_may_be_stale\s*\?", html)
```

It pins that the substring `t.state_may_be_stale ?` exists somewhere in the file.
It does not pin what the two branches are. Measured:

| Mutation | Result |
|---|---|
| badge emitted unconditionally (the one the record names) | **KILLED** |
| **N5 — `${!t.state_may_be_stale ? '<p class="stale-badge">…' : ""}`** | **SURVIVES all 757** |
| **N6 — `${t.state_may_be_stale ? "" : ""}`** | **SURVIVES all 757** |

**Concrete failure scenario.** N5 ships. Every trap the station *is* confident
about now wears "⚠ puede no ser el último reporte de esta trampa", and the one trap
whose newest report the station genuinely could not read wears nothing. A biologist
walking a line of twenty traps reads the one with no warning as the trustworthy
one. The signal is not merely lost — it is **exactly inverted**, and CI is green.
N6 is the milder mirror: the badge is deleted, the regex still matches, and the
suite still claims the flag "reaches the operator".

The record's own criterion at `apply-progress-batch2c.md:87-88` is *"a bare
substring check would not catch 'always emit the badge' or 'never emit it'"* — the
regex does catch the first and **does not catch the second**.

*(The file-read is fine on its own: `tests/test_app.py:234-240` already asserts
`GET /` is byte-identical to this file, so the regex does run against the shipped
page. The weakness is the pattern, not the source.)*

**Fix direction**: anchor the whole ternary, e.g.
`re.search(r"""\$\{t\.state_may_be_stale \? '<p class="stale-badge">""", html)`.
One line changed. N5 and N6 both die.

### WARNING 3 — "every skip/drop log site is pinned" is still not true: the fourth site is unpinned

**Where**: `src/feeder_station/adapters/store.py:556`
(`logger.exception("rehydrate: could not read the node roster")`).

The two sites the batch targeted are now genuinely pinned — M10 and M11 both die,
against `tests/adapters/test_store.py:604` and `tests/adapters/test_ingest.py:285`,
both well-built (they drive the *second* `try/except` and the *`PayloadError`*
branch specifically, not a message-text coincidence).

But `store.py` has **three** `logger.exception` sites, not two, and the stated rule
at `apply-progress-batch2c.md:103` is *"every skip/drop log site is pinned, not
only the two the earlier fixture happened to drive."* Enumerated exhaustively:

| Site | Pinned by |
|---|---|
| `store.py:572` state-race loop | `test_a_skipped_row_is_logged_at_error_with_the_node_id_and_a_traceback` |
| `store.py:626` `_rehydrate_desired_command` | `test_a_skipped_desired_command_row_is_logged_at_error_…` (new) |
| **`store.py:556` roster failure** | **nothing** |
| `ingest.py:100` drop log (3 exception types) | two tests, `FrameError` + `PayloadError` (new) |

**Measured (N7)**: replacing `:556` with `logger.debug("rehydrate: roster
unreadable")` — dropping the level *and* the traceback — **passes all 757**. A
station whose roster query dies would log nothing an operator could find, and the
`sqlite3` exception that explains *why* would be discarded.

Operationally this is the mildest of the four findings — `roster_lost: true` and
`ok: false` still surface the condition independently, so the station is not
silent, only undiagnosable. It is rated WARNING because the *claim* is the thing
that is false, for the third round running on this same finding. One `caplog`
assertion on the existing `test_boot_survives_a_broken_node_roster_query` closes it.

### WARNING 4 — `roster_incomplete` measures a difference that is partly by design, and can read `ok: false` forever with no other signal

**Where**: `src/feeder_station/adapters/store.py:647-655` (`_known_node_ids`);
`:657-670` (`_count_all_known_nodes`); `src/feeder_station/app.py:367-372`.

**First, the good news — the false alarm the brief predicted does not happen.**
A node legitimately absent because its only row was honestly skipped is *not*
flagged. Measured, 20 nodes, node 13's only row a corrupt HEARTBEAT:

```
ok=True  roster_lost=False  rehydration_errors=1  expected=20  rehydrated=20  traps=19 of 20
```

Correct: the node *is* in the roster (a node-sourced row always upserts
`node_cursor` in the same transaction), it *was* visited, its failure *was*
counted, and `ok` correctly stays true. The healthy control and the empty store
both read clean. The measured WARNING 4 case is genuinely fixed and pinned
(MR1, N10, N11, N13 all die).

**The problem is what the two queries actually are.** `_known_node_ids()` unions
`node_cursor` with `source = 'operator'`. `_count_all_known_nodes()` counts
`DISTINCT node_id` across **all** of `event`. `Source` is
`Literal["node", "operator", "station"]` (`domain/events.py:28`), and
`adapters/clock.py:238-248` writes `STATION_TIME_SYNC_WITHHELD` rows that name a
`node_id`. **The two queries differ by exactly the `source = 'station'` set, by
design — not by corruption.** Measured:

```
node 1 healthy, node 5 with only a station-sourced row:
  ok=False  roster_lost=False  rehydration_errors=0  expected=2  rehydrated=1  traps=[1]
```

`ok: false`, permanently, with every other field reading clean and nothing in the
body or the log naming the cause. A restart does not fix it — `docs/deploy.md`'s
new paragraph correctly says so, which means the runbook's own advice is to leave
the station showing `ok: false` indefinitely.

The code comment at `:661-666` asserts the opposite: *"this can only ever under- or
over-count relative to `_known_node_ids` if that narrower union itself missed
something -- a `node_cursor` row lost to a partial write, for instance."* **That is
false**: the union also misses every station-sourced row, always, deliberately.
verify-report-batch2.md's SUGGESTION 2 documented this gap and it was not carried
forward; this batch built a consistency check on top of it without widening the
union to match.

**Reachability today: none, and only by an invariant nothing enforces.**
`attempt_time_sync` is called only from `workers.py:246`, iterating
`registry.traps()`; every registry entry traces back to a `node_cursor` row
(ingest appends before `_apply_to_registry`, `ingest.py:113` before `:126`) or an
operator row (`_record_operator_event` runs before every `command_*` call,
`app.py:395-398`, `:405-408`, `:449-461`). So a station-sourced-only node cannot
exist in production **today**. The invariant is real, undocumented, and untested —
and the cost of breaking it is now a permanent, unexplained `ok: false` rather than
the previous round's merely-cosmetic docstring inaccuracy.

Note also (mutation N16): widening the roster to `SELECT DISTINCT node_id FROM
event` — the simplest fix, and the one that would close this — is now **killed by
`test_a_partial_roster_says_so_instead_of_reading_healthy`**, because it makes the
two counts equal by construction. The new check structurally depends on the union
staying narrow. That is a design commitment worth stating out loud.

**Fix direction**: either widen the union to `source != 'node'` (and rewrite the
test to corrupt `node_cursor` rather than rely on the source gap), or subtract the
station-only node ids from `roster_nodes_expected`. Either way, correct the
`_count_all_known_nodes` docstring's claim.

---

## Mutation testing — 38 mutations, 30 killed, 8 survived

**Method gotcha honoured.** Every mutation was applied in the real package root:
`feeder_station.adapters.store.__file__`,
`feeder_station.app.__file__` and `feeder_station.domain.traps.__file__` were all
asserted to resolve under this repo's `src/` before the first run — never a
`git worktree`, which this venv's editable-install `sys.meta_path` finder would
silently ignore and report a false green. Each mutation was restored immediately
and `git status --porcelain` asserted equal to the two expected untracked files
**after every single one**, programmatically, inside a `finally`. The per-commit
greenness sweep used the same real tree. Final tree: clean.

### Killed (30)

| # | Mutation | Killed by |
|---|---|---|
| M9 | roster → `source='operator' AND msg_type=256` (ARM) *(implementer's)* | parametrize `[disarm]`, `[set_targets]` — **reproduced** |
| M28 | roster → `msg_type=258` (SET_TARGETS) *(implementer's)* | parametrize `[arm]`, `[disarm]` — **reproduced** |
| M8 | `max(facts, key=seq)` → `key=at` *(implementer's)* | the ordering test — **reproduced** |
| M10 | downgrade `_rehydrate_desired_command`'s skip log *(implementer's)* | the new store `caplog` test — **reproduced** |
| M11 | ingest: `FrameError` at WARNING, rest at DEBUG *(implementer's)* | the new `PayloadError` test — **reproduced** |
| M15 | `view()` emits `state_may_be_stale: False` always *(implementer's)* | the new `/api/traps` test — **reproduced** |
| M26 | `a_state_row_was_unreadable = True` always *(implementer's)* | the same — **reproduced** |
| MB1 | badge emitted unconditionally *(implementer's)* | the structural test — **reproduced** |
| MR1 | `roster_incomplete = False` *(implementer's)* | the partial-roster test — **reproduced** |
| **N1** | `_reported_state`: `KeyError` → `ReportedState(armed=False)` | parametrize `[disarm]` |
| **N3** | `_reported_state`: full pre-batch revert | parametrize `[disarm]` |
| **N4** | drop the `reported is not None` guard, coerce to a fabricated fact | parametrize `[disarm]` |
| **N8** | `_rehydrate_heartbeat`: `raise` → `return None` | `test_a_heartbeats_non_int_state_counts_as_unreadable_not_silent` |
| **N9** | `_rehydrate_capture`: `raise` → `return None` | 2 tests, incl. the shifted `test_cli` one |
| **N10** | `roster_ok` drops the `roster_incomplete` term | the partial-roster test |
| **N11** | `roster_nodes_expected = len(_known_node_ids())` (self-fulfilling) | the partial-roster test |
| **N13** | `/health` drops both new roster count fields | the partial-roster test |
| **N15** | roster → `msg_type IN (ARM, DISARM)` | parametrize `[set_targets]` |
| **N16** | roster → `SELECT DISTINCT node_id FROM event` | the partial-roster test |
| M12 | `Registry.captured()` hardcodes `FIRED` | the 4-case `state_after` table |
| M13 | `_rehydrate_capture` hardcodes `FIRED` | the same |
| M14 | `mark_state_possibly_stale` never called | the new `/api/traps` test |
| M16 | `heartbeat()` stops clearing the stale flag | `test_a_fresh_live_report_clears_the_possibly_stale_flag` |
| M19 | state-step `except Exception` → `except ValueError` | 3 tests — **Batch 1 totality intact** |
| M20 | roster query moved back outside the guard | `test_boot_survives_a_broken_node_roster_query` |
| M21 | `rehydration_errors` not reset per pass | `test_rehydration_errors_resets_each_call_not_accumulating` |
| M22a | `payloads._VALID_STATE_VALUES` → `range(5)` | wire-byte-4 tests (HEARTBEAT + CAPTURED) |
| M22b | the state-range check removed entirely | the same |
| M23 | `view()`: `None` state → `"idle"` | 2 tests |
| M27 | `/health` drops `roster_lost` | the partial-roster test |

### Survived (8)

| # | Mutation | Meaning |
|---|---|---|
| **N2** | `_reported_state`: `state is None` → `ReportedState(armed=False, …)` | **CRITICAL 1.** Reachable with zero corruption. |
| **N5** | badge inverted (`!t.state_may_be_stale ?`) | **WARNING 2.** The signal ships exactly backwards. |
| **N6** | badge ternary with both branches empty | **WARNING 2.** The badge can be deleted. |
| **N7** | roster-failure log → `logger.debug`, traceback dropped | **WARNING 3.** Fourth log site. |
| **N14** | roster excludes `OPERATOR_CAPTURE_ACKNOWLEDGED` | **SUGGESTION 2** — equivalent mutant, see below. |
| **N12** | the two roster counts not reset at the top of `rehydrate()` | **SUGGESTION 1** — same shape as last round's M18. |
| **M19b** | outer roster guard `except Exception` → `except sqlite3.Error` | **SUGGESTION 3** — pre-existing, breadth unpinned. |
| **M22** | `store._WIRE_STATE_ORDER` widened to 5 entries | **SUGGESTION 4** — the payload-layer guard (M22a/b) is the one that matters and it holds. |

---

## Verdict per claimed closure

### 1. CRITICAL 1 — the roster rule, parametrized — **CLOSED**

Both named mutations die (M9, M28), and so does the narrower N15. The parametrize
reads its cases from `_OPERATOR_COMMAND_MSG_TYPES` rather than assuming them, and
uses two real `build_app` boots.

**The fourth operator-sourced row type, as asked.** Reading every `msg_type` an
operator action can write (`app.py:392-467`, four routes) against
`domain/events.py:34-37`: `OPERATOR_ARM` `0x100`, `OPERATOR_DISARM` `0x101`,
`OPERATOR_SET_TARGETS` `0x102`, **`OPERATOR_CAPTURE_ACKNOWLEDGED` `0x103`**. The
parametrize covers three. N14 (roster restricted to the three) **survives** — but
it is an **equivalent mutant**, not a proof gap that matters. Verified: an
acknowledge-only node produces no registry entry either way. Measured —
`POST /api/traps/99/acknowledge` on a never-heard node writes its durable row and
*then* raises `KeyError` (HTTP 500, `traps.py:281`), so such a node really can
exist in the log:

```
rows for node 99: [('operator', 259)]
boot2: ok=True  expected=1  rehydrated=1  traps=[]
```

Nothing in `_rehydrate_desired_command` or `_rehydrate_capture` reads an ACK row on
its own, so including or excluding it changes nothing observable. Recorded as
SUGGESTION 2, not a finding.

### 2. WARNING 5 — the ordering test's teeth — **CLOSED**

M8 dies: the FAULT's `received_epoch` moved `2060.0 → 1000.0`, below the
HEARTBEAT's `2000.0`, so `key=seq` (FAULT) and `key=at` (ARMED) now disagree, and
the new `trap.last_seen == fromtimestamp(1000.0)` assertion pins the winner's
timestamp too.

**The `node_epoch` axis is still guarded**: `tests/adapters/test_store.py:400,414`
keep `node_epoch=1_000_000` on the HEARTBEAT and `999_520` on the FAULT, so a
future switch to the node's own clock still picks ARMED and still fails.

**Physically coherent**: a station whose wall clock (`Clock.now()` →
`system_now()`) is stepped backward by chrony between two appends produces exactly
this row pair, while `seq` — an `INTEGER PRIMARY KEY AUTOINCREMENT` — cannot
regress. The docstring's derivation is correct on every fact I checked. One nit in
SUGGESTION 6.

### 3. WARNING 1 — an unreadable field counts as unreadable — **PARTIALLY CLOSED**

The two named guards raise, are counted, are flagged, and are pinned in both
directions (N8, N9 die). **The new `raise`s cannot escape the guard**: both live in
`_rehydrate_heartbeat`/`_rehydrate_capture`, whose only callers are the
`state_step` tuple inside `rehydrate()`'s own `try` (`store.py:564-575`, verified
by grep — no other call site in `src/`). M19 still dies, so that guard is still
proven broad. **CRITICAL 2 is not reopened.** Three residual silent paths —
WARNING 1 above.

### 4. WARNING 2 — the stale badge — **PARTIALLY CLOSED**

API layer: genuinely closed, both directions, through a real boot. UI layer: the
badge exists and is keyed on the flag in the shipped page, but its test constrains
neither polarity nor content — WARNING 2 above.

**Judged as an operator would**, as asked. The badge is real and its Spanish is
clear, but it is the *quietest* thing on the card:

```
.state            { font-weight: 700; text-transform: uppercase; }   <- "Trampa 7 · ARMED"
.trap.no-contact  { border-color: #a86a00; }                          <- card border changes
.no-contact-badge { color: #ffb648; font-weight: 700; }               <- bold amber
.stale-badge      { color: #ffcf7a; font-style: italic; font-size: .85rem; }   <- not bold, smaller
```

The state chip itself still asserts **ARMED** in bold uppercase with full
confidence, and the card gets no border treatment. Compare WARNING J's own fix,
where an unknown state changes *the chip* to `DESCONOCIDO`. Here the doubt lives in
a footnote under the claim, not in the claim. Worse, the two badges usually
co-occur: after a restart `reachable` is computed from the winning row's
`received_epoch` against a 5-minute default silence window, so any station down
longer than that renders `⚠ SIN CONTACTO` in bold directly above the italic stale
line — including in the batch's own test fixture, whose `received_epoch=1000.0` is
1970. **At a glance it reads as a second-line qualifier on an already-alarmed
card, not as doubt about the state itself.** SUGGESTION 7.

### 5. WARNING 3 — the log sites — **PARTIALLY CLOSED**

M10 and M11 die against two well-targeted new tests. Full enumeration of every
skip/drop log site in `store.py` and `ingest.py` above: 3 of 4 pinned — WARNING 3.

### 6. WARNING 4 — the partial roster — **CLOSED, with a new latent false alarm**

The measured case is fixed and pinned four ways. The honest-skip false alarm the
brief flagged **does not occur**. A different one is now latent — WARNING 4 above.

### 7. SUGGESTION 1 — `docs/deploy.md` — **CLOSED**

Both sentences (`docs/deploy.md:78-84`) are accurate. `/health` re-measured as
**HTTP 200** across five scenarios including `ok: false`, `roster_lost: true` and
`roster_incomplete`. The restart-fixability claim matches the code
(`app.py:301, 331, 372`). The wording correctly names
`roster_nodes_rehydrated` below `roster_nodes_expected` as *not* restart-fixable.

---

## The shifted test (question 8) — **verified, and 2 is right**

`tests/test_cli.py:742` `test_a_node_with_no_readable_state_source_shows_unknown_not_idle`:
`rehydration_errors` `1 → 2`. Re-derived from the fixture, not the record: node 7
has exactly **two** rows, and after WARNING 1 each raises exactly once, in a
different `state_step`:

- `HEARTBEAT` `fields={"state": 7, …}` → `isinstance` passes, `trap_state_from_wire(7)`
  raises `ValueError` (`store.py:240`) — counted once, as before this batch.
- `CAPTURED` `fields={}` → `state_after` is `None`, `isinstance` fails, raises
  `TypeError` (`store.py:750`) — **newly** counted once.

Two distinct rows, one count each. **No double counting**: the capture object is
restored *before* the raise (`store.py:737-743`), which is why the test's
`capture["tag"]` assertion still holds, and `_rehydrate_capture` is invoked once
per node. `facts` ends empty, so `restore_reported_state` and
`mark_state_possibly_stale` are both correctly skipped and `state` stays `None`.

**`git diff 1c59681..HEAD -- tests/` — every removed and modified line examined
individually.** 588 diff lines, **24 removed**, all accounted for:

- 1 import line (`OPERATOR_CAPTURE_ACKNOWLEDGED` → `OPERATOR_ARM, OPERATOR_CAPTURE_ACKNOWLEDGED`)
- 10 docstring lines in the ordering test, replaced by the WARNING-5 correction
- 4 comment lines on the two fixture/assertion edits in the ordering test
- 1 fixture line (`received_epoch=2060.0` → `1000.0`)
- 5 lines in the never-heard-node test: the `def` signature, 2 docstring lines,
  the inline `arm_response` two-liner, 2 assertion lines that became
  `expected_pending`, and the failure message text
- **1 assertion value: `health["rehydration_errors"] == 1` → `== 2`**

**That is the only pre-existing assertion whose value changed**, exactly as the
record claims. No test was deleted, no assertion was weakened, no `assert` became a
comment. The change is an *addition to the count*, correctly characterised in the
record this time (contrast the batch2b "strictly stricter" inversion the record
itself corrects).

---

## `ok`'s fourth condition (question 10)

| Condition | Field | Restart fixes it? |
|---|---|---|
| A started worker thread is dead | `workers.*.alive: false` | **Usually yes** — the only one. |
| A configured radio link is down | `link.connected: false` | **No.** The gateway already reconnects on its own backoff. |
| The roster query (or the new count query) raised | `roster_lost: true` | **No.** The store is unchanged by a restart. |
| **NEW — the roster came back short** | `roster_nodes_rehydrated < roster_nodes_expected` | **No.** Permanent until the store is repaired, and per WARNING 4 it can also fire on a by-design source gap with no field naming the cause. |

Three of four are not restart-fixable. This is the pre-existing hazard widened by
one, not a new class — and `docs/deploy.md` now says so in the only place a human
is told to read `ok`, which is the right mitigation.

**Nothing in this repo consumes `ok` — still true after this diff.** Re-checked
exhaustively: `deploy/feeder-station.service` has `Restart=always`/`RestartSec=2`
and **no `WatchdogSec`, no `ExecStartPost`, no health poll**; there is no
Dockerfile, no `HEALTHCHECK`, no compose file, no `.github/`, no script;
`static/index.html` fetches `/health` and reads **only** `clock_trust` (line 80)
and `link` (line 92) — it reads neither `ok` nor `roster_lost` nor either new
count (`response.ok` at line 190 is the `fetch` Response's own property).
`/health` still returns **200** in every degraded state measured. The crash-loop
hazard remains hypothetical.

---

## Regressions (question 11) — none

| Property | Probe | Status |
|---|---|---|
| `rehydrate()` totality (`except Exception`, per row-source) | M19 killed by 3 tests | intact — and the two new `raise`s are **inside** it |
| Roster query inside the guard | M20 killed | intact |
| `rehydration_errors` reset per pass | M21 killed | intact |
| `_VALID_STATE_VALUES == range(len(TrapState))`, wire byte 4 rejected | M22a, M22b both killed on HEARTBEAT and CAPTURED | intact — nullable `state` has not widened the range |
| Nullable `state` honest through every consumer | M23 killed | intact — `/api/traps` `state: null`, `index.html:132` → `desconocido` |
| CRITICAL A, live-equals-rehydrated for all four `state_after` | M12, M13 killed by the 4-case table; all 4 cases collected and green | intact |
| The five-ordering property test | 5 cases collected and green | intact |
| Batch 1 / 2a / 2b behaviour | no test removed, no assertion weakened (diff audited above) | intact |

---

## SUGGESTION

1. **Pin the two new roster counts' per-pass reset.** N12 survives: deleting
   `roster_nodes_expected = 0` / `roster_nodes_rehydrated = 0` from the top of
   `rehydrate()` (`store.py:550-551`) passes all 757. Identical in shape to last
   round's M18 for `roster_lost`, which is also still unpinned.
   `test_rehydration_errors_resets_each_call_not_accumulating` is the obvious home
   for all three — three extra assertions.
2. **`_known_node_ids`' docstring and the roster rule still overstate.** The rule
   as written everywhere is "every node with *any* operator-sourced row"; the
   parametrize proves three of four `msg_type`s and N14 shows the fourth
   (`OPERATOR_CAPTURE_ACKNOWLEDGED`) is uncovered. It is an equivalent mutant today
   — an ACK-only node rehydrates to nothing either way (measured) — so this is a
   wording fix, not a test. Say "every node with a *command*-bearing operator row",
   or add the fourth case for symmetry.
3. **The outer roster guard's breadth is unpinned.** M19b: narrowing
   `store.py:555` from `except Exception` to `except sqlite3.Error` passes all 757,
   because the only test that drives it drops a table (an `OperationalError`).
   Pre-existing, inherited from Batch 2b, and the same class CRITICAL A's own
   `OverflowError`/`AttributeError` discovery closed for the inner guard.
4. **`store._WIRE_STATE_ORDER`'s length is unpinned.** M22 (a fifth entry)
   survives; `test_trap_state_from_wire_maps_every_valid_byte_to_the_right_state`
   iterates `range(len(TrapState))` and so widens with the mutation. The guard that
   matters (`payloads._VALID_STATE_VALUES`) is solid, so this is low value — one
   `pytest.raises(ValueError): trap_state_from_wire(4)` closes it.
5. **The widened pending-command population meets a pre-existing gap, and Batch 3
   owns the consequence.** `captured()` and `fault()` never run `satisfied_by` —
   only `heartbeat()` does (`traps.py:132-137`, measured table above). This diff
   correctly keeps more commands pending across a restart, so a trap that is in
   FAULT and sends only FAULTs now resends its command on the capped backoff
   indefinitely. Unchanged behaviour, increased exposure, and it lands squarely in
   Batch 3's frame-budget scope (CRITICAL 3/4). Worth one line in the Batch 3 brief.
6. **The ordering fixture's `clock_trust="rtc"` slightly undercuts its own story.**
   Both rows carry `clock_trust="rtc"` while the docstring's mechanism is the
   *unsynced-at-boot* clock (`ClockTrust.UNSET`, `clock_trust.py:38`). The field is
   never read by the race so the test is valid either way, but `"unset"` on the
   first row would make the fixture's narrative airtight. One string.
7. **The stale badge's visual weight.** See the WARNING 2 section. The chip still
   reads bold `ARMED`; the doubt is a smaller, non-bold, italic line that usually
   sits under a bolder `⚠ SIN CONTACTO`. Consider marking the chip itself
   (`ARMED (?)`, or a `.trap.stale` border treatment) so the doubt attaches to the
   claim rather than following it.
8. **`POST /acknowledge` on an unknown node writes a durable row, then 500s.**
   `_record_operator_event` runs before `registry.acknowledge`, which raises
   `KeyError` (`traps.py:281`) for a node with no entry. Measured: HTTP 500 with the
   `OPERATOR_CAPTURE_ACKNOWLEDGED` row already committed. Pre-existing and harmless
   (the row rehydrates to nothing), but it is how an ACK-only node can exist at all,
   and it is the same "durable side effect, failed request" shape S9 is careful
   about everywhere else.

Also noted and **not** re-litigated, since the record already discloses it: the
batch ran **684 lines against a ~350 target, six for six.** The apply record's own
diagnosis is right — the lever is scope, not prose. Seven findings plus five record
corrections in one slice cannot fit a 350-line budget when every finding requires a
mutation-killing test and a citation trail. This is the second consecutive report
asking for that design-time conversation before Batch 3's forecast is trusted.

---

## Spec compliance movement (Batch 2c scope only)

| Capability | Scenario | Before | After | Evidence |
|---|---|---|---|---|
| event-store | rebuild without depending on the node re-sending | ⚠️ PARTIAL | ✅ **MET** | Roster proven for all three command-bearing operator types (M9, M28, N15 all die) |
| trap-commands | resend continues until version confirmed, across a restart | ⚠️ PARTIAL | ⚠️ **PARTIAL** | Correct for all three kinds (truth table measured); the `state is None` branch untested — CRITICAL 1 |
| trap-commands | displayed state is the trap's own most recent report (S4) | ⚠️ PARTIAL | ✅ **MET** | `key=seq` now pinned against both `key=at` and `key=node_epoch` |
| event-store | an unreadable row is counted, never a silent all-clear | ⚠️ PARTIAL | ⚠️ **PARTIAL** | Two guards closed and pinned; three silent paths remain — WARNING 1 |
| operator-ui | an unreachable / unknown / doubtful trap is marked, never shown as calm | ⚠️ PARTIAL | ⚠️ **PARTIAL** | The badge ships and the API is pinned both ways; its polarity is not — WARNING 2 |
| station-deployment | `/health` distinguishes a degraded boot from a healthy one | ⚠️ PARTIAL | ⚠️ **PARTIAL** | The truncated roster is now visible; a by-design source gap can also trip it — WARNING 4 |

---

## Final verdict

**FAIL** — 1 CRITICAL, 4 WARNING, 8 SUGGESTION.

What is genuinely good here, said plainly. **The `_reported_state` discovery is the
best work on this change so far.** It was found *because* the implementer closed
CRITICAL 1 properly instead of patching around it, it is a real defect on a real
operator path, the fix is the correct one (withhold the check rather than fabricate
a fact — S4 applied to the absence of a report, not just to its content), and it is
right for all three command kinds, verified exhaustively, with the live path
verified free of any equivalent fabrication. WARNING 5's fixture now has real teeth
and keeps the axis it already guarded. WARNING 3's two new tests drive the exact
branches their predecessors missed rather than the message text. WARNING 2's
`/api/traps` test kills both directions through a real boot with a genuinely stale
node and a genuinely healthy one in the same response. WARNING 4's measured case is
closed four ways and does **not** false-alarm on the honest-skip case the brief
worried about. Every commit is independently green on all three gates, counts climb
monotonically, format drift is provably zero, the five record corrections are pure
additions with zero removed lines and every one of them is accurate, and the
`git checkout --` incident left nothing behind.

And the diagnosis recurs, for the sixth time, in the same two shapes:

- **CRITICAL 1** — the batch's own headline fix states a two-branch rule and proves
  one branch. The unproven branch is *more* reachable than the proven one: two
  operator clicks, no corruption, and a pending `DISARM` vanishes across a restart.
- **WARNING 1 and WARNING 2** — the previous report asked, in as many words, that
  these two be carried "as a single item ... rather than letting them close
  narrower than the hole a third time." Both closed narrower than the hole a third
  time. WARNING 1's rule is "raising or returning `None`"; three more `return None`
  paths in the same 90 lines are still silent. WARNING 2's rule is "neither
  'always false' nor 'always true' can ship silently"; the *inverted* badge ships
  silently.

In every case the production code is correct. The proof is not.

**May Batch 2 be pushed?** **Not as a complete close-out.** The remaining work is
about ten test lines and belongs in this stack, not a seventh round:

1. CRITICAL 1 — extend the `set_targets` parametrize case with a following
   `POST /disarm` against the same never-heard node (~4 lines). N2 dies.
2. WARNING 2 — anchor the badge regex to the full ternary including the markup
   (~1 line). N5 and N6 die.
3. WARNING 3 — one `caplog` assertion on the existing
   `test_boot_survives_a_broken_node_roster_query` (~3 lines). N7 dies.

WARNING 1's three residual paths and WARNING 4's `source = 'station'` gap are real
but need production changes, not test lines; they should be carried as one
explicitly-scoped item rather than folded into a fourth narrow close.

If the team pushes the seven commits as they stand, they must land with:
**task 2.6** recording CRITICAL 1's `_reported_state` fix as *code correct, one of
two branches proven*; **task 2.9**'s WARNING-C half recording the UI pin as
*present but polarity-unconstrained*; and **task 2.10** recording *3 of 4 log
sites*. Do not tick any of the three clean.

**May Batch 3 start?** **Yes, now.** Batch 3 is CRITICAL 3 + CRITICAL 4 — the
station-wide frame budget in `adapters/workers.py`, `adapters/scheduler.py` and
`domain/backlog.py`. It touches none of `_known_node_ids`, `_reported_state`,
`/health`'s roster fields, `rehydrate()`'s state race, `domain/traps.py` or
`static/index.html`. Carry SUGGESTION 5 into its brief: this batch legitimately
increased the number of commands that survive a restart still pending, and a trap
reporting only `FAULT` will now resend one indefinitely — that is frame budget,
and Batch 3 owns it.
