# Remediation Tasks: field-station-p4 — second-verification CRITICALs

**Source of truth**: `openspec/changes/field-station-p4/verify-report-2.md` (six CRITICAL, thirteen
WARNING, thirteen SUGGESTION). Cross-referenced against `verify-report.md` (first pass) for the two
CRITICALs that are only partially fixed. Specs: `specs/event-store/spec.md`,
`specs/radio-gateway/spec.md`, `specs/clock-trust/spec.md`, `specs/trap-commands/spec.md`,
`specs/station-deployment/spec.md`, `docs/requirements.md`.

**Does NOT touch** `tasks.md` (138/138 boxed, now partly describing code that no longer exists per
WARNING 14) or `design.md` except the one line CRITICAL 6 names explicitly. Both are flagged in
Appendix A for a future session.

**Mode**: Strict TDD. RED before GREEN, every task. `uv run pytest`, `uv run ruff check .`,
`uv run mypy src` must stay clean after every batch.

---

## Review Workload Forecast

| Field | Value |
|---|---|
| Estimated changed lines (total, all 4 batches) | ~900–1,230 |
| 400-line budget risk | **High for the whole remediation; Low–Medium per individual batch** |
| Chained PRs recommended | **Yes** |
| Suggested split | 4 sequential PRs/commits, one per batch below — not one PR |
| Delivery strategy | not passed by the orchestrator for this run; recommend `ask-on-risk` |
| Chain strategy | not cached for this run; recommend `stacked-to-main` — each batch is independently mergeable, fixes a real production risk on its own, and batch 1 (the crash-loop) should not wait behind a tracker branch |

```text
Decision needed before apply: Yes
Chained PRs recommended: Yes
Chain strategy: stacked-to-main
400-line budget risk: Medium
```

### Per-batch line-count forecast

| Batch | Fixes | Files touched (main) | Est. changed lines | Budget risk | Fits in one commit? |
|---|---|---|---|---|---|
| 1 | CRITICAL 2 | `adapters/store.py`, `app.py`, `domain/payloads.py`, 2 test files | **150–220** | Low | Yes |
| 2 | CRITICAL 1 + CRITICAL 6 | `adapters/store.py`, `domain/traps.py`, `design.md` §6, 2–3 test files | **250–380** | **Medium — closest to budget** | Yes, but see split contingency below |
| 3 | CRITICAL 3 + CRITICAL 4 | `adapters/workers.py`, `domain/commands.py`, new `domain/` pacer module, `config.py`, 3 test files | **280–380** | **Medium — closest to budget** | Yes, but see split contingency below |
| 4 | CRITICAL 5 | `adapters/scheduler.py` (deletion), `tests/adapters/test_backlog_rotation.py`, `tests/adapters/test_workers.py` | **150–250** | Low | Yes |

