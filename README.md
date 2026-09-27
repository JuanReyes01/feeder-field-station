# feeder-field-station

The field station of the CICG feeder project. Runs on a Raspberry Pi at the study site, permanently
powered, and is the only always-on part of the network. It is the gateway to the feeder nodes, the
local UI for arming traps and editing target tags, and the place alerts come from.

**Status: shipped and running.** The station serves the SQLite event store, the serial gateway to the
nodes, the command sender, clock trust, the operator UI at `/`, CSV + snapshot export and the
specimen roster. `docs/requirements.md` holds the acceptance criteria, `docs/deploy.md` is the install
runbook, and `openspec/changes/field-station-p4/` is the change that built it. The contracts below are
what must not drift.

This paragraph read **"Nothing is implemented yet"** until 2026-09-27. It was written on 2026-09-22
and stayed there through 117 commits while the service was built underneath it — by then this same
file's own "## Running it" block was already telling you to run `uv run feeder-station`. That is not a
harmless stale line: whoever believes it either starts a second implementation of the store, the
gateway and the roster UI, or writes the whole file off as obsolete and loses the two boundaries below
with it.

## Where the rest of the project is documented

- [Design](https://github.com/JuanReyes01/cicg-feeder-project/blob/main/docs/design.md)
  — requirements, architecture and roadmap, including what this station owns and, just as
  important, what it must not do
- [Node ↔ station protocol](https://github.com/JuanReyes01/cicg-feeder-project/blob/main/docs/protocol/messages.md)
  — the frames it exchanges with the nodes

Two boundaries are worth repeating here because breaking them is easy and expensive: the station
**never decides when a trap fires**, and it **is not the scientific record**. The raw per-reading CSV
layout [feeder-analysis](https://github.com/JuanReyes01/feeder-analysis) ingests, and its parity gate,
are a contract between the *node* and the analysis app (design.md §3.7, R8): the node writes it to its
own SD card and it is collected by hand. The station exports only what it witnessed — visits, captures,
trap-state changes — as CSV in its own labelled format, alongside a consistent-snapshot copy of the
whole database (S6, re-scoped 2026-09-23 by design decision D1; `docs/requirements.md` S6,
`adapters/export.py`). It must never try to synthesise the raw-ingest file, because the radio carries
only collapsed events and never an individual reader pass.

This used to read that the station "never invents a data format" and writes the layout
`feeder-analysis` already ingests. It was wrong for this repo even before D1: it took a node-level
sentence out of design.md §3.7 and pinned it on the station, which is not in the parity loop at all.
Left standing it is actionable in both directions — feed `station-export-*/events.csv` to the parity
gate and file a bug when it will not load, or "fix" `adapters/export.py` back to the per-reading
layout and reopen a ruling that exists because the radio has no message for a single reader pass.

## Settled

- **Language and stack.** Python 3.13, FastAPI, SQLite (`pyproject.toml`). Python keeps it close to
  the analysis app and the people who maintain it.
- **The link to the nodes.** The Pi uses no radio of its own. A gateway ESP32 sits on a USB serial
  cable at 115200 baud — `--transport serial`, which is the only choice the flag has — and the trap
  reaches that gateway over ESP-NOW. `config.py`'s `SERIAL_*` block carries the airtime arithmetic,
  and `cli.py`'s `--transport` help carries the reason the Wi-Fi/TCP uplink was removed: associating
  with an AP drags an ESP32's radio onto the AP's channel, the same radio ESP-NOW needs.

Both of those sat under **Open decisions** until 2026-09-27, long after they were built and measured.
Presented as open, the link layer reads as unbuilt and unbought — which invites someone to size or
order a LoRa hat and design a second gateway path around a serial gateway that already works, and
hides that the channel budget in `config.py` is a cable's throughput, not a radio duty cycle.

## Open decisions

- Whether LoRa is bought at all — P5's range survey decides that, and design.md is explicit that no
  LoRa is bought yet — and if it is, whether the module sits on a Pi hat or on the gateway ESP32.
  `config.py`'s `DEFAULT_CHANNEL_BUDGET_FRAMES_PER_S_LORA` exists for that case; today's default
  budget is `..._SERIAL`, the cable's own throughput.
- How an alert reaches a person when nobody is looking at the screen. `docs/requirements.md` records
  this gap deliberately rather than pretending SSE closes it.

## Running it

```bash
uv sync
uv run feeder-station          # serves on 0.0.0.0:8000
uv run pytest -q               # the tests
```

Open `http://<the station's address>:8000` from a phone on the same network.

## Layout

```
src/feeder_station/
  domain/              pure logic: frames, payloads, serial framing, traps,
                       commands, backlog, pacing, clock trust, events
  adapters/store.py    the append-only event log and its schema; every trap
                       state is derived from it, never stored (DD2, S9)
  adapters/ingest.py   the only place that knows the S9 order:
                       write + fsync -> ACK -> registry -> SSE
  adapters/serial_gateway.py  COBS transport to the ESP32 USB-CDC modem
  adapters/workers.py  the gateway and scheduler threads, and their wiring
  adapters/command_sender.py  pending commands -> real downlink frames
  adapters/scheduler.py  backlog rotation -> GET_BACKLOG frames
  adapters/clock.py    DS3231 / NTP trust, and the gate on TIME_SYNC
  adapters/export.py   the event record copied to removable media
  config.py            values that must not be literals inside other modules
  app.py               HTTP routes and Server-Sent Events; the page itself is
                       static/index.html
  static/index.html    the operator page (DD1, slice 10 — a 400-line HTML
                       string in app.py was not reviewable as Python)
  cli.py               entry point; also where the gateway is turned on
```

`domain/` is pure logic with no FastAPI and no I/O, which is what makes it
testable without a radio, a trap or a browser.

This map listed four files until 2026-09-27, and the one pointer it gave for the UI sent you to
`app.py`. That is the expensive kind of wrong: a markup change made there comes back as a Python HTML
string, which is exactly what DD1 removed.

**The frame tests use the firmware's own byte vectors.** Two languages implement
one contract; `tests/domain/test_frames.py` is what stops them drifting apart.
