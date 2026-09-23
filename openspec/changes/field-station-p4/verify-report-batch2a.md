# Verification Report — field-station-p4, Batch 2a (CRITICAL 1 + WARNING J)

**Scope**: tasks 2.1, 2.2, 2.3, 2.4, 2.8 only. Diff `7901ac3..HEAD` (4 commits, 831 changed lines
including docs; 610 in code+tests). **Artifact store**: openspec. **Mode**: Strict TDD.
**Out of scope, deliberately not re-checked**: CRITICAL 6 and WARNING K (tasks 2.5-2.7, 2.9, 2.10 —
Batch 2b), CRITICAL 3/4/5/I (Batches 3-5).

**Verdict: FAIL** — 1 CRITICAL, 4 WARNING, 6 SUGGESTION.

Every claim below was re-derived from the repository. The implementer's account was treated as a
hypothesis: its two mutations were re-run, ten more were invented, the Part 1.2 table was
re-measured from real wire bytes through the real decoder, and every RED claim was replayed against
the pre-batch tree.

---

## Executive verdicts (the four the brief asked for)

| Question | Verdict |
|---|---|
| **Is CRITICAL 1 closed?** | **PARTIALLY.** All four orderings the report measured, plus the unmeasured mirror, genuinely agree now — independently re-measured. But CRITICAL 1's own invariant is *"the rehydrated registry equals the live registry, for every ordering **and every report type**"*, and this batch **introduces a new live-vs-rehydrated divergence on the report-content axis**: any `CAPTURED` whose `state_after` is not `2`. See CRITICAL A. |
| **Is WARNING J closed?** | **YES** for its stated scenario. `state` is genuinely nullable end to end, no consumer crashes/coerces/mis-serializes, and both "undo WARNING J" mutations are killed. A narrower residual variant remains (WARNING C) and belongs with Batch 2b's task 2.9. |
| **Is the `ARMED`→`FIRED` assertion change justified?** | **YES — not a blind revert.** Verified from the fixture, not from the argument. Option (b) is genuinely satisfied: the sibling covers the mirror direction with a different fixture and a different expected answer. |
| **May Batch 2a be pushed? May Batch 2b start?** | **Push: NO, not as a CRITICAL 1 closure** — fold the CRITICAL A fix into this stack first (est. 3 production lines + 1 parametrize axis). **Batch 2b: YES, may start now** — it touches `_known_node_ids`, `/health` and `caplog` only, no overlap with the state race. |

---

## Gates (each exit status checked directly, nothing piped into `head`/`tail`)

| Gate | Exit | Result |
|---|---|---|
| `uv run pytest` | **0** | 737 passed (729 baseline + 8 new) |
| `uv run ruff check .` | **0** | All checks passed |
| `uv run ruff format --check .` | **1** | 6 files — **identical set to baseline `504d18e`**, verified by running the repo's own `ruff` against a `504d18e` worktree and diffing the file lists. **Zero new drift.** |
| `uv run mypy src` | **0** | no issues in 22 source files |

Baseline comparison (both lists identical): `openspec/changes/field-station-p4/design.md`,
`src/feeder_station/domain/payloads.py`, `tests/adapters/test_ingest.py`,
`tests/adapters/test_serial_gateway.py`, `tests/domain/test_events.py`, `tests/domain/test_traps.py`.

**Per-commit greenness** (the stacked-PR claim, verified rather than trusted — `src`+`tests` checked
out at each sha, suite run, tree restored):

```
1fb6f15 (WARNING J domain foundation) -> 729 passed
68002dd (CRITICAL 1 core)             -> 731 passed
86f93ec (CRITICAL 1 coverage)         -> 737 passed
```

All three are independently green. A bisect through this stack is safe.

---

## Task completeness

| Task | Boxed | Verified | Note |
|---|---|---|---|
| 2.1 property-shaped test | `[x]` | **Yes, with a caveat** | Five distinct logs, one parametrized test. Not a property test in the sense the rule requires — see WARNING B |
| 2.2 winning-row restructuring | `[x]` | Yes | Four-step `rehydrate()`; `_ReportedFact`; `max(..., key=seq)` |
| 2.3 stale-comment refactor | `[x]` | Yes | `store.py::_rehydrate_capture` and `traps.py::restore_capture` docstrings both rewritten; one *new* stale comment created elsewhere (SUGGESTION 3) |
| 2.4 edited assertion | `[x]` | Yes | Option (b), correctly executed — see below |
| 2.8 WARNING J | `[x]` | Yes | `Trap.state: TrapState \| None` |
| 2.5-2.7, 2.9, 2.10 | `[ ]` | Correctly left open | Batch 2b |

