"""Corrige adquisiciones históricas migradas como anticipos contra Bancos.

Estas compras fueron importadas como contado, aunque corresponden a saldos por
pagar a proveedores. La corrección conserva los débitos de inventario y cambia
solamente las partidas acreedoras de Bancos a CxP; además, deja el auxiliar de
CxP completo por cada adquisición.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "autolote.sqlite"
SOURCES = ("migracion_adquisicion_lote4", "migracion_adquisicion_lote4_conciliada")


def account_id(cursor, code):
    row = cursor.execute(
        "SELECT id FROM cuentas_contables WHERE codigo=? AND activo=1 AND acepta_movimiento=1",
        (code,),
    ).fetchone()
    if not row:
        raise ValueError(f"No existe la cuenta activa {code}.")
    return row[0]


def source_acquisitions(cursor):
    marks = ",".join("?" for _ in SOURCES)
    return cursor.execute(f"""
        SELECT DISTINCT a.id, a.proveedor_id, a.fecha, a.costo_compra,
               a.anticipo, a.saldo, a.observaciones
        FROM adquisiciones a
        JOIN partidas p ON p.referencia_id=a.id
        WHERE p.referencia_tipo IN ({marks})
        ORDER BY a.id
    """, SOURCES).fetchall()


def inspect(cursor, bank_id):
    acquisitions = source_acquisitions(cursor)
    marks = ",".join("?" for _ in SOURCES)
    bank_lines = cursor.execute(f"""
        SELECT p.id, p.referencia_id, p.haber
        FROM partidas p
        WHERE p.referencia_tipo IN ({marks})
          AND p.cuenta_id=? AND p.haber>0
        ORDER BY p.id
    """, (*SOURCES, bank_id)).fetchall()
    acquisition_ids = [row["id"] for row in acquisitions]
    if not acquisition_ids:
        raise ValueError("No se encontraron adquisiciones migradas para corregir.")
    id_marks = ",".join("?" for _ in acquisition_ids)
    auxiliaries = cursor.execute(f"""
        SELECT * FROM cuentas_por_pagar
        WHERE adquisicion_id IN ({id_marks})
        ORDER BY adquisicion_id, id
    """, acquisition_ids).fetchall()
    payments = cursor.execute(f"""
        SELECT COUNT(*)
        FROM pagos_cuentas_por_pagar pc
        JOIN cuentas_por_pagar cp ON cp.id=pc.cuenta_por_pagar_id
        WHERE cp.adquisicion_id IN ({id_marks})
    """, acquisition_ids).fetchone()[0]
    auxiliary_by_acquisition = {}
    for row in auxiliaries:
        if row["adquisicion_id"] in auxiliary_by_acquisition:
            raise ValueError(f"La adquisición {row['adquisicion_id']} tiene auxiliares de CxP duplicados.")
        auxiliary_by_acquisition[row["adquisicion_id"]] = row
    return acquisitions, bank_lines, auxiliary_by_acquisition, payments


def migrate(apply=False):
    connection = sqlite3.connect(DB)
    connection.row_factory = sqlite3.Row
    cursor = connection.cursor()
    try:
        bank_id = account_id(cursor, "1101")
        payable_id = account_id(cursor, "2101")
        acquisitions, bank_lines, auxiliaries, payments = inspect(cursor, bank_id)
        total_cost = round(sum(row["costo_compra"] for row in acquisitions), 2)
        total_advance = round(sum(row["anticipo"] for row in acquisitions), 2)
        reclassification = round(sum(row["haber"] for row in bank_lines), 2)
        existing = len(auxiliaries)
        new = len(acquisitions) - existing

        if payments:
            raise ValueError("Hay pagos aplicados a estas CxP; no es seguro sobrescribir los auxiliares.")
        if len(bank_lines) != 45 or abs(reclassification - 8369399.85) > 0.01:
            raise ValueError(
                f"El alcance de la corrección cambió: {len(bank_lines)} partidas por L {reclassification:,.2f}."
            )
        if abs(total_advance - reclassification) > 0.01:
            raise ValueError("Los anticipos de adquisiciones no coinciden con las partidas de Bancos.")

        print(f"Adquisiciones afectadas: {len(acquisitions)}")
        print(f"Costo total que quedará en CxP: L {total_cost:,.2f}")
        print(f"Anticipos a eliminar: L {total_advance:,.2f}")
        print(f"Partidas Bancos → CxP: {len(bank_lines)} por L {reclassification:,.2f}")
        print(f"Auxiliares CxP existentes a ajustar: {existing}")
        print(f"Auxiliares CxP nuevos: {new}")
        if not apply:
            print("Revisión finalizada. Use --apply para ejecutar la corrección.")
            return

        cursor.execute("BEGIN")
        for acquisition in acquisitions:
            note = "Corrección de migración: compra histórica reclasificada de anticipo a crédito con proveedor."
            original_note = (acquisition["observaciones"] or "").strip()
            observations = f"{original_note}\n{note}".strip() if note not in original_note else original_note
            cursor.execute("""
                UPDATE adquisiciones
                SET anticipo=0, saldo=?, metodo_pago='Crédito', condicion_pago='Crédito', observaciones=?
                WHERE id=?
            """, (acquisition["costo_compra"], observations, acquisition["id"]))
            auxiliary = auxiliaries.get(acquisition["id"])
            if auxiliary:
                cursor.execute("""
                    UPDATE cuentas_por_pagar
                    SET proveedor_id=?, fecha=?, monto_original=?, saldo=?, estado='Pendiente'
                    WHERE id=?
                """, (acquisition["proveedor_id"], acquisition["fecha"], acquisition["costo_compra"],
                      acquisition["costo_compra"], auxiliary["id"]))
            else:
                cursor.execute("""
                    INSERT INTO cuentas_por_pagar(adquisicion_id, proveedor_id, fecha, monto_original, saldo, estado)
                    VALUES(?,?,?,?,?, 'Pendiente')
                """, (acquisition["id"], acquisition["proveedor_id"], acquisition["fecha"],
                      acquisition["costo_compra"], acquisition["costo_compra"]))

        marks = ",".join("?" for _ in SOURCES)
        cursor.execute(f"""
            UPDATE partidas
            SET cuenta_id=?
            WHERE referencia_tipo IN ({marks}) AND cuenta_id=? AND haber>0
        """, (payable_id, *SOURCES, bank_id))
        if cursor.rowcount != len(bank_lines):
            raise ValueError("No se actualizaron todas las partidas de Bancos esperadas.")
        connection.commit()
        print("Corrección aplicada correctamente.")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    migrate("--apply" in sys.argv)
