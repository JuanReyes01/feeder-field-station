"""Prints the body of a GitHub release: which file to download, and the
changes of that version from CHANGELOG.md.

    python packaging/release_notes.py 0.2.0-rc2 > notes.md

Used by .github/workflows/release.yml; the asset names here are the ones
that workflow uploads.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = "JuanReyes01/feeder-field-station"
ROOT = Path(__file__).resolve().parent.parent


def changes_for(version: str, changelog: str) -> str:
    """The CHANGELOG section for ``version``; a pre-release (0.2.0-rc2) uses
    the section of the version it leads to (0.2.0)."""
    base = re.split(r"[-]?(?:a|b|rc)\d+$", version)[0]
    match = re.search(rf"^## {re.escape(base)}\s*$(.*?)(?=^## |\Z)", changelog, re.MULTILINE | re.DOTALL)
    return match.group(1).strip() if match else "Sin notas para esta versión."


def notes(version: str, changelog: str) -> str:
    def link(suffix: str) -> str:
        name = f"EstacionCICG-{version}-{suffix}"
        return f"[{name}](https://github.com/{REPO}/releases/download/v{version}/{name})"

    pre = (
        "\n> Versión de prueba: para probar antes de llevarla a campo.\n" if "rc" in version else ""
    )
    return f"""## Descargas
{pre}
| Sistema | Instalar (recomendado) | Portable (sin instalar) |
|---|---|---|
| Windows | {link("Windows-Instalador.exe")} | {link("Windows-Portable.exe")} |
| Linux | {link("Linux-Instalador.tar.gz")} | {link("Linux-Portable")} |

- **Windows, instalador:** ábrelo y sigue los pasos; queda "Estación CICG" en el menú Inicio. Si Windows dice "Windows protegió tu PC", elige "Más información" y "Ejecutar de todas formas" (el archivo no está firmado).
- **Linux, instalador:** descomprime y corre `./instalar.sh`; queda "Estación CICG" en el menú de aplicaciones.
- **Portable:** se abre directo, sin instalar. En Linux, primero `chmod +x` al archivo.
- Los datos quedan en la carpeta `CICG` de tu usuario y no se tocan al actualizar.

## Cambios

{changes_for(version, changelog)}
"""


def main() -> None:
    version = sys.argv[1].removeprefix("v")
    print(notes(version, (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")))


if __name__ == "__main__":
    main()
