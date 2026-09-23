# Verification Report — `field-station-p4`, Batch 1 only (CRITICAL 2)

**Diff under review**: `504d18e..HEAD` (`3f53b11`), 4 commits
**Artifact store**: openspec
**Mode**: Strict TDD
**Scope**: Batch 1 (tasks 1.1-1.6) / CRITICAL 2 only. CRITICAL 1, 3, 4, 5, 6 are other batches' work,
were confirmed untouched by this diff, and are **not** reported as findings here.
**Method**: every claim re-derived from the repository. Implementer's report treated as a hypothesis.
Mutation testing used to measure what the new tests actually pin.

---

## Verdict

**FAIL** — 1 CRITICAL, 3 WARNING, 4 SUGGESTION.

CRITICAL 2 **as literally reported** is genuinely fixed: the report's own repro now boots.
CRITICAL 2's **rule** — *"a boot that cannot read one row must still be a boot"*, the phrasing Batch 1
chose for itself at `remediation-tasks.md:101-103` — is **not** met. `rehydrate()` still raises, and
still produces the identical crash loop, for three reproducible classes of unreadable row. This is the
same "fixed against the failing example rather than against the rule" pattern the whole remediation
exists to stop, recurring inside the batch written to stop it.

**Batch 2 may NOT start** until CRITICAL A is closed — see *Gate decision* at the end for the
hard-sequencing argument.

---

## Part 0 — Gates (each exit status captured directly; nothing piped)

| Gate | Command | Exit | Result |
|---|---|---|---|
| Tests | `uv run pytest` | **0** | 218 passed, 2 warnings |
| Lint | `uv run ruff check .` | **0** | All checks passed |
| Format | `uv run ruff format --check .` | **1** | 6 files would be reformatted |
| Types | `uv run mypy src` | **0** | no issues in 22 source files |

**Baseline comparison for the format gate — implementer's claim VERIFIED TRUE.** `504d18e` checked
out into a throwaway worktree and re-run: exit 1, same 6 files, same line numbers
(`design.md:396:21`, `payloads.py:63:28`, `test_ingest.py:95:10`, `test_serial_gateway.py:63:27`,
`test_events.py:41:10`, `test_traps.py:27:74`). `diff` of the two full outputs, minus the trailing
count line, is **byte-identical**. **Zero new formatting drift.**

**Test-count claim VERIFIED.** Baseline `504d18e`: 208 passed. HEAD: 218 passed. +10, exactly as
claimed (8 `test_payloads.py`, 1 `test_store.py`, 1 `test_cli.py`).

**Per-commit isolation claim VERIFIED.** Each intermediate tree checked out into the real package
root (the venv uses an editable install, so worktree-only runs are meaningless — see *Method note*):
`fca494b` → 216 passed, exit 0; `77fad51` → 217 passed, exit 0; `HEAD` → 218 passed, exit 0.

**Diffstat claim VERIFIED.** `git diff --stat 504d18e..c150952` = 7 files, 241 insertions,
11 deletions — exactly the figures in `apply-progress-batch1.md:23`.

---

## Part 1 — CRITICAL 2's own repro, re-run from scratch

Script: seed a store, boot through `cli.build_app` + real `TestClient` + real ASGI lifespan.

```
STEP 1 — does interpret() still accept a state=7 HEARTBEAT?
  REJECTED by guard 1: PayloadError: HEARTBEAT state byte 7 has no known TrapState (expected 0-3)

STEP 2 — bad-state row written directly to the store, then boot:
  state=7 row: BOOTED, /health 200, rehydration_errors=1
```

Both guards behave as advertised **for the reported input**. The infinite crash loop
(`app.py:228` → uvicorn exit → `Restart=always` → same row) is closed for a `state`/`state_after`
byte. Guard 1 additionally prevents the row from ever being written through the live path.

---

## Part 2 — Findings

### CRITICAL A — `rehydrate()` is still not total: three non-`ValueError` escape hatches reproduce CRITICAL 2's exact crash loop

**Where**: `src/feeder_station/adapters/store.py:494-510` (the guard), `:205`
(`_epoch_to_datetime`), `:521` (`heartbeat.fields.get`), `src/feeder_station/app.py:227-228`
(the lifespan call, itself unguarded).

The guard is `except ValueError`. `PayloadError` **does** subclass `ValueError`
(`domain/payloads.py:50`, claim 5 verified true), and `json.JSONDecodeError` and
`bytes.fromhex` also raise `ValueError` — so the guard works for those. It works for **nothing else**,
and the three replay steps demonstrably raise other things.

