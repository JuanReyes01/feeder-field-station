# Apply progress — Batch 2a (CRITICAL 1 + WARNING J only)

**Change**: `field-station-p4`
**Source**: `openspec/changes/field-station-p4/remediation-tasks.md`, Batch 2 section, tasks
**2.1, 2.2, 2.3, 2.4, 2.8 only**. Tasks 2.5-2.7, 2.9, 2.10 (CRITICAL 6, WARNING K) are **Batch 2b** —
not touched by this run.
**Mode**: Strict TDD (RED confirmed-failing before every GREEN, evidence table below)
**Starting commit**: `7901ac3` (docs(sdd): carry batch-1 round-2 findings into batch 2, schedule
batch 5)
**Scope discipline**: `_known_node_ids`'s roster derivation, `design.md`, `/health`'s degraded-boot
reporting and `caplog` pins were **not** touched — all Batch 2b.

## Why 2.1-2.4 and 2.8 are one unit

Task 2.2 has to decide the state-race skip granularity (per-step vs per-node) to restructure
`rehydrate()` at all. WARNING J (task 2.8) is a direct consequence of that same decision — a node
whose one state-bearing row is unreadable must not fall back to a fabricated default. Deciding
granularity once, with both requirements in view, is what the batch split note in
`remediation-tasks.md` asks for; deciding it twice is how it drifts.

## What "the rule, not the example" means here (verify-report-2.md's own diagnosis)

Not *"this ordering rehydrates correctly"* but **the rehydrated registry equals the live registry,
for every ordering and every report type**. `HEARTBEAT`, `CAPTURED` and `FAULT` are all reports the
node makes about itself (radio-gateway/spec.md; `ingest.py::_apply_to_registry` already treats them
that way live); rehydration must answer the same question the same way, not treat the heartbeat as
the sole source of truth for `state`.

## Letter-labeling discrepancy, flagged rather than silently resolved

`remediation-tasks.md` task 2.1's own prose labels the orderings backwards relative to
`verify-report-2.md`'s actual Part 1.2 table:

- Task 2.1 text: *"HEARTBEAT 6h old, CAPTURED 2min new (report's ordering A — already passing)"*.
- `verify-report-2.md` Part 1.2, actually: **ordering A** is *"capture 6h OLD, heartbeat 2min NEW"*
  (`AGREE`, already passing) and **ordering B** (*"heartbeat 6h OLD, capture 2min NEW"*) is the one
  that `DIVERGES`. The pre-existing, already-passing test in `test_cli.py`
  (`test_an_older_capture_does_not_regress_a_newer_heartbeats_state`) is CAPTURE-old/HEARTBEAT-new —
  confirming the report's own table, not the task text's restatement of it.

Resolved by trusting the report's own measured table (as this run's brief explicitly asked, "especially
its Part 1.2 four-ordering table") over the task list's restatement, and by covering **both**
directions plus the extra un-reported FAULT/HEARTBEAT mirror regardless of which letter anyone
assigns them — the parametrized property test (2.1) is keyed by descriptive `case_id`, not by letter,
so this ambiguity cannot leak into the test suite itself.

## Design decisions

### 1. `rehydrate()`'s four-step restructuring (task 2.2)

Replaced the old three-step `_rehydrate_heartbeat` → `_rehydrate_capture` →
`_rehydrate_desired_command` sequence (where `state`/`last_seen` were exclusively the heartbeat
step's business) with:

- **(a) State race** — `_rehydrate_heartbeat`, `_rehydrate_capture` and the new `_rehydrate_fault`
  each return an optional `_ReportedFact(seq, state, at)` candidate for their own row type. Whichever
  candidate has the highest store `seq` wins and is applied via the new
  `Registry.restore_reported_state`. `CAPTURED`'s candidate reads `state_after` (WARNING 7 — decoded
  then discarded until now); `FAULT`'s candidate is always `TrapState.FAULT` (the message type is the
  fact, no payload field needed).
- **(b) `battery_mv`/`targets_version`** — restored from the latest `HEARTBEAT` regardless of whether
  it won (a), via the new `Registry.restore_heartbeat_fields`, since those fields exist on no other
  report type.
