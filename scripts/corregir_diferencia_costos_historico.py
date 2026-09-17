#!/usr/bin/env python3
"""Corrige una conciliación puntual contra el libro histórico de vehículos.

Reglas autorizadas por el usuario:
* 16 adquisiciones del cuarto lote quedan al valor acumulado mostrado por el
  libro histórico. En esos casos dicho valor corresponde al anticipo ya
  registrado; por tanto no queda CxP pendiente.
* Dos vehículos que se importaron sin compra reciben su adquisición histórica
  completa. Los conceptos de OT indicados no forman parte de su costo.

El script conserva una copia de seguridad antes de iniciar y ejecuta toda la
operación en una sola transacción.
"""
from datetime import datetime, timedelta
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server


# Costo acumulado confirmado en la hoja Vehiculo del histórico.
CORRECCIONES_ANTICIPO = {
    '2T3H1RFV0MC090163': 235_000.00,
    '1FTEW1EP0NFC08089': 200_000.00,
    '1FMSK8DH1MGC08687': 166_000.00,
    '1FTER4FH9NLD36137': 170_000.00,
    '1FTER4FH4KLA26652': 170_000.00,
    '1N6AD0EV0JN713768': 140_000.00,
    '2HKRW1H85KH510615': 140_000.00,
    '1N6AD0EV1CC450284': 100_000.00,
    '5J6RM4H38DL007200': 105_000.00,
    '1N6AD0EV8CC476333': 80_000.00,
    '5J6RM4H70DL059763': 105_000.00,
    # Incluye L 2,700.00 de gastos de taller que ya estaban cargados.
    '2T3H1RFV4MC139297': 402_777.00,
    '4A4AP3AUXEE003459': 70_000.00,
    '4A4AP3AUXDE022172': 70_000.00,
    '5N1AT2MV1EC831389': 60_000.00,
    '5NPD84LF6HH091405': 52_789.00,
}

COMPRAS_FALTANTES = {
    '1GCGTDE39G1270896': {
        'costo': 295_981.00, 'fecha': '2025-12-20',
        'proveedor': 'INVERSA', 'tipo': 'Importación',
    },
    '1GCGSBEA7J1163732': {
        'costo': 214_267.00, 'fecha': '2026-03-27',
        'proveedor': 'INVERSA', 'tipo': 'Compra Local',
    },
}


def account_id(connection, code):
    row = connection.execute(
        'SELECT id FROM cuentas_contables WHERE codigo=? AND activo=1', (code,)
    ).fetchone()
    if not row:
        raise ValueError(f'No existe la cuenta contable activa {code}.')
    return row['id']


def borrar_asiento_adquisicion(connection, acquisition_id):
    headers = connection.execute(
        "SELECT id FROM asientos_contables WHERE referencia_id=? "
        "AND referencia_tipo LIKE 'migracion_adquisicion_lote4%'", (acquisition_id,)
    ).fetchall()
    for header in headers:
        connection.execute('DELETE FROM partidas WHERE asiento_id=?', (header['id'],))
        connection.execute('DELETE FROM asientos_contables WHERE id=?', (header['id'],))


def registrar_adquisicion_pagada(connection, vehicle, acquisition_id, fecha, costo, descripcion, referencia_tipo):
    """Registra inventario contra caja: adquisición totalmente pagada."""
    server.registrar_asiento(connection, fecha, descripcion, referencia_tipo, acquisition_id, [
        {'cuenta_id': account_id(connection, '1105'), 'debe': costo},
        {'cuenta_id': account_id(connection, '1101'), 'haber': costo},
    ], vehicle['id'])


