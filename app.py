"""
P2P Monitor Binance — Vision Maker
Flask app con colector en background + dashboard web
"""

import requests
import pandas as pd
import threading
import time
import os
import json
from datetime import datetime
from collections import deque
from flask import Flask, jsonify, render_template_string

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
MAX_HISTORIAL   = 1000  # snapshots en memoria

URL     = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
HEADERS = {"Content-Type": "application/json"}

# Storage en memoria
historial     = deque(maxlen=MAX_HISTORIAL)
ultimo_estado = {}
lock          = threading.Lock()

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
            "completadas":int(trade.get("tradeCount", 0)),
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
    while True:
        try:
            compradores = parsear_y_filtrar(obtener_anuncios("BUY"),  "BUY")
            vendedores  = parsear_y_filtrar(obtener_anuncios("SELL"), "SELL")
            estado      = analizar(compradores, vendedores)
            if estado:
                with lock:
                    historial.append(estado)
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
  body { background: #0d0d1a; color: #fff; font-family: 'Segoe UI', sans-serif; min-height: 100vh; }
  header { background: #12122a; padding: 16px 24px; border-bottom: 1px solid #1e1e3f;
           display: flex; justify-content: space-between; align-items: center; }
  header h1 { color: #00d4ff; font-size: 1.1rem; }
  header span { color: #888; font-size: 0.8rem; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
          gap: 16px; padding: 20px; }
  .card { background: #12122a; border: 1px solid #1e1e3f; border-radius: 12px; padding: 16px; }
  .card .label { color: #888; font-size: 0.75rem; text-transform: uppercase; margin-bottom: 6px; }
  .card .value { font-size: 1.4rem; font-weight: bold; }
  .card .sub   { color: #888; font-size: 0.75rem; margin-top: 4px; }
  .green  { color: #00e676; }
  .yellow { color: #ffd740; }
  .orange { color: #ff9100; }
  .red    { color: #ff5252; }
  .cyan   { color: #00d4ff; }
  .estado-banner { margin: 0 20px; padding: 14px 20px; border-radius: 10px;
                   font-size: 1rem; font-weight: bold; text-align: center; }
  .banner-green  { background: #00e67622; border: 1px solid #00e676; color: #00e676; }
  .banner-yellow { background: #ffd74022; border: 1px solid #ffd740; color: #ffd740; }
  .banner-orange { background: #ff910022; border: 1px solid #ff9100; color: #ff9100; }
  .banner-red    { background: #ff525222; border: 1px solid #ff5252; color: #ff5252; }
  .seccion { padding: 20px; }
  .seccion h2 { color: #00d4ff; font-size: 0.9rem; text-transform: uppercase;
                letter-spacing: 1px; margin-bottom: 16px; }
  .chart-wrap { background: #12122a; border: 1px solid #1e1e3f; border-radius: 12px;
                padding: 16px; }
  .precios { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin: 0 20px 20px; }
  .precio-card { background: #12122a; border: 1px solid #1e1e3f; border-radius: 12px;
                 padding: 16px; text-align: center; }
  .precio-card .tipo { font-size: 0.7rem; color: #888; text-transform: uppercase; margin-bottom: 8px; }
  .precio-card .precio { font-size: 1.6rem; font-weight: bold; }
  .precio-card .nota { font-size: 0.7rem; color: #888; margin-top: 6px; }
  .refresh { color: #888; font-size: 0.7rem; text-align: center; padding: 10px; }
  #countdown { color: #00d4ff; }
  .sin-datos { text-align: center; padding: 60px; color: #888; }
</style>
</head>
<body>

<header>
  <h1>📊 P2P Monitor — Unión Austral Capital</h1>
  <span id="ultima-act">Cargando...</span>
</header>

<div id="contenido">
  <div class="sin-datos">⏳ Cargando datos del mercado...</div>
</div>

<div class="refresh">Actualización automática en <span id="countdown">30</span>s</div>

<script>
let cuenta = 30;
setInterval(() => {
  cuenta--;
  document.getElementById('countdown').textContent = cuenta;
  if (cuenta <= 0) { cuenta = 30; cargarDatos(); }
}, 1000);

async function cargarDatos() {
  try {
    const [estado, hist] = await Promise.all([
      fetch('/api/estado').then(r => r.json()),
      fetch('/api/historial').then(r => r.json())
    ]);
    if (!estado.timestamp) {
      document.getElementById('contenido').innerHTML =
        '<div class="sin-datos">⏳ Esperando primer ciclo de datos (5 min)...</div>';
      return;
    }
    renderDashboard(estado, hist);
    document.getElementById('ultima-act').textContent = 'Última actualización: ' + estado.timestamp;
  } catch(e) {
    console.error(e);
  }
}

function colorClass(color) {
  return color === 'green' ? 'green' : color === 'yellow' ? 'yellow' :
         color === 'orange' ? 'orange' : 'red';
}

function renderDashboard(e, hist) {
  const cc = colorClass(e.color);
  const bannerClass = 'banner-' + e.color;

  const html = `
    <div style="height:16px"></div>

    <div class="estado-banner ${bannerClass}">${e.estado} — Spread ${e.spread_pct.toFixed(2)}%</div>

    <div class="grid">
      <div class="card">
        <div class="label">Mejor comprador</div>
        <div class="value green">$${e.mejor_comprador.toLocaleString('es-CL')}</div>
        <div class="sub">${e.n_compradores} anuncios (+200 USDT)</div>
      </div>
      <div class="card">
        <div class="label">Mejor vendedor</div>
        <div class="value red">$${e.mejor_vendedor.toLocaleString('es-CL')}</div>
        <div class="sub">${e.n_vendedores} anuncios (+200 USDT)</div>
      </div>
      <div class="card">
        <div class="label">Liquidez compra</div>
        <div class="value cyan">${e.liq_compra_usdt.toLocaleString('es-CL')} USDT</div>
        <div class="sub">Disponible en mercado</div>
      </div>
      <div class="card">
        <div class="label">Liquidez venta</div>
        <div class="value cyan">${e.liq_venta_usdt.toLocaleString('es-CL')} USDT</div>
        <div class="sub">Disponible en mercado</div>
      </div>
      <div class="card">
        <div class="label">Ganancia neta est.</div>
        <div class="value ${e.ganancia_neta_pct > 0 ? 'green' : 'red'}">${e.ganancia_neta_pct.toFixed(2)}%</div>
        <div class="sub">Descontada comisión 0.40%</div>
      </div>
      <div class="card">
        <div class="label">Snapshots hoy</div>
        <div class="value cyan">${hist.length}</div>
        <div class="sub">Últimos registros</div>
      </div>
    </div>

    <div class="precios">
      <div class="precio-card">
        <div class="tipo">Precio sugerido si vas a VENDER USDT</div>
        <div class="precio yellow">$${e.precio_sug_venta.toLocaleString('es-CL')}</div>
        <div class="nota">Un peso menos que el mejor vendedor actual</div>
      </div>
      <div class="precio-card">
        <div class="tipo">Precio sugerido si vas a COMPRAR USDT</div>
        <div class="precio yellow">$${e.precio_sug_compra.toLocaleString('es-CL')}</div>
        <div class="nota">Un peso más que el mejor comprador actual</div>
      </div>
    </div>

    <div class="seccion">
      <h2>Spread histórico</h2>
      <div class="chart-wrap">
        <canvas id="chartSpread" height="80"></canvas>
      </div>
    </div>
  `;

  document.getElementById('contenido').innerHTML = html;
  renderChart(hist);
}

function renderChart(hist) {
  if (hist.length < 2) return;
  const labels = hist.map(h => h.timestamp.slice(11,16));
  const data   = hist.map(h => h.spread_pct);
  const ctx    = document.getElementById('chartSpread').getContext('2d');
  new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        label: 'Spread %',
        data,
        borderColor: '#00d4ff',
        backgroundColor: 'rgba(0,212,255,0.08)',
        borderWidth: 2,
        pointRadius: 2,
        tension: 0.3,
        fill: true,
      }]
    },
    options: {
      responsive: true,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: '#888', maxTicksLimit: 10 }, grid: { color: '#1e1e3f' } },
        y: { ticks: { color: '#888', callback: v => v.toFixed(2) + '%' }, grid: { color: '#1e1e3f' } }
      }
    }
  });
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
    with lock:
        return jsonify(dict(ultimo_estado))

@app.route("/api/historial")
def api_historial():
    with lock:
        return jsonify(list(historial))

# ──────────────────────────────────────────────
#  INICIO
# ──────────────────────────────────────────────
if __name__ == "__main__":
    t = threading.Thread(target=ciclo_colector, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
