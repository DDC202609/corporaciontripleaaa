#!/usr/bin/env python3
"""Carga el lote consolidado directamente a DPV sin duplicar adquisiciones."""
from datetime import datetime
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import server


DATA = """WA1LYAFE9AD001716|HBP2923|200590.00|Proveedor Local-SI|Compra Local|2026-03-23
5UXWX7C56E0E77370|JDM4942|223088.00|Importaciones Sierra|Importación|2026-02-04
WBA3A5C54EP601258|HAG2585|175000.00|Proveedor Local-SI|Compra Local|2026-02-13
1GCGTCEN0J1259837|JDD7610|308325.00|Luis Alonso Vasquez Murillo-COPART|Importación|2025-08-30
1GCHSBEA8N1187497|HXW7380|220000.00|INVERSA|Compra Local|2026-03-27
1D7RB1GP5AS165175|HDH0087|157584.00|INVERSA|Compra Local|2025-12-06
3C6RR7LT5HG580145|JAH3522|298081.00|INVERSA|Compra Local|2025-12-06
1FM5K7F80GGA09347|HBQ5508|261150.00|Proveedor Local-SI|Compra Local|2025-06-03
1FMCU0F66LUA55140|JAF3768|210082.00|INVERSA|Compra Local|2025-12-20
1FTER4FH4LLA06760|JDL3513|484878.00|Luis Alonso Vasquez Murillo-COPART|Importación|2026-01-19
1FM5K7D8XKGB07149|JDL0117|269670.00|Copart|Importación|2026-02-02
1FTFW1E89PFA46849|JDM2040|717252.00|Antonio Fajardo Jr - Copart|Importación|2026-02-28
1FM5K8GT9HGB90880|HDO5802|221000.00|Proveedor Local-SI|Compra Local|2026-03-12
1FMCU0EG9BK77804|JAE2204|96731.89|INVERSA|Compra Local|2026-03-27
1FMCU0H95DUB37532|HAI8260|92300.00|Proveedor Local-SI|Compra Local|2026-03-29
1FTEW1EP6HKC28116|JDM4897|324250.00|Importaciones Sierra|Importación|2026-04-15
1FMCU0H68LUA23206|HDL6043|233200.00|Proveedor Local-SI|Compra Local|2026-05-14
1FM5K7D81JGA52105|JAY4099|253150.00|Proveedor Local-SI|Compra Local|2026-05-19
1FMCU0C70CKB74908|JAL0964|138700.00|Proveedor Local-SI|Compra Local|2026-05-22
JHLRD68463C008552|JDK6401|141694.00|Importaciones Sierra|Importación|2026-01-13
5J6RW1H55NA002687|JDK4766|454750.00|Antonio Fajardo Jr - Copart|Importación|2026-01-21
5FNRL5H66FB089426|JDI9553|256200.00|Importaciones Sierra|Importación|2026-01-22
3CZRU6H32NM731007|JDM1018|364100.00|Importaciones Sierra|Importación|2026-02-06
2HKRM3H33FH506283|HDY4269|266000.00|Proveedor Local-SI|Compra Local|2026-02-10
5J6RW1H82MA008424|JAE4944|272500.00|Proveedor Local-SI|Compra Local|2026-02-12
5J6RE4H59BL023661|JDO3660|192100.00|Importaciones Sierra|Importación|2026-02-19
1HGCV2F58KA002798|HAG5587|300000.00|Proveedor Local-SI|Compra Local|2026-03-11
19XFB2F56DE008264|HAB5996|150000.00|Proveedor Local-SI|Compra Local|2026-05-07
19XFB2F95CE310193|HBI7062|119000.00|Proveedor Local-SI|Compra Local|2026-05-20
5J6MR4H31DL074639|HBF2122|225000.00|Cambio - SI|Cambio|2026-06-05
2HKRW1H52MH420914|JAQ4917|430000.00|Proveedor Local-SI|Compra Local|2026-07-13
5NMS23AD2KH030304|JAT0489|227721.38|INVERSA|Compra Local|2025-12-06
KMHDU46D28U570198|HBN1317|72600.00|Proveedor Local-SI|Compra Local|2026-03-14
5NPD84LF5HH142179|HBH1019|170101.00|INVERSA|Compra Local|2026-03-27
5NPDH4AE5DH329359|JDC3736|130000.00|Proveedor Local-SI|Compra Local|2026-03-29
5NMZT3LB0JH071374|JAE8444|238000.00|INVERSA|Compra Local|2026-04-29
5NPDH4AEXDH159449|HDQ8375|146000.00|Cambio - SI|Cambio|2026-05-31
JN8AZ2NC3C9316344|JDI5724|219000.00|Proveedor Local-SI|Compra Local|2025-12-20
1C4NJRFBXDD234600|JAT2563|158390.00|Importaciones Sierra|Importación|2024-10-01
1C4NJCVA0ED803658|HBE6778|102230.00|INVERSA|Compra Local|2025-12-20
5XYPGDA37HG257582|JDP0435|187862.00|Luis Alonso Vasquez Murillo-COPART|Importación|2026-04-14
5LMJJ2JT8GEL02941|JDB7917|326059.00|Importaciones Sierra|Importación|2025-05-25
WDDGJ4HB7CF814632|HBF1070|279000.00|Importaciones Sierra|Importación|2025-10-12
WMWLN9C53G2E47592|JDF5586|199250.00|Luis Alonso Vasquez Murillo-COPART|Importación|2025-11-10
JA4AR3AU6HZ043224|HBQ3117|135000.00|INVERSA|Compra Local|2025-12-06
5N1AZ2MG7JN122982|HDG7753|285822.00|INVERSA|Compra Local|2025-12-06
5N1AT2MV6HC830436|HAI4657|149880.00|INVERSA|Compra Local|2025-12-06
JN8AT2MV5HW284896|JAG9510|217489.00|Cambio - SI|Cambio|2025-12-20
5N1AT2MT4FC904688|HEG0729|154755.00|INVERSA|Compra Local|2025-12-20
1N6AD0EV2FN730837|JDL2467|234620.00|Importaciones Sierra|Importación|2026-02-21
1N6AD0CW9CC455812|JDL1879|207735.00|Importaciones Sierra|Importación|2026-03-04
1N6AD0ER7AC408959|JAT0719|149868.00|INVERSA|Compra Local|2026-03-27
5N1AT2MV7GC756362|JDQ9136|165812.00|Luis Alonso Vasquez Murillo-COPART|Importación|2026-06-01
4XAMH76A5DA051116||130000.00|Proveedor Local-SI|Compra Local|2025-04-01
JT6HT00W7X0035859|JAJ6414|228650.00|Importaciones Sierra|Importación|2024-02-03
1NXBR32EX3Z180928|JDK2365|131750.00|Importaciones Sierra|Importación|2026-01-22
2T3C1RFV7MW158855|JDL2556|560468.00|Moises Fajardo - Copart|Importación|2026-02-04
2T3ZFREB1FW225188|JDO6520|333750.00|Importaciones Sierra|Importación|2026-04-09
2T3W1RFV4LW073399|JDO3106|420732.00|Luis Alonso Vasquez Murillo-COPART|Importación|2026-04-15
JTEBH9FJ505090122|HAW9572|850000.00|Cambio - SI|Compra Local|2026-04-28
3TMCZ5AN2LM308430|JDK8114|560000.00|Proveedor Local-SI|Compra Local|2026-05-08
1NXBU4EE1AZ177915|HAK8362|144500.00|Cambio - SI|Cambio|2026-05-23
1NXBR32E23Z179806|JAE2914|72000.00|Cambio - SI|Cambio|2026-05-30"""