`remediation-tasks.md`'s diff is tick marks only — no prose was silently edited to match the code.

---

## TDD Compliance (Strict TDD active)

RED was not taken on trust. `src/` was checked out at `7901ac3` (pre-batch) with HEAD's tests, and
the suite was run:

```
7 failed, 58 passed
FAILED test_cli.py::...for_every_ordering[heartbeat_old_capture_new]
FAILED test_cli.py::...for_every_ordering[capture_alone_no_heartbeat_ever]
FAILED test_cli.py::...for_every_ordering[heartbeat_then_fault]
FAILED test_cli.py::test_a_node_with_no_readable_state_source_shows_unknown_not_idle
FAILED test_store.py::test_registry_reflects_last_known_state_after_restart
FAILED test_store.py::test_a_winning_captured_row_reads_its_own_state_after_not_a_hardcoded_fired
FAILED test_store.py::test_rehydrate_still_shows_the_state_of_an_already_acknowledged_capture
```

| Check | Result |
|---|---|
| TDD Evidence table present | Yes |
| RED confirmed by replay | Yes — every task's named test genuinely fails against the pre-batch tree |
| GREEN confirmed | Yes — 737/737 at HEAD |
| Triangulation | 2.1: 5 cases (but see WARNING B); 2.4: 2 cases (both directions); 2.8: 1 case |
| Safety net | Every modified test file's other assertions verified unchanged by reading `git diff` — confirmed, only the two disclosed assertions changed |

One inaccuracy in the record: the evidence table says *"4 of 5 parametrized cases failed before the
fix"*. Measured: **3 of 5** (`capture_old_heartbeat_new` and `fault_then_newer_heartbeat` both
already passed). The prose that follows it names the three correctly, so this is an arithmetic slip,
not a fabrication — but on a change whose entire history is inaccurate records, it is worth naming
(SUGGESTION 4).

**Test layer distribution (new tests)**: unit 3 (`tests/adapters/test_store.py`, direct
`Store.rehydrate()`), integration/boot 6 (`tests/test_cli.py`, real `cli.build_app` + ASGI lifespan
+ `TestClient`). Coverage analysis skipped — no coverage tool in `pyproject.toml`.

---

## Part 1.2, re-measured from scratch

Not the implementer's script. Frames were built as **real wire payloads**, pushed through
`frames.encode` → `frames.decode` → `events.interpret`, appended to a real `Store`, replayed into a
live `Registry` via `ingest._apply_to_registry`, and compared against a real `cli.build_app` boot
read back through `GET /api/traps`. `capture` was compared as well as `state`/`last_seen`/`reachable`.

```
A. capture 6h OLD (state_after=2), heartbeat 2min NEW (state=0)   state idle  / idle   AGREE
B. heartbeat 6h OLD (state=1), capture 2min NEW (state_after=2)   state fired / fired  AGREE   <- was DIVERGES
C. capture 30s ago (state_after=2), no heartbeat ever             state fired / fired  AGREE   <- was DIVERGES
D. heartbeat (state=1), then FAULT 1min later                     state fault / fault  AGREE   <- was DIVERGES
E. FAULT, then a newer HEARTBEAT (state=1)                        state armed / armed  AGREE
   (last_seen, reachable and capture also AGREE in all five)
```

The implementer's A-E table reproduces exactly. Then the axis nobody tested:

```
F. heartbeat OLD (state=1), capture NEW with state_after=0   live 'fired' / rehydrated 'idle'   DIVERGES
G. capture alone, state_after=3 (FAULT)                      live 'fired' / rehydrated 'fault'  DIVERGES
H. capture alone, state_after=1 (ARMED)                      live 'fired' / rehydrated 'armed'  DIVERGES
```

---

## Mutation testing — 2 reproduced, 10 invented

