"""S11's timings, named once. See docs/requirements.md S11 and design.md's
decision index (used at task 6.9/6.10 to replace app.py's hardcoded
``SILENCE_AFTER``).
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from feeder_station import config as config_module
from feeder_station.config import StationConfig


def test_heartbeat_interval_is_5_minutes_silence_after_is_15_minutes() -> None:
    config = StationConfig.load(argv=[], env={})

    assert config.heartbeat_interval == timedelta(minutes=5)
    assert config.silence_after == timedelta(minutes=15)


def test_silence_after_is_three_missed_heartbeats() -> None:
    config = StationConfig.load(argv=[], env={})

    assert config.silence_after == config.heartbeat_interval * 3


def test_env_overrides_the_default_heartbeat_interval() -> None:
    config = StationConfig.load(argv=[], env={"FEEDER_STATION_HEARTBEAT_INTERVAL_S": "60"})

    assert config.heartbeat_interval == timedelta(seconds=60)


def test_argv_overrides_both_env_and_the_default() -> None:
    config = StationConfig.load(
        argv=["--silence-after-s", "42"],
        env={"FEEDER_STATION_SILENCE_AFTER_S": "99"},
    )

    assert config.silence_after == timedelta(seconds=42)


def test_unknown_argv_flags_are_ignored_not_rejected() -> None:
    """A later slice's cli.py flags share this argv; this parser must not choke on them."""
    config = StationConfig.load(argv=["--db-path", "/tmp/station.db"], env={})

    assert config.heartbeat_interval == StationConfig().heartbeat_interval


def test_backlog_retry_budget_defaults_to_three() -> None:
    """7.7: the backlog rotation's retry budget, named once, here — not a
    bare literal inside `adapters/scheduler.py`.
    """
    config = StationConfig.load(argv=[], env={})

    assert config.backlog_retry_budget == 3


def test_env_overrides_the_backlog_retry_budget() -> None:
    config = StationConfig.load(argv=[], env={"FEEDER_STATION_BACKLOG_RETRY_BUDGET": "5"})

    assert config.backlog_retry_budget == 5


def test_arm_duration_defaults_to_24_hours() -> None:
    """12.0d: `adapters/command_sender.py` reads this rather than a bare
    literal — messages.md does not pin ARM's `expires_epoch` duration, so
    this is this slice's own documented assumption, the same discipline
    `domain/payloads.py` already applies to unpinned field widths.
    """
    config = StationConfig.load(argv=[], env={})

    assert config.arm_duration == timedelta(hours=24)


def test_env_overrides_the_arm_duration() -> None:
    config = StationConfig.load(argv=[], env={"FEEDER_STATION_ARM_DURATION_S": "3600"})

    assert config.arm_duration == timedelta(seconds=3600)


def test_channel_budget_frames_per_s_defaults_to_the_serial_cable_budget() -> None:
    """Defect 2 (bench repro, docs/pending.md): the deployment today is a
    USB serial cable at 115200 baud, not LoRa -- the old default modeled a
    LoRa Long Fast link's 1% duty-cycle allowance (`docs/protocol/
    messages.md`'s own arithmetic), throttling a cable by orders of
    magnitude and silently doubling as the reason `TIME_SYNC` never reached
    the node. The default now describes the link this station actually
    ships with.
    """
    config = StationConfig.load(argv=[], env={})

    assert config.channel_budget_frames_per_s == pytest.approx(
        config_module.DEFAULT_CHANNEL_BUDGET_FRAMES_PER_S_SERIAL
    )
    # The arithmetic, independent of the module's own derivation: 50 bytes
    # x 10 bits/byte (8N1) / 115200 baud = one frame's wire time; at no
    # duty cycle (a point-to-point cable, not a shared radio channel), the
    # budget is the full inverse of that.
    assert config.channel_budget_frames_per_s == pytest.approx(115_200 / (50 * 10))


def test_lora_channel_budget_constant_is_still_available_for_a_future_radio_deployment() -> None:
    """docs/design.md still lists LoRa as an open decision pending a site
    survey (requirements.md R1: the client may need to move beyond Wi-Fi
    range later) -- the LoRa-derived budget is kept, named and computed,
    not deleted, for an operator to pass explicitly via
    `--channel-budget-frames-per-s` once that survey happens.
    """
    assert config_module.DEFAULT_CHANNEL_BUDGET_FRAMES_PER_S_LORA == pytest.approx(0.01 / 0.37)


def test_env_overrides_the_channel_budget() -> None:
    config = StationConfig.load(argv=[], env={"FEEDER_STATION_CHANNEL_BUDGET_FRAMES_PER_S": "5.0"})

    assert config.channel_budget_frames_per_s == 5.0


def test_argv_overrides_the_channel_budget() -> None:
    config = StationConfig.load(argv=["--channel-budget-frames-per-s", "2.5"], env={})

    assert config.channel_budget_frames_per_s == 2.5
