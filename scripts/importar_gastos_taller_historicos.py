#!/usr/bin/env python3
"""Importa el libro Gastos Taller Migracion como OT histórica por vehículo.

La fuente no contiene información de pago, por lo que registra costos operativos
del vehículo y no genera movimientos de caja ni cuentas por pagar.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import os
from pathlib import Path
import re
import sqlite3
import sys
import zipfile
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = Path('/Users/erickmadrid/Library/Mobile Documents/com~apple~CloudDocs/Autolote AAA/Gastos Taller Migracion.xlsx')
sys.path.insert(0, str(ROOT))
import server

NS = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}


def col_index(reference: str) -> int:
    value = 0
    for letter in re.match(r'[A-Z]+', reference).group(0):
        value = value * 26 + ord(letter) - 64
    return value - 1


def workbook_rows(path: Path):
    with zipfile.ZipFile(path) as archive:
        shared_root = ET.fromstring(archive.read('xl/sharedStrings.xml'))
        shared = [''.join(item.itertext()) for item in shared_root.findall('m:si', NS)]
        sheet = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
    rows = []
    for row in sheet.findall('.//m:sheetData/m:row', NS):
        values = []
        for cell in row.findall('m:c', NS):
            index = col_index(cell.attrib['r'])
            while len(values) <= index:
                values.append('')
            node = cell.find('m:v', NS)
            raw = node.text if node is not None else ''
            values[index] = shared[int(raw)] if cell.attrib.get('t') == 's' and raw else raw
        rows.append(values)
    return rows


def clean(value) -> str:
    return str(value or '').replace('\xa0', ' ').strip()


def amount(value) -> float:
    text = clean(value).replace('L', '').replace(',', '').replace(' ', '')
    return float(text or 0)


def excel_date(value) -> str:
    text = clean(value)
    if not text:
        raise ValueError('Fecha de ingreso a taller vacía')
    try:
        return (datetime(1899, 12, 30) + timedelta(days=float(text))).strftime('%Y-%m-%d')
    except ValueError:
        for pattern in ('%d/%m/%y', '%d/%m/%Y', '%Y-%m-%d'):
            try:
                return datetime.strptime(text, pattern).strftime('%Y-%m-%d')
            except ValueError:
                pass
    raise ValueError(f'Fecha inválida: {text}')


def ensure_provider(connection, name: str, provider_type='Proveedor') -> int:
    row = connection.execute('SELECT id FROM proveedores WHERE lower(trim(nombre))=lower(trim(?))', (name,)).fetchone()
    if row:
        return row['id']
    cursor = connection.execute(
        "INSERT INTO proveedores(nombre,condicion_pago,tipo,activo) VALUES(?,'Contado',?,1)",
        (name, provider_type),
    )
    return cursor.lastrowid


def make_groups(rows):
    groups = {}
    for number, row in enumerate(rows[1:], start=2):
        if len(row) < 17 or not clean(row[0]):
            continue
        vin = clean(row[0]).upper()
        groups.setdefault(vin, []).append({
            'line': number, 'vin': vin, 'placa': clean(row[1]).upper(), 'fecha': excel_date(row[7]),
            'taller': clean(row[8]) or 'Taller no especificado', 'categoria': clean(row[9]) or 'Otros',
            'descripcion': clean(row[10]) or 'Sin descripción', 'subtotal': amount(row[11]),
            'isv': amount(row[12]), 'total': amount(row[13]), 'proveedor': clean(row[14]) or 'Proveedor no especificado',
            'factura': clean(row[15]), 'estatus': clean(row[16]),
        })
    return groups


def main():
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SOURCE
    if not source.exists():
        raise FileNotFoundError(source)
    groups = make_groups(workbook_rows(source))
    server.init_db()
    connection = server.db()
    try:
        prior = connection.execute("SELECT COUNT(*) FROM ordenes_trabajo WHERE migracion_historica=1").fetchone()[0]
        if prior:
            raise ValueError('Ya existe una carga histórica de gastos de taller. No se importó nada para evitar duplicados.')
        connection.execute('BEGIN')
        ensure_provider(connection, 'Taller-Inicial', 'Taller')
        ensure_provider(connection, 'Proveedor Inicial')
        ensure_provider(connection, 'Yonker Manuel')
        imported = open_orders = skipped = cost_lines = 0
        total_cost = 0.0
        for vin, items in groups.items():
            vehicle = connection.execute('SELECT * FROM vehiculos WHERE vin=?', (vin,)).fetchone()
            if not vehicle:
                skipped += 1
                print(f'OMITIDO {vin}: no existe en datos maestros')
                continue
            purchase = connection.execute('SELECT id FROM adquisiciones WHERE vehiculo_id=?', (vehicle['id'],)).fetchone()
            all_repaired = all(item['estatus'].lower() == 'reparado' for item in items)
            pending_items = [item for item in items if item['estatus'].lower() != 'reparado']
            active_source = pending_items[-1] if pending_items else items[0]
            workshops = {item['taller'] for item in items}
            workshop = active_source['taller'] if pending_items else (items[0]['taller'] if len(workshops) == 1 else 'Múltiples talleres')
            categories = {item['categoria'] for item in items}
            repair_type = next(iter(categories)) if len(categories) == 1 else 'Mantenimiento histórico'
            date = min(item['fecha'] for item in items)
            status = 'Finalizada' if all_repaired else 'Pendiente'
            description = f'Migración histórica de gastos de taller. {len(items)} conceptos importados.'
            correlativo = connection.execute('SELECT COALESCE(MAX(correlativo),0)+1 FROM ordenes_trabajo').fetchone()[0]
            cursor = connection.execute('''INSERT INTO ordenes_trabajo(
                vehiculo_id,adquisicion_id,fecha,estado,detalle,correlativo,numero_ot,taller,tipo_reparacion,
                valor_negociado,valor_final,descripcion,costo_cargado,migracion_historica)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1)''',
                (vehicle['id'], purchase['id'] if purchase else None, date, status, description, correlativo,
                 f'OT-{correlativo:06d}', workshop, repair_type, 0, 0 if all_repaired else None, description,
                 1 if all_repaired else 0))
            order_id = cursor.lastrowid
            for item in items:
                provider_id = ensure_provider(connection, item['proveedor'])
                cost_id = None
                if item['total'] > 0:
                    cost = connection.execute('''INSERT INTO costos_vehiculo(
                        vehiculo_id,fecha,concepto,categoria,monto,proveedor,documento,observaciones)
                        VALUES(?,?,?,?,?,?,?,?)''',
                        (vehicle['id'], item['fecha'], item['descripcion'], f"{item['categoria']} / OT histórica",
                         item['total'], item['proveedor'], item['factura'] or None,
                         f"OT-{correlativo:06d}; taller: {item['taller']}; migración histórica"))
                    cost_id = cost.lastrowid
                    cost_lines += 1
                    total_cost += item['total']
                connection.execute('''INSERT INTO gastos_ot(
                    orden_trabajo_id,proveedor_id,taller_origen,factura,fecha,categoria,descripcion,subtotal,isv,total,costo_vehiculo_id)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                    (order_id, provider_id, item['taller'], item['factura'] or None, item['fecha'], item['categoria'],
                     item['descripcion'], item['subtotal'], item['isv'], item['total'], cost_id))
            server.recalcular_estado(connection, vehicle['id'], 'OT histórica importada')
            imported += 1
            open_orders += status == 'Pendiente'
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    print(f'OT importadas: {imported}; abiertas: {open_orders}; omitidas: {skipped}')
    print(f'Conceptos con costo: {cost_lines}; costo aplicado: {total_cost:,.2f}')


if __name__ == '__main__':
    main()