Reproduced end to end through `cli.build_app` + `TestClient`, one corrupt row each:

| Probe | Corruption | Outcome | Raised at | `isinstance(ValueError)` |
|---|---|---|---|---|
| P1 | `received_epoch` = `1e20` (REAL column) | **BOOT FAILED** `OverflowError: timestamp out of range for platform time_t` | `store.py:205` | **False** |
| P2 | `fields` = `'null'` (valid JSON, not an object) | **BOOT FAILED** `AttributeError: 'NoneType' object has no attribute 'get'` | `store.py:521` | **False** |
| P3 | `fields` cell storage class flipped TEXT→INTEGER | **BOOT FAILED** `AttributeError: 'int' object has no attribute 'get'` | `store.py:521` | **False** |
| P4 | `fields` = `'{broken'` (control) | BOOTED, `/health` 200, `rehydration_errors=1` | — | True (caught) |

**Concrete failure scenario (P1, the realistic one).** The station runs on a Raspberry Pi
(`docs/deploy.md:5`, `docs/requirements.md:72`), i.e. SD-card storage, and S7
(`docs/requirements.md:38`) is explicitly about repeated unclean power cuts. `received_epoch` is an
IEEE-754 double holding ~`1.75e9`. One flipped exponent bit in that 8-byte cell — the same single-bit
media corruption class the report used to justify CRITICAL 2's realism, just on the card instead of
the air — yields a value `datetime.fromtimestamp` refuses.

Inputs → wrong outcome:
1. Any stored row's `received_epoch` double takes one bit flip on the SD card.
2. Station reboots (power cut — the S7 case).
3. `store.rehydrate()` (`app.py:228`) → `_rehydrate_heartbeat` → `_epoch_to_datetime`
   (`store.py:205`) → `OverflowError`.
4. `except ValueError` does not match. The exception leaves `rehydrate()`, leaves the lifespan.
5. uvicorn exits. `deploy/feeder-station.service`'s `Restart=always` restarts after 2 s. Same row.
6. **Infinite crash loop, no `/health`, no UI, unrecoverable without hand-editing SQLite in the
   field.** Byte-for-byte the failure CRITICAL 2 names, one column over.

**Why this is CRITICAL and not a WARNING.** Batch 1 wrote its own acceptance criterion at
`remediation-tasks.md:101-103`: *"rehydration must be total (skip-and-count, never raise)"*, and task
1.4 says *"skips and counts an unreadable row rather than propagating"*. It is not total. It skips and
counts one exception family and propagates the rest, out of the same method, into the same unguarded
lifespan, with the same consequence. Task 1.3's own docstring even names the intended scope —
*"any future row the decode boundary did not anticipate"* — which is exactly the case that still
bricks the boot.

**Additional un-reproduced but real surface (analysis, flagged as lower confidence — I could not
force SQLite to surface it on demand):** the loop header is **outside** any guard.
`store.py:494` `for node_id in self._known_node_ids():` and `_known_node_ids()` itself
(`store.py:512-514`, a `SELECT` plus row unpacking) sit before the `try` at `:500`. A
`sqlite3.DatabaseError` ("database disk image is malformed") from the roster query, or from the
`latest()` query inside a step, is likewise not a `ValueError` and would escape. I corrupted a
mid-file page and SQLite happened not to fault on the pages `rehydrate()` read (BOOTED,
`rehydration_errors=0`), so I am **not** claiming an empirical repro for this one — only that the
code path is unguarded by inspection.

**Minimum bar to close**: the guard must cover the exception surface the steps actually have
(`except Exception` with the same skip-and-count, or an explicit tuple that at least includes
`OverflowError`, `TypeError`, `AttributeError`, `OSError` and `sqlite3.Error`), and the roster query
plus loop iteration must be inside it. A regression test must drive at least one non-`ValueError`
corrupt row through `cli.build_app`, not only the `state`-byte one.

---

### WARNING B — the new tests prove the examples, not the rule; both directions of the boundary are unpinned

**Where**: `tests/domain/test_payloads.py:190-240`, `src/feeder_station/domain/payloads.py:84-89`.

Two mutations run against the **real** package, full suite each time:

