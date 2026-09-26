from flask import Flask, request, jsonify, send_from_directory, send_file, session, redirect, url_for, render_template_string, g, has_request_context
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash
import sqlite3, os, shutil, uuid, hmac, io, json, tempfile, zipfile, re, csv
from datetime import datetime, timedelta
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
ESTADOS_VEHICULO=('En Tránsito','Nacionalizado','En Taller','DPV','Reservado','Vendido')

# Módulos que se pueden asignar a un perfil.  Los permisos se validan tanto en
# la interfaz como en cada solicitud al servidor.
ACCESS_MODULES=(
    ('dashboard','Dashboard'),('vehiculos','Vehículos'),('informacion_vehiculo','Información de vehículo'),
    ('proveedores','Proveedores'),('adquisiciones','Adquisiciones'),('taller','Taller'),('costeo','Costeo'),
    ('ventas','Ventas y cobros'),('vendedores','Vendedores'),('inventario','Consulta de inventario'),
    ('catalogo','Catálogo contable'),('centros_costo','Centros de costo'),('cartera','Cuentas por cobrar y pagar'),('contabilidad','Contabilidad'),
    ('caja_bancos','Caja y bancos'),('gastos','Registro de gastos'),('comisiones','Comisiones'),
    ('planificacion_financiera','Planificación financiera'),('auditoria','Bitácora de auditoría'),
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
    operational={'dashboard','vehiculos','informacion_vehiculo','proveedores','adquisiciones','taller','costeo','ventas','inventario','catalogo','centros_costo','vendedores','gastos','cartera'}
    sales={'dashboard','vehiculos','ventas','vendedores','inventario','caja_bancos','comisiones'}
    consultation={'dashboard','vehiculos','inventario','contabilidad','caja_bancos','ventas'}
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

def request_module(path):
    if path.startswith('/api/usuarios') or path.startswith('/api/perfiles'): return 'usuarios'
    if path.startswith('/api/auditoria'): return 'auditoria'
    if path.startswith('/api/dashboard'): return 'dashboard'
    if path.startswith('/api/planificacion-financiera'): return 'planificacion_financiera'
    if path.startswith('/api/contabilidad'): return 'contabilidad'
    if path.startswith('/api/cuentas-por-'): return 'cartera'
    if path.startswith('/api/caja'): return 'caja_bancos'
    if path.startswith('/api/gastos-ot'): return 'taller'
    if path.startswith('/api/gastos') or path.startswith('/api/conceptos-gasto'): return 'gastos'
    if path.startswith('/api/comisiones'): return 'comisiones'
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
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_pagos_cxp_referencia ON pagos_cuentas_por_pagar(referencia) WHERE referencia IS NOT NULL AND referencia<>''")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_cobros_cxc_referencia ON cobros_cuentas_por_cobrar(referencia) WHERE referencia IS NOT NULL AND referencia<>''")
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
    sincronizar_anticipos_ot_en_kardex(c)
    reconciliar_auxiliar_cxc(c)
    c.execute("UPDATE vehiculos SET estado='DPV' WHERE estado NOT IN ('En Tránsito','Nacionalizado','En Taller','DPV','Reservado','Vendido')")
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
        COALESCE(SUM(co.monto),0) cobrado
        FROM cuentas_por_cobrar cc
        LEFT JOIN cobros_cuentas_por_cobrar co ON co.cuenta_por_cobrar_id=cc.id
        GROUP BY cc.id''').fetchall()
    for row in rows:
        expected=max(round(float(row['monto_original'] or 0)-float(row['cobrado'] or 0),2),0)
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

def contabilizar_venta(c, venta_id):
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
        inventory_key=cuenta_inventario_por_estado(sale['vehiculo_estado'])
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

def sincronizar_contabilidad_historica(c):
    """Genera una vez los asientos y saldos para documentos existentes."""
    # Las adquisiciones históricas migradas ya tienen una póliza propia contra
    # CxP proveedores; no deben volver a contabilizarse como CxP del auxiliar.
    for row in c.execute("""SELECT id FROM adquisiciones
        WHERE COALESCE(observaciones,'') NOT LIKE 'Migración histórica.%'
          AND COALESCE(metodo_pago,'')<>'Cambio'
        ORDER BY id""").fetchall():
        contabilizar_adquisicion(c,row['id'])
    for row in c.execute('SELECT id FROM ventas ORDER BY id').fetchall():
        contabilizar_venta(c,row['id'])

# Gunicorn importa ``server:app`` y no ejecuta el bloque __main__.  Las
# migraciones deben correr durante la importación para que un despliegue nuevo
# use el mismo esquema que el entorno local. Todas son idempotentes.
init_db(sync_history=False)
init_access_control_schema()

def recalcular_estado(c, vehiculo_id, motivo='Actualización automática de etapa'):
    """Calcula las únicas etapas permitidas sin aceptar cambios manuales de estado."""
    vehicle=c.execute('SELECT estado,ubicacion FROM vehiculos WHERE id=?',(vehiculo_id,)).fetchone()
    if not vehicle: return None
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
                   permisos=user.get('permisos',{}))

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
        'consignacion':{'cantidad':sum(1 for vehicle in vehicles if vehicle.get('tipo_compra')=='Consignación' and vehicle.get('estado')!='Vendido'),
                        'costo':round(sum(float(vehicle.get('precio_compra') or 0) for vehicle in vehicles if vehicle.get('tipo_compra')=='Consignación' and vehicle.get('estado')!='Vendido'),2)}
    }
    inventory_by_type={}
    for vehicle in vehicles:
        if vehicle.get('estado')=='Vendido' or vehicle.get('tipo_compra')=='Consignación':
            continue
        key=(vehicle.get('tipo_vehiculo') or 'Sin tipo').strip() or 'Sin tipo'
        entry=inventory_by_type.setdefault(key,{'tipo_vehiculo':key,'cantidad':0,'costo':0.0})
        entry['cantidad']+=1
        entry['costo']+=costo_consolidado(c,vehicle['id'])

    current_month=datetime.now().strftime('%Y-%m')
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
        brand_entry=brands.setdefault(brand,{'marca':brand,'cantidad':0,'costo':0.0,'venta':0.0})
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
        'top_modelos':sorted(summarize(list(models.values())),key=lambda row:row['venta'],reverse=True)[:10],
        'ventas_por_marca':sorted(summarize(list(brands.values())),key=lambda row:row['venta'],reverse=True),
        'ventas_por_vendedor':sorted(summarize(list(sellers.values())),key=lambda row:row['venta'],reverse=True),
        # Se mantienen estas claves para consumidores existentes de la API.
        'total':len(vehicles),'disponibles':statuses['dpv']['cantidad'],'inventario':round(sum(costo_consolidado(c,row['id']) for row in vehicles if row.get('estado')!='Vendido' and row.get('tipo_compra')!='Consignación'),2),
        'comisiones_pendientes':0
    }
    c.close(); return jsonify(out)

@app.get('/api/catalogo')
def catalogo():
    c=db()
    where='' if request.args.get('todos')=='1' else ' WHERE activo=1'
    r=c.execute('SELECT * FROM cuentas_contables'+where+' ORDER BY CAST(codigo AS INTEGER),codigo').fetchall()
    c.close(); return jsonify([dict(x) for x in r])

@app.put('/api/catalogo/<int:cuenta_id>')
def actualizar_catalogo(cuenta_id):
    data=request.get_json(silent=True) or {}
    codigo=(data.get('codigo') or '').strip()
    cuenta=(data.get('cuenta') or '').strip()
    tipo=(data.get('tipo') or '').strip()
    naturaleza=(data.get('naturaleza') or '').strip()
    grupo=(data.get('grupo') or '').strip()
    tipos={'Activo','Contraactivo','Pasivo','Patrimonio','Ingreso','Costo','Gasto'}
    naturalezas={'Deudora','Acreedora'}
    if not codigo or not cuenta or not tipo or not naturaleza:
        return jsonify(error='Código, cuenta, tipo y naturaleza son obligatorios.'),400
    if tipo not in tipos or naturaleza not in naturalezas:
        return jsonify(error='Tipo o naturaleza contable no válidos.'),400
    try:
        acepta_movimiento=1 if bool(data.get('acepta_movimiento',True)) else 0
        requiere_activo=1 if bool(data.get('requiere_activo',False)) else 0
        requiere_centro_costo=1 if bool(data.get('requiere_centro_costo',False)) else 0
        activo=1 if bool(data.get('activo',True)) else 0
    except (TypeError,ValueError):
        return jsonify(error='Los indicadores de la cuenta no son válidos.'),400
    c=db()
    if not c.execute('SELECT id FROM cuentas_contables WHERE id=?',(cuenta_id,)).fetchone():
        c.close(); return jsonify(error='Cuenta contable no encontrada.'),404
    duplicate=c.execute('SELECT id FROM cuentas_contables WHERE codigo=? AND id<>?',(codigo,cuenta_id)).fetchone()
    if duplicate:
        c.close(); return jsonify(error='Ya existe otra cuenta con ese código.'),409
    try:
        c.execute('''UPDATE cuentas_contables SET codigo=?,cuenta=?,tipo=?,grupo=?,naturaleza=?,
            acepta_movimiento=?,requiere_activo=?,requiere_centro_costo=?,activo=? WHERE id=?''',
            (codigo,cuenta,tipo,grupo,naturaleza,acepta_movimiento,requiere_activo,requiere_centro_costo,activo,cuenta_id))
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
    c=db(); rows=c.execute('''SELECT cp.*,p.nombre proveedor_nombre,r.factura,o.numero_ot,v.vin,v.marca,v.modelo
        FROM cuentas_por_pagar_repuestos_ot cp JOIN proveedores p ON p.id=cp.proveedor_id
        JOIN repuestos_ot r ON r.id=cp.repuesto_ot_id JOIN ordenes_trabajo o ON o.id=r.orden_trabajo_id
        JOIN vehiculos v ON v.id=o.vehiculo_id ORDER BY cp.fecha DESC,cp.id DESC''').fetchall(); c.close(); return jsonify([dict(row) for row in rows])

@app.get('/api/cuentas-por-cobrar')
def cuentas_por_cobrar():
    c=db(); rows=c.execute('''SELECT cc.*,COALESCE(cl.nombre,fc.nombre,cc.financiera_nombre) tercero_nombre,
        CASE WHEN cc.cliente_id IS NOT NULL THEN 'Pagaré' ELSE 'Financiada' END tipo_cxc,
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
    area=request.args.get('area_id'); c=db(); sql='SELECT * FROM departamentos WHERE activo=1'; args=[]
    if area: sql+=' AND area_id=?'; args.append(area)
    r=c.execute(sql+' ORDER BY area_id,codigo',args).fetchall(); c.close(); return jsonify([dict(x) for x in r])

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
    rows=c.execute(sql+' ORDER BY ve.fecha DESC,ve.id DESC',args).fetchall(); result=[]
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

