#!/bin/zsh
# Respaldo local diario de Corporación Triple AAA.
# Este archivo se instala en la carpeta de respaldos del Mac.
set -euo pipefail

BACKUP_DIR="/Users/erickmadrid/Documents/Respaldos Triple AAA"
LOG_DIR="$BACKUP_DIR/logs"
mkdir -p "$BACKUP_DIR" "$LOG_DIR"

BACKUP_TOKEN=$(/usr/bin/security find-generic-password -s "Corporacion Triple AAA Respaldo" -w 2>/dev/null || true)
if [[ -z "$BACKUP_TOKEN" ]]; then
  print -u2 "No se encontró la clave segura de respaldo en el Llavero."
  exit 1
fi

# Si el Mac inicia antes de las 10 p. m., no descarga todavía. Si inicia
# después, recupera el respaldo pendiente del día automáticamente.
HOUR=$(date +%H)
if [[ "$HOUR" < "22" ]]; then
  exit 0
fi
TODAY=$(date +%F)
if ls "$BACKUP_DIR"/CorporacionTripleAAA-"$TODAY"_*.zip >/dev/null 2>&1; then
  exit 0
fi

STAMP=$(date +%F_%H%M%S)
TEMP_FILE="$BACKUP_DIR/.respaldo-$STAMP.tmp"
FINAL_FILE="$BACKUP_DIR/CorporacionTripleAAA-$STAMP.zip"
curl --fail --silent --show-error --location --retry 3 --retry-delay 15 \
  -H "Authorization: Bearer $BACKUP_TOKEN" \
  "https://corporaciontripleaaa.com/respaldo/sistema.zip" \
  -o "$TEMP_FILE"
unzip -tqq "$TEMP_FILE"
mv "$TEMP_FILE" "$FINAL_FILE"

# Conserva 90 días de copias locales; el resto se elimina automáticamente.
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'CorporacionTripleAAA-*.zip' -mtime +90 -delete
print "$(date '+%Y-%m-%d %H:%M:%S') · Respaldo creado: $FINAL_FILE" >> "$LOG_DIR/respaldo.log"
