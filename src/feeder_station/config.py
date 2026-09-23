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

# Slice 12 (auto-start-deployment): messages.md does not pin how long an
# ARM's `expires_epoch` should hold — the same "documented assumption,
# never a bare literal" discipline domain/payloads.py already applies to
# unpinned field widths. `adapters/command_sender.py` is the one call site
# that reads this; a trap the station still wants armed past this window
# gets a fresh ARM from the same resend-until-confirmed discipline S2
# already requires for SET_TARGETS.
DEFAULT_ARM_DURATION = timedelta(hours=24)

# CRITICAL 3 (sdd-verify field-station-p4): docs/protocol/messages.md's own
# arithmetic (also quoted in adapters/workers.py's docstring) -- a ~50-byte
# frame costs 0.37 s of airtime at Long Fast, and the design budget is
# "keep the channel under 1% occupied". Named once, here, exactly like
# every other DEFAULT_* in this module, so `domain/pacing.py`'s bucket is
# sized from a config value rather than a literal buried in
# `adapters/workers.py`.
AIRTIME_PER_FRAME_S = 0.37
CHANNEL_BUDGET_FRACTION = 0.01
DEFAULT_CHANNEL_BUDGET_FRAMES_PER_S = CHANNEL_BUDGET_FRACTION / AIRTIME_PER_FRAME_S

ENV_HEARTBEAT_INTERVAL_S = "FEEDER_STATION_HEARTBEAT_INTERVAL_S"
ENV_SILENCE_AFTER_S = "FEEDER_STATION_SILENCE_AFTER_S"
ENV_BACKLOG_RETRY_BUDGET = "FEEDER_STATION_BACKLOG_RETRY_BUDGET"
ENV_ARM_DURATION_S = "FEEDER_STATION_ARM_DURATION_S"
ENV_CHANNEL_BUDGET_FRAMES_PER_S = "FEEDER_STATION_CHANNEL_BUDGET_FRAMES_PER_S"


@dataclass(frozen=True)
class StationConfig:
    heartbeat_interval: timedelta = DEFAULT_HEARTBEAT_INTERVAL
    silence_after: timedelta = DEFAULT_SILENCE_AFTER
    backlog_retry_budget: int = DEFAULT_BACKLOG_RETRY_BUDGET
    arm_duration: timedelta = DEFAULT_ARM_DURATION
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
        parser.add_argument("--arm-duration-s", type=float, default=None)
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
            arm_duration=_resolve_seconds(
                args.arm_duration_s,
                resolved_env.get(ENV_ARM_DURATION_S),
                DEFAULT_ARM_DURATION,
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
