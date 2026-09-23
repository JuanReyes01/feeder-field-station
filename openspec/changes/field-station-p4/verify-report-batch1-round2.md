# Verification Report — `field-station-p4`, Batch 1, round 2 (re-verification)

**Diff under review**: `504d18e..HEAD` (`2456f69`), 8 commits — round 1 (`504d18e..3f53b11`) plus the
remediation round (`3f53b11..HEAD`, 4 commits).
**Predecessor**: `verify-report-batch1.md` — FAIL, 1 CRITICAL, 3 WARNING, 4 SUGGESTION.
**Artifact store**: openspec **Mode**: Strict TDD
**Scope**: Batch 1 (tasks 1.1–1.6) / CRITICAL 2 only. CRITICAL 1, 3, 4, 5, 6 belong to Batches 2–4,
are expected to still be broken, and are **not** reported here.
**Method**: every claim re-derived from the repository. The implementer's `apply-progress-batch1.md`
was treated as a hypothesis throughout. 12 mutations, 33 boot probes, and a 300-trial single-bit-flip
sweep, all against the real package root.

---

## Verdict

**PASS for Batch 1's own scope — every one of the predecessor's eight findings is genuinely closed,
and nothing in this round newly broke anything.** The "fixed the example, not the rule" pattern did
**not** recur a third time: the two mutations that survived round 1 are now killed, and eight new
mutations I invented confirm the fixes are pinned rather than coincidental.

**But the round is not clean.** One **CRITICAL**, two **WARNING** and six **SUGGESTION** findings are
raised below. The CRITICAL is *outside* Batch 1's mandate and is **not a regression** — it is a
pre-existing hole the brief for this round explicitly asked me to hunt for, and I found it, measured
it, and it is larger than the hole Batch 1 closed:

> Of 300 independent single-bit flips on a realistic `station.db`, **5 are now contained** by Batch 1's
> new guard and **8 still brick the boot** — in `Store.__init__`, before `rehydrate()` is ever reached.

**Batch 1 may be pushed. Batch 2 may start.** See *Gate decisions*.

Counts: **1 CRITICAL** (out of Batch 1 scope, not a regression), **2 WARNING**, **6 SUGGESTION**.

---

## Part 0 — Gates (each exit status captured directly; nothing piped)

| Gate | Command | Exit | Result |
|---|---|---|---|
| Tests | `uv run pytest` | **0** | 729 passed, 2 warnings, 4.46 s |
| Lint | `uv run ruff check .` | **0** | All checks passed |
| Format | `uv run ruff format --check .` | **1** | 6 files — *identical to baseline*, see below |
| Types | `uv run mypy src` | **0** | no issues in 22 source files |

**Format drift vs baseline — implementer's claim VERIFIED TRUE, zero new drift.** `504d18e` checked out
into a throwaway worktree and re-run: exit 1, the same 6 files. Full outputs diffed: the *only*
difference is `payloads.py:63:28` → `payloads.py:64:28`, a one-line shift caused by the single added
import `from .traps import TrapState` (`payloads.py:24`). `design.md:396:21`, `test_ingest.py:95:10`,
`test_serial_gateway.py:63:27`, `test_events.py:41:10`, `test_traps.py:27:74` are byte-identical.

**No unintended reformatting anywhere.** `git diff 504d18e..HEAD -- src/` reviewed line by line: every
changed production line is either a semantic change tied to a named finding or a comment/docstring
explaining one. **Zero** production lines changed with no semantic difference. The formatter did not
sweep `payloads.py` or any other file — the whole `payloads.py` diff is five hunks, all additive, and
the two `state=payload[0]` → `state=state` rewrites are required by the new bounds check.

---

## Part 1 — Verdict on each of the predecessor's findings

### CRITICAL A — `rehydrate()` is not total → **CLOSED**

`store.py:497–514`. The guard is now `except Exception` on both the roster query and each replay step;
the roster query result is materialised into a `list[int]` before the loop, so the loop header cannot
raise either.

All four of the predecessor's probes re-run end to end through the real `cli.build_app` +
`TestClient` + real ASGI lifespan, plus 21 probes it did not try:

