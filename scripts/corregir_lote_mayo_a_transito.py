#!/usr/bin/env python3
"""Reclasifica el lote histórico del 25-may-2026 de DPV a En Tránsito.

La nacionalización se conserva confirmada porque estos vehículos podrán recibir
posteriormente costos de reparación. Solo cambia la etapa operativa y la cuenta
de inventario asociada a los asientos históricos del lote.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import server

PREFIJO = "Migración histórica. Importación nacionalizada y disponible para venta.%"
FECHA_CORRECCION = "2026-09-15"


def cuenta_id(conexion, codigo):
    row = conexion.execute(
        "SELECT id FROM cuentas_contables WHERE codigo=? AND activo=1", (codigo,)
    ).fetchone()
    if not row:
        raise ValueError(f"No existe la cuenta activa {codigo}.")
    return row["id"]


def main():
    server.init_db()
    conexion = server.db()
    try:
        lote = conexion.execute(
            """SELECT a.id AS adquisicion_id, a.vehiculo_id, v.vin, v.estado
               FROM adquisiciones a
               JOIN vehiculos v ON v.id=a.vehiculo_id
               WHERE a.observaciones LIKE ?
               ORDER BY a.id""",
            (PREFIJO,),
        ).fetchall()
        if len(lote) != 29:
            raise ValueError(f"Se esperaban 29 adquisiciones del lote; se encontraron {len(lote)}.")

        inventario_dpv = cuenta_id(conexion, "1103")
        inventario_transito = cuenta_id(conexion, "1105")
        ids_adquisicion = [row["adquisicion_id"] for row in lote]
        ids_vehiculo = [row["vehiculo_id"] for row in lote]
        marcadores = ",".join("?" for _ in ids_adquisicion)
        marcadores_vehiculos = ",".join("?" for _ in ids_vehiculo)

        partidas = conexion.execute(
            f"""SELECT COUNT(*) AS cantidad, COALESCE(SUM(p.debe),0) AS total
                FROM partidas p
                JOIN asientos_contables ac ON ac.id=p.asiento_id
                WHERE ac.referencia_tipo='migracion_adquisicion_dpv'
                  AND ac.referencia_id IN ({marcadores})
                  AND p.cuenta_id=?""",
            (*ids_adquisicion, inventario_dpv),
        ).fetchone()
        if partidas["cantidad"] != 29:
            raise ValueError(
                f"Se esperaban 29 partidas en inventario DPV; se encontraron {partidas['cantidad']}."
            )

        conexion.execute("BEGIN")
        conexion.execute(
            f"UPDATE vehiculos SET estado='En Tránsito' WHERE id IN ({marcadores_vehiculos})",
            ids_vehiculo,
        )
        # Se conserva estatus Nacionalizado: solo se reclasifica la disponibilidad operativa.
        conexion.execute(
            f"""UPDATE adquisiciones
                SET observaciones='Migración histórica. Importación nacionalizada en tránsito. Contrapartida: CxP proveedores.'
                WHERE id IN ({marcadores})""",
            ids_adquisicion,
        )
        conexion.execute(
            f"""UPDATE partidas SET cuenta_id=?
                WHERE cuenta_id=? AND asiento_id IN (
                    SELECT id FROM asientos_contables
                    WHERE referencia_tipo='migracion_adquisicion_dpv'
                      AND referencia_id IN ({marcadores})
                )""",
            (inventario_transito, inventario_dpv, *ids_adquisicion),
        )
        conexion.execute(
            f"""UPDATE asientos_contables
                SET descripcion=REPLACE(descripcion, 'Adquisición histórica DPV', 'Adquisición histórica en tránsito'),
                    referencia_tipo='migracion_adquisicion_transito'
                WHERE referencia_tipo='migracion_adquisicion_dpv'
                  AND referencia_id IN ({marcadores})""",
            ids_adquisicion,
        )
        for row in lote:
            server.add_movimiento(
                conexion,
                row["vehiculo_id"],
                FECHA_CORRECCION,
                "Corrección de etapa histórica",
                row["estado"],
                "En Tránsito",
                None,
                None,
                referencia="Reclasificación de inventario",
                observaciones="Se conserva nacionalización; inventario reclasificado de DPV a En Tránsito.",
            )
        conexion.commit()
        print(f"Vehículos reclasificados: {len(lote)}")
        print(f"Inventario reclasificado a tránsito: {float(partidas['total']):,.2f}")
    except Exception:
        conexion.rollback()
        raise
    finally:
        conexion.close()


if __name__ == "__main__":
    main()
