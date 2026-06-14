"""
P2P Monitor Binance — Vision Maker v2 + Detalle por Anunciante + UI v3
"""
import requests
import threading
import time
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, jsonify, Response, request
import psycopg2
from psycopg2 import pool as pg_pool
from psycopg2.extras import RealDictCursor
from contextlib import contextmanager

app = Flask(__name__)
SANTIAGO_TZ = ZoneInfo("America/Santiago")

config = {
    "MONEDA":               "USDT",
    "FIAT":                 "CLP",
    "INTERVALO_MIN":        2,
    "FILTRO_MIN_USDT":      200,
    "FILTRO_MIN_ORD":       100,
    "FILTRO_MIN_TASA":      90.0,
    "ALERTA_SPREAD":        0.8,
    "SPREAD_MINIMO":        0.2,
    # Comisión Binance P2P por operación (cada lado).
    # Usuario regular: ~0.35% por lado.
    # Merchant verificado LATAM: ~0.18% por lado → 0.36% total round-trip.
    "COMISION_BN":          0.0018,
    # Spread neto mínimo (después de comisiones) para considerar operable.
    "SPREAD_MIN_OPERATIVO": 0.35,
    "TOP_ANUNCIOS":         20,
}
config_lock = threading.Lock()

DATABASE_URL = os.environ.get("DATABASE_URL")
URL     = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
HEADERS = {"Content-Type": "application/json"}

ultimo_estado = {}
prev_detalle_raw = {}   # {tipo: {anunciante: (disponible, datetime)}}
data_lock = threading.Lock()

# ──────────────────────────────────────────────
#  CONNECTION POOL
# ──────────────────────────────────────────────
_pool = None

def init_pool():
    global _pool
    _pool = pg_pool.ThreadedConnectionPool(2, 10, DATABASE_URL)
    print("✅ Connection pool listo (2-10 conexiones)")

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
        conn.commit()
    print("\u2705 Base de datos lista (snapshots + snapshots_detalle)")

def reset_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS snapshots_detalle")
            cur.execute("DROP TABLE IF EXISTS snapshots")
        conn.commit()
    init_db()
    with data_lock:
        ultimo_estado.clear()
        prev_detalle_raw.clear()
    print("\u2705 Base de datos reseteada")

def guardar_snapshot(m):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO snapshots (
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

def guardar_detalle(timestamp, hora, anuncios_raw_compra, anuncios_raw_venta):
    """Guarda los top 20 anunciantes de cada lado SIN filtros de mínimos"""
    rows = []
    for pos, item in enumerate(anuncios_raw_compra[:20], 1):
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
    for pos, item in enumerate(anuncios_raw_venta[:20], 1):
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
    with config_lock:
        c = dict(config)
    payload = {
        "asset": c["MONEDA"], "fiat": c["FIAT"],
        "merchantCheck": False, "page": 1,
        "publisherType": None, "rows": c["TOP_ANUNCIOS"],
        "tradeType": tipo,
    }
    try:
        r = requests.post(URL, json=payload, headers=HEADERS, timeout=10)
        r.raise_for_status()
        return r.json().get("data", [])
    except Exception as e:
        print(f"[ERROR obtener_anuncios {tipo}] {e}")
        return []

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

def analizar(tab_compra, tab_venta):
    if not tab_compra or not tab_venta:
        return None
    with config_lock:
        c = dict(config)
    lider_tc    = min(tab_compra, key=lambda x: x["precio"])
    mas_caro_tc = max(tab_compra, key=lambda x: x["precio"])
    lider_tv    = max(tab_venta,  key=lambda x: x["precio"])
    menos_tv    = min(tab_venta,  key=lambda x: x["precio"])
    spread_abs = lider_tc["precio"] - lider_tv["precio"]
    spread_pct = round((spread_abs / lider_tv["precio"]) * 100, 4) if lider_tv["precio"] > 0 else 0
    pond_tc = round(precio_ponderado(tab_compra), 2)
    pond_tv = round(precio_ponderado(tab_venta),  2)
    spread_pond_abs = round(pond_tc - pond_tv, 2)
    spread_pond_pct = round((spread_pond_abs / pond_tv) * 100, 4) if pond_tv > 0 else 0
    liq_tc = sum(a["disponible"] for a in tab_compra)
    liq_tv = sum(a["disponible"] for a in tab_venta)
    precio_maker_vender  = round(lider_tc["precio"] - 0.01, 2)
    precio_maker_comprar = round(lider_tv["precio"] + 0.01, 2)
    comision_total_pct = c["COMISION_BN"] * 2 * 100
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
        "n_tab_compra":               len(tab_compra),
        "n_tab_venta":                len(tab_venta),
        "precio_maker_vender":        precio_maker_vender,
        "precio_maker_comprar":       precio_maker_comprar,
        "ganancia_neta_pct":          ganancia,
        "comision_total_pct":         round(comision_total_pct, 4),
        "spread_min_operativo":       c["SPREAD_MIN_OPERATIVO"],
        "brecha_ok":                  brecha_ok,
        "estado":                     estado,
        "color":                      color,
    }

