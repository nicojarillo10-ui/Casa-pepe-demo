# -*- coding: utf-8 -*-
"""
CASA PEPE · RESERVAS EN VIVO
App aparte, sincronizada con la web de Casa Pepe (casapepe_demo.py).

Lee y escribe en la misma base de datos SQLite que la web (tablas `reservas` y `reservas_meta`):
una reserva hecha en la web aparece aquí en menos de 1 segundo, y lo que se haga aquí
(reservar por teléfono, cancelar, atender) se refleja en la web al momento.

Importante: la sincronización es por la base de datos, así que esta app debe ejecutarse en la
misma máquina (o con acceso al mismo archivo) que la web.

Arranque, desde la carpeta donde está la web:
    pip install Flask tzdata
    python casapepe_reservas_live.py
Si la base de datos de la web está en otra ruta: CASAPEPE_DB_PATH=/ruta/casapepe_v7_enterprise.db
Abre http://localhost:5050 (puerto con CASAPEPE_LIVE_PORT).
Sin contraseña: usar solo en una red de confianza (el local).
Mismas variables que la web: CASAPEPE_RESERVA_CAPACIDAD (25), CASAPEPE_RESERVA_DURACION_MIN (60).
"""
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import closing
from urllib.parse import quote
from datetime import date, datetime, timedelta, timezone

from flask import (Flask, Response, abort, jsonify, redirect, render_template_string,
                   request, session, stream_with_context, url_for)

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo('Europe/Madrid')
except Exception:  # sin tzdata: hora de invierno peninsular como último recurso
    TZ = timezone(timedelta(hours=1))
    logging.getLogger(__name__).warning('Sin tzdata: instala "pip install tzdata" para el horario de Madrid correcto.')

NOMBRE_BD = 'casapepe_v7_enterprise.db'


def localizar_bd():
    """Encuentra la base de datos de la web. La web la guarda con ruta relativa (en la carpeta desde la que
    se lanza), así que se busca en las carpetas habituales y se elige la más recientemente usada."""
    explicita = os.environ.get('CASAPEPE_DB_PATH', '').strip()
    if explicita:
        return os.path.abspath(explicita), [], False
    aqui = os.path.dirname(os.path.abspath(__file__))
    bases = [os.getcwd(), aqui, os.path.dirname(aqui), os.path.expanduser('~')]
    vistos, hallados = set(), []
    for base in bases:
        for raiz, dirs, files in os.walk(base):
            if raiz[len(base):].count(os.sep) >= 2:
                dirs[:] = []
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('node_modules', '__pycache__', 'site-packages')]
            if NOMBRE_BD in files:
                ruta = os.path.realpath(os.path.join(raiz, NOMBRE_BD))
                if ruta not in vistos:
                    vistos.add(ruta)
                    hallados.append(ruta)
    if not hallados:
        return os.path.join(aqui, NOMBRE_BD), [], True
    def actividad(ruta):
        return max(os.path.getmtime(f) for f in (ruta, ruta + '-wal') if os.path.exists(f))

    def es_de_casapepe(ruta):
        """Solo vale una base que ya tenga las tablas de la web (reservas y pedidos); un archivo vacio o ajeno no."""
        try:
            with closing(sqlite3.connect('file:' + quote(ruta) + '?mode=ro', uri=True, timeout=3)) as c:
                tablas = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            return {'reservas', 'pedidos'} <= tablas
        except sqlite3.Error:
            return False
    validas = [r for r in hallados if es_de_casapepe(r)]
    if not validas:
        return os.path.join(aqui, NOMBRE_BD), [], True
    hallados = validas
    hallados.sort(key=actividad, reverse=True)
    return hallados[0], hallados[1:], False


DB_FILE, BD_OTRAS, BD_NUEVA = localizar_bd()
CAPACIDAD = int(os.environ.get('CASAPEPE_RESERVA_CAPACIDAD', '25'))
DURACION = int(os.environ.get('CASAPEPE_RESERVA_DURACION_MIN', '60'))
ZONAS = ('Salón Central', 'Barra', 'Terraza')
ESTADOS = ('Confirmada', 'Cancelada', 'Atendida', 'No presentada')
HORAS = [f'{m // 60:02d}:{m % 60:02d}' for lo, hi in ((750, 990), (1170, 1410)) for m in range(lo, hi + 1, 15)]

app = Flask(__name__)
app.config.update(
    SECRET_KEY=hashlib.sha256(('casapepe-live|' + (os.environ.get('CASAPEPE_RESERVAS_SECRET') or secrets.token_hex(16))).encode()).hexdigest(),
    SESSION_COOKIE_NAME='casapepe_live', SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.environ.get('CASAPEPE_LIVE_HTTPS') == '1',
    PERMANENT_SESSION_LIFETIME=timedelta(hours=14), MAX_CONTENT_LENGTH=64 * 1024,
)

# Despierta al instante a los clientes conectados cuando esta misma app escribe.
_cambio = threading.Condition()
_version = 0


