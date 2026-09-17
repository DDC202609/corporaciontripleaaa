#!/usr/bin/env python3
"""Carga el lote histórico nacionalizado directamente a inventario DPV."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import server

FECHA = "2026-05-25"
REGISTROS = [
    ("ZAM57RTAXH1224159", "JDL4686", 281000, "Copart"),
    ("1FTFW1E81MFC24927", "JDO2751", 533100.83, "Copart"),
    ("3TMCZ5AN5KM231017", "JDM3477", 469000, "Importaciones Sierra"),
    ("5FNRL38208B081942", "JD02721", 113000, "Importaciones Sierra"),
    ("1C4BJWFGXGL154057", "JDO2750", 286436.14, "Copart"),
    ("1FTEW1EG6GKF68378", "JDP3698", 324000, "Importaciones Sierra"),
    ("1N6AD0EV1GN756993", "JDO5075", 211958, "Copart"),
    ("5UXWX9C50G0D79773", "JDO6491", 168000, "Importaciones Sierra"),
    ("1FMJK2AT1KEA34484", "JDO8272", 361000, "Copart"),
    ("1C4BJWEG4FL651377", "", 225304, "Copart"),
    ("1FMSK8BH0LGC24736", "JDO9258", 300000, "DON RICARDO TGA"),
    ("1FTEW1EG4FFC71603", "JDP1674", 306000, "Importaciones Sierra"),
    ("5XYPGDA36KG515687", "JDP3679", 195000, "Importaciones Sierra"),
    ("JHMGK5H73GX007628", "JDP1676", 180000, "Importaciones Sierra"),
    ("5N1AT2MV4GC737588", "JDP2712", 146000, "Importaciones Sierra"),
    ("5NPD84LF4HH065210", "JD08636", 169380, "Luis Alonso Vasquez Murillo-COPART"),
    ("5J6RM3H39DL000380", "JDO8635", 192204, "Luis Alonso Vasquez Murillo-COPART"),
    ("JTMWFREV1HJ146737", "JDO9131", 286125, "Luis Alonso Vasquez Murillo-COPART"),
    ("3TMLU4ENXFM165587", "JDP0826", 336000, "Importaciones Sierra"),
    ("5N1AT2MK8FC791822", "JDP0786", 124000, "Importaciones Sierra"),
    ("5UXKR2C59H0U20352", "JDP3675", 396000, "Importaciones Sierra"),
    ("1FTER4FH3LLA95916", "JDP1658", 360000, "Importaciones Sierra"),
    ("1N6AD07W69C409535", "JDP4795", 169000, "Importaciones Sierra"),
    ("JHLRE3H39AC009873", "JDP6601", 125000, "Copart"),
    ("1FTEW1EP9MKD43481", "JDP2714", 404000, "Importaciones Sierra"),
    ("5N1AT2MV8GC767676", "JDQ0133", 142985, "Moises Fajardo - Copart"),
    ("5J6RM4H72EL096511", "JDQ0134", 239763, "Moises Fajardo - Copart"),
    ("1N6AD0EVXKN766768", "JDO7006", 286000, "Importaciones Sierra"),
    ("19XFB2F75FE248328", "JDP3676", 155000, "Importaciones Sierra"),
]


def cuenta_id(conexion, codigo):
    row = conexion.execute("SELECT id FROM cuentas_contables WHERE codigo=? AND activo=1", (codigo,)).fetchone()
    if not row:
        raise ValueError(f"No existe la cuenta activa {codigo}.")
    return row["id"]


def proveedor_id(conexion, nombre):
    row = conexion.execute("SELECT id FROM proveedores WHERE nombre=?", (nombre,)).fetchone()
    if not row:
        raise ValueError(f"No existe el proveedor {nombre}.")
    return row["id"]


def main():
    if len(REGISTROS) != 29:
        raise ValueError("El lote debe contener 29 vehículos.")
    server.init_db()
    conexion = server.db()
    try:
        inventario_dpv = cuenta_id(conexion, "1103")
        cxp = cuenta_id(conexion, "2101")
        total = 0.0
        conexion.execute("BEGIN")
        for vin, placa, costo, proveedor_nombre in REGISTROS:
            vehiculo = conexion.execute("SELECT * FROM vehiculos WHERE vin=?", (vin,)).fetchone()
            if not vehiculo:
                raise ValueError(f"No existe el vehículo maestro {vin}.")
            if conexion.execute("SELECT 1 FROM adquisiciones WHERE vehiculo_id=?", (vehiculo["id"],)).fetchone():
                raise ValueError(f"El VIN {vin} ya tiene una adquisición registrada.")
            proveedor = proveedor_id(conexion, proveedor_nombre)
            placa = placa.strip().upper()
            if placa and not server.placa_disponible(conexion, placa, vehiculo["id"]):
                raise ValueError(f"La placa {placa} ya está registrada en otro vehículo.")
            nota = "Migración histórica. Importación nacionalizada y disponible para venta. Contrapartida: CxP proveedores."
            cur = conexion.execute(
                """INSERT INTO adquisiciones
                (vehiculo_id,proveedor_id,fecha,costo_compra,anticipo,metodo_pago,
                 condicion_pago,dias_credito,saldo,observaciones,necesita_reparacion,
                 tipo_compra,placa_cambio,banco)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (vehiculo["id"], proveedor, FECHA, costo, 0, "Efectivo", "Contado", 0,
                 0, nota, 0, "Importación", None, None),
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
                fecha_adquisicion=?,tipo_compra='Importación',estado='DPV' WHERE id=?""",
                (placa or None, costo, proveedor_nombre, FECHA, vehiculo["id"]),
            )
            server.add_movimiento(
                conexion, vehiculo["id"], FECHA, "Adquisición histórica DPV",
                vehiculo["estado"], "DPV", vehiculo["ubicacion"], vehiculo["ubicacion"],
                referencia="CxP proveedores",
                observaciones=f"Importación nacionalizada. Costo: {costo:.2f}. Disponible para venta.",
            )
            server.registrar_asiento(
                conexion, FECHA, f"Adquisición histórica DPV · {vin} · CxP proveedores",
                "migracion_adquisicion_dpv", cur.lastrowid,
                [{"cuenta_id": inventario_dpv, "debe": costo}, {"cuenta_id": cxp, "haber": costo}],
                vehiculo["id"],
            )
            total += costo
        conexion.commit()
    except Exception:
        conexion.rollback()
        raise
    finally:
        conexion.close()
    print(f"Adquisiciones DPV cargadas: {len(REGISTROS)}")
    print(f"Total: {total:,.2f}")


if __name__ == "__main__":
    main()
