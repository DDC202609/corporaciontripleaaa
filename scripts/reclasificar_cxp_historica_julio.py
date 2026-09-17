"""Reclasifica el saldo técnico de CxP al cierre de julio de 2026."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "autolote.sqlite"
DATE = "2026-07-31"
DESCRIPTION = "Reclasificación histórica de CxP y pasivos reales al 31-jul-2026"
EXPECTED_CXP = 21_633_085.01
SOCIOS = 6_000_000.00
OTRAS_CXP = (
    ("José Elías Paz", 450_000.00),
    ("Moisés Fajardo", 437_500.00),
    ("Andrés Fajardo", 475_000.00),
)
LOANS = (("Ficohsa", 700_000.00), ("Davivienda", 215_000.00))


def account_id(cursor, code):
    row = cursor.execute(
        "SELECT id FROM cuentas_contables WHERE codigo=? AND activo=1 AND acepta_movimiento=1",
        (code,),
    ).fetchone()
    if not row:
        raise ValueError(f"No existe la cuenta activa {code}.")
    return row[0]


def ensure_accounts(cursor):
    cursor.execute("UPDATE cuentas_contables SET activo=1, acepta_movimiento=1 WHERE codigo='2104'")
    row = cursor.execute("SELECT id FROM cuentas_contables WHERE codigo='2105'").fetchone()
    if not row:
        cursor.execute("""
            INSERT INTO cuentas_contables(codigo,cuenta,tipo,grupo,naturaleza,acepta_movimiento,requiere_activo,requiere_centro_costo,activo)
            VALUES('2105','Otras Cuentas por Pagar','Pasivo','Pasivo Corriente','Acreedora',1,0,0,1)
        """)


def execute(apply=False):
    connection = sqlite3.connect(DB)
    connection.row_factory = sqlite3.Row
    cursor = connection.cursor()
    try:
        if cursor.execute("SELECT 1 FROM asientos_contables WHERE descripcion=?", (DESCRIPTION,)).fetchone():
            raise ValueError("Esta reclasificación ya fue registrada.")
        cxp_id = cursor.execute("SELECT id FROM cuentas_contables WHERE codigo='2101'").fetchone()[0]
        current_cxp = round(cursor.execute(
            "SELECT COALESCE(SUM(haber-debe),0) FROM partidas WHERE cuenta_id=?", (cxp_id,)
        ).fetchone()[0], 2)
        if abs(current_cxp - EXPECTED_CXP) > 0.01:
            raise ValueError(f"CxP actual L {current_cxp:,.2f}; se esperaba L {EXPECTED_CXP:,.2f}.")
        other_total = round(sum(amount for _, amount in OTRAS_CXP), 2)
        loan_total = round(sum(amount for _, amount in LOANS), 2)
        retained = round(EXPECTED_CXP - SOCIOS - other_total - loan_total, 2)
        print(f"CxP técnica a reclasificar: L {EXPECTED_CXP:,.2f}")
        print(f"CxP Socios: L {SOCIOS:,.2f}")
        print(f"Otras CxP: L {other_total:,.2f}")
        print(f"Préstamos: L {loan_total:,.2f}")
        print(f"Resultados acumulados: L {retained:,.2f}")
        if not apply:
            print("Revisión finalizada. Use --apply para registrar el asiento.")
            return

        cursor.execute("BEGIN")
        ensure_accounts(cursor)
        accounts = {code: account_id(cursor, code) for code in ("2101", "2104", "2105", "2201", "3103")}
        header = cursor.execute("""
            INSERT INTO asientos_contables(fecha,descripcion,referencia_tipo,referencia_id)
            VALUES(?,?,?,?)
        """, (DATE, DESCRIPTION, "reclasificacion_historica", 0))
        header_id = header.lastrowid

        lines = [(accounts["2101"], EXPECTED_CXP, 0, "Depuración de CxP técnica migrada")]
        lines.append((accounts["2104"], 0, SOCIOS, "CxP Socios · Exclusión de capital"))
        lines.extend((accounts["2105"], 0, amount, f"Otras CxP · {name}") for name, amount in OTRAS_CXP)
        lines.extend((accounts["2201"], 0, amount, f"Préstamo bancario · {bank}") for bank, amount in LOANS)
        lines.append((accounts["3103"], 0, retained, "Ajuste histórico a resultados acumulados"))
        if abs(sum(line[1] for line in lines) - sum(line[2] for line in lines)) > 0.01:
            raise ValueError("El asiento de reclasificación no cuadra.")
        for account, debit, credit, detail in lines:
            cursor.execute("""
                INSERT INTO partidas(fecha,descripcion,referencia_tipo,referencia_id,cuenta_id,debe,haber,asiento_id)
                VALUES(?,?,?,?,?,?,?,?)
            """, (DATE, f"{DESCRIPTION} · {detail}", "reclasificacion_historica", header_id,
                  account, debit, credit, header_id))
        connection.commit()
        print("Reclasificación aplicada correctamente.")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    execute("--apply" in sys.argv)
