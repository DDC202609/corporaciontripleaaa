#!/usr/bin/env python3
"""Carga los maestros autorizados entregados por Corporación Triple AAA."""
import os
import sys

ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0,ROOT)
import server

FINANCIERAS=['Cofisa','Prestadito','Presta Ya','Credi Rapid','Presta Auto','Credi Movil','Credi Motor','Maple','Easy Car','T-Presto','Presto','CrediQ','Solfisa','Aquí Cash','SkyLimited']
TALLERES=[
('Taller Neco','Pintura'),('Taller el Sarco','Pintura'),('Taller Limonar','Pintura'),('Taller Manuelito','Pintura'),('Taller Marcos','Pintura'),('Taller Alex','Pintura'),('Taller Roberto','Pintura'),('Taller Sandro','Pintura'),('Taller Marcio','Aire Acondicionado'),('Taller Danny','Mecanica'),('Taller Jorge','Mecanica'),('Taller Kevin','Shampoo'),('Taller Orange','Shampoo'),('Taller Monkey','Shampoo'),('Taller Prix','Mecanica'),('Tapicería José','Tapicería'),('Taller Carhand','Audio'),('Taller Elmer','Pintura'),('Calillantas','Llantas'),('Taller Dennis','Pintura'),('Carwash Potrillo','Shampoo'),('Polarizados Vega','Polarizados'),('EfiCar','Electromecanica'),('Taller Danny','Electromecanica'),('Mofles','Mofles'),('La Casa del Spoiler','Accesorios'),('Baterias American','Baterias'),('Veloster','Yonker'),('CCR','Yonker'),('Taller Samir','Mecanica'),('Samir Llantera','Llantas'),('La Mundial','Varios'),('Glass Depot','Repuestos'),('Inmecro','Accesorios'),('Inversiones Suyapa','Repuestos'),('Tornicentro','Repuestos'),('Taller Grandote','Mecanica'),('Taller Guamilito','Carroceria'),('Reconco`s','Repuestos'),('Taller Satelite','Mecanica'),('AUREMO','Repuestos'),('Autorepuestos REMO','Polarizados'),('Autopartes Lennon','Repuestos'),('Parrillas y Repuestos ONE','Repuestos'),('Doctor Covers','Repuestos'),('Gibson','Taller'),('Clock Spring','Repuestos'),('Taller Inicial','Mecanica'),('Autos Import','Repuestos'),('Taller Christian','Electromecanica'),('Taller Jesus','Mecanica'),('MAEGA','Repuestos'),('Cromos Dennis','Cromos'),('Taller Mike','Mecanica'),('Vidrieria Estrellita de Sula','Repuestos'),('Llantilandia','Repuestos'),('Vidrios Sephora','Repuestos'),('Taller Elias','Mecanica'),('Taller Gata seca','Aire Acondicionado'),('Bolsas de Aire','Repuestos'),('CEC Consultancy','Repuestos'),('Taller Maynor','Mecanica'),('Taller Alexander','Mecanica'),('Cerrajeria Josue','Varios'),('SPEED AUTOPARTS','Repuestos'),('Inversiones Vidal Pacheco','Vidrios'),('Elva Esther Ordoñez Sanchez','Repuestos'),('Mirian Suyapa Obando Salgado','Mecanica'),('Inversiones Aliadas','Repuestos'),('Best Tires','Mecanica')]

server.init_db(); c=server.db()
for name in FINANCIERAS:
    c.execute("INSERT OR IGNORE INTO clientes(nombre,tipo,condicion_pago,activo) VALUES(?,'Financiera','Contado',1)",(name,))
for name,specialty in TALLERES:
    c.execute("INSERT OR IGNORE INTO proveedores(nombre,tipo,condicion_pago,dias_credito,activo) VALUES(?,'Taller','Contado',0,1)",(name,))
    provider=c.execute('SELECT id FROM proveedores WHERE nombre=?',(name,)).fetchone()
    c.execute('INSERT OR IGNORE INTO proveedor_especialidades(proveedor_id,especialidad) VALUES(?,?)',(provider['id'],specialty))
c.commit(); print('Financieras:',c.execute("SELECT COUNT(*) FROM clientes WHERE tipo='Financiera'").fetchone()[0]); print('Talleres:',c.execute("SELECT COUNT(*) FROM proveedores WHERE tipo='Taller'").fetchone()[0]); print('Especialidades:',c.execute('SELECT COUNT(*) FROM proveedor_especialidades').fetchone()[0]); c.close()