@app.get('/api/vehiculos/placa/<placa>')
def get_vehiculo_placa(placa):
    c=db(); v=c.execute('SELECT * FROM vehiculos WHERE UPPER(placa)=?',(placa.strip().upper(),)).fetchone()
    if not v:
        c.close(); return jsonify(error='Vehículo no encontrado con esa placa'),404
    data=dict(v)
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
    precio=lista-descuento
    if descuento>lista: c.close(); return jsonify(error='El descuento no puede exceder el precio lista'),400
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
    contabilizar_venta(c,cur.lastrowid)
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

@app.get('/api/adquisiciones')
def get_adquisiciones():
    c=db(); r=c.execute('''SELECT a.*,v.vin,v.marca,v.modelo,p.nombre proveedor_nombre FROM adquisiciones a JOIN vehiculos v ON v.id=a.vehiculo_id JOIN proveedores p ON p.id=a.proveedor_id ORDER BY a.fecha DESC,a.id DESC''').fetchall(); c.close(); return jsonify([dict(x) for x in r])

@app.get('/api/planificacion-financiera')
def get_planificacion_financiera():
    """Compromisos proyectados; es una vista y no genera CxP ni asientos."""
    c=db()
    transit=c.execute('''SELECT a.id,a.fecha_llegada_estimada fecha_compromiso,a.fecha fecha_origen,
            a.saldo pendiente,a.costo_compra monto_original,a.anticipo anticipos,
            a.metodo_pago,a.condicion_pago,v.vin,v.marca,v.modelo,p.nombre proveedor,
            'Adquisición' origen,'En Tránsito' etapa
        FROM adquisiciones a
        JOIN vehiculos v ON v.id=a.vehiculo_id
        JOIN proveedores p ON p.id=a.proveedor_id
        WHERE v.estado='En Tránsito' AND ROUND(COALESCE(a.saldo,0),2)>0
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
    c.close(); return jsonify(resumen=summary,compromisos=rows)

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
    work_orders=c.execute('SELECT * FROM ordenes_trabajo WHERE vehiculo_id=? ORDER BY fecha DESC,id DESC',(vid,)).fetchall()
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
    out=dict(v); out['costos']= [dict(x) for x in costs]; out['movimientos']=[dict(x) for x in movements]; out['compra']=dict(purchase_record) if purchase_record else None; out['ordenes_trabajo']=[dict(x) for x in work_orders]; out['adquisicion']=dict(acq) if acq else None; out['costeo']=costeo; out['costo_adquisicion']=base_cost; out['costo_adicional']=cost_total; out['costo_real']=real_cost; out['desglose_costos']=desglose_costo_consolidado(c,vid)
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

@app.post('/api/vehiculos/<int:vid>/costos')
def post_costo(vid):
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
    c=db(); c.execute('DELETE FROM costos_vehiculo WHERE id=? AND vehiculo_id=?',(cid,vid)); c.commit(); c.close(); return jsonify(ok=True)

@app.put('/api/vehiculos/<int:vid>/adquisicion')
def put_adquisicion(vid):
    d=request.get_json(force=True); c=db()
    vehicle=c.execute('SELECT estado,ubicacion,precio_compra FROM vehiculos WHERE id=?',(vid,)).fetchone()
    if not vehicle:
        c.close(); return jsonify(error='Vehículo no encontrado'),404
    previous=c.execute('SELECT * FROM costos_adquisicion WHERE vehiculo_id=?',(vid,)).fetchone()
    previous=dict(previous) if previous else {}
    def amount(key):
        try: return float(d[key]) if key in d else float(previous.get(key) or 0)
        except (TypeError,ValueError): raise ValueError(key)
    try:
        ajuste=amount('ajuste_compra'); grua=amount('grua'); flete=amount('flete'); isv=amount('isv_pagado'); std=amount('cl_std'); almacenaje=amount('almacenaje'); gastos=amount('gastos_aduaneros')
    except ValueError:
        c.close(); return jsonify(error='Los valores de costeo deben ser numéricos'),400
    estatus=d.get('estatus',previous.get('estatus') or 'Pendiente')
    if estatus not in ('Pendiente','Nacionalizado'):
        c.close(); return jsonify(error='Estatus de nacionalización inválido'),400
    placa=(d.get('placa_nacionalizacion',previous.get('placa_nacionalizacion')) or '').strip().upper()
    acquisition=c.execute('SELECT tipo_compra FROM adquisiciones WHERE vehiculo_id=?',(vid,)).fetchone()
    is_import=acquisition and acquisition['tipo_compra']=='Importación'
    if is_import and estatus=='Nacionalizado' and not placa:
        c.close(); return jsonify(error='Ingrese la placa del vehículo al nacionalizar la importación'),400
    if estatus=='Nacionalizado' and placa and not placa_disponible(c,placa,vid):
        c.close(); return jsonify(error='La placa ya está registrada en otro vehículo'),409
    fob=float(vehicle['precio_compra'] or 0)+ajuste; cif=fob+grua+flete; total=cif+isv+std+almacenaje+gastos
    c.execute('''INSERT INTO costos_adquisicion(vehiculo_id,costo_exw,grua,flete,costo_estimado,ajuste_cif,isv_pagado,cl_std,almacenaje,gastos_aduaneros,estatus,ajuste_compra,placa_nacionalizacion,fecha_actualizacion)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                 ON CONFLICT(vehiculo_id) DO UPDATE SET costo_exw=excluded.costo_exw,grua=excluded.grua,flete=excluded.flete,costo_estimado=excluded.costo_estimado,ajuste_cif=excluded.ajuste_cif,isv_pagado=excluded.isv_pagado,cl_std=excluded.cl_std,almacenaje=excluded.almacenaje,gastos_aduaneros=excluded.gastos_aduaneros,estatus=excluded.estatus,ajuste_compra=excluded.ajuste_compra,placa_nacionalizacion=excluded.placa_nacionalizacion,fecha_actualizacion=CURRENT_TIMESTAMP''',(vid,fob,grua,flete,cif,0,isv,std,almacenaje,gastos,estatus,ajuste,placa or None))
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
