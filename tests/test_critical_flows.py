"""Pruebas de regresión para los flujos que no deben fallar en producción.

Se ejecutan sobre una copia temporal de la base incluida en el proyecto; nunca
modifican los datos locales ni los del disco persistente de Render.
"""

import importlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class CriticalFlowsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = Path(tempfile.mkdtemp(prefix='aaa-critical-tests-'))
        cls.database_path = cls.temp_dir / 'autolote.sqlite'
        shutil.copy2(PROJECT_ROOT / 'data' / 'autolote.sqlite', cls.database_path)
        os.environ['DATABASE_PATH'] = str(cls.database_path)
        os.environ.pop('BACKUP_TOKEN', None)
        sys.path.insert(0, str(PROJECT_ROOT))
        sys.modules.pop('server', None)
        cls.server = importlib.import_module('server')
        connection = cls.server.db()
        cls.admin_id = connection.execute(
            'SELECT id FROM usuarios WHERE activo=1 ORDER BY id LIMIT 1'
        ).fetchone()['id']
        connection.close()

    @classmethod
    def tearDownClass(cls):
        os.environ.pop('DATABASE_PATH', None)
        shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def login_as_admin(self):
        with self.client.session_transaction() as session:
            session['user_id'] = self.admin_id

    def setUp(self):
        self.client = self.server.app.test_client()

    def vehicle_payload(self, vin='TEST-CRITICAL-FLOW-0001'):
        return {
            'vin': vin,
            'lote': 'QA-001',
            'tipo_vehiculo': 'Pick up',
            'marca': 'FORD',
            'modelo': 'F-150',
            'version': 'XLT',
            'color': 'Plateado',
            'anio': 2018,
            'kilometraje': 0,
            'placa': '',
            'ubicacion': None,
            'precio_venta': 0,
            'observaciones': 'Prueba automática aislada',
        }

    def test_healthcheck_verifies_database(self):
        response = self.client.get('/healthz')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {'status': 'ok'})
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')

    def test_api_requires_a_session(self):
        response = self.client.get('/api/vehiculos')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()['error'], 'Sesión requerida.')

    def test_create_vehicle_and_register_master_data(self):
        self.login_as_admin()
        response = self.client.post('/api/vehiculos', json=self.vehicle_payload())
        self.assertEqual(response.status_code, 201)
        vehicle_id = response.get_json()['id']
        connection = self.server.db()
        vehicle = connection.execute('SELECT vin FROM vehiculos WHERE id=?', (vehicle_id,)).fetchone()
        master = connection.execute(
            '''SELECT 1 FROM informacion_vehiculo
               WHERE tipo_vehiculo=? AND marca=? AND modelo=? AND version=?''',
            ('Pick up', 'FORD', 'F-150', 'XLT'),
        ).fetchone()
        connection.close()
        self.assertEqual(vehicle['vin'], 'TEST-CRITICAL-FLOW-0001')
        self.assertIsNotNone(master)

    def test_duplicate_vin_returns_a_controlled_error(self):
        self.login_as_admin()
        self.client.post('/api/vehiculos', json=self.vehicle_payload('TEST-DUPLICATE-VIN-0001'))
        response = self.client.post('/api/vehiculos', json=self.vehicle_payload('TEST-DUPLICATE-VIN-0001'))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()['error'], 'El VIN ya existe')

    def test_vehicle_list_includes_associated_purchase_summary(self):
        self.login_as_admin()
        connection = self.server.db()
        purchase = connection.execute(
            '''SELECT a.vehiculo_id,a.id,a.fecha,a.costo_compra,p.nombre proveedor_nombre
               FROM adquisiciones a JOIN proveedores p ON p.id=a.proveedor_id
               ORDER BY a.id DESC LIMIT 1'''
        ).fetchone()
        connection.close()
        self.assertIsNotNone(purchase)
        response = self.client.get('/api/vehiculos')
        self.assertEqual(response.status_code, 200)
        vehicle = next(item for item in response.get_json() if item['id'] == purchase['vehiculo_id'])
        self.assertEqual(vehicle['compra_id'], purchase['id'])
        self.assertEqual(vehicle['compra_fecha'], purchase['fecha'])
        self.assertEqual(vehicle['compra_costo'], purchase['costo_compra'])
        self.assertEqual(vehicle['compra_proveedor'], purchase['proveedor_nombre'])

    def test_kardex_breakdown_identifies_cost_documents(self):
        self.login_as_admin()
        connection = self.server.db()
        vehicle = connection.execute(
            'SELECT id FROM vehiculos WHERE precio_compra>0 ORDER BY id LIMIT 1'
        ).fetchone()
        connection.close()
        self.assertIsNotNone(vehicle)
        response = self.client.get(f"/api/vehiculos/{vehicle['id']}")
        self.assertEqual(response.status_code, 200)
        detail = response.get_json()
        breakdown = detail['desglose_costos']
        self.assertTrue(breakdown)
        self.assertTrue(all(item['documento'] for item in breakdown))
        self.assertAlmostEqual(
            sum(item['monto'] for item in breakdown), detail['costo_real'], places=2
        )

    def test_continuing_workshop_does_not_create_a_new_order(self):
        self.login_as_admin()
        connection = self.server.db()
        order = connection.execute(
            "SELECT id,vehiculo_id FROM ordenes_trabajo WHERE estado='Pendiente' ORDER BY id LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(order)
        before = connection.execute(
            'SELECT COUNT(*) FROM ordenes_trabajo WHERE vehiculo_id=?', (order['vehiculo_id'],)
        ).fetchone()[0]
        connection.close()
        response = self.client.put(
            f"/api/ordenes-trabajo/{order['id']}",
            json={'accion': 'continuar_mantenimiento', 'valor_final': 0},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.get_json()['nueva_ot'])
        connection = self.server.db()
        after = connection.execute(
            'SELECT COUNT(*) FROM ordenes_trabajo WHERE vehiculo_id=?', (order['vehiculo_id'],)
        ).fetchone()[0]
        vehicle = connection.execute('SELECT estado FROM vehiculos WHERE id=?', (order['vehiculo_id'],)).fetchone()
        connection.close()
        self.assertEqual(after, before)
        self.assertEqual(vehicle['estado'], 'En Taller')
        correction = self.client.put(
            f"/api/ordenes-trabajo/{order['id']}",
            json={'accion': 'corregir_cierre', 'valor_final': 1234.5},
        )
        self.assertEqual(correction.status_code, 200)
        connection = self.server.db()
        cost = connection.execute(
            '''SELECT monto FROM costos_vehiculo
               WHERE vehiculo_id=? AND documento=(SELECT numero_ot FROM ordenes_trabajo WHERE id=?)
                 AND categoria='Mano de obra / OT' ''',
            (order['vehiculo_id'], order['id']),
        ).fetchone()
        connection.close()
        self.assertIsNotNone(cost)
        self.assertAlmostEqual(cost['monto'], 1234.5, places=2)

    def test_unexpected_write_error_rolls_back_and_returns_json(self):
        self.login_as_admin()
        original = self.server.asegurar_informacion_vehiculo
        self.server.asegurar_informacion_vehiculo = lambda *_: (_ for _ in ()).throw(RuntimeError('fallo simulado'))
        try:
            response = self.client.post('/api/vehiculos', json=self.vehicle_payload('TEST-ROLLBACK-0001'))
        finally:
            self.server.asegurar_informacion_vehiculo = original
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()['error'], 'No se pudo completar la operación. Intente nuevamente.')
        connection = self.server.db()
        row = connection.execute('SELECT 1 FROM vehiculos WHERE vin=?', ('TEST-ROLLBACK-0001',)).fetchone()
        connection.close()
        self.assertIsNone(row)

    def test_backup_requires_a_configured_token(self):
        response = self.client.get('/respaldo/sistema.zip')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()['error'], 'El respaldo local aún no está configurado.')


if __name__ == '__main__':
    unittest.main(verbosity=2)
