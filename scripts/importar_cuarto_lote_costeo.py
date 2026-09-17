#!/usr/bin/env python3
"""Importa el cuarto lote histórico con adquisición, costeo y partida doble.

Reglas confirmadas:
- Costo Estimado = valor de adquisición.
- Costo EXW = anticipo, excepto el VIN indicado con anticipo cero.
- Todas las unidades quedan En Tránsito.
- Nacionalizado en la fuente define el estado de nacionalización.
"""
from datetime import datetime, timedelta
from pathlib import Path
import os
import sys
import zipfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server

ARCHIVO = Path('/Users/erickmadrid/Library/Mobile Documents/com~apple~CloudDocs/Autolote AAA/4to Lote.xlsx')
PREFIJO = 'Migración histórica. Cuarto lote'
VIN_COMPRA_EXISTENTE = '5FNYF5H87JB015591'
VIN_SIN_COSTO_ESTIMADO = '2T3H1RFV8MC111485'
VIN_PLACA_EN_BLANCO = '4A4AP3AUXEE003459'
NS = {'x': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}


def column_number(reference):
    letters = ''.join(char for char in reference if char.isalpha())
    number = 0
    for char in letters:
        number = number * 26 + ord(char.upper()) - 64
    return number


def read_xlsx_rows(path):
    """Lee la primera hoja XLSX con biblioteca estándar para que el script sea portable."""
    with zipfile.ZipFile(path) as book:
        shared = []
        if 'xl/sharedStrings.xml' in book.namelist():
            root = ET.fromstring(book.read('xl/sharedStrings.xml'))
            for item in root.findall('x:si', NS):
                shared.append(''.join(node.text or '' for node in item.iterfind('.//x:t', NS)))
        root = ET.fromstring(book.read('xl/worksheets/sheet1.xml'))
    rows = []
    for row in root.findall('.//x:sheetData/x:row', NS):
        values = {}
        for cell in row.findall('x:c', NS):
            reference = cell.attrib['r']
            value_node = cell.find('x:v', NS)
            raw = value_node.text if value_node is not None else ''
            if cell.attrib.get('t') == 's' and raw != '':
                value = shared[int(raw)]
            elif cell.attrib.get('t') == 'inlineStr':
                value = ''.join(node.text or '' for node in cell.findall('.//x:t', NS))
            else:
                try:
                    value = float(raw) if raw != '' else None
                    if value is not None and value.is_integer():
                        value = int(value)
                except ValueError:
                    value = raw
            values[column_number(reference)] = value
        rows.append(values)
    if len(rows) < 2:
        raise ValueError('El archivo no contiene encabezados y registros.')
    headers = {column: str(value or '').strip() for column, value in rows[0].items()}
    return [{headers[column]: value for column, value in row.items() if column in headers}
            for row in rows[1:] if row.get(1)]


def number(value):
    return float(value or 0)


def date_iso(value):
    if isinstance(value, (int, float)):
        return (datetime(1899, 12, 30) + timedelta(days=float(value))).strftime('%Y-%m-%d')
    return str(value or '').strip()


def blank_plate(value, vin):
    plate = str(value or '').strip().upper()
    return '' if vin == VIN_PLACA_EN_BLANCO or plate in ('', 'N/A', '0') else plate


def account_id(connection, code):
    row = connection.execute('SELECT id FROM cuentas_contables WHERE codigo=? AND activo=1', (code,)).fetchone()
    if not row:
        raise ValueError(f'No existe la cuenta contable activa {code}.')
    return row['id']


