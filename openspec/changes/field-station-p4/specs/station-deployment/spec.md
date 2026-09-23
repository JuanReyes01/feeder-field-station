# Station Deployment Specification

## Purpose

Auto-start with no keyboard, an offline-safe boot order, and a runbook naming what was actually
tested — so `Restart=always` is safe because S9's fsync discipline makes crash-then-restart safe,
not the other way around.

## Requirements

### Requirement: Starts by itself after a power cut (S7)

The system MUST ship a systemd unit with `Restart=always` so the service resumes serving after a
crash, kill, or power cut with no manual intervention.

**Traces:** S7

#### Scenario: process restarts after being killed
- GIVEN the unit installed and running
- WHEN the process is killed
- THEN systemd restarts it and `/health` responds again within the unit's configured restart delay
- **Hardware gate:** CI proves the restart policy via the unit file and a killed-process test; a literal power-pull on the physical Pi is a named outstanding gate

### Requirement: Boot ordering never blocks on the internet (S8)

The unit MUST be ordered `After=network.target` and MUST NOT declare `Requires=network-online.target`
or any dependency that waits for outbound internet reachability.

**Traces:** S8

#### Scenario: unit file has no internet-waiting dependency
- GIVEN the shipped unit file
- WHEN its directives are inspected
- THEN `network-online.target` does not appear in `Requires=`, `Wants=`, or `After=`

#### Scenario: service starts with no route to the internet
- GIVEN a boot with no internet route
- WHEN the unit starts
- THEN the station serves `/health` and the operator UI without waiting on any internet-dependent condition

### Requirement: Documented install path

The system MUST ship a runbook naming the install steps and the exact Raspberry Pi OS image and
version the deployment was tested on, so any image-specific detail (`chrony` vs `ntp`,
`/boot/firmware` vs `/boot`) lives there rather than in the unit file.

**Traces:** S7, S8

#### Scenario: runbook names the tested image
- GIVEN the shipped runbook
- WHEN it is read
- THEN it states the exact OS image and version the install steps were verified against
