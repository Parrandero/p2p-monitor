"""
P2P Monitor Binance — Vision Maker v2
"""

import requests
import threading
import time
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, jsonify, render_template_string, request
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)
SANTIAGO_TZ = ZoneInfo("America/Santiago")

# ──────────────────────────────────────────────
#  CONFIG (modificable desde la UI)
# ──────────────────────────────────────────────
config = {
    "MONEDA":           "USDT",
    "FIAT":             "CLP",
    "INTERVALO_MIN":    5,
    "FILTRO_MIN_USDT":  200,
    "FILTRO_MIN_ORD":   150,
    "FILTRO_MIN_TASA":  95.0,
    "FILTRO_MAX_RESP":  30,
    "FILTRO_MAX_MIN_CLP": 500000,
    "ALERTA_SPREAD":    0.8,
    "SPREAD_MINIMO":    0.2,
    "COMISION_BN":      0.002,
    "TOP_ANUNCIOS":     20,
}
config_lock = threading.Lock()

DATABASE_URL = os.environ.get("DATABASE_URL")
URL     = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
HEADERS = {"Content-Type": "application/json"}

ultimo_estado = {}
data_lock = threading.Lock()

# ──────────────────────────────────────────────
#  BASE DE DATOS
# ──────────────────────────────────────────────
def get_conn():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                DO $$ BEGIN
                  IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='snapshots'
                    AND column_name NOT IN (
                      'id','timestamp','hora','dia',
                      'mejor_vendedor_tab_compra','peor_vendedor_tab_compra',
                      'precio_pond_tab_compra','lider_tab_compra',
                      'mejor_comprador_tab_venta','peor_comprador_tab_venta',
                      'precio_pond_tab_venta','lider_tab_venta',
                      'spread_abs','spread_pct',
                      'spread_pond_abs','spread_pond_pct',
                      'liq_tab_compra','liq_tab_venta',
                      'n_tab_compra','n_tab_venta',
                      'precio_maker_vender','precio_maker_comprar',
                      'ganancia_neta_pct','estado','color'
                    )
                  ) THEN DROP TABLE snapshots;
                  END IF;
                END $$;
            """)
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
        conn.commit()
    print("✅ Base de datos lista")

def reset_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS snapshots")
        conn.commit()
    init_db()
    with data_lock:
        ultimo_estado.clear()
    print("✅ Base de datos reseteada")

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

def obtener_historial(limit=200):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM snapshots ORDER BY timestamp DESC LIMIT %s", (limit,))
            rows = cur.fetchall()
    return [dict(r) for r in reversed(rows)]

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

# ──────────────────────────────────────────────
#  COLECTOR
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
    except:
        return []

def parsear_y_filtrar(anuncios, tipo):
    with config_lock:
        c = dict(config)
    resultado = []
    for item in anuncios:
        adv   = item.get("adv", {})
        trade = item.get("advertiser", {})
        disponible  = float(adv.get("tradableQuantity", 0))
        completadas = int(trade.get("tradeCount", 0))
        tasa_exito  = float(trade.get("monthFinishRate", 0)) * 100
        resp_time   = int(adv.get("avgAveReleastTime", 0)) // 60
        min_clp     = float(adv.get("minSingleTransAmount", 0))
        if disponible  < c["FILTRO_MIN_USDT"]:  continue
        if completadas < c["FILTRO_MIN_ORD"]:   continue
        if tasa_exito  < c["FILTRO_MIN_TASA"]:  continue
        if c["FILTRO_MAX_RESP"] < 60 and resp_time > c["FILTRO_MAX_RESP"]: continue
        if c["FILTRO_MAX_MIN_CLP"] > 0 and min_clp > c["FILTRO_MAX_MIN_CLP"]: continue
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
    ganancia = round(spread_pond_pct - (c["COMISION_BN"] * 2 * 100), 4)

    if spread_pond_pct >= c["ALERTA_SPREAD"]:
        estado, color = "MUY APTO", "green"
    elif spread_pond_pct >= c["SPREAD_MINIMO"]:
        estado, color = "APTO", "yellow"
    elif spread_pond_pct >= 0:
        estado, color = "ESTRECHO", "orange"
    else:
        estado, color = "NO APTO", "red"

    return {
        "timestamp":                  datetime.now(SANTIAGO_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "hora":                       datetime.now(SANTIAGO_TZ).hour,
        "dia":                        datetime.now(SANTIAGO_TZ).strftime("%A"),
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
        "estado":                     estado,
        "color":                      color,
    }

def ciclo_colector():
    print("[COLECTOR] Iniciando thread...")
    time.sleep(5)
    print("[COLECTOR] Primer ciclo comenzando")
    while True:
        try:
            print("[COLECTOR] Consultando Binance BUY...")
            raw_compra = obtener_anuncios("BUY")
            print(f"[COLECTOR] BUY raw: {len(raw_compra)} anuncios")
            tab_compra = parsear_y_filtrar(raw_compra, "BUY")
            print(f"[COLECTOR] BUY filtrado: {len(tab_compra)} anuncios")

            print("[COLECTOR] Consultando Binance SELL...")
            raw_venta = obtener_anuncios("SELL")
            print(f"[COLECTOR] SELL raw: {len(raw_venta)} anuncios")
            tab_venta = parsear_y_filtrar(raw_venta, "SELL")
            print(f"[COLECTOR] SELL filtrado: {len(tab_venta)} anuncios")

            estado = analizar(tab_compra, tab_venta)
            if estado:
                guardar_snapshot(estado)
                with data_lock:
                    ultimo_estado.update(estado)
                print(f"[{estado['timestamp']}] Spread pond: {estado['spread_pond_pct']}% — {estado['estado']}")
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
DASHBOARD = r"""
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>P2P Monitor — Unión Austral</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0;}
body{background:#0d0d1a;color:#fff;font-family:'Segoe UI',sans-serif;min-height:100vh;}
header{background:#12122a;padding:14px 20px;border-bottom:1px solid #1e1e3f;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;}
header h1{color:#00d4ff;font-size:0.9rem;}
header span{color:#888;font-size:0.7rem;}
.tabs{display:flex;gap:6px;padding:14px 20px 0;}
.tab{padding:8px 14px;border-radius:8px 8px 0 0;cursor:pointer;font-size:0.8rem;background:#12122a;border:1px solid #1e1e3f;border-bottom:none;color:#888;}
.tab.active{background:#1a1a3e;color:#00d4ff;border-color:#00d4ff;}
.tab-content{display:none;}.tab-content.active{display:block;}
.estado-banner{margin:14px 20px 12px;padding:12px 20px;border-radius:10px;font-size:0.9rem;font-weight:bold;text-align:center;}
.banner-green{background:#00e67222;border:1px solid #00e676;color:#00e676;}
.banner-yellow{background:#ffd74022;border:1px solid #ffd740;color:#ffd740;}
.banner-orange{background:#ff910022;border:1px solid #ff9100;color:#ff9100;}
.banner-red{background:#ff525222;border:1px solid #ff5252;color:#ff5252;}
.panel-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;padding:0 20px 12px;}
.panel-tc{background:#0a1a0a;border:1.5px solid #00e676;border-radius:12px;padding:16px;}
.panel-tv{background:#1a0a0a;border:1.5px solid #ff5252;border-radius:12px;padding:16px;}
.tab-badge{font-size:0.62rem;padding:2px 8px;border-radius:4px;font-weight:bold;}
.badge-tc{background:#00e67222;color:#00e676;border:1px solid #00e676;}
.badge-tv{background:#ff525222;color:#ff5252;border:1px solid #ff5252;}
.panel-header{display:flex;align-items:center;gap:8px;margin-bottom:10px;}
.panel-desc{font-size:0.68rem;color:#999;margin-bottom:10px;}
.precio-pond-label{font-size:0.62rem;color:#888;margin-bottom:2px;}
.precio-pond{font-size:1.8rem;font-weight:bold;}
.tc .precio-pond{color:#00e676;}.tv .precio-pond{color:#ff5252;}
.precio-puntual-label{font-size:0.62rem;color:#888;margin-top:8px;margin-bottom:2px;}
.precio-puntual{font-size:1.1rem;font-weight:bold;}
.tc .precio-puntual{color:#4dff8a;}.tv .precio-puntual{color:#ff8a8a;}
.panel-lider{font-size:0.72rem;color:#bbb;}
.panel-sep{border:none;border-top:1px solid #ffffff11;margin:10px 0;}
.panel-stats{font-size:0.68rem;color:#777;}
.brecha-bar{margin:0 20px 12px;background:#12122a;border:1px solid #1e1e3f;border-radius:10px;padding:14px 18px;display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;}
.bc-item .bc-label{font-size:0.62rem;color:#888;margin-bottom:4px;}
.bc-item .bc-val{font-size:1.1rem;font-weight:bold;}
.maker-title{font-size:0.68rem;color:#888;text-align:center;padding:2px 0 10px;margin:0 20px;}
.maker-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;padding:0 20px 16px;}
.maker-card{border-radius:10px;padding:14px;}
.maker-tc{background:#0a1a0a;border:1px solid #00e676;}
.maker-tv{background:#1a0a0a;border:1px solid #ff5252;}
.maker-card .mtipo{font-size:0.62rem;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px;line-height:1.4;}
.maker-tc .mtipo{color:#00e676;}.maker-tv .mtipo{color:#ff5252;}
.maker-card .mprecio{font-size:1.4rem;font-weight:bold;color:#ffd740;}
.maker-card .mnota{font-size:0.62rem;color:#888;margin-top:6px;line-height:1.4;}
.filtros-panel{margin:0 20px 16px;background:#12122a;border:1px solid #1e1e3f;border-radius:12px;overflow:hidden;}
.filtros-header{padding:12px 16px;border-bottom:1px solid #1e1e3f;display:flex;align-items:center;gap:10px;cursor:pointer;user-select:none;}
.filtros-header span{font-size:0.82rem;font-weight:bold;color:#00d4ff;}
.filtros-header .toggle-icon{margin-left:auto;font-size:0.8rem;color:#888;}
.filtros-body{padding:16px;display:none;}
.filtros-body.open{display:block;}
.filtros-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;}
.f-item label{display:block;font-size:0.68rem;color:#888;margin-bottom:5px;}
.f-item input,.f-item select{width:100%;background:#0d0d1a;border:1px solid #333355;color:#fff;padding:7px 10px;border-radius:6px;font-size:0.82rem;}
.f-item input:focus,.f-item select:focus{outline:none;border-color:#00d4ff;}
.f-item select option{background:#12122a;}
.btn-aplicar{width:100%;margin-top:12px;padding:9px;background:#00d4ff22;border:1px solid #00d4ff;color:#00d4ff;border-radius:8px;cursor:pointer;font-size:0.82rem;font-weight:bold;}
.btn-aplicar:hover{background:#00d4ff44;}
.btn-reset{width:100%;margin-top:6px;padding:8px;background:#ff525211;border:1px solid #ff5252;color:#ff5252;border-radius:8px;cursor:pointer;font-size:0.78rem;}
.btn-reset:hover{background:#ff525222;}
.seccion{padding:0 20px 16px;}
.seccion h2{color:#00d4ff;font-size:0.75rem;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px;padding-top:14px;}
.chart-wrap{background:#12122a;border:1px solid #1e1e3f;border-radius:10px;padding:14px;}
.stat-row{display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px solid #1e1e3f;font-size:0.8rem;}
.stat-row:last-child{border-bottom:none;}
.stat-label{color:#888;}.stat-value{font-weight:bold;}
.refresh{color:#888;font-size:0.68rem;text-align:center;padding:10px;}
#countdown{color:#00d4ff;}
.sin-datos{text-align:center;padding:60px;color:#888;}
.toast{position:fixed;bottom:20px;right:20px;background:#00e67233;border:1px solid #00e676;color:#00e676;padding:10px 18px;border-radius:8px;font-size:0.82rem;display:none;z-index:999;}
.green{color:#00e676;}.yellow{color:#ffd740;}.orange{color:#ff9100;}.red{color:#ff5252;}.cyan{color:#00d4ff;}
</style>
</head>
<body>
<header>
  <h1>P2P Monitor — Unión Austral Capital</h1>
  <span id="ultima-act">Cargando...</span>
</header>
<div class="tabs">
  <div class="tab active" onclick="showTab('tr',this)">Tiempo Real</div>
  <div class="tab" onclick="showTab('hist',this)">Histórico</div>
  <div class="tab" onclick="showTab('heat',this)">Mapa de Calor</div>
</div>

<div id="tr" class="tab-content active">
  <div id="contenido-tr"><div class="sin-datos">Esperando primer ciclo de datos (5 min)...</div></div>
  <div class="filtros-panel">
    <div class="filtros-header" onclick="toggleFiltros()">
      <span>Filtros del mercado</span>
      <span style="font-size:0.68rem;color:#888;">se aplican en el próximo ciclo</span>
      <span class="toggle-icon" id="toggle-icon">▼</span>
    </div>
    <div class="filtros-body" id="filtros-body">
      <div class="filtros-grid">
        <div class="f-item"><label>Mín. USDT disponible</label><input type="number" id="f-usdt" value="200"></div>
        <div class="f-item"><label>Mín. órdenes completadas</label><input type="number" id="f-ord" value="150"></div>
        <div class="f-item"><label>Tasa de éxito mínima (%)</label><input type="number" id="f-tasa" value="95" step="0.1"></div>
        <div class="f-item"><label>Tiempo respuesta máx.</label>
          <select id="f-resp">
            <option value="15">15 min</option>
            <option value="30" selected>30 min</option>
            <option value="60">60 min</option>
            <option value="999">Sin filtro</option>
          </select>
        </div>
        <div class="f-item"><label>Mín. CLP por transacción</label>
          <select id="f-clp">
            <option value="0">Sin filtro</option>
            <option value="200000">200,000 CLP</option>
            <option value="500000" selected>500,000 CLP</option>
            <option value="1000000">1,000,000 CLP</option>
          </select>
        </div>
        <div class="f-item"><label>Intervalo de consulta</label>
          <select id="f-int">
            <option value="1">1 minuto</option>
            <option value="2">2 minutos</option>
            <option value="5" selected>5 minutos</option>
            <option value="10">10 minutos</option>
          </select>
        </div>
      </div>
      <button class="btn-aplicar" onclick="aplicarFiltros()">Aplicar filtros</button>
      <button class="btn-reset" onclick="resetearDatos()">Resetear todos los datos historicos</button>
    </div>
  </div>
</div>

<div id="hist" class="tab-content">
  <div class="seccion"><h2>Spread ponderado histórico</h2><div class="chart-wrap"><canvas id="chartSpread" height="100"></canvas></div></div>
  <div class="seccion"><h2>Liquidez histórica</h2><div class="chart-wrap"><canvas id="chartLiq" height="100"></canvas></div></div>
</div>

<div id="heat" class="tab-content">
  <div class="seccion"><h2>Spread ponderado promedio por hora y día</h2><div class="chart-wrap"><canvas id="chartHeat" height="180"></canvas></div></div>
  <div class="seccion"><div id="stats-resumen" class="chart-wrap"></div></div>
</div>

<div class="refresh">Actualización en <span id="countdown">30</span>s</div>
<div class="toast" id="toast"></div>

<script>
function showTab(id,el){
  document.querySelectorAll('.tab-content').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.getElementById(id).classList.add('active');el.classList.add('active');
}
function toggleFiltros(){
  const b=document.getElementById('filtros-body');
  const i=document.getElementById('toggle-icon');
  b.classList.toggle('open');
  i.textContent=b.classList.contains('open')?'▲':'▼';
}
function showToast(msg,color='#00e676'){
  const t=document.getElementById('toast');
  t.textContent=msg;t.style.color=color;t.style.borderColor=color;
  t.style.background=color+'22';t.style.display='block';
  setTimeout(()=>t.style.display='none',3000);
}
async function aplicarFiltros(){
  const body={
    FILTRO_MIN_USDT: parseInt(document.getElementById('f-usdt').value),
    FILTRO_MIN_ORD:  parseInt(document.getElementById('f-ord').value),
    FILTRO_MIN_TASA: parseFloat(document.getElementById('f-tasa').value),
    FILTRO_MAX_RESP: parseInt(document.getElementById('f-resp').value),
    FILTRO_MAX_MIN_CLP: parseInt(document.getElementById('f-clp').value),
    INTERVALO_MIN:   parseInt(document.getElementById('f-int').value),
  };
  const r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(r.ok) showToast('Filtros aplicados');
  else showToast('Error al aplicar filtros','#ff5252');
}
async function resetearDatos(){
  if(!confirm('Esto borrará todos los datos históricos. ¿Continuar?')) return;
  const r=await fetch('/api/reset',{method:'POST'});
  if(r.ok){ showToast('Datos reseteados'); setTimeout(cargarDatos,500); }
  else showToast('Error al resetear','#ff5252');
}

let cuenta=30;
setInterval(()=>{cuenta--;document.getElementById('countdown').textContent=cuenta;if(cuenta<=0){cuenta=30;cargarDatos();}},1000);
let chartSpread,chartLiq,chartHeat;
function fmt(n,dec=2){return parseFloat(n).toLocaleString('es-CL',{minimumFractionDigits:dec,maximumFractionDigits:dec});}

async function cargarDatos(){
  try{
    const [estado,hist,heat,cnt]=await Promise.all([
      fetch('/api/estado').then(r=>r.json()),
      fetch('/api/historial').then(r=>r.json()),
      fetch('/api/heatmap').then(r=>r.json()),
      fetch('/api/count').then(r=>r.json()),
    ]);
    if(estado.timestamp){renderTR(estado,cnt.count);document.getElementById('ultima-act').textContent='Última act: '+estado.timestamp;}
    if(hist.length>1) renderHistorico(hist);
    if(heat.length>0){renderHeatmap(heat);renderStats(hist);}
    sincronizarFiltros();
  }catch(e){console.error(e);}
}

async function sincronizarFiltros(){
  try{
    const cfg=await fetch('/api/config').then(r=>r.json());
    document.getElementById('f-usdt').value=cfg.FILTRO_MIN_USDT;
    document.getElementById('f-ord').value=cfg.FILTRO_MIN_ORD;
    document.getElementById('f-tasa').value=cfg.FILTRO_MIN_TASA;
    document.getElementById('f-resp').value=cfg.FILTRO_MAX_RESP;
    document.getElementById('f-clp').value=cfg.FILTRO_MAX_MIN_CLP;
    document.getElementById('f-int').value=cfg.INTERVALO_MIN;
  }catch(e){}
}

function renderTR(e,count){
  const sp=parseFloat(e.spread_pct).toFixed(2);
  const spp=parseFloat(e.spread_pond_pct).toFixed(2);
  const gn=parseFloat(e.ganancia_neta_pct).toFixed(2);
  const gnColor=parseFloat(e.ganancia_neta_pct)>0?'green':'red';
  const estadoIcon={'MUY APTO':'🔥','APTO':'✅','ESTRECHO':'⚠️','NO APTO':'❌'}[e.estado]||'';
  document.getElementById('contenido-tr').innerHTML=`
    <div class="estado-banner banner-${e.color}">${estadoIcon} ${e.estado} — Spread ponderado ${spp}% · Spread puntual ${sp}%</div>
    <div class="panel-grid">
      <div class="panel-tc tc">
        <div class="panel-header"><span class="tab-badge badge-tc">TAB COMPRA</span></div>
        <div class="panel-desc">Vendedores de USDT · el usuario va aquí a comprar</div>
        <div class="precio-pond-label">Precio ponderado</div>
        <div class="precio-pond">$${fmt(e.precio_pond_tab_compra)}</div>
        <div class="precio-puntual-label">Líder puntual (más barato)</div>
        <div class="precio-puntual">$${fmt(e.mejor_vendedor_tab_compra)}</div>
        <div class="panel-lider">👤 ${e.lider_tab_compra||'—'}</div>
        <hr class="panel-sep">
        <div class="panel-stats">${fmt(e.liq_tab_compra,0)} USDT · ${e.n_tab_compra} anuncios · rango $${fmt(e.mejor_vendedor_tab_compra)} – $${fmt(e.peor_vendedor_tab_compra)}</div>
      </div>
      <div class="panel-tv tv">
        <div class="panel-header"><span class="tab-badge badge-tv">TAB VENTA</span></div>
        <div class="panel-desc">Compradores de USDT · el usuario va aquí a vender</div>
        <div class="precio-pond-label">Precio ponderado</div>
        <div class="precio-pond">$${fmt(e.precio_pond_tab_venta)}</div>
        <div class="precio-puntual-label">Líder puntual (más paga)</div>
        <div class="precio-puntual">$${fmt(e.mejor_comprador_tab_venta)}</div>
        <div class="panel-lider">👤 ${e.lider_tab_venta||'—'}</div>
        <hr class="panel-sep">
        <div class="panel-stats">${fmt(e.liq_tab_venta,0)} USDT · ${e.n_tab_venta} anuncios · rango $${fmt(e.peor_comprador_tab_venta)} – $${fmt(e.mejor_comprador_tab_venta)}</div>
      </div>
    </div>
    <div class="brecha-bar">
      <div class="bc-item">
        <div class="bc-label">Brecha ponderada</div>
        <div class="bc-val yellow">$${fmt(e.spread_pond_abs)} · ${spp}%</div>
      </div>
      <div class="bc-item">
        <div class="bc-label">Ganancia neta est.</div>
        <div class="bc-val ${gnColor}">${gn}%</div>
      </div>
      <div class="bc-item">
        <div class="bc-label">Snapshots guardados</div>
        <div class="bc-val cyan">${count}</div>
      </div>
    </div>
    <div class="maker-title">— Como Maker pondrías tu anuncio un centavo mejor que el líder —</div>
    <div class="maker-grid">
      <div class="maker-card maker-tc">
        <div class="mtipo">Si quiero VENDER USDT (posteo en Tab Compra)</div>
        <div class="mprecio">$${fmt(e.precio_maker_vender)}</div>
        <div class="mnota">Un centavo menos que <b>${e.lider_tab_compra||'el líder'}</b> que pide $${fmt(e.mejor_vendedor_tab_compra)}<br>→ aparecer primero en Tab Compra</div>
      </div>
      <div class="maker-card maker-tv">
        <div class="mtipo">Si quiero COMPRAR USDT (posteo en Tab Venta)</div>
        <div class="mprecio">$${fmt(e.precio_maker_comprar)}</div>
        <div class="mnota">Un centavo más que <b>${e.lider_tab_venta||'el líder'}</b> que paga $${fmt(e.mejor_comprador_tab_venta)}<br>→ aparecer primero en Tab Venta</div>
      </div>
    </div>
  `;
}

function renderHistorico(hist){
  const labels=hist.map(h=>h.timestamp.toString().slice(11,16));
  const spp=hist.map(h=>parseFloat(h.spread_pond_pct||0));
  const sp=hist.map(h=>parseFloat(h.spread_pct||0));
  const liqC=hist.map(h=>parseFloat(h.liq_tab_compra||0));
  const liqV=hist.map(h=>parseFloat(h.liq_tab_venta||0));
  const sc={x:{ticks:{color:'#888',maxTicksLimit:10},grid:{color:'#1e1e3f'}},y:{ticks:{color:'#888'},grid:{color:'#1e1e3f'}}};
  if(chartSpread) chartSpread.destroy();
  if(chartLiq) chartLiq.destroy();
  chartSpread=new Chart(document.getElementById('chartSpread').getContext('2d'),{
    type:'line',data:{labels,datasets:[
      {label:'Spread ponderado %',data:spp,borderColor:'#ffd740',backgroundColor:'rgba(255,215,64,0.08)',borderWidth:2,pointRadius:2,tension:0.3,fill:true},
      {label:'Spread puntual %',data:sp,borderColor:'#00d4ff',backgroundColor:'rgba(0,212,255,0.04)',borderWidth:1,pointRadius:1,tension:0.3,fill:false,borderDash:[4,3]},
    ]},options:{responsive:true,plugins:{legend:{labels:{color:'#aaa'}}},scales:sc}
  });
  chartLiq=new Chart(document.getElementById('chartLiq').getContext('2d'),{
    type:'line',data:{labels,datasets:[
      {label:'Liquidez Tab Compra',data:liqC,borderColor:'#00e676',backgroundColor:'rgba(0,230,118,0.06)',borderWidth:2,pointRadius:1,tension:0.3,fill:true},
      {label:'Liquidez Tab Venta',data:liqV,borderColor:'#ff5252',backgroundColor:'rgba(255,82,82,0.06)',borderWidth:2,pointRadius:1,tension:0.3,fill:true},
    ]},options:{responsive:true,plugins:{legend:{labels:{color:'#aaa'}}},scales:sc}
  });
}

function renderHeatmap(heat){
  const dias=['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'];
  const diasEs=['Lunes','Martes','Miércoles','Jueves','Viernes','Sábado','Domingo'];
  const horas=Array.from({length:24},(_,i)=>i);
  const datasets=dias.map((dia,di)=>({
    label:diasEs[di],
    data:horas.map(h=>{const m=heat.find(r=>r.dia===dia&&parseInt(r.hora)===h);return m?parseFloat(m.avg_spread):null;}),
    backgroundColor:`hsla(${di*50},70%,55%,0.75)`,borderColor:`hsla(${di*50},70%,55%,1)`,borderWidth:1
  }));
  if(chartHeat) chartHeat.destroy();
  chartHeat=new Chart(document.getElementById('chartHeat').getContext('2d'),{
    type:'bar',data:{labels:horas.map(h=>h+':00'),datasets},
    options:{responsive:true,plugins:{legend:{labels:{color:'#aaa',boxWidth:12}}},
      scales:{x:{ticks:{color:'#888'},grid:{color:'#1e1e3f'}},y:{ticks:{color:'#888',callback:v=>v+'%'},grid:{color:'#1e1e3f'}}}}
  });
}

function renderStats(hist){
  if(!hist.length) return;
  const spp=hist.map(h=>parseFloat(h.spread_pond_pct||0));
  const avg=(spp.reduce((a,b)=>a+b,0)/spp.length).toFixed(2);
  const max=Math.max(...spp).toFixed(2);
  const min=Math.min(...spp).toFixed(2);
  const hc={};
  hist.forEach(h=>{(hc[h.hora]=hc[h.hora]||[]).push(parseFloat(h.spread_pond_pct||0));});
  const mh=Object.entries(hc).map(([h,v])=>[h,v.reduce((a,b)=>a+b,0)/v.length]).sort((a,b)=>b[1]-a[1])[0];
  document.getElementById('stats-resumen').innerHTML=`
    <div style="color:#00d4ff;font-size:0.75rem;text-transform:uppercase;letter-spacing:1px;margin-bottom:12px">Estadísticas del período (spread ponderado)</div>
    <div class="stat-row"><span class="stat-label">Total snapshots</span><span class="stat-value cyan">${hist.length}</span></div>
    <div class="stat-row"><span class="stat-label">Spread ponderado promedio</span><span class="stat-value yellow">${avg}%</span></div>
    <div class="stat-row"><span class="stat-label">Spread ponderado máximo</span><span class="stat-value green">${max}%</span></div>
    <div class="stat-row"><span class="stat-label">Spread ponderado mínimo</span><span class="stat-value red">${min}%</span></div>
    ${mh?`<div class="stat-row"><span class="stat-label">Mejor hora promedio</span><span class="stat-value green">${mh[0]}:00 hs (${parseFloat(mh[1]).toFixed(2)}%)</span></div>`:''}
  `;
}
cargarDatos();
</script>
</body>
</html>
"""

# ──────────────────────────────────────────────
#  RUTAS
# ──────────────────────────────────────────────
@app.route("/")
def index():
    return render_template_string(DASHBOARD)

def clean(data):
    for k,v in data.items():
        if hasattr(v,'__float__'): data[k]=float(v)
        elif hasattr(v,'isoformat'): data[k]=str(v)
    return data

@app.route("/api/estado")
def api_estado():
    return jsonify(clean(obtener_ultimo()))

@app.route("/api/historial")
def api_historial():
    return jsonify([clean(r) for r in obtener_historial()])

@app.route("/api/heatmap")
def api_heatmap():
    rows=obtener_heatmap()
    for r in rows:
        for k,v in r.items():
            if hasattr(v,'__float__'): r[k]=float(v)
    return jsonify(rows)

@app.route("/api/count")
def api_count():
    return jsonify({"count": obtener_count()})

@app.route("/api/config", methods=["GET","POST"])
def api_config():
    global config
    if request.method == "POST":
        data = request.get_json()
        allowed = ["FILTRO_MIN_USDT","FILTRO_MIN_ORD","FILTRO_MIN_TASA",
                   "FILTRO_MAX_RESP","FILTRO_MAX_MIN_CLP","INTERVALO_MIN"]
        with config_lock:
            for k in allowed:
                if k in data:
                    config[k] = data[k]
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
    init_db()
    threading.Thread(target=ciclo_colector, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
else:
    init_db()
    threading.Thread(target=ciclo_colector, daemon=True).start()
