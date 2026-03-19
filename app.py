"""
P2P Monitor Binance — Vision Maker
"""

import requests
import threading
import time
import os
from datetime import datetime
from zoneinfo import ZoneInfo

SANTIAGO_TZ = ZoneInfo("America/Santiago")
from flask import Flask, jsonify, render_template_string
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)

MONEDA          = "USDT"
FIAT            = "CLP"
INTERVALO_MIN   = 5
FILTRO_MIN_USDT     = 200
FILTRO_MIN_ORDENES  = 150       # Minimo de ordenes completadas
FILTRO_MIN_TASA     = 95.0      # Tasa de exito minima (%)
TOP_ANUNCIOS    = 30
ALERTA_SPREAD   = 0.8
SPREAD_MINIMO   = 0.2
COMISION_BN     = 0.002
DATABASE_URL    = os.environ.get("DATABASE_URL")

URL     = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
HEADERS = {"Content-Type": "application/json"}

ultimo_estado = {}
lock = threading.Lock()

def get_conn():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Borrar tabla si tiene esquema viejo
            cur.execute("""
                DO $$ BEGIN
                  IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='snapshots' AND column_name='mejor_comprador'
                  ) THEN DROP TABLE snapshots;
                  END IF;
                  IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='snapshots' AND column_name='spread_pond_abs'
                  ) THEN DROP TABLE IF EXISTS snapshots;
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
                    liq_tab_compra NUMERIC,
                    liq_tab_venta NUMERIC,
                    n_tab_compra INTEGER,
                    n_tab_venta INTEGER,
                    spread_pond_abs NUMERIC,
                    spread_pond_pct NUMERIC,
                    precio_maker_vender NUMERIC,
                    precio_maker_comprar NUMERIC,
                    ganancia_neta_pct NUMERIC,
                    estado TEXT,
                    color TEXT
                )
            """)
        conn.commit()
    print("✅ Base de datos lista")


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
                    liq_tab_compra, liq_tab_venta,
                    n_tab_compra, n_tab_venta,
                    spread_pond_abs, spread_pond_pct,
                    precio_maker_vender, precio_maker_comprar,
                    ganancia_neta_pct, estado, color
                ) VALUES (
                    %(timestamp)s, %(hora)s, %(dia)s,
                    %(mejor_vendedor_tab_compra)s, %(peor_vendedor_tab_compra)s,
                    %(precio_pond_tab_compra)s, %(lider_tab_compra)s,
                    %(mejor_comprador_tab_venta)s, %(peor_comprador_tab_venta)s,
                    %(precio_pond_tab_venta)s, %(lider_tab_venta)s,
                    %(spread_abs)s, %(spread_pct)s,
                    %(liq_tab_compra)s, %(liq_tab_venta)s,
                    %(n_tab_compra)s, %(n_tab_venta)s,
                    %(spread_pond_abs)s, %(spread_pond_pct)s,
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
                       ROUND(AVG(spread_pct)::numeric, 2) as avg_spread,
                       COUNT(*) as muestras
                FROM snapshots
                GROUP BY hora, dia
                ORDER BY hora
            """)
            rows = cur.fetchall()
    return [dict(r) for r in rows]

def obtener_anuncios(tipo):
    payload = {
        "asset": MONEDA, "fiat": FIAT,
        "merchantCheck": False, "page": 1,
        "publisherType": None, "rows": TOP_ANUNCIOS,
        "tradeType": tipo,
    }
    try:
        r = requests.post(URL, json=payload, headers=HEADERS, timeout=10)
        r.raise_for_status()
        return r.json().get("data", [])
    except:
        return []

def parsear_y_filtrar(anuncios, tipo):
    resultado = []
    for item in anuncios:
        adv   = item.get("adv", {})
        trade = item.get("advertiser", {})
        disponible  = float(adv.get("tradableQuantity", 0))
        completadas = int(trade.get("tradeCount", 0))
        tasa_exito  = float(trade.get("monthFinishRate", 0)) * 100
        if disponible < FILTRO_MIN_USDT:
            continue
        if completadas < FILTRO_MIN_ORDENES:
            continue
        if tasa_exito < FILTRO_MIN_TASA:
            continue
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
    """
    tab_compra = anuncios de vendedores de USDT (API tradeType BUY)
                 → donde el usuario va a COMPRAR USDT
                 → precios MÁS ALTOS (vendedores quieren más CLP)

    tab_venta  = anuncios de compradores de USDT (API tradeType SELL)
                 → donde el usuario va a VENDER USDT
                 → precios MÁS BAJOS (compradores ofrecen menos CLP)

    Como Maker:
    - Para VENDER USDT → pones anuncio en Tab Compra → compites con el más barato de Tab Compra
    - Para COMPRAR USDT → pones anuncio en Tab Venta → compites con el que más paga de Tab Venta
    """
    if not tab_compra or not tab_venta:
        return None

    # Tab Compra: vendedores de USDT. El más barato es el líder (primero en la lista).
    lider_tc     = min(tab_compra, key=lambda x: x["precio"])  # más barato
    mas_caro_tc  = max(tab_compra, key=lambda x: x["precio"])  # más caro

    # Tab Venta: compradores de USDT. El que más paga es el líder (primero en la lista).
    lider_tv     = max(tab_venta, key=lambda x: x["precio"])   # paga más
    menos_tv     = min(tab_venta, key=lambda x: x["precio"])   # paga menos

    # Spread = lo que pagan compradores (Tab Venta) − lo que piden vendedores (Tab Compra)
    spread_abs = lider_tv["precio"] - lider_tc["precio"]
    spread_pct = round((spread_abs / lider_tv["precio"]) * 100, 4) if lider_tv["precio"] > 0 else 0

    liq_tc = sum(a["disponible"] for a in tab_compra)
    liq_tv = sum(a["disponible"] for a in tab_venta)

    pond_tc = round(precio_ponderado(tab_compra), 2)
    pond_tv = round(precio_ponderado(tab_venta),  2)

    # Spread entre precios ponderados
    spread_pond_abs = round(pond_tv - pond_tc, 2)
    spread_pond_pct = round((spread_pond_abs / pond_tv) * 100, 4) if pond_tv > 0 else 0

    # Maker: un centavo mejor que el lider de cada lado
    precio_maker_vender  = round(lider_tc["precio"]  - 0.01, 2)  # un centavo más barato que el más barato de Tab Compra
    precio_maker_comprar = round(lider_tv["precio"]  + 0.01, 2)  # un centavo más que el que más paga de Tab Venta

    ganancia = round(spread_pct - (COMISION_BN * 2 * 100), 4)

    if spread_pct >= ALERTA_SPREAD:
        estado, color = "MUY APTO 🔥", "green"
    elif spread_pct >= SPREAD_MINIMO:
        estado, color = "APTO ✅", "yellow"
    elif spread_pct >= 0:
        estado, color = "ESTRECHO ⚠️", "orange"
    else:
        estado, color = "NO APTO ❌", "red"

    return {
        "timestamp":                datetime.now(SANTIAGO_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "hora":                     datetime.now(SANTIAGO_TZ).hour,
        "dia":                      datetime.now(SANTIAGO_TZ).strftime("%A"),
        # Tab Compra (vendedores de USDT)
        "mejor_vendedor_tab_compra":  lider_tc["precio"],
        "peor_vendedor_tab_compra":   mas_caro_tc["precio"],
        "precio_pond_tab_compra":     pond_tc,
        "lider_tab_compra":           lider_tc["anunciante"],
        # Tab Venta (compradores de USDT)
        "mejor_comprador_tab_venta":  lider_tv["precio"],
        "peor_comprador_tab_venta":   menos_tv["precio"],
        "precio_pond_tab_venta":      pond_tv,
        "lider_tab_venta":            lider_tv["anunciante"],
        # Métricas
        "spread_abs":                 round(spread_abs, 2),
        "spread_pct":                 spread_pct,
        "liq_tab_compra":             round(liq_tc, 2),
        "liq_tab_venta":              round(liq_tv, 2),
        "n_tab_compra":               len(tab_compra),
        "n_tab_venta":                len(tab_venta),
        "precio_maker_vender":        precio_maker_vender,
        "precio_maker_comprar":       precio_maker_comprar,
        "spread_pond_abs":            spread_pond_abs,
        "spread_pond_pct":            spread_pond_pct,
        "ganancia_neta_pct":          ganancia,
        "estado":                     estado,
        "color":                      color,
    }

def ciclo_colector():
    time.sleep(5)
    while True:
        try:
            # BUY = Tab Compra (vendedores de USDT)
            # SELL = Tab Venta (compradores de USDT)
            tab_compra = parsear_y_filtrar(obtener_anuncios("BUY"),  "BUY")
            tab_venta  = parsear_y_filtrar(obtener_anuncios("SELL"), "SELL")
            estado     = analizar(tab_compra, tab_venta)
            if estado:
                guardar_snapshot(estado)
                with lock:
                    ultimo_estado.update(estado)
                print(f"[{estado['timestamp']}] Spread: {estado['spread_pct']}% — {estado['estado']}")
        except Exception as e:
            print(f"[ERROR colector] {e}")
        time.sleep(INTERVALO_MIN * 60)

DASHBOARD = """
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>P2P Monitor — Unión Austral</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0d0d1a; color: #fff; font-family: 'Segoe UI', sans-serif; }
  header { background: #12122a; padding: 14px 20px; border-bottom: 1px solid #1e1e3f;
           display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }
  header h1 { color: #00d4ff; font-size: 0.95rem; }
  header span { color: #888; font-size: 0.72rem; }
  .tabs { display: flex; gap: 6px; padding: 14px 20px 0; }
  .tab { padding: 8px 16px; border-radius: 8px 8px 0 0; cursor: pointer; font-size: 0.82rem;
         background: #12122a; border: 1px solid #1e1e3f; border-bottom: none; color: #888; }
  .tab.active { background: #1a1a3e; color: #00d4ff; border-color: #00d4ff; }
  .tab-content { display: none; }
  .tab-content.active { display: block; }
  .estado-banner { margin: 14px 20px 4px; padding: 12px 20px; border-radius: 10px;
                   font-size: 0.95rem; font-weight: bold; text-align: center; }
  .banner-green  { background:#00e67222; border:1px solid #00e676; color:#00e676; }
  .banner-yellow { background:#ffd74022; border:1px solid #ffd740; color:#ffd740; }
  .banner-orange { background:#ff910022; border:1px solid #ff9100; color:#ff9100; }
  .banner-red    { background:#ff525222; border:1px solid #ff5252; color:#ff5252; }

  .panel-grid { display:grid; grid-template-columns:1fr 1fr; gap:12px; padding:14px 20px; }
  .panel-tc { background:#0d1f0d; border:2px solid #00e676; border-radius:12px; padding:16px; }
  .panel-tv { background:#1f0d0d; border:2px solid #ff5252; border-radius:12px; padding:16px; }
  .panel-header { font-size:0.68rem; font-weight:bold; text-transform:uppercase;
                  letter-spacing:1px; margin-bottom:8px; display:flex; align-items:center; gap:6px; }
  .panel-tc .panel-header { color:#00e676; }
  .panel-tv .panel-header { color:#ff5252; }
  .tab-badge { font-size:0.6rem; padding:2px 7px; border-radius:4px; font-weight:bold; }
  .badge-tc { background:#00e67233; color:#00e676; border:1px solid #00e676; }
  .badge-tv { background:#ff525233; color:#ff5252; border:1px solid #ff5252; }
  .panel-desc { font-size:0.68rem; color:#aaa; margin-bottom:10px; }
  .panel-precio-main { font-size:1.7rem; font-weight:bold; margin:2px 0; }
  .panel-tc .panel-precio-main { color:#00e676; }
  .panel-tv .panel-precio-main { color:#ff5252; }
  .panel-precio-label { font-size:0.62rem; color:#888; margin-bottom:6px; }
  .panel-lider { font-size:0.75rem; color:#ccc; margin:2px 0; }
  .panel-lider span { font-weight:bold; color:white; }
  .panel-sep { border:none; border-top:1px solid #ffffff11; margin:8px 0; }
  .panel-pond { font-size:0.7rem; color:#888; }
  .panel-pond b { color:#aaa; }
  .panel-liq { font-size:0.7rem; color:#888; margin-top:3px; }
  .panel-liq b { color:#aaa; }
  .panel-rango { font-size:0.68rem; color:#666; margin-top:3px; }

  .spread-row { display:grid; grid-template-columns:1fr 1fr 1fr; gap:12px; padding:0 20px 14px; }
  .mini-card { background:#12122a; border:1px solid #1e1e3f; border-radius:10px; padding:12px; text-align:center; }
  .mini-card .mlabel { font-size:0.62rem; color:#888; text-transform:uppercase; margin-bottom:6px; }
  .mini-card .mvalue { font-size:1.15rem; font-weight:bold; }
  .mini-card .mnota  { font-size:0.6rem; color:#888; margin-top:4px; }

  .maker-section { padding:0 20px 6px; }
  .maker-title { font-size:0.7rem; color:#888; text-align:center; padding:6px 0 10px;
                 border-top:1px solid #1e1e3f; margin-top:4px; }
  .maker-grid { display:grid; grid-template-columns:1fr 1fr; gap:12px; padding:0 20px 16px; }
  .maker-card { border-radius:10px; padding:14px; }
  .maker-tc { background:#0d1f0d; border:1px solid #00e676; }
  .maker-tv { background:#1f0d0d; border:1px solid #ff5252; }
  .maker-card .mtipo { font-size:0.65rem; text-transform:uppercase; letter-spacing:0.5px; margin-bottom:8px; line-height:1.4; }
  .maker-tc .mtipo { color:#00e676; }
  .maker-tv .mtipo { color:#ff5252; }
  .maker-card .mprecio { font-size:1.5rem; font-weight:bold; color:#ffd740; }
  .maker-card .mnota { font-size:0.62rem; color:#888; margin-top:6px; line-height:1.4; }

  .seccion { padding:0 20px 16px; }
  .seccion h2 { color:#00d4ff; font-size:0.78rem; text-transform:uppercase;
                letter-spacing:1px; margin-bottom:10px; padding-top:14px; }
  .chart-wrap { background:#12122a; border:1px solid #1e1e3f; border-radius:10px; padding:14px; }
  .stat-row { display:flex; justify-content:space-between; padding:7px 0;
              border-bottom:1px solid #1e1e3f; font-size:0.82rem; }
  .stat-row:last-child { border-bottom:none; }
  .stat-label { color:#888; }
  .stat-value { font-weight:bold; }
  .refresh { color:#888; font-size:0.7rem; text-align:center; padding:10px; }
  #countdown { color:#00d4ff; }
  .sin-datos { text-align:center; padding:60px; color:#888; }
  .green{color:#00e676;} .yellow{color:#ffd740;} .orange{color:#ff9100;} .red{color:#ff5252;} .cyan{color:#00d4ff;}
</style>
</head>
<body>
<header>
  <h1>📊 P2P Monitor — Unión Austral Capital</h1>
  <span id="ultima-act">Cargando...</span>
</header>
<div class="tabs">
  <div class="tab active" onclick="showTab('tiempo-real',this)">⚡ Tiempo Real</div>
  <div class="tab" onclick="showTab('historico',this)">📈 Histórico</div>
  <div class="tab" onclick="showTab('heatmap',this)">🔥 Mapa de Calor</div>
</div>

<div id="tiempo-real" class="tab-content active">
  <div id="contenido-tr"><div class="sin-datos">⏳ Esperando primer ciclo de datos (5 min)...</div></div>
</div>

<div id="historico" class="tab-content">
  <div class="seccion">
    <h2>Spread histórico</h2>
    <div class="chart-wrap"><canvas id="chartSpread" height="100"></canvas></div>
  </div>
  <div class="seccion">
    <h2>Liquidez histórica</h2>
    <div class="chart-wrap"><canvas id="chartLiq" height="100"></canvas></div>
  </div>
</div>

<div id="heatmap" class="tab-content">
  <div class="seccion">
    <h2>Spread promedio por hora y día</h2>
    <div class="chart-wrap"><canvas id="chartHeat" height="180"></canvas></div>
  </div>
  <div class="seccion">
    <div id="stats-resumen" class="chart-wrap"></div>
  </div>
</div>

<div class="refresh">Actualización automática en <span id="countdown">30</span>s</div>

<script>
function showTab(id,el){
  document.querySelectorAll('.tab-content').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.getElementById(id).classList.add('active'); el.classList.add('active');
}
let cuenta=30;
setInterval(()=>{ cuenta--; document.getElementById('countdown').textContent=cuenta; if(cuenta<=0){cuenta=30;cargarDatos();} },1000);
let chartSpread,chartLiq,chartHeat;
function fmt(n,dec=2){ return parseFloat(n).toLocaleString('es-CL',{minimumFractionDigits:dec,maximumFractionDigits:dec}); }

async function cargarDatos(){
  try {
    const [estado,hist,heat] = await Promise.all([
      fetch('/api/estado').then(r=>r.json()),
      fetch('/api/historial').then(r=>r.json()),
      fetch('/api/heatmap').then(r=>r.json())
    ]);
    if(estado.timestamp){ renderTR(estado); document.getElementById('ultima-act').textContent='Última act: '+estado.timestamp; }
    if(hist.length>1) renderHistorico(hist);
    if(heat.length>0){ renderHeatmap(heat); renderStats(hist); }
  } catch(e){ console.error(e); }
}

function renderTR(e){
  const sp = parseFloat(e.spread_pct).toFixed(2);
  const gn = parseFloat(e.ganancia_neta_pct).toFixed(2);
  const gnColor = parseFloat(e.ganancia_neta_pct)>0?'green':'red';
  document.getElementById('contenido-tr').innerHTML = `
    <div class="estado-banner banner-${e.color}">${e.estado} — Spread ${sp}%</div>

    <div class="panel-grid">

      <!-- TAB COMPRA: vendedores de USDT -->
      <div class="panel-tc">
        <div class="panel-header">
          <span class="tab-badge badge-tc">TAB COMPRA</span>
        </div>
        <div class="panel-desc">Vendedores de USDT<br>El usuario va aquí a <b>comprar</b></div>
        <div class="panel-precio-label">El más barato (líder):</div>
        <div class="panel-precio-main">$${fmt(e.mejor_vendedor_tab_compra)}</div>
        <div class="panel-lider">👤 <span>${e.lider_tab_compra||'—'}</span></div>
        <hr class="panel-sep">
        <div class="panel-pond"><b>Precio ponderado:</b> $${fmt(e.precio_pond_tab_compra)}</div>
        <div class="panel-liq"><b>Liquidez total:</b> ${fmt(e.liq_tab_compra,0)} USDT · ${e.n_tab_compra} anuncios</div>
        <div class="panel-rango">Más caro: $${fmt(e.peor_vendedor_tab_compra)}</div>
      </div>

      <!-- TAB VENTA: compradores de USDT -->
      <div class="panel-tv">
        <div class="panel-header">
          <span class="tab-badge badge-tv">TAB VENTA</span>
        </div>
        <div class="panel-desc">Compradores de USDT<br>El usuario va aquí a <b>vender</b></div>
        <div class="panel-precio-label">El que más paga (líder):</div>
        <div class="panel-precio-main">$${fmt(e.mejor_comprador_tab_venta)}</div>
        <div class="panel-lider">👤 <span>${e.lider_tab_venta||'—'}</span></div>
        <hr class="panel-sep">
        <div class="panel-pond"><b>Precio ponderado:</b> $${fmt(e.precio_pond_tab_venta)}</div>
        <div class="panel-liq"><b>Liquidez total:</b> ${fmt(e.liq_tab_venta,0)} USDT · ${e.n_tab_venta} anuncios</div>
        <div class="panel-rango">Menos paga: $${fmt(e.peor_comprador_tab_venta)}</div>
      </div>
    </div>

    <div style="margin:0 20px 4px; background:#12122a; border:1px solid #1e1e3f; border-radius:10px; padding:10px 16px; display:flex; justify-content:space-between; align-items:center;">
      <span style="font-size:0.72rem; color:#888;">Brecha precio ponderado</span>
      <span style="font-size:1rem; font-weight:bold; color:#ffd740;">
        $${fmt(e.spread_pond_abs)} CLP — ${parseFloat(e.spread_pond_pct).toFixed(2)}%
      </span>
    </div>

    <div class="spread-row">
      <div class="mini-card">
        <div class="mlabel">Spread actual</div>
        <div class="mvalue ${e.color==='green'?'green':e.color==='yellow'?'yellow':'red'}">${sp}%</div>
        <div class="mnota">$${fmt(e.spread_abs)} CLP/USDT</div>
      </div>
      <div class="mini-card">
        <div class="mlabel">Ganancia neta est.</div>
        <div class="mvalue ${gnColor}">${gn}%</div>
        <div class="mnota">Descontada comisión 0.40%</div>
      </div>
      <div class="mini-card">
        <div class="mlabel">Snapshots</div>
        <div class="mvalue cyan">—</div>
        <div class="mnota">En base de datos</div>
      </div>
    </div>

    <div class="maker-title">
      ── Como Maker, pondrías tu anuncio un centavo mejor que el líder ──
    </div>

    <div class="maker-grid">
      <div class="maker-card maker-tc">
        <div class="mtipo">🟢 Quiero VENDER USDT<br>(posteo en Tab Compra)</div>
        <div class="mprecio">$${fmt(e.precio_maker_vender)}</div>
        <div class="mnota">
          Un centavo menos que <b>${e.lider_tab_compra||'el líder'}</b><br>
          que pide $${fmt(e.mejor_vendedor_tab_compra)}<br>
          → aparecer primero en Tab Compra
        </div>
      </div>
      <div class="maker-card maker-tv">
        <div class="mtipo">🔴 Quiero COMPRAR USDT<br>(posteo en Tab Venta)</div>
        <div class="mprecio">$${fmt(e.precio_maker_comprar)}</div>
        <div class="mnota">
          Un centavo más que <b>${e.lider_tab_venta||'el líder'}</b><br>
          que paga $${fmt(e.mejor_comprador_tab_venta)}<br>
          → aparecer primero en Tab Venta
        </div>
      </div>
    </div>
  `;
}

function renderHistorico(hist){
  const labels=hist.map(h=>h.timestamp.toString().slice(11,16));
  const spreads=hist.map(h=>parseFloat(h.spread_pct));
  const liqTC=hist.map(h=>parseFloat(h.liq_tab_compra||0));
  const liqTV=hist.map(h=>parseFloat(h.liq_tab_venta||0));
  const sc={ x:{ticks:{color:'#888',maxTicksLimit:10},grid:{color:'#1e1e3f'}}, y:{ticks:{color:'#888'},grid:{color:'#1e1e3f'}} };
  if(chartSpread) chartSpread.destroy();
  if(chartLiq) chartLiq.destroy();
  chartSpread=new Chart(document.getElementById('chartSpread').getContext('2d'),{
    type:'line', data:{labels,datasets:[{label:'Spread %',data:spreads,borderColor:'#00d4ff',backgroundColor:'rgba(0,212,255,0.08)',borderWidth:2,pointRadius:2,tension:0.3,fill:true}]},
    options:{responsive:true,plugins:{legend:{labels:{color:'#aaa'}}},scales:sc}
  });
  chartLiq=new Chart(document.getElementById('chartLiq').getContext('2d'),{
    type:'line', data:{labels,datasets:[
      {label:'Liquidez Tab Compra',data:liqTC,borderColor:'#00e676',backgroundColor:'rgba(0,230,118,0.06)',borderWidth:2,pointRadius:1,tension:0.3,fill:true},
      {label:'Liquidez Tab Venta', data:liqTV,borderColor:'#ff5252',backgroundColor:'rgba(255,82,82,0.06)', borderWidth:2,pointRadius:1,tension:0.3,fill:true}
    ]},
    options:{responsive:true,plugins:{legend:{labels:{color:'#aaa'}}},scales:sc}
  });
}

function renderHeatmap(heat){
  const dias=['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'];
  const diasEs=['Lunes','Martes','Miércoles','Jueves','Viernes','Sábado','Domingo'];
  const horas=Array.from({length:24},(_,i)=>i);
  const datasets=dias.map((dia,di)=>({
    label:diasEs[di], data:horas.map(h=>{const m=heat.find(r=>r.dia===dia&&parseInt(r.hora)===h);return m?parseFloat(m.avg_spread):null;}),
    backgroundColor:`hsla(${di*50},70%,55%,0.75)`, borderColor:`hsla(${di*50},70%,55%,1)`, borderWidth:1
  }));
  if(chartHeat) chartHeat.destroy();
  chartHeat=new Chart(document.getElementById('chartHeat').getContext('2d'),{
    type:'bar', data:{labels:horas.map(h=>h+':00'),datasets},
    options:{responsive:true,plugins:{legend:{labels:{color:'#aaa',boxWidth:12}}},
      scales:{x:{ticks:{color:'#888'},grid:{color:'#1e1e3f'}},y:{ticks:{color:'#888',callback:v=>v+'%'},grid:{color:'#1e1e3f'}}}}
  });
}

function renderStats(hist){
  if(!hist.length) return;
  const spreads=hist.map(h=>parseFloat(h.spread_pct));
  const avg=(spreads.reduce((a,b)=>a+b,0)/spreads.length).toFixed(2);
  const max=Math.max(...spreads).toFixed(2);
  const min=Math.min(...spreads).toFixed(2);
  const hc={};
  hist.forEach(h=>{(hc[h.hora]=hc[h.hora]||[]).push(parseFloat(h.spread_pct));});
  const mh=Object.entries(hc).map(([h,v])=>[h,v.reduce((a,b)=>a+b,0)/v.length]).sort((a,b)=>b[1]-a[1])[0];
  document.getElementById('stats-resumen').innerHTML=`
    <div style="color:#00d4ff;font-size:0.78rem;text-transform:uppercase;letter-spacing:1px;margin-bottom:12px">Estadísticas del período</div>
    <div class="stat-row"><span class="stat-label">Total snapshots</span><span class="stat-value cyan">${hist.length}</span></div>
    <div class="stat-row"><span class="stat-label">Spread promedio</span><span class="stat-value yellow">${avg}%</span></div>
    <div class="stat-row"><span class="stat-label">Spread máximo</span><span class="stat-value green">${max}%</span></div>
    <div class="stat-row"><span class="stat-label">Spread mínimo</span><span class="stat-value red">${min}%</span></div>
    ${mh?`<div class="stat-row"><span class="stat-label">Mejor hora promedio</span><span class="stat-value green">${mh[0]}:00 hs (${parseFloat(mh[1]).toFixed(2)}%)</span></div>`:''}
  `;
}
cargarDatos();
</script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(DASHBOARD)

@app.route("/api/estado")
def api_estado():
    data=obtener_ultimo()
    for k,v in data.items():
        if hasattr(v,'__float__'): data[k]=float(v)
        elif hasattr(v,'isoformat'): data[k]=str(v)
    return jsonify(data)

@app.route("/api/historial")
def api_historial():
    rows=obtener_historial()
    for row in rows:
        for k,v in row.items():
            if hasattr(v,'__float__'): row[k]=float(v)
            elif hasattr(v,'isoformat'): row[k]=str(v)
    return jsonify(rows)

@app.route("/api/heatmap")
def api_heatmap():
    rows=obtener_heatmap()
    for row in rows:
        for k,v in row.items():
            if hasattr(v,'__float__'): row[k]=float(v)
    return jsonify(rows)

if __name__ == "__main__":
    init_db()
    t=threading.Thread(target=ciclo_colector,daemon=True); t.start()
    port=int(os.environ.get("PORT",5000))
    app.run(host="0.0.0.0",port=port)
else:
    init_db()
    t=threading.Thread(target=ciclo_colector,daemon=True); t.start()