Method gotcha honoured: every mutation was applied **in the real package root**
(`uv run python -c "import feeder_station.adapters.store as m; print(m.__file__)"` resolves to this
repo's `src/`), never in a worktree; each was reverted with `git checkout -- src/` and
`git status --porcelain` was asserted empty after every single one. Final tree: clean.

| # | Mutation | Result | Killed by |
|---|---|---|---|
| M1 | `winner = max(facts, key=seq)` → `facts[0]` *(implementer's #1)* | KILLED | `test_registry_reflects_last_known_state_after_restart` + `[heartbeat_old_capture_new]` + `[heartbeat_then_fault]` — **reproduces their claim exactly** |
| M2 | winning `CAPTURED` fact → hardcoded `FIRED` *(implementer's #2)* | KILLED | only `test_a_winning_captured_row_reads_its_own_state_after_not_a_hardcoded_fired` — **reproduces their claim, including that it is a single-test kill** |
| **M3** | **winner ordered by `fact.at` (timestamp) instead of `fact.seq`** | ***SURVIVED* all 737** | nobody — see WARNING A |
| M4 | `FAULT` never produces a candidate | KILLED | `test_rehydrate_skips_and_counts_an_unreadable_row...` |
| M5a | `view()`: `None` state falls back to `"idle"` | KILLED | `test_a_node_with_no_readable_state_source_shows_unknown_not_idle` |
| M5b | `Trap.state` default back to `TrapState.IDLE` (undo WARNING J) | KILLED | same |
| M6 | drop the `battery_mv`/`targets_version` restore | KILLED | `test_registry_reflects_last_known_state_after_restart` |
| M7 | `_rehydrate_heartbeat` non-atomic (restore fields *before* validating `state`) | KILLED | `test_rehydrate_skips_and_counts_an_unreadable_row...` — **the atomicity claim in its docstring is genuinely pinned** |
| M8 | oldest report wins (`max`→`min`) | KILLED | `test_registry_reflects_last_known_state_after_restart` |
| M9 | `restore_reported_state` stops setting `last_seen` | KILLED | 6 tests — `last_seen` is genuinely asserted, not decorative |
| M10 | an acknowledged `CAPTURED` contributes no state fact | KILLED | `test_rehydrate_still_shows_the_state_of_an_already_acknowledged_capture` |
| M11 | `rehydration_errors` not reset per pass | KILLED | `test_rehydration_errors_resets_each_call_not_accumulating` (Batch 1 — intact) |
| M12 | `except Exception` → `except ValueError` in the row guard | KILLED | `test_boot_survives_a_non_value_error_corrupt_row[received_epoch_overflow, fields_not_an_object]` (Batch 1 — intact) |
| M13 | roster query moved back outside the guard | KILLED | `test_boot_survives_a_broken_node_roster_query` (Batch 1 — intact) |
| M14 | `_VALID_STATE_VALUES` widened to `range(5)` | KILLED | `test_decode_heartbeat_rejects_an_out_of_range_state_byte[4]` + the `CAPTURED` twin (Batch 1 — intact) |
| M15 | capture object restored even when acknowledged (S3 resurfacing) | KILLED | the renamed test **and** `test_app.py::test_acknowledging_a_capture_survives_a_restart_through_the_route` |

Adversarial experiments:

| # | Experiment | Result |
|---|---|---|
| E1 | M2 run against **the property test alone** | ***SURVIVED*** (5 passed) — the property test cannot see a hardcoded `FIRED` |
| E2 | general rule when ≤2 state-bearing rows, deliberately wrong (second-newest) otherwise | ***SURVIVED* all 737** — no test anywhere has a node with three competing report types |
| E3 | **`max(seq)` replaced by a 5-entry lookup table keyed on the exact signatures of the five parametrized logs, falling back to `facts[0]` (wrong on purpose) for everything else** | ***SURVIVED* all 737** |

---

## CRITICAL

### CRITICAL A — rehydration reads `CAPTURED.state_after`; the live path hardcodes `FIRED`. The same log now produces two different states depending on whether the station restarted — which is CRITICAL 1's own defect, on a new axis

**Where**:
- `src/feeder_station/adapters/store.py:627-634` — `_rehydrate_capture` returns
  `_ReportedFact(state=trap_state_from_wire(state_after), ...)`
- `src/feeder_station/domain/traps.py:162-167` — `Registry.captured()`, the **live** path
  (`adapters/ingest.py:137`), sets `trap.state = TrapState.FIRED` unconditionally and never looks at
  `state_after`
- `src/feeder_station/domain/payloads.py:178-189` — `decode_captured` accepts `state_after` of
  **0, 1, 2 or 3** (`_require_valid_state`); the station's own comment calls it *"the same small enum
  as HEARTBEAT.state"*
- `tests/adapters/test_store.py:~290` — `test_a_winning_captured_row_reads_its_own_state_after_not_a_hardcoded_fired` **pins the divergent answer as correct**

**Inputs → wrong outcome** (measured, case F above):

1. Trap 11 heartbeats `state=1` (armed).
2. Trap 11 fires and sends `CAPTURED` with `state_after=0` — it fired, the door closed, and by its own
   report it is now idle. A perfectly conformant frame; the decoder accepts it.
3. Live station: `GET /api/traps` → `"state": "fired"`, capture card visible. Correct-looking.
4. Power cut. Station reboots. Same log, no new frames.
5. `GET /api/traps` → **`"state": "idle"`**, capture card still visible, `/health` `ok: true`,
   `rehydration_errors: 0`.

That is verify-report-2.md CRITICAL 1's own words reproduced verbatim: *"the page contradicts itself,
one row saying 'capture, tag 0384199C87EC06' and the state chip beside it saying 'idle'… a false
all-clear, on the one event the whole system exists to surface."* `state_after=3` (fired then faulted)
gives the mirror: live `fired`, rehydrated `fault`.

**Which requirement this violates**: the batch's own rule of proof — *"the rehydrated registry equals
the live registry, for every ordering **and every report type**"* (`remediation-tasks.md:163-165`).
`event-store/spec.md`'s "Registry rehydration at boot" requires the **last known reported state** to
be rebuilt; two paths that disagree cannot both be rebuilding it.

**Why no test caught it**: all five parametrized logs use `state_after: 2`
(`tests/test_cli.py`, `_REHYDRATION_ORDERINGS`), and `2` is the one value at which
`trap_state_from_wire(state_after)` and the live hardcoded `FIRED` coincide. E1 proves it directly:
the mutation that hardcodes `FIRED` in rehydration survives the property test untouched. The only
test that distinguishes them, `test_a_winning_captured_row_reads_its_own_state_after_not_a_hardcoded_fired`,
uses `state_after=1` and asserts the rehydrated answer is `ARMED` — **for a log whose live answer is
`FIRED`.** The batch consciously chose a `state_after` value other than 2 to kill a mutation, and did
not check what the live path says about that same log. This is the fourth appearance of the same
diagnosis: validated against the failing example (`state_after=2`), not against the rule.

**Not the implementer's invention.** Task 2.2 explicitly required `state_after` ("not a hardcoded
`FIRED`"), and verify-report-2.md's Fix paragraph says the same. The task list demanded a source the
live path does not use, and nothing asked anyone to reconcile the two. The defect is in the plan as
much as in the execution — but it ships either way.

**Fix direction** (not applied — verification does not fix): close WARNING 7 on the **live** side too,
i.e. `Registry.captured()` takes the reported `state_after` (`ingest.py::_apply_to_registry` already
decodes it), so both paths read one source; then add `state_after` as a second axis to
`_REHYDRATION_ORDERINGS` (`0`/`1`/`3`) so the invariant is actually held on the content axis. If
instead `FIRED` is deemed the rule for both, then `test_a_winning_captured_row...` is asserting the
wrong answer and must be rewritten. Either way: **one rule, both paths, with a test that would notice.**

---

## WARNING

### WARNING A — the winner is decided by arrival (`seq`), never by the node's own report time, and no test pins which

**Where**: `src/feeder_station/adapters/store.py:556` (`max(facts, key=lambda fact: fact.seq)`);
`_ReportedFact.at` is `_epoch_to_datetime(row.received_epoch)` — the **station's** receipt clock —
while the store already persists `node_epoch` (`events.py::_node_epoch_of` fills it from
`Heartbeat.clock_epoch` / `Captured.epoch`) and rehydration ignores it entirely.

**Unpinned**: mutation **M3** — reorder the race by `fact.at` instead of `fact.seq` — **survives all
737 tests**. The suite cannot tell the two rules apart because in every fixture the two orders coincide.

**Where they diverge** (measured):

```
seq=1  HEARTBEAT  node_epoch=18:45:20  received=18:45:20   state=armed
seq=2  FAULT      node_epoch=18:37:20  received=18:46:20   (buffered on the node, pulled by GET_BACKLOG)

most recent report by the NODE's own clock : HEARTBEAT (armed), 8 minutes newer
most recent row by store seq               : FAULT
live       state='fault'  last_seen=18:46:20  reachable=True
rehydrated state='fault'  last_seen=18:46:20  reachable=True
```

`GET_BACKLOG` exists precisely so a node can replay what it buffered while the link was down, so
out-of-order arrival is a designed-for case, not an exotic one. `trap-commands/spec.md:14` says the
displayed state must be *"whatever its own most recent HEARTBEAT or HELLO reported"* — the node's
report, not the station's mailbox order. The station displays a stale `fault` and stamps `last_seen`
with the arrival time of an eight-minute-old report, which also inflates `reachable`.

**Not a CRITICAL, and not a regression**: live and rehydrated *agree*, so this is a shared reading of
"most recent" inherited from `ingest.py`'s arrival-order application, not a restart-dependent answer.
Before this batch rehydration read only the latest `HEARTBEAT` and would have shown `armed` here —
i.e. the two paths disagreed *before* and agree *now*. The batch improved consistency. What is missing
is that the choice was never stated as a decision, and M3 shows no test would notice it being changed.

### WARNING B — the "property test" is a five-entry example table; a lookup keyed on exactly those five logs passes the whole suite

**Where**: `tests/test_cli.py`, `_REHYDRATION_ORDERINGS` +
`test_rehydrated_registry_matches_the_live_registry_for_every_ordering`.

The brief's question, answered by construction (**E3**): `max(facts, key=seq)` was replaced by a
`_cheat()` function holding a five-entry dict keyed on the exact `(seq, state)` signature of the five
parametrized logs, returning `facts[0]` — deliberately wrong — for anything else. **737 passed.**

E2 sharpens it: an implementation that is correct only when a node has ≤2 state-bearing rows also
passes everything, because no test in the suite gives one node a `HEARTBEAT`, a `CAPTURED` *and* a
`FAULT`. And M1's kill set shows the whole 737-test suite contains **exactly three logs** where two
state-bearing rows genuinely compete for the state.

Task 2.1's own definition — *"parametrize one test over all five cases — this is what makes it a
property test"* — was met literally. The remediation document's rule ("**every** ordering and **every**
report type") was not. `state` / `last_seen` / `reachable` **are** all three asserted uniformly in
every case (verified: the assertion is a `for field in (...)` loop, no case asserts less), and M1/M8/M9
confirm the test has real discriminating power on the ordering axis. It is a good example table. It is
not a property test, and the difference is exactly where CRITICAL A slipped through.

Cheapest honest close: make the parametrize table a product of `(ordering) × (state_after ∈ {0,1,2,3})`
and add one three-report case. That is a table change, not a new harness.

### WARNING C — WARNING J is closed for "no readable state row at all", not for "the newest state row is unreadable"

**Where**: `src/feeder_station/adapters/store.py:536-557` — skip granularity is per row-source, so a
node whose *newest* state-bearing row is unreadable silently falls back to its *older* readable one and
presents it as current.

Measured (three-row node: `HEARTBEAT armed` seq1, `CAPTURED state_after=2` seq2, `FAULT` seq3; the
`FAULT` row's `received_epoch` corrupted):

```
rehydration_errors=1   displayed state='fired'      (the FAULT was the node's latest word)
```

With only `HEARTBEAT armed` + a corrupt `FAULT`, the display is **`armed`** — a calm, healthy-looking
trap whose real last report was a fault. That is WARNING J's own inputs→outcome one row over: the
station is not fabricating a dataclass default any more, it is presenting a real-but-superseded report
as if it were current, and nothing per-node says so. `/health`'s `rehydration_errors` is station-wide
and `ok` stays `true`.

This is **strictly better than before** (a real report beats an invented `idle`) and it is a
deliberate, documented decision (`store.py:503-512`), which is what task 2.8 asked for. But WARNING J's
stated close was *"a state the interface can render as unknown beats a default that renders as safe"*,
and this path still renders as safe. The natural home is Batch 2b's task 2.9 (WARNING K(b), degraded
boot reporting) — per-node "some of this node's rows were unreadable" is the same information need.
Not a blocker for 2a; recorded so 2.9 picks it up rather than closing narrower than the hole.

### WARNING D — a test comment this batch made false

**Where**: `tests/test_app.py:737-741`.

```python
# No trap is even created by rehydration here (neither a heartbeat nor
# an unacknowledged capture exists for node 7 anymore) -- which is
# itself the proof: querying through the real route shows nothing left
# to attend to.
rows = TestClient(create_app(fresh_registry)).get("/api/traps").json()
assert all(row["capture"] is None for row in rows)
```

Measured at HEAD: `rows` is **not** empty — it is
`[{'node_id': 7, 'state': 'fired', ..., 'capture': None}]`. A trap *is* created now, because the
acknowledged `CAPTURED` row still supplies the winning state fact. The assertion still holds and still
has teeth (it is one of the two tests that kill M15), but the stated *proof* is now the opposite of
what the code does. This is the WARNING 14 family — "the record actively misleads the next reader" —
created fresh by this batch, in the same run that correctly fixed two other stale comments under
task 2.3.

---

## The `ARMED` → `FIRED` assertion change — verified from the fixture, not from the argument

`tests/adapters/test_store.py:186-228`, read directly:

| Row | order | `received_epoch` | payload |
|---|---|---|---|
| `HEARTBEAT` | appended first → **seq 1** | `1000.0` | `{"state": 1}` → ARMED |
| `CAPTURED` | appended second → **seq 2** | `1001.0` | `{"state_after": 2}` → FIRED |

The `CAPTURED` row is newer by **both** candidate orderings — store `seq` (2 > 1) and the reports' own
receipt times (1001.0 > 1000.0) — so the two rules that WARNING A shows are untested agree here, and
the winner is unambiguous under either. Replaying this exact log through the **live** path
(`ingest._apply_to_registry`) yields `state=fired`. Under `event-store/spec.md`'s "rebuild the last
known reported state" and `trap-commands/spec.md:14`'s S4 discipline as broadened by verify-report-2.md
(*"a CAPTURED … is a report, made by the node, about itself"*, corroborated by `radio-gateway/spec.md`'s
"FAULT and silence handling" giving a non-heartbeat uplink the power to set reported state), **`FIRED`
is the spec-correct answer for this fixture.**

**Verdict: justified. Not a blind revert.** The prior `FIRED` was right by accident (the old code let
the capture replay stamp `state` regardless of order); the new `FIRED` is right by the rule, and the
same rule produces `IDLE` for the mirror fixture. The two are not the same claim.

**Option (b) genuinely satisfied.** `test_registry_prefers_a_newer_heartbeat_over_an_older_capture`
is not a near-duplicate: different fixture (`CAPTURED` seq 1 / `HEARTBEAT state=0` seq 2), opposite
ordering, different expected answer (`IDLE`), and it also pins `trap.capture is not None` (S3 survives
losing the race). It was disclosed as already-passing against the old code — accurate, and correct for
a regression guard. The store-level layer it guards is real: the boot-level property test only ever
sees `/api/traps` through the ASGI stack.

**One caveat**: this fixture cannot distinguish `state_after`-sourced from hardcoded-`FIRED` (its
`state_after` *is* 2). That is precisely why M2 is killed only by the separate `state_after=1` test —
and why CRITICAL A was invisible.

---

## The renamed test, and the S3 invariant behind it

`test_rehydrate_does_not_resurface_an_already_acknowledged_capture` →
`test_rehydrate_still_shows_the_state_of_an_already_acknowledged_capture`, assertion changed from
"no trap exists for node 4" to `state is FIRED, capture is None`.

**Spec citation holds.** `Registry.acknowledge()` (`traps.py:240-241`) sets `trap.capture = None` and
touches nothing else — live or rehydrated. The live registry for this log shows `state=fired`
regardless of acknowledgement, so rehydration agreeing is the CRITICAL 1 invariant, not a weakening.
The old assertion conflated "the capture object was acknowledged" with "nothing was ever reported".

**The original invariant was relocated, not dropped.** Mutation M15 (restore the capture object even
when acknowledged) is killed by **two** tests: the renamed test's own `assert trap.capture is None`,
and `tests/test_app.py::test_acknowledging_a_capture_survives_a_restart_through_the_route`, which
drives the real `POST /api/traps/7/acknowledge` route, closes the store, reopens it, rehydrates, and
asserts no capture comes back. S3's "an acknowledged capture must not resurface after a restart" is
covered at both the unit and the route layer. (That second test carries the now-false comment in
WARNING D, but its assertion is sound.)

---

## WARNING J — is "unknown" honest all the way through?

Every consumer exercised against a real `None`-state trap (real `build_app`, real routes):

| Consumer | Behaviour with `state = None` | Verdict |
|---|---|---|
| `Registry.view()` (`traps.py:263`) | `"state": None` | honest |
| `GET /api/traps` | `{"node_id": 42, "state": null, "reachable": false, …}` | honest, no crash |
| `GET /health` | 200, does not read `Trap.state` at all | unaffected |
| SSE `GET /api/events` | payload is `{"type": "traps_changed", "node_id": …}` only — never carries state | unaffected |
| `static/index.html::renderTrap` | `const state = t.state != null ? t.state : "desconocido"` | distinct label |
| Export (`adapters/export.py::export_events_csv`) | reads raw `event` rows from SQLite; never touches `Trap.state` | unaffected; `POST /api/export` 200 with a null-state trap in the registry |
| Sorting | `Registry.traps()` sorts by `node_id` only; roster order `[1, 42, 43]` with null states | no odd sort |
| `store.py::_reported_state` | `trap.state is TrapState.ARMED` — identity comparison, `None` → `armed=False`, same as the old `IDLE` default | no behaviour change |

No coercion back to a default anywhere (M5a and M5b both killed). `mypy src` clean, so the
`TrapState | None` widening is type-checked at every use.

**Is "unknown" visibly different from "idle" for an operator?** Yes, and better than a label alone:
`state = None` is only reachable when no state-bearing row was readable, and `last_seen` is set
*only* by `restore_reported_state` — so `state: null` always implies `last_seen: null`, hence
`reachable: false`, hence the UI also renders the
`⚠ SIN CONTACTO — este estado puede estar desactualizado` badge and the `no-contact` card styling. The
chip reads `TRAMPA 7 · DESCONOCIDO` (uppercased by `.state`'s CSS) instead of `TRAMPA 7 · IDLE`. The
badge does not *distinguish* unknown from any other unreachable trap, but the word does, and it is the
largest text on the card.

**Does anything still fabricate a state?** Every `Trap`-creating path was checked:
`_set_pending` (`command_arm`/`command_disarm`/`command_set_targets`), `restore_capture`,
`restore_desired_tags`, `restore_heartbeat_fields`, `restore_reported_state`. All use
`setdefault(node_id, Trap(node_id=node_id))`, which now yields `state=None`. Measured end to end:
`POST /api/traps/42/arm` on a never-heard node →
`{"node_id": 42, "state": null, "pending_command": "arm", "last_seen": null, "reachable": false}` →
UI shows `TRAMPA 42 · DESCONOCIDO`. Honest: the station has genuinely never heard from it. No path
fabricates a state any more. (Whether that node survives a restart is CRITICAL 6 / Batch 2b — not
assessed here.)

---

## Regressions against Batch 1 — none

| Batch 1 property | Pinned by | Status |
|---|---|---|
| `rehydrate()` totality (`except Exception`, not a type whitelist) | M12 killed | intact |
| Roster query inside the guard | M13 killed | intact |
| `rehydration_errors` reset per pass | M11 killed | intact |
| `payloads._VALID_STATE_VALUES = range(len(TrapState))` | M14 killed; measured `range(0, 4)`, `len(TrapState) == 4` | intact |
| Wire byte `4` rejected | `decode_heartbeat` → `PayloadError: HEARTBEAT state byte 4 has no known TrapState (expected 0-3)`; `decode_captured` → the same for `state_after` | intact — **nullable `state` did not widen the range**, which is exactly why nullable was chosen over a fifth `TrapState` member |

Totality was re-probed on the **new** code paths specifically (corrupt `FAULT` rows: huge/NaN
`received_epoch`, non-object `fields`, invalid JSON; corrupt `CAPTURED`: `state_after` 7 / -1 / 10⁹,
huge `received_epoch`, NULL tag; `HEARTBEAT state=4`). **No probe raised.** Every one either counted a
skip or degraded honestly. `_rehydrate_heartbeat`'s validate-then-mutate atomicity is real and pinned
(M7).

---

## SUGGESTION

1. **`trap_state_from_wire` silently wraps on a negative index.** `store.py:236-240` does
   `_WIRE_STATE_ORDER[value]`; `trap_state_from_wire(-1)` returns `TrapState.FAULT` and `(-3)` returns
   `ARMED`, with no raise. Unreachable from the wire (`_require_valid_state` rejects anything outside
   0-3, and payload bytes cannot be negative) but **reachable from a corrupt `fields` JSON cell**,
   which is the exact threat model Batch 1 exists for: a corrupt row becomes a plausible state instead
   of a counted skip. Pre-existing and unchanged by this batch, but its blast radius grew — `state_after`
   now feeds it too. One `if not 0 <= value < len(_WIRE_STATE_ORDER)` closes it.
2. **Mixed-language UI in the state chip.** `index.html:131` renders `"desconocido"` (Spanish) while
   `idle`/`armed`/`fired`/`fault` remain English (`traps.py:25-29` enum values, rendered raw at
   `index.html:139`). Facts confirmed: before this batch the chip was uniformly English; the project's
   convention — every other label on the page (`sin dato`, `nunca`, `ninguno`, `SIN CONTACTO`,
   `Batería`, `Últ. contacto`) and the operator audience — makes the *new* string right and the other
   four the pre-existing gap. `operator-ui/spec.md` states no language requirement, so this is
   convention, not spec. **Rated SUGGESTION, not WARNING**: the mix is an inconsistency, not a
   comprehension failure — "DESCONOCIDO" is more readable to the intended operator than "IDLE" was.
   The honest close is a four-entry label map in the same function (~4 lines), which would also retire
   Appendix A's WARNING 3.
3. `tests/test_app.py:737-741`'s comment — see WARNING D (listed there because it is a false statement,
   not a style point; the fix is one comment).
4. Apply record says "4 of 5 parametrized cases failed" in RED; measured 3 of 5. Correct the record.
5. `remediation-tasks.md` task 2.1's ordering labels are inverted relative to `verify-report-2.md`'s
   Part 1.2 table. **The report is correct and the task prose is wrong** — independently confirmed
   three ways: the report's table is a measurement (A = capture-old/heartbeat-new = `AGREE`); CRITICAL 1's
   own prose table lists "HB 6h old, `CAPTURED` 2 min ago" among the *broken* rows; and the pre-existing
   already-passing test `test_an_older_capture_does_not_regress_a_newer_heartbeats_state` is
   capture-old/heartbeat-new. The implementer's decision to trust the report was right, and flagging it
   rather than silently editing the task file was right — but the file still contains the wrong text for
   the next reader. Add a one-line correction note in place, as Batch 1 did for its own wrong assumptions.
6. **The five implemented cases genuinely cover the four measured scenarios plus the unmeasured
   mirror** — `capture_old_heartbeat_new` (A), `heartbeat_old_capture_new` (B),
   `capture_alone_no_heartbeat_ever` (C), `heartbeat_then_fault` (D), `fault_then_newer_heartbeat`
   (the mirror). Five distinct logs, no duplication, no scenario covered twice. Two minor notes:
   (a) the live registry in the property test hardcodes `silence_after=timedelta(minutes=15)`, which
   matches `config.DEFAULT_SILENCE_AFTER` only by coincidence — if that default ever changes, the
   `reachable` comparison silently compares two differently-configured registries; source it from
   `StationConfig()` instead. (b) `_rehydrate_capture` is mutate-then-validate (the capture object is
   restored before `state_after` can raise) while `_rehydrate_heartbeat` is validate-then-mutate. Both
   choices are defensible and the asymmetry is deliberate, but only the heartbeat half is documented.

---

## Spec compliance movement (Batch 2a scope only)

| Capability | Scenario | Before | After | Evidence |
|---|---|---|---|---|
| event-store | registry reflects last known state after restart | ❌ VIOLATED | ⚠️ **PARTIAL** | Orderings A-E agree (measured); `state_after ≠ 2` diverges live-vs-rehydrated — CRITICAL A. CRITICAL 6's half untouched (Batch 2b) |
| radio-gateway | FAULT updates reported state to fault | ⚠️ PARTIAL | ✅ **MET** | Case D measured `fault`/`fault` across a real restart; `_rehydrate_fault` replays it, M4 pins it |
| trap-commands | displayed state is the trap's own most recent report (S4) | ⚠️ PARTIAL | ⚠️ **PARTIAL** | Holds by arrival order; "most recent" is arrival, not node time — WARNING A |
| operator-ui | capture from hours ago is still visible / unreachable trap is marked | ⚠️ PARTIAL | ✅ **MET** for the state chip beside it in cases A-E; ⚠️ re-broken for `state_after ≠ 2` (capture card + `idle` chip) — CRITICAL A |

---

## Final verdict

**FAIL** — 1 CRITICAL, 4 WARNING, 6 SUGGESTION.

This is the best-executed round of the four so far, and that is worth saying plainly: the mutation
evidence is real and reproduced exactly, every RED claim replays, every commit is independently green,
the Batch 1 guards are intact and re-pinned, WARNING J is honest end to end, the `ARMED`→`FIRED` edit
is justified from the fixture rather than argued, and the scope discipline (2.5-2.7/2.9/2.10
untouched) held. The four orderings that CRITICAL 1 was written about are genuinely fixed.

And it is the fourth recurrence of the same diagnosis, one axis over. The rule was *"every ordering
**and every report type**"*; the tests vary the ordering and hold the report **content** fixed at the
one value where the live and rehydrated paths coincide. The single new test that does vary it
(`state_after=1`) asserts the rehydrated answer without ever asking what the live path says about the
same log — and the answer is different. An implementation that hardcodes the five test logs passes all
737 tests, which is the mechanical proof that the suite pins examples, not the rule.

**May Batch 2a be pushed? Not as a CRITICAL 1 closure.** The fix is small and belongs in this stack as
a fifth commit: make the live `Registry.captured()` read `state_after` (closing WARNING 7 on both sides
at once), add `state_after ∈ {0,1,3}` as a second parametrize axis, and correct
`tests/test_app.py:737`'s comment. If the team would rather push the four commits now, they must land
with CRITICAL A recorded as open and CRITICAL 1 marked PARTIAL — not closed — in `remediation-tasks.md`
and in the Spec Compliance Matrix.

**May Batch 2b start? Yes, now.** It touches `_known_node_ids`, `design.md:288`, `/health`'s
degraded-boot reporting and `caplog` pins — no overlap with the state race, no dependency on CRITICAL A
either way. Carry WARNING C into task 2.9's scope: "one bad row vs a lost registry" and "this node's
newest report was unreadable" are the same information need, and 2.9 is where the per-node half belongs.