- **(c) The capture object** — `_rehydrate_capture`, unchanged in effect, still gated on
  acknowledgement, independent of which report won the state race.
- **(d) The desired command** — `_rehydrate_desired_command`, unchanged, now explicitly documented as
  needing to run *last*, after (a)/(b) settle the node's final `state`/`targets_version` (DD15).

Skip granularity: **kept per row-source** (not per node), deliberately. `_rehydrate_heartbeat` is
atomic per row — the state candidate is computed first (may raise on an out-of-range byte) and only
once that succeeds are `battery_mv`/`targets_version` restored, so one corrupt row fails as one unit,
matching the pre-existing `test_rehydrate_skips_and_counts_an_unreadable_row_without_blocking_other_
nodes` test's own invariant (an unreadable row with no other source for that node leaves the node
absent, not half-created). Considered switching to per-node skip granularity (drop the whole node on
any unreadable row) — rejected: `battery_mv`/`last_seen`/`targets_version` already go honestly `null`
on partial failure elsewhere in this method, and WARNING J's own text praises that partial-honesty
shape rather than asking for it to be replaced with an all-or-nothing per-node drop.

### 2. `Trap.state: TrapState | None = None` (task 2.8, WARNING J)

Chose **nullable `state`**, not a fifth `TrapState.UNKNOWN` member. `adapters/store.py::
_WIRE_STATE_ORDER` and `payloads.py::_VALID_STATE_VALUES = range(len(TrapState))` derive the valid
wire-byte range directly from `len(TrapState)` (WARNING B, Batch 1) — adding a member would silently
widen that range to accept a wire byte (4) the protocol has never sent, undoing Batch 1's own fix.
Nullable also matches the shape every neighbouring field already has (`battery_mv`, `targets_version`,
`last_seen` are all `X | None = None`); `state` was the one field that invented a value instead. Cost:
`Registry.view()` gained one `if trap.state is not None else None` branch; nothing else in `src/`
touches `.state.value` directly (checked by hand across `app.py`, `store.py`,
`traps.py`) — `_reported_state()`'s `trap.state is TrapState.ARMED` already tolerates `None` via
identity comparison, no code change needed there. UI: `static/index.html`'s `renderTrap` renders
`"desconocido"` (Spanish, matching the project's operator-facing-copy convention) when `t.state` is
`null`, alongside the existing English state words it does not touch (out of scope — a pre-existing
gap, not introduced or worsened here).

## Mutation-proved, not merely observed

Both mutations applied **in the real package root** (confirmed via `uv run python -c "import
feeder_station.adapters.store as m; print(m.__file__)"` → resolves to this repo's own `src/`, not a
worktree), each via `Edit`, each reverted via `Edit` back to the original text, `git status
--porcelain` confirmed empty after every revert.

**Mutation 1** — winning-row selection changed from `max(facts, key=lambda fact: fact.seq)` to
`facts[0]` (always the heartbeat's own candidate, ignoring order):

```
FAILED tests/test_cli.py::...for_every_ordering[heartbeat_old_capture_new]
FAILED tests/test_cli.py::...for_every_ordering[heartbeat_then_fault]
2 failed, 3 passed
```

**Mutation 2** — a winning `CAPTURED` row's fact changed from `trap_state_from_wire(state_after)` to
a hardcoded `TrapState.FIRED`:

```
35 passed  <- the mutation SURVIVED the suite as it stood at that point
```

This survival was real, not a false negative: every existing test's `state_after` happened to be `2`
(`FIRED`) already, so a hardcode was indistinguishable from reading the field. Added
`test_a_winning_captured_row_reads_its_own_state_after_not_a_hardcoded_fired` (`tests/adapters/
test_store.py`) with `state_after=1` (`ARMED`) specifically to close this gap — re-run against the
same mutation:

```
FAILED tests/adapters/test_store.py::test_a_winning_captured_row_reads_its_own_state_after_not_a_hardcoded_fired
1 failed, 16 deselected
```

Mutation killed once the gap was closed. Both mutations reverted; full suite green afterward (see
Verification below).

## Task 2.4 — which option, and why

**Option (b)**: kept `test_registry_reflects_last_known_state_after_restart` as a narrow store-level
regression guard, *in addition to* 2.1's boot-level property test, and added a sibling case in the
same file (`test_registry_prefers_a_newer_heartbeat_over_an_older_capture`) proving the mirror
direction. Reasoning: the store-level test exercises `Store.rehydrate()` directly, a layer the
boot-level property test (which only sees `/api/traps` through the full ASGI stack) does not reach on
its own — losing it would lose that layer of coverage, not just a duplicate assertion. The assertion
changed from `TrapState.ARMED` to `TrapState.FIRED` — not a blind revert to the pre-CRITICAL-1-fix
`FIRED`: the CAPTURED row in this fixture is the *newer* of the two rows (`seq=2` vs the heartbeat's
`seq=1`), so under the new "most recent report wins" rule its own `state_after` (`2` → `FIRED`) is the
correct answer, sourced through the same winning-row logic 2.1's property test proves generally, not
hardcoded back to what happened to be right before.

## Existing tests whose behaviour shifted, with spec citations

1. **`test_rehydrate_skips_and_counts_an_unreadable_row_without_blocking_other_nodes`** —
   assertion unchanged (`rehydration_errors == 1`; node 7 absent; node 9 unaffected). Verified this
   still holds under the new four-step design specifically *because* `_rehydrate_heartbeat` stayed
   atomic per row (see Design decision 1) — an earlier draft that restored `battery_mv`/
   `targets_version` *before* validating `state` broke this test by creating a half-populated node 7;
   reordered to validate-then-mutate instead of loosening the test.
2. **`test_rehydrate_does_not_resurface_an_already_acknowledged_capture`** → renamed
   `test_rehydrate_still_shows_the_state_of_an_already_acknowledged_capture`, assertion changed from
   "no trap exists at all for node 4" to `trap.state is TrapState.FIRED`, `trap.capture is None`.
   **Spec citation**: `verify-report-2.md` CRITICAL 1 — *"S4's fact is the trap's most recent report —
   and a CAPTURED ... is a report, made by the node, about itself"*. The old assertion conflated "the
   capture object is acknowledged" with "no state fact was ever reported" — but
   `Registry.acknowledge()` only ever sets `trap.capture = None`; it does not touch `state`, live or
   rehydrated. The live registry, replayed through `ingest.py::_apply_to_registry` for this exact log,
   shows `state=fired` whether or not the operator later acknowledged it — rehydration now agrees,
   which is the CRITICAL 1 invariant this whole batch exists to establish.

No other existing assertion changed. `git diff --numstat` for `tests/adapters/test_store.py` and
`tests/test_cli.py` shows every other line as pure addition.

## `rg` cross-check: nothing else reads `.state.value` or assumes `TrapState.IDLE` as a default

```
rg -n '\.state\b' src/feeder_station --type py
```

Only `domain/traps.py::view()` (updated) and `adapters/store.py::_reported_state()` (tolerates `None`
via identity comparison, no change needed) touch `Trap.state` outside the methods that set it
directly. No test in the suite constructs `Trap(node_id=...)` directly or asserts the bare default —
checked before making the change, not after.

## Diff size — over budget, flagged rather than pressed through silently

`git diff --stat 7901ac3..HEAD` (see full report to orchestrator for the exact command output):

```
 openspec/changes/field-station-p4/remediation-tasks.md |  10 +-
 src/feeder_station/adapters/store.py                   | 182 +++++++++-----
 src/feeder_station/domain/traps.py                      |  71 ++++++-
 src/feeder_station/static/index.html                    |   7 +-
 tests/adapters/test_store.py                            | 136 ++++++++--
 tests/test_cli.py                                       | 204 ++++++++++++-
 6 files changed, 536 insertions(+), 74 deletions(-)
```

**610 changed lines, well above the stated 350-line target** (plus this doc). Composition:
`traps.py`+`index.html` (WARNING J domain foundation) ≈ 78 lines; `store.py`+`test_store.py`
(CRITICAL 1 core + task 2.4's tests) ≈ 318 lines; `test_cli.py` (task 2.1's five-case property test +
task 2.8's boot-level RED test) ≈ 204 lines. The property test's own parametrize table (5 scenarios ×
~5 lines) and its docstring, and the two boot-level test docstrings citing S3/S4/S11/CRITICAL 1/
WARNING J per this project's own commenting convention, account for most of the excess over the
`store.py`-only forecast. Not trimmed further: the batch's own instructions were explicit that a test
whose scenario coverage or citation trail gets cut to hit a budget defeats the purpose of this
remediation existing in batches at all. Flagged here, per instruction, rather than pressed through
silently; the orchestrator/reviewer should decide whether this lands as one batch (as delivered,
4 independently-green stacked commits below) or is split further before merge.

## TDD Cycle Evidence

| Task | RED (confirmed failing, reason) | GREEN | REFACTOR |
|---|---|---|---|
| 2.1 | `tests/test_cli.py::test_rehydrated_registry_matches_the_live_registry_for_every_ordering` — 4 of 5 parametrized cases failed before the fix (`heartbeat_old_capture_new`, `capture_alone_no_heartbeat_ever`, `heartbeat_then_fault` on `state`/`last_seen`/`reachable`; `fault_then_newer_heartbeat` happened to already pass since the old code always trusted the latest heartbeat regardless of order) | Closed by 2.2's `rehydrate()` restructuring — all 5 cases pass | n/a |
| 2.2 | (shares 2.1's RED, plus `test_a_node_with_no_readable_state_source_shows_unknown_not_idle`) | `domain/traps.py::restore_reported_state`/`restore_heartbeat_fields` + `adapters/store.py`'s four-step `rehydrate()` (`_rehydrate_heartbeat`/`_rehydrate_capture`/new `_rehydrate_fault` each return an optional `_ReportedFact`, `max(..., key=seq)` wins) | n/a |
| 2.3 | n/a (docstring/comment only) | Stale "never `captured()`, always `restore_capture`... heartbeat replay's fact alone" comment rewritten in both `store.py::_rehydrate_capture` and `traps.py::restore_capture`'s docstrings to describe the winning-row race instead | Done alongside 2.2 |
| 2.4 | `tests/adapters/test_store.py::test_registry_reflects_last_known_state_after_restart` edited to assert `TrapState.FIRED`; ran and failed (`assert <TrapState.ARMED> is <TrapState.FIRED>`) against the pre-2.2 code | Closed by 2.2. Sibling `test_registry_prefers_a_newer_heartbeat_over_an_older_capture` added, confirmed already-passing (regression guard, ordering the old code got right) | n/a |
| 2.8 | `tests/test_cli.py::test_a_node_with_no_readable_state_source_shows_unknown_not_idle` ran and failed (`assert 'idle' is None`) against the pre-fix code | Closed by `Trap.state: TrapState | None = None` (`domain/traps.py`) + `view()`'s `None`-aware branch + `static/index.html`'s `"desconocido"` label | n/a |

## Files changed

| File | Action | What was done |
|---|---|---|
| `src/feeder_station/domain/traps.py` | Modified | `Trap.state` → `TrapState \| None = None`; new `Registry.restore_reported_state`/`restore_heartbeat_fields`; `view()` renders `state: null` instead of a fabricated value; `restore_capture`'s docstring corrected |
| `src/feeder_station/static/index.html` | Modified | `renderTrap` shows `"desconocido"` when `t.state` is `null`, leaving the existing (untranslated) state words for the other four untouched |
| `src/feeder_station/adapters/store.py` | Modified | `rehydrate()` restructured into the four-step (a)/(b)/(c)/(d) design above; new `_ReportedFact` dataclass; new `_rehydrate_fault`; `_rehydrate_heartbeat`/`_rehydrate_capture` now each return a state-race candidate as well as doing their existing job |
| `tests/adapters/test_store.py` | Modified | Task 2.4's edited assertion + sibling ordering test; acknowledged-capture test's assertion corrected per CRITICAL 1; new `state_after`-not-hardcoded mutation-kill test |
| `tests/test_cli.py` | Modified | New five-case parametrized property test (task 2.1); new WARNING J boot-level RED test (task 2.8) |
| `openspec/changes/field-station-p4/remediation-tasks.md` | Modified | Tasks 2.1, 2.2, 2.3, 2.4, 2.8 ticked `[x]`. 2.5-2.7, 2.9, 2.10 left unticked (Batch 2b) |

## Commits (4, stacked on `main`)

1. WARNING J domain foundation — `domain/traps.py` + `static/index.html`. Verified independently green
   (full suite run with `store.py`/`test_store.py`/`test_cli.py` stashed back to baseline: 729 passed).
2. CRITICAL 1 core — `adapters/store.py` + `tests/adapters/test_store.py` (tasks 2.2, 2.3, 2.4).
3. CRITICAL 1 coverage — `tests/test_cli.py` additions only (tasks 2.1, 2.8's boot-level proof),
   test-only, no production code. Verified the *original* `test_cli.py` already passes against commits
   1+2 alone (13 passed) before adding these.
4. Docs — `remediation-tasks.md` ticks + this file.

## Verification (Batch 2a's own CRITICAL-1 half, run after the final commit)

`uv run pytest`: 737 passed (729 baseline + 8 new: 5 parametrized `test_rehydrated_registry_matches_
the_live_registry_for_every_ordering` cases, 1 `test_a_node_with_no_readable_state_source_shows_
unknown_not_idle`, 1 `test_registry_prefers_a_newer_heartbeat_over_an_older_capture`, 1
`test_a_winning_captured_row_reads_its_own_state_after_not_a_hardcoded_fired` — `test_rehydrate_
does_not_resurface_an_already_acknowledged_capture` was renamed in place, not added).

`uv run ruff check .`: All checks passed (exit 0).

`uv run ruff format --check .`: exit 1, same 6 files as baseline `7901ac3`/`504d18e`
(`design.md`, `payloads.py`, `test_ingest.py`, `test_serial_gateway.py`, `test_events.py`,
`test_traps.py`), zero new drift — confirmed via direct comparison, not assumed.

`uv run mypy src`: Success: no issues found in 22 source files (exit 0).

**verify-report-2.md Part 1.2's own four-ordering repro, re-run by hand, plus the fifth (un-reported)
ordering task 2.1 adds**:

```
A. capture 6h OLD, heartbeat 2min NEW   (the ordering the first report named)
     state      live='idle'    rehydrated='idle'    AGREE
     last_seen  AGREE
     reachable  AGREE

B. heartbeat 6h OLD, capture 2min NEW   (the MIRROR ordering)
     state      live='fired'   rehydrated='fired'   AGREE   <- was DIVERGES
     last_seen  AGREE
     reachable  AGREE

C. capture 30s ago, no heartbeat at all
     state      live='fired'   rehydrated='fired'   AGREE   <- was DIVERGES
     last_seen  AGREE
     reachable  AGREE

D. heartbeat, then FAULT (the node's own latest report)
     state      live='fault'   rehydrated='fault'   AGREE   <- was DIVERGES
     last_seen  AGREE
     reachable  AGREE

E. FAULT, then a newer HEARTBEAT reporting a different state (un-reported mirror of D)
     state      live='armed'   rehydrated='armed'   AGREE
     last_seen  AGREE
     reachable  AGREE
```

All four of the report's own scenarios now read `AGREE`; the fifth (E), which the report never
measured, also agrees.

**Scope check**: CRITICAL 6 (never-heard-node roster survival) was **not** re-verified — Batch 2b's
subject matter, not touched by this run. `_known_node_ids`, `design.md` and `/health`'s
`rehydration_errors`-vs-degraded-boot distinction (WARNING K) also untouched, as instructed.

## Status

5/5 Batch 2a tasks complete (2.1, 2.2, 2.3, 2.4, 2.8). CRITICAL 1 is closed for the scope this batch
covers (all five rehydration orderings agree live-vs-rehydrated). WARNING J is closed (an unreadable
state-bearing row now renders `state: null` / "desconocido", never a fabricated `"idle"`). CRITICAL 6
and WARNING K (Batch 2b: tasks 2.5-2.7, 2.9, 2.10) remain untouched and still broken, as expected —
this run did not read or fix them. CRITICAL 3, 4, 5 and I (Batches 3-5) also remain untouched.