def avisar_cambio():
    global _version
    with _cambio:
        _version += 1
        _cambio.notify_all()


def ahora():
    return datetime.now(TZ)


_ultima_busqueda = 0.0


def reenganchar_bd():
    """Si la app arranco antes que la web, sigue buscando su base de datos y se engancha sola cuando aparece."""
    global DB_FILE, BD_OTRAS, BD_NUEVA, _ultima_busqueda
    if not BD_NUEVA or os.environ.get('CASAPEPE_DB_PATH', '').strip() or time.time() - _ultima_busqueda < 10:
        return
    _ultima_busqueda = time.time()
    ruta, otras, nueva = localizar_bd()
    if not nueva:
        DB_FILE, BD_OTRAS, BD_NUEVA = ruta, otras, False
        app.logger.warning('Base de datos de la web localizada: %s', ruta)


def abrir_db():
    reenganchar_bd()
    db = sqlite3.connect(DB_FILE, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA busy_timeout=10000')
    return db


def preparar_db():
    """Crea las tablas solo si la web aún no lo ha hecho (idénticas a las de la web)."""
    with closing(abrir_db()) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute("""CREATE TABLE IF NOT EXISTS reservas (
            id TEXT PRIMARY KEY, nombre TEXT NOT NULL, telefono TEXT NOT NULL, fecha TEXT NOT NULL,
            hora TEXT NOT NULL, pax INTEGER NOT NULL, zona TEXT NOT NULL DEFAULT 'Salón Central', created_at TEXT NOT NULL)""")
        db.execute('CREATE TABLE IF NOT EXISTS reservas_meta (reserva_id TEXT PRIMARY KEY REFERENCES reservas(id), '
                   'token TEXT UNIQUE NOT NULL, submission TEXT UNIQUE, estado TEXT NOT NULL DEFAULT "Confirmada", '
                   'cancelada_at TEXT, privacidad_at TEXT)')
        db.execute('CREATE INDEX IF NOT EXISTS idx_reservas_fecha ON reservas(fecha)')
        db.execute('CREATE INDEX IF NOT EXISTS idx_reservas_fecha_hora ON reservas(fecha,hora)')
        db.commit()


# ------------------------------------------------------------------ lógica de aforo
def cargar_confirmadas(db, fecha, excluir=None):
    rows = db.execute('SELECT r.id,r.hora,r.pax FROM reservas r LEFT JOIN reservas_meta m ON m.reserva_id=r.id '
                      'WHERE r.fecha=? AND COALESCE(m.estado,"Confirmada")="Confirmada"', (fecha,)).fetchall()
    return [(datetime.fromisoformat(fecha + 'T' + r['hora']), r['pax']) for r in rows if r['id'] != excluir]


def cabe(db, fecha, hora, pax, excluir=None):
    """En ningún instante de la reserva se supera el aforo."""
    starts = cargar_confirmadas(db, fecha, excluir)
    target = datetime.fromisoformat(fecha + 'T' + hora)
    fin = target + timedelta(minutes=DURACION)
    for instante in [target] + [t for t, _ in starts if target <= t < fin]:
        if sum(p for t, p in starts if t <= instante < t + timedelta(minutes=DURACION)) + pax > CAPACIDAD:
            return False
    return True


def ocupacion(reservas_confirmadas, fecha):
    out = []
    for h in HORAS:
        t = datetime.fromisoformat(fecha + 'T' + h)
        n = sum(r['pax'] for r in reservas_confirmadas
                if (s := datetime.fromisoformat(fecha + 'T' + r['hora'])) <= t < s + timedelta(minutes=DURACION))
        out.append({'hora': h, 'ocupadas': n})
    return out


def fila_a_dict(r):
    return {'id': r['id'], 'nombre': r['nombre'], 'telefono': r['telefono'], 'fecha': r['fecha'], 'hora': r['hora'],
            'pax': r['pax'], 'zona': r['zona'], 'estado': r['estado'] or 'Confirmada', 'creada': r['created_at'],
            'origen': 'Local' if (r['submission'] or '').startswith('local-') else 'Web'}


def datos_dia(db, fecha):
    rows = db.execute('SELECT r.*,m.estado,m.submission FROM reservas r LEFT JOIN reservas_meta m ON m.reserva_id=r.id '
                      'WHERE r.fecha=? ORDER BY r.hora,r.created_at', (fecha,)).fetchall()
    reservas = [fila_a_dict(r) for r in rows]
    conf = [r for r in reservas if r['estado'] == 'Confirmada']
    pendientes = [r for r in conf if r['fecha'] != ahora().date().isoformat() or
                  datetime.fromisoformat(r['fecha'] + 'T' + r['hora']).replace(tzinfo=TZ) >= ahora()]
    total = db.execute('SELECT COUNT(*) FROM reservas').fetchone()[0]
    return {'bd': {'ruta': DB_FILE, 'otras': BD_OTRAS, 'nueva': BD_NUEVA, 'total': total},
            'fecha': fecha, 'hoy': ahora().date().isoformat(), 'ahora': ahora().strftime('%H:%M'),
            'capacidad': CAPACIDAD, 'duracion': DURACION, 'reservas': reservas, 'ocupacion': ocupacion(conf, fecha),
            'resumen': {'confirmadas': len(conf), 'comensales': sum(r['pax'] for r in conf),
                        'pendientes': len(pendientes), 'atendidas': sum(r['estado'] == 'Atendida' for r in reservas),
                        'canceladas': sum(r['estado'] == 'Cancelada' for r in reservas)}}


def huella(db):
    """Resumen barato de lo que cambia (altas y cambios de estado) para empujarlo en vivo."""
    desde = (ahora().date() - timedelta(days=1)).isoformat()
    rows = db.execute('SELECT r.id,r.fecha,r.hora,r.pax,r.nombre,r.telefono,r.zona,COALESCE(m.estado,"Confirmada") FROM reservas r '
                      'LEFT JOIN reservas_meta m ON m.reserva_id=r.id WHERE r.fecha>=? ORDER BY r.id', (desde,)).fetchall()
    return hashlib.sha1(json.dumps([tuple(x) for x in rows]).encode()).hexdigest()[:12]


# ------------------------------------------------------------------ seguridad
def csrf():
    if 'csrf' not in session:
        session['csrf'] = secrets.token_urlsafe(32)
    return session['csrf']


def requiere_csrf():
    if not hmac.compare_digest(request.headers.get('X-CSRF', ''), session.get('csrf', '')):
        abort(400)


CLAVE_ACCESO = os.environ.get('CASAPEPE_LIVE_PASSWORD', '')


@app.before_request
def acceso_opcional():
    """Por defecto la app no pide contrasena (uso en red local). Si se define CASAPEPE_LIVE_PASSWORD,
    el navegador la pide al entrar (recomendado al publicarla en internet)."""
    if not CLAVE_ACCESO or request.path == '/healthz':
        return None
    auth = request.authorization
    if auth and hmac.compare_digest((auth.password or '').encode(), CLAVE_ACCESO.encode()):
        return None
    return Response('Acceso restringido', 401, {'WWW-Authenticate': 'Basic realm="Casa Pepe - Reservas en vivo"'})


@app.after_request
def cabeceras(resp):
    resp.headers['Cache-Control'] = 'no-store'
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['X-Frame-Options'] = 'DENY'
    resp.headers['Referrer-Policy'] = 'no-referrer'
    resp.headers['Content-Security-Policy'] = ("default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                               "script-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'")
    return resp


@app.errorhandler(sqlite3.Error)
def _error_bd(exc):
    app.logger.exception('Error de base de datos')
    return jsonify(error='La base de datos esta ocupada o no responde. Se reintentara solo; vuelve a intentarlo en unos segundos.'), 503


# ------------------------------------------------------------------ rutas
@app.route('/')
def inicio():
    return render_template_string(PAGINA, base=request.script_root, csrf=csrf(), zonas=ZONAS, horas=HORAS, capacidad=CAPACIDAD)


def fecha_arg():
    f = request.args.get('fecha') or ahora().date().isoformat()
    try:
        date.fromisoformat(f)
    except ValueError:
        abort(400)
    return f


@app.route('/api/dia')
def api_dia():
    with closing(abrir_db()) as db:
        return jsonify(datos_dia(db, fecha_arg()))


@app.route('/api/reservas', methods=['POST'])
def api_crear():
    requiere_csrf()
    d = request.get_json(silent=True) or {}
    try:
        nombre = ' '.join(str(d.get('nombre', '')).split())
        telefono = re.sub(r'[\s().-]', '', str(d.get('telefono', '')))
        zona = d.get('zona', 'Salón Central')
        hora = str(d.get('hora', ''))
        pax = int(d.get('pax'))
        dia = date.fromisoformat(str(d.get('fecha', '')))
    except (ValueError, TypeError):
        return jsonify(error='Revisa fecha, hora y número de personas.'), 400
    if not 2 <= len(nombre) <= 60:
        return jsonify(error='El nombre debe tener entre 2 y 60 caracteres.'), 400
    if not re.fullmatch(r'\+?[0-9]{9,15}', telefono):
        return jsonify(error='Teléfono no válido (9 a 15 dígitos).'), 400
    if zona not in ZONAS:
        return jsonify(error='Zona no válida.'), 400
    if not re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d', hora):
        return jsonify(error='Hora no válida.'), 400
    if not 1 <= pax <= 40:
        return jsonify(error='Entre 1 y 40 personas.'), 400
    if not ahora().date() <= dia <= ahora().date() + timedelta(days=365):
        return jsonify(error='La fecha debe ser desde hoy hasta dentro de un año.'), 400
    forzar = d.get('forzar') is True
    fecha = dia.isoformat()
    codigo = 'R-' + secrets.token_hex(6).upper()
    with closing(abrir_db()) as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM reservas r JOIN reservas_meta m ON m.reserva_id=r.id WHERE r.telefono=? AND r.fecha=? '
                      'AND r.hora=? AND m.estado="Confirmada"', (telefono, fecha, hora)).fetchone():
            db.rollback()
            return jsonify(error='Ya hay una reserva confirmada con ese teléfono a esa hora.'), 409
        if not forzar and not cabe(db, fecha, hora, pax):
            db.rollback()
            return jsonify(error=f'No caben {pax} personas a las {hora}: aforo completo.', codigo='aforo'), 409
        stamp = ahora().isoformat()
        db.execute('INSERT INTO reservas (id,nombre,telefono,fecha,hora,pax,zona,created_at) VALUES (?,?,?,?,?,?,?,?)',
                   (codigo, nombre, telefono, fecha, hora, pax, zona, stamp))
        db.execute('INSERT INTO reservas_meta (reserva_id,token,submission,privacidad_at) VALUES (?,?,?,?)',
                   (codigo, secrets.token_urlsafe(32), 'local-' + secrets.token_urlsafe(18), stamp))
        db.commit()
    avisar_cambio()
    return jsonify(ok=True, id=codigo), 201


@app.route('/api/reservas/<rid>/estado', methods=['POST'])
def api_estado(rid):
    requiere_csrf()
    estado = (request.get_json(silent=True) or {}).get('estado')
    if estado not in ESTADOS or not re.fullmatch(r'R-[A-F0-9]{12}', rid):
        return jsonify(error='Petición no válida.'), 400
    with closing(abrir_db()) as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT r.fecha,r.hora,r.pax,COALESCE(m.estado,"Confirmada") AS estado, m.reserva_id AS tiene_meta '
                         'FROM reservas r LEFT JOIN reservas_meta m ON m.reserva_id=r.id WHERE r.id=?', (rid,)).fetchone()
        if not row:
            db.rollback()
            return jsonify(error='Reserva no encontrada.'), 404
        if not row['tiene_meta']:   # reserva antigua sin metadatos: se completa para poder gestionarla
            db.execute('INSERT INTO reservas_meta (reserva_id,token,submission) VALUES (?,?,?)',
                       (rid, secrets.token_urlsafe(32), 'local-' + secrets.token_urlsafe(18)))
        if estado in ('Atendida', 'No presentada'):
            cita = datetime.fromisoformat(row['fecha'] + 'T' + row['hora']).replace(tzinfo=TZ)
            if cita > ahora():
                db.rollback()
                return jsonify(error='Aún no ha llegado la hora de esta reserva.'), 409
        if estado == 'Confirmada' and row['estado'] != 'Confirmada' and not cabe(db, row['fecha'], row['hora'], row['pax'], excluir=rid):
            db.rollback()
            return jsonify(error='No se puede reactivar: ya no quedan plazas en esa franja.'), 409
        db.execute('UPDATE reservas_meta SET estado=?, cancelada_at=? WHERE reserva_id=?',
                   (estado, ahora().isoformat() if estado == 'Cancelada' else None, rid))
        db.commit()
    avisar_cambio()
    return jsonify(ok=True)


