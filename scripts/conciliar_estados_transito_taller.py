#!/usr/bin/env python3
"""Alinea solo los excedentes de En Tránsito/En Taller con el histórico."""
from datetime import datetime
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server
from inspeccionar_libro_excel import rows

ARCHIVO=Path('/Users/erickmadrid/Library/Mobile Documents/com~apple~CloudDocs/Autolote AAA/Sistema de Gestión Corporación Triple AAA V3 Version WEb.xlsm')
ESTADOS_DESTINO={'DPV','Vendido'}


def main():
    source_rows=rows(ARCHIVO,'Vehiculo'); headers=source_rows[3]
    history={}
    for row in source_rows[4:]:
        item=dict(zip(headers,row)); vin=str(item.get('VIN') or '').strip().upper()
        if vin: history[vin]=str(item.get('Estatus General') or '').strip()
    database=ROOT/'data'/'autolote.sqlite'
    backup=ROOT/'data'/f'autolote-antes-conciliar-estados-{datetime.now():%Y%m%d-%H%M%S}.sqlite'
    shutil.copy2(database,backup)
    connection=server.db()
    try:
        candidates=connection.execute("SELECT * FROM vehiculos WHERE estado IN ('En Tránsito','En Taller')").fetchall()
        updates=[(vehicle,history.get(vehicle['vin'])) for vehicle in candidates
                 if history.get(vehicle['vin']) in ESTADOS_DESTINO and history.get(vehicle['vin'])!=vehicle['estado']]
        if len(updates)!=11:
            raise ValueError(f'Se esperaban 11 ajustes de estado; se encontraron {len(updates)}.')
        connection.execute('BEGIN')
        for vehicle, target in updates:
            connection.execute('UPDATE vehiculos SET estado=? WHERE id=?',(target,vehicle['id']))
            server.add_movimiento(connection,vehicle['id'],datetime.now().strftime('%Y-%m-%d'),
                'Conciliación de estatus histórico',vehicle['estado'],target,
                vehicle['ubicacion'],vehicle['ubicacion'],referencia='Histórico Excel',
                observaciones='Estatus alineado con la hoja Vehiculo del histórico; costo sin cambios.')
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    print(f'Respaldo: {backup}')
    print(f'Estatus actualizados: {len(updates)}')
    for vehicle,target in updates:
        print(f"{vehicle['vin']}: {vehicle['estado']} -> {target}")


if __name__=='__main__':
    main()
