"""Rebase contable al saldo inicial oficial del 31 de julio de 2026.

Conserva las entidades operativas (vehículos, adquisiciones, ventas, etc.),
retira del mayor los asientos anteriores al 1 de agosto y registra una apertura
detallada. Los movimientos desde agosto permanecen como operación posterior.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "autolote.sqlite"
CUTOFF = "2026-08-01"
OPENING_DATE = "2026-07-31"
OPENING_DESCRIPTION = "Apertura contable oficial al 31-jul-2026"
SYNTHETIC_PAYMENT_PREFIX = "AJUSTE-CXP-HIST-20260916%"

BANKS = (
    ("BAC", 733_804.05),
    ("Ficohsa", 292_213.00),
    ("Atlántida Personal AFJ", 1_250_000.00),
    ("Occidente", 478_834.00),
)
OPENING_LINES = (
    ("1103", 11_398_515.00, 0, "Saldo inicial · Inventario DPV"),
    ("1105", 4_823_595.00, 0, "Saldo inicial · Inventario en tránsito"),
    ("1104", 7_713_957.00, 0, "Saldo inicial · Inventario en taller"),
    ("1101", 2_754_851.05, 0, "Saldo inicial · Bancos"),
    ("2104", 0, 6_000_000.00, "Saldo inicial · Exclusión de capital / CxP socios"),
    ("2105", 0, 450_000.00, "Saldo inicial · Otras CxP · José Elías Paz"),
    ("2105", 0, 437_500.00, "Saldo inicial · Otras CxP · Moisés Fajardo"),
    ("2105", 0, 475_000.00, "Saldo inicial · Otras CxP · Andrés Fajardo"),
    ("2201", 0, 700_000.00, "Saldo inicial · Préstamo Ficohsa"),
    ("2201", 0, 215_000.00, "Saldo inicial · Préstamo Davivienda"),
    ("3101", 0, 50_000.00, "Saldo inicial · Capital social"),
    # Ajusta L 0.05 derivado del saldo bancario detallado recibido.
    ("3103", 0, 18_363_418.05, "Saldo inicial · Resultados acumulados"),
)


def account_id(cursor, code):
    row = cursor.execute(
        "SELECT id FROM cuentas_contables WHERE codigo=? AND activo=1 AND acepta_movimiento=1",
        (code,),
    ).fetchone()
    if not row:
        raise ValueError(f"No existe la cuenta activa {code}.")
    return row[0]


def ensure_other_payables_account(cursor):
    row = cursor.execute("SELECT id FROM cuentas_contables WHERE codigo='2105'").fetchone()
    if row:
        return row[0]
    cursor.execute("""
        INSERT INTO cuentas_contables(codigo,cuenta,tipo,grupo,naturaleza,acepta_movimiento,requiere_activo,requiere_centro_costo,activo)
        VALUES('2105','Otras Cuentas por Pagar','Pasivo','Pasivo Corriente','Acreedora',1,0,0,1)
    """)
    return cursor.lastrowid


def synthetic_payments(cursor):
    return cursor.execute("""
        SELECT id, cuenta_por_pagar_id
        FROM pagos_cuentas_por_pagar
        WHERE referencia LIKE ?
        ORDER BY id
    """, (SYNTHETIC_PAYMENT_PREFIX,)).fetchall()


def describe(cursor):
    old_parts = cursor.execute("SELECT COUNT(*) FROM partidas WHERE fecha<?", (CUTOFF,)).fetchone()[0]
    old_headers = cursor.execute("SELECT COUNT(*) FROM asientos_contables WHERE fecha<?", (CUTOFF,)).fetchone()[0]
    old_cash = cursor.execute("SELECT COUNT(*) FROM movimientos_caja WHERE fecha<?", (CUTOFF,)).fetchone()[0]
    payments = synthetic_payments(cursor)
    debit = round(sum(line[1] for line in OPENING_LINES), 2)
    credit = round(sum(line[2] for line in OPENING_LINES), 2)
    if abs(debit - credit) > 0.01:
        raise ValueError(f"La apertura no cuadra: debe {debit:.2f}, haber {credit:.2f}.")
    return old_parts, old_headers, old_cash, payments, debit, credit


def restore_cxp_after_synthetic_payments(cursor, account_ids):
    if not account_ids:
        return
    marks = ",".join("?" for _ in account_ids)
    rows = cursor.execute(f"""
        SELECT cp.id, cp.monto_original, COALESCE(SUM(pc.monto),0) pagos_reales
        FROM cuentas_por_pagar cp
        LEFT JOIN pagos_cuentas_por_pagar pc ON pc.cuenta_por_pagar_id=cp.id
        WHERE cp.id IN ({marks})
        GROUP BY cp.id, cp.monto_original
    """, account_ids).fetchall()
    for row in rows:
        balance = round(max(row["monto_original"] - row["pagos_reales"], 0), 2)
        state = "Pagada" if balance <= 0.01 else "Pendiente"
        cursor.execute("UPDATE cuentas_por_pagar SET saldo=?, estado=? WHERE id=?", (balance, state, row["id"]))


def rebase(apply=False):
    connection = sqlite3.connect(DB)
    connection.row_factory = sqlite3.Row
    cursor = connection.cursor()
    try:
        if cursor.execute("SELECT 1 FROM asientos_contables WHERE descripcion=?", (OPENING_DESCRIPTION,)).fetchone():
            raise ValueError("La apertura de julio ya existe; no se aplicará dos veces.")
        old_parts, old_headers, old_cash, payments, debit, credit = describe(cursor)
        print(f"Partidas anteriores al corte a retirar: {old_parts}")
        print(f"Asientos anteriores al corte a retirar: {old_headers}")
        print(f"Movimientos de caja anteriores al corte a retirar: {old_cash}")
        print(f"Pagos técnicos de CxP a revertir: {len(payments)}")
        print(f"Apertura: Debe L {debit:,.2f} · Haber L {credit:,.2f}")
        if not apply:
            print("Revisión finalizada. Use --apply para ejecutar la rebase.")
            return

        cursor.execute("BEGIN")
        ensure_other_payables_account(cursor)
        # La cuenta de socios pertenece al catálogo vigente de la apertura.
        # Se habilita para recibir el saldo inicial sin crear una cuenta paralela.
        cursor.execute("UPDATE cuentas_contables SET activo=1, acepta_movimiento=1 WHERE codigo='2104'")
        accounts = {code: account_id(cursor, code) for code, *_ in OPENING_LINES}
        # Revierte la aplicación técnica de CxP, pues no representa pagos reales
        # posteriores a la apertura oficial.
        payment_ids = [row["id"] for row in payments]
        affected_accounts = list({row["cuenta_por_pagar_id"] for row in payments})
        if payment_ids:
            marks = ",".join("?" for _ in payment_ids)
            cursor.execute(f"DELETE FROM movimientos_caja WHERE referencia_tipo='pago_cxp' AND referencia_id IN ({marks})", payment_ids)
            cursor.execute(f"DELETE FROM partidas WHERE referencia_tipo='pago_cxp' AND referencia_id IN ({marks})", payment_ids)
            cursor.execute(f"DELETE FROM asientos_contables WHERE referencia_tipo='pago_cxp' AND referencia_id IN ({marks})", payment_ids)
            cursor.execute(f"DELETE FROM pagos_cuentas_por_pagar WHERE id IN ({marks})", payment_ids)
            restore_cxp_after_synthetic_payments(cursor, affected_accounts)

        # Se conserva la información operativa, pero la historia contable anterior
        # al corte se sustituye por el asiento de apertura.
        cursor.execute("DELETE FROM partidas WHERE fecha<?", (CUTOFF,))
        cursor.execute("DELETE FROM movimientos_caja WHERE fecha<?", (CUTOFF,))
        cursor.execute("DELETE FROM asientos_contables WHERE fecha<?", (CUTOFF,))

        header = cursor.execute("""
            INSERT INTO asientos_contables(fecha,descripcion,referencia_tipo,referencia_id)
            VALUES(?,?,?,?)
        """, (OPENING_DATE, OPENING_DESCRIPTION, "apertura_contable", 0))
        header_id = header.lastrowid
        for code, debit_value, credit_value, description in OPENING_LINES:
            cursor.execute("""
                INSERT INTO partidas(fecha,descripcion,referencia_tipo,referencia_id,cuenta_id,debe,haber,asiento_id)
                VALUES(?,?,?,?,?,?,?,?)
            """, (OPENING_DATE, description, "apertura_contable", header_id, accounts[code],
                  debit_value, credit_value, header_id))
        for index, (bank, balance) in enumerate(BANKS, 1):
            cursor.execute("""
                INSERT INTO movimientos_caja(fecha,tipo,medio,banco,entrada,salida,descripcion,referencia_tipo,referencia_id)
                VALUES(?,?,?,?,?,?,?,?,?)
            """, (OPENING_DATE, "Saldo inicial", "Banco", bank, balance, 0,
                  "Saldo inicial al 31-jul-2026", "apertura_banco", header_id * 10 + index))
        connection.commit()
        print("Rebase y apertura aplicadas correctamente.")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    rebase("--apply" in sys.argv)