| Probe | Corruption | Round 1 | Now |
|---|---|---|---|
| P1 | `received_epoch = 1e20` → `OverflowError` | **BOOT FAILED** | BOOTED, `rehydration_errors=1` |
| P2 | `fields = 'null'` → `AttributeError` | **BOOT FAILED** | BOOTED, `rehydration_errors=1` |
| P3 | `fields` storage class TEXT→INTEGER → `AttributeError` | **BOOT FAILED** | BOOTED, `rehydration_errors=1` |
| P4 | `fields = '{broken'` → `JSONDecodeError` (control) | BOOTED | BOOTED, `rehydration_errors=1` |
| P5 | `state = 7` (CRITICAL 2's own repro) | BOOTED | BOOTED, `rehydration_errors=1` |
| N5 | `received_epoch` NULL (`NOT NULL` stripped) → `TypeError` | — | BOOTED, `=1` |
| N6 | `fields` NULL → `TypeError` in `json.loads` | — | BOOTED, `=1` |
| N7 | `fields` blob of 120 MiB | — | BOOTED, `=0` (parsed fine) |
| N9 | `event` table dropped | — | BOOTED, `=0` (`_ensure_schema` recreates it) |
| N10 | `node_cursor` dropped before `Store()` | — | BOOTED, `=0` (recreated) |
| N11 | `fields = '[1,2,3]'` → `AttributeError` | — | BOOTED, `=1` |
| N14 | `state = 99999999999999999999999` → `IndexError`→`ValueError` | — | BOOTED, `=1` |
| N17 | `station.db-wal` replaced with 4 KiB of random bytes | — | BOOTED, `=0` |
| N21 | `received_epoch = -1e20` → `OverflowError` | — | BOOTED, `=1` |
| N23 | `OPERATOR_SET_TARGETS tags = ['zzzz']` → `ValueError` in `fromhex` | — | BOOTED, `=1` |
| R1 | `fields` = 200 000-deep nested JSON → `RecursionError` | — | BOOTED, `=1` |
| R3 | typo'd attribute in the replay path (a real *programming* bug) | — | BOOTED, `=1` |
| — | roster query dies (`node_cursor` dropped *after* `Store()`) → `sqlite3.OperationalError` | **BOOT FAILED** (by inspection) | BOOTED, `=1` |

**`BaseException` audit — nothing reachable escapes.** `MemoryError` and `sqlite3.Error` both derive
from `Exception` (verified: `MemoryError.__mro__` = `MemoryError → Exception → BaseException`), so the
prompt's "`MemoryError` on a huge blob is arguably reachable" is answered: it is reachable in
principle but it is **caught**. In practice SQLite caps it first — a ~1 GiB `fields` value is refused
at write time with `sqlite3.DataError: string or blob too big` (`SQLITE_MAX_LENGTH`), and 120 MiB
loads without incident. `RecursionError` derives from `RuntimeError → Exception`: caught (R1). The
only `BaseException`-only classes that matter are `KeyboardInterrupt`, `SystemExit` and
`GeneratorExit`; nothing in `_known_node_ids`, `latest`, `_row_to_event`, `_epoch_to_datetime`,
`trap_state_from_wire` or any `Registry` method raises any of them, and letting `KeyboardInterrupt`
through is correct behaviour anyway. **`except Exception` is the right width here.**

**Mutation-proved, not merely observed** — M3 and M4 below. The fix is pinned by tests, not by luck.

**One further mitigating fact worth recording**: `rehydrate()` is **read-only**. It issues only
`SELECT`s and mutates only the in-memory `Registry`. The broad catch therefore cannot swallow a
durability write — S9 is structurally out of its blast radius.

### WARNING B — the tests prove the examples, not the rule → **CLOSED**

`payloads.py:74–88`, `tests/domain/test_payloads.py:191–272`, `tests/adapters/test_store.py:384–392`.

Both of the predecessor's surviving mutations are now killed (M1: 2 failures; M2: 496 failures —
re-run myself, exactly the figures `apply-progress-batch1.md:87–104` reports). `_VALID_STATE_VALUES`
is derived from `len(TrapState)`. Reject is parametrized over `range(len(TrapState), 256)`; accept
over `range(len(TrapState))`, closing the gap the predecessor named (nothing asserted a valid
`state=2`/`state=3` decodes).

**The drift scenario the predecessor invented is now caught.** M12: adding a 5th `TrapState` member
(`TAMPER`) without extending `store._WIRE_STATE_ORDER` fails
`test_trap_state_from_wire_maps_every_valid_byte_to_the_right_state`. Extending `_WIRE_STATE_ORDER`
too still fails it (5-element LHS vs 4-element literal RHS), forcing an explicit decision. The two
independent literals in two layers can no longer drift apart in silence.

### WARNING C — a live-ingest `PayloadError` is counted nowhere → **CLOSED**

