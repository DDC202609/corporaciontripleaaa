#!/usr/bin/env python3
"""Devuelve a DPV vehículos históricos marcados como vendidos sin venta migrada."""
from datetime import datetime
from pathlib import Path
import shutil
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import server

VINS=(
    '1GCGTCEN0J1259837','1GCGTDE39G1270896','1FTER4FH4LLA06760',
    '1FTFW1E89PFA46849','1FTEW1EP6HKC28116','5NMZT3LB8JH059358',
    '5TBRT54147S456968',
)

def main():
    database=ROOT/'data'/'autolote.sqlite'
    backup=ROOT/'data'/f'autolote-antes-vendidos-a-dpv-{datetime.now():%Y%m%d-%H%M%S}.sqlite'
    shutil.copy2(database,backup)
    c=server.db()
    try:
        vehicles=c.execute("SELECT * FROM vehiculos WHERE vin IN ({})".format(','.join('?'*len(VINS))),VINS).fetchall()
        if len(vehicles)!=len(VINS) or any(v['estado']!='Vendido' for v in vehicles):
            raise ValueError('Los siete vehículos esperados no están exactamente en estado Vendido.')
        c.execute('BEGIN')
        for vehicle in vehicles:
            c.execute("UPDATE vehiculos SET estado='DPV' WHERE id=?",(vehicle['id'],))
            server.add_movimiento(c,vehicle['id'],datetime.now().strftime('%Y-%m-%d'),
                'Corrección de estatus histórico','Vendido','DPV',vehicle['ubicacion'],vehicle['ubicacion'],
                referencia='Sin venta migrada',observaciones='La venta histórica aún no se ha migrado; unidad disponible para venta.')
        c.commit()
    except Exception:
        c.rollback(); raise
    finally:
        c.close()
    print(f'Respaldo: {backup}')
    print(f'Vehículos devueltos a DPV: {len(vehicles)}')

if __name__=='__main__':
    main()
