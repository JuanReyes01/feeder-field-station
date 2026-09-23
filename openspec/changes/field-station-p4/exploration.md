# Exploration — field station P4

Phase: `sdd-explore`. Date: 2026-09-23. Status: **partial** — ready for propose, with one scope
question that must be answered first (see §3).

Scope agreed with the user: the complete field station — persistence, radio gateway and operator UI.

## Where the code stands

`src/feeder_station/` holds a pure wire-frame codec (`domain/frames.py`, byte-exact against the
firmware's own test vectors) and a pure in-memory trap registry (`domain/traps.py`: state from
heartbeats only per S4, capture as persistent state per S3). `app.py` is FastAPI plus SSE with no
persistence and no radio; the registry lives in process memory and is lost on restart. 17 tests
pass; ruff and mypy strict are clean.

Nothing about storage, serial I/O, clock trust or systemd exists yet, and there is no `adapters/`
package.

**Two upstream gaps this phase is coupled to**, found on disk rather than assumed:

- `feeder-node-firmware/components/` contains only `trigger_link`, `link_frame` and `rfid_frame`.
  That repo's README lists a `station_link/` module; it is not written yet.
- No gateway-side ESP32 image exists anywhere. P4 cannot be tested truly end to end until it does —
  though this repo can build and test its adapters against a loopback serial device.

---

## 1. Persistence (S9)

S9: nothing is acknowledged to a trap before it is durably stored, and the database survives
repeated power loss on an SD card.

| Option | Durability | SD wear | Fits dedup + cursor | Effort |
|---|---|---|---|---|
| SQLite, rollback journal, `synchronous=FULL` | Full | High: several fsyncs per commit | Yes, with an index | Low |
| **SQLite, WAL, `synchronous=FULL`** | **Full** | **Lower: one fsync of a small appended WAL per commit** | **Yes, free via a UNIQUE key** | **Low** |
| SQLite, WAL, `synchronous=NORMAL` | **Insufficient for S9** — SQLite's own documentation says NORMAL stays uncorrupted but can lose the most recent committed transaction on power loss | Lowest | Yes | Low |
| Hand-rolled append-only log with fsync per line | Full if done right | Possibly lowest | No — dedup, cursor and a boot-time index all reinvented | Medium-high |

**Recommendation: SQLite, WAL, `synchronous=FULL`.** It is the only option that gives S9's exact
guarantee at lower card wear than the rollback journal, and a `(node_id, msg_id)` UNIQUE constraint
hands us both deduplication and the `GET_BACKLOG` query
(`WHERE node_id=? AND msg_id>? ORDER BY msg_id LIMIT ?`) for nothing.

**The ordering S9 dictates:** receive frame → **write and fsync** → *then* ACK over the radio →
update the in-memory registry → publish SSE. Only the first two steps are S9-critical; the rest may
be best effort.

**A trap hidden inside WAL, affecting S14.** With WAL, recently committed rows can live only in
`station.db-wal` and not yet in `station.db`. Copying `station.db` to a USB drive while the station
runs can therefore silently omit acknowledged events — precisely what S9 forbids. S14's export must
use SQLite's backup API or `VACUUM INTO`, or force `PRAGMA wal_checkpoint(TRUNCATE)` before any
plain file copy. This belongs in the spec, not in the judgement of whoever writes the export.

**Automating S9's acceptance test — honestly, in two tiers.** The criterion as written ("pull the
plug repeatedly") is a hardware test: what it really exercises is whether the SD card's controller
honours flush the way SQLite assumes, which cheap cards are known to lie about. No software
simulation replaces it.

1. **Crash-consistency proxy, runnable in CI today:** drive frames through the real write path,
   `kill -9` at randomised points, reopen, assert `PRAGMA integrity_check` is `ok` and that every
   event the harness saw acknowledged is present. Tests our code, not the medium.
2. **The physical gate:** a switchable plug cutting power to the real Pi under load, repeatedly.
   No such rig exists for this project yet. It is hardware in the loop, not a unit test.

---

## 2. The radio gateway

The station reaches the nodes through an ESP32 acting as a USB-serial modem.

**The gap this exposes in our own protocol:** the frame in `messages.md` has **no delimiter and no
checksum**. Over ESP-NOW that is fine — the radio hands the receiver a whole packet. **USB serial
does not**: the operating system delivers a byte stream with no packet boundaries, so the frame
format alone cannot be parsed safely from a live serial line.

| Framing | Resynchronises after corruption or a replug | Overhead | Effort |
|---|---|---|---|
| None — read 9 bytes, then `payload_len` | **No.** One lost byte desynchronises the parser until the port is reopened | Zero | Lowest |
| SLIP-style delimiter with escaping | Yes | Variable: escapes every literal delimiter byte, and tag bytes are arbitrary binary | Medium |
| **COBS** | **Yes, self-synchronising at the next delimiter** | **About one byte for frames this size** | **Low** |

**Recommendation: COBS**, with the codec in a pure `domain/serial_framing.py` — same pattern as
`frames.py`, deterministic and testable with fixed byte vectors — and the pyserial I/O, device
lookup and reconnect-with-backoff loop in an impure `adapters/serial_gateway.py`.

