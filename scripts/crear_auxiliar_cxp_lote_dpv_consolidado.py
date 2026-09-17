#!/usr/bin/env python3
"""Expone en CxP el saldo pendiente del lote histórico consolidado en DPV."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import server


def main():
    server.init_db()
    connection = server.db()
    try:
        purchases = connection.execute("""SELECT id,proveedor_id,fecha,saldo
            FROM adquisiciones
            WHERE observaciones LIKE 'Migración histórica. Lote consolidado%'
              AND ROUND(COALESCE(saldo,0),2)>0
            ORDER BY fecha,id""").fetchall()
        connection.execute('BEGIN')
        for purchase in purchases:
            balance = round(float(purchase['saldo'] or 0), 2)
            connection.execute('''INSERT INTO cuentas_por_pagar
                (adquisicion_id,proveedor_id,fecha,monto_original,saldo,estado)
                VALUES(?,?,?,?,?,'Pendiente')
                ON CONFLICT(adquisicion_id) DO UPDATE SET
                    proveedor_id=excluded.proveedor_id,fecha=excluded.fecha,
                    monto_original=excluded.monto_original,saldo=excluded.saldo,estado='Pendiente' ''',
                (purchase['id'], purchase['proveedor_id'], purchase['fecha'], balance, balance))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    print(f'Auxiliares CxP creados/actualizados: {len(purchases)}')
    print(f'Saldo pendiente total: {sum(float(row["saldo"] or 0) for row in purchases):,.2f}')


if __name__ == '__main__':
    main()