`ingest.py:88–102`, `serial_gateway.py:149–153, 188, 209, 213–217`, `app.py:321–324`. A payload-level
decode failure now increments `SerialGateway._payload_errors`, surfaced as `/health`'s
`link.payload_errors`, separate from `link.decode_errors`. M5 (aliasing `payload_errors` to
`decode_errors`) and M8 (deleting the `record_payload_error()` call) are both killed, so the
*distinction* — the whole point of the finding — is pinned, not just the field's existence.
See WARNING K and SUGGESTION P for what is still missing around it.

### WARNING D — the docstring asserts a totality the code does not have → **CLOSED**

`store.py:447–496`. Statement-by-statement re-derivation:

| Claim in the docstring | Verdict |
|---|---|
| "An unreadable row is skipped, logged with its node id, and counted … instead of propagating" | **TRUE** — 21 probes |
| "a narrower `except ValueError` missed three exception types CRITICAL A found (`OverflowError`, two `AttributeError` shapes)" | **TRUE** — P1/P2/P3 reproduced |
| "`BaseException` stays uncaught" | **TRUE** and accurate |
| "The roster query (`_known_node_ids`) … [is] inside this guard too now" | **TRUE** |
| "…and the loop over it" | **literally false, effectively true** — SUGGESTION M |
| "`event-store/spec.md`'s 'Registry rehydration at boot'" | **TRUE** — `specs/event-store/spec.md:74` |
| "`station-deployment/spec.md`'s S7" | **TRUE** — `specs/station-deployment/spec.md:11` |

