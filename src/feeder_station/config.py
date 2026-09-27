"""Values that must never live as a literal deep inside some other module.

S11's heartbeat interval and its silence timeout are the first case: today
``app.py`` hardcodes ``SILENCE_AFTER = timedelta(minutes=5)`` next to the
route handlers that use it. That hardcoding is exactly what a later slice
(6.9/6.10) removes, replacing it with ``StationConfig.silence_after`` built
here instead.

Built from ``argv`` first, ``FEEDER_STATION_*`` environment variables next,
and the defaults below last — the same precedence ``argparse`` itself uses
for a flag with an environment-backed default. ``cli.py`` will eventually
pass the process's real ``sys.argv``/``os.environ``; tests pass both
explicitly so no test result depends on the machine it runs on.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta

# S11, user decision: a heartbeat every 5 minutes, unreachable after three
# missed beats (15 minutes). Named once, here, so nothing downstream repeats
# either number as a bare literal.
DEFAULT_HEARTBEAT_INTERVAL = timedelta(minutes=5)
DEFAULT_SILENCE_AFTER = timedelta(minutes=15)

# Slice 7 (backlog-rotation): how many consecutive silent GET_BACKLOG
# attempts a node gets before the rotation moves on without it — echoes
# S11's own "three missed beats" shape, named once here rather than as a
# bare literal inside adapters/scheduler.py.
DEFAULT_BACKLOG_RETRY_BUDGET = 3

# CRITICAL 3 (sdd-verify field-station-p4), revised by the scheduler-
# throttle bench fix (docs/pending.md): this budget models one *link*, and
# every number below says plainly which one -- the original version of
# this comment named only LoRa, silently making its 1% duty-cycle
# allowance the default for every deployment, including the USB serial
# cable this project has actually shipped with since day one. Throttling a
# cable by the same rule a battery-powered radio needs is why `TIME_SYNC`
# never reached the node (docs/pending.md: "raising the channel budget
# hangs the station" -- the *other* half of that bug is the concurrency
# fix in `adapters/workers.py::run_scheduler_loop`; this half is the
# number itself being wrong for the link in use).
#
# --- LoRa Long Fast (not the current deployment; kept for the radio this
# --- project may still need -- docs/design.md lists LoRa as an open
# --- decision pending a site survey, and requirements.md's R1 is that the
# --- client may need to move beyond Wi-Fi range later). A shared radio
# --- channel, unlike a point-to-point cable: airtime is a scarce resource
# --- every node on the channel competes for, and regulators (or, here,
# --- this project's own battery-life budget) cap how much of it any one
# --- device may occupy.
#
# WARNING 2 (verify-report-final.md, sdd-verify field-station-p4): two
# things about this constant are not what a first read suggests, recorded
# here rather than left implied by a path that does not resolve:
#
# 1. `docs/protocol/messages.md` does not exist in *this* repository -- it
#    lives in the parent monorepo, at
#    `/home/juanc/code/cicg/docs/protocol/messages.md`. Fourteen files in
#    this repository cite it (`rg -l 'docs/protocol/messages.md'`); this
#    station's most load-bearing constant is not something a clone of
#    `feeder-field-station` alone can check.
# 2. The 1% in that document is the node *uplink* allocation -- its own
#    arithmetic is `10 nodes x 200 events/day x 0.37 s / 86400 s = 0.857%`.
#    This module reallocates the *whole* 1% to station *downlink*, on top
#    of that uplink and on top of one ACK per ack-required uplink -- channel
#    total at design load is closer to 2%, not "under 1%". Partly offset in
#    practice: 0.37 s models a ~50-byte frame, while most station downlinks
#    (ARM, DISARM, GET_BACKLOG) are 12-16 bytes, so this bucket over-charges
#    its own traffic -- except SET_TARGETS at ~200 bytes, the one downlink
#    this offset does not cover (see command_sender.py's own BEHAVIOUR 1
#    docstring for the fix that keeps a chunked SET_TARGETS inside this
#    same budget rather than bypassing it).
#
# Not re-derived here: a correct number needs a real field measurement of
# this deployment's own channel, which this project has not taken -- this
# comment exists so the next person sizing the real budget starts from the
# actual arithmetic above instead of the document's own summary of it.
LORA_LONG_FAST_AIRTIME_PER_FRAME_S = 0.37
LORA_CHANNEL_BUDGET_FRACTION = 0.01
DEFAULT_CHANNEL_BUDGET_FRAMES_PER_S_LORA = (
    LORA_CHANNEL_BUDGET_FRACTION / LORA_LONG_FAST_AIRTIME_PER_FRAME_S
)

# --- USB serial cable at 115200 baud (the current deployment, confirmed
# --- with the user: Wi-Fi reaches every feeder, and the station-to-node
# --- link today is this cable, not a radio) -- derived, not invented:
#
# `SERIAL_BAUD_RATE` matches `adapters/serial_gateway.py::DEFAULT_BAUDRATE`
# (a separate literal there, not an import of it -- domain/commands.py's
# own `RESEND_BACKOFF_S` sets this precedent: a config-level constant
# stays its own copy rather than reaching into `adapters/`).
#
# 8N1 framing (pyserial's own default, and this project's: 8 data bits, no
# parity, 1 stop bit) puts 10 bits on the wire per byte: 1 start + 8 data +
# 1 stop. A ~50-byte frame -- this project's own figure, the same one the
# LoRa arithmetic above uses -- therefore costs:
#
#     50 bytes x 10 bits/byte / 115200 bits/s = 500 / 115200 ≈ 0.00434 s
#
# -- about 4 ms, matching what the bench measured directly.
#
# Unlike a shared radio channel, a point-to-point serial cable has no duty
# cycle to divide: TX and RX run on separate wires (full duplex, not
# half-duplex or CSMA over shared spectrum), there is no regulator-imposed
# airtime allowance, and this station is the only sender on its own TX
# line -- so the fraction of the link available to it is the whole of it,
# not a percentage. The budget is therefore the wire's own raw throughput
# ceiling, `1.0 / SERIAL_AIRTIME_PER_FRAME_S`, not a fraction of it.
#
# Not modelled here, the same "not re-derived" honesty as the LoRa
# constant above: how fast the *node's own firmware* can actually process
# an incoming frame is a separate, unmeasured constraint from what the
# wire itself can carry -- this ceiling describes the cable, not the
# microcontroller on the other end of it.
SERIAL_BAUD_RATE = 115_200
SERIAL_BITS_PER_BYTE = 10  # 1 start + 8 data + 1 stop, no parity (8N1)
SERIAL_FRAME_BYTES = 50
SERIAL_AIRTIME_PER_FRAME_S = SERIAL_FRAME_BYTES * SERIAL_BITS_PER_BYTE / SERIAL_BAUD_RATE
SERIAL_DUTY_CYCLE_FRACTION = 1.0  # no shared-channel allowance to divide
DEFAULT_CHANNEL_BUDGET_FRAMES_PER_S_SERIAL = SERIAL_DUTY_CYCLE_FRACTION / SERIAL_AIRTIME_PER_FRAME_S

# The default describes what is actually deployed today (the cable), not
# an aspiration -- an operator moving to LoRa passes
# `DEFAULT_CHANNEL_BUDGET_FRAMES_PER_S_LORA`'s value explicitly via
# `--channel-budget-frames-per-s` (or `FEEDER_STATION_CHANNEL_BUDGET_
# FRAMES_PER_S`); both constants stay named and computed above so that
# switch is a value, not a re-derivation.
DEFAULT_CHANNEL_BUDGET_FRAMES_PER_S = DEFAULT_CHANNEL_BUDGET_FRAMES_PER_S_SERIAL

ENV_HEARTBEAT_INTERVAL_S = "FEEDER_STATION_HEARTBEAT_INTERVAL_S"
ENV_SILENCE_AFTER_S = "FEEDER_STATION_SILENCE_AFTER_S"
ENV_BACKLOG_RETRY_BUDGET = "FEEDER_STATION_BACKLOG_RETRY_BUDGET"
ENV_CHANNEL_BUDGET_FRAMES_PER_S = "FEEDER_STATION_CHANNEL_BUDGET_FRAMES_PER_S"


@dataclass(frozen=True)
class StationConfig:
    heartbeat_interval: timedelta = DEFAULT_HEARTBEAT_INTERVAL
    silence_after: timedelta = DEFAULT_SILENCE_AFTER
    backlog_retry_budget: int = DEFAULT_BACKLOG_RETRY_BUDGET
    channel_budget_frames_per_s: float = DEFAULT_CHANNEL_BUDGET_FRAMES_PER_S

    @classmethod
    def load(
        cls,
        argv: Sequence[str] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> StationConfig:
        """Build a config from ``argv``, then ``env``, then the defaults.

        Neither source is required: an empty ``argv`` and an empty ``env``
        yield exactly the defaults this module names. Unknown flags in
        ``argv`` are ignored rather than rejected, so a later slice's own
        ``cli.py`` flags (``--db-path`` and friends) can share the same
        ``argv`` without this parser tripping over them.
        """
        resolved_env: Mapping[str, str] = os.environ if env is None else env

        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--heartbeat-interval-s", type=float, default=None)
        parser.add_argument("--silence-after-s", type=float, default=None)
        parser.add_argument("--backlog-retry-budget", type=int, default=None)
        parser.add_argument("--channel-budget-frames-per-s", type=float, default=None)
        args, _unknown = parser.parse_known_args(list(argv) if argv is not None else [])

        return cls(
            heartbeat_interval=_resolve_seconds(
                args.heartbeat_interval_s,
                resolved_env.get(ENV_HEARTBEAT_INTERVAL_S),
                DEFAULT_HEARTBEAT_INTERVAL,
            ),
            silence_after=_resolve_seconds(
                args.silence_after_s,
                resolved_env.get(ENV_SILENCE_AFTER_S),
                DEFAULT_SILENCE_AFTER,
            ),
            backlog_retry_budget=_resolve_int(
                args.backlog_retry_budget,
                resolved_env.get(ENV_BACKLOG_RETRY_BUDGET),
                DEFAULT_BACKLOG_RETRY_BUDGET,
            ),
            channel_budget_frames_per_s=_resolve_float(
                args.channel_budget_frames_per_s,
                resolved_env.get(ENV_CHANNEL_BUDGET_FRAMES_PER_S),
                DEFAULT_CHANNEL_BUDGET_FRAMES_PER_S,
            ),
        )


def _resolve_seconds(
    from_argv: float | None, from_env: str | None, default: timedelta
) -> timedelta:
    if from_argv is not None:
        return timedelta(seconds=from_argv)
    if from_env is not None:
        return timedelta(seconds=float(from_env))
    return default


def _resolve_int(from_argv: int | None, from_env: str | None, default: int) -> int:
    if from_argv is not None:
        return from_argv
    if from_env is not None:
        return int(from_env)
    return default


def _resolve_float(from_argv: float | None, from_env: str | None, default: float) -> float:
    if from_argv is not None:
        return from_argv
    if from_env is not None:
        return float(from_env)
    return default