@app.route('/api/stream')
def api_stream():
    """Server-Sent Events: avisa al instante cuando cambian las reservas (web o esta app)."""

    def generar():
        """Cada 0,25 s se mira PRAGMA data_version (coste casi nulo): cambia cuando OTRO proceso, como la web,
        confirma una escritura. Solo entonces se calcula la huella y se avisa al navegador."""
        ultima, visto, dv_ant, ruta, conn = None, -1, None, None, None
        fin, ultimo_latido, tic = time.time() + 900, 0.0, 0
        yield 'retry: 1000\n\n'
        try:
            while time.time() < fin:
                try:
                    if conn is None or ruta != DB_FILE:
                        if conn is not None:
                            conn.close()
                        conn, ruta, dv_ant = abrir_db(), DB_FILE, None
                    dv = conn.execute('PRAGMA data_version').fetchone()[0]
                    tic += 1
                    if dv != dv_ant or _version != visto or tic % 20 == 0:   # el ultimo caso es red de seguridad (5 s)
                        dv_ant, visto = dv, _version
                        h = huella(conn)
                        if h != ultima:
                            ultima = h
                            yield f'event: cambio\ndata: {h}\n\n'
                except sqlite3.Error:
                    if conn is not None:
                        try:
                            conn.close()
                        except sqlite3.Error:
                            pass
                    conn = None
                if time.time() - ultimo_latido > 10:
                    ultimo_latido = time.time()
                    yield ': latido\n\n'
                with _cambio:   # las escrituras de esta app la despiertan al instante
                    if _version == visto:
                        _cambio.wait(0.25)
        finally:
            if conn is not None:
                conn.close()
    resp = Response(stream_with_context(generar()), mimetype='text/event-stream')
    resp.headers['X-Accel-Buffering'] = 'no'
    return resp


