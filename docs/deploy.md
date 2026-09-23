# Deploy runbook — the field station as a systemd service

Status: draft, 2026-09-23. Companion to `deploy/feeder-station.service`, `openspec/changes/
field-station-p4/specs/station-deployment/spec.md`, and `design.md` section 7. Written for whoever
images and installs the deployed Raspberry Pi — nothing here is exercised by `pytest`, and this file
says explicitly, in each section, what has and has not actually been verified against real hardware.

## What is verified (this repository's own CI)

- The unit file's own directives: `Restart=always`, `After=network.target`, and no
  `network-online.target` anywhere in `Requires=`/`Wants=`/`After=`
  (`tests/test_deploy_unit.py::test_unit_file_has_restart_always_and_no_internet_wait_dependency`).
- A killed process, relaunched against the same `station.db`, answers `/health` again
  (`tests/test_deploy_unit.py::test_process_restarts_after_being_killed`) — a process-level proxy for
  what `Restart=always` depends on systemd to do in production, not a `kill -9` on a real Pi.
- No startup path calls out to the network: `/health` and `/` both answer with
  `socket.socket.connect` itself blocked
  (`tests/test_deploy_unit.py::test_service_starts_with_no_route_to_the_internet`).
- All four background workers (the gateway thread, backlog rotation, the command sender, and clock
  trust's own `TIME_SYNC` sender) actually start from `cli.py`/`app.py`'s real lifespan
  (`tests/test_app.py::test_the_app_starts_its_background_workers`).

## What is **not** verified — named plainly, not left implicit

- **The exact Raspberry Pi OS image and version.** This is the literal open question this runbook
  exists to eventually close. Nothing below (`ExecStart` path, `WorkingDirectory`, the `feeder-station`
  system user) has been installed on a real Pi boot as of this writing. Until it has, treat every path
  and command below as **the intended shape**, not a verified one — record the real image, its
  version, and any deviation here the first time this is actually installed.
- **A literal power-pull on the physical device** — `specs/station-deployment/spec.md`'s own acceptance
  criterion for S7. `Restart=always` and S9's fsync discipline (`adapters/store.py`) are what make a
  power cut *survivable in principle*; nothing in this repository's own test suite can pull power on
  real hardware. `tests/test_deploy_unit.py::test_process_restarts_after_being_killed` is the nearest
  CI proxy — a `SIGKILL`, not a power cut, and a Python subprocess, not a systemd-managed one.
- **Whether the shipped image's `chrony`/`systemd-timesyncd`/`ntpd` actually maintains the `adjtimex`
  `STA_UNSYNC` bit `adapters/clock.py` reads** — see `docs/hardware/clock-trust.md`, which carries this
  identical open question for the clock-trust half of the same hardware.

## Install steps (intended shape — see the open question above)

These assume a Raspberry Pi already imaged, network-reachable over SSH or a local console, and `uv`
available (or a pre-built wheel installed with `pip`).

1. Create the system user and its data directory (matches the unit file's `User=`/`Group=`/
   `WorkingDirectory=`):

   ```sh
   sudo useradd --system --home-dir /var/lib/feeder-station --create-home feeder-station
   sudo mkdir -p /var/lib/feeder-station
   sudo chown feeder-station:feeder-station /var/lib/feeder-station
   ```

2. Install the package into a location every user can execute, e.g. a `pipx`-managed venv or a system
   Python with `uv pip install feeder-field-station`. The unit file's `ExecStart=/usr/bin/env
   feeder-station ...` line resolves `feeder-station` off `$PATH` — confirm `which feeder-station`
   resolves for the `feeder-station` system user specifically, not just the installing operator's own
   shell, before proceeding (a `pipx install --global` or a system-wide `pip install` are the two
   shapes that satisfy this; a per-user venv activated only in the installer's own shell will not).

3. Copy the unit file and enable it:

   ```sh
   sudo cp deploy/feeder-station.service /etc/systemd/system/feeder-station.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now feeder-station.service
   ```

4. Confirm it is actually serving, from the Pi itself or another device on the same local network
   (S10 — no internet route is required for this step, and none should be):

   ```sh
   curl http://<pi-hostname-or-ip>:8000/health
   ```

   A healthy response's `workers.gateway.enabled` is `true` only once a real serial device is
   configured — see the next section. Until then, `false` there is correct, not a fault.

5. Point the unit at a real serial device once the radio is wired up. The shipped unit's `ExecStart=`
   passes no `--serial-device`, so the gateway starts and immediately reports the link down (step 4 of
   `adapters/serial_gateway.py`'s own resolution order) rather than crashing the station. Edit
   `ExecStart=` (or use a systemd drop-in, `systemctl edit feeder-station.service`) to add
   `--serial-device /dev/serial/by-id/<the-real-one>` once that path is known for the shipped
   USB-UART chip (see `tests/adapters/test_serial_gateway_pty.py`'s own note on this — CP2102 vs.
   CH340 is not something this repository can confirm without the real board).

6. `--no-gateway` is the rollback kill switch (12.8): add it to `ExecStart=` (again via a drop-in) to
   start the HTTP interface — `/health`, the operator UI, `/api/export` — with no gateway or scheduler
   thread at all, whatever hardware is or is not attached. Useful for isolating whether a problem is
   the radio link or the rest of the station.

## Data export

`POST /api/export` with a JSON body `{"dest_root": "<a path>"}` produces one
`station-export-<timestamp>/` folder there (design.md section 8, DD13): a compacted copy of the whole
database, a CSV of what the station itself witnessed, and a README explaining what the folder is. The
destination is whatever path is mounted at the time — this runbook is also where the real mount point
for the image that ships should be recorded (design.md names `/media/*` and `/run/media/*` as the
likely candidates; neither has been confirmed against a real image yet).

## Uninstall / rollback

```sh
sudo systemctl disable --now feeder-station.service
sudo rm /etc/systemd/system/feeder-station.service
sudo systemctl daemon-reload
```

`station.db` is left untouched under `/var/lib/feeder-station` — nothing here deletes the event log.