| Mutation | Meaning | Suite |
|---|---|---|
| `_VALID_STATE_VALUES = range(4)` → `range(3)` | rejects valid `state=3` (`TrapState.FAULT`) | **218 passed, exit 0** |
| `if value not in _VALID_STATE_VALUES:` → `if value in {4, 7, 130, 255}:` | accepts every out-of-range byte except the four the tests happen to use | **218 passed, exit 0** |

The second mutation is the adversarial implementation task 1.1 was written to exclude, and it
survives untouched. Parametrizing over four values instead of one is still four examples; the rule
*"every byte outside 0-3"* is not encoded anywhere a test can see.

The first mutation matters more in the field. **No test anywhere asserts that a valid `state=2`
(`FIRED`) or `state=3` (`FAULT`) heartbeat decodes successfully.** The valid range is therefore
pinned on the reject side only.

**Answer to claim 7 (off-by-one): the range is correct today.** Verified directly —
`TrapState` has exactly 4 members, `store._WIRE_STATE_ORDER` has exactly 4 entries,
`payloads._VALID_STATE_VALUES == range(0, 4)`; bytes 0/1/2/3 decode to IDLE/ARMED/FIRED/FAULT and 4
is rejected. **No valid heartbeat is dropped.** But the `4` is hardcoded in
`domain/payloads.py:84` with **no runtime link** to the enum — `payloads.py` imports neither
`TrapState` nor `_WIRE_STATE_ORDER` (it cannot: DD1 forbids `domain/` importing `adapters/`), so the
two are independent literals in two layers.

Failure scenario: a 5th `TrapState` (say `TAMPER`) is added and `_WIRE_STATE_ORDER` extended to 5
entries. `payloads.py` still says `range(4)`. Every `TAMPER` heartbeat is rejected at decode and
silently dropped (see WARNING C — nothing counts it). That trap goes dark, is marked unreachable
after `silence_after`, and **the full suite stays green** — mutation 1 is the proof.

Suggested close: parametrize the reject test over `range(4, 256)` (or Hypothesis), add an accept test
over `range(4)` asserting each maps to the right `TrapState`, and add one test asserting
`len(_VALID_STATE_VALUES) == len(_WIRE_STATE_ORDER) == len(TrapState)` so the two literals cannot
drift apart in silence.

---

### WARNING C — task 1.2's `decode_errors` claim is false; a live-ingest `PayloadError` is now counted nowhere (claim 8, independently confirmed TRUE)

**Where**: `src/feeder_station/adapters/ingest.py:79-86`; `src/feeder_station/domain/serial_framing.py:124, 147, 159`;
`src/feeder_station/adapters/serial_gateway.py:148, 202`; `src/feeder_station/app.py:320`;
`openspec/changes/field-station-p4/remediation-tasks.md:116-119` (task 1.2).

Task 1.2 asserts the new `PayloadError` means *"the existing `decode_errors` counter (surfaced in
`/health`'s `link` block) picks it up for free — no new counter needed at this layer."* Trace, walked
end to end:

- `decode_errors` is owned by `domain/serial_framing.py::FrameStream` (`:124`) and incremented at
  `:147` and `:159` — **COBS framing errors only**, one layer below payload decoding.
- It reaches `/health` via `serial_gateway.py:202` → `app.py:320`, inside the `link` block.
- `payloads.PayloadError` never touches it. `ingest.py:79-86` catches
  `(FrameError, PayloadError, UninterpretableFrame)` and `return`s — **no increment, no log, no
  counter of any kind, anywhere.**

**The implementer's flagged discrepancy is TRUE.** Live-ingest decode failures — including every one
the new `_require_valid_state` now creates — are silently dropped with no operator-visible signal.

**Is this a regression introduced by this diff?** Partly. Before Batch 1, a `state=7` heartbeat was
stored and then raised `ValueError` out of `_apply_to_registry`, which `run_supervised`
(`adapters/workers.py:57-71`) caught into `status.last_error`, killing the gateway thread —
catastrophic, but loudly visible in `/health`. After Batch 1 the frame is dropped at decode, the
gateway survives, and **nothing is recorded at all**. Availability improved; observability for this
class went to zero.

**Severity: WARNING, not CRITICAL.** No spec requires counting undecodable payloads (checked
`openspec/changes/field-station-p4/specs/radio-gateway/spec.md` — no such requirement), and there is a downstream, if misleading,
symptom: a trap whose every heartbeat is rejected receives no frames, so it eventually shows
`reachable: false`. The operator sees a dead trap that is in fact transmitting perfectly — enough of a
signal to not be silent-failure CRITICAL, but wrong enough to be worth a counter.

