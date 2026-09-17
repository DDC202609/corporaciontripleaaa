#!/usr/bin/env python3
"""Carga el precio de lista del Kardex histórico solo a unidades DPV sin precio."""
from datetime import datetime
from pathlib import Path
import shutil
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from inspeccionar_libro_excel import rows

ARCHIVO = Path('/Users/erickmadrid/Library/Mobile Documents/com~apple~CloudDocs/Autolote AAA/Sistema de Gestión Corporación Triple AAA V3 Version WEb.xlsm')


def monetary(value):
    text = str(value or '').replace('L', '').replace(',', '').strip()
    return float(text) if text else 0.0


def main():
    source_rows = rows(ARCHIVO, 'KARDEX')
    headers = source_rows[0]
    vin_index = headers.index('VIN')
    price_index = headers.index('Precio_de_Venta')
    prices = {}
    for row in source_rows[1:]:
        vin = str(row[vin_index] if vin_index < len(row) else '').strip().upper()
        price = monetary(row[price_index] if price_index < len(row) else 0)
        if vin and price > 0:
            prices[vin] = price

    database = ROOT / 'data' / 'autolote.sqlite'
    backup = ROOT / 'data' / f'autolote-antes-precios-lista-dpv-{datetime.now():%Y%m%d-%H%M%S}.sqlite'
    shutil.copy2(database, backup)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        candidates = connection.execute(
            "SELECT id, vin FROM vehiculos WHERE estado='DPV' AND COALESCE(precio_venta, 0)<=0"
        ).fetchall()
        missing = [row['vin'] for row in candidates if row['vin'] not in prices]
        if missing:
            raise ValueError(f'Hay VIN DPV sin precio en Kardex: {", ".join(missing)}')
        connection.execute('BEGIN')
        for vehicle in candidates:
            connection.execute('UPDATE vehiculos SET precio_venta=? WHERE id=?',
                               (prices[vehicle['vin']], vehicle['id']))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    print(f'Respaldo: {backup}')
    print(f'Precios de lista cargados: {len(candidates)}')
    print(f'Valor total de lista: {sum(prices[row["vin"]] for row in candidates):.2f}')


if __name__ == '__main__':
    main()