**Split contingency** (only if a batch's real diff exceeds ~380 lines once written): Batch 2 splits
into **2a — CRITICAL 1** (state-source fix + property test) and **2b — CRITICAL 6** (roster fix +
design.md line), landed as two commits in the same order, 2a before 2b, since 2b's roster fix is a
narrow, independent query change with no dependency on 2a's Registry method. Batch 3 splits into
**3a — CRITICAL 3** (station-wide pacer for `send_due_commands`/`GET_BACKLOG`) and **3b — CRITICAL 4**
(withheld-`TIME_SYNC` cadence), 3a before 3b, since 3b can consume whatever pacer primitive 3a
introduces. Do not pre-split unless the estimate is exceeded — the report's own diagnosis is that
splitting unnecessarily fragments a coupled fix as badly as over-batching does.

---

## Why this order (from the report, not re-derived)

1. **Batch 1 (CRITICAL 2) first, alone.** It is the only finding that can **brick a deployed
   station** — one corrupt `HEARTBEAT.state` byte makes `rehydrate()` raise inside the ASGI lifespan,
   `uvicorn` exits, `Restart=always` restarts it, rehydration reads the same row, raises again:
   infinite crash loop, no `/health`, no UI, unrecoverable without hand-editing SQLite in the field.
   Containment is cheap and has no dependency on the other five.
2. **Batch 2 (CRITICAL 1 + CRITICAL 6) together.** Both are `rehydrate()` — one is the state source,
   one is the roster. They touch the same method and the same design.md section; splitting them
   further than necessary would mean re-deriving the same boot harness twice. **Hard-sequenced after
   Batch 1**: Batch 2's property tests boot through the same `rehydrate()` path Batch 1 just made
   total (non-raising) — writing Batch 2's tests against a `rehydrate()` that can still raise on a bad
   row means testing over a landmine Batch 1 exists to defuse.
3. **Batch 3 (CRITICAL 3 + CRITICAL 4) together.** Both are the scheduler's unpaced transmitters; the
   report's own fix is "a single station-wide frame budget... closes both." No code dependency on
   Batches 1–2, but land after them to keep unrelated review context out of the crash-loop fix.
4. **Batch 4 (CRITICAL 5) last.** Delete-or-rewire `run_rotation_round` and move its coverage onto the
   live loop. Depends on Batch 3 only because both edit `run_scheduler_loop`/`scheduler.py` — landing
   4 after 3 avoids a merge conflict, not a correctness dependency.

**Parallelism**: within a batch, RED tasks for independent test files may be written in parallel;
GREEN must follow RED confirmed-failing, sequentially, per Strict TDD. Batches 3 and 4 could in
principle be implemented by a second engineer in parallel with Batches 1–2 (no shared code path), but
should still be reviewed and merged in the stated order to keep each diff's blast radius small.

---

## The rule every RED test in this list must encode

The report's own diagnosis: **"each fix was validated against the failing example rather than against
the rule."** Three of the four new CRITICALs are that mistake, verbatim. So:

- Not *"this ordering rehydrates correctly"* but **"the rehydrated registry equals the live registry,
  for every ordering and every report type"** (Batch 2).
- Not *"this transmitter is paced"* but **"the station's total airtime stays under the budget in
  `docs/protocol/messages.md`, measured off the wire through `cli.build_app`"** (Batch 3).
- Not *"the wiring works"* but **"what happens when the wired-in thing raises"** (Batch 1).

Any task below that edits an *existing* assertion states what it asserted before, what it asserts
after, and why the new one is the rule and not the example — this is called out explicitly wherever it
applies (2.4, 3.6, 4.3).

---

## Batch 1 — CRITICAL 2: a corrupt row must not brick the boot

**Rule to prove**: *a boot that cannot read one row must still be a boot* — rehydration must be total
(skip-and-count, never raise), and the decode boundary must reject an out-of-range `state`/
`state_after` byte the same way it already rejects any other corrupt frame, so a bad byte never
reaches the store at all.

**Satisfies**: `event-store/spec.md` "Registry rehydration at boot" (rebuild MUST NOT abort the whole
station over one row); `station-deployment/spec.md` "Starts by itself after a power cut (S7)";
S7, S9 (`docs/requirements.md`).

- [x] 1.1 RED — `tests/domain/test_payloads.py`: parametrized test asserting `decode_heartbeat`
  rejects every `state` byte outside `0–3` the same way `_require_len` rejects a truncated payload
  (raises the module's existing decode-error type, not a bare `ValueError` that later explodes
  elsewhere); same for `decode_captured`'s `state_after` byte. Cover the boundary values (`4`, `255`)
  and at least one mid-range invalid value, not just the report's example (`7`).
- [x] 1.2 GREEN — `src/feeder_station/domain/payloads.py`: validate `state`/`state_after` inside
  `decode_heartbeat`/`decode_captured`, raising the same decode-error class `frames.decode`/
  `interpret()` already use for a corrupt frame, so the existing `decode_errors` counter (surfaced in
  `/health`'s `link` block) picks it up for free — no new counter needed at this layer.
  **Correction (verify-report-batch1, WARNING C): the sentence above was false.** `decode_errors` is
  owned by `serial_framing.py::FrameStream` and counts COBS-level framing failures only;
  `ingest.py::ingest_frame` caught `PayloadError` and returned with no counter anywhere, so this task
  as written turned a loud failure into a silent one. A separate `payload_errors` counter was added
  and surfaced in `/health`'s `link` block. Left visible rather than rewritten: a plan that quietly
  edits away its own wrong assumption teaches nobody why the assumption was wrong.
- [x] 1.3 RED — `tests/test_cli.py`: boot `cli.build_app` (real `TestClient`, real ASGI lifespan) on a
  store that already holds one syntactically-valid-but-out-of-range `HEARTBEAT.state` row, written
  directly to bypass the now-fixed decode boundary (simulating a bit-flip that happened before 1.2
  existed, or any future row the decode boundary did not anticipate). Assert the app **boots**,
  `/health` responds `200`, and `/health` reports a non-zero rehydration-errors count for that node —
  not a `ValueError` escaping the lifespan.
- [x] 1.4 GREEN — `src/feeder_station/adapters/store.py::rehydrate`: wrap each node's three replay
  steps in a `try/except` that skips and counts an unreadable row rather than propagating, the same
  best-effort discipline `adapters/ingest.py::_apply_to_registry` already has for the live path.
  Return or store the skip count where `app.py`'s lifespan can reach it.
  **Correction (verify-report-batch1, WARNING D): `ingest.py::_apply_to_registry` has no exception
  handling at all** — the "best-effort discipline it already has" cited above does not exist, and that
  false precedent was copied verbatim into the production docstring before review caught it.
  **Correction (verify-report-batch1, CRITICAL A): `except ValueError` was not enough.** A corrupt
  `received_epoch` raises `OverflowError`, a non-object `fields` cell raises `AttributeError`, and the
  node-roster query itself sat outside the guard. The implemented fix catches `Exception` — the
  contract is indifference to the error type, so enumerating types is a whitelist that fails on the
  next one nobody predicted.
- [x] 1.5 GREEN — `src/feeder_station/app.py::health()`: add a `rehydration_errors` (or equivalent)
  field to the response, sourced from 1.4's count, following the same "surface it, don't hide it"
  convention the `link` block already sets for decode errors.
- [x] 1.6 REFACTOR — update `rehydrate`'s own docstring (already mid-edit per the CRITICAL-1/2
  history) to describe the skip-and-count contract; do not touch `design.md` here — that line is
  fine as written and out of scope for this batch.

**Verification (after Batch 1, before Batch 2 starts)**: `uv run pytest`, `uv run ruff check .`,
`uv run ruff format --check .` (no new drift), `uv run mypy src`. Re-run the report's own repro
(`interpret()` a `state=7` HEARTBEAT, append it directly, boot `cli.build_app` on that store) and
confirm it now returns a live `/health` `200` instead of a crash-loop stack trace. Scope: CRITICAL 2
only — do not re-check CRITICAL 1/6 yet, they are not touched by this batch.

---

## Batch 2 — CRITICAL 1 + CRITICAL 6: rehydrate the right state, for the right roster

### CRITICAL 1 — state source

**Rule to prove**: **the rehydrated registry equals the live registry, for every ordering and every
report type** — `HEARTBEAT`, `CAPTURED` and `FAULT`, in both directions. Not "ordering A rehydrates
correctly" (the example the first report printed and the only one the previous fix validated against).

**Satisfies**: `event-store/spec.md` "Registry rehydration at boot" ("last known reported state...
without depending on the node re-sending anything"); `radio-gateway/spec.md` "FAULT and silence
handling" (`FAULT` MUST update reported state — including across a restart); `trap-commands/spec.md`'s
S4 discipline ("displayed state MUST always be whatever its own most recent report said"); S3, S4, S11.

- [x] 2.1 RED — new property-shaped test (suggested home: `tests/test_cli.py`, reusing the report's
  own harness pattern — real `cli.build_app`, `with TestClient(app)`, frames written to a real pty).
  For each of these logs, build the **live** registry by replaying the log through
  `adapters/ingest.py::_apply_to_registry`, restart through `build_app`, and assert the **rehydrated**
  registry's `state`, `last_seen` and `reachable` match the live one exactly:
  - `HEARTBEAT` 6h old, `CAPTURED` 2min new (report's ordering A — already passing, keep as a
    regression guard)
  - `CAPTURED` 6h old, `HEARTBEAT` 2min new (ordering B — the mirror the previous fix broke)
  - `CAPTURED` 30s ago, no heartbeat ever (ordering C)
  - `HEARTBEAT`, then `FAULT` 1min later (ordering D)
  - `FAULT`, then a newer `HEARTBEAT` reporting a different state (the un-reported mirror of D — the
    report only measured one direction; this task adds the other so "every ordering" is actually
    covered, not just the four the report happened to print)
  Parametrize one test over all five cases — this is what makes it a property test rather than five
  copies of "this example passes."
- [x] 2.2 GREEN — `src/feeder_station/domain/traps.py`: add a rehydration-only `Registry` method that
  sets `state`/`last_seen` alone from a single winning fact (mirrors `restore_capture`'s existing
  "never touch the other one's fields" discipline) — this is the counterpart `restore_capture` already
  has for the capture object, now needed for reported state.
  `src/feeder_station/adapters/store.py::rehydrate`: replace the unconditional
  `_rehydrate_heartbeat` → `_rehydrate_capture` sequence with: (a) find the most recent
  **state-bearing** row across `HEARTBEAT`, `CAPTURED`, `FAULT` by store `seq`
  (`MAX(seq) WHERE msg_type IN (...)`, the report's own suggested query shape) and apply *that one
  row's* fact via 2.2's new method — `CAPTURED`'s `state_after` (not a hardcoded `FIRED`) for a
  winning `CAPTURED` row, `FAULT` for a winning `FAULT` row; (b) separately, from the latest
  `HEARTBEAT` regardless of whether it won, restore `battery_mv`/`targets_version` only, since those
  fields exist nowhere else; (c) `_rehydrate_capture` unchanged, still restoring the unacknowledged
  capture object independently of which row won the state race. The exact split between (a)/(b) is
  this task's to decide — 2.1's test is what pins the outcome.
- [x] 2.3 REFACTOR — delete the now-redundant "never `captured()`, always `restore_capture`" comment
  in `store.py` if 2.2's restructuring makes it stale; keep the CRITICAL-1/2 provenance comments that
  still apply.
- [x] 2.4 **Edited assertion** — `tests/adapters/test_store.py::test_registry_reflects_last_known_
  state_after_restart`. **Before this batch**: asserts `TrapState.ARMED` (edited from `FIRED` by the
  prior fix pass, correct only for ordering A, and — per the report — "exactly what hid ordering B").
  **After this batch**: this single-ordering unit test is either (a) folded into 2.1's parametrized
  property test as the ordering-A case, deleting the standalone assertion, or (b) kept as a narrow
  store-level regression guard *in addition to* 2.1, but must then also gain a sibling case for
  ordering B in the same file so it cannot regress silently again. Do not simply flip the assertion
  back to `FIRED` — that repeats the exact mistake the report diagnosed (validating one example
  instead of the rule). State explicitly, in the commit, which of (a)/(b) was chosen and why.

### CRITICAL 6 — roster

**Rule to prove**: a command issued to a node the station has **never heard from** survives a restart
— the roster rehydration walks must include every node with an operator-sourced row, not only nodes
present in `node_cursor` (which is populated exclusively by node-sourced events).

**Satisfies**: `event-store/spec.md` "Registry rehydration at boot" ("without depending on the node
re-sending anything"); `trap-commands/spec.md` "Target-list delivery with version reconciliation"
(resend continues until confirmed — including across a restart); S1, S2.

- [ ] 2.5 RED — new test (suggested home: `tests/test_cli.py`, alongside 2.1's boot harness): boot 1,
  `POST /arm` (or `PUT /targets`) a node id the station has never received a frame from, confirm it
  is pending via `/api/traps`, restart through `build_app`, assert the node and its pending command
  are still present in boot 2's `/api/traps` — not absent, as the report measured.
- [ ] 2.6 GREEN — `src/feeder_station/adapters/store.py::_known_node_ids`: derive the roster from
  `SELECT DISTINCT node_id FROM event` (simplest correct fix per the report), or union `node_cursor`
  with the distinct `node_id`s of operator-sourced rows if a narrower query is preferred. Either way,
  `rehydrate()`'s loop must visit a commanded-but-never-heard node.
- [ ] 2.7 GREEN — `openspec/changes/field-station-p4/design.md`, line ~288: amend "For each `node_id`
  in `node_cursor`" to match 2.6's actual roster derivation. This is the one design.md edit that is
  **in scope for this batch**, not deferred to Appendix A — the report ties it directly to CRITICAL 6
  ("design.md §6 step 3 as written applies to... `node_cursor`, so the design shares the defect"). Do
  not touch any other design.md line here (the reachability line, the scheduler diagram label, etc.
  are Appendix A / WARNING 14, out of scope).

### Carried in from Batch 1's round-2 verification

These three are Batch 2's subject matter by topic, not a separate batch: 2.8 is the direct consequence
of the skip granularity task 2.2 has to decide anyway, and 2.9/2.10 are about what `rehydrate()`
reports when it gives up on something.

- [x] 2.8 **WARNING J — containment must not fabricate a calm reading.** Batch 1's guard skips
  per *step*, not per *node*, so a node whose heartbeat replay fails but whose capture replay succeeds
  is created by `restore_capture` with `Trap.state`'s dataclass default: a trap that last reported
  `FAULT` displays `"state": "idle"` indefinitely, with `ok: true`. `battery_mv`, `targets_version`
  and `last_seen` all honestly go `null` in the same situation — `state` is the one field that
  invents the least alarming value instead of admitting it does not know.
  This is the same class of defect the first verification called CRITICAL in its own words ("a false
  all-clear on a trap holding an animal"), so treat it at that severity even though it is reachable
  only after a corrupt row. Decide the granularity deliberately in 2.2 rather than inheriting it, and
  make "unknown" representable: a state the interface can render as unknown beats a default that
  renders as safe. RED test first, driving a corrupt heartbeat row for a node that also has a good
  capture row, asserting the view never shows `idle` for a node whose real last report was `FAULT`.
- [ ] 2.9 **WARNING K(b) — one bad row and a lost registry must not look identical.** `/health`
  reporting `rehydration_errors: 1` is byte-for-byte the same for "1 unreadable row out of 20 healthy
  nodes" (`/api/traps` returns 20) and "the roster query itself died, registry empty" (`/api/traps`
  returns 0), `ok: true` in both. The second case used to crash loudly; Batch 1 made it silent, which
  is the same trade WARNING C caught on the ingest path. Distinguish them — a failure that loses the
  whole registry is not an error count, it is a degraded boot, and `/health` has to say so.
- [ ] 2.10 **WARNING K(a) — pin the logging.** Two mutations survived the whole 729-test suite:
  downgrading `rehydrate`'s skip log to `logger.debug` while dropping the node id and traceback, and
  the same on `ingest`'s drop log. `rg 'caplog' tests/` finds nothing. The *counted* half of the
  `except Exception` mitigation is pinned five ways; the *logged with its node id* half is pinned by
  nobody — and logging is the entire reason the broad catch was acceptable. Add `caplog` assertions
  for both.

**Verification (after Batch 2, before Batch 3 starts)**: `uv run pytest`, `uv run ruff check .`,
`uv run mypy src`. Re-run the report's own four-ordering repro plus the never-heard-node repro by
hand; confirm all four `rehydrate` scenarios in Part 1.2 of `verify-report-2.md` now read `AGREE`
instead of `DIVERGES`, and the never-heard-node case reads `SURVIVED` instead of `LOST`. Spot-check
the `Spec Compliance Matrix` rows for `event-store` "registry reflects last known state after restart"
and `trap-commands` "resend continues until version confirmed" — both should move off ❌/⚠️.

---

## Batch 3 — CRITICAL 3 + CRITICAL 4: one station-wide frame budget

**Rule to prove**: **the station's total transmit rate stays under the budget in
`docs/protocol/messages.md`** (~50 bytes/frame, 0.37 s airtime at Long Fast, "keep the channel under
1% occupied" → ≈0.027 frames/s), **measured off the wire through the real `cli.build_app` path**, not
computed from two constants — the exact failure mode of the test the report names
(`test_default_backlog_pacing_holds_the_stations_own_airtime_budget`, which "measures nothing... is
arithmetic asserting arithmetic").

**Satisfies**: `docs/protocol/messages.md`'s stated design budget; S5 (ten traps commanded without the
operator waiting — at 11.68% occupancy the confirming heartbeats cannot get through, which is the
same deadlock argument the first report made); `clock-trust/spec.md` "Withhold TIME_SYNC when
untrusted" (the log obligation, MUST log each withheld send — the defect is volume, not the log
itself); S9 (indirectly, via the fsync-inside-the-lock interaction with WARNING 4).

- [ ] 3.1 RED — new integration test (suggested home: `tests/adapters/test_workers.py` or
  `tests/test_cli.py`, reusing the report's own measurement harness — real `build_app`, real pty, 10
  traps, all armed and unconfirmed, backoff burst allowed to settle, then a steady-state window
  measured off the wire): assert the station's **total** frames/s across `GET_BACKLOG`, `ARM`/
  `DISARM`/`SET_TARGETS` resends and `TIME_SYNC` stays under the `messages.md` budget. This test
  supersedes `test_default_backlog_pacing_holds_the_stations_own_airtime_budget`'s scope (that test's
  own docstring "honestly scopes it to backlog polling's own contribution" — this task is the first
  one that names and measures the *station's* total, which nothing today does).
- [ ] 3.2 GREEN — new pure module under `src/feeder_station/domain/` (e.g. `pacing.py`, following the
  existing `backlog.RotationSchedule` shape — pure state, mutated under `registry_lock` in
  `adapters/workers.py`, never importing `adapters/`, DD1): a token-bucket or equivalent primitive
  sized from the `messages.md` budget (configurable via `config.py`, not hardcoded, mirroring how
  `heartbeat_interval` is already configurable).
  `src/feeder_station/adapters/workers.py::run_scheduler_loop`: gate `poll_node` (`GET_BACKLOG`),
  `send_due_commands` and `attempt_time_sync`'s actual sends all through the same bucket — "no future
  driver can bypass it either," per the report's own fix language.
- [ ] 3.3 GREEN — `src/feeder_station/domain/commands.py::RESEND_BACKOFF_S`: raise the 30 s cap toward
  `config.heartbeat_interval` (a command cannot be confirmed faster than the trap's own heartbeat
  cadence, so capping the backoff below that cadence only adds unconfirmable retransmits).
- [ ] 3.4 RED — `tests/domain/test_commands.py` (or wherever `RESEND_BACKOFF_S`/`due_for_resend` is
  currently covered): assert the new cap value and that `due_for_resend` still behaves correctly at
  the new ceiling — a straightforward value-change test, not property-shaped, since this one genuinely
  is a constant.
- [ ] 3.5 GREEN for 3.4.
- [ ] 3.6 **Edited assertion** — `tests/adapters/test_workers.py::test_default_backlog_pacing_holds_
  the_stations_own_airtime_budget`. **Before**: asserts `1 / DEFAULT_HEARTBEAT_INTERVAL.total_
  seconds() * 0.37 < 0.01` — arithmetic on two module constants, no station constructed, no frame
  counted. **After**: either deleted in favor of 3.1's real measurement, or rewritten to assert the
  same real-wire measurement 3.1 performs for the `GET_BACKLOG`-alone case specifically (matching its
  original, narrower, honestly-scoped docstring) while 3.1 owns the *total*. State which was chosen.
- [ ] 3.7 RED — new test bounding `STATION_TIME_SYNC_WITHHELD` row-write rate with an unset clock and
  a multi-node roster over a multi-tick window (property-shaped: an upper bound on rows/second that
  holds regardless of roster size or tick count — not "at least one row exists," which is what
  `test_time_sync_resumes_once_the_scheduler_loop_sees_a_trusted_clock` currently checks and is not
  touched by this task, since that test's own claim about *resuming* is correct and stays).
- [ ] 3.8 GREEN — `src/feeder_station/adapters/workers.py::run_scheduler_loop`: give the withheld
  `TIME_SYNC` path its own retry cadence — attempt an unsynced node at most once per
  `config.heartbeat_interval`, or log the withholding once per node per transition into `unset` rather
  than once per attempt (report's two named options; either preserves CRITICAL 3's fix — the node
  stays eligible — without the 1 Hz write amplification).

**Verification (after Batch 3, before Batch 4 starts)**: `uv run pytest`, `uv run ruff check .`,
`uv run mypy src`. Re-run the report's own three-row occupancy table (10 traps unconfirmed ARM; 1 trap
unconfirmed ARM; 10 traps idle `GET_BACKLOG`-only) and confirm all three now read under ~1%, not
11.68%/1.17%/0.12%. Re-run the withheld-`TIME_SYNC` volume repro (10 traps, unset clock, 10 s) and
confirm rows/s is bounded, not 10.0. Note for the record (not a task): pacing changes here may shift
WARNING 12's 50–150-minute lap time further; that is a WARNING-level follow-up, not blocking.

---

## Batch 4 — CRITICAL 5: delete-or-rewire the dead rotation primitive

**Rule to prove**: `radio-gateway/spec.md`'s "one silent node does not block the rotation" scenario is
covered by a test that exercises the **production** scheduler loop's poll **order**, not a function
with zero callers in `src/`.

**Satisfies**: `radio-gateway/spec.md` "Backlog rotation across nodes" — "MUST skip a node that does
not respond within its retry budget and continue the rotation"; S5, S9.

- [ ] 4.1 GREEN (deletion path, recommended — re-wiring `run_rotation_round` as a "catch-up sweep"
  re-introduces the exact full-roster-sweep-per-call shape that caused CRITICAL 4 originally, per the
  report's own framing of this as "the same defect family... re-created"): delete
  `run_rotation_round` and `poll_node`'s now-unused re-export from
  `src/feeder_station/adapters/scheduler.py`, keeping `poll_node` itself (still used by
  `run_scheduler_loop`).
- [ ] 4.2 RED — before deleting the four tests in `tests/adapters/test_backlog_rotation.py`, write
  their production-loop replacement first: a new test against `run_scheduler_loop` (or a thin wrapper
  exposing the schedule for assertion) that drives ten known traps, one of which never responds, and
  asserts the **poll order** `[1,1,1,2,2,2,3,3,3]` at `retry_budget=3` — the exact order the report
  verified by hand and the exact thing the current replacement test
  (`test_backlog_rotation_persists_across_ticks_and_is_paced`) never checks.
- [ ] 4.3 **Edited assertion** — `tests/adapters/test_workers.py::test_backlog_rotation_persists_
  across_ticks_and_is_paced`. **Before**: counts `GET_BACKLOG` frames and asserts `< 15/s`; never
  inspects poll order, retry accumulation, or schedule survival despite its name promising
  "persists... and is paced." **After**: 4.2's order assertion is added to this test (or a sibling in
  the same file), so the name and the assertion agree. State that the frame-count assertion is kept
  as a coarse regression guard, not removed, since 4.2 checks a different property (order, not rate).
- [ ] 4.4 GREEN — remove `tests/adapters/test_backlog_rotation.py` (or gut it to nothing once its four
  tests' behavior is covered by 4.2/4.3 against the live loop) once 4.1's deletion lands and 4.2 is
  green.
- [ ] 4.5 REFACTOR — `tests/test_app.py:446-456`'s docstring (named in WARNING 14 as one of the three
  places that still claims a rotation round runs "slice 7" style): correct it to describe what the
  test actually exercises post-deletion. This one docstring is in scope here because 4.1 is what makes
  it wrong in a new way; the other WARNING 14 items (design.md's three stale lines, tasks.md,
  `attempt_time_sync`'s docstring claim) are Appendix A, deferred.

**Verification (after Batch 4 — full remediation re-verification)**: `uv run pytest`,
`uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src`, `uv build`. Re-run the
report's own AST-based "does this symbol have any non-test caller" sweep and confirm
`run_rotation_round` no longer appears at all (deleted, not merely uncalled). This is the point to
run a full `sdd-verify` pass (verify-report #3) against all six CRITICALs plus the Spec Compliance
Matrix, since it is the first point after which every CRITICAL in this list should be closed.

---

## Batch 5 — CRITICAL I: a corrupt database must not brick the boot either

Found by Batch 1's round-2 verification, *after* Batch 1 passed. Not a regression — Batch 1 made this
strictly better and broke nothing — but it is the larger half of the same hole, and it blocks
**archive**, not Batch 2.

**Rule to prove**: *a boot that cannot read its store must still be a boot.* Batch 1 proved that for
one unreadable **row**. This proves it for the store as a whole.

**Evidence** (300-trial single-bit-flip sweep on a realistic 45 KB `station.db`):

```
287 (95.7%)  booted clean
  5 ( 1.7%)  booted, contained      <- what Batch 1 bought
  8 ( 2.7%)  BOOT FAILED: sqlite3.DatabaseError: malformed database schema
```

All 8 failures land in `Store.__init__`/`_ensure_schema`, clustered in the `sqlite_master` schema text
on page 1 — reached from `cli.py:73`, which constructs the `Store` **before** `create_app` exists. No
app, no lifespan, no `/health`, and under `Restart=always`/`RestartSec=2` that is CRITICAL 2's exact
crash loop. Also reproduced deterministically six ways: corrupt header, garbage file, truncation,
shredded mid-file pages, `chmod 000`, and path-is-a-directory.

`docs/requirements.md:94` names SD-card corruption as this system's top hardware risk, and S7 is
explicitly about surviving repeated unclean power cuts. Batch 1 covers 38% of the flips that cause any
problem at all; this is the other 62%.

**Satisfies**: `station-deployment/spec.md` S7; `event-store/spec.md`.

**This one needs a design decision before tasks, not a patch.** The question is what a station does
when its store is unreadable, and there is a real fork: refuse to serve (safe, but a dead station in
the field), or boot degraded — serve `/health` and the interface, report the store as failed, accept
live frames into memory, and stand up a fresh store beside the corrupt one so the next power cut does
not also lose the new data. The second is almost certainly right for a box that nobody can drive out
to reboot, but it trades away the guarantee that a running station is a recording station, and the
interface then has to make that distinction impossible to miss. Decide it deliberately, in the open,
before any code — that is what `sdd-design` is for. Do not let it be settled by whoever happens to
write the `try`.

---

## Appendix A — WARNING (13), not scheduled here

Severity order as in the report; numbers match `verify-report-2.md`.

| # | One-line summary | Status |
|---|---|---|
| 1 | `ARM.expires_epoch` distributes an untrusted-clock time (S12) | STANDS |
| 2 | Export presents commanded intentions as trap-state changes (S4) | STANDS |
| 3 | UI labels desired tags "Objetivos actuales" (should be "solicitados") | STANDS |
| 4 | `registry_lock` held across all serial I/O and store writes | STANDS, worse after CRITICAL 4's fix |
| 5 | `POST /acknowledge` on unknown node → 500 (S13) | STANDS |
| 6 | Reachability *is* rehydrated, contradicting design.md §6 | STANDS |
| 7 | `CAPTURED.state_after` decoded then discarded | STANDS — **now load-bearing for Batch 2's CRITICAL 1 fix** |
| 8 | Runbook doesn't name the tested OS image | STANDS, blocked on ops not code |
| 9 | Two replacement tests assert less than their names promise | Partially closed by Batch 3 (3.6) / Batch 4 (4.3) as a side effect; not separately scheduled |
| 10 | Two adapter policies resolved; `TIME_SYNC` withheld case became CRITICAL 4 | RESOLVED (see Batch 3) |
| 11 | Strict TDD evidence still per-change, not per-slice | STANDS |
| 12 | Full `GET_BACKLOG` lap now 50–150 min, undocumented decision | STANDS — watch after Batch 3 |
| 13 | `run_scheduler_loop(last_frame_arrival=None)` degrades silently | STANDS |
| **14** | **`design.md`, `tasks.md` and a test docstring describe code that no longer exists** | **STANDS — see below** |
| 15 | The prior fix shipped as one 892-line commit (2.2× budget) | The reason this document exists; addressed structurally by the 4-batch split above |

**WARNING 14, called out specifically, per the report's own framing that this is where "the record
actively misleads the next reader":**
- `design.md:293` still says step 2 calls `registry.captured(...)`; it calls `restore_capture(...)`.
- `design.md:303` "Reachability is not rehydrated" — still contradicted (WARNING 6).
- `design.md` §7's diagram still labels the scheduler "1 s tick · backlog rotation."
- `design.md:288` still refers to a `traps.rehydrate()` that has never existed.
- `tasks.md:384-385` still claims all four orphaned drivers "actually run, proven end to end" —
  `run_rotation_round` has not run in production since the commit under re-verification, and is
  deleted outright by Batch 4 of this list.
- `tests/test_app.py:446-456`'s docstring claims things about `run_rotation_round`/`attempt_time_sync`
  that are not true — **partially fixed by 4.5 above**; the `design.md`/`tasks.md` lines are not.

This document deliberately does not fix `tasks.md` or the remaining `design.md` lines — the user
request that produced this file was explicit that `tasks.md`'s 138 boxes stay as-is and the
misleading-record problem is noted, not corrected, here. A follow-up session should either (a) add a
`tasks.md` amendment note per slice rather than un-checking boxes, or (b) fold the correction into
this change's eventual archive step.

---

## Appendix B — SUGGESTION (13), not scheduled here

| # | Summary |
|---|---|
| 1 | `payloads.py` docstring stale re: which fields `messages.md` pins |
| 2 | `decode_heartbeat` comment still anticipates a removed GPS clock source |
| 3 | `export.py` comment stale — slice 13 already did what it says is unassigned |
| 4 | Dead code: `events_since`, `integrity_check` (now dead in tests too), `unacknowledged_captures`, `encode_get_state`/`GET_STATE`, `Capture.acknowledged` never set `True` |
| 5 | `Trap.is_reachable`'s 5-minute default should be required, not optional |
| 6 | design.md §2's `quick_check` at boot still not surfaced in `/health` |
| 7 | No `py.typed` marker; wheel ships untyped |
| 8 | `RELEASE_EPOCH_FLOOR` comment disagrees with its own value |
| 9 | `command_arm`/`command_disarm` take an unused `at` parameter |
| 10 | `restore_capture`/`restore_desired_tags` indistinguishable from the live API — mistaken route call risk |
| 11 | A rehydrated `Command` resets `attempts=0`, re-bursting the backoff ramp on every restart loop |
| 12 | A roster change resets rotation to position 0 — self-healing, but starves new nodes transiently |
| 13 | UI link banner shows only `connected`/`last_error`, not the full `/health` link block |

---

## Verification cadence (summary)

| After | Scope of re-verification |
|---|---|
| Batch 1 | CRITICAL 2 only: crash-loop repro, `/health` still reachable with a bad row |
| Batch 2 | CRITICAL 1 + 6: all five rehydration orderings agree live-vs-rehydrated; never-heard-node command survives restart |
| Batch 3 | CRITICAL 3 + 4: total station occupancy under budget in all three roster states; withheld-`TIME_SYNC` row rate bounded |
| Batch 4 | CRITICAL 5: `run_rotation_round` gone from the AST sweep; poll order asserted against the live loop. **Full `sdd-verify` re-run recommended here** — first point all six CRITICALs should be closed. |
