"""
P2P Monitor Binance — Vision Maker
Flask app con colector en background + dashboard web + PostgreSQL
"""

import requests
import threading
import time
import os
from datetime import datetime
from flask import Flask, jsonify, render_template_string
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)

# ──────────────────────────────────────────────
#  CONFIGURACIÓN
# ──────────────────────────────────────────────
MONEDA          = "USDT"
FIAT            = "CLP"
INTERVALO_MIN   = 5
FILTRO_MIN_USDT = 200
TOP_ANUNCIOS    = 20
ALERTA_SPREAD   = 0.8
SPREAD_MINIMO   = 0.2
COMISION_BN     = 0.002
DATABASE_URL    = os.environ.get("DATABASE_URL")

URL     = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
HEADERS = {"Content-Type": "application/json"}

ultimo_estado = {}
lock = threading.Lock()

# ──────────────────────────────────────────────
#  BASE DE DATOS
# ──────────────────────────────────────────────
def get_conn():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS snapshots (
                    id SERIAL PRIMARY KEY,
                    timestamp TIMESTAMP NOT NULL,
                    hora INTEGER,
                    dia TEXT,
                    mejor_comprador NUMERIC,
                    mejor_vendedor NUMERIC,
                    spread_abs NUMERIC,
                    spread_pct NUMERIC,
                    liq_compra_usdt NUMERIC,
                    liq_venta_usdt NUMERIC,
                    n_compradores INTEGER,
                    n_vendedores INTEGER,
                    precio_sug_venta NUMERIC,
                    precio_sug_compra NUMERIC,
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
                    mejor_comprador, mejor_vendedor,
                    spread_abs, spread_pct,
                    liq_compra_usdt, liq_venta_usdt,
                    n_compradores, n_vendedores,
                    precio_sug_venta, precio_sug_compra,
                    ganancia_neta_pct, estado, color
                ) VALUES (
                    %(timestamp)s, %(hora)s, %(dia)s,
                    %(mejor_comprador)s, %(mejor_vendedor)s,
                    %(spread_abs)s, %(spread_pct)s,
                    %(liq_compra_usdt)s, %(liq_venta_usdt)s,
                    %(n_compradores)s, %(n_vendedores)s,
                    %(precio_sug_venta)s, %(precio_sug_compra)s,
                    %(ganancia_neta_pct)s, %(estado)s, %(color)s
                )
            """, m)
        conn.commit()

def obtener_historial(limit=200):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM snapshots
                ORDER BY timestamp DESC
                LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
    return [dict(r) for r in reversed(rows)]

def obtener_ultimo():
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM snapshots
                ORDER BY timestamp DESC
                LIMIT 1
            """)
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

# ──────────────────────────────────────────────
#  COLECTOR
# ──────────────────────────────────────────────
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
        disponible = float(adv.get("tradableQuantity", 0))
        if disponible < FILTRO_MIN_USDT:
            continue
        resultado.append({
            "tipo":       tipo,
            "precio":     float(adv.get("price", 0)),
            "disponible": disponible,
            "anunciante": trade.get("nickName", ""),
        })
    return resultado

def analizar(compradores, vendedores):
    if not compradores or not vendedores:
        return None
    mejor_comprador = max(c["precio"] for c in compradores)
    mejor_vendedor  = min(v["precio"] for v in vendedores)
    spread_abs = mejor_comprador - mejor_vendedor
    spread_pct = round((spread_abs / mejor_vendedor) * 100, 4) if mejor_vendedor > 0 else 0
    liq_compra = sum(c["disponible"] for c in compradores)
    liq_venta  = sum(v["disponible"] for v in vendedores)
    ganancia   = round(spread_pct - (COMISION_BN * 2 * 100), 4)

    if spread_pct >= ALERTA_SPREAD:
        estado, color = "MUY APTO 🔥", "green"
    elif spread_pct >= SPREAD_MINIMO:
        estado, color = "APTO ✅", "yellow"
    elif spread_pct >= 0:
        estado, color = "ESTRECHO ⚠️", "orange"
    else:
        estado, color = "NO APTO ❌", "red"

    return {
        "timestamp":         datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "hora":              datetime.now().hour,
        "dia":               datetime.now().strftime("%A"),
        "mejor_comprador":   mejor_comprador,
        "mejor_vendedor":    mejor_vendedor,
        "spread_abs":        round(spread_abs, 2),
        "spread_pct":        spread_pct,
        "liq_compra_usdt":   round(liq_compra, 2),
        "liq_venta_usdt":    round(liq_venta, 2),
        "n_compradores":     len(compradores),
        "n_vendedores":      len(vendedores),
        "precio_sug_venta":  mejor_vendedor - 1,
        "precio_sug_compra": mejor_comprador + 1,
        "ganancia_neta_pct": ganancia,
        "estado":            estado,
        "color":             color,
    }

