#!/usr/bin/env python3
"""Restaura las 19 compras que fueron modificadas al cargar el lote DPV de 63.

La fuente es el respaldo tomado antes de esa carga. No toca las 44 compras
nuevas que sí pertenecen al lote consolidado.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATABASE = ROOT / "data" / "autolote.sqlite"
SOURCE = ROOT / "data" / "autolote-antes-reversion-a-4-8m.sqlite"
SAFETY_COPY = ROOT / "data" / "autolote-antes-restaurar-19-preexistentes.sqlite"

VINS = (
    "WA1LYAFE9AD001716", "5UXWX7C56E0E77370", "WBA3A5C54EP601258",
    "1GCGTCEN0J1259837", "1GCHSBEA8N1187497", "1D7RB1GP5AS165175",
    "3C6RR7LT5HG580145", "1FM5K7F80GGA09347", "1FMCU0F66LUA55140",
    "1FTER4FH4LLA06760", "1FM5K7D8XKGB07149", "1FTFW1E89PFA46849",
    "1FM5K8GT9HGB90880", "1FMCU0EG9BK77804", "1FMCU0H95DUB37532",
    "1FTEW1EP6HKC28116", "1FMCU0H68LUA23206", "1FM5K7D81JGA52105",
    "1FMCU0C70CKB74908",
)


def row_by_vin(connection: sqlite3.Connection, vin: str):
    return connection.execute(
        """SELECT v.*, a.id AS adquisicion_id
           FROM vehiculos v JOIN adquisiciones a ON a.vehiculo_id = v.id
           WHERE v.vin = ?""",
        (vin,),
    ).fetchone()


def copy_columns(destination, source, table: str, where_column: str, where_value: int, excluded=(), delete_if_missing=False):
    columns = [r[1] for r in source.execute(f"PRAGMA table_info({table})")]
    columns = [c for c in columns if c not in {"id", *excluded}]
    source_row = source.execute(
        f"SELECT {','.join(columns)} FROM {table} WHERE {where_column}=?", (where_value,)
    ).fetchone()
    if not source_row and delete_if_missing:
        destination.execute(f"DELETE FROM {table} WHERE {where_column}=?", (where_value,))
        return
    if not source_row:
        raise ValueError(f"No existe {table} para {where_column}={where_value} en el respaldo.")
    assignments = ",".join(f"{column}=?" for column in columns)
    destination.execute(
        f"UPDATE {table} SET {assignments} WHERE {where_column}=?",
        (*source_row, where_value),
    )


def main():
    if not SOURCE.exists():
        raise FileNotFoundError(f"No existe el respaldo requerido: {SOURCE}")
    shutil.copy2(DATABASE, SAFETY_COPY)

    current = sqlite3.connect(DATABASE)
    source = sqlite3.connect(SOURCE)
    current.row_factory = sqlite3.Row
    source.row_factory = sqlite3.Row
    try:
        current.execute("BEGIN")
        restored = 0
        for vin in VINS:
            old = row_by_vin(source, vin)
            now = row_by_vin(current, vin)
            if not old or not now:
                raise ValueError(f"No se encontró la compra de {vin} en ambos archivos.")

            # Los IDs son los mismos en ambos archivos; se restaura la ficha y compra completas.
            copy_columns(current, source, "vehiculos", "id", now["id"])
            copy_columns(current, source, "adquisiciones", "id", now["adquisicion_id"])
            copy_columns(current, source, "costos_adquisicion", "vehiculo_id", now["id"], delete_if_missing=True)

            # El lote agregó indebidamente auxiliares CxP para estas compras.
            current.execute("DELETE FROM cuentas_por_pagar WHERE adquisicion_id=?", (now["adquisicion_id"],))

            # Devuelve el asiento de adquisición a sus partidas y descripción originales.
            current_headers = current.execute(
                "SELECT id FROM asientos_contables WHERE referencia_id=? AND referencia_tipo LIKE 'migracion_adquisicion%'",
                (now["adquisicion_id"],),
            ).fetchall()
            old_headers = source.execute(
                "SELECT * FROM asientos_contables WHERE referencia_id=? AND referencia_tipo LIKE 'migracion_adquisicion%'",
                (old["adquisicion_id"],),
            ).fetchall()
            if len(current_headers) != len(old_headers):
                raise ValueError(f"Asientos inesperados para {vin}; no se hizo una restauración parcial.")
            for current_header, old_header in zip(current_headers, old_headers):
                current.execute(
                    "UPDATE asientos_contables SET fecha=?,descripcion=?,referencia_tipo=?,referencia_id=?,creado_en=? WHERE id=?",
                    (old_header["fecha"], old_header["descripcion"], old_header["referencia_tipo"],
                     old_header["referencia_id"], old_header["creado_en"], current_header["id"]),
                )
                current.execute("DELETE FROM partidas WHERE asiento_id=?", (current_header["id"],))
                old_parts = source.execute(
                    """SELECT fecha,descripcion,referencia_tipo,referencia_id,cuenta_id,area_id,
                              departamento_id,vehiculo_id,debe,haber
                       FROM partidas WHERE asiento_id=? ORDER BY id""",
                    (old_header["id"],),
                ).fetchall()
                current.executemany(
                    """INSERT INTO partidas(fecha,descripcion,referencia_tipo,referencia_id,cuenta_id,area_id,
                                              departamento_id,vehiculo_id,debe,haber,asiento_id)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    [(*part, current_header["id"]) for part in old_parts],
                )

            # Borra solamente el movimiento agregado por la carga errónea del lote DPV.
            current.execute(
                """DELETE FROM movimientos_vehiculo
                   WHERE vehiculo_id=? AND tipo='Migración a DPV'
                     AND observaciones LIKE 'Costo acumulado migrado:%'""",
                (now["id"],),
            )
            restored += 1
        current.commit()
    except Exception:
        current.rollback()
        raise
    finally:
        current.close()
        source.close()
    print(f"Compras restauradas: {restored}")
    print(f"Respaldo de seguridad: {SAFETY_COPY}")


if __name__ == "__main__":
    main()