**When the device vanishes:** unplugging a USB-CDC device raises `SerialException`, and
`/dev/ttyUSB0` is not guaranteed to come back under the same name when several serial devices are
present. Resolve by `/dev/serial/by-id` or VID:PID and reopen with backoff. The gateway does **not**
need to guarantee zero byte loss on a hot unplug: the protocol already assumes an unreliable link
(node-side buffering, ACK with retry, `GET_BACKLOG` recovery). It needs to **notice honestly** and
let those mechanisms work.

---

## 3. The event model, and an S6 question that blocks the spec

Facts from `messages.md`: the uplink catalogue is `HELLO, HEARTBEAT, VISIT, CAPTURED, FAULT, ACK`.
**There is no per-reading message**, which matches design ruling 2: the radio carries events, never
raw readings.

### A protocol defect: `msg_id` wraps inside one field season

`msg_id` is **two bytes** (offset 4-5). The design's own arithmetic puts a node at about **200
events a day**. That wraps the counter in **roughly 327 days** — under a single field season, and R7
has the nodes awake permanently.

Neither `messages.md` nor `design.md` mentions this. The station's storage **must not** treat the
wire `msg_id` as globally unique or monotonic: it needs its own ever-increasing sequence and should
compare the wire value only inside a bounded recent window. Widening the wire field is a firmware
change and out of this repo's scope, but the schema has to be designed around it now.

### The S6 tension — needs a decision before `sdd-spec`

S6 says the station must write the raw file format the analysis app ingests. But:

- The real raw format is **per reading**: `id,<unit>,` followed by `rfid,<DD.MM.YYYY HH:MM:SS>,<14
  hex>`, `night start`, `time update`. One row per individual read.
- By ruling 2 the station only ever receives **collapsed visit and capture events**. It structurally
  **cannot** reproduce a per-reading file: it never sees the individual reads.
- The design's own network diagram draws **two separate arrows** into the analysis app: the node's
  SD card collected by hand, *and* the station's event exports. That reads as two different exports,
  not one replacing the other.
- `messages.md` has **no bulk file transfer message**. `GET_BACKLOG` moves event cursors, not files.
  There is no wire mechanism for the station to pull a node's raw CSV at all.

So either S6 means something other than "synthesise a raw-ingest CSV from visit events" — implying a
transfer mechanism the protocol does not have — or its acceptance criterion should be re-scoped to
**the station's own event export**, a simpler format not gated by the raw-ingest spec. This is a
decision, not a detail, and it changes what the event store is for.

**Storage volume is trivial either way:** about 2,000 events a day network-wide at ~50 bytes is well
under a megabyte a day. Retention (requirements open question 5) affects ergonomics, not sizing.

---

## 4. Clock discipline (S12, S15)

The chain is DS3231 on the station (about 2 ppm, a minute a year) carrying time across outages, NTP
correcting it when the internet appears, and S12 as the floor.

**A protocol fact that shapes S12:** `TIME_SYNC` carries only `epoch`. There is no confidence field
and no "clock unset" reply. So "refuses to answer" means **withholding the downlink**, because the
protocol has no frame in which to refuse. Worth stating explicitly in the spec.

Two gaps, named rather than invented:

- There is no documented I²C wiring, device-tree overlay or `hwclock` sequence for the station's own
  DS3231. `docs/hardware/wiring.md` covers only the two node devices.
- The PCF8523's drift is not stated anywhere; only the DS3231's is.

**A documentation inconsistency for whoever owns the protocol:** `messages.md` still defines
`clock_source: 2 = GPS`, but the design removed GPS from the nodes.

---

## 5. Auto-start and recovery (S7)

`pyproject.toml` already exposes a `feeder-station` console script bound to `0.0.0.0`, so a systemd
unit with `Restart=always` satisfies S7's literal test once installed. The costs, plainly:

- No `.service` file and no documented install path exist yet.
- `Restart=always` is **not** a substitute for S9. It is safe only *because* the fsync discipline
  makes crash-then-restart safe.
- Order it `After=network.target`, **never** `Requires=network-online.target`: S8 forbids blocking
  on the internet, and the wait-online services have real timeouts that would delay an offline boot.
- Clock trust belongs in a runtime check on each relevant request, not in unit ordering.
- The target Raspberry Pi OS image is unknown, which changes real specifics (`chrony` versus `ntp`,
  `/boot/firmware/config.txt` versus `/boot/config.txt`).

---

## Risks

| Risk | Why it matters |
|---|---|
| **S6 scope ambiguity** | Could produce a "station export" that operators mistake for the scientific record, or that the analysis app rejects despite our own tests passing |
| **`msg_id` wraps in ~327 days** | A protocol defect shared with the firmware; the schema must be designed around it now |
| **Two upstream firmware gaps** | No `station_link`, no gateway image: no true end-to-end test yet |
| **S9's real test needs hardware** | The rig does not exist; CI can only prove crash consistency, not that the card tells the truth about flushing |

## Explicitly not determined

- The SD card model's real behaviour under power loss.
- Which USB-UART chip the ESP32 board carries, which decides whether `/dev/serial/by-id` is stable.
- The target Raspberry Pi OS image and version.
- The PCF8523's drift specification.

## Next

`sdd-propose` — **after** the S6 scope question in §3 is answered or recorded as an explicit
assumption.
