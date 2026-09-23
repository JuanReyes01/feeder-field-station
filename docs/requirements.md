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
| S2 | Edit the target-tag list of a trap (R3) | An edited list reaches the trap and the UI distinguishes **delivered** from **pending** by comparing the version the trap reports with the version the station holds |
| S3 | Notify a person on capture, naming the trap (R4) | A capture on any trap reaches a person's device within the agreed time, and says which trap and which tag |
| S4 | Show which traps are armed (R5) | The UI shows, per trap: armed state, battery, clock, target-list version and time since last contact — all sourced from the trap's own heartbeat, never from what the station last commanded |
| S5 | Serve at least 10 traps (R6) | Ten traps report and are commanded without the operator waiting on the UI |
| S6 | Write the raw file format the analysis app ingests (R8's station half) | `feeder-analysis` ingests a station export with no code changes and no warnings |

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

---

## Two risks that this document exists to catch

**A Raspberry Pi has no real-time clock.** It loses the time on every power cut and normally gets it
back from the internet. We removed the GPS from the nodes and made the station the time authority,
so a station that boots with a wrong clock would hand a wrong time to every trap and quietly corrupt
the scientific record. Two ways out, and we need one of them: **a battery-backed RTC on the station**,
or S12's refusal to answer at all while the clock is unset. The RTC costs a few dollars and is the
better answer; S12 stays as the safety net.

**A Raspberry Pi's SD card is the least reliable part of the system.** They corrupt under power loss,
which is how most Pi deployments eventually fail. S9's acceptance criterion — pulling the plug
repeatedly and checking the database still opens — is there to force this into the design rather
than discover it in the field.

## Open questions

| # | Question | What it decides |
|---|----------|-----------------|
| 1 | Who receives a capture alert, on what device, and how fast? | Whether the station needs internet, a phone on the local network, SMS/LTE, or something audible on site |
| 2 | Is there permanent power where the station will sit? | Whether it needs a UPS, and how hard S7 and S9 have to work |
| 3 | Is there a route to the internet, or only a local network? | Whether alerting can leave the site at all, and how the clock is disciplined |
| 4 | How many people operate it, and do they need separate accounts? | Whether the UI needs any notion of users |
| 5 | How long must the station keep events before they are collected? | Storage sizing, and whether anything is ever deleted |

## Not in scope

- Scientific analysis: visits, presence and returns belong to `feeder-analysis`.
- Deciding when a trap fires: that is the reader's, and never leaves it.
- Being reachable from outside the site.