def main():
    rows = read_xlsx_rows(ARCHIVO)
    if len(rows) != 46:
        raise ValueError(f'Se esperaban 46 registros en el cuarto lote; se encontraron {len(rows)}.')
    server.init_db()
    connection = server.db()
    try:
        inventory = account_id(connection, '1105')
        cash = account_id(connection, '1101')
        payable = account_id(connection, '2101')
        total_acquisition = total_advance = total_payable = 0.0
        connection.execute('BEGIN')
        for source in rows:
            vin = str(source['VIN']).strip().upper()
            vehicle = connection.execute('SELECT * FROM vehiculos WHERE vin=?', (vin,)).fetchone()
            if not vehicle:
                raise ValueError(f'No existe el vehículo maestro {vin}.')
            provider_name = str(source['Proveedor'] or '').strip()
            provider = connection.execute('SELECT * FROM proveedores WHERE nombre=? AND activo=1', (provider_name,)).fetchone()
            if not provider:
                raise ValueError(f'No existe el proveedor activo {provider_name} para {vin}.')
            plate = blank_plate(source.get('Placa'), vin)
            if plate and not server.placa_disponible(connection, plate, vehicle['id']):
                raise ValueError(f'La placa {plate} ya existe en otro vehículo.')
            estimated = number(source.get('Costo Estimado'))
            advance = number(source.get('Costo_EXW'))
            if vin == VIN_SIN_COSTO_ESTIMADO:
                estimated, advance = 380718.0, 0.0
            if estimated <= 0:
                raise ValueError(f'Costo estimado inválido para {vin}.')
            if advance < 0 or advance > estimated:
                raise ValueError(f'Anticipo inválido para {vin}.')
            crane = number(source.get('Grúa'))
            freight = number(source.get('Flete'))
            adjustment = number(source.get('Ajuste a Valor CIF (-/+)'))
            isv = number(source.get('CL_ISV Pagado'))
            std = number(source.get('CL_STD'))
            storage = number(source.get('Almacenaje'))
            customs = number(source.get('Gastos_Aduanero'))
            cost_status = 'Nacionalizado' if str(source.get('Estatus') or '').strip().lower() == 'nacionalizado' else 'Pendiente'
            purchase_type = str(source.get('Tipo') or '').strip()
            purchase_date = date_iso(source.get('Fecha de compr'))
            balance = round(estimated - advance, 2)
            note = f'{PREFIJO}. Fuente: 4to Lote.xlsx. Anticipo EXW: {advance:.2f}.'

            existing = connection.execute('SELECT * FROM adquisiciones WHERE vehiculo_id=?', (vehicle['id'],)).fetchone()
            if existing and vin != VIN_COMPRA_EXISTENTE:
                raise ValueError(f'El VIN {vin} ya tiene una compra registrada.')
            if existing:
                # Reemplaza la compra previa de prueba por el dato histórico confirmado.
                headers = connection.execute("SELECT id FROM asientos_contables WHERE referencia_tipo='adquisicion' AND referencia_id=?", (existing['id'],)).fetchall()
                for header in headers:
                    connection.execute('DELETE FROM partidas WHERE asiento_id=?', (header['id'],))
                    connection.execute('DELETE FROM asientos_contables WHERE id=?', (header['id'],))
                connection.execute("DELETE FROM movimientos_caja WHERE referencia_tipo='adquisicion_anticipo' AND referencia_id=?", (existing['id'],))
                connection.execute('DELETE FROM cuentas_por_pagar WHERE adquisicion_id=?', (existing['id'],))
                acquisition_id = existing['id']
                connection.execute('''UPDATE adquisiciones SET proveedor_id=?,fecha=?,costo_compra=?,anticipo=?,metodo_pago='Efectivo',
                    condicion_pago='Contado',dias_credito=0,saldo=?,observaciones=?,necesita_reparacion=0,tipo_compra=?,placa_cambio=NULL,banco=NULL
                    WHERE id=?''', (provider['id'], purchase_date, estimated, advance, balance, note, purchase_type, acquisition_id))
            else:
                cur = connection.execute('''INSERT INTO adquisiciones
                    (vehiculo_id,proveedor_id,fecha,costo_compra,anticipo,metodo_pago,condicion_pago,dias_credito,saldo,observaciones,necesita_reparacion,tipo_compra,placa_cambio,banco)
                    VALUES(?,?,?,? ,?,'Efectivo','Contado',0,?,?,0,?,?,NULL)''',
                    (vehicle['id'], provider['id'], purchase_date, estimated, advance, balance, note, purchase_type, None))
                acquisition_id = cur.lastrowid

            connection.execute('''UPDATE vehiculos SET placa=?,precio_compra=?,proveedor=?,fecha_adquisicion=?,tipo_compra=?,estado='En Tránsito'
                WHERE id=?''', (plate or None, estimated, provider_name, purchase_date, purchase_type, vehicle['id']))
            connection.execute('''INSERT INTO costos_adquisicion
                (vehiculo_id,costo_exw,grua,flete,costo_estimado,ajuste_cif,isv_pagado,cl_std,almacenaje,gastos_aduaneros,estatus,ajuste_compra,placa_nacionalizacion,fecha_actualizacion)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(vehiculo_id) DO UPDATE SET costo_exw=excluded.costo_exw,grua=excluded.grua,flete=excluded.flete,
                    costo_estimado=excluded.costo_estimado,ajuste_cif=excluded.ajuste_cif,isv_pagado=excluded.isv_pagado,
                    cl_std=excluded.cl_std,almacenaje=excluded.almacenaje,gastos_aduaneros=excluded.gastos_aduaneros,
                    estatus=excluded.estatus,ajuste_compra=excluded.ajuste_compra,placa_nacionalizacion=excluded.placa_nacionalizacion,
                    fecha_actualizacion=CURRENT_TIMESTAMP''',
                (vehicle['id'], advance, crane, freight, estimated, adjustment, isv, std, storage, customs,
                 cost_status, adjustment, plate or None))
            server.add_movimiento(connection, vehicle['id'], purchase_date, 'Migración histórica cuarto lote', vehicle['estado'], 'En Tránsito',
                vehicle['ubicacion'], vehicle['ubicacion'], referencia='CxP proveedores',
                observaciones=f'Adquisición {estimated:.2f}; anticipo EXW {advance:.2f}; nacionalización {cost_status}.')
            lines = [{'cuenta_id': inventory, 'debe': estimated}]
            if advance:
                lines.append({'cuenta_id': cash, 'haber': advance})
                server.registrar_movimiento_caja(connection, purchase_date, 'Anticipo histórico de compra', 'Efectivo', None, 0, advance,
                    f'Anticipo EXW · {vin}', 'migracion_lote4_anticipo', acquisition_id)
            if balance:
                lines.append({'cuenta_id': payable, 'haber': balance})
            server.registrar_asiento(connection, purchase_date, f'Adquisición histórica cuarto lote · {vin}',
                'migracion_adquisicion_lote4', acquisition_id, lines, vehicle['id'])
            total_acquisition += estimated
            total_advance += advance
            total_payable += balance
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    print(f'Registros cargados: {len(rows)}')
    print(f'Valor de adquisición: {total_acquisition:,.2f}')
    print(f'Anticipos EXW: {total_advance:,.2f}')
    print(f'CxP contable: {total_payable:,.2f}')


if __name__ == '__main__':
    main()
