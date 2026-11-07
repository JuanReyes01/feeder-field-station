# PyInstaller spec for the station's single-file executable.
#
#   uv sync --frozen --no-dev
#   uv run --no-sync --with pyinstaller==6.22.3 pyinstaller --noconfirm packaging/cicg-estacion.spec
#
# Output: dist/cicg-estacion (Linux) or dist/cicg-estacion.exe (Windows).
# .github/workflows/release.yml runs exactly this on both systems for every
# v* tag and attaches the results to the GitHub release.
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

a = Analysis(
    [os.path.join(SPECPATH, "run_station.py")],
    # static/index.html is read from next to app.py at import time.
    datas=collect_data_files("feeder_station"),
    # uvicorn picks its protocol, loop and lifespan modules by name at
    # runtime, so the static import graph never sees them.
    hiddenimports=collect_submodules("uvicorn"),
    excludes=["tkinter"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="cicg-estacion",
    # A console window on purpose: closing it is how the station is stopped,
    # and it is where the address and any startup error are shown.
    console=True,
    upx=False,
)
