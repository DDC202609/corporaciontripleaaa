#!/usr/bin/env python3
"""Elimina conceptos históricos de taller cargados como complementos de L 1.00/L 0.01."""
from datetime import datetime
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server


def main():
    server.init_db()
    database = ROOT / 'data' / 'autolote.sqlite'
    backup = ROOT / 'data' / f'autolote-antes-eliminar-complementos-{datetime.now():%Y%m%d-%H%M%S}.sqlite'
    shutil.copy2(database, backup)
    connection = server.db()
    try:
        rows = connection.execute('''
            SELECT cv.id, v.vin, cv.monto
            FROM costos_vehiculo cv
            JOIN vehiculos v ON v.id=cv.vehiculo_id
            WHERE ROUND(cv.monto, 2) IN (0.01, 1.00)
              AND LOWER(TRIM(cv.concepto))='complemento'
            ORDER BY cv.id
        ''').fetchall()
        connection.execute('BEGIN')
        for row in rows:
            connection.execute('DELETE FROM gastos_ot WHERE costo_vehiculo_id=?', (row['id'],))
            connection.execute('DELETE FROM costos_vehiculo WHERE id=?', (row['id'],))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    print(f'Respaldo: {backup}')
    print(f'Conceptos eliminados: {len(rows)}')
    print(f'Total eliminado: {sum(float(row["monto"]) for row in rows):.2f}')
    for row in rows:
        print(f'{row["vin"]}: {float(row["monto"]):.2f}')


if __name__ == '__main__':
    main()
