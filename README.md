# feeder-field-station

The field station of the CICG feeder project. Runs on a Raspberry Pi at the study site, permanently
powered, and is the only always-on part of the network. It is the gateway to the feeder nodes, the
local UI for arming traps and editing target tags, and the place alerts come from.

**Nothing is implemented yet.** This README exists so that whoever starts knows where the contracts
are before writing code.

## Where the rest of the project is documented

- [Design](https://github.com/JuanReyes01/cicg-feeder-project/blob/main/docs/design.md)
  — requirements, architecture and roadmap, including what this station owns and, just as
  important, what it must not do
- [Node ↔ station protocol](https://github.com/JuanReyes01/cicg-feeder-project/blob/main/docs/protocol/messages.md)
  — the frames it exchanges with the nodes

Two boundaries are worth repeating here because breaking them is easy and expensive: the station
**never decides when a trap fires**, and it **never invents a data format** — it writes the CSV
layout [feeder-analysis](https://github.com/JuanReyes01/feeder-analysis) already ingests, so the
existing pipeline and its parity gate keep working.

## Open decisions

- Language and stack. Python keeps it close to the analysis app and the people who maintain it.
- Which radio the Pi uses, and whether it is a USB/SPI LoRa hat or an ESP32 acting as a serial
  bridge.
- How an alert reaches a person when nobody is looking at the screen.

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
  domain/frames.py   the wire frame, the Python half of the firmware's link_frame
  domain/traps.py    what the station believes about each trap, and why
  app.py             HTTP and Server-Sent Events
  cli.py             entry point
```

`domain/` is pure logic with no FastAPI and no I/O, which is what makes it
testable without a radio, a trap or a browser.

**The frame tests use the firmware's own byte vectors.** Two languages implement
one contract; `tests/domain/test_frames.py` is what stops them drifting apart.
