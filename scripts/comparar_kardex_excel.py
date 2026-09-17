#!/usr/bin/env python3
"""Compara Costo_Total del Kardex Excel contra el costo consolidado del sistema."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from inspeccionar_libro_excel import rows

ROOT = Path(__file__).resolve().parents[1]


def number(value):
    text = str(value or '').replace('L', '').replace(',', '').replace(' ', '').strip()
    return float(text or 0)


def main():
    source = Path(sys.argv[1])
    sheet = sys.argv[2] if len(sys.argv) > 2 else 'KARDEX'
    data = rows(source, sheet)
    header_row = next(index for index, row in enumerate(data) if 'VIN' in row and ('Costo_Total' in row or 'Costo Acumulado' in row))
    header = data[header_row]
    columns = {name: index for index, name in enumerate(header)}
    expected = {}
    cost_column = 'Costo_Total' if 'Costo_Total' in columns else 'Costo Acumulado'
    for row in data[header_row + 1:]:
        if not row or not row[columns['VIN']].strip():
            continue
        vin = row[columns['VIN']].strip().upper()
        expected[vin] = number(row[columns[cost_column]])

    connection = sqlite3.connect(ROOT / 'data' / 'autolote.sqlite')
    connection.row_factory = sqlite3.Row
    actual = {}
    for vehicle in connection.execute('SELECT id,vin,precio_compra FROM vehiculos'):
        cost = float(vehicle['precio_compra'] or 0)
        cost_row = connection.execute('''SELECT ajuste_compra,grua,flete,isv_pagado,cl_std,almacenaje,gastos_aduaneros
            FROM costos_adquisicion WHERE vehiculo_id=?''', (vehicle['id'],)).fetchone()
        if cost_row:
            cost += sum(float(cost_row[key] or 0) for key in ('ajuste_compra', 'grua', 'flete', 'isv_pagado', 'cl_std', 'almacenaje', 'gastos_aduaneros'))
        cost += float(connection.execute('SELECT COALESCE(SUM(monto),0) FROM costos_vehiculo WHERE vehiculo_id=?', (vehicle['id'],)).fetchone()[0])
        actual[vehicle['vin']] = round(cost, 2)
    connection.close()

    shared = sorted(set(expected) & set(actual))
    only_excel = sorted(set(expected) - set(actual))
    differences = [(vin, expected[vin], actual[vin], round(actual[vin] - expected[vin], 2)) for vin in shared if abs(actual[vin] - expected[vin]) > 0.01]
    differences.sort(key=lambda item: abs(item[3]), reverse=True)
    print(f'Excel {sheet}: {len(expected)} VIN; encontrados en sistema: {len(shared)}; faltantes: {len(only_excel)}')
    print(f'Total Excel (VIN compartidos): {sum(expected[vin] for vin in shared):,.2f}')
    print(f'Total sistema (VIN compartidos): {sum(actual[vin] for vin in shared):,.2f}')
    print(f'Diferencia sistema - Excel: {sum(actual[vin] - expected[vin] for vin in shared):,.2f}')
    print(f'VIN con diferencia: {len(differences)}')
    for vin, excel, system, delta in differences:
        print(f'{vin}|Excel={excel:.2f}|Sistema={system:.2f}|Diferencia={delta:.2f}')
    if only_excel:
        print('Solo en Excel:', ','.join(only_excel))


if __name__ == '__main__':
    main()
