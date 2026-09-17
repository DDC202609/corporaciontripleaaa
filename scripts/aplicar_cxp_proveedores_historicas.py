"""Aplica el saldo íntegro del auxiliar de CxP a Bancos.

Es una conciliación histórica: genera un pago por cada obligación de proveedor,
actualiza el auxiliar, el movimiento de banco y el asiento de doble partida.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "autolote.sqlite"
DATE = "2026-09-16"
REFERENCE_PREFIX = "AJUSTE-CXP-HIST-20260916"
EXPECTED_TOTAL = 19_795_907.23


def account_id(cursor, code):
    row = cursor.execute(
        "SELECT id FROM cuentas_contables WHERE codigo=? AND activo=1 AND acepta_movimiento=1",
        (code,),
    ).fetchone()
    if not row:
        raise ValueError(f"No existe la cuenta activa {code}.")
    return row[0]


def pending_accounts(cursor):
    return cursor.execute("""
        SELECT cp.id, cp.saldo, p.nombre proveedor
        FROM cuentas_por_pagar cp
        JOIN proveedores p ON p.id=cp.proveedor_id
        WHERE cp.saldo>0.01
        ORDER BY cp.fecha, cp.id
    """).fetchall()


def apply(apply_changes=False):
    connection = sqlite3.connect(DB)
    connection.row_factory = sqlite3.Row
    cursor = connection.cursor()
    try:
        accounts = pending_accounts(cursor)
        total = round(sum(row["saldo"] for row in accounts), 2)
        existing = cursor.execute(
            "SELECT COUNT(*) FROM pagos_cuentas_por_pagar WHERE referencia LIKE ?",
            (REFERENCE_PREFIX + "%",),
        ).fetchone()[0]
        if existing:
            raise ValueError("La aplicación histórica de CxP ya fue registrada.")
        if abs(total - EXPECTED_TOTAL) > 0.01:
            raise ValueError(
                f"El saldo pendiente cambió: L {total:,.2f}; se esperaba L {EXPECTED_TOTAL:,.2f}."
            )
        print(f"Documentos pendientes a aplicar: {len(accounts)}")
        print(f"Total a aplicar: L {total:,.2f}")
        print(f"Fecha de conciliación: {DATE}")
        if not apply_changes:
            print("Revisión finalizada. Use --apply para registrar los pagos.")
            return

        payable_account = account_id(cursor, "2101")
        bank_account = account_id(cursor, "1101")
        cursor.execute("BEGIN")
        for account in accounts:
            amount = round(account["saldo"], 2)
            reference = f"{REFERENCE_PREFIX}-{account['id']}"
            payment = cursor.execute("""
                INSERT INTO pagos_cuentas_por_pagar(cuenta_por_pagar_id,fecha,tipo_pago,banco,referencia,monto)
                VALUES(?,?,?,?,?,?)
            """, (account["id"], DATE, "Transferencia", "BAC Credomatic", reference, amount))
            payment_id = payment.lastrowid
            cursor.execute(
                "UPDATE cuentas_por_pagar SET saldo=0, estado='Pagada' WHERE id=?",
                (account["id"],),
            )
            description = f"Aplicación histórica de CxP · {account['proveedor']}"
            cursor.execute("""
                INSERT INTO movimientos_caja(fecha,tipo,medio,banco,entrada,salida,descripcion,referencia_tipo,referencia_id)
                VALUES(?,?,?,?,?,?,?,?,?)
            """, (DATE, "Aplicación histórica de CxP", "Banco", "BAC Credomatic", 0, amount,
                  description, "pago_cxp", payment_id))
            journal = cursor.execute("""
                INSERT INTO asientos_contables(fecha,descripcion,referencia_tipo,referencia_id)
                VALUES(?,?,?,?)
            """, (DATE, description, "pago_cxp", payment_id))
            journal_id = journal.lastrowid
            cursor.execute("""
                INSERT INTO partidas(fecha,descripcion,referencia_tipo,referencia_id,cuenta_id,debe,haber,asiento_id)
                VALUES(?,?,?,?,?,?,?,?)
            """, (DATE, description, "pago_cxp", payment_id, payable_account, amount, 0, journal_id))
            cursor.execute("""
                INSERT INTO partidas(fecha,descripcion,referencia_tipo,referencia_id,cuenta_id,debe,haber,asiento_id)
                VALUES(?,?,?,?,?,?,?,?)
            """, (DATE, description, "pago_cxp", payment_id, bank_account, 0, amount, journal_id))
        connection.commit()
        print(f"Aplicación registrada: {len(accounts)} pagos por L {total:,.2f}.")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    apply("--apply" in sys.argv)
