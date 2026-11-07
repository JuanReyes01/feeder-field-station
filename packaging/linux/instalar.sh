#!/bin/sh
# Instala Estación CICG para este usuario: queda en el menú de aplicaciones
# (se busca como "Estación CICG"). No necesita sudo. Para quitarla:
#   ~/.local/bin/cicg-estacion --desinstalar
set -e
cd "$(dirname "$0")"
chmod +x ./cicg-estacion
./cicg-estacion --instalar