Suggested close: increment a `payload_errors` counter in `ingest_frame`'s `except` and surface it
next to `decode_errors` in `/health`'s `link` block, exactly as task 1.2 assumed already happened.
Either that, or amend task 1.2's text — it is currently a false statement in the record.

---

### WARNING D — the new `rehydrate` docstring asserts a totality the code does not have, and repeats a false claim about `_apply_to_registry`

**Where**: `src/feeder_station/adapters/store.py:474-490` (docstring), `:503-509` (inline comment);
`src/feeder_station/adapters/ingest.py:116-141`.

Two false statements, now committed into production source:

1. *"is skipped and counted in `rehydration_errors` **instead of propagating out of this
   method**"* — unqualified, and disproved by P1/P2/P3 above. The docstring does not say
   "`ValueError` is skipped"; it says unreadable rows are skipped. That is the same class of defect
   WARNING 14 names (*"the record actively misleads the next reader"*), re-created in a docstring
   written this batch, and it will read as an assurance to whoever touches `rehydrate()` in Batch 2.
2. *"The same best-effort discipline `adapters/ingest.py::_apply_to_registry` already applies to the
   live path"* (`store.py:485-487`, copied verbatim from task 1.4's own text). **`_apply_to_registry`
   has no exception handling at all** — `ingest.py:116-141` contains zero `try`. Its only containment
   is `isinstance` type guards at `:131` and `:139`, and `_apply_heartbeat` calls
   `trap_state_from_wire(state_value)` (`:137`) completely unguarded. The only thing catching anything
   on the live path is `run_supervised`, three layers up, which kills the worker thread. The cited
   precedent does not exist.

---

### SUGGESTION E — the per-call reset of `rehydration_errors` is correct but unpinned by any test

Claim 3 verified: `store.py:493` resets to `0` at the top of `rehydrate()`. Three consecutive
`rehydrate()` calls against a store with one bad row yield `1, 1, 1` — **no accumulation, no
staleness**, `/health` cannot report a growing count. Correct as written.

But deleting the reset line leaves the full suite at **218 passed, exit 0**. Nothing pins it. Since
`rehydrate()` is called exactly once per process (`app.py:228`), this is latent rather than live —
it becomes real the moment anything calls `rehydrate()` twice, which Batch 2's property tests
(task 2.1: *"restart through `build_app`"*) plausibly will.

### SUGGESTION F — the `None`-when-no-store branch is untested and unreachable through the production boot path

`apply-progress-batch1.md:39` claims `rehydration_errors` is *"`None` when no store is configured"*.
`app.py:337-338` does implement that, and `app.state.store` is always assigned (`app.py:273`), so
`/health` never raises on a store-less station — claim 6 holds at the code level.

Two caveats: (a) `cli.py:73` constructs `Store(args.db_path)` **unconditionally**, so through the real
`cli.build_app` path `store` is never `None`; booting with no `--db-path` returns
`rehydration_errors: 0`, not `None` (verified — and it creates a `station.db` in the cwd as a side
effect). (b) `rg rehydration_errors tests/` returns exactly two assertions, both for the `== 1` case.
The `None` branch has no test.

### SUGGESTION G — `apply-progress-batch1.md`'s TDD table misattributes where the CLI test landed

The table (`:37`) says task 1.3's RED test was written in commit `77fad51`. `git show --stat` shows
`77fad51` touches only `remediation-tasks.md`, `store.py` and `tests/adapters/test_store.py`;
`tests/test_cli.py` landed in `c150952` alongside `app.py`. The narrative is wrong; the underlying
"each commit green in isolation" claim is nonetheless **verified true** (216/217/218, all exit 0) —
precisely *because* the CLI test landed with the code that makes it pass.

### SUGGESTION H — spec paths in the verification brief do not exist

`openspec/specs/` contains only `.gitkeep`. The specs cited as
`openspec/specs/event-store/spec.md` and `openspec/specs/station-deployment/spec.md` actually live at
`openspec/changes/field-station-p4/specs/…`. Worth correcting before the archive phase promotes them.

---

## Part 3 — Claims checked, one by one