def build_detalle_memory(raw_anuncios, tipo, now_dt):
    """Construye el array detalle desde raw para el frontend (no va a DB).
    Calcula velocidad de consumo USDT/min comparando con el ciclo anterior."""
    global prev_detalle_raw
    prev = prev_detalle_raw.get(tipo, {})
    rows = []
    nuevo_prev = {}
    for pos, item in enumerate(raw_anuncios[:20], 1):
        adv        = item.get("adv", {})
        trade      = item.get("advertiser", {})
        nombre     = trade.get("nickName", "")
        disp       = float(adv.get("tradableQuantity", 0))
        velocidad  = 0.0
        if nombre in prev:
            prev_disp, prev_dt = prev[nombre]
            delta_min = (now_dt - prev_dt).total_seconds() / 60
            if delta_min > 0 and prev_disp > disp:
                velocidad = round((prev_disp - disp) / delta_min, 1)
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

def ciclo_colector():
    print("[COLECTOR] Iniciando thread...")
    time.sleep(5)
    print("[COLECTOR] Primer ciclo comenzando")
    while True:
        try:
            print("[COLECTOR] Consultando Binance BUY...")
            raw_compra = obtener_anuncios("BUY")
            print(f"[COLECTOR] BUY raw: {len(raw_compra)} anuncios")
            print("[COLECTOR] Consultando Binance SELL...")
            raw_venta = obtener_anuncios("SELL")
            print(f"[COLECTOR] SELL raw: {len(raw_venta)} anuncios")

            tab_compra = parsear_y_filtrar(raw_compra, "BUY")
            tab_venta  = parsear_y_filtrar(raw_venta,  "SELL")

            estado = analizar(tab_compra, tab_venta)
            if estado:
                guardar_snapshot(estado)
                # Reutilizar el mismo timestamp que generó analizar() → consistencia DB
                ts     = estado["timestamp"]
                hora   = estado["hora"]
                now_dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=SANTIAGO_TZ)
                guardar_detalle(ts, hora, raw_compra, raw_venta)
                estado["detalle_compra"] = build_detalle_memory(raw_compra, "BUY",  now_dt)
                estado["detalle_venta"]  = build_detalle_memory(raw_venta,  "SELL", now_dt)
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
.tabbar { display: flex; gap: 4px; padding: 14px 0 18px; }
.tab {
  font-family: var(--font); font-size: 13px; font-weight: 500;
  color: var(--text-3); background: transparent;
  border: 1px solid transparent; border-radius: 9px;
  padding: 8px 15px; cursor: pointer; transition: all .15s;
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
.mc-price { font-size: clamp(26px, 3.4vw, 34px); font-weight: 500; color: var(--tone); margin: 8px 0 6px; }
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
.pl-brecha { font-size: 12px; color: var(--color-warn, #ffd740); background: rgba(255,215,64,0.1); border: 1px solid rgba(255,215,64,0.25); border-radius: 6px; padding: 2px 10px; margin-left: 4px; }
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
.intel-scroll { overflow-x:auto; }
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
}

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
    dc = dc.slice().sort((a, b) => a.precio - b.precio).map((r, i) => ({ ...r, posicion: i + 1 }));
    dv = dv.slice().sort((a, b) => b.precio - a.precio).map((r, i) => ({ ...r, posicion: i + 1 }));

    const pond = (rows) => { let v = 0, w = 0; rows.forEach((r) => { v += r.precio * r.disponible; w += r.disponible; }); return w ? v / w : 0; };
    const r2 = (n) => Math.round(n * 100) / 100;
    const pond_tc = r2(pond(dc)), pond_tv = r2(pond(dv));
    const lider_tc = dc[0], lider_tv = dv[0];
    const spread_abs = r2(lider_tc.precio - lider_tv.precio);
    const spread_pct = Math.round((spread_abs / lider_tv.precio) * 10000) / 100;
    const spread_pond_abs = r2(pond_tc - pond_tv);
    const spread_pond_pct = Math.round((spread_pond_abs / pond_tv) * 10000) / 100;
    const ganancia_neta_pct = r2(spread_pond_pct - COMISION_BN * 2 * 100);
    const liq_tc = dc.reduce((s, r) => s + r.disponible, 0);
    const liq_tv = dv.reduce((s, r) => s + r.disponible, 0);
    const cls = clasificar(spread_pond_pct);

    return {
      ...snap,
      precio_pond_tab_compra: pond_tc, precio_pond_tab_venta: pond_tv,
      mejor_vendedor_tab_compra: lider_tc.precio, peor_vendedor_tab_compra: dc[dc.length - 1].precio,
      mejor_comprador_tab_venta: lider_tv.precio, peor_comprador_tab_venta: dv[dv.length - 1].precio,
      lider_tab_compra: lider_tc.anunciante, lider_tab_venta: lider_tv.anunciante,
      spread_abs, spread_pct, spread_pond_abs, spread_pond_pct, ganancia_neta_pct,
      liq_tab_compra: liq_tc, liq_tab_venta: liq_tv,
      n_tab_compra: dc.length, n_tab_venta: dv.length,
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

  function createLiveEngine({ baseUrl = "", pollMs = 30000, intervaloMin = 5 } = {}) {
    const B = baseUrl.replace(/\/$/, "");
    let snap = null, history = [], heatmap = [], count = 0, vel = null;
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
        vel = calcVelocidad(history, intervaloMin) || vel;
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
        <LivePulse tone="buy" label="EN VIVO" />
        <div className="last-upd">
          <div className="lu-label">Última actualización</div>
          <div className="lu-time tnum">{snap.timestamp ? snap.timestamp.slice(11, 19) : "—"}</div>
        </div>
        <CountdownRing secondsLeft={secondsLeft} total={cycleMs / 1000} />
      </div>
    </header>
  );
}

/* ---------- Tab bar ---------- */
function Tabs({ tab, setTab }) {
  const items = [["tr", "Tiempo Real"], ["hist", "Histórico"], ["precio", "Precio"], ["intel", "Inteligencia"], ["heat", "Mapa de Calor"]];
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
        <span className="sc-role">{isBuy ? "Vendedores de USDT" : "Compradores de USDT"}</span>
      </div>
      <div className="sc-desc">{isBuy ? "El usuario viene acá a comprar" : "El usuario viene acá a vender"}</div>

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
        <div className="card-head"><h3>Precio ponderado · compra vs venta</h3><span className="card-sub">cómo se mueve el precio</span></div>
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
          color: "#35e07a", lineWidth: 2, title: "Compra",
          priceFormat: { type: "price", precision: 2, minMove: 0.01 },
        });
        serieVenta = chart.addLineSeries({
          color: "#ff5d6c", lineWidth: 2, title: "Venta",
          priceFormat: { type: "price", precision: 2, minMove: 0.01 },
        });
        serieCompra.setData(compra);
        serieVenta.setData(venta);
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
          } else {
            setBrecha(null);
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
    const desde = cual === "24h" ? ahora - 86400 : ahora - 7 * 86400;
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
              <span className="pl-item"><span className="pl-dot" style={{ background: "#35e07a" }} />Compra {meta.ultCompra ? "$" + meta.ultCompra.toFixed(2) : ""}</span>
              <span className="pl-item"><span className="pl-dot" style={{ background: "#ff5d6c" }} />Venta {meta.ultVenta ? "$" + meta.ultVenta.toFixed(2) : ""}</span>
              {brecha && (
                <span className="pl-brecha tnum">
                  Brecha <b>${brecha.abs}</b> · <b>{brecha.pct}%</b>
                </span>
              )}
            </div>
            <div className="precio-rangos">
              {[["24h", "24h"], ["7d", "7 días"], ["todo", "Todo"]].map(([k, lbl]) => (
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
function Inteligencia() {
  const B = (window.P2P_CONFIG && window.P2P_CONFIG.baseUrl) || "";
  const [horario, setHorario] = vS(null);
  const [anunciantes, setAnunciantes] = vS(null);
  const [traders, setTraders] = vS(null);
  const [fill, setFill] = vS(null);
  const [patron, setPatron] = vS(null);
  const [profundidad, setProfundidad] = vS(null);
  const [precioFill, setPrecioFill] = vS(null);
  const [loading, setLoading] = vS(true);
  const [seccion, setSeccion] = vS("horario");

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
    ]).then(([h,a,t,f,p,prof,pvf]) => {
      setHorario(h); setAnunciantes(a); setTraders(t); setFill(f); setPatron(p);
      setProfundidad(Array.isArray(prof) ? prof : (prof.datos || []));
      setPrecioFill(Array.isArray(pvf) ? pvf : (pvf.datos || []));
      setLoading(false);
    }).catch(()=>setLoading(false));
  }, []);

  const fN = (v) => v != null ? Number(v).toLocaleString("es-CL") : "—";
  const fC = (v) => v != null ? "$"+parseFloat(v).toFixed(2) : "—";

  const SECS = [
    ["horario","⏰ Horario"],["anunciantes","👥 Pares"],
    ["traders","🏆 Top traders"],["fill","⚡ Fill"],["patron","📅 Patrones"],
    ["profundidad","📊 Profundidad"],["preciofill","💡 Precio vs Fill"]
  ];

  if (loading) return <div className="intel-loading">Consultando base de datos…</div>;

  return (
    <div className="view">
      <div className="intel-tabs">
        {SECS.map(([k,lbl])=>(
          <button key={k} className={"intel-tab"+(seccion===k?" active":"")} onClick={()=>setSeccion(k)}>{lbl}</button>
        ))}
      </div>

      {seccion==="horario" && horario && (
        <section className="chart-card">
          <div className="card-head"><h3>Ventanas operativas por hora</h3><span className="card-sub">últimos 7 días · spread neto merchant verificado (−0.36%)</span></div>
          <div className="intel-scroll">
            <table className="intel-table">
              <thead><tr>
                <th title="Hora del día en horario Santiago (Chile)">Hora</th>
                <th title="Ganancia neta estimada por vuelta: diferencia entre precio compra y venta, descontando la comisión merchant verificado de 0.36% (0.18% × 2 lados). Ej: +1.2% significa que por cada 1.000 USDT ganás $12.">Spread neto</th>
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
          <div className="card-head"><h3>¿Cuánto te compran según dónde estés en el libro?</h3><span className="card-sub">USDT por orden recibida · posiciones 1-3 vs 4-6 vs 7+ · últimos 7 días</span></div>
          <div className="intel-scroll">
            <table className="intel-table">
              <thead><tr>
                <th title="Hora del día en horario Santiago">Hora</th>
                <th title="Si publicás tu anuncio en las posiciones 1, 2 o 3 del libro (los primeros que ve el usuario), cuántos USDT te compran por orden recibida en promedio. Mayor número = mejor." style={{color:"#35e07a"}}>📍 Posición 1-3 (primero)</th>
                <th title="Si estás en posiciones 4, 5 o 6, cuántos USDT te compran por orden. A veces similar al top, a veces menos.">📍 Posición 4-6 (medio)</th>
                <th title="Si estás en posiciones 7 o más atrás, cuántos USDT te compran por orden. En horas de baja liquidez suele ser muy poco." style={{color:"var(--text-3)"}}>📍 Posición 7+ (atrás)</th>
              </tr></thead>
              <tbody>{Array.from({length:24},(_,h)=>{
                const get=(rp)=>{const r=fill.find(f=>parseInt(f.hora)===h&&f.rango_pos===rp); return r&&r.consumo_med?`${fN(r.consumo_med)} U`:"–";};
                const top=fill.find(f=>parseInt(f.hora)===h&&f.rango_pos==="top1-3");
                const topVal=top&&top.consumo_med?parseFloat(top.consumo_med):0;
                const rowColor=topVal>=1500?"rgba(53,224,122,0.05)":topVal>=800?"rgba(255,215,64,0.04)":"transparent";
                return <tr key={h} style={{background:rowColor}}>
                  <td className="tnum"><b>{String(h).padStart(2,"0")}h</b></td>
                  <td className="tnum" style={{color:"#35e07a",fontWeight:topVal>=1500?700:400}}>{get("top1-3")}</td>
                  <td className="tnum">{get("mid4-6")}</td>
                  <td className="tnum" style={{color:"var(--text-3)"}}>{get("back7+")}</td>
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
            <b>P. Compra vs P. Venta:</b> la diferencia entre ambos es la brecha que el mercado ofrece. Si P. Compra = $922 y P. Venta = $916, el spread bruto es ~0.65% — de ahí se descuenta tu comisión (0.36% merchant verificado) y te queda tu ganancia neta.<br/>
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
                <th title="Posición en el libro de órdenes: top1-3 = los primeros que ve el usuario, mid4-6 = zona media, back7+ = posiciones alejadas del top.">Posición</th>
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
            <b>Estrategia:</b> si el ratio de top1-3 es 60% pero el de back7+ es 5%, estar en posición 7+ es básicamente invisible. Calcula el costo de bajar tu precio para entrar al top.
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
window.P2PViews = { TiempoReal, Historico, Heatmap, PrecioChart, Inteligencia };

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
      <main className="content">
        {tab === "tr" && <V.TiempoReal snap={viewSnap} history={history} showOrderBook={t.orderBook} vel={vel}
          filters={{ cfg: filters, onApply: applyFilters, info: viewSnap._filtro }} />}
        {tab === "hist" && <V.Historico history={history} />}
        {tab === "precio" && <V.PrecioChart />}
        {tab === "intel" && <V.Inteligencia />}
        {tab === "heat" && <V.Heatmap heatmap={heatmap} />}
      </main>
      <footer className="foot">
        <span className="foot-snap tnum">{window.P2P.fmtNum(count)}</span> snapshots guardados
        <span className="foot-sep">·</span>
        <span>Próximo ciclo en <b className="tnum">{Math.ceil(secondsLeft)}s</b></span>
        <span className="foot-sep">·</span>
        <span className="foot-demo">Unión Austral Capital · USDT/CLP · Binance P2P</span>
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
    return Response(DASHBOARD, mimetype='text/html')

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
            # Si el timestamp ya tiene tzinfo, usarlo directo; si no, asumir Santiago
            if hasattr(ts, 'tzinfo') and ts.tzinfo is not None:
                dt_aware = ts
            else:
                ts_str = str(ts)[:19].replace(" ", "T")
                dt_naive = _dt.fromisoformat(ts_str)
                dt_aware = dt_naive.replace(tzinfo=SANTIAGO_TZ)
            unix = int(dt_aware.astimezone(_tz.utc).timestamp())
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
                    CASE WHEN posicion <= 3 THEN 'top1-3'
                         WHEN posicion <= 6 THEN 'mid4-6'
                         ELSE 'back7+' END AS rango_pos,
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
                            ORDER BY CASE WHEN d.tipo = 'BUY' THEN -d.precio
                                          ELSE d.precio END
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
        data = request.get_json() or {}
        type_map = {
            "FILTRO_MIN_USDT":      float,
            "FILTRO_MIN_ORD":       int,
            "FILTRO_MIN_TASA":      float,
            "INTERVALO_MIN":        int,
            "COMISION_BN":          float,
            "SPREAD_MIN_OPERATIVO": float,
            "ALERTA_SPREAD":        float,
            "SPREAD_MINIMO":        float,
        }
        errores = {}
        with config_lock:
            for k, cast in type_map.items():
                if k in data:
                    try:
                        config[k] = cast(data[k])
                    except (ValueError, TypeError):
                        errores[k] = "valor invalido"
        if errores:
            return jsonify({"ok": False, "errores": errores}), 400
        return jsonify({"ok": True})
    with config_lock:
        return jsonify(dict(config))


@app.route("/api/reset", methods=["POST"])
def api_reset():
    try:
        reset_db()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
#  INICIO
# ──────────────────────────────────────────────
if __name__ == "__main__":
    init_pool()
    init_db()
    threading.Thread(target=ciclo_colector, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
else:
    init_pool()
    init_db()
    threading.Thread(target=ciclo_colector, daemon=True).start()
