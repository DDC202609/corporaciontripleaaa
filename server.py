from flask import Flask, request, jsonify, send_from_directory, send_file, session, redirect, url_for, render_template_string, g, has_request_context
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash
import sqlite3, os, shutil, uuid, hmac, io, json, tempfile, zipfile, re, csv, calendar
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
ROOT=os.path.dirname(os.path.abspath(__file__))
BUNDLED_DATA=os.path.join(ROOT,'data')
DATA_DIR=os.environ.get('DATA_DIR', BUNDLED_DATA)
DB=os.environ.get('DATABASE_PATH', os.path.join(DATA_DIR,'autolote.sqlite')); APP=os.path.join(ROOT,'app')
os.makedirs(os.path.dirname(DB), exist_ok=True)
# En Render la base se crea en el disco persistente solo en el primer arranque.
# Los siguientes despliegues conservan esa copia y nunca pisan la operación real.
if not os.path.exists(DB) and os.path.abspath(DB)!=os.path.abspath(os.path.join(BUNDLED_DATA,'autolote.sqlite')):
    bundled_db=os.path.join(BUNDLED_DATA,'autolote.sqlite')
    if os.path.exists(bundled_db): shutil.copy2(bundled_db, DB)
SECRET_FILE=os.path.join(os.path.dirname(DB),'.session_secret')
configured_secret=os.environ.get('SECRET_KEY')
if configured_secret:
    SECRET_KEY=configured_secret
elif os.path.exists(SECRET_FILE):
    with open(SECRET_FILE,'rb') as secret_file: SECRET_KEY=secret_file.read()
else:
    SECRET_KEY=os.urandom(32)
    with open(SECRET_FILE,'wb') as secret_file: secret_file.write(SECRET_KEY)
app=Flask(__name__)
app.config.update(SECRET_KEY=SECRET_KEY,SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SAMESITE='Lax',SESSION_COOKIE_SECURE=os.environ.get('SESSION_COOKIE_SECURE','').lower() in {'1','true','yes'})
ESTADOS_VEHICULO=('En Tránsito','Nacionalizado','En Taller','DPV','Reservado','Vendido','Devuelto')
ZONA_HORARIA_OPERACION=ZoneInfo('America/Tegucigalpa')
COSTEO_EDITOR_USERNAME=os.environ.get('COSTEO_EDITOR_USERNAME','erickmadrid.sabillon@gmail.com').strip().casefold()
COSTEO_NACIONALIZACION_USERNAME=os.environ.get('COSTEO_NACIONALIZACION_USERNAME','alorenzo').strip().casefold()

def ahora_operacion():
    """Fecha y hora que rigen los períodos operativos de Corporación Triple AAA."""
    return datetime.now(ZONA_HORARIA_OPERACION)

# Módulos que se pueden asignar a un perfil.  Los permisos se validan tanto en
# la interfaz como en cada solicitud al servidor.
ACCESS_MODULES=(
    ('dashboard','Dashboard'),('vehiculos','Vehículos'),('informacion_vehiculo','Información de vehículo'),
    ('proveedores','Proveedores'),('adquisiciones','Adquisiciones'),('taller','Taller'),('costeo','Costeo'),
    ('ventas','Ventas y cobros'),('vendedores','Vendedores'),('inventario','Consulta de inventario'),('conteo_inventario','Conteo físico de inventario'),
    ('catalogo','Catálogo contable'),('centros_costo','Centros de costo'),('cartera','Cuentas por cobrar y pagar'),('contabilidad','Contabilidad'),
    ('caja_bancos','Caja y bancos'),('gastos','Registro de gastos'),('comisiones','Comisiones'),('planillas','Planillas'),('contratos','Contratos'),
    ('planificacion_financiera','Planificación financiera'),('planificacion_compra','Planificación de compra'),('auditoria','Bitácora de auditoría'),
)

def init_access_control_schema():
    """Migración aislada y segura para perfiles y permisos de acceso."""
    c=db()
    c.execute('''CREATE TABLE IF NOT EXISTS perfiles(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre TEXT NOT NULL UNIQUE COLLATE NOCASE,
        descripcion TEXT,
        activo INTEGER NOT NULL DEFAULT 1,
        es_administrador INTEGER NOT NULL DEFAULT 0,
        es_sistema INTEGER NOT NULL DEFAULT 0,
        creado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS perfil_permisos(
        perfil_id INTEGER NOT NULL REFERENCES perfiles(id) ON DELETE CASCADE,
        modulo TEXT NOT NULL,
        puede_ver INTEGER NOT NULL DEFAULT 0,
        puede_modificar INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(perfil_id,modulo)
    )''')
    # La tabla usuarios existe en todas las instalaciones actuales.  Esta
    # columna es aditiva y preserva las cuentas que ya estaban creadas.
    try:
        user_columns={row['name'] for row in c.execute('PRAGMA table_info(usuarios)')}
        if user_columns and 'perfil_id' not in user_columns:
            c.execute('ALTER TABLE usuarios ADD COLUMN perfil_id INTEGER REFERENCES perfiles(id)')
    except sqlite3.OperationalError:
        c.close(); return

    defaults=(
        ('Administrador','Acceso completo al sistema.',1,1),
        ('Operaciones','Operaciones, datos maestros, gastos y cartera.',0,1),
        ('Ventas','Ventas, clientes, vehículos disponibles y cobros.',0,1),
        ('Consulta','Acceso de solo lectura a indicadores y consultas.',0,1),
    )
    for name,description,is_admin,is_system in defaults:
        c.execute('INSERT OR IGNORE INTO perfiles(nombre,descripcion,es_administrador,es_sistema) VALUES(?,?,?,?)',
                  (name,description,is_admin,is_system))
    profiles={row['nombre']:row['id'] for row in c.execute('SELECT id,nombre FROM perfiles')}
    all_modules={key for key,_ in ACCESS_MODULES}
    operational={'dashboard','vehiculos','informacion_vehiculo','proveedores','adquisiciones','taller','costeo','ventas','inventario','conteo_inventario','catalogo','centros_costo','vendedores','gastos','cartera','planificacion_compra','planillas'}
    sales={'dashboard','vehiculos','ventas','vendedores','inventario','caja_bancos','comisiones'}
    consultation={'dashboard','vehiculos','inventario','conteo_inventario','contabilidad','caja_bancos','ventas'}
    for profile_name,allowed in (('Administrador',all_modules),('Operaciones',operational),('Ventas',sales),('Consulta',consultation)):
        profile_id=profiles.get(profile_name)
        if not profile_id: continue
        is_admin=profile_name=='Administrador'
        for module,_ in ACCESS_MODULES:
            can_view=1 if (is_admin or module in allowed) else 0
            can_edit=1 if (is_admin or (profile_name!='Consulta' and module in allowed)) else 0
            c.execute('''INSERT OR IGNORE INTO perfil_permisos(perfil_id,modulo,puede_ver,puede_modificar)
                         VALUES(?,?,?,?)''',(profile_id,module,can_view,can_edit))
    # Actualiza una sola vez el perfil de sistema anterior de Operaciones. Se
    # conserva cualquier ajuste manual posterior porque ya no coincide con la
    # descripción anterior.
    operations_id=profiles.get('Operaciones')
    operations_profile=c.execute('SELECT descripcion FROM perfiles WHERE id=?',(operations_id,)).fetchone() if operations_id else None
    if operations_profile and operations_profile['descripcion']=='Inventario, adquisiciones, taller y costeo.':
        c.execute("UPDATE perfiles SET descripcion='Operaciones, datos maestros, gastos y cartera.' WHERE id=?",(operations_id,))
        for module,_ in ACCESS_MODULES:
            enabled=module in operational
            c.execute('''INSERT INTO perfil_permisos(perfil_id,modulo,puede_ver,puede_modificar) VALUES(?,?,?,?)
                ON CONFLICT(perfil_id,modulo) DO UPDATE SET puede_ver=excluded.puede_ver,puede_modificar=excluded.puede_modificar''',
                (operations_id,module,int(enabled),int(enabled)))
    admin_id=profiles.get('Administrador')
    if admin_id:
        # No se le quita acceso al administrador histórico durante la migración.
        c.execute('UPDATE usuarios SET perfil_id=? WHERE perfil_id IS NULL',(admin_id,))
    c.commit(); c.close()

def user_access(user_id):
    c=db()
    row=c.execute('''SELECT u.id,u.username,u.nombre,u.email,u.activo,u.perfil_id,u.sesion_version,
        p.nombre perfil_nombre,COALESCE(p.es_administrador,0) es_administrador
        FROM usuarios u LEFT JOIN perfiles p ON p.id=u.perfil_id WHERE u.id=?''',(user_id,)).fetchone()
    if not row:
        c.close(); return None
    data=dict(row)
    permissions={item['modulo']:{'ver':bool(item['puede_ver']),'modificar':bool(item['puede_modificar'])}
                 for item in c.execute('SELECT modulo,puede_ver,puede_modificar FROM perfil_permisos WHERE perfil_id=?',(data.get('perfil_id'),))}
    c.close(); data['permisos']=permissions
    return data

def nivel_actualizacion_costeo():
    """Distingue el editor total del usuario limitado a nacionalización."""
    user=user_access(session.get('user_id')) if session.get('user_id') else None
    username=str(user.get('username') or '').strip().casefold() if user else ''
    # La cuenta de Anderson está registrada como ``a.lorenzo``; según el
    # origen también puede llegar como ``alorenzo`` o como correo. Para no
    # depender de puntos, guiones o el dominio, comparamos el alias
    # normalizado, conservando la autorización limitada a esa identidad.
    def alias_normalizado(value):
        alias=str(value or '').strip().casefold().split('@',1)[0]
        return re.sub(r'[^a-z0-9]', '', alias)
    alias=alias_normalizado(username)
    editor_alias=alias_normalizado(COSTEO_EDITOR_USERNAME)
    nacionalizacion_alias=alias_normalizado(COSTEO_NACIONALIZACION_USERNAME)
    if username==COSTEO_EDITOR_USERNAME or alias==editor_alias: return 'total'
    if username==COSTEO_NACIONALIZACION_USERNAME or alias==nacionalizacion_alias: return 'nacionalizacion'
    return 'ninguno'

def puede_actualizar_costeo():
    """El costeo histórico completo solo puede ser actualizado por la cuenta autorizada."""
    return nivel_actualizacion_costeo()=='total'

def request_module(path):
    if path.startswith('/api/usuarios') or path.startswith('/api/perfiles'): return 'usuarios'
    if path.startswith('/api/auditoria'): return 'auditoria'
    if path.startswith('/api/dashboard'): return 'dashboard'
    if path.startswith('/api/planificacion-financiera'): return 'planificacion_financiera'
    if path.startswith('/api/planificacion-compra'): return 'planificacion_compra'
    if path.startswith('/api/conteos-inventario'): return 'conteo_inventario'
    if path.startswith('/api/contabilidad'): return 'contabilidad'
    if path.startswith('/api/cuentas-por-'): return 'cartera'
    if path.startswith('/api/caja'): return 'caja_bancos'
    if path.startswith('/api/gastos-ot'): return 'taller'
    if path.startswith('/api/gastos') or path.startswith('/api/conceptos-gasto'): return 'gastos'
    if path.startswith('/api/comisiones'): return 'comisiones'
    if path.startswith('/api/planillas'): return 'planillas'
    if path.startswith('/api/contratos'): return 'contratos'
    # Consultar el catálogo activo es parte del registro de una venta.  La
    # administración (crear, editar o desactivar) sigue protegida por el
    # permiso específico de Vendedores.
    if path.startswith('/api/vendedores'):
        return 'ventas' if request.method=='GET' else 'vendedores'
    if path.startswith('/api/ventas'): return 'ventas'
    if path.startswith('/api/proveedores') or path.startswith('/api/financieras'): return 'proveedores'
    if path.startswith('/api/informacion-vehiculo'): return 'informacion_vehiculo'
    if path.startswith('/api/catalogo'): return 'catalogo'
    if path.startswith('/api/areas') or path.startswith('/api/departamentos'): return 'centros_costo'
    if path.startswith('/api/inventario'): return 'inventario'
    if path.startswith('/api/adquisiciones'): return 'adquisiciones'
    if path.startswith('/api/ordenes-trabajo') or path.startswith('/api/repuestos-ot'): return 'taller'
    if path.startswith('/api/costeos'): return 'costeo'
    if path.startswith('/api/vehiculos'):
        return 'vehiculos'
    return None

LOGIN_TEMPLATE='''<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{ title }} · Corporación Triple AAA</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f4f7fb;font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#182230}.card{width:min(410px,calc(100vw - 40px));background:#fff;border:1px solid #d9e0ea;border-radius:18px;padding:34px;box-shadow:0 16px 42px #18223016}h1{margin:0 0 8px;font-size:2rem}.sub{color:#667085;margin-bottom:28px}label{font-weight:700;display:block;margin:16px 0 7px}input{box-sizing:border-box;width:100%;border:1px solid #cbd5e1;border-radius:9px;padding:12px;font-size:1rem}button{width:100%;border:0;border-radius:9px;padding:13px;background:#175cd3;color:#fff;font-weight:800;font-size:1rem;margin-top:24px;cursor:pointer}.error{margin:14px 0;padding:10px;border-radius:8px;background:#fef3f2;color:#b42318}.brand{color:#175cd3;font-weight:800;margin-bottom:10px}</style></head><body><main class="card"><div class="brand">Corporación Triple AAA</div><h1>{{ title }}</h1><div class="sub">{{ subtitle }}</div>{% if error %}<div class="error">{{ error }}</div>{% endif %}<form method="post"><label>Usuario</label><input name="username" required autocomplete="username" minlength="3" autofocus><label>Contraseña</label><input name="password" type="password" required autocomplete="{{ 'new-password' if setup else 'current-password' }}" minlength="8"><button>{{ button }}</button></form></main></body></html>'''

def db():
    c=sqlite3.connect(DB, timeout=15); c.row_factory=sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON'); c.execute('PRAGMA busy_timeout=15000')
    if has_request_context():
        connections=getattr(g,'sqlite_connections',[]); connections.append(c); g.sqlite_connections=connections
    return c

def user_count():
    c=db()
    try: return c.execute('SELECT COUNT(*) FROM usuarios').fetchone()[0]
    except sqlite3.OperationalError: return 0
    finally: c.close()

# La bitácora guarda evidencia de operaciones que modifican información. Las
# contraseñas, hashes y secretos nunca se persisten en ella.
AUDIT_RESOURCES=(
    (r'^/api/vehiculos/(\d+)', 'Vehículo', 'vehiculos'),
    (r'^/api/adquisiciones/(\d+)', 'Adquisición', 'adquisiciones'),
    (r'^/api/ordenes-trabajo/(\d+)', 'Orden de trabajo', 'ordenes_trabajo'),
    (r'^/api/repuestos-ot/(\d+)', 'Repuesto / insumo', 'repuestos_ot'),
    (r'^/api/gastos-ot/(\d+)', 'Gasto de OT', 'gastos_ot'),
    (r'^/api/ventas/(\d+)', 'Venta', 'ventas'),
    (r'^/api/planillas/(\d+)', 'Empleado de planilla', 'empleados_planilla'),
    (r'^/api/proveedores/(\d+)', 'Proveedor', 'proveedores'),
    (r'^/api/usuarios/(\d+)', 'Usuario', 'usuarios'),
)
AUDIT_PREFIXES=(
    ('/api/vehiculos', 'Vehículo', 'vehiculos'), ('/api/adquisiciones', 'Adquisición', 'adquisiciones'),
    ('/api/ordenes-trabajo', 'Orden de trabajo', 'ordenes_trabajo'), ('/api/repuestos-ot', 'Repuesto / insumo', 'repuestos_ot'),
    ('/api/gastos-ot', 'Gasto de OT', 'gastos_ot'), ('/api/ventas', 'Venta', 'ventas'),
    ('/api/proveedores', 'Proveedor', 'proveedores'), ('/api/usuarios', 'Usuario', 'usuarios'),
    ('/api/perfiles', 'Perfil de acceso', 'perfiles'), ('/api/cuentas-por-', 'Cuenta por pagar/cobrar', None),
    ('/api/caja', 'Caja y bancos', None), ('/api/gastos', 'Gasto operativo', 'gastos_operativos'),
    ('/api/clientes', 'Cliente', 'clientes'), ('/api/vendedores', 'Vendedor', 'vendedores'),
    ('/api/catalogo', 'Catálogo contable', None), ('/api/informacion-vehiculo', 'Información de vehículo', None),
    ('/api/areas', 'Centro de costo', None), ('/api/departamentos', 'Centro de costo', None),
    ('/api/comisiones', 'Comisión', None),
    ('/api/planillas', 'Empleado de planilla', 'empleados_planilla'),
    ('/api/contratos', 'Contrato', 'contratos'),
)

def redact_auditoria(value):
    if isinstance(value, dict):
        return {key: ('***' if any(word in str(key).lower() for word in ('password','contrasena','secret'))
                      else redact_auditoria(item)) for key,item in value.items()}
    if isinstance(value, list): return [redact_auditoria(item) for item in value]
    return value

def recurso_auditoria(path):
    for pattern,resource,table in AUDIT_RESOURCES:
        match=re.match(pattern,path)
        if match: return resource,int(match.group(1)),table
    for prefix,resource,table in AUDIT_PREFIXES:
        if path.startswith(prefix): return resource,None,table
    return 'Operación del sistema',None,None

def instantanea_auditoria(table, resource_id):
    if not table or resource_id is None: return None
    # Los nombres de tabla se definen únicamente en las constantes internas.
    c=db()
    try:
        row=c.execute(f'SELECT * FROM {table} WHERE id=?',(resource_id,)).fetchone()
        return redact_auditoria(dict(row)) if row else None
    except sqlite3.Error:
        return None
    finally:
        c.close()

@app.before_request
def preparar_auditoria():
    if not request.path.startswith('/api/') or request.method not in {'POST','PUT','PATCH','DELETE'}:
        return None
    if request.path in {'/api/sesion/actividad'} or request.path.startswith('/api/auditoria'):
        return None
    resource,resource_id,table=recurso_auditoria(request.path)
    g.audit_event={
        'resource':resource,'resource_id':resource_id,'table':table,
        'before':instantanea_auditoria(table,resource_id),
        'request':redact_auditoria(request.get_json(silent=True) or {}),
    }

@app.after_request
def registrar_auditoria(response):
    event=getattr(g,'audit_event',None)
    if not event or response.status_code>=400:
        return response
    result=response.get_json(silent=True) or {}
    resource_id=event['resource_id']
    if resource_id is None and isinstance(result,dict):
        for key in ('id','vehiculo_id','orden_trabajo_id','adquisicion_id'):
            if result.get(key) is not None:
                try:
                    resource_id=int(result[key]); break
                except (TypeError,ValueError): pass
    after=instantanea_auditoria(event['table'],resource_id)
    detail={'solicitud':event['request']}
    if after is not None: detail['registro']=after
    actions={'POST':'Crear','PUT':'Modificar','PATCH':'Modificar','DELETE':'Eliminar'}
    action=actions.get(request.method,request.method)
    operation=event['request'].get('accion') if isinstance(event['request'],dict) else None
    if operation: action=f'{action}: {operation}'
    user=user_access(session.get('user_id')) if session.get('user_id') else None
    c=db()
    try:
        c.execute('''INSERT INTO bitacora_auditoria(usuario_id,usuario,accion,recurso,recurso_id,ruta,metodo,antes,despues,ip)
            VALUES(?,?,?,?,?,?,?,?,?,?)''',(
            user.get('id') if user else None, user.get('username') if user else 'Sistema', action,
            event['resource'],resource_id,request.path,request.method,
            json.dumps(event['before'],ensure_ascii=False),json.dumps(detail,ensure_ascii=False),request.remote_addr))
        c.commit()
    except sqlite3.Error:
        app.logger.exception('No fue posible registrar una entrada de auditoría.')
    finally:
        c.close()
    return response

@app.before_request
def require_authenticated_user():
    if request.endpoint in {'login','setup','logout','static','download_system_backup','healthcheck'} or request.path.startswith('/static/'):
        return None
    if not user_count():
        return redirect(url_for('setup'))
    user_id=session.get('user_id')
    if user_id:
        user=user_access(user_id)
        if user and user.get('activo'):
            now=datetime.now().timestamp()
            last_activity=session.get('last_activity')
            # La cookie por sí sola no prolonga una sesión: cualquier petición
            # realizada después de dos minutos sin interacción la invalida.
            if last_activity is not None and now-float(last_activity)>120:
                session.clear()
                if request.path.startswith('/api/'):
                    return jsonify(error='La sesión expiró por 2 minutos de inactividad.'),401
                return redirect(url_for('login'))
            session_version=session.get('sesion_version')
            if session_version is not None and int(session_version)!=int(user.get('sesion_version') or 1):
                session.clear()
                if request.path.startswith('/api/'):
                    return jsonify(error='La contraseña fue restablecida. Inicie sesión nuevamente.'),401
                return redirect(url_for('login'))
            session['last_activity']=now
            session['sesion_version']=int(user.get('sesion_version') or 1)
            module=request_module(request.path)
            if module=='usuarios':
                if user.get('es_administrador'): return None
                return jsonify(error='No tiene permisos para administrar accesos.'),403
            if module=='auditoria':
                if user.get('es_administrador'): return None
                return jsonify(error='La bitácora de auditoría solo está disponible para administradores.'),403
            if module and not user.get('es_administrador'):
                required='ver' if request.method in {'GET','HEAD','OPTIONS'} else 'modificar'
                if not user.get('permisos',{}).get(module,{}).get(required,False):
                    return jsonify(error='No tiene permisos para realizar esta acción.'),403
            return None
    session.clear()
    if request.path.startswith('/api/'):
        return jsonify(error='Sesión requerida.'),401
    return redirect(url_for('login'))

@app.before_request
def actualizar_obligaciones_de_contratos():
    """En cada ingreso autenticado revisa la ventana de diez días de contratos."""
    if not session.get('user_id') or request.path.startswith('/static/'):
        return None
    try:
        c=db(); generar_cxp_contratos_programadas(c); c.commit(); c.close()
    except (sqlite3.Error, ValueError):
        try: c.close()
        except Exception: pass

@app.get('/healthz')
def healthcheck():
    """Verifica aplicación y acceso real a SQLite para Render."""
    c=None
    try:
        c=db(); c.execute('SELECT 1').fetchone(); c.execute('SELECT COUNT(*) FROM usuarios').fetchone()
        return jsonify(status='ok'),200
    except sqlite3.Error:
        app.logger.exception('Falló la verificación de salud de la base de datos.')
        return jsonify(status='error'),503
    finally:
        if c: c.close()

@app.errorhandler(Exception)
def handle_unexpected_error(error):
    """Evita respuestas HTML en las APIs y conserva el detalle en los logs."""
    if isinstance(error,HTTPException):
        return error
    for connection in getattr(g,'sqlite_connections',[]):
        try: connection.rollback()
        except sqlite3.Error: pass
    app.logger.exception('Error no controlado en %s %s',request.method,request.path)
    if request.path.startswith('/api/'):
        return jsonify(error='No se pudo completar la operación. Intente nuevamente.'),500
    return 'Ocurrió un error inesperado. Intente nuevamente.',500

@app.teardown_request
def close_request_connections(error=None):
    """Cierra conexiones que un flujo interrumpido no alcanzó a liberar."""
    for connection in getattr(g,'sqlite_connections',[]):
        try:
            if error: connection.rollback()
            connection.close()
        except sqlite3.Error:
            pass

@app.after_request
def apply_response_safety_headers(response):
    """Protecciones compatibles con la interfaz actual y respuestas sensibles."""
    response.headers.setdefault('X-Content-Type-Options','nosniff')
    response.headers.setdefault('X-Frame-Options','DENY')
    response.headers.setdefault('Referrer-Policy','strict-origin-when-cross-origin')
    if request.path.startswith('/api/') or request.path.startswith('/respaldo/'):
        response.headers['Cache-Control']='no-store'
    return response

def placa_disponible(c, placa, vehiculo_id=None):
    placa=(placa or '').strip().upper()
    if not placa:
        return True
    sql="SELECT 1 FROM vehiculos WHERE UPPER(TRIM(placa))=?"
    args=[placa]
    if vehiculo_id is not None:
        sql+=' AND id<>?'; args.append(vehiculo_id)
    return c.execute(sql,args).fetchone() is None

def init_db(sync_history=True):
    c=db()
    c.execute('''CREATE TABLE IF NOT EXISTS usuarios(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE COLLATE NOCASE,
        password_hash TEXT NOT NULL,
        nombre TEXT,
        email TEXT,
        rol TEXT NOT NULL DEFAULT 'Administrador',
        activo INTEGER NOT NULL DEFAULT 1,
        creado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        ultimo_acceso TEXT
    )''')
    user_columns={row['name'] for row in c.execute('PRAGMA table_info(usuarios)')}
    if 'username' not in user_columns:
        c.execute('ALTER TABLE usuarios ADD COLUMN username TEXT')
    if 'password_hash' not in user_columns:
        c.execute('ALTER TABLE usuarios ADD COLUMN password_hash TEXT')
    if 'email' not in user_columns:
        c.execute('ALTER TABLE usuarios ADD COLUMN email TEXT')
    if 'creado_en' not in user_columns:
        c.execute('ALTER TABLE usuarios ADD COLUMN creado_en TEXT')
    if 'ultimo_acceso' not in user_columns:
        c.execute('ALTER TABLE usuarios ADD COLUMN ultimo_acceso TEXT')
    if 'sesion_version' not in user_columns:
        c.execute('ALTER TABLE usuarios ADD COLUMN sesion_version INTEGER NOT NULL DEFAULT 1')
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_usuarios_username ON usuarios(username COLLATE NOCASE) WHERE username IS NOT NULL")
    c.execute('CREATE UNIQUE INDEX IF NOT EXISTS ux_vehiculos_vin ON vehiculos(vin)')
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_vehiculos_placa ON vehiculos(UPPER(TRIM(placa))) WHERE TRIM(COALESCE(placa,''))<>''")
    c.execute('''CREATE TABLE IF NOT EXISTS costos_adquisicion(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        vehiculo_id INTEGER NOT NULL REFERENCES vehiculos(id) ON DELETE CASCADE,
        costo_exw REAL DEFAULT 0,
        grua REAL DEFAULT 0,
        flete REAL DEFAULT 0,
        costo_estimado REAL DEFAULT 0,
        ajuste_cif REAL DEFAULT 0,
        isv_pagado REAL DEFAULT 0,
        cl_std REAL DEFAULT 0,
        almacenaje REAL DEFAULT 0,
        gastos_aduaneros REAL DEFAULT 0,
        estatus TEXT,
        fecha_actualizacion TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(vehiculo_id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS movimientos_vehiculo(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        vehiculo_id INTEGER NOT NULL REFERENCES vehiculos(id) ON DELETE CASCADE,
        fecha TEXT NOT NULL,
        tipo TEXT NOT NULL,
        estado_anterior TEXT,
        estado_nuevo TEXT,
        ubicacion_anterior TEXT,
        ubicacion_nueva TEXT,
        referencia TEXT,
        observaciones TEXT,
        creado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS proveedores(
        id INTEGER PRIMARY KEY AUTOINCREMENT,nombre TEXT NOT NULL UNIQUE,rtn TEXT,telefono TEXT,email TEXT,direccion TEXT,
        condicion_pago TEXT NOT NULL DEFAULT 'Contado',dias_credito INTEGER NOT NULL DEFAULT 0,activo INTEGER NOT NULL DEFAULT 1)''')
    provider_columns={row['name'] for row in c.execute('PRAGMA table_info(proveedores)')}
    if 'tipo' not in provider_columns:
        c.execute("ALTER TABLE proveedores ADD COLUMN tipo TEXT NOT NULL DEFAULT 'Proveedor'")
    if 'identidad' not in provider_columns:
        c.execute('ALTER TABLE proveedores ADD COLUMN identidad TEXT')
    c.execute('''CREATE TABLE IF NOT EXISTS proveedor_especialidades(
        id INTEGER PRIMARY KEY AUTOINCREMENT, proveedor_id INTEGER NOT NULL REFERENCES proveedores(id) ON DELETE CASCADE,
        especialidad TEXT NOT NULL, UNIQUE(proveedor_id,especialidad))''')
    c.execute('''CREATE TABLE IF NOT EXISTS clientes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,nombre TEXT NOT NULL,identidad TEXT,rtn TEXT,telefono TEXT,direccion TEXT,email TEXT)''')
    client_columns={row['name'] for row in c.execute('PRAGMA table_info(clientes)')}
    if 'tipo' not in client_columns:
        c.execute("ALTER TABLE clientes ADD COLUMN tipo TEXT NOT NULL DEFAULT 'Cliente'")
    if 'condicion_pago' not in client_columns:
        c.execute("ALTER TABLE clientes ADD COLUMN condicion_pago TEXT NOT NULL DEFAULT 'Contado'")
    if 'activo' not in client_columns:
        c.execute('ALTER TABLE clientes ADD COLUMN activo INTEGER NOT NULL DEFAULT 1')
    c.execute('''CREATE TABLE IF NOT EXISTS vendedores(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre TEXT NOT NULL UNIQUE,
        identidad TEXT,
        telefono TEXT,
        email TEXT,
        activo INTEGER NOT NULL DEFAULT 1,
        creado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS adquisiciones(
        id INTEGER PRIMARY KEY AUTOINCREMENT,vehiculo_id INTEGER NOT NULL UNIQUE REFERENCES vehiculos(id),proveedor_id INTEGER NOT NULL REFERENCES proveedores(id),
        fecha TEXT NOT NULL,costo_compra REAL NOT NULL,anticipo REAL NOT NULL DEFAULT 0,metodo_pago TEXT NOT NULL,condicion_pago TEXT NOT NULL,
        dias_credito INTEGER NOT NULL DEFAULT 0,saldo REAL NOT NULL DEFAULT 0,observaciones TEXT,creado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS planificacion_compra_parametros(
        modelo TEXT PRIMARY KEY COLLATE NOCASE,
        inventario_optimo_dpv INTEGER NOT NULL DEFAULT 0,
        inventario_optimo_taller INTEGER NOT NULL DEFAULT 0,
        inventario_optimo_transito INTEGER NOT NULL DEFAULT 0,
        distribucion_anios TEXT NOT NULL DEFAULT '{}',
        actualizado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''')
    # Las autorizaciones se conservan separadas de la meta de inventario: son
    # una decisión de compra manual y no deben alterar el sugerido calculado.
    c.execute('''CREATE TABLE IF NOT EXISTS planificacion_compra_autorizaciones(
        modelo TEXT NOT NULL COLLATE NOCASE,
        anio INTEGER NOT NULL,
        cantidad INTEGER NOT NULL DEFAULT 0,
        actualizado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(modelo,anio)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS ordenes_trabajo(
        id INTEGER PRIMARY KEY AUTOINCREMENT,vehiculo_id INTEGER NOT NULL REFERENCES vehiculos(id),adquisicion_id INTEGER REFERENCES adquisiciones(id),
        fecha TEXT NOT NULL,estado TEXT NOT NULL DEFAULT 'Pendiente',detalle TEXT,creado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
    # Migración aditiva: conserva los registros existentes y permite guardar
    # los datos de adquisición solicitados en la ficha de vehículo.
    columns={row['name'] for row in c.execute('PRAGMA table_info(vehiculos)')}
    if 'proveedor' not in columns:
        c.execute('ALTER TABLE vehiculos ADD COLUMN proveedor TEXT')
    if 'tipo_compra' not in columns:
        c.execute('ALTER TABLE vehiculos ADD COLUMN tipo_compra TEXT')
    acquisition_columns={row['name'] for row in c.execute('PRAGMA table_info(adquisiciones)')}
    if 'necesita_reparacion' not in acquisition_columns:
        c.execute('ALTER TABLE adquisiciones ADD COLUMN necesita_reparacion INTEGER NOT NULL DEFAULT 0')
    if 'tipo_compra' not in acquisition_columns:
        c.execute('ALTER TABLE adquisiciones ADD COLUMN tipo_compra TEXT')
        c.execute('''UPDATE adquisiciones SET tipo_compra=(SELECT tipo_compra FROM vehiculos
            WHERE vehiculos.id=adquisiciones.vehiculo_id) WHERE tipo_compra IS NULL''')
    if 'placa_cambio' not in acquisition_columns:
        c.execute('ALTER TABLE adquisiciones ADD COLUMN placa_cambio TEXT')
    if 'banco' not in acquisition_columns:
        c.execute('ALTER TABLE adquisiciones ADD COLUMN banco TEXT')
    if 'fecha_llegada_estimada' not in acquisition_columns:
        c.execute('ALTER TABLE adquisiciones ADD COLUMN fecha_llegada_estimada TEXT')
    if 'contabilizada' not in acquisition_columns:
        c.execute('ALTER TABLE adquisiciones ADD COLUMN contabilizada INTEGER NOT NULL DEFAULT 1')
    # La fecha es una proyección operativa: para importaciones siempre son
    # cuarenta días desde la adquisición. También se completa el histórico
    # para que la planificación no deje unidades antiguas fuera.
    c.execute("""UPDATE adquisiciones SET fecha_llegada_estimada=date(fecha,'+40 days')
        WHERE tipo_compra='Importación' AND (fecha_llegada_estimada IS NULL OR fecha_llegada_estimada='')""")
    c.execute("UPDATE adquisiciones SET contabilizada=0 WHERE tipo_compra='Consignación' AND contabilizada IS NULL")
    costing_columns={row['name'] for row in c.execute('PRAGMA table_info(costos_adquisicion)')}
    if 'ajuste_compra' not in costing_columns:
        c.execute('ALTER TABLE costos_adquisicion ADD COLUMN ajuste_compra REAL NOT NULL DEFAULT 0')
    if 'ajuste_cxp_contabilizado' not in costing_columns:
        # Conserva el importe del ajuste que ya fue llevado a CxP. Así, al
        # guardar de nuevo el costeo solo se contabiliza la diferencia.
        c.execute('ALTER TABLE costos_adquisicion ADD COLUMN ajuste_cxp_contabilizado REAL NOT NULL DEFAULT 0')
    if 'placa_nacionalizacion' not in costing_columns:
        c.execute('ALTER TABLE costos_adquisicion ADD COLUMN placa_nacionalizacion TEXT')
    work_columns={row['name'] for row in c.execute('PRAGMA table_info(ordenes_trabajo)')}
    for name, definition in [('correlativo','INTEGER'),('numero_ot','TEXT'),('taller','TEXT'),('tipo_reparacion','TEXT'),('valor_negociado','REAL NOT NULL DEFAULT 0'),('valor_final','REAL'),('descripcion','TEXT'),('costo_cargado','INTEGER NOT NULL DEFAULT 0'),('migracion_historica','INTEGER NOT NULL DEFAULT 0'),('metodo_pago','TEXT'),('banco_pago','TEXT'),('referencia_pago','TEXT'),('fecha_entrega_estimada','TEXT')]:
        if name not in work_columns:
            c.execute(f'ALTER TABLE ordenes_trabajo ADD COLUMN {name} {definition}')
    existing_orders=c.execute('SELECT id FROM ordenes_trabajo WHERE correlativo IS NULL ORDER BY id').fetchall()
    next_number=c.execute('SELECT COALESCE(MAX(correlativo),0) FROM ordenes_trabajo').fetchone()[0]
    for order in existing_orders:
        next_number+=1
        c.execute('UPDATE ordenes_trabajo SET correlativo=?,numero_ot=?,descripcion=COALESCE(descripcion,detalle) WHERE id=?',(next_number,f'OT-{next_number:06d}',order['id']))
    c.execute('CREATE UNIQUE INDEX IF NOT EXISTS ux_ordenes_trabajo_correlativo ON ordenes_trabajo(correlativo)')
    c.execute('''CREATE TABLE IF NOT EXISTS anticipos_ot(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        orden_trabajo_id INTEGER NOT NULL REFERENCES ordenes_trabajo(id) ON DELETE CASCADE,
        fecha TEXT NOT NULL, monto REAL NOT NULL,
        metodo_pago TEXT NOT NULL, banco TEXT, referencia TEXT,
        creado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS ix_anticipos_ot_orden ON anticipos_ot(orden_trabajo_id,fecha DESC,id DESC)')
    c.execute('''CREATE TABLE IF NOT EXISTS repuestos_ot(
        id INTEGER PRIMARY KEY AUTOINCREMENT,orden_trabajo_id INTEGER NOT NULL REFERENCES ordenes_trabajo(id) ON DELETE CASCADE,
        proveedor_id INTEGER NOT NULL REFERENCES proveedores(id),factura TEXT NOT NULL,fecha TEXT NOT NULL,descripcion TEXT NOT NULL,
        subtotal REAL NOT NULL,isv REAL NOT NULL DEFAULT 0,total REAL NOT NULL,creado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''')
    parts_columns={row['name'] for row in c.execute('PRAGMA table_info(repuestos_ot)')}
    for name, definition in [('metodo_pago','TEXT'),('banco_pago','TEXT'),('referencia_pago','TEXT')]:
        if name not in parts_columns:
            c.execute(f'ALTER TABLE repuestos_ot ADD COLUMN {name} {definition}')
    c.execute('CREATE UNIQUE INDEX IF NOT EXISTS ux_repuestos_ot_factura ON repuestos_ot(orden_trabajo_id,factura)')
    c.execute('''CREATE TABLE IF NOT EXISTS costos_vehiculo(
        id INTEGER PRIMARY KEY AUTOINCREMENT,vehiculo_id INTEGER NOT NULL REFERENCES vehiculos(id),fecha TEXT NOT NULL,
        concepto TEXT NOT NULL,categoria TEXT,monto REAL NOT NULL DEFAULT 0,proveedor TEXT,documento TEXT,observaciones TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS gastos_ot(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        orden_trabajo_id INTEGER NOT NULL REFERENCES ordenes_trabajo(id) ON DELETE CASCADE,
        proveedor_id INTEGER REFERENCES proveedores(id), taller_origen TEXT, factura TEXT, fecha TEXT NOT NULL,
        categoria TEXT NOT NULL, descripcion TEXT NOT NULL, subtotal REAL NOT NULL DEFAULT 0,
        isv REAL NOT NULL DEFAULT 0, total REAL NOT NULL DEFAULT 0,
        costo_vehiculo_id INTEGER REFERENCES costos_vehiculo(id) ON DELETE SET NULL,
        creado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS ix_gastos_ot_orden ON gastos_ot(orden_trabajo_id)')
    # Maestro de información del vehículo. Alimenta los selectores dependientes
    # de tipo, marca, modelo y versión en la ficha de la unidad.
    c.execute('''CREATE TABLE IF NOT EXISTS informacion_vehiculo(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tipo_vehiculo TEXT NOT NULL DEFAULT '', marca TEXT NOT NULL DEFAULT '',
        modelo TEXT NOT NULL DEFAULT '', version TEXT NOT NULL DEFAULT '',
        activo INTEGER NOT NULL DEFAULT 1,
        UNIQUE(tipo_vehiculo,marca,modelo,version)
    )''')
    c.execute('''INSERT OR IGNORE INTO informacion_vehiculo(tipo_vehiculo,marca,modelo,version)
        SELECT COALESCE(TRIM(tipo_vehiculo),''),COALESCE(TRIM(marca),''),
               COALESCE(TRIM(modelo),''),COALESCE(TRIM(version),'')
        FROM vehiculos
        WHERE TRIM(COALESCE(marca,''))<>'' OR TRIM(COALESCE(modelo,''))<>''
               OR TRIM(COALESCE(version,''))<>'' OR TRIM(COALESCE(tipo_vehiculo,''))<>'' ''')
    sale_columns={row['name'] for row in c.execute('PRAGMA table_info(ventas)')}
    if 'factura' not in sale_columns:
        c.execute('ALTER TABLE ventas ADD COLUMN factura TEXT')
    for name, definition in [('correlativo','INTEGER'),('numero_transaccion','TEXT'),('tipo_venta','TEXT'),('forma_pago','TEXT'),('financiera_banco','TEXT'),('monto_financiado','REAL NOT NULL DEFAULT 0'),('transferencia','REAL NOT NULL DEFAULT 0'),('observaciones','TEXT')]:
        if name not in sale_columns:
            c.execute(f'ALTER TABLE ventas ADD COLUMN {name} {definition}')
    if 'garantia_dias' not in sale_columns:
        c.execute('ALTER TABLE ventas ADD COLUMN garantia_dias INTEGER')
    if 'vendedor_id' not in sale_columns:
        c.execute('ALTER TABLE ventas ADD COLUMN vendedor_id INTEGER REFERENCES vendedores(id)')
    c.execute('CREATE INDEX IF NOT EXISTS ix_ventas_vendedor ON ventas(vendedor_id)')
    existing_sales=c.execute('SELECT id FROM ventas WHERE correlativo IS NULL ORDER BY id').fetchall()
    next_sale=c.execute('SELECT COALESCE(MAX(correlativo),0) FROM ventas').fetchone()[0]
    for sale in existing_sales:
        next_sale+=1
        c.execute('UPDATE ventas SET correlativo=?,numero_transaccion=? WHERE id=?',(next_sale,f'V-{next_sale:05d}',sale['id']))
    c.execute('CREATE UNIQUE INDEX IF NOT EXISTS ux_ventas_correlativo ON ventas(correlativo)')
    c.execute('CREATE UNIQUE INDEX IF NOT EXISTS ux_ventas_factura ON ventas(factura) WHERE factura IS NOT NULL')
    c.execute('''CREATE TABLE IF NOT EXISTS correlativos_plataforma(
        tipo TEXT PRIMARY KEY,ultimo_numero INTEGER NOT NULL DEFAULT 0
    )''')
    max_sale=c.execute('SELECT COALESCE(MAX(correlativo),0) FROM ventas').fetchone()[0]
    c.execute('INSERT OR IGNORE INTO correlativos_plataforma(tipo,ultimo_numero) VALUES(?,?)',('FACTURA',max_sale))
    c.execute('INSERT OR IGNORE INTO correlativos_plataforma(tipo,ultimo_numero) VALUES(?,?)',('VENTA',max_sale))
    c.execute('''CREATE TABLE IF NOT EXISTS pagos_venta(
        id INTEGER PRIMARY KEY AUTOINCREMENT,venta_id INTEGER NOT NULL REFERENCES ventas(id) ON DELETE CASCADE,
        tipo_pago TEXT NOT NULL,referencia TEXT,financiera_id INTEGER REFERENCES proveedores(id),financiera_nombre TEXT,
        banco TEXT,monto REAL NOT NULL,fecha TEXT NOT NULL,creado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''')
    payment_columns={row['name'] for row in c.execute('PRAGMA table_info(pagos_venta)')}
    if 'banco' not in payment_columns:
        c.execute('ALTER TABLE pagos_venta ADD COLUMN banco TEXT')
    if 'financiera_cliente_id' not in payment_columns:
        c.execute('ALTER TABLE pagos_venta ADD COLUMN financiera_cliente_id INTEGER REFERENCES clientes(id)')
    if 'fecha_vencimiento' not in payment_columns:
        c.execute('ALTER TABLE pagos_venta ADD COLUMN fecha_vencimiento TEXT')
    if 'vehiculo_recibido_id' not in payment_columns:
        c.execute('ALTER TABLE pagos_venta ADD COLUMN vehiculo_recibido_id INTEGER REFERENCES vehiculos(id)')
    sale_columns={row['name'] for row in c.execute('PRAGMA table_info(ventas)')}
    if 'fee_administrativo_pct' not in sale_columns:
        c.execute('ALTER TABLE ventas ADD COLUMN fee_administrativo_pct REAL NOT NULL DEFAULT 0')
    if 'fee_administrativo_monto' not in sale_columns:
        c.execute('ALTER TABLE ventas ADD COLUMN fee_administrativo_monto REAL NOT NULL DEFAULT 0')
    c.execute("""INSERT OR IGNORE INTO cuentas_contables(codigo,cuenta,tipo,grupo,naturaleza,acepta_movimiento,activo)
        VALUES('4190','Otros ingresos','Ingreso','Otros ingresos','Acreedora',1,1)""")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_pagos_venta_referencia ON pagos_venta(referencia) WHERE referencia IS NOT NULL AND referencia<>''")
    # Contabilidad: los encabezados agrupan las líneas del Libro Diario. Las
    # migraciones son aditivas para preservar las partidas ya existentes.
    c.execute('''CREATE TABLE IF NOT EXISTS asientos_contables(
        id INTEGER PRIMARY KEY AUTOINCREMENT,fecha TEXT NOT NULL,descripcion TEXT NOT NULL,
        referencia_tipo TEXT NOT NULL,referencia_id INTEGER NOT NULL,
        creado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(referencia_tipo,referencia_id))''')
    partida_columns={row['name'] for row in c.execute('PRAGMA table_info(partidas)')}
    if 'asiento_id' not in partida_columns:
        c.execute('ALTER TABLE partidas ADD COLUMN asiento_id INTEGER REFERENCES asientos_contables(id)')
    c.execute('''CREATE TABLE IF NOT EXISTS cuentas_por_pagar(
        id INTEGER PRIMARY KEY AUTOINCREMENT,adquisicion_id INTEGER NOT NULL UNIQUE REFERENCES adquisiciones(id) ON DELETE CASCADE,
        proveedor_id INTEGER NOT NULL REFERENCES proveedores(id),fecha TEXT NOT NULL,monto_original REAL NOT NULL,
        saldo REAL NOT NULL,estado TEXT NOT NULL DEFAULT 'Pendiente',creado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS ajustes_compra_cxp(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        vehiculo_id INTEGER NOT NULL REFERENCES vehiculos(id) ON DELETE CASCADE,
        adquisicion_id INTEGER NOT NULL REFERENCES adquisiciones(id) ON DELETE CASCADE,
        fecha TEXT NOT NULL,monto REAL NOT NULL,creado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''')
    # Las CxP de taller se conservan separadas de las adquisiciones para no
    # alterar la relación histórica obligatoria adquisicion_id de la tabla
    # original. Ambas aparecen unificadas en la cartera.
    c.execute('''CREATE TABLE IF NOT EXISTS cuentas_por_pagar_ot(
        id INTEGER PRIMARY KEY AUTOINCREMENT,orden_trabajo_id INTEGER NOT NULL UNIQUE REFERENCES ordenes_trabajo(id) ON DELETE CASCADE,
        proveedor_id INTEGER NOT NULL REFERENCES proveedores(id),fecha TEXT NOT NULL,monto_original REAL NOT NULL,
        saldo REAL NOT NULL,estado TEXT NOT NULL DEFAULT 'Pendiente',metodo_pago TEXT NOT NULL DEFAULT 'Crédito',
        banco TEXT,referencia TEXT,creado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS cuentas_por_pagar_repuestos_ot(
        id INTEGER PRIMARY KEY AUTOINCREMENT,repuesto_ot_id INTEGER NOT NULL UNIQUE REFERENCES repuestos_ot(id) ON DELETE CASCADE,
        proveedor_id INTEGER NOT NULL REFERENCES proveedores(id),fecha TEXT NOT NULL,monto_original REAL NOT NULL,
        saldo REAL NOT NULL,estado TEXT NOT NULL DEFAULT 'Pendiente',metodo_pago TEXT NOT NULL DEFAULT 'Crédito',
        banco TEXT,referencia TEXT,creado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS cuentas_por_cobrar(
        id INTEGER PRIMARY KEY AUTOINCREMENT,venta_id INTEGER NOT NULL REFERENCES ventas(id) ON DELETE CASCADE,
        financiera_id INTEGER REFERENCES proveedores(id),financiera_nombre TEXT NOT NULL,fecha TEXT NOT NULL,
        monto_original REAL NOT NULL,saldo REAL NOT NULL,estado TEXT NOT NULL DEFAULT 'Pendiente',creado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(venta_id,financiera_nombre))''')
    cxc_columns={row['name'] for row in c.execute('PRAGMA table_info(cuentas_por_cobrar)')}
    if 'financiera_cliente_id' not in cxc_columns:
        c.execute('ALTER TABLE cuentas_por_cobrar ADD COLUMN financiera_cliente_id INTEGER REFERENCES clientes(id)')
    if 'cliente_id' not in cxc_columns:
        c.execute('ALTER TABLE cuentas_por_cobrar ADD COLUMN cliente_id INTEGER REFERENCES clientes(id)')
    if 'fecha_vencimiento' not in cxc_columns:
        c.execute('ALTER TABLE cuentas_por_cobrar ADD COLUMN fecha_vencimiento TEXT')
    if 'tipo_cxc' not in cxc_columns:
        c.execute("ALTER TABLE cuentas_por_cobrar ADD COLUMN tipo_cxc TEXT NOT NULL DEFAULT 'Financiada'")
    # Aplicaciones no monetarias de una CxC.  En particular, una unidad de
    # reemplazo recibida por un cambio devuelto reduce la deuda sin simular un
    # cobro en caja o banco.
    c.execute('''CREATE TABLE IF NOT EXISTS aplicaciones_cuentas_por_cobrar(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cuenta_por_cobrar_id INTEGER NOT NULL REFERENCES cuentas_por_cobrar(id) ON DELETE CASCADE,
        fecha TEXT NOT NULL,monto REAL NOT NULL,tipo TEXT NOT NULL,referencia TEXT,
        creado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS devoluciones_cambio(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        vehiculo_devuelto_id INTEGER NOT NULL UNIQUE REFERENCES vehiculos(id),
        venta_origen_id INTEGER NOT NULL REFERENCES ventas(id),
        pago_venta_id INTEGER NOT NULL REFERENCES pagos_venta(id),
        cliente_id INTEGER REFERENCES clientes(id),proveedor_id INTEGER REFERENCES proveedores(id),
        cuenta_por_cobrar_id INTEGER NOT NULL UNIQUE REFERENCES cuentas_por_cobrar(id),
        vehiculo_reemplazo_id INTEGER UNIQUE REFERENCES vehiculos(id),
        cuenta_por_pagar_id INTEGER REFERENCES cuentas_por_pagar(id),
        fecha TEXT NOT NULL,motivo TEXT NOT NULL,monto_original REAL NOT NULL,
        monto_reemplazo REAL,estado TEXT NOT NULL DEFAULT 'CxC pendiente',
        creado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS movimientos_caja(
        id INTEGER PRIMARY KEY AUTOINCREMENT,fecha TEXT NOT NULL,tipo TEXT NOT NULL,medio TEXT NOT NULL,banco TEXT,
        entrada REAL NOT NULL DEFAULT 0,salida REAL NOT NULL DEFAULT 0,descripcion TEXT,
        referencia_tipo TEXT NOT NULL,referencia_id INTEGER NOT NULL,
        UNIQUE(referencia_tipo,referencia_id))''')
    # Gastos operativos pagados. El concepto se conserva separado del pago para
    # que el usuario pueda mantener la relación concepto <-> cuenta contable.
    c.execute('''CREATE TABLE IF NOT EXISTS conceptos_gasto(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre TEXT NOT NULL UNIQUE,
        cuenta_id INTEGER NOT NULL REFERENCES cuentas_contables(id),
        activo INTEGER NOT NULL DEFAULT 1,
        creado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''')
    # Esta forma conserva la tabla de gastos inicial que ya existía en la
    # base, ampliándola sin eliminar su historial.
    c.execute('''CREATE TABLE IF NOT EXISTS gastos_operativos(
        id INTEGER PRIMARY KEY AUTOINCREMENT, fecha TEXT NOT NULL, categoria TEXT NOT NULL,
        clasificacion TEXT, proveedor TEXT, documento TEXT, subtotal REAL NOT NULL DEFAULT 0,
        isv REAL NOT NULL DEFAULT 0, total REAL NOT NULL DEFAULT 0, forma_pago TEXT, banco TEXT,
        origen_clave TEXT UNIQUE NOT NULL, creado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''')
    gasto_columns={row['name'] for row in c.execute('PRAGMA table_info(gastos_operativos)')}
    for name, definition in [('concepto_id','INTEGER REFERENCES conceptos_gasto(id)'),('proveedor_id','INTEGER REFERENCES proveedores(id)'),('referencia','TEXT'),('observaciones','TEXT')]:
        if name not in gasto_columns:
            c.execute(f'ALTER TABLE gastos_operativos ADD COLUMN {name} {definition}')
    c.execute('CREATE INDEX IF NOT EXISTS ix_gastos_operativos_fecha ON gastos_operativos(fecha DESC,id DESC)')
    # Se proponen cuentas iniciales del catálogo actual. La asignación es
    # editable desde el maestro y no se reemplaza si el usuario ya la cambió.
    conceptos_iniciales=(
        ('Renta','6206'),('Servicios Básicos - Luz','6204'),('Servicios Básicos - Agua','6204'),
        ('Seguridad','6202'),('Planillas - Comercial','6101'),('Planillas - Operativa','6101'),
        ('Planillas - Administrativa','6201'),('Planillas - Gerencial','6201'),
        ('Planillas - Comisiones','6102'),('Insumos Consumibles','6203'),('Repuestos','6304'),
        ('Talleres','6303'),('Combustibles','6301'),('Servicios Outsourcing','6202'),
        ('Otros Gastos','7201'),('Publicidad','6401'),
    )
    for concepto, codigo in conceptos_iniciales:
        cuenta=c.execute('SELECT id FROM cuentas_contables WHERE codigo=?',(codigo,)).fetchone()
        if cuenta:
            c.execute('INSERT OR IGNORE INTO conceptos_gasto(nombre,cuenta_id) VALUES(?,?)',(concepto,cuenta['id']))
    c.execute('''CREATE TABLE IF NOT EXISTS pagos_cuentas_por_pagar(
        id INTEGER PRIMARY KEY AUTOINCREMENT,cuenta_por_pagar_id INTEGER NOT NULL REFERENCES cuentas_por_pagar(id) ON DELETE CASCADE,
        fecha TEXT NOT NULL,tipo_pago TEXT NOT NULL,banco TEXT,referencia TEXT,monto REAL NOT NULL,creado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS pagos_cuentas_por_pagar_ot(
        id INTEGER PRIMARY KEY AUTOINCREMENT,cuenta_por_pagar_ot_id INTEGER NOT NULL REFERENCES cuentas_por_pagar_ot(id) ON DELETE CASCADE,
        fecha TEXT NOT NULL,tipo_pago TEXT NOT NULL,banco TEXT,referencia TEXT,monto REAL NOT NULL,creado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS pagos_cuentas_por_pagar_repuestos_ot(
        id INTEGER PRIMARY KEY AUTOINCREMENT,cuenta_por_pagar_repuesto_id INTEGER NOT NULL REFERENCES cuentas_por_pagar_repuestos_ot(id) ON DELETE CASCADE,
        fecha TEXT NOT NULL,tipo_pago TEXT NOT NULL,banco TEXT,referencia TEXT,monto REAL NOT NULL,creado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS cobros_cuentas_por_cobrar(
        id INTEGER PRIMARY KEY AUTOINCREMENT,cuenta_por_cobrar_id INTEGER NOT NULL REFERENCES cuentas_por_cobrar(id) ON DELETE CASCADE,
        fecha TEXT NOT NULL,tipo_pago TEXT NOT NULL,banco TEXT,referencia TEXT,monto REAL NOT NULL,creado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
    # Maestro de planillas. Este registro define la composición mensual de
    # cada empleado; no genera por sí mismo pago, movimiento de caja ni asiento.
    c.execute('''CREATE TABLE IF NOT EXISTS empleados_planilla(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre TEXT NOT NULL,
        numero_identidad TEXT NOT NULL UNIQUE COLLATE NOCASE,
        fecha_ingreso TEXT,
        salario REAL NOT NULL DEFAULT 0,
        comisiones REAL NOT NULL DEFAULT 0,
        bonos REAL NOT NULL DEFAULT 0,
        ihss REAL NOT NULL DEFAULT 0,
        rap REAL NOT NULL DEFAULT 0,
        otras_deducciones REAL NOT NULL DEFAULT 0,
        departamento_id INTEGER NOT NULL REFERENCES departamentos(id),
        cuenta_contable_id INTEGER NOT NULL REFERENCES cuentas_contables(id),
        activo INTEGER NOT NULL DEFAULT 1,
        creado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        actualizado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''')
    payroll_columns={row['name'] for row in c.execute('PRAGMA table_info(empleados_planilla)')}
    if 'activo' not in payroll_columns:
        c.execute('ALTER TABLE empleados_planilla ADD COLUMN activo INTEGER NOT NULL DEFAULT 1')
    if 'fecha_ingreso' not in payroll_columns:
        c.execute('ALTER TABLE empleados_planilla ADD COLUMN fecha_ingreso TEXT')
    c.execute('CREATE INDEX IF NOT EXISTS ix_empleados_planilla_departamento ON empleados_planilla(departamento_id)')
    c.execute('CREATE INDEX IF NOT EXISTS ix_empleados_planilla_cuenta ON empleados_planilla(cuenta_contable_id)')
    # Comisiones variables y planillas generadas.  Se mantienen fuera del
    # maestro para que cada quincena conserve exactamente lo que se calculó.
    c.execute('''CREATE TABLE IF NOT EXISTS comisiones_planilla(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        empleado_id INTEGER NOT NULL REFERENCES empleados_planilla(id),
        fecha TEXT NOT NULL,
        monto REAL NOT NULL CHECK(monto>0),
        bono REAL NOT NULL DEFAULT 0 CHECK(bono>=0),
        descripcion TEXT,
        creado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''')
    payroll_commission_columns={row['name'] for row in c.execute('PRAGMA table_info(comisiones_planilla)')}
    if 'bono' not in payroll_commission_columns:
        c.execute('ALTER TABLE comisiones_planilla ADD COLUMN bono REAL NOT NULL DEFAULT 0')
    c.execute('CREATE INDEX IF NOT EXISTS ix_comisiones_planilla_empleado_fecha ON comisiones_planilla(empleado_id,fecha)')
    c.execute('''CREATE TABLE IF NOT EXISTS planillas_quincenales(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        periodo_inicio TEXT NOT NULL,
        periodo_fin TEXT NOT NULL,
        fecha_generacion TEXT NOT NULL,
        estado TEXT NOT NULL DEFAULT 'Generada',
        fecha_pago TEXT,
        banco TEXT,
        referencia_pago TEXT,
        total_salarios REAL NOT NULL DEFAULT 0,
        total_comisiones REAL NOT NULL DEFAULT 0,
        total_bonos REAL NOT NULL DEFAULT 0,
        total_deducciones REAL NOT NULL DEFAULT 0,
        total_neto REAL NOT NULL DEFAULT 0,
        creado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(periodo_inicio,periodo_fin)
    )''')
    payroll_run_columns={row['name'] for row in c.execute('PRAGMA table_info(planillas_quincenales)')}
    for name, definition in [('fecha_pago','TEXT'),('banco','TEXT'),('referencia_pago','TEXT')]:
        if name not in payroll_run_columns:
            c.execute(f'ALTER TABLE planillas_quincenales ADD COLUMN {name} {definition}')
    # Las planillas creadas antes de separar generación y pago eran solo un
    # cálculo; al no tener banco ni fecha de pago quedan en revisión.
    c.execute("UPDATE planillas_quincenales SET estado='Generada' WHERE estado='Pagada' AND fecha_pago IS NULL")
    c.execute('''CREATE TABLE IF NOT EXISTS planillas_quincenales_detalle(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        planilla_id INTEGER NOT NULL REFERENCES planillas_quincenales(id) ON DELETE CASCADE,
        empleado_id INTEGER NOT NULL REFERENCES empleados_planilla(id),
        empleado_nombre TEXT NOT NULL,
        numero_identidad TEXT NOT NULL,
        departamento_id INTEGER,
        cuenta_contable_id INTEGER,
        departamento_nombre TEXT,
        cuenta_contable_nombre TEXT,
        salario REAL NOT NULL DEFAULT 0,
        comisiones_fijas REAL NOT NULL DEFAULT 0,
        comisiones_registradas REAL NOT NULL DEFAULT 0,
        comisiones REAL NOT NULL DEFAULT 0,
        bonos REAL NOT NULL DEFAULT 0,
        ihss REAL NOT NULL DEFAULT 0,
        rap REAL NOT NULL DEFAULT 0,
        otras_deducciones REAL NOT NULL DEFAULT 0,
        total_devengado REAL NOT NULL DEFAULT 0,
        neto REAL NOT NULL DEFAULT 0
    )''')
    payroll_detail_columns={row['name'] for row in c.execute('PRAGMA table_info(planillas_quincenales_detalle)')}
    for name, definition in [('departamento_id','INTEGER'),('cuenta_contable_id','INTEGER')]:
        if name not in payroll_detail_columns:
            c.execute(f'ALTER TABLE planillas_quincenales_detalle ADD COLUMN {name} {definition}')
    c.execute('CREATE INDEX IF NOT EXISTS ix_planillas_quincenales_detalle_planilla ON planillas_quincenales_detalle(planilla_id)')
    c.execute('''CREATE TABLE IF NOT EXISTS contratos(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        numero_contrato TEXT NOT NULL UNIQUE COLLATE NOCASE,
        proveedor_id INTEGER NOT NULL REFERENCES proveedores(id),
        fecha_inicio TEXT NOT NULL,
        fecha_fin TEXT NOT NULL,
        monto_mensual REAL NOT NULL CHECK(monto_mensual>0),
        moneda TEXT NOT NULL DEFAULT 'L' CHECK(moneda IN ('L','USD')),
        dia_pago INTEGER NOT NULL CHECK(dia_pago BETWEEN 1 AND 31),
        cuenta_contable_id INTEGER NOT NULL REFERENCES cuentas_contables(id),
        activo INTEGER NOT NULL DEFAULT 1,
        creado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        actualizado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''')
    contract_columns={row['name'] for row in c.execute('PRAGMA table_info(contratos)')}
    if 'moneda' not in contract_columns:
        c.execute("ALTER TABLE contratos ADD COLUMN moneda TEXT NOT NULL DEFAULT 'L'")
    c.execute('''CREATE TABLE IF NOT EXISTS contratos_depositos_garantia(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        contrato_id INTEGER NOT NULL REFERENCES contratos(id) ON DELETE CASCADE,
        fecha TEXT NOT NULL,monto REAL NOT NULL CHECK(monto>0),
        moneda TEXT NOT NULL DEFAULT 'L' CHECK(moneda IN ('L','USD')),
        descripcion TEXT,creado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''')
    deposit_columns={row['name'] for row in c.execute('PRAGMA table_info(contratos_depositos_garantia)')}
    if 'moneda' not in deposit_columns:
        c.execute("ALTER TABLE contratos_depositos_garantia ADD COLUMN moneda TEXT NOT NULL DEFAULT 'L'")
    # Regulariza depósitos creados antes de que el módulo manejara moneda.
    # El importe no cambia: hereda la moneda del contrato al que pertenece.
    c.execute('''UPDATE contratos_depositos_garantia
        SET moneda=COALESCE((SELECT moneda FROM contratos WHERE contratos.id=contratos_depositos_garantia.contrato_id),'L')
        WHERE moneda IS NULL OR moneda<>COALESCE((SELECT moneda FROM contratos WHERE contratos.id=contratos_depositos_garantia.contrato_id),'L')''')
    c.execute('''CREATE TABLE IF NOT EXISTS cuentas_por_pagar_contratos(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        contrato_id INTEGER NOT NULL REFERENCES contratos(id) ON DELETE CASCADE,
        periodo TEXT NOT NULL,fecha TEXT NOT NULL,monto_original REAL NOT NULL,
        saldo REAL NOT NULL,estado TEXT NOT NULL DEFAULT 'Pendiente',creado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(contrato_id,periodo)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS pagos_cuentas_por_pagar_contratos(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cuenta_por_pagar_contrato_id INTEGER NOT NULL REFERENCES cuentas_por_pagar_contratos(id) ON DELETE CASCADE,
        fecha TEXT NOT NULL,tipo_pago TEXT NOT NULL,banco TEXT,referencia TEXT,monto REAL NOT NULL,
        creado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS ix_contratos_vigencia ON contratos(activo,fecha_inicio,fecha_fin)')
    c.execute('CREATE INDEX IF NOT EXISTS ix_cxp_contratos_estado ON cuentas_por_pagar_contratos(estado,fecha)')
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_pagos_cxp_referencia ON pagos_cuentas_por_pagar(referencia) WHERE referencia IS NOT NULL AND referencia<>''")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_cobros_cxc_referencia ON cobros_cuentas_por_cobrar(referencia) WHERE referencia IS NOT NULL AND referencia<>''")
    c.execute('''CREATE TABLE IF NOT EXISTS conteos_inventario(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre TEXT NOT NULL,
        fecha TEXT NOT NULL,
        estatus TEXT NOT NULL DEFAULT 'Todos',
        estado TEXT NOT NULL DEFAULT 'Abierto' CHECK(estado IN ('Abierto','Cerrado')),
        creado_por INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
        creado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        cerrado_en TEXT
    )''')
    count_columns={row['name'] for row in c.execute('PRAGMA table_info(conteos_inventario)')}
    if 'estatus' not in count_columns:
        # Los conteos creados antes de este filtro ya contienen todas las etapas.
        c.execute("ALTER TABLE conteos_inventario ADD COLUMN estatus TEXT NOT NULL DEFAULT 'Todos'")
    c.execute('''CREATE TABLE IF NOT EXISTS conteos_inventario_detalle(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conteo_id INTEGER NOT NULL REFERENCES conteos_inventario(id) ON DELETE CASCADE,
        vehiculo_id INTEGER REFERENCES vehiculos(id) ON DELETE SET NULL,
        vin TEXT NOT NULL COLLATE NOCASE,
        marca TEXT,
        modelo TEXT,
        anio INTEGER,
        color TEXT,
        existencia INTEGER NOT NULL DEFAULT 0 CHECK(existencia>=0),
        fisico INTEGER NOT NULL DEFAULT 0 CHECK(fisico>=0),
        contado_en TEXT,
        UNIQUE(conteo_id,vin)
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS ix_conteos_inventario_estado ON conteos_inventario(estado,fecha DESC)')
    c.execute('CREATE INDEX IF NOT EXISTS ix_conteos_detalle_conteo ON conteos_inventario_detalle(conteo_id,vin)')
    c.execute('''CREATE TABLE IF NOT EXISTS bitacora_auditoria(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fecha TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        usuario_id INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
        usuario TEXT NOT NULL DEFAULT 'Sistema',
        accion TEXT NOT NULL, recurso TEXT NOT NULL, recurso_id INTEGER,
        ruta TEXT NOT NULL, metodo TEXT NOT NULL,
        antes TEXT, despues TEXT, ip TEXT
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS ix_bitacora_fecha ON bitacora_auditoria(fecha DESC,id DESC)')
    c.execute('CREATE INDEX IF NOT EXISTS ix_bitacora_recurso ON bitacora_auditoria(recurso,recurso_id)')
    c.execute('CREATE INDEX IF NOT EXISTS ix_vehiculos_estado ON vehiculos(estado)')
    c.execute('CREATE INDEX IF NOT EXISTS ix_vehiculos_fecha_adquisicion ON vehiculos(fecha_adquisicion)')
    c.execute('CREATE INDEX IF NOT EXISTS ix_ordenes_trabajo_vehiculo_estado ON ordenes_trabajo(vehiculo_id,estado)')
    c.execute('CREATE INDEX IF NOT EXISTS ix_ventas_vehiculo_fecha ON ventas(vehiculo_id,fecha DESC)')
    c.execute('CREATE INDEX IF NOT EXISTS ix_adquisiciones_proveedor_fecha ON adquisiciones(proveedor_id,fecha DESC)')
    c.execute('CREATE INDEX IF NOT EXISTS ix_partidas_asiento ON partidas(asiento_id)')
    c.execute('CREATE INDEX IF NOT EXISTS ix_cxp_estado ON cuentas_por_pagar(estado)')
    c.execute('CREATE INDEX IF NOT EXISTS ix_cxc_estado ON cuentas_por_cobrar(estado)')
    # Cuenta separada para pagos adelantados de taller. No se capitaliza hasta
    # que la OT se cierre con su factura/valor final.
    c.execute('''INSERT OR IGNORE INTO cuentas_contables
        (codigo,cuenta,tipo,grupo,naturaleza,acepta_movimiento,requiere_activo,requiere_centro_costo,activo)
        VALUES('1107','Anticipos a proveedores','Activo','Activo circulante','Deudora',1,0,0,1)''')
    # Planillas: las bonificaciones se presentan y contabilizan por separado
    # de las comisiones (6102) y de los sueldos/salarios.
    c.execute('''INSERT OR IGNORE INTO cuentas_contables
        (codigo,cuenta,tipo,grupo,naturaleza,acepta_movimiento,requiere_activo,requiere_centro_costo,activo)
        VALUES('6103','Bonos al Personal','Gasto','Gasto de Venta','Deudora',1,0,0,1)''')
    sincronizar_anticipos_ot_en_kardex(c)
    reconciliar_auxiliar_cxc(c)
    c.execute("UPDATE vehiculos SET estado='DPV' WHERE estado NOT IN ('En Tránsito','Nacionalizado','En Taller','DPV','Reservado','Vendido','Devuelto')")
    if sync_history:
        sincronizar_contabilidad_historica(c)
    c.commit(); c.close()

def validate_vehiculo(data):
    if not isinstance(data, dict):
        return 'Se requiere un objeto JSON válido'
    if not (data.get('vin') or '').strip():
        return 'VIN es obligatorio'
    for key, label in [('kilometraje', 'Millaje')]:
        value=data.get(key)
        if value in (None, ''):
            continue
        try:
            if float(value) < 0:
                return f'{label} no puede ser negativo'
        except (TypeError, ValueError):
            return f'{label} debe ser numérico'
    year=data.get('anio')
    if year not in (None, ''):
        try:
            if not 1886 <= int(year) <= datetime.now().year + 1:
                return 'Año fuera de rango válido'
        except (TypeError, ValueError):
            return 'Año debe ser numérico'
    return None

def asegurar_informacion_vehiculo(c, data):
    """Conserva en el maestro cualquier combinación creada desde la ficha."""
    tipo=(data.get('tipo_vehiculo') or '').strip()
    marca=(data.get('marca') or '').strip()
    modelo=(data.get('modelo') or '').strip()
    version=(data.get('version') or '').strip()
    if tipo or marca or modelo or version:
        c.execute('''INSERT OR IGNORE INTO informacion_vehiculo(tipo_vehiculo,marca,modelo,version)
            VALUES(?,?,?,?)''',(tipo,marca,modelo,version))

def add_movimiento(c, vehiculo_id, fecha, tipo, estado_anterior=None, estado_nuevo=None,
                    ubicacion_anterior=None, ubicacion_nueva=None, referencia=None, observaciones=None):
    cursor=c.execute('''INSERT INTO movimientos_vehiculo
        (vehiculo_id,fecha,tipo,estado_anterior,estado_nuevo,ubicacion_anterior,ubicacion_nueva,referencia,observaciones)
        VALUES(?,?,?,?,?,?,?,?,?)''',
        (vehiculo_id,fecha,tipo,estado_anterior,estado_nuevo,ubicacion_anterior,ubicacion_nueva,referencia,observaciones))
    return cursor.lastrowid

def crear_orden_trabajo(c, vehiculo_id, adquisicion_id, fecha, taller, tipo_reparacion, valor_negociado, descripcion, fecha_entrega_estimada=None):
    correlativo=c.execute('SELECT COALESCE(MAX(correlativo),0)+1 FROM ordenes_trabajo').fetchone()[0]
    numero=f'OT-{correlativo:06d}'
    cur=c.execute('''INSERT INTO ordenes_trabajo(vehiculo_id,adquisicion_id,fecha,estado,detalle,correlativo,numero_ot,taller,tipo_reparacion,valor_negociado,descripcion,fecha_entrega_estimada)
        VALUES(?,?,?,'Pendiente',?,?,?,?,?,?,?,?)''',(vehiculo_id,adquisicion_id,fecha,descripcion,correlativo,numero,taller,tipo_reparacion,valor_negociado,descripcion,fecha_entrega_estimada))
    return cur.lastrowid, numero

def fecha_llegada_importacion(fecha, tipo_compra):
    """Calcula la llegada comprometida de una importación sin depender del cliente."""
    if tipo_compra!='Importación' or not fecha:
        return None
    try:
        return (datetime.strptime(str(fecha)[:10],'%Y-%m-%d')+timedelta(days=40)).strftime('%Y-%m-%d')
    except ValueError:
        raise ValueError('La fecha de adquisición debe tener el formato AAAA-MM-DD.')

def siguiente_correlativo_plataforma(c, tipo):
    current=c.execute('SELECT ultimo_numero FROM correlativos_plataforma WHERE tipo=?',(tipo,)).fetchone()
    if not current:
        c.execute('INSERT INTO correlativos_plataforma(tipo,ultimo_numero) VALUES(?,0)',(tipo,)); number=1
    else:
        number=int(current['ultimo_numero'])+1
    c.execute('UPDATE correlativos_plataforma SET ultimo_numero=? WHERE tipo=?',(number,tipo))
    return number

def costo_consolidado(c, vehiculo_id):
    """Costo real: compra ajustada/nacionalizada más costos ya incurridos."""
    vehicle=c.execute('SELECT precio_compra FROM vehiculos WHERE id=?',(vehiculo_id,)).fetchone()
    if not vehicle:
        return 0.0
    base=float(vehicle['precio_compra'] or 0)
    costing=c.execute('SELECT ajuste_compra,grua,flete,isv_pagado,cl_std,almacenaje,gastos_aduaneros FROM costos_adquisicion WHERE vehiculo_id=?',(vehiculo_id,)).fetchone()
    if costing:
        base+=sum(float(costing[key] or 0) for key in ('ajuste_compra','grua','flete','isv_pagado','cl_std','almacenaje','gastos_aduaneros'))
    extras=c.execute('SELECT COALESCE(SUM(monto),0) FROM costos_vehiculo WHERE vehiculo_id=?',(vehiculo_id,)).fetchone()[0]
    return base+float(extras or 0)

def desglose_costo_consolidado(c, vehiculo_id):
    """Entrega la trazabilidad documental de cada componente del costo."""
    vehicle=c.execute('SELECT precio_compra,fecha_adquisicion FROM vehiculos WHERE id=?',(vehiculo_id,)).fetchone()
    if not vehicle:
        return []
    purchase=c.execute('SELECT id,fecha,costo_compra FROM adquisiciones WHERE vehiculo_id=?',(vehiculo_id,)).fetchone()
    costing=c.execute('SELECT * FROM costos_adquisicion WHERE vehiculo_id=?',(vehiculo_id,)).fetchone()
    items=[]
    def add(fecha, documento, origen, concepto, monto):
        value=round(float(monto or 0),2)
        if value:
            items.append({'fecha':fecha or '', 'documento':documento, 'origen':origen,
                          'concepto':concepto, 'monto':value})
    # El consolidado toma la base desde la ficha del vehículo; el documento de
    # adquisición se usa como trazabilidad, no como una segunda fuente de monto.
    purchase_value=float(vehicle['precio_compra'] or 0)
    add((purchase['fecha'] if purchase else vehicle['fecha_adquisicion']),
        f"Adquisición #{purchase['id']}" if purchase else 'Ficha de vehículo',
        'Adquisición', 'Costo de compra', purchase_value)
    if costing:
        cost_date=(costing['fecha_actualizacion'] or '')[:10]
        for field,label in (
            ('ajuste_compra','Ajuste de compra'),('grua','Grúa'),('flete','Flete'),
            ('isv_pagado','ISV pagado'),('cl_std','CL_STD'),('almacenaje','Almacenaje'),
            ('gastos_aduaneros','Gasto aduanero')):
            add(cost_date,'Costeo de adquisición','Costeo',label,costing[field])
    rows=c.execute('SELECT * FROM costos_vehiculo WHERE vehiculo_id=? ORDER BY fecha,id',(vehiculo_id,)).fetchall()
    for row in rows:
        observation=row['observaciones'] or ''
        ot_match=re.search(r'\bOT-\d+\b',f"{row['documento'] or ''} {observation}")
        document=ot_match.group(0) if ot_match else (row['documento'] or 'Sin documento')
        origin='OT' if ot_match else ('Factura / costo adicional' if row['documento'] else 'Costo adicional')
        add(row['fecha'],document,origin,row['concepto'] or row['categoria'] or 'Costo adicional',row['monto'])
    items.sort(key=lambda item:(item['fecha'],item['documento'],item['concepto']))
    running=0.0
    for item in items:
        running=round(running+item['monto'],2)
        item['acumulado']=running
    return items

# Cuentas utilizadas por los asientos automáticos. Se resuelven por código,
# por lo que respetan el catálogo contable ya cargado por Corporación Triple AAA.
CUENTAS_AUTOMATICAS={
    'caja_bancos':'1101','cxc':'1102','inventario_dpv':'1103',
    'inventario_taller':'1104','inventario_transito':'1105',
    'cxp':'2101','anticipos_proveedores':'1107','ingresos_venta':'4101','otros_ingresos':'4190','costo_ventas':'5101'
}

def cuenta_contable_id(c, clave):
    row=c.execute('SELECT id FROM cuentas_contables WHERE codigo=? AND activo=1',(CUENTAS_AUTOMATICAS[clave],)).fetchone()
    if not row:
        raise ValueError(f"No existe la cuenta contable {CUENTAS_AUTOMATICAS[clave]}")
    return row['id']

def registrar_asiento(c, fecha, descripcion, referencia_tipo, referencia_id, lineas, vehiculo_id=None):
    """Crea un asiento balanceado una sola vez para cada documento origen."""
    existing=c.execute('SELECT id FROM asientos_contables WHERE referencia_tipo=? AND referencia_id=?',(referencia_tipo,referencia_id)).fetchone()
    if existing:
        return existing['id']
    debe=round(sum(float(line.get('debe') or 0) for line in lineas),2)
    haber=round(sum(float(line.get('haber') or 0) for line in lineas),2)
    if not lineas or abs(debe-haber)>0.01:
        raise ValueError('El asiento contable no está cuadrado')
    cur=c.execute('INSERT INTO asientos_contables(fecha,descripcion,referencia_tipo,referencia_id) VALUES(?,?,?,?)',(fecha,descripcion,referencia_tipo,referencia_id))
    asiento_id=cur.lastrowid
    for line in lineas:
        amount_debe=round(float(line.get('debe') or 0),2); amount_haber=round(float(line.get('haber') or 0),2)
        if not amount_debe and not amount_haber:
            continue
        c.execute('''INSERT INTO partidas(fecha,descripcion,referencia_tipo,referencia_id,cuenta_id,vehiculo_id,debe,haber,asiento_id)
            VALUES(?,?,?,?,?,?,?,?,?)''',(fecha,descripcion,referencia_tipo,referencia_id,line['cuenta_id'],vehiculo_id,amount_debe,amount_haber,asiento_id))
    return asiento_id

def registrar_movimiento_caja(c, fecha, tipo, medio, banco, entrada, salida, descripcion, referencia_tipo, referencia_id):
    banco=normalizar_nombre_banco(banco)
    c.execute('''INSERT OR IGNORE INTO movimientos_caja(fecha,tipo,medio,banco,entrada,salida,descripcion,referencia_tipo,referencia_id)
        VALUES(?,?,?,?,?,?,?,?,?)''',(fecha,tipo,medio,banco,round(float(entrada or 0),2),round(float(salida or 0),2),descripcion,referencia_tipo,referencia_id))

def normalizar_nombre_banco(banco):
    """Evita que un mismo banco se divida por variantes históricas del nombre."""
    nombre=(banco or '').strip()
    clave=nombre.upper()
    equivalencias={
        'BAC':'BAC Credomatic',
        'BAC CREDOMATIC':'BAC Credomatic',
        'OCCIDENTE':'Banco de Occidente',
        'BANCO OCCIDENTE':'Banco de Occidente',
        'BANCO DE OCCIDENTE':'Banco de Occidente',
    }
    return equivalencias.get(clave,nombre)

def medio_caja(tipo_pago, banco=None):
    if tipo_pago=='Efectivo':
        return 'Efectivo',None
    return 'Banco',(banco or 'Banco no especificado')

def cuenta_inventario_adquisicion(purchase):
    if purchase['tipo_compra']=='Importación':
        return 'inventario_transito'
    if int(purchase['necesita_reparacion'] or 0):
        return 'inventario_taller'
    return 'inventario_dpv'

def convertir_consignacion_en_compra(c, vehiculo_id, fecha):
    """Hace visible contablemente una consignación únicamente al venderla."""
    purchase=c.execute('SELECT * FROM adquisiciones WHERE vehiculo_id=?',(vehiculo_id,)).fetchone()
    if not purchase or purchase['tipo_compra']!='Consignación' or int(purchase['contabilizada'] or 0):
        return purchase
    # El valor pactado al recibirla es el importe que se debe al consignante.
    # No hubo pago ni inventario antes de este momento.
    c.execute('''UPDATE adquisiciones SET contabilizada=1,metodo_pago='Crédito',condicion_pago='Crédito',
        anticipo=0,saldo=costo_compra WHERE id=?''',(purchase['id'],))
    c.execute("UPDATE vehiculos SET estado='DPV' WHERE id=?",(vehiculo_id,))
    contabilizar_adquisicion(c,purchase['id'])
    add_movimiento(c,vehiculo_id,fecha,'Ingreso de consignación a inventario','DPV','DPV',
        referencia=f"Adquisición #{purchase['id']}",observaciones='Compra contabilizada al concretar la venta de consignación.')
    return c.execute('SELECT * FROM adquisiciones WHERE id=?',(purchase['id'],)).fetchone()

def cuenta_inventario_por_estado(estado):
    return {
        'En Tránsito':'inventario_transito',
        'En Taller':'inventario_taller',
        'DPV':'inventario_dpv'
    }.get(estado, 'inventario_transito')

def validar_pago_cierre_ot(data):
    """Valida el método que documenta la contrapartida al cerrar una OT."""
    metodo=(data.get('metodo_pago') or '').strip()
    banco=(data.get('banco') or '').strip()
    referencia=(data.get('referencia') or '').strip()
    if metodo not in ('Efectivo','Transferencia','Crédito'):
        return None,'Seleccione Efectivo, Transferencia o Crédito como método de pago.'
    if metodo=='Transferencia' and not banco:
        return None,'Seleccione el banco desde el cual se realizó la transferencia.'
    if metodo=='Transferencia' and not referencia:
        return None,'Ingrese el número de referencia de la transferencia.'
    return (metodo,banco or None,referencia or None),None

def validar_pago_anticipo_ot(data):
    """Un anticipo es un pago real, por eso no puede quedar como crédito."""
    metodo=(data.get('anticipo_metodo_pago') or data.get('metodo_pago') or '').strip()
    banco=(data.get('anticipo_banco') or data.get('banco') or '').strip()
    referencia=(data.get('anticipo_referencia') or data.get('referencia') or '').strip()
    if metodo not in ('Efectivo','Transferencia'):
        return None,'Seleccione Efectivo o Transferencia para el anticipo de la OT.'
    if metodo=='Transferencia' and not banco:
        return None,'Seleccione el banco desde el cual se realizó el anticipo.'
    # La referencia ayuda a auditar, pero no se fuerza: en efectivo no existe
    # y algunos bancos no la entregan al momento del pago.
    return (metodo,banco or None,referencia or None),None

def total_anticipos_ot(c, orden_trabajo_id):
    return round(float(c.execute('SELECT COALESCE(SUM(monto),0) FROM anticipos_ot WHERE orden_trabajo_id=?',(orden_trabajo_id,)).fetchone()[0] or 0),2)

def registrar_movimiento_kardex_anticipo_ot(c, order, advance_id, monto, metodo_pago, banco, referencia, fecha):
    """Da trazabilidad al anticipo sin convertirlo en costo del vehículo."""
    vehicle=c.execute('SELECT estado,ubicacion FROM vehiculos WHERE id=?',(order['vehiculo_id'],)).fetchone()
    if not vehicle:
        return
    movement_reference=f"{order['numero_ot'] or 'OT'} · Anticipo #{advance_id}"
    if c.execute('SELECT 1 FROM movimientos_vehiculo WHERE vehiculo_id=? AND referencia=?',
                 (order['vehiculo_id'],movement_reference)).fetchone():
        return
    payment_detail=f'Método: {metodo_pago}.'
    if banco: payment_detail+=f' Banco: {banco}.'
    if referencia: payment_detail+=f' Referencia bancaria: {referencia}.'
    add_movimiento(c,order['vehiculo_id'],fecha,'Anticipo de OT (informativo)',
                   vehicle['estado'],vehicle['estado'],vehicle['ubicacion'],vehicle['ubicacion'],
                   referencia=movement_reference,
                   observaciones=f'Anticipo registrado: {float(monto):.2f}. {payment_detail} No se incorpora al costo hasta el cierre de la OT.')

def sincronizar_anticipos_ot_en_kardex(c):
    """Completa la trazabilidad de anticipos registrados antes de esta mejora."""
    rows=c.execute('''SELECT ao.*,o.vehiculo_id,o.numero_ot FROM anticipos_ot ao
        JOIN ordenes_trabajo o ON o.id=ao.orden_trabajo_id ORDER BY ao.id''').fetchall()
    for row in rows:
        registrar_movimiento_kardex_anticipo_ot(c,row,row['id'],row['monto'],row['metodo_pago'],
                                                row['banco'],row['referencia'],row['fecha'])

def reconciliar_auxiliar_cxc(c):
    """Alinea la cartera con los cobros que ya poseen soporte contable.

    Una versión anterior podía registrar el cobro y su asiento, pero dejar el
    saldo del auxiliar intacto.  Aquí no se crean pólizas ni movimientos: se
    recalcula cada saldo únicamente a partir de los cobros ya persistidos.
    """
    rows=c.execute('''SELECT cc.id,cc.monto_original,cc.saldo,cc.estado,
        COALESCE((SELECT SUM(co.monto) FROM cobros_cuentas_por_cobrar co
                  WHERE co.cuenta_por_cobrar_id=cc.id),0) cobrado,
        COALESCE((SELECT SUM(ap.monto) FROM aplicaciones_cuentas_por_cobrar ap
                  WHERE ap.cuenta_por_cobrar_id=cc.id),0) aplicado
        FROM cuentas_por_cobrar cc''').fetchall()
    for row in rows:
        expected=max(round(float(row['monto_original'] or 0)-float(row['cobrado'] or 0)-float(row['aplicado'] or 0),2),0)
        estado='Cobrada' if expected<=0.01 else 'Pendiente'
        if abs(float(row['saldo'] or 0)-expected)>0.01 or row['estado']!=estado:
            c.execute('UPDATE cuentas_por_cobrar SET saldo=?,estado=? WHERE id=?',
                      (expected,estado,row['id']))

def registrar_anticipo_ot(c, order, monto, metodo_pago, banco, referencia, fecha=None):
    """Registra el pago adelantado sin convertirlo aún en costo del vehículo."""
    value=round(float(monto or 0),2)
    if value<=0:
        return None
    fecha=fecha or datetime.now().strftime('%Y-%m-%d')
    cur=c.execute('''INSERT INTO anticipos_ot(orden_trabajo_id,fecha,monto,metodo_pago,banco,referencia)
        VALUES(?,?,?,?,?,?)''',(order['id'],fecha,value,metodo_pago,banco,referencia))
    advance_id=cur.lastrowid
    description=f"Anticipo {order['numero_ot'] or 'OT'} · {order['taller'] or 'Taller'}"
    medio,banco_caja=medio_caja(metodo_pago,banco)
    registrar_movimiento_caja(c,fecha,'Anticipo de OT',medio,banco_caja,0,value,description,'anticipo_ot',advance_id)
    registrar_asiento(c,fecha,description,'anticipo_ot',advance_id,[
        {'cuenta_id':cuenta_contable_id(c,'anticipos_proveedores'),'debe':value},
        {'cuenta_id':cuenta_contable_id(c,'caja_bancos'),'haber':value},
    ],order['vehiculo_id'])
    registrar_movimiento_kardex_anticipo_ot(c,order,advance_id,value,metodo_pago,banco,referencia,fecha)
    return advance_id

def contabilizar_cierre_ot(c, order, provider, valor_final, metodo_pago, banco, referencia, fecha):
    """Capitaliza el cierre, aplica anticipos y deja solo el saldo real en CxP."""
    value=round(float(valor_final or 0),2)
    if not value:
        return
    vehicle=c.execute('SELECT estado FROM vehiculos WHERE id=?',(order['vehiculo_id'],)).fetchone()
    inventory=cuenta_inventario_por_estado(vehicle['estado'] if vehicle else 'En Taller')
    description=f"Cierre {order['numero_ot'] or 'OT'} · {provider['nombre']}"
    advances=min(total_anticipos_ot(c,order['id']),value)
    remaining=round(value-advances,2)
    lines=[{'cuenta_id':cuenta_contable_id(c,inventory),'debe':value},
           {'cuenta_id':cuenta_contable_id(c,'cxp'),'haber':value}]
    if advances:
        lines.extend([
            {'cuenta_id':cuenta_contable_id(c,'cxp'),'debe':advances},
            {'cuenta_id':cuenta_contable_id(c,'anticipos_proveedores'),'haber':advances},
        ])
    if metodo_pago!='Crédito' and remaining:
        lines.extend([
            {'cuenta_id':cuenta_contable_id(c,'cxp'),'debe':remaining},
            {'cuenta_id':cuenta_contable_id(c,'caja_bancos'),'haber':remaining},
        ])
        medio,banco_caja=medio_caja(metodo_pago,banco)
        registrar_movimiento_caja(c,fecha,'Pago de saldo OT',medio,banco_caja,0,remaining,description,'pago_ot',order['id'])
    if remaining and metodo_pago=='Crédito':
        c.execute('''INSERT INTO cuentas_por_pagar_ot(orden_trabajo_id,proveedor_id,fecha,monto_original,saldo,estado,metodo_pago,banco,referencia)
            VALUES(?,?,?,?,?,'Pendiente',?,?,?)''',(order['id'],provider['id'],fecha,remaining,remaining,metodo_pago,banco,referencia))
    registrar_asiento(c,fecha,description,'cierre_ot',order['id'],lines,order['vehiculo_id'])

def validar_pago_repuesto(data):
    metodo=(data.get('metodo_pago') or '').strip()
    banco=(data.get('banco') or '').strip()
    referencia=(data.get('referencia_pago') or '').strip()
    if metodo not in ('Efectivo','Transferencia','Crédito'):
        return None,'Seleccione Efectivo, Transferencia o Crédito como método de pago.'
    if metodo=='Transferencia' and not banco:
        return None,'Seleccione el banco desde el cual se realizó la transferencia.'
    # La factura es el documento obligatorio de esta compra; una referencia
    # bancaria adicional puede registrarse, pero no se fuerza para el repuesto.
    return (metodo,banco or None,referencia or None),None

def contabilizar_repuesto_ot(c, part_id, order, provider, total, metodo_pago, banco, referencia, fecha, factura, descripcion):
    value=round(float(total or 0),2)
    if not value:
        return
    vehicle=c.execute('SELECT estado FROM vehiculos WHERE id=?',(order['vehiculo_id'],)).fetchone()
    inventory=cuenta_inventario_por_estado(vehicle['estado'] if vehicle else 'En Taller')
    description_text=f"Repuesto {factura} · {provider['nombre']}"
    lines=[{'cuenta_id':cuenta_contable_id(c,inventory),'debe':value}]
    if metodo_pago=='Crédito':
        lines.append({'cuenta_id':cuenta_contable_id(c,'cxp'),'haber':value})
        c.execute('''INSERT INTO cuentas_por_pagar_repuestos_ot(repuesto_ot_id,proveedor_id,fecha,monto_original,saldo,estado,metodo_pago,banco,referencia)
            VALUES(?,?,?,?,?,'Pendiente',?,?,?)''',(part_id,provider['id'],fecha,value,value,metodo_pago,banco,referencia))
    else:
        lines.append({'cuenta_id':cuenta_contable_id(c,'caja_bancos'),'haber':value})
        medio,banco_caja=medio_caja(metodo_pago,banco)
        registrar_movimiento_caja(c,fecha,'Pago de repuesto OT',medio,banco_caja,0,value,description_text,'pago_repuesto_ot',part_id)
    registrar_asiento(c,fecha,description_text,'repuesto_ot',part_id,lines,order['vehiculo_id'])

def asegurar_proveedor_desde_cliente(c, cliente):
    """Convierte en proveedor al cliente que entrega una unidad como cambio."""
    nombre=(cliente.get('nombre') or '').strip()
    if not nombre:
        raise ValueError('El cliente que entrega el vehículo en cambio debe tener nombre')
    existente=c.execute('SELECT id FROM proveedores WHERE nombre=? COLLATE NOCASE',(nombre,)).fetchone()
    valores=(
        (cliente.get('identidad') or '').strip() or None,
        (cliente.get('rtn') or '').strip() or None,
        (cliente.get('telefono') or '').strip() or None,
        (cliente.get('email') or '').strip() or None,
        (cliente.get('direccion') or '').strip() or None,
    )
    if existente:
        # Se completan únicamente campos vacíos: nunca se reemplaza la ficha
        # de un proveedor existente con datos distintos por accidente.
        c.execute('''UPDATE proveedores SET identidad=COALESCE(NULLIF(identidad,''),?),
            rtn=COALESCE(NULLIF(rtn,''),?),telefono=COALESCE(NULLIF(telefono,''),?),email=COALESCE(NULLIF(email,''),?),
            direccion=COALESCE(NULLIF(direccion,''),?) WHERE id=?''',(*valores,existente['id']))
        return existente['id']
    cur=c.execute('''INSERT INTO proveedores(nombre,identidad,rtn,telefono,email,direccion,condicion_pago,activo,tipo)
        VALUES(?,?,?,?,?,?,'Contado',1,'Proveedor')''',(nombre,*valores))
    return cur.lastrowid

def registrar_vehiculo_recibido_cambio(c, cambio, fecha, cliente, numero_venta):
    """Registra la unidad recibida como parte de pago, sin movimiento de caja.

    La contrapartida contable de esta adquisición es el ingreso de la venta
    original; por eso no se genera una CxP ni se contabiliza como compra normal.
    """
    if not isinstance(cambio, dict):
        raise ValueError('Complete la información del vehículo recibido en cambio')
    vin=(cambio.get('vin') or '').strip().upper()
    placa=(cambio.get('placa') or '').strip().upper()
    try:
        costo=round(float(cambio.get('costo_compra')),2)
    except (TypeError, ValueError):
        raise ValueError('El valor de adquisición del cambio es obligatorio')
    if not vin:
        raise ValueError('El VIN del vehículo recibido en cambio es obligatorio')
    if not placa:
        raise ValueError('La placa del vehículo recibido en cambio es obligatoria')
    if costo<=0:
        raise ValueError('El valor de adquisición del cambio debe ser mayor que cero')
    if c.execute('SELECT 1 FROM vehiculos WHERE vin=?',(vin,)).fetchone():
        raise ValueError('El VIN del vehículo recibido en cambio ya está registrado')
    if not placa_disponible(c,placa):
        raise ValueError('La placa del vehículo recibido en cambio ya está registrada')
    proveedor_id=asegurar_proveedor_desde_cliente(c,cliente)
    proveedor=c.execute('SELECT * FROM proveedores WHERE id=? AND activo=1',(proveedor_id,)).fetchone()
    if not proveedor:
        raise ValueError('No fue posible preparar al cliente como proveedor del vehículo recibido')
    for key,label in [('tipo_vehiculo','Tipo de vehículo'),('marca','Marca'),('modelo','Modelo')]:
        if not (cambio.get(key) or '').strip():
            raise ValueError(f'{label} es obligatorio para el vehículo recibido en cambio')
    anio=cambio.get('anio')
    try:
        anio=int(anio)
        if not 1886 <= anio <= datetime.now().year+1: raise ValueError
    except (TypeError, ValueError):
        raise ValueError('Ingrese un año válido para el vehículo recibido en cambio')
    cur=c.execute('''INSERT INTO vehiculos
        (vin,lote,tipo_vehiculo,marca,modelo,version,color,anio,kilometraje,placa,estado,ubicacion,precio_compra,precio_venta,fecha_adquisicion,proveedor,tipo_compra,observaciones)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?,'Cambio',?)''',(
        vin,(cambio.get('lote') or '').strip() or None,(cambio.get('tipo_vehiculo') or '').strip(),
        (cambio.get('marca') or '').strip(),(cambio.get('modelo') or '').strip(),(cambio.get('version') or '').strip() or None,
        (cambio.get('color') or '').strip() or None,anio,cambio.get('kilometraje') or None,placa,'En Tránsito',
        costo,0,fecha,proveedor['nombre'],
        f"Vehículo recibido como cambio del cliente {cliente['nombre']}. Venta {numero_venta}. {(cambio.get('observaciones') or '').strip()}".strip()))
    vehiculo_id=cur.lastrowid
    asegurar_informacion_vehiculo(c,cambio)
    cur=c.execute('''INSERT INTO adquisiciones(vehiculo_id,proveedor_id,fecha,costo_compra,anticipo,metodo_pago,condicion_pago,dias_credito,saldo,observaciones,necesita_reparacion,tipo_compra)
        VALUES(?,?,?,?,?,'Cambio','Contado',0,0,?,0,'Cambio')''',
        (vehiculo_id,proveedor_id,fecha,costo,costo,f"Adquisición por cambio vinculada a venta {numero_venta}"))
    add_movimiento(c,vehiculo_id,fecha,'Ingreso por cambio',None,'En Tránsito',None,None,
        referencia=numero_venta,observaciones=f"Valor de adquisición aplicado como pago: {costo:.2f}")
    return vehiculo_id, cur.lastrowid, costo

def contabilizar_adquisicion(c, adquisicion_id):
    purchase=c.execute('''SELECT a.*,p.nombre proveedor_nombre,v.estado FROM adquisiciones a
        JOIN proveedores p ON p.id=a.proveedor_id JOIN vehiculos v ON v.id=a.vehiculo_id WHERE a.id=?''',(adquisicion_id,)).fetchone()
    if not purchase:
        return
    if purchase['tipo_compra']=='Consignación' and not int(purchase['contabilizada'] or 0):
        return
    total=float(purchase['costo_compra'] or 0); anticipo=float(purchase['anticipo'] or 0); saldo=round(total-anticipo,2)
    inventory_key=cuenta_inventario_adquisicion(purchase)
    lineas=[{'cuenta_id':cuenta_contable_id(c,inventory_key),'debe':total}]
    if anticipo:
        lineas.append({'cuenta_id':cuenta_contable_id(c,'caja_bancos'),'haber':anticipo})
        medio,banco=medio_caja(purchase['metodo_pago'],purchase['banco'])
        registrar_movimiento_caja(c,purchase['fecha'],'Anticipo de compra',medio,banco,0,anticipo,f"Adquisición a {purchase['proveedor_nombre']}",'adquisicion_anticipo',purchase['id'])
    if saldo:
        lineas.append({'cuenta_id':cuenta_contable_id(c,'cxp'),'haber':saldo})
        c.execute('''INSERT INTO cuentas_por_pagar(adquisicion_id,proveedor_id,fecha,monto_original,saldo,estado)
            VALUES(?,?,?,?,?,?) ON CONFLICT(adquisicion_id) DO UPDATE SET monto_original=excluded.monto_original,saldo=excluded.saldo,estado=excluded.estado''',
            (purchase['id'],purchase['proveedor_id'],purchase['fecha'],saldo,saldo,'Pendiente'))
    registrar_asiento(c,purchase['fecha'],f"Adquisición de vehículo · {purchase['proveedor_nombre']}",'adquisicion',purchase['id'],lineas,purchase['vehiculo_id'])

def contabilizar_ajuste_compra(c, acquisition, vehicle, ajuste_actual, ajuste_contabilizado, fecha):
    """Capitaliza solo la diferencia del ajuste y la deja en la CxP original.

    El campo de costeo conserva el ajuste total; esta función compara contra el
    importe ya contabilizado para evitar que cada guardado duplique la deuda.
    """
    diferencia=round(float(ajuste_actual or 0)-float(ajuste_contabilizado or 0),2)
    if abs(diferencia)<0.005:
        return
    if acquisition['tipo_compra']=='Consignación' and not int(acquisition['contabilizada'] or 0):
        raise ValueError('La consignación no puede generar CxP hasta que se convierta en compra.')
    if vehicle['estado']=='Vendido':
        raise ValueError('No se puede ajustar una compra de un vehículo vendido.')

    cuenta=c.execute('SELECT * FROM cuentas_por_pagar WHERE adquisicion_id=?',(acquisition['id'],)).fetchone()
    if cuenta:
        monto_original=round(float(cuenta['monto_original'] or 0)+diferencia,2)
        saldo=round(float(cuenta['saldo'] or 0)+diferencia,2)
        if monto_original<-0.01 or saldo<-0.01:
            raise ValueError('No puede reducir el ajuste por debajo del saldo ya pagado de la CxP.')
        estado='Pagada' if saldo<=0.01 else 'Pendiente'
        c.execute('UPDATE cuentas_por_pagar SET monto_original=?,saldo=?,estado=? WHERE id=?',
            (max(monto_original,0),max(saldo,0),estado,cuenta['id']))
    elif diferencia>0:
        c.execute('''INSERT INTO cuentas_por_pagar(adquisicion_id,proveedor_id,fecha,monto_original,saldo,estado)
            VALUES(?,?,?,?,?,'Pendiente')''',(acquisition['id'],acquisition['proveedor_id'],fecha,diferencia,diferencia))
    else:
        raise ValueError('No existe una CxP para disminuir el ajuste de compra.')

    cur=c.execute('INSERT INTO ajustes_compra_cxp(vehiculo_id,adquisicion_id,fecha,monto) VALUES(?,?,?,?)',
        (vehicle['id'],acquisition['id'],fecha,diferencia))
    inventory=cuenta_inventario_por_estado(vehicle['estado'])
    provider=c.execute('SELECT nombre FROM proveedores WHERE id=?',(acquisition['proveedor_id'],)).fetchone()
    provider_name=(provider['nombre'] if provider else 'Proveedor')
    description=f'Ajuste de compra · {provider_name}'
    if diferencia>0:
        lineas=[{'cuenta_id':cuenta_contable_id(c,inventory),'debe':diferencia},
                {'cuenta_id':cuenta_contable_id(c,'cxp'),'haber':diferencia}]
    else:
        importe=abs(diferencia)
        lineas=[{'cuenta_id':cuenta_contable_id(c,'cxp'),'debe':importe},
                {'cuenta_id':cuenta_contable_id(c,inventory),'haber':importe}]
    registrar_asiento(c,fecha,description,'ajuste_compra_cxp',cur.lastrowid,lineas,vehicle['id'])

def contabilizar_venta(c, venta_id, inventario_origen=None):
    sale=c.execute('''SELECT ve.*,cl.nombre cliente_nombre,v.estado vehiculo_estado FROM ventas ve
        JOIN vehiculos v ON v.id=ve.vehiculo_id
        LEFT JOIN clientes cl ON cl.id=ve.cliente_id WHERE ve.id=?''',(venta_id,)).fetchone()
    if not sale:
        return
    payments=c.execute('SELECT * FROM pagos_venta WHERE venta_id=? ORDER BY id',(venta_id,)).fetchall()
    price=float(sale['precio'] or 0); lines=[]
    for payment in payments:
        amount=float(payment['monto'] or 0)
        if payment['tipo_pago'] in ('Financiado','Pagaré'):
            lines.append({'cuenta_id':cuenta_contable_id(c,'cxc'),'debe':amount})
            name=(payment['financiera_nombre'] if payment['tipo_pago']=='Financiado' else sale['cliente_nombre']) or 'Cliente no especificado'
            c.execute('''INSERT INTO cuentas_por_cobrar(venta_id,financiera_id,financiera_cliente_id,cliente_id,financiera_nombre,fecha,fecha_vencimiento,monto_original,saldo,estado)
                VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(venta_id,financiera_nombre) DO UPDATE SET monto_original=excluded.monto_original,saldo=excluded.saldo,estado=excluded.estado,financiera_cliente_id=excluded.financiera_cliente_id,cliente_id=excluded.cliente_id,fecha_vencimiento=excluded.fecha_vencimiento''',
                (sale['id'],payment['financiera_id'],payment['financiera_cliente_id'],sale['cliente_id'] if payment['tipo_pago']=='Pagaré' else None,name,sale['fecha'],payment['fecha_vencimiento'],amount,amount,'Pendiente'))
        elif payment['tipo_pago']=='Cambio':
            received=c.execute('SELECT estado FROM vehiculos WHERE id=?',(payment['vehiculo_recibido_id'],)).fetchone()
            if not received:
                raise ValueError('No se encontró el vehículo recibido como cambio')
            lines.append({'cuenta_id':cuenta_contable_id(c,cuenta_inventario_por_estado(received['estado'])),'debe':amount})
        else:
            lines.append({'cuenta_id':cuenta_contable_id(c,'caja_bancos'),'debe':amount})
            medio,banco=medio_caja(payment['tipo_pago'],payment['banco'])
            registrar_movimiento_caja(c,payment['fecha'],'Cobro de venta',medio,banco,amount,0,f"Venta {sale['numero_transaccion'] or sale['factura']}",'pago_venta',payment['id'])
    if price:
        lines.append({'cuenta_id':cuenta_contable_id(c,'ingresos_venta'),'haber':price})
    registrar_asiento(c,sale['fecha'],f"Venta de vehículo · {sale['numero_transaccion'] or sale['factura']}",'venta_ingreso',sale['id'],lines,sale['vehiculo_id'])
    cost=costo_consolidado(c,sale['vehiculo_id'])
    if cost:
        # El costo debe salir de la cuenta donde la unidad estaba al facturarse,
        # no de su tipo de compra original. Una importación ya nacionalizada y
        # DPV, por ejemplo, se mantiene en Inventario DPV hasta su venta.
        # Al facturar, la etapa del vehículo ya cambió a "Vendido". Por eso
        # el origen se recibe desde el flujo de venta, antes de ese cambio.
        inventory_key=cuenta_inventario_por_estado(inventario_origen or sale['vehiculo_estado'])
        lines_cost=[{'cuenta_id':cuenta_contable_id(c,'costo_ventas'),'debe':cost},{'cuenta_id':cuenta_contable_id(c,inventory_key),'haber':cost}]
        registrar_asiento(c,sale['fecha'],f"Costo de venta · {sale['numero_transaccion'] or sale['factura']}",'venta_costo',sale['id'],lines_cost,sale['vehiculo_id'])
    fee=round(float(sale['fee_administrativo_monto'] or 0),2)
    if fee:
        consignacion=c.execute('''SELECT a.proveedor_id,p.nombre proveedor_nombre FROM adquisiciones a
            JOIN proveedores p ON p.id=a.proveedor_id WHERE a.vehiculo_id=? AND a.tipo_compra='Consignación' ''',(sale['vehiculo_id'],)).fetchone()
        if not consignacion:
            raise ValueError('No se encontró el proveedor consignante para registrar el fee administrativo')
        label=f"Fee administrativo · {consignacion['proveedor_nombre']}"
        c.execute('''INSERT INTO cuentas_por_cobrar(venta_id,financiera_id,financiera_nombre,fecha,monto_original,saldo,estado)
            VALUES(?,?,?,?,?,?,?)''',(sale['id'],consignacion['proveedor_id'],label,sale['fecha'],fee,fee,'Pendiente'))
        registrar_asiento(c,sale['fecha'],f"Fee administrativo de consignación · {sale['numero_transaccion'] or sale['factura']}",
            'consignacion_fee',sale['id'],[
                {'cuenta_id':cuenta_contable_id(c,'cxc'),'debe':fee},
                {'cuenta_id':cuenta_contable_id(c,'otros_ingresos'),'haber':fee},
            ],sale['vehiculo_id'])

def corregir_costo_ventas_desde_transito(c):
    """Reclasifica ventas históricas que usaron Tránsito por una etapa vendida.

    Antes se determinaba la cuenta después de marcar la unidad como Vendida y
    el valor por defecto terminaba siendo 1105. Se corrigen solo pólizas de
    costo de venta cuyo movimiento de venta conserva una etapa anterior válida.
    """
    transito_id=cuenta_contable_id(c,'inventario_transito')
    rows=c.execute('''SELECT p.id partida_id,ve.vehiculo_id,ve.factura
        FROM partidas p
        JOIN asientos_contables a ON a.id=p.asiento_id
        JOIN ventas ve ON ve.id=a.referencia_id
        WHERE a.referencia_tipo='venta_costo' AND p.cuenta_id=? AND p.haber>0''',(transito_id,)).fetchall()
    for row in rows:
        movement=c.execute('''SELECT estado_anterior FROM movimientos_vehiculo
            WHERE vehiculo_id=? AND tipo='Venta facturada' AND referencia=?
            ORDER BY id DESC LIMIT 1''',(row['vehiculo_id'],row['factura'])).fetchone()
        origin=movement['estado_anterior'] if movement else None
        if origin not in ('DPV','En Taller'):
            continue
        target=cuenta_contable_id(c,cuenta_inventario_por_estado(origin))
        c.execute('UPDATE partidas SET cuenta_id=? WHERE id=?',(target,row['partida_id']))

def sincronizar_contabilidad_historica(c):
    """Genera una vez los asientos y saldos para documentos existentes."""
    # Las adquisiciones históricas migradas ya tienen una póliza propia contra
    # CxP proveedores; no deben volver a contabilizarse como CxP del auxiliar.
    for row in c.execute("""SELECT id FROM adquisiciones
        WHERE COALESCE(observaciones,'') NOT LIKE 'Migración histórica.%'
          AND COALESCE(metodo_pago,'')<>'Cambio'
        ORDER BY id""").fetchall():
        contabilizar_adquisicion(c,row['id'])
    for row in c.execute('SELECT id,vehiculo_id,factura FROM ventas ORDER BY id').fetchall():
        movimiento=c.execute('''SELECT estado_anterior FROM movimientos_vehiculo
            WHERE vehiculo_id=? AND tipo='Venta facturada' AND referencia=?
            ORDER BY id DESC LIMIT 1''',(row['vehiculo_id'],row['factura'])).fetchone()
        origen=movimiento['estado_anterior'] if movimiento else None
        contabilizar_venta(c,row['id'],origen)

# Gunicorn importa ``server:app`` y no ejecuta el bloque __main__.  Las
# migraciones deben correr durante la importación para que un despliegue nuevo
# use el mismo esquema que el entorno local. Todas son idempotentes.
init_db(sync_history=False)
correction_connection=db()
corregir_costo_ventas_desde_transito(correction_connection)
correction_connection.commit()
correction_connection.close()
init_access_control_schema()

def recalcular_estado(c, vehiculo_id, motivo='Actualización automática de etapa'):
    """Calcula las únicas etapas permitidas sin aceptar cambios manuales de estado."""
    vehicle=c.execute('SELECT estado,ubicacion FROM vehiculos WHERE id=?',(vehiculo_id,)).fetchone()
    if not vehicle: return None
    # Una devolución de cambio es una baja formal; ningún cierre de OT o
    # actualización posterior debe reingresar la unidad al inventario.
    if vehicle['estado']=='Devuelto':
        return 'Devuelto'
    sale=c.execute('SELECT * FROM ventas WHERE vehiculo_id=? ORDER BY id DESC LIMIT 1',(vehiculo_id,)).fetchone()
    purchase=c.execute('SELECT tipo_compra,contabilizada FROM adquisiciones WHERE vehiculo_id=?',(vehiculo_id,)).fetchone()
    pending=c.execute("SELECT COUNT(*) FROM ordenes_trabajo WHERE vehiculo_id=? AND estado='Pendiente'",(vehiculo_id,)).fetchone()[0]
    costing=c.execute('SELECT estatus FROM costos_adquisicion WHERE vehiculo_id=?',(vehiculo_id,)).fetchone()
    if sale and ((sale['factura'] or '').strip() or sale['estado'] in ('Facturada','Vendido')):
        next_status='Vendido'
    elif sale and float(sale['prima'] or 0)>0:
        next_status='Reservado'
    elif pending:
        next_status='En Taller'
    elif purchase and 'import' in (purchase['tipo_compra'] or '').lower():
        if not costing or costing['estatus']!='Nacionalizado':
            next_status='En Tránsito'
        else:
            # Una importación nacionalizada queda DPV cuando no tiene una OT abierta.
            next_status='DPV'
    else:
        next_status='DPV'
    if next_status != vehicle['estado']:
        c.execute('UPDATE vehiculos SET estado=? WHERE id=?',(next_status,vehiculo_id))
        move_id=add_movimiento(c,vehiculo_id,datetime.now().strftime('%Y-%m-%d'),'Cambio automático de etapa',vehicle['estado'],next_status,vehicle['ubicacion'],vehicle['ubicacion'],observaciones=motivo)
        # La consignación solo se visualiza: aun si cambia de etapa, no debe
        # reclasificar cuentas de inventario hasta que se convierta en compra.
        if purchase and purchase['tipo_compra']=='Consignación' and not int(purchase['contabilizada'] or 0):
            return next_status
        old_inventory=cuenta_inventario_por_estado(vehicle['estado'])
        new_inventory=cuenta_inventario_por_estado(next_status)
        # El traslado entre etapas no modifica el costo: solo reclasifica el
        # activo para que Taller, Tránsito y DPV reflejen dónde está la unidad.
        if next_status!='Vendido' and old_inventory!=new_inventory:
            value=round(costo_consolidado(c,vehiculo_id),2)
            if value:
                registrar_asiento(c,datetime.now().strftime('%Y-%m-%d'),f'Reclasificación de inventario · {vehicle["estado"]} a {next_status}','traslado_inventario',move_id,[
                    {'cuenta_id':cuenta_contable_id(c,new_inventory),'debe':value},
                    {'cuenta_id':cuenta_contable_id(c,old_inventory),'haber':value},
                ],vehiculo_id)
    return next_status

@app.route('/setup',methods=['GET','POST'])
def setup():
    if user_count(): return redirect(url_for('login'))
    error=None
    if request.method=='POST':
        username=(request.form.get('username') or '').strip()
        password=request.form.get('password') or ''
        if len(username)<3: error='El usuario debe tener al menos 3 caracteres.'
        elif len(password)<8: error='La contraseña debe tener al menos 8 caracteres.'
        else:
            c=db(); c.execute('INSERT INTO usuarios(username,password_hash,nombre,email,rol) VALUES(?,?,?,?,?)',
                (username,generate_password_hash(password, method='pbkdf2:sha256'),username,f'{username}@local','Administrador'))
            user_id=c.execute('SELECT id FROM usuarios WHERE username=?',(username,)).fetchone()[0]; c.commit(); c.close()
            session.clear(); session['user_id']=user_id; session['username']=username; session['sesion_version']=1; session['last_activity']=datetime.now().timestamp()
            return redirect(url_for('home'))
    return render_template_string(LOGIN_TEMPLATE,title='Crear administrador',subtitle='Configure el primer acceso seguro al sistema.',button='Crear acceso',setup=True,error=error)

@app.route('/login',methods=['GET','POST'])
def login():
    if not user_count(): return redirect(url_for('setup'))
    if session.get('user_id'): return redirect(url_for('home'))
    error=None
    if request.method=='POST':
        username=(request.form.get('username') or '').strip(); password=request.form.get('password') or ''
        c=db(); user=c.execute('SELECT * FROM usuarios WHERE username=? AND activo=1',(username,)).fetchone()
        if user and check_password_hash(user['password_hash'],password):
            c.execute('UPDATE usuarios SET ultimo_acceso=CURRENT_TIMESTAMP WHERE id=?',(user['id'],)); c.commit(); c.close()
            session.clear(); session['user_id']=user['id']; session['username']=user['username']; session['sesion_version']=int(user['sesion_version'] or 1); session['last_activity']=datetime.now().timestamp()
            return redirect(url_for('home'))
        c.close(); error='Usuario o contraseña incorrectos.'
    return render_template_string(LOGIN_TEMPLATE,title='Iniciar sesión',subtitle='Ingrese sus credenciales para acceder al sistema.',button='Ingresar',setup=False,error=error)

@app.post('/logout')
def logout():
    session.clear(); return jsonify(ok=True)

@app.get('/respaldo/sistema.zip')
def download_system_backup():
    """Entrega código y una copia consistente, recuperable y legible de los datos."""
    configured_token=os.environ.get('BACKUP_TOKEN','')
    header=request.headers.get('Authorization','')
    supplied_token=header[7:] if header.startswith('Bearer ') else ''
    if not configured_token:
        return jsonify(error='El respaldo local aún no está configurado.'),503
    if not supplied_token or not hmac.compare_digest(supplied_token,configured_token):
        return jsonify(error='No autorizado.'),401
    created=datetime.now().astimezone().isoformat()
    with tempfile.TemporaryDirectory(prefix='aaa-backup-') as temporary_directory:
        database_copy=os.path.join(temporary_directory,'autolote.sqlite')
        source=sqlite3.connect(DB); target=sqlite3.connect(database_copy)
        source.backup(target); target.close(); source.close()
        content=io.BytesIO(); files=['datos/autolote.sqlite']
        with zipfile.ZipFile(content,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
            archive.write(database_copy,'datos/autolote.sqlite')
            # Además de la base SQLite (la fuente completa y recuperable), se
            # exporta cada tabla a CSV UTF-8 para abrirla directamente en Excel.
            data_connection=sqlite3.connect(database_copy)
            tables=[row[0] for row in data_connection.execute("""
                SELECT name FROM sqlite_master
                WHERE type='table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
            """)]
            table_summary=[]
            for table in tables:
                quoted_table='"'+table.replace('"','""')+'"'
                cursor=data_connection.execute(f'SELECT * FROM {quoted_table}')
                csv_content=io.StringIO(newline='')
                writer=csv.writer(csv_content)
                writer.writerow([column[0] for column in cursor.description])
                rows=0
                for row in cursor:
                    writer.writerow(row); rows+=1
                csv_path=f'datos/tablas/{table}.csv'
                archive.writestr(csv_path,'\ufeff'+csv_content.getvalue())
                files.append(csv_path)
                table_summary.append({'tabla':table,'filas':rows,'archivo':csv_path})
            data_connection.close()
            archive.writestr('datos/LEEME.txt',
                'autolote.sqlite es la copia completa para restaurar el sistema.\n'
                'tablas/*.csv contiene cada tabla en formato legible por Excel.\n')
            files.append('datos/LEEME.txt')
            for relative in ('server.py','requirements.txt','render.yaml','README.md'):
                path=os.path.join(ROOT,relative)
                if os.path.isfile(path):
                    archive.write(path,f'sistema/{relative}'); files.append(f'sistema/{relative}')
            for folder_name in ('app','scripts','tests'):
                source_folder=os.path.join(ROOT,folder_name)
                if not os.path.isdir(source_folder): continue
                for folder,subfolders,names in os.walk(source_folder):
                    subfolders[:]=[name for name in subfolders if name != '__pycache__']
                    for name in names:
                        if name.endswith(('.pyc','.sqlite')): continue
                        path=os.path.join(folder,name); relative=os.path.relpath(path,ROOT)
                        archive.write(path,f'sistema/{relative}'); files.append(f'sistema/{relative}')
            archive.writestr('manifiesto.json',json.dumps({
                'generado_en':created,'contenido':files,
                'tablas':table_summary,
                'nota':'Las variables de entorno y secretos no se incluyen por seguridad.'
            },ensure_ascii=False,indent=2))
        content.seek(0)
        return send_file(content,mimetype='application/zip',as_attachment=True,
                         download_name=f'corporacion-triple-aaa-{datetime.now().strftime("%Y-%m-%d_%H%M%S")}.zip')

@app.get('/api/sesion')
def sesion_actual():
    user=user_access(session.get('user_id'))
    if not user: return jsonify(error='Sesión requerida.'),401
    return jsonify(usuario=user['username'],nombre=user.get('nombre') or user['username'],
                   perfil=user.get('perfil_nombre') or 'Sin perfil',es_administrador=bool(user.get('es_administrador')),
                   permisos=user.get('permisos',{}),costeo_acceso=nivel_actualizacion_costeo())

@app.post('/api/sesion/actividad')
def registrar_actividad_sesion():
    """Actualiza la sesión únicamente tras una interacción explícita de la UI."""
    session['last_activity']=datetime.now().timestamp()
    return jsonify(ok=True)

@app.get('/api/auditoria')
def get_auditoria():
    """Consulta acotada de cambios; el control de acceso es solo administrador."""
    filters=[]; args=[]
    for field,column in (('desde','date(b.fecha)'),('hasta','date(b.fecha)')):
        value=(request.args.get(field) or '').strip()
        if value:
            filters.append(f'{column}{">=" if field=="desde" else "<="}?'); args.append(value)
    for field,column in (('usuario','b.usuario'),('recurso','b.recurso'),('accion','b.accion')):
        value=(request.args.get(field) or '').strip()
        if value:
            filters.append(f'{column} LIKE ?'); args.append(f'%{value}%')
    try: limit=max(1,min(int(request.args.get('limite',200)),500))
    except ValueError: limit=200
    where=(' WHERE '+' AND '.join(filters)) if filters else ''
    c=db()
    rows=c.execute(f'''SELECT b.* FROM bitacora_auditoria b{where}
        ORDER BY b.fecha DESC,b.id DESC LIMIT ?''',(*args,limit)).fetchall()
    c.close()
    result=[]
    for row in rows:
        item=dict(row)
        for field in ('antes','despues'):
            try: item[field]=json.loads(item[field]) if item.get(field) else None
            except (TypeError,json.JSONDecodeError): item[field]=None
        result.append(item)
    return jsonify(result)

@app.post('/api/mi-cuenta/contrasena')
def cambiar_mi_contrasena():
    """Permite a cada usuario reemplazar la contraseña inicial que recibió."""
    data=request.get_json(silent=True) or {}
    actual=data.get('contrasena_actual') or ''
    nueva=data.get('contrasena_nueva') or ''
    confirmacion=data.get('confirmacion') or ''
    if len(nueva)<8: return jsonify(error='La nueva contraseña debe tener al menos 8 caracteres.'),400
    if nueva!=confirmacion: return jsonify(error='La confirmación no coincide con la nueva contraseña.'),400
    c=db(); user=c.execute('SELECT * FROM usuarios WHERE id=? AND activo=1',(session.get('user_id'),)).fetchone()
    if not user or not check_password_hash(user['password_hash'],actual):
        c.close(); return jsonify(error='La contraseña actual no es correcta.'),400
    version=int(user['sesion_version'] or 1)+1
    c.execute('UPDATE usuarios SET password_hash=?,sesion_version=? WHERE id=?',
              (generate_password_hash(nueva,method='pbkdf2:sha256'),version,user['id']))
    c.commit(); c.close(); session['sesion_version']=version; session['last_activity']=datetime.now().timestamp()
    return jsonify(ok=True)

def profile_permissions(c, profile_id):
    rows={row['modulo']:{'ver':bool(row['puede_ver']),'modificar':bool(row['puede_modificar'])}
          for row in c.execute('SELECT modulo,puede_ver,puede_modificar FROM perfil_permisos WHERE perfil_id=?',(profile_id,))}
    return {key:rows.get(key,{'ver':False,'modificar':False}) for key,_ in ACCESS_MODULES}

def profile_payload(c, row):
    data=dict(row)
    data['activo']=bool(data['activo']); data['es_administrador']=bool(data['es_administrador'])
    data['es_sistema']=bool(data['es_sistema']); data['permisos']=profile_permissions(c,data['id'])
    return data

@app.get('/api/perfiles')
def get_perfiles():
    c=db(); rows=[profile_payload(c,row) for row in c.execute('SELECT * FROM perfiles ORDER BY es_administrador DESC,nombre')]
    c.close(); return jsonify(rows)

def save_profile_permissions(c, profile_id, permissions, is_admin=False):
    permissions=permissions if isinstance(permissions,dict) else {}
    for module,_ in ACCESS_MODULES:
        item=permissions.get(module,{}) if isinstance(permissions.get(module,{}),dict) else {}
        can_view=bool(item.get('ver')) or bool(item.get('modificar'))
        can_edit=bool(item.get('modificar'))
        if is_admin: can_view=can_edit=True
        c.execute('''INSERT INTO perfil_permisos(perfil_id,modulo,puede_ver,puede_modificar) VALUES(?,?,?,?)
            ON CONFLICT(perfil_id,modulo) DO UPDATE SET puede_ver=excluded.puede_ver,puede_modificar=excluded.puede_modificar''',
                  (profile_id,module,int(can_view),int(can_edit)))

def profile_input(data):
    name=(data.get('nombre') or '').strip()
    if len(name)<3: return None,'El nombre del perfil debe tener al menos 3 caracteres.'
    return (name,(data.get('descripcion') or '').strip() or None,1 if data.get('activo',True) not in (False,0,'0') else 0),None

@app.post('/api/perfiles')
def post_perfil():
    data=request.get_json(silent=True) or {}; values,error=profile_input(data)
    if error: return jsonify(error=error),400
    c=db()
    try:
        cur=c.execute('INSERT INTO perfiles(nombre,descripcion,activo) VALUES(?,?,?)',values)
        save_profile_permissions(c,cur.lastrowid,data.get('permisos'))
        c.commit()
    except sqlite3.IntegrityError:
        c.rollback(); c.close(); return jsonify(error='Ya existe un perfil con ese nombre.'),409
    row=c.execute('SELECT * FROM perfiles WHERE id=?',(cur.lastrowid,)).fetchone(); result=profile_payload(c,row); c.close()
    return jsonify(result),201

@app.put('/api/perfiles/<int:profile_id>')
def put_perfil(profile_id):
    data=request.get_json(silent=True) or {}; values,error=profile_input(data)
    if error: return jsonify(error=error),400
    c=db(); profile=c.execute('SELECT * FROM perfiles WHERE id=?',(profile_id,)).fetchone()
    if not profile: c.close(); return jsonify(error='Perfil no encontrado.'),404
    try:
        c.execute('UPDATE perfiles SET nombre=?,descripcion=?,activo=? WHERE id=?',(*values,profile_id))
        save_profile_permissions(c,profile_id,data.get('permisos'),bool(profile['es_administrador']))
        c.commit()
    except sqlite3.IntegrityError:
        c.rollback(); c.close(); return jsonify(error='Ya existe otro perfil con ese nombre.'),409
    row=c.execute('SELECT * FROM perfiles WHERE id=?',(profile_id,)).fetchone(); result=profile_payload(c,row); c.close()
    return jsonify(result)

@app.get('/api/usuarios')
def get_usuarios():
    c=db(); rows=[]
    for row in c.execute('''SELECT u.id,u.username,u.nombre,u.email,u.activo,u.creado_en,u.ultimo_acceso,u.perfil_id,
        p.nombre perfil_nombre,p.es_administrador FROM usuarios u LEFT JOIN perfiles p ON p.id=u.perfil_id ORDER BY u.nombre,u.username'''):
        data=dict(row); data['activo']=bool(data['activo']); data['es_administrador']=bool(data['es_administrador']); rows.append(data)
    c.close(); return jsonify(rows)

def user_input(data, password_required=False):
    username=(data.get('username') or '').strip()
    name=(data.get('nombre') or '').strip()
    # La instalación inicial tenía email como NOT NULL; se conserva un valor
    # interno cuando el usuario no desea registrar correo.
    email=(data.get('email') or '').strip() or f'{username}@local'
    password=data.get('password') or ''
    try: profile_id=int(data.get('perfil_id'))
    except (TypeError,ValueError): profile_id=0
    if len(username)<3: return None,'El usuario debe tener al menos 3 caracteres.'
    if not name: return None,'El nombre es obligatorio.'
    if password_required and len(password)<8: return None,'La contraseña debe tener al menos 8 caracteres.'
    if password and len(password)<8: return None,'La contraseña debe tener al menos 8 caracteres.'
    return (username,name,email,profile_id,password),None

@app.post('/api/usuarios')
def post_usuario():
    data=request.get_json(silent=True) or {}; values,error=user_input(data,True)
    if error: return jsonify(error=error),400
    username,name,email,profile_id,password=values; c=db()
    if not c.execute('SELECT id FROM perfiles WHERE id=? AND activo=1',(profile_id,)).fetchone():
        c.close(); return jsonify(error='Seleccione un perfil activo.'),400
    try:
        cur=c.execute('''INSERT INTO usuarios(username,password_hash,nombre,email,rol,activo,perfil_id)
            VALUES(?,?,?,?,?,1,?)''',(username,generate_password_hash(password,method='pbkdf2:sha256'),name,email,'Usuario',profile_id))
        c.commit()
    except sqlite3.IntegrityError:
        c.rollback(); c.close(); return jsonify(error='Ese usuario ya existe.'),409
    c.close(); return jsonify(id=cur.lastrowid),201

@app.put('/api/usuarios/<int:target_id>')
def put_usuario(target_id):
    data=request.get_json(silent=True) or {}; values,error=user_input(data,False)
    if error: return jsonify(error=error),400
    username,name,email,profile_id,password=values; active=1 if data.get('activo',True) not in (False,0,'0') else 0
    c=db(); previous=c.execute('SELECT * FROM usuarios WHERE id=?',(target_id,)).fetchone()
    profile=c.execute('SELECT * FROM perfiles WHERE id=?',(profile_id,)).fetchone()
    if not previous: c.close(); return jsonify(error='Usuario no encontrado.'),404
    if not profile or (not profile['activo'] and active): c.close(); return jsonify(error='Seleccione un perfil válido y activo.'),400
    # Siempre debe permanecer al menos un administrador activo.
    old_profile=c.execute('SELECT es_administrador FROM perfiles WHERE id=?',(previous['perfil_id'],)).fetchone()
    leaving_admin=bool(old_profile and old_profile['es_administrador']) and (not active or not profile['es_administrador'])
    if leaving_admin:
        admins=c.execute('''SELECT COUNT(*) FROM usuarios u JOIN perfiles p ON p.id=u.perfil_id
            WHERE u.activo=1 AND p.es_administrador=1 AND u.id<>?''',(target_id,)).fetchone()[0]
        if not admins: c.close(); return jsonify(error='Debe mantenerse al menos un administrador activo.'),400
    try:
        if password:
            c.execute('''UPDATE usuarios SET username=?,nombre=?,email=?,perfil_id=?,activo=?,password_hash=?,sesion_version=COALESCE(sesion_version,1)+1 WHERE id=?''',
                      (username,name,email,profile_id,active,generate_password_hash(password,method='pbkdf2:sha256'),target_id))
        else:
            c.execute('UPDATE usuarios SET username=?,nombre=?,email=?,perfil_id=?,activo=? WHERE id=?',
                      (username,name,email,profile_id,active,target_id))
        c.commit()
    except sqlite3.IntegrityError:
        c.rollback(); c.close(); return jsonify(error='Ese usuario ya existe.'),409
    c.close(); return jsonify(ok=True)

@app.post('/api/usuarios/<int:target_id>/restablecer-contrasena')
def restablecer_contrasena_usuario(target_id):
    """El administrador entrega una nueva clave temporal y revoca sesiones previas."""
    data=request.get_json(silent=True) or {}; password=data.get('password') or ''
    if len(password)<8: return jsonify(error='La contraseña temporal debe tener al menos 8 caracteres.'),400
    c=db(); target=c.execute('SELECT id FROM usuarios WHERE id=?',(target_id,)).fetchone()
    if not target: c.close(); return jsonify(error='Usuario no encontrado.'),404
    c.execute('''UPDATE usuarios SET password_hash=?,sesion_version=COALESCE(sesion_version,1)+1
        WHERE id=?''',(generate_password_hash(password,method='pbkdf2:sha256'),target_id))
    c.commit(); c.close(); return jsonify(ok=True)

@app.get('/')
def home():
    # La interfaz es un único archivo con lógica operativa. No se debe servir
    # una copia vieja después de un despliegue, especialmente entre perfiles.
    response=send_from_directory(APP,'index.html')
    response.cache_control.no_store=True
    response.cache_control.max_age=0
    return response

@app.get('/api/dashboard')
def dashboard():
    c=db()
    vehicles=[dict(row) for row in c.execute('SELECT * FROM vehiculos').fetchall()]

    def status_summary(status):
        rows=[vehicle for vehicle in vehicles if vehicle.get('estado')==status and vehicle.get('tipo_compra')!='Consignación']
        summary={'cantidad':len(rows),'costo':round(sum(costo_consolidado(c,row['id']) for row in rows),2)}
        if status=='DPV':
            summary['venta_proyectada']=round(sum(float(row.get('precio_venta') or 0) for row in rows),2)
        return summary

    # El tablero presenta el inventario operativo por las tres etapas que se
    # administran diariamente. Los costos se calculan con la misma función del
    # Kardex, no solo con el valor de compra inicial.
    statuses={
        'dpv':status_summary('DPV'),
        'transito':status_summary('En Tránsito'),
        'taller':status_summary('En Taller'),
        'consignacion':{'cantidad':sum(1 for vehicle in vehicles if vehicle.get('tipo_compra')=='Consignación' and vehicle.get('estado') not in ('Vendido','Devuelto')),
                        'costo':round(sum(float(vehicle.get('precio_compra') or 0) for vehicle in vehicles if vehicle.get('tipo_compra')=='Consignación' and vehicle.get('estado') not in ('Vendido','Devuelto')),2)}
    }
    inventory_by_type={}
    for vehicle in vehicles:
        if vehicle.get('estado') in ('Vendido','Devuelto') or vehicle.get('tipo_compra')=='Consignación':
            continue
        key=(vehicle.get('tipo_vehiculo') or 'Sin tipo').strip() or 'Sin tipo'
        entry=inventory_by_type.setdefault(key,{'tipo_vehiculo':key,'cantidad':0,'costo':0.0})
        entry['cantidad']+=1
        entry['costo']+=costo_consolidado(c,vehicle['id'])

    # Render ejecuta en UTC; el período comercial debe cambiar con la hora de
    # Honduras, no al iniciar el siguiente día UTC. El usuario puede consultar
    # cualquier mes desde el Dashboard sin modificar datos operativos.
    default_month=ahora_operacion().strftime('%Y-%m')
    requested_month=(request.args.get('mes') or '').strip()
    if requested_month and not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])',requested_month):
        c.close(); return jsonify(error='El período debe tener el formato AAAA-MM.'),400
    current_month=requested_month or default_month
    sales=[dict(row) for row in c.execute('''SELECT v.*,ve.vin,ve.marca,ve.modelo,ve.version,ve.anio,ven.nombre vendedor_nombre
        FROM ventas v JOIN vehiculos ve ON ve.id=v.vehiculo_id LEFT JOIN vendedores ven ON ven.id=v.vendedor_id
        WHERE substr(v.fecha,1,7)=? AND (v.factura IS NOT NULL OR v.estado IN ('Facturada','Vendido'))''',(current_month,)).fetchall()]
    models={}; brands={}; sellers={}
    for sale in sales:
        cost=costo_consolidado(c,sale['vehiculo_id'])
        value=float(sale.get('precio') or 0)
        model=(sale.get('modelo') or 'Sin modelo').strip() or 'Sin modelo'
        model_entry=models.setdefault(model,{'modelo':model,'cantidad':0,'costo':0.0,'venta':0.0})
        brand=(sale.get('marca') or 'Sin marca').strip() or 'Sin marca'
        # Las marcas históricas pueden estar cargadas como Toyota, TOYOTA o
        # toyota. La agrupación comercial debe tratarlas como una sola marca.
        brand_key=brand.casefold()
        brand_entry=brands.setdefault(brand_key,{'marca':brand.upper(),'cantidad':0,'costo':0.0,'venta':0.0})
        # Las ventas históricas no tenían vendedor; se mantienen claramente
        # separadas de las nuevas ventas que sí lo requieren.
        seller=sale.get('vendedor_nombre') or 'Sin vendedor asignado'
        seller_entry=sellers.setdefault(seller,{'vendedor':seller,'cantidad':0,'costo':0.0,'venta':0.0})
        for entry in (model_entry,brand_entry,seller_entry):
            entry['cantidad']+=1; entry['costo']+=cost; entry['venta']+=value

    def summarize(rows):
        result=[]
        for row in rows:
            row['costo']=round(row['costo'],2); row['venta']=round(row['venta'],2)
            row['margen']=round(row['venta']-row['costo'],2)
            row['margen_pct']=round((row['margen']/row['venta']*100) if row['venta'] else 0,2)
            result.append(row)
        return result

    out={
        'mes':current_month,
        'dpv':statuses['dpv'],'transito':statuses['transito'],'taller':statuses['taller'],'consignacion':statuses['consignacion'],
        'inventario_por_tipo':sorted(({'tipo_vehiculo':row['tipo_vehiculo'],'cantidad':row['cantidad'],'costo':round(row['costo'],2)} for row in inventory_by_type.values()),key=lambda row:row['tipo_vehiculo']),
        # Este panel es siempre el Top 10 por valor de venta; el total de
        # Marcas puede ser mayor porque allí se muestran todas las ventas.
        'top_modelos':sorted(summarize(list(models.values())),key=lambda row:row['venta'],reverse=True)[:10],
        'ventas_por_marca':sorted(summarize(list(brands.values())),key=lambda row:row['venta'],reverse=True),
        'ventas_por_vendedor':sorted(summarize(list(sellers.values())),key=lambda row:row['venta'],reverse=True),
        # Se mantienen estas claves para consumidores existentes de la API.
        'total':sum(1 for row in vehicles if row.get('estado') not in ('Vendido','Devuelto')),'disponibles':statuses['dpv']['cantidad'],'inventario':round(sum(costo_consolidado(c,row['id']) for row in vehicles if row.get('estado') not in ('Vendido','Devuelto') and row.get('tipo_compra')!='Consignación'),2),
        'comisiones_pendientes':0
    }
    c.close(); return jsonify(out)

@app.get('/api/catalogo')
def catalogo():
    c=db()
    where='' if request.args.get('todos')=='1' else ' WHERE activo=1'
    r=c.execute('SELECT * FROM cuentas_contables'+where+' ORDER BY CAST(codigo AS INTEGER),codigo').fetchall()
    c.close(); return jsonify([dict(x) for x in r])

def datos_cuenta_contable(data):
    codigo=(data.get('codigo') or '').strip()
    cuenta=(data.get('cuenta') or '').strip()
    tipo=(data.get('tipo') or '').strip()
    naturaleza=(data.get('naturaleza') or '').strip()
    grupo=(data.get('grupo') or '').strip()
    tipos={'Activo','Contraactivo','Pasivo','Patrimonio','Ingreso','Costo','Gasto'}
    naturalezas={'Deudora','Acreedora'}
    if not codigo or not cuenta or not tipo or not naturaleza:
        return None,'Código, cuenta, tipo y naturaleza son obligatorios.'
    if tipo not in tipos or naturaleza not in naturalezas:
        return None,'Tipo o naturaleza contable no válidos.'
    return {
        'codigo':codigo,'cuenta':cuenta,'tipo':tipo,'grupo':grupo,'naturaleza':naturaleza,
        'acepta_movimiento':1 if bool(data.get('acepta_movimiento',True)) else 0,
        'requiere_activo':1 if bool(data.get('requiere_activo',False)) else 0,
        'requiere_centro_costo':1 if bool(data.get('requiere_centro_costo',False)) else 0,
        'activo':1 if bool(data.get('activo',True)) else 0,
    },None

@app.post('/api/catalogo')
def crear_catalogo():
    data,error=datos_cuenta_contable(request.get_json(silent=True) or {})
    if error: return jsonify(error=error),400
    c=db()
    try:
        cur=c.execute('''INSERT INTO cuentas_contables(codigo,cuenta,tipo,grupo,naturaleza,
            acepta_movimiento,requiere_activo,requiere_centro_costo,activo)
            VALUES(:codigo,:cuenta,:tipo,:grupo,:naturaleza,:acepta_movimiento,:requiere_activo,:requiere_centro_costo,:activo)''',data)
        c.commit()
        return jsonify(dict(c.execute('SELECT * FROM cuentas_contables WHERE id=?',(cur.lastrowid,)).fetchone())),201
    except sqlite3.IntegrityError:
        c.rollback(); return jsonify(error='Ya existe una cuenta contable con ese código.'),409
    finally:
        c.close()

@app.put('/api/catalogo/<int:cuenta_id>')
def actualizar_catalogo(cuenta_id):
    data,error=datos_cuenta_contable(request.get_json(silent=True) or {})
    if error: return jsonify(error=error),400
    c=db()
    if not c.execute('SELECT id FROM cuentas_contables WHERE id=?',(cuenta_id,)).fetchone():
        c.close(); return jsonify(error='Cuenta contable no encontrada.'),404
    duplicate=c.execute('SELECT id FROM cuentas_contables WHERE codigo=? AND id<>?',(data['codigo'],cuenta_id)).fetchone()
    if duplicate:
        c.close(); return jsonify(error='Ya existe otra cuenta con ese código.'),409
    try:
        c.execute('''UPDATE cuentas_contables SET codigo=?,cuenta=?,tipo=?,grupo=?,naturaleza=?,
            acepta_movimiento=?,requiere_activo=?,requiere_centro_costo=?,activo=? WHERE id=?''',
            (data['codigo'],data['cuenta'],data['tipo'],data['grupo'],data['naturaleza'],data['acepta_movimiento'],data['requiere_activo'],data['requiere_centro_costo'],data['activo'],cuenta_id))
        c.commit()
        row=dict(c.execute('SELECT * FROM cuentas_contables WHERE id=?',(cuenta_id,)).fetchone())
    except sqlite3.IntegrityError:
        c.rollback(); c.close(); return jsonify(error='No se pudo guardar la cuenta por un conflicto de datos.'),409
    c.close(); return jsonify(row)

@app.get('/api/conceptos-gasto')
def get_conceptos_gasto():
    incluir_inactivos=request.args.get('todos')=='1'
    c=db(); sql='''SELECT cg.*,cc.codigo cuenta_codigo,cc.cuenta cuenta_nombre,cc.activo cuenta_activa,
        cc.acepta_movimiento FROM conceptos_gasto cg JOIN cuentas_contables cc ON cc.id=cg.cuenta_id'''
    if not incluir_inactivos:
        sql+=' WHERE cg.activo=1'
    rows=[dict(row) for row in c.execute(sql+' ORDER BY cg.nombre').fetchall()]
    c.close(); return jsonify(rows)

def validar_concepto_gasto(data, c):
    nombre=(data.get('nombre') or '').strip()
    try:
        cuenta_id=int(data.get('cuenta_id'))
    except (TypeError,ValueError):
        return None,'Seleccione la cuenta contable para el concepto.'
    if not nombre:
        return None,'El nombre del concepto es obligatorio.'
    cuenta=c.execute('SELECT id FROM cuentas_contables WHERE id=? AND activo=1 AND acepta_movimiento=1',(cuenta_id,)).fetchone()
    if not cuenta:
        return None,'La cuenta seleccionada no está activa o no acepta movimientos.'
    return (nombre,cuenta_id),None

@app.post('/api/conceptos-gasto')
def post_concepto_gasto():
    data=request.get_json(silent=True) or {}; c=db(); values,error=validar_concepto_gasto(data,c)
    if error:
        c.close(); return jsonify(error=error),400
    try:
        cur=c.execute('INSERT INTO conceptos_gasto(nombre,cuenta_id,activo) VALUES(?,?,1)',values)
        c.commit()
    except sqlite3.IntegrityError:
        c.rollback(); c.close(); return jsonify(error='Ya existe un concepto de gasto con ese nombre.'),409
    c.close(); return jsonify(id=cur.lastrowid),201

@app.put('/api/conceptos-gasto/<int:concepto_id>')
def put_concepto_gasto(concepto_id):
    data=request.get_json(silent=True) or {}; c=db(); values,error=validar_concepto_gasto(data,c)
    if error:
        c.close(); return jsonify(error=error),400
    activo=1 if data.get('activo',True) not in (False,0,'0',None) else 0
    if not c.execute('SELECT id FROM conceptos_gasto WHERE id=?',(concepto_id,)).fetchone():
        c.close(); return jsonify(error='Concepto de gasto no encontrado.'),404
    try:
        c.execute('UPDATE conceptos_gasto SET nombre=?,cuenta_id=?,activo=? WHERE id=?',(*values,activo,concepto_id))
        c.commit()
    except sqlite3.IntegrityError:
        c.rollback(); c.close(); return jsonify(error='Ya existe otro concepto de gasto con ese nombre.'),409
    c.close(); return jsonify(ok=True)

@app.get('/api/gastos-operativos')
def get_gastos_operativos():
    desde,hasta=rango_reporte(); where,args=filtro_fechas('go.fecha',desde,hasta)
    c=db(); sql='''SELECT go.*,go.fecha fecha_pago,go.proveedor proveedor_nombre,COALESCE(cg.nombre,go.categoria) concepto,
        cc.codigo cuenta_codigo,cc.cuenta cuenta_nombre,
        p.nombre proveedor_maestro
        FROM gastos_operativos go
        LEFT JOIN conceptos_gasto cg ON cg.id=go.concepto_id
        LEFT JOIN cuentas_contables cc ON cc.id=cg.cuenta_id
        LEFT JOIN proveedores p ON p.id=go.proveedor_id'''
    if where: sql+=' WHERE '+' AND '.join(where)
    rows=[dict(row) for row in c.execute(sql+' ORDER BY go.fecha DESC,go.id DESC',args).fetchall()]
    c.close(); return jsonify(rows)

@app.post('/api/gastos-operativos')
def post_gasto_operativo():
    data=request.get_json(silent=True) or {}
    try:
        concepto_id=int(data.get('concepto_id'))
        subtotal=round(float(data.get('subtotal') or 0),2)
        isv=round(float(data.get('isv') or 0),2)
    except (TypeError,ValueError):
        return jsonify(error='Subtotal e ISV deben ser valores numéricos.'),400
    fecha=(data.get('fecha_pago') or '').strip()
    proveedor_nombre=(data.get('proveedor_nombre') or '').strip()
    forma_pago=(data.get('forma_pago') or '').strip()
    banco=(data.get('banco') or '').strip()
    referencia=(data.get('referencia') or '').strip()
    if not fecha or not proveedor_nombre or not forma_pago:
        return jsonify(error='Concepto, proveedor, fecha y forma de pago son obligatorios.'),400
    try:
        datetime.fromisoformat(fecha)
    except ValueError:
        return jsonify(error='La fecha de pago no es válida.'),400
    if subtotal<0 or isv<0 or subtotal+isv<=0:
        return jsonify(error='El total del gasto debe ser mayor que cero.'),400
    formas_validas={'Efectivo','Transferencia','Depósito','Cheque','Tarjeta'}
    if forma_pago not in formas_validas:
        return jsonify(error='La forma de pago no es válida.'),400
    if forma_pago!='Efectivo' and not banco:
        return jsonify(error='Seleccione el banco desde el que se realizó el pago.'),400
    total=round(subtotal+isv,2); c=db()
    concepto=c.execute('''SELECT cg.id,cg.nombre,cg.cuenta_id,cc.activo,cc.acepta_movimiento
        FROM conceptos_gasto cg JOIN cuentas_contables cc ON cc.id=cg.cuenta_id
        WHERE cg.id=? AND cg.activo=1''',(concepto_id,)).fetchone()
    if not concepto or not concepto['activo'] or not concepto['acepta_movimiento']:
        c.close(); return jsonify(error='El concepto o su cuenta contable no está disponible.'),400
    proveedor=c.execute('SELECT id FROM proveedores WHERE nombre=?',(proveedor_nombre,)).fetchone()
    isv_cuenta=c.execute("SELECT id FROM cuentas_contables WHERE codigo='1106' AND activo=1 AND acepta_movimiento=1").fetchone()
    try:
        caja_id=cuenta_contable_id(c,'caja_bancos')
        if isv and not isv_cuenta:
            raise ValueError('No existe la cuenta activa 1106 · ISV Acreditable.')
        cur=c.execute('''INSERT INTO gastos_operativos(concepto_id,proveedor_id,fecha,categoria,clasificacion,proveedor,
            documento,subtotal,isv,total,forma_pago,banco,origen_clave,referencia,observaciones)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (concepto_id,proveedor['id'] if proveedor else None,fecha,concepto['nombre'],'Gasto operativo',proveedor_nombre,
             (data.get('documento') or '').strip() or None,subtotal,isv,total,forma_pago,banco or None,
             f'gasto_operativo:{uuid.uuid4().hex}',referencia or None,(data.get('observaciones') or '').strip() or None))
        gasto_id=cur.lastrowid
        lineas=[{'cuenta_id':concepto['cuenta_id'],'debe':subtotal,'haber':0}]
        if isv: lineas.append({'cuenta_id':isv_cuenta['id'],'debe':isv,'haber':0})
        lineas.append({'cuenta_id':caja_id,'debe':0,'haber':total})
        descripcion=f"Pago de gasto · {concepto['nombre']} · {proveedor_nombre}"
        registrar_asiento(c,fecha,descripcion,'gasto_operativo',gasto_id,lineas)
        medio,banco_movimiento=medio_caja(forma_pago,banco)
        registrar_movimiento_caja(c,fecha,'Pago de gasto',medio,banco_movimiento,0,total,descripcion,'gasto_operativo',gasto_id)
        c.commit()
    except ValueError as error:
        c.rollback(); c.close(); return jsonify(error=str(error)),400
    except sqlite3.IntegrityError:
        c.rollback(); c.close(); return jsonify(error='No se pudo registrar el gasto.'),409
    c.close(); return jsonify(id=gasto_id,total=total),201

def rango_reporte():
    return (request.args.get('desde') or '').strip(),(request.args.get('hasta') or '').strip()

def filtro_fechas(campo, desde, hasta):
    where=[]; args=[]
    if desde: where.append(f'{campo}>=?'); args.append(desde)
    if hasta: where.append(f'{campo}<=?'); args.append(hasta)
    return where,args

@app.get('/api/contabilidad/libro-diario')
def libro_diario():
    # Para la consulta de cierre, el Libro Diario presenta el histórico
    # acumulado hasta la fecha "Hasta", igual que el Balance General.
    _,hasta=rango_reporte(); where=[]; args=[]
    if hasta:
        where.append('a.fecha<=?'); args.append(hasta)
    sql='''SELECT a.*,COALESCE(SUM(p.debe),0) total_debe,COALESCE(SUM(p.haber),0) total_haber
        FROM asientos_contables a LEFT JOIN partidas p ON p.asiento_id=a.id'''
    if where: sql+=' WHERE '+' AND '.join(where)
    rows=c=db(); headers=c.execute(sql+' GROUP BY a.id ORDER BY a.fecha DESC,a.id DESC',args).fetchall(); out=[]
    for header in headers:
        item=dict(header); item['lineas']=[dict(x) for x in c.execute('''SELECT p.*,cc.codigo,cc.cuenta
            FROM partidas p JOIN cuentas_contables cc ON cc.id=p.cuenta_id WHERE p.asiento_id=? ORDER BY p.id''',(header['id'],)).fetchall()]; out.append(item)
    c.close(); return jsonify(out)

@app.post('/api/contabilidad/asientos-manuales')
def crear_asiento_manual():
    """Registra una póliza manual sin interferir con documentos automáticos."""
    data=request.get_json(silent=True) or {}
    fecha=(data.get('fecha') or '').strip()
    descripcion=(data.get('descripcion') or '').strip()
    lineas=data.get('lineas')
    if not fecha or not descripcion or not isinstance(lineas,list) or len(lineas)<2:
        return jsonify(error='Ingrese fecha, descripción y al menos dos líneas.'),400
    try:
        datetime.fromisoformat(fecha)
    except ValueError:
        return jsonify(error='La fecha del asiento no es válida.'),400
    limpias=[]
    try:
        for linea in lineas:
            cuenta_id=int(linea.get('cuenta_id'))
            debe=round(float(linea.get('debe') or 0),2)
            haber=round(float(linea.get('haber') or 0),2)
            if debe<0 or haber<0 or (debe and haber) or (not debe and not haber):
                raise ValueError
            limpias.append({'cuenta_id':cuenta_id,'debe':debe,'haber':haber})
    except (AttributeError,TypeError,ValueError):
        return jsonify(error='Cada línea debe tener una cuenta y un valor en Debe o Haber.'),400
    total_debe=round(sum(linea['debe'] for linea in limpias),2)
    total_haber=round(sum(linea['haber'] for linea in limpias),2)
    if total_debe<=0 or abs(total_debe-total_haber)>0.01:
        return jsonify(error='El Debe y el Haber deben cuadrar exactamente.'),400
    c=db()
    try:
        ids={linea['cuenta_id'] for linea in limpias}
        validas={fila['id'] for fila in c.execute(
            f"SELECT id FROM cuentas_contables WHERE activo=1 AND acepta_movimiento=1 AND id IN ({','.join('?' for _ in ids)})",
            list(ids),
        ).fetchall()}
        if validas != ids:
            raise ValueError('Una o más cuentas no están activas o no aceptan movimiento.')
        referencia=c.execute("SELECT COALESCE(MAX(referencia_id),0)+1 FROM asientos_contables WHERE referencia_tipo='asiento_manual'").fetchone()[0]
        asiento_id=registrar_asiento(c,fecha,descripcion,'asiento_manual',referencia,limpias)
        c.commit()
    except ValueError as error:
        c.rollback(); c.close(); return jsonify(error=str(error)),400
    except sqlite3.IntegrityError:
        c.rollback(); c.close(); return jsonify(error='No fue posible registrar el asiento manual.'),409
    c.close(); return jsonify(id=asiento_id,total_debe=total_debe,total_haber=total_haber),201

def filas_resultado(c, desde='', hasta=''):
    where,args=filtro_fechas('p.fecha',desde,hasta)
    sql='''SELECT cc.id,cc.codigo,cc.cuenta,cc.tipo,cc.grupo,COALESCE(SUM(p.debe),0) debe,COALESCE(SUM(p.haber),0) haber
        FROM partidas p JOIN cuentas_contables cc ON cc.id=p.cuenta_id WHERE cc.tipo IN ('Ingreso','Costo','Gasto')'''
    if where: sql+=' AND '+' AND '.join(where)
    return [dict(row) for row in c.execute(sql+' GROUP BY cc.id ORDER BY cc.codigo',args).fetchall()]

@app.get('/api/contabilidad/estado-resultado')
def estado_resultado():
    desde,hasta=rango_reporte(); c=db(); rows=filas_resultado(c,desde,hasta); ingresos=[]; costos=[]; gastos=[]
    for row in rows:
        row['saldo']=round((row['haber']-row['debe']) if row['tipo']=='Ingreso' else (row['debe']-row['haber']),2)
        (ingresos if row['tipo']=='Ingreso' else costos if row['tipo']=='Costo' else gastos).append(row)
    total_ingresos=sum(row['saldo'] for row in ingresos); total_costos=sum(row['saldo'] for row in costos); total_gastos=sum(row['saldo'] for row in gastos)
    c.close(); return jsonify(ingresos=ingresos,costos=costos,gastos=gastos,total_ingresos=total_ingresos,total_costos=total_costos,total_gastos=total_gastos,utilidad_neta=total_ingresos-total_costos-total_gastos)

@app.get('/api/contabilidad/balance-general')
def balance_general():
    # El balance es una fotografía acumulada a la fecha de corte. El campo
    # "Desde" sirve para los reportes de período, no debe ocultar saldos
    # provenientes de meses anteriores.
    _,hasta=rango_reporte(); where=[]; args=[]
    if hasta:
        where.append('p.fecha<=?'); args.append(hasta)
    c=db(); sql='''SELECT cc.id,cc.codigo,cc.cuenta,cc.tipo,COALESCE(SUM(p.debe),0) debe,COALESCE(SUM(p.haber),0) haber
        FROM cuentas_contables cc LEFT JOIN partidas p ON p.cuenta_id=cc.id'''
    if where: sql+=' WHERE '+' AND '.join(where)
    rows=[dict(row) for row in c.execute(sql+' GROUP BY cc.id HAVING COALESCE(SUM(p.debe),0)<>0 OR COALESCE(SUM(p.haber),0)<>0 ORDER BY cc.codigo',args).fetchall()]
    activo=[]; pasivo=[]; patrimonio=[]
    for row in rows:
        row['saldo']=round(row['debe']-row['haber'],2) if row['tipo'] in ('Activo','Contraactivo') else round(row['haber']-row['debe'],2)
        if row['tipo'] in ('Activo','Contraactivo'): activo.append(row)
        elif row['tipo']=='Pasivo': pasivo.append(row)
        elif row['tipo']=='Patrimonio': patrimonio.append(row)
    # El resultado incorporado al patrimonio también debe ser acumulado al
    # cierre, para mantener la ecuación contable del balance.
    resultado_rows=filas_resultado(c,'',hasta)
    resultado=0
    for row in resultado_rows:
        saldo=(row['haber']-row['debe']) if row['tipo']=='Ingreso' else (row['debe']-row['haber'])
        resultado+=saldo if row['tipo']=='Ingreso' else -saldo
    if resultado: patrimonio.append({'codigo':'RESULTADO','cuenta':'Resultado del período','tipo':'Patrimonio','saldo':resultado})
    total_activo=sum(x['saldo'] for x in activo); total_pasivo=sum(x['saldo'] for x in pasivo); total_patrimonio=sum(x['saldo'] for x in patrimonio)
    c.close(); return jsonify(activo=activo,pasivo=pasivo,patrimonio=patrimonio,total_activo=total_activo,total_pasivo=total_pasivo,total_patrimonio=total_patrimonio,diferencia=round(total_activo-total_pasivo-total_patrimonio,2))

@app.get('/api/contabilidad/conciliacion-inventario')
def conciliacion_inventario():
    """Compara el inventario operativo actual contra sus cuentas contables.

    No registra ni altera información. Sirve para identificar, por VIN, las
    unidades cuyos costos o reclasificaciones no llegaron a la misma cuenta que
    su etapa actual. La venta con pago ``Cambio`` es la única excepción de
    trazabilidad: su débito se asigna al vehículo recibido, aunque el asiento
    original pertenezca a la venta que lo recibió.
    """
    c=db()
    cuentas={
        'inventario_dpv':'DPV',
        'inventario_taller':'En Taller',
        'inventario_transito':'En Tránsito',
    }
    cuenta_ids={key:cuenta_contable_id(c,key) for key in cuentas}
    por_id={account_id:key for key,account_id in cuenta_ids.items()}
    vehicles=[dict(row) for row in c.execute('''SELECT *
        FROM vehiculos WHERE estado NOT IN ('Vendido','Devuelto') AND COALESCE(tipo_compra,'')<>'Consignación' ''').fetchall()]
    operativo={key:0.0 for key in cuentas}
    esperado={}
    detalle_dashboard=[]
    alertas_dashboard=[]
    for vehicle in vehicles:
        key=cuenta_inventario_por_estado(vehicle['estado'])
        if key not in cuentas:
            continue
        costeo=c.execute('''SELECT ajuste_compra,grua,flete,isv_pagado,cl_std,almacenaje,gastos_aduaneros
            FROM costos_adquisicion WHERE vehiculo_id=?''',(vehicle['id'],)).fetchone()
        compra=round(float(vehicle['precio_compra'] or 0),2)
        nacionalizacion=round(sum(float(costeo[field] or 0) for field in
            ('ajuste_compra','grua','flete','isv_pagado','cl_std','almacenaje','gastos_aduaneros')) if costeo else 0,2)
        extras=round(c.execute('SELECT COALESCE(SUM(monto),0) FROM costos_vehiculo WHERE vehiculo_id=?',
            (vehicle['id'],)).fetchone()[0],2)
        # Esta suma reproduce exactamente costo_consolidado(), dejando visibles
        # sus tres componentes para descartar una duplicación en el Dashboard.
        cost=round(compra+nacionalizacion+extras,2)
        operativo[key]+=cost
        esperado[vehicle['id']]={'vehiculo':vehicle,'cuenta':key,'costo':cost}
        acquisition_count=c.execute('SELECT COUNT(*) FROM adquisiciones WHERE vehiculo_id=?',(vehicle['id'],)).fetchone()[0]
        detail={
            'vehiculo_id':vehicle['id'],'vin':vehicle['vin'],
            'vehiculo':f"{vehicle['marca'] or ''} {vehicle['modelo'] or ''}".strip(),
            'etapa':vehicle['estado'],'compra':compra,'nacionalizacion':nacionalizacion,
            'costos_adicionales':extras,'total_dashboard':cost,
            'adquisiciones':acquisition_count,
        }
        detalle_dashboard.append(detail)
        if acquisition_count!=1:
            alertas_dashboard.append({**detail,'motivo':'El vehículo tiene más de una adquisición o no tiene adquisición.'})
    for duplicate in c.execute('''SELECT cv.vehiculo_id,cv.documento,cv.monto,COUNT(*) cantidad
        FROM costos_vehiculo cv WHERE COALESCE(TRIM(cv.documento),'')<>''
        GROUP BY cv.vehiculo_id,cv.documento,cv.monto HAVING COUNT(*)>1''').fetchall():
        owner=esperado.get(duplicate['vehiculo_id'])
        if owner:
            vehicle=owner['vehiculo']
            alertas_dashboard.append({'vehiculo_id':vehicle['id'],'vin':vehicle['vin'],
                'vehiculo':f"{vehicle['marca'] or ''} {vehicle['modelo'] or ''}".strip(),
                'etapa':vehicle['estado'],'documento':duplicate['documento'],'monto':round(float(duplicate['monto'] or 0),2),
                'cantidad':duplicate['cantidad'],'motivo':'Documento de costo repetido con el mismo importe.'})

    # Saldo contable neto por unidad y cuenta. Las líneas de costo de ventas de
    # unidades ya vendidas no aparecen en el detalle porque no forman parte del
    # inventario operativo vigente.
    registrado={vehicle_id:{key:0.0 for key in cuentas} for vehicle_id in esperado}
    for line in c.execute('''SELECT p.vehiculo_id,p.cuenta_id,p.debe,p.haber,p.referencia_tipo,p.referencia_id
        FROM partidas p WHERE p.cuenta_id IN (?,?,?)''',tuple(cuenta_ids.values())).fetchall():
        key=por_id[line['cuenta_id']]
        value=round(float(line['debe'] or 0)-float(line['haber'] or 0),2)
        if line['vehiculo_id'] in registrado:
            registrado[line['vehiculo_id']][key]+=value

    # El ingreso de una venta con cambio debita inventario pero, por diseño del
    # asiento de venta, lleva inicialmente el ID de la unidad vendida. Para la
    # conciliación se atribuye al VIN realmente recibido como parte de pago.
    cambios=c.execute('''SELECT pv.venta_id,pv.vehiculo_recibido_id,pv.monto
        FROM pagos_venta pv WHERE pv.tipo_pago='Cambio' AND pv.vehiculo_recibido_id IS NOT NULL''').fetchall()
    for cambio in cambios:
        vehicle_id=cambio['vehiculo_recibido_id']
        if vehicle_id not in registrado:
            continue
        amount=round(float(cambio['monto'] or 0),2)
        line=c.execute('''SELECT p.cuenta_id FROM partidas p
            JOIN asientos_contables a ON a.id=p.asiento_id
            WHERE a.referencia_tipo='venta_ingreso' AND a.referencia_id=?
              AND p.cuenta_id IN (?,?,?) AND ROUND(p.debe,2)=?
            ORDER BY p.id LIMIT 1''',(cambio['venta_id'],*cuenta_ids.values(),amount)).fetchone()
        if line:
            registrado[vehicle_id][por_id[line['cuenta_id']]]+=amount

    contable={key:round(c.execute('''SELECT COALESCE(SUM(p.debe-p.haber),0)
        FROM partidas p WHERE p.cuenta_id=?''',(account_id,)).fetchone()[0],2)
               for key,account_id in cuenta_ids.items()}
    resumen=[]
    for key,label in cuentas.items():
        resumen.append({'cuenta':key,'etapa':label,'operativo':round(operativo[key],2),
                        'contable':contable[key],
                        'diferencia':round(operativo[key]-contable[key],2)})
    inconsistencias=[]
    for vehicle_id,data in esperado.items():
        por_cuenta={key:round(value,2) for key,value in registrado[vehicle_id].items()}
        contable_total=round(sum(por_cuenta.values()),2)
        expected_by_account={key:(data['costo'] if key==data['cuenta'] else 0.0) for key in cuentas}
        if any(abs(expected_by_account[key]-por_cuenta[key])>0.01 for key in cuentas):
            vehicle=data['vehiculo']
            inconsistencias.append({
                'vehiculo_id':vehicle_id,'vin':vehicle['vin'],'vehiculo':f"{vehicle['marca'] or ''} {vehicle['modelo'] or ''}".strip(),
                'etapa':vehicle['estado'],'costo_operativo':data['costo'],'saldo_contable':contable_total,
                'diferencia':round(data['costo']-contable_total,2),
                'cuenta_esperada':cuentas[data['cuenta']],
                'dpv_contable':por_cuenta['inventario_dpv'],
                'taller_contable':por_cuenta['inventario_taller'],
                'transito_contable':por_cuenta['inventario_transito'],
            })
    inconsistencias.sort(key=lambda row:(abs(row['diferencia']),row['vin']),reverse=True)
    # Partidas que sí están vinculadas a los VIN activos con diferencia. Esto
    # permite distinguir entre un costo que falta (no habrá partida) y una
    # partida que está aplicada a una cuenta/vehículo incorrecto.
    trazabilidad=[]
    ids_inconsistentes=[row['vehiculo_id'] for row in inconsistencias]
    if ids_inconsistentes:
        placeholders=','.join('?' for _ in ids_inconsistentes)
        trace_rows=c.execute('''SELECT p.vehiculo_id,p.fecha,cc.codigo,cc.cuenta,p.debe,p.haber,
                p.referencia_tipo,p.referencia_id,COALESCE(a.descripcion,p.descripcion) documento,
                v.vin,v.marca,v.modelo
            FROM partidas p
            JOIN cuentas_contables cc ON cc.id=p.cuenta_id
            LEFT JOIN asientos_contables a ON a.id=p.asiento_id
            LEFT JOIN vehiculos v ON v.id=p.vehiculo_id
            WHERE p.vehiculo_id IN ('''+placeholders+''')
              AND p.cuenta_id IN (?,?,?)
            ORDER BY v.vin,p.fecha,p.id''',
            (*ids_inconsistentes,*cuenta_ids.values())).fetchall()
        trazabilidad=[{
            'vin':row['vin'],'vehiculo':f"{row['marca'] or ''} {row['modelo'] or ''}".strip(),
            'fecha':row['fecha'],'cuenta':f"{row['codigo']} · {row['cuenta']}",
            'documento':row['documento'],'origen':row['referencia_tipo'],
            'debe':round(float(row['debe'] or 0),2),'haber':round(float(row['haber'] or 0),2),
        } for row in trace_rows]
    contable_vinculado=round(sum(sum(values.values()) for values in registrado.values()),2)
    contable_total=round(sum(contable.values()),2)
    trazabilidad_resumen={
        'dashboard':round(sum(operativo.values()),2),
        'contable_vinculado_a_vin_activo':contable_vinculado,
        'saldo_sin_vinculo_a_vin_activo':round(contable_total-contable_vinculado,2),
        'balance_inventario':contable_total,
    }
    c.close()
    detalle_dashboard.sort(key=lambda row:(row['etapa'],row['vin']))
    return jsonify(
        resumen=resumen,
        validacion_dashboard={
            'vehiculos_incluidos':len(detalle_dashboard),
            'vins_unicos':len({row['vin'] for row in detalle_dashboard}),
            'total_recalculado':round(sum(row['total_dashboard'] for row in detalle_dashboard),2),
            'total_por_etapa':round(sum(operativo.values()),2),
            'sin_duplicar_vehiculos':len(detalle_dashboard)==len({row['vehiculo_id'] for row in detalle_dashboard}),
        },
        detalle_dashboard=detalle_dashboard,
        alertas_dashboard=alertas_dashboard,
        inconsistencias=inconsistencias,
        trazabilidad=trazabilidad,
        trazabilidad_resumen=trazabilidad_resumen,
        diferencia_total=round(sum(item['diferencia'] for item in resumen),2),
    )

@app.get('/api/cuentas-por-pagar')
def cuentas_por_pagar():
    c=db(); rows=c.execute('''SELECT cp.*,p.nombre proveedor_nombre,a.costo_compra,a.anticipo,a.metodo_pago,v.vin,v.marca,v.modelo
        FROM cuentas_por_pagar cp JOIN proveedores p ON p.id=cp.proveedor_id JOIN adquisiciones a ON a.id=cp.adquisicion_id
        JOIN vehiculos v ON v.id=a.vehiculo_id ORDER BY cp.fecha DESC,cp.id DESC''').fetchall(); c.close(); return jsonify([dict(row) for row in rows])

@app.get('/api/cuentas-por-pagar-taller')
def cuentas_por_pagar_taller():
    c=db(); rows=c.execute('''SELECT cp.*,p.nombre proveedor_nombre,o.numero_ot,o.tipo_reparacion,v.vin,v.marca,v.modelo
        FROM cuentas_por_pagar_ot cp JOIN proveedores p ON p.id=cp.proveedor_id
        JOIN ordenes_trabajo o ON o.id=cp.orden_trabajo_id JOIN vehiculos v ON v.id=o.vehiculo_id
        ORDER BY cp.fecha DESC,cp.id DESC''').fetchall(); c.close(); return jsonify([dict(row) for row in rows])

@app.get('/api/cuentas-por-pagar-repuestos')
def cuentas_por_pagar_repuestos():
    c=db(); rows=c.execute('''SELECT cp.*,p.nombre proveedor_nombre,r.factura,o.numero_ot,v.vin,v.marca,v.modelo,
            (SELECT pp.id FROM pagos_cuentas_por_pagar_repuestos_ot pp
             WHERE pp.cuenta_por_pagar_repuesto_id=cp.id ORDER BY pp.id DESC LIMIT 1) ultimo_pago_id
        FROM cuentas_por_pagar_repuestos_ot cp JOIN proveedores p ON p.id=cp.proveedor_id
        JOIN repuestos_ot r ON r.id=cp.repuesto_ot_id JOIN ordenes_trabajo o ON o.id=r.orden_trabajo_id
        JOIN vehiculos v ON v.id=o.vehiculo_id ORDER BY cp.fecha DESC,cp.id DESC''').fetchall(); c.close(); return jsonify([dict(row) for row in rows])

@app.get('/api/cuentas-por-cobrar')
def cuentas_por_cobrar():
    c=db(); rows=c.execute('''SELECT cc.*,COALESCE(cl.nombre,fc.nombre,cc.financiera_nombre) tercero_nombre,
        COALESCE(NULLIF(cc.tipo_cxc,''),CASE WHEN cc.cliente_id IS NOT NULL THEN 'Pagaré' ELSE 'Financiada' END) tipo_cxc,
        ve.numero_transaccion,ve.factura,ve.precio,v.vin,v.marca,v.modelo
        FROM cuentas_por_cobrar cc JOIN ventas ve ON ve.id=cc.venta_id JOIN vehiculos v ON v.id=ve.vehiculo_id
        LEFT JOIN clientes cl ON cl.id=cc.cliente_id LEFT JOIN clientes fc ON fc.id=cc.financiera_cliente_id
        ORDER BY cc.fecha DESC,cc.id DESC''').fetchall(); c.close(); return jsonify([dict(row) for row in rows])

def validar_pago_cartera(data, saldo):
    try:
        monto=round(float(data.get('monto')),2)
    except (TypeError,ValueError):
        return None,'Ingrese un monto válido'
    tipo=(data.get('tipo_pago') or '').strip(); banco=(data.get('banco') or '').strip(); referencia=(data.get('referencia') or '').strip()
    if tipo not in ('Efectivo','Transferencia','Depósito','Tarjeta'):
        return None,'Seleccione un tipo de pago válido'
    if monto<=0 or monto>round(float(saldo),2)+0.01:
        return None,'El monto no puede ser mayor que el saldo pendiente'
    if tipo!='Efectivo' and not banco:
        return None,'Seleccione el banco'
    if tipo!='Efectivo' and not referencia:
        return None,'Ingrese el número de referencia'
    return (monto,tipo,banco or None,referencia or None),None

@app.post('/api/vehiculos/<int:vid>/devolucion-cambio')
def devolver_vehiculo_recibido_cambio(vid):
    """Da de baja una unidad recibida en cambio y abre la CxC al cliente.

    La venta original nunca se modifica: la devolución es un documento nuevo,
    con su propio asiento, para conservar la auditoría de ambos vehículos.
    """
    data=request.get_json(silent=True) or {}
    fecha=(data.get('fecha') or ahora_operacion().date().isoformat()).strip()
    motivo=(data.get('motivo') or '').strip()
    if not motivo:
        return jsonify(error='Indique el motivo de la devolución.'),400
    try: datetime.strptime(fecha,'%Y-%m-%d')
    except ValueError: return jsonify(error='La fecha debe tener formato AAAA-MM-DD.'),400
    c=db()
    try:
        vehicle=c.execute('SELECT * FROM vehiculos WHERE id=?',(vid,)).fetchone()
        if not vehicle: return jsonify(error='Vehículo no encontrado.'),404
        if vehicle['estado']=='Devuelto': return jsonify(error='Este vehículo ya fue dado de baja por devolución.'),409
        if vehicle['estado']=='Vendido': return jsonify(error='No se puede devolver un vehículo que ya fue vendido; primero debe resolver formalmente la venta posterior.'),409
        link,regularizar_vinculo=buscar_vinculo_pago_cambio(c,vehicle)
        if not link: return jsonify(error='Este vehículo no está vinculado a un pago por cambio.'),409
        # Las ventas históricas pueden tener el vehículo y el pago registrados,
        # pero sin la llave que los relaciona. Solo se regulariza cuando la
        # observación identifica una venta y hay exactamente un pago Cambio
        # pendiente de enlazar en dicha venta; nunca se adivina ni se crea un
        # pago nuevo desde esta pantalla.
        if regularizar_vinculo:
            updated=c.execute('''UPDATE pagos_venta SET vehiculo_recibido_id=?
                WHERE id=? AND tipo_pago='Cambio' AND vehiculo_recibido_id IS NULL''',
                (vid,link['pago_venta_id'])).rowcount
            if updated!=1:
                return jsonify(error='El vínculo histórico de cambio fue modificado por otro registro. Actualice la ficha e intente de nuevo.'),409
        if c.execute("SELECT 1 FROM ordenes_trabajo WHERE vehiculo_id=? AND estado='Pendiente'",(vid,)).fetchone():
            return jsonify(error='Finalice o cancele la OT pendiente antes de devolver este vehículo.'),409
        amount=round(float(link['monto'] or 0),2)
        consolidated=round(costo_consolidado(c,vid),2)
        if abs(consolidated-amount)>0.01:
            return jsonify(error=f'El vehículo tiene un costo consolidado de L {consolidated:,.2f}, distinto al cambio original de L {amount:,.2f}. Primero regularice los costos adicionales para poder devolverlo.'),409
        provider_id=asegurar_proveedor_desde_cliente(c,{'nombre':link['cliente_nombre'] or 'Cliente de cambio'})
        label=f"Cambio devuelto · {vehicle['vin']}"
        cur=c.execute('''INSERT INTO cuentas_por_cobrar
            (venta_id,financiera_nombre,fecha,monto_original,saldo,estado,cliente_id,fecha_vencimiento,tipo_cxc)
            VALUES(?,?,?,?,?,?,?,?,?)''',
            (link['venta_id'],label,fecha,amount,amount,'Pendiente',link['cliente_id'],fecha,'Cambio devuelto'))
        cxc_id=cur.lastrowid
        cur=c.execute('''INSERT INTO devoluciones_cambio
            (vehiculo_devuelto_id,venta_origen_id,pago_venta_id,cliente_id,proveedor_id,cuenta_por_cobrar_id,fecha,motivo,monto_original)
            VALUES(?,?,?,?,?,?,?,?,?)''',(vid,link['venta_id'],link['pago_venta_id'],link['cliente_id'],provider_id,cxc_id,fecha,motivo,amount))
        return_id=cur.lastrowid
        old_state=vehicle['estado']
        c.execute("UPDATE vehiculos SET estado='Devuelto' WHERE id=?",(vid,))
        add_movimiento(c,vid,fecha,'Devolución de vehículo recibido en cambio',old_state,'Devuelto',vehicle['ubicacion'],vehicle['ubicacion'],
            referencia=f"Devolución #{return_id}",observaciones=f'{motivo}. Se generó CxC #{cxc_id} por L {amount:,.2f}.')
        registrar_asiento(c,fecha,f"Devolución de cambio · {vehicle['vin']}",'devolucion_cambio',return_id,[
            {'cuenta_id':cuenta_contable_id(c,'cxc'),'debe':amount},
            {'cuenta_id':cuenta_contable_id(c,cuenta_inventario_por_estado(old_state)),'haber':amount},
        ],vid)
        c.commit()
        return jsonify(ok=True,devolucion_id=return_id,cuenta_por_cobrar_id=cxc_id,monto=amount,estado='CxC pendiente'),201
    except sqlite3.IntegrityError:
        c.rollback(); return jsonify(error='Ya existe una devolución registrada para este vehículo.'),409
    except Exception:
        c.rollback(); raise
    finally: c.close()

def es_vehiculo_recibido_cambio(vehicle):
    """Indica si la ficha corresponde a una unidad recibida como cambio."""
    return (vehicle['tipo_compra'] or '').strip().casefold()=='cambio'

def buscar_vinculo_pago_cambio(c, vehicle):
    """Obtiene el pago Cambio asociado a una unidad, sin inventar documentos.

    El segundo valor señala que el vínculo se puede regularizar de manera
    determinista al confirmar la devolución. Se admite únicamente una venta
    explícita en las observaciones y un único pago Cambio aún no vinculado.
    """
    direct=c.execute('''SELECT pv.id pago_venta_id,pv.monto,ve.id venta_id,ve.numero_transaccion,ve.factura,
                ve.cliente_id,cl.nombre cliente_nombre
            FROM pagos_venta pv JOIN ventas ve ON ve.id=pv.venta_id
            LEFT JOIN clientes cl ON cl.id=ve.cliente_id
            WHERE pv.vehiculo_recibido_id=? AND pv.tipo_pago='Cambio'
            ORDER BY pv.id DESC LIMIT 1''',(vehicle['id'],)).fetchone()
    if direct:
        return direct,False
    if not es_vehiculo_recibido_cambio(vehicle):
        return None,False
    reference=re.search(r'\bV-\d+\b',vehicle['observaciones'] or '',re.IGNORECASE)
    if not reference:
        return None,False
    candidates=c.execute('''SELECT pv.id pago_venta_id,pv.monto,ve.id venta_id,ve.numero_transaccion,ve.factura,
                ve.cliente_id,cl.nombre cliente_nombre
            FROM pagos_venta pv JOIN ventas ve ON ve.id=pv.venta_id
            LEFT JOIN clientes cl ON cl.id=ve.cliente_id
            WHERE UPPER(COALESCE(ve.numero_transaccion,''))=?
              AND pv.tipo_pago='Cambio' AND pv.vehiculo_recibido_id IS NULL
            ORDER BY pv.id''',(reference.group(0).upper(),)).fetchall()
    return (candidates[0],True) if len(candidates)==1 else (None,False)

@app.post('/api/devoluciones-cambio/<int:return_id>/reemplazo')
def registrar_reemplazo_devolucion_cambio(return_id):
    """Recibe el reemplazo y lo compensa contra la CxC de la devolución."""
    data=request.get_json(silent=True) or {}
    fecha=(data.get('fecha') or ahora_operacion().date().isoformat()).strip()
    try: datetime.strptime(fecha,'%Y-%m-%d')
    except ValueError: return jsonify(error='La fecha debe tener formato AAAA-MM-DD.'),400
    c=db()
    try:
        change=c.execute('''SELECT dc.*,cc.saldo cxc_saldo,cc.estado cxc_estado,cl.nombre cliente_nombre,
                ve.numero_transaccion FROM devoluciones_cambio dc
            JOIN cuentas_por_cobrar cc ON cc.id=dc.cuenta_por_cobrar_id
            LEFT JOIN clientes cl ON cl.id=dc.cliente_id
            JOIN ventas ve ON ve.id=dc.venta_origen_id WHERE dc.id=?''',(return_id,)).fetchone()
        if not change: return jsonify(error='Devolución de cambio no encontrada.'),404
        if change['vehiculo_reemplazo_id']: return jsonify(error='Esta devolución ya tiene un vehículo de reemplazo registrado.'),409
        if float(change['cxc_saldo'] or 0)<=0.01: return jsonify(error='La CxC de esta devolución no tiene saldo disponible.'),409
        customer={'nombre':change['cliente_nombre'] or 'Cliente de cambio'}
        new_vehicle_id,acquisition_id,value=registrar_vehiculo_recibido_cambio(c,data,fecha,customer,change['numero_transaccion'] or f'Devolución #{return_id}')
        applied=min(round(float(change['cxc_saldo']),2),value)
        excess=round(max(value-applied,0),2)
        c.execute('''UPDATE adquisiciones SET anticipo=?,saldo=?,condicion_pago=?,observaciones=? WHERE id=?''',
            (applied,excess,'Crédito' if excess else 'Contado',
             f"Vehículo de reemplazo de devolución #{return_id}; CxC aplicada: {applied:.2f}.",acquisition_id))
        cxp_id=None
        if applied:
            cur=c.execute('''INSERT INTO aplicaciones_cuentas_por_cobrar(cuenta_por_cobrar_id,fecha,monto,tipo,referencia)
                VALUES(?,?,?,?,?)''',(change['cuenta_por_cobrar_id'],fecha,applied,'Vehículo de reemplazo',data.get('vin','').strip().upper()))
        cxc_remaining=round(max(float(change['cxc_saldo'])-applied,0),2)
        cxc_state='Cobrada' if cxc_remaining<=0.01 else 'Pendiente'
        c.execute('UPDATE cuentas_por_cobrar SET saldo=?,estado=? WHERE id=?',(cxc_remaining,cxc_state,change['cuenta_por_cobrar_id']))
        if excess:
            cur=c.execute('''INSERT INTO cuentas_por_pagar(adquisicion_id,proveedor_id,fecha,monto_original,saldo,estado)
                VALUES(?,?,?,?,?,'Pendiente')''',(acquisition_id,change['proveedor_id'],fecha,excess,excess))
            cxp_id=cur.lastrowid
        change_state='CxP pendiente' if excess else ('Compensada' if cxc_remaining<=0.01 else 'CxC pendiente')
        c.execute('''UPDATE devoluciones_cambio SET vehiculo_reemplazo_id=?,cuenta_por_pagar_id=?,monto_reemplazo=?,estado=? WHERE id=?''',
            (new_vehicle_id,cxp_id,value,change_state,return_id))
        lines=[{'cuenta_id':cuenta_contable_id(c,'inventario_transito'),'debe':value}]
        if applied: lines.append({'cuenta_id':cuenta_contable_id(c,'cxc'),'haber':applied})
        if excess: lines.append({'cuenta_id':cuenta_contable_id(c,'cxp'),'haber':excess})
        registrar_asiento(c,fecha,f"Vehículo de reemplazo · devolución #{return_id}",'reemplazo_devolucion_cambio',return_id,lines,new_vehicle_id)
        add_movimiento(c,new_vehicle_id,fecha,'Ingreso como reemplazo de cambio',None,'En Tránsito',None,None,
            referencia=f"Devolución #{return_id}",observaciones=f'CxC aplicada: {applied:.2f}; CxP generada: {excess:.2f}.')
        c.commit()
        return jsonify(ok=True,vehiculo_id=new_vehicle_id,adquisicion_id=acquisition_id,cuenta_por_cobrar_id=change['cuenta_por_cobrar_id'],
                       saldo_cxc=cxc_remaining,cuenta_por_pagar_id=cxp_id,monto_cxp=excess,estado=change_state),201
    except ValueError as error:
        c.rollback(); return jsonify(error=str(error)),400
    except sqlite3.IntegrityError:
        c.rollback(); return jsonify(error='No fue posible registrar el reemplazo; verifique VIN, placa y documentos relacionados.'),409
    except Exception:
        c.rollback(); raise
    finally: c.close()

def referencia_pago_disponible(c, referencia):
    if not referencia:
        return True
    for table in ('pagos_venta','pagos_cuentas_por_pagar','cobros_cuentas_por_cobrar'):
        if c.execute(f"SELECT 1 FROM {table} WHERE referencia=? LIMIT 1",(referencia,)).fetchone():
            return False
    return True

@app.post('/api/cuentas-por-pagar/<int:cid>/pagos')
def registrar_pago_cxp(cid):
    data=request.get_json(silent=True) or {}; c=db(); account=c.execute('''SELECT cp.*,p.nombre proveedor_nombre FROM cuentas_por_pagar cp
        JOIN proveedores p ON p.id=cp.proveedor_id WHERE cp.id=?''',(cid,)).fetchone()
    if not account: c.close(); return jsonify(error='Cuenta por pagar no encontrada'),404
    values,error=validar_pago_cartera(data,account['saldo'])
    if error: c.close(); return jsonify(error=error),400
    monto,tipo,banco,referencia=values; fecha=data.get('fecha') or datetime.now().strftime('%Y-%m-%d'); saldo=round(float(account['saldo'])-monto,2); estado='Pagada' if saldo<=0.01 else 'Pendiente'
    if not referencia_pago_disponible(c,referencia): c.close(); return jsonify(error='El número de referencia ya fue utilizado'),409
    try:
        cur=c.execute('''INSERT INTO pagos_cuentas_por_pagar(cuenta_por_pagar_id,fecha,tipo_pago,banco,referencia,monto) VALUES(?,?,?,?,?,?)''',(cid,fecha,tipo,banco,referencia,monto))
    except sqlite3.IntegrityError: c.close(); return jsonify(error='El número de referencia ya fue utilizado'),409
    c.execute('UPDATE cuentas_por_pagar SET saldo=?,estado=? WHERE id=?',(max(saldo,0),estado,cid))
    medio,banco_caja=medio_caja(tipo,banco); descripcion=f"Pago a proveedor · {account['proveedor_nombre']}"
    registrar_movimiento_caja(c,fecha,'Pago de cuenta por pagar',medio,banco_caja,0,monto,descripcion,'pago_cxp',cur.lastrowid)
    lineas=[{'cuenta_id':cuenta_contable_id(c,'cxp'),'debe':monto},{'cuenta_id':cuenta_contable_id(c,'caja_bancos'),'haber':monto}]
    registrar_asiento(c,fecha,descripcion,'pago_cxp',cur.lastrowid,lineas)
    c.commit(); c.close(); return jsonify(ok=True,saldo=max(saldo,0),estado=estado),201

@app.post('/api/cuentas-por-pagar-contratos/<int:cid>/pagos')
def registrar_pago_cxp_contrato(cid):
    data=request.get_json(silent=True) or {}; c=db(); account=c.execute('''SELECT cp.*,co.numero_contrato,p.nombre proveedor_nombre
        FROM cuentas_por_pagar_contratos cp JOIN contratos co ON co.id=cp.contrato_id JOIN proveedores p ON p.id=co.proveedor_id WHERE cp.id=?''',(cid,)).fetchone()
    if not account: c.close(); return jsonify(error='Cuenta por pagar de contrato no encontrada'),404
    values,error=validar_pago_cartera(data,account['saldo'])
    if error: c.close(); return jsonify(error=error),400
    monto,tipo,banco,referencia=values; fecha=data.get('fecha') or ahora_operacion().date().isoformat(); saldo=round(float(account['saldo'])-monto,2); estado='Pagada' if saldo<=0.01 else 'Pendiente'
    if not referencia_pago_disponible(c,referencia): c.close(); return jsonify(error='El número de referencia ya fue utilizado'),409
    try:
        cur=c.execute('''INSERT INTO pagos_cuentas_por_pagar_contratos(cuenta_por_pagar_contrato_id,fecha,tipo_pago,banco,referencia,monto) VALUES(?,?,?,?,?,?)''',(cid,fecha,tipo,banco,referencia,monto))
        c.execute('UPDATE cuentas_por_pagar_contratos SET saldo=?,estado=? WHERE id=?',(max(saldo,0),estado,cid))
        descripcion=f"Pago de contrato {account['numero_contrato']} · {account['proveedor_nombre']}"
        medio,banco_caja=medio_caja(tipo,banco); registrar_movimiento_caja(c,fecha,'Pago de contrato',medio,banco_caja,0,monto,descripcion,'pago_cxp_contrato',cur.lastrowid)
        registrar_asiento(c,fecha,descripcion,'pago_cxp_contrato',cur.lastrowid,[{'cuenta_id':cuenta_contable_id(c,'cxp'),'debe':monto},{'cuenta_id':cuenta_contable_id(c,'caja_bancos'),'haber':monto}])
        c.commit(); return jsonify(ok=True,saldo=max(saldo,0),estado=estado),201
    except sqlite3.IntegrityError:
        c.rollback(); return jsonify(error='El número de referencia ya fue utilizado'),409
    finally:c.close()

@app.post('/api/cuentas-por-pagar-taller/<int:cid>/pagos')
def registrar_pago_cxp_taller(cid):
    data=request.get_json(silent=True) or {}; c=db(); account=c.execute('''SELECT cp.*,p.nombre proveedor_nombre,o.numero_ot,o.vehiculo_id
        FROM cuentas_por_pagar_ot cp JOIN proveedores p ON p.id=cp.proveedor_id
        JOIN ordenes_trabajo o ON o.id=cp.orden_trabajo_id WHERE cp.id=?''',(cid,)).fetchone()
    if not account: c.close(); return jsonify(error='Cuenta por pagar de taller no encontrada'),404
    values,error=validar_pago_cartera(data,account['saldo'])
    if error: c.close(); return jsonify(error=error),400
    monto,tipo,banco,referencia=values; fecha=data.get('fecha') or datetime.now().strftime('%Y-%m-%d'); saldo=round(float(account['saldo'])-monto,2); estado='Pagada' if saldo<=0.01 else 'Pendiente'
    if not referencia_pago_disponible(c,referencia): c.close(); return jsonify(error='El número de referencia ya fue utilizado'),409
    try:
        cur=c.execute('''INSERT INTO pagos_cuentas_por_pagar_ot(cuenta_por_pagar_ot_id,fecha,tipo_pago,banco,referencia,monto) VALUES(?,?,?,?,?,?)''',(cid,fecha,tipo,banco,referencia,monto))
    except sqlite3.IntegrityError: c.close(); return jsonify(error='El número de referencia ya fue utilizado'),409
    c.execute('UPDATE cuentas_por_pagar_ot SET saldo=?,estado=? WHERE id=?',(max(saldo,0),estado,cid))
    medio,banco_caja=medio_caja(tipo,banco); descripcion=f"Pago OT {account['numero_ot']} · {account['proveedor_nombre']}"
    registrar_movimiento_caja(c,fecha,'Pago de cuenta por pagar · OT',medio,banco_caja,0,monto,descripcion,'pago_cxp_ot',cur.lastrowid)
    lineas=[{'cuenta_id':cuenta_contable_id(c,'cxp'),'debe':monto},{'cuenta_id':cuenta_contable_id(c,'caja_bancos'),'haber':monto}]
    registrar_asiento(c,fecha,descripcion,'pago_cxp_ot',cur.lastrowid,lineas,account['vehiculo_id'])
    c.commit(); c.close(); return jsonify(ok=True,saldo=max(saldo,0),estado=estado),201

@app.post('/api/cuentas-por-pagar-repuestos/<int:cid>/pagos')
def registrar_pago_cxp_repuesto(cid):
    data=request.get_json(silent=True) or {}; c=db(); account=c.execute('''SELECT cp.*,p.nombre proveedor_nombre,r.factura,o.numero_ot,o.vehiculo_id
        FROM cuentas_por_pagar_repuestos_ot cp JOIN proveedores p ON p.id=cp.proveedor_id
        JOIN repuestos_ot r ON r.id=cp.repuesto_ot_id JOIN ordenes_trabajo o ON o.id=r.orden_trabajo_id WHERE cp.id=?''',(cid,)).fetchone()
    if not account: c.close(); return jsonify(error='Cuenta por pagar de repuesto no encontrada'),404
    values,error=validar_pago_cartera(data,account['saldo'])
    if error: c.close(); return jsonify(error=error),400
    monto,tipo,banco,referencia=values; fecha=data.get('fecha') or datetime.now().strftime('%Y-%m-%d'); saldo=round(float(account['saldo'])-monto,2); estado='Pagada' if saldo<=0.01 else 'Pendiente'
    if not referencia_pago_disponible(c,referencia): c.close(); return jsonify(error='El número de referencia ya fue utilizado'),409
    try:
        cur=c.execute('''INSERT INTO pagos_cuentas_por_pagar_repuestos_ot(cuenta_por_pagar_repuesto_id,fecha,tipo_pago,banco,referencia,monto) VALUES(?,?,?,?,?,?)''',(cid,fecha,tipo,banco,referencia,monto))
    except sqlite3.IntegrityError: c.close(); return jsonify(error='El número de referencia ya fue utilizado'),409
    c.execute('UPDATE cuentas_por_pagar_repuestos_ot SET saldo=?,estado=? WHERE id=?',(max(saldo,0),estado,cid))
    medio,banco_caja=medio_caja(tipo,banco); descripcion=f"Pago repuesto {account['factura']} · {account['proveedor_nombre']}"
    registrar_movimiento_caja(c,fecha,'Pago de cuenta por pagar · repuesto',medio,banco_caja,0,monto,descripcion,'pago_cxp_repuesto',cur.lastrowid)
    lineas=[{'cuenta_id':cuenta_contable_id(c,'cxp'),'debe':monto},{'cuenta_id':cuenta_contable_id(c,'caja_bancos'),'haber':monto}]
    registrar_asiento(c,fecha,descripcion,'pago_cxp_repuesto',cur.lastrowid,lineas,account['vehiculo_id'])
    c.commit(); c.close(); return jsonify(ok=True,saldo=max(saldo,0),estado=estado),201

@app.delete('/api/cuentas-por-pagar-repuestos/<int:cid>/pagos/<int:payment_id>')
def reversar_pago_cxp_repuesto(cid,payment_id):
    """Deshace un pago registrado por error sin borrar la factura de repuesto.

    Se elimina exclusivamente el pago, su salida de caja y su asiento; la CxP
    vuelve a quedar pendiente por el importe reversado.
    """
    c=db(); account=c.execute('''SELECT cp.*,r.factura,o.numero_ot,o.vehiculo_id,p.nombre proveedor_nombre
        FROM cuentas_por_pagar_repuestos_ot cp
        JOIN repuestos_ot r ON r.id=cp.repuesto_ot_id
        JOIN ordenes_trabajo o ON o.id=r.orden_trabajo_id
        JOIN proveedores p ON p.id=cp.proveedor_id WHERE cp.id=?''',(cid,)).fetchone()
    payment=c.execute('''SELECT * FROM pagos_cuentas_por_pagar_repuestos_ot
        WHERE id=? AND cuenta_por_pagar_repuesto_id=?''',(payment_id,cid)).fetchone()
    if not account or not payment:
        c.close(); return jsonify(error='No se encontró el pago de CxP de repuesto.'),404
    try:
        c.execute('BEGIN')
        c.execute("DELETE FROM movimientos_caja WHERE referencia_tipo='pago_cxp_repuesto' AND referencia_id=?",(payment_id,))
        headers=c.execute("SELECT id FROM asientos_contables WHERE referencia_tipo='pago_cxp_repuesto' AND referencia_id=?",(payment_id,)).fetchall()
        for header in headers:
            c.execute('DELETE FROM partidas WHERE asiento_id=?',(header['id'],))
            c.execute('DELETE FROM asientos_contables WHERE id=?',(header['id'],))
        c.execute('DELETE FROM pagos_cuentas_por_pagar_repuestos_ot WHERE id=?',(payment_id,))
        saldo=min(float(account['monto_original'] or 0),round(float(account['saldo'] or 0)+float(payment['monto'] or 0),2))
        estado='Pagada' if saldo<=0.01 else 'Pendiente'
        c.execute('UPDATE cuentas_por_pagar_repuestos_ot SET saldo=?,estado=? WHERE id=?',(saldo,estado,cid))
        c.commit()
    except Exception:
        c.rollback(); c.close(); raise
    c.close(); return jsonify(ok=True,saldo=saldo,estado=estado,documento=account['factura'])

@app.post('/api/cuentas-por-cobrar/<int:cid>/pagos')
def registrar_cobro_cxc(cid):
    data=request.get_json(silent=True) or {}; c=db(); account=c.execute('SELECT * FROM cuentas_por_cobrar WHERE id=?',(cid,)).fetchone()
    if not account: c.close(); return jsonify(error='Cuenta por cobrar no encontrada'),404
    values,error=validar_pago_cartera(data,account['saldo'])
    if error: c.close(); return jsonify(error=error),400
    monto,tipo,banco,referencia=values; fecha=data.get('fecha') or datetime.now().strftime('%Y-%m-%d'); saldo=round(float(account['saldo'])-monto,2); estado='Cobrada' if saldo<=0.01 else 'Pendiente'
    if not referencia_pago_disponible(c,referencia): c.close(); return jsonify(error='El número de referencia ya fue utilizado'),409
    try:
        cur=c.execute('''INSERT INTO cobros_cuentas_por_cobrar(cuenta_por_cobrar_id,fecha,tipo_pago,banco,referencia,monto) VALUES(?,?,?,?,?,?)''',(cid,fecha,tipo,banco,referencia,monto))
    except sqlite3.IntegrityError: c.close(); return jsonify(error='El número de referencia ya fue utilizado'),409
    c.execute('UPDATE cuentas_por_cobrar SET saldo=?,estado=? WHERE id=?',(max(saldo,0),estado,cid))
    medio,banco_caja=medio_caja(tipo,banco); descripcion=f"Cobro de cuenta por cobrar · {account['financiera_nombre']}"
    registrar_movimiento_caja(c,fecha,'Cobro de cuenta por cobrar',medio,banco_caja,monto,0,descripcion,'cobro_cxc',cur.lastrowid)
    lineas=[{'cuenta_id':cuenta_contable_id(c,'caja_bancos'),'debe':monto},{'cuenta_id':cuenta_contable_id(c,'cxc'),'haber':monto}]
    registrar_asiento(c,fecha,descripcion,'cobro_cxc',cur.lastrowid,lineas)
    c.commit(); c.close(); return jsonify(ok=True,saldo=max(saldo,0),estado=estado),201

@app.get('/api/caja')
def caja():
    desde,hasta=rango_reporte(); where,args=filtro_fechas('fecha',desde,hasta); c=db(); sql='SELECT medio,COALESCE(banco,\'\') banco,COALESCE(SUM(entrada),0) entrada,COALESCE(SUM(salida),0) salida FROM movimientos_caja'
    if where: sql+=' WHERE '+' AND '.join(where)
    raw_groups=[dict(row) for row in c.execute(sql+' GROUP BY medio,COALESCE(banco,\'\') ORDER BY medio,banco',args).fetchall()]
    grouped={}
    for row in raw_groups:
        row['banco']=normalizar_nombre_banco(row['banco'])
        key=(row['medio'],row['banco'])
        summary=grouped.setdefault(key,{'medio':row['medio'],'banco':row['banco'],'entrada':0,'salida':0})
        summary['entrada']+=float(row['entrada'] or 0); summary['salida']+=float(row['salida'] or 0)
    groups=[]
    for row in grouped.values():
        row['entrada']=round(row['entrada'],2); row['salida']=round(row['salida'],2); row['saldo']=round(row['entrada']-row['salida'],2)
        groups.append(row)
    groups.sort(key=lambda row:(row['medio'],row['banco']))
    moves_sql='SELECT * FROM movimientos_caja'+((' WHERE '+' AND '.join(where)) if where else '')+' ORDER BY fecha DESC,id DESC'
    moves=[dict(row) for row in c.execute(moves_sql,args).fetchall()]
    for move in moves: move['banco']=normalizar_nombre_banco(move['banco'])
    efectivo=sum(row['saldo'] for row in groups if row['medio']=='Efectivo'); bancos=[row for row in groups if row['medio']=='Banco']
    c.close(); return jsonify(efectivo=efectivo,bancos=bancos,movimientos=moves)

@app.get('/api/areas')
def get_areas():
    c=db(); r=c.execute('SELECT * FROM areas WHERE activo=1 ORDER BY codigo').fetchall(); c.close(); return jsonify([dict(x) for x in r])

@app.get('/api/departamentos')
def get_deps():
    area=request.args.get('area_id'); c=db(); sql='''SELECT d.*,a.nombre area_nombre,
        CASE WHEN a.nombre IN ('Administración','Finanzas') THEN 'Gasto Administrativo' ELSE 'Gasto de Venta' END grupo_resultado
        FROM departamentos d JOIN areas a ON a.id=d.area_id WHERE d.activo=1'''; args=[]
    if area: sql+=' AND area_id=?'; args.append(area)
    r=c.execute(sql+' ORDER BY d.area_id,d.codigo',args).fetchall(); c.close(); return jsonify([dict(x) for x in r])

def cuenta_salarios_departamento(c, departamento_id):
    departamento=c.execute('''SELECT a.nombre area_nombre FROM departamentos d
        JOIN areas a ON a.id=d.area_id WHERE d.id=? AND d.activo=1''',(departamento_id,)).fetchone()
    if not departamento:
        raise ValueError('El departamento seleccionado no está activo.')
    codigo='6201' if departamento['area_nombre'] in ('Administración','Finanzas') else '6101'
    cuenta=c.execute('SELECT id FROM cuentas_contables WHERE codigo=? AND activo=1 AND acepta_movimiento=1',(codigo,)).fetchone()
    if not cuenta:
        raise ValueError(f'No existe una cuenta activa de salarios para el grupo del departamento ({codigo}).')
    return cuenta['id']

def datos_empleado_planilla(data):
    """Valida el maestro de planillas sin crear movimientos contables."""
    nombre=(data.get('nombre') or '').strip()
    identidad=(data.get('numero_identidad') or '').strip()
    fecha_ingreso=fecha_planilla(data.get('fecha_ingreso'))
    if not nombre or not identidad or not fecha_ingreso:
        return None, 'Nombre, número de identidad y fecha de ingreso son obligatorios.'
    try:
        values={key:round(float(data.get(key) or 0),2) for key in
                ('salario','comisiones','bonos','ihss','rap','otras_deducciones')}
        departamento_id=int(data.get('departamento_id') or 0)
        cuenta_contable_id=int(data.get('cuenta_contable_id') or 0)
    except (TypeError,ValueError):
        return None, 'Los importes, el departamento y la cuenta contable deben ser válidos.'
    if any(value<0 for value in values.values()):
        return None, 'Los importes de planilla no pueden ser negativos.'
    if not departamento_id or not cuenta_contable_id:
        return None, 'Seleccione un departamento y una cuenta contable.'
    values.update(nombre=nombre,numero_identidad=identidad,fecha_ingreso=fecha_ingreso.isoformat(),departamento_id=departamento_id,cuenta_contable_id=cuenta_contable_id)
    return values, None

def datos_contrato(data):
    numero=(data.get('numero_contrato') or '').strip(); fecha_inicio=fecha_planilla(data.get('fecha_inicio')); fecha_fin=fecha_planilla(data.get('fecha_fin'))
    try:
        proveedor_id=int(data.get('proveedor_id') or 0); cuenta_id=int(data.get('cuenta_contable_id') or 0)
        monto=round(float(data.get('monto_mensual') or 0),2); dia=int(data.get('dia_pago') or 0)
    except (TypeError,ValueError):
        return None,'Proveedor, cuenta, monto mensual y fecha de pago son obligatorios.'
    if not numero or not fecha_inicio or not fecha_fin or fecha_fin<fecha_inicio or not proveedor_id or not cuenta_id or monto<=0 or not 1<=dia<=31:
        return None,'Revise el número, la vigencia, el monto, el día de pago, el proveedor y la cuenta contable.'
    moneda=(data.get('moneda') or 'L').strip().upper()
    if moneda not in ('L','USD'): return None,'Seleccione una moneda válida.'
    return {'numero_contrato':numero,'proveedor_id':proveedor_id,'fecha_inicio':fecha_inicio.isoformat(),'fecha_fin':fecha_fin.isoformat(),
            'monto_mensual':monto,'moneda':moneda,'dia_pago':dia,'cuenta_contable_id':cuenta_id},None

def fecha_vencimiento_contrato(year,month,day):
    return datetime(year,month,min(day,calendar.monthrange(year,month)[1])).date()

def generar_cxp_contratos_programadas(c, fecha=None):
    """Genera una obligación mensual cuando faltan 10 días o menos para su pago."""
    hoy=fecha or ahora_operacion().date()
    contratos=c.execute('SELECT * FROM contratos WHERE activo=1').fetchall(); creadas=0
    meses=[(hoy.year,hoy.month)]
    siguiente=(hoy.replace(day=28)+timedelta(days=4)).replace(day=1)
    meses.append((siguiente.year,siguiente.month))
    for contrato in contratos:
        if contrato['moneda']!='L':
            continue
        inicio=fecha_planilla(contrato['fecha_inicio']); fin=fecha_planilla(contrato['fecha_fin'])
        for year,month in meses:
            vence=fecha_vencimiento_contrato(year,month,int(contrato['dia_pago']))
            if not inicio<=vence<=fin or hoy<vence-timedelta(days=10): continue
            periodo=f'{year:04d}-{month:02d}'
            if c.execute('SELECT 1 FROM cuentas_por_pagar_contratos WHERE contrato_id=? AND periodo=?',(contrato['id'],periodo)).fetchone(): continue
            cur=c.execute('''INSERT INTO cuentas_por_pagar_contratos(contrato_id,periodo,fecha,monto_original,saldo,estado)
                VALUES(?,?,?,?,?,'Pendiente')''',(contrato['id'],periodo,vence.isoformat(),contrato['monto_mensual'],contrato['monto_mensual']))
            descripcion=f"Contrato {contrato['numero_contrato']} · obligación {periodo}"
            registrar_asiento(c,hoy.isoformat(),descripcion,'contrato_cxp',cur.lastrowid,[
                {'cuenta_id':contrato['cuenta_contable_id'],'debe':contrato['monto_mensual']},
                {'cuenta_id':cuenta_contable_id(c,'cxp'),'haber':contrato['monto_mensual']},
            ])
            creadas+=1
    return creadas

@app.get('/api/contratos')
def get_contratos():
    c=db()
    try:
        generar_cxp_contratos_programadas(c); c.commit()
        rows=c.execute('''SELECT co.*,p.nombre proveedor_nombre,cc.codigo cuenta_codigo,cc.cuenta cuenta_nombre,
            COALESCE((SELECT SUM(monto) FROM contratos_depositos_garantia dg WHERE dg.contrato_id=co.id),0) depositos_garantia
            FROM contratos co JOIN proveedores p ON p.id=co.proveedor_id JOIN cuentas_contables cc ON cc.id=co.cuenta_contable_id
            ORDER BY co.activo DESC,co.fecha_fin DESC,co.id DESC''').fetchall()
        return jsonify([dict(row) for row in rows])
    finally: c.close()

@app.post('/api/contratos')
def crear_contrato():
    data,error=datos_contrato(request.get_json(silent=True) or {})
    if error:return jsonify(error=error),400
    c=db()
    try:
        if not c.execute('SELECT 1 FROM proveedores WHERE id=? AND activo=1',(data['proveedor_id'],)).fetchone(): return jsonify(error='Seleccione un proveedor activo.'),400
        if not c.execute('SELECT 1 FROM cuentas_contables WHERE id=? AND activo=1 AND acepta_movimiento=1',(data['cuenta_contable_id'],)).fetchone(): return jsonify(error='Seleccione una cuenta contable activa que acepte movimientos.'),400
        cur=c.execute('''INSERT INTO contratos(numero_contrato,proveedor_id,fecha_inicio,fecha_fin,monto_mensual,moneda,dia_pago,cuenta_contable_id)
            VALUES(:numero_contrato,:proveedor_id,:fecha_inicio,:fecha_fin,:monto_mensual,:moneda,:dia_pago,:cuenta_contable_id)''',data)
        c.commit(); return jsonify(id=cur.lastrowid),201
    except sqlite3.IntegrityError:
        c.rollback(); return jsonify(error='Ya existe un contrato con ese número.'),409
    finally:c.close()

@app.post('/api/contratos/<int:contract_id>/depositos-garantia')
def crear_deposito_garantia(contract_id):
    data=request.get_json(silent=True) or {}; fecha=fecha_planilla(data.get('fecha'))
    try:monto=round(float(data.get('monto') or 0),2)
    except (TypeError,ValueError):monto=0
    if not fecha or monto<=0:return jsonify(error='Indique una fecha y un monto de depósito válidos.'),400
    c=db()
    try:
        contrato=c.execute('SELECT moneda FROM contratos WHERE id=?',(contract_id,)).fetchone()
        if not contrato:return jsonify(error='Contrato no encontrado.'),404
        # La moneda se toma del contrato; nunca se acepta una distinta desde pantalla o API.
        moneda=contrato['moneda'] or 'L'
        cur=c.execute('''INSERT INTO contratos_depositos_garantia(contrato_id,fecha,monto,moneda,descripcion)
            VALUES(?,?,?,?,?)''',(contract_id,fecha.isoformat(),monto,moneda,(data.get('descripcion') or '').strip() or None))
        c.commit();return jsonify(id=cur.lastrowid,moneda=moneda),201
    finally:c.close()

@app.get('/api/cuentas-por-pagar-contratos')
def cuentas_por_pagar_contratos():
    c=db()
    try:
        generar_cxp_contratos_programadas(c);c.commit()
        rows=c.execute('''SELECT cp.*,co.numero_contrato,p.nombre proveedor_nombre FROM cuentas_por_pagar_contratos cp
            JOIN contratos co ON co.id=cp.contrato_id JOIN proveedores p ON p.id=co.proveedor_id ORDER BY cp.fecha DESC,cp.id DESC''').fetchall()
        return jsonify([dict(row) for row in rows])
    finally:c.close()

def empleado_planilla_detalle(c, employee_id):
    row=c.execute('''SELECT ep.*,d.codigo departamento_codigo,d.nombre departamento_nombre,a.nombre area_nombre,
        CASE WHEN a.nombre IN ('Administración','Finanzas') THEN 'Gasto Administrativo' ELSE 'Gasto de Venta' END grupo_resultado,
        cc.codigo cuenta_codigo,cc.cuenta cuenta_nombre
        FROM empleados_planilla ep
        JOIN departamentos d ON d.id=ep.departamento_id JOIN areas a ON a.id=d.area_id
        JOIN cuentas_contables cc ON cc.id=ep.cuenta_contable_id
        WHERE ep.id=?''',(employee_id,)).fetchone()
    return dict(row) if row else None

def fecha_planilla(value):
    try:
        return datetime.strptime(str(value), '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return None

def periodo_quincenal(fecha_referencia):
    """Devuelve la quincena fija (1–15 o 16–fin de mes) de una fecha."""
    fecha=fecha_planilla(fecha_referencia)
    if not fecha:
        return None
    inicio=fecha.replace(day=1 if fecha.day<=15 else 16)
    fin=fecha.replace(day=15 if fecha.day<=15 else calendar.monthrange(fecha.year,fecha.month)[1])
    return inicio.isoformat(),fin.isoformat()

def calcular_planilla_quincenal(c, fecha_referencia):
    periodo=periodo_quincenal(fecha_referencia)
    if not periodo:
        return None, 'La fecha de referencia no es válida.'
    inicio,fin=periodo
    empleados=[dict(row) for row in c.execute('''SELECT ep.*,d.nombre departamento_nombre,
        cc.codigo cuenta_codigo,cc.cuenta cuenta_nombre
        FROM empleados_planilla ep
        JOIN departamentos d ON d.id=ep.departamento_id
        JOIN cuentas_contables cc ON cc.id=ep.cuenta_contable_id
        WHERE ep.activo=1 ORDER BY ep.nombre COLLATE NOCASE,ep.id''').fetchall()]
    variables={row['empleado_id']:{'comisiones':float(row['monto'] or 0),'bonos':float(row['bono'] or 0)} for row in c.execute('''SELECT empleado_id,SUM(monto) monto,SUM(bono) bono
        FROM comisiones_planilla WHERE fecha BETWEEN ? AND ? GROUP BY empleado_id''',(inicio,fin)).fetchall()}
    detalles=[]
    for empleado in empleados:
        salario=round(float(empleado['salario'] or 0)/2,2)
        comisiones_fijas=round(float(empleado['comisiones'] or 0)/2,2)
        comisiones_registradas=round(variables.get(empleado['id'],{}).get('comisiones',0),2)
        total_comisiones=round(comisiones_fijas+comisiones_registradas,2)
        bonos_registrados=round(variables.get(empleado['id'],{}).get('bonos',0),2)
        bonos=round(float(empleado['bonos'] or 0)/2+bonos_registrados,2)
        ihss=round(float(empleado['ihss'] or 0)/2,2)
        rap=round(float(empleado['rap'] or 0)/2,2)
        otras=round(float(empleado['otras_deducciones'] or 0)/2,2)
        devengado=round(salario+total_comisiones+bonos,2)
        deducciones=round(ihss+rap+otras,2)
        detalles.append({
            'empleado_id':empleado['id'],'empleado_nombre':empleado['nombre'],'numero_identidad':empleado['numero_identidad'],
            'departamento_id':empleado['departamento_id'],'cuenta_contable_id':empleado['cuenta_contable_id'],
            'departamento_nombre':empleado['departamento_nombre'],'cuenta_contable_nombre':f"{empleado['cuenta_codigo']} · {empleado['cuenta_nombre']}",
            'salario':salario,'comisiones_fijas':comisiones_fijas,'comisiones_registradas':comisiones_registradas,
            'comisiones':total_comisiones,'bonos_registrados':bonos_registrados,'bonos':bonos,'ihss':ihss,'rap':rap,'otras_deducciones':otras,
            'total_devengado':devengado,'total_deducciones':deducciones,'neto':round(devengado-deducciones,2)
        })
    totales={key:round(sum(detalle[key] for detalle in detalles),2) for key in
        ('salario','comisiones','bonos','total_deducciones','neto')}
    return {'periodo_inicio':inicio,'periodo_fin':fin,'empleados':detalles,'totales':totales},None

@app.get('/api/planillas')
def get_planillas():
    c=db()
    activos=request.args.get('activos') in ('1','true','si')
    rows=c.execute('''SELECT ep.*,d.codigo departamento_codigo,d.nombre departamento_nombre,a.nombre area_nombre,
        CASE WHEN a.nombre IN ('Administración','Finanzas') THEN 'Gasto Administrativo' ELSE 'Gasto de Venta' END grupo_resultado,
        cc.codigo cuenta_codigo,cc.cuenta cuenta_nombre
        FROM empleados_planilla ep
        JOIN departamentos d ON d.id=ep.departamento_id JOIN areas a ON a.id=d.area_id
        JOIN cuentas_contables cc ON cc.id=ep.cuenta_contable_id
        WHERE (?=0 OR ep.activo=1)
        ORDER BY ep.nombre COLLATE NOCASE,ep.id''',(1 if activos else 0,)).fetchall()
    c.close(); return jsonify([dict(row) for row in rows])

@app.post('/api/planillas')
def crear_empleado_planilla():
    data, error=datos_empleado_planilla(request.get_json(silent=True) or {})
    if error: return jsonify(error=error),400
    c=db()
    try:
        if not c.execute('SELECT 1 FROM departamentos WHERE id=? AND activo=1',(data['departamento_id'],)).fetchone():
            return jsonify(error='El departamento seleccionado no está activo.'),400
        data['cuenta_contable_id']=cuenta_salarios_departamento(c,data['departamento_id'])
        cur=c.execute('''INSERT INTO empleados_planilla(nombre,numero_identidad,fecha_ingreso,salario,comisiones,bonos,ihss,rap,otras_deducciones,departamento_id,cuenta_contable_id)
            VALUES(:nombre,:numero_identidad,:fecha_ingreso,:salario,:comisiones,:bonos,:ihss,:rap,:otras_deducciones,:departamento_id,:cuenta_contable_id)''',data)
        c.commit(); employee=empleado_planilla_detalle(c,cur.lastrowid)
    except sqlite3.IntegrityError:
        c.rollback(); return jsonify(error='Ya existe un empleado de planilla con ese número de identidad.'),409
    finally:
        c.close()
    return jsonify(employee),201

@app.post('/api/planillas/<int:employee_id>/baja')
def dar_baja_empleado_planilla(employee_id):
    c=db()
    try:
        employee=empleado_planilla_detalle(c,employee_id)
        if not employee: return jsonify(error='Empleado de planilla no encontrado.'),404
        if not employee['activo']: return jsonify(error='El empleado ya está inactivo.'),409
        c.execute('UPDATE empleados_planilla SET activo=0,actualizado_en=CURRENT_TIMESTAMP WHERE id=?',(employee_id,))
        c.commit(); employee=empleado_planilla_detalle(c,employee_id)
    finally:
        c.close()
    return jsonify(employee)

@app.post('/api/planillas/<int:employee_id>/reactivar')
def reactivar_empleado_planilla(employee_id):
    c=db()
    try:
        employee=empleado_planilla_detalle(c,employee_id)
        if not employee: return jsonify(error='Empleado de planilla no encontrado.'),404
        c.execute('UPDATE empleados_planilla SET activo=1,actualizado_en=CURRENT_TIMESTAMP WHERE id=?',(employee_id,))
        c.commit(); employee=empleado_planilla_detalle(c,employee_id)
    finally:
        c.close()
    return jsonify(employee)

@app.get('/api/planillas/comisiones')
def get_comisiones_planilla():
    c=db()
    rows=c.execute('''SELECT cp.*,ep.nombre empleado_nombre,ep.numero_identidad
        FROM comisiones_planilla cp JOIN empleados_planilla ep ON ep.id=cp.empleado_id
        ORDER BY cp.fecha DESC,cp.id DESC''').fetchall()
    c.close(); return jsonify([dict(row) for row in rows])

@app.post('/api/planillas/comisiones')
def crear_comision_planilla():
    data=request.get_json(silent=True) or {}
    try:
        empleado_id=int(data.get('empleado_id') or 0); monto=round(float(data.get('monto') or 0),2); bono=round(float(data.get('bono') or 0),2)
    except (TypeError,ValueError):
        return jsonify(error='El empleado, la comisión y el bono deben ser valores válidos.'),400
    fecha=fecha_planilla(data.get('fecha'))
    if not empleado_id or not fecha or monto<=0 or bono<0:
        return jsonify(error='Indique un empleado activo, una fecha válida, una comisión mayor que cero y un bono no negativo.'),400
    c=db()
    try:
        if not c.execute('SELECT 1 FROM empleados_planilla WHERE id=? AND activo=1',(empleado_id,)).fetchone():
            return jsonify(error='El empleado seleccionado no está activo.'),400
        cur=c.execute('INSERT INTO comisiones_planilla(empleado_id,fecha,monto,bono,descripcion) VALUES(?,?,?,?,?)',
            (empleado_id,fecha.isoformat(),monto,bono,(data.get('descripcion') or '').strip() or None))
        c.commit()
        row=c.execute('''SELECT cp.*,ep.nombre empleado_nombre,ep.numero_identidad FROM comisiones_planilla cp
            JOIN empleados_planilla ep ON ep.id=cp.empleado_id WHERE cp.id=?''',(cur.lastrowid,)).fetchone()
    finally:
        c.close()
    return jsonify(dict(row)),201

@app.get('/api/planillas/quincenas/preview')
def vista_previa_planilla_quincenal():
    c=db()
    try:
        calculo,error=calcular_planilla_quincenal(c,request.args.get('fecha') or ahora_operacion().date().isoformat())
        if error: return jsonify(error=error),400
        existente=c.execute('SELECT id,estado FROM planillas_quincenales WHERE periodo_inicio=? AND periodo_fin=?',
            (calculo['periodo_inicio'],calculo['periodo_fin'])).fetchone()
        calculo['planilla_existente']=dict(existente) if existente else None
        return jsonify(calculo)
    finally:
        c.close()

@app.get('/api/planillas/quincenas')
def get_planillas_quincenales():
    c=db()
    rows=c.execute('''SELECT p.*,COUNT(d.id) empleados
        FROM planillas_quincenales p LEFT JOIN planillas_quincenales_detalle d ON d.planilla_id=p.id
        GROUP BY p.id ORDER BY p.periodo_inicio DESC,p.id DESC''').fetchall()
    c.close(); return jsonify([dict(row) for row in rows])

@app.get('/api/planillas/quincenas/<int:payroll_id>')
def get_planilla_quincenal(payroll_id):
    c=db()
    try:
        header=c.execute('SELECT * FROM planillas_quincenales WHERE id=?',(payroll_id,)).fetchone()
        if not header: return jsonify(error='Planilla no encontrada.'),404
        details=c.execute('SELECT * FROM planillas_quincenales_detalle WHERE planilla_id=? ORDER BY empleado_nombre COLLATE NOCASE,id',(payroll_id,)).fetchall()
        result=dict(header); result['empleados']=[dict(row) for row in details]
        return jsonify(result)
    finally:
        c.close()

def lineas_reclasificacion_planilla(c, payroll_id):
    """Devuelve el ajuste que reclasifica el gasto ya pagado, sin tocar banco."""
    detalles=[dict(row) for row in c.execute('SELECT * FROM planillas_quincenales_detalle WHERE planilla_id=?',(payroll_id,)).fetchall()]
    if not detalles:
        raise ValueError('La planilla no tiene detalle para reclasificar.')
    cuentas={}
    for codigo,label in (('6102','Comisiones'),('6103','Bonos al Personal')):
        cuenta=c.execute('SELECT id FROM cuentas_contables WHERE codigo=? AND activo=1 AND acepta_movimiento=1',(codigo,)).fetchone()
        if not cuenta:
            raise ValueError(f'No existe la cuenta activa {codigo} ({label}).')
        cuentas[codigo]=cuenta['id']
    esperado={}
    for detalle in detalles:
        salario=round(float(detalle.get('neto') or 0)-float(detalle.get('comisiones') or 0)-float(detalle.get('bonos') or 0),2)
        if salario < -0.01:
            raise ValueError(f"Las deducciones de {detalle['empleado_nombre']} exceden el componente de salario.")
        for cuenta_id,monto in ((cuenta_salarios_departamento(c,detalle['departamento_id']),salario),
                                (cuentas['6102'],round(float(detalle.get('comisiones') or 0),2)),
                                (cuentas['6103'],round(float(detalle.get('bonos') or 0),2))):
            if monto:
                esperado[cuenta_id]=round(esperado.get(cuenta_id,0)+monto,2)
    actual={row['cuenta_id']:round(float(row['debe'] or 0),2) for row in c.execute('''SELECT cuenta_id,SUM(debe) debe
        FROM partidas WHERE referencia_tipo='pago_planilla' AND referencia_id=? AND debe>0 GROUP BY cuenta_id''',(payroll_id,)).fetchall()}
    if not actual:
        raise ValueError('No se encontró el asiento original del pago de esta planilla.')
    if round(sum(actual.values()),2)!=round(sum(esperado.values()),2):
        raise ValueError('El asiento original no cuadra con el neto de la planilla; requiere revisión manual.')
    if actual==esperado:
        return []
    # Reversa exclusivamente los débitos de gasto previos y registra los
    # nuevos. El crédito a banco, referencia y movimiento de caja no cambian.
    return ([{'cuenta_id':cuenta_id,'haber':monto} for cuenta_id,monto in actual.items() if monto]+
            [{'cuenta_id':cuenta_id,'debe':monto} for cuenta_id,monto in esperado.items() if monto])

@app.post('/api/planillas/reclasificar-pagos')
def reclasificar_pagos_planilla():
    data=request.get_json(silent=True) or {}
    mes=(data.get('mes') or '').strip()
    if not re.fullmatch(r'\d{4}-\d{2}',mes):
        return jsonify(error='Indique el mes a reclasificar en formato AAAA-MM.'),400
    c=db(); corregidas=[]; sin_cambio=[]; errores=[]
    try:
        planillas=c.execute('''SELECT * FROM planillas_quincenales
            WHERE estado='Pagada' AND substr(periodo_inicio,1,7)=? ORDER BY periodo_inicio,id''',(mes,)).fetchall()
        for planilla in planillas:
            if c.execute("SELECT 1 FROM asientos_contables WHERE referencia_tipo='reclasificacion_planilla' AND referencia_id=?",(planilla['id'],)).fetchone():
                sin_cambio.append({'id':planilla['id'],'motivo':'Ya fue reclasificada.'}); continue
            try:
                lineas=lineas_reclasificacion_planilla(c,planilla['id'])
                if not lineas:
                    sin_cambio.append({'id':planilla['id'],'motivo':'Ya estaba correctamente clasificada.'}); continue
                fecha=planilla['fecha_pago'] or planilla['periodo_fin']
                descripcion=f"Reclasificación de planilla {planilla['periodo_inicio']} al {planilla['periodo_fin']}"
                registrar_asiento(c,fecha,descripcion,'reclasificacion_planilla',planilla['id'],lineas)
                corregidas.append({'id':planilla['id'],'periodo_inicio':planilla['periodo_inicio'],'periodo_fin':planilla['periodo_fin']})
            except ValueError as error:
                errores.append({'id':planilla['id'],'error':str(error)})
        c.commit()
        return jsonify(ok=True,mes=mes,corregidas=corregidas,sin_cambio=sin_cambio,errores=errores)
    except Exception:
        c.rollback(); raise
    finally:
        c.close()

@app.post('/api/planillas/quincenas/<int:payroll_id>/registrar-pago')
def registrar_pago_planilla_quincenal(payroll_id):
    data=request.get_json(silent=True) or {}
    fecha=fecha_planilla(data.get('fecha_pago') or ahora_operacion().date().isoformat())
    banco=(data.get('banco') or '').strip()
    referencia=(data.get('referencia') or '').strip()
    if not fecha or not banco or not referencia:
        return jsonify(error='Indique la fecha de aplicación, el banco y la referencia del pago de planilla.'),400
    c=db()
    try:
        planilla=c.execute('SELECT * FROM planillas_quincenales WHERE id=?',(payroll_id,)).fetchone()
        if not planilla: return jsonify(error='Planilla no encontrada.'),404
        if planilla['estado']=='Pagada': return jsonify(error='Esta planilla ya fue registrada como pagada.'),409
        detalles=[dict(row) for row in c.execute('SELECT * FROM planillas_quincenales_detalle WHERE planilla_id=?',(payroll_id,)).fetchall()]
        if not detalles: return jsonify(error='La planilla no tiene empleados para pagar.'),400
        por_cuenta={}
        cuenta_comisiones=c.execute("SELECT id FROM cuentas_contables WHERE codigo='6102' AND activo=1 AND acepta_movimiento=1").fetchone()
        cuenta_bonos=c.execute("SELECT id FROM cuentas_contables WHERE codigo='6103' AND activo=1 AND acepta_movimiento=1").fetchone()
        if not cuenta_comisiones or not cuenta_bonos:
            return jsonify(error='Deben existir las cuentas activas 6102 (Comisiones) y 6103 (Bonos al Personal).'),400
        for detalle in detalles:
            cuenta_id=cuenta_salarios_departamento(c,detalle['departamento_id'])
            comisiones=round(float(detalle.get('comisiones') or 0),2)
            bonos=round(float(detalle.get('bonos') or 0),2)
            salarios_neto=round(float(detalle['neto'] or 0)-comisiones-bonos,2)
            if salarios_neto < -0.01:
                return jsonify(error=f"Las deducciones de {detalle['empleado_nombre']} exceden el componente de salario de esta planilla."),400
            if salarios_neto:
                por_cuenta[cuenta_id]=round(por_cuenta.get(cuenta_id,0)+salarios_neto,2)
            if comisiones:
                por_cuenta[cuenta_comisiones['id']]=round(por_cuenta.get(cuenta_comisiones['id'],0)+comisiones,2)
            if bonos:
                por_cuenta[cuenta_bonos['id']]=round(por_cuenta.get(cuenta_bonos['id'],0)+bonos,2)
        total=round(sum(por_cuenta.values()),2)
        if total<=0: return jsonify(error='El neto a pagar debe ser mayor que cero.'),400
        descripcion=f"Pago de planilla quincenal {planilla['periodo_inicio']} al {planilla['periodo_fin']}"
        lineas=[{'cuenta_id':cuenta_id,'debe':monto} for cuenta_id,monto in por_cuenta.items() if monto]
        lineas.append({'cuenta_id':cuenta_contable_id(c,'caja_bancos'),'haber':total})
        registrar_asiento(c,fecha.isoformat(),descripcion,'pago_planilla',payroll_id,lineas)
        registrar_movimiento_caja(c,fecha.isoformat(),'Pago de planilla','Banco',banco,0,total,descripcion,'pago_planilla',payroll_id)
        c.execute("UPDATE planillas_quincenales SET estado='Pagada',fecha_pago=?,banco=?,referencia_pago=? WHERE id=?",
            (fecha.isoformat(),normalizar_nombre_banco(banco),referencia or None,payroll_id))
        c.commit()
        return jsonify(ok=True,id=payroll_id,total=total,estado='Pagada')
    except ValueError as error:
        c.rollback(); return jsonify(error=str(error)),400
    finally:
        c.close()

@app.post('/api/planillas/quincenas')
def generar_planilla_quincenal():
    data=request.get_json(silent=True) or {}
    c=db()
    try:
        calculo,error=calcular_planilla_quincenal(c,data.get('fecha_referencia') or ahora_operacion().date().isoformat())
        if error: return jsonify(error=error),400
        if not calculo['empleados']: return jsonify(error='No hay empleados activos para generar la planilla.'),400
        existente=c.execute('SELECT id FROM planillas_quincenales WHERE periodo_inicio=? AND periodo_fin=?',
            (calculo['periodo_inicio'],calculo['periodo_fin'])).fetchone()
        if existente: return jsonify(error='Ya existe una planilla registrada para esta quincena.',planilla_id=existente['id']),409
        totals=calculo['totales']
        cur=c.execute('''INSERT INTO planillas_quincenales(periodo_inicio,periodo_fin,fecha_generacion,estado,total_salarios,total_comisiones,total_bonos,total_deducciones,total_neto)
            VALUES(?,?,?,'Generada',?,?,?,?,?)''',(calculo['periodo_inicio'],calculo['periodo_fin'],ahora_operacion().date().isoformat(),
            totals['salario'],totals['comisiones'],totals['bonos'],totals['total_deducciones'],totals['neto']))
        for detail in calculo['empleados']:
            c.execute('''INSERT INTO planillas_quincenales_detalle(planilla_id,empleado_id,empleado_nombre,numero_identidad,departamento_id,cuenta_contable_id,departamento_nombre,cuenta_contable_nombre,
                salario,comisiones_fijas,comisiones_registradas,comisiones,bonos,ihss,rap,otras_deducciones,total_devengado,neto)
                VALUES(:planilla_id,:empleado_id,:empleado_nombre,:numero_identidad,:departamento_id,:cuenta_contable_id,:departamento_nombre,:cuenta_contable_nombre,:salario,:comisiones_fijas,
                :comisiones_registradas,:comisiones,:bonos,:ihss,:rap,:otras_deducciones,:total_devengado,:neto)''',dict(detail,planilla_id=cur.lastrowid))
        c.commit()
        return jsonify(id=cur.lastrowid,**calculo),201
    finally:
        c.close()

@app.put('/api/planillas/<int:employee_id>')
def actualizar_empleado_planilla(employee_id):
    data, error=datos_empleado_planilla(request.get_json(silent=True) or {})
    if error: return jsonify(error=error),400
    c=db()
    try:
        if not c.execute('SELECT 1 FROM empleados_planilla WHERE id=?',(employee_id,)).fetchone():
            return jsonify(error='Empleado de planilla no encontrado.'),404
        if not c.execute('SELECT 1 FROM departamentos WHERE id=? AND activo=1',(data['departamento_id'],)).fetchone():
            return jsonify(error='El departamento seleccionado no está activo.'),400
        data['cuenta_contable_id']=cuenta_salarios_departamento(c,data['departamento_id'])
        c.execute('''UPDATE empleados_planilla SET nombre=:nombre,numero_identidad=:numero_identidad,fecha_ingreso=:fecha_ingreso,salario=:salario,
            comisiones=:comisiones,bonos=:bonos,ihss=:ihss,rap=:rap,otras_deducciones=:otras_deducciones,
            departamento_id=:departamento_id,cuenta_contable_id=:cuenta_contable_id,actualizado_en=CURRENT_TIMESTAMP WHERE id=:id''',
            dict(data,id=employee_id))
        c.commit(); employee=empleado_planilla_detalle(c,employee_id)
    except sqlite3.IntegrityError:
        c.rollback(); return jsonify(error='Ya existe un empleado de planilla con ese número de identidad.'),409
    finally:
        c.close()
    return jsonify(employee)

@app.get('/api/vehiculos')
def get_veh():
    q=request.args.get('q','').strip(); estado=request.args.get('estado','').strip(); c=db(); where=[]; args=[]
    if q:
        like='%'+q+'%'; where.append('(vin LIKE ? OR marca LIKE ? OR modelo LIKE ? OR placa LIKE ? OR lote LIKE ?)'); args += [like]*5
    if estado: where.append('estado=?'); args.append(estado)
    # Incluimos la última compra en el listado para que las pantallas que
    # seleccionan un VIN puedan confirmar de inmediato si ya fue adquirido.
    # El subquery evita duplicar vehículos si hubiera registros históricos.
    sql='''SELECT v.*,a.id compra_id,a.fecha compra_fecha,a.costo_compra compra_costo,
        a.anticipo compra_anticipo,a.saldo compra_saldo,a.metodo_pago compra_metodo_pago,
        p.nombre compra_proveedor
        FROM vehiculos v
        LEFT JOIN adquisiciones a ON a.id=(SELECT id FROM adquisiciones
            WHERE vehiculo_id=v.id ORDER BY fecha DESC,id DESC LIMIT 1)
        LEFT JOIN proveedores p ON p.id=a.proveedor_id'''+((' WHERE '+' AND '.join(where)) if where else '')+' ORDER BY v.id DESC'
    r=c.execute(sql,args).fetchall(); c.close(); return jsonify([dict(x) for x in r])

@app.get('/api/informacion-vehiculo')
def get_informacion_vehiculo():
    marca=(request.args.get('marca') or '').strip()
    modelo=(request.args.get('modelo') or '').strip()
    c=db(); where=['activo=1']; args=[]
    if marca:
        where.append('marca=?'); args.append(marca)
    if modelo:
        where.append('modelo=?'); args.append(modelo)
    rows=c.execute('SELECT * FROM informacion_vehiculo WHERE '+' AND '.join(where)+
                   ' ORDER BY marca,modelo,version,tipo_vehiculo',args).fetchall()
    c.close(); return jsonify([dict(row) for row in rows])

@app.post('/api/informacion-vehiculo')
def post_informacion_vehiculo():
    d=request.get_json(silent=True) or {}
    marca=(d.get('marca') or '').strip(); modelo=(d.get('modelo') or '').strip()
    if not marca or not modelo:
        return jsonify(error='Marca y modelo son obligatorios.'),400
    values=((d.get('tipo_vehiculo') or '').strip(),marca,modelo,(d.get('version') or '').strip())
    c=db()
    try:
        cur=c.execute('INSERT INTO informacion_vehiculo(tipo_vehiculo,marca,modelo,version) VALUES(?,?,?,?)',values)
        c.commit(); item_id=cur.lastrowid
    except sqlite3.IntegrityError:
        c.close(); return jsonify(error='Esta información de vehículo ya existe.'),409
    c.close(); return jsonify(id=item_id),201

@app.put('/api/informacion-vehiculo/<int:item_id>')
def put_informacion_vehiculo(item_id):
    d=request.get_json(silent=True) or {}
    marca=(d.get('marca') or '').strip(); modelo=(d.get('modelo') or '').strip()
    if not marca or not modelo:
        return jsonify(error='Marca y modelo son obligatorios.'),400
    values=((d.get('tipo_vehiculo') or '').strip(),marca,modelo,(d.get('version') or '').strip(),item_id)
    c=db()
    try:
        cur=c.execute('''UPDATE informacion_vehiculo SET tipo_vehiculo=?,marca=?,modelo=?,version=?
            WHERE id=?''',values)
        if not cur.rowcount:
            c.close(); return jsonify(error='Registro no encontrado.'),404
        c.commit()
    except sqlite3.IntegrityError:
        c.close(); return jsonify(error='Esta información de vehículo ya existe.'),409
    c.close(); return jsonify(ok=True)

@app.get('/api/inventario')
def get_inventario():
    c=db(); rows=c.execute('''SELECT v.*,a.fecha fecha_compra,p.nombre proveedor_nombre
        FROM vehiculos v LEFT JOIN adquisiciones a ON a.vehiculo_id=v.id
        LEFT JOIN proveedores p ON p.id=a.proveedor_id ORDER BY v.id DESC''').fetchall()
    result=[]; commercial_statuses=('DPV','Reservado','Vendido')
    for row in rows:
        vehicle=dict(row); workshop=c.execute("SELECT taller FROM ordenes_trabajo WHERE vehiculo_id=? AND estado='Pendiente' ORDER BY id DESC LIMIT 1",(vehicle['id'],)).fetchone()
        cost=0 if vehicle.get('tipo_compra')=='Consignación' else costo_consolidado(c,vehicle['id'])
        price=float(vehicle['precio_venta'] or 0) if vehicle['estado'] in commercial_statuses else 0
        margin=price-cost if price else 0
        vehicle.update(costo_total=cost,precio_consulta=price,margen=margin,margen_pct=(margin/price*100 if price else 0),taller=workshop['taller'] if workshop and vehicle['estado']=='En Taller' else None,es_consignacion=vehicle.get('tipo_compra')=='Consignación',valor_consignado=float(vehicle.get('precio_compra') or 0))
        result.append(vehicle)
    c.close(); return jsonify(result)

def conteo_inventario_payload(c, count):
    """Devuelve el comparativo de una toma física sin alterar el inventario."""
    if not count:
        return {'conteo':None,'detalles':[], 'resumen':{'existencia':0,'fisico':0,'diferencia':0,'pendientes':0,'sobrantes':0}}
    details=[dict(row) for row in c.execute('''SELECT id,vehiculo_id,vin,marca,modelo,anio,color,existencia,fisico,
        contado_en,(fisico-existencia) diferencia FROM conteos_inventario_detalle
        WHERE conteo_id=? ORDER BY diferencia DESC,marca,modelo,vin''',(count['id'],)).fetchall()]
    summary={
        'existencia':sum(int(row['existencia'] or 0) for row in details),
        'fisico':sum(int(row['fisico'] or 0) for row in details),
        'diferencia':sum(int(row['diferencia'] or 0) for row in details),
        'pendientes':sum(1 for row in details if int(row['diferencia'] or 0)<0),
        'sobrantes':sum(1 for row in details if int(row['diferencia'] or 0)>0),
    }
    return {'conteo':dict(count),'detalles':details,'resumen':summary}

@app.get('/api/conteos-inventario/actual')
def get_conteo_inventario_actual():
    c=db()
    count=c.execute("SELECT * FROM conteos_inventario WHERE estado='Abierto' ORDER BY id DESC LIMIT 1").fetchone()
    payload=conteo_inventario_payload(c,count)
    c.close(); return jsonify(payload)

@app.post('/api/conteos-inventario')
def post_conteo_inventario():
    data=request.get_json(silent=True) or {}
    name=(data.get('nombre') or '').strip() or f"Conteo físico {ahora_operacion().strftime('%d/%m/%Y')}"
    date=(data.get('fecha') or ahora_operacion().strftime('%Y-%m-%d')).strip()
    inventory_status=(data.get('estatus') or '').strip()
    if len(name)>120: return jsonify(error='El nombre del conteo no puede exceder 120 caracteres.'),400
    try: datetime.strptime(date,'%Y-%m-%d')
    except ValueError: return jsonify(error='La fecha del conteo no es válida.'),400
    if inventory_status not in ('DPV','En Tránsito','En Taller'):
        return jsonify(error='Seleccione el estatus que desea inventariar: DPV, En Tránsito o En Taller.'),400
    c=db()
    try:
        current=c.execute("SELECT id,nombre FROM conteos_inventario WHERE estado='Abierto' ORDER BY id DESC LIMIT 1").fetchone()
        if current:
            return jsonify(error=f"Ya existe el conteo abierto «{current['nombre']}». Ciérrelo antes de iniciar otro."),409
        created=c.execute('INSERT INTO conteos_inventario(nombre,fecha,estatus,creado_por) VALUES(?,?,?,?)',
                          (name,date,inventory_status,session.get('user_id')))
        count_id=created.lastrowid
        vehicles=c.execute("""SELECT id,vin,marca,modelo,anio,color FROM vehiculos
            WHERE estado=? ORDER BY marca,modelo,vin""",(inventory_status,)).fetchall()
        c.executemany('''INSERT INTO conteos_inventario_detalle
            (conteo_id,vehiculo_id,vin,marca,modelo,anio,color,existencia,fisico)
            VALUES(?,?,?,?,?,?,?,?,0)''',[(count_id,row['id'],row['vin'],row['marca'],row['modelo'],row['anio'],row['color'],1) for row in vehicles])
        c.commit(); count=c.execute('SELECT * FROM conteos_inventario WHERE id=?',(count_id,)).fetchone()
        payload=conteo_inventario_payload(c,count)
    except sqlite3.Error:
        c.rollback(); raise
    finally:
        c.close()
    return jsonify(payload),201

@app.post('/api/conteos-inventario/<int:count_id>/capturas')
def post_captura_conteo_inventario(count_id):
    data=request.get_json(silent=True) or {}
    vin=re.sub(r'\s+','',(data.get('vin') or '')).upper()
    if len(vin)<5 or len(vin)>40: return jsonify(error='Capture o digite un VIN válido.'),400
    c=db()
    try:
        count=c.execute('SELECT * FROM conteos_inventario WHERE id=?',(count_id,)).fetchone()
        if not count: return jsonify(error='El conteo no existe.'),404
        if count['estado']!='Abierto': return jsonify(error='El conteo ya fue cerrado y no admite más capturas.'),409
        detail=c.execute('SELECT * FROM conteos_inventario_detalle WHERE conteo_id=? AND vin=?',(count_id,vin)).fetchone()
        already_counted=bool(detail and int(detail['fisico'] or 0)>0)
        if detail:
            if not already_counted:
                c.execute("UPDATE conteos_inventario_detalle SET fisico=1,contado_en=? WHERE id=?",
                          (ahora_operacion().isoformat(timespec='seconds'),detail['id']))
        else:
            vehicle=c.execute('SELECT id,vin,marca,modelo,anio,color,estado FROM vehiculos WHERE UPPER(TRIM(vin))=?',(vin,)).fetchone()
            if vehicle and vehicle['estado']!=count['estatus']:
                return jsonify(error=f"El VIN {vin} está en «{vehicle['estado']}» y no pertenece al conteo de «{count['estatus']}»."),409
            c.execute('''INSERT INTO conteos_inventario_detalle
                (conteo_id,vehiculo_id,vin,marca,modelo,anio,color,existencia,fisico,contado_en)
                VALUES(?,?,?,?,?,?,?,?,1,?)''',(
                    count_id,vehicle['id'] if vehicle else None,vin,vehicle['marca'] if vehicle else None,
                    vehicle['modelo'] if vehicle else None,vehicle['anio'] if vehicle else None,
                    vehicle['color'] if vehicle else None,0,ahora_operacion().isoformat(timespec='seconds')))
        c.commit(); detail=c.execute('SELECT *,(fisico-existencia) diferencia FROM conteos_inventario_detalle WHERE conteo_id=? AND vin=?',(count_id,vin)).fetchone()
    except sqlite3.Error:
        c.rollback(); raise
    finally:
        c.close()
    return jsonify(detalle=dict(detail),ya_contado=already_counted),200

@app.post('/api/conteos-inventario/<int:count_id>/cerrar')
def post_cerrar_conteo_inventario(count_id):
    c=db()
    try:
        count=c.execute('SELECT * FROM conteos_inventario WHERE id=?',(count_id,)).fetchone()
        if not count: return jsonify(error='El conteo no existe.'),404
        if count['estado']=='Cerrado': return jsonify(error='El conteo ya está cerrado.'),409
        c.execute("UPDATE conteos_inventario SET estado='Cerrado',cerrado_en=? WHERE id=?",
                  (ahora_operacion().isoformat(timespec='seconds'),count_id))
        c.commit(); count=c.execute('SELECT * FROM conteos_inventario WHERE id=?',(count_id,)).fetchone()
        payload=conteo_inventario_payload(c,count)
    except sqlite3.Error:
        c.rollback(); raise
    finally:
        c.close()
    return jsonify(payload)

@app.get('/api/vendedores')
def get_vendedores():
    include_all=request.args.get('todos')=='1'
    c=db(); sql='SELECT * FROM vendedores'
    if not include_all:
        sql+=' WHERE activo=1'
    rows=[dict(row) for row in c.execute(sql+' ORDER BY nombre').fetchall()]
    c.close(); return jsonify(rows)

@app.get('/api/ventas/vendedores-activos')
def vendedores_activos_para_venta():
    """Lista mínima usada dentro de la boleta, bajo el permiso de Ventas."""
    c=db()
    rows=[dict(row) for row in c.execute('''SELECT id,nombre FROM vendedores
        WHERE activo=1 ORDER BY nombre''').fetchall()]
    c.close(); return jsonify(rows)

@app.get('/api/ventas/financieras-activas')
def financieras_activas_para_venta():
    """Financieras disponibles al registrar un pago financiado."""
    c=db()
    rows=[dict(row) for row in c.execute('''SELECT id,nombre FROM clientes
        WHERE tipo='Financiera' AND activo=1 ORDER BY nombre''').fetchall()]
    c.close(); return jsonify(rows)

def vendedor_values(data):
    nombre=(data.get('nombre') or '').strip()
    if not nombre:
        return None,'El nombre del vendedor es obligatorio.'
    return (nombre,(data.get('identidad') or '').strip() or None,(data.get('telefono') or '').strip() or None,
            (data.get('email') or '').strip() or None),None

@app.post('/api/vendedores')
def post_vendedor():
    data=request.get_json(silent=True) or {}; values,error=vendedor_values(data)
    if error: return jsonify(error=error),400
    c=db()
    try:
        cur=c.execute('INSERT INTO vendedores(nombre,identidad,telefono,email,activo) VALUES(?,?,?,?,1)',values)
        c.commit()
    except sqlite3.IntegrityError:
        c.rollback(); c.close(); return jsonify(error='Ya existe un vendedor con ese nombre.'),409
    c.close(); return jsonify(id=cur.lastrowid),201

@app.put('/api/vendedores/<int:vendedor_id>')
def put_vendedor(vendedor_id):
    data=request.get_json(silent=True) or {}; values,error=vendedor_values(data)
    if error: return jsonify(error=error),400
    activo=1 if data.get('activo',True) not in (False,0,'0',None) else 0
    c=db()
    if not c.execute('SELECT id FROM vendedores WHERE id=?',(vendedor_id,)).fetchone():
        c.close(); return jsonify(error='Vendedor no encontrado.'),404
    try:
        c.execute('UPDATE vendedores SET nombre=?,identidad=?,telefono=?,email=?,activo=? WHERE id=?',(*values,activo,vendedor_id))
        c.commit()
    except sqlite3.IntegrityError:
        c.rollback(); c.close(); return jsonify(error='Ya existe otro vendedor con ese nombre.'),409
    c.close(); return jsonify(ok=True)

@app.get('/api/ventas')
def get_ventas():
    month=(request.args.get('mes') or '').strip()
    c=db(); sql='''SELECT ve.*,v.vin,v.placa,v.marca,v.modelo,v.version,v.anio,v.color,
        cl.nombre cliente_nombre,ven.nombre vendedor_nombre FROM ventas ve JOIN vehiculos v ON v.id=ve.vehiculo_id
        LEFT JOIN clientes cl ON cl.id=ve.cliente_id LEFT JOIN vendedores ven ON ven.id=ve.vendedor_id'''; args=[]
    if month:
        sql+=" WHERE substr(ve.fecha,1,7)=?"; args.append(month)
    # El correlativo es la secuencia operativa de la venta; siempre se muestra
    # primero la transacción más reciente aunque la fecha haya sido digitada
    # manualmente con un día anterior.
    rows=c.execute(sql+' ORDER BY COALESCE(ve.correlativo,0) DESC,ve.id DESC',args).fetchall(); result=[]
    for row in rows:
        sale=dict(row); cost=costo_consolidado(c,sale['vehiculo_id']); price=float(sale['precio'] or 0); margin=price-cost
        sale.update(costo_consolidado=cost,margen_bruto=margin,margen_bruto_pct=(margin/price*100 if price else 0))
        result.append(sale)
    c.close(); return jsonify(result)

@app.get('/api/ventas/<int:sid>')
def get_venta(sid):
    c=db(); sale=c.execute('''SELECT ve.*,v.vin,v.placa,v.marca,v.modelo,v.version,v.anio,v.color,v.ubicacion,
        cl.nombre cliente_nombre,cl.identidad,cl.rtn,cl.telefono,cl.direccion,ven.nombre vendedor_nombre FROM ventas ve
        JOIN vehiculos v ON v.id=ve.vehiculo_id LEFT JOIN clientes cl ON cl.id=ve.cliente_id
        LEFT JOIN vendedores ven ON ven.id=ve.vendedor_id WHERE ve.id=?''',(sid,)).fetchone()
    if not sale: c.close(); return jsonify(error='Venta no encontrada'),404
    data=dict(sale); cost=costo_consolidado(c,data['vehiculo_id']); price=float(data['precio'] or 0)
    data.update(costo_consolidado=cost,margen_bruto=price-cost,margen_bruto_pct=((price-cost)/price*100 if price else 0))
    data['pagos']=[dict(row) for row in c.execute('SELECT * FROM pagos_venta WHERE venta_id=? ORDER BY id',(sid,)).fetchall()]
    c.close(); return jsonify(data)

@app.put('/api/ventas/<int:sid>/vendedor')
def actualizar_vendedor_venta(sid):
    """La única corrección permitida sobre una boleta ya facturada es su vendedor."""
    data=request.get_json(silent=True) or {}
    nombre=' '.join(str(data.get('vendedor_nombre') or '').split())
    if not nombre:
        return jsonify(error='Ingrese el nombre del vendedor.'),400
    c=db(); sale=c.execute('SELECT id FROM ventas WHERE id=?',(sid,)).fetchone()
    if not sale:
        c.close(); return jsonify(error='Venta no encontrada.'),404
    seller=c.execute('''SELECT id,nombre FROM vendedores
        WHERE activo=1 AND lower(trim(nombre))=lower(?)''',(nombre,)).fetchone()
    if not seller:
        c.close(); return jsonify(error='El nombre ingresado no corresponde a un vendedor activo.'),400
    c.execute('UPDATE ventas SET vendedor_id=? WHERE id=?',(seller['id'],sid))
    c.commit(); c.close(); return jsonify(ok=True,vendedor_id=seller['id'],vendedor_nombre=seller['nombre'])

@app.get('/api/vehiculos/placa/<placa>')
def get_vehiculo_placa(placa):
    c=db(); v=c.execute('SELECT * FROM vehiculos WHERE UPPER(placa)=?',(placa.strip().upper(),)).fetchone()
    if not v:
        c.close(); return jsonify(error='Vehículo no encontrado con esa placa'),404
    data=dict(v)
    # El tipo de compra histórico puede estar únicamente en Adquisiciones.
    # La boleta necesita conocerlo para presentar el flujo de consignación.
    acquisition=c.execute('''SELECT tipo_compra FROM adquisiciones WHERE vehiculo_id=?
        ORDER BY id DESC LIMIT 1''',(data['id'],)).fetchone()
    if acquisition and acquisition['tipo_compra']:
        data['tipo_compra']=acquisition['tipo_compra']
    data['es_consignacion']=data.get('tipo_compra')=='Consignación'
    # La boleta usa esta misma consulta inicial; incluir la lista evita que el
    # selector de vendedor dependa de una segunda llamada o de otro módulo.
    data['vendedores_activos']=[dict(row) for row in c.execute('''SELECT id,nombre
        FROM vendedores WHERE activo=1 ORDER BY nombre''').fetchall()]
    c.close(); return jsonify(data)

@app.get('/api/vehiculos/vendedores-activos')
def vendedores_activos_para_boleta():
    """Vendedores elegibles dentro de la boleta.

    La pantalla primero valida la placa desde Vehículos; exponer esta lista
    bajo el mismo módulo evita que un perfil operativo pueda cargar la unidad
    pero quede bloqueado al seleccionar al vendedor.
    """
    c=db()
    rows=[dict(row) for row in c.execute('''SELECT id,nombre FROM vendedores
        WHERE COALESCE(activo,1)<>0 ORDER BY nombre''').fetchall()]
    c.close(); return jsonify(rows)

@app.get('/api/ventas/siguiente-factura')
def siguiente_factura():
    c=db(); factura=c.execute("SELECT ultimo_numero FROM correlativos_plataforma WHERE tipo='FACTURA'").fetchone(); venta=c.execute("SELECT ultimo_numero FROM correlativos_plataforma WHERE tipo='VENTA'").fetchone(); c.close()
    next_invoice=int(factura[0] if factura else 0)+1; next_sale=int(venta[0] if venta else 0)+1
    return jsonify(correlativo_factura=next_invoice,correlativo_transaccion=next_sale,factura=f'FAC-{next_invoice:06d}',numero_transaccion=f'V-{next_sale:05d}')

@app.post('/api/ventas')
def post_venta():
    d=request.get_json(silent=True) or {}
    try:
        vid=int(d.get('vehiculo_id')); lista=float(d.get('precio_lista')); descuento=float(d.get('descuento') or 0); prima=float(d.get('prima') or 0); financiado=float(d.get('monto_financiado') or 0); transferencia=float(d.get('transferencia') or 0)
    except (TypeError,ValueError): return jsonify(error='Vehículo, vendedor y valores de venta son obligatorios'),400
    precio_final_raw=d.get('precio_final')
    try:
        precio_final=float(precio_final_raw) if precio_final_raw not in (None,'') else None
    except (TypeError,ValueError): return jsonify(error='El precio final debe ser un valor numérico.'),400
    factura=(d.get('factura') or '').strip(); nombre=(d.get('cliente_nombre') or '').strip()
    if not nombre: return jsonify(error='Nombre del cliente es obligatorio'),400
    garantia_raw=d.get('garantia_dias')
    try:
        garantia_dias=int(garantia_raw) if garantia_raw not in (None,'') else None
    except (TypeError,ValueError): return jsonify(error='Los días de garantía deben ser un número entero.'),400
    if garantia_dias is not None and garantia_dias<=0:
        return jsonify(error='Los días de garantía deben ser mayores que cero.'),400
    if min(descuento,prima,financiado,transferencia)<0: return jsonify(error='Revise los valores de la venta'),400
    try: fee_pct=float(d.get('fee_administrativo_pct') or 0)
    except (TypeError,ValueError): return jsonify(error='El fee administrativo debe ser un porcentaje válido.'),400
    if fee_pct<0 or fee_pct>100: return jsonify(error='El fee administrativo debe estar entre 0% y 100%.'),400
    pagos=d.get('pagos')
    if not isinstance(pagos,list) or not pagos: return jsonify(error='Registre al menos un pago'),400
    pagos_limpios=[]; total_pagos=0
    for pago in pagos:
        try: monto=float(pago.get('monto'))
        except (AttributeError,TypeError,ValueError): return jsonify(error='Monto de pago inválido'),400
        tipo=(pago.get('tipo_pago') or '').strip(); referencia=(pago.get('referencia') or '').strip()
        banco=(pago.get('banco') or '').strip()
        if tipo not in ('Efectivo','Tarjeta','Transferencia','Depósito','Financiado','Pagaré','Cambio') or monto<=0: return jsonify(error='Tipo y monto de pago son obligatorios'),400
        if tipo=='Efectivo': referencia=''
        elif tipo=='Financiado':
            referencia=factura
            if not pago.get('financiera_cliente_id'): return jsonify(error='Seleccione la financiera para el pago financiado'),400
        elif tipo=='Pagaré':
            vencimiento=(pago.get('fecha_vencimiento') or '').strip()
            try:
                datetime.strptime(vencimiento,'%Y-%m-%d')
            except ValueError:
                return jsonify(error='Ingrese la fecha de vencimiento del pagaré'),400
            # Un pagaré no requiere referencia bancaria; se identifica por la
            # venta y su vencimiento dentro del auxiliar de CxC.
            referencia=referencia
        elif tipo=='Cambio':
            cambio=pago.get('cambio')
            if not isinstance(cambio,dict): return jsonify(error='Registre la información del vehículo recibido como cambio'),400
            try:
                valor_cambio=round(float(cambio.get('costo_compra')),2)
            except (TypeError,ValueError): return jsonify(error='El valor de adquisición del cambio es inválido'),400
            if abs(valor_cambio-monto)>0.01: return jsonify(error='El pago por cambio debe coincidir con el valor de adquisición del vehículo recibido'),400
            referencia=(cambio.get('vin') or '').strip().upper()
        elif not referencia: return jsonify(error='Tarjeta, depósito y transferencia requieren número de referencia'),400
        if tipo in ('Tarjeta','Transferencia','Depósito') and not banco:
            return jsonify(error='Seleccione el banco para este pago'),400
        pagos_limpios.append({'tipo_pago':tipo,'referencia':referencia,'financiera_cliente_id':pago.get('financiera_cliente_id'),
            'banco':banco or None,'monto':monto,'fecha_vencimiento':(pago.get('fecha_vencimiento') or '').strip() or None,
            'cambio':pago.get('cambio') if tipo=='Cambio' else None})
        total_pagos+=monto
    if sum(1 for pago in pagos_limpios if pago['tipo_pago']=='Pagaré')>1:
        return jsonify(error='Registre un solo pagaré por venta'),400
    prima=sum(x['monto'] for x in pagos_limpios if x['tipo_pago']=='Efectivo'); financiado=sum(x['monto'] for x in pagos_limpios if x['tipo_pago']=='Financiado'); transferencia=sum(x['monto'] for x in pagos_limpios if x['tipo_pago']=='Transferencia'); saldo=0
    c=db(); vehicle=c.execute('SELECT * FROM vehiculos WHERE id=?',(vid,)).fetchone()
    if not vehicle: c.close(); return jsonify(error='Vehículo no encontrado'),404
    vendedor_nombre=' '.join((d.get('vendedor_nombre') or '').split())
    if vendedor_nombre:
        vendedor=c.execute('''SELECT id FROM vendedores
            WHERE activo=1 AND lower(trim(nombre))=lower(?)''',(vendedor_nombre,)).fetchone()
    else:
        try: vendedor=c.execute('SELECT id FROM vendedores WHERE id=? AND activo=1',(int(d.get('vendedor_id')),)).fetchone()
        except (TypeError,ValueError): vendedor=None
    if not vendedor:
        c.close(); return jsonify(error='El nombre ingresado no corresponde a un vendedor activo.'),400
    vendedor_id=vendedor['id']
    if vehicle['estado']!='DPV': c.close(); return jsonify(error='Solo puede facturar vehículos en DPV'),400
    consignacion=c.execute("SELECT * FROM adquisiciones WHERE vehiculo_id=? AND tipo_compra='Consignación'",(vid,)).fetchone()
    if fee_pct and not consignacion:
        c.close(); return jsonify(error='El fee administrativo solo aplica a vehículos en consignación.'),400
    precio_kardex=float(vehicle['precio_venta'] or 0)
    if precio_kardex > 0 and abs(lista-precio_kardex)>0.01:
        c.close(); return jsonify(error='El precio lista no coincide con el valor registrado en el Kardex'),400
    if lista <= 0:
        c.close(); return jsonify(error='El precio lista debe ser mayor que cero'),400
    if descuento>lista: c.close(); return jsonify(error='El descuento no puede exceder el precio lista'),400
    precio=lista-descuento if precio_final is None else precio_final
    if precio<=0:
        c.close(); return jsonify(error='El precio final debe ser mayor que cero.'),400
    # Se permite vender por encima del precio lista. Si se vende por debajo,
    # el descuento debe explicar exactamente la diferencia; no se acepta una
    # rebaja directa que deje la boleta y la contabilidad sin respaldo.
    if precio<lista and (descuento<=0 or abs((lista-descuento)-precio)>0.01):
        c.close(); return jsonify(error='Un precio final menor al precio lista requiere un descuento que cubra la diferencia.'),400
    if precio>=lista and descuento>0:
        c.close(); return jsonify(error='El descuento solo aplica cuando el precio final es menor al precio lista.'),400
    if abs(total_pagos-precio)>0.01: c.close(); return jsonify(error='El total de pagos debe cuadrar exactamente con el precio final'),400
    fee_monto=round(precio*fee_pct/100,2) if consignacion else 0
    identidad=(d.get('identidad') or '').strip(); rtn=(d.get('rtn') or '').strip()
    cliente=c.execute('SELECT * FROM clientes WHERE identidad=? OR rtn=? ORDER BY id LIMIT 1',(identidad,rtn)).fetchone() if (identidad or rtn) else None
    if cliente:
        c.execute('UPDATE clientes SET nombre=?,identidad=?,rtn=?,telefono=?,direccion=?,email=? WHERE id=?',(nombre,d.get('identidad'),d.get('rtn'),d.get('telefono'),d.get('direccion'),d.get('email'),cliente['id'])); client_id=cliente['id']
    else:
        cur=c.execute('INSERT INTO clientes(nombre,identidad,rtn,telefono,direccion,email) VALUES(?,?,?,?,?,?)',(nombre,d.get('identidad'),d.get('rtn'),d.get('telefono'),d.get('direccion'),d.get('email'))); client_id=cur.lastrowid
    cliente_cambio={
        'nombre':nombre,'identidad':identidad,'rtn':rtn,
        'telefono':(d.get('telefono') or '').strip(),'direccion':(d.get('direccion') or '').strip(),
        'email':(d.get('email') or '').strip(),
    }
    if consignacion:
        convertir_consignacion_en_compra(c,vid,d.get('fecha') or datetime.now().strftime('%Y-%m-%d'))
    correlativo=siguiente_correlativo_plataforma(c,'VENTA'); numero=f'V-{correlativo:05d}'; factura_num=siguiente_correlativo_plataforma(c,'FACTURA'); factura=f'FAC-{factura_num:06d}'
    for pago in pagos_limpios:
        if pago['tipo_pago']=='Financiado': pago['referencia']=factura
    try:
        cur=c.execute('''INSERT INTO ventas(vehiculo_id,cliente_id,vendedor_id,fecha,precio,descuento,prima,saldo,estado,factura,correlativo,numero_transaccion,tipo_venta,forma_pago,financiera_banco,monto_financiado,transferencia,observaciones,garantia_dias,fee_administrativo_pct,fee_administrativo_monto)
            VALUES(?,?,?,?,?,?,?,?,'Facturada',?,?,?,?,?,?,?,?,?,?,?,?)''',(vid,client_id,vendedor_id,d.get('fecha') or datetime.now().strftime('%Y-%m-%d'),precio,descuento,prima,saldo,factura,correlativo,numero,d.get('tipo_venta'),'Registro de pagos',d.get('financiera_banco'),financiado,transferencia,d.get('observaciones'),garantia_dias,fee_pct,fee_monto))
        for pago in pagos_limpios:
            financiera=c.execute("SELECT nombre FROM clientes WHERE id=? AND tipo='Financiera' AND activo=1",(pago['financiera_cliente_id'],)).fetchone() if pago['financiera_cliente_id'] else None
            if pago['tipo_pago']=='Financiado' and not financiera:
                raise ValueError('La financiera seleccionada no existe o no está activa')
            vehiculo_recibido_id=None
            if pago['tipo_pago']=='Cambio':
                vehiculo_recibido_id,_,valor_cambio=registrar_vehiculo_recibido_cambio(c,pago['cambio'],d.get('fecha') or datetime.now().strftime('%Y-%m-%d'),cliente_cambio,numero)
                if abs(valor_cambio-pago['monto'])>0.01:
                    raise ValueError('El valor del cambio no coincide con el pago registrado')
            c.execute('''INSERT INTO pagos_venta(venta_id,tipo_pago,referencia,financiera_cliente_id,financiera_nombre,banco,monto,fecha,fecha_vencimiento,vehiculo_recibido_id)
                VALUES(?,?,?,?,?,?,?,?,?,?)''',(cur.lastrowid,pago['tipo_pago'],pago['referencia'] or None,pago['financiera_cliente_id'] or None,financiera['nombre'] if financiera else None,pago['banco'],pago['monto'],d.get('fecha') or datetime.now().strftime('%Y-%m-%d'),pago['fecha_vencimiento'],vehiculo_recibido_id))
    except ValueError as error:
        c.rollback(); c.close(); return jsonify(error=str(error)),400
    except sqlite3.IntegrityError:
        c.rollback(); c.close(); return jsonify(error='El número de factura o referencia de pago ya está registrado'),409
    c.execute('UPDATE vehiculos SET precio_venta=? WHERE id=?',(precio,vid))
    recalcular_estado(c,vid,'Venta facturada')
    add_movimiento(c,vid,d.get('fecha') or datetime.now().strftime('%Y-%m-%d'),'Venta facturada',vehicle['estado'],'Vendido',vehicle['ubicacion'],vehicle['ubicacion'],referencia=factura,observaciones=f'Factura {factura}. Precio final: {precio:.2f}; pagos registrados: {total_pagos:.2f}')
    contabilizar_venta(c,cur.lastrowid,vehicle['estado'])
    c.commit(); c.close(); return jsonify(id=cur.lastrowid,numero_transaccion=numero),201

@app.delete('/api/ventas/<int:sid>')
def delete_venta_prueba(sid):
    """Elimina una venta de prueba y todas sus huellas financieras directas.

    Esta acción está restringida a administradores y exige escribir el número
    de transacción, para impedir una eliminación accidental desde el detalle.
    No se permite si la CxC ya recibió cobros ni si la venta incorporó un
    vehículo en cambio: esos casos requieren una anulación contable formal.
    """
    user=user_access(session.get('user_id')) if session.get('user_id') else None
    if not user or not user.get('es_administrador'):
        return jsonify(error='Solo un administrador puede eliminar una venta de prueba.'),403
    data=request.get_json(silent=True) or {}
    c=db(); sale=c.execute('SELECT * FROM ventas WHERE id=?',(sid,)).fetchone()
    if not sale:
        c.close(); return jsonify(error='Venta no encontrada.'),404
    number=sale['numero_transaccion'] or ''
    if (data.get('confirmacion') or '').strip().upper()!=number.upper():
        c.close(); return jsonify(error=f'Escriba {number} para confirmar la eliminación.'),400
    payments=c.execute('SELECT id,tipo_pago,vehiculo_recibido_id FROM pagos_venta WHERE venta_id=?',(sid,)).fetchall()
    if any(payment['tipo_pago']=='Cambio' or payment['vehiculo_recibido_id'] for payment in payments):
        c.close(); return jsonify(error='Esta venta contiene un vehículo recibido en cambio y debe anularse con un proceso contable formal.'),409
    cxc_rows=c.execute('SELECT id FROM cuentas_por_cobrar WHERE venta_id=?',(sid,)).fetchall()
    if cxc_rows:
        markers=','.join('?' for _ in cxc_rows)
        if c.execute(f'SELECT 1 FROM cobros_cuentas_por_cobrar WHERE cuenta_por_cobrar_id IN ({markers}) LIMIT 1',[row['id'] for row in cxc_rows]).fetchone():
            c.close(); return jsonify(error='La venta ya tiene cobros aplicados; debe anularse con un proceso contable formal.'),409
    payment_ids=[row['id'] for row in payments]
    previous=c.execute('''SELECT estado_anterior FROM movimientos_vehiculo
        WHERE vehiculo_id=? AND tipo='Venta facturada' AND referencia=? ORDER BY id DESC LIMIT 1''',
        (sale['vehiculo_id'],sale['factura'])).fetchone()
    status=previous['estado_anterior'] if previous and previous['estado_anterior'] in ESTADOS_VEHICULO else 'DPV'
    try:
        if payment_ids:
            markers=','.join('?' for _ in payment_ids)
            c.execute(f"DELETE FROM movimientos_caja WHERE referencia_tipo='pago_venta' AND referencia_id IN ({markers})",payment_ids)
        seats=c.execute("SELECT id FROM asientos_contables WHERE referencia_tipo IN ('venta_ingreso','venta_costo') AND referencia_id=?",(sid,)).fetchall()
        if seats:
            markers=','.join('?' for _ in seats); seat_ids=[row['id'] for row in seats]
            c.execute(f'DELETE FROM partidas WHERE asiento_id IN ({markers})',seat_ids)
            c.execute(f'DELETE FROM asientos_contables WHERE id IN ({markers})',seat_ids)
        c.execute("DELETE FROM movimientos_vehiculo WHERE vehiculo_id=? AND tipo='Venta facturada' AND referencia=?",(sale['vehiculo_id'],sale['factura']))
        c.execute('DELETE FROM ventas WHERE id=?',(sid,))
        c.execute('UPDATE vehiculos SET estado=? WHERE id=?',(status,sale['vehiculo_id']))
        c.commit()
    except sqlite3.Error:
        c.rollback(); c.close(); raise
    c.close(); return jsonify(ok=True,numero_transaccion=number,vehiculo_id=sale['vehiculo_id'],estado_restablecido=status)

@app.get('/api/proveedores')
def get_proveedores():
    tipo=(request.args.get('tipo') or '').strip()
    c=db(); sql='SELECT * FROM proveedores WHERE activo=1'; args=[]
    if tipo: sql+=' AND tipo=?'; args.append(tipo)
    r=c.execute(sql+' ORDER BY nombre',args).fetchall(); result=[]
    for row in r:
        item=dict(row); item['especialidades']=[x['especialidad'] for x in c.execute('SELECT especialidad FROM proveedor_especialidades WHERE proveedor_id=? ORDER BY especialidad',(row['id'],)).fetchall()]; result.append(item)
    c.close(); return jsonify(result)

@app.post('/api/proveedores')
def post_proveedor():
    d=request.get_json(silent=True); nombre=(d or {}).get('nombre','').strip()
    if not nombre: return jsonify(error='Nombre del proveedor es obligatorio'),400
    condicion=d.get('condicion_pago') or 'Contado'; dias=int(d.get('dias_credito') or 0); tipo=d.get('tipo') or 'Proveedor'
    especialidades=[str(x).strip() for x in (d.get('especialidades') or []) if str(x).strip()]
    if condicion not in ('Contado','Crédito') or dias<0 or tipo not in ('Proveedor','Taller'): return jsonify(error='Condición de pago o tipo inválido'),400
    c=db()
    try:
        cur=c.execute('INSERT INTO proveedores(nombre,rtn,telefono,email,direccion,condicion_pago,dias_credito,tipo) VALUES(?,?,?,?,?,?,?,?)',(nombre,d.get('rtn'),d.get('telefono'),d.get('email'),d.get('direccion'),condicion,dias if condicion=='Crédito' else 0,tipo))
        for especialidad in especialidades: c.execute('INSERT OR IGNORE INTO proveedor_especialidades(proveedor_id,especialidad) VALUES(?,?)',(cur.lastrowid,especialidad))
        c.commit()
    except sqlite3.IntegrityError: c.close(); return jsonify(error='El proveedor ya existe'),409
    c.close(); return jsonify(id=cur.lastrowid),201

@app.put('/api/proveedores/<int:proveedor_id>')
def put_proveedor(proveedor_id):
    d=request.get_json(silent=True) or {}; nombre=(d.get('nombre') or '').strip(); condicion=d.get('condicion_pago') or 'Contado'; tipo=d.get('tipo') or 'Proveedor'
    try: dias=int(d.get('dias_credito') or 0)
    except (TypeError,ValueError): return jsonify(error='Días de crédito inválidos.'),400
    especialidades=[str(x).strip() for x in (d.get('especialidades') or []) if str(x).strip()]
    if not nombre or condicion not in ('Contado','Crédito') or tipo not in ('Proveedor','Taller') or dias<0:
        return jsonify(error='Revise el nombre, tipo y condición de pago.'),400
    c=db(); existing=c.execute('SELECT id FROM proveedores WHERE id=?',(proveedor_id,)).fetchone()
    duplicate=c.execute('SELECT id FROM proveedores WHERE nombre=? AND id<>?',(nombre,proveedor_id)).fetchone()
    if not existing: c.close(); return jsonify(error='Proveedor no encontrado.'),404
    if duplicate: c.close(); return jsonify(error='Ya existe otro proveedor o taller con ese nombre.'),409
    c.execute('''UPDATE proveedores SET nombre=?,rtn=?,telefono=?,email=?,direccion=?,condicion_pago=?,dias_credito=?,tipo=? WHERE id=?''',
        (nombre,d.get('rtn'),d.get('telefono'),d.get('email'),d.get('direccion'),condicion,dias if condicion=='Crédito' else 0,tipo,proveedor_id))
    c.execute('DELETE FROM proveedor_especialidades WHERE proveedor_id=?',(proveedor_id,))
    for especialidad in especialidades: c.execute('INSERT OR IGNORE INTO proveedor_especialidades(proveedor_id,especialidad) VALUES(?,?)',(proveedor_id,especialidad))
    c.commit(); c.close(); return jsonify(ok=True)

@app.delete('/api/proveedores/<int:proveedor_id>')
def delete_proveedor(proveedor_id):
    """Desactiva un proveedor para conservar la trazabilidad de sus documentos."""
    c=db(); provider=c.execute('SELECT id,nombre,activo FROM proveedores WHERE id=?',(proveedor_id,)).fetchone()
    if not provider:
        c.close(); return jsonify(error='Proveedor o taller no encontrado.'),404
    if not provider['activo']:
        c.close(); return jsonify(error='El proveedor o taller ya fue eliminado.'),409
    c.execute('UPDATE proveedores SET activo=0 WHERE id=?',(proveedor_id,))
    c.commit(); c.close(); return jsonify(ok=True,eliminado=provider['nombre'])

@app.get('/api/financieras')
def get_financieras():
    c=db(); rows=c.execute("SELECT * FROM clientes WHERE tipo='Financiera' AND activo=1 ORDER BY nombre").fetchall(); c.close(); return jsonify([dict(row) for row in rows])

@app.post('/api/financieras')
def post_financiera():
    data=request.get_json(silent=True) or {}; nombre=(data.get('nombre') or '').strip(); condicion=data.get('condicion_pago') or 'Contado'
    if not nombre or condicion not in ('Contado','Crédito'): return jsonify(error='Nombre y condición válidos son obligatorios.'),400
    c=db()
    existing=c.execute("SELECT id FROM clientes WHERE nombre=? AND tipo='Financiera'",(nombre,)).fetchone()
    if existing: c.close(); return jsonify(error='La financiera ya existe.'),409
    cur=c.execute('''INSERT INTO clientes(nombre,rtn,direccion,tipo,condicion_pago,activo) VALUES(?,?,?,'Financiera',?,1)''',
        (nombre,(data.get('rtn') or '').strip(),(data.get('direccion') or '').strip(),condicion))
    c.commit(); c.close(); return jsonify(id=cur.lastrowid),201

@app.put('/api/financieras/<int:financiera_id>')
def put_financiera(financiera_id):
    data=request.get_json(silent=True) or {}; nombre=(data.get('nombre') or '').strip(); condicion=data.get('condicion_pago') or 'Contado'
    if not nombre or condicion not in ('Contado','Crédito'): return jsonify(error='Nombre y condición válidos son obligatorios.'),400
    c=db(); current=c.execute("SELECT id FROM clientes WHERE id=? AND tipo='Financiera'",(financiera_id,)).fetchone(); duplicate=c.execute("SELECT id FROM clientes WHERE nombre=? AND tipo='Financiera' AND id<>?",(nombre,financiera_id)).fetchone()
    if not current: c.close(); return jsonify(error='Financiera no encontrada.'),404
    if duplicate: c.close(); return jsonify(error='Ya existe otra financiera con ese nombre.'),409
    c.execute("UPDATE clientes SET nombre=?,rtn=?,direccion=?,condicion_pago=? WHERE id=? AND tipo='Financiera'",(nombre,(data.get('rtn') or '').strip(),(data.get('direccion') or '').strip(),condicion,financiera_id))
    c.commit(); c.close(); return jsonify(ok=True)

@app.delete('/api/financieras/<int:financiera_id>')
def delete_financiera(financiera_id):
    """Desactiva la financiera, sin borrar su participación en ventas anteriores."""
    c=db(); finance=c.execute("SELECT id,nombre,activo FROM clientes WHERE id=? AND tipo='Financiera'",(financiera_id,)).fetchone()
    if not finance:
        c.close(); return jsonify(error='Financiera no encontrada.'),404
    if not finance['activo']:
        c.close(); return jsonify(error='La financiera ya fue eliminada.'),409
    c.execute("UPDATE clientes SET activo=0 WHERE id=? AND tipo='Financiera'",(financiera_id,))
    c.commit(); c.close(); return jsonify(ok=True,eliminado=finance['nombre'])

@app.get('/api/adquisiciones')
def get_adquisiciones():
    c=db(); r=c.execute('''SELECT a.*,v.vin,v.marca,v.modelo,p.nombre proveedor_nombre FROM adquisiciones a JOIN vehiculos v ON v.id=a.vehiculo_id JOIN proveedores p ON p.id=a.proveedor_id ORDER BY a.fecha DESC,a.id DESC''').fetchall(); c.close(); return jsonify([dict(x) for x in r])

@app.get('/api/planificacion-financiera')
def get_planificacion_financiera():
    """Compromisos proyectados; es una vista y no genera CxP ni asientos."""
    mes=(request.args.get('mes') or ahora_operacion().strftime('%Y-%m')).strip()
    try:
        datetime.strptime(mes,'%Y-%m')
    except ValueError:
        return jsonify(error='El mes debe tener el formato AAAA-MM.'),400
    c=db()
    transit=c.execute('''SELECT a.id,a.fecha_llegada_estimada fecha_compromiso,a.fecha fecha_origen,
            COALESCE(cp.saldo,a.saldo) pendiente,COALESCE(cp.monto_original,a.costo_compra) monto_original,a.anticipo anticipos,
            a.metodo_pago,a.condicion_pago,v.vin,v.marca,v.modelo,p.nombre proveedor,
            'Adquisición' origen,'En Tránsito' etapa
        FROM adquisiciones a
        JOIN vehiculos v ON v.id=a.vehiculo_id
        JOIN proveedores p ON p.id=a.proveedor_id
        LEFT JOIN cuentas_por_pagar cp ON cp.adquisicion_id=a.id
        WHERE v.estado='En Tránsito' AND ROUND(COALESCE(cp.saldo,a.saldo,0),2)>0
        ORDER BY COALESCE(a.fecha_llegada_estimada,a.fecha),a.id''').fetchall()
    workshop=c.execute('''SELECT o.id,o.fecha_entrega_estimada fecha_compromiso,o.fecha fecha_origen,
            o.valor_negociado monto_original,COALESCE(SUM(ao.monto),0) anticipos,
            v.vin,v.marca,v.modelo,o.taller proveedor,o.numero_ot documento,
            'Orden de trabajo' origen,'En Taller' etapa
        FROM ordenes_trabajo o
        JOIN vehiculos v ON v.id=o.vehiculo_id
        LEFT JOIN anticipos_ot ao ON ao.orden_trabajo_id=o.id
        WHERE o.estado='Pendiente' AND v.estado='En Taller'
        GROUP BY o.id
        HAVING ROUND(o.valor_negociado-COALESCE(SUM(ao.monto),0),2)>0
        ORDER BY COALESCE(o.fecha_entrega_estimada,o.fecha),o.id''').fetchall()
    rows=[]
    for row in transit:
        item=dict(row); item['documento']=f"Adquisición #{item['id']}"; item['pendiente']=round(float(item['pendiente'] or 0),2); rows.append(item)
    for row in workshop:
        item=dict(row); item['anticipos']=round(float(item['anticipos'] or 0),2); item['pendiente']=round(float(item['monto_original'] or 0)-item['anticipos'],2); rows.append(item)
    rows.sort(key=lambda item:(item.get('fecha_compromiso') or '9999-12-31',item['origen'],item['id']))
    summary={'transito':round(sum(item['pendiente'] for item in rows if item['etapa']=='En Tránsito'),2),
             'taller':round(sum(item['pendiente'] for item in rows if item['etapa']=='En Taller'),2)}
    summary['total']=round(summary['transito']+summary['taller'],2)
    # Los contratos en dólares se muestran como compromisos de caja, pero no se
    # provisionan: la CxP y el asiento se registran al pagar, con el tipo de
    # cambio real de ese día. Por eso tampoco se suman a los totales en Lempiras.
    contratos_usd=c.execute('''SELECT co.id,co.numero_contrato,co.fecha_inicio,co.fecha_fin,
            co.monto_mensual,co.dia_pago,p.nombre proveedor
        FROM contratos co JOIN proveedores p ON p.id=co.proveedor_id
        WHERE co.activo=1 AND co.moneda='USD'
        ORDER BY co.fecha_inicio,co.id''').fetchall()
    compromisos_usd=[]
    for contrato in contratos_usd:
        inicio=fecha_planilla(contrato['fecha_inicio']); fin=fecha_planilla(contrato['fecha_fin'])
        cursor=inicio.replace(day=1)
        while cursor<=fin:
            vence=fecha_vencimiento_contrato(cursor.year,cursor.month,int(contrato['dia_pago']))
            if inicio<=vence<=fin:
                compromisos_usd.append({
                    'id':contrato['id'],'fecha_compromiso':vence.isoformat(),'fecha_origen':contrato['fecha_inicio'],
                    'pendiente':round(float(contrato['monto_mensual'] or 0),2),'monto_original':round(float(contrato['monto_mensual'] or 0),2),
                    'anticipos':0,'proveedor':contrato['proveedor'],'documento':f"Contrato {contrato['numero_contrato']}",
                    'origen':'Contrato en USD','etapa':'Contrato USD','moneda':'USD',
                })
            cursor=(cursor.replace(day=28)+timedelta(days=4)).replace(day=1)
    compromisos_usd.sort(key=lambda item:(item['fecha_compromiso'],item['id']))
    # El sugerido de compra es una proyección de caja, no una CxP ni un asiento.
    # Se toma el faltante entero por modelo y se valora con el costo promedio
    # histórico de adquisición de ese mismo modelo.
    costos_promedio={}
    for costo in c.execute('''SELECT v.modelo,
            AVG(CASE WHEN COALESCE(a.costo_compra,0)>0 THEN a.costo_compra
                     WHEN COALESCE(v.precio_compra,0)>0 THEN v.precio_compra END) costo_promedio
        FROM vehiculos v
        LEFT JOIN adquisiciones a ON a.vehiculo_id=v.id
        WHERE COALESCE(a.costo_compra,v.precio_compra,0)>0
        GROUP BY v.modelo''').fetchall():
        costos_promedio[_modelo_planificacion(costo['modelo'])]=round(float(costo['costo_promedio'] or 0),2)
    # La fecha es la semana de ORDENAR la compra, no la de recibirla. Se
    # anticipa la orden cuando la cobertura se agotará dentro de 70 días. Así,
    # por ejemplo, un quiebre estimado en 70 días exige comprar hoy. Las
    # unidades actuales en Taller vuelven en 30 días o en su fecha de OT, y
    # las de Tránsito se consideran disponibles en 45 días.
    inicio_mes=datetime.strptime(f'{mes}-01','%Y-%m-%d').date()
    inicio_plan=max(inicio_mes,ahora_operacion().date())
    entregas_taller={}
    for row in c.execute('''SELECT v.modelo,
            MIN(CASE WHEN o.estado='Pendiente' AND TRIM(COALESCE(o.fecha_entrega_estimada,''))<>''
                     THEN o.fecha_entrega_estimada END) fecha_entrega
        FROM vehiculos v
        LEFT JOIN ordenes_trabajo o ON o.vehiculo_id=v.id
        WHERE v.estado='En Taller' AND COALESCE(v.tipo_compra,'')<>'Consignación'
        GROUP BY v.id,v.modelo''').fetchall():
        modelo=_modelo_planificacion(row['modelo'])
        fecha=fecha_planilla(row['fecha_entrega'])
        entregas_taller.setdefault(modelo,[]).append(fecha or inicio_plan+timedelta(days=30))
    llegadas_transito={}
    for row in c.execute('''SELECT v.modelo,a.fecha
        FROM vehiculos v
        LEFT JOIN adquisiciones a ON a.vehiculo_id=v.id
        WHERE v.estado='En Tránsito' AND COALESCE(v.tipo_compra,'')<>'Consignación' ''').fetchall():
        modelo=_modelo_planificacion(row['modelo'])
        llegadas_transito.setdefault(modelo,[]).append(inicio_plan+timedelta(days=45))

    planificacion_compra=datos_planificacion_compra(c)
    compras_planificadas=[]
    for modelo in planificacion_compra['modelos']:
        unidades=int(modelo.get('faltante_compra') or 0)
        if unidades<=0: continue
        actual=modelo.get('inventario_actual') or {}
        costo_promedio=costos_promedio.get(modelo['modelo'],0)
        # Eventos de disponibilidad ya en curso. Se aplican al saldo DPV para
        # estimar la fecha de quiebre de cada modelo.
        disponibilidades={}
        for fecha in entregas_taller.get(modelo['modelo'],[]):
            disponibilidades[fecha]=disponibilidades.get(fecha,0)+1
        for fecha in llegadas_transito.get(modelo['modelo'],[]):
            disponibilidades[fecha]=disponibilidades.get(fecha,0)+1
        compras_en_camino={}
        demanda_diaria=float(modelo.get('promedio_mensual') or 0)/30.4375

        def fecha_quiebre():
            disponible=float(actual.get('DPV') or 0)
            for offset in range(367):
                fecha=inicio_plan+timedelta(days=offset)
                disponible+=disponibilidades.get(fecha,0)+compras_en_camino.get(fecha,0)
                disponible-=demanda_diaria
                if disponible<=0: return fecha
            return inicio_plan+timedelta(days=366)

        # Cada unidad faltante se coloca en la semana en que hay que ordenarla
        # para proteger una cobertura de 70 días. La llegada de esa compra se
        # proyecta 45 días después, como un nuevo tránsito.
        for _ in range(unidades):
            quiebre=fecha_quiebre() if demanda_diaria>0 else inicio_plan
            fecha=max(inicio_plan,quiebre-timedelta(days=70))
            llegada=fecha+timedelta(days=45)
            compras_en_camino[llegada]=compras_en_camino.get(llegada,0)+1
            semana=fecha-timedelta(days=fecha.weekday())
            compras_planificadas.append({
                'fecha_compromiso':fecha.isoformat(),'semana_compra':semana.isoformat(),
                'modelo':modelo['modelo'],'unidades':1,
                'costo_promedio':costo_promedio,'pendiente':round(costo_promedio,2),
                'origen':'Compra planificada','etapa':'Compra planificada',
                'motivo':f'Cobertura hasta {quiebre.isoformat()}','fecha_disponibilidad':llegada.isoformat(),
            })
    # Se consolidan unidades de un mismo modelo en la misma semana para no
    # llenar la planificación de líneas.
    consolidadas={}
    for item in compras_planificadas:
        key=(item['semana_compra'],item['modelo'])
        grouped=consolidadas.setdefault(key,{**item})
        if grouped is not item:
            grouped['unidades']+=item['unidades']
            grouped['pendiente']=round(grouped['unidades']*grouped['costo_promedio'],2)
            grouped['fecha_compromiso']=min(grouped['fecha_compromiso'],item['fecha_compromiso'])
            grouped['fecha_disponibilidad']=min(grouped['fecha_disponibilidad'],item['fecha_disponibilidad'])
    compras_planificadas=sorted(consolidadas.values(),key=lambda item:(item['fecha_compromiso'],item['modelo']))
    compras_del_mes=[item for item in compras_planificadas if item['fecha_compromiso'][:7]==mes]
    # La compra autorizada se muestra aparte de la sugerida. Es una intención
    # aprobada por gerencia: no crea una adquisición, CxP ni asiento contable.
    compras_autorizadas=[]
    for modelo in planificacion_compra['modelos']:
        costo_promedio=costos_promedio.get(modelo['modelo'],0)
        for detalle in modelo.get('distribucion') or []:
            unidades=max(0,int(detalle.get('autorizada') or 0))
            if not unidades: continue
            compras_autorizadas.append({
                'fecha_compromiso':inicio_plan.isoformat(),
                'semana_compra':(inicio_plan-timedelta(days=inicio_plan.weekday())).isoformat(),
                'modelo':modelo['modelo'],'anio':int(detalle['anio']),'unidades':unidades,
                'costo_promedio':costo_promedio,'pendiente':round(unidades*costo_promedio,2),
                'origen':'Compra autorizada','etapa':'Compra autorizada',
                'motivo':'Autorización manual desde integración de compra sugerida.',
            })
    compras_autorizadas.sort(key=lambda item:(item['modelo'],item['anio']))
    autorizadas_del_mes=[item for item in compras_autorizadas if item['fecha_compromiso'][:7]==mes]
    summary['compra_sugerida']=round(sum(item['pendiente'] for item in compras_del_mes),2)
    # Se conserva la clave anterior para integraciones que ya la consumen.
    summary['compra_planificada']=summary['compra_sugerida']
    summary['compra_autorizada']=round(sum(item['pendiente'] for item in autorizadas_del_mes),2)
    summary['total_con_compra_planificada']=round(summary['total']+summary['compra_sugerida']+summary['compra_autorizada'],2)
    c.close(); return jsonify(resumen=summary,compromisos=rows,compromisos_usd=compromisos_usd,
                              compras_planificadas=compras_planificadas,
                              compras_sugeridas=compras_planificadas,
                              compras_autorizadas=compras_autorizadas)

def _modelo_planificacion(value):
    return ' '.join(str(value or '').strip().split()).upper()

# Configuración inicial entregada por gerencia.  El promedio mensual no se
# guarda porque siempre se calcula a partir de las ventas de los últimos 12
# meses.  Las metas por etapa y por año sí son parámetros editables.
PARAMETROS_INVENTARIO_OPTIMO_BASE = [
    ('CRV',5,5,5,{2010:1,2011:1,2014:2,2015:2,2016:2,2017:1,2018:1,2020:1,2023:1}),
    ('F-150',3,3,2,{2016:2,2018:2,2020:2}),
    ('EXPLORER',6,6,6,{2015:2,2018:2,2021:1,2022:1}),
    ('RAV4',5,5,5,{2012:1,2014:1,2015:1,2016:2,2018:1,2020:2,2021:2,2022:1}),
    ('ELANTRA',2,2,1,{2016:2,2018:2,2020:1}), ('COLORADO',2,2,2,{2018:1,2019:1}),
    ('FRONTIER',3,2,2,{2015:1,2016:1,2017:1}), ('PRADO',0,0,0,{}),
    ('ROGUE',2,1,1,{2016:2,2018:2}), ('NP300',0,0,0,{}),
    ('ESCAPE',4,3,2,{2016:2,2018:2,2019:1,2020:2,2021:2}), ('TACOMA',4,3,1,{}),
    ('RANGER',2,1,2,{2019:1,2020:1,2021:1,2022:1}), ('HILUX',0,0,0,{}),
    ('COROLLA',2,2,1,{2010:1,2012:1,2017:1,2018:1,2022:1}), ('RAM',0,0,0,{}),
    ('CIVIC',2,1,1,{2015:1,2016:1,2018:1,2020:1}), ('SORENTO',3,0,2,{2015:1,2018:1,2020:1}),
    ('200',0,0,0,{}), ('EXPEDITION',1,0,1,{2020:1,2022:1}), ('PILOT',0,0,0,{}),
    ('AMAROK',0,0,0,{}), ('RANGER RPTOR',0,0,0,{}), ('OUTLANDER',0,0,0,{2016:1,2018:1}),
    ('WRANGLER',1,0,1,{2016:1,2017:1}), ('HRV',0,0,0,{}), ('LAND CRUSER',0,0,0,{}),
    ('X3',0,0,0,{}), ('X5',0,0,0,{}), ('SIENNA',1,0,1,{2016:1}), ('SANTA FÉ',0,0,0,{}),
    ('MURANO',0,0,0,{}), ('FIT',2,0,2,{2014:1,2016:1}), ('TUNDRA',0,0,0,{}),
    ('EXPLORER LIMITED',0,0,0,{}), ('PATRIOT',0,0,0,{}), ('TAHOE',0,0,0,{}),
    ('ODISSE',1,0,1,{2016:1}),
]

def _redondear_unidad_compra(cantidad):
    """Convierte un faltante no negativo a una cantidad comprable entera."""
    return int(max(0,float(cantidad or 0))+0.5)

def _normalizar_parametros_planificacion(items):
    if not isinstance(items,list): raise ValueError
    normalized=[]; seen=set()
    for item in items:
        if not isinstance(item,dict): raise ValueError
        model=_modelo_planificacion(item.get('modelo'))
        if not model or model in seen: raise ValueError
        seen.add(model)
        quantities={key:max(0,int(float(item.get(key,0) or 0))) for key in ('dpv','taller','transito')}
        distribution=item.get('distribucion') or {}
        if not isinstance(distribution,dict): raise ValueError
        years={str(year):max(0,int(float(quantity or 0))) for year,quantity in distribution.items()
               if str(year).isdigit() and 1900<=int(year)<=2100}
        normalized.append((model,quantities,years))
    return normalized

def _guardar_parametros_planificacion(items):
    normalized=_normalizar_parametros_planificacion(items)
    c=db()
    try:
        c.execute('DELETE FROM planificacion_compra_parametros')
        for model,quantities,years in normalized:
            # Se conservan incluso las filas en cero: son modelos que se
            # controlan en la matriz y deben ser visibles para futuras ediciones.
            c.execute('''INSERT INTO planificacion_compra_parametros(
                modelo,inventario_optimo_dpv,inventario_optimo_taller,inventario_optimo_transito,distribucion_anios,actualizado_en)
                VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)''',
                (model,quantities['dpv'],quantities['taller'],quantities['transito'],json.dumps(years,ensure_ascii=False)))
        c.commit()
    except Exception:
        c.rollback(); raise
    finally: c.close()

def datos_planificacion_compra(c, semanas=12):
    """Calcula la disponibilidad semanal sin crear compras ni asientos."""
    today=datetime.now().date()
    monday=today-timedelta(days=today.weekday())
    weeks=[monday+timedelta(days=index*7) for index in range(semanas)]
    year_values=set(range(2010,today.year+1))
    parameters={}
    autorizaciones={}
    for row in c.execute('SELECT modelo,anio,cantidad FROM planificacion_compra_autorizaciones'):
        modelo=_modelo_planificacion(row['modelo'])
        try: anio=int(row['anio'])
        except (TypeError,ValueError): continue
        autorizaciones[(modelo,anio)]=max(0,int(row['cantidad'] or 0))
    for row in c.execute('SELECT * FROM planificacion_compra_parametros'):
        item=dict(row)
        try: distribution=json.loads(item.get('distribucion_anios') or '{}')
        except (TypeError,json.JSONDecodeError): distribution={}
        normalized={str(year):max(0,int(float(quantity or 0))) for year,quantity in distribution.items()
                    if str(year).isdigit() and 1900<=int(year)<=2100}
        year_values.update(int(year) for year in normalized)
        parameters[_modelo_planificacion(item['modelo'])]={
            'dpv':max(0,int(item['inventario_optimo_dpv'] or 0)),
            'taller':max(0,int(item['inventario_optimo_taller'] or 0)),
            'transito':max(0,int(item['inventario_optimo_transito'] or 0)),
            'distribucion':normalized,
        }
    inventory={}
    models=set(parameters)
    for row in c.execute("""SELECT modelo,anio,estado,COALESCE(tipo_compra,'') tipo_compra,COUNT(*) cantidad
        FROM vehiculos
        WHERE estado IN ('DPV','En Taller','En Tránsito','Consignación') AND TRIM(COALESCE(modelo,''))<>''
        GROUP BY modelo,anio,estado,COALESCE(tipo_compra,'')"""):
        model=_modelo_planificacion(row['modelo']); models.add(model)
        item=inventory.setdefault(model,{'DPV':0,'En Taller':0,'En Tránsito':0,'Consignación':0,'anios':{}})
        state=row['estado'] or ''
        quantity=int(row['cantidad'] or 0)
        # La consignación puede tener una etapa operativa (por ejemplo DPV),
        # pero debe verse separada y no cubrir la meta de compra propia.
        is_consignment=(row['tipo_compra'] or '').strip().casefold()=='consignación' or state=='Consignación'
        if is_consignment: item['Consignación']+=quantity
        elif state in item: item[state]+=quantity
        # La distribución por año debe representar el inventario propio que
        # cubre la meta de compra; una unidad en consignación se muestra en su
        # columna, pero no se mezcla con DPV/Taller/Tránsito.
        if row['anio'] and not is_consignment:
            year=str(int(row['anio'])); year_values.add(int(year))
            item['anios'][year]=item['anios'].get(year,0)+quantity
    start_month=(today.replace(day=1)-timedelta(days=335)).isoformat()
    sales={}
    for row in c.execute("""SELECT v.modelo,COUNT(*) cantidad FROM ventas ve
        JOIN vehiculos v ON v.id=ve.vehiculo_id
        WHERE ve.fecha>=? AND ve.fecha<=? AND TRIM(COALESCE(v.modelo,''))<>''
        GROUP BY v.modelo""",(start_month,today.isoformat())):
        model=_modelo_planificacion(row['modelo']); models.add(model)
        sales[model]=int(row['cantidad'] or 0)
    costos_promedio={}
    for row in c.execute('''SELECT v.modelo,
            AVG(CASE WHEN COALESCE(a.costo_compra,0)>0 THEN a.costo_compra
                     WHEN COALESCE(v.precio_compra,0)>0 THEN v.precio_compra END) costo_promedio
        FROM vehiculos v LEFT JOIN adquisiciones a ON a.vehiculo_id=v.id
        WHERE COALESCE(a.costo_compra,v.precio_compra,0)>0
        GROUP BY v.modelo'''):
        costos_promedio[_modelo_planificacion(row['modelo'])]=round(float(row['costo_promedio'] or 0),2)
    transit_by_model={model:[0]*semanas for model in models}; workshop_by_model={model:[0]*semanas for model in models}
    transit_records=[]
    without_eta=[]; without_delivery=[]
    for row in c.execute("""SELECT v.id vehiculo_id,v.vin,v.marca,v.modelo,a.id adquisicion_id,a.fecha,a.fecha_llegada_estimada
        FROM vehiculos v JOIN adquisiciones a ON a.vehiculo_id=v.id
        WHERE v.estado='En Tránsito' ORDER BY a.fecha_llegada_estimada,a.id"""):
        model=_modelo_planificacion(row['modelo']); transit_by_model.setdefault(model,[0]*semanas); models.add(model)
        transit_records.append({'adquisicion_id':row['adquisicion_id'],'vehiculo_id':row['vehiculo_id'],
                                'vin':row['vin'],'marca':row['marca'],'modelo':model,
                                'fecha_compra':row['fecha'],'eta':row['fecha_llegada_estimada']})
        try: eta=datetime.strptime((row['fecha_llegada_estimada'] or '')[:10],'%Y-%m-%d').date()
        except ValueError:
            without_eta.append({'vin':row['vin'],'modelo':model,'motivo':'Sin ETA'}); continue
        position=(eta-monday).days//7
        if position<0: position=0
        if position<semanas: transit_by_model[model][position]+=1
    seen_workshop=set()
    for row in c.execute("""SELECT o.vehiculo_id,o.fecha_entrega_estimada,v.vin,v.modelo
        FROM ordenes_trabajo o JOIN vehiculos v ON v.id=o.vehiculo_id
        WHERE o.estado='Pendiente' AND v.estado='En Taller'
        ORDER BY o.vehiculo_id,COALESCE(o.fecha_entrega_estimada,o.fecha),o.id"""):
        if row['vehiculo_id'] in seen_workshop: continue
        seen_workshop.add(row['vehiculo_id'])
        model=_modelo_planificacion(row['modelo']); workshop_by_model.setdefault(model,[0]*semanas); models.add(model)
        try: delivery=datetime.strptime((row['fecha_entrega_estimada'] or '')[:10],'%Y-%m-%d').date()
        except ValueError:
            without_delivery.append({'vin':row['vin'],'modelo':model,'motivo':'Sin fecha de entrega'}); continue
        position=(delivery-monday).days//7
        if position<0: position=0
        if position<semanas: workshop_by_model[model][position]+=1
    rows=[]; weekly=[{'inicio':week.isoformat(),'fin':(week+timedelta(days=6)).isoformat(),
                      'transito':0,'taller':0,'demanda':0.0,'faltante_dpv':0.0} for week in weeks]
    for model in sorted(models,key=lambda item:(-sales.get(item,0),item)):
        current=inventory.get(model,{'DPV':0,'En Taller':0,'En Tránsito':0,'Consignación':0,'anios':{}})
        param=parameters.get(model,{'dpv':0,'taller':0,'transito':0,'distribucion':{}})
        average=round(sales.get(model,0)/12,2)
        target_total=param['dpv']+param['taller']+param['transito']
        # El faltante de compra solo considera inventario propio: DPV,
        # Taller y Tránsito. La consignación sí se informa como existencia
        # física, pero no se utiliza para reducir una compra planificada.
        current_total=current['DPV']+current['En Taller']+current['En Tránsito']
        physical_total=current_total+current['Consignación']
        arrivals=0; deliveries=0; demand_total=0.0; projection=[]
        for index,week in enumerate(weeks):
            arrivals+=transit_by_model.get(model,[0]*semanas)[index]
            deliveries+=workshop_by_model.get(model,[0]*semanas)[index]
            weekly_demand=average/4.345
            demand_total+=weekly_demand
            available=max(0,current['DPV']+arrivals+deliveries-demand_total)
            shortage=max(0,param['dpv']-available)
            projection.append({'inicio':week.isoformat(),'transito':transit_by_model.get(model,[0]*semanas)[index],
                               'taller':workshop_by_model.get(model,[0]*semanas)[index],
                               'demanda':round(weekly_demand,2),'disponible':round(available,2),'faltante_dpv':round(shortage,2)})
            weekly[index]['transito']+=transit_by_model.get(model,[0]*semanas)[index]
            weekly[index]['taller']+=workshop_by_model.get(model,[0]*semanas)[index]
            weekly[index]['demanda']+=weekly_demand
            weekly[index]['faltante_dpv']+=shortage
        # No ocultar unidades reales por no pertenecer a un año objetivo. La
        # tabla incluye la unión de años configurados y años actualmente en
        # inventario para que sus "Inv. actual" cuadren con DPV/Taller/Tránsito.
        distribution=[]
        distribution_years=set(param['distribucion'])|set(current['anios'])
        for year in sorted(distribution_years,key=int):
            value=param['distribucion'].get(year,0)
            actual=current['anios'].get(year,0)
            anio=int(year)
            distribution.append({'anio':anio,'ideal':value,'actual':actual,'sugerido':max(0,value-actual),
                                 'autorizada':autorizaciones.get((model,anio),0)})
        # El sugerido se compra por unidades completas: medio vehículo o más
        # requiere una unidad; cualquier fracción menor no genera compra.
        faltante_bruto=max(0,target_total-current_total)
        faltante_compra=_redondear_unidad_compra(faltante_bruto)
        rows.append({'modelo':model,'promedio_mensual':average,'ventas_12_meses':sales.get(model,0),
                     'inventario_actual':current,'inventario_optimo':param,'inventario_optimo_total':target_total,
                     'existencia_total':physical_total,'existencia_para_compra':current_total,'faltante_compra':faltante_compra,
                     'sugerido_compra':faltante_compra,'costo_promedio':costos_promedio.get(model,0),
                     'distribucion':distribution,'proyeccion':projection})
    for week in weekly:
        for key in ('demanda','faltante_dpv'): week[key]=round(week[key],2)
    return {'anios':sorted(year_values),'modelos':rows,'semanas':weekly,'transitos':transit_records,
            'sin_eta':without_eta,'sin_entrega':without_delivery,'periodo_ventas':{'desde':start_month,'hasta':today.isoformat()}}

@app.get('/api/planificacion-compra')
def get_planificacion_compra():
    c=db()
    try: data=datos_planificacion_compra(c)
    finally: c.close()
    return jsonify(data)

@app.put('/api/planificacion-compra/autorizaciones')
def put_planificacion_compra_autorizaciones():
    """Guarda unidades autorizadas por modelo/año, sin generar documentos."""
    data=request.get_json(silent=True) or {}
    try:
        modelo=_modelo_planificacion(data.get('modelo'))
        anio=int(data.get('anio'))
        cantidad=max(0,int(float(data.get('cantidad',0) or 0)))
        if not modelo or not 1900<=anio<=2100: raise ValueError
    except (TypeError,ValueError,OverflowError):
        return jsonify(error='Revise el modelo, año y cantidad autorizada.'),400
    c=db()
    try:
        if cantidad:
            c.execute('''INSERT INTO planificacion_compra_autorizaciones(modelo,anio,cantidad,actualizado_en)
                VALUES(?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(modelo,anio) DO UPDATE SET cantidad=excluded.cantidad,actualizado_en=CURRENT_TIMESTAMP''',
                (modelo,anio,cantidad))
        else:
            c.execute('DELETE FROM planificacion_compra_autorizaciones WHERE modelo=? AND anio=?',(modelo,anio))
        c.commit()
    except Exception:
        c.rollback(); raise
    finally: c.close()
    return jsonify(ok=True,modelo=modelo,anio=anio,cantidad=cantidad)

@app.put('/api/planificacion-compra/parametros')
def put_planificacion_compra_parametros():
    data=request.get_json(silent=True) or {}; items=data.get('parametros')
    try:
        _guardar_parametros_planificacion(items)
    except (TypeError,ValueError,OverflowError):
        return jsonify(error='Revise las cantidades de los parámetros de compra.'),400
    return jsonify(ok=True)

@app.post('/api/planificacion-compra/parametros/base')
def post_planificacion_compra_parametros_base():
    items=[{'modelo':modelo,'dpv':dpv,'taller':taller,'transito':transito,
            'distribucion':{str(year):quantity for year,quantity in distribution.items()}}
           for modelo,dpv,taller,transito,distribution in PARAMETROS_INVENTARIO_OPTIMO_BASE]
    _guardar_parametros_planificacion(items)
    return jsonify(ok=True,cantidad=len(items))

@app.put('/api/planificacion-compra/transitos/<int:adquisicion_id>/eta')
def put_planificacion_compra_eta(adquisicion_id):
    data=request.get_json(silent=True) or {}; eta=(data.get('eta') or '').strip()
    try: datetime.strptime(eta,'%Y-%m-%d')
    except ValueError: return jsonify(error='Ingrese una fecha ETA válida.'),400
    c=db(); purchase=c.execute('''SELECT a.*,v.vin,v.estado,v.ubicacion FROM adquisiciones a
        JOIN vehiculos v ON v.id=a.vehiculo_id WHERE a.id=?''',(adquisicion_id,)).fetchone()
    if not purchase: c.close(); return jsonify(error='Adquisición no encontrada.'),404
    if purchase['estado']!='En Tránsito': c.close(); return jsonify(error='Solo puede actualizar el ETA de un vehículo en tránsito.'),400
    previous=purchase['fecha_llegada_estimada']
    c.execute('UPDATE adquisiciones SET fecha_llegada_estimada=? WHERE id=?',(eta,adquisicion_id))
    add_movimiento(c,purchase['vehiculo_id'],datetime.now().strftime('%Y-%m-%d'),'ETA actualizado',
                   purchase['estado'],purchase['estado'],purchase['ubicacion'],purchase['ubicacion'],
                   referencia='Planificación de compra',observaciones=f'ETA actualizado de {previous or "sin fecha"} a {eta}.')
    c.commit(); c.close(); return jsonify(ok=True,eta=eta)

@app.get('/api/ordenes-trabajo')
def get_ordenes_trabajo():
    c=db(); r=c.execute('''SELECT o.*,v.vin,v.marca,v.modelo FROM ordenes_trabajo o
        JOIN vehiculos v ON v.id=o.vehiculo_id ORDER BY o.fecha DESC,o.id DESC''').fetchall(); c.close()
    return jsonify([dict(x) for x in r])

@app.put('/api/ordenes-trabajo/<int:oid>')
def put_orden_trabajo(oid):
    d=request.get_json(silent=True) or {}; estado=d.get('estado'); accion=d.get('accion')
    if accion not in (None,'finalizar_reparacion','continuar_mantenimiento','modificar','corregir_cierre','contabilizar_cierre_existente') and estado not in ('Pendiente','Finalizada'):
        return jsonify(error='Acción de orden de trabajo inválida'),400
    c=db(); order=c.execute('SELECT * FROM ordenes_trabajo WHERE id=?',(oid,)).fetchone()
    if not order: c.close(); return jsonify(error='Orden de trabajo no encontrada'),404
    if accion=='modificar':
        fecha=(d.get('fecha') or '').strip(); taller=(d.get('taller') or '').strip()
        descripcion=(d.get('descripcion') or '').strip(); nuevo_estado=d.get('estado')
        entrega=(d.get('fecha_entrega_estimada') or order['fecha_entrega_estimada'] or '').strip()
        if not fecha or not taller or not descripcion or nuevo_estado not in ('Pendiente','Finalizada'):
            c.close(); return jsonify(error='Fecha, taller, descripción y estado válidos son obligatorios'),400
        if nuevo_estado!=order['estado']:
            c.close(); return jsonify(error='El cierre debe realizarse con el botón “Cerrar OT” para registrar el valor final.'),400
        historical=int(order['migracion_historica'] or 0)
        if entrega:
            try: datetime.strptime(entrega,'%Y-%m-%d')
            except ValueError: c.close(); return jsonify(error='La fecha estimada de entrega no es válida.'),400
            if entrega<fecha: c.close(); return jsonify(error='La fecha de entrega no puede ser anterior a la fecha de la OT.'),400
        c.execute('''UPDATE ordenes_trabajo SET fecha=?,taller=?,descripcion=?,detalle=?,estado=?,fecha_entrega_estimada=?,
                     valor_final=CASE WHEN ?=1 AND ?='Finalizada' THEN 0 ELSE valor_final END,
                     costo_cargado=CASE WHEN ?=1 AND ?='Finalizada' THEN 1 ELSE costo_cargado END
                     WHERE id=?''',
                  (fecha,taller,descripcion,descripcion,nuevo_estado,entrega or None,historical,nuevo_estado,historical,nuevo_estado,oid))
        recalcular_estado(c,order['vehiculo_id'],'OT modificada')
        c.commit(); c.close(); return jsonify(ok=True,vehiculo_id=order['vehiculo_id'])
    if accion=='corregir_cierre':
        if order['estado']!='Finalizada':
            c.close(); return jsonify(error='Solo se puede corregir el cierre de una OT finalizada.'),400
        try:
            valor_final=round(float(d.get('valor_final')),2)
        except (TypeError,ValueError):
            c.close(); return jsonify(error='El valor final de la OT debe ser numérico.'),400
        if valor_final<0:
            c.close(); return jsonify(error='El valor final de la OT no puede ser negativo.'),400
        if order['metodo_pago'] and abs(float(order['valor_final'] or 0)-valor_final)>0.01:
            c.close(); return jsonify(error='Esta OT ya tiene pago y partida contable. Registre un ajuste contable antes de cambiar su valor final.'),409
        fecha=datetime.now().strftime('%Y-%m-%d'); documento=order['numero_ot'] or f'OT-{oid}'
        existing=c.execute('''SELECT id FROM costos_vehiculo WHERE vehiculo_id=? AND documento=?
            AND categoria='Mano de obra / OT' ORDER BY id DESC LIMIT 1''',(order['vehiculo_id'],documento)).fetchone()
        if existing and valor_final:
            c.execute('''UPDATE costos_vehiculo SET fecha=?,concepto=?,monto=?,proveedor=?,observaciones=? WHERE id=?''',
                (fecha,order['descripcion'] or order['detalle'] or 'Trabajo de taller',valor_final,order['taller'],
                 'Valor final corregido al cerrar la OT.',existing['id']))
        elif existing:
            c.execute('DELETE FROM costos_vehiculo WHERE id=?',(existing['id'],))
        elif valor_final:
            c.execute('''INSERT INTO costos_vehiculo(vehiculo_id,fecha,concepto,categoria,monto,proveedor,documento,observaciones)
                VALUES(?,?,?,?,?,?,?,?)''',(order['vehiculo_id'],fecha,order['descripcion'] or order['detalle'] or 'Trabajo de taller',
                'Mano de obra / OT',valor_final,order['taller'],documento,'Valor final corregido al cerrar la OT.'))
        c.execute('UPDATE ordenes_trabajo SET valor_final=?,costo_cargado=1 WHERE id=?',(valor_final,oid))
        vehicle=c.execute('SELECT estado,ubicacion FROM vehiculos WHERE id=?',(order['vehiculo_id'],)).fetchone()
        total=costo_consolidado(c,order['vehiculo_id'])
        add_movimiento(c,order['vehiculo_id'],fecha,'Corrección de cierre de OT',vehicle['estado'],vehicle['estado'],vehicle['ubicacion'],vehicle['ubicacion'],
            referencia=documento,observaciones=f'Valor final de mano de obra: {valor_final:.2f}. Costo consolidado: {total:.2f}')
        c.commit(); c.close(); return jsonify(ok=True,vehiculo_id=order['vehiculo_id'],costo_total=total)
    if accion=='contabilizar_cierre_existente':
        if order['estado']!='Finalizada':
            c.close(); return jsonify(error='Solo se puede contabilizar una OT finalizada.'),400
        if order['metodo_pago']:
            c.close(); return jsonify(error='Esta OT ya tiene método de pago y partida de cierre.'),409
        pago,error=validar_pago_cierre_ot(d)
        if error:
            c.close(); return jsonify(error=error),400
        metodo_pago,banco,referencia=pago
        if referencia and not referencia_pago_disponible(c,referencia):
            c.close(); return jsonify(error='El número de referencia ya fue utilizado'),409
        provider=c.execute('SELECT * FROM proveedores WHERE nombre=? AND activo=1',(order['taller'],)).fetchone()
        if not provider:
            c.close(); return jsonify(error='La OT debe tener un taller/proveedor activo para registrar su pago.'),400
        fecha=datetime.now().strftime('%Y-%m-%d')
        c.execute('UPDATE ordenes_trabajo SET metodo_pago=?,banco_pago=?,referencia_pago=? WHERE id=?',(metodo_pago,banco,referencia,oid))
        documento=order['numero_ot'] or f'OT-{oid}'
        existing_cost=c.execute('''SELECT id FROM costos_vehiculo WHERE vehiculo_id=? AND documento=?
            AND categoria='Mano de obra / OT' ORDER BY id DESC LIMIT 1''',(order['vehiculo_id'],documento)).fetchone()
        if not existing_cost and float(order['valor_final'] or 0):
            c.execute('''INSERT INTO costos_vehiculo(vehiculo_id,fecha,concepto,categoria,monto,proveedor,documento,observaciones)
                VALUES(?,?,?,?,?,?,?,?)''',(order['vehiculo_id'],fecha,order['descripcion'] or order['detalle'] or 'Trabajo de taller',
                'Mano de obra / OT',float(order['valor_final']),provider['nombre'],documento,'Costo regularizado desde cierre de OT anterior.'))
        c.execute('UPDATE ordenes_trabajo SET costo_cargado=1 WHERE id=?',(oid,))
        contabilizar_cierre_ot(c,order,provider,float(order['valor_final'] or 0),metodo_pago,banco,referencia,fecha)
        vehicle=c.execute('SELECT estado,ubicacion FROM vehiculos WHERE id=?',(order['vehiculo_id'],)).fetchone()
        total=costo_consolidado(c,order['vehiculo_id'])
        add_movimiento(c,order['vehiculo_id'],fecha,'Regularización contable de OT',vehicle['estado'],vehicle['estado'],vehicle['ubicacion'],vehicle['ubicacion'],referencia=documento,observaciones=f'Método de pago: {metodo_pago}. Partida de cierre registrada. Costo consolidado: {total:.2f}')
        c.commit(); c.close(); return jsonify(ok=True,vehiculo_id=order['vehiculo_id'])
    if accion:
        if order['estado']!='Pendiente': c.close(); return jsonify(error='La OT ya está finalizada'),400
        try:
            valor_final=float(d.get('valor_final',order['valor_negociado'] or 0))
        except (TypeError,ValueError):
            c.close(); return jsonify(error='El valor final de la OT debe ser numérico'),400
        if valor_final<0:
            c.close(); return jsonify(error='El valor final de la OT no puede ser negativo'),400
        pago,error=validar_pago_cierre_ot(d)
        if error:
            c.close(); return jsonify(error=error),400
        metodo_pago,banco,referencia=pago
        if referencia and not referencia_pago_disponible(c,referencia):
            c.close(); return jsonify(error='El número de referencia ya fue utilizado'),409
        provider=c.execute('SELECT * FROM proveedores WHERE nombre=? AND activo=1',(order['taller'],)).fetchone()
        if not provider:
            c.close(); return jsonify(error='La OT debe tener un taller/proveedor activo para registrar su pago.'),400
        fecha_cierre=datetime.now().strftime('%Y-%m-%d')
        c.execute("UPDATE ordenes_trabajo SET estado='Finalizada',valor_final=?,metodo_pago=?,banco_pago=?,referencia_pago=? WHERE id=?",(valor_final,metodo_pago,banco,referencia,oid))
        contabilizar_cierre_ot(c,order,provider,valor_final,metodo_pago,banco,referencia,fecha_cierre)
        # Una continuación no crea documentos en automático. La persona debe
        # completar la pantalla normal de nueva OT y confirmar “Generar OT”.
        nueva_ot=None
        pending_costs=c.execute("SELECT * FROM ordenes_trabajo WHERE vehiculo_id=? AND costo_cargado=0 AND estado='Finalizada'",(order['vehiculo_id'],)).fetchall()
        vehicle=c.execute('SELECT estado,ubicacion,precio_compra FROM vehiculos WHERE id=?',(order['vehiculo_id'],)).fetchone()
        total_trabajo=0
        for work in pending_costs:
            value=float(work['valor_final'] if work['valor_final'] is not None else work['valor_negociado'] or 0); total_trabajo+=value
            if value:
                c.execute('''INSERT INTO costos_vehiculo(vehiculo_id,fecha,concepto,categoria,monto,proveedor,documento,observaciones)
                    VALUES(?,?,?,?,?,?,?,?)''',(order['vehiculo_id'],fecha_cierre,work['descripcion'] or work['detalle'] or 'Trabajo de taller','Mano de obra / OT',value,work['taller'],work['numero_ot'],f'Costo al cerrar la OT · método: {metodo_pago}'))
            c.execute('UPDATE ordenes_trabajo SET costo_cargado=1 WHERE id=?',(work['id'],))
        if pending_costs:
            total_consolidado=costo_consolidado(c,order['vehiculo_id'])
            add_movimiento(c,order['vehiculo_id'],fecha_cierre,'Cierre de OT',vehicle['estado'],vehicle['estado'],vehicle['ubicacion'],vehicle['ubicacion'],referencia=order['numero_ot'],observaciones=f'Mano de obra cargada: {total_trabajo:.2f}. Método: {metodo_pago}. Costo consolidado: {total_consolidado:.2f}')
        if accion=='finalizar_reparacion':
            recalcular_estado(c,order['vehiculo_id'],'Reparación finalizada')
        else:
            vehicle=c.execute('SELECT estado,ubicacion FROM vehiculos WHERE id=?',(order['vehiculo_id'],)).fetchone()
            if vehicle['estado']!='En Taller':
                c.execute("UPDATE vehiculos SET estado='En Taller' WHERE id=?",(order['vehiculo_id'],))
                add_movimiento(c,order['vehiculo_id'],datetime.now().strftime('%Y-%m-%d'),'Mantenimiento continúa',vehicle['estado'],'En Taller',vehicle['ubicacion'],vehicle['ubicacion'],referencia=order['numero_ot'],observaciones='Cree una nueva OT desde el formulario para continuar el trabajo.')
        c.commit(); c.close(); return jsonify(ok=True,nueva_ot=nueva_ot,vehiculo_id=order['vehiculo_id'],estado='DPV' if accion=='finalizar_reparacion' else 'En Taller')
    if estado not in ('Pendiente','Finalizada'): c.close(); return jsonify(error='Estado de orden de trabajo inválido'),400
    c.execute('UPDATE ordenes_trabajo SET estado=? WHERE id=?',(estado,oid))
    recalcular_estado(c,order['vehiculo_id'],'Orden de trabajo finalizada' if estado=='Finalizada' else 'Orden de trabajo reabierta')
    c.commit(); c.close(); return jsonify(ok=True)

@app.post('/api/ordenes-trabajo')
def post_orden_trabajo():
    d=request.get_json(silent=True) or {}
    try: vid=int(d.get('vehiculo_id'))
    except (TypeError,ValueError): return jsonify(error='Vehículo es obligatorio'),400
    taller=(d.get('taller') or '').strip(); reparacion=(d.get('tipo_reparacion') or '').strip(); detalle=(d.get('descripcion') or d.get('detalle') or '').strip()
    fecha_entrega=(d.get('fecha_entrega_estimada') or '').strip()
    try: valor=float(d.get('valor_negociado'))
    except (TypeError,ValueError): return jsonify(error='El valor negociado es obligatorio'),400
    if not taller or not reparacion or not detalle or not fecha_entrega: return jsonify(error='Taller, tipo de reparación, descripción y fecha estimada de entrega son obligatorios'),400
    if valor<0: return jsonify(error='El valor negociado no puede ser negativo'),400
    c=db(); vehicle=c.execute('SELECT * FROM vehiculos WHERE id=?',(vid,)).fetchone()
    if not vehicle: c.close(); return jsonify(error='Vehículo no encontrado'),404
    acquisition=c.execute('SELECT id,tipo_compra FROM adquisiciones WHERE vehiculo_id=?',(vid,)).fetchone()
    costing=c.execute('SELECT estatus FROM costos_adquisicion WHERE vehiculo_id=?',(vid,)).fetchone()
    # Una unidad que continúa en Tránsito no puede abrir una OT. Sin embargo,
    # las unidades históricas que ya están en Taller conservan a veces el
    # marcador de costeo "Pendiente"; bloquearlas contradice su etapa real y
    # deja el taller sin poder documentar la reparación.
    if (acquisition and acquisition['tipo_compra']=='Importación'
            and vehicle['estado']=='En Tránsito'
            and (not costing or costing['estatus']!='Nacionalizado')):
        c.close(); return jsonify(error='El vehículo importado debe estar nacionalizado antes de crear una OT'),400
    fecha_ot=d.get('fecha') or datetime.now().strftime('%Y-%m-%d')
    try:
        datetime.strptime(fecha_entrega,'%Y-%m-%d')
    except ValueError:
        c.close(); return jsonify(error='La fecha estimada de entrega no es válida.'),400
    if fecha_entrega<fecha_ot:
        c.close(); return jsonify(error='La fecha estimada de entrega no puede ser anterior a la fecha de la OT.'),400
    order_id,numero=crear_orden_trabajo(c,vid,acquisition['id'] if acquisition else None,fecha_ot,taller,reparacion,valor,detalle,fecha_entrega)
    advance_id=None
    try:
        anticipo=round(float(d.get('anticipo_ot') or 0),2)
    except (TypeError,ValueError):
        c.close(); return jsonify(error='El anticipo de la OT debe ser numérico.'),400
    if anticipo<0 or anticipo>valor:
        c.close(); return jsonify(error='El anticipo no puede ser negativo ni mayor que el valor negociado.'),400
    if anticipo:
        pago,error=validar_pago_anticipo_ot(d)
        if error:
            c.close(); return jsonify(error=error),400
        metodo,banco,referencia=pago
        if referencia and not referencia_pago_disponible(c,referencia):
            c.close(); return jsonify(error='El número de referencia ya fue utilizado.'),409
        order=c.execute('SELECT * FROM ordenes_trabajo WHERE id=?',(order_id,)).fetchone()
        advance_id=registrar_anticipo_ot(c,order,anticipo,metodo,banco,referencia,fecha_ot)
    recalcular_estado(c,vid,'Orden de trabajo creada')
    c.commit(); c.close(); return jsonify(ok=True,numero_ot=numero,anticipo=anticipo,anticipo_id=advance_id),201

@app.get('/api/ordenes-trabajo/<int:oid>/anticipos')
def get_anticipos_ot(oid):
    c=db(); rows=c.execute('SELECT * FROM anticipos_ot WHERE orden_trabajo_id=? ORDER BY fecha DESC,id DESC',(oid,)).fetchall(); c.close()
    return jsonify([dict(row) for row in rows])

@app.post('/api/ordenes-trabajo/<int:oid>/anticipos')
def post_anticipo_ot(oid):
    d=request.get_json(silent=True) or {}
    try:
        monto=round(float(d.get('monto')),2)
    except (TypeError,ValueError):
        return jsonify(error='El monto del anticipo es obligatorio.'),400
    if monto<=0: return jsonify(error='El anticipo debe ser mayor que cero.'),400
    c=db(); order=c.execute('SELECT * FROM ordenes_trabajo WHERE id=?',(oid,)).fetchone()
    if not order: c.close(); return jsonify(error='OT no encontrada.'),404
    if order['estado']!='Pendiente': c.close(); return jsonify(error='Solo puede anticipar una OT pendiente.'),400
    if monto+total_anticipos_ot(c,oid)>round(float(order['valor_negociado'] or 0),2)+0.01:
        c.close(); return jsonify(error='Los anticipos no pueden superar el valor negociado de la OT.'),400
    pago,error=validar_pago_anticipo_ot(d)
    if error: c.close(); return jsonify(error=error),400
    metodo,banco,referencia=pago
    if referencia and not referencia_pago_disponible(c,referencia):
        c.close(); return jsonify(error='El número de referencia ya fue utilizado.'),409
    advance_id=registrar_anticipo_ot(c,order,monto,metodo,banco,referencia,d.get('fecha') or datetime.now().strftime('%Y-%m-%d'))
    c.commit(); c.close(); return jsonify(ok=True,id=advance_id),201

@app.get('/api/ordenes-trabajo/<int:oid>/repuestos')
def get_repuestos_ot(oid):
    c=db(); items=c.execute('''SELECT r.*,p.nombre proveedor_nombre FROM repuestos_ot r
        JOIN proveedores p ON p.id=r.proveedor_id WHERE r.orden_trabajo_id=? ORDER BY r.fecha DESC,r.id DESC''',(oid,)).fetchall(); c.close()
    return jsonify([dict(x) for x in items])

@app.get('/api/ordenes-trabajo/<int:oid>/gastos')
def get_gastos_ot(oid):
    # El detalle de una OT no puede depender solo de la tabla histórica de
    # gastos: los repuestos y la mano de obra final también son costos propios
    # de la orden. Se entrega una vista unificada, sin duplicar el costo que
    # ya se registró en costos_vehiculo.
    c=db(); items=c.execute('''
        SELECT g.fecha,g.taller_origen,g.categoria,g.descripcion,
               p.nombre proveedor_nombre,g.factura,g.total,
               NULL metodo_pago,'Gasto registrado' origen,g.id orden,
               g.id origen_id,'gasto_ot' origen_tipo
          FROM gastos_ot g
          LEFT JOIN proveedores p ON p.id=g.proveedor_id
         WHERE g.orden_trabajo_id=?
        UNION ALL
        SELECT r.fecha,o.taller,'Repuestos / insumos',r.descripcion,
               p.nombre,r.factura,r.total,r.metodo_pago,'Repuesto / insumo',r.id,
               r.id,'repuesto_ot'
          FROM repuestos_ot r
          JOIN ordenes_trabajo o ON o.id=r.orden_trabajo_id
          JOIN proveedores p ON p.id=r.proveedor_id
         WHERE r.orden_trabajo_id=?
        UNION ALL
        SELECT cv.fecha,o.taller,'Mano de obra / cierre',cv.concepto,
               cv.proveedor,cv.documento,cv.monto,o.metodo_pago,'Cierre de OT',cv.id,
               cv.id,'cierre_ot'
          FROM costos_vehiculo cv
          JOIN ordenes_trabajo o ON o.vehiculo_id=cv.vehiculo_id
         WHERE o.id=? AND cv.documento=o.numero_ot
           AND COALESCE(cv.categoria,'')='Mano de obra'
         ORDER BY fecha,orden
    ''',(oid,oid,oid)).fetchall(); c.close()
    return jsonify([dict(x) for x in items])

@app.delete('/api/gastos-ot/<int:gasto_id>')
def eliminar_gasto_ot(gasto_id):
    """Revierte una línea histórica de OT junto con su costo y contabilidad."""
    data=request.get_json(silent=True) or {}
    if data.get('confirmacion')!='ELIMINAR':
        return jsonify(error='Confirme la eliminación para continuar.'),400
    c=db(); gasto=c.execute('''SELECT g.*,o.vehiculo_id,o.numero_ot
        FROM gastos_ot g JOIN ordenes_trabajo o ON o.id=g.orden_trabajo_id
        WHERE g.id=?''',(gasto_id,)).fetchone()
    if not gasto:
        c.close(); return jsonify(error='Concepto histórico no encontrado.'),404
    try:
        # Si una instalación anterior llegó a contabilizar este gasto, sus
        # partidas y la salida de caja usan el ID del gasto como referencia.
        for reference_type in ('gasto_ot','gasto_ot_historico'):
            c.execute('DELETE FROM movimientos_caja WHERE referencia_tipo=? AND referencia_id=?',(reference_type,gasto_id))
            headers=c.execute('SELECT id FROM asientos_contables WHERE referencia_tipo=? AND referencia_id=?',(reference_type,gasto_id)).fetchall()
            for header in headers:
                c.execute('DELETE FROM partidas WHERE asiento_id=?',(header['id'],))
                c.execute('DELETE FROM asientos_contables WHERE id=?',(header['id'],))
        # gasto_ot conoce exactamente el costo derivado, por lo que no se toca
        # ningún otro concepto de la misma OT aunque comparta factura "0".
        if gasto['costo_vehiculo_id']:
            c.execute('DELETE FROM costos_vehiculo WHERE id=? AND vehiculo_id=?',(gasto['costo_vehiculo_id'],gasto['vehiculo_id']))
        c.execute('DELETE FROM gastos_ot WHERE id=?',(gasto_id,))
        c.commit()
    except Exception:
        c.rollback(); raise
    finally:
        c.close()
    return jsonify(ok=True,numero_ot=gasto['numero_ot'])

@app.get('/api/contabilidad/inventario-transito/movimientos')
def movimientos_inventario_transito():
    """Trazabilidad de los asientos que aumentan o disminuyen Tránsito."""
    _,hasta=rango_reporte(); c=db(); where=['cc.codigo=?']; args=['1105']
    if hasta:
        where.append('p.fecha<=?'); args.append(hasta)
    rows=c.execute('''SELECT p.fecha,p.debe,p.haber,p.vehiculo_id,p.referencia_tipo,
                             p.referencia_id,p.descripcion,a.descripcion asiento_descripcion,
                             v.vin,v.marca,v.modelo
                        FROM partidas p
                        JOIN cuentas_contables cc ON cc.id=p.cuenta_id
                        LEFT JOIN asientos_contables a ON a.id=p.asiento_id
                        LEFT JOIN vehiculos v ON v.id=p.vehiculo_id
                       WHERE '''+' AND '.join(where)+''' ORDER BY p.fecha DESC,p.id DESC''',args).fetchall()
    c.close(); return jsonify([dict(row) for row in rows])

@app.post('/api/ordenes-trabajo/<int:oid>/repuestos')
def post_repuesto_ot(oid):
    d=request.get_json(silent=True) or {}
    factura=(d.get('factura') or '').strip(); descripcion=(d.get('descripcion') or '').strip()
    try:
        proveedor_id=int(d.get('proveedor_id')); subtotal=float(d.get('subtotal')); isv=float(d.get('isv') or 0)
    except (TypeError,ValueError): return jsonify(error='Proveedor y subtotal son obligatorios'),400
    if not factura or not descripcion: return jsonify(error='Factura y descripción son obligatorias'),400
    if subtotal<0 or isv<0: return jsonify(error='Subtotal e ISV no pueden ser negativos'),400
    c=db(); order=c.execute('SELECT * FROM ordenes_trabajo WHERE id=?',(oid,)).fetchone(); provider=c.execute('SELECT * FROM proveedores WHERE id=? AND activo=1',(proveedor_id,)).fetchone()
    if not order or not provider: c.close(); return jsonify(error='OT o proveedor no encontrado'),404
    if order['estado']!='Pendiente': c.close(); return jsonify(error='No puede registrar repuestos en una OT finalizada'),400
    pago,error=validar_pago_repuesto(d)
    if error: c.close(); return jsonify(error=error),400
    metodo_pago,banco,referencia_pago=pago
    total=subtotal+isv; fecha=d.get('fecha') or datetime.now().strftime('%Y-%m-%d')
    try:
        cur=c.execute('''INSERT INTO repuestos_ot(orden_trabajo_id,proveedor_id,factura,fecha,descripcion,subtotal,isv,total,metodo_pago,banco_pago,referencia_pago)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)''',(oid,proveedor_id,factura,fecha,descripcion,subtotal,isv,total,metodo_pago,banco,referencia_pago))
    except sqlite3.IntegrityError: c.close(); return jsonify(error='Esta factura ya fue registrada en la OT'),409
    c.execute('''INSERT INTO costos_vehiculo(vehiculo_id,fecha,concepto,categoria,monto,proveedor,documento,observaciones)
        VALUES(?,?,?,?,?,?,?,?)''',(order['vehiculo_id'],fecha,descripcion,'Repuestos / OT',total,provider['nombre'],factura,f'{order["numero_ot"] or "OT"}: repuesto o insumo'))
    vehicle=c.execute('SELECT estado,ubicacion FROM vehiculos WHERE id=?',(order['vehiculo_id'],)).fetchone(); total_consolidado=costo_consolidado(c,order['vehiculo_id'])
    contabilizar_repuesto_ot(c,cur.lastrowid,order,provider,total,metodo_pago,banco,referencia_pago,fecha,factura,descripcion)
    add_movimiento(c,order['vehiculo_id'],fecha,'Repuesto cargado a OT',vehicle['estado'],vehicle['estado'],vehicle['ubicacion'],vehicle['ubicacion'],referencia=factura,observaciones=f'{order["numero_ot"] or "OT"}: {descripcion}. Método: {metodo_pago}. Costo consolidado: {total_consolidado:.2f}')
    c.commit(); c.close(); return jsonify(ok=True,total=total),201

@app.put('/api/repuestos-ot/<int:part_id>/regularizar-pago')
def regularizar_pago_repuesto(part_id):
    d=request.get_json(silent=True) or {}; c=db(); part=c.execute('''SELECT r.*,o.vehiculo_id,o.numero_ot,o.estado,p.nombre proveedor_nombre
        FROM repuestos_ot r JOIN ordenes_trabajo o ON o.id=r.orden_trabajo_id
        JOIN proveedores p ON p.id=r.proveedor_id WHERE r.id=?''',(part_id,)).fetchone()
    if not part: c.close(); return jsonify(error='Repuesto no encontrado'),404
    if part['metodo_pago']:
        c.close(); return jsonify(error='Este repuesto ya tiene método de pago contabilizado.'),409
    pago,error=validar_pago_repuesto(d)
    if error: c.close(); return jsonify(error=error),400
    metodo_pago,banco,referencia=pago; fecha=d.get('fecha') or part['fecha'] or datetime.now().strftime('%Y-%m-%d')
    provider=c.execute('SELECT * FROM proveedores WHERE id=?',(part['proveedor_id'],)).fetchone()
    c.execute('UPDATE repuestos_ot SET metodo_pago=?,banco_pago=?,referencia_pago=? WHERE id=?',(metodo_pago,banco,referencia,part_id))
    contabilizar_repuesto_ot(c,part_id,part,provider,float(part['total']),metodo_pago,banco,referencia,fecha,part['factura'],part['descripcion'])
    vehicle=c.execute('SELECT estado,ubicacion FROM vehiculos WHERE id=?',(part['vehiculo_id'],)).fetchone()
    add_movimiento(c,part['vehiculo_id'],fecha,'Regularización contable de repuesto',vehicle['estado'],vehicle['estado'],vehicle['ubicacion'],vehicle['ubicacion'],referencia=part['factura'],observaciones=f'Método de pago: {metodo_pago}. Partida de repuesto registrada.')
    c.commit(); c.close(); return jsonify(ok=True)

@app.delete('/api/repuestos-ot/<int:part_id>')
def eliminar_repuesto_ot(part_id):
    """Elimina una compra capturada en la OT y todos sus efectos derivados.

    Esta operación es deliberadamente explícita: la factura, su costo del
    vehículo, movimiento de caja, CxP/pagos y asientos relacionados deben
    desaparecer juntos para no dejar una contabilidad incompleta.
    """
    data=request.get_json(silent=True) or {}
    if data.get('confirmacion')!='ELIMINAR':
        return jsonify(error='Confirme la eliminación para continuar.'),400
    c=db(); part=c.execute('''SELECT r.*,o.vehiculo_id,o.numero_ot,o.taller,v.estado,v.ubicacion
        FROM repuestos_ot r JOIN ordenes_trabajo o ON o.id=r.orden_trabajo_id
        JOIN vehiculos v ON v.id=o.vehiculo_id WHERE r.id=?''',(part_id,)).fetchone()
    if not part:
        c.close(); return jsonify(error='Repuesto o insumo no encontrado.'),404
    try:
        # Primero se eliminan los pagos posteriores de una CxP, incluido su
        # caja y asiento, en caso de que la compra haya sido a crédito.
        payable=c.execute('SELECT id FROM cuentas_por_pagar_repuestos_ot WHERE repuesto_ot_id=?',(part_id,)).fetchone()
        if payable:
            payment_ids=[row['id'] for row in c.execute('SELECT id FROM pagos_cuentas_por_pagar_repuestos_ot WHERE cuenta_por_pagar_repuesto_id=?',(payable['id'],))]
            for payment_id in payment_ids:
                c.execute("DELETE FROM movimientos_caja WHERE referencia_tipo='pago_cxp_repuesto' AND referencia_id=?",(payment_id,))
                headers=c.execute("SELECT id FROM asientos_contables WHERE referencia_tipo='pago_cxp_repuesto' AND referencia_id=?",(payment_id,)).fetchall()
                for header in headers:
                    c.execute('DELETE FROM partidas WHERE asiento_id=?',(header['id'],)); c.execute('DELETE FROM asientos_contables WHERE id=?',(header['id'],))
            c.execute('DELETE FROM pagos_cuentas_por_pagar_repuestos_ot WHERE cuenta_por_pagar_repuesto_id=?',(payable['id'],))
            c.execute('DELETE FROM cuentas_por_pagar_repuestos_ot WHERE id=?',(payable['id'],))
        # Pago inmediato y asiento de la compra original.
        c.execute("DELETE FROM movimientos_caja WHERE referencia_tipo='pago_repuesto_ot' AND referencia_id=?",(part_id,))
        headers=c.execute("SELECT id FROM asientos_contables WHERE referencia_tipo='repuesto_ot' AND referencia_id=?",(part_id,)).fetchall()
        for header in headers:
            c.execute('DELETE FROM partidas WHERE asiento_id=?',(header['id'],)); c.execute('DELETE FROM asientos_contables WHERE id=?',(header['id'],))
        # La línea de costo fue creada junto con este repuesto. Se restringe
        # por OT, vehículo, factura y descripción para no borrar otra factura.
        c.execute('''DELETE FROM costos_vehiculo WHERE vehiculo_id=? AND documento=?
            AND concepto=? AND categoria='Repuestos / OT' AND observaciones=?''',
            (part['vehiculo_id'],part['factura'],part['descripcion'],f'{part["numero_ot"] or "OT"}: repuesto o insumo'))
        c.execute('''DELETE FROM movimientos_vehiculo WHERE vehiculo_id=? AND referencia=?
            AND tipo IN ('Repuesto cargado a OT','Regularización contable de repuesto')''',(part['vehiculo_id'],part['factura']))
        c.execute('DELETE FROM repuestos_ot WHERE id=?',(part_id,))
        c.commit()
    except Exception:
        c.rollback(); raise
    finally:
        c.close()
    return jsonify(ok=True,vin=part['vin'] if 'vin' in part.keys() else None,numero_ot=part['numero_ot'])

@app.post('/api/adquisiciones')
def post_adquisicion():
    d=request.get_json(silent=True)
    if not isinstance(d,dict): return jsonify(error='Se requiere un objeto JSON válido'),400
    try: vid=int(d.get('vehiculo_id')); pid=int(d.get('proveedor_id')); costo=float(d.get('costo_compra')); anticipo=float(d.get('anticipo') or 0)
    except (TypeError,ValueError): return jsonify(error='Vehículo, proveedor y costo de compra son obligatorios'),400
    if costo<=0: return jsonify(error='El costo de compra debe ser mayor que cero.'),400
    if anticipo<0 or anticipo>costo: return jsonify(error='Revise costo de compra y anticipo'),400
    c=db(); v=c.execute('SELECT * FROM vehiculos WHERE id=?',(vid,)).fetchone(); p=c.execute('SELECT * FROM proveedores WHERE id=? AND activo=1',(pid,)).fetchone()
    if not v or not p: c.close(); return jsonify(error='Vehículo o proveedor no encontrado'),404
    tipo_compra=(d.get('tipo_compra') or '').strip()
    metodo=d.get('metodo_pago'); condicion=p['condicion_pago']; banco=(d.get('banco') or '').strip()
    if tipo_compra!='Consignación' and ((condicion=='Contado' and metodo not in ('Efectivo','Transferencia')) or (condicion=='Crédito' and metodo!='Crédito')):
        c.close(); return jsonify(error='El método de pago no corresponde a la condición del proveedor'),400
    if metodo=='Transferencia' and anticipo>0 and not banco:
        c.close(); return jsonify(error='Seleccione el banco desde donde se realizó el anticipo'),400
    if tipo_compra not in ('Compra local','Importación','Cambio','Consignación'):
        c.close(); return jsonify(error='Seleccione un tipo de compra válido'),400
    placa_cambio=(d.get('placa_cambio') or '').strip().upper()
    if tipo_compra=='Cambio' and not placa_cambio:
        c.close(); return jsonify(error='Ingrese la placa del vehículo recibido en cambio'),400
    if tipo_compra in ('Compra local','Cambio') and not (v['placa'] or '').strip():
        c.close(); return jsonify(error='Ingrese la placa del vehículo antes de registrar una compra local o cambio'),400
    if tipo_compra=='Importación' and d.get('necesita_reparacion') in (True,1,'1'):
        c.close(); return jsonify(error='Un vehículo importado no puede generar una OT antes de nacionalizarse'),400
    if tipo_compra=='Consignación' and anticipo:
        c.close(); return jsonify(error='Una consignación no registra anticipos ni pagos hasta que se venda.'),400
    saldo=costo-anticipo
    fecha_compra=d.get('fecha') or datetime.now().strftime('%Y-%m-%d')
    try:
        fecha_llegada=fecha_llegada_importacion(fecha_compra,tipo_compra)
    except ValueError as error:
        c.close(); return jsonify(error=str(error)),400
    try:
        cur=c.execute('''INSERT INTO adquisiciones(vehiculo_id,proveedor_id,fecha,costo_compra,anticipo,metodo_pago,condicion_pago,dias_credito,saldo,observaciones,necesita_reparacion,tipo_compra,placa_cambio,banco,fecha_llegada_estimada,contabilizada) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(vid,pid,fecha_compra,costo,anticipo,metodo,condicion,p['dias_credito'],saldo,d.get('observaciones'),1 if d.get('necesita_reparacion') in (True,1,'1') else 0,tipo_compra,placa_cambio or None,banco or None,fecha_llegada,0 if tipo_compra=='Consignación' else 1))
    except sqlite3.IntegrityError: c.close(); return jsonify(error='Este vehículo ya tiene una compra registrada'),409
    purchase_type=tipo_compra.lower(); repair=d.get('necesita_reparacion') in (True,1,'1')
    c.execute('UPDATE vehiculos SET precio_compra=?,proveedor=?,fecha_adquisicion=?,tipo_compra=? WHERE id=?',(costo,p['nombre'],fecha_compra,tipo_compra,vid))
    if repair and ('local' in purchase_type or 'cambio' in purchase_type):
        crear_orden_trabajo(c,vid,cur.lastrowid,d.get('fecha') or datetime.now().strftime('%Y-%m-%d'),'Pendiente de asignar','Reparación general',0,d.get('detalle_reparacion') or 'Reparaciones requeridas después de la adquisición')
    new_status=recalcular_estado(c,vid,'Etapa calculada al registrar la adquisición')
    add_movimiento(c,vid,fecha_compra,'Registro de adquisición',v['estado'],new_status,v['ubicacion'],v['ubicacion'],referencia=metodo,observaciones=f'Compra {tipo_compra}: {p["nombre"]}. Anticipo {anticipo:.2f}; saldo pendiente {saldo:.2f}.' + (f' Llegada estimada: {fecha_llegada}.' if fecha_llegada else ''))
    if tipo_compra!='Consignación': contabilizar_adquisicion(c,cur.lastrowid)
    c.commit(); c.close(); return jsonify(ok=True),201

@app.put('/api/adquisiciones/<int:adquisicion_id>')
def put_compra(adquisicion_id):
    """Actualiza la compra y conserva la póliza ligada al mismo documento."""
    d=request.get_json(silent=True)
    if not isinstance(d,dict): return jsonify(error='Se requiere un objeto JSON válido'),400
    try:
        pid=int(d.get('proveedor_id')); costo=float(d.get('costo_compra')); anticipo=float(d.get('anticipo') or 0)
    except (TypeError,ValueError):
        return jsonify(error='Proveedor y costo de compra son obligatorios'),400
    if costo<=0: return jsonify(error='El costo de compra debe ser mayor que cero.'),400
    if anticipo<0 or anticipo>costo: return jsonify(error='Revise costo de compra y anticipo'),400
    tipo_compra=(d.get('tipo_compra') or '').strip()
    if tipo_compra not in ('Compra local','Importación','Cambio','Consignación'):
        return jsonify(error='Seleccione un tipo de compra válido'),400
    c=db()
    purchase=c.execute('SELECT * FROM adquisiciones WHERE id=?',(adquisicion_id,)).fetchone()
    provider=c.execute('SELECT * FROM proveedores WHERE id=? AND activo=1',(pid,)).fetchone()
    vehicle=c.execute('SELECT * FROM vehiculos WHERE id=?',(purchase['vehiculo_id'],)).fetchone() if purchase else None
    if not purchase: c.close(); return jsonify(error='Compra no encontrada'),404
    if not provider or not vehicle: c.close(); return jsonify(error='Proveedor o vehículo no encontrado'),404
    metodo=(d.get('metodo_pago') or '').strip(); condicion=provider['condicion_pago']; banco=(d.get('banco') or '').strip()
    if (condicion=='Contado' and metodo not in ('Efectivo','Transferencia')) or (condicion=='Crédito' and metodo!='Crédito'):
        c.close(); return jsonify(error='El método de pago no corresponde a la condición del proveedor'),400
    if metodo=='Transferencia' and anticipo>0 and not banco:
        c.close(); return jsonify(error='Seleccione el banco desde donde se realizó el anticipo'),400
    placa_cambio=(d.get('placa_cambio') or '').strip().upper()
    if tipo_compra=='Cambio' and not placa_cambio:
        c.close(); return jsonify(error='Ingrese la placa del vehículo recibido en cambio'),400
    if tipo_compra in ('Compra local','Cambio') and not (vehicle['placa'] or '').strip():
        c.close(); return jsonify(error='Ingrese la placa del vehículo antes de registrar una compra local o cambio'),400
    repair=d.get('necesita_reparacion') in (True,1,'1')
    if tipo_compra=='Importación' and repair:
        c.close(); return jsonify(error='Un vehículo importado no puede generar una OT antes de nacionalizarse'),400
    fecha=d.get('fecha') or purchase['fecha']; saldo=round(costo-anticipo,2)
    try:
        fecha_llegada=fecha_llegada_importacion(fecha,tipo_compra)
    except ValueError as error:
        c.close(); return jsonify(error=str(error)),400
    historical=(purchase['observaciones'] or '').startswith('Migración histórica.')
    observaciones=d.get('observaciones')
    # Las migraciones usan este prefijo para no generar CxP auxiliares al
    # reiniciar la aplicación; una edición no debe quitar esa protección.
    if historical and not str(observaciones or '').startswith('Migración histórica.'):
        observaciones=purchase['observaciones']
    try:
        c.execute('BEGIN')
        c.execute('''UPDATE adquisiciones SET proveedor_id=?,fecha=?,costo_compra=?,anticipo=?,metodo_pago=?,
            condicion_pago=?,dias_credito=?,saldo=?,observaciones=?,necesita_reparacion=?,tipo_compra=?,placa_cambio=?,banco=?,fecha_llegada_estimada=?
            WHERE id=?''',(pid,fecha,costo,anticipo,metodo,condicion,provider['dias_credito'],saldo,
            observaciones,1 if repair else 0,tipo_compra,placa_cambio or None,banco or None,fecha_llegada,adquisicion_id))
        c.execute('''UPDATE vehiculos SET precio_compra=?,proveedor=?,fecha_adquisicion=?,tipo_compra=? WHERE id=?''',
            (costo,provider['nombre'],fecha,tipo_compra,vehicle['id']))
        if historical:
            headers=c.execute('SELECT id FROM asientos_contables WHERE referencia_id=? AND referencia_tipo LIKE \'migracion_adquisicion%\'',(adquisicion_id,)).fetchall()
            for header in headers:
                c.execute('UPDATE asientos_contables SET fecha=?,descripcion=? WHERE id=?',
                    (fecha,f'Adquisición histórica actualizada · {vehicle["vin"]} · CxP proveedores',header['id']))
                c.execute('''UPDATE partidas SET fecha=?,descripcion=?,debe=CASE WHEN debe>0 THEN ? ELSE 0 END,
                    haber=CASE WHEN haber>0 THEN ? ELSE 0 END WHERE asiento_id=?''',
                    (fecha,f'Adquisición histórica actualizada · {vehicle["vin"]} · CxP proveedores',costo,costo,header['id']))
        else:
            # Se reconstruye la póliza automática y la CxP del mismo documento.
            headers=c.execute("SELECT id FROM asientos_contables WHERE referencia_tipo='adquisicion' AND referencia_id=?",(adquisicion_id,)).fetchall()
            for header in headers:
                c.execute('DELETE FROM partidas WHERE asiento_id=?',(header['id'],))
                c.execute('DELETE FROM asientos_contables WHERE id=?',(header['id'],))
            c.execute("DELETE FROM movimientos_caja WHERE referencia_tipo='adquisicion_anticipo' AND referencia_id=?",(adquisicion_id,))
            c.execute('DELETE FROM cuentas_por_pagar WHERE adquisicion_id=?',(adquisicion_id,))
            contabilizar_adquisicion(c,adquisicion_id)
        new_status=recalcular_estado(c,vehicle['id'],'Compra modificada')
        add_movimiento(c,vehicle['id'],fecha,'Modificación de compra',vehicle['estado'],new_status,vehicle['ubicacion'],vehicle['ubicacion'],
            referencia=metodo,observaciones=f'Compra actualizada: {provider["nombre"]}. Costo {costo:.2f}; anticipo {anticipo:.2f}.')
        c.commit()
    except Exception:
        c.rollback(); c.close(); raise
    c.close(); return jsonify(ok=True)

@app.get('/api/vehiculos/<int:vid>')
def get_vehicle(vid):
    c=db(); v=c.execute('SELECT * FROM vehiculos WHERE id=?',(vid,)).fetchone()
    if not v: c.close(); return jsonify(error='Vehículo no encontrado'),404
    costs=c.execute('SELECT * FROM costos_vehiculo WHERE vehiculo_id=? ORDER BY fecha DESC,id DESC',(vid,)).fetchall()
    movements=c.execute('SELECT * FROM movimientos_vehiculo WHERE vehiculo_id=? ORDER BY fecha DESC,id DESC',(vid,)).fetchall()
    purchase_record=c.execute('''SELECT a.*,p.nombre proveedor_nombre FROM adquisiciones a
        JOIN proveedores p ON p.id=a.proveedor_id WHERE a.vehiculo_id=?''',(vid,)).fetchone()
    payable_record=c.execute('SELECT monto_original,saldo,estado FROM cuentas_por_pagar WHERE adquisicion_id=?',
        (purchase_record['id'],)).fetchone() if purchase_record else None
    work_orders=c.execute('SELECT * FROM ordenes_trabajo WHERE vehiculo_id=? ORDER BY fecha DESC,id DESC',(vid,)).fetchall()
    devolucion=c.execute('''SELECT dc.*,cc.saldo saldo_cxc,cc.estado estado_cxc,
            vr.vin vin_reemplazo,vr.marca marca_reemplazo,vr.modelo modelo_reemplazo
        FROM devoluciones_cambio dc JOIN cuentas_por_cobrar cc ON cc.id=dc.cuenta_por_cobrar_id
        LEFT JOIN vehiculos vr ON vr.id=dc.vehiculo_reemplazo_id
        WHERE dc.vehiculo_devuelto_id=?''',(vid,)).fetchone()
    # Toda ficha de tipo Cambio elegible muestra el flujo de devolución. El
    # endpoint valida el pago original antes de registrar cualquier efecto
    # contable y, si es un registro histórico inequívoco, regulariza solo su
    # vínculo técnico con el pago existente.
    pago_cambio,_=buscar_vinculo_pago_cambio(c,v)
    acq=c.execute('SELECT * FROM costos_adquisicion WHERE vehiculo_id=?',(vid,)).fetchone()
    cost_total=c.execute('SELECT COALESCE(SUM(monto),0) FROM costos_vehiculo WHERE vehiculo_id=?',(vid,)).fetchone()[0]
    purchase=float(v['precio_compra'] or 0)
    acq_total=0; costeo={}
    if acq:
        adjustment=float(acq['ajuste_compra'] or 0)
        fob=purchase+adjustment
        cif=fob+float(acq['grua'] or 0)+float(acq['flete'] or 0)
        nationalization=float(acq['isv_pagado'] or 0)+float(acq['cl_std'] or 0)+float(acq['almacenaje'] or 0)+float(acq['gastos_aduaneros'] or 0)
        acq_total=adjustment+float(acq['grua'] or 0)+float(acq['flete'] or 0)+nationalization
        costeo={'costo_compra':purchase,'anticipo':float(purchase_record['anticipo'] or 0) if purchase_record else 0,'ajuste_compra':adjustment,'total_fob':fob,'grua':float(acq['grua'] or 0),'flete':float(acq['flete'] or 0),'valor_cif':cif,'isv_pagado':float(acq['isv_pagado'] or 0),'cl_std':float(acq['cl_std'] or 0),'almacenaje':float(acq['almacenaje'] or 0),'gastos_aduaneros':float(acq['gastos_aduaneros'] or 0),'costo_total':cif+nationalization,'estatus':acq['estatus'] or 'Pendiente','placa_nacionalizacion':acq['placa_nacionalizacion'] or ''}
    sale=c.execute('''SELECT ve.*,cl.nombre cliente_nombre FROM ventas ve LEFT JOIN clientes cl ON cl.id=ve.cliente_id WHERE ve.vehiculo_id=? ORDER BY ve.id DESC LIMIT 1''',(vid,)).fetchone()
    comm=[]
    if sale: comm=c.execute('SELECT * FROM comisiones WHERE venta_id=? ORDER BY id DESC',(sale['id'],)).fetchall()
    sale_price=float((sale['precio'] if sale else v['precio_venta']) or 0)
    commission_total=sum(float(x['monto'] or 0) for x in comm)
    base_cost=purchase+acq_total; real_cost=base_cost+float(cost_total or 0); gross=sale_price-real_cost; net=gross-commission_total
    compra=dict(purchase_record) if purchase_record else None
    if compra and payable_record:
        # El saldo de adquisición es el original; Kardex debe exponer el saldo
        # vivo de cartera, que puede cambiar por pagos y ajustes de compra.
        compra['saldo_cxp']=float(payable_record['saldo'] or 0)
        compra['monto_cxp']=float(payable_record['monto_original'] or 0)
        compra['estado_cxp']=payable_record['estado']
    out=dict(v); out['costos']= [dict(x) for x in costs]; out['movimientos']=[dict(x) for x in movements]; out['compra']=compra; out['ordenes_trabajo']=[dict(x) for x in work_orders]; out['devolucion_cambio']=dict(devolucion) if devolucion else None; out['puede_devolver_cambio']=bool(es_vehiculo_recibido_cambio(v) and not devolucion and v['estado'] not in ('Vendido','Devuelto')); out['vinculo_cambio_detectado']=bool(pago_cambio); out['adquisicion']=dict(acq) if acq else None; out['costeo']=costeo; out['costo_adquisicion']=base_cost; out['costo_adicional']=cost_total; out['costo_real']=real_cost; out['desglose_costos']=desglose_costo_consolidado(c,vid)
    out['venta']=dict(sale) if sale else None; out['comisiones']=[dict(x) for x in comm]
    out['precio_venta_calculado']=sale_price; out['comision_total']=commission_total; out['utilidad_bruta']=gross; out['utilidad_real']=net
    out['margen_real']= (net/sale_price*100) if sale_price else 0
    c.close(); return jsonify(out)

@app.post('/api/vehiculos')
def post_veh():
    d=request.get_json(silent=True)
    error=validate_vehiculo(d)
    if error: return jsonify(error=error),400
    vin=(d.get('vin') or '').strip().upper(); placa=(d.get('placa') or '').strip().upper()
    c=db()
    if not placa_disponible(c,placa):
        c.close(); return jsonify(error='La placa ya está registrada en otro vehículo'),409
    try:
        cur=c.execute('''INSERT INTO vehiculos
        (vin,lote,tipo_vehiculo,marca,modelo,version,color,anio,kilometraje,placa,estado,ubicacion,precio_compra,precio_venta,fecha_adquisicion,proveedor,tipo_compra,observaciones)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (vin,d.get('lote'),d.get('tipo_vehiculo'),d.get('marca'),d.get('modelo'),d.get('version'),d.get('color'),d.get('anio') or None,d.get('kilometraje') if d.get('kilometraje') not in (None,'') else None,placa,'En Tránsito',d.get('ubicacion'),0,d.get('precio_venta') or 0,None,None,None,d.get('observaciones')))
        asegurar_informacion_vehiculo(c,d)
        add_movimiento(c,cur.lastrowid,datetime.now().strftime('%Y-%m-%d'),'Ingreso a inventario',estado_nuevo='En Tránsito',ubicacion_nueva=d.get('ubicacion'),observaciones='Registro inicial; pendiente de adquisición')
        c.commit(); vid=cur.lastrowid
    except sqlite3.IntegrityError:
        c.rollback(); c.close(); return jsonify(error='El VIN ya existe'),409
    except Exception:
        c.rollback(); c.close(); raise
    c.close(); return jsonify(id=vid),201

@app.put('/api/vehiculos/<int:vid>')
def put_veh(vid):
    d=request.get_json(silent=True)
    if not isinstance(d, dict): return jsonify(error='Se requiere un objeto JSON válido'),400
    if 'vin' in d and not (d.get('vin') or '').strip(): return jsonify(error='VIN es obligatorio'),400
    for key, label in [('kilometraje', 'Millaje'), ('precio_compra', 'Costo de compra'), ('precio_venta', 'Precio de venta')]:
        if key in d and d[key] not in (None, ''):
            try:
                if float(d[key]) < 0: return jsonify(error=f'{label} no puede ser negativo'),400
            except (TypeError, ValueError): return jsonify(error=f'{label} debe ser numérico'),400
    if 'anio' in d and d['anio'] not in (None, ''):
        try:
            if not 1886 <= int(d['anio']) <= datetime.now().year + 1: return jsonify(error='Año fuera de rango válido'),400
        except (TypeError, ValueError): return jsonify(error='Año debe ser numérico'),400
    allowed=['vin','lote','tipo_vehiculo','marca','modelo','version','color','anio','kilometraje','placa','ubicacion','precio_venta','observaciones']
    sets=[]; args=[]
    for k in allowed:
        if k in d:
            val=d[k]
            if k=='vin': val=(val or '').strip().upper()
            if k=='placa': val=(val or '').strip().upper()
            sets.append(k+'=?'); args.append(val)
    if not sets:
        if any(k in d for k in ('estado','tipo_compra','precio_compra','fecha_adquisicion','proveedor')):
            return jsonify(ok=True, mensaje='Los datos de adquisición y la etapa se administran desde Adquisiciones')
        return jsonify(error='No hay cambios'),400
    args.append(vid); c=db()
    previous=c.execute('SELECT estado,ubicacion FROM vehiculos WHERE id=?',(vid,)).fetchone()
    if not previous: c.close(); return jsonify(error='Vehículo no encontrado'),404
    if 'placa' in d and not placa_disponible(c,d.get('placa'),vid):
        c.close(); return jsonify(error='La placa ya está registrada en otro vehículo'),409
    try:
        cur=c.execute('UPDATE vehiculos SET '+','.join(sets)+' WHERE id=?',args)
        catalog_data=c.execute('SELECT tipo_vehiculo,marca,modelo,version FROM vehiculos WHERE id=?',(vid,)).fetchone()
        asegurar_informacion_vehiculo(c,dict(catalog_data))
        c.commit()
    except sqlite3.IntegrityError:
        c.close(); return jsonify(error='El VIN ya existe'),409
    if 'ubicacion' in d:
        next_estado=previous['estado']
        next_ubicacion=d.get('ubicacion',previous['ubicacion'])
        if next_estado != previous['estado'] or next_ubicacion != previous['ubicacion']:
            add_movimiento(c,vid,datetime.now().strftime('%Y-%m-%d'),'Actualización de inventario',previous['estado'],next_estado,previous['ubicacion'],next_ubicacion,observaciones='Cambio realizado desde la ficha del vehículo')
            c.commit()
    c.close(); return jsonify(ok=True)

@app.get('/api/vehiculos/<int:vid>/movimientos')
def get_movimientos(vid):
    c=db(); items=c.execute('SELECT * FROM movimientos_vehiculo WHERE vehiculo_id=? ORDER BY fecha DESC,id DESC',(vid,)).fetchall(); c.close()
    return jsonify([dict(x) for x in items])

@app.post('/api/vehiculos/<int:vid>/movimientos')
def post_movimiento(vid):
    d=request.get_json(silent=True)
    if not isinstance(d,dict): return jsonify(error='Se requiere un objeto JSON válido'),400
    tipo=(d.get('tipo') or '').strip()
    if not tipo: return jsonify(error='Tipo de movimiento es obligatorio'),400
    c=db(); vehicle=c.execute('SELECT estado,ubicacion FROM vehiculos WHERE id=?',(vid,)).fetchone()
    if not vehicle: c.close(); return jsonify(error='Vehículo no encontrado'),404
    next_estado=vehicle['estado']
    next_ubicacion=d.get('ubicacion',vehicle['ubicacion'])
    c.execute('UPDATE vehiculos SET estado=?,ubicacion=? WHERE id=?',(next_estado,next_ubicacion,vid))
    add_movimiento(c,vid,d.get('fecha') or datetime.now().strftime('%Y-%m-%d'),tipo,vehicle['estado'],next_estado,vehicle['ubicacion'],next_ubicacion,d.get('referencia'),d.get('observaciones'))
    c.commit(); c.close(); return jsonify(ok=True),201

@app.get('/api/vehiculos/<int:vid>/costos')
def get_costos(vid):
    c=db(); items=c.execute('SELECT * FROM costos_vehiculo WHERE vehiculo_id=? ORDER BY fecha DESC,id DESC',(vid,)).fetchall(); total=c.execute('SELECT COALESCE(SUM(monto),0) FROM costos_vehiculo WHERE vehiculo_id=?',(vid,)).fetchone()[0]; c.close(); return jsonify(total=total,items=[dict(x) for x in items])

@app.get('/api/costeos')
def get_costeos():
    """Historial de vehículos con costeo de adquisición registrado."""
    c=db()
    rows=c.execute('''SELECT ca.*,v.vin,v.marca,v.modelo,v.anio,v.color,v.estado,v.placa
        FROM costos_adquisicion ca
        JOIN vehiculos v ON v.id=ca.vehiculo_id
        ORDER BY ca.fecha_actualizacion DESC,ca.id DESC''').fetchall()
    result=[]
    for row in rows:
        item=dict(row)
        item['costo_total']=costo_consolidado(c,item['vehiculo_id'])
        result.append(item)
    c.close(); return jsonify(result)

@app.post('/api/vehiculos/<int:vid>/costos')
def post_costo(vid):
    if nivel_actualizacion_costeo()=='nacionalizacion':
        return jsonify(error='Su acceso de Costeo solo permite actualizar Nacionalización y Placa.'),403
    d=request.get_json(force=True)
    if not d.get('concepto') or d.get('monto') is None: return jsonify(error='Concepto y monto son obligatorios'),400
    c=db();
    exists=c.execute('SELECT 1 FROM vehiculos WHERE id=?',(vid,)).fetchone()
    if not exists: c.close(); return jsonify(error='Vehículo no encontrado'),404
    fecha=d.get('fecha') or datetime.now().strftime('%Y-%m-%d')
    c.execute('''INSERT INTO costos_vehiculo(vehiculo_id,fecha,concepto,categoria,monto,proveedor,documento,observaciones) VALUES(?,?,?,?,?,?,?,?)''',(vid,fecha,d.get('concepto'),d.get('categoria'),d.get('monto',0),d.get('proveedor'),d.get('documento'),d.get('observaciones')))
    vehicle=c.execute('SELECT estado,ubicacion,precio_compra FROM vehiculos WHERE id=?',(vid,)).fetchone(); extra=c.execute('SELECT COALESCE(SUM(monto),0) FROM costos_vehiculo WHERE vehiculo_id=?',(vid,)).fetchone()[0]
    add_movimiento(c,vid,fecha,'Costo adicional registrado',vehicle['estado'],vehicle['estado'],vehicle['ubicacion'],vehicle['ubicacion'],referencia=d.get('documento'),observaciones=f"{d.get('concepto')}. Costo acumulado: {float(vehicle['precio_compra'] or 0)+float(extra or 0):.2f}")
    recalcular_estado(c,vid,'Costo adicional actualizado'); c.commit(); c.close(); return jsonify(ok=True),201

@app.delete('/api/vehiculos/<int:vid>/costos/<int:cid>')
def delete_costo(vid,cid):
    if nivel_actualizacion_costeo()=='nacionalizacion':
        return jsonify(error='Su acceso de Costeo solo permite actualizar Nacionalización y Placa.'),403
    c=db(); c.execute('DELETE FROM costos_vehiculo WHERE id=? AND vehiculo_id=?',(cid,vid)); c.commit(); c.close(); return jsonify(ok=True)

@app.put('/api/vehiculos/<int:vid>/adquisicion')
def put_adquisicion(vid):
    access=nivel_actualizacion_costeo()
    if access=='ninguno':
        return jsonify(error='No tiene permiso para actualizar el costeo.'),403
    d=request.get_json(force=True); c=db()
    vehicle=c.execute('SELECT id,estado,ubicacion,precio_compra FROM vehiculos WHERE id=?',(vid,)).fetchone()
    if not vehicle:
        c.close(); return jsonify(error='Vehículo no encontrado'),404
    previous=c.execute('SELECT * FROM costos_adquisicion WHERE vehiculo_id=?',(vid,)).fetchone()
    previous=dict(previous) if previous else {}
    def amount(key):
        try:
            saved=float(previous.get(key) or 0)
            proposed=float(d[key]) if key in d else saved
            # El costeo se completa por etapas. Para el acceso limitado cada
            # importe sigue abierto mientras su valor guardado sea cero; una
            # vez cargado, se vuelve histórico y ya no puede reemplazarse.
            if access=='nacionalizacion' and previous and saved!=0 and proposed!=saved:
                raise PermissionError(key)
            return proposed
        except (TypeError,ValueError): raise ValueError(key)
    try:
        ajuste=amount('ajuste_compra'); grua=amount('grua'); flete=amount('flete'); isv=amount('isv_pagado'); std=amount('cl_std'); almacenaje=amount('almacenaje'); gastos=amount('gastos_aduaneros')
    except PermissionError:
        c.close(); return jsonify(error='Solo puede registrar importes cuyo valor actual sea cero.'),403
    except ValueError:
        c.close(); return jsonify(error='Los valores de costeo deben ser numéricos'),400
    estatus=d.get('estatus',previous.get('estatus') or 'Pendiente')
    if estatus not in ('Pendiente','Nacionalizado'):
        c.close(); return jsonify(error='Estatus de nacionalización inválido'),400
    placa=(d.get('placa_nacionalizacion',previous.get('placa_nacionalizacion')) or '').strip().upper()
    acquisition=c.execute('SELECT * FROM adquisiciones WHERE vehiculo_id=?',(vid,)).fetchone()
    is_import=acquisition and acquisition['tipo_compra']=='Importación'
    if is_import and estatus=='Nacionalizado' and not placa:
        c.close(); return jsonify(error='Ingrese la placa del vehículo al nacionalizar la importación'),400
    if estatus=='Nacionalizado' and placa and not placa_disponible(c,placa,vid):
        c.close(); return jsonify(error='La placa ya está registrada en otro vehículo'),409
    fob=float(vehicle['precio_compra'] or 0)+ajuste; cif=fob+grua+flete; total=cif+isv+std+almacenaje+gastos
    try:
        if acquisition:
            contabilizar_ajuste_compra(c,acquisition,vehicle,ajuste,previous.get('ajuste_cxp_contabilizado') or 0,
                datetime.now().strftime('%Y-%m-%d'))
    except ValueError as error:
        c.rollback(); c.close(); return jsonify(error=str(error)),400
    c.execute('''INSERT INTO costos_adquisicion(vehiculo_id,costo_exw,grua,flete,costo_estimado,ajuste_cif,isv_pagado,cl_std,almacenaje,gastos_aduaneros,estatus,ajuste_compra,placa_nacionalizacion,fecha_actualizacion)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                 ON CONFLICT(vehiculo_id) DO UPDATE SET costo_exw=excluded.costo_exw,grua=excluded.grua,flete=excluded.flete,costo_estimado=excluded.costo_estimado,ajuste_cif=excluded.ajuste_cif,isv_pagado=excluded.isv_pagado,cl_std=excluded.cl_std,almacenaje=excluded.almacenaje,gastos_aduaneros=excluded.gastos_aduaneros,estatus=excluded.estatus,ajuste_compra=excluded.ajuste_compra,placa_nacionalizacion=excluded.placa_nacionalizacion,fecha_actualizacion=CURRENT_TIMESTAMP''',(vid,fob,grua,flete,cif,0,isv,std,almacenaje,gastos,estatus,ajuste,placa or None))
    c.execute('UPDATE costos_adquisicion SET ajuste_cxp_contabilizado=? WHERE vehiculo_id=?',(ajuste,vid))
    if estatus=='Nacionalizado' and placa:
        c.execute('UPDATE vehiculos SET placa=? WHERE id=?',(placa,vid))
    movimiento='Nacionalización actualizada' if estatus=='Nacionalizado' else 'Costeo actualizado'
    total_consolidado=costo_consolidado(c,vid)
    nota_placa=f' Placa actualizada: {placa}.' if estatus=='Nacionalizado' and placa else ''
    add_movimiento(c,vid,datetime.now().strftime('%Y-%m-%d'),movimiento,vehicle['estado'],vehicle['estado'],vehicle['ubicacion'],vehicle['ubicacion'],referencia=estatus,observaciones=f'Costo consolidado: {total_consolidado:.2f}.{nota_placa}')
    estado=recalcular_estado(c,vid,'Nacionalización confirmada' if estatus=='Nacionalizado' else 'Costeo y nacionalización actualizados'); c.commit(); c.close(); return jsonify(ok=True,costo_total=total_consolidado,estado=estado)

@app.get('/api/comisiones')
def get_comisiones():
    c=db(); r=c.execute('''SELECT co.*,v.fecha venta_fecha,ve.vin,ve.marca,ve.modelo FROM comisiones co JOIN ventas v ON v.id=co.venta_id JOIN vehiculos ve ON ve.id=v.vehiculo_id ORDER BY co.id DESC''').fetchall(); c.close(); return jsonify([dict(x) for x in r])

if __name__=='__main__':
    app.run(host='127.0.0.1',port=5000,debug=False)
