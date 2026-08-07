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
from psycopg2.extras import RealDictCursor, execute_values
from contextlib import contextmanager

app = Flask(__name__)
SANTIAGO_TZ = ZoneInfo("America/Santiago")

# Version del codigo: se expone en /api/version y en el pie del dashboard, para
# confirmar de un vistazo QUE version esta corriendo en Railway tras un deploy.
VERSION       = "COL57"
VERSION_FECHA = "2026-08-06"

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
    "COM_BYBIT_MAKER":      0.0,     # Bybit CLP: sin comision al publicar
    # BORRADAS en COL43: COM_BINANCE_MAKER, COM_BINANCE_TAKER, COM_BYBIT_TAKER
    # y COSTO_TRANSFER_USDT. Ninguna se leia en el codigo (COMISION_BN es la
    # que manda para Binance) ni se habia persistido nunca en config_persistente
    # -- verificado contra la DB antes de sacarlas. La taker real ademas NO es
    # un %, son 0,07 USDT FIJOS por orden (medido, ver Doctrina), asi que
    # COM_BINANCE_TAKER=0.001 era ademas un valor equivocado esperando a que
    # alguien lo usara.
    # Spread neto mínimo (después de comisiones) para considerar operable.
    "SPREAD_MIN_OPERATIVO": 0.35,
    # Cuantos dias de detalle crudo (top-80 cada 2 min) se conservan.
    # Pesa ~25 MB/dia entre Binance y Bybit. Con 10 dias la base se estabiliza
    # en ~270 MB de los 500 disponibles (53%), asi que NO hace falta vaciar a
    # mano: la purga diaria sola la mantiene ahi. (COL31: era 7 fijo en el codigo.)
    "DETALLE_DIAS":         10,
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
    # Ticket PROPIO (COL24), medido de mis_ordenes_reales (CSV real, ground truth).
    # Corrige un bug real: el ticket para fills 'enmascarado' de MI cuenta caia al
    # generico del MERCADO (~400 USDT) porque el historial en memoria por
    # anunciante (st["tickets"]) se resetea con cada redeploy, y en un ciclo de
    # desarrollo activo eso pasa seguido. Medido 28-jul: mi ticket real es ~69
    # USDT (mediana de 31 fills directos), 6x mas chico que el generico -> mis
    # fills enmascarados se sobrestimaban ~150%. 0 = todavia sin datos suficientes.
    "MI_TICKET_MEDIO":      0.0,
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
    # ── Banda de inventario (COL22) ──────────────────────────────
    # Politica, NO un numero medido: sale del capital (~700-1.000) y del ticket
    # (30-60). Doctrina maxima 7: nunca parar cargado de un solo lado.
    #   40-60%  -> zona comoda: farmear dual como maker.
    #   fuera de 40-60 -> correccion: repreciar AGRESIVO el lado corto (todavia maker).
    #   <30 o >70      -> limite duro: cruzar como TAKER si o si.
    # A futuro el monitor la afina solo viendo con que % se queda trabado.
    # ── Modulo Ciclo de recompra (COL27) ────────────────────────────
    # El monto es el input CLAVE porque la comision taker es un monto FIJO
    # (0,07 USDT): su peso en % depende enteramente de cuanto cruces.
    #   100 USDT -> 0,0700%    1.200 USDT -> 0,0058%
    # Arriba de ~300 USDT la taker es ruido frente al 0,20% del maker.
    "CICLO_MONTO_DEFAULT":   1200,  # USDT sugeridos por ciclo
    "CICLO_MARGEN_OBJETIVO": 0.30,  # % neto deseado por vuelta
    "CICLO_FLUJO_MIN_DIA":   2000,  # USDT/dia capturables para considerar la banda "con flujo"
    # ── Rutinas de mantenimiento (COL25): cada cuantos dias toca cada tarea ──
    # Se avisa en el dashboard. Los dias salen de la experiencia de la sesion
    # 28-jul: el ancla driftea si pasan varios dias, el CSV afina calibracion y
    # ticket propio, y el backup protege ante la purga automatica.
    # ── Contexto macro (COL35): cada cuantos minutos se leen dolar/VIX/cobre.
    # NO tiene sentido al ritmo del libro P2P (2 min): el VIX y el cobre se
    # mueven mucho mas lento y ademas cierran los fines de semana. 15 min da
    # ~96 filas/dia, despreciable, y no castiga la fuente gratuita.
    "MACRO_MIN":            15,
    # Desfase minimo (en puntos porcentuales) entre lo que se movio el dolar
    # forex y lo que se movio el P2P en la misma ventana, para dar aviso.
    # MEDIDO el 31-jul-2026 sobre 233 pares de horas: el cambio del forex
    # correlaciona +0,461 con el cambio del P2P de la hora SIGUIENTE, contra
    # +0,063 en la misma hora y +0,035 en el sentido inverso (control). O sea:
    # el forex se mueve primero y el P2P lo sigue, no al reves.
    "MACRO_DESFASE_PCT":    0.15,
    "MACRO_DESFASE_MIN":    75,    # ventana en minutos para medir ese desfase
    # ── Capacidad real de operacion (COL37) ──────────────────────────
    # Sebastian trabaja en un restaurant con turnos rotativos: NO puede operar
    # los 7 dias. Sin esto el plan de Merchant reparte la meta entre 30 dias
    # como si todos fueran iguales, y da un ritmo diario que no es alcanzable
    # en la practica. Con 4 dias operables por semana, el ritmo por dia
    # OPERADO es 7/4 = 1,75 veces el que sale del reparto parejo.
    # La hoja "Plan" de la bitacora ya distingue dias "Libre / oro" de
    # "Trabajo": esto es la version simple de ese mismo concepto.
    "DIAS_OPERABLES_SEMANA": 4,
    # ── Libro en vivo (COL38) ────────────────────────────────────────
    # Cada cuantos SEGUNDOS se refresca la cabeza del libro. NO escribe en
    # la base: solo memoria. Medido 2-ago: la API tarda ~590 ms y aguanta
    # consultas seguidas; a 10s son ~17.000 req/dia (0,2 por segundo).
    "LIBRO_VIVO_SEG":       10,
    # Metodos de pago que PODES usar, por 'identifier' de Binance.
    # VACIO = no filtra nada. Sirve para no calcular el ciclo contra un
    # precio de alguien con quien no podes operar.
    #
    # RESUELTO 4-ago (COL46): Sebastian mostro su pantalla de metodos y dijo
    # "solo utilizo la opcion de transferencia con banco especifico".
    # Verificado contra el libro EN VIVO que el identifier de eso es
    # exactamente "SpecificBank" (nombre mostrado: "Transfers with specific
    # bank") — no se asumio, se leyo de los tradeMethods reales.
    # MEDIDO antes de activarlo, en el lado donde recompra (tradeType=BUY):
    #   - 43 de 80 anuncios (54%) aceptan SpecificBank
    #   - el VWAP de una tanda de 240 USDT pasa de 917,89 a 918,31 = 0,036%
    # Ese 0,036% es el precio de que TODO lo que muestre sea operable de
    # verdad; contra el 0,20% de comision por pierna, es barato.
    # Sumar "BANK" (Transferencia Bancaria, que tambien tiene registrada)
    # agrega 6 anuncios pero NO mejora el VWAP: no se incluye.
    #
    # OJO con la direccion de los lados al re-medir esto: tradeType=BUY es
    # donde Sebastian COMPRA (los anunciantes venden) y hay que barrer desde
    # el precio MAS BARATO; tradeType=SELL es donde vende y se barre desde el
    # MAS CARO. Mezclarlos da un "spread" de 3,7% que no existe.
    "MIS_METODOS_PAGO":     "SpecificBank",
    "RUT_ANCLA_DIAS":       3,     # re-anclar inventario (saldos reales)
    "RUT_CSV_DIAS":         10,    # importar CSV de Binance (calibracion)
    "RUT_BACKUP_DIAS":      7,     # backup general de la base
    "INV_BANDA_MIN":        40,
    "INV_BANDA_MAX":        60,
    "INV_DURO_MIN":         30,
    "INV_DURO_MAX":         70,
}
config_lock = threading.Lock()

# Claves de config editables en caliente (POST /api/config). Estas mismas claves
# se PERSISTEN en la tabla config_persistente: sobreviven reinicios de Railway
# (antes un redeploy volvia todo a los defaults y el preset Farming se perdia solo).
CONFIG_TYPE_MAP = {
    "FILTRO_MIN_USDT":      float,
    "FILTRO_MIN_ORD":       int,
    "FILTRO_MIN_TASA":      float,
    "DETALLE_DIAS":         int,
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
    "MI_TICKET_MEDIO":      float,
    "RITMO_MEDIDO_ORD_H":   float,
    "RITMO_MEDIDO_RANGO":   str,
    "COM_TAKER_FIJA_USDT":  float,
    "COM_MAKER_PCT":        float,
    "CICLO_MONTO_DEFAULT":   float,
    "CICLO_MARGEN_OBJETIVO": float,
    "CICLO_FLUJO_MIN_DIA":   float,
    "MACRO_MIN":            int,
    "MACRO_DESFASE_PCT":    float,
    "MACRO_DESFASE_MIN":    int,
    "DIAS_OPERABLES_SEMANA": int,
    "LIBRO_VIVO_SEG":       int,
    "MIS_METODOS_PAGO":     str,
    "RUT_ANCLA_DIAS":       int,
    "RUT_CSV_DIAS":         int,
    "RUT_BACKUP_DIAS":      int,
    "INV_BANDA_MIN":        float,
    "INV_BANDA_MAX":        float,
    "INV_DURO_MIN":         float,
    "INV_DURO_MAX":         float,
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
            # ── LIMITES DE ORDEN por anuncio (COL32) ─────────────────────
            # min/max que el anunciante configuro, en CLP TAL COMO VIENEN de la
            # API (no se convierten a USDT: es un monto fiat fijo, convertirlo
            # lo haria variar con el precio). INTEGER alcanza: el techo real es
            # ~7.000.000 contra los 2.147 millones que soporta el tipo.
            # Aditivas: las filas viejas quedan NULL, asi que TODA consulta
            # tiene que tolerar min_orden IS NULL.
            for _t in ("snapshots_detalle", "snapshots_detalle_bybit"):
                cur.execute(f"ALTER TABLE {_t} ADD COLUMN IF NOT EXISTS min_orden INTEGER")
                cur.execute(f"ALTER TABLE {_t} ADD COLUMN IF NOT EXISTS max_orden INTEGER")
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
            # ticket propio de cada anunciante (COL31). Aditiva: en los dias
            # viejos queda NULL y no rompe nada. La llena guardar_agregados_dia
            # con la mediana de sus fills 'directo'; la lee al arranque
            # recalibrar_tickets_por_anunciante() para que el FillTracker no
            # empiece de cero en cada redeploy.
            cur.execute("ALTER TABLE agregados_anunciante_dia ADD COLUMN IF NOT EXISTS ticket_medio NUMERIC")
            # limites de orden propagados al resumen permanente (COL32): asi el
            # segmento de cada competidor (mayorista / minorista) sobrevive a la
            # purga del detalle. La MODA del minimo va aparte del promedio: si
            # el anunciante edita el limite un rato, el promedio se ensucia y la
            # moda sigue mostrando el valor con el que opera de verdad.
            cur.execute("ALTER TABLE agregados_anunciante_dia ADD COLUMN IF NOT EXISTS min_orden_med INTEGER")
            cur.execute("ALTER TABLE agregados_anunciante_dia ADD COLUMN IF NOT EXISTS max_orden_med INTEGER")
            cur.execute("ALTER TABLE agregados_anunciante_dia ADD COLUMN IF NOT EXISTS min_orden_moda INTEGER")
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
            # ── INVENTARIO EN VIVO (COL22) ──────────────────────────────
            # Diseño HIBRIDO: el monitor NO puede saber el saldo solo (no ve el
            # banco ni las ordenes taker, que no dejan anuncio en el libro).
            #  1. ANCLA = la verdad. El usuario pega sus saldos reales.
            #  2. ESTIMACION = ancla + movimientos desde el ts del ancla.
            #  3. Al re-anclar se ve el DRIFT (estimado vs real) = calibracion gratis.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS inventario_ancla (
                    id SERIAL PRIMARY KEY,
                    ts TIMESTAMP NOT NULL,
                    usdt NUMERIC NOT NULL,
                    clp NUMERIC NOT NULL,
                    precio_ref NUMERIC,
                    nota TEXT
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_inv_ancla_ts ON inventario_ancla(ts DESC)")
            # Dolar formal al momento de anclar (COL35). precio_ref ya guarda el
            # P2P; con los dos se ve la BRECHA, que es el numero que Sebastian
            # queria tener claro para anotar en la bitacora. Aditiva: las anclas
            # viejas quedan en NULL y el historial lo tolera.
            cur.execute("ALTER TABLE inventario_ancla ADD COLUMN IF NOT EXISTS usdclp_forex NUMERIC")
            # ── CONTEXTO MACRO (COL35) ───────────────────────────────────
            # Dolar oficial (forex), VIX y cobre. Sebastian ya opera mirando
            # estos graficos al lado porque el P2P tiene RETARDO respecto del
            # mercado formal: si el cobre/dolar se mueven, el P2P los sigue
            # unos minutos despues. Guardar la serie permite despues MEDIR si
            # esa anticipacion es real (igual que se hizo con la presion, que
            # resulto NO predecir el precio) en vez de confiar en el ojo.
            # Todas las columnas admiten NULL: si una fuente falla, la fila
            # igual sirve por las otras.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS snapshots_macro (
                    id SERIAL PRIMARY KEY,
                    ts TIMESTAMP NOT NULL,
                    usdclp_forex NUMERIC,
                    vix NUMERIC,
                    cobre NUMERIC,
                    p2p_ref NUMERIC,
                    brecha_pct NUMERIC
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_macro_ts ON snapshots_macro(ts DESC)")
            # BTC (COL36): las metas de Merchant estan EN BTC, asi que sin el
            # precio no se pueden traducir a USDT operables.
            cur.execute("ALTER TABLE snapshots_macro ADD COLUMN IF NOT EXISTS btc_usd NUMERIC")
            # ── ANCLA DE MERCHANT (COL36) ────────────────────────────────
            # Los 6 numeros de la pagina de elegibilidad de Binance, cargados
            # a mano cuando Sebastian la mira. MISMA FILOSOFIA QUE
            # inventario_ancla: el monitor estima en vivo, pero la verdad la
            # fija el ancla y el drift entre ambas mide el error.
            # Por que hace falta: Binance NO publica el volumen propio por API
            # publica. El contador de ORDENES si se lee del libro (exacto), el
            # volumen hay que estimarlo — y esta tabla es lo que permite
            # calibrar esa estimacion contra el numero real.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS merchant_ancla (
                    id SERIAL PRIMARY KEY,
                    ts TIMESTAMP NOT NULL,
                    ordenes_total INTEGER,
                    ordenes_30d INTEGER,
                    vol_total_btc NUMERIC,
                    vol_30d_btc NUMERIC,
                    tasa_finalizacion NUMERIC,
                    dias_verificado INTEGER,
                    btc_usd NUMERIC,
                    nota TEXT
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_merch_ancla_ts ON merchant_ancla(ts DESC)")
            # ── VOLUMEN DE MERCADO, historico diario (COL39) ─────────────
            # fills_estimados ya calcula esto (lo usa /api/volumen_v2), pero
            # esa tabla se purga a los 30 dias (purgar_fills_antiguos) y nunca
            # queda una SERIE para mirar la tendencia. Esta tabla congela 1
            # fila por (fecha, exchange) -- el MISMO calculo, solo que
            # persistido antes de que se recicle el detalle. Costo real:
            # ~2 filas/dia para siempre, nada que ver con los limites del
            # servidor que preocupan al ir a 2min o a 10s en el libro vivo.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS volumen_mercado_dia (
                    fecha DATE NOT NULL,
                    exchange TEXT NOT NULL,
                    volumen_usdt NUMERIC,
                    ordenes INTEGER,
                    pct_enmascarado NUMERIC,
                    presion_compra_pct NUMERIC,
                    anunciantes_activos INTEGER,
                    PRIMARY KEY (fecha, exchange)
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_volmerc_fecha ON volumen_mercado_dia(fecha)")
            # Movimientos MANUALES (taker / externo). Los 'maker' NO se guardan
            # aca: se derivan en vivo de fills_estimados para que no haya doble
            # conteo ni drift si un fill se corrige despues.
            # El tipo importa porque son economicamente distintos:
            #   maker  -> trade, comision 0,20% en USDT, cuenta P&L y reputacion
            #   taker  -> trade, comision 0,07 USDT fija, cuenta para los 300
            #   externo-> deposito/retiro: NO es P&L, solo mueve el saldo
            cur.execute("""
                CREATE TABLE IF NOT EXISTS movimientos_inventario (
                    id SERIAL PRIMARY KEY,
                    ts TIMESTAMP NOT NULL,
                    tipo TEXT NOT NULL,
                    lado TEXT,
                    usdt NUMERIC,
                    clp NUMERIC,
                    precio NUMERIC,
                    nota TEXT,
                    creado TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mov_inv_ts ON movimientos_inventario(ts)")
            # ── RUTINAS (COL25): mantenimiento recurrente ────────────────
            # Registro de cuando se hizo por ultima vez cada tarea periodica.
            # Va en la DB y NO en localStorage a proposito: el backup viejo se
            # guardaba en el navegador, asi que al abrir desde el telefono
            # creia que nunca se habia hecho. En DB el estado es unico y real,
            # se mire desde donde se mire.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS rutinas_log (
                    id SERIAL PRIMARY KEY,
                    tarea TEXT NOT NULL,
                    ts TIMESTAMP NOT NULL,
                    nota TEXT
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_rutinas_tarea ON rutinas_log(tarea, ts DESC)")
            # ── PERFIL POR BANDA DE PRECIO (COL27) ───────────────────────
            # Cuanto se consume por banda de % sobre el mejor precio del libro.
            # Alimenta al modulo "Ciclo de recompra": saber si el precio de venta
            # sugerido cae en una zona con flujo real o en una zona muerta.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS perfil_banda (
                    banda TEXT PRIMARY KEY,
                    pct_desde NUMERIC,
                    pct_hasta NUMERIC,
                    consumo_dia NUMERIC,
                    competidores NUMERIC,
                    capturable_dia NUMERIC,
                    actualizado TIMESTAMP
                )
            """)
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


def recalibrar_mi_ticket():
    """TICKET PROPIO (COL24), medido de mis_ordenes_reales (el CSV real que
    Sebastian importa) en vez del generico del mercado.

    Por que hace falta ademas de recalibrar_tickets(): el ticket por anunciante
    que ya calcula el FillTracker (st["tickets"]) vive SOLO en memoria del
    proceso, asi que se resetea con cada redeploy. En un ciclo de desarrollo
    activo (COL16->COL23 en pocos dias) eso paso seguido, y el ticket de MI
    cuenta nunca llegaba a las 3 muestras necesarias antes del siguiente reset
    -> siempre caia al generico del MERCADO (~400 USDT), 6x mas grande que mi
    ticket real (~69 USDT, medido). Esto INFLABA mis fills 'enmascarado' ~150%.

    mis_ordenes_reales SI persiste (viene del CSV), asi que sirve de ancla
    estable: sobrevive a cualquier redeploy. Se usa solo como FALLBACK cuando
    el historial en memoria todavia no tiene datos (recien reiniciado)."""
    with config_lock:
        nick = str(config.get("MI_NICKNAME") or "").strip()
    if not nick:
        return {}
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY usdt) AS mediana,
                           COUNT(*) AS n
                    FROM mis_ordenes_reales
                    WHERE rol = 'maker' AND estado = 'completada' AND usdt > 0
                """)
                r = cur.fetchone()
    except Exception as e:
        print(f"[MI_TICKET] {e}")
        return {}
    if not r or not r["n"] or int(r["n"]) < 5 or r["mediana"] is None:
        print(f"[MI_TICKET] muestras insuficientes ({r['n'] if r else 0}<5), se mantiene el valor actual")
        return {}
    val = round(float(r["mediana"]), 1)
    with config_lock:
        anterior = config.get("MI_TICKET_MEDIO")
        config["MI_TICKET_MEDIO"] = val
    print(f"[MI_TICKET] auto-calibrado: {anterior} -> {val} USDT (mediana de {r['n']} órdenes reales)")
    guardar_config_db({"MI_TICKET_MEDIO": val})
    return {"MI_TICKET_MEDIO": val}


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


#  Bandas fijas del perfil (en % sobre el mejor precio real del libro).
#  Son las mismas del pliego, para poder comparar contra los valores medidos.
BANDAS_CICLO = [
    ("0.0-0.1", 0.0, 0.1), ("0.1-0.2", 0.1, 0.2), ("0.2-0.3", 0.2, 0.3),
    ("0.3-0.4", 0.3, 0.4), ("0.4-0.5", 0.4, 0.5), ("0.5-0.6", 0.5, 0.6),
    ("0.6-0.8", 0.6, 0.8), ("0.8-1.0", 0.8, 1.0), ("1.0-1.5", 1.0, 1.5),
    ("1.5-2.0", 1.5, 2.0), ("2.0-3.0", 2.0, 3.0),
]


def _banda_de(pct):
    """Devuelve el nombre de la banda que contiene ese % sobre el top."""
    for nombre, desde, hasta in BANDAS_CICLO:
        if desde <= pct < hasta:
            return nombre
    return None


def recalibrar_bandas():
    """FLUJO POR BANDA DE PRECIO (COL27): cuanto se consume, por dia, en cada
    banda de % sobre el mejor precio del libro.

    Sirve para saber si el precio de venta que sugiere el modulo Ciclo cae en
    una zona con flujo real o en una zona muerta -- que es la diferencia entre
    llenar 7 veces al dia o 1.

    METODO ANTI-REPOSICIONAMIENTO (critico): la caida de `disponible` cuenta
    como consumo SOLO si el precio del anunciante NO cambio en ese paso. Si
    cambio el precio, la caida es una EDICION del anuncio (lo reposiciono), no
    una venta. Sin este filtro el volumen se infla ~45%.

    Ademas:
    - Solo anuncios REALES (mismos filtros que el resto del monitor).
    - Solo el tab BUY (es donde se recompra en la estrategia de gotas).
    - Se agrupa por (fecha, anunciante) y se divide por dias para el diario, y
      por competidores simultaneos para lo capturable por uno."""
    with config_lock:
        min_usdt = float(config.get("FILTRO_MIN_USDT", 200))
        min_tasa = float(config.get("FILTRO_MIN_TASA", 90))
    filas = []
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SET LOCAL statement_timeout = '90s'")
                cur.execute("""
                    WITH real AS (
                        -- anuncios reales del tab BUY, con el mejor precio de su snapshot
                        SELECT snapshot_timestamp AS ts, anunciante, precio, disponible,
                               MIN(precio) OVER (PARTITION BY snapshot_timestamp) AS mejor
                        FROM snapshots_detalle
                        WHERE tipo = 'BUY'
                          AND snapshot_timestamp >= NOW() - INTERVAL '7 days'
                          AND disponible >= %(mu)s AND tasa_exito >= %(mt)s
                          AND precio > 0
                    ), pasos AS (
                        -- comparar cada anuncio contra su propio paso anterior
                        SELECT ts, anunciante, precio, mejor, disponible,
                               LAG(disponible) OVER w AS disp_prev,
                               LAG(precio)     OVER w AS precio_prev,
                               EXTRACT(EPOCH FROM (ts - LAG(ts) OVER w))/60 AS gap_min
                        FROM real
                        WINDOW w AS (PARTITION BY anunciante ORDER BY ts)
                    ), consumo AS (
                        SELECT ts, ts::date AS dia, anunciante,
                               (precio - mejor) / NULLIF(mejor,0) * 100 AS pct_top,
                               -- ANTI-REPOSICIONAMIENTO: solo si el precio no cambio
                               CASE WHEN precio_prev = precio
                                     AND disp_prev > disponible
                                     AND gap_min BETWEEN 0 AND 10
                                    THEN disp_prev - disponible ELSE 0 END AS consumido
                        FROM pasos
                        WHERE disp_prev IS NOT NULL
                    ), clasificado AS (
                        SELECT ts, dia, anunciante, consumido,
                            CASE
                              WHEN pct_top >= 0.0 AND pct_top < 0.1 THEN '0.0-0.1'
                              WHEN pct_top < 0.2 THEN '0.1-0.2'
                              WHEN pct_top < 0.3 THEN '0.2-0.3'
                              WHEN pct_top < 0.4 THEN '0.3-0.4'
                              WHEN pct_top < 0.5 THEN '0.4-0.5'
                              WHEN pct_top < 0.6 THEN '0.5-0.6'
                              WHEN pct_top < 0.8 THEN '0.6-0.8'
                              WHEN pct_top < 1.0 THEN '0.8-1.0'
                              WHEN pct_top < 1.5 THEN '1.0-1.5'
                              WHEN pct_top < 2.0 THEN '1.5-2.0'
                              WHEN pct_top < 3.0 THEN '2.0-3.0'
                            END AS banda
                        FROM consumo
                        WHERE pct_top >= 0 AND pct_top < 3.0
                    ), por_snapshot AS (
                        -- competidores SIMULTANEOS: cuantos anuncios coexisten en la
                        -- banda EN CADA snapshot. Contar anunciantes distintos de toda
                        -- la ventana daria ~35 (todo el que paso alguna vez), no los
                        -- ~3-7 que de verdad compiten a la vez por ese flujo.
                        SELECT banda, ts, COUNT(DISTINCT anunciante) AS n
                        FROM clasificado GROUP BY 1, 2
                    )
                    SELECT c.banda,
                           SUM(c.consumido) AS consumo_total,
                           COUNT(DISTINCT c.dia) AS dias,
                           (SELECT AVG(n) FROM por_snapshot p WHERE p.banda = c.banda) AS competidores
                    FROM clasificado c
                    GROUP BY c.banda
                    HAVING SUM(c.consumido) > 0
                """, {"mu": min_usdt, "mt": min_tasa})
                filas = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f"[BANDAS] {e}")
        return None
    if not filas:
        print("[BANDAS] sin datos suficientes, se mantiene lo anterior")
        return None
    now = datetime.now(SANTIAGO_TZ).strftime("%Y-%m-%d %H:%M:%S")
    rangos = {n: (d, h) for n, d, h in BANDAS_CICLO}
    out = []
    for f in filas:
        b = f["banda"]
        if not b or b not in rangos:
            continue
        dias = max(1, int(f["dias"] or 1))
        consumo_dia = float(f["consumo_total"] or 0) / dias
        comp = float(f["competidores"] or 0) or 1.0
        out.append((b, rangos[b][0], rangos[b][1], round(consumo_dia, 1),
                    round(comp, 2), round(consumo_dia / comp, 1), now))
    if not out:
        return None
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.executemany("""
                    INSERT INTO perfil_banda
                        (banda, pct_desde, pct_hasta, consumo_dia, competidores,
                         capturable_dia, actualizado)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (banda) DO UPDATE SET
                        pct_desde=EXCLUDED.pct_desde, pct_hasta=EXCLUDED.pct_hasta,
                        consumo_dia=EXCLUDED.consumo_dia,
                        competidores=EXCLUDED.competidores,
                        capturable_dia=EXCLUDED.capturable_dia,
                        actualizado=EXCLUDED.actualizado
                """, out)
            conn.commit()
    except Exception as e:
        print(f"[BANDAS guardar] {e}")
        return None
    total = sum(x[3] for x in out)
    print(f"[BANDAS] {len(out)} bandas · consumo total {total:,.0f} USDT/dia")
    return out


def guardar_agregados_dia(fecha=None):
    """Congela el resumen diario ANTES de que la purga recicle el detalle top-80.
    Sin esto perdemos la historia: snapshots_detalle solo guarda DETALLE_DIAS
    (10 por defecto), asi que todo analisis de competidores quedaria limitado a
    esa ventana movil.

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
                             ordenes_dia, es_merchant,
                             min_orden_med, max_orden_med, min_orden_moda)
                        SELECT snapshot_timestamp::date, %(ex)s, anunciante, tipo,
                               COUNT(*), ROUND(AVG(posicion)::numeric, 1),
                               MIN(posicion), ROUND(AVG(precio)::numeric, 2),
                               ROUND(AVG(disponible)::numeric, 1),
                               MIN(completadas), MAX(completadas),
                               GREATEST(MAX(completadas) - MIN(completadas), 0),
                               BOOL_OR(es_merchant),
                               -- limites de orden (COL32). AVG y mode() ignoran
                               -- los NULL y dan NULL si TODO es NULL, asi que los
                               -- dias anteriores al deploy quedan limpios en vez
                               -- de en 0 (verificado contra la DB antes de usarlo).
                               ROUND(AVG(min_orden)::numeric)::int,
                               ROUND(AVG(max_orden)::numeric)::int,
                               mode() WITHIN GROUP (ORDER BY min_orden)
                        FROM {tabla}
                        WHERE snapshot_timestamp::date = %(f)s
                          AND anunciante IS NOT NULL AND anunciante <> ''
                        GROUP BY 1,3,4
                        ON CONFLICT (fecha, exchange, anunciante, tipo) DO UPDATE SET
                            apariciones = EXCLUDED.apariciones, pos_media = EXCLUDED.pos_media,
                            pos_min = EXCLUDED.pos_min, precio_medio = EXCLUDED.precio_medio,
                            disp_medio = EXCLUDED.disp_medio, comp_min = EXCLUDED.comp_min,
                            comp_max = EXCLUDED.comp_max, ordenes_dia = EXCLUDED.ordenes_dia,
                            es_merchant = EXCLUDED.es_merchant,
                            min_orden_med = EXCLUDED.min_orden_med,
                            max_orden_med = EXCLUDED.max_orden_med,
                            min_orden_moda = EXCLUDED.min_orden_moda
                    """, {"ex": ex, "f": fecha or (datetime.now(SANTIAGO_TZ).date() - timedelta(days=1))})
                    total += cur.rowcount

                # ── ticket propio de cada anunciante (COL31) ──────────────
                # OJO: se calcula SOLO con fills 'directo'. Los 'enmascarado'
                # ya se estimaron multiplicando POR un ticket, asi que usarlos
                # aca seria circular (el estimador comiendose su propia cola).
                # monto/ordenes replica exactamente lo que el FillTracker mete
                # en st["tickets"] (d_disp/resid), incluido el tope de 5000 que
                # descarta outliers, y el minimo de 3 muestras de _ticket().
                cur.execute("""
                    UPDATE agregados_anunciante_dia a
                    SET ticket_medio = t.tk
                    FROM (
                        SELECT exchange, anunciante, tipo,
                               PERCENTILE_CONT(0.5) WITHIN GROUP (
                                   ORDER BY monto / NULLIF(ordenes, 0)) AS tk
                        FROM fills_estimados
                        WHERE ts::date = %(f)s AND metodo = 'directo'
                          AND ordenes > 0 AND monto > 0 AND monto < 5000
                        GROUP BY 1, 2, 3
                        HAVING COUNT(*) >= 3
                    ) t
                    WHERE a.fecha = %(f)s AND a.exchange = t.exchange
                      AND a.anunciante = t.anunciante AND a.tipo = t.tipo
                """, {"f": fecha or (datetime.now(SANTIAGO_TZ).date() - timedelta(days=1))})
            conn.commit()
        if total:
            print(f"[AGREGADOS] {total:,} filas anunciante/dia congeladas")
    except Exception as e:
        print(f"[AGREGADOS] {e}")
    return total


def guardar_volumen_mercado_dia(fecha=None):
    """Congela el volumen TOTAL de mercado (todos los anunciantes, no solo
    Sebastian) antes de que purgar_fills_antiguos() recicle fills_estimados
    a los 30 dias. Es el MISMO calculo que ya usa /api/volumen_v2 (SUM(monto)
    agrupado), asi que el numero de hoy en el dashboard y la fila que queda
    grabada siempre van a coincidir -- no es una segunda formula.

    Por que vale la pena (COL39, pedido de Sebastian): es la unica forma de
    ver la TENDENCIA de cuanta plata mueve el mercado, en vez de un numero
    instantaneo que se pierde al otro dia. Costo: 1-2 filas por dia para
    siempre (una por exchange), nada comparado con snapshots_detalle."""
    f = fecha or (datetime.now(SANTIAGO_TZ).date() - timedelta(days=1))
    total = 0
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO volumen_mercado_dia
                        (fecha, exchange, volumen_usdt, ordenes, pct_enmascarado,
                         presion_compra_pct, anunciantes_activos)
                    SELECT ts::date, exchange,
                           ROUND(SUM(monto)::numeric, 2),
                           SUM(ordenes)::int,
                           ROUND(100.0 * SUM(monto) FILTER (WHERE metodo = 'enmascarado')
                                 / NULLIF(SUM(monto), 0), 1),
                           ROUND(100.0 * SUM(monto) FILTER (WHERE tipo = 'BUY')
                                 / NULLIF(SUM(monto), 0), 1),
                           COUNT(DISTINCT anunciante)
                    FROM fills_estimados
                    WHERE ts::date = %(f)s
                    GROUP BY 1, 2
                    ON CONFLICT (fecha, exchange) DO UPDATE SET
                        volumen_usdt = EXCLUDED.volumen_usdt,
                        ordenes = EXCLUDED.ordenes,
                        pct_enmascarado = EXCLUDED.pct_enmascarado,
                        presion_compra_pct = EXCLUDED.presion_compra_pct,
                        anunciantes_activos = EXCLUDED.anunciantes_activos
                """, {"f": f})
                total = cur.rowcount
            conn.commit()
        if total:
            print(f"[VOLUMEN DIA] {f}: {total} filas (exchange) congeladas")
    except Exception as e:
        print(f"[VOLUMEN DIA] {e}")
    return total


def recalibrar_tickets_por_anunciante(dias=14):
    """Carga a memoria el ticket propio de CADA anunciante, desde el agregado
    permanente. Devuelve cuantos cargo.

    POR QUE EXISTE (COL31): el FillTracker ya aprende el ticket de cada
    anunciante en st["tickets"], pero eso vive SOLO en memoria del proceso y
    necesita 3 muestras para activarse. Cada redeploy lo borra, asi que en la
    practica casi nunca llega a usarse y todos los anunciantes terminan
    estimados con el ticket GENERICO del mercado. Medido: los tickets reales
    van de ~170 a ~2.900 USDT contra un generico de ~785, y el 32% del volumen
    se estima justamente asi. Es el mismo problema que en COL24 inflo MI
    volumen +154,6% -- aquel fix persistio solo MI ticket; este lo generaliza
    a todos, leyendo del agregado que ya es permanente.

    No pisa lo que el tracker aprende en vivo: solo mejora el FALLBACK (ver
    _ticket(), que sigue prefiriendo st["tickets"] cuando tiene 3+ muestras).

    DE DONDE LEE: de fills_estimados, no del agregado. Es a proposito — el
    agregado recien tiene el dato despues de la primera corrida diaria, asi
    que leer de ahi dejaria el arranque sin tickets por hasta 24h. Los fills
    se retienen 30 dias, de sobra para la ventana que se pide. La columna
    ticket_medio del agregado guarda lo mismo pero para SIEMPRE (analisis
    historico mas alla de esos 30 dias), no para este arranque."""
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT anunciante, tipo,
                           PERCENTILE_CONT(0.5) WITHIN GROUP (
                               ORDER BY monto / NULLIF(ordenes, 0)) AS tk
                    FROM fills_estimados
                    WHERE exchange = 'binance' AND metodo = 'directo'
                      AND ordenes > 0 AND monto > 0 AND monto < 5000
                      AND ts >= NOW() - (%s || ' days')::INTERVAL
                    GROUP BY 1, 2
                    HAVING COUNT(*) >= 3
                """, [dias])
                mapa = {(r["anunciante"], r["tipo"]): float(r["tk"])
                        for r in cur.fetchall() if r["tk"] and float(r["tk"]) > 0}
        fill_tracker.tk_previo = mapa
        if mapa:
            vals = sorted(mapa.values())
            print(f"[TICKET x ANUNCIANTE] {len(mapa)} cargados · "
                  f"mediana {vals[len(vals)//2]:,.0f} USDT")
        return len(mapa)
    except Exception as e:
        print(f"[TICKET x ANUNCIANTE] {e}")
        return 0


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


def _limites_adv(adv):
    """LIMITES DE ORDEN del anuncio (COL32): devuelve (min_orden, max_orden) en
    CLP entero, o None si el campo no viene.

    UN SOLO helper para los dos exchanges A PROPOSITO: cinco lugares del codigo
    parsean anuncios (guardado del detalle, parsear_y_filtrar, _items_binance,
    y sus dos equivalentes de Bybit). Si cada uno leyera los campos por su
    cuenta, el mismo anuncio podria mostrar limites distintos segun la vista.

    Nombres por exchange (verificados contra las APIs el 29-jul-2026):
      Binance -> minSingleTransAmount / maxSingleTransAmount   ('400000')
      Bybit   -> minAmount / maxAmount                          ('20000.00')
    Bybit los manda con decimales, de ahi el float() antes del int().

    NO se convierte a USDT: es un monto fiat fijo que el anunciante configuro;
    convertirlo lo haria variar con el precio. La conversion se hace al mostrar."""
    if not adv:
        return (None, None)

    def _n(*claves):
        for k in claves:
            v = adv.get(k)
            if v not in (None, "", 0, "0"):
                try:
                    n = int(float(v))
                    return n if n > 0 else None
                except (TypeError, ValueError):
                    continue
        return None

    return (_n("minSingleTransAmount", "minAmount"),
            _n("maxSingleTransAmount", "maxAmount"))


def guardar_detalle(timestamp, hora, anuncios_raw_compra, anuncios_raw_venta):
    """Guarda los top 80 anunciantes de cada lado SIN filtros de mínimos"""
    with config_lock:
        top  = config["TOP_ANUNCIOS"]
        band = config["BANDA_DETALLE_PCT"]
    _pb = lambda item: float((item.get("adv") or {}).get("price", 0) or 0)
    anuncios_raw_compra = _detalle_banda(anuncios_raw_compra, _pb, band)
    anuncios_raw_venta  = _detalle_banda(anuncios_raw_venta,  _pb, band)
    rows = []
    for lado, crudos in (("BUY", anuncios_raw_compra), ("SELL", anuncios_raw_venta)):
        for pos, item in enumerate(crudos[:top], 1):
            adv   = item.get("adv", {})
            trade = item.get("advertiser", {})
            mino, maxo = _limites_adv(adv)
            rows.append((
                timestamp, hora, lado, pos,
                trade.get("nickName", ""),
                float(adv.get("price", 0)),
                float(adv.get("tradableQuantity", 0)),
                int(trade.get("monthOrderCount", 0) or trade.get("tradeCount", 0) or 0),
                float(trade.get("monthFinishRate", trade.get("finishRate", 0)) or 0) * 100,
                bool(trade.get("userType") == "merchant"),
                mino, maxo,
            ))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO snapshots_detalle
                (snapshot_timestamp, hora, tipo, posicion, anunciante, precio,
                 disponible, completadas, tasa_exito, es_merchant,
                 min_orden, max_orden)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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

# ══════════════════════════════════════════════════════════════
#  LIBRO EN VIVO (COL38)
#  ------------------------------------------------------------
#  EL PROBLEMA QUE RESUELVE: para la estrategia de vender y recomprar
#  rapido, 2 minutos de latencia son una eternidad. Y eran DOS capas
#  de 2 min: el colector actualiza ultimo_estado cada 2 min, y el chip
#  del Ciclo consultaba cada 2 min -> hasta 4 min de dato viejo.
#
#  POR QUE UN HILO APARTE Y NO BAJAR INTERVALO_MIN: el colector hace
#  dos cosas con el mismo ciclo, mirar el mercado y GUARDAR historia.
#  Bajarlo a 10s multiplicaria por 12 el peso de la base (de 25 a
#  ~300 MB/dia) y reventaria los 500 MB en dos dias.
#  Este hilo NO ESCRIBE NADA: solo trae la cabeza del libro (una
#  pagina por lado) y la deja en memoria.
#
#  COSTO MEDIDO (2-ago-2026): la API responde en ~590 ms y aguanto 6
#  consultas seguidas sin cortar. A 10s son 2 req cada 10s = ~17.000
#  al dia = 0,2 por segundo. El colector ya hace ~5.800.
# ══════════════════════════════════════════════════════════════
ultimo_libro_vivo = {"BUY": [], "SELL": [], "ts": None}
libro_vivo_lock = threading.Lock()


def obtener_libro_vivo(tipo):
    """Solo la primera pagina (top-20) del lado pedido. Devuelve la lista
    cruda de Binance o None si falla — nunca levanta excepcion."""
    with config_lock:
        c = dict(config)
    try:
        r = requests.post(URL, json={
            "asset": c["MONEDA"], "fiat": c["FIAT"], "merchantCheck": False,
            "page": 1, "publisherType": None, "rows": 20, "tradeType": tipo,
        }, headers=HEADERS, timeout=8)
        r.raise_for_status()
        return r.json().get("data", []) or []
    except Exception as e:
        print(f"[LIBRO VIVO {tipo}] {e}")
        return None


def ciclo_libro_vivo():
    """Hilo propio. Igual que el macro: aislado, con try/except en todo,
    para que si la fuente falla no arrastre al colector ni a la app."""
    print("[LIBRO VIVO] Iniciando thread...")
    time.sleep(12)          # deja arrancar primero al colector principal
    while True:
        try:
            b = obtener_libro_vivo("BUY")
            s = obtener_libro_vivo("SELL")
            if b is not None or s is not None:
                with libro_vivo_lock:
                    if b is not None:
                        ultimo_libro_vivo["BUY"] = b
                    if s is not None:
                        ultimo_libro_vivo["SELL"] = s
                    ultimo_libro_vivo["ts"] = datetime.now(SANTIAGO_TZ)
        except Exception as e:
            print(f"[LIBRO VIVO ciclo] {e}")
        try:
            with config_lock:
                seg = int(config.get("LIBRO_VIVO_SEG", 10) or 10)
        except Exception:
            seg = 10
        time.sleep(max(5, seg))


def libro_vivo_como_detalle(tipo):
    """Convierte el libro vivo al MISMO formato que usa ultimo_estado
    (detalle_compra / detalle_venta), para que quien lo consuma no tenga
    que saber de donde vino. Devuelve (filas, edad_segundos) o (None, None)
    si no hay dato fresco."""
    with libro_vivo_lock:
        crudos = list(ultimo_libro_vivo.get(tipo) or [])
        ts = ultimo_libro_vivo.get("ts")
    if not crudos or not ts:
        return None, None
    edad = (datetime.now(SANTIAGO_TZ) - ts).total_seconds()
    filas = []
    for pos, item in enumerate(crudos, 1):
        adv = item.get("adv", {})
        tr = item.get("advertiser", {})
        mino, maxo = _limites_adv(adv)
        filas.append({
            "posicion": pos, "anunciante": tr.get("nickName", ""),
            "precio": float(adv.get("price", 0) or 0),
            "disponible": float(adv.get("tradableQuantity", 0) or 0),
            "completadas": int(tr.get("monthOrderCount", 0) or 0),
            "tasa_exito": round(float(tr.get("monthFinishRate", 0) or 0) * 100, 1),
            "es_merchant": tr.get("userType") == "merchant",
            "min_orden": mino, "max_orden": maxo,
            # metodos de pago: solo existe en el libro vivo, el detalle
            # guardado no los persiste (no valdria la pena el peso)
            "pagos": [m.get("identifier") for m in (adv.get("tradeMethods") or [])
                      if m.get("identifier")],
        })
    return filas, edad


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
        mino, maxo = _limites_adv(adv)
        resultado.append({
            "tipo":       tipo,
            "precio":     float(adv.get("price", 0)),
            "disponible": disponible,
            "anunciante": trade.get("nickName", ""),
            "min_orden":  mino,
            "max_orden":  maxo,
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
        mino, maxo = _limites_adv(adv)
        rows.append({
            "posicion":    pos,
            "anunciante":  nombre,
            "precio":      float(adv.get("price", 0)),
            "disponible":  disp,
            "completadas": int(trade.get("monthOrderCount", 0) or trade.get("tradeCount", 0) or 0),
            "tasa_exito":  round(float(trade.get("monthFinishRate", trade.get("finishRate", 0)) or 0) * 100, 1),
            "es_merchant": trade.get("userType") == "merchant",
            "velocidad":   velocidad,
            "min_orden":   mino,
            "max_orden":   maxo,
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
        # (anunciante, tipo) -> ticket medido en dias anteriores, leido del
        # agregado permanente al arrancar (COL31). Sobrevive redeploys; se usa
        # solo como FALLBACK, st["tickets"] de este proceso tiene prioridad.
        self.tk_previo = {}

    def _cfg(self):
        with config_lock:
            tk_key = "FILL_TICKET_DEF_BYBIT" if self.exchange == "bybit" else "FILL_TICKET_DEF"
            # ticket propio (COL24): solo aplica a MI cuenta y solo en Binance
            # (MI_NICKNAME es un nick de Binance P2P). Persiste en DB, asi que
            # sobrevive a los redeploys -- a diferencia de st["tickets"], que es
            # en memoria y se resetea con cada uno.
            mi_nick   = str(config.get("MI_NICKNAME") or "").strip().lower() if self.exchange == "binance" else ""
            mi_ticket = float(config.get("MI_TICKET_MEDIO", 0) or 0) if self.exchange == "binance" else 0.0
            return (float(config.get("FILL_CAP_USDT", 10000)),
                    float(config.get("FILL_VENTANA_MIN", 15)),
                    float(config.get(tk_key, config.get("FILL_TICKET_DEF", 272))),
                    mi_nick, mi_ticket)

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
        cap, ventana_min, ticket_def, mi_nick, mi_ticket = self._cfg()
        ts_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        vistos = set()
        seguros = []       # fills confirmados por evidencia real (stock/pendientes)
        masc_cand = []     # candidatos a enmascarado, se resuelven en el paso 2
        ordenes_por_stock = {}   # nombre -> ordenes explicadas por evidencia real (cuenta)
        # PRESUPUESTO DE ORDENES POR CUENTA (fix COL20). El contador es por
        # cuenta: si el anunciante opera en los dos lados, AMBOS ven el mismo
        # d_comp y cada uno atribuia esas ordenes -> se contaban dos veces.
        # Medido antes del fix: 73.340 ordenes contadas contra 49.162 reales
        # (149%). Ahora el delta de la cuenta es un presupuesto que los lados
        # CONSUMEN, no un numero que cada uno copia.
        presupuesto = {}

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
                # el delta pertenece a la CUENTA, no a este anuncio: se toma del
                # presupuesto comun para no contarlo dos veces en cuentas duales
                if nombre not in presupuesto:
                    presupuesto[nombre] = d_comp
                d_comp_cuenta = d_comp          # lo que subio el contador de la cuenta
                d_comp = min(d_comp, presupuesto[nombre])
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
                elif d_comp_cuenta > 0 and d_disp > 1:
                    # El contador de la cuenta SI subio, pero las ordenes ya se
                    # atribuyeron al otro lado. Esta caida de stock es real: se
                    # cuenta su VOLUMEN con ordenes=0, para no duplicar el conteo
                    # pero tampoco perder plata que efectivamente se movio.
                    monto, metodo, ordenes_expl = min(d_disp, cap), "directo", 0
                elif d_disp > 1:
                    st["pend"].append({"monto": min(d_disp, cap),
                                       "nivel_previo": nivel_previo, "ts": now_dt})
                if monto > 0 and metodo:
                    ordenes_por_stock[nombre] = ordenes_por_stock.get(nombre, 0) + ordenes_expl
                    presupuesto[nombre] = max(0, presupuesto[nombre] - ordenes_expl)
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
                # ticket propio (COL24): si es MI cuenta y ya tengo un ticket
                # medido de mis ordenes reales, ese es el default -- no el
                # generico del mercado. self._ticket() igual prioriza el
                # historial en memoria (st["tickets"]) si ya acumulo 3+
                # muestras en este proceso; esto solo mejora el FALLBACK.
                # COL31: el mismo criterio para CUALQUIER anunciante, usando el
                # ticket que ya se le midio en dias previos (tk_previo, leido
                # del agregado permanente). Orden de preferencia:
                #   st["tickets"] de este proceso > mi ticket / el suyo previo > generico
                es_mi_cuenta = mi_nick and mc["nombre"].strip().lower() == mi_nick
                if es_mi_cuenta and mi_ticket > 0:
                    default_ticket = mi_ticket
                else:
                    default_ticket = self.tk_previo.get(mc["key"]) or ticket_def
                monto = min(residual * self._ticket(mc["st"], default_ticket), cap)
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
        mino, maxo = _limites_adv(adv)
        out.append({
            "anunciante":  trade.get("nickName", ""),
            "precio":      float(adv.get("price", 0) or 0),
            "disponible":  float(adv.get("tradableQuantity", 0) or 0),
            "completadas": int(trade.get("monthOrderCount", 0) or trade.get("tradeCount", 0) or 0),
            "min_orden":   mino,
            "max_orden":   maxo,
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
    # Bybit manda los limites en la raiz del item (no en un sub-objeto 'adv')
    # y con decimales: minAmount='20000.00'. Verificado contra la API el
    # 29-jul-2026; _limites_adv() se encarga del float()->int().
    mino, maxo = _limites_adv(item)
    return {
        "anunciante":  item.get("nickName", ""),
        "precio":      float(item.get("price", 0) or 0),
        "disponible":  float(item.get("lastQuantity", item.get("quantity", 0)) or 0),
        "completadas": int(item.get("recentOrderNum", 0) or 0),
        "tasa_exito":  float(item.get("recentExecuteRate", 0) or 0),   # ya viene 0-100
        "es_merchant": item.get("userType", "PERSONAL") != "PERSONAL",
        "min_orden":   mino,
        "max_orden":   maxo,
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
        res.append({"tipo": tipo, "precio": f["precio"], "disponible": f["disponible"],
                    "anunciante": f["anunciante"],
                    "min_orden": f["min_orden"], "max_orden": f["max_orden"]})
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
                         f["disponible"], f["completadas"], f["tasa_exito"], f["es_merchant"],
                         f["min_orden"], f["max_orden"]))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO snapshots_detalle_bybit
                (snapshot_timestamp, hora, tipo, posicion, anunciante, precio,
                 disponible, completadas, tasa_exito, es_merchant,
                 min_orden, max_orden)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
                     "tasa_exito": round(f["tasa_exito"], 1), "es_merchant": f["es_merchant"], "velocidad": vel,
                     "min_orden": f["min_orden"], "max_orden": f["max_orden"]})
    prev_detalle_raw_bybit[tipo] = nuevo
    return rows

# ══════════════════════════════════════════════════════════════
#  CONTEXTO MACRO (COL35) — dolar forex, VIX, cobre
#  ------------------------------------------------------------
#  AISLADO A PROPOSITO. Esto es un "nice to have": si la fuente
#  externa se cae, cambia de formato o empieza a rate-limitear, el
#  monitor tiene que seguir funcionando igual. Por eso:
#   - vive en su PROPIO hilo (no toca el ciclo del colector P2P)
#   - toda llamada de red va con timeout corto y try/except
#   - el endpoint lee de MEMORIA, nunca dispara red en el request
#   - si falla, se conserva el ultimo valor bueno y se marca viejo
#  Nada de esto puede tumbar el colector ni la app.
# ══════════════════════════════════════════════════════════════
URL_YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/"
# un solo proveedor para las tres series (sin API key). Si algun dia
# hay que cambiarlo, se cambia aca y nada mas.
MACRO_SIMBOLOS = {"usdclp_forex": "CLP=X", "vix": "^VIX", "cobre": "HG=F",
                  # BTC (COL36): no es contexto de mercado, es una CONVERSION —
                  # las metas de Merchant estan en BTC y hay que pasarlas a USDT.
                  "btc_usd": "BTC-USD"}

ultimo_macro = {}          # cache en memoria, lo lee /api/macro
macro_lock = threading.Lock()


def _yahoo_precio(simbolo):
    """Ultimo precio de un simbolo. Devuelve None ante CUALQUIER problema
    (red, timeout, JSON raro, campo faltante): nunca levanta excepcion."""
    try:
        r = requests.get(URL_YAHOO + simbolo,
                         params={"interval": "1d", "range": "1d"},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
        r.raise_for_status()
        meta = (((r.json() or {}).get("chart") or {}).get("result") or [{}])[0].get("meta") or {}
        v = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose")
        if v is None:
            return None
        return {"valor": float(v),
                "previo": float(prev) if prev is not None else None}
    except Exception as e:
        print(f"[MACRO {simbolo}] {e}")
        return None


def obtener_macro():
    """Lee las tres series y las guarda en memoria + DB. Devuelve el dict.
    Si una fuente falla, esa queda en None y las demas se guardan igual."""
    now = datetime.now(SANTIAGO_TZ)
    datos = {"ts": now.strftime("%Y-%m-%d %H:%M:%S")}
    for clave, simbolo in MACRO_SIMBOLOS.items():
        d = _yahoo_precio(simbolo)
        datos[clave] = d["valor"] if d else None
        # variacion vs el cierre previo: es lo que de verdad interesa mirar
        # (que el VIX este en 16 no dice nada; que haya saltado 12% si).
        if d and d.get("previo"):
            datos[clave + "_var_pct"] = round((d["valor"] / d["previo"] - 1) * 100, 2)
        else:
            datos[clave + "_var_pct"] = None

    # brecha del P2P contra el dolar formal: el numero que Sebastian queria
    # tener a mano al anclar inventario.
    p2p = None
    try:
        p2p = _precio_mid()
    except Exception as e:
        print(f"[MACRO p2p] {e}")
    datos["p2p_ref"] = round(p2p, 2) if p2p else None
    fx = datos.get("usdclp_forex")
    datos["brecha_pct"] = (round((p2p / fx - 1) * 100, 3)
                           if (p2p and fx) else None)

    with macro_lock:
        ultimo_macro.clear()
        ultimo_macro.update(datos)

    # persistir: si TODO vino en None no vale la pena escribir una fila vacia
    if any(datos.get(k) is not None for k in MACRO_SIMBOLOS):
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""INSERT INTO snapshots_macro
                                   (ts, usdclp_forex, vix, cobre, p2p_ref, brecha_pct, btc_usd)
                                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                                (datos["ts"], datos["usdclp_forex"], datos["vix"],
                                 datos["cobre"], datos["p2p_ref"], datos["brecha_pct"],
                                 datos.get("btc_usd")))
                conn.commit()
        except Exception as e:
            print(f"[MACRO guarda] {e}")
    return datos


def backfill_macro(rango="6mo"):
    """Trae el HISTORICO horario de Yahoo y rellena snapshots_macro hacia
    atras. Se autolimita: solo escribe horas que todavia no tienen fila.

    POR QUE (COL45): el colector macro arranco el 31-jul, pero snapshots (P2P)
    tiene datos desde el 18-mar. Medir el retardo forex->P2P con 4 dias de
    macro cuando hay 4,5 MESES de P2P al lado era desperdiciar la mitad del
    experimento. Yahoo devuelve 1h de granularidad hasta 6 meses atras, que
    cubre todo el solapamiento.

    Lo que se rellena: usdclp_forex, vix, cobre, btc_usd (cada uno con su
    propia serie de Yahoo) + p2p_ref y brecha_pct calculados contra el
    promedio horario real de snapshots. Las horas sin dato de mercado (fin de
    semana, feriado) simplemente no existen en la respuesta de Yahoo y no se
    inventan."""
    insertadas = 0
    try:
        # ── 1. bajar cada serie a un dict {hora_chile: valor} ──
        series = {}
        for clave, simbolo in MACRO_SIMBOLOS.items():
            try:
                r = requests.get(URL_YAHOO + simbolo,
                                 params={"interval": "1h", "range": rango},
                                 headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
                r.raise_for_status()
                res = ((r.json() or {}).get("chart") or {}).get("result") or []
                if not res:
                    continue
                res = res[0]
                ts_list = res.get("timestamp") or []
                quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
                closes = quote.get("close") or []
                d = {}
                for t, c in zip(ts_list, closes):
                    if c is None:
                        continue
                    # fromtimestamp con tz convierte el epoch directo a hora
                    # Chile. Se usa SANTIAGO_TZ (ZoneInfo) y no un offset fijo
                    # -4 a proposito: Chile cambia de hora, y la DB guarda hora
                    # local naive — con offset fijo, marzo/abril quedarian
                    # corridos una hora contra los snapshots.
                    dt = datetime.fromtimestamp(t, tz=SANTIAGO_TZ)
                    d[dt.replace(minute=0, second=0, microsecond=0)] = float(c)
                series[clave] = d
                print(f"[MACRO backfill] {simbolo}: {len(d)} horas")
            except Exception as e:
                print(f"[MACRO backfill {simbolo}] {e}")

        if not series.get("usdclp_forex"):
            print("[MACRO backfill] sin forex, se aborta")
            return 0

        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # ── 2. horas que YA tienen fila: no se pisan ──
                cur.execute("SELECT DISTINCT date_trunc('hour', ts) h FROM snapshots_macro")
                ya = {r["h"] for r in cur.fetchall()}
                # ── 3. P2P promedio por hora, para p2p_ref y la brecha ──
                cur.execute("""
                    SELECT date_trunc('hour', timestamp) h,
                           AVG((precio_pond_tab_compra + precio_pond_tab_venta)/2) v
                    FROM snapshots
                    WHERE precio_pond_tab_compra > 0 AND precio_pond_tab_venta > 0
                    GROUP BY 1
                """)
                p2p_hora = {r["h"]: float(r["v"]) for r in cur.fetchall()}

                filas = []
                for hora, fx in sorted(series["usdclp_forex"].items()):
                    # comparar naive contra naive: la DB guarda hora Chile sin tz
                    hora_naive = hora.replace(tzinfo=None)
                    if hora_naive in ya:
                        continue
                    p2p = p2p_hora.get(hora_naive)
                    brecha = round((p2p / fx - 1) * 100, 3) if (p2p and fx) else None
                    filas.append((
                        hora_naive.strftime("%Y-%m-%d %H:%M:%S"), fx,
                        series.get("vix", {}).get(hora),
                        series.get("cobre", {}).get(hora),
                        round(p2p, 2) if p2p else None, brecha,
                        series.get("btc_usd", {}).get(hora),
                    ))
                if filas:
                    # execute_values y NO executemany: son ~1.500 filas y
                    # executemany manda una sentencia por fila — medido, tarda
                    # mas de un minuto contra la base remota. Con VALUES en
                    # lote es un solo viaje.
                    execute_values(cur, """INSERT INTO snapshots_macro
                                           (ts, usdclp_forex, vix, cobre, p2p_ref, brecha_pct, btc_usd)
                                           VALUES %s""", filas)
                    insertadas = len(filas)
            conn.commit()
        if insertadas:
            print(f"[MACRO backfill] {insertadas} horas historicas insertadas")
    except Exception as e:
        print(f"[MACRO backfill] {e}")
    return insertadas


def calcular_desfase():
    """LA SENAL OPERATIVA (COL35): cuanto se movio el dolar forex que el P2P
    TODAVIA no acompano.

    POR QUE EXISTE — medido el 31-jul-2026 sobre 233 pares de horas reales,
    cruzando snapshots (P2P) contra el USD/CLP horario de Yahoo:
        forex y P2P en la MISMA hora ............ +0,063  (nada)
        forex 1h ANTES  -> P2P despues .......... +0,461  <- el retardo
        forex 2h antes  -> P2P despues .......... +0,134  (se diluye)
        P2P antes -> forex despues (control) .... +0,035  (nada)
    El control invertido en ~0 es lo que descarta que sea casualidad o una
    relacion de ida y vuelta: el forex se mueve PRIMERO y el P2P lo sigue.

    OJO CON LA LECTURA: +0,461 dice que la DIRECCION suele acompanar, NO que
    el P2P vaya a moverse exactamente lo mismo ni en un plazo garantizado.
    Por eso el mensaje sugiere, no promete, y la magnitud va como referencia.

    Devuelve None si no hay serie suficiente (recien desplegado, fin de semana
    con el forex cerrado, o fuente caida)."""
    with config_lock:
        vent = int(config.get("MACRO_DESFASE_MIN", 75) or 75)
        umbral = float(config.get("MACRO_DESFASE_PCT", 0.15) or 0.15)
    now = datetime.now(SANTIAGO_TZ)
    # ZONA HORARIA: los ts se guardan en hora Chile NAIVE y la DB corre en UTC,
    # asi que NOW() de Postgres esta 4 h adelantado y una ventana corta como
    # esta daria CERO filas. Se pasa la fecha ya formateada (Manual §4.5).
    desde = (now - timedelta(minutes=vent)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # forex: primera y ultima lectura dentro de la ventana
                cur.execute("""
                    SELECT usdclp_forex v, ts FROM snapshots_macro
                    WHERE ts >= %s AND usdclp_forex IS NOT NULL
                    ORDER BY ts ASC LIMIT 1
                """, [desde])
                fx0 = cur.fetchone()
                cur.execute("""
                    SELECT usdclp_forex v, ts FROM snapshots_macro
                    WHERE usdclp_forex IS NOT NULL ORDER BY ts DESC LIMIT 1
                """)
                fx1 = cur.fetchone()
                # P2P: mismo criterio, sobre el ponderado medio del libro
                cur.execute("""
                    SELECT (precio_pond_tab_compra + precio_pond_tab_venta)/2 v
                    FROM snapshots
                    WHERE timestamp >= %s AND precio_pond_tab_compra > 0
                      AND precio_pond_tab_venta > 0
                    ORDER BY timestamp ASC LIMIT 1
                """, [desde])
                p0 = cur.fetchone()
                cur.execute("""
                    SELECT (precio_pond_tab_compra + precio_pond_tab_venta)/2 v
                    FROM snapshots
                    WHERE precio_pond_tab_compra > 0 AND precio_pond_tab_venta > 0
                    ORDER BY timestamp DESC LIMIT 1
                """)
                p1 = cur.fetchone()
    except Exception as e:
        print(f"[MACRO desfase] {e}")
        return None

    if not (fx0 and fx1 and p0 and p1):
        return None

    # ⚠️ FOREX CERRADO (COL37): el mercado formal no opera sabados, domingos ni
    # feriados. Yahoo sigue devolviendo el ULTIMO valor (el cierre del viernes),
    # asi que el monitor guarda el mismo numero una y otra vez.
    # VERIFICADO 2-ago-2026: usdclp_forex congelado en 930,47 desde el viernes,
    # mientras el P2P se movio 6,29 CLP el sabado y 5,29 el domingo.
    # Sin este corte la señal diria "el P2P quedo adelantado, vende" cada fin de
    # semana — pero el forex no se quedo atras, esta CERRADO. Seria una señal
    # inventada el 29% del tiempo.
    if float(fx0["v"]) == float(fx1["v"]):
        return {"ventana_min": vent, "fx_var_pct": 0.0,
                "p2p_var_pct": None, "pendiente_pct": None,
                "umbral_pct": umbral, "senal": None, "mensaje": None,
                "fuente_quieta": True,
                "nota": ("El dólar formal no se movió en toda la ventana: o el mercado "
                         "está cerrado (fin de semana o feriado) o realmente no hubo "
                         "cambio. Sin movimiento del forex no hay retardo que medir, "
                         "así que la señal se apaga en vez de inventar una lectura.")}
    try:
        fxa, fxb = float(fx0["v"]), float(fx1["v"])
        pa, pb = float(p0["v"]), float(p1["v"])
    except (TypeError, ValueError):
        return None
    if fxa <= 0 or pa <= 0:
        return None
    # si las dos lecturas del forex son la MISMA fila, no hay ventana que medir
    if fx0["ts"] == fx1["ts"]:
        return None

    fx_var = (fxb / fxa - 1) * 100
    p2p_var = (pb / pa - 1) * 100
    pendiente = fx_var - p2p_var          # lo que el P2P "debe" segun el forex

    señal, mensaje = None, None
    if abs(pendiente) >= umbral:
        if pendiente > 0:
            señal = "SUBE"
            mensaje = (f"El dólar formal subió {fx_var:+.2f}% en los últimos {vent} min y el "
                       f"P2P solo {p2p_var:+.2f}%: suele acompañar después. "
                       f"Conviene ESPERAR antes de vender, y si vas a recomprar, hacerlo ya.")
        else:
            señal = "BAJA"
            mensaje = (f"El dólar formal se movió {fx_var:+.2f}% y el P2P {p2p_var:+.2f}%: "
                       f"el P2P quedó adelantado. Suele corregir a la baja — "
                       f"buen momento para VENDER, y esperar para recomprar.")

    return {"ventana_min": vent,
            "fx_var_pct": round(fx_var, 3),
            "p2p_var_pct": round(p2p_var, 3),
            "pendiente_pct": round(pendiente, 3),
            "umbral_pct": umbral,
            "senal": señal, "mensaje": mensaje,
            "nota": ("Medido: el cambio del forex correlaciona +0,46 con el cambio del P2P "
                     "de la hora siguiente (control invertido: +0,03). Indica DIRECCION "
                     "probable, no magnitud garantizada.")}


def ciclo_colector_macro():
    """Hilo propio. El while/try esta armado para que NINGUN error pueda
    terminar el hilo ni propagarse al resto del proceso."""
    print("[MACRO] Iniciando thread...")
    # ESPERAR A QUE EL COLECTOR TENGA PRECIO (COL36). Con un sleep fijo de 20s
    # la PRIMERA lectura de cada arranque salia sin p2p_ref y por lo tanto sin
    # brecha — medido: fallaba en 4 de 45 filas, y las 4 eran reinicios.
    # Se espera hasta 3 min a que ultimo_estado tenga datos; si no llega, se
    # arranca igual (la fila queda sin brecha pero el dolar/VIX/cobre sirven).
    for _ in range(18):
        time.sleep(10)
        try:
            with data_lock:
                if (ultimo_estado or {}).get("precio_pond_tab_compra"):
                    break
        except Exception:
            pass
    # BACKFILL HISTORICO (COL45), una sola vez: si la serie macro cubre menos
    # de 30 dias es que arranco hace poco (el colector macro es de COL35, muy
    # posterior a snapshots) y conviene traer los 6 meses que Yahoo ofrece en
    # 1h. Se autolimita solo: una vez rellenado, el rango supera los 30 dias y
    # no vuelve a correr. Va DESPUES de la espera del precio para que p2p_ref
    # y la brecha se puedan calcular contra snapshots.
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT MIN(ts), MAX(ts) FROM snapshots_macro")
                r = cur.fetchone()
        dias_cubiertos = (r[1] - r[0]).days if (r and r[0] and r[1]) else 0
        if dias_cubiertos < 30:
            print(f"[MACRO] serie de solo {dias_cubiertos} dias -> backfill historico")
            backfill_macro("6mo")
    except Exception as e:
        print(f"[MACRO backfill arranque] {e}")
    while True:
        try:
            d = obtener_macro()
            print(f"[MACRO] dolar {d.get('usdclp_forex')} · VIX {d.get('vix')} · "
                  f"cobre {d.get('cobre')} · brecha {d.get('brecha_pct')}%")
        except Exception as e:
            print(f"[MACRO ciclo] {e}")
        try:
            with config_lock:
                minutos = int(config.get("MACRO_MIN", 15) or 15)
        except Exception:
            minutos = 15
        time.sleep(max(60, minutos * 60))


def ciclo_colector_bybit():
    print("[BYBIT] Iniciando thread...")
    time.sleep(8)
    _ultima_purga = None
    while True:
        try:
            hoy = datetime.now(SANTIAGO_TZ).date()
            if _ultima_purga != hoy:
                try:
                    # COL32: respeta DETALLE_DIAS igual que la purga de Binance.
                    # Estaba en 7 fijo, asi que al subir la retencion en COL31
                    # las dos tablas quedaban con ventanas distintas.
                    with config_lock:
                        _dd = int(config.get("DETALLE_DIAS", 10) or 10)
                    with get_conn() as conn:
                        with conn.cursor() as cur:
                            cur.execute("DELETE FROM snapshots_detalle_bybit "
                                        "WHERE snapshot_timestamp < NOW() - (%s || ' days')::INTERVAL", [_dd])
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

def _frase_min_op(gan, min_op):
    """Frase base del spread vs el minimo. Si el preset Farming esta activo
    (min_op<0) y el spread real tambien es negativo, el veredicto OPERAR
    implica una perdida ACEPTADA a proposito (farmear ordenes hacia Merchant),
    no un error de calculo — hay que decirlo asi de explicito, si no un numero
    negativo en pantalla se lee como bug."""
    if min_op < 0 and gan < 0:
        return f"Farmeando a {gan}% de perdida controlada (tope {min_op}%) para sumar ordenes hacia Merchant"
    return f"Spread neto {gan}% sobre tu minimo ({min_op}%)"


def decidir_operativa(gan, min_op, ratio, presion, rot_lento, rot_dual, sesgo_min):
    """Arbol de decision del asistente — UNICA fuente (lo usan api_operativa y
    _registrar_operativa; antes estaba duplicado y podia desincronizarse).
    Devuelve (decision, color, razon).
    ratio None = sin datos de rotacion (tracker recien arrancado tras un
    reinicio): NO asumir mercado agil — degradar a paciente, no dar verde ciego."""
    if ratio is None:
        if gan >= min_op:
            return ("OPERAR DUAL (paciente)", "yellow",
                    f"{_frase_min_op(gan, min_op)}, pero todavia no hay datos de rotacion (colector recien iniciado): entra con paciencia")
        return ("ESPERAR", "red",
                f"Spread neto {gan}% bajo tu minimo ({min_op}%) y sin datos de rotacion aun — mejor conservar el capital")
    if gan >= min_op and ratio >= rot_dual:
        return ("OPERAR DUAL", "green",
                f"{_frase_min_op(gan, min_op)} y mercado rotando {ratio}x su promedio de 12h")
    if gan >= min_op and ratio >= rot_lento:
        return ("OPERAR DUAL (paciente)", "yellow",
                f"{_frase_min_op(gan, min_op)}, pero la rotacion esta en {ratio}x del promedio (umbral dual: {rot_dual}x): los fills tardaran mas de lo habitual")
    if gan >= min_op:
        return ("SOLO PIERNA CON FLUJO", "orange",
                f"{_frase_min_op(gan, min_op)} pero mercado lento ({ratio}x): no bloquees capital en dual; opera solo el lado que la presion favorece")
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
    # Backfill de volumen_mercado_dia (COL39): si la tabla esta vacia (deploy
    # nuevo o tabla recien creada), reconstruye desde lo que fills_estimados
    # todavia tiene en disco (~21-30 dias) en vez de arrancar el historial
    # desde cero. Se autolimita: una vez que hay filas, no vuelve a correr.
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM volumen_mercado_dia")
                if cur.fetchone()[0] == 0:
                    cur.execute("SELECT DISTINCT ts::date FROM fills_estimados ORDER BY 1")
                    dias_disp = [r[0] for r in cur.fetchall()]
        if dias_disp:
            for d in dias_disp:
                guardar_volumen_mercado_dia(d)
            print(f"[VOLUMEN DIA] backfill: {len(dias_disp)} dias reconstruidos")
    except Exception as e:
        print(f"[VOLUMEN DIA backfill] {e}")
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
                try:
                    # mismo orden que arriba: congelar ANTES de que
                    # purgar_fills_antiguos() se coma fills_estimados.
                    guardar_volumen_mercado_dia(hoy - timedelta(days=1))
                    guardar_volumen_mercado_dia(hoy)
                except Exception as e:
                    print(f"[VOLUMEN DIA diario] {e}")
                with config_lock:
                    _det_dias = int(config.get("DETALLE_DIAS", 10) or 10)
                purgar_detalle_antiguo(dias=_det_dias)
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
                try:
                    recalibrar_mi_ticket()
                except Exception as e:
                    print(f"[MI_TICKET diario] {e}")
                try:
                    recalibrar_tickets_por_anunciante()
                except Exception as e:
                    print(f"[TICKET x ANUN diario] {e}")
                try:
                    recalibrar_bandas()
                except Exception as e:
                    print(f"[BANDAS diario] {e}")
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

/* ---------- Layout BETA (COL36) ----------
   El layout normal apila TODO en una columna (.view es flex column), por eso
   la primera pantalla se llena rapido y hay que bajar mucho. La beta usa una
   grilla de 12 columnas: las tarjetas conviven a lo ancho y entra mucha mas
   informacion arriba. Colapsa a una sola columna en pantallas chicas, que es
   donde el apilado si tiene sentido. */
.beta-grid { display: grid; grid-template-columns: repeat(12, minmax(0, 1fr));
             gap: var(--gap); align-items: start; }
.beta-grid > * { min-width: 0; }              /* evita desbordes de tablas */
.bc-4  { grid-column: span 4; }
.bc-5  { grid-column: span 5; }
.bc-6  { grid-column: span 6; }
.bc-7  { grid-column: span 7; }
.bc-8  { grid-column: span 8; }
.bc-12 { grid-column: span 12; }
@media (max-width: 1100px) {
  .bc-4, .bc-5, .bc-6, .bc-7, .bc-8 { grid-column: span 6; }
}
@media (max-width: 760px) {
  .bc-4, .bc-5, .bc-6, .bc-7, .bc-8 { grid-column: span 12; }
}
/* barra de precios compacta: reemplaza arriba a las dos tarjetas grandes de
   precio ponderado, que ocupaban media pantalla para 4 numeros */
.px-bar { display: flex; gap: 8px; flex-wrap: wrap; align-items: stretch; }
.px-item { flex: 1 1 118px; min-width: 108px; background: var(--bg-1);
           border: 1px solid var(--line-soft); border-radius: 10px; padding: 7px 11px; }
.px-lbl { font-size: 9.5px; color: var(--text-3); text-transform: uppercase; letter-spacing: .07em; }
.px-val { font-family: var(--mono); font-size: 17px; font-weight: 600; line-height: 1.25;
          font-variant-numeric: tabular-nums; }
.px-sub { font-size: 9.5px; color: var(--text-3); }

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

/* Layout beta (COL36): la ruta /beta reemplaza este false por true. Mismo
   codigo, otra disposicion — no es una copia paralela que haya que mantener. */
window.P2P_BETA = false;

/* POST autenticado: si el backend tiene APP_TOKEN (env var en Railway), los POST
   sensibles piden el header X-App-Token. Este helper lo agrega desde localStorage;
   ante un 401 lo pide UNA vez con prompt() y reintenta. Sin APP_TOKEN en el
   backend, funciona igual que un fetch comun. */
window.P2P_AUTH = {
  /* COL48: generalizado a cualquier metodo (PATCH/DELETE para corregir
     movimientos). 'post' se mantiene como atajo para no tocar los llamados
     que ya existian. El reintento pidiendo token vale para todos por igual. */
  req: function (metodo, url, body) {
    var mk = function (tk) {
      var h = { "Content-Type": "application/json" };
      if (tk) h["X-App-Token"] = tk;
      return fetch(url, { method: metodo, headers: h, body: body ? JSON.stringify(body) : undefined });
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
  },
  post: function (url, body) { return this.req("POST", url, body); }
};

</script>
<script>
/* ============================================================
   Unión Austral · P2P Monitor — utilidades compartidas del front
   fmtPrice/fmtNum/fmtPct/clasificar/applyFilters/FILTROS_DEFAULT/COLOR_TONE
   los usa la app real en vivo (filtros de Tiempo Real, formateo de precios,
   umbrales de semáforo). NO es un motor de datos simulados: el
   generador de precios falsos (createEngine y su fallback silencioso
   ante fallos del backend) se retiró en COL29 — si el fetch inicial
   falla, la pantalla debe decir "SIN DATOS EN VIVO", no inventar un
   número sin avisar.
   ============================================================ */
(function () {
  const fmtPrice = (n) => "$" + Number(n).toLocaleString("es-CL", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const fmtNum = (n) => Math.round(n).toLocaleString("es-CL");
  const fmtPct = (n) => Number(n).toFixed(2) + "%";

  const COMISION_BN = 0.002;     // 0.2% por lado
  const ALERTA_SPREAD = 0.8;     // umbral MUY APTO
  const SPREAD_MINIMO = 0.2;     // umbral APTO

  function clasificar(spread_pond_pct) {
    if (spread_pond_pct >= ALERTA_SPREAD) return { estado: "MUY APTO", color: "green" };
    if (spread_pond_pct >= SPREAD_MINIMO) return { estado: "APTO", color: "yellow" };
    if (spread_pond_pct >= 0) return { estado: "ESTRECHO", color: "orange" };
    return { estado: "NO APTO", color: "red" };
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

  window.P2P = { clasificar, applyFilters, FILTROS_DEFAULT, COLOR_TONE, fmtPrice, fmtNum, fmtPct, ALERTA_SPREAD, SPREAD_MINIMO };
})();

</script>
<script>
/* ============================================================
   Unión Austral · P2P Monitor — MOTOR EN VIVO
   Sondea tu API Flask real y emite snapshots con los campos del
   backend. Si la API falla, mantiene el último dato bueno y reintenta
   (el estado "SIN DATOS EN VIVO" ya lo muestra el header cuando el
   último snapshot envejece — no hay fallback a datos inventados).
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
    // momento en que ESTE cliente recibio el snapshot: sirve para hacer avanzar
    // en vivo la edad server-side (edad_seg) sin depender del reloj del equipo.
    s._recibido = Date.now();
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
        // si nunca cargo, NO inventamos data: se queda sin snap y el header
        // ya muestra "SIN DATOS EN VIVO" (ver ageSec/dead mas abajo) hasta
        // que el proximo intervalo o un evento de despertar reconecte.
        console.warn("[P2P live] no se pudo refrescar:", e.message);
      }
    }

    refresh("init");
    const id = setInterval(() => refresh("cycle"), pollMs);

    // AUTO-RECUPERACION (COL23): los navegadores CONGELAN los setInterval de las
    // pestañas en segundo plano (Brave/Chrome, y mas si el equipo se durmio). Al
    // volver a la pestaña, el timer puede tardar en re-disparar y la pagina se
    // ve "colgada" mostrando el ultimo dato viejo ("SIN DATOS EN VIVO / hace 1h")
    // aunque el backend este perfecto. Estos listeners fuerzan un refresh
    // INMEDIATO cuando la pestaña vuelve al frente o se recupera la red.
    const despertar = () => {
      if (!stopped && (typeof document === "undefined" || document.visibilityState === "visible")) {
        refresh("wake");
      }
    };
    if (typeof document !== "undefined") document.addEventListener("visibilitychange", despertar);
    if (typeof window !== "undefined") {
      window.addEventListener("focus", despertar);
      window.addEventListener("online", despertar);
    }

    return {
      get state() { return { snap, history, heatmap, count, vel, cycleStart, cycleMs: pollMs }; },
      subscribe(fn) { subs.add(fn); if (snap) fn({ snap, history, heatmap, count, vel, cycleStart, cycleMs: pollMs, type: "init" }); return () => subs.delete(fn); },
      forceCycle: () => refresh("cycle"),
      stop() {
        stopped = true; clearInterval(id);
        if (typeof document !== "undefined") document.removeEventListener("visibilitychange", despertar);
        if (typeof window !== "undefined") {
          window.removeEventListener("focus", despertar);
          window.removeEventListener("online", despertar);
        }
      },
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
  // EDAD del dato: preferimos la que calcula el SERVIDOR (edad_seg), que no
  // depende del reloj de este dispositivo. Se la aumenta con los segundos
  // transcurridos desde que llego el snapshot, para que el contador avance en
  // vivo sin volver a pedirle al server cada segundo. Fallback (dato viejo sin
  // edad_seg): la cuenta de antes con el reloj local.
  const recibido = snap._recibido || now;
  let ageSec;
  if (snap.edad_seg != null) {
    ageSec = snap.edad_seg + Math.max(0, (now - recibido) / 1000);
  } else {
    const ts = snap.timestamp ? Date.parse(String(snap.timestamp).replace(" ", "T")) : null;
    ageSec = ts ? Math.max(0, (now - ts) / 1000) : null;
  }
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
  /* COL54 — en /beta arranca CERRADO; en la vista principal queda como
     estaba (abierto). Son 302 px de configuracion ocupando lugar fijo en la
     pantalla que se mira todo el dia, para algo que se toca una vez cada
     tanto. Cerrado igual muestra los chips con los filtros vigentes: no se
     pierde saber COMO esta filtrado, solo deja de ocupar pantalla el COMO
     SE CAMBIA.
     Va atado a P2P_BETA porque Sebastian congelo la vista principal hasta
     que cada cambio este probado en beta. */
  const [open, setOpen] = uS(!window.P2P_BETA);
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
function TiempoReal({ snap, history, showOrderBook, filters, vel, sinGrafico }) {
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
        {/* sinGrafico (COL36): /beta NO lo muestra, la vista principal SI.
            Medido el 6-ago: son 365 px de los 1.895 que ocupa este bloque.
            NO se toca la vista principal — Sebastian la congelo hasta que
            los cambios esten probados en beta. */}
        {!sinGrafico && (
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
        )}
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
function BaseCompetidores({ onElegir }) {
  const B = (window.P2P_CONFIG && window.P2P_CONFIG.baseUrl) || "";
  const [d, setD] = React.useState(null);
  const [orden, setOrden] = React.useState({ col: "ordenes_dia", desc: true });
  const [q, setQ] = React.useState("");
  const [soloMerchant, setSoloMerchant] = React.useState(false);
  const [soloDual, setSoloDual] = React.useState(false);
  const [soloLibro, setSoloLibro] = React.useState(false);
  React.useEffect(() => {
    let stop = false;
    fetch(B + "/api/competidores").then(r => r.json())
      .then(j => { if (!stop) setD(j); }).catch(() => {});
    return () => { stop = true; };
  }, []);
  if (!d) return <div className="intel-loading">Cargando base de competidores…</div>;
  const fN = (v) => v == null ? "—" : Number(v).toLocaleString("es-CL");

  // COLS: [clave, etiqueta, ayuda, formateador]
  const COLS = [
    ["anunciante", "Anunciante", "Nickname en Binance P2P. ✦ = merchant verificado.", null],
    ["ordenes_dia", "Órd/día", "Órdenes completadas por día. Sale del contador OFICIAL de Binance: es el dato más confiable de la tabla.", fN],
    ["volumen_dia", "Vol/día", "USDT movidos por día. Se calcula como órdenes oficiales × ticket observado, porque el conteo directo se pierde las operaciones de quien recarga el stock al instante.", fN],
    ["ticket", "Ticket", "Tamaño típico de sus órdenes en USDT. Estimado.", fN],
    ["capital", "Capital", "USDT que tiene publicados en el libro (suma de ambos lados).", fN],
    ["giros_dia", "Giros/día", "Cuántas veces rota su capital por día = volumen diario ÷ capital. Mide qué tan intensamente lo trabaja.", (v) => v == null ? "—" : v],
    ["pos_media", "Pos. media", "Posición promedio en el libro. 1 = mejor precio.", (v) => v == null ? "—" : "#" + v],
    ["cobertura_h", "Cobertura", "En cuántas horas distintas del día apareció publicado. 24 = siempre presente.", (v) => v + "h"],
    ["deteccion_pct", "Detección", "Qué porcentaje de su volumen alcanzamos a ver directamente. Bajo (<30%) significa que recarga el stock al instante y se nos escapa: ahí el Vol/día es más incierto. Alto (>80%) = muy confiable.", (v) => v == null ? "—" : v + "%"],
    ["gap_propio", "Gap propio", "Su margen bruto AHORA: diferencia entre su precio de venta y el de compra. Sólo se puede calcular si está publicado en ambos lados en este momento.", (v) => v == null ? "—" : v + "%"],
    ["ganancia_mes_est", "Gan/mes est.", "Estimación gruesa: volumen × su gap actual − comisión. Asume que sostiene ese gap todo el mes, así que es optimista.", fN],
  ];

  let filas = (d.filas || []).filter(r =>
    (!q || r.anunciante.toLowerCase().indexOf(q.toLowerCase()) >= 0) &&
    (!soloMerchant || r.merchant) && (!soloDual || r.dual_ahora) && (!soloLibro || r.en_libro));
  filas = filas.slice().sort((a, b) => {
    const va = a[orden.col], vb = b[orden.col];
    if (va == null && vb == null) return 0;
    if (va == null) return 1;
    if (vb == null) return -1;
    if (typeof va === "string") return orden.desc ? vb.localeCompare(va) : va.localeCompare(vb);
    return orden.desc ? vb - va : va - vb;
  });
  const clic = (c) => setOrden(o => o.col === c ? { col: c, desc: !o.desc } : { col: c, desc: true });
  const chip = (activo, set, txt) => (
    <button onClick={() => set(v => !v)} className={"pr-btn" + (activo ? " on" : "")}>{txt}</button>
  );

  return (
    <section className="chart-card">
      <div className="card-head">
        <h3>Base de competidores</h3>
        <span className="card-sub">{d.total} anunciantes · últimos {d.dias} días · clic en cualquier columna para ordenar</span>
      </div>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", marginBottom: 12 }}>
        <input value={q} onChange={e => setQ(e.target.value)} placeholder="Buscar por nombre…"
          style={{ background: "var(--bg-2)", border: "1px solid var(--line)", color: "var(--text)",
                   padding: "7px 11px", borderRadius: 9, fontFamily: "var(--mono)", fontSize: 12.5, minWidth: 190 }} />
        {chip(soloMerchant, setSoloMerchant, "✦ solo verificados")}
        {chip(soloDual, setSoloDual, "solo duales ahora")}
        {chip(soloLibro, setSoloLibro, "solo en el libro ahora")}
        <span style={{ fontSize: 11.5, color: "var(--text-3)", marginLeft: "auto" }}>{filas.length} resultados</span>
      </div>
      <div className="intel-scroll" style={{ maxHeight: 460, overflowY: "auto" }}>
        <table className="intel-table">
          <thead><tr>{COLS.map(([c, lbl, ayuda]) => (
            <th key={c} title={ayuda} onClick={() => clic(c)}
              style={{ cursor: "pointer", whiteSpace: "nowrap", userSelect: "none",
                       color: orden.col === c ? "var(--accent)" : undefined,
                       position: "sticky", top: 0, background: "var(--bg-1)" }}>
              {lbl}{orden.col === c ? (orden.desc ? " ▼" : " ▲") : ""}
            </th>
          ))}</tr></thead>
          <tbody>{filas.map(r => (
            <tr key={r.anunciante} onClick={() => onElegir && onElegir(r.anunciante)}
              style={{ cursor: onElegir ? "pointer" : "default" }}
              title={onElegir ? "Ver ficha completa de " + r.anunciante : undefined}>
              {COLS.map(([c, lbl, ayuda, fmt], i) => (
                <td key={c} className={i ? "tnum" : undefined}
                  style={i === 0 ? { fontWeight: 600, whiteSpace: "nowrap" } : undefined}>
                  {i === 0
                    ? <>{r.merchant && <span className="merch">✦ </span>}{r.anunciante}
                        {r.dual_ahora && <span style={{ fontSize: 9, color: "var(--buy)" }}> dual</span>}</>
                    : (fmt ? fmt(r[c]) : r[c])}
                </td>
              ))}
            </tr>
          ))}</tbody>
        </table>
      </div>
      <div className="intel-explain">
        <b>Cuánto confiar en cada columna:</b> <b>Órd/día</b> sale del contador oficial de Binance — es dato duro. <b>Capital, posición y cobertura</b> se observan directo del libro. <b>Ticket</b> es estimado, y <b>Vol/día</b> se calcula como órdenes × ticket (el conteo directo subestima mucho a quienes recargan al instante). <b>Gap propio y Gan/mes</b> sólo aparecen si el anunciante está publicado en ambos lados ahora mismo, y la ganancia asume que sostiene ese gap todo el mes (optimista).<br/>
        <b>Cómo usarla:</b> ordená por <b>Giros/día</b> para encontrar a los que exprimen poco capital (el modelo más parecido al tuyo), o por <b>Gap propio</b> entre los duales para ver qué margen está pagando el mercado hoy. Clic en una fila abre su ficha completa.
      </div>
    </section>
  );
}

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

function VolumenMercado() {
  const B = (window.P2P_CONFIG && window.P2P_CONFIG.baseUrl) || "";
  const [dias, setDias] = React.useState(30);
  const [d, setD] = React.useState(null);
  React.useEffect(() => {
    let stop = false;
    setD(null);
    fetch(B + "/api/volumen_mercado?dias=" + dias).then(r => r.json())
      .then(j => { if (!stop) setD(j); }).catch(() => { if (!stop) setD({ serie: [] }); });
    return () => { stop = true; };
  }, [dias]);

  const fN = (v) => v == null ? "—" : Number(v).toLocaleString("es-CL");

  if (!d) return <div className="intel-loading">Cargando volumen de mercado…</div>;
  const serie = d.serie || [];
  const hoy = serie.length ? serie[serie.length - 1] : null;
  const prevs = serie.slice(0, -1);
  const promPrevio = prevs.length ? prevs.reduce((s, r) => s + (r.volumen_usdt || 0), 0) / prevs.length : null;
  const cambioPct = (hoy && hoy.volumen_usdt != null && promPrevio) ? Math.round((hoy.volumen_usdt / promPrevio - 1) * 1000) / 10 : null;

  return (
    <section className="chart-card">
      <div className="card-head">
        <h3>Volumen de mercado — cuánta plata se mueve</h3>
        <span className="card-sub">todos los anunciantes del top-80, no solo lo tuyo · Binance + Bybit · congelado a diario, no se pierde con la purga</span>
      </div>
      <div style={{ display: "flex", gap: 6, marginBottom: 10, alignItems: "center" }}>
        {[7, 14, 30, 60, 90].map(n => (
          <button key={n} className={"intel-tab" + (dias === n ? " active" : "")} onClick={() => setDias(n)}>{n}d</button>
        ))}
        <a href={B + "/api/volumen_mercado?dias=" + dias + "&fmt=csv"} download
           style={{ marginLeft: "auto", fontSize: 11, fontFamily: "var(--mono)", color: "var(--accent)",
                   textDecoration: "none", border: "1px solid var(--accent)", borderRadius: 7, padding: "3px 9px", whiteSpace: "nowrap" }}>
          ⬇ CSV
        </a>
      </div>

      {!serie.length && <div className="intel-loading">Todavía sin historial — se arma solo, un día a la vez.</div>}

      {serie.length > 0 && (
        <>
          {hoy && (
            <div style={{ fontSize: 12.5, color: "var(--text-2)", marginBottom: 8 }}>
              Hoy: <b style={{ color: "var(--text-1)" }}>{fN(hoy.volumen_usdt)} USDT</b> en {fN(hoy.ordenes)} órdenes
              {cambioPct != null && (
                <span style={{ color: cambioPct >= 0 ? "#35e07a" : "var(--warn)" }}>
                  {" "}({cambioPct >= 0 ? "+" : ""}{cambioPct}% vs. promedio de los {prevs.length} días previos)
                </span>
              )}
            </div>
          )}
          <div className="intel-scroll">
            <table className="intel-table">
              <thead><tr>
                <th>Fecha</th>
                <th title="Estimado desde las caídas de stock de los anuncios del top-80 (mismo método que ya usa el indicador de volumen en vivo). No es el 100% del mercado, es lo visible en el libro público.">Volumen (USDT)</th>
                <th title="Órdenes completadas contadas en las mismas caídas de stock.">Órdenes</th>
                <th title="Anunciantes distintos con al menos un fill ese día.">Anunciantes activos</th>
                <th title="% del volumen que fue ESTIMADO indirectamente (anunciante con dos avisos a la vez) en vez de observado directo. Más alto = número menos confiable ese día.">% estimado</th>
                <th title="Qué parte del volumen fue gente COMPRANDO USDT (tab Compra) vs. vendiendo. 50% = equilibrado.">Presión compra</th>
              </tr></thead>
              <tbody>{serie.slice().reverse().map(r => (
                <tr key={r.fecha}>
                  <td className="tnum">{r.fecha}</td>
                  <td className="tnum" style={{ fontWeight: 600 }}>{fN(r.volumen_usdt)}</td>
                  <td className="tnum">{fN(r.ordenes)}</td>
                  <td className="tnum">{fN(r.anunciantes_activos)}</td>
                  <td className="tnum" style={{ color: "var(--text-3)" }}>{r.pct_enmascarado != null ? r.pct_enmascarado + "%" : "—"}</td>
                  <td className="tnum" style={{ color: "var(--text-3)" }}>{r.presion_compra_pct != null ? r.presion_compra_pct + "%" : "—"}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        </>
      )}
      <div className="intel-explain">
        <b>Qué es:</b> la plata que se movió en TODO el libro top-80 ese día (no solo lo tuyo) — mismo cálculo que ya usa el indicador de volumen en vivo, pero guardado para poder mirar la tendencia en vez de solo el instante.<br/>
        <b>Límite honesto:</b> es una estimación desde caídas de stock públicas, no el número real de Binance. El "% estimado" te dice cuánto de ese día fue indirecto (anunciantes con 2+ avisos a la vez) — con eso más alto, confiá menos en el número de ese día puntual.
      </div>
    </section>
  );
}

function PnlCiclos() {
  const B = (window.P2P_CONFIG && window.P2P_CONFIG.baseUrl) || "";
  const [dias, setDias] = React.useState(0);   // 0 = historial completo
  const [d, setD] = React.useState(null);
  React.useEffect(() => {
    let stop = false;
    setD(null);
    const q = dias ? ("?dias=" + dias) : "";
    fetch(B + "/api/pnl_ciclos" + q).then(r => r.json())
      .then(j => { if (!stop) setD(j); }).catch(() => { if (!stop) setD({ ciclos: [] }); });
    return () => { stop = true; };
  }, [dias]);

  const fN = (v) => v == null ? "—" : Number(v).toLocaleString("es-CL");
  const box = (label, val, sub) => (
    <div style={{ flex: 1, minWidth: 170, background: "var(--bg-2)", border: "1px solid var(--line-soft)", borderRadius: 10, padding: "10px 13px" }}>
      <div style={{ fontSize: 10, color: "var(--text-3)", textTransform: "uppercase", letterSpacing: "0.08em" }}>{label}</div>
      <div style={{ fontFamily: "var(--mono)", fontSize: 19, margin: "3px 0 1px", fontVariantNumeric: "tabular-nums" }}>{val}</div>
      {sub && <div style={{ fontSize: 10.5, color: "var(--text-3)" }}>{sub}</div>}
    </div>
  );

  if (!d) return <div className="intel-loading">Calculando P&L real…</div>;
  if (d.configurado === false) return <div className="intel-loading">{d.nota}</div>;

  const r = d.resumen || {};
  const pos = (r.pnl_neto_total_clp || 0) >= 0;
  const pr = d.por_rol || {};

  return (
    <section className="chart-card">
      <div className="card-head">
        <h3>P&L real por ciclo — costo promedio ponderado</h3>
        <span className="card-sub">cada venta contra el costo real de compra, no un margen teórico · ya descuenta comisión</span>
      </div>
      <div style={{ display: "flex", gap: 6, marginBottom: 10, alignItems: "center" }}>
        {[0, 7, 30, 90].map(n => (
          <button key={n} className={"intel-tab" + (dias === n ? " active" : "")} onClick={() => setDias(n)}>{n === 0 ? "todo" : n + "d"}</button>
        ))}
        <a href={B + "/api/pnl_ciclos?fmt=csv" + (dias ? "&dias=" + dias : "")} download
           style={{ marginLeft: "auto", fontSize: 11, fontFamily: "var(--mono)", color: "var(--accent)",
                   textDecoration: "none", border: "1px solid var(--accent)", borderRadius: 7, padding: "3px 9px", whiteSpace: "nowrap" }}>
          ⬇ CSV
        </a>
      </div>

      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginBottom: 12 }}>
        {box("P&L neto", <span style={{ color: pos ? "#35e07a" : "var(--warn)" }}>{fN(r.pnl_neto_total_clp)} CLP</span>,
             fN(r.n_ciclos) + " ciclos · " + fN(r.pnl_medio_clp) + " CLP/ciclo")}
        {box("Tasa de acierto", (r.tasa_acierto_pct != null ? r.tasa_acierto_pct + "%" : "—"),
             fN(r.ganadores) + " / " + fN(r.n_ciclos) + " ciclos ganadores")}
        {d.sin_costo_base && d.sin_costo_base.n > 0 &&
          box("Sin costo base", fN(d.sin_costo_base.n) + " ventas", fN(d.sin_costo_base.usdt) + " USDT · fondeo externo probable, no entra al P&L")}
      </div>

      {(pr.maker || pr.taker) && (
        <div style={{ display: "flex", gap: 18, marginBottom: 14, fontSize: 11.5, color: "var(--text-2)", flexWrap: "wrap" }}>
          {["maker", "taker"].map(rol => pr[rol] && pr[rol].n_ciclos > 0 && (
            <span key={rol}>
              <b style={{ textTransform: "uppercase", color: "var(--text)" }}>{rol}</b>{": "}
              <span style={{ color: pr[rol].pnl_neto_total_clp >= 0 ? "#35e07a" : "var(--warn)" }}>{fN(pr[rol].pnl_neto_total_clp)} CLP</span>
              {" en " + fN(pr[rol].n_ciclos) + " ciclos (" + pr[rol].tasa_acierto_pct + "% acierto)"}
            </span>
          ))}
        </div>
      )}

      {(!d.ciclos || d.ciclos.length === 0) && <div className="intel-loading">Sin ciclos con costo base en este rango.</div>}

      {d.ciclos && d.ciclos.length > 0 && (
        <div className="intel-scroll">
          <table className="intel-table">
            <thead><tr>
              <th>Fecha y hora</th><th>Rol</th><th>USDT</th><th>Precio venta</th>
              <th title="El costo promedio ponderado con el que se compró ese USDT hasta ese momento.">Costo base</th>
              <th>P&L neto</th><th>%</th>
            </tr></thead>
            <tbody>{d.ciclos.map((c, i) => (
              <tr key={c.orden_id || i}>
                <td className="tnum">{c.ts}</td>
                <td>{c.rol}</td>
                <td className="tnum">{fN(c.usdt)}</td>
                <td className="tnum">${fN(c.precio_venta)}</td>
                <td className="tnum" style={{ color: "var(--text-3)" }}>${fN(c.costo_base_clp)}</td>
                <td className="tnum" style={{ fontWeight: 600, color: c.pnl_neto_clp >= 0 ? "#35e07a" : "var(--warn)" }}>{fN(c.pnl_neto_clp)}</td>
                <td className="tnum" style={{ color: "var(--text-3)" }}>{c.pct >= 0 ? "+" : ""}{c.pct}%</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
      <div className="intel-explain">
        <b>Cómo se calcula:</b> costo promedio ponderado sobre TODO tu historial de órdenes reales — cada compra actualiza el costo base, cada venta realiza P&L contra ese costo, menos la comisión de esa orden puntual. Es el mismo método que cualquier libro contable de inventario fungible, no una estimación del monitor.<br/>
        <b>Lo que NO se inventa:</b> si vendiste USDT que nunca compraste por P2P (probable que lo hayas fondeado por otro lado), esa venta queda en "sin costo base" y no entra al cálculo — inventarle un costo daría un número falso.
      </div>
    </section>
  );
}

function FichaAnunciante({ inicial }) {
  const B = (window.P2P_CONFIG && window.P2P_CONFIG.baseUrl) || "";
  const [q, setQ] = React.useState("");
  const [lista, setLista] = React.useState(null);
  const [sel, setSel] = React.useState(inicial || null);
  const [ficha, setFicha] = React.useState(null);
  // si llega uno elegido desde la Base de competidores, abrirlo
  React.useEffect(() => { if (inicial) setSel(inicial); }, [inicial]);
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
                {ficha.limites && (
                  <div style={boxSt} title="Mínimo por orden que exige (moda de los días medidos). Marca a qué segmento apunta: mínimo alto = mayorista, bajo = minorista.">
                    <div style={lbl}>Mínimo por orden</div>
                    <div style={val}>${fN(ficha.limites.min_habitual)}</div>
                    <div style={{ fontSize: 10, color: "var(--text-3)" }}>
                      CLP · {ficha.limites.min_visto_desde !== ficha.limites.min_visto_hasta
                        ? "varía $" + fN(ficha.limites.min_visto_desde) + "–" + fN(ficha.limites.min_visto_hasta)
                        : "estable"} · {ficha.limites.dias_con_dato}d
                    </div>
                  </div>
                )}
              </div>
              {ficha.en_libro_ahora && (
                <div style={{ fontSize: 11.5, color: "var(--text-2)", marginBottom: 10 }}>
                  {["venta", "compra"].map(k => ficha.posiciones[k] && (
                    <span key={k} style={{ marginRight: 14 }}>
                      <b style={{ color: k === "venta" ? "var(--buy)" : "var(--sell)" }}>{k === "venta" ? "vende" : "compra"}</b>
                      {" "}#{ficha.posiciones[k].posicion} a ${fN(ficha.posiciones[k].precio)} · {fN(ficha.posiciones[k].disponible)} USDT
                      {ficha.posiciones[k].min_orden != null && (
                        <span style={{ color: "var(--text-3)" }}>
                          {" "}· orden ${fN(ficha.posiciones[k].min_orden)}–{fN(ficha.posiciones[k].max_orden)}
                        </span>
                      )}
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

/* ============================================================
   CICLO DE RECOMPRA (COL27)
   Dado un monto, dice a que precio recomprar cruzando (VWAP real del libro)
   y a que precio publicar la venta para que quede el margen objetivo.
   Los dos precios van en grande: es lo unico que se copia para operar.
   ============================================================ */
const CICLO_TONO = { VERDE: "var(--buy)", AMBAR: "var(--warn)", ROJO: "var(--sell)" };

function CicloRecompra() {
  const B = (window.P2P_CONFIG && window.P2P_CONFIG.baseUrl) || "";
  const [monto, setMonto] = React.useState(1200);
  const [margen, setMargen] = React.useState(0.30);
  const [guardado, setGuardado] = React.useState({ monto: 1200, margen: 0.30 });
  const [d, setD] = React.useState(null);
  const [cargando, setCargando] = React.useState(true);
  const [msg, setMsg] = React.useState("");
  const [busy, setBusy] = React.useState(false);

  // COL28: arrancar con los valores GUARDADOS en config, no con los hardcodeados.
  // Asi el monto y el margen que dejaste fijados sobreviven al recargar la pagina
  // y son los mismos que usa el chip del Plan de Hoy (que lee /api/ciclo sin
  // parametros, o sea con el default de config).
  React.useEffect(() => {
    let stop = false;
    fetch(B + "/api/config").then(r => r.json()).then(c => {
      if (stop || !c) return;
      const m = c.CICLO_MONTO_DEFAULT, g = c.CICLO_MARGEN_OBJETIVO;
      if (m != null) { setMonto(m); }
      if (g != null) { setMargen(g); }
      setGuardado({ monto: m != null ? m : 1200, margen: g != null ? g : 0.30 });
    }).catch(() => {});
    return () => { stop = true; };
  }, []);

  React.useEffect(() => {
    let stop = false;
    const load = () => fetch(B + "/api/ciclo?monto=" + monto + "&margen=" + margen)
      .then(r => r.json())
      .then(j => { if (!stop) { setD(j); setCargando(false); } })
      .catch(() => { if (!stop) setCargando(false); });
    load();
    const id = setInterval(load, 30000);
    return () => { stop = true; clearInterval(id); };
  }, [monto, margen]);

  const nMonto = parseFloat(String(monto)) || 0;
  const nMargen = parseFloat(String(margen)) || 0;
  const sinGuardar = nMonto !== Number(guardado.monto) || nMargen !== Number(guardado.margen);

  const guardarDefaults = () => {
    if (busy) return;
    setBusy(true); setMsg("Guardando…");
    window.P2P_AUTH.post(B + "/api/config",
      { CICLO_MONTO_DEFAULT: nMonto, CICLO_MARGEN_OBJETIVO: nMargen })
      .then(r => r.json().then(j => ({ ok: r.ok && j && j.ok !== false })))
      .then(({ ok }) => {
        setBusy(false);
        if (!ok) { setMsg("✗ no se pudo guardar (¿token?)"); return; }
        setGuardado({ monto: nMonto, margen: nMargen });
        setMsg("✓ guardado como tu configuración");
        setTimeout(() => setMsg(""), 6000);
      })
      .catch(() => { setBusy(false); setMsg("✗ error de red"); });
  };

  const fN = (x, n) => x == null ? "—" : Number(x).toLocaleString("es-CL",
    { minimumFractionDigits: n || 0, maximumFractionDigits: n || 0 });
  if (cargando) return <div className="intel-loading">Calculando el ciclo…</div>;
  if (!d || d.error) return <div className="intel-loading">{(d && d.error) || "Sin datos del libro."}</div>;

  const tono = CICLO_TONO[d.veredicto] || "var(--text-3)";
  const r = d.recompra || {}, v = d.venta || {}, co = d.costos || {},
        fl = d.flujo || {}, g = d.ganancia || {};
  // COL44: los presets salen de las recompras REALES, no de numeros redondos.
  // La estrategia es vender de a poco e ir recomprando en tandas mientras
  // tanto — no esperar a vender todo para recomprar de una — asi que ofrecer
  // "tu saldo completo" sugeria una operacion que nunca ocurre. Medido sobre
  // sus taker de compra: p25=210 / mediana=235 / p75=320 / max 425.
  // COL56: el tope es cuanto se puede COMPRAR con los pesos disponibles.
  // Antes miraba el saldo en USDT, que es al reves: los dolares son lo que
  // se vende, no con lo que se recompra.
  const saldo = d.puede_recomprar_usdt;
  const cap = d.capacidad;      // COL57: cuánto podés recomprar ya / cuando vendas todo
  const t = d.tandas;
  const PRESETS = t
    ? [["chica", t.chica], ["habitual", t.habitual], ["grande", t.grande], ["máxima", t.maxima]]
        .filter(([, v]) => v > 0)
        // sin duplicados: si dos percentiles redondean al mismo numero, sobra uno
        .filter(([, v], i, arr) => arr.findIndex(([, w]) => w === v) === i)
    : [["", 200], ["", 300], ["", 400], ["", 600]];

  return (
    <section className="chart-card" style={{ marginBottom: 14 }}>
      <div className="card-head">
        <h3>Ciclo de recompra</h3>
        <span className="card-sub">cuánto cruzar → a qué precio recomprar y a cuál vender</span>
      </div>

      {/* controles */}
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "flex-end", marginBottom: 14 }}>
        <div>
          <div style={{ fontSize: 10.5, color: "var(--text-3)", marginBottom: 4 }}>
            Tanda a recomprar (USDT)
            {t && <span style={{ marginLeft: 6, color: "var(--text-3)" }}
                    title={"Salen de tus " + t.n + " recompras taker reales, no de números redondos."}>
              · según tus {t.n} recompras reales</span>}
          </div>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
            {PRESETS.map(([etq, p]) => (
              <button key={p} className={"pr-btn" + (Number(monto) === p ? " on" : "")}
                title={etq ? ("tu tanda " + etq) : ""}
                onClick={() => setMonto(p)}>{fN(p)}{etq && <span style={{ fontSize: 9, opacity: 0.7 }}> {etq}</span>}</button>
            ))}
            {/* clase 0-9 explicita: un backslash aca dispararia SyntaxWarning
                dentro del string de Python que contiene el DASHBOARD */}
            <input value={monto} onChange={e => setMonto(e.target.value.replace(/[^0-9.]/g, "") || 0)}
              inputMode="decimal"
              style={{ width: 90, background: "var(--bg-2)", border: "1px solid var(--line)",
                       borderRadius: 7, color: "var(--text)", fontFamily: "var(--mono)",
                       fontSize: 13, padding: "6px 9px" }} />
          </div>
          {/* COL57 — CUANTO PODES RECOMPRAR, en dos tiempos.
              Pedido de Sebastian: "puedo comprar solo 220 ahora, pero en
              cuanto me salgan unas ordenes voy a poder recomprar mas —
              no estoy viendo el precio que realmente podria recomprar".
              El de la izquierda es lo que puede hacer YA; el de la derecha
              es el techo de la jornada si le entran todas las ventas. */}
          {cap && (cap.ahora_usdt != null || cap.total_usdt != null) && (
            <div style={{ display: "flex", gap: 14, marginTop: 8, alignItems: "flex-end", flexWrap: "wrap" }}>
              <div title={"Con los " + fN(cap.clp_disponible) + " pesos que tenés ahora, al precio de barrer el libro ($" + fN(cap.precio_usado, 2) + ")."}>
                <div style={{ fontSize: 11, color: "var(--text-3)", textTransform: "uppercase", letterSpacing: "0.08em" }}>Podés recomprar ya</div>
                <div style={{ fontFamily: "var(--mono)", fontSize: 17, color: "var(--text)" }}>
                  {fN(cap.ahora_usdt, 0)} <span style={{ fontSize: 11, color: "var(--text-3)" }}>USDT</span>
                </div>
              </div>
              <div style={{ color: "var(--text-3)", paddingBottom: 3 }}>→</div>
              <div title={"Si además se te venden los " + fN(cap.usdt_por_vender, 0) + " USDT que tenés publicados, vas a poder recomprar hasta acá. Es el techo de la jornada, no algo que puedas hacer ahora mismo."}>
                <div style={{ fontSize: 11, color: "var(--text-3)", textTransform: "uppercase", letterSpacing: "0.08em" }}>Cuando vendas todo</div>
                <div style={{ fontFamily: "var(--mono)", fontSize: 17, color: "var(--accent)" }}>
                  {fN(cap.total_usdt, 0)} <span style={{ fontSize: 11, color: "var(--text-3)" }}>USDT</span>
                </div>
              </div>
              {cap.usdt_por_vender ? (
                <div style={{ fontSize: 10.5, color: "var(--text-3)", paddingBottom: 4 }}>
                  te quedan {fN(cap.usdt_por_vender, 0)} USDT por vender
                </div>
              ) : null}
            </div>
          )}
        </div>
        <div>
          <div style={{ fontSize: 10.5, color: "var(--text-3)", marginBottom: 4 }}>Margen objetivo (%)</div>
          <input value={margen} onChange={e => setMargen(e.target.value.replace(/[^0-9.]/g, "") || 0)}
            inputMode="decimal"
            style={{ width: 80, background: "var(--bg-2)", border: "1px solid var(--line)",
                     borderRadius: 7, color: "var(--text)", fontFamily: "var(--mono)",
                     fontSize: 13, padding: "6px 9px" }} />
        </div>
        {/* guardar como default: solo aparece si cambiaste algo respecto de lo
            guardado, para no ensuciar la barra cuando ya esta como lo queres */}
        {sinGuardar && (
          <button disabled={busy} onClick={guardarDefaults}
            title="Deja este monto y margen como tu configuración fija: se usan al abrir la página y en el chip del Plan de Hoy"
            style={{ cursor: "pointer", borderRadius: 7, padding: "7px 12px", fontSize: 11.5,
                     fontFamily: "var(--mono)", border: "1px solid var(--accent)",
                     background: "var(--accent-soft)", color: "var(--accent)" }}>
            Fijar como mío
          </button>
        )}
        {msg && (
          <span style={{ fontFamily: "var(--mono)", fontSize: 11.5, alignSelf: "center",
                         color: msg[0] === "✓" ? "var(--buy)" : "var(--warn)" }}>{msg}</span>
        )}
        {!sinGuardar && !msg && (
          <span style={{ fontSize: 10.5, color: "var(--text-3)", alignSelf: "center" }}>
            usando tu configuración guardada
          </span>
        )}
      </div>

      {/* COL44: dos avisos distintos, en orden de gravedad.
          El de saldo es el duro (plata que no existe); el de tanda es blando
          (podés hacerlo, pero nunca lo hiciste y el VWAP empeora al barrer
          más hondo). */}
      {saldo != null && nMonto > saldo + 1 && (
        <div style={{ marginBottom: 12, padding: "7px 11px", borderRadius: 8, fontSize: 11.5,
                     background: "rgba(255,145,0,0.1)", border: "1px solid var(--warn)", color: "var(--warn)" }}>
          ⚠ Estás simulando {fN(nMonto)} USDT, pero con tus {d.clp_disponible ? fN(d.clp_disponible) + " pesos" : "pesos"} alcanza
          para comprar {fN(saldo)}. Sirve para ver el escenario, no para operar ahora.
        </div>
      )}
      {t && t.maxima && nMonto > t.maxima && (saldo == null || nMonto <= saldo + 1) && (
        <div style={{ marginBottom: 12, padding: "7px 11px", borderRadius: 8, fontSize: 11.5,
                     background: "var(--bg-2)", border: "1px solid var(--line)", color: "var(--text-2)" }}>
          Tu recompra más grande hasta hoy fue {fN(t.maxima)} USDT (habitual: {fN(t.habitual)}).
          Barrer una tanda más honda empeora el VWAP — el precio de acá arriba ya lo tiene en cuenta.
        </div>
      )}

      {/* LOS DOS PRECIOS: lo unico que se copia para operar */}
      <div style={{ background: "var(--bg-2)", border: "1px solid " + tono, borderRadius: 12,
                    padding: "16px 18px", marginBottom: 12 }}>
        <div style={{ display: "flex", gap: 14, flexWrap: "wrap", alignItems: "center" }}>
          <div style={{ flex: "1 1 150px", minWidth: 140 }}>
            <div style={{ fontSize: 10.5, color: "var(--text-3)", textTransform: "uppercase",
                          letterSpacing: "0.08em" }}>Comprá hasta</div>
            <div style={{ fontFamily: "var(--mono)", fontSize: 30, fontWeight: 600,
                          color: "var(--text)", lineHeight: 1.15 }}>{fN(r.vwap, 2)}</div>
            <div style={{ fontSize: 10.5, color: "var(--text-3)" }}>
              cruzando · barre {r.niveles} {r.niveles === 1 ? "anuncio" : "anuncios"}
            </div>
          </div>
          <div style={{ fontSize: 20, color: tono, flex: "0 0 auto" }}>→</div>
          <div style={{ flex: "1 1 150px", minWidth: 140 }}>
            <div style={{ fontSize: 10.5, color: "var(--text-3)", textTransform: "uppercase",
                          letterSpacing: "0.08em" }}>Vendé a</div>
            <div style={{ fontFamily: "var(--mono)", fontSize: 30, fontWeight: 600,
                          color: tono, lineHeight: 1.15 }}>{fN(v.precio, 2)}</div>
            <div style={{ fontSize: 10.5, color: "var(--text-3)" }}>
              publicando · ≈pos {v.posicion_est}
            </div>
          </div>
        </div>
      </div>

      {/* estado: banda y flujo */}
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", marginBottom: 10 }}>
        <span style={{ fontFamily: "var(--mono)", fontSize: 12.5, fontWeight: 600, color: tono,
                       border: "1px solid " + tono, borderRadius: 7, padding: "3px 10px" }}>
          {d.veredicto === "VERDE" ? "🟢" : d.veredicto === "AMBAR" ? "🟡" : "🔴"} {d.veredicto}
        </span>
        <span style={{ fontSize: 12, color: "var(--text-2)" }}>
          banda {fN(v.banda_pct, 2)}%{fl.banda ? " (" + fl.banda + ")" : ""}
          {fl.capturable_dia != null && <> · se mueven <b style={{ color: "var(--text)" }}>{fN(fl.capturable_dia)}</b> USDT/día por competidor</>}
        </span>
      </div>

      <div style={{ fontSize: 12.5, color: "var(--text-2)", marginBottom: 10, lineHeight: 1.6 }}>
        {d.mensaje}
      </div>

      {(d.avisos || []).map((a, i) => (
        <div key={i} style={{ background: "var(--warn-soft)", border: "1px solid var(--warn)",
                              borderRadius: 8, padding: "7px 11px", marginBottom: 8,
                              fontSize: 11.5, color: "var(--warn)" }}>⚠ {a}</div>
      ))}

      {/* ganancia y costos */}
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
        <div style={{ flex: 1, minWidth: 150, background: "var(--bg-2)", borderRadius: 9,
                      padding: "9px 12px", border: "1px solid var(--line-soft)" }}>
          <div style={{ fontSize: 10, color: "var(--text-3)", textTransform: "uppercase" }}>Ganancia</div>
          <div style={{ fontFamily: "var(--mono)", fontSize: 15, color: "var(--text)" }}>
            {fN(g.por_vuelta_usdt, 2)} USDT<span style={{ fontSize: 11, color: "var(--text-3)" }}> /vuelta</span>
          </div>
          <div style={{ fontSize: 10.5, color: "var(--text-3)" }}>
            {fl.ciclos_dia_est != null ? "≈" + fN(fl.ciclos_dia_est, 1) + " vueltas/día · " + fN(g.por_dia_usdt, 1) + " USDT/día" : "sin estimación de vueltas"}
          </div>
        </div>
        <div style={{ flex: 1, minWidth: 150, background: "var(--bg-2)", borderRadius: 9,
                      padding: "9px 12px", border: "1px solid var(--line-soft)" }}>
          <div style={{ fontSize: 10, color: "var(--text-3)", textTransform: "uppercase" }}>Costo del ciclo</div>
          <div style={{ fontFamily: "var(--mono)", fontSize: 15, color: "var(--text)" }}>{fN(co.total_pct, 4)}%</div>
          <div style={{ fontSize: 10.5, color: "var(--text-3)" }}>
            {fN(co.maker_pct, 2)}% maker + {co.taker_usdt} USDT taker ({fN(co.taker_pct, 4)}%)
          </div>
        </div>
      </div>

      <div className="intel-explain">
        <b>Por qué el monto es el input clave:</b> la comisión taker es un <b>monto fijo</b> (0,07 USDT), así que su peso en % depende de cuánto cruces: en 100 USDT pesa 0,07%, en 1.200 apenas 0,0058%. La maker es porcentual y no cambia. Por eso ciclar montos grandes sale proporcionalmente más barato.<br/>
        <b>El precio de compra es el VWAP real:</b> se calcula barriendo el libro en vivo, no con el "mejor precio" a secas — si el tope tiene poco volumen, comprar de verdad te sale bastante más caro que ese número.<br/>
        <b>El semáforo mira el flujo, no solo el margen:</b> un precio de venta puede darte el margen que pediste pero caer en una banda donde casi no se opera. Ahí la orden tarda en llenarse, y eso es la diferencia entre 7 vueltas al día y 1.
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
  const [doble, setDoble] = vS(null);
  const [loading, setLoading] = vS(true);
  const [seccion, setSeccion] = vS("perfilhoras");
  const [fichaSel, setFichaSel] = vS(null);   // anunciante elegido desde la base

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
      fetch(B+"/api/inteligencia/doble_precio?dias=10").then(r=>r.json()).catch(()=>null),
    ]).then(([h,a,t,f,p,prof,pvf,vr,fa,cl,dp]) => {
      setHorario(h); setAnunciantes(a); setTraders(t); setFill(f); setPatron(p);
      setProfundidad(Array.isArray(prof) ? prof : (prof.datos || []));
      setPrecioFill(Array.isArray(pvf) ? pvf : (pvf.datos || []));
      setVentanas(Array.isArray(vr) ? vr : []);
      setFarmers(Array.isArray(fa) ? fa : []);
      setCurva((cl && cl.filas) ? cl.filas : []);
      setDoble(dp && dp.casos ? dp : null);
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
    ["CONTRA QUIÉN",  [["basecomp","🗂️ Base de competidores"],["ficha","🔍 Ficha del competidor"],["farmers","🌾 Farmers"],["doble","🏷️ Doble precio"]]],
    ["CUÁNTO",        [["volumen","📊 Volumen de mercado"]]],
    ["CÓMO ME FUE",   [["pnl","💰 P&L por ciclo"]]],
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

      {seccion==="cruzar" && <div id="ciclo-recompra"><CicloRecompra /><CruzarOEsperar /></div>}
      {seccion==="ficha" && <FichaAnunciante inicial={fichaSel} />}
      {seccion==="perfilhoras" && <PerfilHoras />}
      {seccion==="volumen" && <VolumenMercado />}
      {seccion==="pnl" && <PnlCiclos />}
      {seccion==="basecomp" && <BaseCompetidores onElegir={(n) => { setFichaSel(n); setSeccion("ficha"); }} />}

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

      {seccion==="doble" && (
        <section className="chart-card">
          <div className="card-head"><h3>Doble precio por ticket</h3><span className="card-sub">quién publica 2+ avisos del mismo lado y cuánto vende por cada uno · {doble ? doble.dias : 10} días</span></div>
          {!doble && <div className="intel-loading">Sin datos todavía.</div>}
          {doble && (
          <>
          <div style={{display:"flex",gap:8,flexWrap:"wrap",marginBottom:10}}>
            {[["multi-aviso", doble.resumen.pares_multi_aviso],
              ["analizables", doble.resumen.analizables],
              ["% por la caja (mediana)", doble.resumen.mediana_pct_por_la_caja != null ? doble.resumen.mediana_pct_por_la_caja+"%" : "—"],
              ["segmentan por mínimo", doble.resumen.con_dato_de_limites ? doble.resumen.segmentan_por_minimo+" de "+doble.resumen.con_dato_de_limites : "sin datos aún"]
            ].map(([l,v])=>(
              <div key={l} style={{background:"var(--bg-2)",border:"1px solid var(--line-soft)",borderRadius:9,padding:"8px 12px",minWidth:120}}>
                <div style={{fontSize:10,color:"var(--text-3)",textTransform:"uppercase"}}>{l}</div>
                <div style={{fontFamily:"var(--mono)",fontSize:16,fontWeight:600}}>{v}</div>
              </div>
            ))}
          </div>
          <div className="intel-scroll">
            <table className="intel-table">
              <thead><tr>
                <th title="Nickname en Binance P2P.">Anunciante</th>
                <th title="Lado del libro. En BUY él vende USDT (precio atractivo = el más bajo); en SELL él compra (atractivo = el más alto).">Lado</th>
                <th title="Volumen consumido en sus avisos, medido por caída de stock con filtro anti-reposicionamiento.">Volumen</th>
                <th title="Qué porcentaje de su volumen se fue por el aviso de PEOR precio para el cliente (el que más margen le deja). El hallazgo: en muchos es el canal principal.">% por la caja</th>
                <th title="Diferencia porcentual entre sus dos precios.">Spread propio</th>
                <th title="Precio y posición del aviso con precio atractivo (la vidriera).">Vidriera</th>
                <th title="Precio y posición del aviso con peor precio (la caja).">Caja</th>
                <th title="¿La vidriera exige un mínimo de orden MÁS ALTO que la caja? Ese es el mecanismo: el mínimo alto excluye al comprador chico y lo empuja al aviso caro. Requiere días de límites capturados.">Segmenta</th>
              </tr></thead>
              <tbody>{doble.casos.map(c=>(
                <tr key={c.anunciante+c.lado} style={{opacity: c.analizable ? 1 : 0.45}}>
                  <td style={{fontWeight:600}}>{c.anunciante}</td>
                  <td>{c.lado}</td>
                  <td className="tnum">{fN(c.volumen_total)}</td>
                  <td className="tnum" style={{fontWeight:600,color: c.analizable ? (c.pct_por_la_caja>=50?"var(--buy)":"var(--text)") : "var(--text-3)"}}>
                    {c.analizable ? c.pct_por_la_caja+"%" : "—"}
                    {!c.analizable && <span title={"Inventario compartido entre sus avisos ("+c.stock_compartido_pct+"% de los ciclos): la caída de stock no se puede atribuir a un aviso concreto."} style={{cursor:"help"}}> ⚠</span>}
                  </td>
                  <td className="tnum" style={{color:"var(--warn)"}}>{c.spread_propio_pct!=null?c.spread_propio_pct+"%":"—"}</td>
                  <td className="tnum">{fC(c.vidriera.precio)} <span style={{color:"var(--text-3)"}}>#{c.vidriera.pos}</span>
                    {c.vidriera.min_orden!=null && <div style={{fontSize:10,color:"var(--text-3)"}}>mín ${fN(c.vidriera.min_orden)}</div>}</td>
                  <td className="tnum">{fC(c.caja.precio)} <span style={{color:"var(--text-3)"}}>#{c.caja.pos}</span>
                    {c.caja.min_orden!=null && <div style={{fontSize:10,color:"var(--text-3)"}}>mín ${fN(c.caja.min_orden)}</div>}</td>
                  <td>{c.segmenta_por_minimo===true?<span style={{color:"var(--buy)",fontWeight:600}}>✓ sí</span>
                      :c.segmenta_por_minimo===false?<span style={{color:"var(--text-3)"}}>no</span>
                      :<span style={{color:"var(--text-3)"}} title="Falta acumular días con límites capturados (empezó el 29-jul-2026).">—</span>}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
          </>
          )}
          <div className="intel-explain">
            <b>Qué es esto:</b> publicar <b>dos avisos del mismo lado</b> a precios distintos. El barato queda arriba y visible (vidriera) pero con <b>mínimo de orden alto</b>, así que el comprador chico no califica y termina comprando en el aviso caro de más abajo (caja). El hallazgo del análisis: <b>la caja suele ser el canal principal de volumen</b>, y duplica el margen ponderado.<br/><br/>
            <b>Ojo con la dirección:</b> en el tab <b>BUY</b> el anunciante vende, así que el precio atractivo es el <b>más bajo</b>. En <b>SELL</b> compra, y el atractivo es el <b>más alto</b>. La regla es "precio atractivo con mínimo alto", y qué es atractivo depende del lado.<br/><br/>
            <b>Cómo se mide (y qué no se puede medir):</b> el reparto sale de la <b>caída de stock de cada aviso</b>, no del contador de órdenes — ese es por CUENTA y no se puede repartir entre avisos. Las filas <b>grises con ⚠</b> tienen inventario compartido (los dos avisos muestran el mismo disponible): ahí el reparto no es atribuible y no se informa.<br/><br/>
            <b>La columna "Segmenta"</b> confirma el mecanismo con el mínimo real. Necesita días acumulados desde el 29-jul-2026, cuando se empezó a capturar. <b>Tener 2 avisos no implica segmentar por ticket</b>: buena parte usa el mismo mínimo en los dos.
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
  // cuantos dias de detalle se conservan: se lee de la config, no se escribe a
  // mano en el texto (antes decia "30 dias" fijo, que ademas era el numero
  // equivocado y llevaba a recomendar backup mensual = perder datos)
  const [detDias, setDetDias] = React.useState(10);
  React.useEffect(() => {
    fetch(B + "/api/config").then(r => r.json())
      .then(c => { if (c && c.DETALLE_DIAS) setDetDias(Number(c.DETALLE_DIAS)); })
      .catch(() => {});
  }, []);
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
    // COL25: registra la rutina en la DB, asi el recordatorio se apaga solo y
    // el estado es el mismo desde cualquier dispositivo (antes vivia en
    // localStorage y el telefono creia que nunca se habia hecho backup).
    try { window.P2P_AUTH.post(B + "/api/rutinas/marcar", { tarea: "backup" }); } catch (e) {}
    setMsg("✅ Backup COMPLETO: todas las tablas en un ZIP (trae un LEEME.txt con el inventario). Guardalo en Drive o disco externo.");
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
        <div className="card-head"><h3>Backup / Exportar base de datos</h3><span className="card-sub">todas las tablas en un ZIP</span></div>
        <div style={{ marginBottom: 6 }}><SystemBar /></div>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap", margin: "10px 0 6px" }}>
          <button className="btn-apply dirty" onClick={descargarTodo}>⬇ Backup COMPLETO (todas las tablas)</button>
          <button className="btn-reset" onClick={vaciar}>Vaciar listas (conservar 24h)</button>
        </div>
        <p className="backup-last">Un clic baja TODA la base en un ZIP (con un LEEME.txt adentro). Última copia: <b>{lastTxt}</b>{diasDesde !== null ? " (hace " + diasDesde + " días)" : ""}.</p>
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
          <b>Qué trae el ZIP:</b> TODAS las tablas. El detalle crudo del libro recortado a los días que elijas, y el resto entero — precio histórico, resumen diario por competidor, tus órdenes reales, inventario y config. Adentro va un <code>LEEME.txt</code> con el inventario de lo que bajó.<br/><br/>
          <b>Cómo hacerlo vos mismo sin esta página:</b> abrí en el navegador la dirección de tu monitor seguida de:<br/>
          <code>/api/export/todo?dias=30</code> (el ZIP completo) o <code>/api/export/detalle?dias=30&tipo=ALL&fmt=csv&fuente=binance</code> (solo una tabla).<br/><br/>
          <b>La rutina, en orden:</b> 1) exportar → 2) guardar el ZIP → 3) recién ahí vaciar, si querés. <b>Nunca al revés:</b> el vaciado deja 24h, y lo que no esté en el ZIP a esa altura se pierde para siempre.<br/><br/>
          <b>Cada cuánto:</b> una vez por semana. El detalle del libro se purga solo a los {detDias} días, así que exportando cada 7 quedan {Math.max(0, detDias - 7)} días de solape y nunca hay hueco. Un backup mensual NO alcanza: perderías las semanas del medio.
        </div>
      </section>
    </div>
  );
}

/* ============================================================
   RUTINAS DE MANTENIMIENTO (COL25)
   Reemplaza al BackupBanner viejo (BORRADO en COL43), que guardaba la fecha
   en localStorage y por eso cada dispositivo creia una cosa distinta. Ahora
   el estado sale de la DB: ancla y CSV se detectan solos de la actividad
   real; el backup se marca a mano porque el ZIP se descarga fuera del
   monitor.
   ============================================================ */
const RUT_COLOR = { vencida: "var(--sell)", nunca: "var(--sell)", pronto: "var(--warn)", ok: "var(--buy)" };

function RutinasPanel({ onGoBackup }) {
  const B = (window.P2P_CONFIG && window.P2P_CONFIG.baseUrl) || "";
  const [d, setD] = React.useState(null);
  const [abierto, setAbierto] = React.useState(false);
  const [msg, setMsg] = React.useState("");
  const cargar = React.useCallback(() => {
    fetch(B + "/api/rutinas").then(r => r.json()).then(setD).catch(() => {});
  }, []);
  React.useEffect(() => { cargar(); const id = setInterval(cargar, 300000); return () => clearInterval(id); }, [cargar]);
  if (!d || !d.rutinas || !d.rutinas.length) return null;

  const marcarBackup = () => {
    window.P2P_AUTH.post(B + "/api/rutinas/marcar", { tarea: "backup" })
      .then(r => r.json().then(j => ({ ok: r.ok && j && j.ok !== false })))
      .then(({ ok }) => { setMsg(ok ? "✓ backup registrado" : "✗ no se pudo registrar"); cargar(); setTimeout(() => setMsg(""), 6000); })
      .catch(() => setMsg("✗ error de red"));
  };

  const pendientes = d.rutinas.filter(r => r.estado === "vencida" || r.estado === "nunca");
  const proximas = d.rutinas.filter(r => r.estado === "pronto");
  // si no hay nada vencido ni por vencer, el panel se calla (no ocupa lugar)
  if (!pendientes.length && !proximas.length && !abierto) {
    return (
      <button onClick={() => setAbierto(true)}
        style={{ margin: "10px 0 0", cursor: "pointer", background: "transparent", border: "none",
                 color: "var(--text-3)", fontSize: 11, fontFamily: "var(--font)" }}>
        ✓ mantenimiento al día — ver rutinas
      </button>
    );
  }
  const urgente = pendientes.length > 0;
  const tono = urgente ? "var(--sell)" : "var(--warn)";

  const fila = (r) => {
    const col = RUT_COLOR[r.estado] || "var(--text-3)";
    const cuando = r.estado === "nunca" ? "nunca se hizo"
      : r.estado === "vencida" ? "hace " + r.dias_desde + " días (toca cada " + r.cada + ")"
      : "en " + r.dias_restantes + " días";
    return (
      <div key={r.id} style={{ display: "flex", alignItems: "flex-start", gap: 10, padding: "8px 0",
                               borderTop: "1px solid var(--line-soft)" }}>
        <span style={{ color: col, fontSize: 13, lineHeight: 1.3 }}>
          {r.estado === "ok" ? "✓" : r.estado === "pronto" ? "◔" : "●"}
        </span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 12.5, color: "var(--text)" }}>
            <b>{r.titulo}</b> <span style={{ color: col, fontSize: 11 }}>· {cuando}</span>
          </div>
          <div style={{ fontSize: 11, color: "var(--text-2)", marginTop: 2 }}>{r.accion}</div>
          <div style={{ fontSize: 10.5, color: "var(--text-3)", marginTop: 1 }}>{r.porque}</div>
        </div>
        {r.id === "backup" && (
          <div style={{ display: "flex", gap: 6, flexShrink: 0 }}>
            <button onClick={onGoBackup} style={{ cursor: "pointer", borderRadius: 7, padding: "4px 10px",
              border: "1px solid var(--accent)", background: "var(--accent-soft)", color: "var(--accent)",
              fontSize: 11, fontFamily: "var(--mono)" }}>Ir</button>
            <button onClick={marcarBackup} title="Ya lo hice (registra la fecha)"
              style={{ cursor: "pointer", borderRadius: 7, padding: "4px 10px", border: "1px solid var(--line)",
              background: "transparent", color: "var(--text-3)", fontSize: 11, fontFamily: "var(--mono)" }}>Ya está</button>
          </div>
        )}
      </div>
    );
  };

  return (
    <div style={{ margin: "10px 0 0", background: "var(--bg-1)", border: "1px solid var(--line)",
                  borderLeft: "4px solid " + tono, borderRadius: 14, padding: "11px 16px" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <span style={{ fontSize: 10.5, color: "var(--text-3)", textTransform: "uppercase", letterSpacing: "0.12em" }}>Mantenimiento</span>
        <span style={{ fontFamily: "var(--mono)", fontSize: 13, fontWeight: 600, color: tono }}>
          {pendientes.length ? pendientes.length + " pendiente" + (pendientes.length > 1 ? "s" : "") : proximas.length + " por vencer"}
        </span>
        {!abierto && (
          <span style={{ fontSize: 11.5, color: "var(--text-2)" }}>
            {(pendientes.length ? pendientes : proximas).map(r => r.titulo).join(" · ")}
          </span>
        )}
        {msg && <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: msg[0] === "✓" ? "var(--buy)" : "var(--warn)" }}>{msg}</span>}
        <button onClick={() => setAbierto(!abierto)}
          style={{ marginLeft: "auto", background: "transparent", border: "1px solid var(--line)",
                   borderRadius: 7, color: "var(--text-3)", fontSize: 11, padding: "3px 10px", cursor: "pointer" }}>
          {abierto ? "ocultar" : "ver"}
        </button>
      </div>
      {abierto && <div style={{ marginTop: 6 }}>{d.rutinas.map(fila)}</div>}
      {!abierto && pendientes.length > 0 && (
        <div style={{ marginTop: 6 }}>{pendientes.map(fila)}</div>
      )}
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
      {d.macro && d.macro.senal && (
        <div title={d.macro.mensaje} style={{ display: "inline-flex", alignItems: "center", gap: 6, cursor: "help",
                     fontSize: 11, fontWeight: 600, marginBottom: 10, padding: "3px 9px", borderRadius: 7,
                     background: d.macro.senal === "SUBE" ? "var(--buy-soft)" : "var(--sell-soft)",
                     border: "1px solid " + (d.macro.senal === "SUBE" ? "var(--buy)" : "var(--sell)"),
                     color: d.macro.senal === "SUBE" ? "var(--buy)" : "var(--sell)" }}>
          {d.macro.senal === "SUBE" ? "↗" : "↘"} dólar formal {d.macro.senal === "SUBE" ? "adelantado" : "el P2P quedó adelantado"} {Math.abs(d.macro.pendiente_pct)}%
        </div>
      )}
      {d.nota_macro && (
        <div style={{ fontSize: 11.5, color: "var(--text-2)", margin: "0 0 12px", maxWidth: 900,
                     padding: "6px 10px", background: "var(--bg-2)", borderRadius: 8, borderLeft: "3px solid var(--accent)" }}>
          <b style={{ color: "var(--accent)" }}>Además (contexto macro):</b> {d.nota_macro}
        </div>
      )}
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

function ChipCiclo() {
  /* Chip compacto del Ciclo de recompra dentro del Plan de Hoy.
     Solo aparece si hay una oportunidad util (VERDE): si no, no molesta.
     Al tocarlo lleva a Inteligencia -> Cruzar o esperar, donde esta la tarjeta. */
  const B = (window.P2P_CONFIG && window.P2P_CONFIG.baseUrl) || "";
  const [d, setD] = React.useState(null);
  React.useEffect(() => {
    let stop = false;
    const load = () => fetch(B + "/api/ciclo").then(r => r.json())
      .then(j => { if (!stop) setD(j); }).catch(() => {});
    load();
    /* COL55 — 10s -> 30s. Medido el 6-ago: /api/ciclo tarda entre 5 y 7
       segundos, asi que a 10s el pedido anterior casi no terminaba antes de
       salir el siguiente. Con el monitor abierto en el celular Y en la compu
       (que es como Sebastian trabaja) los pedidos se apilaban y todo el
       servidor se ponia lento.
       POR QUE 30 Y NO MAS: el dato de fondo sigue siendo el libro vivo de
       10s (COL38), asi que el precio no envejece por esto — lo unico que
       cambia es cada cuanto la pantalla lo va a buscar. 30s sigue siendo
       muchisimo mas fresco que los 2 min que habia antes de COL38. */
    const id = setInterval(load, 30000);
    return () => { stop = true; clearInterval(id); };
  }, []);
  if (!d || d.error || d.veredicto !== "VERDE") return null;
  const fN = (x, n) => x == null ? "—" : Number(x).toLocaleString("es-CL",
    { minimumFractionDigits: n || 0, maximumFractionDigits: n || 0 });
  const ir = () => {
    // el detalle vive en Inteligencia; el usuario cambia de pestaña a mano,
    // asi que damos la instruccion en el title en vez de simular navegacion.
    const el = document.getElementById("ciclo-recompra");
    if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
  };
  return (
    <button onClick={ir}
      title="Ciclo de recompra: comprá cruzando y publicá la venta a ese precio. El detalle está en Inteligencia → Cruzar o esperar."
      style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer",
               background: "var(--bg-2)", border: "1px solid var(--buy)",
               borderRadius: 9, padding: "6px 11px", marginTop: 8,
               fontFamily: "var(--font)", textAlign: "left" }}>
      <span style={{ fontSize: 10, color: "var(--text-3)", textTransform: "uppercase", letterSpacing: "0.09em" }}>Ciclo</span>
      <span style={{ fontFamily: "var(--mono)", fontSize: 13, color: "var(--text)" }}>
        {fN(d.recompra.vwap, 2)} <span style={{ color: "var(--buy)" }}>→</span> {fN(d.venta.precio, 2)}
      </span>
      <span style={{ fontSize: 11, color: "var(--text-2)" }}>
        con {fN(d.monto)} USDT · {fN(d.ganancia.por_vuelta_usdt, 2)}/vuelta
      </span>
      {/* profundidad: "voy a poder recomprar rápido?" es lo que se mira acá */}
      {d.recompra.profundidad_libro != null && (
        <span style={{ fontSize: 10.5, color: "var(--text-3)" }}
              title="USDT accesibles para vos en el libro ahora mismo, respetando los límites de cada anuncio. Si es holgado, la recompra es rápida.">
          · {fN(d.recompra.profundidad_libro)} disp
        </span>
      )}
      {/* la EDAD del dato: sin esto no se sabe si el precio sigue existiendo */}
      {d.edad_seg != null && (
        <span style={{ fontSize: 10, marginLeft: "auto",
                       color: d.edad_seg <= 30 ? "var(--buy)" : d.edad_seg <= 90 ? "var(--warn)" : "var(--sell)" }}
              title={"Antigüedad del libro con el que se calculó. Fuente: " + (d.fuente_libro === "vivo" ? "libro en vivo (10s)" : "colector (2 min)") +
                     (d.descartados_sin_pago ? " · " + d.descartados_sin_pago + " anuncio(s) descartado(s) por método de pago" : "")}>
          {d.edad_seg < 60 ? Math.round(d.edad_seg) + "s" : Math.round(d.edad_seg / 60) + "m"}
          {d.fuente_libro === "vivo" ? " ⚡" : ""}
        </span>
      )}
    </button>
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
      <ChipCiclo />
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

/* PRECIOS COMPACTOS (COL36, beta) — los mismos numeros que las dos tarjetas
   grandes de precio ponderado, en una franja fina. Sebastian: "eso puede estar
   mucho mas chiquito y en unos cuadritos bien arriba".
   Recibe el snapshot ya filtrado, no vuelve a pedir nada al backend. */
function PreciosCompactos({ snap }) {
  if (!snap) return null;
  const f = (v, d) => v == null ? "—" : Number(v).toLocaleString("es-CL",
    { minimumFractionDigits: d == null ? 2 : d, maximumFractionDigits: d == null ? 2 : d });
  const gan = Number(snap.ganancia_neta_pct);
  const tono = gan >= 0.6 ? "var(--buy)" : gan >= 0.2 ? "var(--warn)" : "var(--sell)";
  const items = [
    { l: "Vendés a", v: "$" + f(snap.mejor_vendedor_tab_compra), s: snap.lider_tab_compra || "tab compra", c: "var(--buy)" },
    { l: "Comprás a", v: "$" + f(snap.mejor_comprador_tab_venta), s: snap.lider_tab_venta || "tab venta", c: "var(--sell)" },
    { l: "Brecha", v: "$" + f(snap.spread_pond_abs), s: f(snap.spread_pond_pct) + "% ponderado" },
    { l: "Neta est.", v: f(gan) + "%", s: snap.estado || "", c: tono },
    { l: "Liquidez", v: f(snap.liq_tab_compra, 0), s: "USDT en compra" },
    { l: "Liquidez", v: f(snap.liq_tab_venta, 0), s: "USDT en venta" },
  ];
  return (
    <div className="px-bar">
      {items.map((i, k) => (
        <div key={k} className="px-item">
          <div className="px-lbl">{i.l}</div>
          <div className="px-val" style={i.c ? { color: i.c } : null}>{i.v}</div>
          <div className="px-sub">{i.s}</div>
        </div>
      ))}
    </div>
  );
}

/* ESTRATEGIA RAPIDA (COL36, beta) — los presets y el gap, sin tener que bajar
   al panel completo. Sebastian: "me gustaria cambiarla directamente desde ahi".
   Es el MISMO endpoint que usa EstrategiaPanel, no una via paralela. */
function EstrategiaRapida() {
  const B = (window.P2P_CONFIG && window.P2P_CONFIG.baseUrl) || "";
  const [cfg, setCfg] = React.useState(null);
  const [msg, setMsg] = React.useState("");
  const cargar = React.useCallback(() => {
    fetch(B + "/api/config").then(r => r.json()).then(setCfg).catch(() => {});
  }, []);
  React.useEffect(() => { cargar(); }, [cargar]);
  if (!cfg) return null;
  const PRESETS = [
    { n: "Margen ancho", gap: 1.35, min: 0.28 },
    { n: "Equilibrado", gap: 1.25, min: 0.20 },
    { n: "Rotación", gap: 1.10, min: 0.28 },
    { n: "Farming", gap: 0.60, min: -0.20 },
  ];
  const gapAct = Number(cfg.GAP_OBJETIVO_BRUTO);
  const minAct = Number(cfg.SPREAD_MIN_OPERATIVO);
  const aplicar = (p) => {
    window.P2P_AUTH.post(B + "/api/config", { GAP_OBJETIVO_BRUTO: p.gap, SPREAD_MIN_OPERATIVO: p.min })
      .then(r => r.json()).then(() => { setMsg("✓ " + p.n); cargar(); setTimeout(() => setMsg(""), 5000); })
      .catch(() => setMsg("✗ error"));
  };
  return (
    <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center", marginTop: 8 }}>
      <span style={{ fontSize: 10, color: "var(--text-3)", textTransform: "uppercase", letterSpacing: ".08em" }}>Estrategia</span>
      {PRESETS.map(p => {
        const activo = Math.abs(gapAct - p.gap) < 0.001 && Math.abs(minAct - p.min) < 0.001;
        return (
          <button key={p.n} onClick={() => aplicar(p)} title={"gap " + p.gap + "% · mínimo " + p.min + "%"}
            style={{ cursor: "pointer", borderRadius: 7, padding: "3px 9px", fontSize: 10.5,
                     fontFamily: "var(--mono)", border: "1px solid " + (activo ? "var(--accent)" : "var(--line)"),
                     background: activo ? "var(--accent-soft)" : "transparent",
                     color: activo ? "var(--accent)" : "var(--text-3)" }}>
            {p.n}
          </button>
        );
      })}
      <span style={{ fontSize: 10, color: "var(--text-3)" }}>
        gap {gapAct}% · mín {minAct}% · capital {Number(cfg.CAPITAL_OPERATIVO)}
      </span>
      {msg && <span style={{ fontSize: 10.5, fontFamily: "var(--mono)", color: "var(--buy)" }}>{msg}</span>}
    </div>
  );
}

/* CARRERA A MERCHANT (COL36) — los 6 requisitos REALES de la pagina de
   elegibilidad de Binance, con conteo en vivo y el plan para llegar.
   Antes el codigo usaba metas inventadas (300 ordenes, que no existe). */
function CarreraMerchant() {
  const B = (window.P2P_CONFIG && window.P2P_CONFIG.baseUrl) || "";
  const [d, setD] = React.useState(null);
  const [form, setForm] = React.useState(false);
  const [msg, setMsg] = React.useState("");
  const [f, setF] = React.useState({ ordenes_total: "", ordenes_30d: "", vol_total_btc: "",
                                     vol_30d_btc: "", tasa_finalizacion: "", dias_verificado: "" });
  const cargar = React.useCallback(() => {
    fetch(B + "/api/merchant").then(r => r.json()).then(setD).catch(() => {});
  }, []);
  React.useEffect(() => { cargar(); const id = setInterval(cargar, 120000); return () => clearInterval(id); }, [cargar]);
  if (!d || !d.requisitos) return null;

  const nf = (v, dec) => v == null ? "—" : Number(v).toLocaleString("es-CL", { minimumFractionDigits: dec || 0, maximumFractionDigits: dec || 0 });
  const p = d.plan || {};
  const NOMBRE = {
    dias_verificado: "Días desde verificación", tasa_finalizacion: "Tasa de finalización",
    ordenes_30d: "Órdenes (30d móvil)", vol_30d_btc: "Volumen (30d móvil)",
    ordenes_total: "Órdenes totales", vol_total_btc: "Volumen total",
  };
  const guardar = () => {
    const body = {};
    Object.keys(f).forEach(k => { if (String(f[k]).trim() !== "") body[k] = parseFloat(String(f[k]).replace(",", ".")); });
    window.P2P_AUTH.post(B + "/api/merchant/ancla", body)
      .then(r => r.json()).then(j => {
        setMsg(j.ok ? "✓ datos de Binance guardados" : "✗ " + (j.error || "no se pudo"));
        if (j.ok) { setForm(false); cargar(); }
        setTimeout(() => setMsg(""), 8000);
      }).catch(() => setMsg("✗ error de red"));
  };

  const inp = { background: "var(--bg-1)", border: "1px solid var(--line)", borderRadius: 7,
                padding: "5px 8px", color: "var(--text)", fontFamily: "var(--mono)", fontSize: 12, width: 95 };
  const lbl = { fontSize: 9.5, color: "var(--text-3)", textTransform: "uppercase", marginBottom: 2 };

  return (
    <div className="chart-card" style={{ margin: "10px 0 0", padding: "13px 16px" }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 8, marginBottom: 9, flexWrap: "wrap" }}>
        <div style={{ fontSize: 10.5, color: "var(--text-3)", textTransform: "uppercase", letterSpacing: "0.12em" }}>Carrera a Merchant</div>
        {d.cumple_todo && <span style={{ color: "var(--buy)", fontWeight: 600, fontSize: 12 }}>✓ cumplís todo</span>}
        <button onClick={() => setForm(!form)} style={{ marginLeft: "auto", cursor: "pointer", borderRadius: 7,
          padding: "3px 10px", border: "1px solid var(--line)", background: "transparent",
          color: "var(--text-3)", fontSize: 10.5, fontFamily: "var(--mono)" }}>
          {form ? "cancelar" : "actualizar desde Binance"}
        </button>
      </div>

      {form && (
        <div style={{ background: "var(--bg-2)", border: "1px solid var(--line-soft)", borderRadius: 10, padding: "10px 12px", marginBottom: 10 }}>
          <div style={{ fontSize: 11.5, color: "var(--text-2)", marginBottom: 8 }}>
            Copiá los números de <b>p2p.binance.com → Solicitud para comerciante</b>. Son la verdad de terreno: con eso el monitor calibra la estimación de volumen.
          </div>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "flex-end" }}>
            {[["ordenes_30d", "Órdenes 30d"], ["vol_30d_btc", "Volumen 30d (BTC)"], ["ordenes_total", "Órdenes totales"],
              ["vol_total_btc", "Volumen total (BTC)"], ["tasa_finalizacion", "Tasa final. (%)"], ["dias_verificado", "Días verificado"]].map(([k, l]) => (
              <div key={k}><div style={lbl}>{l}</div>
                <input value={f[k]} onChange={e => setF({ ...f, [k]: e.target.value })} inputMode="decimal" style={inp} /></div>
            ))}
            <button onClick={guardar} className="btn-apply dirty">Guardar</button>
          </div>
        </div>
      )}
      {msg && <div style={{ fontFamily: "var(--mono)", fontSize: 11.5, marginBottom: 8, color: msg[0] === "✓" ? "var(--buy)" : "var(--warn)" }}>{msg}</div>}

      {/* los 6 requisitos */}
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 10 }}>
        {d.requisitos.map(r => (
          <div key={r.clave} style={{ flex: "1 1 150px", minWidth: 150, background: "var(--bg-2)",
                border: "1px solid " + (r.cumple ? "var(--buy)" : "var(--line-soft)"), borderRadius: 10, padding: "8px 11px" }}>
            <div style={{ fontSize: 9.5, color: "var(--text-3)", textTransform: "uppercase" }}>{NOMBRE[r.clave] || r.clave}</div>
            <div style={{ fontFamily: "var(--mono)", fontSize: 15, fontWeight: 600, color: r.cumple ? "var(--buy)" : "var(--text)" }}>
              {r.actual == null ? "—" : nf(r.actual, r.unidad === "BTC" ? 4 : (r.unidad === "%" ? 1 : 0))}
              <span style={{ fontSize: 10.5, color: "var(--text-3)", fontWeight: 400 }}> / {nf(r.meta, r.unidad === "BTC" ? 1 : 0)}</span>
            </div>
            {r.pct != null && (
              <div className="hbar" style={{ marginTop: 4 }}>
                <div className="hbar-fill" style={{ width: Math.min(100, r.pct) + "%", background: r.cumple ? "var(--buy)" : "var(--warn)" }} />
              </div>
            )}
            <div style={{ fontSize: 10, color: r.cumple ? "var(--buy)" : "var(--text-3)", marginTop: 3 }}>
              {r.actual == null ? "cargá los datos de Binance"
                : r.cumple ? "✓ cumplido"
                : "faltan " + nf(r.falta, r.unidad === "BTC" ? 4 : 0) + " " + r.unidad}
            </div>
          </div>
        ))}
      </div>

      {/* el plan */}
      {p.usdt_por_dia_necesario && (
        <div style={{ background: "var(--bg-2)", border: "1px solid var(--line-soft)", borderRadius: 10, padding: "10px 12px" }}>
          <div style={{ fontSize: 10.5, color: "var(--text-3)", textTransform: "uppercase", marginBottom: 7 }}>Qué hace falta para llegar</div>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 8 }}>
            {[["USDT/día necesario", nf(p.usdt_por_dia_necesario), "sostenido — la ventana es móvil"],
              ["tu ritmo hoy", p.usdt_por_dia_real ? nf(p.usdt_por_dia_real) : "—", p.factor_necesario ? "hay que multiplicar x" + p.factor_necesario : "sin medir aún"],
              ["ticket objetivo", nf(p.ticket_objetivo_usdt), "para que 150 órdenes alcancen"],
              ["tu ticket", p.ticket_actual_usdt ? nf(p.ticket_actual_usdt, 1) : "—", p.ticket_fuente === "ancla" ? "calibrado con Binance" : "del CSV (subestima)"]
            ].map(([l, v, s]) => (
              <div key={l} style={{ flex: "1 1 130px", minWidth: 130 }}>
                <div style={{ fontSize: 9.5, color: "var(--text-3)", textTransform: "uppercase" }}>{l}</div>
                <div style={{ fontFamily: "var(--mono)", fontSize: 15, fontWeight: 600 }}>{v}</div>
                <div style={{ fontSize: 9.5, color: "var(--text-3)" }}>{s}</div>
              </div>
            ))}
          </div>
          {/* días parados: la ventana móvil drena igual (COL37) */}
          {p.dias_parado != null && p.dias_parado >= 1 && (
            <div style={{ background: "var(--sell-soft)", border: "1px solid var(--sell)",
                          borderRadius: 9, padding: "8px 11px", marginBottom: 8 }}>
              <div style={{ fontSize: 11.5, fontWeight: 600, color: "var(--sell)" }}>
                ⚠ Hace {p.dias_parado} día{p.dias_parado > 1 ? "s" : ""} que no aparecés en el libro
              </div>
              <div style={{ fontSize: 11, color: "var(--text-2)", marginTop: 3, lineHeight: 1.5 }}>
                La ventana de 30 días <b>sigue drenando igual</b>: cada día parado no suma nada
                y además se te cae lo de hace 30 días. Son <b>{(p.dias_parado * p.usdt_por_dia_necesario).toLocaleString("es-CL")} USDT</b> que
                había que hacer y no se hicieron.
              </div>
            </div>
          )}

          {/* abanico de escenarios según cuántos días puedas operar (COL37).
              No se fija un número: la vida real no es tan prolija. */}
          {p.escenarios_dias && p.escenarios_dias.length > 0 && (
            <div style={{ background: "var(--bg-1)", border: "1px solid var(--line-soft)",
                          borderRadius: 9, padding: "9px 11px", marginBottom: 8 }}>
              <div style={{ fontSize: 10, color: "var(--text-3)", textTransform: "uppercase", marginBottom: 6 }}>
                Cuánto te exige según los días que puedas operar
                {p.dias_operables_medidos != null && (
                  <span style={{ textTransform: "none", color: "var(--warn)" }}>
                    {" "}· venís operando ~{p.dias_operables_medidos} de 7
                  </span>
                )}
              </div>
              <div className="intel-scroll">
                <table className="intel-table">
                  <thead><tr>
                    <th>Días/semana</th>
                    <th title="Cuánto tenés que mover en cada jornada que operás.">USDT por jornada</th>
                    <th title="Con tu ticket actual.">Órdenes con tu ticket</th>
                    <th title="Si llevaras el ticket al objetivo.">Con ticket objetivo</th>
                  </tr></thead>
                  <tbody>{p.escenarios_dias.map(e => (
                    <tr key={e.dias_semana} style={{ opacity: e.logrado_alguna_vez === false ? 0.55 : 1 }}>
                      <td className="tnum"><b>{e.dias_semana}</b> <span style={{ color: "var(--text-3)" }}>({e.dias_en_30} en 30d)</span></td>
                      <td className="tnum">{e.usdt_por_jornada.toLocaleString("es-CL")}</td>
                      <td className="tnum" style={{ color: e.logrado_alguna_vez ? "var(--buy)" : "var(--sell)" }}>
                        {e.ordenes_por_jornada}
                        {e.logrado_alguna_vez === true && <span title="Ya tuviste un día así"> ✓</span>}
                      </td>
                      <td className="tnum" style={{ color: "var(--text-2)" }}>{e.ordenes_ticket_objetivo}</td>
                    </tr>
                  ))}</tbody>
                </table>
              </div>
              <div style={{ fontSize: 10.5, color: "var(--text-3)", marginTop: 6, lineHeight: 1.5 }}>
                No hace falta comprometerse a un número: mirá qué fila te resulta sostenible.
                El <span style={{ color: "var(--buy)" }}>✓</span> marca las que ya lograste alguna vez
                {p.mejor_dia_medido_ordenes && <> (tu mejor día: <b>{p.mejor_dia_medido_ordenes}</b> órdenes)</>}.
                Menos días operando = más carga por jornada, y ahí es donde el <b>ticket</b> hace la diferencia.
              </div>
            </div>
          )}

          <div style={{ fontSize: 11.5, color: "var(--text-2)", lineHeight: 1.55, paddingTop: 7, borderTop: "1px solid var(--line-soft)" }}>
            {!p.alcanza_con_ritmo_actual && (
              <><b style={{ color: "var(--warn)" }}>Con el ticket actual no llegás.</b>{" "}
              Necesitarías <b>{p.ordenes_dia_con_ticket_actual}</b> órdenes por día todos los días, contra las <b>{p.ordenes_por_dia_necesario}</b> que
              harían falta si el ticket fuera de {nf(p.ticket_objetivo_usdt)} USDT. <b>La palanca es el ticket, no la cantidad de órdenes.</b><br /></>
            )}
            <span style={{ color: "var(--text-3)" }}>
              La ventana de 30 días es <b>móvil</b>: lo de hace 31 días desaparece. Por debajo del ritmo necesario la meta nunca se alcanza —
              se cae por atrás al mismo ritmo que entra.
            </span>
          </div>
        </div>
      )}

      {/* como subir el ticket: medido */}
      {d.minimos_medidos && d.minimos_medidos.length > 0 && (
        <div style={{ marginTop: 9, background: "var(--bg-2)", border: "1px solid var(--line-soft)", borderRadius: 10, padding: "10px 12px" }}>
          <div style={{ fontSize: 10.5, color: "var(--text-3)", textTransform: "uppercase", marginBottom: 6 }}>Cómo conseguir tickets más grandes (medido en el mercado)</div>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
            {d.minimos_medidos.map(m => {
              const objetivo = p.ticket_objetivo_usdt && m.ticket_mediano >= p.ticket_objetivo_usdt;
              return (
                <div key={m.min_desde} style={{ flex: "1 1 120px", minWidth: 120, padding: "7px 10px", borderRadius: 8,
                      background: objetivo ? "var(--buy-soft)" : "var(--bg-1)",
                      border: "1px solid " + (objetivo ? "var(--buy)" : "var(--line-soft)") }}>
                  <div style={{ fontSize: 9.5, color: "var(--text-3)" }}>
                    mín ${nf(m.min_desde)}{m.min_hasta ? "–" + nf(m.min_hasta) : "+"}
                  </div>
                  <div style={{ fontFamily: "var(--mono)", fontSize: 15, fontWeight: 600, color: objetivo ? "var(--buy)" : "var(--text)" }}>
                    {nf(m.ticket_mediano)} <span style={{ fontSize: 10, fontWeight: 400, color: "var(--text-3)" }}>USDT</span>
                  </div>
                  <div style={{ fontSize: 9.5, color: "var(--text-3)" }}>{m.n} anunciantes</div>
                </div>
              );
            })}
          </div>
          <div style={{ fontSize: 11, color: "var(--text-2)", marginTop: 7, lineHeight: 1.5 }}>
            El <b>mínimo de orden que publicás</b> define el ticket que te llega — correlación medida <b>+0,80</b>.
            Es una perilla <b>distinta</b> del gap: filtra <i>quién</i> te toma, no <i>a qué precio</i>. Subir el mínimo no te obliga a resignar margen.
          </div>
        </div>
      )}

      <div style={{ fontSize: 10, color: "var(--text-3)", marginTop: 7, lineHeight: 1.5 }}>
        {d.vivo && d.vivo.ordenes_30d != null && <>Órdenes leídas del libro público (exacto, cada 2 min): <b>{d.vivo.ordenes_30d}</b>. </>}
        El volumen es <b>estimado</b> (órdenes × ticket); se calibra cada vez que cargás los datos de Binance.
        {d.ancla && <> Último dato real: {String(d.ancla.ts).slice(0, 16)}.</>}
      </div>
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
        {/* COL54 — SOLO EN /beta. La vista principal queda intacta.
            Esta tarjeta ("Carrera al verificado") duplica a CarreraMerchant
            (COL36), que muestra los MISMOS dos numeros pero con los
            requisitos REALES de Binance. Peor: aca la meta dice "300
            ordenes", que NO EXISTE como requisito — ya se corrigio en
            /api/merchant y esta tarjeta sigue mostrando el numero viejo.
            En beta se sacan esas dos cajas y queda lo unico que no esta en
            ningun otro lado: donde estan parados MIS anuncios. Sebastian:
            "esa me gusta, cuando esta ahi en vivo y te dice en que parte
            estas del libro".
            MEDIDO: sacarlas ahorra solo 13 px (267 -> 254). Van en la misma
            fila que las cajas de anuncios, y esa fila ya mide lo que mide su
            caja mas alta. Se hace igual porque corrige un dato FALSO, no
            para ganar espacio — el espacio esta en otro lado. */}
        <span style={{ fontSize: 10.5, color: "var(--text-3)", textTransform: "uppercase", letterSpacing: "0.12em" }}>
          {window.P2P_BETA ? "Mis anuncios en el libro" : "Carrera al verificado"}</span>
        <span style={{ fontFamily: "var(--mono)", fontSize: 12.5, color: "var(--accent)", fontWeight: 600 }}>{d.nick}</span>
        {!window.P2P_BETA && <span title={d.nota} style={{ color: "var(--text-3)", cursor: "help" }}>ⓘ</span>}
        {!d.en_libro && <span style={{ fontSize: 11, color: "var(--text-3)" }}>· no aparecés en el libro ahora</span>}
      </div>
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
        {(d.anuncios || []).map(adBox)}
        {!window.P2P_BETA && (
        <div style={boxSt}>
          <div style={lbl}>Órdenes 30d (meta 300)</div>
          <div style={val}>{fmt(pr.ordenes_30d)} <span style={{ fontSize: 11, color: "var(--text-3)" }}>/ 300</span></div>
          {bar(pr.ordenes_pct, "var(--accent)")}
          <div style={sub}>{pr.ordenes_ganadas_7d != null ? "+" + fmt(pr.ordenes_ganadas_7d) + " esta semana" : "contador oficial de Binance"}</div>
        </div>
        )}
        {!window.P2P_BETA && (
        <div style={boxSt}>
          <div style={lbl}>Volumen 30d estimado</div>
          <div style={val}>{fmt(pr.vol_30d_estimado)} <span style={{ fontSize: 11, color: "var(--text-3)" }}>USDT</span></div>
          {bar(pr.vol_pct_minima, "var(--buy)")}
          <div style={sub}>{fmt(pr.vol_pct_minima)}% de 0,5 BTC (mínimo) · {fmt(pr.vol_pct_comoda)}% de 1 BTC</div>
        </div>
        )}
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

/* ============================================================
   MI INVENTARIO EN VIVO (COL22)
   Hibrido: el ancla es la verdad (saldos pegados a mano), lo de al lado es
   estimacion (ancla + ordenes detectadas). Nunca se confunde uno con otro.
   ============================================================ */
const INV_ZONA_COLOR = { comoda: "var(--buy)", correccion: "var(--warn)", dura: "var(--sell)" };

function invFmt(n, dec) {
  if (n == null) return "—";
  return Number(n).toLocaleString("es-CL", { minimumFractionDigits: dec || 0, maximumFractionDigits: dec || 0 });
}

function ChipBalance() {
  const B = (window.P2P_CONFIG && window.P2P_CONFIG.baseUrl) || "";
  const [d, setD] = React.useState(null);
  React.useEffect(() => {
    let stop = false;
    const load = () => fetch(B + "/api/inventario").then(r => r.json())
      .then(j => { if (!stop) setD(j); }).catch(() => {});
    load();
    const id = setInterval(load, 60000);
    return () => { stop = true; clearInterval(id); };
  }, []);
  if (!d || !d.configurado) return null;
  const tono = INV_ZONA_COLOR[d.zona] || "var(--text-3)";
  const etiqueta = d.zona === "comoda" ? "en banda"
    : (d.reequilibrio.lado_corto === "usdt" ? "corto de USDT" : "cargado de USDT");
  const irACard = () => {
    const el = document.getElementById("inv-card");
    if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
  };
  return (
    <button onClick={irACard} title="Tu balance USDT/CLP. Tocá para ver el inventario completo."
      style={{ display: "inline-flex", alignItems: "center", gap: 8, cursor: "pointer",
               background: "var(--bg-1)", border: "1px solid var(--line)",
               borderLeft: "3px solid " + tono, borderRadius: 10,
               padding: "7px 12px", margin: "10px 0 0", fontFamily: "var(--font)" }}>
      <span style={{ fontSize: 10, color: "var(--text-3)", textTransform: "uppercase", letterSpacing: "0.1em" }}>Balance</span>
      <span style={{ fontFamily: "var(--mono)", fontSize: 15, fontWeight: 600, color: tono }}>
        {invFmt(d.pct_usdt, 1)}% USDT
      </span>
      <span style={{ fontSize: 11.5, color: "var(--text-2)" }}>· {etiqueta}</span>
    </button>
  );
}

function BarraBalance({ pct, banda }) {
  /* Barra de DOS niveles: verde = zona comoda (farmear), ambar = correccion
     (repreciar agresivo), rojo = limite duro (cruzar). La marca blanca es
     donde estas ahora. */
  const b = banda || { min: 40, max: 60, duro_min: 30, duro_max: 70 };
  const seg = [
    { w: b.duro_min, c: "var(--sell-soft)" },
    { w: b.min - b.duro_min, c: "var(--warn-soft)" },
    { w: b.max - b.min, c: "var(--buy-soft)" },
    { w: b.duro_max - b.max, c: "var(--warn-soft)" },
    { w: 100 - b.duro_max, c: "var(--sell-soft)" },
  ];
  const marcas = [
    { p: b.duro_min, c: "var(--sell)" }, { p: b.min, c: "var(--buy)" },
    { p: b.max, c: "var(--buy)" }, { p: b.duro_max, c: "var(--sell)" },
  ];
  const pos = Math.max(0, Math.min(100, pct || 0));
  return (
    <div style={{ margin: "4px 0 2px" }}>
      <div style={{ position: "relative", height: 16, borderRadius: 6, overflow: "hidden",
                    display: "flex", border: "1px solid var(--line-soft)" }}>
        {seg.map((s, i) => <div key={i} style={{ width: s.w + "%", background: s.c }} />)}
        {marcas.map((m, i) => (
          <div key={i} style={{ position: "absolute", left: m.p + "%", top: 0, bottom: 0,
                                width: 1, background: m.c, opacity: 0.7 }} />
        ))}
        <div style={{ position: "absolute", left: pos + "%", top: -2, bottom: -2, width: 3,
                      background: "var(--text)", transform: "translateX(-50%)", borderRadius: 2,
                      boxShadow: "0 0 0 2px var(--bg-1)" }} />
      </div>
      <div style={{ position: "relative", height: 13, fontFamily: "var(--mono)", fontSize: 9,
                    color: "var(--text-3)", marginTop: 2 }}>
        {marcas.map((m, i) => (
          <span key={i} style={{ position: "absolute", left: m.p + "%", transform: "translateX(-50%)" }}>{m.p}</span>
        ))}
      </div>
    </div>
  );
}

/* CONTEXTO MACRO (COL35) — dolar forex, VIX, cobre + brecha del P2P.
   Un solo componente para los dos usos: 'chip' (compacto, arriba en Plan de
   Hoy) y 'card' (completo, junto al inventario). Si el backend no tiene dato
   todavia o la fuente esta caida, NO renderiza nada en vez de mostrar un
   hueco roto — es contexto, no puede ensuciar la pantalla. */
function MacroBar({ modo }) {
  const B = (window.P2P_CONFIG && window.P2P_CONFIG.baseUrl) || "";
  const [m, setM] = React.useState(null);
  React.useEffect(() => {
    const load = () => fetch(B + "/api/macro").then(r => r.json()).then(setM).catch(() => {});
    load();
    const id = setInterval(load, 300000);   // 5 min: el dato de fondo cambia cada 15
    return () => clearInterval(id);
  }, []);
  if (!m || !m.disponible) return null;

  const nf = (v, d) => v == null ? "—" : Number(v).toLocaleString("es-CL", { minimumFractionDigits: d, maximumFractionDigits: d });
  const flecha = (v) => v == null ? "" : v > 0 ? "▲" : v < 0 ? "▼" : "=";
  const tono = (v, invertido) => {
    if (v == null) return "var(--text-3)";
    const bueno = invertido ? v < 0 : v > 0;
    return Math.abs(v) < 0.05 ? "var(--text-3)" : bueno ? "var(--buy)" : "var(--sell)";
  };
  // VIX al reves: que SUBA es senal de miedo/volatilidad, no algo "bueno"
  const items = [
    { k: "Dólar forex", v: nf(m.usdclp_forex, 2), var: m.usdclp_forex_var_pct, inv: false,
      t: "USD/CLP en el mercado formal (Yahoo Finance). El P2P suele seguirlo con retardo." },
    { k: "VIX", v: nf(m.vix, 2), var: m.vix_var_pct, inv: true,
      t: "Índice de miedo del mercado. Si sube fuerte, suele venir volatilidad." },
    { k: "Cobre", v: nf(m.cobre, 3), var: m.cobre_var_pct, inv: false,
      t: "Futuro del cobre (COMEX), en USD/libra. Chile exporta cobre: si sube, el peso tiende a fortalecerse (dólar baja)." },
  ];

  if (modo === "chip") {
    return (
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center",
                    fontSize: 11, color: "var(--text-2)", margin: "6px 0 0" }}>
        {items.map(it => (
          <span key={it.k} title={it.t} style={{ cursor: "help" }}>
            {it.k} <b style={{ fontFamily: "var(--mono)", color: "var(--text)" }}>{it.v}</b>
            {it.var != null && <span style={{ color: tono(it.var, it.inv) }}> {flecha(it.var)}{Math.abs(it.var)}%</span>}
          </span>
        ))}
        {m.brecha_pct != null && (
          <span title="Cuánto está el P2P por encima del dólar formal. Es la prima que paga el mercado cripto." style={{ cursor: "help" }}>
            brecha P2P <b style={{ fontFamily: "var(--mono)", color: "var(--warn)" }}>{nf(m.brecha_pct, 2)}%</b>
          </span>
        )}
        {m.desfase && m.desfase.senal && (
          <span title={m.desfase.mensaje} style={{ cursor: "help", fontWeight: 600,
                       color: m.desfase.senal === "SUBE" ? "var(--buy)" : "var(--sell)" }}>
            {m.desfase.senal === "SUBE" ? "↗" : "↘"} P2P retrasado {Math.abs(m.desfase.pendiente_pct)}%
          </span>
        )}
        {m.viejo && <span style={{ color: "var(--sell)" }} title={"Último dato hace " + m.edad_min + " min: la fuente externa no está respondiendo."}>⚠ dato viejo</span>}
      </div>
    );
  }

  return (
    <div style={{ marginTop: 10, padding: "10px 12px", background: "var(--bg-2)", borderRadius: 10, border: "1px solid var(--line-soft)" }}>
      <div style={{ display: "flex", alignItems: "baseline", marginBottom: 8 }}>
        <div style={{ fontSize: 10.5, color: "var(--text-3)", textTransform: "uppercase", letterSpacing: "0.1em" }}>Contexto de mercado</div>
        <div style={{ marginLeft: "auto", fontSize: 10, color: m.viejo ? "var(--sell)" : "var(--text-3)" }}>
          {m.viejo ? "⚠ sin actualizar hace " + m.edad_min + " min" : "hace " + m.edad_min + " min"}
        </div>
      </div>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        {items.map(it => (
          <div key={it.k} title={it.t} style={{ background: "var(--bg-1)", border: "1px solid var(--line-soft)",
                                                borderRadius: 9, padding: "8px 12px", minWidth: 105, cursor: "help" }}>
            <div style={{ fontSize: 10, color: "var(--text-3)", textTransform: "uppercase" }}>{it.k}</div>
            <div style={{ fontFamily: "var(--mono)", fontSize: 16, fontWeight: 600 }}>{it.v}</div>
            {it.var != null && <div style={{ fontSize: 10.5, color: tono(it.var, it.inv) }}>{flecha(it.var)} {Math.abs(it.var)}% vs cierre previo</div>}
          </div>
        ))}
        {m.brecha_pct != null && (
          <div title="El P2P contra el dólar formal. Este es el número para anotar como precio de referencia en la bitácora."
               style={{ background: "var(--warn-soft)", border: "1px solid var(--warn)", borderRadius: 9,
                        padding: "8px 12px", minWidth: 115, cursor: "help" }}>
            <div style={{ fontSize: 10, color: "var(--warn)", textTransform: "uppercase" }}>Brecha P2P</div>
            <div style={{ fontFamily: "var(--mono)", fontSize: 16, fontWeight: 600, color: "var(--warn)" }}>{nf(m.brecha_pct, 2)}%</div>
            <div style={{ fontSize: 10.5, color: "var(--text-3)" }}>P2P ${nf(m.p2p_ref, 2)} vs forex ${nf(m.usdclp_forex, 2)}</div>
          </div>
        )}
      </div>
      {m.desfase && m.desfase.senal && (
        <div style={{ marginTop: 9, padding: "9px 12px", borderRadius: 9,
                      background: m.desfase.senal === "SUBE" ? "var(--buy-soft)" : "var(--sell-soft)",
                      border: "1px solid " + (m.desfase.senal === "SUBE" ? "var(--buy)" : "var(--sell)") }}>
          <div style={{ fontSize: 11.5, fontWeight: 600, marginBottom: 3,
                        color: m.desfase.senal === "SUBE" ? "var(--buy)" : "var(--sell)" }}>
            {m.desfase.senal === "SUBE" ? "↗ El P2P viene retrasado hacia arriba" : "↘ El P2P quedó adelantado"}
          </div>
          <div style={{ fontSize: 11.5, color: "var(--text-2)", lineHeight: 1.5 }}>{m.desfase.mensaje}</div>
          <div style={{ fontSize: 10, color: "var(--text-3)", marginTop: 4 }}>
            forex {m.desfase.fx_var_pct > 0 ? "+" : ""}{m.desfase.fx_var_pct}% · P2P {m.desfase.p2p_var_pct > 0 ? "+" : ""}{m.desfase.p2p_var_pct}% · ventana {m.desfase.ventana_min} min
          </div>
        </div>
      )}
      <div style={{ fontSize: 10.5, color: "var(--text-3)", marginTop: 7, lineHeight: 1.5 }}>
        El P2P sigue al mercado formal con <b>retardo</b>, y eso está <b>medido</b>: el cambio del dólar forex
        correlaciona <b>+0,46</b> con el cambio del P2P de la hora siguiente (en la misma hora: +0,06; en el
        sentido inverso: +0,03 — o sea, el forex va primero). Indica <b>dirección probable, no magnitud garantizada</b>.
        <br/>El <b>precio de referencia</b> de arriba es el que conviene anotar en la bitácora al anclar saldos.
        <span style={{ color: "var(--text-3)" }}> Ojo: el cobre resultó mucho más débil (−0,16) que el dólar — probablemente te sirve porque mueve al dólar, no directo.</span>
      </div>
    </div>
  );
}

function InventarioCard() {
  const B = (window.P2P_CONFIG && window.P2P_CONFIG.baseUrl) || "";
  const [d, setD] = React.useState(null);
  const [detalle, setDetalle] = React.useState(false);
  const [form, setForm] = React.useState(null);   // 'ancla' | 'mov' | null
  const [msg, setMsg] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  // ancla
  const [aU, setAU] = React.useState(""); const [aC, setAC] = React.useState("");
  const [aNota, setANota] = React.useState("");   // COL34: para distinguir apertura/cierre en el historial
  const [confirmAviso, setConfirmAviso] = React.useState(null);   // COL40: salto sospechoso, pendiente de confirmar
  // movimiento
  const [mTipo, setMTipo] = React.useState("taker");
  const [mLado, setMLado] = React.useState("compra");
  const [mU, setMU] = React.useState(""); const [mP, setMP] = React.useState(""); const [mC, setMC] = React.useState("");
  // historial de anclas (COL34)
  const [hist, setHist] = React.useState(null);
  const [histAbierto, setHistAbierto] = React.useState(false);
  // movimientos manuales, para corregirlos (COL48)
  const [movs, setMovs] = React.useState(null);
  const [movsAbierto, setMovsAbierto] = React.useState(false);
  const [editId, setEditId] = React.useState(null);      // fila en edicion
  const [eU, setEU] = React.useState(""); const [eP, setEP] = React.useState("");
  const [confirmMov, setConfirmMov] = React.useState(null);   // aviso de salto raro

  const cargarMovs = React.useCallback(() => {
    fetch(B + "/api/inventario/movimientos?dias=7").then(r => r.json())
      .then(j => setMovs(j.movimientos || [])).catch(() => setMovs([]));
  }, []);
  const verMovs = () => {
    const abrir = !movsAbierto;
    setMovsAbierto(abrir);
    if (abrir && !movs) cargarMovs();
  };
  const abrirEdicion = (m) => {
    setEditId(m.id); setEU(String(m.usdt)); setEP(String(m.precio)); setConfirmMov(null); setMsg("");
  };
  const guardarMov = (id, forzar) => {
    if (busy) return;
    const body = { usdt: parseFloat(String(eU).replace(",", ".")), precio: parseFloat(String(eP).replace(",", ".")) };
    if (forzar) body.confirmar = true;
    setBusy(true); setMsg("Guardando…");
    window.P2P_AUTH.req("PATCH", B + "/api/inventario/movimiento/" + id, body)
      .then(r => r.json()).then(j => {
        setBusy(false);
        if (j && j.requiere_confirmacion) { setConfirmMov(j.aviso); setMsg(""); return; }
        if (!j || j.ok === false) { setMsg("✗ " + ((j && j.error) || "no se pudo guardar")); return; }
        setConfirmMov(null); setEditId(null);
        setMsg("✓ movimiento corregido");
        cargarMovs(); cargar();
        setTimeout(() => setMsg(""), 8000);
      })
      .catch(() => { setBusy(false); setMsg("✗ error de red"); });
  };
  const borrarMov = (m) => {
    if (busy) return;
    /* OJO: los saltos de linea van con doble backslash. Esto vive dentro de
       un string de Python, asi que un \\n simple lo consumiria Python y
       partiria el string de JS en dos (lo agarro esbuild en COL48). */
    if (!window.confirm("¿Borrar este movimiento?\\n\\n" + m.ts + "\\n" + m.tipo +
                        (m.lado ? " " + m.lado : "") + " · " + m.usdt + " USDT @ " + m.precio +
                        "\\n\\nNo se puede deshacer.")) return;
    setBusy(true); setMsg("Borrando…");
    window.P2P_AUTH.req("DELETE", B + "/api/inventario/movimiento/" + m.id)
      .then(r => r.json()).then(j => {
        setBusy(false);
        if (!j || j.ok === false) { setMsg("✗ " + ((j && j.error) || "no se pudo borrar")); return; }
        setMsg("✓ movimiento borrado");
        cargarMovs(); cargar();
        setTimeout(() => setMsg(""), 8000);
      })
      .catch(() => { setBusy(false); setMsg("✗ error de red"); });
  };
  const verHistorial = () => {
    const abrir = !histAbierto;
    setHistAbierto(abrir);
    if (abrir && !hist) {
      fetch(B + "/api/inventario/historial?dias=30").then(r => r.json())
        .then(j => setHist(j.historial || [])).catch(() => setHist([]));
    }
  };

  const cargar = React.useCallback(() => {
    fetch(B + "/api/inventario").then(r => r.json()).then(setD).catch(() => {});
  }, []);
  React.useEffect(() => { cargar(); const id = setInterval(cargar, 60000); return () => clearInterval(id); }, [cargar]);

  const post = (url, body, okTxt) => {
    if (busy) return;
    setBusy(true); setMsg("Guardando…");
    window.P2P_AUTH.post(B + url, body)
      .then(r => r.json().then(j => ({ ok: r.ok && j && j.ok !== false, j })))
      .then(({ ok, j }) => {
        setBusy(false);
        if (!ok) { setMsg("✗ " + ((j && j.error) || "no se pudo guardar")); return; }
        let extra = "";
        if (j.drift && (Math.abs(j.drift.usdt) > 0.01 || Math.abs(j.drift.clp) > 1)) {
          extra = " · drift vs estimado: " + invFmt(j.drift.usdt, 2) + " USDT / " + invFmt(j.drift.clp) + " CLP";
        }
        setMsg("✓ " + okTxt + extra);
        setForm(null); setANota(""); cargar();
        setTimeout(() => setMsg(""), 9000);
      })
      .catch(() => { setBusy(false); setMsg("✗ error de red"); });
  };

  // Guardar el ancla es un caso aparte de post() generico (COL40): el
  // backend puede devolver 409 pidiendo confirmacion (salto de magnitud
  // sospechoso, ej. el typo de 51.517 USDT que motivo esto). Lee SIEMPRE
  // los inputs actuales al click, asi que si el usuario edita el numero
  // despues de ver el aviso, "Guardar igual" manda lo nuevo, no lo viejo.
  const guardarAncla = (forzar) => {
    if (busy) return;
    const body = { usdt: parseFloat(String(aU).replace(",", ".")), clp: parseFloat(String(aC).replace(",", ".")), nota: aNota };
    if (forzar) body.confirmar = true;
    setBusy(true); setMsg("Guardando…");
    window.P2P_AUTH.post(B + "/api/inventario/ancla", body)
      .then(r => r.json().then(j => ({ j })))
      .then(({ j }) => {
        setBusy(false);
        if (j && j.requiere_confirmacion) { setConfirmAviso(j.aviso); setMsg(""); return; }
        if (!j || j.ok === false) { setConfirmAviso(null); setMsg("✗ " + ((j && j.error) || "no se pudo guardar")); return; }
        setConfirmAviso(null);
        let extra = "";
        if (j.drift && (Math.abs(j.drift.usdt) > 0.01 || Math.abs(j.drift.clp) > 1)) {
          extra = " · drift vs estimado: " + invFmt(j.drift.usdt, 2) + " USDT / " + invFmt(j.drift.clp) + " CLP";
        }
        if (j.aviso_duplicado) extra += " · ⚠ " + j.aviso_duplicado;
        setMsg("✓ saldos actualizados" + extra);
        setForm(null); setANota(""); cargar();
        setTimeout(() => setMsg(""), 9000);
      })
      .catch(() => { setBusy(false); setMsg("✗ error de red"); });
  };

  const box = { background: "var(--bg-2)", border: "1px solid var(--line-soft)", borderRadius: 10, padding: "10px 13px" };
  const lbl = { fontSize: 10, color: "var(--text-3)", textTransform: "uppercase", letterSpacing: "0.08em" };
  const val = { fontFamily: "var(--mono)", fontSize: 19, color: "var(--text)", margin: "3px 0 1px", fontVariantNumeric: "tabular-nums" };
  const inp = { width: "100%", background: "var(--bg-1)", border: "1px solid var(--line)", borderRadius: 7,
                color: "var(--text)", fontFamily: "var(--mono)", fontSize: 13, padding: "7px 9px" };
  const btn = (activo) => ({ cursor: "pointer", borderRadius: 7, padding: "6px 12px", fontSize: 11.5,
                             fontFamily: "var(--mono)",
                             border: "1px solid " + (activo ? "var(--accent)" : "var(--line)"),
                             background: activo ? "var(--accent-soft)" : "var(--bg-2)",
                             color: activo ? "var(--accent)" : "var(--text-2)" });

  if (!d) return null;

  if (!d.configurado) {
    return (
      <div id="inv-card" style={{ margin: "10px 0 0", background: "var(--bg-1)", border: "1px solid var(--line)",
                                  borderLeft: "4px solid var(--text-3)", borderRadius: 14, padding: "13px 16px" }}>
        <div style={{ fontSize: 10.5, color: "var(--text-3)", textTransform: "uppercase", letterSpacing: "0.12em", marginBottom: 6 }}>Mi inventario en vivo</div>
        <div style={{ fontSize: 12.5, color: "var(--text-2)", marginBottom: 10 }}>{d.nota}</div>
        {form !== "ancla" && <button onClick={() => setForm("ancla")} style={btn(true)}>Fijar mis saldos</button>}
        {form === "ancla" && (
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "flex-end" }}>
            <div><div style={lbl}>USDT en Binance</div><input value={aU} onChange={e => setAU(e.target.value)} inputMode="decimal" style={{ ...inp, width: 120 }} /></div>
            <div><div style={lbl}>CLP en Mercado Pago</div><input value={aC} onChange={e => setAC(e.target.value)} inputMode="decimal" style={{ ...inp, width: 140 }} /></div>
            <div><div style={lbl}>Nota (opcional)</div><input value={aNota} onChange={e => setANota(e.target.value)} placeholder="apertura / cierre" style={{ ...inp, width: 150 }} /></div>
            <button disabled={busy} style={btn(true)} onClick={() => guardarAncla(false)}>Guardar</button>
            <button onClick={() => { setForm(null); setConfirmAviso(null); }} style={btn(false)}>Cancelar</button>
          </div>
        )}
        {confirmAviso && (
          <div style={{ marginTop: 8, padding: "8px 10px", background: "rgba(255,145,0,0.1)", border: "1px solid var(--warn)", borderRadius: 8, fontSize: 11.5, color: "var(--warn)" }}>
            ⚠ {confirmAviso}
            <div style={{ marginTop: 6, display: "flex", gap: 6 }}>
              <button onClick={() => guardarAncla(true)} style={{ ...btn(true), fontSize: 10.5, padding: "3px 9px" }}>Guardar igual</button>
              <button onClick={() => setConfirmAviso(null)} style={{ ...btn(false), fontSize: 10.5, padding: "3px 9px" }}>Cancelar</button>
            </div>
          </div>
        )}
        {msg && <div style={{ fontFamily: "var(--mono)", fontSize: 11.5, marginTop: 8, color: msg[0] === "✓" ? "var(--buy)" : "var(--warn)" }}>{msg}</div>}
      </div>
    );
  }

  const tono = INV_ZONA_COLOR[d.zona] || "var(--accent)";
  const req = d.reequilibrio || {};
  const det = d.detalle || {};
  const pnlPos = (d.pnl_dia_clp || 0) >= 0;

  return (
    <div id="inv-card" style={{ margin: "10px 0 0", background: "var(--bg-1)", border: "1px solid var(--line)",
                                borderLeft: "4px solid " + tono, borderRadius: 14, padding: "13px 16px" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", marginBottom: 10 }}>
        <span style={{ fontSize: 10.5, color: "var(--text-3)", textTransform: "uppercase", letterSpacing: "0.12em" }}>Mi inventario en vivo</span>
        <span title={d.nota} style={{ fontSize: 9.5, color: "var(--warn)", border: "1px solid var(--warn)",
                                      borderRadius: 5, padding: "1px 6px", cursor: "help" }}>ESTIMADO</span>
        <span style={{ fontSize: 10.5, color: "var(--text-3)" }}>ancla: {String(d.ancla.ts).slice(5, 16)}</span>
        {msg && <span style={{ fontFamily: "var(--mono)", fontSize: 11.5, marginLeft: "auto", color: msg[0] === "✓" ? "var(--buy)" : "var(--warn)" }}>{msg}</span>}
      </div>

      {d.alerta && (
        <div style={{ background: "var(--warn-soft)", border: "1px solid var(--warn)", borderRadius: 9,
                      padding: "8px 12px", marginBottom: 10, fontSize: 11.5, color: "var(--warn)" }}>
          ⚠ {d.alerta}
        </div>
      )}

      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginBottom: 12 }}>
        <div style={{ ...box, flex: 1.2, minWidth: 160 }}>
          <div style={lbl}>Patrimonio</div>
          <div style={val}>{invFmt(d.patrimonio_clp)} <span style={{ fontSize: 11, color: "var(--text-3)" }}>CLP</span></div>
          <div style={{ fontSize: 11, color: pnlPos ? "var(--buy)" : "var(--sell)" }}>
            {pnlPos ? "+" : ""}{invFmt(d.pnl_dia_clp)} CLP hoy
          </div>
        </div>
        <div style={{ ...box, flex: 1, minWidth: 130 }}>
          <div style={lbl}>USDT · Binance</div>
          <div style={val}>{invFmt(d.saldos.usdt, 2)}</div>
          <div style={{ fontSize: 10.5, color: "var(--text-3)" }}>≈ {invFmt(d.saldos.usdt * d.precio_ref_actual)} CLP</div>
        </div>
        <div style={{ ...box, flex: 1, minWidth: 130 }}>
          <div style={lbl}>CLP · Mercado Pago</div>
          <div style={val}>{invFmt(d.saldos.clp)}</div>
          <div style={{ fontSize: 10.5, color: "var(--text-3)" }}>ponderado ${invFmt(d.precio_ref_actual, 2)}</div>
        </div>
      </div>

      <div style={{ marginBottom: 10 }}>
        <div style={{ display: "flex", alignItems: "baseline", gap: 8, marginBottom: 2 }}>
          <span style={lbl}>Balance</span>
          <span style={{ fontFamily: "var(--mono)", fontSize: 15, fontWeight: 600, color: tono }}>{invFmt(d.pct_usdt, 1)}% en USDT</span>
          <span style={{ fontSize: 11.5, color: "var(--text-2)" }}>{d.zona_txt}</span>
        </div>
        <BarraBalance pct={d.pct_usdt} banda={d.banda} />
      </div>

      {d.zona !== "comoda" && (
        <div style={{ background: "var(--bg-2)", border: "1px solid " + tono, borderRadius: 10,
                      padding: "10px 13px", marginBottom: 10 }}>
          <div style={{ fontSize: 12.5, color: "var(--text)", lineHeight: 1.6 }}>
            <b style={{ color: tono }}>{req.modo === "cruzar" ? "Cruzá como taker" : "Repreciá agresivo"}</b>
            {" — "}{req.accion === "comprar" ? "comprá" : "vendé"} <b>{invFmt(req.usdt_a_mover, 1)} USDT</b>
            {req.precio_sugerido ? <> a <b style={{ fontFamily: "var(--mono)" }}>${invFmt(req.precio_sugerido, 2)}</b></> : null}
            {" "}para volver a la banda.
          </div>
          <div style={{ fontSize: 10.5, color: "var(--text-3)", marginTop: 4 }}>
            {req.modo === "cruzar"
              ? "Tomás el precio del otro: instantáneo, y la orden igual cuenta para los 300 de Merchant."
              : "Un centavo mejor que el líder del lado corto. Todavía capturás algo de spread."}
            {req.agresivo ? " · agresivo ${" + invFmt(req.agresivo, 2) + "}" : ""}
            {req.cruce ? " · cruce ${" + invFmt(req.cruce, 2) + "}" : ""}
          </div>
        </div>
      )}

      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
        <button onClick={() => { setForm(form === "ancla" ? null : "ancla"); setAU(String(d.saldos.usdt.toFixed(2))); setAC(String(Math.round(d.saldos.clp))); }} style={btn(form === "ancla")}>Actualizar saldos</button>
        <button onClick={() => setForm(form === "mov" ? null : "mov")} style={btn(form === "mov")}>Registrar movimiento</button>
        <button onClick={verHistorial} style={{ ...btn(histAbierto), fontSize: 11 }}>
          {histAbierto ? "ocultar historial" : "📋 historial de saldos"}
        </button>
        <button onClick={verMovs} style={{ ...btn(movsAbierto), fontSize: 11 }}
          title="Ver, corregir o borrar las órdenes que cargaste a mano">
          {movsAbierto ? "ocultar movimientos" : "✏️ corregir movimientos"}
        </button>
        <button onClick={() => setDetalle(!detalle)} style={{ ...btn(false), border: "none", background: "transparent", marginLeft: "auto" }}>
          {detalle ? "ocultar detalle" : "ver detalle"}
        </button>
      </div>

      {movsAbierto && (
        <div style={{ marginTop: 10, padding: "10px 12px", background: "var(--bg-2)", borderRadius: 10, border: "1px solid var(--line-soft)" }}>
          <div style={{ fontSize: 11.5, color: "var(--text-2)", marginBottom: 8 }}>
            Solo los movimientos que cargaste <b>a mano</b> (taker y externos), últimos 7 días.
            Los maker los deriva el monitor de los fills y no se editan: si esos están mal, lo que corresponde es re-anclar.
          </div>
          {movs === null && <div style={{ fontSize: 11.5, color: "var(--text-3)" }}>Cargando…</div>}
          {movs && movs.length === 0 && <div style={{ fontSize: 11.5, color: "var(--text-3)" }}>Sin movimientos manuales en los últimos 7 días.</div>}
          {movs && movs.length > 0 && (
            <div className="intel-scroll">
              <table className="intel-table">
                <thead><tr>
                  <th>Fecha y hora</th><th>Tipo</th><th>USDT</th><th>Precio</th><th>CLP</th><th></th>
                </tr></thead>
                <tbody>{movs.map(m => (
                  <tr key={m.id}>
                    <td className="tnum">{m.ts.replace("T", " ")}</td>
                    <td>{m.tipo}{m.lado ? " " + m.lado : ""}</td>
                    {editId === m.id ? (
                      <>
                        <td><input value={eU} onChange={e => setEU(e.target.value)} inputMode="decimal"
                              style={{ ...inp, width: 90, padding: "4px 7px", fontSize: 12 }} /></td>
                        <td><input value={eP} onChange={e => setEP(e.target.value)} inputMode="decimal"
                              style={{ ...inp, width: 90, padding: "4px 7px", fontSize: 12 }} /></td>
                        <td className="tnum" style={{ color: "var(--text-3)" }}>
                          {invFmt((parseFloat(String(eU).replace(",", ".")) || 0) * (parseFloat(String(eP).replace(",", ".")) || 0))}
                        </td>
                        <td style={{ whiteSpace: "nowrap" }}>
                          <button disabled={busy} onClick={() => guardarMov(m.id, false)}
                            style={{ ...btn(true), fontSize: 10.5, padding: "3px 9px" }}>Guardar</button>{" "}
                          <button onClick={() => { setEditId(null); setConfirmMov(null); }}
                            style={{ ...btn(false), fontSize: 10.5, padding: "3px 9px" }}>Cancelar</button>
                        </td>
                      </>
                    ) : (
                      <>
                        <td className="tnum">{invFmt(m.usdt, 2)}</td>
                        <td className="tnum">${invFmt(m.precio, 2)}</td>
                        <td className="tnum" style={{ color: "var(--text-3)" }}>{invFmt(m.clp)}</td>
                        <td style={{ whiteSpace: "nowrap" }}>
                          <button onClick={() => abrirEdicion(m)}
                            style={{ ...btn(false), fontSize: 10.5, padding: "3px 9px" }}>Editar</button>{" "}
                          <button disabled={busy} onClick={() => borrarMov(m)}
                            style={{ ...btn(false), fontSize: 10.5, padding: "3px 9px", color: "var(--sell)", borderColor: "var(--sell)" }}>Borrar</button>
                        </td>
                      </>
                    )}
                  </tr>
                ))}</tbody>
              </table>
            </div>
          )}
          {confirmMov && (
            <div style={{ marginTop: 8, padding: "8px 10px", background: "rgba(255,145,0,0.1)", border: "1px solid var(--warn)", borderRadius: 8, fontSize: 11.5, color: "var(--warn)" }}>
              ⚠ {confirmMov}
              <div style={{ marginTop: 6 }}>
                <button onClick={() => guardarMov(editId, true)}
                  style={{ ...btn(true), fontSize: 10.5, padding: "3px 9px" }}>Guardar igual</button>
              </div>
            </div>
          )}
        </div>
      )}

      {histAbierto && (
        <div style={{ marginTop: 10, padding: "10px 12px", background: "var(--bg-2)", borderRadius: 10, border: "1px solid var(--line-soft)" }}>
          <div style={{ display: "flex", alignItems: "center", marginBottom: 8 }}>
            <div style={{ fontSize: 11.5, color: "var(--text-2)" }}>
              Cada fila es un ancla real (nunca se pisan). Cruzalo con lo que anotaste en la bitácora.
            </div>
            <a href={B + "/api/inventario/historial?dias=30&fmt=csv"} download
               style={{ marginLeft: "auto", fontSize: 11, fontFamily: "var(--mono)", color: "var(--accent)",
                       textDecoration: "none", border: "1px solid var(--accent)", borderRadius: 7, padding: "3px 9px", whiteSpace: "nowrap" }}>
              ⬇ CSV
            </a>
          </div>
          {hist === null && <div style={{ fontSize: 11.5, color: "var(--text-3)" }}>Cargando…</div>}
          {hist && hist.length === 0 && <div style={{ fontSize: 11.5, color: "var(--text-3)" }}>Sin anclas en los últimos 30 días.</div>}
          {hist && hist.length > 0 && (
            <div className="intel-scroll">
              <table className="intel-table">
                <thead><tr>
                  <th>Fecha y hora</th><th>Nota</th><th>USDT</th><th>CLP</th>
                  <th title="Precio de referencia al anclar: el P2P (ponderado del libro) y el dólar formal del forex en ese momento. La brecha es cuánto estaba el P2P por encima del oficial.">Precio ref. (P2P / forex)</th>
                  <th title="Cambio respecto del ancla inmediatamente anterior. No es P&L: incluye depósitos/retiros y trades.">Δ desde el ancla previo</th>
                </tr></thead>
                <tbody>{hist.map((h, i) => (
                  <tr key={i}>
                    <td className="tnum">{h.ts.replace("T", " ")}</td>
                    <td>{h.nota || <span style={{ color: "var(--text-3)" }}>—</span>}</td>
                    <td className="tnum">{invFmt(h.usdt, 2)}</td>
                    <td className="tnum">{invFmt(h.clp)}</td>
                    <td className="tnum">
                      {h.precio_ref != null ? "$" + invFmt(h.precio_ref, 2) : "—"}
                      {h.usdclp_forex != null && (
                        <div style={{ fontSize: 10, color: "var(--text-3)" }}>
                          forex ${invFmt(h.usdclp_forex, 2)}
                          {h.brecha_pct != null && <span style={{ color: "var(--warn)" }}> · brecha {h.brecha_pct > 0 ? "+" : ""}{h.brecha_pct}%</span>}
                        </div>
                      )}
                    </td>
                    <td className="tnum" style={{ color: "var(--text-3)" }}>
                      {h.drift_usdt != null ? (h.drift_usdt >= 0 ? "+" : "") + invFmt(h.drift_usdt, 2) + " USDT / " + (h.drift_clp >= 0 ? "+" : "") + invFmt(h.drift_clp) + " CLP" : "—"}
                    </td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {form === "ancla" && (
        <div style={{ marginTop: 10, padding: "10px 12px", background: "var(--bg-2)", borderRadius: 10, border: "1px solid var(--line-soft)" }}>
          <div style={{ fontSize: 11.5, color: "var(--text-2)", marginBottom: 8 }}>
            Pegá los saldos <b>reales</b> de Binance y Mercado Pago. Esto vuelve a anclar la verdad y mide cuánto se había desviado la estimación.
          </div>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "flex-end" }}>
            <div><div style={lbl}>USDT en Binance</div><input value={aU} onChange={e => setAU(e.target.value)} inputMode="decimal" style={{ ...inp, width: 120 }} /></div>
            <div><div style={lbl}>CLP en Mercado Pago</div><input value={aC} onChange={e => setAC(e.target.value)} inputMode="decimal" style={{ ...inp, width: 140 }} /></div>
            <div><div style={lbl}>Nota (opcional)</div><input value={aNota} onChange={e => setANota(e.target.value)} placeholder="apertura / cierre" style={{ ...inp, width: 150 }} /></div>
            <button disabled={busy} style={btn(true)} onClick={() => guardarAncla(false)}>Guardar</button>
          </div>
          <div style={{ display: "flex", gap: 6, marginTop: 6 }}>
            <button onClick={() => setANota("Apertura")} style={{ ...btn(aNota === "Apertura"), fontSize: 10.5, padding: "3px 9px" }}>Apertura</button>
            <button onClick={() => setANota("Chequeo")} style={{ ...btn(aNota === "Chequeo"), fontSize: 10.5, padding: "3px 9px" }}>Chequeo</button>
            <button onClick={() => setANota("Cierre")} style={{ ...btn(aNota === "Cierre"), fontSize: 10.5, padding: "3px 9px" }}>Cierre</button>
          </div>
          {confirmAviso && (
            <div style={{ marginTop: 8, padding: "8px 10px", background: "rgba(255,145,0,0.1)", border: "1px solid var(--warn)", borderRadius: 8, fontSize: 11.5, color: "var(--warn)" }}>
              ⚠ {confirmAviso}
              <div style={{ marginTop: 6, display: "flex", gap: 6 }}>
                <button onClick={() => guardarAncla(true)} style={{ ...btn(true), fontSize: 10.5, padding: "3px 9px" }}>Guardar igual</button>
                <button onClick={() => setConfirmAviso(null)} style={{ ...btn(false), fontSize: 10.5, padding: "3px 9px" }}>Cancelar</button>
              </div>
            </div>
          )}
        </div>
      )}

      {form === "mov" && (
        <div style={{ marginTop: 10, padding: "10px 12px", background: "var(--bg-2)", borderRadius: 10, border: "1px solid var(--line-soft)" }}>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 8 }}>
            {[["taker", "Orden taker (crucé)"], ["externo", "Depósito / retiro"]].map(([k, t]) => (
              <button key={k} onClick={() => setMTipo(k)} style={btn(mTipo === k)}>{t}</button>
            ))}
          </div>
          {mTipo === "externo" ? (
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "flex-end" }}>
              <div><div style={lbl}>USDT (+ entra / − sale)</div><input value={mU} onChange={e => setMU(e.target.value)} inputMode="decimal" placeholder="0" style={{ ...inp, width: 130 }} /></div>
              <div><div style={lbl}>CLP (+ entra / − sale)</div><input value={mC} onChange={e => setMC(e.target.value)} inputMode="decimal" placeholder="0" style={{ ...inp, width: 140 }} /></div>
              <button disabled={busy} style={btn(true)}
                onClick={() => post("/api/inventario/movimiento", { tipo: "externo", usdt: parseFloat(String(mU).replace(",", ".")) || 0, clp: parseFloat(String(mC).replace(",", ".")) || 0 }, "movimiento externo cargado")}>Guardar</button>
              <div style={{ fontSize: 10.5, color: "var(--text-3)", flexBasis: "100%" }}>
                Un depósito/retiro <b>no es ganancia</b>: solo mueve el saldo. Por eso se marca aparte y no entra en el P&amp;L.
              </div>
            </div>
          ) : (
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "flex-end" }}>
              <div style={{ display: "flex", gap: 6 }}>
                {[["compra", "Compré USDT"], ["venta", "Vendí USDT"]].map(([k, t]) => (
                  <button key={k} onClick={() => setMLado(k)} style={btn(mLado === k)}>{t}</button>
                ))}
              </div>
              <div><div style={lbl}>USDT</div><input value={mU} onChange={e => setMU(e.target.value)} inputMode="decimal" style={{ ...inp, width: 100 }} /></div>
              <div><div style={lbl}>Precio</div><input value={mP} onChange={e => setMP(e.target.value)} inputMode="decimal" placeholder={String(d.precio_ref_actual)} style={{ ...inp, width: 110 }} /></div>
              <button disabled={busy} style={btn(true)}
                onClick={() => post("/api/inventario/movimiento", { tipo: "taker", lado: mLado, usdt: parseFloat(String(mU).replace(",", ".")), precio: parseFloat(String(mP).replace(",", ".")) }, "orden taker cargada")}>Guardar</button>
              <div style={{ fontSize: 10.5, color: "var(--text-3)", flexBasis: "100%" }}>
                El monitor no ve las órdenes taker (no dejan anuncio en el libro), por eso van a mano. Comisión fija 0,07 USDT.
              </div>
            </div>
          )}
        </div>
      )}

      {detalle && (
        <div style={{ marginTop: 10, padding: "10px 12px", background: "var(--bg-2)", borderRadius: 10,
                      border: "1px solid var(--line-soft)", fontSize: 11.5, color: "var(--text-2)", lineHeight: 1.8 }}>
          <div>Movimientos desde el ancla: <b>{det.movimientos}</b> ({det.maker} maker · {det.taker} taker · {det.externo} externo)</div>
          {(det.maker_observado_usdt != null || det.maker_estimado_usdt != null) && (() => {
            const obs = det.maker_observado_usdt || 0, est = det.maker_estimado_usdt || 0, tot = obs + est;
            const pct = tot > 0 ? Math.round(est / tot * 100) : 0;
            return (
              <div title="Observado = se vio caer el stock de tu anuncio. Estimado = subió tu contador de órdenes sin caída visible (recargaste en el mismo ciclo), y se calcula con TU ticket medio. Cuanto más alto el % estimado, más conviene re-anclar.">
                De lo maker: <b>{invFmt(obs, 2)} USDT</b> observados ·{" "}
                <b style={{ color: pct >= 40 ? "var(--warn)" : "var(--text)" }}>{invFmt(est, 2)} USDT</b> estimados
                <span style={{ color: "var(--text-3)" }}> ({pct}% del total)</span>
              </div>
            );
          })()}
          <div>Comisiones pagadas: <b>{invFmt(det.comisiones_usdt, 2)} USDT</b></div>
          <div>Revaluación del USDT que ya tenías: <b style={{ color: (det.revaluacion_clp || 0) >= 0 ? "var(--buy)" : "var(--sell)" }}>{invFmt(det.revaluacion_clp)} CLP</b> <span style={{ color: "var(--text-3)" }}>(el precio se movió, no operaste)</span></div>
          <div>Costo de campaña (trading, sin revaluación): <b style={{ color: (det.costo_campania_clp || 0) >= 0 ? "var(--buy)" : "var(--sell)" }}>{invFmt(det.costo_campania_clp)} CLP</b></div>
          {det.aporte_externo_clp ? <div>Depósitos/retiros netos: <b>{invFmt(det.aporte_externo_clp)} CLP</b> <span style={{ color: "var(--text-3)" }}>(fuera del P&amp;L)</span></div> : null}
          <div style={{ color: "var(--text-3)", marginTop: 4 }}>
            Farmear cuesta plata a propósito en esta fase: el objetivo son órdenes y reputación, no margen. Este número es el peaje.
          </div>
        </div>
      )}
    </div>
  );
}

function CalculadoraCruzar() {
  /* Logica EXACTA del prototipo Prototipo_Calculadora_Cruzar.html.
     Doble objetivo: decidir Y entender el concepto compra/venta. */
  const B = (window.P2P_CONFIG && window.P2P_CONFIG.baseUrl) || "";
  const C_MAKER = 0.002, C_TAKER = 0.07;
  const [side, setSide] = React.useState("vendi");
  const [qty, setQty] = React.useState("100");
  const [pmine, setPmine] = React.useState("941.5");
  const [pcross, setPcross] = React.useState("943");
  const [abierto, setAbierto] = React.useState(false);

  // autocompletar con los precios del Asistente (editable a mano igual)
  React.useEffect(() => {
    if (!abierto) return;
    fetch(B + "/api/operativa").then(r => r.json()).then(j => {
      const p = j && j.precios; if (!p) return;
      if (side === "vendi") {
        if (p.flujo_vender) setPmine(String(p.flujo_vender));
        if (p.agresivo_comprar) setPcross(String(p.agresivo_comprar));
      } else {
        if (p.flujo_comprar) setPmine(String(p.flujo_comprar));
        if (p.agresivo_vender) setPcross(String(p.agresivo_vender));
      }
    }).catch(() => {});
  }, [abierto, side]);

  const q = parseFloat(String(qty).replace(",", ".")) || 0;
  const pm = parseFloat(String(pmine).replace(",", ".")) || 0;
  const pc = parseFloat(String(pcross).replace(",", ".")) || 0;
  const bruto = side === "vendi" ? (pm - pc) * q : (pc - pm) * q;
  const cM = C_MAKER * pm * q, cT = C_TAKER * pc;
  const neto = bruto - cM - cT;
  const fmt = (n) => (n < 0 ? "−" : "") + "$" + Math.abs(Math.round(n)).toLocaleString("es-CL");

  const inp = { width: "100%", background: "var(--bg-1)", border: "1px solid var(--line)", borderRadius: 8,
                color: "var(--text)", fontFamily: "var(--mono)", fontSize: 14, padding: "8px 10px" };
  const lbl = { fontSize: 11, color: "var(--text-2)", display: "block", marginBottom: 4 };

  if (!abierto) {
    return (
      <button onClick={() => setAbierto(true)}
        style={{ margin: "10px 0 0", cursor: "pointer", background: "var(--bg-1)",
                 border: "1px solid var(--line)", borderRadius: 10, padding: "8px 13px",
                 color: "var(--text-2)", fontSize: 12, fontFamily: "var(--font)" }}>
        🧮 ¿Conviene cruzar? <span style={{ color: "var(--text-3)" }}>— calculadora</span>
      </button>
    );
  }
  return (
    <div style={{ margin: "10px 0 0", background: "var(--bg-1)", border: "1px solid var(--line)",
                  borderRadius: 14, padding: "13px 16px", maxWidth: 460 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
        <span style={{ fontSize: 13.5, fontWeight: 600 }}>¿Conviene cruzar?</span>
        <button onClick={() => setAbierto(false)}
          style={{ marginLeft: "auto", background: "transparent", border: "none", color: "var(--text-3)", cursor: "pointer", fontSize: 11 }}>cerrar</button>
      </div>
      <div style={lbl}>¿Qué te pasa ahora?</div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8, marginBottom: 14 }}>
        {[["vendi", "Vendí dólares", "me falta recomprar"], ["compre", "Compré dólares", "me falta vender"]].map(([k, t, s]) => (
          <button key={k} onClick={() => setSide(k)}
            style={{ fontSize: 12.5, padding: "9px 6px", borderRadius: 8, cursor: "pointer", lineHeight: 1.3,
                     border: side === k ? "2px solid var(--accent)" : "1px solid var(--line)",
                     background: side === k ? "var(--accent-soft)" : "transparent",
                     color: side === k ? "var(--accent)" : "var(--text-2)" }}>
            {t}<span style={{ fontSize: 10, display: "block" }}>{s}</span>
          </button>
        ))}
      </div>
      <label style={lbl}>Cuántos USDT</label>
      <input value={qty} onChange={e => setQty(e.target.value)} inputMode="decimal" style={{ ...inp, marginBottom: 13 }} />
      <label style={lbl}>{side === "vendi" ? "Precio al que ya vendí (mi anuncio maker)" : "Precio al que ya compré (mi anuncio maker)"}</label>
      <input value={pmine} onChange={e => setPmine(e.target.value)} inputMode="decimal" style={{ ...inp, marginBottom: 13 }} />
      <label style={lbl}>{side === "vendi" ? "Precio al que recompro ahora cruzando (taker)" : "Precio al que vendo ahora cruzando (taker)"}</label>
      <input value={pcross} onChange={e => setPcross(e.target.value)} inputMode="decimal" style={{ ...inp, marginBottom: 13 }} />
      <div style={{ borderRadius: 10, padding: "14px 16px",
                    background: neto >= 0 ? "var(--buy-soft)" : "var(--warn-soft)" }}>
        <div style={{ fontSize: 11, color: "var(--text-2)" }}>Resultado de la vuelta completa</div>
        <div style={{ fontFamily: "var(--mono)", fontSize: 26, fontWeight: 600,
                      color: neto >= 0 ? "var(--buy)" : "var(--warn)" }}>{fmt(neto)} CLP</div>
        <div style={{ fontSize: 12.5, marginTop: 5, color: neto >= 0 ? "var(--buy)" : "var(--warn)" }}>
          {neto >= 0
            ? "✓ Cerrás en ganancia — cruzá tranquilo."
            : "Perdés esto — es el peaje. Conviene si estás trabado (te destraba y suma 1 orden). Si no urge, esperá y hacelo como maker."}
        </div>
      </div>
      <div style={{ fontSize: 11, color: "var(--text-3)", marginTop: 12, lineHeight: 1.6 }}>
        Cuenta: diferencia de precio {fmt(bruto)} − comisión maker {fmt(cM)} − comisión taker {fmt(cT)} (0,07 USDT fija).
      </div>
    </div>
  );
}

window.P2PViews = { CarreraMerchant, PreciosCompactos, EstrategiaRapida, MacroBar, TiempoReal, Historico, Heatmap, PrecioChart, Inteligencia, Backup, RotacionCalc, CrossView, Muros, SystemBar, VolumenBar, VelocidadMercado, AsistenteOperativo, EstrategiaPanel, MiCampania, PlanHoy, InventarioCard, ChipBalance, CalculadoraCruzar, RutinasPanel };

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
    const cfg = window.P2P_CONFIG || {};
    const eng = window.P2P.createLiveEngine({ baseUrl: cfg.baseUrl || "", pollMs: cfg.pollMs || 30000, intervaloMin: cfg.intervaloMin || 5 });
    engRef.current = eng;
    const unsub = eng.subscribe((s) => setState(s));
    return () => { unsub(); eng.stop(); };
  }, []);
  return [state, engRef];
}

function App() {
  const [t, setTweak] = useTweaks(TWEAK_DEFAULTS);
  const beta = !!window.P2P_BETA;      // la ruta /beta lo pone en true
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
      {tab !== "backup" && <V.RutinasPanel onGoBackup={() => setTab("backup")} />}
      <main className="content">
        {/* ── LAYOUT BETA (COL36): grilla en vez de pila ──────────────
            Mismos componentes, otra disposicion. Objetivo: que lo que se
            decide MIRANDO entre en la primera pantalla. El grafico historico
            del ponderado se saca de aca (vive en la pestaña Precio) y el
            libro/listas quedan al final, que es donde se consultan, no donde
            se decide. */}
        {tab === "tr" && beta && (
          <div className="beta-grid">
            <div className="bc-12"><V.PreciosCompactos snap={viewSnap} /></div>
            <div className="bc-7">
              <V.PlanHoy />
              <V.EstrategiaRapida />
            </div>
            <div className="bc-5"><V.AsistenteOperativo /></div>
            <div className="bc-7"><V.CarreraMerchant /></div>
            <div className="bc-5">
              <V.ChipBalance />
              <V.InventarioCard />
            </div>
            <div className="bc-6"><V.MacroBar modo="card" /></div>
            <div className="bc-6"><V.MiCampania /></div>
            <div className="bc-12"><V.VelocidadMercado /></div>
            <div className="bc-12"><V.CalculadoraCruzar /></div>
            <div className="bc-12">
              <V.TiempoReal snap={viewSnap} history={history} showOrderBook={t.orderBook} vel={vel}
                filters={{ cfg: filters, onApply: applyFilters, info: viewSnap._filtro }} sinGrafico />
            </div>
          </div>
        )}
        {tab === "tr" && !beta && <V.PlanHoy />}
        {tab === "tr" && !beta && <V.MacroBar modo="card" />}
        {tab === "tr" && !beta && <V.ChipBalance />}
        {tab === "tr" && !beta && <V.EstrategiaPanel />}
        {tab === "tr" && !beta && <V.MiCampania />}
        {tab === "tr" && !beta && <V.CarreraMerchant />}
        {tab === "tr" && !beta && <V.InventarioCard />}
        {tab === "tr" && !beta && <V.AsistenteOperativo />}
        {tab === "tr" && !beta && <V.CalculadoraCruzar />}
        {tab === "tr" && !beta && <V.VelocidadMercado />}
        {tab === "tr" && !beta && <V.TiempoReal snap={viewSnap} history={history} showOrderBook={t.orderBook} vel={vel}
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
#  DASHBOARD BETA (COL21) — copia de trabajo para el rediseño.
#  Arranca IDENTICA a DASHBOARD a proposito: la ruta /beta se prueba primero
#  sin ningun cambio visual, y el rediseño se va editando SOLO aca adentro.
#  La ruta "/" y la variable DASHBOARD de arriba no se tocan.
# ──────────────────────────────────────────────
DASHBOARD_BETA = DASHBOARD

# ──────────────────────────────────────────────
#  RUTAS
# ──────────────────────────────────────────────
@app.route("/")
def index():
    html = DASHBOARD.replace("{{VERSION}}", f"{VERSION} · {VERSION_FECHA}")
    return Response(html, mimetype='text/html')

@app.route("/beta")
def index_beta():
    """Version beta del dashboard, para probar el rediseno sin afectar '/'.

    MISMO HTML, un flag distinto: window.P2P_BETA pasa a true y el layout usa
    grilla en vez de apilar todo en una columna. Asi la beta no es una copia
    que hay que mantener en paralelo — es el mismo codigo con otra disposicion,
    y cualquier arreglo aplica a las dos."""
    html = (DASHBOARD_BETA
            .replace("{{VERSION}}", f"{VERSION} · {VERSION_FECHA} · beta")
            .replace("window.P2P_BETA = false;", "window.P2P_BETA = true;"))
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
    # EDAD calculada por el SERVIDOR (COL23): cuantos segundos hace que se guardo
    # el snapshot, segun el reloj del server. Asi la frescura NO depende del reloj
    # del dispositivo del usuario. Antes el cliente hacia now - timestamp con su
    # propio reloj; si estaba en otra zona horaria (ej. viajar Chile->Argentina),
    # todo se veia "hace 1h" y disparaba "SIN DATOS EN VIVO" con el backend sano.
    try:
        ts = datetime.strptime(str(snap.get("timestamp"))[:19], "%Y-%m-%d %H:%M:%S")
        ts = ts.replace(tzinfo=SANTIAGO_TZ)
        snap["edad_seg"] = max(0, round((datetime.now(SANTIAGO_TZ) - ts).total_seconds()))
    except Exception:
        snap["edad_seg"] = None
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


@app.route("/api/inteligencia/doble_precio")
def api_doble_precio():
    """DOBLE PRECIO POR TICKET (COL33): mide la estrategia de publicar 2+ avisos
    del mismo lado a precios distintos, segmentados por el minimo de orden.
    Pliego: documentos/Estrategia_Doble_Precio_por_Ticket.md
    Params: ?dias=7&min_vol=2000

    ── POR QUE NO SE USA fills_estimados ──
    Seria lo natural, pero NO SIRVE para esto: _agrupar_items() fusiona los
    avisos de un anunciante en uno solo y guarda "el precio del mejor puesto"
    (hay una razon: 'completadas' es por CUENTA, no por aviso, asi que sin
    fusionar el tracker inflaba el volumen ~31 por ciento). Consecuencia: en un
    anunciante dual, fills_estimados.precio es SIEMPRE el de la vidriera, y
    atribuir volumen por ese campo da ~0 para la caja — medido y descartado.

    ── COMO SE MIDE ENTONCES ──
    Por la CAIDA DE STOCK de cada aviso, que si es propia de cada uno. Mismo
    metodo anti-reposicionamiento que recalibrar_bandas(): la caida cuenta
    como consumo solo si el precio no cambio en ese paso (si cambio, fue una
    edicion del aviso, no una venta).

    ── LIMITACION QUE HAY QUE MIRAR SIEMPRE ──
    Un 23 por ciento de los duales muestra el MISMO 'disponible' en sus dos
    avisos (inventario compartido, espejado por la API). En esos casos la
    caida no se puede atribuir a un aviso concreto y el reparto NO es valido:
    se devuelve 'stock_compartido_pct' alto y 'analizable': false. Medido en
    44h: su lado SELL comparte (97 por ciento) y su lado BUY no (0)."""
    try:
        dias = max(1, min(30, int(request.args.get("dias", 7))))
    except (ValueError, TypeError):
        dias = 7
    try:
        min_vol = max(0.0, float(request.args.get("min_vol", 2000)))
    except (ValueError, TypeError):
        min_vol = 2000.0

    # OJO ZONA HORARIA: los ts se guardan en hora Chile NAIVE y la DB corre en
    # UTC, asi que NOW() esta 4 h adelantado y una ventana corta daria 0 filas.
    # Se pasa la fecha ya formateada en hora Chile, como hace _registrar_operativa.
    desde = (datetime.now(SANTIAGO_TZ) - timedelta(days=dias)).strftime("%Y-%m-%d %H:%M:%S")

    sql = """
        WITH ads AS (
            SELECT snapshot_timestamp ts, anunciante, tipo, precio, disponible,
                   posicion, min_orden, max_orden
            FROM snapshots_detalle
            WHERE snapshot_timestamp >= %(d)s AND precio > 0
              AND anunciante IS NOT NULL AND anunciante <> ''
        ),
        multi AS (
            SELECT ts, anunciante, tipo, COUNT(*) n_ads,
                   COUNT(DISTINCT disponible) n_disp
            FROM ads GROUP BY 1,2,3 HAVING COUNT(*) > 1
        ),
        rk AS (
            SELECT a.*, m.n_ads, m.n_disp,
                   ROW_NUMBER() OVER (
                       PARTITION BY a.ts, a.anunciante, a.tipo
                       ORDER BY CASE WHEN a.tipo='BUY' THEN a.precio ELSE -a.precio END
                   ) rol
            FROM ads a JOIN multi m USING (ts, anunciante, tipo)
        ),
        pasos AS (
            SELECT anunciante, tipo, rol, ts, precio, disponible, posicion,
                   min_orden, max_orden, n_disp,
                   LAG(disponible) OVER w disp_prev,
                   LAG(precio)     OVER w precio_prev,
                   EXTRACT(EPOCH FROM (ts - LAG(ts) OVER w))/60 gap_min
            FROM rk
            WINDOW w AS (PARTITION BY anunciante, tipo, rol ORDER BY ts)
        ),
        consumo AS (
            SELECT anunciante, tipo, rol, precio, posicion, min_orden, max_orden, n_disp,
                   CASE WHEN precio_prev = precio AND disp_prev > disponible
                             AND gap_min BETWEEN 0 AND 10
                        THEN disp_prev - disponible ELSE 0 END consumido
            FROM pasos
        )
        SELECT anunciante, tipo, rol,
               COUNT(*) muestras,
               SUM(consumido) volumen,
               PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY precio)   precio_med,
               PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY posicion) pos_med,
               mode() WITHIN GROUP (ORDER BY min_orden) min_moda,
               mode() WITHIN GROUP (ORDER BY max_orden) max_moda,
               AVG(CASE WHEN n_disp = 1 THEN 1.0 ELSE 0.0 END) frac_compartido
        FROM consumo
        GROUP BY 1,2,3
        ORDER BY 1,2,3
    """
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql, {"d": desde})
                filas = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f"[doble_precio] {e}")
        return jsonify({"error": str(e)[:200]}), 500

    # agrupar por (anunciante, lado) en Python: mas legible que en SQL
    grupos = {}
    for r in filas:
        grupos.setdefault((r["anunciante"], r["tipo"]), []).append(r)

    salida = []
    for (anun, tipo), roles in grupos.items():
        if len(roles) < 2:
            continue
        roles.sort(key=lambda r: int(r["rol"]))
        vol_tot = sum(float(r["volumen"] or 0) for r in roles)
        if vol_tot < min_vol:
            continue
        vid, caja = roles[0], roles[1]
        vol_caja = sum(float(r["volumen"] or 0) for r in roles[1:])
        p_vid, p_caja = float(vid["precio_med"]), float(caja["precio_med"])
        frac_comp = max(float(r["frac_compartido"] or 0) for r in roles)

        # segmenta por minimo? Solo se puede afirmar si HAY dato de limites.
        mv, mc = vid["min_moda"], caja["min_moda"]
        if mv is None or mc is None:
            segmenta = None            # todavia sin datos de limites
        elif int(mv) > int(mc):
            segmenta = True            # la vidriera pide minimo mas alto: el patron
        else:
            segmenta = False           # mismo minimo o invertido: no segmenta por ticket

        salida.append({
            "anunciante": anun, "lado": tipo, "n_roles": len(roles),
            "volumen_total": round(vol_tot),
            "pct_por_la_caja": round(vol_caja / vol_tot * 100, 1) if vol_tot else None,
            "spread_propio_pct": round(abs(p_caja - p_vid) / p_vid * 100, 3) if p_vid else None,
            "stock_compartido_pct": round(frac_comp * 100),
            # si comparten inventario, el reparto por aviso no significa nada
            "analizable": frac_comp < 0.5,
            "segmenta_por_minimo": segmenta,
            "vidriera": {"precio": round(p_vid, 2), "pos": round(float(vid["pos_med"]), 1),
                         "min_orden": int(mv) if mv else None,
                         "max_orden": int(vid["max_moda"]) if vid["max_moda"] else None,
                         "volumen": round(float(vid["volumen"] or 0))},
            "caja": {"precio": round(p_caja, 2), "pos": round(float(caja["pos_med"]), 1),
                     "min_orden": int(mc) if mc else None,
                     "max_orden": int(caja["max_moda"]) if caja["max_moda"] else None,
                     "volumen": round(vol_caja)},
        })

    salida.sort(key=lambda x: -x["volumen_total"])
    validos = [x for x in salida if x["analizable"]]
    pcts = sorted(x["pct_por_la_caja"] for x in validos if x["pct_por_la_caja"] is not None)
    con_lim = [x for x in validos if x["segmenta_por_minimo"] is not None]
    return jsonify({
        "dias": dias,
        "casos": salida,
        "resumen": {
            "pares_multi_aviso": len(salida),
            "analizables": len(validos),
            "descartados_por_stock_compartido": len(salida) - len(validos),
            "mediana_pct_por_la_caja": (pcts[len(pcts) // 2] if pcts else None),
            "con_dato_de_limites": len(con_lim),
            "segmentan_por_minimo": sum(1 for x in con_lim if x["segmenta_por_minimo"]),
        },
        "nota": ("El reparto sale de la CAIDA DE STOCK de cada aviso (con filtro "
                 "anti-reposicionamiento), no del contador de ordenes: ese es por CUENTA "
                 "y no se puede repartir entre los avisos. Los casos con inventario "
                 "compartido salen marcados analizable=false. 'segmenta_por_minimo' queda "
                 "en null hasta que haya dias de limites capturados (COL32, 29-jul-2026)."),
    })


@app.route("/api/competidores")
def api_competidores():
    """BASE DE COMPETIDORES: una fila por anunciante con todo lo observable,
    para ordenar y filtrar por cualquier caracteristica.

    Cada columna dice de donde sale, porque la confianza es distinta:
      - ordenes/dia  -> contador OFICIAL de Binance (dato duro, no estimacion)
      - volumen/ticket -> estimado por el tracker (solo fills 'directo')
      - capital, posicion, cobertura -> observado del libro
      - dual y gap propio -> del libro EN VIVO (ahora mismo)
    Params: ?dias=7 (ventana de analisis)"""
    try:
        dias = max(2, min(14, int(request.args.get("dias", 7))))
    except (ValueError, TypeError):
        dias = 7
    filas = []
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SET LOCAL statement_timeout = '60s'")
                cur.execute("""
                    WITH d AS (
                        SELECT anunciante, tipo,
                               COUNT(*) apar,
                               AVG(posicion) pos,
                               AVG(disponible) stock,
                               BOOL_OR(es_merchant) merch,
                               MAX(completadas) - MIN(completadas) ord_per,
                               COUNT(DISTINCT snapshot_timestamp::date) dias,
                               COUNT(DISTINCT EXTRACT(HOUR FROM snapshot_timestamp)) horas
                        FROM snapshots_detalle
                        WHERE snapshot_timestamp >= NOW() - (%(d)s || ' days')::INTERVAL
                          AND anunciante IS NOT NULL AND anunciante <> ''
                        GROUP BY 1,2
                    ), a AS (
                        -- OJO: ord_per sale del contador POR CUENTA, los dos lados
                        -- traen el mismo numero -> MAX, nunca SUM.
                        SELECT anunciante, MAX(ord_per) ordenes, COUNT(*) lados,
                               SUM(stock) capital, AVG(pos) pos_media, MIN(pos) pos_mejor,
                               BOOL_OR(merch) merch, MAX(dias) dias, MAX(horas) horas
                        FROM d GROUP BY 1
                    ), f AS (
                        -- volumen: TODOS los metodos (v2 completo). El ticket en
                        -- cambio solo de 'directo', que es tamano observado y no
                        -- estimado (si no, se realimentaria el propio supuesto).
                        SELECT anunciante,
                               SUM(monto) vol,
                               SUM(monto) FILTER (WHERE metodo='directo') vol_obs,
                               AVG(monto / NULLIF(ordenes,0)) FILTER (WHERE metodo='directo') ticket
                        FROM fills_estimados
                        WHERE exchange='binance'
                          AND ts >= NOW() - (%(d)s || ' days')::INTERVAL
                        GROUP BY 1
                    )
                    SELECT a.*, f.vol, f.vol_obs, f.ticket
                    FROM a LEFT JOIN f ON f.anunciante = a.anunciante
                    WHERE a.dias >= 2
                    ORDER BY a.ordenes DESC NULLS LAST
                    LIMIT 400
                """, {"d": dias})
                crudo = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f"[competidores] {e}")
        return jsonify({"filas": [], "error": str(e)[:200]})

    # estado EN VIVO: quien esta publicado ahora y con que gap propio
    with data_lock:
        snap = dict(ultimo_estado)
    vivo = {}
    for key, lado in (("detalle_compra", "venta"), ("detalle_venta", "compra")):
        for row in (snap.get(key) or []):
            n = (row.get("anunciante") or "").strip().lower()
            if n:
                vivo.setdefault(n, {})[lado] = row

    with config_lock:
        com_maker = float(config.get("COM_MAKER_PCT", 0.20))
    for r in crudo:
        nom = (r["anunciante"] or "").strip()
        dias_obs = max(1, int(r["dias"] or 1))
        ordenes = float(r["ordenes"] or 0)
        vol = float(r["vol"] or 0)
        cap = float(r["capital"] or 0)
        v = vivo.get(nom.lower(), {})
        gap = None
        if "venta" in v and "compra" in v:
            try:
                pv, pc = float(v["venta"]["precio"]), float(v["compra"]["precio"])
                if pv > 0 and pc > 0:
                    gap = round((pv - pc) / pc * 100, 3)
            except (ValueError, TypeError):
                pass
        vol_dia = vol / dias_obs                      # v2 completo (todos los metodos)
        vol_obs_dia = float(r["vol_obs"] or 0) / dias_obs   # solo lo visto directo
        ord_dia = ordenes / dias_obs
        tk = float(r["ticket"]) if r["ticket"] else None
        # Dos estimaciones del volumen, cada una con su debilidad:
        #  - v2 (vol_dia): suma lo que el tracker registro, incluyendo lo que
        #    estimo cuando el anunciante recargo al instante.
        #  - implicito: ordenes OFICIALES x ticket observado.
        # Medido sobre 12 anunciantes grandes: v2 da el 90% del implicito en
        # agregado, pero por anunciante va del 7% al 187%. Se toma el promedio
        # de ambas cuando hay las dos, que es mas estable que cualquiera sola.
        vol_impl = (ord_dia * tk) if (tk and ord_dia) else None
        if vol_impl and vol_dia:
            vol_ref = (vol_impl + vol_dia) / 2
        else:
            vol_ref = vol_impl or vol_dia
        filas.append({
            "anunciante": nom,
            "merchant": bool(r["merch"]),
            "ordenes_dia": round(ord_dia, 1),
            "volumen_dia": round(vol_ref) if vol_ref else 0,
            "volumen_observado": round(vol_obs_dia),
            # cuanto del volumen se VIO directamente (vs se estimo): indicador
            # de confianza de la fila, no un error
            "deteccion_pct": round(vol_obs_dia / vol_ref * 100) if (vol_ref and vol_ref > 0) else None,
            "ticket": round(tk) if tk else None,
            "capital": round(cap),
            "pos_media": round(float(r["pos_media"]), 1) if r["pos_media"] else None,
            "pos_mejor": round(float(r["pos_mejor"])) if r["pos_mejor"] else None,
            "cobertura_h": int(r["horas"] or 0),
            "lados": int(r["lados"] or 0),
            "giros_dia": round(vol_ref / cap, 1) if (cap > 0 and vol_ref) else None,
            "en_libro": bool(v),
            "dual_ahora": len(v) == 2,
            "gap_propio": gap,
            # ganancia bruta estimada: solo si sabemos su gap (esta dual ahora)
            "ganancia_mes_est": round(vol_ref * 30 * (gap - com_maker * 2) / 100) if (gap and vol_ref) else None,
        })
    return jsonify({
        "dias": dias, "filas": filas, "total": len(filas),
        "nota": ("ordenes_dia sale del contador oficial de Binance. volumen y ticket son "
                 "estimados por el tracker (solo fills observados). gap y dual son del libro "
                 "EN VIVO, por eso solo aparecen si el anunciante esta publicado ahora."),
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
                           disp_medio, ordenes_dia, es_merchant,
                           min_orden_med, max_orden_med, min_orden_moda
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
                              "disponible": round(float(row.get("disponible") or 0)),
                              # limites que declara AHORA (COL32)
                              "min_orden": row.get("min_orden"),
                              "max_orden": row.get("max_orden")}
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
    # ── limites de orden historicos (COL32) ──
    # La MODA es la referencia: si el anunciante edito el limite un rato, el
    # promedio se ensucia y la moda sigue mostrando con cual opera de verdad.
    # Todo puede venir NULL (dias anteriores al deploy) y eso no rompe nada.
    _modas = [int(h["min_orden_moda"]) for h in hist if h.get("min_orden_moda")]
    _maxs  = [int(h["max_orden_med"]) for h in hist if h.get("max_orden_med")]
    limites = None
    if _modas:
        _modas.sort()
        limites = {"min_habitual": _modas[len(_modas) // 2],
                   "min_visto_desde": _modas[0], "min_visto_hasta": _modas[-1],
                   "max_habitual": (sorted(_maxs)[len(_maxs) // 2] if _maxs else None),
                   "dias_con_dato": len(_modas)}
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
        "limites": limites,
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


def _tandas_recompra():
    """CUANTO RECOMPRA DE VERDAD, medido de sus propias ordenes (COL44b).

    Por que existe: los presets del Ciclo eran numeros redondos puestos a
    mano (300/600/1200/2500) y el default de config tambien. Medido sobre las
    compras TAKER reales (que son las recompras rapidas de la estrategia):
    p25=210 · mediana=235 · p75=319 · max historico=425 USDT.
    O sea que 600, 1200 y 2500 estaban TODOS fuera de lo que alguna vez hizo,
    y ofrecer "tu saldo completo" (~638) como preset sugeria una operacion
    que nunca ocurrio: la estrategia es vender de a poco e ir recomprando en
    tandas mientras tanto, no esperar a vender todo para recomprar de una.
    Verificado en los datos: el 30-jul recompro 11:03, 11:48 y 12:27 — tres
    tandas en 90 minutos.

    Se usan las TAKER porque son las recompras deliberadas (cruzar para
    reponer stock rapido). Las maker de compra son otra cosa: son el anuncio
    de compra llenandose solo, con ticket mucho menor (mediana 58)."""
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT to_regclass('public.mis_ordenes_reales')")
                if cur.fetchone()["to_regclass"] is None:
                    return None
                cur.execute("""
                    SELECT COUNT(*) AS n,
                           PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY usdt) AS p25,
                           PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY usdt) AS p50,
                           PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY usdt) AS p75,
                           MAX(usdt) AS maximo
                    FROM mis_ordenes_reales
                    WHERE estado = 'completada' AND lado = 'compra' AND rol = 'taker'
                      AND usdt > 0
                """)
                r = cur.fetchone()
    except Exception as e:
        print(f"[tandas] {e}")
        return None
    if not r or not r["n"] or int(r["n"]) < 5:
        return None      # con menos de 5 recompras no hay percentil que valga
    def _r(v):
        return int(round(float(v) / 10) * 10) if v else None
    return {"n": int(r["n"]), "chica": _r(r["p25"]), "habitual": _r(r["p50"]),
            "grande": _r(r["p75"]), "maxima": _r(r["maximo"])}


@app.route("/api/pnl_ciclos")
def api_pnl_ciclos():
    """P&L REAL POR CICLO (COL42): el numero mas pedido en la auditoria del
    31-jul -- no cuanto CREE el monitor que se gano, sino cuanto se gano DE
    VERDAD, orden por orden, con costo base real en vez de un margen teorico.

    METODO: costo promedio ponderado (weighted-average cost), igual que
    cualquier libro contable de inventario fungible -- el USDT es fungible,
    no tiene sentido tratar de adivinar CUAL dolar puntual se vendio.
    Recorre mis_ordenes_reales EN ORDEN CRONOLOGICO (siempre desde el
    principio, nunca solo 'los ultimos N dias': el costo base depende de
    TODA la historia previa, filtrar el rango de entrada lo rompe):
    - Cada COMPRA actualiza el costo promedio: nuevo_promedio =
      (promedio_viejo*stock_viejo + precio*monto) / (stock_viejo+monto).
    - Cada VENTA realiza P&L contra ese promedio: (precio_venta - costo_base)
      * monto, menos la comision de ESA orden (convertida a CLP).

    LO QUE NO SE INVENTA: la primera orden del historial (3-may-2026) es una
    VENTA -- no hay compra previa que le de costo base. Cualquier venta que
    exceda el USDT con costo base conocido (probable senal de que ese USDT
    se fondeo por fuera del P2P, ej. una compra en otro exchange) se separa
    en 'sin_costo_base' en vez de forzarle un costo inventado. Medido al
    4-ago: 13 de 115 ventas (779 USDT) caen en este caso -- quedan afuera del
    P&L, no adentro con un numero falso.

    Params: ?dias=N (opcional, solo filtra que ciclos se listan/exportan --
    el calculo interno SIEMPRE usa el historial completo, ver arriba)."""
    fmt = (request.args.get("fmt", "json") or "json").lower()
    try:
        dias = int(request.args.get("dias", 0)) or None
    except (ValueError, TypeError):
        dias = None

    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT to_regclass('public.mis_ordenes_reales')")
                if cur.fetchone()["to_regclass"] is None:
                    return jsonify({"configurado": False,
                                    "nota": "todavia no importaste el CSV (scripts/importar_mis_ordenes.bat)"})
                cur.execute("""
                    SELECT orden_id, ts, lado, rol, usdt, precio, comision
                    FROM mis_ordenes_reales
                    WHERE estado='completada' AND usdt IS NOT NULL AND precio IS NOT NULL
                      AND usdt > 0 AND precio > 0
                    ORDER BY ts ASC
                """)
                ordenes = cur.fetchall()
    except Exception as e:
        print(f"[pnl_ciclos] {e}")
        return jsonify({"error": str(e)[:200]}), 500

    avg_cost, usdt_con_costo = None, 0.0
    ciclos = []
    sin_costo_n, sin_costo_usdt = 0, 0.0
    for o in ordenes:
        usdt, precio = float(o["usdt"]), float(o["precio"])
        comision = float(o["comision"] or 0)
        if o["lado"] == "compra":
            if avg_cost is None:
                avg_cost, usdt_con_costo = precio, usdt
            else:
                nuevo_total = usdt_con_costo + usdt
                avg_cost = (avg_cost * usdt_con_costo + precio * usdt) / nuevo_total
                usdt_con_costo = nuevo_total
            continue
        # venta
        if avg_cost is None or usdt_con_costo <= 0:
            sin_costo_n += 1
            sin_costo_usdt += usdt
            continue
        monto_con_costo = min(usdt, usdt_con_costo)
        monto_sin_costo = usdt - monto_con_costo
        if monto_sin_costo > 0.01:
            sin_costo_n += 1
            sin_costo_usdt += monto_sin_costo
        comision_clp = comision * precio
        pnl_bruto = (precio - avg_cost) * monto_con_costo
        pnl_neto = pnl_bruto - comision_clp
        usdt_con_costo -= monto_con_costo
        if usdt_con_costo <= 0.01:
            usdt_con_costo = 0.0
        ciclos.append({
            "orden_id": o["orden_id"], "ts": str(o["ts"])[:19], "rol": o["rol"],
            "usdt": round(monto_con_costo, 2), "precio_venta": precio,
            "costo_base_clp": round(avg_cost, 2),
            "pnl_bruto_clp": round(pnl_bruto), "comision_clp": round(comision_clp),
            "pnl_neto_clp": round(pnl_neto), "pct": round((precio / avg_cost - 1) * 100, 3),
        })

    listado = ciclos
    if dias:
        try:
            desde = (datetime.now(SANTIAGO_TZ) - timedelta(days=dias)).replace(tzinfo=None)
            listado = [c for c in ciclos if datetime.strptime(c["ts"], "%Y-%m-%d %H:%M:%S") >= desde]
        except Exception:
            pass

    def _resumen(lista):
        if not lista:
            return {"n_ciclos": 0, "pnl_neto_total_clp": 0, "pnl_medio_clp": None,
                    "ganadores": 0, "tasa_acierto_pct": None}
        total = sum(c["pnl_neto_clp"] for c in lista)
        gan = sum(1 for c in lista if c["pnl_neto_clp"] > 0)
        return {"n_ciclos": len(lista), "pnl_neto_total_clp": round(total),
                "pnl_medio_clp": round(total / len(lista)), "ganadores": gan,
                "tasa_acierto_pct": round(gan / len(lista) * 100, 1)}

    por_rol = {rol: _resumen([c for c in listado if c["rol"] == rol]) for rol in ("maker", "taker")}

    if fmt == "csv":
        import csv, io
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["orden_id", "fecha_hora", "rol", "usdt", "precio_venta", "costo_base_clp",
                    "pnl_bruto_clp", "comision_clp", "pnl_neto_clp", "pct"])
        for c in listado:
            w.writerow([c["orden_id"], c["ts"], c["rol"], c["usdt"], c["precio_venta"],
                       c["costo_base_clp"], c["pnl_bruto_clp"], c["comision_clp"],
                       c["pnl_neto_clp"], c["pct"]])
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=pnl_ciclos{'_' + str(dias) + 'd' if dias else ''}.csv"})

    return jsonify({
        "configurado": True,
        "resumen": _resumen(listado),
        "por_rol": por_rol,
        "sin_costo_base": {"n": sin_costo_n, "usdt": round(sin_costo_usdt, 2)},
        "ciclos": list(reversed(listado))[:300],
        "dias": dias,
        "nota": ("Costo promedio ponderado sobre TODO el historial (aunque dias= filtre que se lista). "
                 "'sin_costo_base' son ventas de USDT que no tienen una compra P2P previa que las explique "
                 "(probable fondeo externo) -- no entran al P&L, no se les inventa un costo. "
                 "pnl_neto_clp ya descuenta la comision de esa orden puntual."),
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


LIMITE_MB = 500.0   # volumen del plan Railway (ajustar si cambia)

def _uso_disco_mb():
    """MB realmente usados en el volumen de Railway.

    FIX (COL26): antes esto era solo pg_database_size(current_database()),
    que mide las TABLAS pero ignora el WAL (los archivos de transacciones
    pendientes de aplicar/reciclar) y cualquier otra base del mismo servidor.
    Medido 28-jul, justo despues de un vaciado grande: tablas=66 MB pero
    WAL=96 MB (¡mas grande que las tablas!) -> el numero viejo decia 13% de
    uso cuando el real (con WAL) era 34%. Un TRUNCATE/DELETE grande genera
    mucho WAL que tarda en reciclarse, asi que el hueco se nota mas justo
    despues de purgar, que es la peor coincidencia posible.
    Con este fix el % que muestra el monitor debería acercarse mucho mas al
    que reporta Railway. Puede seguir sin coincidir al 100% (hay overhead de
    filesystem/contenedor que Postgres no expone), por eso el numero de
    Railway sigue siendo la referencia final si hay dudas."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_database_size(current_database())")
            tablas_b = float(cur.fetchone()[0])
            try:
                cur.execute("SELECT COALESCE(SUM(size), 0) FROM pg_ls_waldir()")
                wal_b = float(cur.fetchone()[0])
            except Exception:
                wal_b = 0.0   # sin permiso o funcion no disponible: mejor subestimar que romper
    return (tablas_b + wal_b) / 1048576.0, tablas_b / 1048576.0, wal_b / 1048576.0


@app.route("/api/storage")
def api_storage():
    usado, tablas_mb, wal_mb = _uso_disco_mb()
    with get_conn() as conn:
        with conn.cursor() as cur:
            tablas = {}
            for t in ("snapshots", "snapshots_detalle", "snapshots_detalle_bybit"):
                try:
                    cur.execute("SELECT pg_total_relation_size(%s)", (t,))
                    tablas[t] = round(cur.fetchone()[0] / 1048576.0, 1)
                except Exception:
                    tablas[t] = None
    return jsonify({
        "usado_mb":  round(usado, 1),
        "tablas_total_mb": round(tablas_mb, 1),
        "wal_mb":    round(wal_mb, 1),
        "limite_mb": LIMITE_MB,
        "libre_mb":  round(LIMITE_MB - usado, 1),
        "pct":       round(usado / LIMITE_MB * 100, 1),
        "tablas_mb": tablas,
        "nota": ("usado_mb incluye tablas + WAL (antes solo tablas, por eso el % podia verse "
                 "muy por debajo de lo que muestra Railway). Puede no coincidir exacto: Railway "
                 "cuenta ademas overhead de filesystem que Postgres no expone."),
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


@app.route("/api/volumen_mercado")
def api_volumen_mercado():
    """Serie diaria PERSISTIDA de volumen total de mercado (COL39).

    /api/volumen_v2 ya calcula esto pero solo mira las ultimas 48h en vivo
    desde fills_estimados, que se purga a los 30 dias -- no hay forma de ver
    la tendencia. Esta serie viene de volumen_mercado_dia, que guardar_
    volumen_mercado_dia() congela 1x/dia con el MISMO calculo, asi que el
    numero de "hoy" siempre coincide con lo que ya se ve en vivo.

    Suma Binance + Bybit por dia (Bybit es marginal, no aporta separarlo
    aca). Los porcentajes se promedian PONDERADOS por volumen, no a secas.
    Params: ?dias=30&fmt=json|csv"""
    try:
        dias = max(1, min(365, int(request.args.get("dias", 30))))
    except (ValueError, TypeError):
        dias = 30
    fmt = (request.args.get("fmt", "json") or "json").lower()

    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT fecha,
                           SUM(volumen_usdt) volumen_usdt,
                           SUM(ordenes) ordenes,
                           SUM(anunciantes_activos) anunciantes_activos,
                           ROUND(SUM(volumen_usdt * COALESCE(pct_enmascarado, 0))
                                 / NULLIF(SUM(volumen_usdt), 0), 1) pct_enmascarado,
                           ROUND(SUM(volumen_usdt * COALESCE(presion_compra_pct, 50))
                                 / NULLIF(SUM(volumen_usdt), 0), 1) presion_compra_pct
                    FROM volumen_mercado_dia
                    WHERE fecha >= CURRENT_DATE - %s
                    GROUP BY fecha ORDER BY fecha
                """, [dias])
                filas = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f"[volumen_mercado] {e}")
        return jsonify({"error": str(e)[:200]}), 500

    salida = []
    for f in filas:
        salida.append({
            "fecha": str(f["fecha"]),
            "volumen_usdt": float(f["volumen_usdt"]) if f["volumen_usdt"] is not None else None,
            "ordenes": int(f["ordenes"]) if f["ordenes"] is not None else None,
            "anunciantes_activos": int(f["anunciantes_activos"]) if f["anunciantes_activos"] is not None else None,
            "pct_enmascarado": float(f["pct_enmascarado"]) if f["pct_enmascarado"] is not None else None,
            "presion_compra_pct": float(f["presion_compra_pct"]) if f["presion_compra_pct"] is not None else None,
        })

    if fmt == "csv":
        import csv, io
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["fecha", "volumen_usdt", "ordenes", "anunciantes_activos",
                    "pct_enmascarado", "presion_compra_pct"])
        for s in salida:
            w.writerow([s["fecha"], s["volumen_usdt"], s["ordenes"], s["anunciantes_activos"],
                       s["pct_enmascarado"], s["presion_compra_pct"]])
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=volumen_mercado_{dias}d.csv"})
    return jsonify({"serie": salida})


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
    Params: ?capital=USDT (default: patrimonio REAL del inventario en vivo;
    si no esta configurado, cae a CAPITAL_OPERATIVO)."""
    with config_lock:
        c = dict(config)
    try:
        capital = float(request.args.get("capital", 0))
    except (ValueError, TypeError):
        capital = 0.0
    if not capital:
        # COL40: el Asistente SIEMPRE llega hasta aca sin ?capital= (ningun
        # caller del frontend lo manda), asi que hasta ahora esto usaba
        # SIEMPRE el numero configurado a mano en vez de lo que Sebastian
        # tiene de verdad -- la auditoria del 31-jul lo encontro desalineado
        # (config 700 vs inventario real bien distinto).
        try:
            inv = api_inventario().get_json()
            if inv.get("configurado") and inv.get("patrimonio_clp") and inv.get("precio_ref_actual"):
                capital = float(inv["patrimonio_clp"]) / float(inv["precio_ref_actual"])
        except Exception as e:
            print(f"[operativa capital] {e}")
        if not capital:
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

    # ── señal macro (COL40): conectada, sin tocar el arbol de spread/rotacion ──
    # decidir_operativa() ya esta probado y calibrado (operativa_historial se
    # arma con su salida) — no se le mete una entrada mas. El desfase dolar
    # forex<->P2P es un eje DISTINTO (timing direccional, no "hay margen y
    # liquidez ahora"), asi que se agrega COMO CAMPO APARTE, no mezclado en
    # decision/color/razon. La unica conexion real: si el arbol ya dijo
    # ESPERAR (el caso pasivo, donde una pista de hacia donde va el precio
    # importa mas) Y hay señal macro activa, se arma una nota aparte que el
    # frontend puede mostrar junto a la razon — nunca la reemplaza.
    try:
        macro_desfase = calcular_desfase()
    except Exception as e:
        print(f"[operativa macro] {e}")
        macro_desfase = None
    nota_macro = None
    if macro_desfase and macro_desfase.get("senal") and decision == "ESPERAR":
        nota_macro = macro_desfase["mensaje"]

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
        "macro": macro_desfase, "nota_macro": nota_macro,
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


# ──────────────────────────────────────────────
#  INVENTARIO EN VIVO (COL22)
# ──────────────────────────────────────────────
def _precio_mid(snap=None):
    """Precio ponderado de referencia = medio entre los dos ponderados."""
    if snap is None:
        with data_lock:
            snap = dict(ultimo_estado)
    pc = float(snap.get("precio_pond_tab_compra") or 0)
    pv = float(snap.get("precio_pond_tab_venta") or 0)
    if pc and pv:
        return (pc + pv) / 2
    return pc or pv or 0


def _movimientos_maker(desde_ts, hasta_ts=None):
    """Movimientos MAKER derivados en vivo de los fills detectados de MI_NICKNAME.

    OJO con la semantica del libro: mi anuncio de VENTA vive en el tab Compra,
    asi que un fill con tipo='BUY' significa que YO VENDI USDT (y al reves).
    La comision maker se cobra en USDT (medido en ordenes reales: 0,14 USDT
    sobre 74,16), por eso se descuenta del saldo en USDT en ambos lados.

    INCLUYE 'enmascarado' desde COL47 — antes se excluian, y esa exclusion
    quedo obsoleta sin que nadie lo notara:
      - El motivo escrito era "los enmascarado se estiman con el ticket
        MEDIANO DEL MERCADO (~408 USDT) y meterian movimientos fantasma de
        400 que nunca pasaron". Cierto cuando se escribio.
      - Pero COL24 agrego MI_TICKET_MEDIO justamente para eso: los fills
        enmascarados de MI cuenta se estiman con MI ticket, no con el del
        mercado (ver FillTracker, 'es_mi_cuenta and mi_ticket > 0').
        Verificado con datos del 4-ago: 305,60 USDT en 8 ordenes = 38 por
        orden, o sea el ticket propio, no 408.
      - Consecuencia de seguir excluyendolos: se tiraba ~31 por ciento de las
        ventas reales del dia. Medido el 4-ago, con solo 'directo' el saldo
        CLP daba -63.001 (imposible) y 109 por ciento en USDT; incluyendo los
        enmascarados da +218.668 CLP y 68,8 por ciento. El PATRIMONIO total
        casi no cambia (701.038 vs 701.636): las ventas que faltaban solo
        movian valor de una columna a la otra, que es la firma de que
        faltaban movimientos y no de que sobrara plata.
      - Ademas disparaba la alerta de "saldo negativo, perdiste movimientos"
        contra un inventario que en realidad estaba bien: la alerta acusaba
        al usuario de no cargar ordenes cuando el que no sumaba era el codigo.

    Sigue siendo una ESTIMACION: se devuelve 'metodo' en cada movimiento para
    que quien consuma pueda informar que parte es observada y que parte
    estimada. Lo que el tracker no ve se corrige al re-anclar (el drift)."""
    with config_lock:
        nick = str(config.get("MI_NICKNAME") or "").strip()
        com_pct = float(config.get("COM_MAKER_PCT", 0.20))
    if not nick:
        return []
    f = lambda dt: dt.strftime("%Y-%m-%d %H:%M:%S")
    sql = """SELECT ts, tipo, monto, precio, metodo FROM fills_estimados
             WHERE exchange='binance'
               AND LOWER(anunciante)=LOWER(%(n)s)
               AND ts >= %(d)s"""
    params = {"n": nick, "d": f(desde_ts)}
    if hasta_ts is not None:
        sql += " AND ts < %(h)s"
        params["h"] = f(hasta_ts)
    sql += " ORDER BY ts"
    filas = []
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql, params)
                for r in cur.fetchall():
                    monto = float(r["monto"] or 0)
                    precio = float(r["precio"] or 0)
                    if monto <= 0 or precio <= 0:
                        continue
                    fee = monto * com_pct / 100.0
                    vendi = (r["tipo"] == "BUY")   # tab Compra = mi anuncio de VENTA
                    filas.append({
                        "ts": r["ts"], "tipo": "maker",
                        "lado": "venta" if vendi else "compra",
                        # el fee siempre sale del USDT
                        "d_usdt": -(monto + fee) if vendi else (monto - fee),
                        "d_clp": (monto * precio) if vendi else -(monto * precio),
                        "precio": precio, "usdt": monto,
                        "fee_usdt": fee,
                        # COL47: 'directo' = caida de stock observada;
                        # 'enmascarado' = estimado con MI ticket. Viaja para
                        # poder decir que parte del saldo es observada.
                        "metodo": r["metodo"],
                    })
    except Exception as e:
        print(f"[inv maker] {e}")
    return filas


def _movimientos_manuales(desde_ts, hasta_ts=None):
    """Movimientos cargados a mano: taker y externo (y maker manual si lo hubiera)."""
    with config_lock:
        com_taker = float(config.get("COM_TAKER_FIJA_USDT", 0.07))
        com_pct = float(config.get("COM_MAKER_PCT", 0.20))
    f = lambda dt: dt.strftime("%Y-%m-%d %H:%M:%S")
    sql = "SELECT ts, tipo, lado, usdt, clp, precio, nota FROM movimientos_inventario WHERE ts >= %(d)s"
    params = {"d": f(desde_ts)}
    if hasta_ts is not None:
        sql += " AND ts < %(h)s"
        params["h"] = f(hasta_ts)
    sql += " ORDER BY ts"
    filas = []
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql, params)
                for r in cur.fetchall():
                    tipo = (r["tipo"] or "").lower()
                    usdt = float(r["usdt"] or 0)
                    clp = float(r["clp"] or 0)
                    precio = float(r["precio"] or 0)
                    lado = (r["lado"] or "").lower() or None
                    if tipo == "externo":
                        # deposito/retiro: se toma tal cual, sin comision ni P&L
                        filas.append({"ts": r["ts"], "tipo": "externo", "lado": None,
                                      "d_usdt": usdt, "d_clp": clp, "precio": precio,
                                      "usdt": abs(usdt), "fee_usdt": 0.0, "nota": r["nota"]})
                        continue
                    # trade (taker o maker manual): el signo lo da el lado
                    fee = com_taker if tipo == "taker" else usdt * com_pct / 100.0
                    monto = abs(usdt)
                    if not precio and monto:
                        precio = abs(clp) / monto if clp else 0
                    vendi = (lado == "venta")
                    filas.append({
                        "ts": r["ts"], "tipo": tipo, "lado": lado,
                        "d_usdt": -(monto + fee) if vendi else (monto - fee),
                        "d_clp": (monto * precio) if vendi else -(monto * precio),
                        "precio": precio, "usdt": monto, "fee_usdt": fee,
                        "nota": r["nota"],
                    })
    except Exception as e:
        print(f"[inv manual] {e}")
    return filas


def _aplicar(ancla_usdt, ancla_clp, movs):
    """Aplica una lista de movimientos sobre un saldo inicial."""
    usdt, clp, externo_usdt, externo_clp = ancla_usdt, ancla_clp, 0.0, 0.0
    for m in movs:
        usdt += m["d_usdt"]
        clp += m["d_clp"]
        if m["tipo"] == "externo":
            externo_usdt += m["d_usdt"]
            externo_clp += m["d_clp"]
    return usdt, clp, externo_usdt, externo_clp


def _precio_a_las(dt):
    """Precio ponderado medio mas cercano (hacia atras) a un momento dado."""
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT precio_pond_tab_compra pc, precio_pond_tab_venta pv
                    FROM snapshots WHERE timestamp <= %s AND precio_pond_tab_compra IS NOT NULL
                    ORDER BY timestamp DESC LIMIT 1
                """, (dt.strftime("%Y-%m-%d %H:%M:%S"),))
                r = cur.fetchone()
                if r:
                    pc, pv = float(r["pc"] or 0), float(r["pv"] or 0)
                    if pc and pv:
                        return (pc + pv) / 2
                    return pc or pv or 0
    except Exception as e:
        print(f"[inv precio_a_las] {e}")
    return 0


def _ratio_rotacion():
    """Que tan rapido rota el mercado AHORA contra su promedio de 12h.

    UNA consulta. Existe desde COL55 porque api_ciclo sacaba este mismo
    numero llamando a api_operativa() completo, y ese a su vez llamaba a
    api_inventario() — o sea ~6 consultas y un inventario recalculado para
    devolver un solo float. Devuelve None si no hay datos (no asume "normal":
    un ratio inventado haria que el semaforo del Ciclo mienta)."""
    now = datetime.now(SANTIAGO_TZ)
    f = lambda dt: dt.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT COALESCE(SUM(monto) FILTER (WHERE ts >= %(m30)s), 0) v30,
                           COALESCE(SUM(monto), 0) v12
                    FROM fills_estimados
                    WHERE exchange = 'binance' AND ts >= %(h12)s
                """, {"m30": f(now - timedelta(minutes=30)),
                      "h12": f(now - timedelta(hours=12))})
                r = cur.fetchone()
        v30, v12 = float(r["v30"] or 0), float(r["v12"] or 0)
        prom = v12 / (12 * 60)
        return round((v30 / 30) / prom, 2) if prom else None
    except Exception as e:
        print(f"[ratio rotacion] {e}")
        return None


# ── Cache corto del inventario (COL55) ────────────────────────────────
# El inventario es el calculo mas caro que hay (ancla + movimientos maker +
# manuales + precios historicos). Se pedia MUCHAS veces de mas:
#   · api_ciclo lo llamaba para el saldo, y ademas llamaba a api_operativa
#     que lo volvia a llamar -> DOS veces por pedido del Ciclo
#   · con el monitor abierto en el celular Y la compu, cada pantalla lo pide
#     por su cuenta
# 8 segundos alcanzan para matar toda esa duplicacion sin que se note: el
# inventario cambia cuando cargas un movimiento o se detecta un fill, no
# varias veces por segundo. Los POST no leen el cache y ademas lo invalidan.
_inv_cache = {"ts": 0.0, "data": None}
_inv_lock = threading.Lock()
INV_CACHE_SEG = 8.0


def _invalidar_inventario():
    """Se llama despues de cualquier POST que cambie el inventario, para que
    el proximo GET no devuelva el estado viejo."""
    with _inv_lock:
        _inv_cache["ts"] = 0.0
        _inv_cache["data"] = None


@app.route("/api/inventario")
def api_inventario():
    """INVENTARIO EN VIVO: saldos estimados, patrimonio, %USDT, banda y P&L.

    El numero en vivo es ESTIMADO: el monitor no ve el banco ni las ordenes
    taker que no cargues. Es exacto en el momento del ancla y va driftando
    despues; al re-anclar se mide ese drift.

    COL55: cachea el resultado INV_CACHE_SEG segundos (ver _inv_cache)."""
    ahora_ts = time.time()
    with _inv_lock:
        if _inv_cache["data"] is not None and (ahora_ts - _inv_cache["ts"]) < INV_CACHE_SEG:
            return jsonify(_inv_cache["data"])
    with config_lock:
        c = dict(config)
    now = datetime.now(SANTIAGO_TZ)
    # ── ancla ──
    ancla = None
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT ts, usdt, clp, precio_ref FROM inventario_ancla ORDER BY ts DESC LIMIT 1")
                r = cur.fetchone()
                if r:
                    ancla = {"ts": r["ts"], "usdt": float(r["usdt"] or 0),
                             "clp": float(r["clp"] or 0),
                             "precio_ref": float(r["precio_ref"] or 0)}
    except Exception as e:
        print(f"[inventario ancla] {e}")
    if not ancla:
        return jsonify({"configurado": False,
                        "nota": "Todavia no fijaste tus saldos reales. Usa 'actualizar saldos' "
                                "para anclar el inventario (USDT en Binance + CLP en Mercado Pago)."})

    ancla_dt = ancla["ts"]
    if ancla_dt.tzinfo is None:
        ancla_dt = ancla_dt.replace(tzinfo=SANTIAGO_TZ)
    movs = sorted(_movimientos_maker(ancla_dt) + _movimientos_manuales(ancla_dt),
                  key=lambda m: m["ts"])
    usdt, clp, ext_usdt, ext_clp = _aplicar(ancla["usdt"], ancla["clp"], movs)

    precio = _precio_mid()
    patrimonio = usdt * precio + clp
    pct_usdt = (usdt * precio / patrimonio * 100) if patrimonio > 0 else 0

    # ── P&L del dia: patrimonio ahora vs apertura, SIN los movimientos externos ──
    inicio_dia = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if ancla_dt >= inicio_dia:
        # el ancla es de hoy: la apertura es el ancla mismo
        u0, cl0 = ancla["usdt"], ancla["clp"]
        precio0 = ancla["precio_ref"] or _precio_a_las(ancla_dt) or precio
        ext_dia_usdt, ext_dia_clp = ext_usdt, ext_clp
    else:
        movs_hasta = [m for m in movs if (m["ts"].replace(tzinfo=SANTIAGO_TZ)
                                          if m["ts"].tzinfo is None else m["ts"]) < inicio_dia]
        u0, cl0, _, _ = _aplicar(ancla["usdt"], ancla["clp"], movs_hasta)
        precio0 = _precio_a_las(inicio_dia) or precio
        ext_dia_usdt = sum(m["d_usdt"] for m in movs
                           if m["tipo"] == "externo" and
                           (m["ts"].replace(tzinfo=SANTIAGO_TZ) if m["ts"].tzinfo is None else m["ts"]) >= inicio_dia)
        ext_dia_clp = sum(m["d_clp"] for m in movs
                          if m["tipo"] == "externo" and
                          (m["ts"].replace(tzinfo=SANTIAGO_TZ) if m["ts"].tzinfo is None else m["ts"]) >= inicio_dia)
    patrimonio_apertura = u0 * precio0 + cl0
    aporte_externo_dia = ext_dia_usdt * precio + ext_dia_clp
    pnl_dia = patrimonio - aporte_externo_dia - patrimonio_apertura

    # ── Costo de campaña (OCULTO en la UI) ──
    # P&L de trading REALIZADO: se saca la revaluacion del USDT que ya tenias
    # al anclar, para no confundir "el dolar subio" con "operar me dio plata".
    ext_total = ext_usdt * precio + ext_clp
    pnl_total = patrimonio - ext_total - (ancla["usdt"] * (ancla["precio_ref"] or precio) + ancla["clp"])
    revaluacion = ancla["usdt"] * (precio - (ancla["precio_ref"] or precio))
    costo_campania = pnl_total - revaluacion
    fees_usdt = sum(m.get("fee_usdt", 0) for m in movs if m["tipo"] != "externo")

    # ── Banda y reequilibrio ──
    bmin = float(c.get("INV_BANDA_MIN", 40)); bmax = float(c.get("INV_BANDA_MAX", 60))
    dmin = float(c.get("INV_DURO_MIN", 30));  dmax = float(c.get("INV_DURO_MAX", 70))
    with data_lock:
        snap = dict(ultimo_estado)
    # lado CORTO = lo que me falta. Bajo 50% estoy corto de USDT -> comprar.
    corto = "usdt" if pct_usdt < 50 else "clp"
    accion = "comprar" if corto == "usdt" else "vender"
    if bmin <= pct_usdt <= bmax:
        zona, zona_txt = "comoda", "Zona cómoda: farmeá dual como maker."
    elif dmin <= pct_usdt <= dmax:
        zona, zona_txt = "correccion", "Fuera de banda: repreciá agresivo el lado corto (todavía maker)."
    else:
        zona, zona_txt = "dura", "Límite duro: cruzá como TAKER para volver a la banda."

    # cuanto mover para volver al 50%
    usdt_objetivo = (patrimonio * 0.5 / precio) if precio > 0 else 0
    delta_usdt = usdt_objetivo - usdt

    # Precio sugerido: NO se inventa, sale del mismo libro que usa el Asistente.
    #   agresivo = un centavo mejor que el lider (para ser #1 del lado corto)
    #   cruce    = tomar el precio del otro (taker, instantaneo)
    agresivo = float(snap.get("precio_maker_comprar") or 0) if accion == "comprar" \
        else float(snap.get("precio_maker_vender") or 0)
    cruce = float(snap.get("mejor_vendedor_tab_compra") or 0) if accion == "comprar" \
        else float(snap.get("mejor_comprador_tab_venta") or 0)
    # la agresividad ESCALA con la distancia a 50%: en el borde de la banda
    # arranca cerca del ponderado y llega al precio agresivo en el limite duro.
    dist = abs(pct_usdt - 50)
    borde = abs((bmin if pct_usdt < 50 else bmax) - 50)
    tope = abs((dmin if pct_usdt < 50 else dmax) - 50)
    t = 0.0 if tope <= borde else max(0.0, min(1.0, (dist - borde) / (tope - borde)))
    precio_sugerido = None
    if zona == "correccion" and agresivo and precio:
        precio_sugerido = round(precio + (agresivo - precio) * t, 2)
    elif zona == "dura":
        precio_sugerido = round(cruce, 2) if cruce else None

    # Si la estimacion se fue a un imposible (saldo negativo), es señal de que
    # se perdieron movimientos: ordenes taker sin cargar, o fills que el tracker
    # no vio. No se disimula: se avisa que hay que re-anclar.
    alerta = None
    if usdt < 0 or clp < 0:
        alerta = ("La estimación dio un saldo negativo, o sea que se perdieron movimientos "
                  "(órdenes taker sin cargar, o fills que el monitor no detectó). "
                  "Actualizá tus saldos reales para volver a anclar.")
    elif (now - ancla_dt).total_seconds() > 3 * 24 * 3600:
        alerta = ("Hace más de 3 días que no anclás: la estimación se desvía con el tiempo. "
                  "Conviene actualizar los saldos reales.")

    salida = {
        "configurado": True,
        "estimado": True,
        "alerta": alerta,
        "ancla": {"ts": str(ancla["ts"]), "usdt": round(ancla["usdt"], 2),
                  "clp": round(ancla["clp"]), "precio_ref": round(ancla["precio_ref"], 2)},
        "saldos": {"usdt": round(usdt, 2), "clp": round(clp)},
        "precio_ref_actual": round(precio, 2),
        "patrimonio_clp": round(patrimonio),
        "pct_usdt": round(pct_usdt, 1),
        "pnl_dia_clp": round(pnl_dia),
        "patrimonio_apertura_clp": round(patrimonio_apertura),
        "banda": {"min": bmin, "max": bmax, "duro_min": dmin, "duro_max": dmax},
        "zona": zona, "zona_txt": zona_txt,
        "reequilibrio": {
            "lado_corto": corto, "accion": accion,
            "usdt_a_mover": round(abs(delta_usdt), 1),
            "precio_sugerido": precio_sugerido,
            "modo": "cruzar" if zona == "dura" else ("repreciar" if zona == "correccion" else None),
            "agresivo": round(agresivo, 2) if agresivo else None,
            "cruce": round(cruce, 2) if cruce else None,
        },
        "detalle": {
            "movimientos": len(movs),
            "maker": sum(1 for m in movs if m["tipo"] == "maker"),
            "taker": sum(1 for m in movs if m["tipo"] == "taker"),
            "externo": sum(1 for m in movs if m["tipo"] == "externo"),
            # COL47: cuanto del movimiento maker es OBSERVADO y cuanto
            # ESTIMADO. Antes los estimados no entraban al inventario y eso
            # rompia el saldo; ahora entran, pero hay que poder ver de que
            # esta hecho el numero.
            "maker_observado_usdt": round(sum(m.get("usdt", 0) for m in movs
                                              if m["tipo"] == "maker" and m.get("metodo") == "directo"), 2),
            "maker_estimado_usdt": round(sum(m.get("usdt", 0) for m in movs
                                             if m["tipo"] == "maker" and m.get("metodo") == "enmascarado"), 2),
            "costo_campania_clp": round(costo_campania),
            "comisiones_usdt": round(fees_usdt, 3),
            "revaluacion_clp": round(revaluacion),
            "aporte_externo_clp": round(ext_total),
        },
        "nota": ("Estimado: el monitor no ve el banco ni las órdenes taker que no cargues. "
                 "Exacto al anclar, aproximado después. Volvé a anclar para medir el drift."),
    }
    with _inv_lock:
        _inv_cache["ts"], _inv_cache["data"] = time.time(), salida
    return jsonify(salida)


@app.route("/api/ciclo")
def api_ciclo():
    """CICLO DE RECOMPRA (COL27): dado un monto a ciclar, a que precio recomprar
    como taker y a que precio publicar la venta.

    Por que el MONTO es el input clave: la comision taker es un monto FIJO
    (0,07 USDT), asi que su peso en % depende enteramente de cuanto cruces
    (100 USDT -> 0,07% · 1.200 USDT -> 0,0058%). La maker es porcentual y no
    depende del monto. Sin fijar el monto, el costo del ciclo no esta definido.

    El VWAP sale de barrer el libro EN VIVO, no del "mejor precio" a secas:
    si el tope tiene poco volumen, el precio real de barrer es bastante peor.
    Params: ?monto=1200&margen=0.30"""
    with config_lock:
        c = dict(config)
    monto_pedido = request.args.get("monto")
    try:
        monto = float(monto_pedido or c.get("CICLO_MONTO_DEFAULT", 1200))
    except (TypeError, ValueError):
        monto = float(c.get("CICLO_MONTO_DEFAULT", 1200))
    try:
        margen = float(request.args.get("margen") or c.get("CICLO_MARGEN_OBJETIVO", 0.30))
    except (TypeError, ValueError):
        margen = float(c.get("CICLO_MARGEN_OBJETIVO", 0.30))

    # ── ACOTAR A LA PLATA CON LA QUE SE RECOMPRA (COL56) ─────────────
    # ★ FIX de un error conceptual de COL44. Este modulo calcula una
    # RECOMPRA: se compra USDT pagando CLP. La plata que limita cuanto podes
    # recomprar es entonces la de PESOS, no la de dolares — los dolares son
    # justamente lo que se esta vendiendo.
    # COL44 acotaba contra saldos["usdt"], que es al reves. Sebastian lo vio
    # en pantalla el 6-ago: el Ciclo le ofrecia 67 USDT (todo su saldo en
    # dolares, que estaba bajo porque venia de vender) cuando con sus 640.741
    # CLP podia recomprar ~699. Diez veces menos de lo real.
    # El tope ahora es cuantos USDT ALCANZAN los pesos disponibles.
    saldo_clp = None
    saldo_usdt = None
    patrimonio_clp = None
    saldo_real = None          # tope expresado en USDT, para la UI
    # el inventario se pide SIEMPRE (no solo sin monto_pedido) porque los dos
    # numeros de capacidad se muestran igual aunque estes simulando un monto.
    # Sale del cache de 8s (COL55), asi que no cuesta.
    try:
        inv = api_inventario().get_json()
        if inv.get("configurado") and inv.get("saldos"):
            saldo_clp = float(inv["saldos"].get("clp") or 0)
            saldo_usdt = float(inv["saldos"].get("usdt") or 0)
            patrimonio_clp = float(inv.get("patrimonio_clp") or 0)
    except Exception as e:
        print(f"[ciclo saldo] {e}")
    # el tope se aplica MAS ABAJO, recien cuando se conoce el precio del libro
    # (ver "acotar el monto"): con el precio de referencia del inventario daba
    # un numero distinto al de la capacidad y quedaban dos respuestas para la
    # misma pregunta en la misma tarjeta.

    margen = max(0.0, min(10.0, margen))

    # ── de donde sale el libro (COL38) ──────────────────────────────
    # Se PREFIERE el libro vivo (10s). Si el hilo no arranco todavia o la
    # fuente esta caida, cae al de 2 min del colector — que es lo que habia
    # antes, asi que en el peor caso funciona igual que siempre.
    # 'edad_seg' viaja a la UI: operar contra un precio viejo sin saberlo es
    # justo lo que esto viene a evitar.
    fuente, edad_seg = "vivo", None
    compra, edad_seg = libro_vivo_como_detalle("BUY")
    venta, _ = libro_vivo_como_detalle("SELL")
    if not compra:
        fuente = "colector"
        with data_lock:
            snap = dict(ultimo_estado)
        compra = snap.get("detalle_compra") or []
        venta = snap.get("detalle_venta") or []
        try:
            ts_snap = snap.get("timestamp")
            if ts_snap:
                edad_seg = (datetime.now(SANTIAGO_TZ)
                            - datetime.strptime(str(ts_snap)[:19], "%Y-%m-%d %H:%M:%S")
                            .replace(tzinfo=SANTIAGO_TZ)).total_seconds()
        except Exception:
            pass
    if not compra:
        return jsonify({"error": "sin datos del libro aun"}), 503

    mi_nick = str(c.get("MI_NICKNAME") or "").strip().lower()
    min_usdt = float(c.get("FILTRO_MIN_USDT", 200))
    min_tasa = float(c.get("FILTRO_MIN_TASA", 90))
    # ── filtro de METODOS DE PAGO (COL38) ───────────────────────────
    # Un anuncio con el que no compartis banco NO es un precio disponible
    # para vos. Medido 2-ago: 15 de 70 anuncios que pasaban los filtros
    # normales no compartian metodo. Si la lista esta vacia no filtra nada
    # (y si el libro viene del colector, que no guarda 'pagos', tampoco).
    mis_pagos = {x.strip() for x in str(c.get("MIS_METODOS_PAGO", "") or "").split(",") if x.strip()}
    sin_pago = []

    def acepta_pago(a):
        if not mis_pagos:
            return True
        p = a.get("pagos")
        if not p:                      # sin dato de pagos: no se excluye
            return True
        return bool(set(p) & mis_pagos)

    # anuncios REALES y sin los mios: no puedo recomprarme a mi mismo
    reales = []
    for a in compra:
        if not (float(a.get("disponible") or 0) >= min_usdt
                and float(a.get("tasa_exito") or 0) >= min_tasa
                and float(a.get("precio") or 0) > 0
                and (a.get("anunciante") or "").strip().lower() != mi_nick):
            continue
        if not acepta_pago(a):
            sin_pago.append({"anunciante": a.get("anunciante"),
                             "precio": round(float(a["precio"]), 2)})
            continue
        reales.append(a)
    reales.sort(key=lambda a: float(a["precio"]))
    if not reales:
        return jsonify({"error": "no hay anuncios reales en el libro"}), 503

    # ── acotar el monto a los PESOS disponibles (COL56, reubicado en COL57) ──
    # Se hace aca y no arriba porque recien ahora existe el precio del libro.
    # Antes se usaba el precio de referencia del inventario, que esta 0,3% por
    # debajo del libro: el tope daba 219 USDT y la capacidad (calculada con el
    # VWAP del barrido) daba 218. Dos numeros para lo mismo en la misma
    # tarjeta. Ahora ambos salen del libro y coinciden.
    if saldo_clp and saldo_clp > 0:
        saldo_real = saldo_clp / float(reales[0]["precio"])
        if not monto_pedido and saldo_real >= 10:
            monto = min(monto, saldo_real)
    monto = max(10.0, min(100000.0, monto))

    # ── 1. barrer el libro RESPETANDO LOS LIMITES DE CADA ANUNCIO (COL32) ──
    # Antes se barria solo por 'disponible', asi que el VWAP podia salir de
    # anuncios que NUNCA me habrian aceptado la orden -> un precio de recompra
    # inalcanzable. Los limites vienen en CLP, y lo que cruzo son USDT, asi que
    # se convierten con el precio de ESE anuncio (cada uno tiene el suyo).
    #
    # El max NO descalifica al anuncio: acota cuanto le puedo sacar en una orden.
    # El min SI descalifica, pero solo para el resto que me falta en ese momento:
    # si me quedan 50 USDT y su minimo son 213, esa orden no se puede colocar.
    #
    # min_orden/max_orden en None = todavia sin dato (dias previos al deploy, o
    # Bybit si no lo manda): NO se excluye el anuncio. Falta de dato no es
    # prueba de que no acepte -- excluir por eso seria peor que el bug original.
    restante, costo_clp, niveles = monto, 0.0, 0
    usados, saltados = [], []
    for a in reales:
        if restante <= 0.01:
            break
        precio = float(a["precio"])
        disp = float(a["disponible"])
        mino, maxo = a.get("min_orden"), a.get("max_orden")
        # techo de esta orden: su stock y, si lo declara, su maximo por orden
        tope = min(restante, disp)
        if maxo:
            tope = min(tope, float(maxo) / precio)
        # piso: si lo que puedo tomar no llega a su minimo, no hay orden posible
        if mino and tope < float(mino) / precio - 0.01:
            saltados.append({"anunciante": a.get("anunciante"), "precio": round(precio, 2),
                             "min_orden": mino,
                             "min_usdt": round(float(mino) / precio, 1)})
            continue
        if tope <= 0:
            continue
        costo_clp += tope * precio
        restante -= tope
        niveles += 1
        usados.append(a)
    # Profundidad REALMENTE accesible: suma de lo que cada anuncio me deja tomar,
    # no su stock crudo (un anuncio con 40.000 USDT pero maximo de 7.000.000 CLP
    # solo me sirve por ~7.500 USDT por orden).
    def _accesible(a):
        p = float(a["precio"]); d = float(a["disponible"])
        mx = a.get("max_orden")
        return min(d, float(mx) / p) if mx else d
    profundidad = sum(_accesible(a) for a in reales)
    alcanza = restante <= 0.01
    llenado = monto - restante          # lo que SI se pudo comprar
    vwap = (costo_clp / llenado) if llenado > 0 else 0.0
    mejor = float(reales[0]["precio"])
    # el mejor precio que de verdad me acepta (puede no ser el tope del libro)
    mejor_accesible = float(usados[0]["precio"]) if usados else None

    # ── 2-5. costos y precio de venta ──
    com_taker = float(c.get("COM_TAKER_FIJA_USDT", 0.07))
    com_maker = float(c.get("COM_MAKER_PCT", 0.20))
    costo_taker_pct = com_taker / monto * 100
    costo_total_pct = costo_taker_pct + com_maker
    precio_venta = vwap * (1 + (costo_total_pct + margen) / 100) if vwap else 0.0

    # ── 6-7. donde caeria esa venta y en que banda ──
    # mi anuncio de VENTA compite en el tab Compra (mismo lado del libro que
    # acabo de barrer): ahi es donde el comprador me va a encontrar.
    posicion_est = 1 + sum(1 for a in reales if float(a["precio"]) < precio_venta)
    banda_pct = ((precio_venta - mejor) / mejor * 100) if mejor else 0.0
    banda = _banda_de(banda_pct)

    # ── 8-9. flujo de esa banda (dato historico) ──
    flujo = {"banda": banda, "capturable_dia": None, "competidores": None,
             "consumo_dia": None, "ciclos_dia_est": None}
    if banda:
        try:
            with get_conn() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("""SELECT consumo_dia, competidores, capturable_dia
                                   FROM perfil_banda WHERE banda = %s""", (banda,))
                    r = cur.fetchone()
                    if r:
                        cap = float(r["capturable_dia"] or 0)
                        flujo.update({"capturable_dia": round(cap),
                                      "competidores": round(float(r["competidores"] or 0), 1),
                                      "consumo_dia": round(float(r["consumo_dia"] or 0)),
                                      "ciclos_dia_est": round(cap / monto, 1) if monto > 0 else None})
        except Exception as e:
            print(f"[ciclo banda] {e}")

    # ── 10. ganancia ──
    gan_vuelta = monto * margen / 100
    ciclos = flujo.get("ciclos_dia_est") or 0
    gan_dia = gan_vuelta * ciclos

    # ── semaforo ──
    flujo_min = float(c.get("CICLO_FLUJO_MIN_DIA", 2000))
    cap = flujo.get("capturable_dia")
    avisos = []
    if not alcanza:
        veredicto = "ROJO"
        mensaje = (f"El libro solo tiene {profundidad:,.0f} USDT en anuncios reales: "
                   f"no alcanza para ciclar {monto:,.0f}.")
        avisos.append(f"Podés ciclar hasta ~{profundidad:,.0f} USDT con el libro actual.")
    elif cap is None:
        veredicto = "AMBAR"
        mensaje = "Todavía no hay perfil de flujo para esa banda (se calcula 1x/día)."
    elif cap >= flujo_min:
        veredicto = "VERDE"
        mensaje = f"Recomprá hasta {vwap:,.2f} · Vendé a {precio_venta:,.2f} (≈pos {posicion_est})"
    else:
        veredicto = "AMBAR"
        mensaje = (f"Alcanzable, pero esa banda mueve solo {cap:,.0f} USDT/día: "
                   f"la venta va a tardar en llenarse.")

    # mercado lento: aunque el margen exista, los ciclos no se van a cumplir
    # COL55 — antes esto llamaba a api_operativa() ENTERO para sacar UN numero.
    # Y api_operativa() a su vez llama a api_inventario(), que hace 4 consultas
    # mas — o sea que para saber "que tan rapido rota el mercado" se recalculaba
    # todo el inventario, encima por segunda vez en el mismo pedido (api_ciclo
    # ya lo habia llamado arriba para el saldo). Ahora usa el helper directo.
    ratio = _ratio_rotacion()
    if ratio is not None and ratio < 0.7 and veredicto == "VERDE":
        avisos.append(f"Mercado lento ({ratio}x vs 12h): los {ciclos} ciclos/día estimados "
                      "probablemente no se cumplan hoy, aunque el margen exista.")

    # COL32: avisar cuando el tope del libro NO te acepta el monto. Es info que
    # antes simplemente no existia y llevaba a apuntar a un precio imposible.
    if saltados:
        s = saltados[0]
        detalle = (f"{s['anunciante']} a {s['precio']:,.2f} pide mínimo "
                   f"${s['min_orden']:,} CLP (≈{s['min_usdt']:,.0f} USDT)")
        avisos.append(f"{len(saltados)} anuncio(s) más barato(s) quedaron afuera porque no "
                      f"aceptan este monto — ej.: {detalle}. El VWAP ya los excluye.")
    if mejor_accesible and mejor_accesible > mejor + 0.005:
        avisos.append(f"El tope del libro ({mejor:,.2f}) no te acepta {monto:,.0f} USDT; "
                      f"el mejor precio que sí te acepta es {mejor_accesible:,.2f}.")

    return jsonify({
        "monto": round(monto),
        "margen_objetivo": margen,
        # ── CAPACIDAD DE RECOMPRA (COL57) ────────────────────────────
        # DOS numeros, porque responden preguntas distintas y Sebastian
        # necesita las dos:
        #   ahora  = con los pesos que YA tengo (lo que puedo hacer ya)
        #   total  = si ademas vendo todo el USDT que tengo parado
        # El segundo es el que faltaba: "puedo comprar solo 220 ahora, pero
        # en cuanto me salgan unas ordenes voy a poder recomprar mas" — sin
        # ese numero no se ve el techo real de la jornada.
        # Se dividen por el VWAP (el precio REAL de barrer el libro por este
        # monto), no por el precio de referencia: es lo que de verdad va a
        # pagar. Por eso se calcula aca abajo y no arriba.
        # El VWAP del barrido chico y el del grande casi no se diferencian
        # (medido 6-ago: 919,34 barriendo 219 vs 919,24 barriendo 763 — un
        # 0,01%), asi que usar uno solo para los dos numeros no distorsiona.
        # Nota: el grande sale un pelo MAS BARATO, porque al comprar mas se
        # habilitan anuncios con minimo de 400.000 CLP que quedan afuera en
        # una tanda chica.
        "capacidad": ({
            "ahora_usdt": round(saldo_clp / vwap, 1) if (saldo_clp and vwap) else None,
            "total_usdt": round(patrimonio_clp / vwap, 1) if (patrimonio_clp and vwap) else None,
            "clp_disponible": round(saldo_clp) if saldo_clp else None,
            "usdt_por_vender": round(saldo_usdt, 2) if saldo_usdt else None,
            "precio_usado": round(vwap, 2) if vwap else None,
        } if (saldo_clp is not None or patrimonio_clp) else None),
        # misma pregunta que capacidad.ahora_usdt -> tiene que dar lo mismo,
        # asi que sale del mismo calculo y no de una cuenta paralela.
        "puede_recomprar_usdt": (round(saldo_clp / vwap, 2) if (saldo_clp and vwap) else None),
        "clp_disponible": (round(saldo_clp) if saldo_clp else None),
        "acotado_por_saldo": bool(saldo_real and not monto_pedido
                                  and float(c.get("CICLO_MONTO_DEFAULT", 1200)) > saldo_real),
        "tandas": _tandas_recompra(),
        # COL38: de donde salio el libro y hace cuanto. Sin esto no se puede
        # saber si el precio que se esta mirando sigue existiendo.
        "fuente_libro": fuente,
        "edad_seg": (round(edad_seg, 1) if edad_seg is not None else None),
        "descartados_sin_pago": len(sin_pago),
        "sin_pago": sin_pago[:5],
        "recompra": {"vwap": round(vwap, 2), "niveles": niveles,
                     "mejor": round(mejor, 2), "alcanza": alcanza,
                     "profundidad_libro": round(profundidad),
                     "llenado": round(llenado, 2),
                     # COL32: que quedo afuera por no aceptar el monto
                     "mejor_accesible": (round(mejor_accesible, 2) if mejor_accesible else None),
                     "saltados_por_limite": len(saltados),
                     "saltados": saltados[:5]},
        "costos": {"taker_pct": round(costo_taker_pct, 4), "maker_pct": com_maker,
                   "total_pct": round(costo_total_pct, 4), "taker_usdt": com_taker},
        "venta": {"precio": round(precio_venta, 2), "posicion_est": posicion_est,
                  "banda_pct": round(banda_pct, 3)},
        "flujo": flujo,
        "ganancia": {"por_vuelta_usdt": round(gan_vuelta, 2),
                     "por_dia_usdt": round(gan_dia, 1),
                     "por_mes_20d": round(gan_dia * 20)},
        "veredicto": veredicto, "mensaje": mensaje, "avisos": avisos,
        "ratio_mercado": ratio,
        "nota": ("El VWAP es de barrer el libro EN VIVO (no el mejor precio a secas). "
                 "El flujo por banda es histórico de 7 días y se recalcula 1x/día."),
    })


def _version_num(txt):
    """'COL31' -> 31. Sirve para comparar con que version se hizo un backup.
    Devuelve 0 si no se puede leer (backup viejo sin nota, o nota libre)."""
    import re as _re
    m = _re.search(r"COL(\d+)", str(txt or ""), _re.I)
    return int(m.group(1)) if m else 0


@app.route("/api/rutinas")
def api_rutinas():
    """RUTINAS DE MANTENIMIENTO (COL25): que tareas periodicas estan vencidas.

    Cada rutina detecta sola su ultima vez desde datos REALES, no depende de
    que el usuario marque nada:
      - ancla  -> ultima fila de inventario_ancla
      - csv    -> ultimo 'importado' en mis_ordenes_reales
      - backup -> unica que no deja rastro propio (el ZIP se baja al disco del
                  usuario), asi que usa rutinas_log, marcado desde el frontend.
    Asi el estado es real y unico: se ve igual desde la compu o el telefono
    (antes el backup vivia en localStorage y cada dispositivo creia otra cosa)."""
    with config_lock:
        c = dict(config)
    now = datetime.now(SANTIAGO_TZ)

    def dias_desde(dt):
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=SANTIAGO_TZ)
        return (now - dt).total_seconds() / 86400.0

    ultimos = {"ancla": None, "csv": None, "backup": None}
    backup_nota = None
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT MAX(ts) m FROM inventario_ancla")
                ultimos["ancla"] = (cur.fetchone() or {}).get("m")
                cur.execute("SELECT MAX(importado) m FROM mis_ordenes_reales")
                ultimos["csv"] = (cur.fetchone() or {}).get("m")
                cur.execute("SELECT ts, nota FROM rutinas_log WHERE tarea='backup' "
                            "ORDER BY ts DESC LIMIT 1")
                r = cur.fetchone() or {}
                ultimos["backup"] = r.get("ts")
                backup_nota = r.get("nota")
    except Exception as e:
        print(f"[rutinas] {e}")
        return jsonify({"rutinas": [], "error": str(e)[:200]})

    # ¿El ultimo backup se hizo con el formato COMPLETO? Hasta COL30 el ZIP
    # traia 2 tablas de 14 (solo el detalle crudo, justo lo unico descartable),
    # asi que un backup viejo NO respalda nada permanente. Sin este chequeo la
    # rutina diria "al dia" apoyandose en un respaldo que no sirve.
    backup_completo = _version_num(backup_nota) >= 31

    # disco: entra como señal extra para el backup (si esta lleno, urge mas)
    # COL26: usa la misma _uso_disco_mb() de /api/storage (tablas + WAL), asi
    # los dos endpoints nunca se desalinean entre si.
    pct_disco = None
    try:
        usado_mb, _, _ = _uso_disco_mb()
        pct_disco = round(usado_mb / LIMITE_MB * 100)
    except Exception:
        pass

    defs = [
        {"id": "ancla", "titulo": "Re-anclar inventario",
         "cada": int(c.get("RUT_ANCLA_DIAS", 3)),
         "accion": "Actualizar saldos reales (Binance + Mercado Pago)",
         "porque": "La estimación se desvía con el tiempo; anclar la corrige y mide el drift.",
         "auto": True},
        {"id": "csv", "titulo": "Importar CSV de Binance",
         "cada": int(c.get("RUT_CSV_DIAS", 10)),
         "accion": "Exportar órdenes y correr scripts/importar_mis_ordenes.bat",
         "porque": "Recalibra la detección y tu ticket propio (lo que estima tus fills ocultos).",
         "auto": True},
        {"id": "backup", "titulo": "Backup de la base",
         "cada": int(c.get("RUT_BACKUP_DIAS", 7)),
         "accion": "Pestaña Backup → descargar el ZIP general",
         "porque": (f"El detalle del libro se purga a los {int(c.get('DETALLE_DIAS', 10))} días: "
                    "lo que no se respalda, se pierde. Exportá SIEMPRE antes de vaciar."),
         "auto": False},
    ]
    rutinas, vencidas = [], 0
    for d in defs:
        dd = dias_desde(ultimos[d["id"]])
        cada = max(1, d["cada"])
        if dd is None:
            estado, restantes = "nunca", 0
        elif dd >= cada:
            estado, restantes = "vencida", 0
        elif dd >= cada * 0.75:
            estado, restantes = "pronto", round(cada - dd, 1)
        else:
            estado, restantes = "ok", round(cada - dd, 1)
        if estado in ("vencida", "nunca"):
            vencidas += 1
        rutinas.append({**d,
                        "ultima": str(ultimos[d["id"]]) if ultimos[d["id"]] else None,
                        "dias_desde": round(dd, 1) if dd is not None else None,
                        "estado": estado, "dias_restantes": restantes})

    # Un backup hecho con el formato viejo NO cuenta como respaldo: traia solo
    # el detalle crudo (lo unico que caduca solo) y dejaba afuera TODO lo
    # permanente — precio historico, agregado por competidor, mis ordenes
    # reales. Se avisa hasta que se baje un ZIP nuevo.
    if ultimos["backup"] is not None and not backup_completo:
        for r in rutinas:
            if r["id"] == "backup":
                if r["estado"] not in ("vencida", "nunca"):
                    r["estado"] = "vencida"
                    vencidas += 1
                r["porque"] = ("El último backup es del formato viejo (traía 2 tablas de 14): "
                               "lo permanente — precio histórico, resumen por competidor, tus "
                               "órdenes reales — NO está respaldado. Bajá un ZIP nuevo.")
                r["formato_viejo"] = True

    # el disco lleno adelanta el backup. Con DETALLE_DIAS=10 el techo normal es
    # ~53%, asi que pasar de 70% significa que algo se salio de lo previsto
    # (no es la situacion de rutina): respaldar primero, despues investigar.
    if pct_disco is not None and pct_disco >= 70:
        for r in rutinas:
            if r["id"] == "backup" and r["estado"] not in ("vencida", "nunca"):
                r["estado"] = "vencida"
                r["porque"] = (f"Disco al {pct_disco}%, por encima del techo esperado (~53% con "
                               f"{int(c.get('DETALLE_DIAS', 10))} días de detalle): respaldá y "
                               "revisá qué está creciendo de más.")
                vencidas += 1
    return jsonify({"rutinas": rutinas, "vencidas": vencidas, "pct_disco": pct_disco,
                    "backup_completo": backup_completo,
                    "nota": "Las rutinas de ancla y CSV se detectan solas de la actividad real. "
                            "El backup hay que marcarlo porque el ZIP se descarga fuera del monitor."})


@app.route("/api/rutinas/marcar", methods=["POST"])
def api_rutinas_marcar():
    """Marca una rutina como hecha. Hoy solo hace falta para 'backup' (las
    otras se detectan solas), pero se acepta cualquiera por si se agrega otra."""
    if not _token_ok():
        return jsonify({"ok": False, "error": "token requerido o invalido"}), 401
    data = request.get_json() or {}
    tarea = (data.get("tarea") or "").strip().lower()
    if tarea not in ("ancla", "csv", "backup"):
        return jsonify({"ok": False, "error": "tarea invalida"}), 400
    now = datetime.now(SANTIAGO_TZ)
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                # Se guarda la VERSION que hizo el backup (COL31): el formato
                # cambio de raiz — hasta COL30 el ZIP traia 2 tablas de 14, asi
                # que un backup viejo NO sirve como respaldo completo y la
                # rutina tiene que poder distinguirlo (ver api_rutinas).
                cur.execute("INSERT INTO rutinas_log (tarea, ts, nota) VALUES (%s,%s,%s)",
                            (tarea, now.strftime("%Y-%m-%d %H:%M:%S"),
                             (data.get("nota") or VERSION)[:200]))
            conn.commit()
    except Exception as e:
        print(f"[rutinas marcar] {e}")
        return jsonify({"ok": False, "error": str(e)[:200]}), 500
    return jsonify({"ok": True, "tarea": tarea, "ts": now.strftime("%Y-%m-%d %H:%M:%S")})


@app.route("/api/macro")
def api_macro():
    """CONTEXTO MACRO (COL35): dolar forex, VIX y cobre + la brecha del P2P
    contra el dolar formal.

    LEE DE MEMORIA, nunca dispara red: el hilo ciclo_colector_macro es el
    unico que sale a buscar los datos. Asi este endpoint no puede colgarse
    aunque la fuente externa este caida.

    Devuelve 'edad_min' para que la UI pueda avisar si el dato quedo viejo
    (fuente caida) en vez de mostrar un numero rancio como si fuera de ahora.
    Param opcional: ?historial=N para traer las ultimas N filas guardadas."""
    with macro_lock:
        d = dict(ultimo_macro)
    if not d:
        return jsonify({"disponible": False,
                        "nota": "Todavia no se leyo el contexto macro (el hilo "
                                "corre a los ~20s del arranque y cada MACRO_MIN)."})
    edad = None
    try:
        ts = datetime.strptime(d["ts"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=SANTIAGO_TZ)
        edad = round((datetime.now(SANTIAGO_TZ) - ts).total_seconds() / 60, 1)
    except Exception:
        pass
    d["disponible"] = True
    d["edad_min"] = edad
    # 3x el intervalo esperado = algo no esta actualizando
    try:
        with config_lock:
            lim = int(config.get("MACRO_MIN", 15) or 15) * 3
    except Exception:
        lim = 45
    d["viejo"] = bool(edad is not None and edad > lim)

    # la senal operativa: cuanto se movio el forex que el P2P todavia no siguio
    try:
        d["desfase"] = calcular_desfase()
    except Exception as e:
        print(f"[macro desfase] {e}")
        d["desfase"] = None

    try:
        n = int(request.args.get("historial", 0))
    except (ValueError, TypeError):
        n = 0
    if n > 0:
        try:
            with get_conn() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("""SELECT ts, usdclp_forex, vix, cobre, p2p_ref, brecha_pct
                                   FROM snapshots_macro ORDER BY ts DESC LIMIT %s""",
                                [max(1, min(500, n))])
                    d["historial"] = [
                        {"ts": str(r["ts"])[:19],
                         "usdclp_forex": float(r["usdclp_forex"]) if r["usdclp_forex"] else None,
                         "vix": float(r["vix"]) if r["vix"] else None,
                         "cobre": float(r["cobre"]) if r["cobre"] else None,
                         "p2p_ref": float(r["p2p_ref"]) if r["p2p_ref"] else None,
                         "brecha_pct": float(r["brecha_pct"]) if r["brecha_pct"] else None}
                        for r in cur.fetchall()]
        except Exception as e:
            print(f"[macro historial] {e}")
            d["historial"] = []
    d["nota"] = ("El P2P suele seguir al mercado formal con retardo. La brecha es "
                 "cuanto esta el P2P por encima del dolar forex. Las variaciones son "
                 "contra el cierre previo. Serie guardada cada MACRO_MIN para poder "
                 "MEDIR despues si de verdad anticipa (no asumirlo).")
    return jsonify(d)


@app.route("/api/inventario/historial")
def api_inventario_historial():
    """HISTORIAL DE ANCLAS (COL34): cada vez que se ancla el inventario queda
    una fila NUEVA en inventario_ancla (nunca se pisa la anterior) — este
    endpoint expone ese historial, que ya existia en la DB pero no se podia
    consultar desde ningun lado. Pedido de Sebastian: poder cruzar sus anclas
    (apertura/cierre) contra lo que anota a mano en la bitacora.
    Params: ?dias=30&limit=200&fmt=json|csv"""
    try:
        dias = max(1, min(365, int(request.args.get("dias", 30))))
    except (ValueError, TypeError):
        dias = 30
    try:
        limit = max(1, min(1000, int(request.args.get("limit", 200))))
    except (ValueError, TypeError):
        limit = 200
    fmt = (request.args.get("fmt", "json") or "json").lower()

    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT ts, usdt, clp, precio_ref, nota, usdclp_forex
                    FROM inventario_ancla
                    WHERE ts >= NOW() - (%s || ' days')::INTERVAL
                    ORDER BY ts DESC LIMIT %s
                """, [dias, limit])
                filas = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f"[inventario historial] {e}")
        return jsonify({"error": str(e)[:200]}), 500

    # drift entre cada ancla y la INMEDIATAMENTE anterior (mas vieja) — asi se
    # ve de un vistazo cuanto se desvio la estimacion entre una y otra, el
    # mismo calculo que ya hace api_inventario_ancla() para la ultima.
    salida = []
    for i, f in enumerate(filas):
        anterior = filas[i + 1] if i + 1 < len(filas) else None
        drift_usdt = round(float(f["usdt"]) - float(anterior["usdt"]), 2) if anterior else None
        drift_clp = round(float(f["clp"]) - float(anterior["clp"])) if anterior else None
        # brecha del P2P contra el dolar formal EN ESE MOMENTO (COL35). Las
        # anclas anteriores al deploy no tienen forex guardado -> queda None.
        p_ref = float(f["precio_ref"]) if f["precio_ref"] else None
        fx = float(f["usdclp_forex"]) if f.get("usdclp_forex") else None
        salida.append({
            "ts": str(f["ts"])[:19], "usdt": float(f["usdt"]), "clp": float(f["clp"]),
            "precio_ref": p_ref,
            "usdclp_forex": fx,
            "brecha_pct": (round((p_ref / fx - 1) * 100, 2) if (p_ref and fx) else None),
            "nota": f["nota"] or "",
            "drift_usdt": drift_usdt, "drift_clp": drift_clp,
        })

    if fmt == "csv":
        import csv, io
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["fecha_hora", "usdt", "clp", "precio_ref_p2p", "usdclp_forex",
                    "brecha_pct", "nota", "drift_usdt", "drift_clp"])
        for s in salida:
            w.writerow([s["ts"], s["usdt"], s["clp"], s["precio_ref"] or "",
                       s["usdclp_forex"] or "", s["brecha_pct"] if s["brecha_pct"] is not None else "",
                       s["nota"],
                       s["drift_usdt"] if s["drift_usdt"] is not None else "",
                       s["drift_clp"] if s["drift_clp"] is not None else ""])
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=historial_saldos_{dias}d.csv"})

    return jsonify({"historial": salida, "total": len(salida),
                    "nota": ("Cada fila es un ancla real que hiciste (nunca se sobreescriben). "
                             "'drift' es cuanto cambio el saldo respecto del ancla anterior — "
                             "no es P&L, incluye depositos/retiros y trades.")})


@app.route("/api/inventario/ancla", methods=["POST"])
def api_inventario_ancla():
    """Fija los saldos REALES (la verdad). Devuelve el drift vs lo estimado.

    COL40: dos salvaguardas agregadas tras la auditoria del 31-jul, que
    encontro un 51.517 USDT (typo de ~100x) cargado sin ningun chequeo.
    - SALTO DE MAGNITUD: bloquea (409) si usdt o clp saltan 8x+ contra el
      ancla anterior, salvo que venga confirmar=true. No es un limite fijo
      en USDT/CLP (un deposito grande legitimo puede pasar cualquier techo
      fijo) -- es un limite RELATIVO al ancla previa, que es lo que de verdad
      distingue un typo de un movimiento real.
    - DUPLICADO: solo avisa, nunca bloquea. El boton "Chequeo" invita a
      re-anclar seguido a proposito, asi que dos valores parecidos en poco
      tiempo puede ser el chequeo cumpliendo su funcion, no un error.
    """
    if not _token_ok():
        return jsonify({"ok": False, "error": "token requerido o invalido"}), 401
    data = request.get_json() or {}
    try:
        usdt = float(data.get("usdt"))
        clp = float(data.get("clp"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "usdt y clp son obligatorios y numericos"}), 400
    if usdt < 0 or clp < 0:
        return jsonify({"ok": False, "error": "los saldos no pueden ser negativos"}), 400
    precio = _precio_mid()
    now = datetime.now(SANTIAGO_TZ)

    # verdad de terreno previa: alimenta el drift de siempre Y las dos
    # salvaguardas nuevas. Una sola lectura para las tres cosas.
    prev_row = None
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT ts, usdt, clp FROM inventario_ancla ORDER BY ts DESC LIMIT 1")
                r = cur.fetchone()
                if r:
                    prev_row = {"ts": r["ts"], "usdt": float(r["usdt"]), "clp": float(r["clp"])}
    except Exception as e:
        print(f"[ancla prev] {e}")

    UMBRAL_SALTO = 8.0
    if prev_row and not data.get("confirmar"):
        for campo, nuevo in (("usdt", usdt), ("clp", clp)):
            anterior = prev_row[campo]
            if anterior >= 1 and (nuevo / anterior >= UMBRAL_SALTO or nuevo / anterior <= 1 / UMBRAL_SALTO):
                factor = round(nuevo / anterior, 1)
                return jsonify({
                    "ok": False, "requiere_confirmacion": True,
                    "aviso": (f"{campo.upper()} pasa de {anterior:,.2f} a {nuevo:,.2f} "
                              f"({factor}x) contra el ancla anterior — un salto asi suele ser "
                              f"un typo, no un movimiento real. Si es correcto, guardá de nuevo para confirmar."),
                }), 409

    aviso_duplicado = None
    if prev_row:
        try:
            minutos = (now - prev_row["ts"].replace(tzinfo=SANTIAGO_TZ)).total_seconds() / 60
        except Exception:
            minutos = None
        cerca = (prev_row["usdt"] > 0 and prev_row["clp"] > 0
                 and abs(usdt - prev_row["usdt"]) / prev_row["usdt"] < 0.005
                 and abs(clp - prev_row["clp"]) / prev_row["clp"] < 0.005)
        if minutos is not None and minutos < 5 and cerca:
            aviso_duplicado = f"Casi los mismos valores que hace {round(minutos)} min — revisá que no sea una carga repetida."

    # dolar formal del momento (COL35): se lee de MEMORIA, no dispara red, asi
    # que si la fuente macro esta caida el ancla se guarda igual con NULL.
    fx_oficial = None
    try:
        with macro_lock:
            fx_oficial = ultimo_macro.get("usdclp_forex")
    except Exception:
        pass
    # drift: cuanto se desvio la estimacion de la realidad (calibracion gratis)
    drift = None
    try:
        prev = api_inventario().get_json()
        if prev.get("configurado"):
            drift = {"usdt": round(usdt - prev["saldos"]["usdt"], 2),
                     "clp": round(clp - prev["saldos"]["clp"])}
    except Exception:
        pass
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO inventario_ancla
                               (ts, usdt, clp, precio_ref, nota, usdclp_forex)
                               VALUES (%s,%s,%s,%s,%s,%s)""",
                            (now.strftime("%Y-%m-%d %H:%M:%S"), usdt, clp, precio,
                             (data.get("nota") or "")[:200], fx_oficial))
            conn.commit()
    except Exception as e:
        print(f"[ancla] {e}")
        return jsonify({"ok": False, "error": str(e)[:200]}), 500
    _invalidar_inventario()   # COL55: el cache no puede sobrevivir a un ancla nueva
    return jsonify({"ok": True, "usdt": usdt, "clp": clp,
                    "precio_ref": round(precio, 2), "drift": drift,
                    "aviso_duplicado": aviso_duplicado})


@app.route("/api/inventario/movimiento", methods=["POST"])
def api_inventario_movimiento():
    """Carga manual: taker (trade) o externo (deposito/retiro, NO es P&L)."""
    if not _token_ok():
        return jsonify({"ok": False, "error": "token requerido o invalido"}), 401
    data = request.get_json() or {}
    tipo = (data.get("tipo") or "").strip().lower()
    if tipo not in ("taker", "externo", "maker"):
        return jsonify({"ok": False, "error": "tipo debe ser taker, externo o maker"}), 400
    lado = (data.get("lado") or "").strip().lower() or None
    if tipo != "externo" and lado not in ("compra", "venta"):
        return jsonify({"ok": False, "error": "lado debe ser compra o venta"}), 400
    try:
        usdt = float(data.get("usdt") or 0)
        precio = float(data.get("precio") or 0)
        clp = float(data.get("clp") or 0)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "valores numericos invalidos"}), 400
    if tipo != "externo":
        if usdt <= 0 or precio <= 0:
            return jsonify({"ok": False, "error": "usdt y precio deben ser > 0"}), 400
        clp = usdt * precio
    elif usdt == 0 and clp == 0:
        return jsonify({"ok": False, "error": "un movimiento externo necesita usdt o clp"}), 400
    now = datetime.now(SANTIAGO_TZ)
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO movimientos_inventario
                               (ts, tipo, lado, usdt, clp, precio, nota)
                               VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                            (now.strftime("%Y-%m-%d %H:%M:%S"), tipo, lado,
                             usdt, clp, precio, (data.get("nota") or "")[:200]))
                nuevo = cur.fetchone()[0]
            conn.commit()
    except Exception as e:
        print(f"[movimiento] {e}")
        return jsonify({"ok": False, "error": str(e)[:200]}), 500
    _invalidar_inventario()   # COL55
    return jsonify({"ok": True, "id": nuevo, "tipo": tipo, "lado": lado,
                    "usdt": usdt, "clp": round(clp), "precio": precio})


@app.route("/api/inventario/movimientos")
def api_inventario_movimientos():
    """Lista los movimientos MANUALES cargados, para poder corregirlos (COL48).

    Solo los manuales: los maker se derivan en vivo de fills_estimados y no
    son filas editables — si uno de esos esta mal, lo que corresponde es
    re-anclar, no 'editar' una estimacion.
    Params: ?dias=7 (default) — desde el ancla vigente si es mas reciente."""
    try:
        dias = max(1, min(365, int(request.args.get("dias", 7))))
    except (ValueError, TypeError):
        dias = 7
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""SELECT id, ts, tipo, lado, usdt, clp, precio, nota
                               FROM movimientos_inventario
                               WHERE ts >= NOW() - (%s || ' days')::INTERVAL
                               ORDER BY ts DESC LIMIT 200""", [dias])
                filas = [{"id": r["id"], "ts": str(r["ts"])[:19], "tipo": r["tipo"],
                          "lado": r["lado"],
                          "usdt": float(r["usdt"] or 0), "clp": float(r["clp"] or 0),
                          "precio": float(r["precio"] or 0), "nota": r["nota"] or ""}
                         for r in cur.fetchall()]
    except Exception as e:
        print(f"[movimientos lista] {e}")
        return jsonify({"error": str(e)[:200]}), 500
    return jsonify({"movimientos": filas, "dias": dias})


@app.route("/api/inventario/movimiento/<int:mid>", methods=["PATCH", "DELETE"])
def api_inventario_movimiento_editar(mid):
    """Corregir o borrar un movimiento manual (COL48).

    POR QUE EXISTE: el 4-ago Sebastian cargo una orden con el precio mal
    tipeado (919,90 en vez de 917,90 — un 9 por un 7) y NO habia forma de
    arreglarlo desde la app; hubo que hacerlo con SQL a mano contra la base
    de produccion. Un dato mal cargado envenena el inventario, el P&L y el
    costo de campania, asi que tiene que poder corregirse donde se cargo.

    PATCH acepta usdt / precio / lado / nota. El CLP NO se acepta como
    parametro en trades: se recalcula usdt*precio, que es como se guardo en
    el POST — dejar que entren descoordinados es justamente lo que rompe el
    cuadre. En 'externo' si se acepta clp directo (un deposito no tiene
    precio).
    DELETE borra la fila. Devuelve lo borrado para poder rehacerlo a mano si
    fue un error."""
    if not _token_ok():
        return jsonify({"ok": False, "error": "token requerido o invalido"}), 401
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""SELECT id, ts, tipo, lado, usdt, clp, precio, nota
                               FROM movimientos_inventario WHERE id = %s""", [mid])
                actual = cur.fetchone()
                if not actual:
                    return jsonify({"ok": False, "error": f"no existe el movimiento {mid}"}), 404
                antes = {"id": actual["id"], "ts": str(actual["ts"])[:19],
                         "tipo": actual["tipo"], "lado": actual["lado"],
                         "usdt": float(actual["usdt"] or 0), "clp": float(actual["clp"] or 0),
                         "precio": float(actual["precio"] or 0), "nota": actual["nota"] or ""}

                if request.method == "DELETE":
                    cur.execute("DELETE FROM movimientos_inventario WHERE id = %s", [mid])
                    conn.commit()
                    _invalidar_inventario()   # COL55
                    return jsonify({"ok": True, "borrado": antes})

                data = request.get_json() or {}
                tipo = antes["tipo"]
                lado = (data.get("lado") or antes["lado"] or "").strip().lower() or None
                if tipo != "externo" and lado not in ("compra", "venta"):
                    return jsonify({"ok": False, "error": "lado debe ser compra o venta"}), 400
                try:
                    usdt = float(data["usdt"]) if "usdt" in data else antes["usdt"]
                    precio = float(data["precio"]) if "precio" in data else antes["precio"]
                    clp = float(data["clp"]) if "clp" in data else antes["clp"]
                except (TypeError, ValueError):
                    return jsonify({"ok": False, "error": "valores numericos invalidos"}), 400
                if tipo != "externo":
                    if usdt <= 0 or precio <= 0:
                        return jsonify({"ok": False, "error": "usdt y precio deben ser > 0"}), 400
                    clp = usdt * precio          # mismo criterio que el POST
                elif usdt == 0 and clp == 0:
                    return jsonify({"ok": False, "error": "un movimiento externo necesita usdt o clp"}), 400

                # SALVAGUARDA, mismo espiritu que el ancla (COL40): un cambio
                # de 8x+ en el precio es casi siempre un typo, no una
                # correccion real. Se puede forzar con confirmar=true.
                if (tipo != "externo" and antes["precio"] > 0 and not data.get("confirmar")
                        and (precio / antes["precio"] >= 8 or precio / antes["precio"] <= 1 / 8)):
                    return jsonify({
                        "ok": False, "requiere_confirmacion": True,
                        "aviso": (f"El precio pasa de {antes['precio']:,.2f} a {precio:,.2f} "
                                  f"({precio / antes['precio']:.1f}x). Si es correcto, guardá de nuevo."),
                    }), 409

                cur.execute("""UPDATE movimientos_inventario
                               SET lado=%s, usdt=%s, clp=%s, precio=%s, nota=%s
                               WHERE id=%s""",
                            (lado, usdt, clp, precio,
                             (data.get("nota") if "nota" in data else antes["nota"])[:200], mid))
            conn.commit()
    except Exception as e:
        print(f"[movimiento editar] {e}")
        return jsonify({"ok": False, "error": str(e)[:200]}), 500
    _invalidar_inventario()   # COL55
    return jsonify({"ok": True, "antes": antes,
                    "ahora": {"id": mid, "tipo": tipo, "lado": lado, "usdt": usdt,
                              "clp": round(clp), "precio": precio}})


# ══════════════════════════════════════════════════════════════
#  CARRERA A MERCHANT (COL36)
#  Los requisitos REALES, tomados de la pagina de elegibilidad de
#  Binance (captura del 31-jul-2026). Antes el codigo usaba
#  "300 ordenes", que NO existe como requisito.
# ══════════════════════════════════════════════════════════════
MERCHANT_REQ = {
    "dias_verificado":   90,      # dias desde la verificacion de identidad
    "ordenes_total":     500,     # historico completo
    "vol_total_btc":     1.0,     # historico completo
    "ordenes_30d":       150,     # ventana MOVIL de 30 dias
    "vol_30d_btc":       0.5,     # ventana MOVIL de 30 dias
    "tasa_finalizacion": 90.0,    # ultimos 30 dias
}


def _btc_usd():
    """Precio del BTC para traducir las metas (que estan en BTC) a USDT."""
    try:
        with macro_lock:
            v = ultimo_macro.get("btc_usd")
        if v:
            return float(v)
    except Exception:
        pass
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""SELECT btc_usd FROM snapshots_macro
                               WHERE btc_usd IS NOT NULL ORDER BY ts DESC LIMIT 1""")
                r = cur.fetchone()
                if r and r["btc_usd"]:
                    return float(r["btc_usd"])
    except Exception as e:
        print(f"[merchant btc] {e}")
    return None


def _minimo_para_ticket():
    """Tabla MEDIDA de 'que minimo de orden pone cada uno' vs 'que ticket
    recibe'. Es la respuesta con datos a: como consigo tickets mas grandes.

    Medido el 31-jul-2026 sobre 257 anunciantes: correlacion +0,80 entre el
    minimo publicado y el ticket recibido. No es teoria, es el mercado."""
    tramos = [(0, 20000), (20000, 50000), (50000, 150000), (150000, 10 ** 9)]
    out = []
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    WITH lim AS (
                        SELECT anunciante, tipo,
                               mode() WITHIN GROUP (ORDER BY min_orden) mn
                        FROM snapshots_detalle
                        WHERE min_orden IS NOT NULL
                          AND snapshot_timestamp >= NOW() - INTERVAL '7 days'
                        GROUP BY 1, 2
                    ),
                    tk AS (
                        SELECT anunciante, tipo,
                               PERCENTILE_CONT(0.5) WITHIN GROUP (
                                   ORDER BY monto / NULLIF(ordenes, 0)) ticket
                        FROM fills_estimados
                        WHERE metodo = 'directo' AND ordenes > 0
                          AND monto > 0 AND monto < 5000
                          AND ts >= NOW() - INTERVAL '7 days'
                        GROUP BY 1, 2 HAVING COUNT(*) >= 5
                    )
                    SELECT l.mn, t.ticket FROM lim l JOIN tk t USING (anunciante, tipo)
                    WHERE l.mn IS NOT NULL AND t.ticket IS NOT NULL
                """)
                filas = [(float(r["mn"]), float(r["ticket"])) for r in cur.fetchall()]
    except Exception as e:
        print(f"[merchant minimos] {e}")
        return []
    for a, b in tramos:
        g = sorted(t for m, t in filas if a <= m < b)
        if not g:
            continue
        out.append({"min_desde": a, "min_hasta": (None if b > 10 ** 8 else b),
                    "n": len(g), "ticket_mediano": round(g[len(g) // 2])})
    return out


@app.route("/api/merchant")
def api_merchant():
    """CARRERA A MERCHANT (COL36): estado real contra los 6 requisitos, con
    conteo EN VIVO y proyeccion que respeta la ventana movil.

    ── QUE ES EXACTO Y QUE ES ESTIMADO ──
    - ORDENES 30d: EXACTO. Se lee monthOrderCount del libro publico cada 2 min
      cuando estas publicado. Verificado 31-jul: monitor 131 = Binance 131.
    - VOLUMEN: ESTIMADO. Binance no lo publica. Se calcula
      ordenes_en_vivo x ticket, donde el ticket sale del ANCLA (volumen real
      dividido ordenes reales de ese momento). Con el ancla del 31-jul el
      error medido fue 0,04%.
    - El resto (ordenes totales, volumen total, tasa, dias) viene del ancla y
      se proyecta con lo que paso desde entonces.

    ── LA VENTANA MOVIL ──
    Los requisitos de 30 dias son de ventana MOVIL: lo de hace 31 dias
    desaparece. Por eso no alcanza con acumular, hay que SOSTENER un ritmo. Si
    el ritmo diario es menor al necesario, la meta NUNCA se alcanza: se cae por
    atras al mismo ritmo que entra."""
    with config_lock:
        c = dict(config)
    mi_nick = str(c.get("MI_NICKNAME") or "").strip()
    btc = _btc_usd()

    # ── ancla: la verdad de terreno ──
    ancla = None
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""SELECT * FROM merchant_ancla ORDER BY ts DESC LIMIT 1""")
                r = cur.fetchone()
                if r:
                    # los NUMERIC vuelven como Decimal; se pasan a float sin
                    # importar decimal (hasattr es suficiente y no agrega import)
                    ancla = {k: (float(v) if hasattr(v, "__float__") and not isinstance(v, (int, float)) else v)
                             for k, v in dict(r).items()}
                    ancla["ts"] = str(ancla["ts"])[:19]
    except Exception as e:
        print(f"[merchant ancla] {e}")

    # ── conteo EN VIVO desde el libro publico ──
    vivo = {"ordenes_30d": None, "visto": None, "serie": []}
    if mi_nick:
        try:
            with get_conn() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("""
                        SELECT MAX(completadas) o, MAX(snapshot_timestamp) ts
                        FROM snapshots_detalle
                        WHERE LOWER(anunciante) = LOWER(%s)
                          AND snapshot_timestamp >= NOW() - INTERVAL '2 days'
                    """, [mi_nick])
                    r = cur.fetchone()
                    if r and r["o"]:
                        vivo["ordenes_30d"] = int(r["o"])
                        vivo["visto"] = str(r["ts"])[:19]
                    # serie diaria: para ver el ritmo real y como se mueve
                    cur.execute("""
                        SELECT snapshot_timestamp::date d, MAX(completadas) o
                        FROM snapshots_detalle
                        WHERE LOWER(anunciante) = LOWER(%s)
                        GROUP BY 1 ORDER BY 1
                    """, [mi_nick])
                    prev = None
                    for x in cur.fetchall():
                        o = int(x["o"] or 0)
                        vivo["serie"].append({"fecha": str(x["d"]), "ordenes_30d": o,
                                              "variacion": (o - prev) if prev is not None else None})
                        prev = o
        except Exception as e:
            print(f"[merchant vivo] {e}")

    # ── ticket calibrado: el que hace cuadrar el ancla ──
    # (volumen REAL del ancla) / (ordenes REALES del ancla). Es mejor que el
    # ticket del CSV porque sale del mismo numero que Binance publica.
    ticket_cal, ticket_fuente = None, None
    if ancla and btc and ancla.get("vol_30d_btc") and ancla.get("ordenes_30d"):
        ticket_cal = (float(ancla["vol_30d_btc"]) * btc) / float(ancla["ordenes_30d"])
        ticket_fuente = "ancla"
    elif c.get("MI_TICKET_MEDIO"):
        ticket_cal = float(c["MI_TICKET_MEDIO"])
        ticket_fuente = "csv"

    # ── estado actual de cada requisito ──
    ord30 = vivo["ordenes_30d"] or (ancla or {}).get("ordenes_30d")
    vol30_btc = None
    if ord30 and ticket_cal and btc:
        vol30_btc = (ord30 * ticket_cal) / btc
    elif ancla:
        vol30_btc = ancla.get("vol_30d_btc")

    # totales: el ancla + lo hecho desde entonces (aproximado por el delta)
    ord_tot, vol_tot_btc = None, None
    if ancla:
        ord_tot = ancla.get("ordenes_total")
        vol_tot_btc = ancla.get("vol_total_btc")
        if ord_tot and vivo["ordenes_30d"] and ancla.get("ordenes_30d"):
            delta = vivo["ordenes_30d"] - int(ancla["ordenes_30d"])
            if delta > 0:                       # solo suma, nunca resta
                ord_tot = int(ord_tot) + delta
                if vol_tot_btc and ticket_cal and btc:
                    vol_tot_btc = float(vol_tot_btc) + (delta * ticket_cal) / btc

    def req(nombre, actual, meta, unidad):
        falta = None if actual is None else max(0, meta - actual)
        return {"clave": nombre, "actual": actual, "meta": meta, "unidad": unidad,
                "cumple": bool(actual is not None and actual >= meta),
                "falta": falta,
                "pct": (round(min(100, actual / meta * 100), 1) if actual is not None and meta else None)}

    reqs = [
        req("dias_verificado", (ancla or {}).get("dias_verificado"), MERCHANT_REQ["dias_verificado"], "días"),
        req("tasa_finalizacion", (ancla or {}).get("tasa_finalizacion"), MERCHANT_REQ["tasa_finalizacion"], "%"),
        req("ordenes_30d", ord30, MERCHANT_REQ["ordenes_30d"], "órdenes"),
        req("vol_30d_btc", (round(vol30_btc, 5) if vol30_btc else None), MERCHANT_REQ["vol_30d_btc"], "BTC"),
        req("ordenes_total", ord_tot, MERCHANT_REQ["ordenes_total"], "órdenes"),
        req("vol_total_btc", (round(float(vol_tot_btc), 5) if vol_tot_btc else None), MERCHANT_REQ["vol_total_btc"], "BTC"),
    ]

    # ── hace cuanto que no se ve actividad (COL37) ──
    # El ritmo se calcula con la mediana de las variaciones del contador, que
    # son de los dias en que SI aparecio en el libro. Si hace dias que no
    # aparece, ese ritmo es historia vieja y no se puede presentar como "asi
    # vas". Peor: la ventana movil sigue drenando mientras tanto.
    dias_parado = None
    if vivo.get("serie"):
        try:
            ult = datetime.strptime(vivo["serie"][-1]["fecha"], "%Y-%m-%d").date()
            dias_parado = (datetime.now(SANTIAGO_TZ).date() - ult).days
        except Exception:
            pass

    # ── lo que hace falta por dia, con la ventana movil ──
    plan = None
    if btc:
        vol_meta_usdt = MERCHANT_REQ["vol_30d_btc"] * btc
        ord_meta = MERCHANT_REQ["ordenes_30d"]
        # ritmo SOSTENIDO necesario: en equilibrio la ventana vale 30 x ritmo
        usdt_dia = vol_meta_usdt / 30
        ord_dia = ord_meta / 30
        # ticket que hace cuadrar las DOS metas a la vez
        ticket_obj = vol_meta_usdt / ord_meta
        # ritmo real observado (mediana de las variaciones diarias positivas)
        vars_ = [s["variacion"] for s in vivo["serie"] if s["variacion"] is not None]
        ord_dia_real = None
        if vars_:
            vs = sorted(vars_)
            ord_dia_real = vs[len(vs) // 2]
        usdt_dia_real = (ord_dia_real * ticket_cal) if (ord_dia_real and ticket_cal) else None
        plan = {
            "btc_usd": round(btc),
            "vol_meta_30d_usdt": round(vol_meta_usdt),
            "usdt_por_dia_necesario": round(usdt_dia),
            "ordenes_por_dia_necesario": round(ord_dia, 1),
            "ticket_objetivo_usdt": round(ticket_obj),
            "ticket_actual_usdt": (round(ticket_cal, 1) if ticket_cal else None),
            "ticket_fuente": ticket_fuente,
            "ordenes_por_dia_real": ord_dia_real,
            "usdt_por_dia_real": (round(usdt_dia_real) if usdt_dia_real else None),
            "factor_necesario": (round(usdt_dia / usdt_dia_real, 1)
                                 if usdt_dia_real else None),
            # si el ticket no sube, cuantas ordenes/dia harian falta
            "ordenes_dia_con_ticket_actual": (round(usdt_dia / ticket_cal, 1)
                                              if ticket_cal else None),
            # OJO: solo vale si viene operando. Con dias parados el ritmo
            # medido es de otro momento y no dice nada de como va HOY.
            "alcanza_con_ritmo_actual": bool(usdt_dia_real and usdt_dia_real >= usdt_dia
                                             and not (dias_parado and dias_parado >= 2)),
            "dias_parado": dias_parado,
        }

        # ── AJUSTE POR DIAS OPERABLES (COL37) ──────────────────────────
        # Repartir la meta entre 30 dias supone que se opera los 30. Sebastian
        # trabaja en un restaurant por turnos: la meta hay que concentrarla en
        # los dias que realmente puede sentarse a operar.
        #
        # NO SE FIJA UN NUMERO (COL37, corregido): "4 dias" es demasiado
        # rigido — pueden aparecer imprevistos y tampoco tiene sentido
        # comprometerse a una cifra exacta. Se devuelve un ABANICO de
        # escenarios y ademas lo que se MIDIO que hizo, para que la decision
        # sea informada en vez de un parametro inventado.
        with config_lock:
            dias_sem = max(1, min(7, int(config.get("DIAS_OPERABLES_SEMANA", 4) or 4)))
        mejor = max((s["variacion"] for s in vivo["serie"]
                     if s["variacion"] is not None), default=None)

        def escenario(d):
            f = 7.0 / d
            u = usdt_dia * f
            return {"dias_semana": d,
                    "dias_en_30": round(30 * d / 7),
                    "usdt_por_jornada": round(u),
                    "ordenes_por_jornada": (round(u / ticket_cal, 1) if ticket_cal else None),
                    "ordenes_ticket_objetivo": round(u / ticket_obj, 1),
                    # ¿lo logro alguna vez? se compara contra su MEJOR dia real
                    "logrado_alguna_vez": (bool(mejor and ticket_cal and mejor >= u / ticket_cal)
                                           if (mejor and ticket_cal) else None)}

        # dias REALMENTE operados, medidos: dias con variacion positiva del
        # contador sobre el total de dias con lectura. Es el dato honesto
        # contra el cual comparar cualquier plan.
        serie = vivo.get("serie") or []
        con_var = [s for s in serie if s["variacion"] is not None]
        activos = [s for s in con_var if s["variacion"] > 0]
        dias_reales = (round(len(activos) / len(con_var) * 7, 1) if con_var else None)

        plan.update({
            "dias_operables_semana": dias_sem,          # el de config, como referencia
            "dias_operables_medidos": dias_reales,      # lo que de verdad viene haciendo
            "escenarios_dias": [escenario(d) for d in (2, 3, 4, 5, 7)],
            "mejor_dia_medido_ordenes": mejor,
            # el escenario que corresponde al config, para el resumen de arriba
            "usdt_por_dia_operado": round(usdt_dia * 7.0 / dias_sem),
            "ordenes_por_dia_operado": round(ord_dia * 7.0 / dias_sem, 1),
            "ordenes_dia_operado_ticket_actual": (round(usdt_dia * 7.0 / dias_sem / ticket_cal, 1)
                                                  if ticket_cal else None),
        })

    return jsonify({
        "requisitos": reqs,
        "cumple_todo": all(r["cumple"] for r in reqs),
        "ancla": ancla,
        "vivo": vivo,
        "plan": plan,
        "minimos_medidos": _minimo_para_ticket(),
        "nota": ("Las órdenes de 30d son EXACTAS (contador oficial leído del libro). "
                 "El volumen es ESTIMADO: órdenes x ticket, con el ticket calibrado contra "
                 "el último ancla. Las metas de 30 días son de VENTANA MÓVIL: si el ritmo "
                 "diario queda por debajo del necesario, la meta nunca se alcanza."),
    })


@app.route("/api/merchant/ancla", methods=["POST"])
def api_merchant_ancla():
    """Registra los 6 numeros de la pagina de elegibilidad de Binance.
    Es la verdad de terreno que calibra toda la estimacion de volumen."""
    if not _token_ok():
        return jsonify({"ok": False, "error": "token requerido o invalido"}), 401
    d = request.get_json() or {}

    def num(k, tipo=float):
        v = d.get(k)
        if v in (None, ""):
            return None
        try:
            return tipo(v)
        except (TypeError, ValueError):
            return None

    campos = {
        "ordenes_total": num("ordenes_total", int),
        "ordenes_30d": num("ordenes_30d", int),
        "vol_total_btc": num("vol_total_btc"),
        "vol_30d_btc": num("vol_30d_btc"),
        "tasa_finalizacion": num("tasa_finalizacion"),
        "dias_verificado": num("dias_verificado", int),
    }
    if all(v is None for v in campos.values()):
        return jsonify({"ok": False, "error": "hace falta al menos un valor"}), 400

    now = datetime.now(SANTIAGO_TZ)
    btc = _btc_usd()
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO merchant_ancla
                    (ts, ordenes_total, ordenes_30d, vol_total_btc, vol_30d_btc,
                     tasa_finalizacion, dias_verificado, btc_usd, nota)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (now.strftime("%Y-%m-%d %H:%M:%S"), campos["ordenes_total"],
                     campos["ordenes_30d"], campos["vol_total_btc"], campos["vol_30d_btc"],
                     campos["tasa_finalizacion"], campos["dias_verificado"], btc,
                     (d.get("nota") or "")[:200]))
            conn.commit()
    except Exception as e:
        print(f"[merchant ancla POST] {e}")
        return jsonify({"ok": False, "error": str(e)[:200]}), 500
    return jsonify({"ok": True, "guardado": campos, "btc_usd": btc})


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
    # COL36 — METAS CORREGIDAS. Antes decia (32000, 64000, 300): el 300 NO
    # existe como requisito de Binance, y las dos de volumen estaban mal
    # etiquetadas como "minima/comoda" cuando en realidad son DOS requisitos
    # distintos (0,5 BTC en 30 dias movil + 1 BTC historico). Los valores
    # reales estan en MERCHANT_REQ; el detalle completo va en /api/merchant.
    _btc = _btc_usd() or 62000
    meta_min = MERCHANT_REQ["vol_30d_btc"] * _btc        # 0,5 BTC en 30d
    meta_comoda = MERCHANT_REQ["vol_total_btc"] * _btc   # 1 BTC historico
    meta_ord = MERCHANT_REQ["ordenes_30d"]               # 150, no 300
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
                 "metas REALES (COL36): 150 ordenes y 0,5 BTC en la ventana MOVIL de 30 dias, "
                 "mas 500 ordenes y 1 BTC historicos. Ver /api/merchant para el detalle."),
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


# Las dos tablas de detalle crudo son las PESADAS (~25 MB/dia entre las dos) y
# ademas se purgan solas, asi que en el backup se filtran por ventana de dias.
# Todo el resto de la base se vuelca ENTERO: son tablas chicas y son las
# permanentes (agregado diario, precio historico, mis ordenes reales,
# inventario, config). Si algun dia se pierde la DB de Railway, esas son las
# irreemplazables — el detalle crudo, en cambio, esta disenado para caducar.
EXPORT_VENTANA = {"snapshots_detalle": "snapshot_timestamp",
                  "snapshots_detalle_bybit": "snapshot_timestamp"}


def _tablas_de_la_base(cur):
    """Descubre las tablas en vez de tenerlas escritas a mano.

    POR QUE ASI (COL31): el backup viejo listaba dos tablas fijas, asi que
    cada tabla nueva (agregados_anunciante_dia, mis_ordenes_reales,
    inventario_ancla, perfil_banda...) quedaba FUERA del respaldo sin que
    nadie se enterara. Descubriendolas, una tabla nueva entra sola."""
    cur.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
        ORDER BY table_name
    """)
    return [r["table_name"] for r in cur.fetchall()]


def _csv_de_tabla(cur, tabla, dias=None):
    """Vuelca una tabla entera a CSV. Si la tabla tiene columna de ventana
    (las de detalle), filtra por los ultimos `dias`."""
    import csv, io
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        ORDER BY ordinal_position
    """, [tabla])
    cols = [r["column_name"] for r in cur.fetchall()]
    if not cols:
        return None, 0
    sel = ", ".join('"%s"' % c for c in cols)
    sql = 'SELECT %s FROM "%s"' % (sel, tabla)
    params = []
    col_ts = EXPORT_VENTANA.get(tabla)
    if col_ts and dias:
        sql += ' WHERE "%s" >= NOW() - (%%s || \' days\')::INTERVAL' % col_ts
        params.append(dias)
        sql += ' ORDER BY "%s" DESC' % col_ts
    cur.execute(sql, params)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    n = 0
    for r in cur.fetchall():
        fila = []
        for c in cols:
            v = r[c]
            if v is None:
                fila.append("")
            elif isinstance(v, datetime):
                fila.append(str(v)[:19])
            else:
                fila.append(v)
        w.writerow(fila)
        n += 1
    return buf.getvalue(), n


@app.route("/api/export/todo")
def api_export_todo():
    """Backup COMPLETO en un clic: ZIP con TODAS las tablas de la base.
    El detalle crudo (pesado) se recorta a ?dias=N (default 30); el resto va
    entero. Incluye un LEEME.txt con el inventario de lo que trae.
    Param: ?dias=N"""
    import io, zipfile
    try:
        dias = int(request.args.get("dias", 30))
    except (ValueError, TypeError):
        dias = 30
    hoy = datetime.now(SANTIAGO_TZ).strftime("%Y-%m-%d")
    # nombres lindos para las dos de detalle (compatibilidad con los backups viejos)
    ALIAS = {"snapshots_detalle": "binance", "snapshots_detalle_bybit": "bybit"}

    zbuf = io.BytesIO()
    resumen, fallos = [], []
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                for tabla in _tablas_de_la_base(cur):
                    nombre = ALIAS.get(tabla, tabla)
                    try:
                        contenido, n = _csv_de_tabla(cur, tabla, dias)
                        if contenido is None:
                            continue
                        z.writestr(f"{nombre}_{hoy}.csv", contenido)
                        ventana = f"ultimos {dias} dias" if tabla in EXPORT_VENTANA else "TABLA COMPLETA"
                        resumen.append(f"  {nombre}_{hoy}.csv    {n:>9,} filas   ({ventana})")
                    except Exception as e:
                        fallos.append(f"  {tabla}: {e}")
                        print(f"[export todo {tabla}]", e)
        leeme = [
            "BACKUP COMPLETO — P2P Monitor (Union Austral)",
            f"Generado: {datetime.now(SANTIAGO_TZ).strftime('%Y-%m-%d %H:%M')} (hora Chile)   Version: {VERSION}",
            "",
            "CONTENIDO:",
        ] + resumen
        if fallos:
            leeme += ["", "TABLAS QUE FALLARON:"] + fallos
        leeme += [
            "",
            "COMO LEER ESTO:",
            "  binance_*.csv / bybit_*.csv = detalle crudo del libro (top-80 cada 2 min).",
            "      Es lo pesado y lo que se purga solo; por eso viene recortado.",
            "  agregados_anunciante_dia    = resumen PERMANENTE por competidor y dia.",
            "  snapshots / snapshots_bybit = precio y spread del mercado, permanente.",
            "  mis_ordenes_reales          = tus ordenes reales (verdad de terreno).",
            "  fills_estimados             = operaciones inferidas del libro.",
            "  operativa_historial         = que decidio el semaforo, cada 5 min.",
            "",
            "RUTINA: exportar SIEMPRE antes de vaciar. El vaciado deja 24h;",
            "lo que no este en este ZIP a esa altura, se pierde.",
        ]
        z.writestr("LEEME.txt", "\n".join(leeme))
    zbuf.seek(0)
    return Response(zbuf.getvalue(), mimetype="application/zip",
                    headers={"Content-Disposition": f"attachment; filename=backup_p2p_{hoy}.zip"})


@app.route("/api/export/operativa")
def api_export_operativa():
    """Exporta operativa_historial crudo (ts + señales + decision del semaforo)
    para poder validar la presion/decision del monitor contra el precio real
    fuera del dashboard. Params: ?dias=N&fmt=csv|json&limit=N
    Ej: /api/export/operativa?dias=30&fmt=json"""
    try:
        dias = int(request.args.get("dias", 30))
    except (ValueError, TypeError):
        dias = 30
    fmt = (request.args.get("fmt", "csv") or "csv").lower()
    limit_arg = request.args.get("limit")

    sql = """
        SELECT ts, hora, decision, color, spread_neto, ratio, presion, min_op, gap
        FROM operativa_historial
        WHERE ts >= NOW() - (%s || ' days')::INTERVAL
        ORDER BY ts DESC
    """
    params = [dias]
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

    campos = ["ts", "hora", "decision", "color", "spread_neto", "ratio", "presion", "min_op", "gap"]
    if fmt == "json":
        out = []
        for r in rows:
            d = dict(r)
            d["ts"] = str(d["ts"])[:19]
            for k in ("spread_neto", "ratio", "presion", "min_op", "gap"):
                if d.get(k) is not None:
                    d[k] = float(d[k])
            out.append(d)
        return jsonify(out)

    import csv, io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(campos)
    for r in rows:
        w.writerow([
            str(r["ts"])[:19], r["hora"], r["decision"], r["color"],
            float(r["spread_neto"]) if r["spread_neto"] is not None else "",
            float(r["ratio"])       if r["ratio"]       is not None else "",
            float(r["presion"])     if r["presion"]     is not None else "",
            float(r["min_op"])      if r["min_op"]      is not None else "",
            float(r["gap"])         if r["gap"]          is not None else "",
        ])
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=operativa_{dias}d.csv"},
    )


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
    try:
        recalibrar_mi_ticket()  # ticket propio desde el CSV real: sobrevive redeploys
    except Exception as e:
        print(f"[MI_TICKET boot] {e}")
    try:
        # ticket de CADA anunciante desde el agregado: sin esto el tracker
        # arranca sin memoria y estima a todos con el generico (COL31)
        recalibrar_tickets_por_anunciante()
    except Exception as e:
        print(f"[TICKET x ANUN boot] {e}")
    try:
        recalibrar_bandas()     # flujo por banda de precio (modulo Ciclo)
    except Exception as e:
        print(f"[BANDAS boot] {e}")
    threading.Thread(target=ciclo_colector, daemon=True).start()
    threading.Thread(target=ciclo_colector_bybit, daemon=True).start()
    # macro va en su propio hilo: si la fuente externa falla, no arrastra a nadie
    threading.Thread(target=ciclo_colector_macro, daemon=True).start()
    threading.Thread(target=ciclo_libro_vivo, daemon=True).start()

if __name__ == "__main__":
    _boot()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
else:
    _boot()
