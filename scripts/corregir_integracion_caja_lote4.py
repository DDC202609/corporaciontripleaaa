"""Elimina movimientos de caja que quedaron de anticipos reclasificados a CxP."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "autolote.sqlite"
REFERENCE_TYPE = "migracion_lote4_anticipo"
EXPECTED_COUNT = 45
EXPECTED_TOTAL = 8_369_399.85


def run(apply=False):
    connection = sqlite3.connect(DB)
    cursor = connection.cursor()
    try:
        count, total = cursor.execute("""
            SELECT COUNT(*), COALESCE(SUM(salida),0)
            FROM movimientos_caja WHERE referencia_tipo=?
        """, (REFERENCE_TYPE,)).fetchone()
        total = round(total, 2)
        if count != EXPECTED_COUNT or abs(total - EXPECTED_TOTAL) > 0.01:
            raise ValueError(f"Alcance inesperado: {count} movimientos por L {total:,.2f}.")
        print(f"Movimientos de caja a retirar: {count}")
        print(f"Saldo a corregir: L {total:,.2f}")
        if not apply:
            print("Revisión finalizada. Use --apply para corregir la integración.")
            return
        cursor.execute("BEGIN")
        cursor.execute("DELETE FROM movimientos_caja WHERE referencia_tipo=?", (REFERENCE_TYPE,))
        if cursor.rowcount != EXPECTED_COUNT:
            raise ValueError("No se eliminaron todos los movimientos esperados.")
        connection.commit()
        print("Integración de caja corregida.")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    run("--apply" in sys.argv)
