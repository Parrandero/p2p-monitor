"""
P2P Monitor Binance — Vision Maker v2 + Detalle por Anunciante + UI v3
"""
import requests
import threading
import time
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from collections import deque
from flask import Flask, jsonify, Response, request
import psycopg2
from psycopg2 import pool as pg_pool
from psycopg2.extras import RealDictCursor
from contextlib import contextmanager

app = Flask(__name__)
SANTIAGO_TZ = ZoneInfo("America/Santiago")

# Version del codigo: se expone en /api/version y en el pie del dashboard, para
# confirmar de un vistazo QUE version esta corriendo en Railway tras un deploy.
VERSION       = "COL19"
VERSION_FECHA = "2026-07-20"

config = {
    "MONEDA":               "USDT",
    "FIAT":                 "CLP",
    "INTERVALO_MIN":        2,
    "FILTRO_MIN_USDT":      200,
    "FILTRO_MIN_ORD":       100,
    "FILTRO_MIN_TASA":      90.0,
    "ALERTA_SPREAD":        0.8,
    "SPREAD_MINIMO":        0.2,
    # Comisión Binance P2P maker CLP: 0.2% por pierna → 0.4% ida y vuelta (no verificado).
    # El descuento verificado es POR NIVEL, no fijo: Bronce -20% → 0.32% RT (donde se
    # arranca), Plata -30% → 0.28% (>6 BTC/mes), Oro -50% → 0.20% (>60 BTC/mes).
    # Al verificarse (Bronce): cambiar COMISION_BN a 0.0016 desde el panel/API.
    "COMISION_BN":          0.002,   # por pierna (0.2% maker CLP, no verificado)
    "COM_BINANCE_MAKER":    0.002,   # Binance maker CLP 0.2%
    "COM_BINANCE_TAKER":    0.001,   # Binance taker CLP 0.1%
    "COM_BYBIT_MAKER":      0.0,     # Bybit CLP: sin comision al publicar
    "COM_BYBIT_TAKER":      0.0,     # Bybit CLP: taker sin comision
    "COSTO_TRANSFER_USDT":  1.0,     # ~1 USDT fijo por transferir USDT entre exchanges (red TRC20)
    # Spread neto mínimo (después de comisiones) para considerar operable.
    "SPREAD_MIN_OPERATIVO": 0.35,
    "TOP_ANUNCIOS":         80,   # 4 páginas × 20 = 80 por lado (detalle/profundidad)
    "BANDA_DETALLE_PCT":    6,    # descarta del detalle anuncios con precio fuera de +-6% del tope real (anti-basura, mismo criterio Binance/Bybit)
    "ANALISIS_TOP":         20,   # cabecera del libro p/ spread y liquidez (mantiene baseline)
    "BANDA_PONDERADO_PCT":  1.0,  # % desde el lider para ponderar (descarta outliers en libros finos)
    # ── Fills v2 (volumen/velocidad por fills confirmados) ──
    "FILL_VENTANA_MIN":     15,    # min para que 'completadas' confirme una caida (ventana de pago Binance)
    "FILL_CAP_USDT":        10000, # tope de sanidad por evento (antes 3000, ciego; ahora solo protege contra ediciones gigantes)
    # Ticket p/ estimar fills enmascarados. OJO (COL17): para estimar un TOTAL
    # hay que usar la MEDIA (total = n x media), no la mediana. La distribucion
    # de tickets es de cola pesada (medido 20-jul: p50=206, media cruda=561,
    # media recortada al p95=408), asi que la mediana subestima el total y la
    # media cruda se dispara por unos pocos gigantes -> se usa MEDIA RECORTADA.
    # Estos valores son solo el arranque: recalibrar_tickets() los recalcula
    # solo cada dia con los fills observados reales.
    "FILL_TICKET_DEF":      408,   # media recortada p95 (binance), auto-calibrado a diario
    "FILL_TICKET_DEF_BYBIT": 300,  # idem bybit (mercado mas chico)
    "TICKET_AUTOCAL":       1,     # 1 = recalibrar solo a diario; 0 = fijo a mano
    "TICKET_MIN_MUESTRAS":  200,   # no recalibrar con menos fills observados que esto
    "TICKET_RANGO_MIN":     50,    # clamp de sanidad: un dia raro no puede envenenar el parametro
    "TICKET_RANGO_MAX":     2000,
    "CAPITAL_OPERATIVO":    600,   # USDT de capital de trabajo p/ proyecciones del asistente (editable en el panel; persiste en DB)
    # ── Proyeccion realista (COL12) ──
    # La proyeccion vieja (10% de captura, giros ilimitados) era un techo teorico:
    # daba 15.000 CLP/h y ~29 giros/h, imposible a mano. Estos parametros arman
    # el numero REALISTA: captura chica y tope de ordenes/hora atendibles.
    "CAPTURA_REALISTA_PCT": 2.0,   # % del flujo que captura un anunciante nuevo sin verificar
    "CAPTURA_VERIF_PCT":    3.0,   # % estimado al estar verificado (mejor ranking/confianza)
    "ORDENES_H_MAX":        8,     # ordenes/hora maximas atendibles a mano desde el telefono
    "COMISION_BN_VERIF":    0.0016, # por pierna al verificarse Bronce (-20% -> 0.32% ida y vuelta)
    # ── Comision TAKER: es un MONTO FIJO, no un porcentaje (COL18) ──
    # Medido en el historial real de ordenes (CSV Binance): 0.07 USDT constante
    # en ordenes de 21, 66, 157, 190 y 223 USDT. La maker en cambio es 0.19%
    # proporcional. Consecuencia: arriba de cierto tamano CRUZAR paga menos
    # comision que publicar, y la ventaja crece con el tamano de la orden.
    "COM_TAKER_FIJA_USDT":  0.07,  # comision taker por orden (monto fijo)
    # Comision maker NOMINAL de Binance: 0,20% por pierna. En las ordenes propias
    # se midio 0,19% efectivo, pero eso es por REDONDEO: Binance trunca la
    # comision a 2 decimales (74,16 x 0,2% = 0,148 -> cobra 0,14). El nominal es
    # 0,20% y es el que corresponde usar; el efectivo es apenas menor en ordenes
    # chicas. Los descuentos por nivel se aplican sobre este 0,20%.
    "COM_MAKER_PCT":        0.20,  # % por pierna (nominal Binance)
    # ── Mi posicion / carrera al verificado (COL14) ──
    "MI_NICKNAME":          "",    # nickname de Binance P2P: activa el seguimiento de MIS anuncios
    "MI_POSICION_OBJETIVO": 15,    # posicion objetivo en el libro (el plan farming dice top 10-20)
    # Ritmo MEDIDO del mercado en esa posicion (ordenes/hora por pierna).
    # Lo calcula recalibrar_ritmo() con fills observados; 0 = todavia sin medir.
    "RITMO_MEDIDO_ORD_H":   0.0,
    "RITMO_MEDIDO_RANGO":   "",
    # ── Umbrales del asistente (ahora configurables en caliente) ──
    "UMBRAL_ROT_LENTO":     0.7,   # ratio rotacion bajo el cual el mercado se considera lento
    "UMBRAL_ROT_DUAL":      1.0,   # ratio rotacion desde el cual habilita OPERAR DUAL pleno
    "UMBRAL_PRESION_SESGO": 10,    # |presion-50| minimo para habilitar SOLO VENTA/COMPRA
    "GAP_OBJETIVO_BRUTO":   1.30,  # % de gap BRUTO entre tus dos anuncios flujo. Separado del
                                   # semaforo: los duales rentables capturan 1.3-1.7% bruto
                                   # (mediana 1.50%) parados profundo en el libro, aunque el
                                   # spread instantaneo de la punta sea menor
}
config_lock = threading.Lock()

# Claves de config editables en caliente (POST /api/config). Estas mismas claves
# se PERSISTEN en la tabla config_persistente: sobreviven reinicios de Railway
# (antes un redeploy volvia todo a los defaults y el preset Farming se perdia solo).
CONFIG_TYPE_MAP = {
    "FILTRO_MIN_USDT":      float,
    "FILTRO_MIN_ORD":       int,
    "FILTRO_MIN_TASA":      float,
    "INTERVALO_MIN":        int,
    "COMISION_BN":          float,
    "SPREAD_MIN_OPERATIVO": float,
    "ALERTA_SPREAD":        float,
    "SPREAD_MINIMO":        float,
    "FILL_VENTANA_MIN":     float,
    "FILL_CAP_USDT":        float,
    "FILL_TICKET_DEF":      float,
    "FILL_TICKET_DEF_BYBIT": float,
    "CAPITAL_OPERATIVO":    float,
    "UMBRAL_ROT_LENTO":     float,
    "UMBRAL_ROT_DUAL":      float,
    "UMBRAL_PRESION_SESGO": float,
    "GAP_OBJETIVO_BRUTO":   float,
    "TICKET_AUTOCAL":       int,
    "TICKET_MIN_MUESTRAS":  int,
    "TICKET_RANGO_MIN":     float,
    "TICKET_RANGO_MAX":     float,
    "CAPTURA_REALISTA_PCT": float,
    "CAPTURA_VERIF_PCT":    float,
    "ORDENES_H_MAX":        int,
    "COMISION_BN_VERIF":    float,
    "MI_NICKNAME":          str,
    "MI_POSICION_OBJETIVO": int,
    "RITMO_MEDIDO_ORD_H":   float,
    "RITMO_MEDIDO_RANGO":   str,
    "COM_TAKER_FIJA_USDT":  float,
    "COM_MAKER_PCT":        float,
}

DATABASE_URL = os.environ.get("DATABASE_URL")
# Si APP_TOKEN esta seteado (variable de entorno en Railway), los POST sensibles
# (/api/config, /api/mantenimiento/vaciar) exigen el header X-App-Token con ese
# valor. Sin APP_TOKEN todo sigue abierto (retrocompatible). La URL publica de
# Railway la escanean bots: sin esto, cualquiera podia cambiar la estrategia.
APP_TOKEN    = os.environ.get("APP_TOKEN", "")
URL     = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
HEADERS = {"Content-Type": "application/json"}

ultimo_estado = {}
prev_detalle_raw = {}   # {tipo: {anunciante: (disponible, datetime)}}
ultimo_estado_bybit = {}
prev_detalle_raw_bybit = {}
data_lock = threading.Lock()

# ──────────────────────────────────────────────
#  CONNECTION POOL
# ──────────────────────────────────────────────
_pool = None

def init_pool():
    global _pool
    # OJO (fix COL19): los timestamps se GUARDAN en hora de Chile (naive), pero
    # el servidor de Railway corre en UTC. Sin esto, cualquier consulta que use
    # NOW() comparaba contra un reloj 4 horas adelantado y las ventanas salían
    # recortadas (un "ultimas 24h" traía en realidad 20h). Fijando la zona de la
    # sesion, NOW() devuelve hora de Chile y todo cuadra.
    _pool = pg_pool.ThreadedConnectionPool(
        2, 10, DATABASE_URL, options="-c timezone=America/Santiago")
    print("✅ Connection pool listo (2-10 conexiones, TZ America/Santiago)")

@contextmanager
def get_conn():
    conn = _pool.getconn()
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)

# ──────────────────────────────────────────────
#  BASE DE DATOS
# ──────────────────────────────────────────────
def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Tabla existente — sin tocar
            cur.execute("""
                CREATE TABLE IF NOT EXISTS snapshots (
                    id SERIAL PRIMARY KEY,
                    timestamp TIMESTAMP NOT NULL,
                    hora INTEGER,
                    dia TEXT,
                    mejor_vendedor_tab_compra NUMERIC,
                    peor_vendedor_tab_compra NUMERIC,
                    precio_pond_tab_compra NUMERIC,
                    lider_tab_compra TEXT,
                    mejor_comprador_tab_venta NUMERIC,
                    peor_comprador_tab_venta NUMERIC,
                    precio_pond_tab_venta NUMERIC,
                    lider_tab_venta TEXT,
                    spread_abs NUMERIC,
                    spread_pct NUMERIC,
                    spread_pond_abs NUMERIC,
                    spread_pond_pct NUMERIC,
                    liq_tab_compra NUMERIC,
                    liq_tab_venta NUMERIC,
                    n_tab_compra INTEGER,
                    n_tab_venta INTEGER,
                    precio_maker_vender NUMERIC,
                    precio_maker_comprar NUMERIC,
                    ganancia_neta_pct NUMERIC,
                    estado TEXT,
                    color TEXT
                )
            """)
            # Tabla de detalle por anunciante — sin tocar
            cur.execute("""
                CREATE TABLE IF NOT EXISTS snapshots_detalle (
                    id SERIAL PRIMARY KEY,
                    snapshot_timestamp TIMESTAMP NOT NULL,
                    hora INTEGER,
                    tipo TEXT,
                    posicion INTEGER,
                    anunciante TEXT,
                    precio NUMERIC,
                    disponible NUMERIC,
                    completadas INTEGER,
                    tasa_exito NUMERIC,
                    es_merchant BOOLEAN
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_detalle_timestamp
                ON snapshots_detalle(snapshot_timestamp)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_detalle_anunciante
                ON snapshots_detalle(anunciante, tipo)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_detalle_posicion
                ON snapshots_detalle(posicion, tipo, snapshot_timestamp)
            """)
            # ── Tablas Bybit (colector paralelo) ──
            cur.execute("""
                CREATE TABLE IF NOT EXISTS snapshots_bybit (
                    id SERIAL PRIMARY KEY,
                    timestamp TIMESTAMP NOT NULL,
                    hora INTEGER, dia TEXT,
                    mejor_vendedor_tab_compra NUMERIC, peor_vendedor_tab_compra NUMERIC,
                    precio_pond_tab_compra NUMERIC, lider_tab_compra TEXT,
                    mejor_comprador_tab_venta NUMERIC, peor_comprador_tab_venta NUMERIC,
                    precio_pond_tab_venta NUMERIC, lider_tab_venta TEXT,
                    spread_abs NUMERIC, spread_pct NUMERIC,
                    spread_pond_abs NUMERIC, spread_pond_pct NUMERIC,
                    liq_tab_compra NUMERIC, liq_tab_venta NUMERIC,
                    n_tab_compra INTEGER, n_tab_venta INTEGER,
                    precio_maker_vender NUMERIC, precio_maker_comprar NUMERIC,
                    ganancia_neta_pct NUMERIC, estado TEXT, color TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS snapshots_detalle_bybit (
                    id SERIAL PRIMARY KEY,
                    snapshot_timestamp TIMESTAMP NOT NULL,
                    hora INTEGER, tipo TEXT, posicion INTEGER,
                    anunciante TEXT, precio NUMERIC, disponible NUMERIC,
                    completadas INTEGER, tasa_exito NUMERIC, es_merchant BOOLEAN
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_byb_detalle_ts ON snapshots_detalle_bybit(snapshot_timestamp)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_byb_detalle_anun ON snapshots_detalle_bybit(anunciante, tipo)")
            # ── Fills v2: un registro por evento de fill confirmado/estimado ──
            cur.execute("""
                CREATE TABLE IF NOT EXISTS fills_estimados (
                    id SERIAL PRIMARY KEY,
                    ts TIMESTAMP NOT NULL,
                    exchange TEXT,
                    tipo TEXT,
                    anunciante TEXT,
                    monto NUMERIC,
                    ordenes INTEGER,
                    metodo TEXT,
                    precio NUMERIC
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_fills_ts ON fills_estimados(exchange, ts)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_fills_anun ON fills_estimados(anunciante, tipo, ts)")
            # ── Historial de decisiones del asistente (para ver ventanas por hora) ──
            cur.execute("""
                CREATE TABLE IF NOT EXISTS operativa_historial (
                    id SERIAL PRIMARY KEY,
                    ts TIMESTAMP NOT NULL,
                    hora INTEGER,
                    decision TEXT,
                    color TEXT,
                    spread_neto NUMERIC,
                    ratio NUMERIC,
                    presion NUMERIC
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_operativa_ts ON operativa_historial(ts)")
            # Contexto de la decision: con que minimo y gap estaba configurado el
            # asistente al decidir. Sin esto, la calibracion no puede distinguir
            # decisiones en modo Farming (min -0.2) de decisiones en modo normal.
            cur.execute("ALTER TABLE operativa_historial ADD COLUMN IF NOT EXISTS min_op NUMERIC")
            cur.execute("ALTER TABLE operativa_historial ADD COLUMN IF NOT EXISTS gap NUMERIC")
            # \u2500\u2500 Config persistente (sobrevive reinicios/redeploys) \u2500\u2500
            cur.execute("""
                CREATE TABLE IF NOT EXISTS config_persistente (
                    clave TEXT PRIMARY KEY,
                    valor TEXT,
                    actualizado TIMESTAMP
                )
            """)
            # \u2500\u2500 Agregados diarios: preservan la historia antes de la purga \u2500\u2500
            # ordenes_dia sale del contador OFICIAL de Binance, no es estimacion.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS agregados_anunciante_dia (
                    fecha DATE NOT NULL,
                    exchange TEXT NOT NULL,
                    anunciante TEXT NOT NULL,
                    tipo TEXT NOT NULL,
                    apariciones INTEGER,
                    pos_media NUMERIC,
                    pos_min INTEGER,
                    precio_medio NUMERIC,
                    disp_medio NUMERIC,
                    comp_min INTEGER,
                    comp_max INTEGER,
                    ordenes_dia INTEGER,
                    es_merchant BOOLEAN,
                    PRIMARY KEY (fecha, exchange, anunciante, tipo)
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_agr_fecha ON agregados_anunciante_dia(fecha)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_agr_anun ON agregados_anunciante_dia(anunciante, fecha)")
            # \u2500\u2500 MIS ORDENES REALES (verdad de terreno para calibrar) \u2500\u2500
            # Se importan del CSV de Binance. Es el unico caso donde sabemos que
            # paso de verdad: comparar contra lo que el monitor infirio desde
            # afuera es lo que permite medir (y corregir) el error del estimador.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS mis_ordenes_reales (
                    orden_id TEXT PRIMARY KEY,
                    ts TIMESTAMP NOT NULL,
                    lado TEXT,
                    usdt NUMERIC,
                    clp NUMERIC,
                    precio NUMERIC,
                    estado TEXT,
                    contraparte TEXT,
                    importado TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mis_ord_ts ON mis_ordenes_reales(ts)")
            # ── Perfil por hora del dia (COL19) ──
            # Cachea el calculo pesado (spread x flujo por hora) que alimenta al
            # Plan de hoy y al gap adaptativo. Se recalcula 1x/dia.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS perfil_hora (
                    hora INTEGER PRIMARY KEY,
                    spread_med NUMERIC,
                    flujo_ordenes NUMERIC,
                    indice NUMERIC,
                    gap_sugerido NUMERIC,
                    actualizado TIMESTAMP
                )
            """)
            # rol es CLAVE para calibrar: el monitor SOLO puede ver las ordenes
            # 'maker' (mis anuncios publicados en el libro). Las 'taker' (tomando
            # el anuncio de otro) son invisibles para el, asi que no deben contar
            # como fallos de deteccion. Sale del CSV: que columna de comision
            # viene llena (Tarifa de creador = maker / Comision de tomador = taker).
            cur.execute("ALTER TABLE mis_ordenes_reales ADD COLUMN IF NOT EXISTS rol TEXT")
            cur.execute("ALTER TABLE mis_ordenes_reales ADD COLUMN IF NOT EXISTS comision NUMERIC")
        conn.commit()
    print("\u2705 Base de datos lista (snapshots + snapshots_detalle)")

def cargar_config_db():
    """Aplica sobre config los valores guardados en config_persistente.
    Se llama una vez al boot: asi el preset activo (ej. Farming) sobrevive
    los reinicios de Railway en vez de volver silenciosamente a los defaults."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT clave, valor FROM config_persistente")
                rows = cur.fetchall()
    except Exception as e:
        print(f"[CONFIG carga] {e}")
        return
    aplicados = []
    with config_lock:
        for clave, valor in rows:
            cast = CONFIG_TYPE_MAP.get(clave)
            if cast is None:
                continue
            try:
                config[clave] = cast(valor)
                aplicados.append(f"{clave}={config[clave]}")
            except (ValueError, TypeError):
                pass
    if aplicados:
        print(f"[CONFIG] restaurada desde DB: {', '.join(aplicados)}")

def recalibrar_tickets():
    """AUTO-CALIBRACION del ticket usado para estimar fills enmascarados.

    Estadistica (COL17): el ticket se usa para estimar un TOTAL de volumen
    (total = n_ordenes x ticket), y para estimar un total el estimador correcto
    es la MEDIA, no la mediana. Pero la distribucion de tickets tiene cola muy
    pesada (medido: p50=206, media=561), asi que la media cruda la dominan unos
    pocos gigantes. Solucion estandar: MEDIA RECORTADA (se descarta el 5% mas
    alto) -> robusta y sin el sesgo hacia abajo de la mediana.

    Se calcula SOLO con fills 'directo' (caida de stock observada), nunca con
    los estimados, para no realimentar el propio error. Corre 1x/dia."""
    with config_lock:
        if not int(config.get("TICKET_AUTOCAL", 1)):
            return {}
        min_n = int(config.get("TICKET_MIN_MUESTRAS", 200))
        lo    = float(config.get("TICKET_RANGO_MIN", 50))
        hi    = float(config.get("TICKET_RANGO_MAX", 2000))
    nuevos = {}
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                for ex, clave in (("binance", "FILL_TICKET_DEF"),
                                  ("bybit",   "FILL_TICKET_DEF_BYBIT")):
                    cur.execute("""
                        WITH t AS (
                            SELECT monto / NULLIF(ordenes, 0) AS v
                            FROM fills_estimados
                            WHERE exchange = %(ex)s AND metodo = 'directo'
                              AND ts >= NOW() - INTERVAL '7 days' AND ordenes >= 1
                        ), lim AS (
                            SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY v) AS p95
                            FROM t WHERE v BETWEEN 5 AND 5000
                        )
                        SELECT AVG(v) FILTER (WHERE v <= (SELECT p95 FROM lim)) AS media_rec,
                               COUNT(*) AS n
                        FROM t WHERE v BETWEEN 5 AND 5000
                    """, {"ex": ex})
                    r = cur.fetchone()
                    if not r or not r["n"] or int(r["n"]) < min_n or r["media_rec"] is None:
                        print(f"[TICKET {ex}] muestras insuficientes ({r['n'] if r else 0}<{min_n}), se mantiene el valor actual")
                        continue
                    val = max(lo, min(hi, round(float(r["media_rec"]))))
                    with config_lock:
                        anterior = config.get(clave)
                        config[clave] = val
                    nuevos[clave] = val
                    print(f"[TICKET {ex}] auto-calibrado: {anterior} -> {val} (media recortada, n={r['n']})")
    except Exception as e:
        print(f"[TICKET recalibrar] {e}")
        return {}
    if nuevos:
        guardar_config_db(nuevos)
    return nuevos


def recalibrar_ritmo():
    """Mide cuantas ordenes/hora POR PIERNA da el mercado en la posicion
    objetivo, con fills observados (misma logica que /api/inteligencia/
    curva_llenado, pero cacheado: la consulta tarda ~2s y no puede correr en
    cada request del asistente).

    Se guarda en RITMO_MEDIDO_ORD_H y la proyeccion la usa junto al limite
    humano ORDENES_H_MAX: lo que podes hacer = min(lo que el mercado te da,
    lo que alcanzas a atender). Antes ese numero era una suposicion."""
    with config_lock:
        pos_obj = max(1, int(config.get("MI_POSICION_OBJETIVO", 15) or 15))
        intervalo = float(config.get("INTERVALO_MIN", 2))
    lo, hi = ((1, 3) if pos_obj <= 3 else (4, 7) if pos_obj <= 7 else
              (8, 12) if pos_obj <= 12 else (13, 20) if pos_obj <= 20 else
              (21, 30) if pos_obj <= 30 else (31, 50) if pos_obj <= 50 else (51, 80))
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SET LOCAL statement_timeout = '60s'")
                cur.execute("""
                    SELECT COUNT(*) AS obs FROM (
                        SELECT snapshot_timestamp, tipo, anunciante, MIN(posicion) AS pos
                        FROM snapshots_detalle
                        WHERE snapshot_timestamp >= NOW() - INTERVAL '7 days'
                        GROUP BY 1,2,3
                    ) d WHERE pos BETWEEN %(lo)s AND %(hi)s
                """, {"lo": lo, "hi": hi})
                obs = int((cur.fetchone() or {}).get("obs") or 0)
                cur.execute("""
                    SELECT COALESCE(SUM(f.ordenes), 0) AS ordenes
                    FROM fills_estimados f
                    JOIN (
                        SELECT snapshot_timestamp, tipo, anunciante, MIN(posicion) AS pos
                        FROM snapshots_detalle
                        WHERE snapshot_timestamp >= NOW() - INTERVAL '7 days'
                        GROUP BY 1,2,3
                    ) d ON d.anunciante = f.anunciante AND d.tipo = f.tipo
                       AND d.snapshot_timestamp = f.ts
                    WHERE f.exchange = 'binance' AND f.metodo = 'directo'
                      AND f.ts >= NOW() - INTERVAL '7 days'
                      AND d.pos BETWEEN %(lo)s AND %(hi)s
                """, {"lo": lo, "hi": hi})
                ordenes = int((cur.fetchone() or {}).get("ordenes") or 0)
    except Exception as e:
        print(f"[RITMO] {e}")
        return None
    horas = obs * intervalo / 60
    if horas <= 0 or ordenes < 20:
        print(f"[RITMO] muestras insuficientes (ordenes={ordenes}) — se mantiene el valor actual")
        return None
    tasa = round(ordenes / horas, 3)
    with config_lock:
        config["RITMO_MEDIDO_ORD_H"] = tasa
        config["RITMO_MEDIDO_RANGO"] = f"{lo:02d}-{hi:02d}"
    guardar_config_db({"RITMO_MEDIDO_ORD_H": tasa, "RITMO_MEDIDO_RANGO": f"{lo:02d}-{hi:02d}"})
    print(f"[RITMO] posicion {lo}-{hi}: {tasa} ordenes/hora por pierna (n={ordenes})")
    return tasa


def recalibrar_horarios():
    """Perfil de cada hora del dia: cuanto spread hay, cuanto flujo, que tan
    buena es para farmear, y que gap conviene usar. Corre 1x/dia y se cachea
    en perfil_hora (la consulta es pesada para hacerla en cada request).

    HALLAZGO que motivo esto (medido 20-jul, 73 dias): el flujo varia 90x entre
    horas (5 ordenes/hora a las 5h vs 449 a las 12h) mientras el spread solo
    varia 3x (0,41% a la tarde vs 1,26% en la madrugada). Como el flujo domina,
    la madrugada es una trampa: spread ancho pero mercado muerto. El indice
    spread x flujo es lo que de verdad ordena las horas.

    GAP SUGERIDO: sigue al spread mediano de esa hora. Un gap fijo queda
    demasiado ancho al mediodia (spread 0,44%) y demasiado angosto de
    madrugada (1,26%)."""
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SET LOCAL statement_timeout = '90s'")
                cur.execute("""
                    SELECT hora,
                           percentile_cont(0.5) WITHIN GROUP (ORDER BY spread_pond_pct) sp
                    FROM snapshots WHERE spread_pond_pct IS NOT NULL GROUP BY 1
                """)
                spread = {int(r["hora"]): float(r["sp"] or 0) for r in cur.fetchall()}
                cur.execute("""
                    SELECT EXTRACT(HOUR FROM ts)::int hora, SUM(ordenes) ord,
                           COUNT(DISTINCT ts::date) dias
                    FROM fills_estimados
                    WHERE exchange='binance' AND metodo='directo'
                      AND ts >= NOW() - INTERVAL '30 days'
                    GROUP BY 1
                """)
                flujo = {int(r["hora"]): float(r["ord"] or 0) / max(1, int(r["dias"] or 1))
                         for r in cur.fetchall()}
                if not spread or not flujo:
                    print("[HORARIOS] sin datos suficientes")
                    return None
                bruto = {h: spread.get(h, 0) * flujo.get(h, 0) for h in range(24)}
                tope = max(bruto.values()) or 1
                filas = []
                now = datetime.now(SANTIAGO_TZ).strftime("%Y-%m-%d %H:%M:%S")
                for h in range(24):
                    sp = round(spread.get(h, 0), 4)
                    filas.append((h, sp, round(flujo.get(h, 0), 1),
                                  round(bruto[h] / tope * 100, 1),
                                  round(sp, 2) if sp else None, now))
            with conn.cursor() as cur2:
                cur2.executemany("""
                    INSERT INTO perfil_hora
                        (hora, spread_med, flujo_ordenes, indice, gap_sugerido, actualizado)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (hora) DO UPDATE SET
                        spread_med=EXCLUDED.spread_med, flujo_ordenes=EXCLUDED.flujo_ordenes,
                        indice=EXCLUDED.indice, gap_sugerido=EXCLUDED.gap_sugerido,
                        actualizado=EXCLUDED.actualizado
                """, filas)
            conn.commit()
        mejor = max(filas, key=lambda f: f[3])
        print(f"[HORARIOS] recalculado · mejor hora {mejor[0]:02d}h (indice {mejor[3]})")
        return filas
    except Exception as e:
        print(f"[HORARIOS] {e}")
        return None


def guardar_agregados_dia(fecha=None):
    """Congela el resumen diario ANTES de que la purga recicle el detalle top-80.
    Sin esto perdemos la historia: snapshots_detalle solo guarda ~7 dias, asi
    que todo analisis de competidores quedaba limitado a esa ventana movil.

    Lo mas valioso que preserva: ordenes_dia = delta del contador OFICIAL de
    Binance (monthOrderCount) por anunciante. Eso NO es estimacion nuestra, es
    el numero real de ordenes que completo ese anunciante ese dia."""
    total = 0
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                for tabla, ex in (("snapshots_detalle", "binance"),
                                  ("snapshots_detalle_bybit", "bybit")):
                    cur.execute(f"SELECT to_regclass('public.{tabla}')")
                    if cur.fetchone()[0] is None:
                        continue
                    cur.execute(f"""
                        INSERT INTO agregados_anunciante_dia
                            (fecha, exchange, anunciante, tipo, apariciones, pos_media,
                             pos_min, precio_medio, disp_medio, comp_min, comp_max,
                             ordenes_dia, es_merchant)
                        SELECT snapshot_timestamp::date, %(ex)s, anunciante, tipo,
                               COUNT(*), ROUND(AVG(posicion)::numeric, 1),
                               MIN(posicion), ROUND(AVG(precio)::numeric, 2),
                               ROUND(AVG(disponible)::numeric, 1),
                               MIN(completadas), MAX(completadas),
                               GREATEST(MAX(completadas) - MIN(completadas), 0),
                               BOOL_OR(es_merchant)
                        FROM {tabla}
                        WHERE snapshot_timestamp::date = %(f)s
                          AND anunciante IS NOT NULL AND anunciante <> ''
                        GROUP BY 1,3,4
                        ON CONFLICT (fecha, exchange, anunciante, tipo) DO UPDATE SET
                            apariciones = EXCLUDED.apariciones, pos_media = EXCLUDED.pos_media,
                            pos_min = EXCLUDED.pos_min, precio_medio = EXCLUDED.precio_medio,
                            disp_medio = EXCLUDED.disp_medio, comp_min = EXCLUDED.comp_min,
                            comp_max = EXCLUDED.comp_max, ordenes_dia = EXCLUDED.ordenes_dia,
                            es_merchant = EXCLUDED.es_merchant
                    """, {"ex": ex, "f": fecha or (datetime.now(SANTIAGO_TZ).date() - timedelta(days=1))})
                    total += cur.rowcount
            conn.commit()
        if total:
            print(f"[AGREGADOS] {total:,} filas anunciante/dia congeladas")
    except Exception as e:
        print(f"[AGREGADOS] {e}")
    return total


def guardar_config_db(cambios):
    """Persiste los cambios de config aplicados via POST /api/config."""
    if not cambios:
        return
    try:
        now = datetime.now(SANTIAGO_TZ).strftime("%Y-%m-%d %H:%M:%S")
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.executemany("""
                    INSERT INTO config_persistente (clave, valor, actualizado)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (clave) DO UPDATE
                    SET valor = EXCLUDED.valor, actualizado = EXCLUDED.actualizado
                """, [(k, str(v), now) for k, v in cambios.items()])
            conn.commit()
    except Exception as e:
        print(f"[CONFIG guarda] {e}")

def guardar_snapshot(m, tabla="snapshots"):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                INSERT INTO {tabla} (
                    timestamp, hora, dia,
                    mejor_vendedor_tab_compra, peor_vendedor_tab_compra,
                    precio_pond_tab_compra, lider_tab_compra,
                    mejor_comprador_tab_venta, peor_comprador_tab_venta,
                    precio_pond_tab_venta, lider_tab_venta,
                    spread_abs, spread_pct,
                    spread_pond_abs, spread_pond_pct,
                    liq_tab_compra, liq_tab_venta,
                    n_tab_compra, n_tab_venta,
                    precio_maker_vender, precio_maker_comprar,
                    ganancia_neta_pct, estado, color
                ) VALUES (
                    %(timestamp)s, %(hora)s, %(dia)s,
                    %(mejor_vendedor_tab_compra)s, %(peor_vendedor_tab_compra)s,
                    %(precio_pond_tab_compra)s, %(lider_tab_compra)s,
                    %(mejor_comprador_tab_venta)s, %(peor_comprador_tab_venta)s,
                    %(precio_pond_tab_venta)s, %(lider_tab_venta)s,
                    %(spread_abs)s, %(spread_pct)s,
                    %(spread_pond_abs)s, %(spread_pond_pct)s,
                    %(liq_tab_compra)s, %(liq_tab_venta)s,
                    %(n_tab_compra)s, %(n_tab_venta)s,
                    %(precio_maker_vender)s, %(precio_maker_comprar)s,
                    %(ganancia_neta_pct)s, %(estado)s, %(color)s
                )
            """, m)
        conn.commit()

def _detalle_banda(raw, precio_de, band_pct):
    """Descarta anuncios con precio fuera de +-band% de la MEDIANA de los 10 mejores.
    El tope del libro (posiciones 1-10, ya ordenadas por precio) siempre es real, asi
    que sirve de referencia robusta. Elimina ordenes basura lejos del mercado (ej. en
    Bybit habia compras a 1112 y ventas a 741 que ensuciaban todo). Mismo criterio para
    Binance y Bybit -> datos compatibles entre si."""
    if len(raw) < 4:
        return raw
    top = [p for p in (precio_de(x) for x in raw[:10]) if p and p > 0]
    if len(top) < 3:
        return raw
    ref = sorted(top)[len(top) // 2]
    lo, hi = ref * (1 - band_pct / 100.0), ref * (1 + band_pct / 100.0)
    out = [x for x in raw if lo <= (precio_de(x) or 0) <= hi]
    return out if len(out) >= 3 else raw


def guardar_detalle(timestamp, hora, anuncios_raw_compra, anuncios_raw_venta):
    """Guarda los top 80 anunciantes de cada lado SIN filtros de mínimos"""
    with config_lock:
        top  = config["TOP_ANUNCIOS"]
        band = config["BANDA_DETALLE_PCT"]
    _pb = lambda item: float((item.get("adv") or {}).get("price", 0) or 0)
    anuncios_raw_compra = _detalle_banda(anuncios_raw_compra, _pb, band)
    anuncios_raw_venta  = _detalle_banda(anuncios_raw_venta,  _pb, band)
    rows = []
    for pos, item in enumerate(anuncios_raw_compra[:top], 1):
        adv   = item.get("adv", {})
        trade = item.get("advertiser", {})
        rows.append((
            timestamp, hora, "BUY", pos,
            trade.get("nickName", ""),
            float(adv.get("price", 0)),
            float(adv.get("tradableQuantity", 0)),
            int(trade.get("monthOrderCount", 0) or trade.get("tradeCount", 0) or 0),
            float(trade.get("monthFinishRate", trade.get("finishRate", 0)) or 0) * 100,
            bool(trade.get("userType") == "merchant"),
        ))
    for pos, item in enumerate(anuncios_raw_venta[:top], 1):
        adv   = item.get("adv", {})
        trade = item.get("advertiser", {})
        rows.append((
            timestamp, hora, "SELL", pos,
            trade.get("nickName", ""),
            float(adv.get("price", 0)),
            float(adv.get("tradableQuantity", 0)),
            int(trade.get("monthOrderCount", 0) or trade.get("tradeCount", 0) or 0),
            float(trade.get("monthFinishRate", trade.get("finishRate", 0)) or 0) * 100,
            bool(trade.get("userType") == "merchant"),
        ))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO snapshots_detalle
                (snapshot_timestamp, hora, tipo, posicion, anunciante, precio,
                 disponible, completadas, tasa_exito, es_merchant)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, rows)
        conn.commit()

def obtener_historial(limit=720):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM snapshots ORDER BY timestamp DESC LIMIT %s", (limit,))
            rows = cur.fetchall()
    return [dict(r) for r in reversed(rows)]

def obtener_precios_historico():
    """Toda la historia de precios ponderados, liviano (solo 3 campos).
    Para el gráfico interactivo de Lightweight Charts."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT timestamp,
                       precio_pond_tab_compra,
                       precio_pond_tab_venta
                FROM snapshots
                WHERE precio_pond_tab_compra IS NOT NULL
                ORDER BY timestamp ASC
            """)
            rows = cur.fetchall()
    return [dict(r) for r in rows]

def obtener_ultimo():
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM snapshots ORDER BY timestamp DESC LIMIT 1")
            row = cur.fetchone()
    return dict(row) if row else {}

def obtener_heatmap():
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT hora, dia,
                       ROUND(AVG(spread_pond_pct)::numeric, 2) as avg_spread,
                       COUNT(*) as muestras
                FROM snapshots
                GROUP BY hora, dia ORDER BY hora
            """)
            rows = cur.fetchall()
    return [dict(r) for r in rows]

def obtener_count():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM snapshots")
            return cur.fetchone()[0]

def obtener_velocidad_anunciante(anunciante, tipo, limit=50):
    """Retorna la velocidad de consumo de un anunciante específico"""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT snapshot_timestamp, disponible
                FROM snapshots_detalle
                WHERE anunciante = %s AND tipo = %s
                ORDER BY snapshot_timestamp DESC LIMIT %s
            """, (anunciante, tipo, limit))
            rows = cur.fetchall()
    return [dict(r) for r in reversed(rows)]

# ──────────────────────────────────────────────
#  COLECTOR  (sin cambios respecto al Railway actual)
# ──────────────────────────────────────────────
def obtener_anuncios(tipo):
    """Trae los top TOP_ANUNCIOS anuncios paginando el libro de Binance.
    Binance topa 'rows' en 20 por pagina -> hay que recorrer varias paginas."""
    with config_lock:
        c = dict(config)
    top        = c["TOP_ANUNCIOS"]
    POR_PAGINA = 20                       # limite duro del endpoint P2P de Binance
    n_paginas  = (top + POR_PAGINA - 1) // POR_PAGINA
    resultados = []
    for page in range(1, n_paginas + 1):
        payload = {
            "asset": c["MONEDA"], "fiat": c["FIAT"],
            "merchantCheck": False, "page": page,
            "publisherType": None, "rows": POR_PAGINA,
            "tradeType": tipo,
        }
        try:
            r = requests.post(URL, json=payload, headers=HEADERS, timeout=10)
            r.raise_for_status()
            data = r.json().get("data", []) or []
        except Exception as e:
            print(f"[ERROR obtener_anuncios {tipo} p{page}] {e}")
            break
        if not data:
            break                         # el libro no tiene mas anuncios
        resultados.extend(data)
        if len(data) < POR_PAGINA:
            break                         # ultima pagina real del libro
        if page < n_paginas:
            time.sleep(0.3)               # respetar rate-limit de Binance
    return resultados[:top]

def parsear_y_filtrar(anuncios, tipo):
    with config_lock:
        c = dict(config)
    resultado = []
    for item in anuncios:
        adv   = item.get("adv", {})
        trade = item.get("advertiser", {})
        disponible  = float(adv.get("tradableQuantity", 0))
        completadas = int(trade.get("monthOrderCount", 0) or trade.get("tradeCount", 0) or 0)
        if disponible  < c["FILTRO_MIN_USDT"]: continue
        if completadas < c["FILTRO_MIN_ORD"]:  continue
        tasa_exito = float(trade.get("monthFinishRate", trade.get("finishRate", 0)) or 0) * 100
        if tasa_exito < c["FILTRO_MIN_TASA"]:  continue
        resultado.append({
            "tipo":       tipo,
            "precio":     float(adv.get("price", 0)),
            "disponible": disponible,
            "anunciante": trade.get("nickName", ""),
        })
    return resultado

def precio_ponderado(anuncios):
    total = sum(a["disponible"] for a in anuncios)
    if total == 0: return 0
    return sum(a["precio"] * a["disponible"] for a in anuncios) / total

def analizar(tab_compra, tab_venta, comision_lado=None):
    if not tab_compra or not tab_venta:
        return None
    with config_lock:
        c = dict(config)
    # Sanity anti-glitch: descarta anuncios con precio absurdo (scam/mal-tipeado)
    # usando la MEDIANA como referencia robusta. Asi un solo anuncio basura no
    # puede quedar de lider ni contaminar el ponderado (evita los picos del grafico).
    def _sanos(ads):
        if len(ads) < 4:
            return ads
        ps = sorted(a["precio"] for a in ads)
        med = ps[len(ps) // 2]
        if med <= 0:
            return ads
        limpio = [a for a in ads if abs(a["precio"] - med) / med <= 0.04]
        return limpio if len(limpio) >= 3 else ads
    tab_compra = _sanos(tab_compra)
    tab_venta  = _sanos(tab_venta)
    lider_tc    = min(tab_compra, key=lambda x: x["precio"])
    lider_tv    = max(tab_venta,  key=lambda x: x["precio"])
    spread_abs = lider_tc["precio"] - lider_tv["precio"]
    spread_pct = round((spread_abs / lider_tv["precio"]) * 100, 4) if lider_tv["precio"] > 0 else 0
    # Banda anti-outliers: para ponderado/liquidez solo contamos anuncios cerca
    # del lider (descarta ballenas con precio disparatado en libros finos).
    banda = c["BANDA_PONDERADO_PCT"] / 100
    cab_tc = [a for a in tab_compra if a["precio"] <= lider_tc["precio"] * (1 + banda)] or tab_compra
    cab_tv = [a for a in tab_venta  if a["precio"] >= lider_tv["precio"] * (1 - banda)] or tab_venta
    mas_caro_tc = max(cab_tc, key=lambda x: x["precio"])
    menos_tv    = min(cab_tv, key=lambda x: x["precio"])
    pond_tc = round(precio_ponderado(cab_tc), 2)
    pond_tv = round(precio_ponderado(cab_tv), 2)
    spread_pond_abs = round(pond_tc - pond_tv, 2)
    spread_pond_pct = round((spread_pond_abs / pond_tv) * 100, 4) if pond_tv > 0 else 0
    liq_tc = sum(a["disponible"] for a in cab_tc)
    liq_tv = sum(a["disponible"] for a in cab_tv)
    precio_maker_vender  = round(lider_tc["precio"] - 0.01, 2)
    precio_maker_comprar = round(lider_tv["precio"] + 0.01, 2)
    com_lado = c["COMISION_BN"] if comision_lado is None else comision_lado
    comision_total_pct = com_lado * 2 * 100
    ganancia = round(spread_pond_pct - comision_total_pct, 4)
    brecha_ok = ganancia >= c["SPREAD_MIN_OPERATIVO"]
    if spread_pond_pct >= c["ALERTA_SPREAD"]:
        estado, color = "MUY APTO", "green"
    elif spread_pond_pct >= c["SPREAD_MINIMO"]:
        estado, color = "APTO", "yellow"
    elif spread_pond_pct >= 0:
        estado, color = "ESTRECHO", "orange"
    else:
        estado, color = "NO APTO", "red"
    now = datetime.now(SANTIAGO_TZ)
    return {
        "timestamp":                  now.strftime("%Y-%m-%d %H:%M:%S"),
        "hora":                       now.hour,
        "dia":                        now.strftime("%A"),
        "mejor_vendedor_tab_compra":  lider_tc["precio"],
        "peor_vendedor_tab_compra":   mas_caro_tc["precio"],
        "precio_pond_tab_compra":     pond_tc,
        "lider_tab_compra":           lider_tc["anunciante"],
        "mejor_comprador_tab_venta":  lider_tv["precio"],
        "peor_comprador_tab_venta":   menos_tv["precio"],
        "precio_pond_tab_venta":      pond_tv,
        "lider_tab_venta":            lider_tv["anunciante"],
        "spread_abs":                 round(spread_abs, 2),
        "spread_pct":                 spread_pct,
        "spread_pond_abs":            spread_pond_abs,
        "spread_pond_pct":            spread_pond_pct,
        "liq_tab_compra":             round(liq_tc, 2),
        "liq_tab_venta":              round(liq_tv, 2),
        "n_tab_compra":               len(cab_tc),
        "n_tab_venta":                len(cab_tv),
        "precio_maker_vender":        precio_maker_vender,
        "precio_maker_comprar":       precio_maker_comprar,
        "ganancia_neta_pct":          ganancia,
        "comision_total_pct":         round(comision_total_pct, 4),
        "spread_min_operativo":       c["SPREAD_MIN_OPERATIVO"],
        "analisis_top":               c["ANALISIS_TOP"],
        "banda_ponderado_pct":        c["BANDA_PONDERADO_PCT"],
        "brecha_ok":                  brecha_ok,
        "estado":                     estado,
        "color":                      color,
    }

def build_detalle_memory(raw_anuncios, tipo, now_dt):
    """Construye el array detalle desde raw para el frontend (no va a DB).
    Calcula velocidad de consumo USDT/min comparando con el ciclo anterior."""
    global prev_detalle_raw
    with config_lock:
        top = config["TOP_ANUNCIOS"]
    prev = prev_detalle_raw.get(tipo, {})
    rows = []
    nuevo_prev = {}
    for pos, item in enumerate(raw_anuncios[:top], 1):
        adv        = item.get("adv", {})
        trade      = item.get("advertiser", {})
        nombre     = trade.get("nickName", "")
        disp       = float(adv.get("tradableQuantity", 0))
        # Velocidad v2: USDT/min de fills CONFIRMADOS en los ultimos 30 min.
        # (antes: caida cruda del ultimo ciclo -> ruidosa y casi siempre 0)
        velocidad = fill_tracker.velocidad(nombre, tipo, now_dt)
        nuevo_prev[nombre] = (disp, now_dt)
        rows.append({
            "posicion":    pos,
            "anunciante":  nombre,
            "precio":      float(adv.get("price", 0)),
            "disponible":  disp,
            "completadas": int(trade.get("monthOrderCount", 0) or trade.get("tradeCount", 0) or 0),
            "tasa_exito":  round(float(trade.get("monthFinishRate", trade.get("finishRate", 0)) or 0) * 100, 1),
            "es_merchant": trade.get("userType") == "merchant",
            "velocidad":   velocidad,
        })
    prev_detalle_raw[tipo] = nuevo_prev
    return rows

def purgar_detalle_antiguo(dias=30):
    """Borra filas de snapshots_detalle con más de N días. Se ejecuta 1x/día."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM snapshots_detalle
                WHERE snapshot_timestamp < NOW() - (%s || ' days')::INTERVAL
            """, (dias,))
            eliminadas = cur.rowcount
        conn.commit()
    if eliminadas > 0:
        print(f"[PURGA] {eliminadas:,} filas eliminadas (>{dias} días)")
    return eliminadas

# ──────────────────────────────────────────────
#  FILLS v2 — volumen/velocidad por fills CONFIRMADOS
#  Cruza caidas de 'disponible' con incrementos de 'completadas'.
#  Validado sobre 7d de datos reales: 86% de las caidas se confirman
#  en <=5 ciclos; ~35% del volumen real queda enmascarado por recargas
#  (el metodo viejo lo perdia); ticket mediano de mercado: 272 USDT.
# ──────────────────────────────────────────────
class FillTracker:
    """Maquina de estados por (anunciante, tipo). Usada SOLO desde el
    thread de su colector (no necesita lock).
    - Caida de disponible SIN suba de completadas -> queda PENDIENTE.
    - completadas sube -> confirma pendientes + caida actual (metodo 'directo').
    - completadas sube sin caida visible (recargo en el mismo ciclo) ->
      fill ENMASCARADO, se estima con el ticket mediano del anunciante.
    - disponible vuelve al nivel previo -> cancelacion, se descarta.
    - pendiente expira (> FILL_VENTANA_MIN) -> edicion/retiro, no cuenta."""

    def __init__(self, exchange):
        self.exchange = exchange
        self.est = {}            # (anunciante, tipo) -> estado
        self.recent_fills = {}   # (anunciante, tipo) -> deque[(dt, monto)]

    def _cfg(self):
        with config_lock:
            tk_key = "FILL_TICKET_DEF_BYBIT" if self.exchange == "bybit" else "FILL_TICKET_DEF"
            return (float(config.get("FILL_CAP_USDT", 10000)),
                    float(config.get("FILL_VENTANA_MIN", 15)),
                    float(config.get(tk_key, config.get("FILL_TICKET_DEF", 272))))

    def _ticket(self, st, defecto):
        tk = st.get("tickets")
        if tk and len(tk) >= 3:
            s = sorted(tk)
            return s[len(s) // 2]
        return defecto

    def procesar_par(self, items_por_lado, now_dt):
        """Procesa los DOS lados (BUY/SELL) juntos en un ciclo. Devuelve filas
        para INSERT en fills_estimados.
        items_por_lado: {'BUY': [items...], 'SELL': [items...]} ya agrupados
        por anunciante (_agrupar_items).

        POR QUE JUNTOS (fix COL16): 'completadas' (monthOrderCount) es POR
        CUENTA, no por anuncio. Una orden que se llena en un lado sube el
        contador en los DOS anuncios del anunciante. Procesando cada lado por
        separado, el lado que NO se movio veia 'el contador subio sin caida de
        stock' e inventaba un fill 'enmascarado' fantasma (bug de anunciantes
        duales; afectaba tanto Mi Posicion como el volumen v2 del mercado).

        Dos arreglos:
        - ANTI-FANTASMA: los enmascarados se resuelven en un 2do paso, restando
          las ordenes que el OTRO lado de la cuenta ya explico con caida real.
        - ANTI-CANCELADA: al confirmar, se toman como maximo d_comp pendientes,
          los mas VIEJOS primero. Una orden cancelada (la caida mas nueva) queda
          sin confirmar y luego revierte/expira sola, sin inflar el volumen."""
        cap, ventana_min, ticket_def = self._cfg()
        ts_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        vistos = set()
        seguros = []       # fills confirmados por evidencia real (stock/pendientes)
        masc_cand = []     # candidatos a enmascarado, se resuelven en el paso 2
        ordenes_por_stock = {}   # nombre -> ordenes explicadas por evidencia real (cuenta)

        # ── Paso 1: stock + pendientes por (anunciante, lado) ──
        for tipo in ("BUY", "SELL"):
            for it in (items_por_lado.get(tipo) or []):
                nombre = it.get("anunciante") or ""
                if not nombre:
                    continue
                key = (nombre, tipo)
                vistos.add(key)
                disp   = float(it.get("disponible") or 0)
                comp   = int(it.get("completadas") or 0)
                precio = float(it.get("precio") or 0)
                st = self.est.get(key)
                if st is None:
                    self.est[key] = {"disp": disp, "comp": comp, "ts": now_dt,
                                     "pend": [], "tickets": deque(maxlen=20)}
                    continue
                gap_min = (now_dt - st["ts"]).total_seconds() / 60
                d_disp  = st["disp"] - disp
                d_comp  = comp - st["comp"]
                if d_comp < 0:
                    d_comp = 0   # rollover mensual del contador -> re-basear
                if gap_min > 10:
                    st.update({"disp": disp, "comp": comp, "ts": now_dt, "pend": []})
                    continue
                nivel_previo = st["disp"]
                # reversion: si el stock recupero el nivel previo de un pendiente,
                # fue cancelacion/edicion -> descartar ese pendiente
                st["pend"] = [p for p in st["pend"] if disp < p["nivel_previo"] * 0.98]
                # expirar pendientes fuera de ventana
                st["pend"] = [p for p in st["pend"]
                              if (now_dt - p["ts"]).total_seconds() / 60 <= ventana_min]
                monto, metodo, ordenes_expl, resid = 0.0, None, 0, 0
                if d_comp > 0:
                    # ANTI-CANCELADA: confirmar como maximo d_comp pendientes, viejos primero
                    st["pend"].sort(key=lambda p: p["ts"])
                    n_conf = min(len(st["pend"]), d_comp)
                    confirmado = sum(p["monto"] for p in st["pend"][:n_conf])
                    st["pend"] = st["pend"][n_conf:]
                    resid = d_comp - n_conf     # ordenes aun sin explicar por pendientes
                    if confirmado > 0:
                        monto, metodo, ordenes_expl = confirmado, "directo", n_conf
                    if d_disp > 1:
                        if resid > 0:
                            # caida de ESTE ciclo explica las ordenes restantes
                            directo = min(d_disp, cap)
                            if d_disp < 5000:
                                st["tickets"].append(d_disp / resid)
                            monto += directo
                            metodo = "directo"
                            ordenes_expl += resid
                            resid = 0
                        else:
                            # el contador ya quedo explicado por pendientes; esta
                            # caida es NUEVA (aun sin confirmar) -> pendiente
                            st["pend"].append({"monto": min(d_disp, cap),
                                               "nivel_previo": nivel_previo, "ts": now_dt})
                    if resid > 0:
                        # ordenes sin caida ni pendiente -> candidato enmascarado
                        # (se decide en el paso 2 mirando el otro lado de la cuenta)
                        masc_cand.append({"key": key, "nombre": nombre, "tipo": tipo,
                                          "precio": precio, "resid": resid, "st": st})
                elif d_disp > 1:
                    st["pend"].append({"monto": min(d_disp, cap),
                                       "nivel_previo": nivel_previo, "ts": now_dt})
                if monto > 0 and metodo:
                    ordenes_por_stock[nombre] = ordenes_por_stock.get(nombre, 0) + ordenes_expl
                    seguros.append({"key": key, "tipo": tipo, "nombre": nombre,
                                    "monto": monto, "ordenes": ordenes_expl,
                                    "metodo": metodo, "precio": precio})
                st.update({"disp": disp, "comp": comp, "ts": now_dt})

        # ── Paso 2: resolver enmascarados (anti-fantasma) ──
        # Un incremento de contador de una cuenta DUAL ya explicado por una caida
        # real en el otro lado NO es un fill nuevo: se resta y, si no queda
        # residual, se suprime (era el fantasma).
        for mc in masc_cand:
            explicadas = ordenes_por_stock.get(mc["nombre"], 0)
            residual = mc["resid"] - explicadas
            if residual > 0:
                monto = min(residual * self._ticket(mc["st"], ticket_def), cap)
                if monto > 0:
                    seguros.append({"key": mc["key"], "tipo": mc["tipo"], "nombre": mc["nombre"],
                                    "monto": monto, "ordenes": residual,
                                    "metodo": "enmascarado", "precio": mc["precio"]})

        # ── Emitir ──
        fills = []
        for f in seguros:
            if f["monto"] > 0:
                fills.append((ts_str, self.exchange, f["tipo"], f["nombre"],
                              round(f["monto"], 2), f["ordenes"], f["metodo"], f["precio"]))
                rf = self.recent_fills.setdefault(f["key"], deque(maxlen=60))
                rf.append((now_dt, f["monto"]))

        # limpiar estados de anunciantes ausentes hace >30 min (ambos lados)
        muertos = [k for k, s in self.est.items()
                   if k not in vistos and (now_dt - s["ts"]).total_seconds() > 1800]
        for k in muertos:
            self.est.pop(k, None)
            self.recent_fills.pop(k, None)
        return fills

    def velocidad(self, nombre, tipo, now_dt, ventana_min=30):
        """USDT/min de fills confirmados en los ultimos `ventana_min` minutos."""
        rf = self.recent_fills.get((nombre, tipo))
        if not rf:
            return 0.0
        corte = ventana_min * 60
        tot = sum(m for t, m in rf if (now_dt - t).total_seconds() <= corte)
        return round(tot / ventana_min, 1)


fill_tracker       = FillTracker("binance")
fill_tracker_bybit = FillTracker("bybit")


def _agrupar_items(items):
    """FIX multi-anuncio (validado con datos 6-13 jul): 131 anunciantes operan
    2+ anuncios simultaneos en el mismo lado (22% de las filas del libro, 73%
    del volumen). Sin agrupar, el tracker mezcla los stocks de ambos anuncios
    y fabrica volumen falso (~31% de sobreconteo medido). Se fusiona por
    anunciante: stock = suma de sus anuncios, contador = maximo (es por
    anunciante y robusto a glitches de la API), precio = el del mejor puesto."""
    por = {}
    for it in items:
        n = it.get("anunciante") or ""
        if not n:
            continue
        d = por.get(n)
        if d is None:
            por[n] = dict(it)   # conserva el precio del primer (mejor) puesto
        else:
            d["disponible"]  = float(d.get("disponible") or 0) + float(it.get("disponible") or 0)
            d["completadas"] = max(int(d.get("completadas") or 0), int(it.get("completadas") or 0))
    return list(por.values())


def _items_binance(raw):
    """Normaliza los items crudos de Binance al formato del FillTracker."""
    out = []
    for item in raw:
        adv   = item.get("adv", {})
        trade = item.get("advertiser", {})
        out.append({
            "anunciante":  trade.get("nickName", ""),
            "precio":      float(adv.get("price", 0) or 0),
            "disponible":  float(adv.get("tradableQuantity", 0) or 0),
            "completadas": int(trade.get("monthOrderCount", 0) or trade.get("tradeCount", 0) or 0),
        })
    return out


def guardar_fills(fills):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO fills_estimados
                (ts, exchange, tipo, anunciante, monto, ordenes, metodo, precio)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """, fills)
        conn.commit()


def purgar_fills_antiguos(dias=30):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM fills_estimados
                WHERE ts < NOW() - (%s || ' days')::INTERVAL
            """, (dias,))
            n = cur.rowcount
        conn.commit()
    if n > 0:
        print(f"[PURGA fills] {n:,} filas eliminadas (>{dias} dias)")
    return n


def backfill_fills(horas=48):
    """Si fills_estimados esta vacia, la siembra desde snapshots_detalle
    (ultimas N horas) con una version simplificada del criterio (sin
    matching de retraso ni cancelaciones -> metodo 'retro'). Asi el
    grafico de 12h funciona desde el primer deploy."""
    with config_lock:
        cap    = float(config.get("FILL_CAP_USDT", 10000))
        ticket = float(config.get("FILL_TICKET_DEF", 272))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM fills_estimados")
            if cur.fetchone()[0] > 0:
                return 0
            total = 0
            for tabla, ex in (("snapshots_detalle", "binance"),
                              ("snapshots_detalle_bybit", "bybit")):
                try:
                    cur.execute(f"""
                        INSERT INTO fills_estimados
                        (ts, exchange, tipo, anunciante, monto, ordenes, metodo, precio)
                        SELECT ts, %(ex)s, tipo, anunciante,
                               ROUND(CASE
                                   WHEN d_disp > 1 THEN LEAST(d_disp, %(cap)s)
                                   ELSE d_comp * %(ticket)s
                               END::numeric, 2),
                               d_comp, 'retro', precio
                        FROM (
                            SELECT snapshot_timestamp AS ts, tipo, anunciante, precio,
                                   LAG(disponible)  OVER w - disponible  AS d_disp,
                                   completadas - LAG(completadas) OVER w AS d_comp,
                                   EXTRACT(EPOCH FROM (snapshot_timestamp
                                       - LAG(snapshot_timestamp) OVER w)) / 60 AS gap
                            FROM (
                                SELECT snapshot_timestamp, tipo, anunciante,
                                       SUM(disponible)  AS disponible,
                                       MAX(completadas) AS completadas,
                                       MIN(precio)      AS precio
                                FROM {tabla}
                                WHERE snapshot_timestamp >= NOW() - (%(horas)s || ' hours')::INTERVAL
                                GROUP BY snapshot_timestamp, tipo, anunciante
                            ) base
                            WINDOW w AS (PARTITION BY anunciante, tipo
                                         ORDER BY snapshot_timestamp)
                        ) t
                        WHERE d_comp > 0 AND gap IS NOT NULL AND gap <= 10
                    """, {"ex": ex, "cap": cap, "ticket": ticket, "horas": horas})
                    total += cur.rowcount
                    conn.commit()   # commit por tabla: un fallo en bybit no revierte binance
                except Exception as e:
                    print(f"[FILLS backfill {ex}] {e}")
                    conn.rollback()
    print(f"[FILLS backfill] {total:,} fills retro sembrados ({horas}h)")
    return total


# ──────────────────────────────────────────────
#  BYBIT P2P (colector paralelo)
# ──────────────────────────────────────────────
URL_BYBIT     = "https://api2.bybit.com/fiat/otc/item/online"
HEADERS_BYBIT = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Origin": "https://www.bybit.com",
    "Referer": "https://www.bybit.com/",
}

def obtener_anuncios_bybit(tipo):
    """tipo BUY = comprar USDT (side 1) ; SELL = vender USDT (side 0)."""
    side = "1" if tipo == "BUY" else "0"
    with config_lock:
        c = dict(config)
    top = c["TOP_ANUNCIOS"]
    POR_PAGINA = 20
    n_paginas = (top + POR_PAGINA - 1) // POR_PAGINA
    out = []
    for page in range(1, n_paginas + 1):
        payload = {
            "userId": "", "tokenId": c["MONEDA"], "currencyId": c["FIAT"],
            "payment": [], "side": side, "size": str(POR_PAGINA),
            "page": str(page), "amount": "", "authMaker": False, "canTrade": False,
        }
        try:
            r = requests.post(URL_BYBIT, json=payload, headers=HEADERS_BYBIT, timeout=10)
            r.raise_for_status()
            items = ((r.json().get("result") or {}).get("items")) or []
        except Exception as e:
            print(f"[ERROR bybit {tipo} p{page}] {e}")
            break
        if not items:
            break
        out.extend(items)
        if len(items) < POR_PAGINA:
            break
        if page < n_paginas:
            time.sleep(0.3)
    return out[:top]

def _bybit_item(item):
    return {
        "anunciante":  item.get("nickName", ""),
        "precio":      float(item.get("price", 0) or 0),
        "disponible":  float(item.get("lastQuantity", item.get("quantity", 0)) or 0),
        "completadas": int(item.get("recentOrderNum", 0) or 0),
        "tasa_exito":  float(item.get("recentExecuteRate", 0) or 0),   # ya viene 0-100
        "es_merchant": item.get("userType", "PERSONAL") != "PERSONAL",
    }

def parsear_y_filtrar_bybit(items, tipo):
    with config_lock:
        c = dict(config)
    res = []
    for it in items:
        f = _bybit_item(it)
        if f["disponible"]  < c["FILTRO_MIN_USDT"]: continue
        if f["completadas"] < c["FILTRO_MIN_ORD"]:  continue
        if f["tasa_exito"]  < c["FILTRO_MIN_TASA"]: continue
        res.append({"tipo": tipo, "precio": f["precio"], "disponible": f["disponible"], "anunciante": f["anunciante"]})
    return res

def guardar_detalle_bybit(timestamp, hora, raw_compra, raw_venta):
    with config_lock:
        top  = config["TOP_ANUNCIOS"]
        band = config["BANDA_DETALLE_PCT"]
    _pb = lambda it: float(it.get("price", 0) or 0)
    raw_compra = _detalle_banda(raw_compra, _pb, band)
    raw_venta  = _detalle_banda(raw_venta,  _pb, band)
    rows = []
    for tipo, raw in (("BUY", raw_compra), ("SELL", raw_venta)):
        for pos, it in enumerate(raw[:top], 1):
            f = _bybit_item(it)
            rows.append((timestamp, hora, tipo, pos, f["anunciante"], f["precio"],
                         f["disponible"], f["completadas"], f["tasa_exito"], f["es_merchant"]))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO snapshots_detalle_bybit
                (snapshot_timestamp, hora, tipo, posicion, anunciante, precio,
                 disponible, completadas, tasa_exito, es_merchant)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, rows)
        conn.commit()

def build_detalle_memory_bybit(raw, tipo, now_dt):
    global prev_detalle_raw_bybit
    with config_lock:
        top = config["TOP_ANUNCIOS"]
    prev = prev_detalle_raw_bybit.get(tipo, {})
    rows, nuevo = [], {}
    for pos, it in enumerate(raw[:top], 1):
        f = _bybit_item(it)
        nombre, disp = f["anunciante"], f["disponible"]
        # Velocidad v2: USDT/min de fills confirmados ultimos 30 min
        vel = fill_tracker_bybit.velocidad(nombre, tipo, now_dt)
        nuevo[nombre] = (disp, now_dt)
        rows.append({"posicion": pos, "anunciante": nombre, "precio": f["precio"],
                     "disponible": disp, "completadas": f["completadas"],
                     "tasa_exito": round(f["tasa_exito"], 1), "es_merchant": f["es_merchant"], "velocidad": vel})
    prev_detalle_raw_bybit[tipo] = nuevo
    return rows

def ciclo_colector_bybit():
    print("[BYBIT] Iniciando thread...")
    time.sleep(8)
    _ultima_purga = None
    while True:
        try:
            hoy = datetime.now(SANTIAGO_TZ).date()
            if _ultima_purga != hoy:
                try:
                    with get_conn() as conn:
                        with conn.cursor() as cur:
                            cur.execute("DELETE FROM snapshots_detalle_bybit WHERE snapshot_timestamp < NOW() - INTERVAL '7 days'")
                        conn.commit()
                except Exception as e:
                    print(f"[BYBIT purga] {e}")
                _ultima_purga = hoy
            raw_compra = obtener_anuncios_bybit("BUY")
            raw_venta  = obtener_anuncios_bybit("SELL")
            with config_lock:
                anal_top = config["ANALISIS_TOP"]
            tab_compra = parsear_y_filtrar_bybit(raw_compra[:anal_top], "BUY")
            tab_venta  = parsear_y_filtrar_bybit(raw_venta[:anal_top],  "SELL")
            with config_lock:
                com_by = config["COM_BYBIT_MAKER"]
            estado = analizar(tab_compra, tab_venta, com_by)
            if estado:
                guardar_snapshot(estado, "snapshots_bybit")
                ts, hora = estado["timestamp"], estado["hora"]
                now_dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=SANTIAGO_TZ)
                guardar_detalle_bybit(ts, hora, raw_compra, raw_venta)
                # ── Fills v2 (Bybit) ──
                try:
                    fills = fill_tracker_bybit.procesar_par({
                        "BUY":  _agrupar_items([_bybit_item(x) for x in raw_compra]),
                        "SELL": _agrupar_items([_bybit_item(x) for x in raw_venta]),
                    }, now_dt)
                    if fills:
                        guardar_fills(fills)
                except Exception as e:
                    print(f"[FILLS BY] {e}")
                estado["detalle_compra"] = build_detalle_memory_bybit(raw_compra, "BUY",  now_dt)
                estado["detalle_venta"]  = build_detalle_memory_bybit(raw_venta,  "SELL", now_dt)
                with data_lock:
                    ultimo_estado_bybit.clear()
                    ultimo_estado_bybit.update(estado)
                print(f"[BYBIT {ts}] spread {estado['spread_pond_pct']}% | {len(raw_compra)+len(raw_venta)} anuncios")
            else:
                print("[BYBIT] sin datos suficientes")
        except Exception as e:
            import traceback
            print(f"[ERROR BYBIT] {e}")
            print(traceback.format_exc())
        with config_lock:
            intervalo = config["INTERVALO_MIN"]
        time.sleep(intervalo * 60)

def decidir_operativa(gan, min_op, ratio, presion, rot_lento, rot_dual, sesgo_min):
    """Arbol de decision del asistente — UNICA fuente (lo usan api_operativa y
    _registrar_operativa; antes estaba duplicado y podia desincronizarse).
    Devuelve (decision, color, razon).
    ratio None = sin datos de rotacion (tracker recien arrancado tras un
    reinicio): NO asumir mercado agil — degradar a paciente, no dar verde ciego."""
    if ratio is None:
        if gan >= min_op:
            return ("OPERAR DUAL (paciente)", "yellow",
                    f"Spread neto {gan}% sobre tu minimo ({min_op}%), pero todavia no hay datos de rotacion (colector recien iniciado): entra con paciencia")
        return ("ESPERAR", "red",
                f"Spread neto {gan}% bajo tu minimo ({min_op}%) y sin datos de rotacion aun — mejor conservar el capital")
    if gan >= min_op and ratio >= rot_dual:
        return ("OPERAR DUAL", "green",
                f"Spread neto {gan}% sobre tu minimo ({min_op}%) y mercado rotando {ratio}x su promedio de 12h")
    if gan >= min_op and ratio >= rot_lento:
        return ("OPERAR DUAL (paciente)", "yellow",
                f"Spread neto {gan}% es operable, pero la rotacion esta en {ratio}x del promedio (umbral dual: {rot_dual}x): los fills tardaran mas de lo habitual")
    if gan >= min_op:
        return ("SOLO PIERNA CON FLUJO", "orange",
                f"Spread neto {gan}% pero mercado lento ({ratio}x): no bloquees capital en dual; opera solo el lado que la presion favorece")
    if ratio >= rot_dual and abs(presion - 50) >= sesgo_min:
        lado = "VENTA" if presion > 50 else "COMPRA"
        return (f"SOLO {lado}", "orange",
                f"Spread neto {gan}% bajo tu minimo, pero el flujo esta {ratio}x acelerado y {presion}% cargado a la compra: una sola pierna del lado con demanda puede pagar")
    return ("ESPERAR", "red",
            f"Spread neto {gan}% bajo tu minimo ({min_op}%) y rotacion {ratio}x — mejor conservar el capital para la proxima ventana")


_ultimo_reg_operativa = [None]   # throttle del registro (1 fila cada 5 min)

def _registrar_operativa(snap):
    """Registra la decisión del asistente en operativa_historial, para poder
    ver por hora/día cuándo hubo ventana. Copia la lógica de señales+decisión
    de api_operativa. Se llama desde el ciclo del colector (throttle 5 min);
    va envuelto en try/except del que lo llama, nunca rompe el ciclo."""
    now = datetime.now(SANTIAGO_TZ)
    if _ultimo_reg_operativa[0] and (now - _ultimo_reg_operativa[0]).total_seconds() < 300:
        return
    if not snap or snap.get("spread_pond_pct") is None:
        return
    with config_lock:
        c = dict(config)
    f = lambda dt: dt.strftime("%Y-%m-%d %H:%M:%S")
    hp = 12
    v30 = v60b = v60 = v12 = 0.0
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT COALESCE(SUM(monto) FILTER (WHERE ts >= %(m30)s), 0) AS v30,
                       COALESCE(SUM(monto) FILTER (WHERE ts >= %(m60)s AND tipo='BUY'), 0) AS v60b,
                       COALESCE(SUM(monto) FILTER (WHERE ts >= %(m60)s), 0) AS v60,
                       COALESCE(SUM(monto), 0) AS v12
                FROM fills_estimados WHERE exchange='binance' AND ts >= %(h12)s
            """, {"m30": f(now - timedelta(minutes=30)),
                  "m60": f(now - timedelta(minutes=60)),
                  "h12": f(now - timedelta(hours=hp))})
            r = cur.fetchone()
            v30, v60b = float(r["v30"] or 0), float(r["v60b"] or 0)
            v60, v12  = float(r["v60"] or 0), float(r["v12"] or 0)
    gan       = float(snap.get("ganancia_neta_pct") or 0)
    min_op    = float(c["SPREAD_MIN_OPERATIVO"])
    rot_lento = float(c.get("UMBRAL_ROT_LENTO", 0.7))
    rot_dual  = float(c.get("UMBRAL_ROT_DUAL", 1.0))
    sesgo_min = float(c.get("UMBRAL_PRESION_SESGO", 10))
    um30  = v30 / 30
    uprom = v12 / (hp * 60) if v12 else 0
    ratio   = round(um30 / uprom, 2) if uprom else None
    presion = round(v60b / v60 * 100, 1) if v60 else 50.0
    decision, color, _razon = decidir_operativa(gan, min_op, ratio, presion, rot_lento, rot_dual, sesgo_min)
    gap_cfg = float(c.get("GAP_OBJETIVO_BRUTO", 0) or 0)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO operativa_historial (ts, hora, decision, color, spread_neto, ratio, presion, min_op, gap)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (f(now), now.hour, decision, color, round(gan, 4), ratio, presion, min_op, gap_cfg))
        conn.commit()
    _ultimo_reg_operativa[0] = now


def ciclo_colector():
    print("[COLECTOR] Iniciando thread...")
    time.sleep(5)
    print("[COLECTOR] Primer ciclo comenzando")
    # Siembra retro de fills (solo si la tabla esta vacia) -> grafico 12h
    # utilizable desde el primer deploy, sin esperar medio dia de datos.
    try:
        backfill_fills(horas=48)
    except Exception as e:
        print(f"[FILLS backfill] {e}")
    _ultima_purga = None   # controla que la purga se ejecute 1x/día
    while True:
        try:
            # ── Purga diaria ──────────────────────────────────
            hoy = datetime.now(SANTIAGO_TZ).date()
            if _ultima_purga != hoy:
                # OJO al orden: congelar los agregados ANTES de purgar el detalle,
                # si no perdemos la historia que justamente queremos preservar.
                try:
                    guardar_agregados_dia(hoy - timedelta(days=1))
                    guardar_agregados_dia(hoy)   # parcial del dia en curso
                except Exception as e:
                    print(f"[AGREGADOS diario] {e}")
                purgar_detalle_antiguo(dias=7)
                try:
                    purgar_fills_antiguos(dias=30)
                except Exception as e:
                    print(f"[PURGA fills] {e}")
                try:
                    recalibrar_tickets()
                except Exception as e:
                    print(f"[TICKET diario] {e}")
                try:
                    recalibrar_ritmo()
                except Exception as e:
                    print(f"[RITMO diario] {e}")
                try:
                    recalibrar_horarios()
                except Exception as e:
                    print(f"[HORARIOS diario] {e}")
                _ultima_purga = hoy
            print("[COLECTOR] Consultando Binance BUY...")
            raw_compra = obtener_anuncios("BUY")
            print(f"[COLECTOR] BUY raw: {len(raw_compra)} anuncios")
            print("[COLECTOR] Consultando Binance SELL...")
            raw_venta = obtener_anuncios("SELL")
            print(f"[COLECTOR] SELL raw: {len(raw_venta)} anuncios")

            with config_lock:
                anal_top = config["ANALISIS_TOP"]
            # Spread/liquidez de cabecera: solo el top del libro (baseline comparable).
            # El detalle/profundidad si usa las 80 posiciones (guardar_detalle).
            tab_compra = parsear_y_filtrar(raw_compra[:anal_top], "BUY")
            tab_venta  = parsear_y_filtrar(raw_venta[:anal_top],  "SELL")

            estado = analizar(tab_compra, tab_venta)
            if estado:
                guardar_snapshot(estado)
                # Reutilizar el mismo timestamp que generó analizar() → consistencia DB
                ts     = estado["timestamp"]
                hora   = estado["hora"]
                now_dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=SANTIAGO_TZ)
                guardar_detalle(ts, hora, raw_compra, raw_venta)
                # ── Fills v2: confirmar consumo cruzando con 'completadas' ──
                try:
                    fills = fill_tracker.procesar_par({
                        "BUY":  _agrupar_items(_items_binance(raw_compra)),
                        "SELL": _agrupar_items(_items_binance(raw_venta)),
                    }, now_dt)
                    if fills:
                        guardar_fills(fills)
                except Exception as e:
                    print(f"[FILLS BN] {e}")
                estado["detalle_compra"] = build_detalle_memory(raw_compra, "BUY",  now_dt)
                estado["detalle_venta"]  = build_detalle_memory(raw_venta,  "SELL", now_dt)
                # ── Registro del asistente (historial de ventanas por hora) ──
                try:
                    _registrar_operativa(estado)
                except Exception as e:
                    print(f"[REG operativa] {e}")
                with data_lock:
                    ultimo_estado.update(estado)
                print(f"[{estado['timestamp']}] Spread pond: {estado['spread_pond_pct']}% — {estado['estado']} | Detalle: {len(raw_compra)+len(raw_venta)} anunciantes guardados")
            else:
                print("[COLECTOR] Sin datos suficientes para analizar")
        except Exception as e:
            import traceback
            print(f"[ERROR COLECTOR] {e}")
            print(traceback.format_exc())
        with config_lock:
            intervalo = config["INTERVALO_MIN"]
        print(f"[COLECTOR] Esperando {intervalo} minutos...")
        time.sleep(intervalo * 60)

# ──────────────────────────────────────────────
#  DASHBOARD
# ──────────────────────────────────────────────
DASHBOARD = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>P2P Monitor — Unión Austral Capital</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&family=VT323&display=swap" rel="stylesheet">
<style>
/* ============================================================
   Unión Austral · P2P Monitor — sistema visual
   Fintech sobrio · dark tinta · verde/rojo desaturado
   ============================================================ */
:root {
  /* superficies (azul tinta desaturado, no negro puro) */
  --bg:        oklch(0.155 0.012 256);
  --bg-1:      oklch(0.185 0.013 256);
  --bg-2:      oklch(0.212 0.014 256);
  --bg-3:      oklch(0.252 0.015 256);
  --line:      oklch(0.33 0.013 256);
  --line-soft: oklch(0.265 0.011 256);
  /* texto */
  --text:   oklch(0.955 0.004 256);
  --text-2: oklch(0.74 0.008 256);
  --text-3: oklch(0.56 0.010 256);
  /* semánticos desaturados */
  --buy:       oklch(0.74 0.105 158);
  --sell:      oklch(0.665 0.135 23);
  --warn:      oklch(0.80 0.095 82);
  --warn-low:  oklch(0.72 0.105 52);
  --buy-soft:      color-mix(in oklch, var(--buy) 16%, transparent);
  --sell-soft:     color-mix(in oklch, var(--sell) 16%, transparent);
  --warn-soft:     color-mix(in oklch, var(--warn) 16%, transparent);
  --warn-low-soft: color-mix(in oklch, var(--warn-low) 16%, transparent);
  /* acento de marca (lo pisa el tweak) */
  --accent: #5b8def;
  --accent-soft: color-mix(in oklch, var(--accent) 16%, transparent);
  --accent-glow: color-mix(in oklch, var(--accent) 40%, transparent);
  /* tipografía */
  --font: "Space Grotesk", system-ui, sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, monospace;
  /* densidad (la pisa data-density) */
  --gap: 16px; --pad: 20px; --card-r: 16px;
  color-scheme: dark;
}
[data-density="compacta"] { --gap: 10px; --pad: 14px; --card-r: 12px; }
[data-density="comoda"]   { --gap: 16px; --pad: 20px; --card-r: 16px; }

.tone-buy      { --tone: var(--buy);      --tone-soft: var(--buy-soft); }
.tone-sell     { --tone: var(--sell);     --tone-soft: var(--sell-soft); }
.tone-warn     { --tone: var(--warn);     --tone-soft: var(--warn-soft); }
.tone-warn-low { --tone: var(--warn-low); --tone-soft: var(--warn-low-soft); }
.tone-accent   { --tone: var(--accent);   --tone-soft: var(--accent-soft); }

* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { background: var(--bg); }
body {
  font-family: var(--font);
  color: var(--text);
  -webkit-font-smoothing: antialiased;
  min-height: 100vh;
  font-feature-settings: "ss01";
}
.tnum { font-family: var(--mono); font-variant-numeric: tabular-nums; letter-spacing: -0.01em; }

.app { max-width: 1320px; margin: 0 auto; padding: 0 clamp(12px, 3vw, 28px) 40px; }
.loading { display: grid; place-items: center; height: 80vh; color: var(--text-3); font-family: var(--mono); }

/* ---------- TopBar ---------- */
.topbar {
  position: sticky; top: 0; z-index: 30;
  display: flex; align-items: center; gap: 20px;
  padding: 14px 4px; margin-bottom: 4px;
  background: color-mix(in oklch, var(--bg) 88%, transparent);
  backdrop-filter: blur(12px);
  border-bottom: 1px solid var(--line-soft);
}
.brand { display: flex; align-items: center; gap: 12px; }
.brand-mark {
  width: 42px; height: 42px; border-radius: 11px;
  display: grid; place-items: center;
  font-family: var(--mono); font-weight: 600; font-size: 15px; letter-spacing: 0.04em;
  color: var(--text); background: var(--bg-2);
  border: 1px solid var(--line);
  box-shadow: inset 0 0 0 1px color-mix(in oklch, var(--accent) 30%, transparent);
}
.brand-name { font-size: 15px; font-weight: 600; letter-spacing: -0.01em; }
.brand-name span { color: var(--text-3); font-weight: 500; }
.brand-sub { font-size: 11px; color: var(--text-3); text-transform: uppercase; letter-spacing: 0.16em; }
.market-chip {
  display: flex; align-items: center; gap: 7px;
  padding: 7px 13px; border-radius: 999px;
  background: var(--bg-1); border: 1px solid var(--line-soft);
  font-size: 12.5px;
}
.mc-pair { font-family: var(--mono); font-weight: 500; }
.mc-dot { color: var(--text-3); }
.mc-src { color: var(--text-2); }
.topbar-right { margin-left: auto; display: flex; align-items: center; gap: 18px; }
.last-upd { text-align: right; white-space: nowrap; }
.lu-label { font-size: 10px; color: var(--text-3); text-transform: uppercase; letter-spacing: 0.1em; white-space: nowrap; }
.lu-time { font-size: 14px; color: var(--text-2); }

/* live pulse */
.live { position: relative; display: inline-flex; align-items: center; gap: 14px; padding-left: 4px; }
.live-dot { width: 8px; height: 8px; border-radius: 50%; position: relative; z-index: 1; }
.live-ring { position: absolute; left: 4px; top: 50%; width: 8px; height: 8px; margin-top: -4px; border-radius: 50%; border: 1px solid; animation: ping 2s ease-out infinite; }
@keyframes ping { 0% { transform: scale(1); opacity: .7; } 80%,100% { transform: scale(2.6); opacity: 0; } }
.live-txt { font-size: 10px; letter-spacing: 0.18em; color: var(--text-2); font-family: var(--mono); }

/* countdown ring */
.ring { position: relative; display: grid; place-items: center; }
.ring svg { position: absolute; inset: 0; }
.ring-label { font-size: 13px; color: var(--text); }
.ring-s { font-size: 9px; color: var(--text-3); }

/* ---------- Tabs ---------- */
.tabbar { display: flex; gap: 4px; padding: 14px 0 18px; overflow-x: auto; scrollbar-width: none; -ms-overflow-style: none; -webkit-overflow-scrolling: touch; }
.tabbar::-webkit-scrollbar { display: none; }
.tab {
  font-family: var(--font); font-size: 13px; font-weight: 500;
  color: var(--text-3); background: transparent;
  border: 1px solid transparent; border-radius: 9px;
  padding: 8px 15px; cursor: pointer; transition: all .15s;
  white-space: nowrap; flex-shrink: 0;
}
.tab:hover { color: var(--text-2); background: var(--bg-1); }
.tab.active { color: var(--text); background: var(--bg-2); border-color: var(--line); box-shadow: inset 0 -2px 0 var(--accent); }

.content { min-height: 60vh; }
.view { display: flex; flex-direction: column; gap: var(--gap); }

/* ---------- Decision hero ---------- */
.hero {
  position: relative; overflow: hidden;
  border: 1px solid var(--line); border-radius: var(--card-r);
  background:
    radial-gradient(120% 140% at 0% 0%, var(--tone-soft), transparent 55%),
    var(--bg-1);
  padding: clamp(18px, 2.4vw, 28px);
  display: flex; flex-direction: column; gap: 18px;
}
.hero::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 4px; background: var(--tone); }
.hero-main { display: flex; align-items: flex-end; justify-content: space-between; gap: 24px; flex-wrap: wrap; }
.hero-flag { display: flex; align-items: center; gap: 16px; }
.hero-icon { font-size: 20px; color: var(--tone); letter-spacing: -2px; }
.hero-estado { font-size: clamp(26px, 4vw, 40px); font-weight: 600; letter-spacing: -0.02em; line-height: 1; color: var(--text); }
.hero-sub { font-size: 12.5px; color: var(--text-3); margin-top: 6px; }
.hero-metrics { display: flex; gap: clamp(20px, 3vw, 44px); }
.hm-label { font-size: 11px; color: var(--text-3); text-transform: uppercase; letter-spacing: 0.1em; margin-bottom: 5px; }
.hm-val { font-family: var(--mono); font-size: 22px; font-weight: 500; color: var(--text); }
.hm-val.big { font-size: clamp(28px, 3.4vw, 38px); color: var(--tone); }
.hm-val.pos { color: var(--buy); }
.hm-val.neg { color: var(--sell); }
.hm-foot { font-size: 11px; color: var(--text-3); margin-top: 4px; }

/* gauge */
.gauge { display: flex; flex-direction: column; gap: 6px; }
.gauge-track { position: relative; height: 12px; border-radius: 7px; overflow: hidden; display: flex; border: 1px solid var(--line-soft); }
.gauge-zone { height: 100%; }
.gauge-marker { position: absolute; top: -4px; bottom: -4px; width: 2px; transform: translateX(-50%); }
.gauge-stick { width: 3px; height: 100%; margin: 0 auto; background: var(--text); border-radius: 2px; box-shadow: 0 0 0 2px var(--bg-1), 0 0 10px var(--accent-glow); }
.gauge-scale { display: flex; justify-content: space-between; font-family: var(--mono); font-size: 10px; color: var(--text-3); }

/* ---------- Market grid ---------- */
.market { display: grid; grid-template-columns: 1fr auto 1fr; gap: 6px; align-items: stretch; }
.sidecard {
  border: 1px solid var(--line); border-radius: var(--card-r);
  background: var(--bg-1); padding: var(--pad);
  display: flex; flex-direction: column; gap: 12px;
  position: relative;
}
.sidecard::before { content: ""; position: absolute; inset: 0; border-radius: inherit; box-shadow: inset 0 1px 0 color-mix(in oklch, white 5%, transparent); pointer-events: none; }
.sc-head { display: flex; align-items: center; gap: 10px; }
.sc-badge {
  font-family: var(--mono); font-size: 10.5px; font-weight: 600; letter-spacing: 0.08em;
  padding: 3px 9px; border-radius: 6px; color: var(--tone);
  background: var(--tone-soft); border: 1px solid color-mix(in oklch, var(--tone) 40%, transparent);
}
.sc-role { font-size: 12.5px; color: var(--text-2); font-weight: 500; }
.sc-desc { font-size: 11.5px; color: var(--text-3); margin-top: -6px; }
.sc-pond-label { font-size: 11px; color: var(--text-3); text-transform: uppercase; letter-spacing: 0.1em; }
.sc-pond-val { font-family: var(--mono); font-size: clamp(30px, 4vw, 42px); font-weight: 500; color: var(--tone); line-height: 1.05; letter-spacing: -0.02em; }
.spark { width: 100%; display: block; }

.sc-leader { display: flex; align-items: center; justify-content: space-between; padding: 11px 13px; border-radius: 10px; background: var(--bg-2); border: 1px solid var(--line-soft); }
.sc-leader-label { font-size: 10.5px; color: var(--text-3); text-transform: uppercase; letter-spacing: 0.08em; }
.sc-leader-val { font-size: 18px; font-weight: 500; color: var(--text); margin-top: 2px; }
.sc-leader-who { display: flex; align-items: center; gap: 8px; }
.who-name { font-size: 13px; color: var(--text); font-weight: 500; }
.who-tag { font-family: var(--mono); font-size: 9px; text-transform: uppercase; letter-spacing: 0.1em; color: var(--tone); border: 1px solid color-mix(in oklch, var(--tone) 40%, transparent); padding: 1px 6px; border-radius: 5px; }

.sc-stats { display: grid; grid-template-columns: 1.1fr 0.7fr 1.2fr; gap: 10px; margin-top: auto; padding-top: 4px; }
.sc-stat-label { font-size: 10px; color: var(--text-3); text-transform: uppercase; letter-spacing: 0.07em; margin-bottom: 4px; }
.sc-stat-val { font-size: 14px; color: var(--text); }
.sc-stat-val.small { font-size: 11.5px; color: var(--text-2); }
.sc-stat-val .u { font-size: 10px; color: var(--text-3); }
.hbar { height: 4px; border-radius: 3px; background: var(--bg-3); margin-top: 6px; overflow: hidden; }
.hbar-fill { height: 100%; border-radius: 3px; }

/* spine */
.spine { display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 0 8px; min-width: 92px; }
.spine-line { flex: 1; width: 1px; background: linear-gradient(var(--line), transparent); }
.spine-line:last-child { background: linear-gradient(transparent, var(--line)); }
.spine-pill { text-align: center; padding: 12px 14px; border-radius: 12px; background: var(--bg-2); border: 1px solid var(--line); margin: 8px 0; }
.spine-label { font-size: 9.5px; color: var(--text-3); text-transform: uppercase; letter-spacing: 0.1em; }
.spine-val { font-size: 17px; color: var(--text); margin-top: 3px; }
.spine-pct { font-size: 12px; color: var(--accent); margin-top: 2px; }
.sc-leader-val, .spine-val, .spine-pct, .rank-liq, .rank-price, .ob-price, .ob-amt { font-variant-numeric: tabular-nums; }

/* ---------- Maker ---------- */
.maker { border: 1px solid var(--line); border-radius: var(--card-r); background: var(--bg-1); padding: var(--pad); }
.maker-head { display: flex; align-items: baseline; gap: 12px; margin-bottom: 14px; flex-wrap: wrap; }
.maker-kicker { font-family: var(--mono); font-size: 11px; text-transform: uppercase; letter-spacing: 0.12em; color: var(--accent); }
.maker-hint { font-size: 12px; color: var(--text-3); }
.maker-grid { display: grid; grid-template-columns: 1fr 1fr; gap: var(--gap); }
.maker-card { padding: 16px 18px; border-radius: 12px; background: var(--bg-2); border: 1px solid var(--line); border-left: 3px solid var(--tone); }
.mc-top { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
.mc-title { font-size: 13px; font-weight: 600; color: var(--text); letter-spacing: 0.01em; }
.mc-side { font-size: 11px; color: var(--text-3); }
.mc-price { font-size: clamp(26px, 3.4vw, 34px); font-weight: 500; color: var(--tone); margin: 8px 0 6px; font-variant-numeric: tabular-nums; }
.mc-note { font-size: 12.5px; color: var(--text-2); }
.mc-note b { color: var(--text); font-weight: 600; }
.mc-tip { font-size: 11.5px; color: var(--text-3); margin-top: 8px; }

/* ---------- TR bottom: chart + ranking ---------- */
.tr-bottom { display: grid; grid-template-columns: 1.55fr 1fr; gap: var(--gap); align-items: start; }
.chart-card, .orderbook, .ranking { border: 1px solid var(--line); border-radius: var(--card-r); background: var(--bg-1); padding: var(--pad); }
.card-head { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; margin-bottom: 12px; }
.card-head h3 { font-size: 13.5px; font-weight: 600; color: var(--text); letter-spacing: -0.01em; }
.card-sub { font-size: 11px; color: var(--text-3); }
.precio-top { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px; margin-bottom: 12px; }
.precio-leg { display: flex; gap: 16px; flex-wrap: wrap; }
.pl-item { display: flex; align-items: center; gap: 6px; font-size: 13px; font-weight: 600; }
.pl-brecha { font-size: 12px; color: var(--warn); background: var(--warn-soft); border: 1px solid color-mix(in oklch, var(--warn) 35%, transparent); border-radius: 6px; padding: 2px 10px; margin-left: 4px; font-variant-numeric: tabular-nums; }
.pl-dot { width: 10px; height: 10px; border-radius: 3px; }
.precio-rangos { display: flex; gap: 6px; }
.pr-btn { font-size: 12px; padding: 5px 12px; border-radius: 7px; border: 1px solid var(--line); background: var(--bg-2); color: var(--text-2); cursor: pointer; }
.pr-btn.on { border-color: var(--accent); color: var(--accent); background: var(--accent-soft); }
.precio-chart { border-radius: 8px; overflow: hidden; }
.precio-msg { padding: 40px 16px; text-align: center; color: var(--text-3); font-size: 13px; }
.precio-foot { margin-top: 10px; font-size: 11px; color: var(--text-3); text-align: center; }
.intel-tabs { display:flex; gap:6px; flex-wrap:wrap; margin-bottom:14px; }
.intel-tab { font-size:12px; padding:6px 13px; border-radius:8px; border:1px solid var(--line); background:var(--bg-2); color:var(--text-2); cursor:pointer; }
.intel-tab.active { border-color:var(--accent); color:var(--accent); background:var(--accent-soft); font-weight:600; }
.intel-scroll { overflow-x:auto; min-width:0; max-width:100%; -webkit-overflow-scrolling:touch; }
.muros-cols { display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr); gap:16px; margin-top:14px; }
.muros-cols > div { min-width:0; }
.intel-table { width:100%; border-collapse:collapse; font-size:12px; min-width:500px; }
.intel-table th { font-size:10.5px; color:var(--text-3); font-weight:500; padding:7px 10px; border-bottom:1px solid var(--line); text-align:left; white-space:nowrap; }
.intel-table td { padding:7px 10px; border-bottom:0.5px solid var(--line-soft,rgba(255,255,255,0.05)); white-space:nowrap; }
.intel-table tr:hover td { background:var(--bg-2); }
.intel-nota { font-size:11px; color:var(--text-3); margin-top:10px; padding:8px 12px; border-left:2px solid var(--line); line-height:1.5; }
.intel-loading { padding:60px; text-align:center; color:var(--text-3); font-size:13px; }

/* chart */
.chart { position: relative; width: 100%; }
.chart svg { display: block; overflow: visible; }
.ax { font-family: var(--mono); font-size: 9.5px; fill: var(--text-3); }
.ax.th { font-size: 9px; opacity: 0.85; }
.chart-tip { position: absolute; top: 6px; background: var(--bg-3); border: 1px solid var(--line); border-radius: 8px; padding: 7px 9px; pointer-events: none; min-width: 132px; box-shadow: 0 8px 24px rgba(0,0,0,.4); }
.ct-row { display: flex; align-items: center; gap: 7px; font-size: 11px; }
.ct-time { font-size: 10.5px; color: var(--ink-2); margin-bottom: 5px; padding-bottom: 4px; border-bottom: 1px solid var(--line-soft); letter-spacing: .02em; }
.ct-row + .ct-row { margin-top: 3px; }
.ct-dot { width: 7px; height: 7px; border-radius: 2px; }
.ct-lab { color: var(--text-2); flex: 1; }
.ct-val { color: var(--text); }

/* ranking */
.rank-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
.rank-head h3 { font-size: 13.5px; font-weight: 600; }
.rank-toggle { display: flex; gap: 3px; background: var(--bg-2); border: 1px solid var(--line-soft); border-radius: 8px; padding: 2px; }
.rank-toggle button { font-family: var(--font); font-size: 11px; color: var(--text-3); background: transparent; border: none; padding: 4px 11px; border-radius: 6px; cursor: pointer; }
.rank-toggle button.on { background: var(--bg-3); color: var(--text); }
.rank-list { display: flex; flex-direction: column; gap: 1px; }
.rank-row { position: relative; display: grid; grid-template-columns: 16px minmax(0, 1fr) 54px 66px; gap: 8px; align-items: center; padding: 8px 7px; border-radius: 6px; overflow: hidden; font-size: 12px; }
.rank-row + .rank-row { box-shadow: inset 0 1px 0 var(--line-soft); }
.rank-depth { position: absolute; left: 0; top: 2px; bottom: 2px; z-index: 0; border-radius: 5px; background: var(--tone-soft); }
.rank-pos, .rank-name, .rank-liq, .rank-price { position: relative; z-index: 1; }
.rank-pos { color: var(--text-3); font-size: 11px; text-align: center; }
.rank-name { color: var(--text); display: flex; align-items: center; gap: 5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.merch { color: var(--accent); font-size: 10px; }
.rank-liq { color: var(--text-2); text-align: right; }
.rank-price { color: var(--text); text-align: right; }
.rank-legend { display: grid; grid-template-columns: 16px minmax(0, 1fr) 54px 66px; gap: 8px; margin-top: 10px; padding: 0 7px; font-size: 9.5px; color: var(--text-3); text-transform: uppercase; letter-spacing: 0.06em; }
.rank-legend span:nth-child(3),.rank-legend span:nth-child(4){ text-align: right; }

/* order book */
.orderbook { grid-column: 1 / -1; }
.ob-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 22px; }
.ob-coltitle { font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--tone); margin-bottom: 8px; font-family: var(--mono); }
.ob-row { position: relative; display: grid; grid-template-columns: 0.9fr 1.3fr 0.9fr; gap: 8px; align-items: center; padding: 6px 8px; font-size: 12px; border-radius: 6px; overflow: hidden; }
.ob-depth { position: absolute; left: 0; top: 0; bottom: 0; z-index: 0; border-radius: 6px; }
.ob-price, .ob-name, .ob-amt { position: relative; z-index: 1; }
.ob-name { color: var(--text-2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ob-amt { color: var(--text-3); text-align: right; }

/* alert */
.alert { display: flex; align-items: center; gap: 12px; padding: 12px 18px; border-radius: 12px; background: var(--tone-soft); border: 1px solid var(--tone); color: var(--text); font-size: 13.5px; font-weight: 500; animation: slidein .4s ease; }
.alert-pulse { width: 9px; height: 9px; border-radius: 50%; background: var(--tone); box-shadow: 0 0 0 0 var(--tone); animation: pulse 1.4s infinite; }
@keyframes pulse { 0% { box-shadow: 0 0 0 0 color-mix(in oklch, var(--tone) 60%, transparent); } 70% { box-shadow: 0 0 0 8px transparent; } 100% { box-shadow: 0 0 0 0 transparent; } }
@keyframes slidein { from { opacity: 0; transform: translateY(-8px); } to { opacity: 1; transform: none; } }

/* stat cards (histórico) */
.stat-cards { display: grid; grid-template-columns: repeat(4, 1fr); gap: var(--gap); }
.statcard { border: 1px solid var(--line); border-radius: var(--card-r); background: var(--bg-1); padding: 16px 18px; border-top: 2px solid var(--tone); }
.statcard-label { font-size: 11px; color: var(--text-3); text-transform: uppercase; letter-spacing: 0.08em; }
.statcard-val { font-size: 26px; color: var(--tone); margin-top: 8px; font-weight: 500; }

/* heatmap */
.heat { display: grid; grid-template-columns: 42px 1fr; gap: 4px 8px; align-items: center; }
.heat-corner { } 
.heat-hours { display: grid; grid-template-columns: repeat(24, 1fr); font-family: var(--mono); font-size: 9px; color: var(--text-3); }
.heat-h { text-align: center; }
.heat-day { font-size: 11px; color: var(--text-2); font-family: var(--mono); }
.heat-rowcells { display: grid; grid-template-columns: repeat(24, 1fr); gap: 3px; }
.heat-cell { aspect-ratio: 1 / 0.7; border-radius: 3px; background: var(--bg-2); border: 1px solid var(--line-soft); transition: transform .1s; }
.heat-cell:hover { transform: scale(1.2); outline: 1px solid var(--text); z-index: 2; }
.heat-legend { display: flex; align-items: center; gap: 10px; margin-top: 14px; font-size: 11px; color: var(--text-3); }
.heat-scale { width: 120px; height: 8px; border-radius: 5px; background: linear-gradient(90deg, var(--sell), var(--warn), var(--buy)); }
.heat-tip { margin-left: auto; color: var(--text); }

/* footer */
.foot { display: flex; align-items: center; gap: 8px; justify-content: center; flex-wrap: wrap; padding: 26px 0 8px; font-size: 12px; color: var(--text-3); }
.foot-snap { color: var(--accent); font-size: 13px; }
.foot b { color: var(--text-2); }
.foot-sep { opacity: 0.5; }
.foot-demo { color: var(--text-3); opacity: 0.7; }

/* flash de precios */
@keyframes flashUp { 0% { color: var(--buy); } 100% { color: inherit; } }
@keyframes flashDown { 0% { color: var(--sell); } 100% { color: inherit; } }
[data-animate="on"] .num-up { animation: flashUp .55s ease-out; }
[data-animate="on"] .num-down { animation: flashDown .55s ease-out; }

/* ---------- Velocímetro de mercado ---------- */
.velocity {
  display: grid; grid-template-columns: auto 1fr auto; gap: clamp(16px, 3vw, 40px); align-items: center;
  border: 1px solid var(--line); border-radius: var(--card-r);
  background: radial-gradient(100% 180% at 100% 0%, var(--tone-soft), transparent 60%), var(--bg-1);
  padding: 16px var(--pad); position: relative; overflow: hidden;
}
.velocity::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 3px; background: var(--tone); }
.vel-main { display: flex; align-items: center; gap: 14px; }
.vel-icon { font-size: 26px; color: var(--tone); animation: velflow 1.6s linear infinite; }
@keyframes velflow { 0% { opacity: .3; transform: translateX(-3px); } 50% { opacity: 1; } 100% { opacity: .3; transform: translateX(3px); } }
.vel-label { font-size: 11px; color: var(--text-3); text-transform: uppercase; letter-spacing: 0.1em; white-space: nowrap; }
.vel-big { font-family: var(--mono); font-size: clamp(24px, 3vw, 32px); font-weight: 500; color: var(--tone); display: flex; align-items: baseline; gap: 8px; line-height: 1.15; white-space: nowrap; }
.vel-unit { font-size: 12px; color: var(--text-3); letter-spacing: 0; white-space: nowrap; }
.vel-eg { font-size: 13px; color: var(--text-2); }
.vel-ratio { color: var(--text-3); }
.vel-eg b { color: var(--text); }
.vel-meter { display: flex; align-items: center; gap: 16px; min-width: 230px; }
.vel-spark { width: 110px; }
.vel-level { min-width: 96px; }
.vel-nivel { font-family: var(--mono); font-size: 12px; letter-spacing: 0.1em; color: var(--tone); }
.vel-bar { height: 5px; border-radius: 3px; background: var(--bg-3); overflow: hidden; margin-top: 5px; }
.vel-bar-fill { display: block; height: 100%; border-radius: 3px; transition: width .4s ease; }

/* ======== DIRECCIÓN: contraste (cards con tinte fuerte) ======== */
[data-dir="contraste"] .sidecard {
  background: linear-gradient(180deg, var(--tone-soft), transparent 60%), var(--bg-1);
  border-color: color-mix(in oklch, var(--tone) 45%, var(--line));
}
[data-dir="contraste"] .maker-card { background: linear-gradient(180deg, var(--tone-soft), transparent), var(--bg-2); }
[data-dir="contraste"] .sc-pond-val, [data-dir="contraste"] .hero-estado { text-shadow: 0 0 30px var(--tone-soft); }

/* ======== DIRECCIÓN: calmo (más aire, menos líneas) ======== */
[data-dir="calmo"] { --gap: 22px; }
[data-dir="calmo"] .sidecard, [data-dir="calmo"] .maker, [data-dir="calmo"] .chart-card,
[data-dir="calmo"] .ranking, [data-dir="calmo"] .hero { background: var(--bg-1); border-color: var(--line-soft); }
[data-dir="calmo"] .sc-leader { background: transparent; border-color: var(--line-soft); }
[data-dir="calmo"] .hero-estado { font-size: clamp(30px, 5vw, 48px); }
[data-dir="calmo"] .sc-pond-val { font-size: clamp(34px, 4.4vw, 48px); }
[data-dir="calmo"] .hero::before, [data-dir="calmo"] .maker-card { }

/* ======== DIRECCIÓN: cockpit (denso, default) ======== */
[data-dir="cockpit"] .view { --gap: 12px; }

/* ======== DIRECCIÓN: retro (terminal CRT 90s/2000s) ======== */
[data-dir="retro"] {
  --bg: #02080d; --bg-1: #051410; --bg-2: #07190e; --bg-3: #0a2414;
  --line: #1c6b3c; --line-soft: #114a28;
  --text: #bdffd2; --text-2: #5fd98a; --text-3: #3c9a62;
  --buy: #39f58a; --sell: #ff5d5d; --warn: #ffd23a; --warn-low: #ff9d3a;
  --font: "VT323", ui-monospace, monospace; --mono: "VT323", ui-monospace, monospace;
  --card-r: 2px;
}
[data-dir="retro"] body { font-size: 17px; letter-spacing: 0.015em; }
[data-dir="retro"] .app { background: radial-gradient(120% 80% at 50% -10%, color-mix(in oklch, var(--buy) 10%, transparent), transparent 60%); }
[data-dir="retro"] body::before {
  content: ""; position: fixed; inset: 0; z-index: 60; pointer-events: none;
  background: repeating-linear-gradient(0deg, rgba(0,0,0,0.22) 0, rgba(0,0,0,0.22) 1px, transparent 2px, transparent 3px);
}
[data-dir="retro"] body::after {
  content: ""; position: fixed; inset: 0; z-index: 59; pointer-events: none;
  background: radial-gradient(125% 100% at 50% 50%, transparent 55%, rgba(0,0,0,0.6));
  animation: crtflicker 7s infinite steps(60);
}
@keyframes crtflicker { 0%,100% { opacity: 1; } 47% { opacity: .94; } 49% { opacity: 1; } 92% { opacity: .97; } }
[data-dir="retro"] .tnum { letter-spacing: 0.02em; }
[data-dir="retro"] .brand-name, [data-dir="retro"] .sc-role, [data-dir="retro"] .hero-sub,
[data-dir="retro"] .card-head h3, [data-dir="retro"] .mc-title, [data-dir="retro"] .vel-eg,
[data-dir="retro"] .rank-head h3 { text-transform: uppercase; letter-spacing: 0.06em; }
[data-dir="retro"] .brand-sub { letter-spacing: 0.2em; }
[data-dir="retro"] .hero-estado, [data-dir="retro"] .sc-pond-val, [data-dir="retro"] .hm-val.big,
[data-dir="retro"] .mc-price, [data-dir="retro"] .vel-big, [data-dir="retro"] .statcard-val,
[data-dir="retro"] .spine-val { text-shadow: 0 0 7px currentColor, 0 0 16px color-mix(in oklch, currentColor 50%, transparent); }
[data-dir="retro"] .hero-estado::after { content: "_"; margin-left: 6px; animation: blink 1.1s steps(1) infinite; }
@keyframes blink { 50% { opacity: 0; } }
[data-dir="retro"] .live-txt { animation: blink 1s steps(1) infinite; }
[data-dir="retro"] .hero, [data-dir="retro"] .sidecard, [data-dir="retro"] .maker,
[data-dir="retro"] .chart-card, [data-dir="retro"] .ranking, [data-dir="retro"] .orderbook,
[data-dir="retro"] .filters, [data-dir="retro"] .velocity, [data-dir="retro"] .statcard,
[data-dir="retro"] .maker-card, [data-dir="retro"] .sc-leader, [data-dir="retro"] .spine-pill,
[data-dir="retro"] .market-chip, [data-dir="retro"] .verif, [data-dir="retro"] .alert {
  border-radius: 2px;
  box-shadow: inset 1.5px 1.5px 0 color-mix(in oklch, var(--buy) 14%, transparent), inset -1.5px -1.5px 0 rgba(0,0,0,0.6);
}
[data-dir="retro"] .brand-mark {
  border-radius: 2px; background: var(--bg-2); color: var(--buy);
  box-shadow: inset 1.5px 1.5px 0 #2fa863, inset -1.5px -1.5px 0 #000; text-shadow: 0 0 6px var(--buy);
}
[data-dir="retro"] .sc-badge { border-radius: 2px; }
[data-dir="retro"] .tab { border-radius: 2px; text-transform: uppercase; letter-spacing: 0.05em; }
[data-dir="retro"] .tab.active { background: var(--buy); color: #021006; box-shadow: none; border-color: var(--buy); }
[data-dir="retro"] .rank-toggle button.on, [data-dir="retro"] .btn-apply.dirty { background: var(--buy); color: #021006; border-radius: 2px; }
[data-dir="retro"] .btn-apply.dirty { border-color: var(--buy); }
[data-dir="retro"] .f-item input, [data-dir="retro"] .f-item select, [data-dir="retro"] .btn-reset,
[data-dir="retro"] .switch { border-radius: 2px; }
[data-dir="retro"] .switch[aria-checked="true"] { background: var(--buy); border-color: var(--buy); }
[data-dir="retro"] .hero::before, [data-dir="retro"] .velocity::before { box-shadow: 0 0 10px var(--tone); }
[data-dir="retro"] .heat-cell { border-radius: 1px; }

/* ---------- Filtros del mercado ---------- */
.filters { border: 1px solid var(--line); border-radius: var(--card-r); background: var(--bg-1); overflow: hidden; }
.filters-head { width: 100%; display: flex; align-items: center; gap: 12px; padding: 14px var(--pad); background: transparent; border: none; cursor: pointer; text-align: left; font-family: var(--font); }
.fh-title { font-size: 13.5px; font-weight: 600; color: var(--text); }
.fh-note { font-size: 11px; color: var(--text-3); }
.fh-chips { display: flex; gap: 6px; flex-wrap: wrap; margin-left: 4px; }
.fchip { font-family: var(--mono); font-size: 10px; color: var(--text-2); background: var(--bg-2); border: 1px solid var(--line-soft); padding: 2px 7px; border-radius: 6px; }
.fh-arrow { margin-left: auto; color: var(--text-3); transition: transform .2s; }
.fh-arrow[data-open="true"] { transform: rotate(180deg); }
.filters-body { padding: 0 var(--pad) var(--pad); display: flex; flex-direction: column; gap: 14px; border-top: 1px solid var(--line-soft); padding-top: 16px; }

.verif { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 14px 16px; border-radius: 12px; background: var(--bg-2); border: 1px solid var(--line); transition: border-color .2s, background .2s; cursor: pointer; }
.verif.on { border-color: color-mix(in oklch, var(--accent) 55%, var(--line)); background: var(--accent-soft); }
.verif-title { font-size: 13.5px; font-weight: 600; color: var(--text); display: flex; align-items: center; gap: 8px; }
.verif-badge { font-family: var(--mono); font-size: 10px; color: var(--accent); border: 1px solid color-mix(in oklch, var(--accent) 45%, transparent); padding: 1px 6px; border-radius: 5px; font-weight: 500; }
.verif-sub { font-size: 11.5px; color: var(--text-3); margin-top: 3px; display: block; }
.switch { flex: none; width: 46px; height: 26px; border-radius: 999px; background: var(--bg-3); border: 1px solid var(--line); position: relative; cursor: pointer; transition: background .2s; }
.switch[aria-checked="true"] { background: var(--accent); border-color: var(--accent); }
.switch-knob { position: absolute; top: 2px; left: 2px; width: 20px; height: 20px; border-radius: 50%; background: var(--text); transition: transform .2s; box-shadow: 0 1px 4px rgba(0,0,0,.4); }
.switch[aria-checked="true"] .switch-knob { transform: translateX(20px); }

.filters-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
.f-item label { display: block; font-size: 11px; color: var(--text-3); margin-bottom: 6px; }
.f-item input, .f-item select { width: 100%; background: var(--bg-2); border: 1px solid var(--line); color: var(--text); padding: 9px 11px; border-radius: 9px; font-family: var(--mono); font-size: 13px; }
.f-item input:focus, .f-item select:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
.f-item select option { background: var(--bg-2); }

.filters-info { display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--text-2); }
.fi-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--tone); }
.filters-info b { color: var(--text); }
.filters-actions { display: flex; gap: 10px; justify-content: flex-end; }
.btn-reset { font-family: var(--font); font-size: 12.5px; color: var(--text-2); background: transparent; border: 1px solid var(--line); padding: 9px 16px; border-radius: 9px; cursor: pointer; }
.btn-reset:hover { color: var(--text); border-color: var(--text-3); }
.btn-apply { font-family: var(--font); font-size: 12.5px; font-weight: 600; color: var(--text-3); background: var(--bg-2); border: 1px solid var(--line); padding: 9px 18px; border-radius: 9px; cursor: default; }
.btn-apply.dirty { color: var(--bg); background: var(--accent); border-color: var(--accent); cursor: pointer; }
.btn-apply.dirty:hover { filter: brightness(1.08); }

/* ---------- Responsive ---------- */
@media (max-width: 880px) {
  .market { grid-template-columns: 1fr; }
  .spine { flex-direction: row; min-width: 0; padding: 4px 0; }
  .spine-line { width: auto; height: 1px; flex: 1; background: linear-gradient(90deg, transparent, var(--line)); }
  .spine-line:last-child { background: linear-gradient(90deg, var(--line), transparent); }
  .spine-pill { margin: 0 10px; display: flex; align-items: baseline; gap: 8px; }
  .tr-bottom { grid-template-columns: 1fr; }
  .muros-cols { grid-template-columns: 1fr; }
  .stat-cards { grid-template-columns: repeat(2, 1fr); }
  .ob-grid { grid-template-columns: 1fr; gap: 16px; }
  .filters-grid { grid-template-columns: repeat(2, 1fr); }
}
@media (max-width: 640px) {
  .topbar { flex-wrap: wrap; gap: 12px; }
  .market-chip { order: 3; }
  .topbar-right { width: 100%; justify-content: space-between; }
  .hero-main { flex-direction: column; align-items: flex-start; gap: 16px; }
  .hero-metrics { width: 100%; justify-content: space-between; gap: 12px; }
  .maker-grid { grid-template-columns: 1fr; }
  .hm-val.big { font-size: 30px; }
  .heat { grid-template-columns: 34px 1fr; }
  .velocity { grid-template-columns: 1fr; gap: 14px; }
  .vel-meter { min-width: 0; justify-content: space-between; }
  .backup-grid { grid-template-columns: 1fr; }
  .app { padding-bottom: 28px; }
}

/* ---------- Intel explain + rotación ---------- */
.intel-explain { font-size:12px; color:var(--text-2); line-height:1.6; margin-top:14px; padding:12px 14px; border-radius:10px; background:var(--bg-2); border:1px solid var(--line-soft); }
.intel-explain b { color:var(--text); }

/* ---------- Backup banner & panel ---------- */
.backup-banner { display:flex; align-items:center; gap:12px; margin:8px 0 0; padding:8px 14px; border-radius:10px; background:var(--bg-1); border:1px solid var(--line-soft); border-left:3px solid var(--warn); font-size:12.5px; color:var(--text-2); }
.bb-dot { width:7px; height:7px; border-radius:50%; background:var(--warn); flex:none; }
.bb-txt { flex:1; }
.bb-go { font-family:var(--font); font-size:12.5px; font-weight:600; color:var(--bg); background:var(--warn); border:none; padding:7px 14px; border-radius:8px; cursor:pointer; }
.bb-x { background:transparent; border:none; color:var(--text-3); cursor:pointer; font-size:14px; line-height:1; }
.backup-wrap { max-width:680px; }
.backup-grid { display:grid; grid-template-columns:repeat(3,1fr); gap:12px; margin:16px 0 4px; }
.backup-actions { display:flex; align-items:center; gap:14px; margin-top:12px; flex-wrap:wrap; }
.backup-msg { font-size:12.5px; color:var(--buy); margin-top:12px; }
.backup-last { font-size:12px; color:var(--text-3); }
.backup-help { font-size:12px; color:var(--text-2); line-height:1.65; margin-top:20px; padding:14px 16px; border-radius:10px; background:var(--bg-2); border:1px solid var(--line-soft); }
.backup-help code { font-family:var(--mono); background:var(--bg-3); padding:1px 6px; border-radius:5px; font-size:11.5px; color:var(--text); }

</style>
</head>
<body>
<div id="root"></div>
<script crossorigin src="https://cdnjs.cloudflare.com/ajax/libs/react/18.2.0/umd/react.production.min.js"></script>
<script crossorigin src="https://cdnjs.cloudflare.com/ajax/libs/react-dom/18.2.0/umd/react-dom.production.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/babel-standalone/7.23.2/babel.min.js"></script>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<script>
/* ============================================================
   Unión Austral · P2P Monitor — CONFIGURACIÓN
   ------------------------------------------------------------
   Cambiá SOLO este archivo para pasar de demo a data real.

   mode:    "live"  → usa data simulada (no necesita backend)
            "live"  → consume tu API Flask real

   baseUrl: ""                              → mismo dominio que sirve esta página
                                              (recomendado: servir desde tu Flask)
            "https://tu-app.up.railway.app" → si la página vive en otro dominio
                                              (requiere CORS en el backend, ver abajo)

   pollMs:  cada cuánto refresca (ms). 30000 = 30 s.
            Tu backend guarda cada INTERVALO_MIN (5 min); sondear más
            seguido sólo trae el último snapshot ya calculado, no
            golpea a Binance.
   ============================================================ */
window.P2P_CONFIG = {
  mode: "live",
  baseUrl: "",
  pollMs: 30000,
  intervaloMin: 2,   // debe coincidir con INTERVALO_MIN del backend (para la velocidad)
};

/* POST autenticado: si el backend tiene APP_TOKEN (env var en Railway), los POST
   sensibles piden el header X-App-Token. Este helper lo agrega desde localStorage;
   ante un 401 lo pide UNA vez con prompt() y reintenta. Sin APP_TOKEN en el
   backend, funciona igual que un fetch comun. */
window.P2P_AUTH = {
  post: function (url, body) {
    var mk = function (tk) {
      var h = { "Content-Type": "application/json" };
      if (tk) h["X-App-Token"] = tk;
      return fetch(url, { method: "POST", headers: h, body: body ? JSON.stringify(body) : undefined });
    };
    var tk = "";
    try { tk = localStorage.getItem("ua_app_token") || ""; } catch (e) {}
    return mk(tk).then(function (r) {
      if (r.status !== 401) return r;
      var nuevo = window.prompt("Token de administración (APP_TOKEN de Railway):");
      if (!nuevo) return r;
      try { localStorage.setItem("ua_app_token", nuevo); } catch (e) {}
      return mk(nuevo);
    });
  }
};

</script>
<script>
/* ============================================================
   Unión Austral · P2P Monitor — motor de data en vivo (demo)
   Emite snapshots con LOS MISMOS nombres de campo que /api/estado
   del backend Flask, más detalle por anunciante (snapshots_detalle).
   Para producción: reemplazá createEngine() por polling a tus
   endpoints reales (/api/estado, /api/historial, /api/heatmap).
   Mercado: USDT / CLP · Binance P2P · TZ America/Santiago
   ============================================================ */
(function () {
  const NAMES = [
    "SpaMaig", "Jimmy2680", "CryptoSurCL", "BilleteraCL", "NodoP2P",
    "TetherKing", "PesoFuerte_OK", "DigitalCLP", "AndesExchange", "PacificoPay",
    "FastUSDT", "ManoAMano", "RioCripto", "CordilleraP2P", "QuickTether",
    "SantiagoCoin", "AtacamaCash", "ElCambista", "USDT_Express", "MercadoSur",
  ];
  const MERCHANTS = new Set(["SpaMaig", "Jimmy2680", "TetherKing", "AndesExchange", "DigitalCLP", "USDT_Express"]);

  const rnd = (a, b) => a + Math.random() * (b - a);
  const pick = (arr) => arr[Math.floor(Math.random() * arr.length)];

  const fmtPrice = (n) => "$" + Number(n).toLocaleString("es-CL", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const fmtNum = (n) => Math.round(n).toLocaleString("es-CL");
  const fmtPct = (n) => Number(n).toFixed(2) + "%";

  const COMISION_BN = 0.002;     // 0.2% por lado
  const ALERTA_SPREAD = 0.8;     // umbral MUY APTO
  const SPREAD_MINIMO = 0.2;     // umbral APTO

  // detalle por lado: top-N anunciantes (como snapshots_detalle)
  function buildDetalle(side, n, best, worst, leaderName) {
    const rows = [];
    const names = NAMES.slice();
    for (let i = 0; i < n; i++) {
      const t = i / Math.max(1, n - 1);
      let precio = best + (worst - best) * Math.pow(t, 1.2) + rnd(-0.5, 0.5);
      const disponible = Math.round(rnd(900, 16000) * (1 - t * 0.45));
      const anunciante = i === 0 ? leaderName : pick(names);
      rows.push({
        posicion: i + 1,
        anunciante,
        precio: Math.round(precio * 100) / 100,
        disponible,
        completadas: Math.round(rnd(120, 5400)),
        tasa_exito: Math.round(rnd(94, 100) * 10) / 10,
        es_merchant: MERCHANTS.has(anunciante),
        velocidad: Math.round(rnd(40, 900)),    // USDT/min consumidos (de obtener_velocidad_anunciante)
      });
    }
    rows.sort((a, b) => (side === "buy" ? a.precio - b.precio : b.precio - a.precio));
    rows.forEach((r, i) => (r.posicion = i + 1));
    return rows;
  }

  function ponderado(rows) {
    let v = 0, w = 0;
    rows.forEach((r) => { v += r.precio * r.disponible; w += r.disponible; });
    return w ? v / w : 0;
  }

  function clasificar(spread_pond_pct) {
    if (spread_pond_pct >= ALERTA_SPREAD) return { estado: "MUY APTO", color: "green" };
    if (spread_pond_pct >= SPREAD_MINIMO) return { estado: "APTO", color: "yellow" };
    if (spread_pond_pct >= 0) return { estado: "ESTRECHO", color: "orange" };
    return { estado: "NO APTO", color: "red" };
  }

  // genera un snapshot con los nombres EXACTOS del backend
  function buildSnapshot(prev) {
    const drift = (base, amt) => base + rnd(-amt, amt);
    const buyLeader = drift(prev ? prev.mejor_vendedor_tab_compra : 918.0, 0.8);
    const buyWorst = drift(prev ? prev.peor_vendedor_tab_compra : 933.0, 1.0);
    const sellLeader = drift(prev ? prev.mejor_comprador_tab_venta : 915.52, 0.8);
    const sellWorst = drift(prev ? prev.peor_comprador_tab_venta : 909.0, 1.0);

    const dc = buildDetalle("buy", 16, buyLeader, buyWorst, "SpaMaig");
    const dv = buildDetalle("sell", 9, sellLeader, sellWorst, "Jimmy2680");

    const pond_tc = Math.round(ponderado(dc) * 100) / 100;
    const pond_tv = Math.round(ponderado(dv) * 100) / 100;
    const lider_tc = dc[0], lider_tv = dv[0];

    const spread_abs = Math.round((lider_tc.precio - lider_tv.precio) * 100) / 100;
    const spread_pct = Math.round((spread_abs / lider_tv.precio) * 10000) / 100;
    const spread_pond_abs = Math.round((pond_tc - pond_tv) * 100) / 100;
    const spread_pond_pct = Math.round((spread_pond_abs / pond_tv) * 10000) / 100;
    const ganancia_neta_pct = Math.round((spread_pond_pct - COMISION_BN * 2 * 100) * 100) / 100;

    const liq_tc = dc.reduce((s, r) => s + r.disponible, 0);
    const liq_tv = dv.reduce((s, r) => s + r.disponible, 0);
    const cls = clasificar(spread_pond_pct);
    const d = new Date();

    return {
      timestamp: d.toLocaleString("sv-SE").replace("T", " "),
      hora: d.getHours(),
      dia: ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"][d.getDay()],
      mejor_vendedor_tab_compra: lider_tc.precio,
      peor_vendedor_tab_compra: dc[dc.length - 1].precio,
      precio_pond_tab_compra: pond_tc,
      lider_tab_compra: lider_tc.anunciante,
      mejor_comprador_tab_venta: lider_tv.precio,
      peor_comprador_tab_venta: dv[dv.length - 1].precio,
      precio_pond_tab_venta: pond_tv,
      lider_tab_venta: lider_tv.anunciante,
      spread_abs, spread_pct, spread_pond_abs, spread_pond_pct,
      liq_tab_compra: liq_tc, liq_tab_venta: liq_tv,
      n_tab_compra: dc.length, n_tab_venta: dv.length,
      precio_maker_vender: Math.round((lider_tc.precio - 0.01) * 100) / 100,
      precio_maker_comprar: Math.round((lider_tv.precio + 0.01) * 100) / 100,
      ganancia_neta_pct,
      estado: cls.estado, color: cls.color,
      detalle_compra: dc, detalle_venta: dv,
    };
  }

  // heatmap: promedio de spread_pond_pct por hora/día (como /api/heatmap)
  function buildHeatmap() {
    const dias = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
    const out = [];
    dias.forEach((dia, di) => {
      for (let h = 0; h < 24; h++) {
        // patrón: spreads más altos de madrugada/noche, más bajos al mediodía
        const base = 0.85 - 0.6 * Math.cos(((h - 4) / 24) * Math.PI * 2);
        const wknd = di >= 5 ? 0.25 : 0;
        const avg = Math.max(0.05, base + wknd + rnd(-0.18, 0.18));
        out.push({ hora: h, dia, avg_spread: Math.round(avg * 100) / 100, muestras: Math.round(rnd(20, 60)) });
      }
    });
    return out;
  }

  // Aplica filtros sobre el detalle y RECALCULA todo (espejo de parsear_y_filtrar + analizar)
  function applyFilters(snap, cfg) {
    if (!cfg) return snap;
    // si el snapshot no trae detalle real, no se puede filtrar/recalcular
    if (!Array.isArray(snap.detalle_compra) || snap.detalle_compra.length < 2 ||
        !Array.isArray(snap.detalle_venta) || snap.detalle_venta.length < 2) {
      return Object.assign({}, snap, { _filtro: null });
    }
    const pasa = (r) =>
      r.disponible >= (cfg.minUsdt || 0) &&
      r.completadas >= (cfg.minOrd || 0) &&
      r.tasa_exito >= (cfg.minTasa || 0) &&
      (!cfg.soloVerificados || r.es_merchant);

    const totalC = snap.detalle_compra.length, totalV = snap.detalle_venta.length;
    let dc = snap.detalle_compra.filter(pasa);
    let dv = snap.detalle_venta.filter(pasa);
    const pasanC = dc.length, pasanV = dv.length;
    // evitar división por cero: si un lado queda vacío, dejamos el mejor original
    if (!dc.length) dc = snap.detalle_compra.slice(0, 1);
    if (!dv.length) dv = snap.detalle_venta.slice(0, 1);
    // Sanity anti-glitch: descarta ads >4% de la mediana (un scam/mistype no debe ser lider)
    const sanos = (rows) => {
      if (rows.length < 4) return rows;
      const ps = rows.map((r) => r.precio).slice().sort((a, b) => a - b);
      const med = ps[Math.floor(ps.length / 2)];
      if (!med) return rows;
      const limpio = rows.filter((r) => Math.abs(r.precio - med) / med <= 0.04);
      return limpio.length >= 3 ? limpio : rows;
    };
    dc = sanos(dc); dv = sanos(dv);
    dc = dc.slice().sort((a, b) => a.precio - b.precio).map((r, i) => ({ ...r, posicion: i + 1 }));
    dv = dv.slice().sort((a, b) => b.precio - a.precio).map((r, i) => ({ ...r, posicion: i + 1 }));

    const pond = (rows) => { let v = 0, w = 0; rows.forEach((r) => { v += r.precio * r.disponible; w += r.disponible; }); return w ? v / w : 0; };
    const r2 = (n) => Math.round(n * 100) / 100;
    // Cabecera del libro: el ponderado, el spread y la liquidez se miden SOLO
    // sobre las primeras CAB posiciones (igual que el backend, ANALISIS_TOP),
    // para no diluir el spread con los precios profundos del top80.
    const CAB = parseInt(snap.analisis_top) || 20;
    const cabC = dc.slice(0, CAB);
    const cabV = dv.slice(0, CAB);
    // Banda anti-outliers: pondera solo lo cercano al líder (libros finos con ballenas lejos del precio)
    const bandaP = (parseFloat(snap.banda_ponderado_pct) || 2) / 100;
    const wC = cabC.filter((r) => r.precio <= cabC[0].precio * (1 + bandaP));
    const wV = cabV.filter((r) => r.precio >= cabV[0].precio * (1 - bandaP));
    const useC = wC.length ? wC : cabC;
    const useV = wV.length ? wV : cabV;
    const pond_tc = r2(pond(useC)), pond_tv = r2(pond(useV));
    const lider_tc = cabC[0], lider_tv = cabV[0];
    const spread_abs = r2(lider_tc.precio - lider_tv.precio);
    const spread_pct = Math.round((spread_abs / lider_tv.precio) * 10000) / 100;
    const spread_pond_abs = r2(pond_tc - pond_tv);
    const spread_pond_pct = Math.round((spread_pond_abs / pond_tv) * 10000) / 100;
    const ganancia_neta_pct = r2(spread_pond_pct - COMISION_BN * 2 * 100);
    const liq_tc = useC.reduce((s, r) => s + r.disponible, 0);
    const liq_tv = useV.reduce((s, r) => s + r.disponible, 0);
    const cls = clasificar(spread_pond_pct);

    return {
      ...snap,
      precio_pond_tab_compra: pond_tc, precio_pond_tab_venta: pond_tv,
      mejor_vendedor_tab_compra: lider_tc.precio, peor_vendedor_tab_compra: useC[useC.length - 1].precio,
      mejor_comprador_tab_venta: lider_tv.precio, peor_comprador_tab_venta: useV[useV.length - 1].precio,
      lider_tab_compra: lider_tc.anunciante, lider_tab_venta: lider_tv.anunciante,
      spread_abs, spread_pct, spread_pond_abs, spread_pond_pct, ganancia_neta_pct,
      liq_tab_compra: liq_tc, liq_tab_venta: liq_tv,
      n_tab_compra: useC.length, n_tab_venta: useV.length,
      precio_maker_vender: r2(lider_tc.precio - 0.01), precio_maker_comprar: r2(lider_tv.precio + 0.01),
      estado: cls.estado, color: cls.color,
      detalle_compra: dc, detalle_venta: dv,
      _filtro: { pasanC, totalC, pasanV, totalV },
    };
  }

  const FILTROS_DEFAULT = { minUsdt: 200, minOrd: 100, minTasa: 90, soloVerificados: false, intervalo: 5 };

  const COLOR_TONE = { green: "buy", yellow: "warn", orange: "warn-low", red: "sell" };

  function createEngine({ cycleMs = 30000 } = {}) {
    let snap = buildSnapshot(null);
    const history = [];
    let seed = buildSnapshot(null);
    const now = Date.now();
    const step = 5 * 60 * 1000; // 5 min reales entre snapshots
    for (let i = 64; i >= 1; i--) {
      seed = buildSnapshot(seed);
      history.push(liteSnap(seed, now - i * step));
    }
    history.push(liteSnap(snap, now));
    const heatmap = buildHeatmap();
    let count = 9601;

    // --- velocidad de mercado (USDT absorbidos / min) ---
    // Deriva del consumo de 'disponible' de los anuncios entre ciclos.
    let vel = 165; // USDT/min
    const velHistory = [];
    for (let i = 48; i >= 0; i--) { vel = clampVel(vel + rnd(-22, 22)); velHistory.push(vel); }
    let velState = computeVel(velHistory[velHistory.length - 1], velHistory);

    const subs = new Set();
    let cycleStart = Date.now();

    function emit(type) {
      subs.forEach((fn) => fn({ snap, history, heatmap, count, vel: velState, cycleStart, cycleMs, type }));
    }
    function tick() {
      snap = buildSnapshot(snap);
      count += 1;
      history.push(liteSnap(snap, Date.now()));
      if (history.length > 80) history.shift();
      vel = clampVel(vel + rnd(-26, 26) + (snap.spread_pond_pct > 1 ? 8 : -4));
      velHistory.push(vel);
      if (velHistory.length > 60) velHistory.shift();
      velState = computeVel(vel, velHistory);
      cycleStart = Date.now();
      emit("cycle");
    }
    const id = setInterval(tick, cycleMs);
    return {
      get state() { return { snap, history, heatmap, count, vel: velState, cycleStart, cycleMs }; },
      subscribe(fn) { subs.add(fn); fn({ snap, history, heatmap, count, vel: velState, cycleStart, cycleMs, type: "init" }); return () => subs.delete(fn); },
      forceCycle: tick,
      stop() { clearInterval(id); },
    };
  }

  function clampVel(v) { return Math.max(45, Math.min(420, v)); }
  function computeVel(vel, hist) {
    const usdt_min = Math.round(vel);
    const vol_15m = Math.round(vel * 15);
    // base demo fija para simular el ratio contra ritmo normal
    const base = 165;
    const ratio = Math.round((vel / base) * 100) / 100;
    let nivel, tone;
    if (ratio < 0.5) { nivel = "TRANQUILO"; tone = "warn-low"; }
    else if (ratio < 1.3) { nivel = "NORMAL"; tone = "warn"; }
    else if (ratio < 2.2) { nivel = "ACTIVO"; tone = "buy"; }
    else { nivel = "MUY ACTIVO"; tone = "sell"; }
    return { usdt_min, vol_15m, nivel, tone, history: hist.slice(), pct: Math.min(1, ratio / 3), ratio };
  }

  function liteSnap(s, ts) {
    return {
      ts, timestamp: s.timestamp,
      spread_pond_pct: s.spread_pond_pct, spread_pct: s.spread_pct,
      ganancia_neta_pct: s.ganancia_neta_pct,
      liq_tab_compra: s.liq_tab_compra, liq_tab_venta: s.liq_tab_venta,
      precio_pond_tab_compra: s.precio_pond_tab_compra, precio_pond_tab_venta: s.precio_pond_tab_venta,
    };
  }

  window.P2P = { createEngine, buildSnapshot, buildHeatmap, clasificar, applyFilters, FILTROS_DEFAULT, COLOR_TONE, fmtPrice, fmtNum, fmtPct, NAMES, ALERTA_SPREAD, SPREAD_MINIMO };
})();

</script>
<script>
/* ============================================================
   Unión Austral · P2P Monitor — MOTOR EN VIVO
   Sondea tu API Flask real y emite con la MISMA interfaz que
   createEngine() (demo). Mapea los campos del backend 1:1.
   Si la API falla, mantiene el último dato bueno y reintenta.
   ============================================================ */
(function () {
  const P = window.P2P;

  const num = (v, d = 0) => (v == null || isNaN(+v) ? d : +v);

  // Velocidad de mercado: cuánta liquidez se absorbe por minuto.
  // Se promedia sobre una ventana para evitar el ruido de un solo ciclo
  // (un anunciante publicando/retirando un anuncio grande no debe disparar
  // el indicador). Los umbrales se calibran sobre la mediana reciente para
  // que "rápido/lento" sea relativo al propio mercado, no a un número fijo.
  function calcVelocidad(history, intervaloMin) {
    if (!history || history.length < 3) return null;
    const serie = [];
    for (let i = 1; i < history.length; i++) {
      const a = history[i - 1], b = history[i];
      // Solo contamos las CAÍDAS de liquidez (absorción real), no las subidas
      // (que son anunciantes agregando oferta). Cap para descartar outliers
      // extremos de alguien publicando/retirando un anuncio gigante.
      const dCompra = Math.max(0, a.liq_tab_compra - b.liq_tab_compra);
      const dVenta = Math.max(0, a.liq_tab_venta - b.liq_tab_venta);
      const absorbido = (dCompra + dVenta) / Math.max(1, intervaloMin);
      serie.push(absorbido);
    }
    if (!serie.length) return null;

    // Velocidad actual: promedio de los últimos 3 ciclos (suaviza el ruido)
    const recientes = serie.slice(-3);
    const velActual = recientes.reduce((a, b) => a + b, 0) / recientes.length;

    // Línea base: mediana de la última hora-y-media (~45 ciclos a 2 min),
    // descartando outliers, para comparar contra el ritmo habitual.
    const ventana = serie.slice(-45).filter((v) => v < 8000).sort((a, b) => a - b);
    const mediana = ventana.length ? ventana[Math.floor(ventana.length / 2)] : velActual;
    const base = Math.max(40, mediana); // piso para evitar divisiones raras

    // Ratio respecto a la base: 1.0 = ritmo normal del mercado
    const ratio = velActual / base;
    let nivel, tone;
    if (ratio < 0.5) { nivel = "TRANQUILO"; tone = "warn-low"; }
    else if (ratio < 1.3) { nivel = "NORMAL"; tone = "warn"; }
    else if (ratio < 2.2) { nivel = "ACTIVO"; tone = "buy"; }
    else { nivel = "MUY ACTIVO"; tone = "sell"; }

    const usdt_min = Math.round(velActual);
    // La barra muestra el ratio (0 a ~3x), no un absoluto
    const pct = Math.min(1, ratio / 3);
    return {
      usdt_min,
      vol_15m: Math.round(velActual * 15),
      nivel, tone,
      history: serie.slice(-48),
      pct,
      ratio: Math.round(ratio * 100) / 100,
    };
  }

  // Normaliza un row del historial al shape liviano que usan los gráficos
  function liteFromRow(r) {
    return {
      ts: Date.parse((r.timestamp || "").replace(" ", "T")) || Date.now(),
      timestamp: r.timestamp || "",
      spread_pond_pct: num(r.spread_pond_pct),
      spread_pct: num(r.spread_pct),
      ganancia_neta_pct: num(r.ganancia_neta_pct),
      liq_tab_compra: num(r.liq_tab_compra),
      liq_tab_venta: num(r.liq_tab_venta),
      precio_pond_tab_compra: num(r.precio_pond_tab_compra),
      precio_pond_tab_venta: num(r.precio_pond_tab_venta),
    };
  }

  // Asegura que el snapshot tenga detalle_compra/detalle_venta.
  // Si tu /api/estado todavía no los devuelve, sintetiza una fila
  // con el líder para que ranking/libro/filtros no rompan.
  function ensureDetalle(s) {
    const mk = (name, precio, liq, merchant) => ([{
      posicion: 1, anunciante: name || "—", precio: num(precio),
      disponible: num(liq), completadas: 0, tasa_exito: 100,
      es_merchant: !!merchant, velocidad: 0,
    }]);
    if (!Array.isArray(s.detalle_compra) || !s.detalle_compra.length)
      s.detalle_compra = mk(s.lider_tab_compra, s.mejor_vendedor_tab_compra, s.liq_tab_compra);
    if (!Array.isArray(s.detalle_venta) || !s.detalle_venta.length)
      s.detalle_venta = mk(s.lider_tab_venta, s.mejor_comprador_tab_venta, s.liq_tab_venta);
    return s;
  }

  function normSnap(raw) {
    const s = Object.assign({}, raw);
    // tipar numéricos que vienen como string/Decimal del backend
    [
      "mejor_vendedor_tab_compra", "peor_vendedor_tab_compra", "precio_pond_tab_compra",
      "mejor_comprador_tab_venta", "peor_comprador_tab_venta", "precio_pond_tab_venta",
      "spread_abs", "spread_pct", "spread_pond_abs", "spread_pond_pct",
      "liq_tab_compra", "liq_tab_venta", "n_tab_compra", "n_tab_venta",
      "precio_maker_vender", "precio_maker_comprar", "ganancia_neta_pct",
    ].forEach((k) => { if (s[k] != null) s[k] = num(s[k]); });
    if (!s.estado) { const c = P.clasificar(num(s.spread_pond_pct)); s.estado = c.estado; s.color = c.color; }
    return ensureDetalle(s);
  }

  async function getJSON(url) {
    const res = await fetch(url, { headers: { "Accept": "application/json" } });
    if (!res.ok) throw new Error(url + " → " + res.status);
    return res.json();
  }

  // Velocidad real de mercado: suma del consumo por anunciante (campo velocidad,
  // ya en USDT/min). Excluye el ruido de reposicionamiento del proxy de liquidez.
  function marketVelNow(snap) {
    const sumVel = (rows) => (rows || []).reduce((s, r) => s + (Number(r.velocidad) > 0 ? Number(r.velocidad) : 0), 0);
    return sumVel(snap.detalle_compra) + sumVel(snap.detalle_venta);
  }
  function computeVelReal(vNow, hist) {
    const usdt_min = Math.round(vNow);
    const ventana = hist.slice(-45).filter((v) => v > 0 && v < 50000).sort((a, b) => a - b);
    const mediana = ventana.length ? ventana[Math.floor(ventana.length / 2)] : vNow;
    const base = Math.max(40, mediana);
    const ratio = base ? vNow / base : 1;
    let nivel, tone;
    if (ratio < 0.5) { nivel = "TRANQUILO"; tone = "warn-low"; }
    else if (ratio < 1.3) { nivel = "NORMAL"; tone = "warn"; }
    else if (ratio < 2.2) { nivel = "ACTIVO"; tone = "buy"; }
    else { nivel = "MUY ACTIVO"; tone = "sell"; }
    return { usdt_min, vol_15m: Math.round(vNow * 15), nivel, tone, history: hist.slice(-48), pct: Math.min(1, ratio / 3), ratio: Math.round(ratio * 100) / 100 };
  }

  function createLiveEngine({ baseUrl = "", pollMs = 30000, intervaloMin = 5 } = {}) {
    const B = baseUrl.replace(/\\/$/, "");
    let snap = null, history = [], heatmap = [], count = 0, vel = null, velHist = [], lastVelTs = null;
    let cycleStart = Date.now();
    const subs = new Set();
    let stopped = false;

    function emit(type) {
      if (!snap) return;
      subs.forEach((fn) => fn({ snap, history, heatmap, count, vel, cycleStart, cycleMs: pollMs, type }));
    }

    async function refresh(type) {
      try {
        const [estado, hist, heat, cnt] = await Promise.all([
          getJSON(B + "/api/estado"),
          getJSON(B + "/api/historial").catch(() => []),
          getJSON(B + "/api/heatmap").catch(() => []),
          getJSON(B + "/api/count").catch(() => ({ count: 0 })),
        ]);
        if (stopped) return;
        if (estado && Object.keys(estado).length) snap = normSnap(estado);
        if (Array.isArray(hist) && hist.length) history = hist.map(liteFromRow);
        else if (snap) history = [liteFromRow(snap)];
        if (Array.isArray(heat)) heatmap = heat.map((h) => ({
          hora: num(h.hora), dia: h.dia, avg_spread: num(h.avg_spread), muestras: num(h.muestras),
        }));
        count = num(cnt && cnt.count, count);
        if (snap) {
          const vNow = marketVelNow(snap);              // USDT/min absorbidos (consumo real por anunciante)
          if (snap.timestamp && snap.timestamp !== lastVelTs) {
            velHist.push(vNow);
            if (velHist.length > 60) velHist.shift();
            lastVelTs = snap.timestamp;
          }
          vel = computeVelReal(vNow, velHist.length ? velHist : [vNow]);
        }
        cycleStart = Date.now();
        emit(type);
      } catch (e) {
        console.warn("[P2P live] no se pudo refrescar:", e.message);
        // si nunca cargó, caemos a demo para no dejar pantalla vacía
        if (!snap && window.P2P.createEngine) {
          console.warn("[P2P live] usando data demo como respaldo");
          fallbackToDemo();
        }
      }
    }

    let demoEng = null;
    function fallbackToDemo() {
      if (demoEng) return;
      demoEng = window.P2P.createEngine({ cycleMs: pollMs });
      demoEng.subscribe((s) => { snap = s.snap; history = s.history; heatmap = s.heatmap; count = s.count; vel = s.vel; cycleStart = s.cycleStart; emit("cycle"); });
    }

    refresh("init");
    const id = setInterval(() => refresh("cycle"), pollMs);

    return {
      get state() { return { snap, history, heatmap, count, vel, cycleStart, cycleMs: pollMs }; },
      subscribe(fn) { subs.add(fn); if (snap) fn({ snap, history, heatmap, count, vel, cycleStart, cycleMs: pollMs, type: "init" }); return () => subs.delete(fn); },
      forceCycle: () => refresh("cycle"),
      stop() { stopped = true; clearInterval(id); if (demoEng) demoEng.stop(); },
    };
  }

  P.createLiveEngine = createLiveEngine;
})();

</script>
<script type="text/babel">
// @ds-adherence-ignore -- omelette starter scaffold (raw elements/hex/px by design)

/* BEGIN USAGE */
// tweaks-panel.jsx
// Reusable Tweaks shell + form-control helpers.
// Exports (to window): useTweaks, TweaksPanel, TweakSection, TweakRow, TweakSlider,
//   TweakToggle, TweakRadio, TweakSelect, TweakText, TweakNumber, TweakColor, TweakButton.
//
// Owns the host protocol (listens for __activate_edit_mode / __deactivate_edit_mode,
// posts __edit_mode_available / __edit_mode_set_keys / __edit_mode_dismissed) so
// individual prototypes don't re-roll it. Ships a consistent set of controls so you
// don't hand-draw <input type="range">, segmented radios, steppers, etc.
//
// Usage (in an HTML file that loads React + Babel):
//
//   const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
//     "primaryColor": "#D97757",
//     "palette": ["#D97757", "#29261b", "#f6f4ef"],
//     "fontSize": 16,
//     "density": "regular",
//     "dark": false
//   }/*EDITMODE-END*/;
//
//   function App() {
//     const [t, setTweak] = useTweaks(TWEAK_DEFAULTS);
//     return (
//       <div style={{ fontSize: t.fontSize, color: t.primaryColor }}>
//         Hello
//         <TweaksPanel>
//           <TweakSection label="Typography" />
//           <TweakSlider label="Font size" value={t.fontSize} min={10} max={32} unit="px"
//                        onChange={(v) => setTweak('fontSize', v)} />
//           <TweakRadio  label="Density" value={t.density}
//                        options={['compact', 'regular', 'comfy']}
//                        onChange={(v) => setTweak('density', v)} />
//           <TweakSection label="Theme" />
//           <TweakColor  label="Primary" value={t.primaryColor}
//                        options={['#D97757', '#2A6FDB', '#1F8A5B', '#7A5AE0']}
//                        onChange={(v) => setTweak('primaryColor', v)} />
//           <TweakColor  label="Palette" value={t.palette}
//                        options={[['#D97757', '#29261b', '#f6f4ef'],
//                                  ['#475569', '#0f172a', '#f1f5f9']]}
//                        onChange={(v) => setTweak('palette', v)} />
//           <TweakToggle label="Dark mode" value={t.dark}
//                        onChange={(v) => setTweak('dark', v)} />
//         </TweaksPanel>
//       </div>
//     );
//   }
//
// TweakRadio is the segmented control for 2–3 short options (auto-falls-back to
// TweakSelect past ~16/~10 chars per label); reach for TweakSelect directly when
// options are many or long. For color tweaks always curate 3-4 options rather than
// a free picker; an option can also be a whole 2–5 color palette (the stored value
// is the array). The Tweak* controls are a floor, not a ceiling — build custom
// controls inside the panel if a tweak calls for UI they don't cover.
/* END USAGE */
// ─────────────────────────────────────────────────────────────────────────────

const __TWEAKS_STYLE = `
  .twk-panel{position:fixed;right:16px;bottom:16px;z-index:2147483646;width:280px;
    max-height:calc(100vh - 32px);display:flex;flex-direction:column;
    transform:scale(var(--dc-inv-zoom,1));transform-origin:bottom right;
    background:rgba(250,249,247,.78);color:#29261b;
    -webkit-backdrop-filter:blur(24px) saturate(160%);backdrop-filter:blur(24px) saturate(160%);
    border:.5px solid rgba(255,255,255,.6);border-radius:14px;
    box-shadow:0 1px 0 rgba(255,255,255,.5) inset,0 12px 40px rgba(0,0,0,.18);
    font:11.5px/1.4 ui-sans-serif,system-ui,-apple-system,sans-serif;overflow:hidden}
  .twk-hd{display:flex;align-items:center;justify-content:space-between;
    padding:10px 8px 10px 14px;cursor:move;user-select:none}
  .twk-hd b{font-size:12px;font-weight:600;letter-spacing:.01em}
  .twk-x{appearance:none;border:0;background:transparent;color:rgba(41,38,27,.55);
    width:22px;height:22px;border-radius:6px;cursor:default;font-size:13px;line-height:1}
  .twk-x:hover{background:rgba(0,0,0,.06);color:#29261b}
  .twk-body{padding:2px 14px 14px;display:flex;flex-direction:column;gap:10px;
    overflow-y:auto;overflow-x:hidden;min-height:0;
    scrollbar-width:thin;scrollbar-color:rgba(0,0,0,.15) transparent}
  .twk-body::-webkit-scrollbar{width:8px}
  .twk-body::-webkit-scrollbar-track{background:transparent;margin:2px}
  .twk-body::-webkit-scrollbar-thumb{background:rgba(0,0,0,.15);border-radius:4px;
    border:2px solid transparent;background-clip:content-box}
  .twk-body::-webkit-scrollbar-thumb:hover{background:rgba(0,0,0,.25);
    border:2px solid transparent;background-clip:content-box}
  .twk-row{display:flex;flex-direction:column;gap:5px}
  .twk-row-h{flex-direction:row;align-items:center;justify-content:space-between;gap:10px}
  .twk-lbl{display:flex;justify-content:space-between;align-items:baseline;
    color:rgba(41,38,27,.72)}
  .twk-lbl>span:first-child{font-weight:500}
  .twk-val{color:rgba(41,38,27,.5);font-variant-numeric:tabular-nums}

  .twk-sect{font-size:10px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;
    color:rgba(41,38,27,.45);padding:10px 0 0}
  .twk-sect:first-child{padding-top:0}

  .twk-field{appearance:none;box-sizing:border-box;width:100%;min-width:0;height:26px;padding:0 8px;
    border:.5px solid rgba(0,0,0,.1);border-radius:7px;
    background:rgba(255,255,255,.6);color:inherit;font:inherit;outline:none}
  .twk-field:focus{border-color:rgba(0,0,0,.25);background:rgba(255,255,255,.85)}
  select.twk-field{padding-right:22px;
    background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'><path fill='rgba(0,0,0,.5)' d='M0 0h10L5 6z'/></svg>");
    background-repeat:no-repeat;background-position:right 8px center}

  .twk-slider{appearance:none;-webkit-appearance:none;width:100%;height:4px;margin:6px 0;
    border-radius:999px;background:rgba(0,0,0,.12);outline:none}
  .twk-slider::-webkit-slider-thumb{-webkit-appearance:none;appearance:none;
    width:14px;height:14px;border-radius:50%;background:#fff;
    border:.5px solid rgba(0,0,0,.12);box-shadow:0 1px 3px rgba(0,0,0,.2);cursor:default}
  .twk-slider::-moz-range-thumb{width:14px;height:14px;border-radius:50%;
    background:#fff;border:.5px solid rgba(0,0,0,.12);box-shadow:0 1px 3px rgba(0,0,0,.2);cursor:default}

  .twk-seg{position:relative;display:flex;padding:2px;border-radius:8px;
    background:rgba(0,0,0,.06);user-select:none}
  .twk-seg-thumb{position:absolute;top:2px;bottom:2px;border-radius:6px;
    background:rgba(255,255,255,.9);box-shadow:0 1px 2px rgba(0,0,0,.12);
    transition:left .15s cubic-bezier(.3,.7,.4,1),width .15s}
  .twk-seg.dragging .twk-seg-thumb{transition:none}
  .twk-seg button{appearance:none;position:relative;z-index:1;flex:1;border:0;
    background:transparent;color:inherit;font:inherit;font-weight:500;min-height:22px;
    border-radius:6px;cursor:default;padding:4px 6px;line-height:1.2;
    overflow-wrap:anywhere}

  .twk-toggle{position:relative;width:32px;height:18px;border:0;border-radius:999px;
    background:rgba(0,0,0,.15);transition:background .15s;cursor:default;padding:0}
  .twk-toggle[data-on="1"]{background:#34c759}
  .twk-toggle i{position:absolute;top:2px;left:2px;width:14px;height:14px;border-radius:50%;
    background:#fff;box-shadow:0 1px 2px rgba(0,0,0,.25);transition:transform .15s}
  .twk-toggle[data-on="1"] i{transform:translateX(14px)}

  .twk-num{display:flex;align-items:center;box-sizing:border-box;min-width:0;height:26px;padding:0 0 0 8px;
    border:.5px solid rgba(0,0,0,.1);border-radius:7px;background:rgba(255,255,255,.6)}
  .twk-num-lbl{font-weight:500;color:rgba(41,38,27,.6);cursor:ew-resize;
    user-select:none;padding-right:8px}
  .twk-num input{flex:1;min-width:0;height:100%;border:0;background:transparent;
    font:inherit;font-variant-numeric:tabular-nums;text-align:right;padding:0 8px 0 0;
    outline:none;color:inherit;-moz-appearance:textfield}
  .twk-num input::-webkit-inner-spin-button,.twk-num input::-webkit-outer-spin-button{
    -webkit-appearance:none;margin:0}
  .twk-num-unit{padding-right:8px;color:rgba(41,38,27,.45)}

  .twk-btn{appearance:none;height:26px;padding:0 12px;border:0;border-radius:7px;
    background:rgba(0,0,0,.78);color:#fff;font:inherit;font-weight:500;cursor:default}
  .twk-btn:hover{background:rgba(0,0,0,.88)}
  .twk-btn.secondary{background:rgba(0,0,0,.06);color:inherit}
  .twk-btn.secondary:hover{background:rgba(0,0,0,.1)}

  .twk-swatch{appearance:none;-webkit-appearance:none;width:56px;height:22px;
    border:.5px solid rgba(0,0,0,.1);border-radius:6px;padding:0;cursor:default;
    background:transparent;flex-shrink:0}
  .twk-swatch::-webkit-color-swatch-wrapper{padding:0}
  .twk-swatch::-webkit-color-swatch{border:0;border-radius:5.5px}
  .twk-swatch::-moz-color-swatch{border:0;border-radius:5.5px}

  .twk-chips{display:flex;gap:6px}
  .twk-chip{position:relative;appearance:none;flex:1;min-width:0;height:46px;
    padding:0;border:0;border-radius:6px;overflow:hidden;cursor:default;
    box-shadow:0 0 0 .5px rgba(0,0,0,.12),0 1px 2px rgba(0,0,0,.06);
    transition:transform .12s cubic-bezier(.3,.7,.4,1),box-shadow .12s}
  .twk-chip:hover{transform:translateY(-1px);
    box-shadow:0 0 0 .5px rgba(0,0,0,.18),0 4px 10px rgba(0,0,0,.12)}
  .twk-chip[data-on="1"]{box-shadow:0 0 0 1.5px rgba(0,0,0,.85),
    0 2px 6px rgba(0,0,0,.15)}
  .twk-chip>span{position:absolute;top:0;bottom:0;right:0;width:34%;
    display:flex;flex-direction:column;box-shadow:-1px 0 0 rgba(0,0,0,.1)}
  .twk-chip>span>i{flex:1;box-shadow:0 -1px 0 rgba(0,0,0,.1)}
  .twk-chip>span>i:first-child{box-shadow:none}
  .twk-chip svg{position:absolute;top:6px;left:6px;width:13px;height:13px;
    filter:drop-shadow(0 1px 1px rgba(0,0,0,.3))}
`;

// ── useTweaks ───────────────────────────────────────────────────────────────
// Single source of truth for tweak values. setTweak persists via the host
// (__edit_mode_set_keys → host rewrites the EDITMODE block on disk).
function useTweaks(defaults) {
  const [values, setValues] = React.useState(defaults);
  // Accepts either setTweak('key', value) or setTweak({ key: value, ... }) so a
  // useState-style call doesn't write a "[object Object]" key into the persisted
  // JSON block.
  const setTweak = React.useCallback((keyOrEdits, val) => {
    const edits = typeof keyOrEdits === 'object' && keyOrEdits !== null
      ? keyOrEdits : { [keyOrEdits]: val };
    setValues((prev) => ({ ...prev, ...edits }));
    window.parent.postMessage({ type: '__edit_mode_set_keys', edits }, '*');
    // Same-window signal so in-page listeners (deck-stage rail thumbnails)
    // can react — the parent message only reaches the host, not peers.
    window.dispatchEvent(new CustomEvent('tweakchange', { detail: edits }));
  }, []);
  return [values, setTweak];
}

// ── TweaksPanel ─────────────────────────────────────────────────────────────
// Floating shell. Registers the protocol listener BEFORE announcing
// availability — if the announce ran first, the host's activate could land
// before our handler exists and the toolbar toggle would silently no-op.
// The close button posts __edit_mode_dismissed so the host's toolbar toggle
// flips off in lockstep; the host echoes __deactivate_edit_mode back which
// is what actually hides the panel.
function TweaksPanel({ title = 'Tweaks', children }) {
  const [open, setOpen] = React.useState(false);
  const dragRef = React.useRef(null);
  const offsetRef = React.useRef({ x: 16, y: 16 });
  const PAD = 16;

  const clampToViewport = React.useCallback(() => {
    const panel = dragRef.current;
    if (!panel) return;
    const w = panel.offsetWidth, h = panel.offsetHeight;
    const maxRight = Math.max(PAD, window.innerWidth - w - PAD);
    const maxBottom = Math.max(PAD, window.innerHeight - h - PAD);
    offsetRef.current = {
      x: Math.min(maxRight, Math.max(PAD, offsetRef.current.x)),
      y: Math.min(maxBottom, Math.max(PAD, offsetRef.current.y)),
    };
    panel.style.right = offsetRef.current.x + 'px';
    panel.style.bottom = offsetRef.current.y + 'px';
  }, []);

  React.useEffect(() => {
    if (!open) return;
    clampToViewport();
    if (typeof ResizeObserver === 'undefined') {
      window.addEventListener('resize', clampToViewport);
      return () => window.removeEventListener('resize', clampToViewport);
    }
    const ro = new ResizeObserver(clampToViewport);
    ro.observe(document.documentElement);
    return () => ro.disconnect();
  }, [open, clampToViewport]);

  React.useEffect(() => {
    const onMsg = (e) => {
      const t = e?.data?.type;
      if (t === '__activate_edit_mode') setOpen(true);
      else if (t === '__deactivate_edit_mode') setOpen(false);
    };
    window.addEventListener('message', onMsg);
    window.parent.postMessage({ type: '__edit_mode_available' }, '*');
    return () => window.removeEventListener('message', onMsg);
  }, []);

  const dismiss = () => {
    setOpen(false);
    window.parent.postMessage({ type: '__edit_mode_dismissed' }, '*');
  };

  const onDragStart = (e) => {
    const panel = dragRef.current;
    if (!panel) return;
    const r = panel.getBoundingClientRect();
    const sx = e.clientX, sy = e.clientY;
    const startRight = window.innerWidth - r.right;
    const startBottom = window.innerHeight - r.bottom;
    const move = (ev) => {
      offsetRef.current = {
        x: startRight - (ev.clientX - sx),
        y: startBottom - (ev.clientY - sy),
      };
      clampToViewport();
    };
    const up = () => {
      window.removeEventListener('mousemove', move);
      window.removeEventListener('mouseup', up);
    };
    window.addEventListener('mousemove', move);
    window.addEventListener('mouseup', up);
  };

  if (!open) return null;
  return (
    <>
      <style>{__TWEAKS_STYLE}</style>
      <div ref={dragRef} className="twk-panel" data-omelette-chrome=""
           style={{ right: offsetRef.current.x, bottom: offsetRef.current.y }}>
        <div className="twk-hd" onMouseDown={onDragStart}>
          <b>{title}</b>
          <button className="twk-x" aria-label="Close tweaks"
                  onMouseDown={(e) => e.stopPropagation()}
                  onClick={dismiss}>✕</button>
        </div>
        <div className="twk-body">
          {children}
        </div>
      </div>
    </>
  );
}

// ── Layout helpers ──────────────────────────────────────────────────────────

function TweakSection({ label, children }) {
  return (
    <>
      <div className="twk-sect">{label}</div>
      {children}
    </>
  );
}

function TweakRow({ label, value, children, inline = false }) {
  return (
    <div className={inline ? 'twk-row twk-row-h' : 'twk-row'}>
      <div className="twk-lbl">
        <span>{label}</span>
        {value != null && <span className="twk-val">{value}</span>}
      </div>
      {children}
    </div>
  );
}

// ── Controls ────────────────────────────────────────────────────────────────

function TweakSlider({ label, value, min = 0, max = 100, step = 1, unit = '', onChange }) {
  return (
    <TweakRow label={label} value={`${value}${unit}`}>
      <input type="range" className="twk-slider" min={min} max={max} step={step}
             value={value} onChange={(e) => onChange(Number(e.target.value))} />
    </TweakRow>
  );
}

function TweakToggle({ label, value, onChange }) {
  return (
    <div className="twk-row twk-row-h">
      <div className="twk-lbl"><span>{label}</span></div>
      <button type="button" className="twk-toggle" data-on={value ? '1' : '0'}
              role="switch" aria-checked={!!value}
              onClick={() => onChange(!value)}><i /></button>
    </div>
  );
}

function TweakRadio({ label, value, options, onChange }) {
  const trackRef = React.useRef(null);
  const [dragging, setDragging] = React.useState(false);
  // The active value is read by pointer-move handlers attached for the lifetime
  // of a drag — ref it so a stale closure doesn't fire onChange for every move.
  const valueRef = React.useRef(value);
  valueRef.current = value;

  // Segments wrap mid-word once per-segment width runs out. The track is
  // ~248px (280 panel − 28 body pad − 4 seg pad), each button loses 12px
  // to its own padding, and 11.5px system-ui averages ~6.3px/char — so 2
  // options fit ~16 chars each, 3 fit ~10. Past that (or >3 options), fall
  // back to a dropdown rather than wrap.
  const labelLen = (o) => String(typeof o === 'object' ? o.label : o).length;
  const maxLen = options.reduce((m, o) => Math.max(m, labelLen(o)), 0);
  const fitsAsSegments = maxLen <= ({ 2: 16, 3: 10 }[options.length] ?? 0);
  if (!fitsAsSegments) {
    // <select> emits strings — map back to the original option value so the
    // fallback stays type-preserving (numbers, booleans) like the segment path.
    const resolve = (s) => {
      const m = options.find((o) => String(typeof o === 'object' ? o.value : o) === s);
      return m === undefined ? s : typeof m === 'object' ? m.value : m;
    };
    return <TweakSelect label={label} value={value} options={options}
                        onChange={(s) => onChange(resolve(s))} />;
  }
  const opts = options.map((o) => (typeof o === 'object' ? o : { value: o, label: o }));
  const idx = Math.max(0, opts.findIndex((o) => o.value === value));
  const n = opts.length;

  const segAt = (clientX) => {
    const r = trackRef.current.getBoundingClientRect();
    const inner = r.width - 4;
    const i = Math.floor(((clientX - r.left - 2) / inner) * n);
    return opts[Math.max(0, Math.min(n - 1, i))].value;
  };

  const onPointerDown = (e) => {
    setDragging(true);
    const v0 = segAt(e.clientX);
    if (v0 !== valueRef.current) onChange(v0);
    const move = (ev) => {
      if (!trackRef.current) return;
      const v = segAt(ev.clientX);
      if (v !== valueRef.current) onChange(v);
    };
    const up = () => {
      setDragging(false);
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  };

  return (
    <TweakRow label={label}>
      <div ref={trackRef} role="radiogroup" onPointerDown={onPointerDown}
           className={dragging ? 'twk-seg dragging' : 'twk-seg'}>
        <div className="twk-seg-thumb"
             style={{ left: `calc(2px + ${idx} * (100% - 4px) / ${n})`,
                      width: `calc((100% - 4px) / ${n})` }} />
        {opts.map((o) => (
          <button key={o.value} type="button" role="radio" aria-checked={o.value === value}>
            {o.label}
          </button>
        ))}
      </div>
    </TweakRow>
  );
}

function TweakSelect({ label, value, options, onChange }) {
  return (
    <TweakRow label={label}>
      <select className="twk-field" value={value} onChange={(e) => onChange(e.target.value)}>
        {options.map((o) => {
          const v = typeof o === 'object' ? o.value : o;
          const l = typeof o === 'object' ? o.label : o;
          return <option key={v} value={v}>{l}</option>;
        })}
      </select>
    </TweakRow>
  );
}

function TweakText({ label, value, placeholder, onChange }) {
  return (
    <TweakRow label={label}>
      <input className="twk-field" type="text" value={value} placeholder={placeholder}
             onChange={(e) => onChange(e.target.value)} />
    </TweakRow>
  );
}

function TweakNumber({ label, value, min, max, step = 1, unit = '', onChange }) {
  const clamp = (n) => {
    if (min != null && n < min) return min;
    if (max != null && n > max) return max;
    return n;
  };
  const startRef = React.useRef({ x: 0, val: 0 });
  const onScrubStart = (e) => {
    e.preventDefault();
    startRef.current = { x: e.clientX, val: value };
    const decimals = (String(step).split('.')[1] || '').length;
    const move = (ev) => {
      const dx = ev.clientX - startRef.current.x;
      const raw = startRef.current.val + dx * step;
      const snapped = Math.round(raw / step) * step;
      onChange(clamp(Number(snapped.toFixed(decimals))));
    };
    const up = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  };
  return (
    <div className="twk-num">
      <span className="twk-num-lbl" onPointerDown={onScrubStart}>{label}</span>
      <input type="number" value={value} min={min} max={max} step={step}
             onChange={(e) => onChange(clamp(Number(e.target.value)))} />
      {unit && <span className="twk-num-unit">{unit}</span>}
    </div>
  );
}

// Relative-luminance contrast pick — checkmarks drawn over a swatch need to
// read on both #111 and #fafafa without per-option configuration. Hex input
// only (#rgb / #rrggbb); named or rgb()/hsl() colors fall through to "light".
function __twkIsLight(hex) {
  const h = String(hex).replace('#', '');
  const x = h.length === 3 ? h.replace(/./g, (c) => c + c) : h.padEnd(6, '0');
  const n = parseInt(x.slice(0, 6), 16);
  if (Number.isNaN(n)) return true;
  const r = (n >> 16) & 255, g = (n >> 8) & 255, b = n & 255;
  return r * 299 + g * 587 + b * 114 > 148000;
}

const __TwkCheck = ({ light }) => (
  <svg viewBox="0 0 14 14" aria-hidden="true">
    <path d="M3 7.2 5.8 10 11 4.2" fill="none" strokeWidth="2.2"
          strokeLinecap="round" strokeLinejoin="round"
          stroke={light ? 'rgba(0,0,0,.78)' : '#fff'} />
  </svg>
);

// TweakColor — curated color/palette picker. Each option is either a single
// hex string or an array of 1-5 hex strings; the card adapts — a lone color
// renders solid, a palette renders colors[0] as the hero (left ~2/3) with the
// rest stacked in a sharp column on the right. onChange emits the
// option in the shape it was passed (string stays string, array stays array).
// Without options it falls back to the native color input for back-compat.
function TweakColor({ label, value, options, onChange }) {
  if (!options || !options.length) {
    return (
      <div className="twk-row twk-row-h">
        <div className="twk-lbl"><span>{label}</span></div>
        <input type="color" className="twk-swatch" value={value}
               onChange={(e) => onChange(e.target.value)} />
      </div>
    );
  }
  // Native <input type=color> emits lowercase hex per the HTML spec, so
  // compare case-insensitively. String() guards JSON.stringify(undefined),
  // which returns the primitive undefined (no .toLowerCase).
  const key = (o) => String(JSON.stringify(o)).toLowerCase();
  const cur = key(value);
  return (
    <TweakRow label={label}>
      <div className="twk-chips" role="radiogroup">
        {options.map((o, i) => {
          const colors = Array.isArray(o) ? o : [o];
          const [hero, ...rest] = colors;
          const sup = rest.slice(0, 4);
          const on = key(o) === cur;
          return (
            <button key={i} type="button" className="twk-chip" role="radio"
                    aria-checked={on} data-on={on ? '1' : '0'}
                    aria-label={colors.join(', ')} title={colors.join(' · ')}
                    style={{ background: hero }}
                    onClick={() => onChange(o)}>
              {sup.length > 0 && (
                <span>
                  {sup.map((c, j) => <i key={j} style={{ background: c }} />)}
                </span>
              )}
              {on && <__TwkCheck light={__twkIsLight(hero)} />}
            </button>
          );
        })}
      </div>
    </TweakRow>
  );
}

function TweakButton({ label, onClick, secondary = false }) {
  return (
    <button type="button" className={secondary ? 'twk-btn secondary' : 'twk-btn'}
            onClick={onClick}>{label}</button>
  );
}

Object.assign(window, {
  useTweaks, TweaksPanel, TweakSection, TweakRow,
  TweakSlider, TweakToggle, TweakRadio, TweakSelect,
  TweakText, TweakNumber, TweakColor, TweakButton,
});

</script>
<script type="text/babel">
/* ============================================================
   Unión Austral · P2P Monitor — componentes UI compartidos
   ============================================================ */
const { useState, useEffect, useRef, useMemo } = React;

/* Número que hace tween entre valores y destella verde/rojo al cambiar */
function AnimatedNumber({ value, decimals = 2, prefix = "", suffix = "", className = "", flash = true }) {
  const [display, setDisplay] = useState(value);
  const [dir, setDir] = useState(0);
  const prev = useRef(value);
  const raf = useRef(null);

  useEffect(() => {
    const from = prev.current;
    const to = value;
    if (from === to) return;
    setDir(to > from ? 1 : -1);
    const start = performance.now();
    const dur = 520;
    const ease = (t) => 1 - Math.pow(1 - t, 3);
    cancelAnimationFrame(raf.current);
    const step = (now) => {
      const t = Math.min(1, (now - start) / dur);
      setDisplay(from + (to - from) * ease(t));
      if (t < 1) raf.current = requestAnimationFrame(step);
      else { prev.current = to; setTimeout(() => setDir(0), 360); }
    };
    raf.current = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf.current);
  }, [value]);

  const txt = prefix + display.toLocaleString("es-AR", {
    minimumFractionDigits: decimals, maximumFractionDigits: decimals,
  }) + suffix;
  const flashClass = flash && dir > 0 ? "num-up" : flash && dir < 0 ? "num-down" : "";
  return <span className={`tnum ${flashClass} ${className}`}>{txt}</span>;
}

/* Anillo de countdown SVG: progress 0..1 (lleno -> vacío) */
function CountdownRing({ secondsLeft, total, size = 46, stroke = 3.5 }) {
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const p = Math.max(0, Math.min(1, secondsLeft / total));
  return (
    <div className="ring" style={{ width: size, height: size }}>
      <svg width={size} height={size}>
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--line)" strokeWidth={stroke} />
        <circle
          cx={size / 2} cy={size / 2} r={r} fill="none"
          stroke="var(--accent)" strokeWidth={stroke} strokeLinecap="round"
          strokeDasharray={c} strokeDashoffset={c * (1 - p)}
          transform={`rotate(-90 ${size / 2} ${size / 2})`}
          style={{ transition: "stroke-dashoffset .25s linear" }}
        />
      </svg>
      <div className="ring-label tnum">{Math.ceil(secondsLeft)}<span className="ring-s">s</span></div>
    </div>
  );
}

/* Punto pulsante "en vivo" */
function LivePulse({ tone = "buy", label = "EN VIVO" }) {
  return (
    <span className="live">
      <span className="live-dot" style={{ background: `var(--${tone})` }}></span>
      <span className="live-ring" style={{ borderColor: `var(--${tone})` }}></span>
      <span className="live-txt">{label}</span>
    </span>
  );
}

/* Sparkline / área SVG a partir de un array de números */
function Sparkline({ data, tone = "accent", height = 44, fill = true, strokeW = 1.6 }) {
  const path = useMemo(() => {
    if (!data || data.length < 2) return { line: "", area: "" };
    const w = 100, h = height;
    const min = Math.min(...data), max = Math.max(...data);
    const span = max - min || 1;
    const pts = data.map((v, i) => {
      const x = (i / (data.length - 1)) * w;
      const y = h - ((v - min) / span) * (h - 6) - 3;
      return [x, y];
    });
    const line = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(2) + " " + p[1].toFixed(2)).join(" ");
    const area = line + ` L100 ${h} L0 ${h} Z`;
    return { line, area };
  }, [data, height]);

  const id = useMemo(() => "sg" + Math.random().toString(36).slice(2, 8), []);
  return (
    <svg className="spark" viewBox={`0 0 100 ${height}`} preserveAspectRatio="none" style={{ height }}>
      <defs>
        <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={`var(--${tone})`} stopOpacity="0.30" />
          <stop offset="100%" stopColor={`var(--${tone})`} stopOpacity="0" />
        </linearGradient>
      </defs>
      {fill && <path d={path.area} fill={`url(#${id})`} />}
      <path d={path.line} fill="none" stroke={`var(--${tone})`} strokeWidth={strokeW}
        vectorEffect="non-scaling-stroke" strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}

/* Medidor de spread: muestra el valor actual sobre zonas de aptitud */
function SpreadGauge({ value, max = 2.2 }) {
  const zones = [
    { to: 0.2, tone: "sell" },
    { to: 0.55, tone: "warn-low" },
    { to: 1.0, tone: "warn" },
    { to: max, tone: "buy" },
  ];
  const pos = Math.max(0, Math.min(1, value / max)) * 100;
  return (
    <div className="gauge">
      <div className="gauge-track">
        {zones.map((z, i) => {
          const from = i === 0 ? 0 : zones[i - 1].to;
          const wpct = ((z.to - from) / max) * 100;
          return <div key={i} className="gauge-zone" style={{ width: wpct + "%", background: `var(--${z.tone}-soft)` }} />;
        })}
        <div className="gauge-marker" style={{ left: pos + "%" }}>
          <div className="gauge-stick" />
        </div>
      </div>
      <div className="gauge-scale">
        <span>0%</span><span>0.55%</span><span>1.0%</span><span>{max}%</span>
      </div>
    </div>
  );
}

/* Mini barra horizontal para volumen relativo */
function Bar({ pct, tone }) {
  return <div className="hbar"><div className="hbar-fill" style={{ width: pct + "%", background: `var(--${tone})` }} /></div>;
}

Object.assign(window, { AnimatedNumber, CountdownRing, LivePulse, Sparkline, SpreadGauge, Bar });

</script>
<script type="text/babel">
/* ============================================================
   Unión Austral · P2P Monitor — Tiempo Real (núcleo)
   ============================================================ */
const { useState: uS, useEffect: uE, useRef: uR, useMemo: uM } = React;
const TONE = window.P2P.COLOR_TONE; // green->buy, yellow->warn, orange->warn-low, red->sell
const fP = window.P2P.fmtPrice, fN = window.P2P.fmtNum, fPc = window.P2P.fmtPct;
const ESTADO_ICON = { "MUY APTO": "▲▲", "APTO": "▲", "ESTRECHO": "▬", "NO APTO": "▼" };

/* ---------- TopBar ---------- */
function TopBar({ snap, secondsLeft, cycleMs }) {
  // Reloj vivo: recalcula la edad del dato cada segundo. Si el colector sigue
  // guardando, el contador se resetea cada ciclo (confirmacion de "en vivo").
  // Si se corta, sigue subiendo y el indicador pasa a amarillo y luego rojo.
  const [now, setNow] = React.useState(Date.now());
  React.useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);
  const ts = snap.timestamp ? Date.parse(String(snap.timestamp).replace(" ", "T")) : null;
  const ageSec = ts ? Math.max(0, (now - ts) / 1000) : null;
  const fresh = ageSec != null && ageSec < 180;   // < 3 min = en vivo
  const dead  = ageSec == null || ageSec >= 360;   // > 6 min = colector parado
  const tone  = dead ? "sell" : fresh ? "buy" : "warn";
  const label = ageSec == null ? "SIN DATOS" : dead ? "SIN DATOS EN VIVO" : fresh ? "EN VIVO" : "RETRASADO";
  const hace  = ageSec == null ? "—"
    : ageSec < 60 ? "hace " + Math.round(ageSec) + "s"
    : ageSec < 3600 ? "hace " + Math.round(ageSec / 60) + " min"
    : "hace " + Math.round(ageSec / 3600) + " h";
  const col = dead ? "var(--sell)" : fresh ? "var(--buy)" : "var(--warn)";
  return (
    <header className="topbar">
      <div className="brand">
        <div className="brand-mark">UA</div>
        <div className="brand-txt">
          <div className="brand-name">Unión Austral <span>Capital</span></div>
          <div className="brand-sub">P2P Monitor</div>
        </div>
      </div>
      <div className="market-chip">
        <span className="mc-pair">USDT / CLP</span>
        <span className="mc-dot">·</span>
        <span className="mc-src">Binance P2P</span>
      </div>
      <div className="topbar-right">
        <LivePulse tone={tone} label={label} />
        <div className="last-upd">
          <div className="lu-label">Actualizado</div>
          <div className="lu-time tnum" style={{ color: col }}>{hace}</div>
        </div>
        <CountdownRing secondsLeft={secondsLeft} total={cycleMs / 1000} />
      </div>
    </header>
  );
}

/* ---------- Tab bar ---------- */
function Tabs({ tab, setTab }) {
  const items = [["tr", "Tiempo Real"], ["hist", "Histórico"], ["precio", "Precio"], ["intel", "Inteligencia"], ["heat", "Mapa de Calor"], ["rot", "Rotación"], ["cross", "Cross"], ["muros", "Muros"], ["backup", "Backup"]];
  return (
    <nav className="tabbar" role="tablist">
      {items.map(([k, label]) => (
        <button key={k} role="tab" aria-selected={tab === k}
          className={"tab" + (tab === k ? " active" : "")} onClick={() => setTab(k)}>
          {label}
        </button>
      ))}
    </nav>
  );
}

/* ---------- Decision hero ---------- */
function DecisionHero({ snap }) {
  const tone = TONE[snap.color] || "warn";
  return (
    <section className={"hero tone-" + tone} data-screen-label="Semáforo de decisión">
      <div className="hero-main">
        <div className="hero-flag">
          <span className="hero-icon" aria-hidden="true">{ESTADO_ICON[snap.estado]}</span>
          <div>
            <div className="hero-estado">{snap.estado}</div>
            <div className="hero-sub">Spread ponderado del mercado P2P</div>
          </div>
        </div>
        <div className="hero-metrics">
          <div className="hm">
            <div className="hm-label">Spread ponderado</div>
            <div className="hm-val big"><AnimatedNumber value={snap.spread_pond_pct} suffix="%" /></div>
            <div className="hm-foot tnum">{fP(snap.spread_pond_abs)} brecha</div>
          </div>
          <div className="hm">
            <div className="hm-label">Spread puntual</div>
            <div className="hm-val"><AnimatedNumber value={snap.spread_pct} suffix="%" /></div>
            <div className="hm-foot">entre líderes</div>
          </div>
          <div className="hm">
            <div className="hm-label">Ganancia neta est.</div>
            <div className={"hm-val " + (snap.ganancia_neta_pct > 0 ? "pos" : "neg")}>
              <AnimatedNumber value={snap.ganancia_neta_pct} suffix="%" />
            </div>
            <div className="hm-foot">descontada comisión 0,36%</div>
          </div>
        </div>
      </div>
      <SpreadGauge value={snap.spread_pond_pct} />
    </section>
  );
}

/* ---------- Side card (compra / venta) ---------- */
function SideCard({ side, snap, history }) {
  const isBuy = side === "buy";
  const tone = isBuy ? "buy" : "sell";
  const pond = isBuy ? snap.precio_pond_tab_compra : snap.precio_pond_tab_venta;
  const leader = isBuy ? snap.mejor_vendedor_tab_compra : snap.mejor_comprador_tab_venta;
  const leaderName = isBuy ? snap.lider_tab_compra : snap.lider_tab_venta;
  const liq = isBuy ? snap.liq_tab_compra : snap.liq_tab_venta;
  const n = isBuy ? snap.n_tab_compra : snap.n_tab_venta;
  const lo = isBuy ? snap.mejor_vendedor_tab_compra : snap.peor_comprador_tab_venta;
  const hi = isBuy ? snap.peor_vendedor_tab_compra : snap.mejor_comprador_tab_venta;
  const spark = history.map((h) => (isBuy ? h.precio_pond_tab_compra : h.precio_pond_tab_venta));
  const liqOther = isBuy ? snap.liq_tab_venta : snap.liq_tab_compra;
  const liqPct = Math.round((liq / (liq + liqOther)) * 100);

  return (
    <article className={"sidecard tone-" + tone} data-screen-label={isBuy ? "Tab Compra" : "Tab Venta"}>
      <div className="sc-head">
        <span className="sc-badge">{isBuy ? "TAB COMPRA" : "TAB VENTA"}</span>
        <span className="sc-role">{isBuy ? "ACÁ VENDÉS USDT" : "ACÁ COMPRÁS USDT"}</span>
      </div>
      <div className="sc-desc">{isBuy ? "Vendedores de USDT · posteás tu anuncio de VENTA" : "Compradores de USDT · posteás tu anuncio de COMPRA"}</div>

      <div className="sc-pond">
        <div className="sc-pond-label">Precio ponderado</div>
        <div className="sc-pond-val"><AnimatedNumber value={pond} prefix="$" /></div>
      </div>
      <Sparkline data={spark} tone={tone} height={40} />

      <div className="sc-leader">
        <div>
          <div className="sc-leader-label">{isBuy ? "Líder · más barato" : "Líder · más paga"}</div>
          <div className="sc-leader-val tnum">{fP(leader)}</div>
        </div>
        <div className="sc-leader-who">
          <span className="who-name">{leaderName}</span>
          {window.P2P && <span className="who-tag">líder</span>}
        </div>
      </div>

      <div className="sc-stats">
        <div className="sc-stat">
          <div className="sc-stat-label">Liquidez</div>
          <div className="sc-stat-val tnum">{fN(liq)} <span className="u">USDT</span></div>
          <Bar pct={liqPct} tone={tone} />
        </div>
        <div className="sc-stat">
          <div className="sc-stat-label">Anuncios</div>
          <div className="sc-stat-val tnum">{n}</div>
        </div>
        <div className="sc-stat">
          <div className="sc-stat-label">Rango</div>
          <div className="sc-stat-val tnum small">{fP(lo)} – {fP(hi)}</div>
        </div>
      </div>
    </article>
  );
}

/* ---------- Center spine: brecha ---------- */
function BrechaSpine({ snap }) {
  return (
    <div className="spine">
      <div className="spine-line" />
      <div className="spine-pill">
        <div className="spine-label">Brecha</div>
        <div className="spine-val tnum">{fP(snap.spread_pond_abs)}</div>
        <div className="spine-pct tnum">{fPc(snap.spread_pond_pct)}</div>
      </div>
      <div className="spine-line" />
    </div>
  );
}

/* ---------- Maker actions ---------- */
function MakerActions({ snap }) {
  const cards = [
    {
      tone: "buy", side: "Tab Compra",
      title: "Para VENDER USDT", price: snap.precio_maker_vender,
      note: <>Un centavo menos que <b>{snap.lider_tab_compra}</b> (pide {fP(snap.mejor_vendedor_tab_compra)})</>,
      tip: "Aparecés primero entre los vendedores",
    },
    {
      tone: "sell", side: "Tab Venta",
      title: "Para COMPRAR USDT", price: snap.precio_maker_comprar,
      note: <>Un centavo más que <b>{snap.lider_tab_venta}</b> (paga {fP(snap.mejor_comprador_tab_venta)})</>,
      tip: "Aparecés primero entre los compradores",
    },
  ];
  return (
    <section className="maker">
      <div className="maker-head">
        <span className="maker-kicker">Acción maker</span>
        <span className="maker-hint">Postealo un centavo mejor que el líder para encabezar la lista</span>
      </div>
      <div className="maker-grid">
        {cards.map((c, i) => (
          <div key={i} className={"maker-card tone-" + c.tone}>
            <div className="mc-top">
              <span className="mc-title">{c.title}</span>
              <span className="mc-side">postear en {c.side}</span>
            </div>
            <div className="mc-price tnum">{fP(c.price)}</div>
            <div className="mc-note">{c.note}</div>
            <div className="mc-tip">→ {c.tip}</div>
          </div>
        ))}
      </div>
    </section>
  );
}

/* ---------- Top traders ranking ---------- */
function TopTraders({ snap }) {
  const [side, setSide] = uS("buy");
  const rows = (side === "buy" ? snap.detalle_compra : snap.detalle_venta) || [];
  const maxLiq = Math.max(...rows.map((r) => r.disponible), 1);
  const tone = side === "buy" ? "buy" : "sell";
  return (
    <section className="ranking">
      <div className="rank-head">
        <h3>Top anunciantes</h3>
        <div className="rank-toggle">
          <button className={side === "buy" ? "on" : ""} onClick={() => setSide("buy")}>Compra</button>
          <button className={side === "sell" ? "on" : ""} onClick={() => setSide("sell")}>Venta</button>
        </div>
      </div>
      <div className={"rank-list tone-" + tone}>
        {rows.slice(0, 8).map((r, i) => (
          <div key={r.anunciante + i} className="rank-row">
            <span className="rank-depth" style={{ width: (r.disponible / maxLiq * 100) + "%" }} />
            <span className="rank-pos tnum">{r.posicion}</span>
            <span className="rank-name">
              {r.anunciante}
              {r.es_merchant && <span className="merch" title="Merchant verificado">✦</span>}
            </span>
            <span className="rank-liq tnum">{fN(r.disponible)}</span>
            <span className="rank-price tnum">{fP(r.precio)}</span>
          </div>
        ))}
      </div>
      <div className="rank-legend">
        <span>#</span><span>Anunciante</span><span>USDT</span><span>Precio</span>
      </div>
    </section>
  );
}

/* ---------- Panel de filtros del mercado ---------- */
function FiltersPanel({ cfg, onApply, info }) {
  const [open, setOpen] = uS(true);
  const [draft, setDraft] = uS(cfg);
  const [saved, setSaved] = uS(false);
  uE(() => { setDraft(cfg); }, [cfg]);
  const dirty = JSON.stringify(draft) !== JSON.stringify(cfg);
  const set = (k, v) => setDraft((d) => ({ ...d, [k]: v }));
  const apply = () => { onApply(draft); setSaved(true); setTimeout(() => setSaved(false), 1800); };
  const reset = () => { setDraft(window.P2P.FILTROS_DEFAULT); onApply(window.P2P.FILTROS_DEFAULT); };

  const chips = [
    cfg.soloVerificados && "Solo verificados",
    `≥ ${fN(cfg.minUsdt)} USDT`,
    `≥ ${cfg.minOrd} órd.`,
    `≥ ${cfg.minTasa}% éxito`,
    `cada ${cfg.intervalo} min`,
  ].filter(Boolean);

  return (
    <section className="filters">
      <button className="filters-head" onClick={() => setOpen(!open)} aria-expanded={open}>
        <span className="fh-title">Filtros del mercado</span>
        <span className="fh-note">se aplican en el próximo ciclo</span>
        {!open && <span className="fh-chips">{chips.map((c, i) => <span key={i} className="fchip">{c}</span>)}</span>}
        <span className="fh-arrow" data-open={open}>▾</span>
      </button>
      {open && (
        <div className="filters-body">
          <label className={"verif" + (draft.soloVerificados ? " on" : "")}>
            <span className="verif-txt">
              <span className="verif-title">Solo anunciantes verificados <span className="verif-badge">✦ merchant</span></span>
              <span className="verif-sub">Excluye usuarios comunes; deja solo cuentas Merchant de Binance</span>
            </span>
            <button type="button" role="switch" aria-checked={draft.soloVerificados}
              className="switch" onClick={() => set("soloVerificados", !draft.soloVerificados)}>
              <span className="switch-knob" />
            </button>
          </label>

          <div className="filters-grid">
            <div className="f-item">
              <label>Mín. USDT disponible</label>
              <input type="number" value={draft.minUsdt} min="0" step="50"
                onChange={(e) => set("minUsdt", +e.target.value)} />
            </div>
            <div className="f-item">
              <label>Mín. órdenes completadas</label>
              <input type="number" value={draft.minOrd} min="0" step="10"
                onChange={(e) => set("minOrd", +e.target.value)} />
            </div>
            <div className="f-item">
              <label>Mín. tasa de éxito (%)</label>
              <input type="number" value={draft.minTasa} min="0" max="100" step="1"
                onChange={(e) => set("minTasa", +e.target.value)} />
            </div>
            <div className="f-item">
              <label>Intervalo de consulta</label>
              <select value={draft.intervalo} onChange={(e) => set("intervalo", +e.target.value)}>
                <option value="1">1 minuto</option>
                <option value="2">2 minutos</option>
                <option value="5">5 minutos</option>
                <option value="10">10 minutos</option>
              </select>
            </div>
          </div>

          {info && (
            <div className="filters-info">
              <span className="fi-dot tone-buy" />
              Pasan el filtro: <b className="tnum">{info.pasanC}/{info.totalC}</b> en compra ·
              <b className="tnum"> {info.pasanV}/{info.totalV}</b> en venta
            </div>
          )}

          <div className="filters-actions">
            <button className="btn-reset" onClick={reset}>Restablecer</button>
            <button className={"btn-apply" + (dirty ? " dirty" : "")} onClick={apply}>
              {saved ? "✓ Aplicado" : dirty ? "Aplicar filtros" : "Sin cambios"}
            </button>
          </div>
        </div>
      )}
    </section>
  );
}

/* ---------- Velocímetro de mercado ---------- */
function VelocityStrip({ vel }) {
  if (!vel) return null;
  return (
    <section className={"velocity tone-" + vel.tone} data-screen-label="Velocidad de mercado">
      <div className="vel-main">
        <div className="vel-icon" aria-hidden="true">⟶</div>
        <div className="vel-headline">
          <div className="vel-label">Velocidad de mercado</div>
          <div className="vel-big">
            <AnimatedNumber value={vel.usdt_min} decimals={0} />
            <span className="vel-unit">USDT / min</span>
          </div>
        </div>
      </div>
      <div className="vel-eg">
        ≈ <b className="tnum">{fN(vel.vol_15m)} USDT</b> absorbidos cada <b>15 min</b>
        {vel.ratio != null && (
          <span className="vel-ratio"> · <b className="tnum">{vel.ratio}×</b> vs. ritmo normal</span>
        )}
      </div>
      <div className="vel-meter">
        <div className="vel-spark"><Sparkline data={vel.history} tone={vel.tone} height={34} strokeW={1.8} /></div>
        <div className="vel-level">
          <span className="vel-nivel">{vel.nivel}</span>
          <div className="vel-bar"><span className="vel-bar-fill" style={{ width: (vel.pct * 100) + "%", background: `var(--${vel.tone})` }} /></div>
        </div>
      </div>
    </section>
  );
}

window.P2PCore = { TopBar, Tabs, DecisionHero, SideCard, BrechaSpine, MakerActions, TopTraders, FiltersPanel, VelocityStrip };

</script>
<script type="text/babel">
/* ============================================================
   Unión Austral · P2P Monitor — gráficos, vistas y orquestador
   ============================================================ */
const { useState: vS, useEffect: vE, useRef: vR, useMemo: vM } = React;
const C = window.P2PCore;
const fP2 = window.P2P.fmtPrice, fN2 = window.P2P.fmtNum;

/* ---------- Gráfico de líneas con ejes, umbrales y hover ---------- */
function TimeChart({ series, thresholds = [], yUnit = "", height = 240, xLabels, times, decimals = 2 }) {
  const wrapRef = vR(null);
  const [w, setW] = vS(720);
  const [hover, setHover] = vS(null);
  vE(() => {
    if (!wrapRef.current) return;
    const ro = new ResizeObserver((e) => setW(e[0].contentRect.width));
    ro.observe(wrapRef.current);
    return () => ro.disconnect();
  }, []);

  const pad = { l: decimals === 1 ? 52 : 46, r: 14, t: 14, b: 26 };
  const iw = Math.max(10, w - pad.l - pad.r);
  const ih = height - pad.t - pad.b;
  const all = series.flatMap((s) => s.data);
  let min = Math.min(...all, ...thresholds.map((t) => t.value));
  let max = Math.max(...all, ...thresholds.map((t) => t.value));
  const span = (max - min) || 1; min -= span * 0.12; max += span * 0.12;
  const n = series[0].data.length;
  const xAt = (i) => pad.l + (n <= 1 ? iw / 2 : (i / (n - 1)) * iw);
  const yAt = (v) => pad.t + ih - ((v - min) / (max - min)) * ih;

  const yticks = 4;
  const gridY = Array.from({ length: yticks + 1 }, (_, i) => min + ((max - min) * i) / yticks);

  function pathFor(data) {
    return data.map((v, i) => (i ? "L" : "M") + xAt(i).toFixed(1) + " " + yAt(v).toFixed(1)).join(" ");
  }
  function areaFor(data) {
    return pathFor(data) + ` L${xAt(n - 1)} ${pad.t + ih} L${xAt(0)} ${pad.t + ih} Z`;
  }

  function onMove(e) {
    const rect = e.currentTarget.getBoundingClientRect();
    const x = e.clientX - rect.left;
    let i = Math.round(((x - pad.l) / iw) * (n - 1));
    i = Math.max(0, Math.min(n - 1, i));
    setHover(i);
  }

  return (
    <div className="chart" ref={wrapRef} style={{ height }}>
      <svg width={w} height={height} onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
        <defs>
          {series.map((s, i) => (
            <linearGradient key={i} id={"tcg" + i} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={`var(--${s.tone})`} stopOpacity="0.22" />
              <stop offset="100%" stopColor={`var(--${s.tone})`} stopOpacity="0" />
            </linearGradient>
          ))}
        </defs>
        {gridY.map((v, i) => (
          <g key={i}>
            <line x1={pad.l} x2={w - pad.r} y1={yAt(v)} y2={yAt(v)} stroke="var(--line-soft)" />
            <text x={pad.l - 8} y={yAt(v) + 3} textAnchor="end" className="ax">{v.toFixed(decimals)}{yUnit}</text>
          </g>
        ))}
        {thresholds.map((t, i) => (
          <g key={i}>
            <line x1={pad.l} x2={w - pad.r} y1={yAt(t.value)} y2={yAt(t.value)}
              stroke={`var(--${t.tone})`} strokeDasharray="4 4" strokeOpacity="0.7" />
            <text x={w - pad.r} y={yAt(t.value) - 5} textAnchor="end" className="ax th" fill={`var(--${t.tone})`}>{t.label}</text>
          </g>
        ))}
        {series.map((s, i) => (
          <g key={i}>
            {s.fill && <path d={areaFor(s.data)} fill={`url(#tcg${i})`} />}
            <path d={pathFor(s.data)} fill="none" stroke={`var(--${s.tone})`} strokeWidth={s.dashed ? 1.4 : 2}
              strokeDasharray={s.dashed ? "5 4" : "none"} strokeLinejoin="round" strokeLinecap="round" />
          </g>
        ))}
        {xLabels && xLabels.map((lb, i) => (
          <text key={i} x={xAt(lb.i)} y={height - 8} textAnchor="middle" className="ax">{lb.t}</text>
        ))}
        {hover != null && (
          <g>
            <line x1={xAt(hover)} x2={xAt(hover)} y1={pad.t} y2={pad.t + ih} stroke="var(--line)" />
            {series.map((s, i) => (
              <circle key={i} cx={xAt(hover)} cy={yAt(s.data[hover])} r="3.5" fill={`var(--${s.tone})`} stroke="var(--bg-2)" strokeWidth="1.5" />
            ))}
          </g>
        )}
      </svg>
      {hover != null && (
        <div className="chart-tip" style={{ left: Math.min(w - 150, Math.max(0, xAt(hover) + 8)) }}>
          {times && times[hover] && (
            <div className="ct-time tnum">{times[hover]}</div>
          )}
          {series.map((s, i) => (
            <div key={i} className="ct-row">
              <span className="ct-dot" style={{ background: `var(--${s.tone})` }} />
              <span className="ct-lab">{s.label}</span>
              <span className="ct-val tnum">{s.data[hover].toFixed(decimals)}{yUnit}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/* ---------- Alerta de umbral ---------- */
function AlertBanner({ snap }) {
  const [alert, setAlert] = vS(null);
  const prev = vR(snap.estado);
  vE(() => {
    if (prev.current !== snap.estado) {
      if (snap.estado === "MUY APTO")
        setAlert({ tone: "buy", txt: `Oportunidad — el mercado pasó a MUY APTO (${snap.spread_pond_pct}%)` });
      else if (snap.estado === "NO APTO" || snap.estado === "ESTRECHO")
        setAlert({ tone: "sell", txt: `Atención — spread comprimido: mercado ${snap.estado} (${snap.spread_pond_pct}%)` });
      prev.current = snap.estado;
      const t = setTimeout(() => setAlert(null), 6000);
      return () => clearTimeout(t);
    }
  }, [snap.estado, snap.spread_pond_pct]);
  if (!alert) return null;
  return <div className={"alert tone-" + alert.tone}><span className="alert-pulse" />{alert.txt}</div>;
}

/* ---------- Tiempo Real ---------- */
function TiempoReal({ snap, history, showOrderBook, filters, vel }) {
  return (
    <div className="view">
      <AlertBanner snap={snap} />
      <C.DecisionHero snap={snap} />
      <C.VelocityStrip vel={vel} />
      <div className="market">
        <C.SideCard side="buy" snap={snap} history={history} />
        <C.BrechaSpine snap={snap} />
        <C.SideCard side="sell" snap={snap} history={history} />
      </div>
      <C.MakerActions snap={snap} />
      <div className="tr-bottom">
        <section className="chart-card">
          <div className="card-head">
            <h3>Spread ponderado · últimas {history.length} muestras</h3>
            <span className="card-sub">pasá el dedo para ver la hora</span>
          </div>
          <TimeChart
            height={220}
            yUnit="%"
            times={history.map((h) => h.timestamp.slice(5, 16).replace("T", " "))}
            series={[
              { data: history.map((h) => h.spread_pond_pct), tone: "warn", label: "Spread ponderado", fill: true },
              { data: history.map((h) => h.spread_pct), tone: "accent", label: "Spread puntual", dashed: true },
            ]}
            thresholds={[
              { value: window.P2P.ALERTA_SPREAD, tone: "buy", label: "MUY APTO 0,8%" },
              { value: window.P2P.SPREAD_MINIMO, tone: "warn-low", label: "APTO 0,2%" },
            ]}
          />
        </section>
        <C.TopTraders snap={snap} />
      </div>
      {showOrderBook && <OrderBook snap={snap} />}
      {filters && <C.FiltersPanel {...filters} />}
    </div>
  );
}

/* ---------- Order book / profundidad ---------- */
function OrderBook({ snap }) {
  const buy = (snap.detalle_compra || []).slice(0, 8);
  const sell = (snap.detalle_venta || []).slice(0, 8);
  const maxV = Math.max(...buy.map((r) => r.disponible), ...sell.map((r) => r.disponible), 1);
  const Row = ({ r, tone, align }) => (
    <div className={"ob-row " + align}>
      <span className="ob-depth" style={{ width: (r.disponible / maxV * 100) + "%", background: `var(--${tone}-soft)` }} />
      <span className="ob-price tnum">{fP2(r.precio)}</span>
      <span className="ob-name">{r.anunciante}</span>
      <span className="ob-amt tnum">{fN2(r.disponible)}</span>
    </div>
  );
  return (
    <section className="orderbook">
      <div className="card-head"><h3>Libro de órdenes · profundidad</h3><span className="card-sub">top anunciantes sin filtro</span></div>
      <div className="ob-grid">
        <div className="ob-col">
          <div className="ob-coltitle tone-buy">Compra · vendedores</div>
          {buy.map((r, i) => <Row key={i} r={r} tone="buy" align="left" />)}
        </div>
        <div className="ob-col">
          <div className="ob-coltitle tone-sell">Venta · compradores</div>
          {sell.map((r, i) => <Row key={i} r={r} tone="sell" align="left" />)}
        </div>
      </div>
    </section>
  );
}

/* ---------- Histórico ---------- */
function Historico({ history }) {
  const labels = vM(() => {
    const out = [];
    const step = Math.ceil(history.length / 6);
    for (let i = 0; i < history.length; i += step) out.push({ i, t: history[i].timestamp.slice(11, 16) });
    return out;
  }, [history]);
  const times = vM(() => history.map((h) => h.timestamp.slice(5, 16).replace("T", " ")), [history]);
  const spp = history.map((h) => h.spread_pond_pct);
  const avg = (spp.reduce((a, b) => a + b, 0) / spp.length);
  const mx = Math.max(...spp), mn = Math.min(...spp);
  return (
    <div className="view">
      <div className="stat-cards">
        <StatCard label="Spread promedio" value={avg.toFixed(2) + "%"} tone="warn" />
        <StatCard label="Spread máximo" value={mx.toFixed(2) + "%"} tone="buy" />
        <StatCard label="Spread mínimo" value={mn.toFixed(2) + "%"} tone="sell" />
        <StatCard label="Muestras" value={history.length} tone="accent" />
      </div>
      <section className="chart-card">
        <div className="card-head"><h3>Precio · vendés vs comprás USDT</h3><span className="card-sub">verde = vendés USDT · rojo = comprás USDT</span></div>
        <TimeChart height={240} yUnit="" xLabels={labels} times={times} decimals={1}
          series={[
            { data: history.map((h) => h.precio_pond_tab_compra), tone: "buy", label: "Compra (vendedores)", fill: false },
            { data: history.map((h) => h.precio_pond_tab_venta), tone: "sell", label: "Venta (compradores)", fill: false },
          ]} />
      </section>
      <section className="chart-card">
        <div className="card-head"><h3>Spread ponderado vs. puntual</h3><span className="card-sub">% sobre el tiempo</span></div>
        <TimeChart height={240} yUnit="%" xLabels={labels} times={times}
          series={[
            { data: spp, tone: "warn", label: "Ponderado", fill: true },
            { data: history.map((h) => h.spread_pct), tone: "accent", label: "Puntual", dashed: true },
          ]}
          thresholds={[{ value: window.P2P.ALERTA_SPREAD, tone: "buy", label: "MUY APTO" }, { value: window.P2P.SPREAD_MINIMO, tone: "warn-low", label: "APTO" }]} />
      </section>
      <section className="chart-card">
        <div className="card-head"><h3>Liquidez por lado</h3><span className="card-sub">USDT disponible</span></div>
        <TimeChart height={220} xLabels={labels} times={times}
          series={[
            { data: history.map((h) => h.liq_tab_compra), tone: "buy", label: "Compra", fill: true },
            { data: history.map((h) => h.liq_tab_venta), tone: "sell", label: "Venta", fill: true },
          ]} />
      </section>
    </div>
  );
}

function StatCard({ label, value, tone }) {
  return (
    <div className={"statcard tone-" + tone}>
      <div className="statcard-label">{label}</div>
      <div className="statcard-val tnum">{value}</div>
    </div>
  );
}

/* ---------- Mapa de calor ---------- */
function Heatmap({ heatmap }) {
  const dias = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
  const diasEs = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"];
  const vals = heatmap.map((h) => h.avg_spread);
  const max = Math.max(...vals), min = Math.min(...vals);
  const cell = (dia, h) => heatmap.find((r) => r.dia === dia && r.hora === h);
  const colorFor = (v) => {
    if (v == null) return "transparent";
    const t = (v - min) / (max - min || 1);
    // sell(rojo) -> warn(ámbar) -> buy(verde) via opacity sobre acento de aptitud
    const tone = v >= window.P2P.ALERTA_SPREAD ? "buy" : v >= window.P2P.SPREAD_MINIMO ? "warn" : "sell";
    return `color-mix(in oklch, var(--${tone}) ${30 + t * 60}%, transparent)`;
  };
  const [tip, setTip] = vS(null);
  return (
    <div className="view">
      <section className="chart-card">
        <div className="card-head"><h3>Spread ponderado promedio · hora × día</h3><span className="card-sub">cuándo conviene operar</span></div>
        <div className="heat" onMouseLeave={() => setTip(null)}>
          <div className="heat-corner" />
          <div className="heat-hours">
            {Array.from({ length: 24 }, (_, h) => <div key={h} className="heat-h">{h % 2 === 0 ? h : ""}</div>)}
          </div>
          {dias.map((dia, di) => (
            <React.Fragment key={dia}>
              <div className="heat-day">{diasEs[di]}</div>
              <div className="heat-rowcells">
                {Array.from({ length: 24 }, (_, h) => {
                  const c = cell(dia, h);
                  return <div key={h} className="heat-cell"
                    style={{ background: colorFor(c ? c.avg_spread : null) }}
                    onMouseEnter={() => c && setTip({ d: diasEs[di], h, v: c.avg_spread })} />;
                })}
              </div>
            </React.Fragment>
          ))}
        </div>
        <div className="heat-legend">
          <span>Menos apto</span>
          <div className="heat-scale" />
          <span>Más apto</span>
          {tip && <span className="heat-tip tnum">{tip.d} {tip.h}:00 — {tip.v}%</span>}
        </div>
      </section>
    </div>
  );
}

/* ---------- Precio (gráfico interactivo Lightweight Charts) ---------- */
function PrecioChart() {
  const wrapRef = vR(null);
  const chartRef = vR(null);
  const [estado, setEstado] = vS("cargando"); // cargando | ok | vacio | sinlib | error
  const [meta, setMeta] = vS({ puntos: 0, ultCompra: null, ultVenta: null });
  const [rango, setRango] = vS("todo"); // 24h | 7d | todo
  const [brecha, setBrecha] = vS(null); // { abs, pct } al hover
  const [hoverP, setHoverP] = vS(null); // { c, v } precio al hover

  vE(() => {
    let chart = null, serieCompra = null, serieVenta = null, ro = null, cancelado = false;

    if (typeof LightweightCharts === "undefined") {
      setEstado("sinlib");
      return;
    }

    async function init() {
      try {
        const base = (window.P2P_CONFIG && window.P2P_CONFIG.baseUrl) || "";
        const r = await fetch(base + "/api/precios");
        const data = await r.json();
        if (cancelado) return;
        const compra = data.compra || [], venta = data.venta || [];
        if (!compra.length && !venta.length) { setEstado("vacio"); return; }

        // Perfil horario para sombrear el fondo según qué tan buena es cada
        // hora (índice medido). Si falla, el sombreado cae al horario fijo.
        let perfil = null;
        try {
          const rp = await fetch(base + "/api/perfil_horas");
          const jp = await rp.json();
          if (jp && jp.filas && jp.filas.length) {
            perfil = {};
            jp.filas.forEach(f => { perfil[f.hora] = f.indice; });
          }
        } catch (e) { /* sin perfil: se usa el sombreado fijo */ }
        if (cancelado) return;

        const el = wrapRef.current;
        if (!el) return;
        chart = LightweightCharts.createChart(el, {
          layout: {
            background: { color: "transparent" },
            textColor: "rgba(220,226,238,0.7)",
            fontFamily: "Inter, system-ui, sans-serif",
          },
          grid: {
            vertLines: { color: "rgba(255,255,255,0.05)" },
            horzLines: { color: "rgba(255,255,255,0.05)" },
          },
          rightPriceScale: { borderColor: "rgba(255,255,255,0.1)" },
          timeScale: {
            borderColor: "rgba(255,255,255,0.1)",
            timeVisible: true,
            secondsVisible: false,
            tickMarkFormatter: (time) => {
              const d = new Date(time * 1000);
              // Mostrar hora Santiago (UTC-4)
              const h = String(d.getUTCHours()).padStart(2,"0");
              const m = String(d.getUTCMinutes()).padStart(2,"0");
              return h + ":" + m;
            },
          },
          localization: {
            timeFormatter: (time) => {
              const d = new Date(time * 1000);
              const dd = String(d.getUTCDate()).padStart(2,"0");
              const mm = String(d.getUTCMonth()+1).padStart(2,"0");
              const h = String(d.getUTCHours()).padStart(2,"0");
              const min = String(d.getUTCMinutes()).padStart(2,"0");
              return dd + "/" + mm + " " + h + ":" + min;
            },
          },
          crosshair: {
            mode: LightweightCharts.CrosshairMode.Normal,
            vertLine: { labelBackgroundColor: "#2a2a40" },
            horzLine: { labelBackgroundColor: "#2a2a40" },
          },
          handleScroll: true,
          handleScale: true,
          height: 420,
        });

        serieCompra = chart.addLineSeries({
          color: "#35e07a", lineWidth: 2, lastValueVisible: true, priceLineVisible: false,
          priceFormat: { type: "price", precision: 2, minMove: 0.01 },
        });
        serieVenta = chart.addLineSeries({
          color: "#ff5d6c", lineWidth: 2, lastValueVisible: true, priceLineVisible: false,
          priceFormat: { type: "price", precision: 2, minMove: 0.01 },
        });
        serieCompra.setData(compra);
        serieVenta.setData(venta);

        // ── Bandas de contexto (COL19) ──────────────────────────────────
        // Dos series histograma sobre una escala superpuesta, sólo para pintar
        // el fondo: no llevan datos de precio, por eso van con priceScaleId ""
        // y sin etiquetas. Los timestamps ya vienen corridos a hora Chile, así
        // que getUTCHours() devuelve la hora local.
        const serieBase = (compra.length >= venta.length ? compra : venta);
        if (serieBase.length) {
          // 1) intensidad del sombreado según qué tan buena es cada hora
          //    (índice medido spread × flujo). Si el perfil aún no está,
          //    cae a un sombreado plano de 9 a 16h.
          // Ámbar, y sólo en las horas que valen: por debajo de 45 de índice no
          // se pinta nada. Así quedan BANDAS nítidas en vez de un degradado
          // parejo que no se distingue del fondo.
          const pintar = (t) => {
            const h = new Date(t * 1000).getUTCHours();
            let idx;
            if (perfil && perfil[h] != null) idx = perfil[h];
            else idx = (h >= 9 && h <= 16) ? 80 : 0;   // respaldo si no hay perfil
            if (idx < 45) return "rgba(0,0,0,0)";       // hora floja: transparente
            // 45→100 se mapea a 0,06→0,20 de opacidad
            const op = 0.06 + ((idx - 45) / 55) * 0.14;
            return "rgba(224,146,42," + op.toFixed(3) + ")";
          };
          const banda = chart.addHistogramSeries({
            priceScaleId: "", priceLineVisible: false, lastValueVisible: false,
            baseLineVisible: false,
          });
          banda.priceScale().applyOptions({ scaleMargins: { top: 0, bottom: 0 } });
          banda.setData(serieBase.map(p => ({ time: p.time, value: 1, color: pintar(p.time) })));

          // 2) línea vertical en cada cambio de día
          const dias = [];
          let ultDia = null;
          serieBase.forEach(p => {
            const d = new Date(p.time * 1000).getUTCDate();
            if (ultDia !== null && d !== ultDia) {
              dias.push({ time: p.time, value: 1, color: "rgba(255,255,255,0.22)" });
            }
            ultDia = d;
          });
          if (dias.length) {
            const sep = chart.addHistogramSeries({
              priceScaleId: "", priceLineVisible: false, lastValueVisible: false,
              baseLineVisible: false,
            });
            sep.priceScale().applyOptions({ scaleMargins: { top: 0, bottom: 0 } });
            sep.setData(dias);
          }
        }

        chart.timeScale().fitContent();

        // Doble toque en móvil → volver a vista completa
        el.addEventListener("dblclick", () => chart.timeScale().fitContent());

        chartRef.current = { chart, serieCompra, serieVenta, compra, venta };
        setMeta({
          puntos: compra.length,
          ultCompra: compra.length ? compra[compra.length - 1].value : null,
          ultVenta: venta.length ? venta[venta.length - 1].value : null,
        });
        setEstado("ok");

        // Brecha en tiempo real al mover el crosshair
        chart.subscribeCrosshairMove((param) => {
          if (!param.time || !param.seriesData) { setBrecha(null); return; }
          const pc = param.seriesData.get(serieCompra);
          const pv = param.seriesData.get(serieVenta);
          if (pc && pv) {
            const abs = pc.value - pv.value;
            const pct = (abs / pv.value) * 100;
            setBrecha({ abs: abs.toFixed(2), pct: pct.toFixed(3) });
            setHoverP({ c: pc.value, v: pv.value });
          } else {
            setBrecha(null); setHoverP(null);
          }
        });

        ro = new ResizeObserver((ents) => {
          if (chart && ents[0]) chart.applyOptions({ width: ents[0].contentRect.width });
        });
        ro.observe(el);
        chart.applyOptions({ width: el.clientWidth });
      } catch (e) {
        if (!cancelado) setEstado("error");
      }
    }
    init();

    return () => {
      cancelado = true;
      if (ro) ro.disconnect();
      if (chart) chart.remove();
      chartRef.current = null;
    };
  }, []);

  // Botones de rango rápido
  function aplicarRango(cual) {
    setRango(cual);
    const ref = chartRef.current;
    if (!ref) return;
    const { chart, compra } = ref;
    if (!compra.length) return;
    if (cual === "todo") {
      chart.timeScale().fitContent();
      return;
    }
    const ahora = compra[compra.length - 1].time;
    const primero = compra[0].time;
    const dias = cual === "24h" ? 1 : cual === "7d" ? 7 : 30;
    const desde = Math.max(primero, ahora - dias * 86400);
    chart.timeScale().setVisibleRange({ from: desde, to: ahora + 3600 });
  }

  return (
    <div className="view">
      <section className="chart-card">
        <div className="card-head">
          <h3>Precio ponderado · histórico interactivo</h3>
          <span className="card-sub">pellizcá para zoom · arrastrá para mover</span>
        </div>

        {estado === "ok" && (
          <div className="precio-top">
            <div className="precio-leg">
              <span className="pl-item"><span className="pl-dot" style={{ background: "#35e07a" }} />Vendés USDT <b style={{marginLeft:4}}>{hoverP ? "$" + hoverP.c.toFixed(2) : (meta.ultCompra ? "$" + meta.ultCompra.toFixed(2) : "")}</b></span>
              <span className="pl-item"><span className="pl-dot" style={{ background: "#ff5d6c" }} />Comprás USDT <b style={{marginLeft:4}}>{hoverP ? "$" + hoverP.v.toFixed(2) : (meta.ultVenta ? "$" + meta.ultVenta.toFixed(2) : "")}</b></span>
              {brecha && (
                <span className="pl-brecha tnum">
                  Brecha <b>${brecha.abs}</b> · <b>{brecha.pct}%</b>
                </span>
              )}
            </div>
            <div className="precio-rangos">
              {[["24h", "24h"], ["7d", "7 días"], ["30d", "30 días"], ["todo", "Todo"]].map(([k, lbl]) => (
                <button key={k} className={"pr-btn" + (rango === k ? " on" : "")} onClick={() => aplicarRango(k)}>{lbl}</button>
              ))}
            </div>
          </div>
        )}

        <div ref={wrapRef} className="precio-chart" style={{ width: "100%", height: 420 }} />

        {estado === "cargando" && <div className="precio-msg">Cargando histórico de precios…</div>}
        {estado === "vacio" && <div className="precio-msg">Todavía no hay datos de precio acumulados. Esperá unos ciclos.</div>}
        {estado === "sinlib" && <div className="precio-msg">No se pudo cargar la librería del gráfico. Revisá tu conexión y recargá.</div>}
        {estado === "error" && <div className="precio-msg">Error al cargar los precios. Recargá la página.</div>}
        {estado === "ok" && <div className="precio-foot tnum">{window.P2P.fmtNum(meta.puntos)} puntos · doble toque para volver a la vista completa</div>}
      </section>
    </div>
  );
}

/* ─────────── INTELIGENCIA DE MERCADO ─────────── */
function PerfilHoras() {
  const B = (window.P2P_CONFIG && window.P2P_CONFIG.baseUrl) || "";
  const [d, setD] = React.useState(null);
  React.useEffect(() => {
    let stop = false;
    fetch(B + "/api/perfil_horas").then(r => r.json()).then(j => { if (!stop) setD(j); }).catch(() => {});
    return () => { stop = true; };
  }, []);
  if (!d) return <div className="intel-loading">Cargando perfil horario…</div>;
  const filas = d.filas || [];
  if (!filas.length) return <div className="intel-loading">Todavía sin perfil horario (se calcula al arrancar el monitor).</div>;
  const f = (x, n) => x == null ? "—" : Number(x).toFixed(n == null ? 2 : n);
  const ahora = new Date().getHours();
  const mejores = filas.slice().sort((a, b) => b.indice - a.indice).slice(0, 3).map(r => r.hora);
  return (
    <section className="chart-card">
      <div className="card-head">
        <h3>Perfil por hora — cuándo conviene operar</h3>
        <span className="card-sub">73 días de spread × flujo medido · el gap sugerido sigue al spread de cada hora</span>
      </div>
      <div className="intel-scroll">
        <table className="intel-table">
          <thead><tr>
            <th title="Hora del día, horario Chile.">Hora</th>
            <th title="Qué tan buena es la hora para farmear. Combina cuánto margen hay (spread) con cuánta gente opera (flujo). 100 = la mejor hora del día.">Índice</th>
            <th></th>
            <th title="Spread mediano del mercado en esa hora: el margen bruto disponible.">Spread</th>
            <th title="Órdenes que se completan en esa hora, en promedio por día. Es lo que más pesa en el índice.">Flujo</th>
            <th title="El gap que conviene usar en esa hora. Sigue al spread: un gap fijo queda ancho al mediodía y angosto de madrugada.">Gap sugerido</th>
            <th title="Qué porcentaje del tiempo el semáforo dio verde o amarillo en esa hora (últimos 14 días).">% operable</th>
          </tr></thead>
          <tbody>{filas.map(r => {
            const c = r.indice >= 75 ? "#35e07a" : r.indice >= 55 ? "#ffd740" : r.indice >= 35 ? "#ff9100" : "#ff5d6c";
            const esAhora = r.hora === ahora;
            return (
              <tr key={r.hora} style={{ borderLeft: `3px solid ${c}`, background: esAhora ? "var(--bg-2)" : undefined }}>
                <td><b className="tnum">{String(r.hora).padStart(2, "0")}h</b>{esAhora && <span style={{ fontSize: 9, color: "var(--accent)" }}> ahora</span>}{mejores.indexOf(r.hora) >= 0 && <span style={{ fontSize: 10 }}> ★</span>}</td>
                <td className="tnum" style={{ color: c, fontWeight: 600 }}>{f(r.indice, 0)}</td>
                <td style={{ width: 120 }}>
                  <div style={{ background: "var(--bg-3)", borderRadius: 3, height: 7, width: 110 }}>
                    <div style={{ background: c, width: Math.max(2, r.indice / 100 * 110), height: 7, borderRadius: 3 }} />
                  </div>
                </td>
                <td className="tnum">{f(r.spread_med, 3)}%</td>
                <td className="tnum">{f(r.flujo_ordenes, 0)}</td>
                <td className="tnum" style={{ fontWeight: 600 }}>{r.gap_sugerido == null ? "—" : f(r.gap_sugerido) + "%"}</td>
                <td className="tnum" style={{ color: "var(--text-3)" }}>{r.pct_operable == null ? "—" : f(r.pct_operable, 0) + "%"}</td>
              </tr>
            );
          })}</tbody>
        </table>
      </div>
      <div className="intel-explain">
        <b>El hallazgo que ordena todo esto:</b> el <b>flujo</b> varía 90 veces entre horas (de 5 órdenes/hora de madrugada a 449 al mediodía) mientras el <b>spread</b> solo varía 3 veces. Como el flujo pesa mucho más, es el que manda.<br/>
        <b>La trampa de la madrugada:</b> a las 4-5h el spread es el más ancho del día (1,26%), pero pasan 5 órdenes por hora. Margen inmejorable y nadie con quien operar.<br/>
        <b>Qué hacer:</b> operá en las horas con índice alto, y usá el <b>gap sugerido</b> de esa hora — no un gap fijo. Las marcadas con ★ son las tres mejores del día.
      </div>
    </section>
  );
}

function FichaAnunciante() {
  const B = (window.P2P_CONFIG && window.P2P_CONFIG.baseUrl) || "";
  const [q, setQ] = React.useState("");
  const [lista, setLista] = React.useState(null);
  const [sel, setSel] = React.useState(null);
  const [ficha, setFicha] = React.useState(null);
  React.useEffect(() => {
    let stop = false;
    fetch(B + "/api/anunciante?q=" + encodeURIComponent(q)).then(r => r.json())
      .then(j => { if (!stop) setLista(j.lista || []); }).catch(() => {});
    return () => { stop = true; };
  }, [q]);
  React.useEffect(() => {
    if (!sel) { setFicha(null); return; }
    let stop = false;
    fetch(B + "/api/anunciante?nombre=" + encodeURIComponent(sel)).then(r => r.json())
      .then(j => { if (!stop) setFicha(j); }).catch(() => {});
    return () => { stop = true; };
  }, [sel]);
  const fN = (v) => v == null ? "—" : Number(v).toLocaleString("es-CL");
  const boxSt = { flex: 1, minWidth: 130, background: "var(--bg-2)", border: "1px solid var(--line-soft)", borderRadius: 10, padding: "9px 12px" };
  const lbl = { fontSize: 9.5, color: "var(--text-3)", textTransform: "uppercase", letterSpacing: "0.08em" };
  const val = { fontFamily: "var(--mono)", fontSize: 17, color: "var(--text)", margin: "3px 0 1px" };
  return (
    <section className="chart-card">
      <div className="card-head">
        <h3>Ficha del competidor</h3>
        <span className="card-sub">órdenes reales del contador oficial · volumen y ticket estimados</span>
      </div>
      <input value={q} onChange={e => setQ(e.target.value)} placeholder="Buscar anunciante por nombre…"
        style={{ width: "100%", maxWidth: 340, background: "var(--bg-2)", border: "1px solid var(--line)",
                 color: "var(--text)", padding: "8px 11px", borderRadius: 9, fontFamily: "var(--mono)", fontSize: 12.5, marginBottom: 12 }} />
      <div style={{ display: "flex", gap: 14, flexWrap: "wrap", alignItems: "flex-start" }}>
        <div style={{ minWidth: 250, flex: "0 0 auto", maxHeight: 340, overflowY: "auto" }}>
          {(lista || []).map(a => (
            <div key={a.anunciante} onClick={() => setSel(a.anunciante)}
              style={{ cursor: "pointer", padding: "6px 9px", borderRadius: 7, marginBottom: 2, fontSize: 12,
                       background: sel === a.anunciante ? "var(--accent-soft)" : "transparent",
                       border: "1px solid " + (sel === a.anunciante ? "var(--accent)" : "transparent"),
                       display: "flex", justifyContent: "space-between", gap: 10 }}>
              <span style={{ color: "var(--text)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {a.merchant && <span className="merch">✦ </span>}{a.anunciante}
              </span>
              <span className="tnum" style={{ color: "var(--text-3)", whiteSpace: "nowrap" }}>{fN(a.ordenes)} órd</span>
            </div>
          ))}
          {lista && lista.length === 0 && <div style={{ fontSize: 12, color: "var(--text-3)", padding: 8 }}>Sin resultados.</div>}
        </div>
        <div style={{ flex: 1, minWidth: 300 }}>
          {!sel && <div style={{ fontSize: 12.5, color: "var(--text-3)", padding: "20px 0" }}>Elegí un anunciante de la lista para ver su ficha.</div>}
          {sel && ficha && ficha.encontrado === false && <div style={{ fontSize: 12.5, color: "var(--text-3)" }}>Sin datos suficientes de {sel}.</div>}
          {sel && ficha && ficha.encontrado && (
            <div>
              <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10, flexWrap: "wrap" }}>
                <span style={{ fontSize: 15, fontWeight: 600 }}>{ficha.merchant && <span className="merch">✦ </span>}{ficha.nombre}</span>
                {ficha.dual_ahora && <span style={{ fontSize: 10, color: "var(--buy)", border: "1px solid var(--buy)", borderRadius: 5, padding: "1px 6px" }}>DUAL AHORA</span>}
                {!ficha.en_libro_ahora && <span style={{ fontSize: 10.5, color: "var(--text-3)" }}>· no está en el libro ahora</span>}
              </div>
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 10 }}>
                <div style={boxSt}>
                  <div style={lbl}>Órdenes por día</div>
                  <div style={val}>{fN(ficha.ordenes_dia_prom)}</div>
                  <div style={{ fontSize: 10, color: "var(--text-3)" }}>contador oficial · {ficha.dias_observado} días</div>
                </div>
                <div style={boxSt}>
                  <div style={lbl}>Volumen 30d</div>
                  <div style={val}>{fN(ficha.volumen_30d)}</div>
                  <div style={{ fontSize: 10, color: "var(--text-3)" }}>USDT (estimado)</div>
                </div>
                <div style={boxSt}>
                  <div style={lbl}>Ticket medio</div>
                  <div style={val}>{fN(ficha.ticket_medio)}</div>
                  <div style={{ fontSize: 10, color: "var(--text-3)" }}>USDT por orden</div>
                </div>
                <div style={boxSt}>
                  <div style={lbl}>Gap propio</div>
                  <div style={{ ...val, color: ficha.gap_propio_pct ? "var(--warn)" : "var(--text-3)" }}>
                    {ficha.gap_propio_pct != null ? ficha.gap_propio_pct + "%" : "—"}
                  </div>
                  <div style={{ fontSize: 10, color: "var(--text-3)" }}>{ficha.gap_propio_pct != null ? "su margen bruto" : "sólo si está dual"}</div>
                </div>
                {ficha.ganancia_30d_estimada_usdt != null && (
                  <div style={boxSt}>
                    <div style={lbl}>Ganancia 30d est.</div>
                    <div style={{ ...val, color: "var(--buy)" }}>{fN(ficha.ganancia_30d_estimada_usdt)}</div>
                    <div style={{ fontSize: 10, color: "var(--text-3)" }}>USDT, neto de comisión</div>
                  </div>
                )}
              </div>
              {ficha.en_libro_ahora && (
                <div style={{ fontSize: 11.5, color: "var(--text-2)", marginBottom: 10 }}>
                  {["venta", "compra"].map(k => ficha.posiciones[k] && (
                    <span key={k} style={{ marginRight: 14 }}>
                      <b style={{ color: k === "venta" ? "var(--buy)" : "var(--sell)" }}>{k === "venta" ? "vende" : "compra"}</b>
                      {" "}#{ficha.posiciones[k].posicion} a ${fN(ficha.posiciones[k].precio)} · {fN(ficha.posiciones[k].disponible)} USDT
                    </span>
                  ))}
                </div>
              )}
              <div className="intel-scroll">
                <table className="intel-table">
                  <thead><tr><th>Fecha</th><th title="Órdenes completadas ese día, del contador oficial de Binance.">Órdenes</th><th title="Posición media en el libro ese día.">Pos. media</th><th title="Stock promedio publicado.">Stock</th></tr></thead>
                  <tbody>{(ficha.serie || []).map(s => (
                    <tr key={s.fecha}>
                      <td className="tnum">{s.fecha}</td>
                      <td className="tnum" style={{ fontWeight: 600 }}>{fN(s.ordenes)}</td>
                      <td className="tnum">{s.pos_media == null ? "—" : "#" + s.pos_media}</td>
                      <td className="tnum" style={{ color: "var(--text-3)" }}>{fN(s.stock)}</td>
                    </tr>
                  ))}</tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      </div>
      <div className="intel-explain">
        <b>Qué mirar:</b> el <b>gap propio</b> de los que más órdenes hacen es la referencia más útil que tenés — es el margen que el mercado les está pagando hoy por farmear. Si el tuyo está muy lejos, ajustalo.<br/>
        <b>Órdenes por día</b> sale del contador oficial de Binance, no es estimación nuestra: es el dato más confiable de la ficha. El volumen y el ticket sí son estimados por el tracker.<br/>
        <b>Ojo con la historia:</b> arranca el 19 de julio, que es cuando empezamos a congelar los agregados diarios. De acá en adelante se acumula sola.
      </div>
    </section>
  );
}

function CruzarOEsperar() {
  const B = (window.P2P_CONFIG && window.P2P_CONFIG.baseUrl) || "";
  const [usdt, setUsdt] = React.useState(200);
  const [d, setD] = React.useState(null);
  React.useEffect(() => {
    let stop = false;
    const load = () => fetch(B + "/api/taker_maker?usdt=" + usdt)
      .then(r => r.json()).then(j => { if (!stop) setD(j); }).catch(() => {});
    load();
    const id = setInterval(load, 30000);
    return () => { stop = true; clearInterval(id); };
  }, [usdt]);
  const f = (x, n) => x == null ? "—" : Number(x).toFixed(n == null ? 3 : n);
  if (!d || d.error) return <div className="intel-loading">Esperando datos del libro…</div>;
  const u = d.umbral || {}, lib = d.libro || {};
  const cruzarGana = u.ventaja_cruzar_pct > 0;
  const tono = cruzarGana ? "var(--buy)" : "var(--warn)";
  return (
    <section className="chart-card">
      <div className="card-head">
        <h3>Cruzar o esperar</h3>
        <span className="card-sub">comisión taker FIJA ({d.comisiones.taker_fija_usdt} USDT) vs maker {d.comisiones.maker_pct}% · en vivo</span>
      </div>

      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", marginBottom: 14 }}>
        <span style={{ fontSize: 11.5, color: "var(--text-3)" }}>Tamaño de la orden:</span>
        {[50, 100, 200, 500, 1000].map(v => (
          <button key={v} className={"pr-btn" + (usdt === v ? " on" : "")} onClick={() => setUsdt(v)}>{v} USDT</button>
        ))}
      </div>

      <div style={{ background: "var(--bg-2)", border: "1px solid " + tono, borderLeft: "4px solid " + tono, borderRadius: 12, padding: "14px 16px", marginBottom: 14 }}>
        <div style={{ fontFamily: "var(--mono)", fontSize: 18, fontWeight: 600, color: tono, marginBottom: 4 }}>
          {cruzarGana ? "CRUZAR sale más barato" : "PUBLICAR y esperar sale más barato"}
        </div>
        <div style={{ fontSize: 12.5, color: "var(--text-2)", lineHeight: 1.6 }}>
          {u.tamano_equilibrio_usdt
            ? <>Con el spread actual de <b>{f(lib.spread_pct)}%</b>, cruzar conviene en órdenes desde <b>{u.tamano_equilibrio_usdt} USDT</b>. </>
            : <>El spread actual (<b>{f(lib.spread_pct)}%</b>) supera la comisión maker ({d.comisiones.maker_pct}%): con este spread <b>no conviene cruzar a ningún tamaño</b>. </>}
          Para esta orden de {d.usdt_evaluado} USDT la diferencia es de <b style={{ color: tono }}>{u.ventaja_cruzar_pct > 0 ? "+" : ""}{f(u.ventaja_cruzar_pct)}%</b> a favor de {cruzarGana ? "cruzar" : "publicar"}.
        </div>
        <div style={{ fontSize: 11, color: "var(--text-3)", marginTop: 6 }}>
          Umbral: cruzar gana mientras el spread sea menor a {f(u.spread_limite_pct)}% · libro: compra ${f(lib.ask, 2)} / venta ${f(lib.bid, 2)}
        </div>
      </div>

      <div className="muros-cols">
        {(d.piernas || []).map(p => (
          <div key={p.pierna} style={{ background: "var(--bg-1)", border: "1px solid var(--line)", borderRadius: 10, padding: "12px 14px" }}>
            <div style={{ fontSize: 12, fontWeight: 600, color: "var(--text)", textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 8 }}>
              Para {p.pierna}
            </div>
            {["cruzar", "esperar"].map(k => {
              const o = p[k], gana = p.conviene === k;
              return (
                <div key={k} style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 10, padding: "8px 10px", borderRadius: 8, marginBottom: 6,
                  background: gana ? "var(--buy-soft)" : "var(--bg-2)", border: "1px solid " + (gana ? "var(--buy)" : "var(--line-soft)") }}>
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontSize: 12, color: "var(--text)", fontWeight: gana ? 600 : 400 }}>
                      {gana ? "✓ " : ""}{k === "cruzar" ? "Cruzar" : "Publicar y esperar"}
                    </div>
                    <div style={{ fontSize: 10.5, color: "var(--text-3)" }}>{o.accion} · ${f(o.precio, 2)}</div>
                    <div style={{ fontSize: 10.5, color: "var(--text-3)" }}>
                      comisión {o.comision_usdt} USDT · {o.demora_min === 0 ? "instantáneo" : (o.demora_min ? "~" + o.demora_min + " min de espera" : "espera desconocida")}
                    </div>
                  </div>
                  <div className="tnum" style={{ fontSize: 14, color: gana ? "var(--buy)" : "var(--text-2)", whiteSpace: "nowrap" }}>{f(o.costo_total_pct)}%</div>
                </div>
              );
            })}
          </div>
        ))}
      </div>

      <div style={{ marginTop: 16 }}>
        <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 8 }}>Los 4 caminos de una vuelta completa</div>
        <div className="intel-scroll">
          <table className="intel-table">
            <thead><tr>
              <th title="Combinación de cómo hacés cada pierna.">Camino</th>
              <th title="Qué implica.">Cómo</th>
              <th title="Costo total de la vuelta (comisiones + spread), en % sobre el precio medio. Menos es mejor.">Costo vuelta</th>
              <th title="Cuánto tardarías en completar la vuelta, según la curva de llenado medida.">Demora</th>
              <th title="Vueltas por hora teóricas si sólo dependiera del tiempo de llenado.">Vueltas/h</th>
            </tr></thead>
            <tbody>{(d.caminos || []).map((c, i) => (
              <tr key={c.nombre} style={{ borderLeft: `3px solid ${i === 0 ? "var(--buy)" : "var(--line)"}` }}>
                <td style={{ fontWeight: i === 0 ? 700 : 400 }}>{i === 0 ? "★ " : ""}{c.nombre}</td>
                <td style={{ color: "var(--text-3)", fontSize: 11 }}>{c.detalle}</td>
                <td className="tnum" style={{ color: i === 0 ? "var(--buy)" : "var(--text)", fontWeight: 600 }}>{f(c.costo_vuelta_pct)}%</td>
                <td className="tnum">{c.demora_estimada_min == null ? "—" : c.demora_estimada_min === 0 ? "instantáneo" : "~" + c.demora_estimada_min + " min"}</td>
                <td className="tnum" style={{ color: "var(--text-3)" }}>{c.ordenes_h_teoricas == null ? "sin límite" : c.ordenes_h_teoricas}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      </div>

      <div className="intel-explain">
        <b>De dónde sale esto:</b> la comisión taker es un <b>monto fijo</b> (0,07 USDT medidos en tus propias órdenes), mientras la maker es un <b>porcentaje</b> (0,19%). Por eso el que conviene depende del tamaño: cuanto más grande la orden, más barato sale cruzar. La cuenta es <b>spread + comisión_fija/monto vs 0,19%</b>.<br/>
        <b>OJO, el supuesto importante:</b> "publicar y esperar" acá asume que te llenás <b>al precio del líder</b>, o sea compitiendo de igual a igual. Si publicás con un gap más ancho (como el 0,6% del farming) capturás más margen, pero llenás más lento. Esta comparación es <b>a igual velocidad</b>.<br/>
        <b>Para la campaña:</b> las órdenes cruzadas <b>también suman</b> al contador de Merchant y llenan al instante. Si te falta volumen y te sobra spread barato, cruzar compra tiempo.
      </div>
    </section>
  );
}

function Inteligencia() {
  const B = (window.P2P_CONFIG && window.P2P_CONFIG.baseUrl) || "";
  const [horario, setHorario] = vS(null);
  const [anunciantes, setAnunciantes] = vS(null);
  const [traders, setTraders] = vS(null);
  const [fill, setFill] = vS(null);
  const [patron, setPatron] = vS(null);
  const [profundidad, setProfundidad] = vS(null);
  const [precioFill, setPrecioFill] = vS(null);
  const [ventanas, setVentanas] = vS(null);
  const [farmers, setFarmers] = vS(null);
  const [curva, setCurva] = vS(null);
  const [loading, setLoading] = vS(true);
  const [seccion, setSeccion] = vS("perfilhoras");

  vE(() => {
    setLoading(true);
    Promise.all([
      fetch(B+"/api/inteligencia/horario").then(r=>r.json()),
      fetch(B+"/api/inteligencia/anunciantes").then(r=>r.json()),
      fetch(B+"/api/inteligencia/top_traders").then(r=>r.json()),
      fetch(B+"/api/inteligencia/fill").then(r=>r.json()),
      fetch(B+"/api/inteligencia/precio_patron").then(r=>r.json()),
      fetch(B+"/api/inteligencia/profundidad").then(r=>r.json()),
      fetch(B+"/api/inteligencia/precio_vs_fill").then(r=>r.json()),
      fetch(B+"/api/inteligencia/ventanas_reales").then(r=>r.json()).catch(()=>[]),
      fetch(B+"/api/inteligencia/farmers").then(r=>r.json()).catch(()=>[]),
      fetch(B+"/api/inteligencia/curva_llenado").then(r=>r.json()).catch(()=>({filas:[]})),
    ]).then(([h,a,t,f,p,prof,pvf,vr,fa,cl]) => {
      setHorario(h); setAnunciantes(a); setTraders(t); setFill(f); setPatron(p);
      setProfundidad(Array.isArray(prof) ? prof : (prof.datos || []));
      setPrecioFill(Array.isArray(pvf) ? pvf : (pvf.datos || []));
      setVentanas(Array.isArray(vr) ? vr : []);
      setFarmers(Array.isArray(fa) ? fa : []);
      setCurva((cl && cl.filas) ? cl.filas : []);
      setLoading(false);
    }).catch(()=>setLoading(false));
  }, []);

  const fN = (v) => v != null ? Number(v).toLocaleString("es-CL") : "—";
  const fC = (v) => v != null ? "$"+parseFloat(v).toFixed(2) : "—";

  // Agrupadas por DECISIÓN, no por origen del dato (COL18). El grupo se
  // muestra como separador para que se entienda qué pregunta responde cada una.
  // COL19: de 10 pestañas a 6. Las 3 vistas por hora (Ventanas reales /
  // Horario / Patrones) se fusionaron en "Perfil por hora", que mide lo mismo
  // con 73 días y mejor método. Pares y Top traders quedaron absorbidas por la
  // Ficha del competidor, que muestra todo eso y más para cualquiera.
  const GRUPOS = [
    ["CUÁNDO",        [["perfilhoras","🕐 Perfil por hora"]]],
    ["DÓNDE Y CÓMO",  [["curva","📍 Dónde pararme"],["cruzar","⚖️ Cruzar o esperar"],["preciofill","💡 Precio vs Fill"],["profundidad","📊 Profundidad"]]],
    ["CONTRA QUIÉN",  [["ficha","🔍 Ficha del competidor"],["farmers","🌾 Farmers"]]],
  ];

  if (loading) return <div className="intel-loading">Consultando base de datos…</div>;

  return (
    <div className="view">
      <div className="intel-tabs" style={{alignItems:"center"}}>
        {GRUPOS.map(([grupo, items], gi)=>(
          <React.Fragment key={grupo}>
            <span style={{fontSize:9.5,color:"var(--text-3)",textTransform:"uppercase",letterSpacing:"0.12em",
                          marginLeft: gi ? 10 : 0, marginRight:2, whiteSpace:"nowrap"}}>{grupo}</span>
            {items.map(([k,lbl])=>(
              <button key={k} className={"intel-tab"+(seccion===k?" active":"")} onClick={()=>setSeccion(k)}>{lbl}</button>
            ))}
          </React.Fragment>
        ))}
      </div>

      {seccion==="cruzar" && <CruzarOEsperar />}
      {seccion==="ficha" && <FichaAnunciante />}
      {seccion==="perfilhoras" && <PerfilHoras />}

      {seccion==="horario" && horario && (
        <section className="chart-card">
          <div className="card-head"><h3>Ventanas operativas por hora</h3><span className="card-sub">últimos 7 días · spread neto con la comisión vigente (hoy 0,4% · al verificarte Bronce 0,32%)</span></div>
          <div className="intel-scroll">
            <table className="intel-table">
              <thead><tr>
                <th title="Hora del día en horario Santiago (Chile)">Hora</th>
                <th title="Ganancia neta estimada por vuelta: diferencia entre precio compra y venta, descontando la comisión configurada (hoy 0,4% = 0,2% × 2 piernas; al verificarte Bronce baja a 0,32%). Ej: +1.2% significa que por cada 1.000 USDT ganás $12.">Spread neto</th>
                <th title="USDT disponibles en el lado de compra (Tab Compra). Cuánto hay para vender. Mayor número = más fácil llenar tu orden de venta.">Liq. Compra</th>
                <th title="USDT disponibles en el lado de venta (Tab Venta). Cuánto hay para comprar. Mayor número = más fácil reponerte de USDT.">Liq. Venta</th>
                <th title="Cantidad de snapshots tomados en esa hora. Más muestras = dato más confiable.">Muestras</th>
                <th title="Verde 🔥 = muy rentable (>1%). Amarillo ✅ = rentable (>0.5%). Naranja ⚠️ = marginal (>0.35%). Rojo ❌ = no vale la pena.">Semáforo</th>
              </tr></thead>
              <tbody>{horario.map(r=>{
                const sn=parseFloat(r.spread_neto||0);
                const color=sn>=1.0?"#35e07a":sn>=0.5?"#ffd740":sn>=0.35?"#ff9100":"#ff5d6c";
                const label=sn>=1.0?"🔥 PICO":sn>=0.5?"✅ BUENO":sn>=0.35?"⚠️ MARG.":"❌";
                return <tr key={r.hora} style={{borderLeft:`3px solid ${color}`}}>
                  <td><b className="tnum">{String(r.hora).padStart(2,"0")}h</b></td>
                  <td style={{color,fontWeight:600}} className="tnum">{sn>=0?"+":""}{sn.toFixed(3)}%</td>
                  <td className="tnum">{fN(r.liq_compra)}</td>
                  <td className="tnum">{fN(r.liq_venta)}</td>
                  <td className="tnum" style={{color:"var(--text-3)"}}>{r.muestras}</td>
                  <td style={{fontSize:12}}>{label}</td>
                </tr>;
              })}</tbody>
            </table>
          </div>
          <div className="intel-explain">
            <b>Cómo leer esta tabla:</b> buscá las horas con semáforo 🔥 o ✅ — ahí es donde conviene operar porque la diferencia entre lo que te pagan por vender USDT y lo que pagás por comprarlos es suficiente para cubrir la comisión y ganar. Las horas ❌ tienen spread casi cero: mover capital ahí es trabajar para no ganar nada.<br/>
            <b>Qué hacer:</b> si tu turno libre empieza a las 04h, mirá si el spread neto supera +0.5% antes de publicar tu anuncio. Si está en rojo, esperá una hora.
          </div>
        </section>
      )}

      {seccion==="ventanas" && ventanas && (
        <section className="chart-card">
          <div className="card-head"><h3>Ventanas reales (semáforo medido)</h3><span className="card-sub">últimos 7 días · decisiones del asistente cada ~5 min · % del tiempo operable por hora</span></div>
          {ventanas.length === 0 && <div className="intel-loading">Todavía no hay decisiones registradas. El asistente graba una cada ~5 min desde que el monitor está vivo.</div>}
          {ventanas.length > 0 && (
          <div className="intel-scroll">
            <table className="intel-table">
              <thead><tr>
                <th title="Hora del día en horario Santiago (Chile)">Hora</th>
                <th title="% del tiempo con semáforo verde o amarillo (OPERAR DUAL pleno o paciente). Es la probabilidad medida de encontrar ventana a esa hora.">% operable</th>
                <th title="% del tiempo con verde pleno (OPERAR DUAL).">% verde</th>
                <th title="Spread neto promedio de esa hora (con la comisión vigente al decidir).">Spread neto med.</th>
                <th title="Rotación promedio vs el promedio de las 12h previas (1x = normal, menos = lento).">Rotación med.</th>
                <th title="Presión compradora promedio (50% = equilibrado; más = domina la compra).">Presión med.</th>
                <th title="Decisiones registradas en esa hora (~12/día con el monitor vivo). Pocas muestras = dato débil.">Muestras</th>
              </tr></thead>
              <tbody>{ventanas.map(r=>{
                const po = parseFloat(r.pct_operable||0);
                const color = po>=60?"#35e07a":po>=30?"#ffd740":po>0?"#ff9100":"#ff5d6c";
                return <tr key={r.hora} style={{borderLeft:`3px solid ${color}`}}>
                  <td><b className="tnum">{String(r.hora).padStart(2,"0")}h</b></td>
                  <td style={{color,fontWeight:600}} className="tnum">{po.toFixed(0)}%</td>
                  <td className="tnum">{parseFloat(r.pct_verde||0).toFixed(0)}%</td>
                  <td className="tnum">{r.spread_neto_med!=null?(parseFloat(r.spread_neto_med)>=0?"+":"")+parseFloat(r.spread_neto_med).toFixed(3)+"%":"—"}</td>
                  <td className="tnum">{r.rotacion_med!=null?parseFloat(r.rotacion_med).toFixed(2)+"x":"—"}</td>
                  <td className="tnum">{r.presion_med!=null?parseFloat(r.presion_med).toFixed(0)+"%":"—"}</td>
                  <td className="tnum" style={{color:"var(--text-3)"}}>{r.muestras}</td>
                </tr>;
              })}</tbody>
            </table>
          </div>
          )}
          <div className="intel-explain">
            <b>Qué es esto:</b> la versión MEDIDA de las "ventanas buenas". El plan de campaña dice 07-09h y 20-23h; esta tabla muestra qué dijo el semáforo de verdad, hora por hora, la última semana.<br/>
            <b>Qué hacer:</b> planificá las sesiones de farming en las horas con % operable alto. Si una ventana del plan sale roja acá, el plan se corrige con datos. Desconfiá de las horas con pocas muestras.
          </div>
        </section>
      )}

      {seccion==="curva" && curva && (
        <section className="chart-card">
          <div className="card-head"><h3>Dónde pararme — curva de llenado</h3><span className="card-sub">7 días · solo fills OBSERVADOS · órdenes/hora por anuncio parado en ese rango</span></div>
          {curva.length===0 && <div className="intel-loading">Sin datos suficientes todavía.</div>}
          {curva.length>0 && (
          <div className="intel-scroll">
            <table className="intel-table">
              <thead><tr>
                <th title="Rango de posición en el libro (1 = mejor precio).">Posición</th>
                <th title="Órdenes por hora que recibe UN anuncio parado en ese rango. Si publicás en los dos lados (dual), esperá el doble.">Órdenes/hora</th>
                <th title="Margen de error al 95%. Si dos rangos se pisan dentro del margen, la diferencia entre ellos NO es real.">± error</th>
                <th title="Cuánto tardás en promedio en llenar una orden parado ahí.">Min/orden</th>
                <th title="Horas-anuncio observadas en ese rango: cuánta evidencia respalda el número.">Evidencia</th>
                <th title="Cuántos anunciantes distintos pasaron por ese rango.">Anunciantes</th>
              </tr></thead>
              <tbody>{curva.map(r=>{
                const oh = parseFloat(r.ordenes_hora||0);
                const c = oh>=4?"#35e07a":oh>=2?"#ffd740":oh>=1?"#ff9100":"#ff5d6c";
                return <tr key={r.rango} style={{borderLeft:`3px solid ${c}`}}>
                  <td><b className="tnum">{r.rango}</b></td>
                  <td className="tnum" style={{color:c,fontWeight:600}}>{oh.toFixed(2)}</td>
                  <td className="tnum" style={{color:"var(--text-3)"}}>±{parseFloat(r.ic95||0).toFixed(2)}</td>
                  <td className="tnum">{r.min_por_orden!=null?Math.round(r.min_por_orden)+" min":"—"}</td>
                  <td className="tnum" style={{color:"var(--text-3)"}}>{fN(Math.round(r.horas_exposicion))} h</td>
                  <td className="tnum" style={{color:"var(--text-3)"}}>{r.anunciantes}</td>
                </tr>;
              })}</tbody>
            </table>
          </div>
          )}
          <div className="intel-explain">
            <b>Cómo leer esto:</b> es la tasa de llenado MEDIDA, no estimada — se usan solo los fills observados (caída de stock real), y se divide por las horas que hubo anuncios parados en cada rango. Sin esa división, los rangos con más gente parada parecerían mejores solo por ser más concurridos.<br/>
            <b>El margen de error importa:</b> si dos rangos se pisan dentro del ±, la diferencia entre ellos no es real y podés elegir el que te convenga por precio.<br/>
            <b>Qué hacer:</b> mirá dónde está el salto grande. Bajar de posición cuesta órdenes/hora, pero no siempre en forma pareja: hay tramos donde bajás sin perder casi nada (ahí ganás margen gratis) y un punto donde se cae en picada.
          </div>
        </section>
      )}

      {seccion==="farmers" && farmers && (
        <section className="chart-card">
          <div className="card-head"><h3>Radar de farmers</h3><span className="card-sub">anunciantes con muchas órdenes chicas (7d) · tu competencia directa en la campaña</span></div>
          {farmers.length===0 && <div className="intel-loading">Sin farmers detectados todavía (necesita fills acumulados de varios días).</div>}
          {farmers.length>0 && (
          <div className="intel-scroll">
            <table className="intel-table">
              <thead><tr>
                <th title="Nickname del anunciante en Binance P2P.">Anunciante</th>
                <th title="Órdenes por día detectadas (promedio 7 días). Los que más giran son los que mejor farmean.">Órd/día</th>
                <th title="Tamaño mediano de sus órdenes en USDT. Chico = táctica farming (muchas vueltas, poco margen).">Ticket</th>
                <th title="Volumen total detectado en 7 días (USDT).">Vol 7d</th>
                <th title="¿Está publicado AHORA en ambos lados (compra y venta)? Esa es la táctica dual del plan.">Dual</th>
                <th title="Su gap propio AHORA: precio de su venta vs precio de su compra. Es el gap que a él le funciona — referencia directa para tu gap objetivo.">Gap propio</th>
                <th title="Posición actual de su anuncio de venta (en tab Compra) y de compra (en tab Venta). El plan dice pararse top 10-20.">Pos. (V/C)</th>
              </tr></thead>
              <tbody>{farmers.map(r=>(
                <tr key={r.anunciante}>
                  <td style={{fontWeight:600}}>{r.anunciante}</td>
                  <td className="tnum">{fN(r.ordenes_dia)}</td>
                  <td className="tnum">{fN(r.ticket_med)}</td>
                  <td className="tnum">{fN(r.vol_7d)}</td>
                  <td>{r.dual?<span style={{color:"var(--buy)"}}>✓ dual</span>:<span style={{color:"var(--text-3)"}}>—</span>}</td>
                  <td className="tnum" style={{color:"var(--warn)",fontWeight:600}}>{r.gap_propio_pct!=null?r.gap_propio_pct+"%":"—"}</td>
                  <td className="tnum">{r.pos_venta!=null?"#"+r.pos_venta:"—"} / {r.pos_compra!=null?"#"+r.pos_compra:"—"}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
          )}
          <div className="intel-explain">
            <b>Qué es esto:</b> los que YA hacen lo que estás por hacer — muchas órdenes chicas por día, el modelo Inversiones_MH. El monitor los detecta por sus fills confirmados.<br/>
            <b>Qué hacer:</b> mirá el <b>gap propio</b> de los duales activos: ese es el gap que el mercado está pagando hoy por farmear. Si tu gap objetivo (0,6%) queda muy lejos del de ellos, ajustalo. Y copiales la posición: si los que más giran están #8-#15, ahí es donde pasa el flujo.
          </div>
        </section>
      )}

      {seccion==="anunciantes" && anunciantes && (
        <section className="chart-card">
          <div className="card-head"><h3>Merchants con capital similar al tuyo</h3><span className="card-sub">500–8.000 USDT · tasa ≥90% · 7 días</span></div>
          <div className="intel-scroll">
            <table className="intel-table">
              <thead><tr>
                <th title="Nombre del anunciante en Binance P2P">Anunciante</th>
                <th title="Capital típico que mantiene disponible en su anuncio (mediana de la última semana). Es cuánto dinero está moviendo.">Capital</th>
                <th title="Total de órdenes completadas en toda su historia en Binance. Más órdenes = más experiencia y confianza.">Órdenes</th>
                <th title="Porcentaje de órdenes que completó exitosamente. 100% = nunca canceló. Fundamental para mantener el badge Merchant.">Tasa</th>
                <th title="La hora del día en que más aparece en el libro. Su ventana operativa principal — podés copiarla.">H. pico</th>
                <th title="Cuántas horas distintas del día estuvo activo en el libro durante la última semana. 24 = activo todo el día, 8 = opera en una ventana.">Hrs activas</th>
                <th title="Cuántas veces apareció en el top 20 del libro durante la última semana. Más apariciones = más tiempo operando = más ingresos.">Apariciones</th>
              </tr></thead>
              <tbody>{anunciantes.map(r=>(
                <tr key={r.anunciante}>
                  <td style={{fontWeight:600}}>{r.anunciante}</td>
                  <td className="tnum">{fN(r.capital)} U</td>
                  <td className="tnum">{fN(r.ordenes)}</td>
                  <td className="tnum" style={{color:"#35e07a"}}>{parseFloat(r.tasa_exito||0).toFixed(1)}%</td>
                  <td className="tnum">{String(r.hora_pico||0).padStart(2,"0")}h</td>
                  <td className="tnum">{r.horas_activas}</td>
                  <td className="tnum" style={{color:"var(--text-3)"}}>{fN(r.total_apariciones)}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
          <div className="intel-explain">
            <b>Cómo leer esta tabla:</b> estos son merchants que operan con un capital parecido al tuyo (entre 500 y 8.000 USDT) y tienen buena tasa de éxito. Son tu competencia directa y también tu mejor referencia.<br/>
            <b>Qué hacer:</b> fijate en la columna <b>H. pico</b> — si varios de ellos tienen su hora pico en las 05h, ahí es cuando más actividad hay y más probable que tus órdenes se llenen. La columna <b>Apariciones</b> te dice quién opera más horas: un número alto significa que está muy activo y gana más. Mirá sus patrones y copiá lo que funciona.
          </div>
        </section>
      )}

      {seccion==="traders" && traders && (
        <section className="chart-card">
          <div className="card-head"><h3>Top traders más activos</h3><span className="card-sub">estrategia de precio y posicionamiento · 7 días</span></div>
          <div className="intel-scroll">
            <table className="intel-table">
              <thead><tr>
                <th title="Nombre del anunciante">Anunciante</th>
                <th title="BUY = publica en Tab Compra (vende USDT a usuarios). SELL = publica en Tab Venta (compra USDT de usuarios).">Lado</th>
                <th title="Capital típico disponible en su anuncio">Capital</th>
                <th title="Rango de precios que publicó durante la semana. Si el rango es chico (ej $906-$907) usa precio FIJO. Si es grande (ej $900-$930) ajusta dinámicamente.">Rango precios</th>
                <th title="Diferencia en pesos entre su precio mínimo y máximo. Bajo = precio fijo (estrategia simple y efectiva). Alto = ajusta constantemente.">Amplitud $</th>
                <th title="Posición promedio en el libro. 1.0 = siempre primero. 5.0 = siempre en posición 5. No hace falta ser primero para llenar órdenes.">Pos. media</th>
                <th title="Total de órdenes completadas en toda su historia">Órdenes</th>
                <th title="Cuántas horas distintas estuvo activo durante la semana">Hrs</th>
              </tr></thead>
              <tbody>{traders.map((r,i)=>(
                <tr key={i}>
                  <td style={{fontWeight:600,fontSize:12}}>{r.anunciante}</td>
                  <td><span style={{color:r.tipo==="BUY"?"#35e07a":"#ff5d6c",fontWeight:600,fontSize:11}}>{r.tipo}</span></td>
                  <td className="tnum">{fN(r.capital_med)} U</td>
                  <td className="tnum" style={{fontSize:11}}>{fC(r.precio_min)} – {fC(r.precio_max)}</td>
                  <td className="tnum">{r.rango_precio!=null?`$${parseFloat(r.rango_precio).toFixed(2)}`:"—"}</td>
                  <td className="tnum">{r.pos_med!=null?parseFloat(r.pos_med).toFixed(1):"—"}</td>
                  <td className="tnum">{fN(r.ordenes)}</td>
                  <td className="tnum">{r.horas_activas}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
          <div className="intel-explain">
            <b>Cómo leer esta tabla:</b> son los anunciantes que más horas estuvieron en el libro durante la semana — los más activos y probablemente los que más ganan.<br/>
            <b>El dato clave es "Amplitud $":</b> si la amplitud es menor a $1, ese trader usa <b>precio fijo</b> — publica un precio y no lo toca. Si es mayor a $5, ajusta constantemente. Los estudios de SpaMaig y cambiosaular1 confirmaron que el precio fijo funciona igual de bien y ahorra tiempo.<br/>
            <b>Qué hacer:</b> fijate en los traders con posición media 2-4 y amplitud baja — esa es la estrategia a copiar: precio fijo, posición media, sin pelear por el primer puesto.
          </div>
        </section>
      )}

      {seccion==="fill" && fill && (
        <section className="chart-card">
          <div className="card-head"><h3>¿Cuánto te compran según dónde estés en el libro?</h3><span className="card-sub">USDT por orden recibida · por rango de posición (libro top80) · últimos 7 días</span></div>
          <div className="intel-scroll">
            <table className="intel-table">
              <thead><tr>
                <th title="Hora del día en horario Santiago">Hora</th>
                <th title="Posiciones 1-3: la cabeza del libro, los primeros que ve el usuario. USDT por orden recibida." style={{color:"#35e07a"}}>📍 1-3 (cabeza)</th>
                <th title="Posiciones 4 a 10: zona alta-media del libro.">4-10</th>
                <th title="Posiciones 11 a 20: zona media.">11-20</th>
                <th title="Posiciones 21 a 40: zona profunda (visible recién con top80).">21-40</th>
                <th title="Posiciones 41 en adelante: la cola del libro." style={{color:"var(--text-3)"}}>41+</th>
              </tr></thead>
              <tbody>{Array.from({length:24},(_,h)=>{
                const get=(rp)=>{const r=fill.find(f=>parseInt(f.hora)===h&&f.rango_pos===rp); return r&&r.consumo_med?`${fN(r.consumo_med)} U`:"–";};
                const top=fill.find(f=>parseInt(f.hora)===h&&f.rango_pos==="p01-03");
                const topVal=top&&top.consumo_med?parseFloat(top.consumo_med):0;
                const rowColor=topVal>=1500?"rgba(53,224,122,0.05)":topVal>=800?"rgba(255,215,64,0.04)":"transparent";
                return <tr key={h} style={{background:rowColor}}>
                  <td className="tnum"><b>{String(h).padStart(2,"0")}h</b></td>
                  <td className="tnum" style={{color:"#35e07a",fontWeight:topVal>=1500?700:400}}>{get("p01-03")}</td>
                  <td className="tnum">{get("p04-10")}</td>
                  <td className="tnum">{get("p11-20")}</td>
                  <td className="tnum" style={{color:"var(--text-3)"}}>{get("p21-40")}</td>
                  <td className="tnum" style={{color:"var(--text-3)"}}>{get("p41+")}</td>
                </tr>;
              })}</tbody>
            </table>
          </div>
          <div className="intel-explain">
            <b>Qué significa este número:</b> cuando alguien hace una orden contra tu anuncio, ¿cuántos USDT te compra de una vez? Un número alto (ej: 2.500 U) significa que las órdenes son grandes — llenan tu anuncio rápido. Un número bajo (ej: 200 U) significa órdenes chicas — tardás más en vaciar el stock.<br/>
            <b>Cómo usarlo:</b> las filas verdes (fondo verde suave) son las mejores horas para estar en posición 1-3. Fijate que de <b>07h a 14h</b> las órdenes top son de 2.000+ USDT — el mercado es activo y los pedidos son grandes. En madrugada (02h-06h) el spread es alto pero las órdenes son más chicas.<br/>
            <b>Conclusión práctica:</b> para llenarte rápido, estás en posición 1-3 en horas de alta liquidez (07h-14h). Para capturar spread alto, estás en madrugada aunque las órdenes sean más lentas.
          </div>
        </section>
      )}

      {seccion==="patron" && patron && (
        <section className="chart-card">
          <div className="card-head"><h3>Patrones de precio y spread por día de semana</h3><span className="card-sub">promedios últimos 7 días</span></div>
          <div className="intel-scroll">
            <table className="intel-table">
              <thead><tr>
                <th title="Día de la semana en inglés abreviado: Mon=Lunes, Tue=Martes, Wed=Miércoles, Thu=Jueves, Fri=Viernes, Sat=Sábado, Sun=Domingo">Día</th>
                <th title="Hora del día en horario Santiago">Hora</th>
                <th title="Precio ponderado promedio al que los vendedores de USDT ofrecen su stock. Es cuánto tendrías que pagar para comprar USDT en ese momento.">P. Compra</th>
                <th title="Precio ponderado promedio al que los compradores de USDT están dispuestos a pagar. Es cuánto recibirías al vender USDT.">P. Venta</th>
                <th title="Diferencia porcentual entre precio de compra y venta, sin descontar comisión. Es el margen bruto disponible en el mercado en ese momento.">Spread bruto</th>
                <th title="Cantidad de snapshots que forman este promedio. Con menos de 10 muestras el dato puede no ser representativo.">n</th>
              </tr></thead>
              <tbody>{patron.map((r,i)=>{
                const sp=parseFloat(r.spread||0);
                const c=sp>=1?"#35e07a":sp>=0.5?"#ffd740":sp>=0.35?"#ff9100":"var(--text-3)";
                return <tr key={i}>
                  <td style={{fontSize:11,fontWeight:600}}>{(r.dia_semana||"").trim().slice(0,3)}</td>
                  <td className="tnum"><b>{String(r.hora).padStart(2,"0")}h</b></td>
                  <td className="tnum">{fC(r.precio_compra)}</td>
                  <td className="tnum">{fC(r.precio_venta)}</td>
                  <td className="tnum" style={{color:c,fontWeight:600}}>{sp>=0?"+":""}{sp.toFixed(3)}%</td>
                  <td className="tnum" style={{color:"var(--text-3)"}}>{r.muestras}</td>
                </tr>;
              })}</tbody>
            </table>
          </div>
          <div className="intel-explain">
            <b>Cómo leer esta tabla:</b> muestra el precio promedio y el spread disponible para cada combinación de día y hora. Te permite ver si hay días sistemáticamente mejores que otros.<br/>
            <b>P. Compra vs P. Venta:</b> la diferencia entre ambos es la brecha que el mercado ofrece. Si P. Compra = $922 y P. Venta = $916, el spread bruto es ~0.65% — de ahí se descuenta tu comisión (0,4% hoy; 0,32% al verificarte Bronce) y te queda tu ganancia neta.<br/>
            <b>Qué hacer:</b> buscá las combinaciones día+hora con spread verde (>+0.5%) — esas son tus ventanas óptimas según el día de la semana. Si el lunes a las 05h siempre tiene +2%, priorizá operar a esa hora cuando tenés el lunes libre.
          </div>
        </section>
      )}

      {seccion==="profundidad" && profundidad && (
        <section className="chart-card">
          <div className="card-head"><h3>Profundidad del libro de órdenes</h3><span className="card-sub">USDT disponible y consumo por posición · últimos 7 días</span></div>
          <div className="intel-scroll">
            <table className="intel-table">
              <thead><tr>
                <th title="Posición individual en el libro (1 = mejor precio). Con el libro ampliado a 80 ves toda la profundidad real.">Posición</th>
                <th title="USDT acumulados disponibles hasta esa posición. Cuánta liquidez existe con prioridad sobre ti si estás en esa posición.">Liq. acumulada (USDT)</th>
                <th title="USDT consumidos (órdenes recibidas) en esa posición en el período. Cuánto del capital en esa zona rotó.">Consumo acum. (USDT)</th>
                <th title="Porcentaje de la liquidez disponible que fue consumida. 100% = toda la liquidez rotó. Bajo = posición 'muerta'.">Ratio consumo</th>
                <th title="Número de snapshots con datos para esa posición.">Muestras</th>
              </tr></thead>
              <tbody>{profundidad.map((r,i)=>{
                const ratio=parseFloat(r.ratio_consumo||0);
                const c=ratio>=60?"#35e07a":ratio>=30?"#ffd740":ratio>=10?"#ff9100":"var(--text-3)";
                const label=ratio>=60?"🔥 Alta":ratio>=30?"✅ Media":ratio>=10?"⚠️ Baja":"❌ Nula";
                const pos=r.rango_pos||("P"+r.posicion);
                return <tr key={i}>
                  <td style={{fontWeight:600,color:"var(--accent)"}}>{pos}</td>
                  <td className="tnum">{fN(r.liq_disponible_acum)} U</td>
                  <td className="tnum">{fN(r.consumo_acum)} U</td>
                  <td className="tnum" style={{color:c,fontWeight:600}}>{ratio.toFixed(1)}% <span style={{fontSize:11,fontWeight:400}}>{label}</span></td>
                  <td className="tnum" style={{color:"var(--text-3)"}}>{r.observaciones||r.muestras||"—"}</td>
                </tr>;
              })}</tbody>
            </table>
          </div>
          <div className="intel-explain">
            <b>Qué muestra:</b> para cada zona del libro (top, medio, atrás), cuánta liquidez total había y cuánto de eso realmente rotó como órdenes recibidas.<br/>
            <b>Ratio consumo:</b> es la métrica clave. Un ratio alto (verde, 🔥) significa que esa zona del libro tiene demanda real — los compradores llegan hasta ahí. Un ratio bajo (❌) significa que casi nadie llega a esa profundidad, no vale la pena estar tan atrás.<br/>
            <b>Estrategia:</b> mirá hasta qué posición el ratio se mantiene alto: esa es la profundidad hasta donde llega la demanda real. Más allá, estar en el libro es casi invisible. Con 80 posiciones ahora ves dónde está el verdadero corte.
          </div>
        </section>
      )}

      {seccion==="preciofill" && precioFill && (
        <section className="chart-card">
          <div className="card-head"><h3>Precio relativo vs tasa de llenado</h3><span className="card-sub">trade-off entre precio competitivo y volumen recibido · últimos 7 días</span></div>
          <div className="intel-scroll">
            <table className="intel-table">
              <thead><tr>
                <th title="Posición en el libro: top1-3 = más barato/competitivo, back7+ = más caro/alejado del mejor precio.">Posición</th>
                <th title="Precio promedio de esta posición relativo al mejor precio del mercado. Negativo = más barato que el líder, positivo = más caro.">Precio relativo al líder</th>
                <th title="% de ciclos en que esta posición tenía al menos una orden activa. 100% = siempre presente, 0% = nunca.">% Fill (presencia)</th>
                <th title="Cuántos USDT recibe esta posición por cada 1 USDT de diferencia de precio contra el líder. Más alto = más eficiente.">Eficiencia (U por CLP)</th>
                <th title="Número de muestras.">Muestras</th>
              </tr></thead>
              <tbody>{precioFill.map((r,i)=>{
                const ef=parseFloat(r.eficiencia||0);
                const pct=parseFloat(r.pct_fill||0);
                const precio_rel=parseFloat(r.precio_relativo_pct||0);
                const efOk=Math.abs(precio_rel)>=0.001;
                const c=!efOk?"var(--text-3)":ef>=50?"#35e07a":ef>=20?"#ffd740":ef>=5?"#ff9100":"var(--text-3)";
                const pos=r.rango_pos||(r.tipo+" P"+r.posicion);
                return <tr key={i}>
                  <td style={{fontWeight:600,color:"var(--accent)"}}>{pos}</td>
                  <td className="tnum" style={{color:precio_rel<=0?"#35e07a":"#ff5d6c",fontWeight:600}}>{precio_rel>=0?"+":""}{precio_rel.toFixed(3)}%</td>
                  <td className="tnum" style={{color:pct>=70?"#35e07a":pct>=40?"#ffd740":"var(--text-3)",fontWeight:600}}>{pct.toFixed(1)}%</td>
                  <td className="tnum" style={{color:c,fontWeight:600}}>{efOk?ef.toFixed(1):"—"}</td>
                  <td className="tnum" style={{color:"var(--text-3)"}}>{r.observaciones||r.muestras||"—"}</td>
                </tr>;
              })}</tbody>
            </table>
          </div>
          <div className="intel-explain">
            <b>Qué muestra:</b> si bajo mi precio para quedar en el top, ¿cuánto más volumen recibo a cambio? Eso es la eficiencia: órdenes ganadas por cada peso de precio sacrificado.<br/>
            <b>Precio relativo negativo:</b> esa posición cotiza más barato que el líder actual. Si estás en top1-3 generalmente tenés precio relativo negativo porque ofrecés más barato que los de atrás.<br/>
            <b>% Fill (presencia):</b> cuántas veces del total esa posición tenía anuncios activos. Un fill alto + precio relativo bajo = zona competitiva con alta demanda.<br/>
            <b>Decisión práctica:</b> si la eficiencia del top1-3 es 80 y la de mid4-6 es 15, el salto de precio que se necesita para estar en el top genera 5x más volumen — suele valer la pena.
          </div>
        </section>
      )}
    </div>
  );
}
function Backup() {
  const B = (window.P2P_CONFIG && window.P2P_CONFIG.baseUrl) || "";
  const [dias, setDias] = React.useState(30);
  const [tipo, setTipo] = React.useState("ALL");
  const [fmt, setFmt]   = React.useState("csv");
  const [fuente, setFuente] = React.useState("binance");
  const [msg, setMsg]   = React.useState("");
  const last = (() => { try { return parseInt(localStorage.getItem("ua_p2p_last_backup") || "0"); } catch (e) { return 0; } })();
  const lastTxt = last ? new Date(last).toLocaleString("es-CL") : "nunca";
  const diasDesde = last ? Math.floor((Date.now() - last) / 86400000) : null;
  const descargar = () => {
    const url = B + "/api/export/detalle?dias=" + dias + "&tipo=" + tipo + "&fmt=" + fmt + "&fuente=" + fuente;
    const a = document.createElement("a");
    a.href = url;
    a.download = "detalle_" + fuente + "_" + tipo + "_" + dias + "d." + fmt;
    document.body.appendChild(a); a.click(); a.remove();
    try { localStorage.setItem("ua_p2p_last_backup", String(Date.now())); } catch (e) {}
    setMsg("✅ Descarga iniciada. Guardá el archivo en Drive o un disco externo para tener tu copia.");
  };
  const descargarTodo = () => {
    const a = document.createElement("a");
    a.href = B + "/api/export/todo?dias=" + dias;
    a.download = "backup_p2p.zip";
    document.body.appendChild(a); a.click(); a.remove();
    try { localStorage.setItem("ua_p2p_last_backup", String(Date.now())); } catch (e) {}
    setMsg("✅ Backup general (Binance + Bybit) en un ZIP. Guardalo en Drive o disco externo.");
  };
  const vaciar = async () => {
    if (!window.confirm("¿Vaciar las listas conservando las últimas 24h? Hacé el backup ANTES. No borra precios, volumen ni historial.")) return;
    setMsg("Vaciando…");
    try {
      const r = await window.P2P_AUTH.post(B + "/api/mantenimiento/vaciar");
      const d = await r.json();
      setMsg(d.ok ? "🧹 Vaciando en segundo plano — mirá el ALMACENAMIENTO de arriba, en unos segundos baja. No cierres la app." : "No se pudo iniciar el vaciado.");
    } catch (e) { setMsg("No se pudo iniciar el vaciado."); }
  };
  return (
    <div className="view">
      <section className="chart-card backup-wrap">
        <div className="card-head"><h3>Backup / Exportar base de datos</h3><span className="card-sub">descarga el detalle por anunciante</span></div>
        <div style={{ marginBottom: 6 }}><SystemBar /></div>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap", margin: "10px 0 6px" }}>
          <button className="btn-apply dirty" onClick={descargarTodo}>⬇ Backup general (Binance + Bybit)</button>
          <button className="btn-reset" onClick={vaciar}>Vaciar listas (conservar 24h)</button>
        </div>
        <p className="backup-last">Un clic baja TODO en un ZIP. Última copia: <b>{lastTxt}</b>{diasDesde !== null ? " (hace " + diasDesde + " días)" : ""}.</p>
        <div className="backup-grid">
          <div className="f-item"><label>Exchange</label>
            <select value={fuente} onChange={e => setFuente(e.target.value)}>
              <option value="binance">Binance</option>
              <option value="bybit">Bybit</option>
            </select></div>
          <div className="f-item"><label>Días de datos</label>
            <input type="number" min="1" max="365" value={dias} onChange={e => setDias(parseInt(e.target.value) || 1)} /></div>
          <div className="f-item"><label>Lado</label>
            <select value={tipo} onChange={e => setTipo(e.target.value)}>
              <option value="ALL">Ambos (BUY+SELL)</option>
              <option value="BUY">Solo BUY</option>
              <option value="SELL">Solo SELL</option>
            </select></div>
          <div className="f-item"><label>Formato</label>
            <select value={fmt} onChange={e => setFmt(e.target.value)}>
              <option value="csv">CSV (Excel)</option>
              <option value="json">JSON</option>
            </select></div>
        </div>
        <div className="backup-actions">
          <button className="btn-apply dirty" onClick={descargar}>⬇ Descargar {fmt.toUpperCase()}</button>
          <span className="backup-last">Genera: <code>detalle_{fuente}_{tipo}_{dias}d.{fmt}</code></span>
        </div>
        {msg && <div className="backup-msg">{msg}</div>}
        <div className="backup-help">
          <b>Cómo hacerlo vos mismo sin esta página:</b> abrí en el navegador la dirección de tu monitor seguida de:<br/>
          <code>/api/export/detalle?dias=30&tipo=ALL&fmt=csv&fuente=binance</code><br/>
          Cambiá <code>dias</code> por los que quieras, <code>tipo</code> por BUY/SELL/ALL, <code>fmt</code> por csv/json y <code>fuente</code> por binance/bybit. El archivo se descarga solo.<br/><br/>
          <b>Recomendación:</b> exportá al menos una vez por semana y guardá el CSV en Drive o disco externo. La base se purga automáticamente a los 30 días, así que un backup mensual conserva tu historia completa.
        </div>
      </section>
    </div>
  );
}

function BackupBanner({ onGo }) {
  const [dismissed, setDismissed] = React.useState(false);
  const [pct, setPct] = React.useState(null);
  React.useEffect(() => {
    const B = (window.P2P_CONFIG && window.P2P_CONFIG.baseUrl) || "";
    fetch(B + "/api/storage").then(r => r.json()).then(d => setPct(d.pct)).catch(() => {});
  }, []);
  const last = (() => { try { return parseInt(localStorage.getItem("ua_p2p_last_backup") || "0"); } catch (e) { return 0; } })();
  const diasDesde = last ? Math.floor((Date.now() - last) / 86400000) : null;
  const backupVencido = diasDesde === null || diasDesde >= 7;
  const discoAlto = pct != null && pct >= 70;
  if (dismissed || (!backupVencido && !discoAlto)) return null;
  const txt = discoAlto
    ? ("Disco al " + pct + "%. Hacé el backup general y después vaciá las listas.")
    : ((diasDesde === null ? "Todavía no registraste ninguna copia de la base de datos." : ("Tu última copia fue hace " + diasDesde + " días.")) + " Conviene exportar un backup.");
  return (
    <div className="backup-banner" style={discoAlto ? { borderLeftColor: "var(--sell)" } : {}}>
      <span className="bb-dot"></span>
      <span className="bb-txt">{txt}</span>
      <button className="bb-go" onClick={onGo}>{discoAlto ? "Ir a Backup" : "Hacer backup"}</button>
      <button className="bb-x" onClick={() => setDismissed(true)} aria-label="cerrar">✕</button>
    </div>
  );
}

function RotNumIn({ lbl, val, set, step }) {
  return (
    <div className="f-item"><label>{lbl}</label>
      <input type="number" step={step || 1} value={val} onChange={e => set(parseFloat(e.target.value) || 0)} /></div>
  );
}

function RotacionCalc() {
  const B = (window.P2P_CONFIG && window.P2P_CONFIG.baseUrl) || "";
  const [tipo, setTipo] = React.useState("BUY");
  const [data, setData] = React.useState(null);
  const [loading, setLoading] = React.useState(true);
  const [modo, setModo] = React.useState("precio");
  const [capital, setCapital] = React.useState(10000);
  const [orden, setOrden] = React.useState(2000);
  const [dist, setDist] = React.useState(0.3);
  const [pos, setPos] = React.useState(8);
  const [horas, setHoras] = React.useState(8);
  const [spreadBase, setSpreadBase] = React.useState(0.5);
  const [comision, setComision] = React.useState(0.36);
  const [overhead, setOverhead] = React.useState(10);

  React.useEffect(() => {
    setLoading(true);
    fetch(B + "/api/inteligencia/rotacion?tipo=" + tipo + "&dias=7")
      .then(r => r.json()).then(d => { setData(d); setLoading(false); })
      .catch(() => setLoading(false));
  }, [tipo]);

  const fmt = (v, d = 0) => (v == null || !isFinite(v)) ? "—" : Number(v).toLocaleString("es-CL", { maximumFractionDigits: d });

  if (loading) return <div className="intel-loading">Consultando base de datos…</div>;
  if (!data || !data.por_precio) return <div className="intel-loading">Sin datos suficientes todavía.</div>;

  const nivel = (() => {
    if (modo === "posicion") {
      const arr = data.por_posicion || [];
      let row = arr.find(r => parseInt(r.posicion) === parseInt(pos));
      if (!row && arr.length) row = arr.reduce((a, b) => Math.abs(b.posicion - pos) < Math.abs(a.posicion - pos) ? b : a);
      return row ? { caudal: row.caudal_min, distancia: row.distancia_med || 0, pct: row.pct_presencia } : null;
    }
    const arr = data.por_precio || [];
    const row = arr.reduce((a, b) => a == null ? b : (Math.abs(b.banda_pct - dist) < Math.abs(a.banda_pct - dist) ? b : a), null);
    return row ? { caudal: row.caudal_min, distancia: dist, pct: row.pct_presencia } : null;
  })();

  const proyectar = (caudal, distancia) => {
    const tLleno = caudal > 0 ? orden / caudal : Infinity;
    const tVuelta = 2 * tLleno + overhead;
    const rotDia = (isFinite(tVuelta) && tVuelta > 0) ? (horas * 60) / tVuelta : 0;
    const spreadVuelta = spreadBase + 2 * distancia - comision;
    const ganDia = rotDia * orden * spreadVuelta / 100;
    return { tLleno, tVuelta, rotDia, spreadVuelta, ganDia };
  };
  const p = nivel ? proyectar(nivel.caudal, nivel.distancia) : null;

  const sweep = (data.por_precio || []).map(r => {
    const pr = proyectar(r.caudal_min, r.banda_pct);
    return { banda: r.banda_pct, caudal: r.caudal_min, pct: r.pct_presencia, tVuelta: pr.tVuelta, rotDia: pr.rotDia, spreadVuelta: pr.spreadVuelta, ganDia: pr.ganDia };
  });
  const mejor = sweep.reduce((a, b) => (b.ganDia > (a ? a.ganDia : -1) ? b : a), null);

  return (
    <div className="view tone-accent">
      <section className="chart-card">
        <div className="card-head"><h3>Calculadora de rotación de capital</h3><span className="card-sub">proyección: velocidad de llenado, rotaciones/día y $/día · datos {data.dias}d</span></div>

        <div className="rank-toggle" style={{ marginBottom: 14, display: "inline-flex", flexWrap: "wrap" }}>
          <button className={tipo === "BUY" ? "on" : ""} onClick={() => setTipo("BUY")}>Lado BUY</button>
          <button className={tipo === "SELL" ? "on" : ""} onClick={() => setTipo("SELL")}>Lado SELL</button>
          <button className={modo === "precio" ? "on" : ""} onClick={() => setModo("precio")}>Por % al líder</button>
          <button className={modo === "posicion" ? "on" : ""} onClick={() => setModo("posicion")}>Por posición</button>
        </div>

        <div className="filters-grid">
          <RotNumIn lbl="Capital total (USDT)" val={capital} set={setCapital} step={500} />
          <RotNumIn lbl="Tamaño por orden (USDT)" val={orden} set={setOrden} step={100} />
          {modo === "precio"
            ? <RotNumIn lbl="Distancia al líder (%)" val={dist} set={setDist} step={0.1} />
            : <RotNumIn lbl="Posición en el libro" val={pos} set={setPos} step={1} />}
          <RotNumIn lbl="Horas operables/día" val={horas} set={setHoras} step={1} />
          <RotNumIn lbl="Spread base cabeza (%)" val={spreadBase} set={setSpreadBase} step={0.1} />
          <RotNumIn lbl="Comisión total ida+vuelta (%)" val={comision} set={setComision} step={0.01} />
          <RotNumIn lbl="Overhead por vuelta (min)" val={overhead} set={setOverhead} step={1} />
        </div>

        {p && nivel ? (
          <div className="stat-cards" style={{ marginTop: 16 }}>
            <div className="statcard"><div className="statcard-label">Caudal del nivel</div><div className="statcard-val">{fmt(nivel.caudal, 0)} <span style={{ fontSize: 13 }}>U/min</span></div></div>
            <div className="statcard"><div className="statcard-label">Llenado de una orden</div><div className="statcard-val">{fmt(p.tLleno, 0)} <span style={{ fontSize: 13 }}>min</span></div></div>
            <div className="statcard"><div className="statcard-label">Rotaciones/día</div><div className="statcard-val">{fmt(p.rotDia, 1)}</div></div>
            <div className="statcard"><div className="statcard-label">Spread por vuelta</div><div className="statcard-val">{fmt(p.spreadVuelta, 2)}%</div></div>
            <div className="statcard"><div className="statcard-label">Ganancia/día</div><div className="statcard-val">${fmt(p.ganDia, 0)}</div></div>
            <div className="statcard"><div className="statcard-label">Ganancia/mes</div><div className="statcard-val">${fmt(p.ganDia * 30, 0)}</div></div>
            <div className="statcard"><div className="statcard-label">ROI mensual s/capital</div><div className="statcard-val">{capital > 0 ? fmt(p.ganDia * 30 / capital * 100, 1) : "—"}%</div></div>
            <div className="statcard"><div className="statcard-label">Presencia del nivel</div><div className="statcard-val">{fmt(nivel.pct, 0)}%</div></div>
          </div>
        ) : <div className="intel-loading">No hay caudal medible en ese nivel.</div>}

        <div className="intel-explain">
          <b>Cómo leerlo:</b> el <b>caudal</b> es cuántos USDT/min absorbe ese nivel del libro según tus datos reales. El <b>llenado</b> es cuánto tarda en venderse/comprarse una orden de tu tamaño. La <b>vuelta</b> = llenar la compra + llenar la venta + overhead (transferencias). Más lejos del líder = más spread por vuelta pero menos rotaciones.<br/>
          <b>Ojo:</b> el caudal mide a los que hoy ocupan cada nivel (merchants con reputación). Como Bronze tu llenado real puede ser más lento al mismo precio: tomalo como mejor caso.
        </div>
      </section>

      <section className="chart-card">
        <div className="card-head"><h3>Punto óptimo: spread vs velocidad</h3><span className="card-sub">$/día por distancia al líder · resaltado el mejor</span></div>
        <div className="intel-scroll">
          <table className="intel-table">
            <thead><tr>
              <th title="Qué tan lejos del mejor precio te colocás.">Distancia al líder</th>
              <th title="USDT por minuto que absorbe ese nivel.">Caudal (U/min)</th>
              <th title="% de ciclos en que ese nivel recibió órdenes.">Presencia</th>
              <th title="Minutos para una vuelta completa (compra+venta+overhead).">T. vuelta</th>
              <th title="Cuántas vueltas completás al día con tus horas.">Rotaciones/día</th>
              <th title="Spread neto que capturás por vuelta a esa distancia.">Spread/vuelta</th>
              <th title="Ganancia neta diaria estimada.">Ganancia/día</th>
            </tr></thead>
            <tbody>{sweep.map((r, i) => {
              const best = mejor && r.banda === mejor.banda;
              return <tr key={i} style={{ background: best ? "rgba(53,224,122,0.10)" : "transparent" }}>
                <td className="tnum"><b>{r.banda.toFixed(1)}%</b>{best ? " ⭐" : ""}</td>
                <td className="tnum">{fmt(r.caudal, 0)}</td>
                <td className="tnum" style={{ color: "var(--text-3)" }}>{fmt(r.pct, 0)}%</td>
                <td className="tnum">{fmt(r.tVuelta, 0)} min</td>
                <td className="tnum">{fmt(r.rotDia, 1)}</td>
                <td className="tnum" style={{ color: r.spreadVuelta >= 0 ? "#35e07a" : "#ff5d6c" }}>{r.spreadVuelta.toFixed(2)}%</td>
                <td className="tnum" style={{ color: best ? "#35e07a" : "var(--text)", fontWeight: best ? 700 : 400 }}>${fmt(r.ganDia, 0)}</td>
              </tr>;
            })}</tbody>
          </table>
        </div>
        <div className="intel-explain">
          <b>La fila ⭐</b> es la distancia que maximiza tu ganancia diaria con los parámetros de arriba: el equilibrio entre cobrar más spread y rotar más veces. Ajustá capital, tamaño y horas para ver cómo se mueve el óptimo.
        </div>
      </section>
    </div>
  );
}

function CrossView() {
  const B = (window.P2P_CONFIG && window.P2P_CONFIG.baseUrl) || "";
  const [cross, setCross] = React.useState(null);
  const [loading, setLoading] = React.useState(true);
  const [costo, setCosto] = React.useState(0.5);
  React.useEffect(() => {
    let stop = false;
    const load = () => {
      fetch(B + "/api/cross").then(r => r.json()).then(c => { if (!stop) { setCross(c); setLoading(false); } }).catch(() => { if (!stop) setLoading(false); });
    };
    load();
    const id = setInterval(load, 30000);
    return () => { stop = true; clearInterval(id); };
  }, []);
  const fmt = (v, d = 2) => (v == null || !isFinite(v)) ? "—" : Number(v).toLocaleString("es-CL", { minimumFractionDigits: d, maximumFractionDigits: d });
  if (loading) return <div className="intel-loading">Consultando Binance y Bybit…</div>;
  if (!cross) return <div className="intel-loading">Sin datos de cross todavía.</div>;
  const bin = cross.binance || {}, by = cross.bybit || {};
  const sinBybit = (by.comprar_usdt == null && by.vender_usdt == null);
  // Lógica MAKER: comprás USDT cerca del mejor BID (postás un bid) y vendés cerca del mejor ASK (postás un ask).
  const binAsk = bin.comprar_usdt, binBid = bin.vender_usdt;
  const bybAsk = by.comprar_usdt,  bybBid = by.vender_usdt;
  const pctR = (compra, venta) => (compra && venta) ? (venta - compra) / compra * 100 : null;
  const rA = pctR(binBid, bybAsk);  // acumular Binance (comprar al bid) -> distribuir Bybit (vender al ask)
  const rB = pctR(bybBid, binAsk);  // acumular Bybit -> distribuir Binance
  const neto = (g) => g == null ? null : g - costo;
  const card = (titulo, compraEx, ventaEx, compraP, ventaP, gross) => {
    const net = neto(gross);
    const ok = net != null && net > 0;
    return (
      <div className="statcard" style={{ borderTopColor: ok ? "var(--buy)" : "var(--line)" }}>
        <div className="statcard-label">{titulo}</div>
        <div style={{ fontSize: 12, color: "var(--text-3)", margin: "6px 0" }}>comprar en {compraEx} @ {fmt(compraP)} → vender en {ventaEx} @ {fmt(ventaP)}</div>
        <div className="statcard-val" style={{ color: ok ? "var(--buy)" : "var(--text)" }}>{gross == null ? "—" : (gross >= 0 ? "+" : "") + fmt(gross, 3) + "%"} <span style={{ fontSize: 13, color: "var(--text-3)" }}>bruto</span></div>
        <div style={{ fontSize: 13, marginTop: 4, color: ok ? "var(--buy)" : "var(--sell)" }}>neto ≈ {net == null ? "—" : (net >= 0 ? "+" : "") + fmt(net, 3) + "%"} {ok ? "✅" : ""}</div>
      </div>
    );
  };
  return (
    <div className="view tone-accent">
      <section className="chart-card">
        <div className="card-head"><h3>Arbitraje cruzado · Binance ↔ Bybit</h3><span className="card-sub">USDT/CLP · lógica MAKER (vos posteás) · se actualiza cada 30s</span></div>
        {sinBybit ? <div className="intel-explain">Esperando el primer ciclo de Bybit… si recién deployaste, dale 1-2 minutos.</div> : null}
        <div className="stat-cards" style={{ gridTemplateColumns: "1fr 1fr" }}>
          <div className="statcard">
            <div className="statcard-label">Binance (maker)</div>
            <div style={{ fontSize: 13, marginTop: 8 }}>comprar USDT (postás bid) ≈ <b>{fmt(bin.vender_usdt)}</b></div>
            <div style={{ fontSize: 13 }}>vender USDT (postás ask) ≈ <b>{fmt(bin.comprar_usdt)}</b></div>
          </div>
          <div className="statcard">
            <div className="statcard-label">Bybit (maker)</div>
            <div style={{ fontSize: 13, marginTop: 8 }}>comprar USDT (postás bid) ≈ <b>{fmt(by.vender_usdt)}</b></div>
            <div style={{ fontSize: 13 }}>vender USDT (postás ask) ≈ <b>{fmt(by.comprar_usdt)}</b></div>
          </div>
        </div>
        <div className="filters-grid" style={{ gridTemplateColumns: "260px", margin: "14px 0" }}>
          <div className="f-item"><label>Costo total estimado (fees + transferencia) %</label>
            <input type="number" step="0.05" value={costo} onChange={e => setCosto(parseFloat(e.target.value) || 0)} /></div>
        </div>
        <div className="stat-cards" style={{ gridTemplateColumns: "1fr 1fr" }}>
          {card("Acumular Binance → Distribuir Bybit", "Binance", "Bybit", bin.vender_usdt, by.comprar_usdt, rA)}
          {card("Acumular Bybit → Distribuir Binance", "Bybit", "Binance", by.vender_usdt, bin.comprar_usdt, rB)}
        </div>
        <div className="intel-explain">
          <b>Lógica maker:</b> como vos posteás anuncios (no tomás), comprás USDT cerca del <b>mejor bid</b> y vendés cerca del <b>mejor ask</b>. Por eso los precios acá están al revés que si tomaras la orden. El <b>neto</b> resta tu costo estimado (comisiones maker de los dos lados + mover USDT por red).<br/>
          <b>Riesgo de ejecución:</b> esto NO es instantáneo como tomar — posteás en los dos exchanges y esperás que se llenen los dos. Si uno se llena y el otro no, o el precio se mueve, la brecha puede evaporarse. Además necesitás inventario en ambos lados para postear. Verificá que la brecha se sostenga y que Bybit tenga liquidez para tu tamaño.
        </div>
      </section>
    </div>
  );
}

function Muros() {
  const B = (window.P2P_CONFIG && window.P2P_CONFIG.baseUrl) || "";
  const [estado, setEstado] = React.useState(null);
  const [rotB, setRotB] = React.useState(null);
  const [rotS, setRotS] = React.useState(null);
  const [loading, setLoading] = React.useState(true);
  const [umbral, setUmbral] = React.useState(10000);
  const [topN, setTopN] = React.useState(6);
  const [selC, setSelC] = React.useState(0);
  const [selV, setSelV] = React.useState(0);
  const [comision, setComision] = React.useState(0.36);
  const [orden, setOrden] = React.useState(2000);

  React.useEffect(() => {
    let stop = false;
    const load = () => {
      Promise.all([
        fetch(B + "/api/estado").then(r => r.json()).catch(() => null),
        fetch(B + "/api/inteligencia/rotacion?tipo=BUY&dias=7").then(r => r.json()).catch(() => null),
        fetch(B + "/api/inteligencia/rotacion?tipo=SELL&dias=7").then(r => r.json()).catch(() => null),
      ]).then(([e, b, sj]) => { if (!stop) { setEstado(e); setRotB(b); setRotS(sj); setLoading(false); } });
    };
    load();
    const id = setInterval(load, 30000);
    return () => { stop = true; clearInterval(id); };
  }, []);

  const fmt = (v, d = 0) => (v == null || !isFinite(v)) ? "—" : Number(v).toLocaleString("es-CL", { maximumFractionDigits: d });
  if (loading) return <div className="intel-loading">Cargando libro y caudal…</div>;
  if (!estado || !estado.detalle_compra) return <div className="intel-loading">Sin datos del libro todavía.</div>;

  const caudalMap = (rot) => {
    const m = {};
    ((rot && rot.por_posicion) || []).forEach((r) => { m[parseInt(r.posicion)] = r; });
    return m;
  };
  const cmB = caudalMap(rotB), cmS = caudalMap(rotS);

  const muros = (detalle, central) => {
    const ads = (detalle || []).filter((a) => central > 0 && Math.abs(a.precio - central) / central <= 0.03 && a.disponible > 0);
    return ads.slice().sort((x, y) => y.disponible - x.disponible).slice(0, topN);
  };
  const centralC = parseFloat(estado.precio_pond_tab_compra) || parseFloat(estado.mejor_vendedor_tab_compra) || 0;
  const centralV = parseFloat(estado.precio_pond_tab_venta) || parseFloat(estado.mejor_comprador_tab_venta) || 0;
  const murosC = muros(estado.detalle_compra, centralC);
  const murosV = muros(estado.detalle_venta, centralV);
  const wC = murosC[selC] || murosC[0];
  const wV = murosV[selV] || murosV[0];
  const sellP = wC ? wC.precio - 0.01 : null;   // vendés acá (tab compra)
  const buyP  = wV ? wV.precio + 0.01 : null;    // comprás acá (tab venta)
  const brutaPct = (sellP && buyP) ? (sellP - buyP) / buyP * 100 : null;
  const netaPct = brutaPct == null ? null : brutaPct - comision;
  const cdC = wC ? cmB[parseInt(wC.posicion)] : null;
  const cdV = wV ? cmS[parseInt(wV.posicion)] : null;
  const muerta = (cd) => cd && cd.caudal_min != null && cd.caudal_min < 40;

  const lab = (c) => c == null ? { t: "—", col: "var(--text-3)" } : c >= 100 ? { t: "🔥 alta", col: "#35e07a" } : c >= 40 ? { t: "media", col: "#ffd740" } : { t: "❄️ muerta", col: "var(--text-3)" };
  // Tiempo de llenado de TU orden a ese ritmo (caudal en USDT/min)
  const tiempoLlen = (cmin) => {
    if (cmin == null || cmin <= 0) return { t: "no se llena", col: "var(--text-3)" };
    const min = orden / cmin;
    const lo = min * 0.5, hi = min * 2;   // rango: los llenados son a los saltos
    const u = (m) => m < 90 ? Math.round(m) + " min" : (m / 60).toFixed(1) + " h";
    let txt;
    if (hi < 90) txt = Math.round(lo) + "-" + Math.round(hi) + " min";
    else if (lo >= 90) txt = (lo / 60).toFixed(1) + "-" + (hi / 60).toFixed(1) + " h";
    else txt = Math.round(lo) + " min - " + u(hi);
    const col = min <= 20 ? "#35e07a" : min <= 60 ? "#ffd740" : "var(--text-3)";
    return { t: txt, col };
  };
  // Mejor muro de cada lado: el precio mas conveniente que TODAVIA se llena (caudal >= 40)
  const mejorIdx = (lista, cm, lado) => {
    let best = -1, bp = null;
    lista.forEach((a, i) => {
      const cd = cm[parseInt(a.posicion)];
      const cmin = cd ? cd.caudal_min : null;
      if (cmin == null || cmin < 40) return;
      if (cd.obs != null && cd.obs < 200) return;   // ignorar posiciones con poca muestra (ruidosas)
      if (lado === "BUY") { if (bp == null || a.precio > bp) { bp = a.precio; best = i; } }
      else { if (bp == null || a.precio < bp) { bp = a.precio; best = i; } }
    });
    return best;
  };
  const mejorC = mejorIdx(murosC, cmB, "BUY");
  const mejorV = mejorIdx(murosV, cmS, "SELL");
  const resumen = (lista, cm, mejor, accion) => {
    if (mejor < 0) return <span style={{ fontSize: 11.5, color: "var(--text-3)" }}>Ningún muro con flujo decente ahora mismo.</span>;
    const a = lista[mejor];
    const cd = cm[parseInt(a.posicion)];
    const T = tiempoLlen(cd ? cd.caudal_min : null);
    const sug = accion === "vender" ? a.precio - 0.01 : a.precio + 0.01;
    return <span style={{ fontSize: 12, color: "var(--text-2)" }}>★ Mejor para <b>{accion}</b>: poné en <b style={{ color: "#35e07a" }}>${fmt(sug, 2)}</b> (adelante de {a.anunciante}, pos {a.posicion}) — te llenás en {T.t}.</span>;
  };

  const tabla = (lista, cm, lado, sel, setSel, mejor) => (
    <div className="intel-scroll">
      <table className="intel-table">
        <thead><tr>
          <th>Anunciante</th>
          <th title="Liquidez del anuncio (USDT disponible)">Tamaño</th>
          <th>Precio</th>
          <th title="Posición en el libro">Pos</th>
          <th title="Ritmo de compra/venta en esa posición: USDT por minuto (promedio 7 días)">Flujo</th>
          <th title="Cuánto tarda en llenarse TU orden a ese ritmo">Se llena en</th>
          <th title="Precio para quedar 1 centavo adelante del muro">Poné en</th>
        </tr></thead>
        <tbody>{lista.map((a, i) => {
          const big = a.disponible >= umbral;
          const cd = cm[parseInt(a.posicion)];
          const cmin = cd ? cd.caudal_min : null;
          const L = lab(cmin);
          const T = tiempoLlen(cmin);
          const sug = lado === "BUY" ? a.precio - 0.01 : a.precio + 0.01;
          const esMejor = i === mejor;
          return <tr key={i} onClick={() => setSel(i)} style={{ cursor: "pointer", background: i === sel ? "var(--accent-soft)" : (esMejor ? "rgba(53,224,122,0.10)" : (big ? "rgba(91,141,239,0.08)" : "transparent")), outline: i === sel ? "1px solid var(--accent)" : "none" }}>
            <td style={{ fontWeight: big || esMejor ? 700 : 400 }}>{esMejor ? "★ " : ""}{a.anunciante} {a.es_merchant ? <span className="merch" title="Merchant verificado">✦</span> : <span style={{ color: "var(--text-3)", fontSize: 10 }} title="No verificado">·</span>}</td>
            <td className="tnum" style={{ fontWeight: big ? 700 : 400, color: big ? "var(--accent)" : "var(--text)" }}>{fmt(a.disponible)} USDT</td>
            <td className="tnum">${fmt(a.precio, 2)}</td>
            <td className="tnum" style={{ color: "var(--text-3)" }}>{a.posicion}</td>
            <td className="tnum" style={{ color: L.col }}>{cmin == null ? "—" : fmt(cmin) + " U/min "}<span style={{ fontSize: 11 }}>{L.t}</span></td>
            <td className="tnum" style={{ color: T.col }}>{T.t}</td>
            <td className="tnum" style={{ color: "#35e07a", fontWeight: 600 }}>${fmt(sug, 2)}</td>
          </tr>;
        })}</tbody>
      </table>
    </div>
  );

  return (
    <div className="view tone-accent">
      <section className="chart-card">
        <div className="card-head"><h3>Muros de liquidez</h3><span className="card-sub">los anuncios más grandes · dónde ponerte para interceptar su flujo · actualiza 30s</span></div>
        <div className="filters-grid">
          <div className="f-item"><label>Tu orden (USDT)</label><input type="number" step="500" value={orden} onChange={(e) => setOrden(parseFloat(e.target.value) || 0)} /></div>
          <div className="f-item"><label>Cuántos muros por lado</label><input type="number" min="3" max="15" value={topN} onChange={(e) => setTopN(parseInt(e.target.value) || 6)} /></div>
          <div className="f-item"><label>Resaltar si supera (USDT)</label><input type="number" step="1000" value={umbral} onChange={(e) => setUmbral(parseFloat(e.target.value) || 0)} /></div>
          <div className="f-item"><label>Comisión ida+vuelta (%)</label><input type="number" step="0.01" value={comision} onChange={(e) => setComision(parseFloat(e.target.value) || 0)} /></div>
        </div>
        <div style={{ marginTop: 14, padding: "14px 16px", borderRadius: 12, background: "var(--bg-2)", border: "1px solid var(--line)" }}>
          <div className="statcard-label">Brecha hipotética — clic en un muro de cada lado para comparar</div>
          <div style={{ display: "flex", flexWrap: "wrap", alignItems: "baseline", gap: 18, marginTop: 8 }}>
            <span style={{ fontSize: 13 }}>Vendés a <b style={{ color: "var(--buy)" }}>${fmt(sellP, 2)}</b>{wC ? " (" + wC.anunciante + ")" : ""}</span>
            <span style={{ fontSize: 13 }}>Comprás a <b style={{ color: "var(--sell)" }}>${fmt(buyP, 2)}</b>{wV ? " (" + wV.anunciante + ")" : ""}</span>
            <span style={{ fontSize: 22, fontWeight: 700, color: (netaPct != null && netaPct > 0) ? "var(--buy)" : "var(--sell)" }}>{brutaPct == null ? "—" : (brutaPct >= 0 ? "+" : "") + fmt(brutaPct, 3) + "% bruto"}</span>
            <span style={{ fontSize: 15, color: (netaPct != null && netaPct > 0) ? "var(--buy)" : "var(--text-3)" }}>neta {netaPct == null ? "—" : (netaPct >= 0 ? "+" : "") + fmt(netaPct, 3) + "%"} (−{fmt(comision, 2)}% com.)</span>
          </div>
          {(muerta(cdC) || muerta(cdV)) ? <div style={{ fontSize: 12, color: "#ffd740", marginTop: 6 }}>⚠️ Uno de los dos muros tiene caudal muerto — esa brecha es teórica, ahí no te llenás.</div> : null}
        </div>
        <div className="muros-cols">
          <div>
            <div className="ob-coltitle" style={{ color: "var(--buy)" }}>COMPRA · vendedores de USDT (acá VENDÉS)</div>
            <div style={{ margin: "0 0 8px" }}>{resumen(murosC, cmB, mejorC, "vender")}</div>
            {tabla(murosC, cmB, "BUY", selC, setSelC, mejorC)}
          </div>
          <div>
            <div className="ob-coltitle" style={{ color: "var(--sell)" }}>VENTA · compradores de USDT (acá COMPRÁS)</div>
            <div style={{ margin: "0 0 8px" }}>{resumen(murosV, cmS, mejorV, "comprar")}</div>
            {tabla(murosV, cmS, "SELL", selV, setSelV, mejorV)}
          </div>
        </div>
        <div className="intel-explain">
          <b>Cómo usarlo:</b> los muros son los anuncios con más liquidez — marcan dónde se concentra el volumen. La columna <b>Poné en</b> te da el precio para quedar un centavo adelante de ese muro e interceptar su flujo. El <b>✦</b> es merchant verificado.<br/>
          <b>Flujo y "Se llena en":</b> el flujo es cuántos USDT/min se mueven en esa posición (promedio 7 días). "Se llena en" traduce eso a cuánto tardaría TU orden (la de arriba), como <b>rango</b> — los llenados son a los saltos, así que es un estimado, no un cronómetro. Usalo para comparar (este muro llena más rápido que aquel).<br/>
          <b>★ Mejor:</b> el muro con mejor precio de cada lado que todavía se llena. Ignora las posiciones muy profundas con poca muestra (ruidosas), así no te manda a un lugar poco confiable. La línea de arriba te lo dice en criollo.<br/>
          <b>Ojo:</b> el flujo es promedio de 7 días por posición; las posiciones profundas (20+) tienen poca muestra y son ruidosas.
        </div>
      </section>
    </div>
  );
}

function SystemBar({ snapTs }) {
  const B = (window.P2P_CONFIG && window.P2P_CONFIG.baseUrl) || "";
  const [st, setSt] = React.useState(null);
  React.useEffect(() => {
    let stop = false;
    const load = () => fetch(B + "/api/storage").then(r => r.json()).then(d => { if (!stop) setSt(d); }).catch(() => {});
    load();
    const id = setInterval(load, 60000);
    return () => { stop = true; clearInterval(id); };
  }, []);
  const ageMin = snapTs ? (Date.now() - Date.parse(String(snapTs).replace(" ", "T"))) / 60000 : null;
  const stale = ageMin != null && ageMin > 8;
  const pct = st ? st.pct : null;
  const barCol = pct == null ? "var(--text-3)" : pct >= 90 ? "var(--sell)" : pct >= 75 ? "var(--warn)" : "var(--buy)";
  return (
    <div style={{ margin: 0, display: "flex", flexWrap: "wrap", gap: 12, alignItems: "center" }}>
      {st ? (
        <div title="Espacio usado en la base de datos (aprox). Si llega a 100% el colector deja de guardar." style={{ display: "flex", alignItems: "center", gap: 10, fontSize: 12, color: "var(--text-2)", background: "var(--bg-1)", border: "1px solid var(--line-soft)", borderRadius: 999, padding: "6px 14px" }}>
          <span style={{ color: "var(--text-3)", textTransform: "uppercase", letterSpacing: "0.08em", fontSize: 10 }}>Almacenamiento</span>
          <span style={{ width: 90, height: 6, background: "var(--bg-3)", borderRadius: 4, overflow: "hidden", display: "inline-block" }}>
            <span style={{ display: "block", height: "100%", width: Math.min(100, pct || 0) + "%", background: barCol }}></span>
          </span>
          <b style={{ color: barCol }}>{pct == null ? "—" : pct + "%"}</b>
          <span style={{ color: "var(--text-3)" }}>{st.libre_mb} MB libres / {st.limite_mb}</span>
        </div>
      ) : null}
    </div>
  );
}

function VolumenBar() {
  const B = (window.P2P_CONFIG && window.P2P_CONFIG.baseUrl) || "";
  const [v, setV] = React.useState(null);
  const [v2, setV2] = React.useState(null);
  React.useEffect(() => {
    let stop = false;
    const load = () => {
      fetch(B + "/api/volumen").then(r => r.json()).then(d => { if (!stop) setV(d); }).catch(() => {});
      fetch(B + "/api/volumen_v2").then(r => r.json()).then(d => { if (!stop) setV2(d); }).catch(() => {});
    };
    load();
    const id = setInterval(load, 60000);
    return () => { stop = true; clearInterval(id); };
  }, []);
  const fmt = (x) => x == null ? "\u2014" : Number(x).toLocaleString("es-CL");
  const chgTag = (p) => p == null ? <span style={{ color: "var(--text-3)" }}>\u2014</span> :
    <b style={{ color: p >= 0 ? "var(--buy)" : "var(--sell)" }}>{p >= 0 ? "\u25b2" : "\u25bc"}{Math.abs(p)}%</b>;
  if (!v) return null;

  const fila = (nombre, d) => (
    <div style={{ display: "flex", gap: 14, alignItems: "center", flexWrap: "nowrap", whiteSpace: "nowrap", overflowX: "auto" }}>
      <span style={{ width: 60, flexShrink: 0, color: "var(--text-2)", textTransform: "uppercase", letterSpacing: "0.06em", fontSize: 10, fontWeight: 600 }}>{nombre}</span>
      {!d ? <span style={{ color: "var(--text-3)" }}>sin datos a\u00fan</span> : <>
        <span style={{ flexShrink: 0 }} title="Acumulado desde las 00:00 de hoy (hora Chile). Se pone en CERO a la medianoche.">Hoy: <b style={{ color: "var(--text)" }}>{fmt(d.hoy)}</b></span>
        <span style={{ flexShrink: 0 }} title="Ventana movil: ultimos 60 min. La flecha compara contra los 60 min anteriores.">1h: <b style={{ color: "var(--text)" }}>{fmt(d.hora)}</b> {chgTag(d.cambio_1h_pct)}</span>
        <span style={{ flexShrink: 0 }} title="Ventana movil: ultimas 4 horas.">4h: <b style={{ color: "var(--text)" }}>{fmt(d.vol_4h)}</b> {chgTag(d.cambio_4h_pct)}</span>
        <span style={{ flexShrink: 0 }} title="Ventana movil: ultimas 24 horas.">24h: <b style={{ color: "var(--text)" }}>{fmt(d.vol_24h)}</b> {chgTag(d.cambio_24h_pct)}</span>
        <span style={{ display: "flex", alignItems: "center", gap: 6, flexShrink: 0 }}>
          Presi\u00f3n:
          <span style={{ width: 90, height: 7, background: "var(--sell)", borderRadius: 5, overflow: "hidden", display: "inline-block" }}>
            <span style={{ display: "block", height: "100%", width: d.presion_compra_pct + "%", background: "var(--buy)" }}></span>
          </span>
          <b style={{ color: "var(--buy)" }}>{fmt(d.presion_compra_pct)}%</b> <span style={{ color: "var(--text-3)" }}>compran</span>
        </span>
      </>}
    </div>
  );

  return (
    <div style={{ margin: "8px 0 0", background: "var(--bg-1)", border: "1px solid var(--line-soft)", borderRadius: 12, padding: "8px 16px", fontSize: 12, color: "var(--text-2)", fontVariantNumeric: "tabular-nums", display: "flex", flexDirection: "column", gap: 6 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <span style={{ color: "var(--text-3)", textTransform: "uppercase", letterSpacing: "0.08em", fontSize: 10 }}>Volumen USDT \u00b7 estimaci\u00f3n</span>
        <span title="Estimacion propia del tope del libro (no dato oficial). Sirve para la TENDENCIA (sube/baja), no para el USDT exacto. Ya descuenta el ruido de reposicion de avisos." style={{ color: "var(--text-3)", cursor: "help" }}>\u24d8</span>
      </div>
      {fila("Binance", v.binance)}
      {v2 && v2.binance && fila("BN v2 \u2713", v2.binance)}
      {fila("Bybit", v.bybit)}
      {v2 && v2.bybit && fila("BY v2 \u2713", v2.bybit)}
      {v2 && v2.binance && <div style={{ fontSize: 10, color: "var(--text-3)" }}>
        v2 = fills confirmados \u00b7 {fmt(v2.binance.ordenes_hoy)} \u00f3rdenes hoy \u00b7 ticket medio {fmt(v2.binance.ticket_med_hoy)} USDT \u00b7 {fmt(v2.binance.pct_enmascarado_hoy)}% estimado por recarga
      </div>}
    </div>
  );
}

function VelocidadMercado() {
  const B = (window.P2P_CONFIG && window.P2P_CONFIG.baseUrl) || "";
  const [ex, setEx] = React.useState("binance");
  const [d, setD] = React.useState(null);
  React.useEffect(() => {
    let stop = false;
    const load = () => fetch(B + "/api/velocidad_mercado?horas=12&bucket=15&exchange=" + ex)
      .then(r => r.json()).then(j => { if (!stop) setD(j); }).catch(() => {});
    load();
    const id = setInterval(load, 60000);
    return () => { stop = true; clearInterval(id); };
  }, [ex]);
  const fmt = (x) => x == null ? "\u2014" : Number(x).toLocaleString("es-CL");
  if (!d || !d.serie) return null;
  const s = d.serie;
  const W = 760, H = 110, mid = H / 2, padTop = 6;
  const maxV = Math.max(1, ...s.map(p => Math.max(p.buy, p.sell)));
  const bw = W / s.length;
  const ratio = d.vs_promedio;
  const ratioColor = ratio == null ? "var(--text-3)"
    : (ratio >= 1.3 ? "var(--buy)" : (ratio <= 0.7 ? "var(--sell)" : "var(--warn)"));
  const met = (label, val, extra) => (
    <div style={{ minWidth: 92 }}>
      <div style={{ fontSize: 10, color: "var(--text-3)", textTransform: "uppercase", letterSpacing: "0.08em" }}>{label}</div>
      <div style={{ fontFamily: "var(--mono)", fontSize: 19, color: "var(--text)", fontVariantNumeric: "tabular-nums" }}>{val}{extra}</div>
    </div>
  );
  return (
    <div style={{ margin: "10px 0 0", background: "var(--bg-1)", border: "1px solid var(--line)", borderRadius: 14, padding: "14px 16px" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", marginBottom: 10 }}>
        <h3 style={{ fontSize: 13.5, fontWeight: 600, color: "var(--text)" }}>Velocidad del mercado</h3>
        <span style={{ fontSize: 11, color: "var(--text-3)" }}>fills confirmados \u00b7 \u00faltimas 12h \u00b7 buckets 15 min</span>
        <span title="Rotacion medida desde fills CONFIRMADOS (caida de stock validada con el contador de ordenes completadas). No cuenta ediciones ni cancelaciones; los fills tapados por recargas se estiman con el ticket del anunciante." style={{ color: "var(--text-3)", cursor: "help" }}>\u24d8</span>
        <span style={{ marginLeft: "auto", display: "flex", gap: 4 }}>
          {["binance", "bybit"].map(x => (
            <button key={x} onClick={() => setEx(x)} style={{
              fontSize: 11, padding: "4px 11px", borderRadius: 7, cursor: "pointer",
              border: "1px solid " + (ex === x ? "var(--accent)" : "var(--line)"),
              background: ex === x ? "var(--accent-soft)" : "var(--bg-2)",
              color: ex === x ? "var(--accent)" : "var(--text-2)",
            }}>{x === "binance" ? "Binance" : "Bybit"}</button>
          ))}
        </span>
      </div>
      <div style={{ display: "flex", gap: 26, flexWrap: "wrap", marginBottom: 12, fontVariantNumeric: "tabular-nums" }}>
        {met("Ahora (30m)", fmt(d.usdt_min_30m), <span style={{ fontSize: 11, color: "var(--text-3)" }}> USDT/min</span>)}
        {met("Fills/h (60m)", fmt(d.fills_h_60m), null)}
        {met("Ticket medio 60m", fmt(d.ticket_med_60m), <span style={{ fontSize: 11, color: "var(--text-3)" }}> USDT</span>)}
        {met("Promedio 12h", fmt(d.usdt_min_prom), <span style={{ fontSize: 11, color: "var(--text-3)" }}> USDT/min</span>)}
        <div style={{ minWidth: 110 }}>
          <div style={{ fontSize: 10, color: "var(--text-3)", textTransform: "uppercase", letterSpacing: "0.08em" }}>vs promedio</div>
          <div style={{ fontFamily: "var(--mono)", fontSize: 19, color: ratioColor }}>
            {ratio == null ? "\u2014" : ratio + "x"}
            <span style={{ fontSize: 11, color: "var(--text-3)" }}> {ratio == null ? "" : (ratio >= 1.3 ? "acelerado" : (ratio <= 0.7 ? "lento" : "normal"))}</span>
          </div>
        </div>
      </div>
      <svg viewBox={"0 0 " + W + " " + (H + 16)} style={{ width: "100%", display: "block" }}>
        <line x1="0" y1={mid} x2={W} y2={mid} stroke="var(--line-soft)" strokeWidth="1" />
        {s.map((p, i) => {
          const hb = (p.buy  / maxV) * (mid - padTop);
          const hs = (p.sell / maxV) * (mid - padTop);
          return (
            <g key={i}>
              <title>{p.t + "  \u00b7  BUY " + fmt(p.buy) + "  \u00b7  SELL " + fmt(p.sell) + " USDT  \u00b7  " + fmt(p.ordenes) + " \u00f3rdenes"}</title>
              <rect x={i * bw + 1} y={mid - hb} width={Math.max(1, bw - 2)} height={hb} fill="var(--buy)" opacity="0.85" rx="1" />
              <rect x={i * bw + 1} y={mid} width={Math.max(1, bw - 2)} height={hs} fill="var(--sell)" opacity="0.85" rx="1" />
            </g>
          );
        })}
        {s.map((p, i) => (i % 8 === 0) ? (
          <text key={"t" + i} x={i * bw + 2} y={H + 12} fontSize="9"
            fill="var(--text-3)" fontFamily="var(--mono)">{p.t}</text>
        ) : null)}
      </svg>
      <div style={{ fontSize: 10.5, color: "var(--text-3)", marginTop: 6 }}>
        Barras hacia arriba = compras (BUY) \u00b7 hacia abajo = ventas (SELL) \u00b7 pas\u00e1 el cursor sobre una barra para el detalle
      </div>
    </div>
  );
}

function AsistenteOperativo() {
  const B = (window.P2P_CONFIG && window.P2P_CONFIG.baseUrl) || "";
  const [d, setD] = React.useState(null);
  React.useEffect(() => {
    let stop = false;
    const load = () => fetch(B + "/api/operativa").then(r => r.json()).then(j => { if (!stop) setD(j); }).catch(() => {});
    load();
    const id = setInterval(load, 60000);
    return () => { stop = true; clearInterval(id); };
  }, []);
  const fmt = (x) => x == null ? "\u2014" : Number(x).toLocaleString("es-CL");
  if (!d || d.error || !d.decision) return null;
  const toneMap = { green: "var(--buy)", yellow: "var(--warn)", orange: "var(--warn-low)", red: "var(--sell)" };
  const tone = toneMap[d.color] || "var(--accent)";
  const m = d.mercado || {}, p = d.precios || {}, lim = d.limites, pr = d.proyeccion || {};
  const esc10 = (pr.escenarios_captura || []).find(e => e.captura_pct === 10);
  const prReal = d.proyeccion_realista || null;
  const escHoy = prReal ? (prReal.escenarios || []).find(e => e.nombre === "hoy") : null;
  const escVer = prReal ? (prReal.escenarios || []).find(e => e.nombre === "verificado") : null;
  const box = (label, val, sub) => (
    <div style={{ flex: 1, minWidth: 150, background: "var(--bg-2)", border: "1px solid var(--line-soft)", borderRadius: 10, padding: "10px 13px" }}>
      <div style={{ fontSize: 10, color: "var(--text-3)", textTransform: "uppercase", letterSpacing: "0.08em" }}>{label}</div>
      <div style={{ fontFamily: "var(--mono)", fontSize: 20, color: "var(--text)", margin: "3px 0 1px", fontVariantNumeric: "tabular-nums" }}>{val}</div>
      {sub && <div style={{ fontSize: 10.5, color: "var(--text-3)" }}>{sub}</div>}
    </div>
  );
  return (
    <div style={{ margin: "10px 0 0", background: "var(--bg-1)", border: "1px solid var(--line)", borderLeft: "4px solid " + tone, borderRadius: 14, padding: "14px 16px" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        <span style={{ fontSize: 10.5, color: "var(--text-3)", textTransform: "uppercase", letterSpacing: "0.12em" }}>Asistente operativo</span>
        <span style={{ fontFamily: "var(--mono)", fontSize: 17, fontWeight: 600, color: tone }}>{d.decision}</span>
        <span title="Recomendacion generada por ciclo desde: spread neto vs tu minimo operativo, rotacion actual vs promedio 12h (fills confirmados) y presion compra/venta. Es una guia, no una orden." style={{ color: "var(--text-3)", cursor: "help" }}>\u24d8</span>
      </div>
      <div style={{ fontSize: 12.5, color: "var(--text-2)", margin: "6px 0 12px", maxWidth: 900 }}>{d.razon}</div>
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
        {box("Vender a (flujo)", fmt(p.flujo_vender), "margen " + fmt(p.margen_venta_pct) + "% \u00b7 agresivo: " + fmt(p.agresivo_vender))}
        {box("Comprar a (flujo)", fmt(p.flujo_comprar), "margen " + fmt(p.margen_compra_pct) + "% \u00b7 agresivo: " + fmt(p.agresivo_comprar))}
        {lim && box("L\u00edmites orden", fmt(lim.min_clp) + " \u2013 " + fmt(lim.max_clp) + " CLP", "ticket real p25-p90: " + fmt(lim.ticket_p25_usdt) + "\u2013" + fmt(lim.ticket_p90_usdt) + " USDT")}
        {box("Presi\u00f3n compra", fmt(m.presion_compra_pct) + "%", "flujo " + fmt(m.flujo_usdt_h) + " USDT/h \u00b7 " + (m.vs_promedio_12h == null ? "\u2014" : m.vs_promedio_12h + "x") + " vs 12h")}
        {escHoy && box("Proyecci\u00f3n realista",
          <span style={{ color: escHoy.clp_h != null && escHoy.clp_h < 0 ? "var(--sell)" : "var(--text)" }}>{fmt(escHoy.clp_h)} CLP/h</span>,
          "verificado: " + (escVer ? fmt(escVer.clp_h) : "\u2014") + " CLP/h \u00b7 ~" + fmt(escHoy.ordenes_h) + " \u00f3rd/h \u00b7 techo te\u00f3rico 10%: " + (esc10 ? fmt(esc10.ganancia_h_clp) : "\u2014"))}
        {!escHoy && esc10 && box("Proyecci\u00f3n (10% captura)", fmt(esc10.ganancia_h_clp) + " CLP/h", fmt(esc10.usdt_h) + " USDT/h \u00b7 " + fmt(esc10.giros_h) + " giros/h con " + fmt(pr.capital_usdt) + " USDT")}
      </div>
      {d.vacios_liquidez && d.vacios_liquidez.length > 0 && (
        <div style={{ marginTop: 12, display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <span style={{ fontSize: 10.5, color: "var(--text-3)", textTransform: "uppercase", letterSpacing: "0.08em" }} title="Competidores con stock para menos de 30 min al ritmo actual de fills. Cuando se agoten, su hueco en el libro queda libre: ventana para subir tu precio y aun asi llenar.">Por agotarse \u26a1</span>
          {d.vacios_liquidez.map((x, i) => (
            <span key={i} title={"Posici\u00f3n " + fmt(x.posicion) + " del tab " + (x.tipo === "BUY" ? "Compra" : "Venta") + " \u00b7 consume " + fmt(x.velocidad_usdt_min) + " USDT/min. Cuando se agote, su precio queda libre: hueco para pararte ah\u00ed."}
              style={{ fontSize: 11.5, fontFamily: "var(--mono)", background: "var(--bg-2)", border: "1px solid var(--line-soft)", borderRadius: 7, padding: "3px 9px", color: "var(--text-2)", cursor: "default" }}>
              <b style={{ color: x.tipo === "BUY" ? "var(--buy)" : "var(--sell)" }}>{x.tipo === "BUY" ? "COMPRA" : "VENTA"}</b> {x.anunciante} \u00b7 ${fmt(x.precio)} \u00b7 quedan {fmt(x.disponible)} USDT \u00b7 ~{fmt(x.min_restantes)} min
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function PlanHoy() {
  const B = (window.P2P_CONFIG && window.P2P_CONFIG.baseUrl) || "";
  const [d, setD] = React.useState(null);
  const [abierto, setAbierto] = React.useState(true);
  React.useEffect(() => {
    let stop = false;
    const load = () => fetch(B + "/api/plan_hoy").then(r => r.json())
      .then(j => { if (!stop) setD(j); }).catch(() => {});
    load();
    const id = setInterval(load, 120000);
    return () => { stop = true; clearInterval(id); };
  }, []);
  if (!d || d.indice_hora == null) return null;
  const tonos = { excelente: "var(--buy)", buena: "var(--buy)", floja: "var(--warn)", mala: "var(--sell)" };
  const tono = tonos[d.calidad] || "var(--accent)";
  const f = (x, n) => x == null ? "—" : Number(x).toFixed(n == null ? 2 : n);
  return (
    <div style={{ margin: "10px 0 0", background: "var(--bg-1)", border: "1px solid var(--line)",
                  borderLeft: "4px solid " + tono, borderRadius: 14, padding: "13px 16px" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        <span style={{ fontSize: 10.5, color: "var(--text-3)", textTransform: "uppercase", letterSpacing: "0.12em" }}>Plan de hoy</span>
        <span style={{ fontFamily: "var(--mono)", fontSize: 17, fontWeight: 600, color: tono }}>
          {String(d.hora).padStart(2, "0")}h · hora {d.calidad}
        </span>
        <span style={{ fontFamily: "var(--mono)", fontSize: 12, color: "var(--text-3)" }}>
          índice {f(d.indice_hora, 0)}/100
        </span>
        <span title={d.nota} style={{ color: "var(--text-3)", cursor: "help" }}>ⓘ</span>
        <button onClick={() => setAbierto(!abierto)}
          style={{ marginLeft: "auto", background: "transparent", border: "1px solid var(--line)",
                   borderRadius: 7, color: "var(--text-3)", fontSize: 11, padding: "3px 10px", cursor: "pointer" }}>
          {abierto ? "ocultar" : "ver detalle"}
        </button>
      </div>
      <div style={{ fontSize: 13, color: "var(--text)", margin: "8px 0 0", lineHeight: 1.6 }}>
        {(d.acciones || []).map((a, i) => (
          <div key={i} style={{ display: "flex", gap: 8 }}>
            <span style={{ color: tono }}>▸</span><span>{a}</span>
          </div>
        ))}
      </div>
      {abierto && (
        <div style={{ marginTop: 12, display: "flex", gap: 16, flexWrap: "wrap", alignItems: "flex-end" }}>
          <div>
            <div style={{ fontSize: 10, color: "var(--text-3)", textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 5 }}>Próximas horas</div>
            <div style={{ display: "flex", gap: 4 }}>
              {(d.proximas_horas || []).map(p => {
                const c = p.indice >= 75 ? "var(--buy)" : p.indice >= 55 ? "var(--warn)" : p.indice >= 35 ? "var(--warn-low)" : "var(--sell)";
                return (
                  <div key={p.hora} style={{ textAlign: "center", minWidth: 40 }}>
                    <div style={{ height: 34, display: "flex", alignItems: "flex-end", justifyContent: "center" }}>
                      <div style={{ width: 22, height: Math.max(3, p.indice / 100 * 34), background: c, borderRadius: 2 }} />
                    </div>
                    <div style={{ fontFamily: "var(--mono)", fontSize: 9.5, color: "var(--text-3)", marginTop: 3 }}>{String(p.hora).padStart(2, "0")}</div>
                    <div style={{ fontFamily: "var(--mono)", fontSize: 9, color: c }}>{f(p.indice, 0)}</div>
                  </div>
                );
              })}
            </div>
          </div>
          <div style={{ fontSize: 11.5, color: "var(--text-2)", lineHeight: 1.7 }}>
            <div>Gap sugerido para esta hora: <b style={{ color: "var(--text)" }}>{f(d.gap_sugerido)}%</b> <span style={{ color: "var(--text-3)" }}>(tenés {f(d.gap_actual)}%)</span></div>
            <div>Spread típico de las {String(d.hora).padStart(2, "0")}h: <b style={{ color: "var(--text)" }}>{f(d.spread_hora_med, 3)}%</b> · ahora: <b style={{ color: "var(--text)" }}>{f(d.spread_ahora_pct, 3)}%</b></div>
            <div>Posición objetivo: <b style={{ color: "var(--text)" }}>{d.posicion_objetivo}</b>{d.ritmo_ord_h ? <> · el mercado ahí da <b style={{ color: "var(--text)" }}>{f(d.ritmo_ord_h, 1)}</b> órd/h por pierna</> : null}</div>
          </div>
        </div>
      )}
    </div>
  );
}

function MiCampania() {
  const B = (window.P2P_CONFIG && window.P2P_CONFIG.baseUrl) || "";
  const [d, setD] = React.useState(null);
  const [cal, setCal] = React.useState(null);
  React.useEffect(() => {
    let stop = false;
    const load = () => {
      fetch(B + "/api/mi_posicion").then(r => r.json()).then(j => { if (!stop) setD(j); }).catch(() => {});
      fetch(B + "/api/calibracion").then(r => r.json()).then(j => { if (!stop) setCal(j); }).catch(() => {});
    };
    load();
    const id = setInterval(load, 60000);
    return () => { stop = true; clearInterval(id); };
  }, []);
  if (!d || !d.configurado) return null;
  const fmt = (x) => x == null ? "—" : Number(x).toLocaleString("es-CL");
  const pr = d.progreso || {};
  const bar = (pct, tone) => (
    <div className="hbar" style={{ marginTop: 5 }}>
      <div className="hbar-fill" style={{ width: Math.min(100, pct || 0) + "%", background: tone }} />
    </div>
  );
  const boxSt = { flex: 1, minWidth: 170, background: "var(--bg-2)", border: "1px solid var(--line-soft)", borderRadius: 10, padding: "10px 13px" };
  const lbl = { fontSize: 10, color: "var(--text-3)", textTransform: "uppercase", letterSpacing: "0.08em" };
  const val = { fontFamily: "var(--mono)", fontSize: 17, color: "var(--text)", margin: "3px 0 1px", fontVariantNumeric: "tabular-nums" };
  const sub = { fontSize: 10.5, color: "var(--text-3)" };
  const adBox = (a) => (
    <div key={a.rol} style={boxSt}>
      <div style={lbl}>{a.rol} <span style={{ opacity: 0.7 }}>(tab {a.tab})</span></div>
      {!a.publicado && <div style={{ ...val, color: "var(--text-3)" }}>fuera del top-80</div>}
      {!a.publicado && <div style={sub}>sin anuncio publicado o muy abajo en el libro</div>}
      {a.publicado && (
        <div style={{ ...val, color: a.en_objetivo ? "var(--buy)" : "var(--warn)" }}>
          #{a.posicion} · ${fmt(a.precio)}
        </div>
      )}
      {a.publicado && (
        <div style={sub}>
          stock {fmt(a.disponible)} USDT
          {a.en_objetivo ? " · ✓ en objetivo (top " + a.posicion_objetivo + ")" : ""}
          {!a.en_objetivo && a.precio_sugerido ? " · reajustá a $" + fmt(a.precio_sugerido) + " p/ top " + a.posicion_objetivo : ""}
        </div>
      )}
    </div>
  );
  return (
    <div style={{ margin: "10px 0 0", background: "var(--bg-1)", border: "1px solid var(--line)", borderLeft: "4px solid var(--accent)", borderRadius: 14, padding: "13px 16px" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", marginBottom: 10 }}>
        <span style={{ fontSize: 10.5, color: "var(--text-3)", textTransform: "uppercase", letterSpacing: "0.12em" }}>Carrera al verificado</span>
        <span style={{ fontFamily: "var(--mono)", fontSize: 12.5, color: "var(--accent)", fontWeight: 600 }}>{d.nick}</span>
        <span title={d.nota} style={{ color: "var(--text-3)", cursor: "help" }}>ⓘ</span>
        {!d.en_libro && <span style={{ fontSize: 11, color: "var(--text-3)" }}>· no aparecés en el libro ahora</span>}
      </div>
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
        {(d.anuncios || []).map(adBox)}
        <div style={boxSt}>
          <div style={lbl}>Órdenes 30d (meta 300)</div>
          <div style={val}>{fmt(pr.ordenes_30d)} <span style={{ fontSize: 11, color: "var(--text-3)" }}>/ 300</span></div>
          {bar(pr.ordenes_pct, "var(--accent)")}
          <div style={sub}>{pr.ordenes_ganadas_7d != null ? "+" + fmt(pr.ordenes_ganadas_7d) + " esta semana" : "contador oficial de Binance"}</div>
        </div>
        <div style={boxSt}>
          <div style={lbl}>Volumen 30d estimado</div>
          <div style={val}>{fmt(pr.vol_30d_estimado)} <span style={{ fontSize: 11, color: "var(--text-3)" }}>USDT</span></div>
          {bar(pr.vol_pct_minima, "var(--buy)")}
          <div style={sub}>{fmt(pr.vol_pct_minima)}% de 0,5 BTC (mínimo) · {fmt(pr.vol_pct_comoda)}% de 1 BTC</div>
        </div>
      </div>
      {cal && cal.resumen && cal.resumen.ordenes_maker_reales > 0 && (
        <div style={{ marginTop: 12, paddingTop: 10, borderTop: "1px solid var(--line-soft)" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", marginBottom: 8 }}>
            <span style={{ fontSize: 10.5, color: "var(--text-3)", textTransform: "uppercase", letterSpacing: "0.1em" }}>Calibración · realidad vs monitor</span>
            <span title={cal.nota} style={{ color: "var(--text-3)", cursor: "help" }}>ⓘ</span>
            <span style={{ fontSize: 10.5, color: "var(--text-3)" }}>últimos {cal.resumen.dias} días · solo órdenes maker</span>
          </div>
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
            <div style={boxSt}>
              <div style={lbl}>Detección</div>
              <div style={val}>{fmt(cal.resumen.ordenes_detectadas)}/{fmt(cal.resumen.ordenes_maker_reales)}</div>
              <div style={sub}>{cal.resumen.tasa_deteccion_pct != null ? fmt(cal.resumen.tasa_deteccion_pct) + "% de tus órdenes vistas" : "—"}</div>
            </div>
            <div style={boxSt}>
              <div style={lbl}>Volumen real vs estimado</div>
              <div style={val}>{fmt(cal.resumen.usdt_real)} <span style={{ fontSize: 11, color: "var(--text-3)" }}>vs</span> {fmt(cal.resumen.usdt_monitor)}</div>
              <div style={{ ...sub, color: cal.resumen.error_pct == null ? "var(--text-3)" : Math.abs(cal.resumen.error_pct) <= 5 ? "var(--buy)" : "var(--warn)" }}>
                {cal.resumen.error_pct == null ? "—" : (cal.resumen.error_pct > 0 ? "+" : "") + fmt(cal.resumen.error_pct) + "% de error"}
              </div>
            </div>
            <div style={boxSt}>
              <div style={lbl}>Latencia de detección</div>
              <div style={val}>{cal.resumen.latencia_media_min != null ? fmt(cal.resumen.latencia_media_min) + " min" : "—"}</div>
              <div style={sub}>demora en confirmarte una orden</div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function EstrategiaPanel() {
  const B = (window.P2P_CONFIG && window.P2P_CONFIG.baseUrl) || "";
  const [cfg, setCfg] = React.useState(null);
  const [gap, setGap] = React.useState("");
  const [cap, setCap] = React.useState("");
  const [minop, setMinop] = React.useState("");
  const [nickI, setNickI] = React.useState("");
  const [msg, setMsg] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const load = () => fetch(B + "/api/config").then(r => r.json()).then(d => {
    setCfg(d);
    setGap(String(d.GAP_OBJETIVO_BRUTO != null ? d.GAP_OBJETIVO_BRUTO : 1.25));
    setCap(String(d.CAPITAL_OPERATIVO != null ? d.CAPITAL_OPERATIVO : 600));
    setMinop(String(d.SPREAD_MIN_OPERATIVO != null ? d.SPREAD_MIN_OPERATIVO : 0.28));
    setNickI(String(d.MI_NICKNAME || ""));
  }).catch(() => {});
  React.useEffect(() => { load(); }, []);
  const base = { UMBRAL_ROT_LENTO: 0.5, UMBRAL_ROT_DUAL: 0.8, UMBRAL_PRESION_SESGO: 15 };
  const aplicar = (body, nombre) => {
    if (busy) return;
    setBusy(true); setMsg("Aplicando...");
    window.P2P_AUTH.post(B + "/api/config", body)
      .then(r => r.json().then(d => ({ ok: r.ok && d && d.ok !== false, d })))
      .then(({ ok }) => {
        if (!ok) { setMsg("\u2717 No se aplic\u00f3 (\u00bftoken?)"); setBusy(false); return; }
        setMsg("\u2713 Aplicado: " + nombre); load(); setBusy(false); setTimeout(() => setMsg(""), 5000);
      })
      .catch(() => { setMsg("\u2717 Error al aplicar"); setBusy(false); });
  };
  const presets = [
    { n: "Margen ancho", gap: 1.35, min: 0.28,  d: "pocos giros, m\u00e1ximo por peso" },
    { n: "Equilibrado",  gap: 1.25, min: 0.28,  d: "la base recomendada" },
    { n: "Rotaci\u00f3n",     gap: 1.10, min: 0.28,  d: "llena r\u00e1pido, m\u00e1s giros" },
    { n: "Farming",      gap: 0.60, min: -0.20, d: "farmea \u00f3rdenes a margen m\u00ednimo" },
  ];
  const gapActual = cfg ? Number(cfg.GAP_OBJETIVO_BRUTO || 0) : null;
  const minActual = cfg ? Number(cfg.SPREAD_MIN_OPERATIVO) : null;
  const esBaseOk = cfg && Number(cfg.UMBRAL_PRESION_SESGO) === 15 && Number(cfg.UMBRAL_ROT_LENTO) === 0.5;
  return (
    <div style={{ margin: "10px 0 0", background: "var(--bg-1)", border: "1px solid var(--line)", borderRadius: 14, padding: "13px 16px" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", marginBottom: 10 }}>
        <span style={{ fontSize: 10.5, color: "var(--text-3)", textTransform: "uppercase", letterSpacing: "0.12em" }}>Estrategia</span>
        {cfg && <span style={{ fontFamily: "var(--mono)", fontSize: 11.5, color: "var(--text-2)" }}>
          gap {gapActual}% \u00b7 m\u00ednimo sem\u00e1foro {Number(cfg.SPREAD_MIN_OPERATIVO)}% \u00b7 capital {Number(cfg.CAPITAL_OPERATIVO)} USDT
          {!esBaseOk && <span style={{ color: "var(--warn)" }}> \u00b7 umbrales base sin aplicar</span>}
        </span>}
        {msg && <span style={{ fontFamily: "var(--mono)", fontSize: 11.5, color: msg.indexOf("\u2713") === 0 ? "var(--buy)" : "var(--warn)", marginLeft: "auto" }}>{msg}</span>}
      </div>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "stretch" }}>
        {presets.map(p => {
          const activo = gapActual != null && Math.abs(gapActual - p.gap) < 0.001 && minActual != null && Math.abs(minActual - p.min) < 0.001;
          return (
            <button key={p.n} disabled={busy}
              onClick={() => aplicar(Object.assign({}, base, { GAP_OBJETIVO_BRUTO: p.gap, SPREAD_MIN_OPERATIVO: p.min }), p.n + " (gap " + p.gap + ", m\u00edn " + p.min + ")")}
              style={{ flex: 1, minWidth: 130, textAlign: "left", cursor: "pointer", borderRadius: 10, padding: "9px 12px",
                border: "1px solid " + (activo ? "var(--accent)" : "var(--line)"),
                background: activo ? "var(--accent-soft)" : "var(--bg-2)", color: "var(--text)" }}>
              <div style={{ fontFamily: "var(--mono)", fontSize: 12.5, fontWeight: 600, color: activo ? "var(--accent)" : "var(--text)" }}>
                {activo ? "\u25cf " : ""}{p.n} \u00b7 {p.gap}%
              </div>
              <div style={{ fontSize: 10.5, color: "var(--text-3)", marginTop: 2 }}>{p.d}</div>
            </button>
          );
        })}
        <div style={{ display: "flex", gap: 6, alignItems: "center", background: "var(--bg-2)", border: "1px solid var(--line)", borderRadius: 10, padding: "6px 10px" }}>
          <span style={{ fontSize: 10, color: "var(--text-3)" }}>gap</span>
          <input value={gap} onChange={e => setGap(e.target.value)} inputMode="decimal"
            style={{ width: 46, background: "var(--bg-1)", border: "1px solid var(--line-soft)", borderRadius: 6, color: "var(--text)", fontFamily: "var(--mono)", fontSize: 12, padding: "4px 6px" }} />
          <span style={{ fontSize: 10, color: "var(--text-3)" }}>m\u00edn</span>
          <input value={minop} onChange={e => setMinop(e.target.value)} inputMode="decimal"
            style={{ width: 46, background: "var(--bg-1)", border: "1px solid var(--line-soft)", borderRadius: 6, color: "var(--text)", fontFamily: "var(--mono)", fontSize: 12, padding: "4px 6px" }} />
          <span style={{ fontSize: 10, color: "var(--text-3)" }}>capital</span>
          <input value={cap} onChange={e => setCap(e.target.value)} inputMode="numeric"
            style={{ width: 56, background: "var(--bg-1)", border: "1px solid var(--line-soft)", borderRadius: 6, color: "var(--text)", fontFamily: "var(--mono)", fontSize: 12, padding: "4px 6px" }} />
          <button disabled={busy}
            onClick={() => {
              const g = parseFloat(String(gap).replace(",", ".")), c = parseFloat(String(cap).replace(",", ".")), m = parseFloat(String(minop).replace(",", "."));
              if (!(g > 0.3 && g < 5) || !(c > 0) || !(m >= -1 && m <= 5)) { setMsg("\u2717 Valores fuera de rango"); return; }
              aplicar(Object.assign({}, base, { GAP_OBJETIVO_BRUTO: g, CAPITAL_OPERATIVO: c, SPREAD_MIN_OPERATIVO: m }), "personalizado (gap " + g + ", m\u00edn " + m + ")");
            }}
            style={{ cursor: "pointer", borderRadius: 7, padding: "5px 12px", border: "1px solid var(--accent)", background: "var(--accent-soft)", color: "var(--accent)", fontSize: 11.5, fontFamily: "var(--mono)" }}>
            Aplicar
          </button>
        </div>
        <div style={{ display: "flex", gap: 6, alignItems: "center", background: "var(--bg-2)", border: "1px solid var(--line)", borderRadius: 10, padding: "6px 10px" }}
          title="Tu nickname de Binance P2P. Activa el panel 'Carrera al verificado': posición de tus anuncios, sugerencia de reprecio y progreso hacia Merchant.">
          <span style={{ fontSize: 10, color: "var(--text-3)" }}>mi nick</span>
          <input value={nickI} onChange={e => setNickI(e.target.value)} placeholder="(sin configurar)"
            style={{ width: 110, background: "var(--bg-1)", border: "1px solid var(--line-soft)", borderRadius: 6, color: "var(--text)", fontFamily: "var(--mono)", fontSize: 12, padding: "4px 6px" }} />
          <button disabled={busy}
            onClick={() => aplicar({ MI_NICKNAME: nickI.trim() }, nickI.trim() ? "nick " + nickI.trim() : "nick borrado")}
            style={{ cursor: "pointer", borderRadius: 7, padding: "5px 12px", border: "1px solid var(--accent)", background: "var(--accent-soft)", color: "var(--accent)", fontSize: 11.5, fontFamily: "var(--mono)" }}>
            Guardar
          </button>
        </div>
      </div>
      <div style={{ fontSize: 10, color: "var(--text-3)", marginTop: 8 }}>
        El "m\u00ednimo sem\u00e1foro" es el margen NETO m\u00ednimo (ya con comisi\u00f3n) para que diga OPERAR. Farming lo baja a -0,2%: farmea \u00f3rdenes aceptando una p\u00e9rdida m\u00ednima, para reputaci\u00f3n. Pod\u00e9s editar gap, m\u00edn y capital a mano.
      </div>
    </div>
  );
}

window.P2PViews = { TiempoReal, Historico, Heatmap, PrecioChart, Inteligencia, Backup, BackupBanner, RotacionCalc, CrossView, Muros, SystemBar, VolumenBar, VelocidadMercado, AsistenteOperativo, EstrategiaPanel, MiCampania, PlanHoy };

</script>
<script type="text/babel">
/* ============================================================
   Unión Austral · P2P Monitor — App raíz, tabs y tweaks
   ============================================================ */
const { useState: mS, useEffect: mE, useRef: mR } = React;
const V = window.P2PViews, Core = window.P2PCore;

const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "direction": "cockpit",
  "accent": "#5b8def",
  "density": "comoda",
  "animatePrices": true,
  "orderBook": true
}/*EDITMODE-END*/;

function useEngine() {
  const [state, setState] = mS(() => null);
  const engRef = mR(null);
  mE(() => {
    const cfg = window.P2P_CONFIG || { mode: "demo" };
    const eng = cfg.mode === "live" && window.P2P.createLiveEngine
      ? window.P2P.createLiveEngine({ baseUrl: cfg.baseUrl || "", pollMs: cfg.pollMs || 30000, intervaloMin: cfg.intervaloMin || 5 })
      : window.P2P.createEngine({ cycleMs: 30000 });
    engRef.current = eng;
    const unsub = eng.subscribe((s) => setState(s));
    return () => { unsub(); eng.stop(); };
  }, []);
  return [state, engRef];
}

function App() {
  const [t, setTweak] = useTweaks(TWEAK_DEFAULTS);
  const [tab, setTab] = mS("tr");
  const [state, engRef] = useEngine();
  const [secondsLeft, setSecondsLeft] = mS(30);
  const [filters, setFilters] = mS(() => {
    try { return { ...window.P2P.FILTROS_DEFAULT, ...JSON.parse(localStorage.getItem("ua_p2p_filters") || "{}") }; }
    catch (e) { return window.P2P.FILTROS_DEFAULT; }
  });
  const applyFilters = (cfg) => {
    setFilters(cfg);
    try { localStorage.setItem("ua_p2p_filters", JSON.stringify(cfg)); } catch (e) {}
  };

  // countdown ring
  mE(() => {
    if (!state) return;
    const id = setInterval(() => {
      const left = Math.max(0, (state.cycleMs - (Date.now() - state.cycleStart)) / 1000);
      setSecondsLeft(left);
    }, 200);
    return () => clearInterval(id);
  }, [state && state.cycleStart]);

  // aplicar tweaks al root
  mE(() => {
    const r = document.documentElement;
    r.setAttribute("data-dir", t.direction);
    r.setAttribute("data-density", t.density);
    const acc = t.direction === "retro" ? "#35e07a" : t.accent;
    r.style.setProperty("--accent", acc);
    r.style.setProperty("--accent-soft", `color-mix(in oklch, ${acc} 16%, transparent)`);
    r.style.setProperty("--accent-glow", `color-mix(in oklch, ${acc} 40%, transparent)`);
  }, [t.direction, t.density, t.accent]);

  if (!state) return <div className="loading">Conectando al mercado…</div>;
  const { snap, history, heatmap, count, vel } = state;
  const viewSnap = window.P2P.applyFilters(snap, filters);

  return (
    <div className="app" data-animate={t.animatePrices ? "on" : "off"}>
      <Core.TopBar snap={viewSnap} secondsLeft={secondsLeft} cycleMs={state.cycleMs} />
      <Core.Tabs tab={tab} setTab={setTab} />
      <V.VolumenBar />
      {tab !== "backup" && <V.BackupBanner onGo={() => setTab("backup")} />}
      <main className="content">
        {tab === "tr" && <V.PlanHoy />}
        {tab === "tr" && <V.EstrategiaPanel />}
        {tab === "tr" && <V.MiCampania />}
        {tab === "tr" && <V.AsistenteOperativo />}
        {tab === "tr" && <V.VelocidadMercado />}
        {tab === "tr" && <V.TiempoReal snap={viewSnap} history={history} showOrderBook={t.orderBook} vel={vel}
          filters={{ cfg: filters, onApply: applyFilters, info: viewSnap._filtro }} />}
        {tab === "hist" && <V.Historico history={history} />}
        {tab === "precio" && <V.PrecioChart />}
        {tab === "intel" && <V.Inteligencia />}
        {tab === "heat" && <V.Heatmap heatmap={heatmap} />}
        {tab === "rot" && <V.RotacionCalc />}
        {tab === "cross" && <V.CrossView />}
        {tab === "muros" && <V.Muros />}
        {tab === "backup" && <V.Backup />}
      </main>
      <footer className="foot">
        <span className="foot-snap tnum">{window.P2P.fmtNum(count)}</span> snapshots guardados
        <span className="foot-sep">·</span>
        <span>Próximo ciclo en <b className="tnum">{Math.ceil(secondsLeft)}s</b></span>
        <span className="foot-sep">·</span>
        <span className="foot-demo">Unión Austral Capital · USDT/CLP · Binance P2P</span>
        <span className="foot-sep">·</span>
        <span className="foot-demo tnum">{{VERSION}}</span>
      </footer>

      <TweaksPanel>
        <TweakSection label="Dirección visual" />
        <TweakRadio label="Estilo" value={t.direction}
          options={["cockpit", "calmo", "contraste", "retro"]}
          onChange={(v) => setTweak("direction", v)} />
        <TweakSection label="Identidad" />
        <TweakColor label="Acento de marca" value={t.accent}
          options={["#5b8def", "#3fae9a", "#8a7cf0", "#d9a441"]}
          onChange={(v) => setTweak("accent", v)} />
        <TweakRadio label="Densidad" value={t.density}
          options={["compacta", "comoda"]}
          onChange={(v) => setTweak("density", v)} />
        <TweakSection label="Comportamiento" />
        <TweakToggle label="Animar precios al cambiar" value={t.animatePrices}
          onChange={(v) => setTweak("animatePrices", v)} />
        <TweakToggle label="Mostrar libro de órdenes" value={t.orderBook}
          onChange={(v) => setTweak("orderBook", v)} />
        <TweakButton label="Forzar nuevo ciclo" onClick={() => engRef.current && engRef.current.forceCycle()} />
      </TweaksPanel>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);

</script>
</body>
</html>"""

# ──────────────────────────────────────────────
#  RUTAS
# ──────────────────────────────────────────────
@app.route("/")
def index():
    html = DASHBOARD.replace("{{VERSION}}", f"{VERSION} · {VERSION_FECHA}")
    return Response(html, mimetype='text/html')

@app.route("/api/version")
def api_version():
    """Version del codigo corriendo (para chequear deploys al instante)."""
    return jsonify({"version": VERSION, "fecha": VERSION_FECHA})

def _token_ok():
    """Autorizacion de los POST sensibles. Sin APP_TOKEN configurado no exige
    nada (retrocompatible). Con APP_TOKEN, el request debe traer el header
    X-App-Token con el mismo valor (el frontend lo pide 1 vez y lo guarda
    en localStorage)."""
    if not APP_TOKEN:
        return True
    return request.headers.get("X-App-Token", "") == APP_TOKEN

def clean(data):
    out = {}
    for k, v in data.items():
        if k in ("detalle_compra", "detalle_venta"):
            out[k] = v
        elif isinstance(v, bool):
            out[k] = v
        elif isinstance(v, int):
            out[k] = v
        elif isinstance(v, float):
            out[k] = v
        elif hasattr(v, "__float__"):   # Decimal de psycopg2
            out[k] = float(v)
        elif hasattr(v, "isoformat"):   # datetime
            out[k] = str(v)
        else:
            out[k] = v
    return out

@app.route("/api/estado")
def api_estado():
    # Datos principales desde DB, detalle desde memoria
    snap = clean(obtener_ultimo())
    if not snap:
        return jsonify({})
    with data_lock:
        snap["detalle_compra"] = ultimo_estado.get("detalle_compra", [])
        snap["detalle_venta"]  = ultimo_estado.get("detalle_venta",  [])
    return jsonify(snap)

@app.route("/api/historial")
def api_historial():
    return jsonify([clean(r) for r in obtener_historial()])

@app.route("/api/precios")
def api_precios():
    """Serie completa de precios para el gráfico interactivo.
    Devuelve tiempo en Unix UTC (segundos) + ambos precios ponderados.
    Lightweight Charts v4 requiere timestamps estrictamente crecientes — se garantiza."""
    from datetime import datetime as _dt, timezone as _tz
    rows = obtener_precios_historico()
    out_compra, out_venta = [], []
    prev_unix = 0
    for r in rows:
        ts = r["timestamp"]
        try:
            # La hora guardada es hora de pared de Santiago. La codificamos como
            # epoch-UTC para que getUTCHours() en el front muestre esa MISMA hora
            # (hora Chile) y coincida con el resto del panel (Historico, etc.).
            ts_str = str(ts)[:19].replace(" ", "T")
            dt_naive = _dt.fromisoformat(ts_str)
            unix = int(dt_naive.replace(tzinfo=_tz.utc).timestamp())
        except Exception:
            continue
        # Garantizar estrictamente creciente (Lightweight Charts lo exige)
        if unix <= prev_unix:
            unix = prev_unix + 1
        prev_unix = unix
        pc = r.get("precio_pond_tab_compra")
        pv = r.get("precio_pond_tab_venta")
        if pc is not None:
            out_compra.append({"time": unix, "value": round(float(pc), 2)})
        if pv is not None:
            out_venta.append({"time": unix, "value": round(float(pv), 2)})
    return jsonify({"compra": out_compra, "venta": out_venta})

@app.route("/api/inteligencia/horario")
def api_intel_horario():
    """Spread neto + liquidez por hora — últimos 7 días.
    spread_neto = spread_bruto - comision_total (dinámica desde config)."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT hora,
                    ROUND(AVG(spread_pond_pct)::numeric,3) AS spread_bruto,
                    ROUND(AVG(liq_tab_compra)::numeric,0)  AS liq_compra,
                    ROUND(AVG(liq_tab_venta)::numeric,0)   AS liq_venta,
                    COUNT(*)                               AS muestras
                FROM snapshots
                WHERE timestamp >= NOW() - INTERVAL '7 days'
                GROUP BY hora ORDER BY hora
            """)
            rows = cur.fetchall()
    with config_lock:
        comision_total = config["COMISION_BN"] * 2 * 100
        spread_min_op  = config["SPREAD_MIN_OPERATIVO"]
    result = []
    for r in rows:
        d = dict(r)
        bruto = float(d["spread_bruto"]) if d["spread_bruto"] is not None else None
        if bruto is not None:
            neto = round(bruto - comision_total, 3)
            d["spread_neto"]  = neto
            d["brecha_ok"]    = neto >= spread_min_op
        else:
            d["spread_neto"] = None
            d["brecha_ok"]   = False
        result.append(d)
    return jsonify(result)

@app.route("/api/inteligencia/anunciantes")
def api_intel_anunciantes():
    """Merchants con capital 500-8000 USDT: rotación, horario, fill rate — últimos 7 días"""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                WITH base AS (
                    SELECT anunciante,
                        hora,
                        AVG(disponible)          AS disp_med,
                        COUNT(*)                 AS apariciones,
                        MAX(completadas)         AS ordenes_hist,
                        AVG(tasa_exito)          AS tasa,
                        BOOL_OR(es_merchant)     AS merchant
                    FROM snapshots_detalle
                    WHERE snapshot_timestamp >= NOW() - INTERVAL '7 days'
                      AND tipo = 'BUY'
                    GROUP BY anunciante, hora
                ),
                perfil AS (
                    SELECT anunciante,
                        ROUND(AVG(disp_med)::numeric,0)     AS capital,
                        ROUND(AVG(tasa)::numeric,1)         AS tasa_exito,
                        MAX(ordenes_hist)                   AS ordenes,
                        BOOL_OR(merchant)                   AS merchant,
                        COUNT(DISTINCT hora)                AS horas_activas,
                        (array_agg(hora ORDER BY apariciones DESC))[1] AS hora_pico,
                        SUM(apariciones)                    AS total_apariciones
                    FROM base
                    GROUP BY anunciante
                )
                SELECT * FROM perfil
                WHERE merchant = true
                  AND capital BETWEEN 500 AND 8000
                  AND tasa_exito >= 90
                  AND total_apariciones >= 50
                ORDER BY total_apariciones DESC
                LIMIT 20
            """)
            rows = cur.fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/inteligencia/fill")
def api_intel_fill():
    """Velocidad de fill por posición y hora — últimos 7 días.
    Usa LAG() en lugar de subquery correlacionada (O(n) vs O(n²))."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                WITH lagged AS (
                    SELECT
                        hora, posicion,
                        disponible,
                        LAG(disponible) OVER (
                            PARTITION BY anunciante, tipo
                            ORDER BY snapshot_timestamp
                        ) AS disp_prev,
                        EXTRACT(EPOCH FROM (
                            snapshot_timestamp -
                            LAG(snapshot_timestamp) OVER (
                                PARTITION BY anunciante, tipo
                                ORDER BY snapshot_timestamp
                            )
                        )) / 60 AS delta_min
                    FROM snapshots_detalle
                    WHERE snapshot_timestamp >= NOW() - INTERVAL '7 days'
                      AND es_merchant = true
                      AND tipo = 'BUY'
                )
                SELECT
                    hora,
                    CASE WHEN posicion <= 3  THEN 'p01-03'
                         WHEN posicion <= 10 THEN 'p04-10'
                         WHEN posicion <= 20 THEN 'p11-20'
                         WHEN posicion <= 40 THEN 'p21-40'
                         ELSE 'p41+' END AS rango_pos,
                    ROUND(AVG(
                        CASE WHEN disp_prev - disponible > 10
                              AND delta_min BETWEEN 1 AND 6
                             THEN disp_prev - disponible END
                    )::numeric, 0) AS consumo_med,
                    COUNT(
                        CASE WHEN disp_prev - disponible > 10
                              AND delta_min BETWEEN 1 AND 6
                             THEN 1 END
                    ) AS eventos
                FROM lagged
                WHERE disp_prev IS NOT NULL
                GROUP BY hora, rango_pos
                ORDER BY hora, rango_pos
            """)
            rows = cur.fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/inteligencia/top_traders")
def api_intel_top_traders():
    """Top 10 traders más activos con su estrategia de precios — últimos 7 días"""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    anunciante,
                    tipo,
                    ROUND(AVG(disponible)::numeric,0)       AS capital_med,
                    ROUND(MIN(precio)::numeric,2)           AS precio_min,
                    ROUND(MAX(precio)::numeric,2)           AS precio_max,
                    ROUND(AVG(precio)::numeric,2)           AS precio_med,
                    ROUND(MAX(precio)-MIN(precio),2)        AS rango_precio,
                    MAX(completadas)                        AS ordenes,
                    ROUND(AVG(tasa_exito)::numeric,1)       AS tasa_exito,
                    ROUND(AVG(posicion)::numeric,1)         AS pos_med,
                    COUNT(DISTINCT DATE_TRUNC('hour', snapshot_timestamp)) AS horas_activas,
                    COUNT(*)                                AS apariciones
                FROM snapshots_detalle
                WHERE snapshot_timestamp >= NOW() - INTERVAL '7 days'
                  AND es_merchant = true
                GROUP BY anunciante, tipo
                HAVING COUNT(*) >= 100
                ORDER BY apariciones DESC
                LIMIT 20
            """)
            rows = cur.fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/inteligencia/precio_patron")
def api_intel_precio_patron():
    """Precio ponderado promedio y spread por hora y día de semana"""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    hora,
                    TO_CHAR(timestamp, 'Day') AS dia_semana,
                    EXTRACT(DOW FROM timestamp) AS dow,
                    ROUND(AVG(precio_pond_tab_compra)::numeric,2) AS precio_compra,
                    ROUND(AVG(precio_pond_tab_venta)::numeric,2)  AS precio_venta,
                    ROUND(AVG(spread_pond_pct)::numeric,3)        AS spread,
                    COUNT(*)                                       AS muestras
                FROM snapshots
                WHERE timestamp >= NOW() - INTERVAL '7 days'
                GROUP BY hora, dia_semana, dow
                ORDER BY dow, hora
            """)
            rows = cur.fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/inteligencia/fill_por_posicion")
def api_intel_fill_por_posicion():
    """
    TASA DE FILL POR POSICION DEL LIBRO
    Para cada posicion 1-20 (BUY y SELL) calcula cuantos ciclos tuvieron
    una orden completada (delta completadas > 0) y cuanta liquidez se consumio.
    pct_fill = % de ciclos con actividad real confirmada en ese slot.
    Si posicion 3 muestra 12%, 1 de cada 8 snapshots tuvo fill ahi.
    ordenes_por_fill = promedio de ordenes completadas por evento de fill.
    usdt_consumido_med = USDT promedio que desaparece del disponible cuando hay fill.
    Usa esto para decidir en que posicion poner tu anuncio: mayor fill pero
    menor spread (posicion 1) vs menor fill pero mayor spread (posicion 4+).
    """
    dias = int(request.args.get("dias", 7))
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                WITH deltas AS (
                    SELECT
                        posicion,
                        tipo,
                        completadas - LAG(completadas) OVER (
                            PARTITION BY anunciante, tipo
                            ORDER BY snapshot_timestamp
                        ) AS delta_completadas,
                        LAG(disponible) OVER (
                            PARTITION BY anunciante, tipo
                            ORDER BY snapshot_timestamp
                        ) - disponible AS usdt_consumido
                    FROM snapshots_detalle
                    WHERE snapshot_timestamp >= NOW() - (%(dias)s || ' days')::INTERVAL
                )
                SELECT
                    tipo,
                    posicion,
                    COUNT(*) AS observaciones,
                    COUNT(CASE WHEN delta_completadas > 0 THEN 1 END) AS ciclos_con_fill,
                    ROUND(100.0 * COUNT(CASE WHEN delta_completadas > 0 THEN 1 END)
                          / NULLIF(COUNT(*), 0), 1) AS pct_fill,
                    ROUND(AVG(CASE WHEN delta_completadas > 0
                                   THEN delta_completadas END)::numeric, 1) AS ordenes_por_fill,
                    ROUND(AVG(CASE WHEN usdt_consumido > 0
                                   THEN usdt_consumido END)::numeric, 0) AS usdt_consumido_med
                FROM deltas
                WHERE delta_completadas IS NOT NULL
                GROUP BY tipo, posicion
                ORDER BY tipo, posicion
            """, {"dias": dias})
            rows = cur.fetchall()
    result = []
    for r in rows:
        d = dict(r)
        for k, v in d.items():
            if hasattr(v, "__float__"): d[k] = float(v)
        result.append(d)
    return jsonify({
        "descripcion": (
            "Tasa de fill real por posicion del libro. "
            "pct_fill = % ciclos con fill confirmado (delta completadas > 0). "
            "usdt_consumido_med = liquidez absorbida por evento."
        ),
        "dias_analizados": dias,
        "datos": result,
    })


@app.route("/api/inteligencia/profundidad")
def api_intel_profundidad():
    """
    PROFUNDIDAD DEL LIBRO Y CONSUMO REAL DE LIQUIDEZ
    Para cada posicion: liquidez disponible promedio vs liquidez consumida.
    liq_disponible_acum = USDT acumulados desde posicion 1 hasta aqui.
    Si en posicion 6 el acum es 18.000 USDT, hay 18.000 USDT con prioridad
    sobre ti si pones en posicion 7.
    consumo_acum = cuanto absorbe el mercado por ciclo (2 min) hasta esa prof.
    ratio_consumo = consumo_med / liq_disponible_med: que % de su capital
    rota esa posicion por ciclo. Alta rotacion = alta demanda en ese nivel.
    pct_ciclos_activos = % ciclos donde esa posicion tuvo cualquier consumo
    de disponible (mas amplio que fill confirmado).
    """
    dias = int(request.args.get("dias", 7))
    tipo = request.args.get("tipo", "BUY").upper()
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                WITH base AS (
                    SELECT
                        posicion,
                        disponible,
                        LAG(disponible) OVER (
                            PARTITION BY anunciante, tipo
                            ORDER BY snapshot_timestamp
                        ) - disponible AS consumo
                    FROM snapshots_detalle
                    WHERE snapshot_timestamp >= NOW() - (%(dias)s || ' days')::INTERVAL
                      AND tipo = %(tipo)s
                ),
                por_posicion AS (
                    SELECT
                        posicion,
                        ROUND(AVG(disponible)::numeric, 0) AS liq_disponible_med,
                        ROUND(AVG(CASE WHEN consumo > 0 THEN consumo END)::numeric, 0) AS consumo_med,
                        ROUND(100.0 * COUNT(CASE WHEN consumo > 0 THEN 1 END)
                              / NULLIF(COUNT(*), 0), 1) AS pct_ciclos_activos,
                        COUNT(*) AS observaciones
                    FROM base
                    GROUP BY posicion
                )
                SELECT
                    posicion,
                    liq_disponible_med,
                    SUM(liq_disponible_med) OVER (ORDER BY posicion) AS liq_disponible_acum,
                    consumo_med,
                    SUM(COALESCE(consumo_med, 0)) OVER (ORDER BY posicion) AS consumo_acum,
                    ROUND(100.0 * consumo_med / NULLIF(liq_disponible_med, 0), 2) AS ratio_consumo,
                    pct_ciclos_activos,
                    observaciones
                FROM por_posicion
                ORDER BY posicion
            """, {"dias": dias, "tipo": tipo})
            rows = cur.fetchall()
    result = []
    for r in rows:
        d = dict(r)
        for k, v in d.items():
            if hasattr(v, "__float__"): d[k] = float(v)
        result.append(d)
    return jsonify({
        "descripcion": (
            "Profundidad del libro y consumo real por posicion. "
            "liq_disponible_acum = USDT con prioridad sobre la siguiente posicion. "
            "ratio_consumo = % del capital de ese slot que rota por ciclo de 2 min."
        ),
        "tipo": tipo,
        "dias_analizados": dias,
        "datos": result,
    })


@app.route("/api/inteligencia/precio_vs_fill")
def api_intel_precio_vs_fill():
    """
    PRECIO RELATIVO VS TASA DE FILL
    Cuanto precio sacrifico por estar fuera de la cabeza del libro,
    y como afecta eso a que me llenen?
    precio_relativo_pct = diferencia % vs el mejor precio del libro en ese
    snapshot. En BUY: cuanto menos paga esa posicion vs el lider. En SELL:
    cuanto mas cobra vs el lider.
    pct_fill = % ciclos con fill confirmado en esa posicion.
    eficiencia = pct_fill / |precio_relativo_pct|. Cuanto fill obtienes por
    cada 0.1% de precio que resignas. Posicion con mayor eficiencia = mejor
    tradeoff precio vs probabilidad de llenarse.
    """
    dias = int(request.args.get("dias", 7))
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                WITH con_lider AS (
                    SELECT
                        d.posicion,
                        d.tipo,
                        d.precio,
                        d.completadas - LAG(d.completadas) OVER (
                            PARTITION BY d.anunciante, d.tipo
                            ORDER BY d.snapshot_timestamp
                        ) AS delta_completadas,
                        FIRST_VALUE(d.precio) OVER (
                            PARTITION BY d.tipo, d.snapshot_timestamp
                            ORDER BY d.posicion
                        ) AS precio_lider
                    FROM snapshots_detalle d
                    WHERE d.snapshot_timestamp >= NOW() - (%(dias)s || ' days')::INTERVAL
                ),
                relativo AS (
                    SELECT
                        posicion,
                        tipo,
                        precio - precio_lider AS diff_precio,
                        CASE WHEN precio_lider > 0
                             THEN (precio - precio_lider) / precio_lider * 100
                             ELSE NULL END AS diff_pct,
                        CASE WHEN delta_completadas > 0 THEN 1 ELSE 0 END AS tuvo_fill
                    FROM con_lider
                    WHERE delta_completadas IS NOT NULL
                )
                SELECT
                    tipo,
                    posicion,
                    COUNT(*) AS observaciones,
                    ROUND(AVG(diff_precio)::numeric, 2) AS precio_relativo_med,
                    ROUND(AVG(diff_pct)::numeric, 4) AS precio_relativo_pct,
                    ROUND(100.0 * SUM(tuvo_fill) / NULLIF(COUNT(*), 0), 1) AS pct_fill,
                    ROUND(
                        (100.0 * SUM(tuvo_fill) / NULLIF(COUNT(*), 0))
                        / NULLIF(ABS(AVG(diff_pct)), 0),
                    2) AS eficiencia
                FROM relativo
                GROUP BY tipo, posicion
                ORDER BY tipo, posicion
            """, {"dias": dias})
            rows = cur.fetchall()
    result = []
    for r in rows:
        d = dict(r)
        for k, v in d.items():
            if hasattr(v, "__float__"): d[k] = float(v)
        result.append(d)
    return jsonify({
        "descripcion": (
            "Precio relativo vs tasa de fill por posicion. "
            "precio_relativo_pct = diferencia % vs mejor precio del libro en ese snapshot. "
            "eficiencia = pct_fill / |precio_relativo_pct|: fill por cada % de precio resignado."
        ),
        "dias_analizados": dias,
        "datos": result,
    })


@app.route("/api/inteligencia/rotacion")
def api_intel_rotacion():
    """Caudal de llenado (USDT/min) por posicion y por banda de % vs lider.
    Base para estimar tiempo de llenado y rotaciones/dia.
    Params: ?dias=N&tipo=BUY|SELL"""
    try:
        dias = int(request.args.get("dias", 7))
    except (ValueError, TypeError):
        dias = 7
    tipo = (request.args.get("tipo", "BUY") or "BUY").upper()
    if tipo not in ("BUY", "SELL"):
        tipo = "BUY"
    with config_lock:
        intervalo = config["INTERVALO_MIN"]
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                WITH cl AS (
                    SELECT d.posicion, d.precio,
                        FIRST_VALUE(d.precio) OVER (
                            PARTITION BY d.tipo, d.snapshot_timestamp
                            ORDER BY d.posicion
                        ) AS precio_lider,
                        LAG(d.disponible) OVER (
                            PARTITION BY d.anunciante, d.tipo
                            ORDER BY d.snapshot_timestamp
                        ) - d.disponible AS consumo
                    FROM snapshots_detalle d
                    WHERE d.snapshot_timestamp >= NOW() - (%(dias)s || ' days')::INTERVAL
                      AND d.tipo = %(tipo)s
                )
                SELECT posicion,
                    COUNT(*) AS obs,
                    ROUND(AVG(GREATEST(consumo,0))::numeric,1) AS caudal_ciclo,
                    ROUND((100.0*COUNT(CASE WHEN consumo>0 THEN 1 END)/NULLIF(COUNT(*),0))::numeric,1) AS pct_presencia,
                    ROUND(AVG(CASE WHEN precio_lider>0 THEN ABS((precio-precio_lider)/precio_lider*100) END)::numeric,3) AS distancia_med
                FROM cl WHERE consumo IS NOT NULL
                GROUP BY posicion ORDER BY posicion
            """, {"dias": dias, "tipo": tipo})
            por_pos = [dict(r) for r in cur.fetchall()]

            cur.execute("""
                WITH cl AS (
                    SELECT d.precio,
                        FIRST_VALUE(d.precio) OVER (
                            PARTITION BY d.tipo, d.snapshot_timestamp
                            ORDER BY d.posicion
                        ) AS precio_lider,
                        LAG(d.disponible) OVER (
                            PARTITION BY d.anunciante, d.tipo
                            ORDER BY d.snapshot_timestamp
                        ) - d.disponible AS consumo
                    FROM snapshots_detalle d
                    WHERE d.snapshot_timestamp >= NOW() - (%(dias)s || ' days')::INTERVAL
                      AND d.tipo = %(tipo)s
                ),
                banda AS (
                    SELECT consumo,
                        LEAST(FLOOR(ABS((precio-precio_lider)/NULLIF(precio_lider,0)*100)/0.1)*0.1, 1.5) AS banda_pct
                    FROM cl WHERE consumo IS NOT NULL AND precio_lider>0
                )
                SELECT banda_pct,
                    COUNT(*) AS obs,
                    ROUND(AVG(GREATEST(consumo,0))::numeric,1) AS caudal_ciclo,
                    ROUND((100.0*COUNT(CASE WHEN consumo>0 THEN 1 END)/NULLIF(COUNT(*),0))::numeric,1) AS pct_presencia
                FROM banda GROUP BY banda_pct ORDER BY banda_pct
            """, {"dias": dias, "tipo": tipo})
            por_precio = [dict(r) for r in cur.fetchall()]

    def floatify(rows):
        out = []
        for r in rows:
            d = dict(r)
            for k, v in d.items():
                if hasattr(v, "__float__"):
                    d[k] = float(v)
            cc = d.get("caudal_ciclo")
            d["caudal_min"] = round(cc / intervalo, 1) if (cc is not None and intervalo) else None
            out.append(d)
        return out

    return jsonify({
        "intervalo_min": intervalo,
        "dias": dias,
        "tipo": tipo,
        "por_posicion": floatify(por_pos),
        "por_precio": floatify(por_precio),
    })


@app.route("/api/inteligencia/ventanas_reales")
def api_intel_ventanas_reales():
    """VENTANAS REALES: % del tiempo con semaforo operable por hora, medido
    sobre las decisiones registradas del asistente (operativa_historial, 7d,
    una decision cada ~5 min). Es la version MEDIDA de las 'ventanas buenas'
    del plan de campana (07-09h / 20-23h): sirve para validarlas o corregirlas."""
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT hora,
                           COUNT(*) AS muestras,
                           COUNT(*) FILTER (WHERE color = 'green')  AS verdes,
                           COUNT(*) FILTER (WHERE color = 'yellow') AS amarillos,
                           COUNT(*) FILTER (WHERE color = 'orange') AS naranjas,
                           ROUND(AVG(spread_neto)::numeric, 3) AS spread_neto_med,
                           ROUND(AVG(ratio)::numeric, 2)       AS rotacion_med,
                           ROUND(AVG(presion)::numeric, 1)     AS presion_med
                    FROM operativa_historial
                    WHERE ts >= NOW() - INTERVAL '7 days'
                    GROUP BY hora ORDER BY hora
                """)
                rows = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f"[ventanas_reales] {e}")
        return jsonify([])
    for r in rows:
        n = int(r["muestras"] or 0)
        ok = int(r["verdes"] or 0) + int(r["amarillos"] or 0)
        r["pct_operable"] = round(ok / n * 100, 1) if n else 0
        r["pct_verde"]    = round(int(r["verdes"] or 0) / n * 100, 1) if n else 0
    return jsonify(rows)


@app.route("/api/perfil_horas")
def api_perfil_horas():
    """Las 24 horas en UNA tabla: spread, flujo, indice, gap sugerido y que tan
    seguido el semaforo dio verde. Reemplaza a las 3 vistas por hora que habia
    antes (Ventanas reales / Horario / Patrones), que miraban lo mismo con
    metodos peores y ventanas de datos mas cortas."""
    filas = []
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM perfil_hora ORDER BY hora")
                base = {int(r["hora"]): dict(r) for r in cur.fetchall()}
                cur.execute("""
                    SELECT hora, COUNT(*) n,
                           COUNT(*) FILTER (WHERE color IN ('green','yellow')) ok
                    FROM operativa_historial
                    WHERE ts >= NOW() - INTERVAL '14 days' GROUP BY 1
                """)
                sem = {int(r["hora"]): r for r in cur.fetchall()}
    except Exception as e:
        print(f"[perfil_horas] {e}")
        return jsonify({"filas": []})
    for h in range(24):
        b = base.get(h)
        if not b:
            continue
        s = sem.get(h)
        pct_ok = round(int(s["ok"]) / int(s["n"]) * 100, 1) if s and int(s["n"]) else None
        filas.append({
            "hora": h,
            "indice": float(b["indice"] or 0),
            "spread_med": float(b["spread_med"] or 0),
            "flujo_ordenes": float(b["flujo_ordenes"] or 0),
            "gap_sugerido": float(b["gap_sugerido"]) if b["gap_sugerido"] else None,
            "pct_operable": pct_ok,
            "muestras_semaforo": int(s["n"]) if s else 0,
        })
    return jsonify({"filas": filas,
                    "nota": "indice = spread x flujo, normalizado a 100. El flujo varia 90x entre horas "
                            "y el spread solo 3x, por eso el flujo manda. %operable sale del semaforo real."})


@app.route("/api/plan_hoy")
def api_plan_hoy():
    """PLAN DE HOY — la sintesis: que hacer AHORA, en una pantalla.
    Junta el perfil horario medido, el estado del libro, la curva de llenado
    y el progreso de campana, y lo baja a instrucciones concretas."""
    with config_lock:
        c = dict(config)
    now = datetime.now(SANTIAGO_TZ)
    hora = now.hour
    perfil, prox = {}, []
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM perfil_hora ORDER BY hora")
                filas = [dict(r) for r in cur.fetchall()]
        por_hora = {int(f["hora"]): f for f in filas}
        perfil = por_hora.get(hora, {})
        # las proximas 6 horas, para saber si conviene quedarse o volver despues
        for k in range(6):
            h = (hora + k) % 24
            f = por_hora.get(h)
            if f:
                prox.append({"hora": h, "indice": float(f["indice"] or 0),
                             "gap_sugerido": float(f["gap_sugerido"] or 0) if f["gap_sugerido"] else None})
    except Exception as e:
        print(f"[plan_hoy] {e}")
    idx = float(perfil.get("indice") or 0) if perfil else 0
    gap_sug = float(perfil["gap_sugerido"]) if perfil.get("gap_sugerido") else None
    gap_actual = float(c.get("GAP_OBJETIVO_BRUTO", 0) or 0)
    if idx >= 75:   calidad, calidad_txt = "excelente", "de las mejores horas del dia"
    elif idx >= 55: calidad, calidad_txt = "buena", "hora decente para operar"
    elif idx >= 35: calidad, calidad_txt = "floja", "se puede, pero rinde poco"
    else:           calidad, calidad_txt = "mala", "casi no hay flujo: no vale la pena"
    mejor_prox = max(prox, key=lambda p: p["indice"]) if prox else None
    ritmo = float(c.get("RITMO_MEDIDO_ORD_H", 0) or 0)
    pos_obj = int(c.get("MI_POSICION_OBJETIVO", 15) or 15)
    with data_lock:
        snap = dict(ultimo_estado)
    # accion concreta
    acciones = []
    if gap_sug and gap_actual:
        dif = gap_actual - gap_sug
        ga = f"{gap_actual:.2f}".replace(".", ",")
        gs = f"{gap_sug:.2f}".replace(".", ",")
        if abs(dif) >= 0.08:
            acciones.append(f"Ajusta el gap: tenes {ga}% y para esta hora conviene ~{gs}%")
        else:
            acciones.append(f"Tu gap ({ga}%) esta bien para esta hora")
    if idx < 35:
        if mejor_prox and mejor_prox["indice"] >= 55:
            acciones.append(f"Hora floja: si podes, espera a las {mejor_prox['hora']:02d}h (indice {mejor_prox['indice']:.0f})")
        else:
            acciones.append("Hora floja y las proximas tampoco mejoran mucho")
    else:
        # formato es-CL: coma decimal y 1 decimal (si no, "2.339" se lee como 2.339 ordenes)
        ritmo_txt = f"{ritmo:.1f}".replace(".", ",") if ritmo else None
        acciones.append(f"Parate en posicion {pos_obj} o mejor" +
                        (f" · el mercado ahi da ~{ritmo_txt} ordenes/hora por pierna" if ritmo_txt else ""))
    return jsonify({
        "hora": hora, "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
        "calidad": calidad, "calidad_txt": calidad_txt,
        "indice_hora": round(idx, 1),
        "spread_hora_med": round(float(perfil.get("spread_med") or 0), 4) if perfil else None,
        "gap_sugerido": gap_sug, "gap_actual": gap_actual,
        "posicion_objetivo": pos_obj,
        "ritmo_ord_h": ritmo or None,
        "proximas_horas": prox,
        "mejor_proxima": mejor_prox,
        "spread_ahora_pct": snap.get("spread_pond_pct"),
        "estado_libro": snap.get("estado"),
        "acciones": acciones,
        "nota": "indice 0-100: que tan buena es la hora para farmear (spread x flujo, medido). "
                "100 = la mejor hora del dia.",
    })


@app.route("/api/anunciante")
def api_anunciante():
    """FICHA DEL COMPETIDOR: todo lo que sabemos de un anunciante.
    - ordenes/dia REALES (delta del contador oficial de Binance, no estimacion)
    - volumen y ticket estimados por el tracker
    - su gap propio si esta publicado en ambos lados AHORA
    - ganancia bruta estimada = volumen x gap - comision
    Sin ?nombre= devuelve el ranking para elegir a quien mirar."""
    nombre = (request.args.get("nombre") or "").strip()
    with config_lock:
        com_maker = float(config.get("COM_MAKER_PCT", 0.19))
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SET LOCAL statement_timeout = '40s'")
                if not nombre:
                    q = (request.args.get("q") or "").strip()
                    # OJO: ordenes_dia viene del contador POR CUENTA, asi que los
                    # dos lados (BUY/SELL) traen el MISMO numero. Hay que tomar el
                    # MAXIMO por fecha, nunca la suma: sumar duplicaria las ordenes
                    # de todo anunciante que opere en ambos lados.
                    cur.execute("""
                        SELECT anunciante, SUM(ord_dia) ordenes,
                               ROUND(AVG(pos)::numeric,1) pos,
                               COUNT(*) dias, BOOL_OR(merch) merchant
                        FROM (
                            SELECT anunciante, fecha,
                                   MAX(ordenes_dia) ord_dia, AVG(pos_media) pos,
                                   BOOL_OR(es_merchant) merch
                            FROM agregados_anunciante_dia
                            WHERE exchange='binance'
                              AND (%(q)s = '' OR anunciante ILIKE '%%' || %(q)s || '%%')
                            GROUP BY 1,2
                        ) x
                        GROUP BY 1 HAVING SUM(ord_dia) > 0
                        ORDER BY 2 DESC LIMIT 25
                    """, {"q": q})
                    return jsonify({"lista": [dict(r) for r in cur.fetchall()], "q": q})
                # ── ficha ──
                cur.execute("""
                    SELECT fecha, tipo, apariciones, pos_media, pos_min, precio_medio,
                           disp_medio, ordenes_dia, es_merchant
                    FROM agregados_anunciante_dia
                    WHERE exchange='binance' AND LOWER(anunciante)=LOWER(%(n)s)
                    ORDER BY fecha DESC, tipo LIMIT 40
                """, {"n": nombre})
                hist = [dict(r) for r in cur.fetchall()]
                cur.execute("""
                    SELECT COUNT(*) fills, COALESCE(SUM(monto),0) vol,
                           COALESCE(SUM(ordenes),0) ord,
                           ROUND(AVG(monto/NULLIF(ordenes,0))::numeric) ticket,
                           MIN(ts) desde, MAX(ts) hasta
                    FROM fills_estimados
                    WHERE exchange='binance' AND LOWER(anunciante)=LOWER(%(n)s)
                      AND ts >= NOW() - INTERVAL '30 days'
                """, {"n": nombre})
                fl = dict(cur.fetchone() or {})
    except Exception as e:
        print(f"[anunciante] {e}")
        return jsonify({"error": str(e)[:200]}), 500
    if not hist and not fl.get("fills"):
        return jsonify({"encontrado": False, "nombre": nombre})
    # posicion y precio AHORA (libro en vivo) + gap propio si es dual
    with data_lock:
        snap = dict(ultimo_estado)
    vivo = {}
    for key, lado in (("detalle_compra", "venta"), ("detalle_venta", "compra")):
        for row in (snap.get(key) or []):
            if (row.get("anunciante") or "").strip().lower() == nombre.lower():
                vivo[lado] = {"posicion": row.get("posicion"), "precio": row.get("precio"),
                              "disponible": round(float(row.get("disponible") or 0))}
    gap_propio = None
    if "venta" in vivo and "compra" in vivo:
        try:
            pv, pc = float(vivo["venta"]["precio"]), float(vivo["compra"]["precio"])
            if pv > 0 and pc > 0:
                gap_propio = round((pv - pc) / pc * 100, 3)
        except (ValueError, TypeError):
            pass
    # resumen diario (suma los dos lados por fecha)
    por_fecha = {}
    for h in hist:
        d = por_fecha.setdefault(str(h["fecha"]), {"fecha": str(h["fecha"]), "ordenes": 0,
                                                   "pos": [], "precio": [], "stock": 0})
        # MAX y no suma: el contador es por CUENTA, los dos lados traen el mismo
        # numero (ver comentario en la consulta de la lista).
        d["ordenes"] = max(d["ordenes"], int(h["ordenes_dia"] or 0))
        if h["pos_media"]: d["pos"].append(float(h["pos_media"]))
        if h["precio_medio"]: d["precio"].append(float(h["precio_medio"]))
        d["stock"] += float(h["disp_medio"] or 0)
    serie = []
    for d in sorted(por_fecha.values(), key=lambda x: x["fecha"], reverse=True):
        serie.append({"fecha": d["fecha"], "ordenes": d["ordenes"],
                      "pos_media": round(sum(d["pos"]) / len(d["pos"]), 1) if d["pos"] else None,
                      "stock": round(d["stock"])})
    ord_dia = round(sum(s["ordenes"] for s in serie) / len(serie), 1) if serie else None
    vol30 = float(fl.get("vol") or 0)
    # ganancia bruta estimada: solo tiene sentido si sabemos su gap
    gan = None
    if gap_propio and vol30:
        gan = round(vol30 * (gap_propio - com_maker * 2) / 100)
    return jsonify({
        "encontrado": True, "nombre": nombre,
        "merchant": any(h.get("es_merchant") for h in hist),
        "dias_observado": len(serie),
        "ordenes_dia_prom": ord_dia,
        "volumen_30d": round(vol30),
        "ordenes_30d_tracker": int(fl.get("ord") or 0),
        "ticket_medio": float(fl["ticket"]) if fl.get("ticket") else None,
        "en_libro_ahora": bool(vivo), "dual_ahora": len(vivo) == 2,
        "posiciones": vivo, "gap_propio_pct": gap_propio,
        "ganancia_30d_estimada_usdt": gan,
        "serie": serie[:14],
        "nota": ("ordenes_dia sale del contador OFICIAL de Binance (no es estimacion nuestra). "
                 "El volumen y el ticket si son estimados por el tracker. La ganancia asume que "
                 "opera su gap actual en las dos piernas y paga comision maker."),
    })


@app.route("/api/taker_maker")
def api_taker_maker():
    """CRUZAR o ESPERAR: compara, pierna por pierna, si conviene tomar el
    anuncio de otro (taker) o publicar el tuyo y esperar (maker).

    LA MATEMATICA (COL18)
    La comision taker es un MONTO FIJO (0,07 USDT medidos) y la maker es un
    PORCENTAJE (0,19%). Entonces el que conviene depende del TAMANO:
      - costo de cruzar   = medio spread (pagas el precio del otro) + fija/X
      - costo de esperar  = -medio spread (lo GANAS vos) + 0,19%
      - diferencia        = spread + fija/X - 0,19
    => CRUZAR conviene cuando:  spread% < 0,19 - (0,07/X x 100)
    => y el tamano a partir del cual conviene:  X = 7 / (0,19 - spread%)

    Ademas del costo esta el TIEMPO: cruzar llena al instante, esperar tarda
    lo que diga la curva de llenado medida. Por eso, aun empatando en costo,
    cruzar puede convenir (y para la campana, mas ordenes por hora).
    Params: ?usdt=200 (tamano de la orden a evaluar)."""
    with config_lock:
        c = dict(config)
    try:
        usdt = float(request.args.get("usdt", 0)) or float(c.get("FILL_TICKET_DEF", 408))
    except (ValueError, TypeError):
        usdt = float(c.get("FILL_TICKET_DEF", 408))
    usdt = max(5.0, min(100000.0, usdt))
    with data_lock:
        snap = dict(ultimo_estado)
    ask = float(snap.get("mejor_vendedor_tab_compra") or 0)   # mas barato para COMPRAR USDT
    bid = float(snap.get("mejor_comprador_tab_venta") or 0)   # mas alto que PAGAN por tu USDT
    if not ask or not bid:
        return jsonify({"error": "sin datos del libro aun"}), 503
    mid = (ask + bid) / 2
    spread_pct = (ask - bid) / mid * 100
    fija    = float(c.get("COM_TAKER_FIJA_USDT", 0.07))
    mak_pct = float(c.get("COM_MAKER_PCT", 0.19))
    fija_pct = fija / usdt * 100                 # la fija expresada en % de ESTA orden
    ritmo    = float(c.get("RITMO_MEDIDO_ORD_H", 0) or 0)
    espera_min = round(60 / ritmo, 0) if ritmo > 0 else None

    # Umbral: cruzar conviene si el spread es menor que el ahorro de comision
    ahorro_com = mak_pct - fija_pct              # lo que te ahorras en comision al cruzar
    ventaja    = ahorro_com - spread_pct         # >0 => cruzar sale mas barato
    tam_equilibrio = round(fija * 100 / (mak_pct - spread_pct)) if spread_pct < mak_pct else None

    def pierna(nombre, tab_cruzar, tab_publicar, precio_cruzar, precio_publicar):
        """Costo de cada opcion para una pierna, en % sobre el mid."""
        costo_cruzar  = round(abs(precio_cruzar - mid) / mid * 100 + fija_pct, 4)
        costo_esperar = round(-abs(mid - precio_publicar) / mid * 100 + mak_pct, 4)
        conviene = "cruzar" if costo_cruzar < costo_esperar else "esperar"
        return {
            "pierna": nombre,
            "cruzar": {
                "accion": f"tomas un anuncio en {tab_cruzar}",
                "precio": round(precio_cruzar, 2),
                "comision_usdt": round(fija, 3),
                "comision_pct": round(fija_pct, 4),
                "costo_total_pct": costo_cruzar,
                "demora_min": 0,
            },
            "esperar": {
                "accion": f"publicas tu anuncio en {tab_publicar}",
                "precio": round(precio_publicar, 2),
                "comision_usdt": round(usdt * mak_pct / 100, 3),
                "comision_pct": mak_pct,
                "costo_total_pct": costo_esperar,
                "demora_min": espera_min,
            },
            "conviene": conviene,
            "diferencia_pct": round(costo_esperar - costo_cruzar, 4),
        }

    # Al publicar (maker) el supuesto es que te llenas al precio del lider del
    # lado contrario: es el escenario competitivo realista.
    piernas = [
        pierna("comprar USDT", "tab Compra", "tab Venta",  ask, bid),
        pierna("vender USDT",  "tab Venta",  "tab Compra", bid, ask),
    ]

    # Los 4 caminos de la vuelta completa (comprar + vender)
    pc, pv = piernas[0], piernas[1]
    caminos = []
    for nom, desc, kc, kv in (
        ("Dual maker",  "publicas los dos anuncios y esperas (farming clasico)", "esperar", "esperar"),
        ("Cruzar compra", "compras al instante, vendes publicando",              "cruzar",  "esperar"),
        ("Cruzar venta",  "compras publicando, vendes al instante",              "esperar", "cruzar"),
        ("Doble cruce",   "las dos al instante (maxima velocidad)",              "cruzar",  "cruzar"),
    ):
        costo = pc[kc]["costo_total_pct"] + pv[kv]["costo_total_pct"]
        dem   = [pc[kc]["demora_min"], pv[kv]["demora_min"]]
        dem_tot = None if any(d is None for d in dem) else sum(dem)
        caminos.append({
            "nombre": nom, "detalle": desc,
            "compra": kc, "venta": kv,
            "costo_vuelta_pct": round(costo, 4),
            "demora_estimada_min": dem_tot,
            "ordenes_h_teoricas": round(60 / dem_tot, 1) if dem_tot else None,
        })
    caminos.sort(key=lambda x: x["costo_vuelta_pct"])
    barato = caminos[0]

    return jsonify({
        "usdt_evaluado": round(usdt),
        "libro": {"ask": round(ask, 2), "bid": round(bid, 2), "mid": round(mid, 2),
                  "spread_pct": round(spread_pct, 4)},
        "comisiones": {"taker_fija_usdt": fija, "taker_pct_en_esta_orden": round(fija_pct, 4),
                       "maker_pct": mak_pct},
        "umbral": {
            "spread_limite_pct": round(ahorro_com, 4),
            "ventaja_cruzar_pct": round(ventaja, 4),
            "tamano_equilibrio_usdt": tam_equilibrio,
            "veredicto": ("cruzar sale mas barato que publicar" if ventaja > 0
                          else "publicar sale mas barato que cruzar"),
        },
        "piernas": piernas,
        "caminos": caminos,
        "mas_barato": barato["nombre"],
        "espera_maker_min": espera_min,
        "nota": ("Costos en % sobre el precio medio. 'Esperar' asume que te llenas al precio "
                 "del lider (escenario competitivo) y que la espera es la de la curva medida; "
                 "si no te llenas, el costo real es mayor. Las ordenes taker TAMBIEN suman al "
                 "contador de Merchant."),
    })


@app.route("/api/calibracion")
def api_calibracion():
    """CALIBRACION: mis ordenes REALES (importadas del CSV de Binance) contra
    lo que el monitor infirio mirando el libro desde afuera.

    Solo se evaluan las ordenes 'maker' completadas: las 'taker' son invisibles
    para el monitor (cuando tomas el anuncio de otro no estas publicado en el
    libro), asi que contarlas como no detectadas seria injusto y ensuciaria la
    metrica.

    El total se compara por PERIODO, no orden por orden: el monitor agrupa
    varias ordenes en un mismo fill confirmado, asi que sumar 'fills que
    matchean' contaria dos veces. Ademas devuelve la posicion en la que estaba
    tu anuncio en ese momento -> permite atribuir cada orden a la estrategia
    que estabas probando, sin que anotes nada a mano."""
    try:
        dias = max(1, min(90, int(request.args.get("dias", 30))))
    except (ValueError, TypeError):
        dias = 30
    with config_lock:
        nick = str(config.get("MI_NICKNAME") or "").strip()
    if not nick:
        return jsonify({"configurado": False, "nota": "defini tu nickname en el panel Estrategia"})
    resumen, filas = {}, []
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT to_regclass('public.mis_ordenes_reales')")
                if cur.fetchone()["to_regclass"] is None:
                    return jsonify({"configurado": True, "sin_datos": True,
                                    "nota": "todavia no importaste el CSV (scripts/importar_mis_ordenes.bat)"})
                # Totales del periodo: real (maker completada) vs monitor
                cur.execute("""
                    SELECT COUNT(*) AS n, COALESCE(SUM(usdt),0) AS usdt,
                           COALESCE(SUM(comision),0) AS comision
                    FROM mis_ordenes_reales
                    WHERE rol='maker' AND estado='completada'
                      AND ts >= NOW() - (%(d)s || ' days')::INTERVAL
                """, {"d": dias})
                r = cur.fetchone()
                n_real, usdt_real = int(r["n"] or 0), float(r["usdt"] or 0)
                cur.execute("""
                    SELECT COUNT(*) AS n, COALESCE(SUM(monto),0) AS usdt,
                           COALESCE(SUM(ordenes),0) AS ordenes
                    FROM fills_estimados
                    WHERE exchange='binance' AND LOWER(anunciante)=LOWER(%(n)s)
                      AND ts >= NOW() - (%(d)s || ' days')::INTERVAL
                """, {"n": nick, "d": dias})
                r = cur.fetchone()
                usdt_mon = float(r["usdt"] or 0)
                ord_mon  = int(r["ordenes"] or 0)
                # Cuantas ordenes reales tuvieron actividad detectada cerca
                cur.execute("""
                    SELECT o.orden_id, o.ts, o.lado, o.usdt, o.precio, o.contraparte,
                           f.ts AS fill_ts, f.monto AS fill_monto, f.metodo,
                           d.pos, d.precio AS precio_libro
                    FROM mis_ordenes_reales o
                    LEFT JOIN LATERAL (
                        SELECT ts, monto, metodo FROM fills_estimados x
                        WHERE x.exchange='binance' AND LOWER(x.anunciante)=LOWER(%(n)s)
                          AND x.ts BETWEEN o.ts AND o.ts + INTERVAL '25 minutes'
                        ORDER BY x.ts LIMIT 1
                    ) f ON TRUE
                    LEFT JOIN LATERAL (
                        SELECT MIN(posicion) AS pos, MIN(precio) AS precio
                        FROM snapshots_detalle y
                        WHERE LOWER(y.anunciante)=LOWER(%(n)s)
                          AND y.snapshot_timestamp BETWEEN o.ts - INTERVAL '4 minutes' AND o.ts
                    ) d ON TRUE
                    WHERE o.rol='maker' AND o.estado='completada'
                      AND o.ts >= NOW() - (%(d)s || ' days')::INTERVAL
                    ORDER BY o.ts DESC LIMIT 100
                """, {"n": nick, "d": dias})
                for x in cur.fetchall():
                    lat = None
                    if x["fill_ts"] and x["ts"]:
                        lat = round((x["fill_ts"] - x["ts"]).total_seconds() / 60, 1)
                    filas.append({
                        "orden_id": x["orden_id"], "ts": str(x["ts"]), "lado": x["lado"],
                        "usdt": float(x["usdt"] or 0), "precio": float(x["precio"] or 0),
                        "contraparte": x["contraparte"],
                        "detectada": bool(x["fill_ts"]),
                        "latencia_min": lat,
                        "metodo": x["metodo"],
                        "posicion": int(x["pos"]) if x["pos"] is not None else None,
                    })
    except Exception as e:
        print(f"[calibracion] {e}")
        return jsonify({"configurado": True, "error": str(e)[:200]})
    detectadas = sum(1 for f in filas if f["detectada"])
    lats = [f["latencia_min"] for f in filas if f["latencia_min"] is not None]
    resumen = {
        "dias": dias,
        "ordenes_maker_reales": n_real,
        "ordenes_detectadas": detectadas,
        "tasa_deteccion_pct": round(detectadas / n_real * 100, 1) if n_real else None,
        "usdt_real": round(usdt_real, 2),
        "usdt_monitor": round(usdt_mon, 2),
        "error_usdt": round(usdt_mon - usdt_real, 2),
        "error_pct": round((usdt_mon - usdt_real) / usdt_real * 100, 1) if usdt_real else None,
        "ordenes_monitor": ord_mon,
        "latencia_media_min": round(sum(lats) / len(lats), 1) if lats else None,
    }
    return jsonify({
        "configurado": True, "nick": nick, "resumen": resumen, "ordenes": filas,
        "nota": ("Solo ordenes maker completadas (las taker el monitor no puede verlas). "
                 "error_pct > 0 = el monitor sobrestima; < 0 = subestima. "
                 "La posicion es la que tenia tu anuncio al momento de la orden: sirve para "
                 "comparar estrategias sin anotar nada a mano."),
    })


@app.route("/api/inteligencia/curva_llenado")
def api_intel_curva_llenado():
    """CURVA DE LLENADO: ordenes por hora segun la POSICION en el libro.

    Metodo (COL17):
    - Solo fills 'directo' (caida de stock OBSERVADA). Los 'enmascarado' son
      estimaciones nuestras: meterlos aca contaminaria el modelo con el propio
      error del estimador.
    - No se cuentan fills sueltos: se divide por la EXPOSICION (cuantas
      horas-anunciante hubo paradas en cada rango). Contar sin normalizar
      favoreceria los rangos donde simplemente hay mas gente parada.
    - Se informa intervalo de confianza (Poisson) porque un rango con pocos
      eventos no permite concluir nada.
    Resultado: 'si me paro en la posicion N, cuantas ordenes/hora espero'.
    Es el numero que reemplaza al ORDENES_H_MAX puesto a mano."""
    try:
        dias = max(1, min(14, int(request.args.get("dias", 7))))
    except (ValueError, TypeError):
        dias = 7
    with config_lock:
        intervalo = float(config.get("INTERVALO_MIN", 2))
    BIN = """CASE WHEN pos<=3 THEN '01-03' WHEN pos<=7 THEN '04-07'
                  WHEN pos<=12 THEN '08-12' WHEN pos<=20 THEN '13-20'
                  WHEN pos<=30 THEN '21-30' WHEN pos<=50 THEN '31-50'
                  ELSE '51-80' END"""
    datos = {}
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SET LOCAL statement_timeout = '40s'")
                # 1) EXPOSICION: cada aparicion de un anunciante en un ciclo
                cur.execute(f"""
                    SELECT {BIN} AS bin, COUNT(*) AS obs,
                           COUNT(DISTINCT anunciante) AS anunciantes,
                           ROUND(AVG(precio)::numeric, 2) AS precio_medio
                    FROM (
                        SELECT snapshot_timestamp, tipo, anunciante,
                               MIN(posicion) AS pos, MIN(precio) AS precio
                        FROM snapshots_detalle
                        WHERE snapshot_timestamp >= NOW() - (%(d)s || ' days')::INTERVAL
                        GROUP BY 1,2,3
                    ) d GROUP BY 1
                """, {"d": dias})
                for r in cur.fetchall():
                    datos[r["bin"]] = {"rango": r["bin"],
                                       "horas_exposicion": round(float(r["obs"]) * intervalo / 60, 1),
                                       "anunciantes": int(r["anunciantes"]),
                                       "ordenes": 0, "eventos": 0, "volumen": 0}
                # 2) EVENTOS observados en ese mismo rango de posicion.
                #    El JOIN es exacto: el colector escribe el fill con el mismo
                #    timestamp del snapshot que lo detecto.
                cur.execute(f"""
                    SELECT {BIN} AS bin, COUNT(*) AS eventos,
                           COALESCE(SUM(f.ordenes),0) AS ordenes,
                           ROUND(COALESCE(SUM(f.monto),0)) AS volumen
                    FROM fills_estimados f
                    JOIN (
                        SELECT snapshot_timestamp, tipo, anunciante, MIN(posicion) AS pos
                        FROM snapshots_detalle
                        WHERE snapshot_timestamp >= NOW() - (%(d)s || ' days')::INTERVAL
                        GROUP BY 1,2,3
                    ) d
                      ON d.anunciante = f.anunciante AND d.tipo = f.tipo
                     AND d.snapshot_timestamp = f.ts
                    WHERE f.exchange = 'binance' AND f.metodo = 'directo'
                      AND f.ts >= NOW() - (%(d)s || ' days')::INTERVAL
                    GROUP BY 1
                """, {"d": dias})
                for r in cur.fetchall():
                    if r["bin"] in datos:
                        datos[r["bin"]].update({"eventos": int(r["eventos"]),
                                                "ordenes": int(r["ordenes"]),
                                                "volumen": int(r["volumen"])})
    except Exception as e:
        print(f"[curva_llenado] {e}")
        return jsonify({"filas": [], "error": "consulta pesada, reintentar"})
    filas = []
    for bin_ in sorted(datos):
        d = datos[bin_]
        h, n = d["horas_exposicion"], d["ordenes"]
        tasa = (n / h) if h > 0 else None
        # IC 95% Poisson sobre el conteo de ordenes (aprox normal)
        ic = (1.96 * (n ** 0.5) / h) if (h > 0 and n > 0) else None
        d["ordenes_hora"]  = round(tasa, 3) if tasa is not None else None
        d["ic95"]          = round(ic, 3) if ic is not None else None
        d["confiable"]     = bool(n >= 20)
        d["min_por_orden"] = round(60 / tasa, 1) if tasa else None
        filas.append(d)
    return jsonify({
        "dias": dias, "filas": filas,
        "nota": ("ordenes/hora POR ANUNCIANTE parado en ese rango, medido solo con fills "
                 "observados. min_por_orden = cuanto tardarias en promedio en llenar una. "
                 "Los rangos con menos de 20 ordenes no son concluyentes (confiable=false)."),
    })


@app.route("/api/inteligencia/farmers")
def api_intel_farmers():
    """RADAR DE FARMERS: anunciantes con MUCHAS ordenes chicas — los que ya
    corren la misma campana de farming que nosotros. Ritmo y ticket salen de
    fills_estimados (7d); del libro EN VIVO sale a que precio/posicion estan
    parados ahora y su gap propio si estan publicados en ambos lados (dual).
    Para copiarles la tactica: gap y posicion de los que ya ganaron la carrera."""
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT anunciante,
                           SUM(ordenes)               AS ordenes_7d,
                           ROUND(SUM(monto)::numeric) AS vol_7d,
                           ROUND((SUM(monto) / NULLIF(SUM(ordenes), 0))::numeric) AS ticket_med,
                           COUNT(DISTINCT DATE(ts))   AS dias_activo
                    FROM fills_estimados
                    WHERE exchange = 'binance' AND ts >= NOW() - INTERVAL '7 days'
                      AND anunciante IS NOT NULL AND anunciante <> ''
                    GROUP BY anunciante
                    HAVING SUM(ordenes) >= 30
                       AND (SUM(monto) / NULLIF(SUM(ordenes), 0)) <= 600
                    ORDER BY SUM(ordenes) DESC
                    LIMIT 15
                """)
                rows = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f"[farmers] {e}")
        return jsonify([])
    with data_lock:
        snap = dict(ultimo_estado)
    # indice del libro vivo: en detalle_compra estan sus anuncios de VENTA
    # (los que te venden USDT) y en detalle_venta sus anuncios de COMPRA
    idx = {}
    for key, lado in (("detalle_compra", "venta"), ("detalle_venta", "compra")):
        for row in (snap.get(key) or []):
            nom = (row.get("anunciante") or "").strip().lower()
            if nom:
                idx.setdefault(nom, {})[lado] = row
    for r in rows:
        info = idx.get((r["anunciante"] or "").strip().lower(), {})
        v, cmp_ = info.get("venta"), info.get("compra")
        r["pos_venta"]      = v.get("posicion")   if v else None
        r["precio_venta"]   = v.get("precio")     if v else None
        r["pos_compra"]     = cmp_.get("posicion") if cmp_ else None
        r["precio_compra"]  = cmp_.get("precio")   if cmp_ else None
        r["dual"] = bool(v and cmp_)
        gap = None
        if v and cmp_:
            try:
                pv, pc = float(v.get("precio") or 0), float(cmp_.get("precio") or 0)
                if pv > 0 and pc > 0:
                    gap = round((pv - pc) / pc * 100, 3)
            except (ValueError, TypeError):
                pass
        r["gap_propio_pct"] = gap
        r["ordenes_dia"] = round(float(r["ordenes_7d"] or 0) / 7.0, 1)
    return jsonify(rows)


@app.route("/api/bybit/estado")
def api_bybit_estado():
    with data_lock:
        return jsonify(dict(ultimo_estado_bybit) if ultimo_estado_bybit else {})


@app.route("/api/cross")
def api_cross():
    """Brecha cruzada Binance <-> Bybit usando precios lider."""
    with data_lock:
        b = dict(ultimo_estado) if ultimo_estado else {}
        y = dict(ultimo_estado_bybit) if ultimo_estado_bybit else {}
    def g(d, k):
        v = d.get(k)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None
    bin_buy  = g(b, "mejor_vendedor_tab_compra")   # comprar USDT en Binance (mas barato)
    bin_sell = g(b, "mejor_comprador_tab_venta")   # vender USDT en Binance (mas alto)
    byb_buy  = g(y, "mejor_vendedor_tab_compra")
    byb_sell = g(y, "mejor_comprador_tab_venta")
    def pct(compra, venta):
        return round((venta - compra) / compra * 100, 4) if (compra and venta) else None
    return jsonify({
        "binance": {"comprar_usdt": bin_buy, "vender_usdt": bin_sell, "timestamp": b.get("timestamp")},
        "bybit":   {"comprar_usdt": byb_buy, "vender_usdt": byb_sell, "timestamp": y.get("timestamp")},
        "comprar_binance_vender_bybit_pct": pct(bin_buy, byb_sell),
        "comprar_bybit_vender_binance_pct": pct(byb_buy, bin_sell),
        "nota": "Bruto, sin comisiones P2P ni costo de transferir USDT entre exchanges (red/retiro).",
    })


@app.route("/api/storage")
def api_storage():
    LIMITE_MB = 500.0   # volumen del plan Railway (ajustar si cambia)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_database_size(current_database())")
            size = cur.fetchone()[0]
            tablas = {}
            for t in ("snapshots", "snapshots_detalle", "snapshots_detalle_bybit"):
                try:
                    cur.execute("SELECT pg_total_relation_size(%s)", (t,))
                    tablas[t] = round(cur.fetchone()[0] / 1048576.0, 1)
                except Exception:
                    tablas[t] = None
    usado = size / 1048576.0
    return jsonify({
        "usado_mb":  round(usado, 1),
        "limite_mb": LIMITE_MB,
        "libre_mb":  round(LIMITE_MB - usado, 1),
        "pct":       round(usado / LIMITE_MB * 100, 1),
        "tablas_mb": tablas,
    })


def _volumen_tabla(cur, tabla, params):
    """Estima volumen operado (USDT) desde la caida de 'disponible' por anunciante.
    Limpia el ruido de reposicion: si el anunciante CAMBIO el precio Y la caida es
    grande (>3000), es una edicion del aviso (no una venta) -> no cuenta. El resto
    cuenta, capeado a 3000 por paso."""
    cur.execute(f"""
        WITH cons AS (
            SELECT tipo, snapshot_timestamp AS t,
                CASE
                  WHEN (precio <> LAG(precio) OVER w)
                       AND (LAG(disponible) OVER w - disponible) > 3000 THEN 0
                  ELSE LEAST(GREATEST(0, LAG(disponible) OVER w - disponible), 3000)
                END AS c
            FROM {tabla}
            WHERE snapshot_timestamp >= %(h48)s
            WINDOW w AS (PARTITION BY anunciante, tipo ORDER BY snapshot_timestamp)
        )
        SELECT tipo,
            COALESCE(SUM(c) FILTER (WHERE t >= %(hoy0)s), 0) AS hoy,
            COALESCE(SUM(c) FILTER (WHERE t >= %(h1)s), 0)   AS hora,
            COALESCE(SUM(c) FILTER (WHERE t >= %(h2)s AND t < %(h1)s), 0) AS p1,
            COALESCE(SUM(c) FILTER (WHERE t >= %(h4)s), 0)   AS u4,
            COALESCE(SUM(c) FILTER (WHERE t >= %(h8)s AND t < %(h4)s), 0)  AS p4,
            COALESCE(SUM(c) FILTER (WHERE t >= %(h24)s), 0)  AS u24,
            COALESCE(SUM(c) FILTER (WHERE t >= %(h48)s AND t < %(h24)s), 0) AS p24
        FROM cons GROUP BY tipo
    """, params)
    rows = {r["tipo"]: r for r in cur.fetchall()}
    def g(k):
        return float(rows.get("BUY", {}).get(k, 0) or 0) + float(rows.get("SELL", {}).get(k, 0) or 0)
    buy_hoy = float(rows.get("BUY", {}).get("hoy", 0) or 0)
    sell_hoy = float(rows.get("SELL", {}).get("hoy", 0) or 0)
    tot_hoy = buy_hoy + sell_hoy
    u1, p1 = g("hora"), g("p1")
    u4, p4, u24, p24 = g("u4"), g("p4"), g("u24"), g("p24")
    def chg(u, p): return round((u - p) / p * 100, 1) if p else None
    return {
        "hoy": round(tot_hoy), "hora": round(u1), "cambio_1h_pct": chg(u1, p1),
        "presion_compra_pct": round(buy_hoy / tot_hoy * 100, 1) if tot_hoy else 50.0,
        "vol_4h": round(u4), "cambio_4h_pct": chg(u4, p4),
        "vol_24h": round(u24), "cambio_24h_pct": chg(u24, p24),
    }


@app.route("/api/volumen")
def api_volumen():
    """Volumen estimado por exchange, SEPARADO (Binance / Bybit). No se mezclan."""
    now = datetime.now(SANTIAGO_TZ)
    def f(dt): return dt.strftime("%Y-%m-%d %H:%M:%S")
    params = {
        "hoy0": f(now.replace(hour=0, minute=0, second=0, microsecond=0)),
        "h1": f(now - timedelta(hours=1)), "h2": f(now - timedelta(hours=2)),
        "h4": f(now - timedelta(hours=4)), "h8": f(now - timedelta(hours=8)),
        "h24": f(now - timedelta(hours=24)), "h48": f(now - timedelta(hours=48)),
    }
    binance = None; bybit = None
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            try:
                binance = _volumen_tabla(cur, "snapshots_detalle", params)
            except Exception as e:
                print("[volumen binance]", e)
            try:
                bybit = _volumen_tabla(cur, "snapshots_detalle_bybit", params)
            except Exception as e:
                print("[volumen bybit]", e)
    out = {"binance": binance, "bybit": bybit}
    if binance:
        out.update({
            "hoy": {"total": binance["hoy"]}, "hora": {"total": binance["hora"]},
            "cambio_1h_pct": binance["cambio_1h_pct"], "presion_compra_pct": binance["presion_compra_pct"],
            "vol_4h": binance["vol_4h"], "cambio_4h_pct": binance["cambio_4h_pct"],
            "vol_24h": binance["vol_24h"], "cambio_24h_pct": binance["cambio_24h_pct"],
        })
    return jsonify(out)


def _volumen_v2_exchange(cur, exchange, params):
    """Mismas ventanas que /api/volumen pero sumando fills_estimados
    (fills confirmados/estimados en vez de caidas crudas)."""
    cur.execute("""
        SELECT tipo,
            COALESCE(SUM(monto)   FILTER (WHERE ts >= %(hoy0)s), 0) AS hoy,
            COALESCE(SUM(ordenes) FILTER (WHERE ts >= %(hoy0)s), 0) AS ord_hoy,
            COALESCE(SUM(monto)   FILTER (WHERE ts >= %(hoy0)s AND metodo = 'enmascarado'), 0) AS masc_hoy,
            COALESCE(SUM(monto)   FILTER (WHERE ts >= %(h1)s), 0)  AS hora,
            COALESCE(SUM(monto)   FILTER (WHERE ts >= %(h2)s AND ts < %(h1)s), 0) AS p1,
            COALESCE(SUM(monto)   FILTER (WHERE ts >= %(h4)s), 0)  AS u4,
            COALESCE(SUM(monto)   FILTER (WHERE ts >= %(h8)s AND ts < %(h4)s), 0) AS p4,
            COALESCE(SUM(monto)   FILTER (WHERE ts >= %(h24)s), 0) AS u24,
            COALESCE(SUM(monto)   FILTER (WHERE ts >= %(h48)s AND ts < %(h24)s), 0) AS p24
        FROM fills_estimados
        WHERE exchange = %(ex)s AND ts >= %(h48)s
        GROUP BY tipo
    """, dict(params, ex=exchange))
    rows = {r["tipo"]: r for r in cur.fetchall()}
    if not rows:
        return None
    def g(k):
        return float(rows.get("BUY", {}).get(k, 0) or 0) + float(rows.get("SELL", {}).get(k, 0) or 0)
    buy_hoy, sell_hoy = float(rows.get("BUY", {}).get("hoy", 0) or 0), float(rows.get("SELL", {}).get("hoy", 0) or 0)
    tot_hoy  = buy_hoy + sell_hoy
    ord_hoy  = g("ord_hoy")
    masc_hoy = g("masc_hoy")
    u1, p1 = g("hora"), g("p1")
    u4, p4, u24, p24 = g("u4"), g("p4"), g("u24"), g("p24")
    def chg(u, p): return round((u - p) / p * 100, 1) if p else None
    return {
        "hoy": round(tot_hoy), "hora": round(u1), "cambio_1h_pct": chg(u1, p1),
        "presion_compra_pct": round(buy_hoy / tot_hoy * 100, 1) if tot_hoy else 50.0,
        "vol_4h": round(u4), "cambio_4h_pct": chg(u4, p4),
        "vol_24h": round(u24), "cambio_24h_pct": chg(u24, p24),
        "ordenes_hoy": int(ord_hoy),
        "ticket_med_hoy": round(tot_hoy / ord_hoy) if ord_hoy else None,
        "pct_enmascarado_hoy": round(masc_hoy / tot_hoy * 100, 1) if tot_hoy else None,
    }


@app.route("/api/volumen_v2")
def api_volumen_v2():
    """Volumen por fills confirmados (fills_estimados). Mismo formato que
    /api/volumen para comparar ambos metodos en paralelo, mas extras:
    ordenes_hoy, ticket_med_hoy, pct_enmascarado_hoy."""
    now = datetime.now(SANTIAGO_TZ)
    def f(dt): return dt.strftime("%Y-%m-%d %H:%M:%S")
    params = {
        "hoy0": f(now.replace(hour=0, minute=0, second=0, microsecond=0)),
        "h1": f(now - timedelta(hours=1)), "h2": f(now - timedelta(hours=2)),
        "h4": f(now - timedelta(hours=4)), "h8": f(now - timedelta(hours=8)),
        "h24": f(now - timedelta(hours=24)), "h48": f(now - timedelta(hours=48)),
    }
    binance = None; bybit = None
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            try:
                binance = _volumen_v2_exchange(cur, "binance", params)
            except Exception as e:
                print("[volumen_v2 binance]", e)
            try:
                bybit = _volumen_v2_exchange(cur, "bybit", params)
            except Exception as e:
                print("[volumen_v2 bybit]", e)
    return jsonify({"binance": binance, "bybit": bybit, "metodo": "fills confirmados (directo + enmascarado)"})


@app.route("/api/velocidad_mercado")
def api_velocidad_mercado():
    """Velocidad de rotacion del MERCADO desde fills confirmados.
    Params: ?horas=12&bucket=15&exchange=binance|bybit
    Devuelve serie en buckets (BUY/SELL separados) + metricas del momento:
    usdt_min_30m, fills_h_60m, ticket_med_60m, vs_promedio (ratio actual/12h)."""
    try:
        horas  = max(1, min(48, int(request.args.get("horas", 12))))
        bucket = max(5, min(60, int(request.args.get("bucket", 15))))
    except (ValueError, TypeError):
        horas, bucket = 12, 15
    ex = (request.args.get("exchange", "binance") or "binance").lower()
    if ex not in ("binance", "bybit"):
        ex = "binance"
    now   = datetime.now(SANTIAGO_TZ)
    desde = now - timedelta(hours=horas)
    f = lambda dt: dt.strftime("%Y-%m-%d %H:%M:%S")
    n_buckets = (horas * 60) // bucket
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT FLOOR(EXTRACT(EPOCH FROM (ts - %(desde)s)) / (%(bucket)s * 60))::int AS b,
                       COALESCE(SUM(monto)   FILTER (WHERE tipo = 'BUY'),  0) AS buy,
                       COALESCE(SUM(monto)   FILTER (WHERE tipo = 'SELL'), 0) AS sell,
                       COALESCE(SUM(ordenes), 0) AS ordenes
                FROM fills_estimados
                WHERE exchange = %(ex)s AND ts >= %(desde)s
                GROUP BY b ORDER BY b
            """, {"desde": f(desde), "bucket": bucket, "ex": ex})
            por_bucket = {int(r["b"]): r for r in cur.fetchall()}
            cur.execute("""
                SELECT
                    COALESCE(SUM(monto)   FILTER (WHERE ts >= %(m30)s), 0) AS vol_30m,
                    COALESCE(SUM(monto)   FILTER (WHERE ts >= %(m60)s), 0) AS vol_60m,
                    COALESCE(SUM(ordenes) FILTER (WHERE ts >= %(m60)s), 0) AS ord_60m,
                    COALESCE(SUM(monto), 0)   AS vol_total,
                    COALESCE(SUM(ordenes), 0) AS ord_total
                FROM fills_estimados
                WHERE exchange = %(ex)s AND ts >= %(desde)s
            """, {"m30": f(now - timedelta(minutes=30)),
                  "m60": f(now - timedelta(minutes=60)),
                  "ex": ex, "desde": f(desde)})
            tot = cur.fetchone()
    serie = []
    for i in range(n_buckets):
        r = por_bucket.get(i)
        t0 = desde + timedelta(minutes=i * bucket)
        serie.append({
            "t":       t0.strftime("%H:%M"),
            "buy":     round(float(r["buy"]))  if r else 0,
            "sell":    round(float(r["sell"])) if r else 0,
            "ordenes": int(r["ordenes"])       if r else 0,
        })
    vol_30m, vol_60m = float(tot["vol_30m"]), float(tot["vol_60m"])
    ord_60m          = float(tot["ord_60m"])
    vol_total        = float(tot["vol_total"])
    usdt_min_30m  = round(vol_30m / 30, 1)
    usdt_min_prom = round(vol_total / (horas * 60), 1)
    return jsonify({
        "exchange": ex, "horas": horas, "bucket_min": bucket,
        "serie": serie,
        "usdt_min_30m":    usdt_min_30m,
        "fills_h_60m":     int(ord_60m),
        "ticket_med_60m":  round(vol_60m / ord_60m) if ord_60m else None,
        "usdt_min_prom":   usdt_min_prom,
        "vs_promedio":     round(usdt_min_30m / usdt_min_prom, 2) if usdt_min_prom else None,
        "vol_total_ventana": round(vol_total),
        "descripcion": "Velocidad de rotacion desde fills confirmados. vs_promedio > 1 = el mercado rota mas rapido que su promedio de la ventana.",
    })


@app.route("/api/operativa")
def api_operativa():
    """ASISTENTE OPERATIVO — convierte las senales en una recomendacion:
    operar/esperar, precios asimetricos por presion de flujo, limites de
    orden segun ticket real, proyeccion por capital y vacios de liquidez.
    Params: ?capital=USDT (default: config CAPITAL_OPERATIVO)."""
    with config_lock:
        c = dict(config)
    try:
        capital = float(request.args.get("capital", 0)) or float(c.get("CAPITAL_OPERATIVO", 2000))
    except (ValueError, TypeError):
        capital = float(c.get("CAPITAL_OPERATIVO", 2000))
    with data_lock:
        snap = dict(ultimo_estado)
    if not snap or snap.get("spread_pond_pct") is None:
        return jsonify({"error": "sin datos aun, espera el primer ciclo"}), 503

    now = datetime.now(SANTIAGO_TZ)
    f = lambda dt: dt.strftime("%Y-%m-%d %H:%M:%S")
    horas_prom = 12
    stats = {"vol_30m": 0.0, "vol_60m_buy": 0.0, "vol_60m": 0.0, "ord_60m": 0,
             "vol_12h": 0.0, "p25": None, "p50": None, "p90": None, "n_tickets": 0}
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT
                        COALESCE(SUM(monto) FILTER (WHERE ts >= %(m30)s), 0) AS vol_30m,
                        COALESCE(SUM(monto) FILTER (WHERE ts >= %(m60)s AND tipo = 'BUY'), 0) AS vol_60m_buy,
                        COALESCE(SUM(monto) FILTER (WHERE ts >= %(m60)s), 0) AS vol_60m,
                        COALESCE(SUM(ordenes) FILTER (WHERE ts >= %(m60)s), 0) AS ord_60m,
                        COALESCE(SUM(monto), 0) AS vol_12h
                    FROM fills_estimados
                    WHERE exchange = 'binance' AND ts >= %(h12)s
                """, {"m30": f(now - timedelta(minutes=30)),
                      "m60": f(now - timedelta(minutes=60)),
                      "h12": f(now - timedelta(hours=horas_prom))})
                r = cur.fetchone()
                for k in ("vol_30m", "vol_60m_buy", "vol_60m", "vol_12h"):
                    stats[k] = float(r[k] or 0)
                stats["ord_60m"] = int(r["ord_60m"] or 0)
                # ticket real: solo fills 'directo' (tamano observado, no estimado)
                cur.execute("""
                    SELECT percentile_cont(0.25) WITHIN GROUP (ORDER BY t) AS p25,
                           percentile_cont(0.50) WITHIN GROUP (ORDER BY t) AS p50,
                           percentile_cont(0.90) WITHIN GROUP (ORDER BY t) AS p90,
                           COUNT(*) AS n
                    FROM (
                        SELECT monto / NULLIF(ordenes, 0) AS t
                        FROM fills_estimados
                        WHERE exchange = 'binance' AND metodo = 'directo'
                          AND ts >= %(h6)s AND ordenes >= 1
                    ) x WHERE t BETWEEN 10 AND 5000
                """, {"h6": f(now - timedelta(hours=6))})
                r = cur.fetchone()
                if r and r["n"]:
                    stats["p25"], stats["p50"], stats["p90"] = float(r["p25"]), float(r["p50"]), float(r["p90"])
                    stats["n_tickets"] = int(r["n"])
    except Exception as e:
        print(f"[operativa fills] {e}")

    # ── senales ──
    gan       = float(snap.get("ganancia_neta_pct") or 0)     # spread neto %
    min_op    = float(c["SPREAD_MIN_OPERATIVO"])
    rot_lento = float(c.get("UMBRAL_ROT_LENTO", 0.7))
    rot_dual  = float(c.get("UMBRAL_ROT_DUAL", 1.0))
    sesgo_min = float(c.get("UMBRAL_PRESION_SESGO", 10))
    com_total = float(c["COMISION_BN"]) * 2 * 100              # % round-trip
    usdt_min_30m  = stats["vol_30m"] / 30
    usdt_min_prom = stats["vol_12h"] / (horas_prom * 60) if stats["vol_12h"] else 0
    ratio    = round(usdt_min_30m / usdt_min_prom, 2) if usdt_min_prom else None
    presion  = round(stats["vol_60m_buy"] / stats["vol_60m"] * 100, 1) if stats["vol_60m"] else 50.0

    # ── decision (arbol unificado en decidir_operativa) ──
    decision, color, razon = decidir_operativa(gan, min_op, ratio, presion, rot_lento, rot_dual, sesgo_min)

    # ── precios asimetricos por presion ──
    # presion alta compradora -> tu anuncio de VENTA se llena solo: tomale mas
    # margen; tu anuncio de COMPRA tiene menos flujo: pegalo al lider.
    pond_c = float(snap.get("precio_pond_tab_compra") or 0)
    pond_v = float(snap.get("precio_pond_tab_venta") or 0)
    mid    = (pond_c + pond_v) / 2 if pond_c and pond_v else 0
    gap_cfg = float(c.get("GAP_OBJETIVO_BRUTO", 0) or 0)
    objetivo_bruto = gap_cfg if gap_cfg > 0 else (min_op + com_total)   # % gap bruto de tus anuncios flujo
    venta_share = min(0.75, max(0.25, presion / 100))
    margen_venta  = round(objetivo_bruto * venta_share, 3)
    margen_compra = round(objetivo_bruto * (1 - venta_share), 3)
    precio_vender_flujo  = round(mid * (1 + margen_venta / 100), 2)  if mid else None
    precio_comprar_flujo = round(mid * (1 - margen_compra / 100), 2) if mid else None

    # ── limites de orden sugeridos (en CLP, redondeados a 10.000) ──
    limites = None
    if stats["p25"] and mid:
        rnd = lambda x: int(round(x / 10000) * 10000) if x else None
        max_por_capital = capital * mid
        limites = {
            "min_clp": max(10000, rnd(stats["p25"] * mid)),
            "max_clp": rnd(min(stats["p90"] * mid, max_por_capital)),
            "ticket_p25_usdt": round(stats["p25"]),
            "ticket_p50_usdt": round(stats["p50"]),
            "ticket_p90_usdt": round(stats["p90"]),
            "muestras": stats["n_tickets"],
            "nota": "min cubre el p25 del ticket real (farming de ordenes); max en el p90 o tu capital, lo que sea menor",
        }

    # ── proyeccion por capital (TECHO teorico; queda como referencia chica) ──
    flujo_h = round(usdt_min_30m * 60)
    escenarios = []
    for cap_pct in (5, 10, 20):
        capturado = flujo_h * cap_pct / 100
        escenarios.append({
            "captura_pct": cap_pct,
            "usdt_h": round(capturado),
            "giros_h": round(capturado / capital, 2) if capital else None,
            "ganancia_h_clp": round(capturado * gan / 100 * mid) if mid and gan > 0 else 0,
        })

    # ── proyeccion REALISTA (COL12): el numero principal ──
    # Dos limites reales que el techo teorico ignoraba:
    #  1. captura: sin verificar competis por ~2% del flujo, no 10%.
    #  2. tiempo: a mano no se atienden mas de ORDENES_H_MAX ordenes/hora
    #     (el techo teorico daba ~29 giros/h, fisicamente imposible).
    # Escenario "hoy" usa la comision vigente; "verificado" la Bronce (0,32% RT,
    # NO 0,20%: el 50% de descuento es nivel Oro, 60 BTC/mes).
    bruto_pct    = gan + com_total                     # spread bruto ponderado actual
    ticket_ref   = stats["p50"] or float(c.get("FILL_TICKET_DEF", 408))
    # DOS limites reales, y manda el menor (COL17):
    #  1. lo que el MERCADO te da en tu posicion -> ritmo MEDIDO (fills observados)
    #  2. lo que VOS alcanzas a atender a mano   -> ORDENES_H_MAX
    # Antes habia un solo numero y era una suposicion.
    ordenes_max  = max(1, int(c.get("ORDENES_H_MAX", 8)))
    ritmo_medido = float(c.get("RITMO_MEDIDO_ORD_H", 0) or 0)   # por pierna
    mercado_ord_h = ritmo_medido * 2 if ritmo_medido > 0 else None   # dual = 2 piernas
    if mercado_ord_h:
        ordenes_efectivas = min(ordenes_max, mercado_ord_h)
        limite_ordenes = "mercado" if mercado_ord_h < ordenes_max else "tu tiempo"
    else:
        ordenes_efectivas = ordenes_max
        limite_ordenes = "tu tiempo (ritmo de mercado aun sin medir)"
    tope_manual  = ordenes_efectivas * ticket_ref       # USDT/h alcanzables
    com_verif_rt = float(c.get("COMISION_BN_VERIF", 0.0016)) * 2 * 100
    esc_real = []
    for nombre, com_rt, cap_pct in (
        ("hoy",        com_total,    float(c.get("CAPTURA_REALISTA_PCT", 2.0))),
        ("verificado", com_verif_rt, float(c.get("CAPTURA_VERIF_PCT", 3.0))),
    ):
        neto_pct  = round(bruto_pct - com_rt, 4)
        capturado = flujo_h * cap_pct / 100
        usdt_h    = min(capturado, tope_manual)
        # clp_h puede ser NEGATIVO (en farming aceptas perder unos pesos por
        # las ordenes): se muestra tal cual, es el costo real de la campana.
        clp_h = round(usdt_h * neto_pct / 100 * mid) if mid else None
        esc_real.append({
            "nombre": nombre,
            "comision_rt_pct": round(com_rt, 4),
            "captura_pct": cap_pct,
            "neto_pct": neto_pct,
            "usdt_h": round(usdt_h),
            "giros_h": round(usdt_h / capital, 1) if capital else None,
            "ordenes_h": round(usdt_h / ticket_ref, 1) if ticket_ref else None,
            "clp_h": clp_h,
            "limitado_por": "tiempo" if capturado > tope_manual else "captura",
        })

    # ── vacios de liquidez: competidores por agotarse ──
    vacios = []
    for lado, key in (("BUY", "detalle_compra"), ("SELL", "detalle_venta")):
        for row in (snap.get(key) or []):
            vel = float(row.get("velocidad") or 0)
            disp = float(row.get("disponible") or 0)
            if vel > 0 and disp > 0:
                mins = disp / vel
                if mins <= 30:
                    vacios.append({
                        "tipo": lado, "anunciante": row.get("anunciante"),
                        "posicion": row.get("posicion"),
                        "precio": row.get("precio"),
                        "disponible": round(disp),
                        "velocidad_usdt_min": vel,
                        "min_restantes": round(mins, 1),
                    })
    vacios.sort(key=lambda x: x["min_restantes"])
    vacios = vacios[:6]

    return jsonify({
        "timestamp": f(now),
        "decision": decision, "color": color, "razon": razon,
        "mercado": {
            "spread_neto_pct": gan, "spread_min_operativo": min_op,
            "vs_promedio_12h": ratio, "usdt_min_30m": round(usdt_min_30m, 1),
            "flujo_usdt_h": flujo_h, "fills_h_60m": stats["ord_60m"],
            "presion_compra_pct": presion, "estado_libro": snap.get("estado"),
        },
        "precios": {
            "agresivo_vender":  snap.get("precio_maker_vender"),
            "agresivo_comprar": snap.get("precio_maker_comprar"),
            "flujo_vender":  precio_vender_flujo,
            "flujo_comprar": precio_comprar_flujo,
            "margen_venta_pct":  margen_venta,
            "margen_compra_pct": margen_compra,
            "nota": "agresivo = cabeza del libro (fill rapido, menos margen). flujo = gap asimetrico segun presion (mas margen del lado que la demanda llena sola)",
        },
        "limites": limites,
        "proyeccion": {
            "capital_usdt": capital,
            "ganancia_por_giro_clp": round(capital * gan / 100 * mid) if mid and gan > 0 else 0,
            "escenarios_captura": escenarios,
            "nota": "TECHO teorico (referencia): asume capturar 5-20% del flujo con reciclado continuo e ilimitado del capital",
        },
        "proyeccion_realista": {
            "capital_usdt": capital,
            "ticket_ref_usdt": round(ticket_ref),
            "ordenes_h_max": ordenes_max,
            "ritmo_medido_ord_h": ritmo_medido or None,
            "ritmo_medido_rango": c.get("RITMO_MEDIDO_RANGO") or None,
            "ordenes_h_efectivas": round(ordenes_efectivas, 2),
            "limite_ordenes": limite_ordenes,
            "tope_manual_usdt_h": round(tope_manual),
            "spread_bruto_pct": round(bruto_pct, 4),
            "escenarios": esc_real,
            "nota": "numero principal: captura realista (~2% sin verificar) + tope de ordenes/hora atendibles a mano. 'verificado' = Bronce 0,32% RT y algo mas de captura. clp_h negativo = costo de farmear a ese spread.",
        },
        "vacios_liquidez": vacios,
    })


@app.route("/api/mi_posicion")
def api_mi_posicion():
    """MI POSICION + carrera al verificado. Usa datos que el colector YA junta:
    - mis anuncios en el top-80 (posicion, precio, stock) desde ultimo_estado;
    - mis ordenes 30d desde 'completadas' (= monthOrderCount oficial de Binance);
    - mi volumen estimado desde fills_estimados (filtrado por mi nickname).
    OJO semantica: mi anuncio de VENTA aparece en el tab Compra del libro
    (tradeType BUY) y mi anuncio de COMPRA en el tab Venta (tradeType SELL)."""
    with config_lock:
        nick    = str(config.get("MI_NICKNAME") or "").strip()
        pos_obj = max(1, int(config.get("MI_POSICION_OBJETIVO", 15) or 15))
    if not nick:
        return jsonify({"configurado": False,
                        "nota": "defini tu nickname en el panel Estrategia para activar el seguimiento"})
    with data_lock:
        snap = dict(ultimo_estado)
    anuncios = []
    for key, rol, tab in (("detalle_compra", "VENDO USDT", "Compra"),
                          ("detalle_venta",  "COMPRO USDT", "Venta")):
        detalle = snap.get(key) or []
        mio = next((r for r in detalle
                    if (r.get("anunciante") or "").strip().lower() == nick.lower()), None)
        if not mio:
            anuncios.append({"rol": rol, "tab": tab, "publicado": False})
            continue
        pos = int(mio.get("posicion") or 0)
        sugerido = None
        if pos > pos_obj and len(detalle) >= pos_obj:
            p_ref = float(detalle[pos_obj - 1].get("precio") or 0)
            if p_ref > 0:
                # tab Compra ordena ascendente (mejorar = bajar precio);
                # tab Venta ordena descendente (mejorar = subir precio)
                sugerido = round(p_ref - 0.01, 2) if key == "detalle_compra" else round(p_ref + 0.01, 2)
        anuncios.append({
            "rol": rol, "tab": tab, "publicado": True,
            "posicion": pos,
            "precio": mio.get("precio"),
            "disponible": round(float(mio.get("disponible") or 0)),
            "en_objetivo": bool(pos and pos <= pos_obj),
            "posicion_objetivo": pos_obj,
            "precio_sugerido": sugerido,
        })
    prog = {"ordenes_30d": None, "ordenes_hace_7d": None, "vol_30d_estimado": 0,
            "vol_7d_estimado": 0, "fills_detectados_30d": 0}
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT MAX(completadas) AS o
                    FROM snapshots_detalle
                    WHERE LOWER(anunciante) = LOWER(%(n)s)
                      AND snapshot_timestamp >= NOW() - INTERVAL '24 hours'
                """, {"n": nick})
                r = cur.fetchone()
                if r and r["o"] is not None:
                    prog["ordenes_30d"] = int(r["o"])
                cur.execute("""
                    SELECT MAX(completadas) AS o
                    FROM snapshots_detalle
                    WHERE LOWER(anunciante) = LOWER(%(n)s)
                      AND snapshot_timestamp BETWEEN NOW() - INTERVAL '8 days'
                                                 AND NOW() - INTERVAL '6 days'
                """, {"n": nick})
                r = cur.fetchone()
                if r and r["o"] is not None:
                    prog["ordenes_hace_7d"] = int(r["o"])
                cur.execute("""
                    SELECT COALESCE(SUM(monto)   FILTER (WHERE ts >= NOW() - INTERVAL '30 days'), 0) AS v30,
                           COALESCE(SUM(monto)   FILTER (WHERE ts >= NOW() - INTERVAL '7 days'), 0)  AS v7,
                           COALESCE(SUM(ordenes) FILTER (WHERE ts >= NOW() - INTERVAL '30 days'), 0) AS f30
                    FROM fills_estimados
                    WHERE exchange = 'binance' AND LOWER(anunciante) = LOWER(%(n)s)
                """, {"n": nick})
                r = cur.fetchone()
                prog["vol_30d_estimado"] = round(float(r["v30"] or 0))
                prog["vol_7d_estimado"]  = round(float(r["v7"] or 0))
                prog["fills_detectados_30d"] = int(r["f30"] or 0)
    except Exception as e:
        print(f"[mi_posicion] {e}")
    meta_min, meta_comoda, meta_ord = 32000, 64000, 300
    v30 = prog["vol_30d_estimado"]
    o30 = prog["ordenes_30d"]
    return jsonify({
        "configurado": True, "nick": nick,
        "en_libro": any(a.get("publicado") for a in anuncios),
        "anuncios": anuncios,
        "progreso": {
            **prog,
            "ordenes_meta": meta_ord,
            "ordenes_pct": round(o30 / meta_ord * 100, 1) if o30 else None,
            "ordenes_ganadas_7d": (o30 - prog["ordenes_hace_7d"])
                                  if (o30 is not None and prog["ordenes_hace_7d"] is not None) else None,
            "meta_minima_usdt": meta_min, "meta_comoda_usdt": meta_comoda,
            "vol_pct_minima": round(v30 / meta_min * 100, 1) if v30 else 0,
            "vol_pct_comoda": round(v30 / meta_comoda * 100, 1) if v30 else 0,
        },
        "nota": ("ordenes_30d = contador oficial de Binance (30d moviles, aparece cuando estas en el top-80). "
                 "volumen = estimacion del monitor por fills detectados; validalo contra la pagina de Merchant. "
                 "metas: 0,5 BTC ~ 32.000 USDT (minimo real) / 1 BTC ~ 64.000 (comodo) + 300 ordenes."),
    })


@app.route("/api/heatmap")
def api_heatmap():
    rows = obtener_heatmap()
    for r in rows:
        for k, v in r.items():
            if hasattr(v, "__float__"): r[k] = float(v)
    return jsonify(rows)


@app.route("/api/velocidad")
def api_velocidad():
    anunciante = request.args.get("anunciante", "")
    tipo       = request.args.get("tipo", "BUY").upper()
    try:
        limit = int(request.args.get("limit", 50))
    except ValueError:
        limit = 50
    if not anunciante:
        return jsonify({"error": "anunciante requerido"}), 400
    rows = obtener_velocidad_anunciante(anunciante, tipo, limit)
    out = []
    for r in rows:
        ts   = r["snapshot_timestamp"]
        disp = r["disponible"]
        out.append({
            "timestamp":  str(ts)[:19],
            "disponible": float(disp) if disp is not None else None,
        })
    return jsonify(out)


@app.route("/api/count")
def api_count():
    return jsonify({"count": obtener_count()})


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    global config
    if request.method == "POST":
        if not _token_ok():
            return jsonify({"ok": False, "error": "token requerido o invalido"}), 401
        data = request.get_json() or {}
        errores, aplicados = {}, {}
        with config_lock:
            for k, cast in CONFIG_TYPE_MAP.items():
                if k in data:
                    try:
                        config[k] = cast(data[k])
                        aplicados[k] = config[k]
                    except (ValueError, TypeError):
                        errores[k] = "valor invalido"
        guardar_config_db(aplicados)   # persiste: sobrevive reinicios de Railway
        if errores:
            return jsonify({"ok": False, "errores": errores}), 400
        return jsonify({"ok": True})
    with config_lock:
        return jsonify(dict(config))

# NOTA: /api/reset (drop de snapshots) se ELIMINO en COL11: era destructivo,
# estaba abierto a cualquiera que conociera la URL y no se usaba desde la UI.
# Para liberar disco esta /api/mantenimiento/vaciar (conserva 24h y precios).


@app.route("/api/export/detalle")
def api_export_detalle():
    """Exporta snapshots_detalle como CSV o JSON.
    Params: ?dias=N&tipo=BUY|SELL|ALL&fmt=csv|json&limit=N
    Ej: /api/export/detalle?dias=7&tipo=ALL"""
    try:
        dias = int(request.args.get("dias", 7))
    except (ValueError, TypeError):
        dias = 7
    tipo = (request.args.get("tipo", "ALL") or "ALL").upper()
    fmt  = (request.args.get("fmt", "csv") or "csv").lower()
    fuente = (request.args.get("fuente", "binance") or "binance").lower()
    tabla = "snapshots_detalle_bybit" if fuente == "bybit" else "snapshots_detalle"
    limit_arg = request.args.get("limit")

    where  = ["snapshot_timestamp >= NOW() - (%s || ' days')::INTERVAL"]
    params = [dias]
    if tipo in ("BUY", "SELL"):
        where.append("tipo = %s")
        params.append(tipo)
    sql = """
        SELECT snapshot_timestamp, hora, tipo, posicion, anunciante,
               precio, disponible, completadas, tasa_exito, es_merchant
        FROM """ + tabla + """
        WHERE """ + " AND ".join(where) + """
        ORDER BY snapshot_timestamp DESC, tipo, posicion
    """
    if limit_arg:
        try:
            params.append(int(limit_arg))
            sql += " LIMIT %s"
        except (ValueError, TypeError):
            pass

    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

    if fmt == "json":
        out = []
        for r in rows:
            d = dict(r)
            d["snapshot_timestamp"] = str(d["snapshot_timestamp"])[:19]
            for k in ("precio", "disponible", "tasa_exito"):
                if d.get(k) is not None:
                    d[k] = float(d[k])
            out.append(d)
        return jsonify(out)

    import csv, io
    buf = io.StringIO()
    campos = ["snapshot_timestamp", "hora", "tipo", "posicion", "anunciante",
              "precio", "disponible", "completadas", "tasa_exito", "es_merchant"]
    w = csv.writer(buf)
    w.writerow(campos)
    for r in rows:
        w.writerow([
            str(r["snapshot_timestamp"])[:19], r["hora"], r["tipo"], r["posicion"],
            r["anunciante"],
            float(r["precio"])     if r["precio"]     is not None else "",
            float(r["disponible"]) if r["disponible"] is not None else "",
            r["completadas"],
            float(r["tasa_exito"]) if r["tasa_exito"] is not None else "",
            r["es_merchant"],
        ])
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=detalle_{fuente}_{tipo}_{dias}d.csv"},
    )


@app.route("/api/export/todo")
def api_export_todo():
    """Backup general en UN clic: ZIP con el detalle de Binance y Bybit.
    Param: ?dias=N (default 30, cubre toda la base)."""
    import csv, io, zipfile
    try:
        dias = int(request.args.get("dias", 30))
    except (ValueError, TypeError):
        dias = 30
    campos = ["snapshot_timestamp", "hora", "tipo", "posicion", "anunciante",
              "precio", "disponible", "completadas", "tasa_exito", "es_merchant"]
    def csv_de(tabla):
        buf = io.StringIO(); w = csv.writer(buf); w.writerow(campos)
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT snapshot_timestamp, hora, tipo, posicion, anunciante,
                           precio, disponible, completadas, tasa_exito, es_merchant
                    FROM """ + tabla + """
                    WHERE snapshot_timestamp >= NOW() - (%s || ' days')::INTERVAL
                    ORDER BY snapshot_timestamp DESC, tipo, posicion
                """, [dias])
                for r in cur.fetchall():
                    w.writerow([str(r["snapshot_timestamp"])[:19], r["hora"], r["tipo"], r["posicion"],
                                r["anunciante"],
                                float(r["precio"]) if r["precio"] is not None else "",
                                float(r["disponible"]) if r["disponible"] is not None else "",
                                r["completadas"],
                                float(r["tasa_exito"]) if r["tasa_exito"] is not None else "",
                                r["es_merchant"]])
        return buf.getvalue()
    hoy = datetime.now(SANTIAGO_TZ).strftime("%Y-%m-%d")
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
        for tabla, nombre in (("snapshots_detalle", "binance"), ("snapshots_detalle_bybit", "bybit")):
            try:
                z.writestr(f"{nombre}_{hoy}.csv", csv_de(tabla))
            except Exception as e:
                print(f"[export todo {nombre}]", e)
    zbuf.seek(0)
    return Response(zbuf.getvalue(), mimetype="application/zip",
                    headers={"Content-Disposition": f"attachment; filename=backup_p2p_{hoy}.zip"})


@app.route("/api/mantenimiento/vaciar", methods=["POST"])
def api_vaciar_listas():
    """Vacia las listas top-80 conservando las ultimas 24h (para el solape).
    Corre EN SEGUNDO PLANO para no colgar la peticion (el colector sigue vivo).
    NO toca snapshots (precios), fills_estimados ni operativa_historial.
    FIX COL11: la version COL9/COL10 llamaba get_conn() sin 'with' (es un
    contextmanager) -> AttributeError silencioso en el worker: respondia ok
    pero NO vaciaba nada. Ahora usa el pool correctamente."""
    if not _token_ok():
        return jsonify({"ok": False, "error": "token requerido o invalido"}), 401
    def _worker():
        horas = 24
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    try:
                        cur.execute("SET lock_timeout = '25s'")
                        conn.commit()
                    except Exception:
                        conn.rollback()
                    for t in ("snapshots_detalle", "snapshots_detalle_bybit"):
                        try:
                            cur.execute(f"SELECT to_regclass('public.{t}')")
                            if cur.fetchone()[0] is None:
                                continue
                            cur.execute("DROP TABLE IF EXISTS _keep_tmp")
                            cur.execute(f"CREATE TEMP TABLE _keep_tmp AS SELECT * FROM {t} "
                                        f"WHERE snapshot_timestamp >= NOW() - INTERVAL '{horas} hours'")
                            cur.execute(f"TRUNCATE TABLE {t}")
                            cur.execute(f"INSERT INTO {t} SELECT * FROM _keep_tmp")
                            cur.execute("DROP TABLE _keep_tmp")
                            conn.commit()
                            print(f"[VACIAR] {t}: OK (conservadas 24h)")
                        except Exception as e:
                            conn.rollback()
                            print(f"[VACIAR {t}] {e}")
                    try:
                        cur.execute("SET lock_timeout = DEFAULT")
                        conn.commit()
                    except Exception:
                        conn.rollback()
        except Exception as e:
            print(f"[VACIAR] {e}")
    threading.Thread(target=_worker, daemon=True).start()
    return jsonify({"ok": True, "msg": "Vaciando en segundo plano. En unos segundos baja el disco."})


# ──────────────────────────────────────────────
#  INICIO
# ──────────────────────────────────────────────
def _boot():
    init_pool()
    init_db()
    cargar_config_db()   # restaura el preset/config guardado (sobrevive reinicios)
    try:
        recalibrar_tickets()   # arranca con el ticket medido, no con el default
    except Exception as e:
        print(f"[TICKET boot] {e}")
    try:
        recalibrar_ritmo()     # y con el ritmo de mercado medido en tu posicion
    except Exception as e:
        print(f"[RITMO boot] {e}")
    try:
        recalibrar_horarios()  # perfil de cada hora (plan de hoy + gap adaptativo)
    except Exception as e:
        print(f"[HORARIOS boot] {e}")
    threading.Thread(target=ciclo_colector, daemon=True).start()
    threading.Thread(target=ciclo_colector_bybit, daemon=True).start()

if __name__ == "__main__":
    _boot()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
else:
    _boot()
