#!/usr/bin/env python3
"""Inspección de solo lectura del libro histórico de gastos de taller."""
from __future__ import annotations

import re
import sqlite3
import sys
import zipfile
from xml.etree import ElementTree as ET


NS = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}


def column_index(cell_ref: str) -> int:
    letters = re.match(r'[A-Z]+', cell_ref).group(0)
    result = 0
    for letter in letters:
        result = result * 26 + ord(letter) - 64
    return result - 1


def read_rows(path: str):
    with zipfile.ZipFile(path) as archive:
        shared_root = ET.fromstring(archive.read('xl/sharedStrings.xml'))
        shared = [''.join(node.itertext()) for node in shared_root.findall('m:si', NS)]
        sheet = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
    rows = []
    for row in sheet.findall('.//m:sheetData/m:row', NS):
        values = []
        for cell in row.findall('m:c', NS):
            index = column_index(cell.attrib['r'])
            while len(values) <= index:
                values.append('')
            value = cell.find('m:v', NS)
            raw = value.text if value is not None else ''
            values[index] = shared[int(raw)] if cell.attrib.get('t') == 's' and raw else raw
        rows.append(values)
    return rows


if __name__ == '__main__':
    rows = read_rows(sys.argv[1])
    print(f'Filas: {len(rows)}')
    for row in rows[:12]:
        print(row)
    header = rows[0]
    print('Encabezados:', header)
    data = rows[1:]
    print('Registros con VIN:', sum(bool((row[0] if row else '').strip()) for row in data))
    for column, name in ((0, 'VIN'), (8, 'Taller'), (9, 'Tipo gasto'), (14, 'Proveedor'), (16, 'Estatus')):
        counts = {}
        for row in data:
            value = row[column].strip() if len(row) > column else ''
            if value:
                counts[value] = counts.get(value, 0) + 1
        print(name, counts)
    vin_groups = {}
    total = 0.0
    zero = 0
    for row in data:
        if len(row) < 17 or not row[0].strip():
            continue
        vin_groups.setdefault(row[0].strip().upper(), []).append(row)
        value = float((row[13] or '0').replace(',', '') or 0)
        total += value
        zero += value <= 0
    print(f'Vehículos únicos: {len(vin_groups)}')
    print(f'Total de gastos: {total:,.2f}; filas en cero: {zero}')
    print('OT a cerrar / pasar DPV:', sum(all((r[16] or '').strip() == 'Reparado' for r in group) for group in vin_groups.values()))
    print('OT abiertas:', sum(any((r[16] or '').strip() != 'Reparado' for r in group) for group in vin_groups.values()))
    order_groups = {(r[0].strip().upper(), r[7].strip(), r[8].strip()) for group in vin_groups.values() for r in group}
    print(f'OT por VIN + fecha + taller: {len(order_groups)}')
    multiple_tallers = {vin: sorted({r[8].strip() for r in group if r[8].strip()}) for vin, group in vin_groups.items()}
    multiple_tallers = {vin: names for vin, names in multiple_tallers.items() if len(names) > 1}
    print(f'Vehículos con más de un taller en el historial: {len(multiple_tallers)}')
    db = sqlite3.connect('data/autolote.sqlite')
    existing_vins = {row[0] for row in db.execute('SELECT vin FROM vehiculos')}
    providers = {row[0].strip().lower() for row in db.execute('SELECT nombre FROM proveedores WHERE activo=1')}
    missing_vins = sorted(set(vin_groups) - existing_vins)
    missing_workshops = sorted({r[8].strip() for group in vin_groups.values() for r in group if r[8].strip() and r[8].strip().lower() not in providers})
    missing_providers = sorted({r[14].strip() for group in vin_groups.values() for r in group if r[14].strip() and r[14].strip().lower() not in providers})
    print('VIN sin maestro:', missing_vins)
    print('Talleres por crear:', missing_workshops)
    print('Proveedores por crear:', missing_providers)