@app.route('/healthz')
def healthz():
    return 'ok'


# ------------------------------------------------------------------ interfaz
PAGINA = r'''<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><meta name="csrf" content="{{ csrf }}"><meta name="base" content="{{ base }}"><meta name="theme-color" content="#0c0a09"><title>Reservas en vivo · Casa Pepe</title>
<style>
:root{--bg:#0c0a09;--card:#1c1917;--line:#44403c;--gold:#d4af37;--gold2:#e5c15b;--txt:#f5f0e6;--mut:#a8a29e;--ok:#4ade80;--bad:#f87171;--warn:#fbbf24;--blue:#60a5fa}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.4 system-ui,-apple-system,sans-serif;padding-bottom:90px}
header{position:sticky;top:0;z-index:5;background:rgba(12,10,9,.94);backdrop-filter:blur(8px);border-bottom:1px solid #66501b;padding:10px 14px}
.top{display:flex;align-items:center;gap:10px;max-width:980px;margin:0 auto;flex-wrap:wrap}h1{font-size:18px;margin:0;color:var(--gold2);flex:1;min-width:150px}
.live{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--mut)}.dot{width:10px;height:10px;border-radius:50%;background:var(--bad)}.live.on .dot{background:var(--ok);box-shadow:0 0 0 0 rgba(74,222,128,.7);animation:p 2s infinite}
@keyframes p{70%{box-shadow:0 0 0 8px rgba(74,222,128,0)}100%{box-shadow:0 0 0 0 rgba(74,222,128,0)}}
button,.btn{font:inherit;cursor:pointer;border:1px solid var(--line);background:#292524;color:var(--txt);border-radius:10px;padding:8px 12px}button:hover{border-color:var(--gold)}
.gold{background:var(--gold);color:#111;border-color:var(--gold);font-weight:700}.ico{padding:8px 10px}
main{max-width:980px;margin:0 auto;padding:14px}.datebar{display:flex;gap:8px;align-items:center;margin-bottom:12px}.datebar input{background:#292524;color:var(--txt);border:1px solid var(--line);border-radius:10px;padding:8px;font:inherit;flex:1;min-width:0}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:12px}.stat{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:10px;text-align:center}.stat b{display:block;font-size:24px;color:var(--gold2)}.stat span{font-size:11px;color:var(--mut)}
.occ{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px;margin-bottom:12px}.occ h2{font-size:13px;margin:0 0 8px;color:var(--mut);font-weight:600;display:flex;justify-content:space-between}
.bars{display:flex;gap:2px;align-items:flex-end;height:64px}.bar{flex:1;height:100%;background:#292524;border-radius:3px 3px 0 0;position:relative;min-height:3px;cursor:pointer}.bar i{position:absolute;left:0;right:0;bottom:0;border-radius:3px 3px 0 0;background:var(--ok)}.bar.mid i{background:var(--warn)}.bar.full i{background:var(--bad)}.bar.now{outline:1px solid var(--gold2)}
.axis{display:flex;justify-content:space-between;font-size:10px;color:var(--mut);margin-top:4px}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px}.chip{border-radius:999px;padding:6px 12px;font-size:13px}.chip.act{background:var(--gold);color:#111;border-color:var(--gold);font-weight:700}
.grp{margin:16px 0 6px;font-size:13px;color:var(--gold2);font-weight:700;display:flex;gap:8px;align-items:center}.grp small{color:var(--mut);font-weight:400}.grp:after{content:"";flex:1;height:1px;background:var(--line)}
.res{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--blue);border-radius:14px;padding:12px;margin-bottom:8px;display:grid;grid-template-columns:auto 1fr;gap:6px 12px;transition:background .6s}
.res.Atendida{border-left-color:var(--ok);opacity:.75}.res.Cancelada{border-left-color:var(--bad);opacity:.55}.res.Cancelada .nm{text-decoration:line-through}.res.NP{border-left-color:var(--warn);opacity:.65}
.res.nuevo{background:#3b2f0d;animation:flash 2.5s}@keyframes flash{0%{background:#7a5f10}100%{background:#3b2f0d}}
.hr{font-size:22px;font-weight:700;grid-row:span 2;align-self:center;color:var(--gold2);min-width:62px}.nm{font-weight:600}.meta{color:var(--mut);font-size:13px;display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.pax{background:#292524;border-radius:8px;padding:1px 8px;color:var(--txt);font-weight:600}.tag{font-size:11px;border-radius:6px;padding:1px 7px;border:1px solid var(--line)}.tag.web{color:var(--blue);border-color:#1e3a5f}.tag.tel{color:var(--warn);border-color:#5f4a0d}
.acts{grid-column:1/-1;display:flex;gap:6px;flex-wrap:wrap}.acts button{padding:7px 11px;font-size:13px}.acts .a{border-color:#166534;color:var(--ok)}.acts .c{border-color:#7f1d1d;color:var(--bad)}.acts .n{border-color:#78350f;color:var(--warn)}
a{color:var(--gold2)}.vacio{text-align:center;color:var(--mut);padding:40px 10px;background:var(--card);border:1px dashed var(--line);border-radius:14px}
.fab{position:fixed;right:16px;bottom:16px;z-index:6;border-radius:999px;padding:14px 20px;font-size:16px;box-shadow:0 6px 24px rgba(0,0,0,.5)}
dialog{background:var(--card);color:var(--txt);border:1px solid #66501b;border-radius:20px;padding:22px;width:min(440px,94vw)}dialog::backdrop{background:rgba(0,0,0,.7)}
dialog h2{margin:0 0 14px;color:var(--gold2);font-size:19px}label{display:block;font-size:12px;color:var(--mut);margin:10px 0 4px}
dialog input,dialog select{width:100%;padding:11px;border-radius:10px;border:1px solid var(--line);background:#0c0a09;color:#fff;font:inherit}.row{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.err{color:#fca5a5;font-size:14px;margin-top:10px;min-height:1px}.force{display:flex;gap:8px;align-items:center;margin-top:10px;font-size:13px;color:var(--warn)}.force input{width:auto}
.foot{display:flex;gap:8px;margin-top:16px;justify-content:flex-end}
.aviso{background:#3a1d1d;border:1px solid #7f1d1d;color:#fecaca;border-radius:12px;padding:10px 12px;margin-bottom:12px;font-size:13px}.aviso b{color:#fff}.pie{margin-top:22px;color:var(--mut);font-size:11px;word-break:break-all;text-align:center}
#toast{position:fixed;left:50%;top:70px;transform:translateX(-50%) translateY(-20px);background:var(--gold);color:#111;padding:11px 18px;border-radius:12px;font-weight:700;opacity:0;pointer-events:none;transition:.3s;z-index:20;max-width:92vw;text-align:center}#toast.show{opacity:1;transform:translateX(-50%)}#toast.bad{background:var(--bad)}
@media(max-width:520px){.stats{grid-template-columns:repeat(2,1fr)}.hr{font-size:19px;min-width:54px}}
</style></head><body>
<header><div class="top"><h1>Casa Pepe · Reservas en vivo</h1>
<div class="live" id="live"><span class="dot"></span><span id="livet">Conectando…</span></div>
<button id="snd" title="Aviso sonoro al entrar una reserva">Sonido: no</button>
</div></header>
<main>
<div class="datebar"><button id="prev">Anterior</button><input type="date" id="fecha"><button id="next">Siguiente</button><button id="hoy">Hoy</button></div>
<div class="stats" id="stats"></div>
<div class="occ"><h2><span>Ocupación por franja</span><span id="occinfo">Aforo {{ capacidad }}</span></h2><div class="bars" id="bars"></div><div class="axis"><span>12:30</span><span>16:30</span><span>19:30</span><span>23:30</span></div></div>
<div id="aviso"></div>
<div class="chips" id="chips"></div>
<div id="lista"></div>
<div class="pie" id="pie"></div>
</main>
<button class="gold fab" id="nueva">Nueva reserva</button>
<div id="toast"></div>
<dialog id="dlg"><form id="f" method="dialog"><h2>Nueva reserva (teléfono / sala)</h2>
<label>Nombre</label><input name="nombre" required minlength="2" maxlength="60" autocomplete="off">
<label>Teléfono</label><input name="telefono" type="tel" required inputmode="tel" autocomplete="off">
<div class="row"><div><label>Fecha</label><input name="fecha" type="date" required></div><div><label>Hora</label><input name="hora" type="time" step="900" required></div></div>
<div class="row"><div><label>Personas</label><input name="pax" type="number" min="1" max="40" value="2" required></div><div><label>Zona</label><select name="zona">{% for z in zonas %}<option>{{ z }}</option>{% endfor %}</select></div></div>
<div class="force" id="forceb" hidden><input type="checkbox" id="forzar"><label for="forzar" style="margin:0;color:inherit;font-size:13px">Aforo completo: reservar igualmente</label></div>
<div class="err" id="err"></div>
<div class="foot"><button type="button" id="cancel">Cancelar</button><button class="gold" id="ok">Reservar</button></div></form></dialog>
<script>
const $=s=>document.querySelector(s),CSRF=document.querySelector('meta[name=csrf]').content,BASE=document.querySelector('meta[name=base]').content;
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let sync=null,fecha=null,datos=null,filtro='todas',sonido=localStorage.getItem('snd')==='1',conocidas=null,nuevas=new Set(),audio=null,pidiendo=false,pendiente=false;
const hoyLocal=()=>new Intl.DateTimeFormat('sv',{timeZone:'Europe/Madrid'}).format(new Date());
function sumar(f,n){const d=new Date(f+'T12:00:00');d.setDate(d.getDate()+n);return d.toISOString().slice(0,10)}
function toast(t,bad){const e=$('#toast');e.textContent=t;e.className='show'+(bad?' bad':'');clearTimeout(toast.t);toast.t=setTimeout(()=>e.className='',3200)}
function pitido(){if(!sonido)return;try{audio=audio||new (window.AudioContext||window.webkitAudioContext)();[880,1175].forEach((f,i)=>{const o=audio.createOscillator(),g=audio.createGain();o.frequency.value=f;o.connect(g);g.connect(audio.destination);const t=audio.currentTime+i*.16;g.gain.setValueAtTime(.15,t);g.gain.exponentialRampToValueAtTime(.001,t+.3);o.start(t);o.stop(t+.3)})}catch(e){}}
async function api(url,opt={}){const r=await fetch(BASE+url,{...opt,headers:{'Content-Type':'application/json','X-CSRF':CSRF,...(opt.headers||{})}});const j=await r.json().catch(()=>({}));if(!r.ok)throw Object.assign(new Error(j.error||'Error'),{j});return j}
async function cargar(){if(pidiendo){pendiente=true;return}pidiendo=true;try{const pedida=fecha;const d=await api('/api/dia?fecha='+pedida);if(pedida!==fecha)return;const prev=conocidas;datos=d;sync=new Date();
  const ids=new Set(d.reservas.map(r=>r.id));
  if(prev&&prev.fecha===d.fecha){const nu=d.reservas.filter(r=>!prev.ids.has(r.id));if(nu.length){nu.forEach(r=>nuevas.add(r.id));const r=nu[0];toast('Nueva reserva: '+r.nombre+' · '+r.pax+' pax · '+r.hora+(nu.length>1?' (+'+(nu.length-1)+')':''));pitido();setTimeout(()=>{nu.forEach(r=>nuevas.delete(r.id));pintar()},2600)}}
  conocidas={fecha:d.fecha,ids};pintar()}catch(e){if(e&&e.message)toast(e.message,1)}finally{pidiendo=false;if(pendiente){pendiente=false;cargar()}}}
function pintar(){const d=datos;if(!d)return;const s=d.resumen;
 $('#stats').innerHTML=[['Reservas',s.confirmadas],['Comensales',s.comensales],['Por llegar',s.pendientes],['Atendidas',s.atendidas]].map(x=>`<div class="stat"><b>${x[1]}</b><span>${x[0]}</span></div>`).join('');
 const bd=d.bd,av=[];
 if(bd.nueva)av.push('<b>No se ha encontrado la base de datos de la web.</b> Esta app esta usando una nueva y vacia, por eso no ve las reservas de la web. Indica la ruta real con CASAPEPE_DB_PATH o lanza la app desde la carpeta de la web.');
 if(bd.otras.length)av.push('<b>Hay mas de una base de datos de Casa Pepe en este equipo.</b> Esta app usa la mas reciente; si no es la de la web, indica la correcta con CASAPEPE_DB_PATH. Otras encontradas: '+bd.otras.map(esc).join(' ; '));
 $('#aviso').innerHTML=av.map(x=>'<div class="aviso">'+x+'</div>').join('');
 $('#pie').textContent='Base de datos: '+bd.ruta+' ('+bd.total+' reservas en total) · Actualizado '+sync.toLocaleTimeString('es-ES');
 const max=d.capacidad,esHoy=d.fecha===d.hoy;let pico=0;
 $('#bars').innerHTML=d.ocupacion.map(o=>{pico=Math.max(pico,o.ocupadas);const p=Math.min(100,o.ocupadas/max*100),c=p>=100?'full':p>=70?'mid':'';const act=esHoy&&o.hora<=d.ahora&&d.ocupacion[d.ocupacion.indexOf(o)+1]?.hora>d.ahora;
  return `<div class="bar ${c} ${act?'now':''}" title="${o.hora} · ${o.ocupadas}/${max} plazas" data-h="${o.hora}"><i style="height:${p}%"></i></div>`}).join('');
 $('#occinfo').textContent='Aforo '+max+' · pico '+pico+' ('+Math.round(pico/max*100)+'%)';
 const zonas=[...new Set(d.reservas.map(r=>r.zona))];const fs=[['todas','Todas'],['pend','Por llegar'],...zonas.map(z=>['z:'+z,z]),['canc','Canceladas']];
 $('#chips').innerHTML=fs.map(f=>`<button class="chip ${filtro===f[0]?'act':''}" data-f="${esc(f[0])}">${esc(f[1])}</button>`).join('');
 const pasada=r=>esHoy&&r.hora<d.ahora;
 let rs=d.reservas.filter(r=>filtro==='todas'?true:filtro==='pend'?(r.estado==='Confirmada'):filtro==='canc'?r.estado==='Cancelada':r.zona===filtro.slice(2));
 if(!rs.length){$('#lista').innerHTML='<div class="vacio">Sin reservas para este filtro y día.</div>';return}
 const g={};rs.forEach(r=>(g[r.hora]=g[r.hora]||[]).push(r));
 $('#lista').innerHTML=Object.keys(g).sort().map(h=>{const oc=(d.ocupacion.find(o=>o.hora===h)||{}).ocupadas;return `<div class="grp">${h}<small>${oc!=null?oc+'/'+d.capacidad+' plazas':''}</small></div>`+g[h].map(r=>{
  const cls=r.estado==='No presentada'?'NP':r.estado,tel=r.telefono.replace(/[^+\d]/g,'');
  const acc=r.estado==='Confirmada'?`<button class="a" data-id="${r.id}" data-e="Atendida">Atendida</button><button class="n" data-id="${r.id}" data-e="No presentada">No vino</button><button class="c" data-id="${r.id}" data-e="Cancelada">Cancelar</button>`:`<button data-id="${r.id}" data-e="Confirmada">Reactivar</button>`;
  return `<div class="res ${cls} ${nuevas.has(r.id)?'nuevo':''}"><div class="hr">${r.hora}</div><div><div class="nm">${esc(r.nombre)}</div><div class="meta"><span class="pax">${r.pax} pax</span><span>${esc(r.zona)}</span><a href="tel:${esc(tel)}">${esc(r.telefono)}</a><span class="tag ${r.origen==='Web'?'web':'tel'}">${r.origen}</span>${r.estado!=='Confirmada'?'<b>'+esc(r.estado)+'</b>':''}</div></div><div class="acts">${acc}</div></div>`}).join('')}).join('')}
document.addEventListener('click',async e=>{const b=e.target.closest('button');if(!b)return;
 if(b.dataset.e){if(b.dataset.e==='Cancelada'&&!confirm('¿Cancelar esta reserva y liberar las plazas?'))return;b.disabled=true;try{await api('/api/reservas/'+b.dataset.id+'/estado',{method:'POST',body:JSON.stringify({estado:b.dataset.e})});toast('Actualizado')}catch(x){toast(x.message,1)}cargar()}
 else if(b.dataset.f){filtro=b.dataset.f;pintar()}});
$('#bars').addEventListener('click',e=>{const b=e.target.closest('.bar');if(b){const g=[...document.querySelectorAll('.grp')].find(x=>x.firstChild.textContent===b.dataset.h);if(g)g.scrollIntoView({behavior:'smooth',block:'center'});else toast(b.title)}});
function ir(f){fecha=f;$('#fecha').value=f;conocidas=null;cargar()}
$('#fecha').onchange=e=>e.target.value&&ir(e.target.value);$('#prev').onclick=()=>ir(sumar(fecha,-1));$('#next').onclick=()=>ir(sumar(fecha,1));$('#hoy').onclick=()=>ir(hoyLocal());
const rotulo=()=>$('#snd').textContent=sonido?'Sonido: sí':'Sonido: no';rotulo();$('#snd').onclick=()=>{sonido=!sonido;localStorage.setItem('snd',sonido?'1':'0');rotulo();if(sonido)pitido()};
const dlg=$('#dlg'),f=$('#f');
$('#nueva').onclick=()=>{f.reset();f.fecha.value=fecha;f.fecha.min=hoyLocal();$('#err').textContent='';$('#forceb').hidden=true;$('#forzar').checked=false;dlg.showModal();f.nombre.focus()};
$('#cancel').onclick=()=>dlg.close();
f.onsubmit=async e=>{e.preventDefault();const v=Object.fromEntries(new FormData(f));v.pax=+v.pax;v.forzar=$('#forzar').checked;$('#ok').disabled=true;$('#err').textContent='';
 try{await api('/api/reservas',{method:'POST',body:JSON.stringify(v)});dlg.close();toast('Reserva guardada');if(v.fecha!==fecha)ir(v.fecha);else cargar()}
 catch(x){$('#err').textContent=x.message;if(x.j&&x.j.codigo==='aforo')$('#forceb').hidden=false}finally{$('#ok').disabled=false}};
function conectar(){const es=new EventSource(BASE+'/api/stream'),l=$('#live');
 es.onopen=()=>{l.classList.add('on');$('#livet').textContent='En vivo';cargar()};
 es.addEventListener('cambio',()=>cargar());
 es.onerror=()=>{l.classList.remove('on');$('#livet').textContent='Reconectando…'}}
ir(hoyLocal());conectar();
setInterval(cargar,10000);   // red de seguridad: aunque se cortase el canal en vivo, se refresca solo cada 10 s
document.addEventListener('visibilitychange',()=>{if(!document.hidden)cargar()});window.addEventListener('online',cargar);window.addEventListener('focus',cargar);
</script></body></html>'''


# Con gunicorn (Render) no se ejecuta el bloque __main__: las tablas se crean al importar el modulo.
if __name__ != '__main__':
    try:
        preparar_db()
    except sqlite3.Error:
        logging.getLogger(__name__).exception('No se pudo preparar la base de datos')


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    preparar_db()
    puerto = int(os.environ.get('CASAPEPE_LIVE_PORT', '5050'))
    print(f'Casa Pepe · Reservas en vivo: http://0.0.0.0:{puerto}  (BD: {DB_FILE})')
    if BD_NUEVA:
        print('AVISO: no se ha encontrado la base de datos de la web; se creara una nueva. Usa CASAPEPE_DB_PATH con la ruta real.')
    for otra in BD_OTRAS:
        print('AVISO: otra base de datos encontrada:', otra)
    app.run(host=os.environ.get('CASAPEPE_LIVE_HOST', '0.0.0.0'), port=puerto, threaded=True)
