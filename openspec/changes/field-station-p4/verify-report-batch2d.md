# Verification Report — field-station-p4, Batch 2d (claimed Batch 2 close-out)

**Scope**: `0374341..HEAD` — 7 commits (`f6591cd..1bd6f10`), 472 changed lines of
code/tests/records (777 total, 305 of which are `apply-progress-batch2d.md` itself).
Closes `verify-report-batch2c.md`'s CRITICAL 1, WARNING 1-4 and SUGGESTION 2, plus
three record corrections.
**Artifact store**: openspec. **Mode**: Strict TDD.
**Out of scope, deliberately not reported**: CRITICAL 3, 4, 5, I (Batches 3-5);
SUGGESTION 5 (`captured()`/`fault()` never run `satisfied_by`) except where a
*different* symptom of the same root cause lands inside Batch 2's own code.

**Verdict: FAIL** — 4 BEHAVIOUR defects, 5 PROOF gaps, 2 record inaccuracies.

Everything below was re-derived from the repository. **46 mutations were run in the
real package root** (`feeder_station.adapters.store.__file__` asserted under this
repo's `src/` before the first one — never a `git worktree`), each restored inside a
`finally` with `git status --porcelain` asserted back to baseline after **every
single one**. 37 killed, 9 survived. Final tree clean.

**The headline, stated plainly and up front.** Batch 2d's six claimed closures are
real work and four of them are genuinely, exhaustively closed. But this round is
*not* another "code right, proof missing" round. **Four BEHAVIOUR defects remain
inside Batch 2's own scope**, three of them in the exact `rehydrate()` walk the
`_unreadable()` unification claimed to have finished, and one of them makes the
station render a confident, calm `idle` for a trap that never reported it. Three of
the four cost about ten production lines, and I measured that fixing all three
together keeps all 763 tests green.

---

## Executive verdicts

| Question | Verdict |
|---|---|
| **CRITICAL 1 (`_reported_state`'s second branch)** | **CLOSED.** N2 dies. The four cases cover **the rule, not four examples** — see Q1. |
| **WARNING 1 (the `_unreadable()` unification)** | **PARTIALLY CLOSED.** 5 of 8 not-readable expressions route through it. CRITICAL 2 is **not** reopened. Three residual silent paths + one silent fabrication — BEHAVIOUR 1/2/3. |
| **WARNING 2 (the badge pin)** | **PARTIALLY CLOSED.** N5 and N6 die; the `/api/traps` layer is pinned both ways. Three new rendering mutations ship green — PROOF 1. |
| **WARNING 3 (log sites)** | **CLOSED, and exhaustively.** Enumeration re-derived across all of `src/`: exactly 4 sites, exactly as named. All four die. |
| **WARNING 4 (roster counts)** | **CLOSED. No false alarm introduced** — six scenarios measured, correct in all six. |
| **SUGGESTION 2 (ACK-only node)** | **CLOSED.** Covered by a real two-boot test. |
| **Regressions (Batches 1/2a/2b/2c)** | **None.** 22 regression mutations, all killed. |
| **Records (2.6, 2.9, 2.10)** | 2.6 and 2.10 accurate; 2.10 is the **first genuinely exhaustive** enumeration in this change's history. 2.9 and one in-code comment are not — see Q7. |
| **Are there BEHAVIOUR defects left in Batch 2's scope?** | **Yes — four.** Named below with input → wrong outcome. |
| **May Batch 2 be pushed?** | **Not as a complete close-out.** The seven commits are sound; the claim of completeness is false. |
| **May Batch 3 start?** | **Yes, now, in parallel.** No file overlap. Carry BEHAVIOUR 4 into its brief. |

---

## Gates (each exit status checked directly — nothing piped into `head`/`tail`)

| Gate | Exit | Result |
|---|---|---|
| `uv run pytest` | **0** | 763 passed (757 baseline + 6 new) |
| `uv run ruff check .` | **0** | All checks passed |
| `uv run ruff format --check .` | **1** | 6 files — **file list identical to a real `504d18e` checkout**, compared programmatically. **Zero new drift.** |
| `uv run mypy src` | **0** | no issues in 22 source files |

Baseline verified by `git archive 504d18e` into a scratch tree, running this repo's
own `ruff` against it, and diffing the two parsed file lists: identical
(`design.md`, `payloads.py`, `test_ingest.py`, `test_serial_gateway.py`,
`test_events.py`, `test_traps.py`). None of the five files this batch touched
appears in either list.

### Per-commit greenness — bisect-safe

`src` + `tests` checked out at each sha **in the real tree** (never a worktree),
full suite + `ruff check` + `mypy src` at each, tree restored inside a `finally`:

```
f6591cd  pytest=0 (759 passed)  ruff=0  mypy=0   CRITICAL 1, the DISARM branch
9ae7b51  pytest=0 (759 passed)  ruff=0  mypy=0   WARNING 2, badge + CSS
dd1b9a8  pytest=0 (759 passed)  ruff=0  mypy=0   WARNING 3, the fourth log site
81b4aab  pytest=0 (762 passed)  ruff=0  mypy=0   WARNING 1, _unreadable
4271864  pytest=0 (763 passed)  ruff=0  mypy=0   WARNING 4, the source gap
d904fff  pytest=0 (763 passed)  ruff=0  mypy=0   records
1bd6f10  pytest=0 (763 passed)  ruff=0  mypy=0   apply record
```

Monotonic (759, 759, 759, 762, 763, 763, 763), every commit independently green on
all three gates. The `+6 new tests` arithmetic checks out exactly: 2 in `f6591cd`
(the parametrize case + the ACK test), 0 in `9ae7b51`/`dd1b9a8` (assertions added
to existing tests), 3 in `81b4aab`, 1 in `4271864`. The apply record's per-commit
composition table (99 production / 331 tests / 42 docs = 472) was re-derived from
`git diff --numstat` per commit and is **accurate to the line**.

---

# BUCKET A — BEHAVIOUR

*The station does the wrong thing for a reachable input. These block the claim that
Batch 2 is a complete close-out.*

All four were measured through real `build_app` boots and `/health` + `/api/traps`,
not argued. **None of them was introduced by Batch 2d** — `git diff -U0
0374341..HEAD -- src/.../store.py` shows the batch touched `_known_node_ids`,
`_count_all_known_nodes`, the five `_unreadable` call sites and the helper itself,
and nothing else. All four are pre-existing holes this batch's own stated rule
claims to have closed.

---

### BEHAVIOUR 1 — an unreadable target list is still swallowed silently, at the one `_tags_from_fields` call site `_unreadable()` did not reach

**Where**: `src/feeder_station/adapters/store.py:813-815`

```python
            tags = _tags_from_fields(latest_targets.fields)
            if tags is not None:                      # <- no `else`, no raise, no count
                registry.restore_desired_tags(node_id, tags)
```

`_rehydrate_desired_command` reads `tags` from `OPERATOR_SET_TARGETS` at **two**
sites. The batch routed the second (`:857-864`, the *winning command's* tags —
"case F") through `_unreadable()`. It did not touch the first.

**Input**: an operator issues `PUT /api/traps/77/targets` and then presses
**Armar** (`POST /api/traps/77/arm`). The `SET_TARGETS` row's `fields.tags` is later
unreadable in the store — the same corruption class as case D's NULL tag and case
E's non-int version, which this batch accepted as real.

**Wrong outcome** — measured through two real `build_app` boots:

```
boot 1 (healthy)   node=77 pending='arm'  targets=['0384199C87EC06']
boot 2 (corrupt)   node=77 pending='arm'  targets=[]
                   /health  ok=True  rehydration_errors=0  roster_lost=False
```

The page reads **`Objetivos actuales: ninguno`**. A trap configured to hunt a
specific tag now reads as configured to hunt none — verbatim the wrong outcome the
batch's own case F test exists to forbid — and `/health` reads perfectly healthy.

**The proof that this is the same defect, not a different one**: remove the later
`ARM` and the *identical* corruption is counted, because then the corrupt row wins
the command scan and reaches the guarded call site:

```
A' same corruption, no later ARM   /health ok=True  rehydration_errors=1   (node 77 correctly absent)
A  same corruption, ARM supersedes /health ok=True  rehydration_errors=0   (targets=[] shipped)
```

One corrupt row, two answers, decided by whether an unrelated later click happened.

**Mutation W-dt**: routing `:813-815` through `_unreadable()` — the fix — passes
**all 763 tests**. Nothing pins the current behaviour either way.

**Spec**: `event-store/spec.md`, "Registry rehydration at boot" — rebuild trap
state "without depending on the node re-sending anything". Here it depends on the
*operator* re-entering the list, with nothing telling them to.

---

### BEHAVIOUR 2 — a negative stored state byte is not rejected; it indexes backwards into `_WIRE_STATE_ORDER` and fabricates a confident state, including the calm `idle`

**Where**: `src/feeder_station/adapters/store.py:236-240`

```python
def trap_state_from_wire(value: int) -> TrapState:
    try:
        return _WIRE_STATE_ORDER[value]          # <- Python negative indexing
    except IndexError as exc:
        raise ValueError(f"HEARTBEAT state byte {value} has no known TrapState") from exc
```

`_WIRE_STATE_ORDER` is a 4-tuple. `[-1]` is `FAULT`, `[-2]` `FIRED`, `[-3]` `ARMED`,
**`[-4]` `IDLE`**. Only `<= -5` raises. `store.py:713-721` checks that the stored
`state` **is an int** — not that it is a *valid* one — and then hands it straight
here. `_rehydrate_capture`'s `state_after` (`:775-782`) does the same.

The wire path is safe: `payloads._require_valid_state` (`payloads.py:84-88`)
rejects anything outside `range(len(TrapState))` before a frame is ever stored, and
`M22a` (widening it to `range(5)`) dies. So a negative value in the `fields` column
is, exactly like case D's NULL tag, **a corruption shape the decoder could never
itself produce** — the batch's own stated criterion for "not readable".

**Input**: a stored HEARTBEAT whose `fields.state` reads `-4` (the row really said
`3` = FAULT before the `fields` TEXT column was corrupted).

**Wrong outcome** — measured through a real boot:

```
state=-4   /api/traps node=7 state='idle'  state_may_be_stale=False  battery_mv=4000
           /health    ok=True  rehydration_errors=0
state=-1   /api/traps node=7 state='fault' state_may_be_stale=False
           /health    ok=True  rehydration_errors=0
```

The operator's card reads **`Trampa 7 · IDLE`** in bold uppercase, no stale badge,
no border treatment, `ok: true`, `rehydration_errors: 0` — for a trap whose only
report said FAULT. This is not "absence rendered honestly"; it is **a fabricated,
specific, calm fact**, which is strictly worse than the three cases the batch just
fixed and is precisely what WARNING J exists to forbid: *"a false all-clear on a
trap that may be holding an animal."*

**Mutation W-neg**: adding `if value < 0: raise IndexError(value)` — the fix —
passes **all 763 tests**. `test_trap_state_from_wire_maps_every_valid_byte_to_the_
right_state` iterates `range(len(TrapState))` and never goes negative.

**Spec**: `event-store/spec.md`, "Registry rehydration at boot";
`operator-ui/spec.md`, "Displayed state is reported state" (S4).

---

### BEHAVIOUR 3 — a corrupt `battery_mv` or `targets_version` is coerced away and never counted

**Where**: `src/feeder_station/adapters/store.py:728-734`

```python
        battery_mv = heartbeat.fields.get("battery_mv")
        targets_version = heartbeat.fields.get("targets_version")
        registry.restore_heartbeat_fields(
            node_id,
            battery_mv=battery_mv if isinstance(battery_mv, int) else None,
            targets_version=targets_version if isinstance(targets_version, int) else None,
        )
```

`decode_heartbeat` (`payloads.py:125-145`) **always** produces both as ints from
fixed byte offsets. A stored HEARTBEAT missing either, or holding a non-int, is
corruption — the same class as `state` two lines above it, which raises.

**Input**: a stored HEARTBEAT with `fields.battery_mv` and `fields.targets_version`
unreadable, `state` intact.

**Wrong outcome** — measured:

```
/api/traps node=7 state='armed' battery_mv=None targets_version=None state_may_be_stale=False
/health    ok=True  rehydration_errors=0
```

The card reads **`Batería: sin dato · Lista de objetivos: v—`**, byte-identical to
a node the station has never heard from, on a **battery-powered field system where
the battery reading is the operational signal**. `rehydration_errors` stays 0, so
nothing anywhere tells anyone a stored row was corrupt.

This is the mildest of the four — the *values* shown are honest "no data", not
fabrications. The wrong outcome is confined to the health signal: **a station whose
database is corrupting reads `ok: true, rehydration_errors: 0`**, which is the exact
silent all-clear `store.py:522-531`'s own "skip-and-count, never raise" contract and
`event-store/spec.md`'s rehydration requirement (which names `targets_version`
explicitly) forbid.

**Mutation W-bat**: changing `else None` to `else 0` — which would make the station
render **`Batería: 0 mV`**, a dead battery, for a corrupt field — passes **all 763
tests**. The guard's output is completely unconstrained.

---

### BEHAVIOUR 4 — rehydration runs `satisfied_by` where the live path does not, so a restart silently drops a pending command in 5 of 18 reachable combinations

**Where**: `src/feeder_station/adapters/store.py:838-848` + `:892-894`
(`_reported_state`) versus `src/feeder_station/domain/traps.py:174-189`
(`captured()`) and `:269-278` (`fault()`).

`Registry.heartbeat()` runs `satisfied_by` (`traps.py:132-137`). `captured()` and
`fault()` **do not**. But `_rehydrate_desired_command` runs it against whichever
row won the state race — including a CAPTURED or FAULT row. The same log therefore
gives two different answers depending on whether the station has restarted.

**Input**: an operator presses **Desarmar** on trap 7; the trap then reports FAULT
(or a CAPTURED whose `state_after` is not ARMED); the station restarts. **No
corruption of any kind.**

**Wrong outcome** — measured through two real `build_app` boots:

```
boot 1  node=7  state=None    pending='disarm'
  (a FAULT row arrives)
boot 2  node=7  state='fault' pending=None
        /health ok=True  rehydration_errors=0
```

The operator's disarm is gone. The page shows no pending command, `/health` reads
healthy, and the trap is never commanded again. Live, the same sequence keeps
`pending='disarm'` — measured directly on a `Registry`.

**Full matrix, measured** (live registry vs. a real rehydration of the same log):

```
pending  report row                     LIVE     REHYDRATED  diverges
disarm   FAULT (no state byte)          disarm   -           *** YES
disarm   CAPTURED(state_after=0->idle)  disarm   -           *** YES
disarm   CAPTURED(state_after=2->fired) disarm   -           *** YES
disarm   CAPTURED(state_after=3->fault) disarm   -           *** YES
arm      CAPTURED(state_after=1->armed) arm      -           *** YES
   (the other 13 of 18 agree)
```

Every divergence is in the same direction: **rehydration drops a command the live
path keeps pending.** `disarm` + `CAPTURED(fired)` is the sharpest: a trap has just
fired and may be holding an animal, the operator disarms it so nobody re-arms it,
the station power-cycles, and the instruction vanishes.

**Why the existing proof does not see it.** The brief asked me to re-check
"CRITICAL A's live-equals-rehydrated for all four `state_after` values". That test
(`tests/test_cli.py:362-406`) asserts **only** `state` — `assert live_row["state"]
== expected_state` and the same for the rehydrated row. The five-ordering property
test (`tests/test_cli.py:338`) compares exactly `("state", "last_seen",
"reachable")` and none of its five logs contains an operator row at all. So
"live-equals-rehydrated" is proven for 3 of the 13 fields `Registry.view()` emits,
on logs with no pending command. `M12` and `M13` both die, so the `state` half of
the property is genuinely intact — it is the command half that was never asserted.

**Root cause and routing, stated honestly.** This is the same asymmetry as
SUGGESTION 5 (`captured()`/`fault()` never run `satisfied_by`), which the caller
scoped to Batch 3. SUGGESTION 5 describes the *resend-forever* symptom; this is its
mirror — the *silent-drop* symptom — and it lives in `_reported_state` /
`_rehydrate_desired_command`, Batch 2's own code. The fix is a design decision
(should `captured()`/`fault()` check a pending command, or should rehydration stop
checking against rows the live path never checks?), not a line edit, and it belongs
next to task 3.3's `RESEND_BACKOFF_S` work. **I am reporting it, not re-litigating
SUGGESTION 5, and I recommend it be decided in Batch 3 alongside it.**

---

# BUCKET B — PROOF

*The station does the right thing; a mutation survives, so nothing stops a future
change from breaking it. To be recorded as PARTIAL and scheduled, not fixed now.*

### PROOF 1 — the anchored badge regex pins a string in a file, not a badge on a screen: three mutations ship green

**Where**: `tests/test_app.py:417-429`; `src/feeder_station/static/index.html:33`,
`:153`.

The fix is real as far as it goes. The regex

```python
    pattern = re.compile(
        r"""\$\{t\.state_may_be_stale \? '<p class="stale-badge">[^<]*</p>' : ""\}"""
    )
```

kills **N5** (inverted condition) and **N6** (both branches emptied), both
reproduced and both dying. The *data* layer is pinned both ways independently:
`B5` (`view()` emits `False` always) and `B6` (`True` always) both die against
`test_state_may_be_stale_is_true_only_for_the_node_that_earned_it`, and `B7`
(`mark_state_possibly_stale` never called) kills 5 tests. The regex also runs
against the genuinely shipped page — `tests/test_app.py:234-243` asserts `GET /`
is byte-identical to the file.

**Three mutations I invented, all SURVIVE all 763 tests:**

| # | Mutation | Effect on the operator |
|---|---|---|
| **B3** | `'<p class="stale-badge"></p>'` — ternary intact, badge emptied | An empty box. `[^<]*` matches the empty string. |
| **B4** | ternary hoisted verbatim into an unused `const staleBadge`, removed from `renderTrap`'s returned template | **The badge never renders at all.** The regex is position-blind: it matches the ternary *anywhere in the file*, including in dead code. |
| **CSS-hide** | `.stale-badge { display: none; }` | Markup in the DOM, invisible on screen. |

**Is testing rendered markup by regex load-bearing enough here?** **No.** The regex
now constrains the condition's polarity and the truthy branch's *tag and class* —
a genuine improvement over the bare substring — but it cannot constrain **content,
placement, or visibility**, which are the three things that decide whether a
biologist walking a line of traps sees the warning. B4 is the sharpest
demonstration: the exact string the test demands is present, and the badge is gone.
**This remains a structural gap, and it will not close by tightening the regex
further** — a fourth round of pattern-anchoring would kill B3 and still leave B4 and
CSS-hide. It closes by rendering `renderTrap` (a DOM harness, or moving the
decision behind a testable pure function) or by accepting it explicitly as
untestable at this layer and saying so in the record.

*(The `.stale-badge` CSS weight bump to `font-weight: 700` is correct and addresses
SUGGESTION 7's measured complaint. `CSS-del` also survives; that one I do **not**
count against the batch — the apply record explicitly and correctly declines to
assert CSS class values, which this project's own assertion-quality rules forbid.)*

### PROOF 2 — the two new roster counts are still not reset per pass

**Mutation R-reset**: deleting `self.roster_nodes_expected = 0` /
`self.roster_nodes_rehydrated = 0` from the top of `rehydrate()`
(`store.py:550-551`) passes all 763. Carried unchanged from
`verify-report-batch2c.md` SUGGESTION 1; `M21` (the same deletion for
`rehydration_errors`) dies, so the pattern exists — three assertions on
`test_rehydration_errors_resets_each_call_not_accumulating` close it.

### PROOF 3 — the outer roster guard's breadth is unpinned

**Mutation M19b**: narrowing `store.py:555` from `except Exception` to
`except sqlite3.Error` passes all 763, because the only test driving it drops a
table (an `OperationalError`). The two *inner* guards are proven broad — `M19` kills
4 tests and `M19c` kills 1. Pre-existing, inherited from Batch 2b, unchanged here.

### PROOF 4 — `store._WIRE_STATE_ORDER`'s length is unpinned

**Mutation M22**: a fifth entry survives, because
`test_trap_state_from_wire_maps_every_valid_byte_to_the_right_state` iterates
`range(len(TrapState))` and widens with it. The guard that matters
(`payloads._VALID_STATE_VALUES`) is solid — `M22a` dies against
`test_decode_heartbeat_rejects_an_out_of_range_state_byte` /
`..._captured_..._state_after_byte`, both parametrized over `range(len(TrapState),
256)`, so **wire byte 4 is genuinely rejected**. One `pytest.raises(ValueError):
trap_state_from_wire(4)` closes it — and it is the natural home for BEHAVIOUR 2's
`trap_state_from_wire(-1)` case too.

### PROOF 5 — `_unreadable()`'s message content is unconstrained

**Mutation U-msg**: `raise ValueError("")` passes all 763. Low value on its own —
the three `caplog` tests pin *level* and *traceback presence*, which is what an
operator needs — but the helper's whole purpose is to be the one place the reason is
named, and right now that reason could become an empty string silently.

---

## Verdict per claimed closure

### Q1. CRITICAL 1 — `_reported_state`'s second branch — **CLOSED**, and the four cases cover the rule

**N2 reproduced and it dies**: reverting *only* `if trap.state is None: return None`
to `return ReportedState(armed=False, targets_version=trap.targets_version)` fails
`test_a_command_to_a_never_heard_from_node_survives_a_restart[set_targets_then_
disarm]`, with the other three cases green — exactly the shape the record claims.
`N1` (the `KeyError` branch alone), `N3` (both) and `N4` (dropping the `reported is
not None` guard, 5 failures) all die too.

**Is there a third shape of "nothing known" the cases miss? No.** I walked
`_reported_state`'s complete control flow (`store.py:888-894`): it has exactly three
exits — `KeyError → None`, `state is None → None`, and a `ReportedState`. Branch 1
is reached when the node has no registry entry; branch 2 when it has one whose
`state` is still at its `Trap` default. Every method that can create an entry
without setting `state` — `_set_pending` (`traps.py:171`), `restore_desired_tags`
(`:266`), `restore_capture` (`:210`), `mark_state_possibly_stale` (`:240`),
`restore_heartbeat_fields` (`:253`) — lands on branch 2, so every operator command
sequence that reaches "entry exists, nothing reported" reaches the branch the fourth
case pins. A corrupt-`state_after` CAPTURED row is a *second route* to branch 2 (the
capture is restored at `:768-772` before the raise at `:779`), but it is the same
branch. **The rule is covered, not four examples of it.**

`POST /acknowledge` is the one operator route that creates no entry — and it is now
covered separately (`test_an_acknowledge_only_node_is_counted_in_the_roster_and_
rehydrates_to_nothing`, two real boots, asserting the 500 *and* that the roster
counts the node *and* that no `Trap` is fabricated). SUGGESTION 2 **CLOSED**.

**Where the coverage genuinely stops** is one branch further out, and it is not a
"nothing known" shape at all: branch 3 builds `armed=trap.state is TrapState.ARMED`
from a state the *live* path would never have checked a command against. That is
BEHAVIOUR 4.

### Q2. The `_unreadable()` unification — **PARTIALLY CLOSED**

**Every `_unreadable()` call site sits inside `rehydrate()`'s `except Exception` —
verified, CRITICAL 2 is not reopened.** All five sites (`:721`, `:764`, `:779`,
`:835`, `:864`) live in `_rehydrate_heartbeat`, `_rehydrate_capture` and
`_rehydrate_desired_command`; `rg` across `src/` shows those three are called from
exactly two places, both inside `rehydrate()`'s own `try` blocks (`:564-575` and
`:623-627`). `M19` (inner state-step guard → `except ValueError`) kills 4 tests,
`M19c` (desired-command guard → `except ValueError`) kills 1, `M20` (roster query
back above the guard) kills 1, and **`U-base`** — making `_unreadable` raise a
`BaseException` subclass so it escapes the guard — kills 6. Totality intact.

All five reverts die: `U-hb`, `U-tag`, `U-sa`, `U-ver`, `U-tags`.

**The exhaustive field enumeration, built from the source and not from the previous
report's list.** Every value the rehydration walk reads:

| # | Field read | Site | Handling |
|---|---|---|---|
| 1 | `node_cursor.node_id` ∪ `event.node_id` where source in (operator, station) | `:660-667` | inside the guard → `roster_lost` ✓ |
| 2 | `COUNT(DISTINCT event.node_id)` | `:691` | same guard ✓ |
| 3 | `event.fields` JSON text | `_row_to_event:218` | `json.loads` raises inside the guard ✓ |
| 4 | HEARTBEAT `fields["state"]` *type* | `:713-721` | `_unreadable()` ✓ |
| 5 | HEARTBEAT `fields["state"]` **value** | `:724` → `:238` | **negative index fabricates a state — BEHAVIOUR 2** |
| 6 | HEARTBEAT `received_epoch` | `:725` | `OverflowError`/`ValueError` inside the guard ✓ (CRITICAL A) |
| 7 | HEARTBEAT `fields["battery_mv"]` | `:732` | **coerced to `None`, uncounted — BEHAVIOUR 3** |
| 8 | HEARTBEAT `fields["targets_version"]` | `:733` | **coerced to `None`, uncounted — BEHAVIOUR 3** |
| 9 | CAPTURED row absent | `:756-757` | honest "no fact" ✓ |
| 10 | CAPTURED `tag` NULL | `:758-764` | `_unreadable()` ✓ (case D) |
| 11 | ACK `seq` vs CAPTURED `seq` | `:767` | `INTEGER` columns ✓ |
| 12 | CAPTURED `fields["state_after"]` *type* | `:775-779` | `_unreadable()` ✓ |
| 13 | CAPTURED `fields["state_after"]` **value** | `:782` → `:238` | **negative index fabricates a state — BEHAVIOUR 2** |
| 14 | FAULT row presence | `:792-797` | the msg type *is* the fact ✓ |
| 15 | latest `SET_TARGETS` `fields["tags"]` (desired list) | `:813-815` | **silently skipped, uncounted — BEHAVIOUR 1** |
| 16 | operator command rows' `seq` | `:821` | `INTEGER` ✓ |
| 17 | winning `SET_TARGETS` `fields["targets_version"]` | `:829-835` | `_unreadable()` ✓ (case E) |
| 18 | winning `SET_TARGETS` `fields["tags"]` | `:857-864` | `_unreadable()` ✓ (case F) |

**Eight not-readable expressions; five raise, three do not, and one of the five
type-checked fields is then handed to a function that will happily fabricate from
an out-of-range value.** The three new tests (D/E/F) are well built — real `Store`,
real `rehydrate`, genuine RED→GREEN with the actual failure output recorded, and
each asserts a *different* expected shape (ARMED+stale+1 error+no capture; no
command + tags still restored + 1 error; node absent + 1 error). No trivial
assertions anywhere in them.

### Q3. The badge pin — **PARTIALLY CLOSED** — see PROOF 1

### Q4. Log sites — **CLOSED, and this one is genuinely exhaustive**

Re-derived from source across the **whole** `src/` tree, not just the two files the
record names:

```
rg "logger\.(exception|warning|error|debug|info|critical)" src/feeder_station/
  store.py:556   logger.exception  rehydrate: could not read the node roster
  store.py:572   logger.exception  rehydrate: skipping an unreadable row for node %s
  store.py:626   logger.exception  rehydrate: skipping an unreadable row for node %s
  ingest.py:100  logger.warning    ingest: dropped an undecodable frame: %s
rg "getLogger" src/  ->  store.py:48, ingest.py:59 only.  No print(), no warnings.warn().
```

Exactly four. **No site was added by this batch** (`git diff -U0 0374341..HEAD` on
`store.py` touches no `logger.` line). All four now die:

| Site | Mutation | Result |
|---|---|---|
| `store.py:556` roster failure | **N7** (→ `logger.debug`, traceback dropped) | **KILLED** |
| `store.py:572` state-race loop | L572 (→ `logger.debug`) | **KILLED** |
| `store.py:626` desired command | L626 (→ `logger.debug`) | **KILLED** |
| `ingest.py:100` drop log | I-100 (→ `logger.debug`) | **KILLED** (2 tests) |

The new `caplog` assertion is well targeted: it filters records by message, asserts
`len == 1`, `levelno == logging.ERROR` and `exc_info is not None`, wrapped at
`logging.DEBUG` so a downgrade is *visible* rather than filtered out. That is the
right shape. **This is the first claim in this change's history that I could not
find a fourth item hiding behind.**

### Q5. WARNING 4's fix — **CLOSED, and no false alarm was introduced**

Six scenarios measured, plus four more I added. The counts are right in every one:

| Scenario | expected | rehydrated | errors | roster_lost | `ok` | Correct? |
|---|---|---|---|---|---|---|
| healthy roster (5 nodes) | 5 | 5 | 0 | false | **true** | ✓ |
| honestly-skipped corrupt node (20 nodes, node 13 corrupt) | 20 | 20 | **1** | false | **true** | ✓ counted, not alarmed |
| **station-sourced-only node** | 2 | **2** | 0 | false | **true** | ✓ **fixed** (was 2/1 → `ok:false`) |
| operator-only node | 2 | 2 | 0 | false | **true** | ✓ |
| acknowledge-only node | 2 | 2 | 0 | false | **true** | ✓ |
| **genuinely lost `node_cursor` row** | 3 | **2** | 0 | false | **false** | ✓ **the real signal still fires** |
| lost `node_cursor` + a station-only node | 3 | 2 | 0 | false | **false** | ✓ the widening does not mask it |
| `node_cursor` table gone under the live connection | 0 | 0 | 1 | **true** | **false** | ✓ `roster_lost`, not `roster_incomplete` |
| empty store | 0 | 0 | 0 | false | **true** | ✓ |

**No false alarm was introduced.** I looked for one specifically. `Source` is
`Literal["node", "operator", "station"]` (`events.py:28`) and `append()` upserts
`node_cursor` inside the same transaction for **every** node-sourced row
(`store.py:394-407`) — duplicates return before either insert (`:366-368`) — so the
union `node_cursor ∪ event(operator, station)` is now exactly `event(all sources)`
unless a `node_cursor` row was really lost. Over-count is impossible (there is no
`DELETE FROM event` anywhere in `src/`, so no orphan cursor rows), and the check
uses `<` not `!=` anyway. The widening is **source-based, not msg_type-based**, so
it is also future-proof against new `STATION_*` message types.

Five mutations confirm the check has teeth: `R-op` (revert to `source='operator'`)
kills the new test, `R-st` kills 8, `R-all` (`SELECT DISTINCT node_id FROM event`)
kills 2, `R-nocur` (drop the `node_cursor` half) kills 34, `R-ok` (drop the
`roster_incomplete` term from `roster_ok`) kills 1, `R-dir` (flip `<` to `>`) kills 1.
The corrected `_count_all_known_nodes` docstring (`:677-689`) now states the truth.

### Q6. Regressions — **none**

| Property | Probe | Status |
|---|---|---|
| `rehydrate()` totality (`except Exception`, per row-source) | M19 (4 kills), M19c (1), U-base (6) | intact — the new raises are **inside** it |
| Roster query inside the guard | M20 killed | intact |
| `rehydration_errors` reset per pass | M21 killed | intact |
| `_VALID_STATE_VALUES == range(len(TrapState))`, wire byte 4 rejected | M22a killed on HEARTBEAT **and** CAPTURED | intact |
| Nullable `state` honest through every consumer | M23 killed (5 tests) | intact — `/api/traps` `state: null`, `index.html:136` → `desconocido` |
| CRITICAL A live-equals-rehydrated, all four `state_after` | M12 (4 kills), M13 (5 kills); 4 cases collected and green | intact **for `state`** — see BEHAVIOUR 4 for the `pending_command` axis |
| The five-ordering property test | 5 cases collected and green; M8 (`key=at`) and M8b (`min`) both killed | intact, with teeth |
| The ordering key's teeth | M8 killed | intact |
| Batch 1/2a/2b/2c behaviour | no test removed, no assertion weakened — `git diff 0374341..HEAD -- tests/` has **19 deletions**, all import-line, docstring, signature or fixture rewrites; **zero assertion values changed** | intact |

### Q7. Records

| Item | Verdict |
|---|---|
| **2.6** (CRITICAL 1) | **Accurate.** The two-branch description matches the code, N2's reachability claim is reproduced, and "Closed in Batch 2d: a fourth parametrize case... N2 now dies" is true. |
| **2.10** (log sites) | **Accurate and exhaustive.** "exactly four skip/drop log sites exist" is true across all of `src/`, re-derived independently. Correctly says Batch 2c closed 3 of 4. |
| **2.9** (WARNING 1/2/4) | **Accurate about what it did; wrong about what it finished.** Every specific claim checks out. But "all three WARNING 1 paths now raise through one shared `_unreadable()` helper" is presented as the close of the rule, and the rule has three more expressions — the same "closed narrower than the hole" shape for the **fourth** consecutive round. The 2c enumeration was inherited rather than re-derived from source, which is precisely what 2.10 *did* do and why 2.10 is right. |

**Two inaccuracies, one of them introduced by this very batch:**

1. **`src/feeder_station/static/index.html:142-143`** — still reads *"Rendered as
   doubt (italic), not as the bolder 'no contact' alarm"*, while commit `9ae7b51`
   changed `.stale-badge` to `font-weight: 700` with no italic **in the same
   commit** (`:29-33`). The comment now describes the opposite of the CSS three
   lines above it.
2. **`src/feeder_station/adapters/store.py:897-901`** — `_unreadable`'s docstring
   claims it is *"The single expression of 'this value cannot be read'* **every**
   *not-readable guard in this module now raises through -- five call sites"*.
   There are eight not-readable expressions; five raise. This is the load-bearing
   sentence a future reader will trust when deciding whether a new guard needs one,
   and it is false.

---

## Mutation testing — 46 mutations, 37 killed, 9 survived

**Method gotcha honoured.** `feeder_station.adapters.store.__file__`,
`feeder_station.app.__file__`, `feeder_station.domain.traps.__file__`,
`...payloads.__file__` and `...ingest.__file__` all assert under this repo's `src/`
before the first run — never a `git worktree`, which this venv's editable-install
`sys.meta_path` finder would silently ignore and report a false green. Every
mutation restored inside a `finally`, with `git status --porcelain` asserted equal
to the three expected untracked report files **after every single one**,
programmatically. The per-commit sweep used the same real tree, restored the same
way. Final tree: clean.

### Killed (37)

| # | Mutation | Killed by |
|---|---|---|
| N2 | `_reported_state`: `state is None` → fabricated `ReportedState` *(implementer's)* | `[set_targets_then_disarm]` — **reproduced** |
| N1 | `_reported_state`: `KeyError` branch fabricates | `[disarm]` |
| N3 | `_reported_state`: full pre-batch revert | 2 cases |
| N4 | drop the `reported is not None` guard | 5 tests |
| U-hb | `_rehydrate_heartbeat`'s `_unreadable` → `return None` | the non-int-state test |
| U-tag | `_rehydrate_capture`'s tag guard → `return None` *(implementer's D)* | the null-tag test — **reproduced** |
| U-sa | `_rehydrate_capture`'s `state_after` guard → `return None` | 2 tests |
| U-ver | `targets_version` guard → bare `return` *(implementer's E)* | the corrupt-version test — **reproduced** |
| U-tags | `tags` guard → `tags = ()` *(implementer's F)* | the corrupt-tags test — **reproduced** |
| U-base | `_unreadable` raises a `BaseException` subclass (escapes the guard) | 6 tests — **totality proven** |
| N7 | roster-failure log → `logger.debug`, traceback dropped *(implementer's)* | the new `caplog` assertion — **reproduced** |
| L572 | state-race loop log → `logger.debug` | its own `caplog` test |
| L626 | desired-command log → `logger.debug` | its own `caplog` test |
| I-100 | ingest drop log → `logger.debug` | 2 tests |
| R-op | roster union → `source = 'operator'` *(implementer's)* | the station-source test — **reproduced** |
| R-st | roster union → `source = 'station'` | 8 tests |
| R-all | roster → `SELECT DISTINCT node_id FROM event` | 2 tests |
| R-nocur | roster drops the `node_cursor` half | 34 tests |
| R-ok | `roster_ok` drops the `roster_incomplete` term | the partial-roster test |
| R-dir | `roster_incomplete` `<` → `>` | the partial-roster test |
| N5 | badge inverted (`!t.state_may_be_stale ?`) *(implementer's)* | the anchored regex — **reproduced** |
| N6 | badge ternary both branches empty *(implementer's)* | the anchored regex — **reproduced** |
| B5 | `view()` emits `state_may_be_stale: False` always | the `/api/traps` test |
| B6 | `view()` emits `True` always | the same |
| B7 | `mark_state_possibly_stale` never called | 5 tests |
| M19 | inner state-step guard → `except ValueError` | 4 tests — Batch 1 totality intact |
| M19c | desired-command guard → `except ValueError` | 1 test |
| M20 | roster query moved back outside the guard | `test_boot_survives_a_broken_node_roster_query` |
| M21 | `rehydration_errors` not reset per pass | its own test |
| M8 | `max(facts, key=seq)` → `key=at` | the ordering test |
| M8b | `max` → `min` on `seq` | 8 tests |
| M22a | `payloads._VALID_STATE_VALUES` → `range(5)` | wire-byte-4 tests, HEARTBEAT **and** CAPTURED |
| M23 | `view()`: `None` state → `"idle"` | 5 tests |
| M12 | `Registry.captured()` hardcodes `FIRED` | 4 tests (the `state_after` table) |
| M13 | `_rehydrate_capture` hardcodes `FIRED` | 5 tests |

### Survived (9)

| # | Mutation | Meaning |
|---|---|---|
| **B3** | badge markup kept, content emptied | **PROOF 1.** MINE. `[^<]*` matches empty. |
| **B4** | ternary hoisted into dead code, never rendered | **PROOF 1.** MINE. The regex is position-blind; the badge disappears. |
| **CSS-hide** | `.stale-badge { display: none; }` | **PROOF 1.** MINE. Present in the DOM, invisible on screen. |
| **W-dt** | route `:813-815` through `_unreadable` (i.e. **the fix**) | **BEHAVIOUR 1.** MINE. Nothing pins this path either way. |
| **W-neg** | reject a negative index in `trap_state_from_wire` (**the fix**) | **BEHAVIOUR 2.** MINE. Nothing pins it. |
| **W-bat** | corrupt `battery_mv`/`targets_version` → `0` instead of `None` | **BEHAVIOUR 3.** MINE. A fabricated dead battery ships green. |
| **R-reset** | the two roster counts not reset per pass | **PROOF 2.** Carried from 2c. |
| **M19b** | outer roster guard → `except sqlite3.Error` | **PROOF 3.** Pre-existing. |
| **M22** | `_WIRE_STATE_ORDER` widened to 5 entries | **PROOF 4.** |
| **U-msg** | `_unreadable` raises `ValueError("")` | **PROOF 5.** MINE. |

*(`CSS-del` also survives and is deliberately **not** counted — asserting CSS class
values is forbidden by this project's own assertion-quality rules, and the apply
record says so explicitly and correctly.)*

**One combined probe worth recording**: applying the fixes for BEHAVIOUR 1, 2 and 3
**together** — a negative guard in `trap_state_from_wire`, routing `:813-815`
through `_unreadable()`, and raising on a non-int `battery_mv`/`targets_version` —
passes **all 763 tests**. About ten production lines, zero regression.

---

## Spec compliance movement (Batch 2d scope only)

| Capability | Scenario | Before | After | Evidence |
|---|---|---|---|---|
| trap-commands | resend continues until the version is confirmed, across a restart | ⚠️ PARTIAL | ⚠️ **PARTIAL** | Both `_reported_state` branches now pinned (N2 dies); but a restart still drops a pending command in 5 of 18 live combinations — BEHAVIOUR 4 |
| event-store | an unreadable row is counted, never a silent all-clear | ⚠️ PARTIAL | ⚠️ **PARTIAL** | 5 of 8 not-readable expressions raise; three silent + one fabricating — BEHAVIOUR 1/2/3 |
| event-store | rebuild without depending on the node re-sending | ✅ MET | ⚠️ **PARTIAL** | Roster proven for all four operator `msg_type`s and for station-sourced nodes; but a corrupt superseded target list makes it depend on the *operator* re-entering — BEHAVIOUR 1 |
| operator-ui | an unreachable / unknown / doubtful trap is marked, never shown as calm | ⚠️ PARTIAL | ⚠️ **PARTIAL** | Data layer pinned both ways; rendering unpinned (PROOF 1); a negative stored byte renders a confident `idle` (BEHAVIOUR 2) |
| station-deployment | `/health` distinguishes a degraded boot from a healthy one | ⚠️ PARTIAL | ✅ **MET** | Nine roster scenarios measured, correct in all nine, no false alarm; all four log sites pinned |

---

## Final verdict

**FAIL** — 4 BEHAVIOUR, 5 PROOF, 2 record inaccuracies.

**What is genuinely good here, said plainly.** This is the strongest batch of the
six. Four of the six closures are complete, and **WARNING 3's is the first
exhaustive enumeration in this change's history** — the implementer stopped
inheriting the previous report's list and ran `rg` against the source, which is
exactly the discipline four rounds of "one more site hiding behind the claim" were
asking for, and it is why I could not find a fifth log site. WARNING 4's fix is
correct, minimal, source-based rather than msg_type-based (so it is future-proof),
and does not false-alarm in any of the nine scenarios I could construct — including
the two the brief specifically warned about. CRITICAL 1's fourth parametrize case
does not just add an example; it closes the last branch of a two-branch function
whose control flow I walked exhaustively. The `_unreadable()` unification is the
right refactor — one helper instead of four hand-written shapes — and every one of
its call sites sits inside the guard, so CRITICAL 2 stays closed. The three new
WARNING 1 tests are genuine RED→GREEN with the real failure output recorded, they
assert three *different* shapes rather than three empty collections, and no trivial
or tautological assertion appears anywhere in the batch. Every commit is
independently green on all three gates, counts climb monotonically, format drift is
provably zero against a real `504d18e` checkout, and the apply record's line-count
composition table is accurate to the line.

**And the diagnosis has changed shape.** For five rounds the finding was *"the code
is right, the proof is not."* **That is no longer the whole story.** The
`_unreadable()` unification closed five of eight not-readable expressions and then
stated, in the helper's own docstring, that it had closed all of them. The three it
missed are not proof gaps: one loses an operator's target list silently, one
fabricates a calm `idle` from a corrupt byte, one swallows a battery reading on a
battery-powered field system. They are all uncounted, unflagged, and `ok: true`.
The reason four consecutive rounds each left one open is now visible and
structural: **each round enumerated from the previous report's list instead of from
the source.** Task 2.10 broke that habit for logging and got the right answer the
first time. Task 2.9 did not, and got the wrong answer the fourth time.

**Are there any BEHAVIOUR defects remaining in Batch 2's scope?** **Yes — four.**
BEHAVIOUR 1 (`store.py:813-815`), BEHAVIOUR 2 (`store.py:236-240`), BEHAVIOUR 3
(`store.py:728-734`), BEHAVIOUR 4 (`_reported_state` vs `captured()`/`fault()`).
Each has a measured input and a measured wrong outcome above. None of them was
introduced by Batch 2d.

**May Batch 2 be pushed?** **Not as a complete close-out — but the seven commits
themselves are sound and should not be unwound.** They are green, bisect-safe,
regression-free, and every individual technical claim in them is true. What is false
is the claim of completeness. Two options, and I recommend the first:

1. **Push the seven commits as a partial close-out**, after two documentation
   corrections that cost nothing and stop the false claim from propagating into the
   next round's enumeration:
   - `store.py:897-901` — `_unreadable`'s docstring must say *five of the module's
     not-readable expressions*, and name the three that still do not raise.
   - `index.html:142-143` — the comment must match the CSS `9ae7b51` shipped.

   Then **Batch 2e**: BEHAVIOUR 1, 2 and 3 as one explicitly-scoped production
   slice. ~10 production lines (measured green together), plus one test each and one
   `pytest.raises(ValueError): trap_state_from_wire(4)` / `(-1)` that closes PROOF 4
   on the way past. This is **not** a seventh proof-only round — it is three real
   defects, and the fix is the one the rule already implies.

2. Hold the push and fold Batch 2e in first. Slower, and it delays CRITICAL 3
   further for no correctness gain, since the seven commits are independently sound.

**Do not tick task 2.9 clean.** It must record the `_unreadable()` unification as
*five of eight expressions closed, three still silent*, naming `store.py:813`,
`:732`, `:733` and the negative-index path at `:238`. Task 2.10 may be ticked clean
— it is the one claim in this change's history I could not find a hole behind.

**May Batch 3 start?** **Yes, now, in parallel with Batch 2e.** Batch 3 is CRITICAL
3 + CRITICAL 4 — the station-wide frame budget in `adapters/workers.py`,
`adapters/scheduler.py`, `domain/backlog.py` and `domain/commands.py`. It touches
none of `store.py`'s rehydration walk, `_known_node_ids`, `/health`'s roster fields
or `static/index.html`. **CRITICAL 3 is a measured behaviour defect on a
battery-powered field system — 11.68% airtime occupancy against a ~1% budget — and
it has now waited four rounds behind proof work. It should not wait a fifth.**

Carry into Batch 3's brief, as one item with SUGGESTION 5:
**`captured()` and `fault()` never run `satisfied_by`, and rehydration always
does.** SUGGESTION 5 is the resend-forever half; BEHAVIOUR 4 is the silent-drop
half. Both are the same asymmetry, both are frame-budget decisions, and fixing
either one in isolation will get the other one wrong.

---

## Scheduled PROOF gaps — one line each, ready to become tasks

1. **Badge rendering is untestable by source regex.** B3 (empty badge content), B4
   (ternary hoisted to dead code, never rendered) and CSS-hide (`display: none`) all
   pass all 763 tests; decide between a DOM harness for `renderTrap` and an explicit
   recorded acceptance that `index.html:153` is unverifiable at this layer.
2. **`roster_nodes_expected`/`roster_nodes_rehydrated` are not pinned as
   per-pass.** Deleting their reset at `store.py:550-551` passes all 763; add three
   assertions to `test_rehydration_errors_resets_each_call_not_accumulating`.
3. **The outer roster guard's breadth is unpinned.** Narrowing `store.py:555` from
   `except Exception` to `except sqlite3.Error` passes all 763 because the only test
   driving it drops a table; add a non-`sqlite3.Error` roster failure.
4. **`store._WIRE_STATE_ORDER`'s length is unpinned.** Adding a fifth entry passes
   all 763 because the test iterates `range(len(TrapState))`; add
   `pytest.raises(ValueError): trap_state_from_wire(4)` — and `(-1)`, which also
   closes BEHAVIOUR 2's proof.
5. **`_unreadable()`'s message is unconstrained.** `raise ValueError("")` passes all
   763; assert the `what` string appears in one of the three existing `caplog`
   tests' `record.getMessage()` or `exc_info` value.
