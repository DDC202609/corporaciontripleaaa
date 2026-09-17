#!/usr/bin/env python3
"""Carga el tercer lote histórico como inventario en tránsito nacionalizado."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import server


# VIN, placa, adquisición, proveedor, tipo de compra, fecha (AAAA-MM-DD)
REGISTROS = [
    ("5N1BT3AB6PC678533", "JDP6313", 323000.00, "Importaciones Sierra", "Importación", "2026-05-25"),
    ("1G1YY3188K5110391", "", 124000.00, "Importaciones Sierra", "Importación", "2026-05-25"),
    ("1FA6P8TH6G5244681", "", 233000.00, "Importaciones Sierra", "Importación", "2026-06-25"),
    ("5J6RM4H39GL013060", "JDV2602", 261000.00, "Importaciones Sierra", "Importación", "2026-07-02"),
    ("1C4BJWDG4FL597578", "HDY2569", 277742.00, "INVERSA", "Compra Local", "2026-03-27"),
    ("5TELU4EN8AZ735890", "", 236000.00, "Importaciones Sierra", "Importación", "2026-07-03"),
    ("3TYDZ5BN5MT002976", "", 558000.00, "Importaciones Sierra", "Importación", "2026-07-03"),
    ("2HKRM3H54CH505107", "HCW6422", 180000.00, "Karen Yolani Mejía Amaya - JAG9510", "Cambio", "2026-06-29"),
    ("1FMEU73E47UB36303", "HBV6593", 37353.00, "INVERSA", "Compra Local", "2026-03-27"),
    ("5N1AT2MVXGC783233", "HBM4227", 190000.00, "Cambio - SI", "Cambio", "2026-05-22"),
    ("5YFBURHE8EP005140", "HAY1607", 165000.00, "Cambio - SI", "Cambio", "2026-07-15"),
    ("1FMSK8DH8LGB49233", "JDV0922", 309000.00, "Jeovanny Francisco Mendez Montenegro - Copart", "Importación", "2026-07-17"),
    ("1FMCU0D73BKB19544", "HAD4959", 105000.00, "Cambio - SI", "Cambio", "2026-07-21"),
    ("MHKA4DF500J000506", "HAT6458", 149000.00, "Cambio - SI", "Cambio", "2026-07-21"),
    ("5YFBURHEXHP713488", "JDP9222", 173568.15, "Luis Alonso Vasquez Murillo-COPART", "Importación", "2026-04-14"),
    ("7FARW1H80HE011099", "JAD4881", 325000.00, "KENEX ALEXANDER VARELA FLORES", "Compra Local", "2026-07-22"),
    ("1FMSK7DH1LGB52341", "JAG6888", 450000.00, "Proveedor Local-SI", "Compra Local", "2026-07-28"),
    ("5TBRT54147S456968", "HBC8704", 70000.00, "Cambio - SI", "Cambio", "2026-07-28"),
    ("1FMCU0F73HUD04364", "HDY4949", 120926.00, "Proveedor Local-SI", "Compra Local", "2026-07-13"),
    ("1FTMF1E86GKF87923", "", 207000.00, "Importaciones Sierra", "Importación", "2026-08-04"),
    ("1FMSK7DHXNGB38358", "JAY7655", 495000.00, "Proveedor Local-SI", "Compra Local", "2026-07-31"),
    ("1FTMF1E8XHFA62504", "", 213000.00, "Importaciones Sierra", "Importación", "2026-07-26"),
    ("5NPDH4AEXCH090180", "HAF273", 80000.00, "Cesar Augusto Torres Vasquez", "Compra Local", "2026-08-04"),
    ("JTEBU11FX70026150", "HDI4412", 285000.00, "Marco Antonio Palacios", "Compra Local", "2026-07-22"),
    ("4A4AP4AU9EE019271", "HBL9956", 125000.00, "Proveedor Local-SI", "Compra Local", "2026-07-18"),
    ("7FARS5H59RE003285", "JDS6473", 493000.00, "Inversiones Varela Chavarria", "Importación", "2026-07-28"),
    ("1FMCU0G93HUD67108", "HBF1514", 130000.00, "PRESTADITO", "Compra Local", "2026-08-12"),
    ("1GCVKRECXJZ149794", "JAY5900", 440000.00, "PRESTA YÀ", "Compra Local", "2025-11-08"),
    ("1FMCU0GX1EUA78383", "HBF9837", 50000.00, "Cambio - SI", "Cambio", "2026-08-12"),
    ("KMHJU81VBBU207008", "JAD0515", 140000.00, "Cambio - SI", "Cambio", "2026-08-12"),
    ("1NXBU4EE5AZ271179", "JAR5599", 103350.00, "Andrei Marcell Raudales Kmitta", "Compra Local", "2026-07-20"),
    ("5N1BT3AAXPC789027", "", 120000.00, "Importaciones Sierra", "Importación", "2026-08-13"),
    ("5TFNA5AB7PX025348", "JDH2459", 1100000.00, "MANUEL GARRIDO CARCAMO", "Cambio", "2026-08-14"),
    ("2T1BR32EX5C432035", "", 120000.00, "Copart", "Importación", "2026-08-20"),
    ("5NPDH4AE3DH180790", "JAL8125", 110000.00, "Andrei Marcell Raudales Kmitta", "Compra Local", "2026-08-28"),
    ("1FM5K7F85DGB81479", "JAL6480", 150000.00, "Andrei Marcell Raudales Kmitta", "Compra Local", "2026-08-28"),
    ("KNAB2512BKT366595", "HAU0199", 85000.00, "DAVID JOSUE VELASQUEZ FIGUEROA", "Compra Local", "2026-09-03"),
    ("5N1AT2MVXEC849485", "JDV0923", 115000.00, "Jeovanny Francisco Mendez Montenegro - Copart", "Importación", "2026-09-03"),
    ("JTMBF4DV8C5054366", "PQG2395", 165000.00, "MARVIN LENIN HERNANDEZ FUENTES", "Cambio", "2026-09-04"),
    ("5TFTX4CNXDX034466", "HAF5557", 180000.00, "DAPHNNE JUDITH URBINA ROMERO", "Cambio", "2026-09-09"),
    ("5NPDH4AE2GH723910", "JAM7168", 68858.00, "ILIANA JAQUELINE MEJIA MACHADO", "Compra Local", "2026-09-10"),
]


def cuenta_id(conexion, codigo):
    row = conexion.execute(
        "SELECT id FROM cuentas_contables WHERE codigo=? AND activo=1", (codigo,)
    ).fetchone()
    if not row:
        raise ValueError(f"No existe la cuenta activa {codigo}.")
    return row["id"]


def proveedor_id(conexion, nombre):
    row = conexion.execute("SELECT id FROM proveedores WHERE nombre=?", (nombre,)).fetchone()
    if not row:
        raise ValueError(f"No existe el proveedor {nombre}.")
    return row["id"]


def main():
    if len(REGISTROS) != 41:
        raise ValueError(f"El lote debe contener 41 vehículos; contiene {len(REGISTROS)}.")
    server.init_db()
    conexion = server.db()
    try:
        inventario_transito = cuenta_id(conexion, "1105")
        cxp = cuenta_id(conexion, "2101")
        total = 0.0
        conexion.execute("BEGIN")
        for vin, placa, costo, proveedor_nombre, tipo_compra, fecha in REGISTROS:
            vehiculo = conexion.execute("SELECT * FROM vehiculos WHERE vin=?", (vin,)).fetchone()
            if not vehiculo:
                raise ValueError(f"No existe el vehículo maestro {vin}.")
            if conexion.execute("SELECT 1 FROM adquisiciones WHERE vehiculo_id=?", (vehiculo["id"],)).fetchone():
                raise ValueError(f"El VIN {vin} ya tiene una adquisición registrada.")
            proveedor = proveedor_id(conexion, proveedor_nombre)
            placa = placa.strip().upper()
            if placa and not server.placa_disponible(conexion, placa, vehiculo["id"]):
                raise ValueError(f"La placa {placa} ya está registrada en otro vehículo.")
            nota = "Migración histórica. Vehículo nacionalizado en tránsito. Contrapartida: CxP proveedores."
            cur = conexion.execute(
                """INSERT INTO adquisiciones
                (vehiculo_id,proveedor_id,fecha,costo_compra,anticipo,metodo_pago,
                 condicion_pago,dias_credito,saldo,observaciones,necesita_reparacion,
                 tipo_compra,placa_cambio,banco)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (vehiculo["id"], proveedor, fecha, costo, 0, "Efectivo", "Contado", 0,
                 0, nota, 0, tipo_compra, None, None),
            )
            conexion.execute(
                """INSERT INTO costos_adquisicion
                (vehiculo_id,costo_exw,costo_estimado,estatus,placa_nacionalizacion,fecha_actualizacion)
                VALUES(?,?,?,'Nacionalizado',?,CURRENT_TIMESTAMP)
                ON CONFLICT(vehiculo_id) DO UPDATE SET
                    costo_exw=excluded.costo_exw,costo_estimado=excluded.costo_estimado,
                    estatus='Nacionalizado',placa_nacionalizacion=excluded.placa_nacionalizacion,
                    fecha_actualizacion=CURRENT_TIMESTAMP""",
                (vehiculo["id"], costo, costo, placa or None),
            )
            conexion.execute(
                """UPDATE vehiculos SET placa=COALESCE(?,placa),precio_compra=?,proveedor=?,
                fecha_adquisicion=?,tipo_compra=?,estado='En Tránsito' WHERE id=?""",
                (placa or None, costo, proveedor_nombre, fecha, tipo_compra, vehiculo["id"]),
            )
            server.add_movimiento(
                conexion, vehiculo["id"], fecha, "Adquisición histórica en tránsito",
                vehiculo["estado"], "En Tránsito", vehiculo["ubicacion"], vehiculo["ubicacion"],
                referencia="CxP proveedores",
                observaciones=f"Vehículo nacionalizado en tránsito. Costo: {costo:.2f}.",
            )
            server.registrar_asiento(
                conexion, fecha, f"Adquisición histórica en tránsito · {vin} · CxP proveedores",
                "migracion_adquisicion_transito_lote3", cur.lastrowid,
                [{"cuenta_id": inventario_transito, "debe": costo}, {"cuenta_id": cxp, "haber": costo}],
                vehiculo["id"],
            )
            total += costo
        conexion.commit()
    except Exception:
        conexion.rollback()
        raise
    finally:
        conexion.close()
    print(f"Adquisiciones históricas cargadas: {len(REGISTROS)}")
    print(f"Total: {total:,.2f}")


if __name__ == "__main__":
    main()
