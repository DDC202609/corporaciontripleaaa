#!/usr/bin/env python3
"""Aplica la regla de migración: una OT histórica reparada queda en DPV."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import server


def main():
    server.init_db()
    connection = server.db()
    try:
        rows = connection.execute('''SELECT DISTINCT o.vehiculo_id FROM ordenes_trabajo o
            JOIN adquisiciones a ON a.vehiculo_id=o.vehiculo_id
            WHERE o.migracion_historica=1 AND o.estado='Finalizada' AND a.tipo_compra='Importación' ''').fetchall()
        connection.execute('BEGIN')
        for row in rows:
            connection.execute('''INSERT INTO costos_adquisicion(vehiculo_id,estatus,fecha_actualizacion)
                VALUES(?,'Nacionalizado',CURRENT_TIMESTAMP)
                ON CONFLICT(vehiculo_id) DO UPDATE SET estatus='Nacionalizado',fecha_actualizacion=CURRENT_TIMESTAMP''',
                (row['vehiculo_id'],))
            server.recalcular_estado(connection, row['vehiculo_id'], 'OT histórica reparada y nacionalizada')
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    print(f'Vehículos verificados: {len(rows)}')


if __name__ == '__main__':
    main()