| # | Claim | Verdict | Evidence |
|---|---|---|---|
| 1 | Task 1.1 proves the rule, not the example | **FALSE** | Mutation: special-casing `{4,7,130,255}` → 218 passed, exit 0. WARNING B |
| 2 | `rehydrate()` is now total (skip-and-count) | **FALSE — partial** | P1 `OverflowError`, P2/P3 `AttributeError` all brick the boot. Loop header + `_known_node_ids()` outside the guard. CRITICAL A |
| 3 | `rehydration_errors` resets each call; `/health` never stale/accumulating | **TRUE** (untested) | 3 calls → `1, 1, 1`. Reset line deletable with suite green. SUGGESTION E |
| 4 | Zero existing tests weakened | **TRUE** | `git diff --numstat 504d18e..HEAD -- tests/` = `49/0`, `52/0`, `53/0` — **zero deletions**, zero modified lines. Mutation-confirmed not vacuous: reverting `store.py` fails 2 tests; removing the `/health` field fails 1 |
| 5 | `PayloadError` subclasses `ValueError`, so guard 2 is not a no-op | **TRUE** | `domain/payloads.py:50` `class PayloadError(ValueError)`. Control probe P4 caught and counted |
| 6 | `/health` correct for a store-less station | **TRUE** (with caveats) | `app.py:273` always assigns `app.state.store`; `:337-338` handles `None`. Unreachable via CLI, untested. SUGGESTION F |
| 7 | Decode boundary rejects no valid value | **TRUE** | `range(0,4)` == 4 `TrapState` members == 4 `_WIRE_STATE_ORDER` entries; 0-3 all decode, 4 rejected. No off-by-one today — but see WARNING B for drift risk |
| 8 | Task 1.2's `decode_errors` claim is false for live ingest | **TRUE — confirmed independently** | `decode_errors` is COBS-only (`serial_framing.py:147,159`); `ingest.py:82-86` swallows `PayloadError` uncounted. WARNING C |

## Part 4 — Task completeness

| Task | Boxed | Code state agrees | Note |
|---|---|---|---|
| 1.1 | `[x]` | Yes | 8 parametrized cases land; strength gap → WARNING B |
| 1.2 | `[x]` | Yes | Validation lands; the task's own `decode_errors` rationale is false → WARNING C |
| 1.3 | `[x]` | Yes | End-to-end boot test lands and is mutation-confirmed non-vacuous |
| 1.4 | `[x]` | **Partially** | Guard lands but is not total → CRITICAL A |
| 1.5 | `[x]` | Yes | `/health` field lands, mutation-confirmed pinned |
| 1.6 | `[x]` | **No** | Docstring rewritten, but states a contract the code does not honour → WARNING D |

Scope discipline: `remediation-tasks.md` diff is **only** the six `[ ]`→`[x]` ticks — no Batch 2/3/4
boxes touched, no text edited. `design.md` and `tasks.md` untouched, as task 1.6 requires.

## Method note

The venv installs `feeder_station` as an **editable install with a `sys.meta_path` finder**
(`_editable_impl_feeder_field_station.pth`) pinned to the main repo's `src/`. Mutations applied inside
a `git worktree` are therefore **silently ignored** — pytest imports the unmutated main-repo package
and reports green. My first mutation round hit exactly this and was discarded. Every mutation result
above was re-run against the real package root, with `git checkout --` restore and
`git status --porcelain` confirmed empty after each. HEAD was never moved; nothing was committed.

---

## Gate decision: may Batch 2 start?

**No.**

This is not process formalism — Batch 1 exists to be the ground Batch 2 stands on, and
`remediation-tasks.md:63-68` says so explicitly:

> **Hard-sequenced after Batch 1**: Batch 2's property tests boot through the same `rehydrate()` path
> Batch 1 just made total (non-raising) — writing Batch 2's tests against a `rehydrate()` that can
> still raise on a bad row means testing over a landmine Batch 1 exists to defuse.

`rehydrate()` can still raise on a bad row. The landmine is still armed, and Batch 2 restructures that
exact method (tasks 2.2, 2.6) while adding a five-ordering property test that reboots through it
repeatedly. Worse: Batch 2's task 2.2 introduces a `MAX(seq) WHERE msg_type IN (...)` query and a new
`Registry` method, i.e. **new** ways for a step to raise, against a guard that already under-covers
the old ones.

Fix CRITICAL A first — it is a one-line widening of the `except` plus moving the loop header inside
the guard, plus one regression test driving a non-`ValueError` corrupt row through `cli.build_app`.
WARNING D should be closed in the same commit, since it is the same docstring. WARNING B and C can
land alongside or immediately after; neither blocks Batch 2's code path.

**Do not push this batch as-is.** Its headline claim — "rehydration is now total" — is in the commit
message, in the docstring, and in the task list, and it is not true.