def main():
    server.init_db()
    database = ROOT / 'data' / 'autolote.sqlite'
    backup = ROOT / 'data' / f'autolote-antes-conciliacion-costos-{datetime.now():%Y%m%d-%H%M%S}.sqlite'
    shutil.copy2(database, backup)
    connection = server.db()
    try:
        connection.execute('BEGIN')
        corregidos = []
        for vin, costo_historico in CORRECCIONES_ANTICIPO.items():
            vehicle = connection.execute('SELECT * FROM vehiculos WHERE vin=?', (vin,)).fetchone()
            acquisition = connection.execute(
                'SELECT * FROM adquisiciones WHERE vehiculo_id=?', (vehicle['id'],)
            ).fetchone() if vehicle else None
            if not vehicle or not acquisition:
                raise ValueError(f'No se encontró adquisición para {vin}.')
            gastos = connection.execute(
                'SELECT COALESCE(SUM(monto), 0) FROM costos_vehiculo WHERE vehiculo_id=?',
                (vehicle['id'],)
            ).fetchone()[0]
            costo_compra = round(costo_historico - float(gastos), 2)
            if costo_compra <= 0 or abs(costo_compra - float(acquisition['anticipo'])) > 0.01:
                raise ValueError(
                    f'{vin}: el costo histórico menos gastos ({costo_compra:.2f}) '
                    f'no coincide con su anticipo ({float(acquisition["anticipo"]):.2f}).'
                )
            borrar_asiento_adquisicion(connection, acquisition['id'])
            connection.execute('DELETE FROM cuentas_por_pagar WHERE adquisicion_id=?', (acquisition['id'],))
            connection.execute('''UPDATE adquisiciones
                SET costo_compra=?, anticipo=?, saldo=0,
                    observaciones=COALESCE(observaciones, '') || ' · Conciliado con costo acumulado histórico.'
                WHERE id=?''', (costo_compra, costo_compra, acquisition['id']))
            connection.execute('UPDATE vehiculos SET precio_compra=? WHERE id=?', (costo_compra, vehicle['id']))
            connection.execute('''UPDATE costos_adquisicion
                SET costo_exw=?, costo_estimado=?, fecha_actualizacion=CURRENT_TIMESTAMP
                WHERE vehiculo_id=?''', (costo_compra, costo_compra, vehicle['id']))
            registrar_adquisicion_pagada(
                connection, vehicle, acquisition['id'], acquisition['fecha'], costo_compra,
                f'Adquisición histórica conciliada · {vin}',
                'migracion_adquisicion_lote4_conciliada',
            )
            corregidos.append((vin, costo_historico, costo_compra, float(gastos)))

        restaurados = []
        for vin, info in COMPRAS_FALTANTES.items():
            vehicle = connection.execute('SELECT * FROM vehiculos WHERE vin=?', (vin,)).fetchone()
            if not vehicle:
                raise ValueError(f'No existe el vehículo {vin}.')
            existing = connection.execute('SELECT id FROM adquisiciones WHERE vehiculo_id=?', (vehicle['id'],)).fetchone()
            if existing:
                raise ValueError(f'{vin} ya tiene adquisición {existing["id"]}; no se reemplaza automáticamente.')
            provider = connection.execute(
                'SELECT id FROM proveedores WHERE nombre=? AND activo=1', (info['proveedor'],)
            ).fetchone()
            if not provider:
                raise ValueError(f'No existe el proveedor {info["proveedor"]}.')
            # Los dos conceptos de OT fueron indicados expresamente como no registrables.
            cost_ids = connection.execute(
                'SELECT id FROM costos_vehiculo WHERE vehiculo_id=?', (vehicle['id'],)
            ).fetchall()
            for cost in cost_ids:
                connection.execute('DELETE FROM gastos_ot WHERE costo_vehiculo_id=?', (cost['id'],))
            connection.execute('DELETE FROM costos_vehiculo WHERE vehiculo_id=?', (vehicle['id'],))
            cursor = connection.execute('''INSERT INTO adquisiciones
                (vehiculo_id, proveedor_id, fecha, costo_compra, anticipo, metodo_pago,
                 condicion_pago, dias_credito, saldo, observaciones, necesita_reparacion,
                 tipo_compra, placa_cambio, banco)
                VALUES(?,?,?,?,?,'Efectivo','Contado',0,0,?,0,?,NULL,NULL)''',
                (vehicle['id'], provider['id'], info['fecha'], info['costo'], info['costo'],
                 'Compra histórica restaurada desde conciliación; sin gasto de OT.', info['tipo']))
            acquisition_id = cursor.lastrowid
            connection.execute('''UPDATE vehiculos SET precio_compra=?, proveedor=?, fecha_adquisicion=?, tipo_compra=?
                WHERE id=?''', (info['costo'], info['proveedor'], info['fecha'], info['tipo'], vehicle['id']))
            connection.execute('''INSERT INTO costos_adquisicion
                (vehiculo_id, costo_exw, costo_estimado, estatus, fecha_actualizacion)
                VALUES(?,?,?,'Pendiente',CURRENT_TIMESTAMP)
                ON CONFLICT(vehiculo_id) DO UPDATE SET costo_exw=excluded.costo_exw,
                    costo_estimado=excluded.costo_estimado, fecha_actualizacion=CURRENT_TIMESTAMP''',
                (vehicle['id'], info['costo'], info['costo']))
            server.registrar_movimiento_caja(
                connection, info['fecha'], 'Compra histórica conciliada', 'Efectivo', None, 0, info['costo'],
                f'Compra restaurada · {vin}', 'migracion_compra_conciliada', acquisition_id)
            registrar_adquisicion_pagada(
                connection, vehicle, acquisition_id, info['fecha'], info['costo'],
                f'Compra histórica restaurada · {vin}', 'migracion_compra_conciliada',
            )
            restaurados.append((vin, info['costo']))

        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    print(f'Respaldo: {backup}')
    print(f'Adquisiciones conciliadas: {len(corregidos)}')
    for vin, total, compra, gastos in corregidos:
        print(f'{vin}: costo acumulado={total:.2f}; compra={compra:.2f}; gastos conservados={gastos:.2f}')
    print(f'Compras restauradas sin gasto de OT: {len(restaurados)}')
    for vin, costo in restaurados:
        print(f'{vin}: compra={costo:.2f}')


if __name__ == '__main__':
    main()
