#!/usr/bin/env python3
"""Alinea el mayor de inventario con el Kardex operativo, sin tocar vehículos.

La migración histórica conservó compras, costos y estados operativos, pero no
siempre sus reclasificaciones contables entre Tránsito, Taller y DPV. Este
asiento único deja cada cuenta de inventario igual al costo consolidado de las
unidades que hoy pertenecen a su etapa. La diferencia histórica se lleva a
Resultados acumulados, nunca a ventas, compras, cobros o CxP/CxC.
"""
import argparse
import shutil
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server  # noqa: E402

REFERENCE_TYPE = "conciliacion_inventario_historico"
REFERENCE_ID = 20260915
AS_OF = "2026-09-15"
INVENTORY_BY_STATE = {
    "DPV": "inventario_dpv",
    "En Taller": "inventario_taller",
    "En Tránsito": "inventario_transito",
}


def reconciliation(c):
    keys = tuple(INVENTORY_BY_STATE.values())
    account_ids = {key: server.cuenta_contable_id(c, key) for key in keys}
    actual = {}
    for key, account_id in account_ids.items():
        row = c.execute("SELECT COALESCE(SUM(debe-haber),0) saldo FROM partidas WHERE cuenta_id=?", (account_id,)).fetchone()
        actual[key] = round(float(row["saldo"] or 0), 2)
    expected = {key: 0.0 for key in keys}
    quantities = {key: 0 for key in keys}
    for vehicle in c.execute("SELECT id,estado FROM vehiculos WHERE estado IN ('DPV','En Taller','En Tránsito')"):
        key = INVENTORY_BY_STATE[vehicle["estado"]]
        expected[key] += server.costo_consolidado(c, vehicle["id"])
        quantities[key] += 1
    expected = {key: round(value, 2) for key, value in expected.items()}
    deltas = {key: round(expected[key] - actual[key], 2) for key in keys}
    return account_ids, actual, expected, deltas, quantities


def main(apply=False):
    c = server.db()
    try:
        account_ids, actual, expected, deltas, quantities = reconciliation(c)
        print("Conciliación al", AS_OF)
        for key in INVENTORY_BY_STATE.values():
            print(f"{key}: operativo={expected[key]:,.2f}; mayor={actual[key]:,.2f}; ajuste={deltas[key]:,.2f}; unidades={quantities[key]}")
        net = round(sum(deltas.values()), 2)
        print(f"Contrapartida Resultados acumulados (3103): {net:,.2f}")
        exists = c.execute("SELECT id FROM asientos_contables WHERE referencia_tipo=? AND referencia_id=?", (REFERENCE_TYPE, REFERENCE_ID)).fetchone()
        if exists:
            raise ValueError("La conciliación histórica ya fue aplicada; no se duplicó el asiento.")
        if not apply:
            print("Simulación correcta. Use --apply para crear el asiento.")
            return
        backup = ROOT / "data" / "autolote-antes-conciliacion-inventario-20260915.sqlite"
        if not backup.exists():
            shutil.copy2(ROOT / "data" / "autolote.sqlite", backup)
        lines = []
        for key, delta in deltas.items():
            if delta > 0:
                lines.append({"cuenta_id": account_ids[key], "debe": delta})
            elif delta < 0:
                lines.append({"cuenta_id": account_ids[key], "haber": -delta})
        retained = c.execute("SELECT id FROM cuentas_contables WHERE codigo='3103' AND activo=1").fetchone()
        if not retained:
            raise ValueError("No existe la cuenta 3103 Resultados Acumulados")
        if net > 0:
            lines.append({"cuenta_id": retained["id"], "haber": net})
        elif net < 0:
            lines.append({"cuenta_id": retained["id"], "debe": -net})
        c.execute("BEGIN")
        server.registrar_asiento(c, AS_OF,
            "Conciliación histórica de inventario: Tránsito, Taller y DPV contra Kardex operativo",
            REFERENCE_TYPE, REFERENCE_ID, lines)
        c.commit()
        print("Asiento de conciliación creado.")
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    main(parser.parse_args().apply)
