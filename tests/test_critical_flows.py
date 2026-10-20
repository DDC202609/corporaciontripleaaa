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

    def test_catalog_allows_creating_a_movement_account(self):
        self.login_as_admin()
        response = self.client.post('/api/catalogo', json={
            'codigo': '7999', 'cuenta': 'Cuenta QA creada desde catálogo',
            'tipo': 'Gasto', 'grupo': 'Gasto de Venta', 'naturaleza': 'Deudora',
            'acepta_movimiento': True, 'activo': True,
        })
        self.assertEqual(response.status_code, 201)
        account = response.get_json()
        self.assertEqual(account['codigo'], '7999')
        self.assertEqual(account['cuenta'], 'Cuenta QA creada desde catálogo')

    def test_payroll_generates_biweekly_run_with_registered_commissions(self):
        self.login_as_admin()
        connection = self.server.db()
        department = connection.execute('SELECT id FROM departamentos WHERE activo=1 ORDER BY id LIMIT 1').fetchone()
        account = connection.execute('SELECT id FROM cuentas_contables WHERE activo=1 ORDER BY id LIMIT 1').fetchone()
        connection.close()
        self.assertIsNotNone(department)
        self.assertIsNotNone(account)
        employee_response = self.client.post('/api/planillas', json={
            'nombre': 'Empleado Planilla QA', 'numero_identidad': 'QA-PLANILLA-001',
            'fecha_ingreso': '2026-01-15',
            'salario': 20000, 'comisiones': 1200, 'bonos': 600,
            'ihss': 400, 'rap': 200, 'otras_deducciones': 100,
            'departamento_id': department['id'], 'cuenta_contable_id': account['id'],
        })
        self.assertEqual(employee_response.status_code, 201)
        employee = employee_response.get_json()
        commission_response = self.client.post('/api/planillas/comisiones', json={
            'empleado_id': employee['id'], 'fecha': '2026-10-10',
            'monto': 350, 'bono': 250, 'descripcion': 'Comisión y bono QA',
        })
        self.assertEqual(commission_response.status_code, 201)
        preview = self.client.get('/api/planillas/quincenas/preview?fecha=2026-10-10')
        self.assertEqual(preview.status_code, 200)
        preview_data = preview.get_json()
        detail = next(row for row in preview_data['empleados'] if row['empleado_id'] == employee['id'])
        self.assertEqual((preview_data['periodo_inicio'], preview_data['periodo_fin']), ('2026-10-01', '2026-10-15'))
        self.assertAlmostEqual(detail['salario'], 10000)
        self.assertAlmostEqual(detail['comisiones_fijas'], 600)
        self.assertAlmostEqual(detail['comisiones_registradas'], 350)
        self.assertAlmostEqual(detail['bonos_registrados'], 250)
        self.assertAlmostEqual(detail['bonos'], 550)
        self.assertAlmostEqual(detail['neto'], 11150)
        generated = self.client.post('/api/planillas/quincenas', json={'fecha_referencia': '2026-10-10'})
        self.assertEqual(generated.status_code, 201)
        payroll_id = generated.get_json()['id']
        history = self.client.get('/api/planillas/quincenas').get_json()
        self.assertTrue(any(row['id'] == payroll_id and row['estado'] == 'Generada' for row in history))
        duplicate = self.client.post('/api/planillas/quincenas', json={'fecha_referencia': '2026-10-10'})
        self.assertEqual(duplicate.status_code, 409)
        missing_reference = self.client.post(f'/api/planillas/quincenas/{payroll_id}/registrar-pago', json={
            'fecha_pago': '2026-10-16', 'banco': 'BAC Credomatic',
        })
        self.assertEqual(missing_reference.status_code, 400)
        paid = self.client.post(f'/api/planillas/quincenas/{payroll_id}/registrar-pago', json={
            'fecha_pago': '2026-10-16', 'banco': 'BAC Credomatic', 'referencia': 'QA-PLANILLA-PAGO',
        })
        self.assertEqual(paid.status_code, 200)
        self.assertAlmostEqual(paid.get_json()['total'], 11150)
        connection = self.server.db()
        cash = connection.execute("SELECT salida,banco FROM movimientos_caja WHERE referencia_tipo='pago_planilla' AND referencia_id=?", (payroll_id,)).fetchone()
        entries = connection.execute("SELECT debe,haber FROM partidas WHERE referencia_tipo='pago_planilla' AND referencia_id=?", (payroll_id,)).fetchall()
        payroll_accounts = connection.execute('''SELECT cc.codigo,SUM(p.debe) debe
            FROM partidas p JOIN cuentas_contables cc ON cc.id=p.cuenta_id
            WHERE p.referencia_tipo='pago_planilla' AND p.referencia_id=?
            GROUP BY cc.codigo''', (payroll_id,)).fetchall()
        connection.close()
        self.assertAlmostEqual(cash['salida'], 11150)
        self.assertEqual(cash['banco'], 'BAC Credomatic')
        self.assertAlmostEqual(sum(row['debe'] for row in entries), 11150)
        self.assertAlmostEqual(sum(row['haber'] for row in entries), 11150)
        debits = {row['codigo']: row['debe'] for row in payroll_accounts}
        self.assertAlmostEqual(debits['6201'], 9650)
        self.assertAlmostEqual(debits['6102'], 950)
        self.assertAlmostEqual(debits['6103'], 550)
        # Simula la clasificación histórica que llevaba todo el pago a
        # sueldos y verifica que la corrección no altera banco ni el pago.
        connection = self.server.db()
        legacy_salary = connection.execute("SELECT id FROM cuentas_contables WHERE codigo='6101'").fetchone()
        connection.execute("UPDATE partidas SET cuenta_id=? WHERE referencia_tipo='pago_planilla' AND referencia_id=? AND debe>0", (legacy_salary['id'], payroll_id))
        connection.commit(); connection.close()
        reclassified = self.client.post('/api/planillas/reclasificar-pagos', json={'mes': '2026-10'})
        self.assertEqual(reclassified.status_code, 200)
        self.assertEqual(len(reclassified.get_json()['corregidas']), 1)
        connection = self.server.db()
        corrected = connection.execute('''SELECT cc.codigo,SUM(p.debe)-SUM(p.haber) saldo
            FROM partidas p JOIN cuentas_contables cc ON cc.id=p.cuenta_id
            WHERE (p.referencia_tipo='pago_planilla' OR p.referencia_tipo='reclasificacion_planilla')
              AND p.referencia_id=? GROUP BY cc.codigo''', (payroll_id,)).fetchall()
        connection.close()
        corrected_balances = {row['codigo']: row['saldo'] for row in corrected}
        self.assertAlmostEqual(corrected_balances['6201'], 9650)
        self.assertAlmostEqual(corrected_balances['6102'], 950)
        self.assertAlmostEqual(corrected_balances['6103'], 550)
        termination = self.client.post(f"/api/planillas/{employee['id']}/baja")
        self.assertEqual(termination.status_code, 200)
        active = self.client.get('/api/planillas?activos=1').get_json()
        self.assertFalse(any(row['id'] == employee['id'] for row in active))
        all_employees = self.client.get('/api/planillas').get_json()
        self.assertTrue(any(row['id'] == employee['id'] and not row['activo'] for row in all_employees))

    def test_contract_generates_payable_ten_days_before_due_date(self):
        self.login_as_admin()
        connection = self.server.db()
        provider = connection.execute('SELECT id FROM proveedores WHERE activo=1 ORDER BY id LIMIT 1').fetchone()
        account = connection.execute('SELECT id FROM cuentas_contables WHERE activo=1 AND acepta_movimiento=1 ORDER BY id LIMIT 1').fetchone()
        connection.close()
        response = self.client.post('/api/contratos', json={
            'numero_contrato': 'QA-CONTRATO-001', 'proveedor_id': provider['id'],
            'fecha_inicio': '2026-10-01', 'fecha_fin': '2026-12-31',
            'monto_mensual': 1500, 'dia_pago': 25, 'cuenta_contable_id': account['id'],
        })
        self.assertEqual(response.status_code, 201)
        contract_id = response.get_json()['id']
        contracts = self.client.get('/api/contratos')
        self.assertEqual(contracts.status_code, 200)
        payable_rows = self.client.get('/api/cuentas-por-pagar-contratos').get_json()
        payable = next(row for row in payable_rows if row['contrato_id'] == contract_id and row['periodo'] == '2026-10')
        self.assertAlmostEqual(payable['saldo'], 1500)
        connection = self.server.db()
        entry = connection.execute("SELECT 1 FROM asientos_contables WHERE referencia_tipo='contrato_cxp' AND referencia_id=?", (payable['id'],)).fetchone()
        connection.close()
        self.assertIsNotNone(entry)
        deposit = self.client.post(f'/api/contratos/{contract_id}/depositos-garantia', json={
            'fecha': '2026-10-19', 'monto': 3000, 'descripcion': 'Garantía QA',
        })
        self.assertEqual(deposit.status_code, 201)

    def test_usd_contract_is_projected_without_automatic_payable_or_entry(self):
        self.login_as_admin()
        connection = self.server.db()
        provider = connection.execute('SELECT id FROM proveedores WHERE activo=1 ORDER BY id LIMIT 1').fetchone()
        account = connection.execute('SELECT id FROM cuentas_contables WHERE activo=1 AND acepta_movimiento=1 ORDER BY id LIMIT 1').fetchone()
        connection.close()
        response = self.client.post('/api/contratos', json={
            'numero_contrato': 'QA-CONTRATO-USD-001', 'proveedor_id': provider['id'],
            'fecha_inicio': '2026-10-01', 'fecha_fin': '2026-12-31',
            'monto_mensual': 275.50, 'moneda': 'USD', 'dia_pago': 25,
            'cuenta_contable_id': account['id'],
        })
        self.assertEqual(response.status_code, 201)
        contract_id = response.get_json()['id']

        plan = self.client.get('/api/planificacion-financiera').get_json()
        projected = next(row for row in plan['compromisos_usd']
                         if row['id'] == contract_id and row['fecha_compromiso'] == '2026-10-25')
        self.assertEqual(projected['moneda'], 'USD')
        self.assertAlmostEqual(projected['pendiente'], 275.50)
        self.assertAlmostEqual(plan['resumen']['total'],
                               plan['resumen']['transito'] + plan['resumen']['taller'])

        payables = self.client.get('/api/cuentas-por-pagar-contratos').get_json()
        self.assertFalse(any(row['contrato_id'] == contract_id for row in payables))
        deposit = self.client.post(f'/api/contratos/{contract_id}/depositos-garantia', json={
            'fecha': '2026-10-20', 'monto': 500, 'moneda': 'L',
            'descripcion': 'Garantía USD QA',
        })
        self.assertEqual(deposit.status_code, 201)
        self.assertEqual(deposit.get_json()['moneda'], 'USD')
        connection = self.server.db()
        entry = connection.execute('''SELECT 1 FROM asientos_contables ac
            JOIN cuentas_por_pagar_contratos cp ON cp.id=ac.referencia_id
            WHERE ac.referencia_tipo='contrato_cxp' AND cp.contrato_id=?''', (contract_id,)).fetchone()
        deposit_currency = connection.execute('SELECT moneda FROM contratos_depositos_garantia WHERE contrato_id=?',
                                              (contract_id,)).fetchone()['moneda']
        connection.close()
        self.assertIsNone(entry)
        self.assertEqual(deposit_currency, 'USD')

    def test_physical_inventory_count_compares_system_and_scanned_vins(self):
        self.login_as_admin()
        response = self.client.post('/api/conteos-inventario', json={
            'nombre': 'Conteo QA', 'fecha': '2026-10-14', 'estatus': 'DPV'
        })
        self.assertEqual(response.status_code, 201)
        count = response.get_json()
        self.assertEqual(count['conteo']['estatus'], 'DPV')
        self.assertGreater(count['resumen']['existencia'], 0)
        connection = self.server.db()
        other_statuses = connection.execute('''SELECT COUNT(*) FROM conteos_inventario_detalle d
            JOIN vehiculos v ON v.id=d.vehiculo_id
            WHERE d.conteo_id=? AND v.estado<>'DPV' ''', (count['conteo']['id'],)).fetchone()[0]
        connection.close()
        self.assertEqual(other_statuses, 0)
        detail = next(item for item in count['detalles'] if item['existencia'] == 1)

        capture = self.client.post(
            f"/api/conteos-inventario/{count['conteo']['id']}/capturas",
            json={'vin': detail['vin']},
        )
        self.assertEqual(capture.status_code, 200)
        self.assertEqual(capture.get_json()['detalle']['fisico'], 1)
        self.assertEqual(capture.get_json()['detalle']['diferencia'], 0)

        current = self.client.get('/api/conteos-inventario/actual').get_json()
        self.assertEqual(current['resumen']['fisico'], 1)
        close = self.client.post(f"/api/conteos-inventario/{count['conteo']['id']}/cerrar")
        self.assertEqual(close.status_code, 200)
        self.assertEqual(close.get_json()['conteo']['estado'], 'Cerrado')

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

    def test_costing_history_lists_registered_vehicle_costings(self):
        self.login_as_admin()
        response = self.client.get('/api/costeos')
        self.assertEqual(response.status_code, 200)
        rows = response.get_json()
        self.assertTrue(rows)
        self.assertTrue(all('vin' in row and 'costo_total' in row for row in rows))
        self.assertTrue(all(row['fecha_actualizacion'] for row in rows))

    def test_only_authorized_account_can_update_costing(self):
        connection = self.server.db()
        profile = connection.execute('SELECT id FROM perfiles WHERE es_administrador=1 LIMIT 1').fetchone()
        vehicle = connection.execute('SELECT id FROM vehiculos ORDER BY id LIMIT 1').fetchone()
        cursor = connection.execute('''INSERT INTO usuarios(username,password_hash,nombre,email,rol,activo,perfil_id)
            VALUES(?,?,?,?,?,?,?)''', ('other-costing-user', 'unused', 'Otro usuario', 'other-costing-user@example.com', 'Administrador', 1, profile['id']))
        user_id = cursor.lastrowid
        connection.commit()
        connection.close()
        with self.client.session_transaction() as session:
            session['user_id'] = user_id
            session['sesion_version'] = 1
        response = self.client.put(f"/api/vehiculos/{vehicle['id']}/adquisicion", json={'ajuste_compra': 1})
        self.assertEqual(response.status_code, 403)

    def test_continuing_workshop_does_not_create_a_new_order(self):
        self.login_as_admin()
        connection = self.server.db()
        order = connection.execute(
            '''SELECT o.id,o.vehiculo_id FROM ordenes_trabajo o
               JOIN proveedores p ON p.nombre=o.taller AND p.activo=1
               WHERE o.estado='Pendiente' ORDER BY o.id LIMIT 1'''
        ).fetchone()
        self.assertIsNotNone(order)
        before = connection.execute(
            'SELECT COUNT(*) FROM ordenes_trabajo WHERE vehiculo_id=?', (order['vehiculo_id'],)
        ).fetchone()[0]
        connection.close()
        response = self.client.put(
            f"/api/ordenes-trabajo/{order['id']}",
            json={'accion': 'continuar_mantenimiento', 'valor_final': 1234.5, 'metodo_pago': 'Efectivo'},
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
        connection = self.server.db()
        cash_move = connection.execute(
            "SELECT salida FROM movimientos_caja WHERE referencia_tipo='pago_ot' AND referencia_id=?", (order['id'],)
        ).fetchone()
        journal = connection.execute(
            "SELECT 1 FROM asientos_contables WHERE referencia_tipo='cierre_ot' AND referencia_id=?", (order['id'],)
        ).fetchone()
        connection.close()
        self.assertAlmostEqual(cash_move['salida'], 1234.5, places=2)
        self.assertIsNotNone(journal)
        correction = self.client.put(
            f"/api/ordenes-trabajo/{order['id']}",
            json={'accion': 'corregir_cierre', 'valor_final': 999},
        )
        self.assertEqual(correction.status_code, 409)
        self.assertIn('partida contable', correction.get_json()['error'])

    def test_credit_work_order_creates_payable_then_cash_payment(self):
        self.login_as_admin()
        connection = self.server.db()
        vehicle = connection.execute('SELECT id FROM vehiculos ORDER BY id LIMIT 1').fetchone()
        provider = connection.execute('SELECT id,nombre FROM proveedores WHERE activo=1 ORDER BY id LIMIT 1').fetchone()
        self.assertIsNotNone(vehicle)
        self.assertIsNotNone(provider)
        order_id, _ = self.server.crear_orden_trabajo(
            connection, vehicle['id'], None, '2026-09-20', provider['nombre'], 'Prueba contable', 300, 'Prueba de crédito de taller'
        )
        connection.commit()
        connection.close()
        closing = self.client.put(
            f'/api/ordenes-trabajo/{order_id}',
            json={'accion': 'continuar_mantenimiento', 'valor_final': 300, 'metodo_pago': 'Crédito'},
        )
        self.assertEqual(closing.status_code, 200)
        connection = self.server.db()
        payable = connection.execute(
            'SELECT id,saldo,estado FROM cuentas_por_pagar_ot WHERE orden_trabajo_id=?', (order_id,)
        ).fetchone()
        connection.close()
        self.assertIsNotNone(payable)
        self.assertAlmostEqual(payable['saldo'], 300, places=2)
        payment = self.client.post(
            f"/api/cuentas-por-pagar-taller/{payable['id']}/pagos",
            json={'fecha': '2026-09-20', 'tipo_pago': 'Efectivo', 'monto': 300},
        )
        self.assertEqual(payment.status_code, 201)
        self.assertEqual(payment.get_json()['estado'], 'Pagada')

    def test_legacy_closed_order_regularization_adds_missing_cost_once(self):
        self.login_as_admin()
        connection = self.server.db()
        vehicle = connection.execute('SELECT id FROM vehiculos ORDER BY id LIMIT 1').fetchone()
        provider = connection.execute('SELECT id,nombre FROM proveedores WHERE activo=1 ORDER BY id LIMIT 1').fetchone()
        order_id, order_number = self.server.crear_orden_trabajo(
            connection, vehicle['id'], None, '2026-09-20', provider['nombre'], 'Prueba histórica', 450, 'OT cerrada antes del pago'
        )
        connection.execute("UPDATE ordenes_trabajo SET estado='Finalizada',valor_final=450,costo_cargado=0 WHERE id=?", (order_id,))
        connection.commit()
        connection.close()
        response = self.client.put(
            f'/api/ordenes-trabajo/{order_id}',
            json={'accion': 'contabilizar_cierre_existente', 'metodo_pago': 'Efectivo'},
        )
        self.assertEqual(response.status_code, 200)
        connection = self.server.db()
        cost = connection.execute(
            "SELECT monto FROM costos_vehiculo WHERE vehiculo_id=? AND documento=? AND categoria='Mano de obra / OT'",
            (vehicle['id'], order_number),
        ).fetchone()
        connection.close()
        self.assertIsNotNone(cost)
        self.assertAlmostEqual(cost['monto'], 450, places=2)

    def test_parts_purchase_requires_payment_and_posts_cash_entry(self):
        self.login_as_admin()
        connection = self.server.db()
        vehicle = connection.execute('SELECT id FROM vehiculos ORDER BY id LIMIT 1').fetchone()
        provider = connection.execute('SELECT id,nombre FROM proveedores WHERE activo=1 ORDER BY id LIMIT 1').fetchone()
        order_id, _ = self.server.crear_orden_trabajo(
            connection, vehicle['id'], None, '2026-09-20', provider['nombre'], 'Prueba repuesto', 0, 'Prueba compra de repuesto'
        )
        connection.commit()
        connection.close()
        response = self.client.post(
            f'/api/ordenes-trabajo/{order_id}/repuestos',
            json={'factura': 'QA-PART-0001', 'fecha': '2026-09-20', 'proveedor_id': provider['id'],
                  'subtotal': 100, 'isv': 15, 'descripcion': 'Repuesto de prueba', 'metodo_pago': 'Efectivo'},
        )
        self.assertEqual(response.status_code, 201)
        connection = self.server.db()
        move = connection.execute(
            "SELECT salida FROM movimientos_caja WHERE referencia_tipo='pago_repuesto_ot' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        journal = connection.execute(
            "SELECT 1 FROM asientos_contables WHERE referencia_tipo='repuesto_ot'"
        ).fetchone()
        connection.close()
        self.assertAlmostEqual(move['salida'], 115, places=2)
        self.assertIsNotNone(journal)

    def test_reversing_parts_payable_payment_restores_payable_and_removes_its_entry(self):
        self.login_as_admin()
        connection = self.server.db()
        vehicle = connection.execute('SELECT id FROM vehiculos ORDER BY id LIMIT 1').fetchone()
        provider = connection.execute('SELECT id,nombre FROM proveedores WHERE activo=1 ORDER BY id LIMIT 1').fetchone()
        order_id, _ = self.server.crear_orden_trabajo(
            connection, vehicle['id'], None, '2026-09-20', provider['nombre'], 'Prueba reversión', 0, 'Prueba pago CxP repuesto'
        )
        connection.commit()
        connection.close()
        part = self.client.post(
            f'/api/ordenes-trabajo/{order_id}/repuestos',
            json={'factura': 'QA-REVERSE-PART-0001', 'fecha': '2026-09-20', 'proveedor_id': provider['id'],
                  'subtotal': 200, 'isv': 0, 'descripcion': 'Repuesto para reversión', 'metodo_pago': 'Crédito'},
        )
        self.assertEqual(part.status_code, 201)
        connection = self.server.db()
        payable = connection.execute('''SELECT cp.id FROM cuentas_por_pagar_repuestos_ot cp
            JOIN repuestos_ot r ON r.id=cp.repuesto_ot_id WHERE r.factura='QA-REVERSE-PART-0001' ''').fetchone()
        connection.close()
        payment = self.client.post(
            f"/api/cuentas-por-pagar-repuestos/{payable['id']}/pagos",
            json={'fecha': '2026-09-21', 'tipo_pago': 'Efectivo', 'monto': 200},
        )
        self.assertEqual(payment.status_code, 201)
        connection = self.server.db()
        payment_id = connection.execute('''SELECT id FROM pagos_cuentas_por_pagar_repuestos_ot
            WHERE cuenta_por_pagar_repuesto_id=?''', (payable['id'],)).fetchone()['id']
        connection.close()
        reversal = self.client.delete(
            f"/api/cuentas-por-pagar-repuestos/{payable['id']}/pagos/{payment_id}"
        )
        self.assertEqual(reversal.status_code, 200)
        connection = self.server.db()
        restored = connection.execute('SELECT saldo,estado FROM cuentas_por_pagar_repuestos_ot WHERE id=?', (payable['id'],)).fetchone()
        payment_row = connection.execute('SELECT 1 FROM pagos_cuentas_por_pagar_repuestos_ot WHERE id=?', (payment_id,)).fetchone()
        journal = connection.execute("SELECT 1 FROM asientos_contables WHERE referencia_tipo='pago_cxp_repuesto' AND referencia_id=?", (payment_id,)).fetchone()
        cash = connection.execute("SELECT 1 FROM movimientos_caja WHERE referencia_tipo='pago_cxp_repuesto' AND referencia_id=?", (payment_id,)).fetchone()
        connection.close()
        self.assertAlmostEqual(restored['saldo'], 200, places=2)
        self.assertEqual(restored['estado'], 'Pendiente')
        self.assertIsNone(payment_row)
        self.assertIsNone(journal)
        self.assertIsNone(cash)

    def test_legacy_parts_can_be_regularized_against_bac(self):
        self.login_as_admin()
        connection = self.server.db()
        vehicle = connection.execute('SELECT id FROM vehiculos ORDER BY id LIMIT 1').fetchone()
        provider = connection.execute('SELECT id,nombre FROM proveedores WHERE activo=1 ORDER BY id LIMIT 1').fetchone()
        order_id, _ = self.server.crear_orden_trabajo(
            connection, vehicle['id'], None, '2026-09-20', provider['nombre'], 'Prueba histórica', 0, 'OT histórica'
        )
        cursor = connection.execute(
            '''INSERT INTO repuestos_ot(orden_trabajo_id,proveedor_id,factura,fecha,descripcion,subtotal,isv,total)
               VALUES(?,?,?,?,?,?,?,?)''',
            (order_id, provider['id'], 'QA-LEGACY-BAC-0001', '2026-09-20', 'Repuesto histórico', 200, 30, 230),
        )
        connection.commit()
        connection.close()
        response = self.client.put(
            f'/api/repuestos-ot/{cursor.lastrowid}/regularizar-pago',
            json={'metodo_pago': 'Transferencia', 'banco': 'BAC Credomatic'},
        )
        self.assertEqual(response.status_code, 200)
        connection = self.server.db()
        part = connection.execute('SELECT metodo_pago,banco_pago FROM repuestos_ot WHERE id=?', (cursor.lastrowid,)).fetchone()
        move = connection.execute(
            "SELECT salida,banco FROM movimientos_caja WHERE referencia_tipo='pago_repuesto_ot' AND referencia_id=?", (cursor.lastrowid,)
        ).fetchone()
        connection.close()
        self.assertEqual(part['metodo_pago'], 'Transferencia')
        self.assertEqual(part['banco_pago'], 'BAC Credomatic')
        self.assertAlmostEqual(move['salida'], 230, places=2)

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

    def test_backup_contains_complete_data_exports_and_operational_code(self):
        import json
        import zipfile
        os.environ['BACKUP_TOKEN']='respaldo-prueba'
        try:
            response=self.client.get('/respaldo/sistema.zip',headers={
                'Authorization':'Bearer respaldo-prueba'
            })
        finally:
            os.environ.pop('BACKUP_TOKEN',None)
        self.assertEqual(response.status_code,200)
        with zipfile.ZipFile(__import__('io').BytesIO(response.data)) as archive:
            entries=set(archive.namelist())
            self.assertTrue({'datos/autolote.sqlite','datos/LEEME.txt','sistema/server.py',
                             'sistema/app/index.html','sistema/scripts/respaldo_local.sh',
                             'datos/tablas/vehiculos.csv','datos/tablas/ventas.csv',
                             'datos/tablas/costos_vehiculo.csv','datos/tablas/asientos_contables.csv'} <= entries)
            manifest=json.loads(archive.read('manifiesto.json'))
            self.assertTrue(any(item['tabla']=='vehiculos' for item in manifest['tablas']))

    def test_purchase_planning_calculates_weekly_projection_and_saves_parameters(self):
        self.login_as_admin()
        saved=self.client.put('/api/planificacion-compra/parametros',json={'parametros':[{
            'modelo':'Modelo prueba planificación','dpv':3,'taller':2,'transito':1,
            'distribucion':{'2020':2,'2021':1}
        }]})
        self.assertEqual(saved.status_code,200)
        response=self.client.get('/api/planificacion-compra')
        self.assertEqual(response.status_code,200)
        data=response.get_json()
        row=next(item for item in data['modelos'] if item['modelo']=='MODELO PRUEBA PLANIFICACIÓN')
        self.assertEqual({key:row['inventario_optimo'][key] for key in ('dpv','taller','transito')},
                         {'dpv':3,'taller':2,'transito':1})
        self.assertEqual(row['sugerido_compra'],6)
        self.assertEqual(row['existencia_total'],0)
        self.assertEqual(row['faltante_compra'],6)
        self.assertEqual(len(row['proyeccion']),12)
        self.assertEqual(len(data['semanas']),12)

    def test_purchase_planning_rounds_purchase_shortage_to_whole_vehicles(self):
        self.assertEqual(self.server._redondear_unidad_compra(0.49), 0)
        self.assertEqual(self.server._redondear_unidad_compra(0.50), 1)
        self.assertEqual(self.server._redondear_unidad_compra(1.65), 2)

    def test_purchase_planning_counts_only_physical_stages_and_separates_consignment(self):
        self.login_as_admin()
        connection=self.server.db()
        model='MODELO INVENTARIO FISICO QA'
        for vin,state,purchase_type in (
            ('QA-PHYSICAL-DPV','DPV','Compra local'),
            ('QA-PHYSICAL-WORKSHOP','En Taller','Compra local'),
            ('QA-PHYSICAL-CONSIGNMENT','DPV','Consignación'),
            ('QA-NOT-PHYSICAL-STAGE','Nacionalizado','Compra local'),
        ):
            connection.execute('''INSERT INTO vehiculos(vin,modelo,anio,estado,tipo_compra)
                VALUES(?,?,?,?,?)''',(vin,model,2020,state,purchase_type))
        connection.commit(); connection.close()
        saved=self.client.put('/api/planificacion-compra/parametros',json={'parametros':[{
            'modelo':model,'dpv':3,'taller':0,'transito':0,'distribucion':{'2020':3}
        }]})
        self.assertEqual(saved.status_code,200)
        data=self.client.get('/api/planificacion-compra').get_json()
        row=next(item for item in data['modelos'] if item['modelo']==model)
        self.assertEqual(row['inventario_actual'],{
            'DPV':1,'En Taller':1,'En Tránsito':0,'Consignación':1,'anios':{'2020':3}
        })
        self.assertEqual(row['existencia_total'],3)
        self.assertEqual(row['existencia_para_compra'],2)
        self.assertEqual(row['faltante_compra'],1)
        self.assertEqual(row['distribucion'],[{'anio':2020,'ideal':3,'actual':3,'sugerido':0}])

    def test_purchase_planning_loads_supplied_optimal_inventory_matrix(self):
        self.login_as_admin()
        response=self.client.post('/api/planificacion-compra/parametros/base')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.get_json()['cantidad'],38)
        data=self.client.get('/api/planificacion-compra').get_json()
        crv=next(item for item in data['modelos'] if item['modelo']=='CRV')
        self.assertEqual(crv['inventario_optimo'],{'dpv':5,'taller':5,'transito':5,
                         'distribucion':{'2010':1,'2011':1,'2014':2,'2015':2,'2016':2,
                                          '2017':1,'2018':1,'2020':1,'2023':1}})

    def test_purchase_adjustment_increases_original_payable_and_posts_once(self):
        self.login_as_admin()
        connection = self.server.db()
        purchase = connection.execute('''SELECT a.*,v.estado,COALESCE(c.ajuste_compra,0) ajuste_compra,
                COALESCE(c.ajuste_cxp_contabilizado,0) ajuste_cxp_contabilizado,
                cp.monto_original cxp_monto_original,cp.saldo cxp_saldo
            FROM adquisiciones a
            JOIN vehiculos v ON v.id=a.vehiculo_id
            JOIN cuentas_por_pagar cp ON cp.adquisicion_id=a.id
            LEFT JOIN costos_adquisicion c ON c.vehiculo_id=a.vehiculo_id
            WHERE a.tipo_compra<>'Consignación' AND v.estado<>'Vendido'
            ORDER BY a.id LIMIT 1''').fetchone()
        self.assertIsNotNone(purchase)
        target = round(float(purchase['ajuste_compra']) + 1234.5, 2)
        expected_difference = round(target - float(purchase['ajuste_cxp_contabilizado']), 2)
        before_original = float(purchase['cxp_monto_original'])
        before_balance = float(purchase['cxp_saldo'])
        vehicle_id = purchase['vehiculo_id']
        response = self.client.put(
            f'/api/vehiculos/{vehicle_id}/adquisicion', json={'ajuste_compra': target}
        )
        self.assertEqual(response.status_code, 200)
        connection = self.server.db()
        payable = connection.execute(
            'SELECT monto_original,saldo FROM cuentas_por_pagar WHERE adquisicion_id=?', (purchase['id'],)
        ).fetchone()
        journal = connection.execute('''SELECT a.id FROM asientos_contables a
            JOIN ajustes_compra_cxp ac ON ac.id=a.referencia_id
            WHERE a.referencia_tipo='ajuste_compra_cxp' AND ac.adquisicion_id=?
            ORDER BY a.id DESC LIMIT 1''', (purchase['id'],)).fetchone()
        lines = connection.execute('''SELECT cc.codigo,p.debe,p.haber FROM partidas p
            JOIN cuentas_contables cc ON cc.id=p.cuenta_id WHERE p.asiento_id=?''', (journal['id'],)).fetchall()
        connection.close()
        self.assertAlmostEqual(payable['monto_original'], before_original + expected_difference, places=2)
        self.assertAlmostEqual(payable['saldo'], before_balance + expected_difference, places=2)
        self.assertTrue(any(line['codigo'] == '2101' and line['haber'] == expected_difference for line in lines))
        vehicle_detail = self.client.get(f'/api/vehiculos/{vehicle_id}').get_json()
        self.assertAlmostEqual(vehicle_detail['compra']['saldo_cxp'], payable['saldo'], places=2)

        repeated = self.client.put(
            f'/api/vehiculos/{vehicle_id}/adquisicion', json={'ajuste_compra': target}
        )
        self.assertEqual(repeated.status_code, 200)
        connection = self.server.db()
        count = connection.execute(
            "SELECT COUNT(*) FROM ajustes_compra_cxp WHERE adquisicion_id=?", (purchase['id'],)
        ).fetchone()[0]
        connection.close()
        self.assertEqual(count, 1)

    def test_sale_cost_credits_the_vehicle_inventory_stage_not_transit(self):
        self.login_as_admin()
        connection = self.server.db()
        vehicle = connection.execute('''SELECT v.id,v.precio_venta FROM vehiculos v
            WHERE v.estado='DPV' AND NOT EXISTS(
                SELECT 1 FROM ventas ve WHERE ve.vehiculo_id=v.id
            ) ORDER BY v.id LIMIT 1''').fetchone()
        seller = connection.execute(
            'SELECT nombre FROM vendedores WHERE activo=1 ORDER BY id LIMIT 1'
        ).fetchone()
        connection.close()
        self.assertIsNotNone(vehicle)
        self.assertIsNotNone(seller)
        price = float(vehicle['precio_venta'] or 300000)

        response = self.client.post('/api/ventas', json={
            'vehiculo_id': vehicle['id'],
            'precio_lista': price,
            'precio_final': price,
            'descuento': 0,
            'prima': price,
            'monto_financiado': 0,
            'transferencia': 0,
            'cliente_nombre': 'Cliente QA costo de venta',
            'vendedor_nombre': seller['nombre'],
            'fecha': '2026-10-01',
            'pagos': [{'tipo_pago': 'Efectivo', 'monto': price, 'referencia': ''}],
        })
        self.assertEqual(response.status_code, 201, response.get_json())
        sale_id = response.get_json()['id']
        connection = self.server.db()
        cost_line = connection.execute('''SELECT p.id,cc.codigo FROM partidas p
            JOIN asientos_contables a ON a.id=p.asiento_id
            JOIN cuentas_contables cc ON cc.id=p.cuenta_id
            WHERE a.referencia_tipo='venta_costo' AND a.referencia_id=? AND p.haber>0''',
            (sale_id,)).fetchone()
        self.assertEqual(cost_line['codigo'], '1103')

        # La reparación histórica solo reclasifica la línea errónea; no crea
        # una póliza adicional ni modifica los débitos de la venta.
        transit_id = self.server.cuenta_contable_id(connection, 'inventario_transito')
        connection.execute('UPDATE partidas SET cuenta_id=? WHERE id=?', (transit_id, cost_line['id']))
        self.server.corregir_costo_ventas_desde_transito(connection)
        repaired = connection.execute('''SELECT cc.codigo FROM partidas p
            JOIN cuentas_contables cc ON cc.id=p.cuenta_id WHERE p.id=?''',
            (cost_line['id'],)).fetchone()
        connection.close()
        self.assertEqual(repaired['codigo'], '1103')

    def test_inventory_reconciliation_is_read_only_and_exposes_differences_by_vin(self):
        self.login_as_admin()
        response = self.client.get('/api/contabilidad/conciliacion-inventario')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual({row['cuenta'] for row in data['resumen']}, {
            'inventario_dpv', 'inventario_taller', 'inventario_transito'
        })
        self.assertAlmostEqual(
            data['diferencia_total'], sum(row['diferencia'] for row in data['resumen']), places=2
        )
        validation = data['validacion_dashboard']
        self.assertEqual(validation['vehiculos_incluidos'], validation['vins_unicos'])
        self.assertTrue(validation['sin_duplicar_vehiculos'])
        self.assertAlmostEqual(
            validation['total_recalculado'], sum(row['total_dashboard'] for row in data['detalle_dashboard']), places=2
        )
        for row in data['inconsistencias']:
            self.assertTrue(row['vin'])
            self.assertIn(row['cuenta_esperada'], ('DPV', 'En Taller', 'En Tránsito'))

    def test_financial_planning_uses_the_current_purchase_payable_balance(self):
        self.login_as_admin()
        connection = self.server.db()
        purchase = connection.execute('''SELECT a.id,a.vehiculo_id,a.proveedor_id,a.fecha
            FROM adquisiciones a
            JOIN vehiculos v ON v.id=a.vehiculo_id
            WHERE a.tipo_compra<>'Consignación'
            ORDER BY a.id LIMIT 1''').fetchone()
        self.assertIsNotNone(purchase)
        expected = 4321.45
        connection.execute("UPDATE vehiculos SET estado='En Tránsito' WHERE id=?", (purchase['vehiculo_id'],))
        connection.execute('''INSERT INTO cuentas_por_pagar(adquisicion_id,proveedor_id,fecha,monto_original,saldo,estado)
            VALUES(?,?,?,?,?,'Pendiente')
            ON CONFLICT(adquisicion_id) DO UPDATE SET monto_original=excluded.monto_original,
                saldo=excluded.saldo,estado='Pendiente' ''',
            (purchase['id'], purchase['proveedor_id'], purchase['fecha'], expected, expected))
        connection.commit()
        connection.close()

        response = self.client.get('/api/planificacion-financiera')
        self.assertEqual(response.status_code, 200)
        commitment = next(row for row in response.get_json()['compromisos']
                          if row['origen'] == 'Adquisición' and row['id'] == purchase['id'])
        self.assertAlmostEqual(commitment['pendiente'], expected, places=2)

    def test_financial_planning_values_planned_purchases_from_purchase_shortages(self):
        self.login_as_admin()
        self.client.post('/api/planificacion-compra/parametros/base')
        response=self.client.get('/api/planificacion-financiera?mes=2026-10')
        self.assertEqual(response.status_code,200)
        planned=response.get_json()['compras_planificadas']
        self.assertTrue(planned)
        for row in planned:
            self.assertEqual(row['origen'],'Compra planificada')
            self.assertEqual(row['pendiente'],round(row['unidades']*row['costo_promedio'],2))


if __name__ == '__main__':
    unittest.main(verbosity=2)
