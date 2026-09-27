# Clock trust — hardware notes

Status: draft, 2026-09-23. Companion to `openspec/changes/field-station-p4/specs/clock-trust/spec.md`
and `design.md` section 5 (DD14). Written for whoever images the deployed Raspberry Pi, not for the
test suite — nothing here is exercised by `pytest`.

## What is implemented (`src/feeder_station/adapters/clock.py`)

| Probe | How | Verified on |
|-------|-----|-------------|
| RTC read | `RTC_RD_TIME` ioctl against `/dev/rtc0` (Linux `<linux/rtc.h>`) | **Not verified against real hardware.** Written against the documented ioctl contract only; see "Open question" below |
| NTP sync, primary | `adjtimex(2)` via `ctypes`, checks the kernel's `STA_UNSYNC` bit | Verified on the Fedora 43 development laptop (`x86_64`, `libc.so.6`) — returns a real answer with no NTP daemon configuration assumed on the caller's part |
| NTP sync, fallback | `timedatectl show -p NTPSynchronized --value` | Verified on the same Fedora 43 development laptop (systemd present). Exists for an image where the `adjtimex` syscall path is unavailable for any reason |

Both probes return `None` on any failure to make the call at all (missing `libc`, missing
`timedatectl`, a non-systemd image) rather than raising — `adapters/clock.py::
_default_ntp_synchronized` only falls back to `timedatectl` when `adjtimex` itself was inconclusive,
and treats both being inconclusive as `False`, never as "trust it anyway."

## What cannot be verified without the shipped hardware (S15's own honesty requirement)

- **Which Raspberry Pi OS image ships**, and therefore whether it is Debian-based with
  `systemd-timesyncd`, or carries `chrony`/`ntpd` instead. `adjtimex`'s `STA_UNSYNC` bit is
  maintained by all three, which is exactly why it is the primary probe (design.md section 5) — but
  this has not been confirmed on an actual Raspberry Pi OS boot.
- **Whether `/dev/rtc0` is the DS3231's device node on the shipped image**, and whether the
  `i2c-rtc` / `rtc-ds1307` overlay that exposes it is enabled by default or needs an explicit
  `/boot/config.txt` (or `/boot/firmware/config.txt`) entry. `RTC_RD_TIME` is written against the
  documented kernel ioctl contract; it has never been run against a real DS3231.
- **Whether the shipped image sets the *system* clock from the RTC at boot** — `rtc_hctosys`, or a
  `hwclock --hctosys` unit ordered before `feeder-station.service`. design.md section 5 lists the
  `hwclock` sequence as an unverified item *separate* from the overlay, and that separation is the
  whole point: reading the DS3231 and actually using its value are two different things, and only
  one of them is what this station hands out. `adapters/clock.py::StationClock.now()` returns
  `system_now()` — `datetime.now(UTC)` — and that is the value `attempt_time_sync` puts in the
  `TIME_SYNC` payload (`clock.py:250-253`). The RTC read feeds `trust()` and nothing else
  (`clock.py:194-197`); `domain/clock_trust.py::trust_from_evidence` grants `rtc` from a readable,
  above-floor RTC epoch *alone*, and nothing in `src/` ever compares the RTC's value against the
  system clock. So a Pi with a working DS3231 and no `hctosys` reports `clock_trust: "rtc"` on
  `/health`, passes every other check in this document, and stamps every node from an unset system
  clock — the station cannot notice, and neither can the test suite. Raspberry Pi OS also ships
  `fake-hwclock`, a competing writer of the boot-time system clock, so which source actually won has
  to be read off the booted device rather than inferred from the overlay being present. **The gate:
  on a cold boot with no internet, `date` and `sudo hwclock -r` must agree, and `/health` must not
  report `ntp`** — an `ntp` reading means the network disciplined the clock and the RTC path was
  never exercised, so it proves nothing. Until that passes, the station is not trusted to hand out
  time. This bullet was missing until 2026-09-27: the list named the overlay and stopped, which
  reads as though a successful `/dev/rtc0` read were the whole RTC story. It is the project's
  characteristic defect aimed at its own checklist — the imager would have enabled the overlay, seen
  `rtc` on `/health`, and shipped.
- **Whether the physical DS3231 actually holds time across an unpowered hour** — S15's literal
  acceptance criterion. This is a hardware gate, not a software one; `tests/adapters/test_clock.py`
  proves only the failure-mode discipline (a read failure degrades to `unset`, never a guessed
  time), against a fake RTC reader, not a real chip.

## Open question (carried from `design.md`)

Which Raspberry Pi OS image is used decides `chrony` vs. `timesyncd`, `/boot` vs. `/boot/firmware`,
and which of the two probes above is actually authoritative on the shipped device. Until an image is
chosen and this file is updated with a real boot's result, both probes ship, in the fallback order
above, and the honest state is `unset` whenever neither can be verified.
