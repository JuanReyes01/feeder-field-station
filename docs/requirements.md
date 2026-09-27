# Field station — requirements

Status: draft, 2026-09-23. Not yet agreed with the client.

The system-level requirements R1-R8 live in the
[project design document](https://github.com/JuanReyes01/cicg-feeder-project/blob/main/docs/design.md)
and are not repeated here. This document covers what the **station** must do: the ones it
implements on behalf of the system, and the ones it has purely by being the thing that stays awake.

Every requirement below carries an acceptance criterion. One that cannot be checked is an intention,
not a requirement.

## What the station is

The only always-on device in the network. It holds the radio link to every trap, it is where an
operator arms them and edits target tags, it stores what the traps report, and it is what turns a
capture into a person finding out.

It is **not** the analysis app, and it does **not** decide when a trap fires.

---

## Implemented on behalf of the system

| ID | Requirement | Accepted when |
|----|-------------|---------------|
| S1 | Arm and disarm a named trap (R2) | An operator arms a trap from the UI; the trap's next heartbeat reports `armed`, and the UI shows it as armed rather than as commanded |
| S2 | Edit the target-tag list **the whole network hunts** (R3) — one shared wanted set, marked on the specimen roster, not a list typed per trap (**re-scoped 2026-09-26 by messages.md's open question 2**: "the study decides which animals are of interest, not the feeder"; the list is one, the *confirmation* stays per trap) | An edited list reaches **every known trap** and the UI distinguishes **delivered** from **pending** per trap, by comparing the version the trap reports with the version the station holds |
| S3 | Notify a person on capture, naming the trap (R4) | A capture appears **at once** in every browser that has the interface open, over plain HTTP on the local network, naming the trap and the tag — **and** is still there for anyone who opens the interface hours later |
| S4 | Show which traps are armed (R5) | The UI shows, per trap: armed state, battery, clock, target-list version and time since last contact — all sourced from the trap's own heartbeat, never from what the station last commanded |
| S5 | Serve at least 10 traps (R6) | Ten traps report and are commanded without the operator waiting on the UI |
| S6 | Export the station's own witnessed events — visits, captures, trap-state changes — as CSV, alongside a consistent-snapshot copy of the database (R8's station half, **re-scoped 2026-09-23 by design decision D1**: the radio only ever carries collapsed events, never an individual reader pass, so the station structurally cannot produce the per-reading raw-ingest file `feeder-analysis` wants; that stays the node's own SD card, collected by hand) | A produced export's CSV contains exactly the visit, capture and trap-state-change events the station has stored, labelled as the station's own record, and nothing else |
| S16 | The specimen roster: one row per tag ever read, and marking a specimen is how targets are chosen (R3) | The station holds exactly one row per tag it has ever read — created by the read itself, in the same transaction, never pre-registered by hand (`PATCH /api/specimens/{tag}` 404s an unknown tag); `species` and `band_number` are the only fields an operator may edit and are blank until someone fills them in, never invented; marking a specimen `of_interest` is what puts its tag on the one shared target list pushed to every known trap, and at most `of_interest_limit` (81 — what one chunked `SET_TARGETS` send can ever be budgeted tokens for) may be `of_interest` at once, a 422 past that; `times_seen` and `last_seen` are computed from the stored `READS`/`CAPTURED` events on every query of the roster and are never columns on the specimen row |

**The per-trap door in the API is not authoritative.** A single-trap door remains in the API for
one trap at a time, but the next roster edit recomputes the shared wanted set and re-pushes it to
every trap, overwriting a list set for one trap alone. S2 said "of a trap" until today because it
predates the 2026-09-26 re-scoping and was never amended; read that way, an operator plans trap A
after one tag and trap B after another, and then believes a trap is hunting a tag it is not — this
project's characteristic defect exactly, a list that was true once presented as true now.

**S16 is out of sequence on purpose.** The S-numbers are one series running across both tables, and
they are cited by name from the code, the tests and `deploy.md` — S1 through S13. Numbering the
roster S7 would renumber nine live requirements and silently falsify every one of those references,
so it took the next free number instead of the next logical one. It is not a gap to tidy.

## The station's own requirements

| ID | Requirement | Accepted when |
|----|-------------|---------------|
| S7 | **Starts by itself.** A power cut must not need a person | Power is pulled and restored; the station is serving and listening again with nobody touching a keyboard |
| S8 | **Works with no internet.** The site may have Wi-Fi with no route out | The internet is disconnected and every function except outbound alerting still works |
| S9 | **Never loses an event.** Nothing is acknowledged to a trap before it is durably stored | Power is pulled mid-operation, repeatedly; no acknowledged event is missing afterwards and the database still opens |
| S10 | **Usable from a phone.** Biologists in the field do not carry laptops | Every operator task is done from a phone browser on the local network |
| S11 | **Silence is reported.** A trap that stops answering is shown as a fault, not left looking healthy | A trap is switched off; within an agreed interval the UI marks it unreachable |
| S12 | **Never distributes a time it does not trust** | With no internet and no RTC after a power cut, the station refuses to answer `TIME_SYNC` and says its clock is unset, instead of stamping the record with a wrong time |
| S13 | **Operable by a biologist**, not an engineer | Someone who has not seen the system arms a trap and edits a tag list without reading a manual |
| S14 | **The data can be taken away.** The record is not trapped inside the appliance | An operator copies the whole record to a USB drive or another machine in one documented step |
| S15 | **Carries a battery-backed RTC** (DS3231 class) | The station is powered down with the internet unplugged, left for an hour, powered up, and still knows the time to the second |

---

## A capture is a state, not an event

S3 has two halves on purpose, and the second one is the important one.

If a capture is modelled as a notification that flies past, it only exists for whoever happened to
be looking. The trap fires at three in the morning, no browser is open, and the animal waits until
somebody turns on a screen.

So the trap **stays** in an unacknowledged `fired` state until a person clears it. Anyone who opens
the interface sees it, whether it happened two minutes or six hours ago. The live push is an
accelerator, not the mechanism. It is the same rule as S4: read what the device says it *is*, never
what it once *said*.

Mechanically that means the server stores the capture first and pushes second, and pushes over
Server-Sent Events — one-way, plain HTTP, reconnects on its own.

**The gap this leaves**, recorded so nobody is surprised by it: with nobody on site and no screen on,
the delay is however long until someone looks. Two cheap ways to close it when it matters — the page
makes a sound, or the station itself drives a buzzer, which needs nothing open anywhere.

## Two risks that this document exists to catch

**A Raspberry Pi has no real-time clock.** It loses the time on every power cut and normally gets it
back from the internet. We removed the GPS from the nodes and made the station the time authority,
so a station that boots with a wrong clock would hand a wrong time to every trap and quietly corrupt
the scientific record.

Intermittent internet is the worst case for this, worse than none at all. With no internet you know
you cannot trust the clock and design for it. With intermittent internet the station works for weeks,
and then one power cut coincides with one outage and it silently starts stamping the record with
1970 — or worse, with a plausible wrong time. **A failure that appears only when two ordinary events
coincide is one that will happen in the field and never on the bench.**

Hence the three layers, S15 and S12 together:

1. **The RTC** carries the time across outages. A DS3231 drifts about 2 ppm, a minute a year.
2. **NTP** corrects the RTC whenever the internet happens to be up.
3. **S12** is the net underneath: if neither has ever set the clock, the station refuses to answer
   `TIME_SYNC` and says so, instead of handing out a time it invented.

The chain ends up being NTP → the station's RTC → `TIME_SYNC` → each node's PCF8523. Every link
knows whether it is trusted.

**A Raspberry Pi's SD card is the least reliable part of the system.** They corrupt under power loss,
which is how most Pi deployments eventually fail. S9's acceptance criterion — pulling the plug
repeatedly and checking the database still opens — is there to force this into the design rather
than discover it in the field.

Mains power at the site does not retire S7 or S9: grids fail, breakers trip and people unplug things.
It only lowers the pressure. A small UPS remains the cheapest insurance for the card, and an SSD or
a USB stick for the database is cheaper still.

## Open questions

| # | Question | What it decides |
|---|----------|-----------------|
| ~~1~~ | ~~Who receives a capture alert, on what device, and how fast?~~ | **Answered 2026-09-23:** immediately, in the browser of whoever has the interface open, over plain HTTP on the local network. No internet needed for alerting. |
| ~~2~~ | ~~Is there permanent power where the station will sit?~~ | **Answered 2026-09-23:** yes, mains power is available. A UPS is no longer required, only recommended — see the SD card risk. |
| ~~3~~ | ~~Is there a route to the internet, or only a local network?~~ | **Answered 2026-09-23:** there is internet, but it is intermittent. This makes a battery-backed RTC on the station **required**, not optional — see below. |
| 4 | How many people operate it, and do they need separate accounts? | Whether the UI needs any notion of users |
| 5 | How long must the station keep events before they are collected? | Storage sizing, and whether anything is ever deleted |

## Not in scope

- Scientific analysis: visits, presence and returns belong to `feeder-analysis`.
- Deciding when a trap fires: that is the reader's, and never leaves it.
- Being reachable from outside the site.
