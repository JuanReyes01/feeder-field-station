"""Entry point frozen into the station's executable (see cicg-estacion.spec).

Kept outside the package so PyInstaller analyses the real import graph from
`feeder_station.cli`, exactly as `feeder-station` runs from a source checkout.
"""

from feeder_station.cli import main

if __name__ == "__main__":
    main()