def records():
    return [tuple(part.strip() for part in line.split('|')) for line in DATA.splitlines() if line.strip()]


def account_id(connection, code):
    row = connection.execute('SELECT id FROM cuentas_contables WHERE codigo=? AND activo=1', (code,)).fetchone()
    if not row:
        raise ValueError(f'No existe la cuenta contable activa {code}.')
    return row['id']


def upsert_costing(connection, vehicle_id, cost, plate):
    connection.execute('''INSERT INTO costos_adquisicion
        (vehiculo_id,costo_exw,costo_estimado,estatus,placa_nacionalizacion,fecha_actualizacion)
        VALUES(?,?,?,'Nacionalizado',?,CURRENT_TIMESTAMP)
        ON CONFLICT(vehiculo_id) DO UPDATE SET
            costo_estimado=excluded.costo_estimado,estatus='Nacionalizado',
            placa_nacionalizacion=excluded.placa_nacionalizacion,fecha_actualizacion=CURRENT_TIMESTAMP''',
        (vehicle_id, 0, cost, plate or None))


def main():
    lote = records()
    if len(lote) != 63:
        raise ValueError(f'Se esperaban 63 vehículos; se recibieron {len(lote)}.')
    server.init_db()
    connection = server.db()
    try:
        inventory_dpv = account_id(connection, '1103')
        payable = account_id(connection, '2101')
        created = updated = 0
        total = 0.0
        connection.execute('BEGIN')
        for vin, plate, cost_text, provider_name, purchase_type, purchase_date in lote:
            cost = float(cost_text)
            vehicle = connection.execute('SELECT * FROM vehiculos WHERE vin=?', (vin,)).fetchone()
            provider = connection.execute('SELECT * FROM proveedores WHERE nombre=? AND activo=1', (provider_name,)).fetchone()
            if not vehicle:
                raise ValueError(f'No existe el vehículo maestro {vin}.')
            if not provider:
                raise ValueError(f'No existe el proveedor activo {provider_name}.')
            plate = '' if plate.upper() in ('', 'N/A', '0') else plate.upper()
            if plate and not server.placa_disponible(connection, plate, vehicle['id']):
                raise ValueError(f'La placa {plate} ya está registrada en otro vehículo.')
            purchase = connection.execute('SELECT * FROM adquisiciones WHERE vehiculo_id=?', (vehicle['id'],)).fetchone()
            note = 'Migración histórica. Lote consolidado disponible para venta. Contrapartida: CxP proveedores.'
            if purchase:
                # Solo los 19 registros iniciales ya existen y se reclasifican, no se duplican.
                if not (purchase['observaciones'] or '').startswith('Migración histórica.'):
                    raise ValueError(f'El VIN {vin} tiene una compra operativa existente; no se sobrescribirá.')
                acquisition_id = purchase['id']
                connection.execute('''UPDATE adquisiciones SET proveedor_id=?,fecha=?,costo_compra=?,anticipo=0,
                    metodo_pago='Efectivo',condicion_pago='Contado',dias_credito=0,saldo=?,observaciones=?,
                    necesita_reparacion=0,tipo_compra=?,placa_cambio=NULL,banco=NULL WHERE id=?''',
                    (provider['id'], purchase_date, cost, cost, note, purchase_type, acquisition_id))
                headers = connection.execute("SELECT id FROM asientos_contables WHERE referencia_id=? AND referencia_tipo LIKE 'migracion_adquisicion%'", (acquisition_id,)).fetchall()
                for header in headers:
                    connection.execute('UPDATE asientos_contables SET fecha=?,descripcion=?,referencia_tipo=? WHERE id=?',
                        (purchase_date, f'Adquisición histórica DPV · {vin} · CxP proveedores', 'migracion_adquisicion_dpv_consolidado', header['id']))
                    connection.execute('''UPDATE partidas SET fecha=?,descripcion=?,referencia_tipo=?,
                        cuenta_id=CASE WHEN debe>0 THEN ? ELSE cuenta_id END,
                        debe=CASE WHEN debe>0 THEN ? ELSE 0 END,
                        haber=CASE WHEN haber>0 THEN ? ELSE 0 END
                        WHERE asiento_id=?''',
                        (purchase_date, f'Adquisición histórica DPV · {vin} · CxP proveedores',
                         'migracion_adquisicion_dpv_consolidado', inventory_dpv, cost, cost, header['id']))
                if not headers:
                    server.registrar_asiento(connection, purchase_date, f'Adquisición histórica DPV · {vin} · CxP proveedores',
                        'migracion_adquisicion_dpv_consolidado', acquisition_id,
                        [{'cuenta_id': inventory_dpv, 'debe': cost}, {'cuenta_id': payable, 'haber': cost}], vehicle['id'])
                updated += 1
            else:
                cur = connection.execute('''INSERT INTO adquisiciones
                    (vehiculo_id,proveedor_id,fecha,costo_compra,anticipo,metodo_pago,condicion_pago,dias_credito,saldo,observaciones,necesita_reparacion,tipo_compra,placa_cambio,banco)
                    VALUES(?,?,?,? ,0,'Efectivo','Contado',0,?,?,0,?,?,NULL)''',
                    (vehicle['id'], provider['id'], purchase_date, cost, cost, note, purchase_type, None))
                acquisition_id = cur.lastrowid
                server.registrar_asiento(connection, purchase_date, f'Adquisición histórica DPV · {vin} · CxP proveedores',
                    'migracion_adquisicion_dpv_consolidado', acquisition_id,
                    [{'cuenta_id': inventory_dpv, 'debe': cost}, {'cuenta_id': payable, 'haber': cost}], vehicle['id'])
                created += 1
            connection.execute('''UPDATE vehiculos SET placa=?,precio_compra=?,proveedor=?,fecha_adquisicion=?,tipo_compra=?,estado='DPV'
                WHERE id=?''', (plate or None, cost, provider_name, purchase_date, purchase_type, vehicle['id']))
            upsert_costing(connection, vehicle['id'], cost, plate)
            server.add_movimiento(connection, vehicle['id'], purchase_date, 'Migración a DPV', vehicle['estado'], 'DPV',
                vehicle['ubicacion'], vehicle['ubicacion'], referencia='CxP proveedores',
                observaciones=f'Costo acumulado migrado: {cost:.2f}. Disponible para venta.')
            total += cost
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    print(f'Registros nuevos: {created}')
    print(f'Registros reclasificados: {updated}')
    print(f'Costo acumulado del lote: {total:,.2f}')


if __name__ == '__main__':
    main()
