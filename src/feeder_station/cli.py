"""Starts the station.

Binds every interface on purpose: the operator reaches it from a phone on the
local network (S10), and there is no route in from outside the site anyway.
"""

from __future__ import annotations

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(prog="feeder-station")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    uvicorn.run("feeder_station.app:create_app", factory=True, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