The false `adapters/ingest.py::_apply_to_registry` precedent claim is **gone from production source**
(`rg _apply_to_registry src/` returns only `ingest.py`'s own definition and call). It was **not**
replaced with a new unverifiable claim — both new spec citations resolve to real requirement headings.
The claim does, however, survive verbatim in the task list — SUGGESTION O.

### SUGGESTION E — the per-call reset is unpinned → **CLOSED**

`tests/adapters/test_store.py:355–381`. M11 (deleting `store.py:497`'s `self.rehydration_errors = 0`)
fails `test_rehydration_errors_resets_each_call_not_accumulating`. The pin is real, not decorative.

### SUGGESTION F — the `None`-when-no-store branch → **NOT ADDRESSED (correctly)**

Explicitly left out of scope by the implementer (`apply-progress-batch1.md:217–218`). Still true,
still harmless. No action needed for Batch 1.

### SUGGESTION G — the TDD table misattributed the CLI test → **CLOSED**

`apply-progress-batch1.md:154–158` now carries the correction, naming `c150952` and stating why
`77fad51` cannot have contained it. Verified against `git show --stat 77fad51`: it touches only
`remediation-tasks.md`, `store.py` and `tests/adapters/test_store.py`.

### SUGGESTION H — spec paths → **NOTHING TO CLOSE; the closure claim is unearned**

`openspec/specs/` does contain only `.gitkeep`, and the specs do live at
`openspec/changes/field-station-p4/specs/…`. But the wrong paths were in the *orchestrator's brief*,
not in any repository file: `rg 'openspec/specs/'` finds exactly three hits, two of which are the
predecessor's own report quoting the brief, and the third (`proposal.md:75`, "`openspec/specs/` is
empty; this change establishes the first specs") is **correct**. Nothing in `504d18e..HEAD` touches
this. `apply-progress-batch1.md:216–217` listing H among the items "round 2 closes" is an unearned
claim — see SUGGESTION O.

---

## Part 2 — Mutation testing (12 mutations, all against the real package root)

Two survived.

| # | Mutation | Target | Result |
|---|---|---|---|
| M1 | `range(len(TrapState))` → `range(len(TrapState) - 1)` | `payloads.py:81` | **2 failed** ✅ |
| M2 | `if value not in _VALID_STATE_VALUES:` → `if value in {4, 7, 130, 255}:` | `payloads.py:85` | **496 failed** ✅ |
| M3 | per-step `except Exception` → `except ValueError` | `store.py:512` | **2 failed** ✅ |
| M4 | roster query moved back outside the guard | `store.py:498–503` | **1 failed** ✅ |
| M5 | `payload_errors=self._payload_errors` → `=self._frame_stream.decode_errors` | `serial_gateway.py:209` | **1 failed** ✅ |
| M6 | per-step `self.rehydration_errors += 1` deleted | `store.py:514` | **5 failed** ✅ |
| **M7** | **skip log → `logger.debug("rehydrate: skipped a row")`, node id and traceback dropped** | **`store.py:513`** | **729 passed, exit 0 — SURVIVED** ❌ |
| M8 | `record_payload_error()` call deleted | `ingest.py:101` | **1 failed** ✅ |
| **M9** | **drop log → `logger.debug("ingest: dropped a frame")`, exception dropped** | **`ingest.py:100`** | **729 passed, exit 0 — SURVIVED** ❌ |
| M10 | roster-branch `self.rehydration_errors += 1` deleted | `store.py:502` | **1 failed** ✅ |
| M11 | per-call reset `self.rehydration_errors = 0` deleted | `store.py:497` | **1 failed** ✅ |
| M12 | 5th `TrapState` member added, `_WIRE_STATE_ORDER` left at 4 | `traps.py:29` | **1 failed** ✅ |

M1 and M2 re-confirm the implementer's own re-verification figures exactly. M3–M12 are mine; the
implementer and the predecessor tried none of them. M7 and M9 are WARNING K.

Every mutation was applied to the real package root (never a worktree — see *Method note*), restored
with `git checkout --`, and `git status --porcelain` confirmed empty after each. `HEAD` was never
moved; nothing was committed.

---

## Part 3 — New findings

### CRITICAL I — a corrupt `station.db` still bricks the boot, in `Store.__init__`, before `rehydrate()` ever runs

**Where**: `src/feeder_station/cli.py:73` (`store = Store(args.db_path)`, unguarded);
`src/feeder_station/adapters/store.py:242` (`_ensure_schema(self._conn)`), `:125–128`
(`open_connection`); `deploy/feeder-station.service` (`Restart=always`, `RestartSec=2`).

`rehydrate()` is now total. **The line that opens the database is not.** `cli.build_app` constructs
`Store` before `create_app` exists, so a database SQLite cannot open raises out of `build_app`, out of
`main()`, with no app object, no lifespan, no `/health`, no UI.

Reproduced end to end, six ways:

| Probe | Corruption | Outcome | Raised at |
|---|---|---|---|
| N1 | first 16 bytes overwritten (header) | **BOOT FAILED** `sqlite3.DatabaseError: file is not a database` | `store.py:127` |
| N3 | 8 KiB of random bytes as `station.db` | **BOOT FAILED** `file is not a database` | `store.py:127` |
| N4 | file truncated to half its length | **BOOT FAILED** `database disk image is malformed` | `store.py:127` |
| N20 | pages 2–10 overwritten with `0xFF` | **BOOT FAILED** `database disk image is malformed` | `store.py:135` |
| N18 | `chmod 000` | **BOOT FAILED** `sqlite3.OperationalError: unable to open database file` | `store.py:125` |
| N19 | path is a directory | **BOOT FAILED** `unable to open database file` | `store.py:125` |

N2 (zero-length file) **boots** — a zero-length file is a valid empty SQLite database.

**Quantified, not asserted.** 300 independent single-bit flips at uniformly random offsets in a
realistic 45 056-byte `station.db` (10 nodes, 50 heartbeats, 10 pending `ARM`s), each booted through
`cli.build_app` + `TestClient`:

```
 287  (95.7%)  BOOTED, clean
   5  ( 1.7%)  BOOTED, corruption contained  <-- what Batch 1 bought
   8  ( 2.7%)  BOOT FAILED: sqlite3.DatabaseError: malformed database schema (…)
```

Every one of the 8 is `Store.__init__`. Their offsets cluster in the `sqlite_master` schema text on
page 1 (3776, 3913, 4031, …) — one flipped bit inside the stored `CREATE TABLE` text and SQLite
refuses the file outright: `malformed database schema (event) - near "(": syntax error`,
`(event_by_type) - no such column: sgq`, `(sqlite_autoindex_}eta_1) - orphan index`.

**So Batch 1's containment covers 5 of the 13 bit flips that cause any problem at all — 38%. The
other 62% still produce CRITICAL 2's exact failure, one layer up.**

**Concrete failure scenario.** `docs/requirements.md:94–96` names this risk in its own words:

> **A Raspberry Pi's SD card is the least reliable part of the system.** They corrupt under power
> loss, which is how most Pi deployments eventually fail.

1. A bit flips inside `station.db`'s schema page on the SD card (2.7% of single-bit flips).
2. The station reboots — the S7 case.
3. `cli.build_app` → `Store(args.db_path)` → `_ensure_schema` → `sqlite3.DatabaseError`.
4. Nothing catches it. `main()` never reaches `uvicorn.run`. The process exits non-zero.
5. `Restart=always`, `RestartSec=2`. Same file. **Infinite crash loop, no `/health`, no UI, no
   `journalctl` breadcrumb beyond a bare traceback, unrecoverable without a keyboard on site** —
   which is precisely what S7 says must never be needed.

**Scope — read this before deciding.** This is **not** a regression from this round, **not** inside the
predecessor's stated minimum bar for CRITICAL A (which named only `rehydrate()`'s guard, and that bar
*is* met), and **not** inside Batch 1's own written rule (`remediation-tasks.md:102`, "a boot that
cannot read **one row** must still be a boot" — a schema page is not a row). It is a pre-existing hole
that this round's brief explicitly directed me to look for, and it is bigger than the one that was
closed. Its fix is not a one-line widening: it needs a design decision about what a store-less
degraded boot means (`/api/traps` behaviour, whether the gateway thread should still run and write,
whether to quarantine the file and start a fresh one, what `/health` reports). That is a batch, not a
patch. **It must not block Batch 1's push and it must not be lost.** Recommended: schedule it as
Batch 5, or as a `2a` slice ahead of Batch 2 if the reviewer wants the crash-loop class fully closed
before more `rehydrate()` surgery lands.

### WARNING J — a skipped heartbeat replay displays a fabricated `idle`, violating trap-commands S4

**Where**: `store.py:504–514` (per-step skip granularity), `traps.py:42` (`state: TrapState =
TrapState.IDLE`), `traps.py:149–157` (`restore_capture` creates the trap), `app.py:353–357`.

`trap-commands/spec.md:14`: *"The trap's displayed state MUST always be whatever its own most recent
HEARTBEAT or HELLO reported."* `store.py:548–550` quotes that same rule in its own comment.

Skip granularity is per **replay step**, not per **node**. When step 1 is skipped but steps 2/3
succeed, the trap is still created — by `restore_capture` or by a desired-command replay — with
`Trap`'s dataclass default, and `TrapState`'s default is `IDLE`. Reproduced on a store holding one
node whose last heartbeat reported `FAULT` and whose `HEARTBEAT` row is then made unreadable:

```
BEFORE: {"node_id": 7, "state": "fault", "battery_mv": 3100, "targets_version": 9,
         "last_seen": "2023-11-14T22:13:20+00:00", "capture": {...}}
AFTER:  {"node_id": 7, "state": "idle",  "battery_mv": null, "targets_version": null,
         "last_seen": null,                                  "capture": {...}}
   /health: rehydration_errors = 1, ok = true
```

`battery_mv: null`, `targets_version: null` and `last_seen: null` all read honestly as "no data".
**`state: "idle"` does not** — it is indistinguishable from a trap that genuinely reported IDLE, and
it is the *least* alarming of the four states. A biologist opening the UI sees a calm trap.

**Inputs → wrong outcome.** A trap enters `FAULT` and stops transmitting (which is what a faulted trap
does). Its `HEARTBEAT` row takes a bit flip. The station restarts. The UI shows `idle` **indefinitely**
— the only thing that corrects `state` is the trap's next heartbeat, and a dead trap has none.
`reachable: false` is visible alongside, which is the mitigating signal, but a station full of
unreachable traps at boot is the normal case (S11 — reachability is deliberately never rehydrated),
so it carries no information here.

**Round-1 origin, round-2 amplification.** Round 1's `except ValueError` already produced this for a
`state=7` row; the predecessor did not flag it. `except Exception` widens the set of rows that reach
it. Suggested close: on a skipped step-1, leave the node out of the registry entirely, or carry an
explicit "state unknown" so `/api/traps` cannot present a default as a report.

### WARNING K — the logging that justifies `except Exception` is entirely unpinned, and `rehydration_errors` cannot distinguish one bad row from a lost registry

**Where**: `store.py:501, 513`; `ingest.py:100`; `app.py:341–342, 344–351`.

Two halves, same root cause: the observability that was accepted as the price of the broad catch is
asserted by nobody.

**(a) No test pins the logging.** M7 downgrades `rehydrate`'s skip log to
`logger.debug("rehydrate: skipped a row")` — dropping the node id *and* the traceback — and the suite
stays at **729 passed, exit 0**. M9 does the same to `ingest.py`'s drop log: **729 passed, exit 0**.
`rg 'caplog|LogCaptureFixture' tests/` finds nothing. The docstring at `store.py:477–480` sells
"skipped, **logged with its node id**, and counted" as the mitigation; the "counted" half is pinned
five ways (M6, M10, M11), the "logged" half not at all.

The levels are correct *today*: `logger.exception` emits at ERROR with a full traceback and
`logger.warning` at WARNING, and since nothing in `src/` calls `logging.basicConfig` and uvicorn's
default config leaves the root logger alone, both reach `logging.lastResort` → stderr → journald. So
today, a genuine programming bug in the replay path **is** diagnosable: R3 (a deliberately typo'd
attribute, `registry.heartbeatt`) boots with `rehydration_errors=1` and a full
`AttributeError: 'Registry' object has no attribute 'heartbeatt'. Did you mean: 'heartbeat'?`
traceback in the journal. That is the right answer to the question — **but it holds by accident of
Python's defaults and by nobody having edited the line, not by anything the suite would notice.**

**(b) `rehydration_errors: 1` means two completely different things.** The roster-failure branch
(`store.py:500–503`) counts `1` and returns, so the registry comes up **empty**. Measured on a store
with 20 nodes and 20 pending `ARM` commands:

| | `/health.ok` | `/health.rehydration_errors` | `/api/traps` |
|---|---|---|---|
| one unreadable heartbeat row out of 20 nodes | `true` | `1` | **20** |
| roster query unreadable — whole registry lost | `true` | `1` | **0** |

From `/health` alone these are byte-identical. Before this round the second case crashed the boot:
loud, unmissable, and the thing CRITICAL A was raised to change. Now it is a silent empty station
claiming `ok: true`, with every pending operator command gone until re-issued and the scheduler
polling nobody. The `/api/traps` count is the only tell, and only to someone who knows how many traps
there should be.

Suggested close: one `caplog` assertion per log site (node id present, level ≥ WARNING, exception
chained); and either a distinct signal for the roster branch (`roster_unreadable: true`, or a count of
nodes expected vs rehydrated) or folding it into `ok`.

### SUGGESTION M — the `rehydrate` docstring's "and the loop over it" is literally false

`store.py:487–489`: *"The roster query (`_known_node_ids`) and the loop over it are inside this guard
too now."* The query is (`:498–503`). The loop is **not** — `for node_id in node_ids:` sits at `:504`,
after the `except`. The statement is *effectively* true only because `_known_node_ids` returns a fully
materialised `list[int]` (`:518`), so iterating it cannot raise. A docstring rewritten specifically
because its predecessor overclaimed should not need that footnote. Either say "the roster query", or
move the loop inside.

### SUGGESTION N — `apply-progress-batch1.md`'s explanation of the `test_app.py` breakage is wrong

`apply-progress-batch1.md:57–58`: *"required because `start_workers` now accesses that attribute on
whatever `gateway` it is given"*. It does not. `workers.py:291`'s `gateway.record_payload_error` sits
**inside the `lambda`** passed to `threading.Thread`, so it is evaluated on the worker thread, inside
`run_supervised`'s `except BaseException`. Proven directly: `start_workers` called with a
gateway-shaped object lacking the method **returns normally**; the gateway thread then dies on
`.start()` and `gateway_status.last_error` reads
`"AttributeError: 'LegacyGateway' object has no attribute 'record_payload_error'"`, which `/health`
surfaces as `workers.gateway.alive: false`, `ok: false` — the designed 12.0c behaviour, not a brick.

This *improves* the answer to the coupling question rather than worsening it, which is why it is a
SUGGESTION and not a WARNING. **No production caller or gateway-shaped object can now fail at
runtime**: `run_gateway_loop` has exactly one production caller (`workers.py:282`), `LinkState` has
exactly one production constructor (`serial_gateway.py:202`), `app.py` only ever receives
`SerialGateway | None`, `cli.build_app:81` only ever constructs a real `SerialGateway`, and
`mypy src` is clean at 0. But the record now carries a false mechanism claim — the same defect class
WARNING D was raised for, one document over.

### SUGGESTION O — three statements in the record are still false or unearned

1. `remediation-tasks.md:116–119` (task 1.2) still says the existing `decode_errors` counter *"picks
   it up for free — no new counter needed at this layer."* A new counter **was** needed and was added
   (`payload_errors`). WARNING C's own suggested close offered exactly this alternative — *"amend task
   1.2's text — it is currently a false statement in the record"* — and it was not taken.
2. `remediation-tasks.md:126–128` (task 1.4) still says *"the same best-effort discipline
   `adapters/ingest.py::_apply_to_registry` already has for the live path."* It still has none;
   `ingest.py:132–157` contains zero `try`. The production docstring was corrected; its source was not.
3. `apply-progress-batch1.md:216–217` lists SUGGESTION H among what round 2 closes. Nothing in
   `504d18e..HEAD` touches it, and there was nothing in-repo to touch.

The `remediation-tasks.md` diff is **only** the six `[ ]`→`[x]` ticks — no Batch 2/3/4 boxes touched,
no text edited. Scope discipline is clean; the text is stale.

### SUGGESTION P — `payload_errors` is documented nowhere an operator would find it

`rg 'payload_errors|decode_errors' docs/ README.md src/feeder_station/static/index.html
openspec/changes/field-station-p4/specs/` returns **nothing**. `docs/deploy.md` is the runbook and
mentions `/health` four times (`:13, :16, :72, :87`), naming only `workers.gateway.enabled`. The
operator UI shows only `connected`/`last_error` (Appendix B, SUGGESTION 13). So the distinction
WARNING C existed to create — payload-level vs COBS-level failure — is real, pinned (M5), and
invisible to everyone except someone reading `serial_gateway.py`. `decode_errors` was equally
undocumented before, so this is not a regression; but the finding is only half-closed until the
runbook says which number means "the radio link is noisy" and which means "a trap is speaking a
protocol this station does not understand."

### SUGGESTION Q — `store.trap_state_from_wire` accepts negative indices; `payloads._require_valid_state` does not

`store.py:220–224` uses `_WIRE_STATE_ORDER[value]`, so Python's negative indexing applies:

```
trap_state_from_wire(-1) -> TrapState.FAULT     trap_state_from_wire(-3) -> TrapState.ARMED
trap_state_from_wire(-2) -> TrapState.FIRED     trap_state_from_wire(-4) -> TrapState.IDLE
trap_state_from_wire(True) -> TrapState.ARMED   trap_state_from_wire(False) -> TrapState.IDLE
```

Verified end to end (probe N15): a stored row with `fields = {"state": -1}` rehydrates to `FAULT` with
`rehydration_errors = 0` — no error, no log, no count. `payloads._VALID_STATE_VALUES` correctly rejects
`-1` (`-1 in range(4)` is `False`), so the two guards on the same domain disagree. The wire path is
bounded (`payload[0]` is 0–255), so this is reachable only from a corrupt or pre-1.2 stored row — the
same family as WARNING J, and the same "rule, not example" shape WARNING B fixed one layer over.
One line: `if not 0 <= value < len(_WIRE_STATE_ORDER): raise`.

### SUGGESTION R — `_require_valid_state`'s message hardcodes a range the code derives

`payloads.py:86–88` raises `"… has no known TrapState (expected 0-3)"` while `_VALID_STATE_VALUES` is
`range(len(TrapState))`. The whole point of WARNING B's fix was that a hand-written `4` drifts. The
message carries the same literal and will lie the day a 5th member lands — in the one artefact an
operator actually reads. Interpolate the range.

---

## Part 4 — The five checks the brief named, answered

| # | Check | Answer |
|---|---|---|
| 1 | Is CRITICAL A closed for real? | **Yes for `rehydrate()`** — 21 probes, no escape, no reachable `BaseException`. **No for the boot as a whole** — CRITICAL I |
| 2 | Did `except Exception` swallow something it should not? | **Yes, twice**: the logging that justifies it is unpinned (WARNING K a), and a skipped step-1 fabricates `state: "idle"` (WARNING J). A *programming* bug is diagnosable in the journal (R3), invisible in `/health` |
| 3 | Are the mutation kills real? | **Yes** — M1 → 2 failed, M2 → 496 failed, both re-run in the real package root. Eight further mutations invented; **two survived** (M7, M9) |
| 4 | Is the test-count inflation honest? | **Yes, and disclosed.** 512 of 729 collected tests (70.2%) are the four new parametrized payload tests; 504 are the exhaustive reject sweep. Suite still runs in 4.9–5.7 s; the 15 slowest are all pre-existing integration tests. No case is vacuous (M2 kills all 504). But "729 passed" is no longer a coverage metric — the round added ~8 tests' worth of distinct behaviour, not 511. Minor: the reject tests use bare `pytest.raises(PayloadError)` with no `match=` |
| 5 | Did the formatter sweep anything unintended? | **No.** Zero production lines changed without semantic reason; `ruff format --check` drift is the same 6 files with byte-identical bodies, differing only by a 1-line offset from one added import |
| 6 | Did WARNING C's fix create a coupling problem? | **No production risk** — one caller, one `LinkState` constructor, `mypy src` clean. The implementer's stated mechanism is wrong (SUGGESTION N). `payload_errors` is genuinely distinguishable and pinned (M5), but documented nowhere (SUGGESTION P) |
| 7 | Is the `rehydrate` docstring now true? | **Yes**, except one clause (SUGGESTION M). The false `_apply_to_registry` claim is gone from source and was not replaced with a new unverifiable claim; both new spec citations resolve |
| 8 | Did any existing test's expected outcome change? | **No.** `git diff --numstat 504d18e..HEAD -- tests/` = `22/0, 13/0, 89/1, 2/0, 85/0, 8/0, 134/0`. The **single** deleted line is `-from feeder_station.adapters.store import Store`, replaced by `+from … import Store, trap_state_from_wire`. `test_registry_reflects_last_known_state_after_restart` does not appear in the diff at all and still asserts `TrapState.ARMED` (`tests/adapters/test_store.py:238`) |
| 9 | Does SUGGESTION E's pin actually pin? | **Yes** — M11 |

## Part 5 — Task completeness

| Task | Boxed | Code state agrees | Note |
|---|---|---|---|
| 1.1 | `[x]` | Yes | Rule now encoded, not examples — M1/M2 both killed |
| 1.2 | `[x]` | Yes | Validation lands; the task's own `decode_errors` rationale is still false in the record → SUGGESTION O |
| 1.3 | `[x]` | Yes | Four end-to-end boot tests, all mutation-confirmed non-vacuous (M3, M4, M6, M10) |
| 1.4 | `[x]` | **Yes** | Guard is total for a row. Boot is not total for a file → CRITICAL I |
| 1.5 | `[x]` | Yes | Field lands and is pinned; cannot distinguish one row from a lost roster → WARNING K |
| 1.6 | `[x]` | Yes | Docstring true except one clause → SUGGESTION M |

---

## Gate decisions

### May Batch 1 be pushed? — **Yes.**

Every finding raised against it is closed, and closed with evidence rather than assertion: eight
mutations I invented could not find a hole in the four fixes, and 21 boot probes could not make
`rehydrate()` raise. Measured against its own baseline it contains 5 of every 300 single-bit flips
that previously killed the station and breaks nothing: zero existing assertions changed, zero new
format drift, all four gates at their baseline status. Its headline claim — *"a restart that cannot
read one row must still be a restart"* — is now, scoped exactly as written, **true**.

Hold WARNING J and WARNING K for a follow-up commit; neither is a regression and neither is worth
blocking a crash-loop fix that is already three rounds old. SUGGESTION O's two false task-list
statements should be amended in the same commit that ticks Batch 2, since they are one `sd` away and
they are what the next reader will believe.

### May Batch 2 start? — **Yes.**

The hard-sequencing precondition at `remediation-tasks.md:63–68` is met on its own terms:

> writing Batch 2's tests against a `rehydrate()` that can still raise on a bad row means testing over
> a landmine Batch 1 exists to defuse.

`rehydrate()` can no longer raise on a bad row — for `OverflowError`, `AttributeError`, `TypeError`,
`sqlite3.Error`, `RecursionError`, `JSONDecodeError`, `ValueError`, or anything else short of
`BaseException`. Batch 2's task 2.2 introduces a `MAX(seq) WHERE msg_type IN (…)` query and a new
`Registry` method — both new ways for a step to raise, and both now land inside a guard that no longer
under-covers. The landmine is defused.

**Two conditions.** (1) Batch 2's five-ordering property test boots repeatedly through `build_app` and
will therefore *construct `Store` repeatedly* — CRITICAL I's surface, not `rehydrate()`'s. If any of
those orderings is later extended to corrupt the file rather than a row, the test will fail in
`build_app` and the failure will look like a Batch 2 bug. Note it in the test. (2) Batch 2 restructures
the exact method WARNING J lives in; whoever writes task 2.2 should decide the skip *granularity*
question (per-step vs per-node) deliberately rather than inherit it, because "which single row wins the
state race" and "what happens when that row is unreadable" are the same design decision.

### Is CRITICAL I a blocker for anything? — **Not for Batch 1 or Batch 2. For the change.**

It cannot be archived with `station-deployment/spec.md`'s S7 requirement marked satisfied while 2.7%
of single-bit flips produce an infinite crash loop with no `/health`. Schedule it explicitly — as
Batch 5, or as a `2a` slice ahead of Batch 2 if the reviewer wants the whole crash-loop class closed
before more `rehydrate()` surgery lands. Do not fold it into Batch 2: it is a different method, a
different layer, and it needs a design decision about what a degraded, store-less boot means.

---

## Method note

The venv installs `feeder_station` as an **editable install with a `sys.meta_path` finder**
(`.venv/lib/python3.13/site-packages/_editable_impl_feeder_field_station.pth`) pinned to the main
repo's `src/`. Mutations applied inside a `git worktree` are silently ignored — pytest imports the
unmutated main-repo package and reports a false green. Every one of the 12 mutations above was applied
to the real package root, restored with `git checkout --`, with `git status --porcelain` confirmed
empty after each. The `504d18e` worktree was used **only** for `ruff format --check`, which reads
files directly and does not import the package.

`HEAD` was never moved. Nothing was committed. Nothing was fixed. The working tree is clean; this
report is the only file left on disk, untracked.