def ciclo_colector():
    time.sleep(5)
    while True:
        try:
            compradores = parsear_y_filtrar(obtener_anuncios("BUY"),  "BUY")
            vendedores  = parsear_y_filtrar(obtener_anuncios("SELL"), "SELL")
            estado      = analizar(compradores, vendedores)
            if estado:
                guardar_snapshot(estado)
                with lock:
                    ultimo_estado.update(estado)
                print(f"[{estado['timestamp']}] Spread: {estado['spread_pct']}% — {estado['estado']}")
        except Exception as e:
            print(f"[ERROR colector] {e}")
        time.sleep(INTERVALO_MIN * 60)

# ──────────────────────────────────────────────
#  DASHBOARD HTML
# ──────────────────────────────────────────────
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
  header { background: #12122a; padding: 16px 24px; border-bottom: 1px solid #1e1e3f;
           display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }
  header h1 { color: #00d4ff; font-size: 1rem; }
  header span { color: #888; font-size: 0.75rem; }
  .tabs { display: flex; gap: 8px; padding: 16px 20px 0; }
  .tab { padding: 8px 16px; border-radius: 8px 8px 0 0; cursor: pointer; font-size: 0.85rem;
         background: #12122a; border: 1px solid #1e1e3f; border-bottom: none; color: #888; }
  .tab.active { background: #1a1a3e; color: #00d4ff; border-color: #00d4ff; }
  .tab-content { display: none; }
  .tab-content.active { display: block; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
          gap: 12px; padding: 16px 20px; }
  .card { background: #12122a; border: 1px solid #1e1e3f; border-radius: 10px; padding: 14px; }
  .card .label { color: #888; font-size: 0.7rem; text-transform: uppercase; margin-bottom: 6px; }
  .card .value { font-size: 1.3rem; font-weight: bold; }
  .card .sub   { color: #888; font-size: 0.7rem; margin-top: 4px; }
  .green { color: #00e676; } .yellow { color: #ffd740; }
  .orange { color: #ff9100; } .red { color: #ff5252; } .cyan { color: #00d4ff; }
  .estado-banner { margin: 0 20px 4px; padding: 12px 20px; border-radius: 10px;
                   font-size: 0.95rem; font-weight: bold; text-align: center; }
  .banner-green  { background: #00e67622; border: 1px solid #00e676; color: #00e676; }
  .banner-yellow { background: #ffd74022; border: 1px solid #ffd740; color: #ffd740; }
  .banner-orange { background: #ff910022; border: 1px solid #ff9100; color: #ff9100; }
  .banner-red    { background: #ff525222; border: 1px solid #ff5252; color: #ff5252; }
  .seccion { padding: 0 20px 20px; }
  .seccion h2 { color: #00d4ff; font-size: 0.8rem; text-transform: uppercase;
                letter-spacing: 1px; margin-bottom: 12px; padding-top: 16px; }
  .chart-wrap { background: #12122a; border: 1px solid #1e1e3f; border-radius: 10px; padding: 14px; }
  .precios { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin: 0 20px 16px; }
  .precio-card { background: #12122a; border: 1px solid #1e1e3f; border-radius: 10px;
                 padding: 14px; text-align: center; }
  .precio-card .tipo { font-size: 0.65rem; color: #888; text-transform: uppercase; margin-bottom: 8px; }
  .precio-card .precio { font-size: 1.5rem; font-weight: bold; }
  .precio-card .nota { font-size: 0.65rem; color: #888; margin-top: 4px; }
  .refresh { color: #888; font-size: 0.7rem; text-align: center; padding: 10px; }
  #countdown { color: #00d4ff; }
  .sin-datos { text-align: center; padding: 60px; color: #888; }
  .stat-row { display: flex; justify-content: space-between; padding: 8px 0;
              border-bottom: 1px solid #1e1e3f; font-size: 0.85rem; }
  .stat-row:last-child { border-bottom: none; }
  .stat-label { color: #888; }
  .stat-value { font-weight: bold; }
</style>
</head>
<body>
<header>
  <h1>📊 P2P Monitor — Unión Austral Capital</h1>
  <span id="ultima-act">Cargando...</span>
</header>

<div class="tabs">
  <div class="tab active" onclick="showTab('tiempo-real')">⚡ Tiempo Real</div>
  <div class="tab" onclick="showTab('historico')">📈 Histórico</div>
  <div class="tab" onclick="showTab('heatmap')">🔥 Mapa de Calor</div>
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
    <div class="chart-wrap"><canvas id="chartHeat" height="200"></canvas></div>
  </div>
  <div class="seccion">
    <div id="stats-resumen" class="card"></div>
  </div>
</div>

<div class="refresh">Actualización automática en <span id="countdown">30</span>s</div>

<script>
function showTab(id) {
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  event.target.classList.add('active');
}

let cuenta = 30;
setInterval(() => {
  cuenta--;
  document.getElementById('countdown').textContent = cuenta;
  if (cuenta <= 0) { cuenta = 30; cargarDatos(); }
}, 1000);

let chartSpread, chartLiq, chartHeat;

async function cargarDatos() {
  try {
    const [estado, hist, heat] = await Promise.all([
      fetch('/api/estado').then(r => r.json()),
      fetch('/api/historial').then(r => r.json()),
      fetch('/api/heatmap').then(r => r.json())
    ]);
    if (estado.timestamp) {
      renderTR(estado);
      document.getElementById('ultima-act').textContent = 'Última actualización: ' + estado.timestamp;
    }
    if (hist.length > 1) {
      renderHistorico(hist);
    }
    if (heat.length > 0) {
      renderHeatmap(heat);
      renderStats(hist);
    }
  } catch(e) { console.error(e); }
}

function renderTR(e) {
  const cc = e.color;
  document.getElementById('contenido-tr').innerHTML = `
    <div style="height:12px"></div>
    <div class="estado-banner banner-${cc}">${e.estado} — Spread ${parseFloat(e.spread_pct).toFixed(2)}%</div>
    <div class="grid">
      <div class="card">
        <div class="label">Mejor comprador</div>
        <div class="value green">$${parseInt(e.mejor_comprador).toLocaleString('es-CL')}</div>
        <div class="sub">${e.n_compradores} anuncios (+200 USDT)</div>
      </div>
      <div class="card">
        <div class="label">Mejor vendedor</div>
        <div class="value red">$${parseInt(e.mejor_vendedor).toLocaleString('es-CL')}</div>
        <div class="sub">${e.n_vendedores} anuncios (+200 USDT)</div>
      </div>
      <div class="card">
        <div class="label">Liquidez compra</div>
        <div class="value cyan">${parseInt(e.liq_compra_usdt).toLocaleString('es-CL')} USDT</div>
        <div class="sub">Disponible en mercado</div>
      </div>
      <div class="card">
        <div class="label">Liquidez venta</div>
        <div class="value cyan">${parseInt(e.liq_venta_usdt).toLocaleString('es-CL')} USDT</div>
        <div class="sub">Disponible en mercado</div>
      </div>
      <div class="card">
        <div class="label">Ganancia neta est.</div>
        <div class="value ${parseFloat(e.ganancia_neta_pct) > 0 ? 'green' : 'red'}">${parseFloat(e.ganancia_neta_pct).toFixed(2)}%</div>
        <div class="sub">Descontada comisión 0.40%</div>
      </div>
    </div>
    <div class="precios">
      <div class="precio-card">
        <div class="tipo">Si vas a VENDER USDT</div>
        <div class="precio yellow">$${parseInt(e.precio_sug_venta).toLocaleString('es-CL')}</div>
        <div class="nota">Un peso menos que el mejor vendedor</div>
      </div>
      <div class="precio-card">
        <div class="tipo">Si vas a COMPRAR USDT</div>
        <div class="precio yellow">$${parseInt(e.precio_sug_compra).toLocaleString('es-CL')}</div>
        <div class="nota">Un peso más que el mejor comprador</div>
      </div>
    </div>`;
}

function renderHistorico(hist) {
  const labels = hist.map(h => h.timestamp.toString().slice(11,16));
  const spreads = hist.map(h => parseFloat(h.spread_pct));
  const liqC = hist.map(h => parseFloat(h.liq_compra_usdt));
  const liqV = hist.map(h => parseFloat(h.liq_venta_usdt));
  const opts = { responsive: true, plugins: { legend: { labels: { color: '#aaa' } } },
    scales: { x: { ticks: { color:'#888', maxTicksLimit:10 }, grid:{ color:'#1e1e3f' } },
              y: { ticks: { color:'#888' }, grid:{ color:'#1e1e3f' } } } };
  if (chartSpread) chartSpread.destroy();
  if (chartLiq) chartLiq.destroy();
  chartSpread = new Chart(document.getElementById('chartSpread').getContext('2d'), {
    type: 'line', data: { labels, datasets: [{
      label: 'Spread %', data: spreads, borderColor: '#00d4ff',
      backgroundColor: 'rgba(0,212,255,0.08)', borderWidth: 2, pointRadius: 2, tension: 0.3, fill: true
    }]}, options: { ...opts, plugins: { ...opts.plugins,
      annotation: { annotations: { line1: { type:'line', yMin:0.8, yMax:0.8,
        borderColor:'#00e676', borderWidth:1, borderDash:[4,4] } } } } }
  });
  chartLiq = new Chart(document.getElementById('chartLiq').getContext('2d'), {
    type: 'line', data: { labels, datasets: [
      { label: 'Liquidez Compra', data: liqC, borderColor: '#00e676', backgroundColor: 'rgba(0,230,118,0.05)', borderWidth: 2, pointRadius: 1, tension: 0.3, fill: true },
      { label: 'Liquidez Venta', data: liqV, borderColor: '#ff5252', backgroundColor: 'rgba(255,82,82,0.05)', borderWidth: 2, pointRadius: 1, tension: 0.3, fill: true }
    ]}, options: opts
  });
}

function renderHeatmap(heat) {
  const dias = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'];
  const diasEs = ['Lunes','Martes','Miércoles','Jueves','Viernes','Sábado','Domingo'];
  const horas = Array.from({length:24}, (_,i) => i);
  const datasets = dias.map((dia, di) => {
    const data = horas.map(h => {
      const match = heat.find(r => r.dia === dia && parseInt(r.hora) === h);
      return match ? parseFloat(match.avg_spread) : null;
    });
    return { label: diasEs[di], data, backgroundColor: `hsla(${di*50},70%,60%,0.7)`,
             borderColor: `hsla(${di*50},70%,60%,1)`, borderWidth: 1 };
  });
  if (chartHeat) chartHeat.destroy();
  chartHeat = new Chart(document.getElementById('chartHeat').getContext('2d'), {
    type: 'bar',
    data: { labels: horas.map(h => h+':00'), datasets },
    options: { responsive: true, plugins: { legend: { labels: { color:'#aaa', boxWidth:12 } } },
      scales: { x: { ticks:{ color:'#888' }, grid:{ color:'#1e1e3f' }, stacked: false },
                y: { ticks:{ color:'#888', callback: v => v+'%' }, grid:{ color:'#1e1e3f' } } } }
  });
}

function renderStats(hist) {
  if (!hist.length) return;
  const spreads = hist.map(h => parseFloat(h.spread_pct));
  const avg = (spreads.reduce((a,b)=>a+b,0)/spreads.length).toFixed(2);
  const max = Math.max(...spreads).toFixed(2);
  const min = Math.min(...spreads).toFixed(2);
  const horaConteo = {};
  hist.forEach(h => { horaConteo[h.hora] = (horaConteo[h.hora]||[]).concat(parseFloat(h.spread_pct)); });
  const mejorHora = Object.entries(horaConteo)
    .map(([h,v]) => [h, v.reduce((a,b)=>a+b,0)/v.length])
    .sort((a,b)=>b[1]-a[1])[0];
  document.getElementById('stats-resumen').innerHTML = `
    <div class="label" style="margin-bottom:12px">Estadísticas del período</div>
    <div class="stat-row"><span class="stat-label">Total snapshots</span><span class="stat-value cyan">${hist.length}</span></div>
    <div class="stat-row"><span class="stat-label">Spread promedio</span><span class="stat-value yellow">${avg}%</span></div>
    <div class="stat-row"><span class="stat-label">Spread máximo</span><span class="stat-value green">${max}%</span></div>
    <div class="stat-row"><span class="stat-label">Spread mínimo</span><span class="stat-value red">${min}%</span></div>
    ${mejorHora ? `<div class="stat-row"><span class="stat-label">Mejor hora promedio</span><span class="stat-value green">${mejorHora[0]}:00 hs (${parseFloat(mejorHora[1]).toFixed(2)}%)</span></div>` : ''}
  `;
}

cargarDatos();
</script>
</body>
</html>
"""

# ──────────────────────────────────────────────
#  RUTAS FLASK
# ──────────────────────────────────────────────
@app.route("/")
def index():
    return render_template_string(DASHBOARD)

@app.route("/api/estado")
def api_estado():
    data = obtener_ultimo()
    for k, v in data.items():
        if hasattr(v, '__float__'):
            data[k] = float(v)
    return jsonify(data)

@app.route("/api/historial")
def api_historial():
    rows = obtener_historial()
    for row in rows:
        for k, v in row.items():
            if hasattr(v, '__float__'):
                row[k] = float(v)
            elif hasattr(v, 'isoformat'):
                row[k] = str(v)
    return jsonify(rows)

@app.route("/api/heatmap")
def api_heatmap():
    rows = obtener_heatmap()
    for row in rows:
        for k, v in row.items():
            if hasattr(v, '__float__'):
                row[k] = float(v)
    return jsonify(rows)

# ──────────────────────────────────────────────
#  INICIO
# ──────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    t = threading.Thread(target=ciclo_colector, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
