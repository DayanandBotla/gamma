import time
from flask import Flask, render_template_string
from dhanhq import dhanhq
import urllib.request
import csv
import io
from datetime import datetime

import os
from dotenv import load_dotenv

# ===== LOAD ENV =====
load_dotenv()

CLIENT_ID = os.getenv("CLIENT_ID")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")

# 🚨 HARD FAIL if missing
if not CLIENT_ID or not ACCESS_TOKEN:
    raise Exception("❌ Missing CLIENT_ID or ACCESS_TOKEN in .env")

# ===== GLOBALS =====
LAST_CE = 0
LAST_PE = 0
LAST_VALID_DATA = None
LAST_FETCH = 0

app = Flask(__name__)

dhan = dhanhq(CLIENT_ID, ACCESS_TOKEN)

STRADDLE_HISTORY = []
SPOT_HISTORY = []

DYNAMIC_CE_ID = None
DYNAMIC_PE_ID = None
LAST_ATM = 22500


# ===== SAFE FETCH =====
def safe_fetch(securities):
    global LAST_FETCH

    if time.time() - LAST_FETCH < 5:
        return None

    LAST_FETCH = time.time()

    try:
        res = dhan.quote_data(securities)

        print("API RESPONSE:", res)

        if res.get("status") == "failure":
            print("API FAILED:", res)
            return None

        return res

    except Exception as e:
        print("ERROR:", e)
        return None


# ===== ATM =====
def get_atm(spot):
    return round(spot / 50) * 50


# ===== GET IDS =====
def get_ids(atm):
    url = "https://images.dhan.co/api-data/api-scrip-master.csv"

    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req) as r:
        content = r.read().decode()

    reader = csv.DictReader(io.StringIO(content))
    rows = []

    for row in reader:
        if row.get('SEM_INSTRUMENT_NAME') != 'OPTIDX':
            continue

        if not str(row.get('SEM_CUSTOM_SYMBOL', '')).startswith("NIFTY"):
            continue

        try:
            if float(row.get('SEM_STRIKE_PRICE', 0)) != float(atm):
                continue
        except:
            continue

        rows.append(row)

    rows.sort(key=lambda x: datetime.strptime(x['SEM_EXPIRY_DATE'].split()[0], "%Y-%m-%d"))

    expiry = rows[0]['SEM_EXPIRY_DATE']

    ce, pe = None, None

    for r in rows:
        if r['SEM_EXPIRY_DATE'] == expiry:
            if r['SEM_OPTION_TYPE'] == 'CE':
                ce = r['SEM_SMST_SECURITY_ID']
            elif r['SEM_OPTION_TYPE'] == 'PE':
                pe = r['SEM_SMST_SECURITY_ID']

    return ce, pe


# ===== FETCH =====
def fetch():
    global DYNAMIC_CE_ID, DYNAMIC_PE_ID, LAST_ATM
    global LAST_CE, LAST_PE, LAST_VALID_DATA

    if not DYNAMIC_CE_ID:
        DYNAMIC_CE_ID, DYNAMIC_PE_ID = get_ids(LAST_ATM)

    sec = {"NSE_FNO": [int(DYNAMIC_CE_ID), int(DYNAMIC_PE_ID)]}

    res = safe_fetch(sec)

    if not res:
        if LAST_VALID_DATA:
            return LAST_VALID_DATA
        return 0, LAST_ATM, 0, 0, 0, 0, "API issue"

    try:
        data = res["data"]["data"]["NSE_FNO"]
    except Exception:
        print("DATA FORMAT ERROR:", res)
        if LAST_VALID_DATA:
            return LAST_VALID_DATA
        return 0, LAST_ATM, 0, 0, 0, 0, "Bad API data"

    ce = data[str(DYNAMIC_CE_ID)]["last_price"]
    pe = data[str(DYNAMIC_PE_ID)]["last_price"]

    ce_oi = data[str(DYNAMIC_CE_ID)]["oi"]
    pe_oi = data[str(DYNAMIC_PE_ID)]["oi"]

    LAST_CE = ce
    LAST_PE = pe

    spot = LAST_ATM + (ce - pe)
    new_atm = get_atm(spot)

    if new_atm != LAST_ATM:
        LAST_ATM = new_atm
        DYNAMIC_CE_ID = None

    result = (round(spot), LAST_ATM, ce, pe, ce_oi, pe_oi, "")
    LAST_VALID_DATA = result

    return result


# ===== SIGNAL =====
def get_signal():
    if len(STRADDLE_HISTORY) < 5:
        return "WAIT", 0

    change = STRADDLE_HISTORY[-1] - STRADDLE_HISTORY[0]
    pct = (change / STRADDLE_HISTORY[0]) * 100

    if pct > 2:
        return "EXPANSION", round(pct, 2)
    elif pct < -2:
        return "CONTRACTION", round(pct, 2)
    return "NEUTRAL", round(pct, 2)


# ===== GAMMA =====
def detect_gamma():
    if len(STRADDLE_HISTORY) < 3 or len(SPOT_HISTORY) < 3:
        return "NO DATA"

    speed = STRADDLE_HISTORY[-1] - STRADDLE_HISTORY[-2]
    acc = SPOT_HISTORY[-1] - SPOT_HISTORY[-3]
    imbalance = abs(LAST_CE - LAST_PE)

    if speed > 15 and abs(acc) > 30 and imbalance > 80:
        return "🔥 GAMMA ACTIVE"

    return "NORMAL"


# ===== UI =====
@app.route("/")
def home():
    spot, atm, ce, pe, co, po, err = fetch()

    if spot > 0:
        SPOT_HISTORY.append(spot)
        if len(SPOT_HISTORY) > 20:
            SPOT_HISTORY.pop(0)

    straddle = ce + pe

    if straddle > 0:
        STRADDLE_HISTORY.append(straddle)
        if len(STRADDLE_HISTORY) > 60:
            STRADDLE_HISTORY.pop(0)

    signal, change = get_signal()
    gamma = detect_gamma()

    # Premium Dark Mode Glassmorphism Theme
    gamma_class = "gamma-active" if "ACTIVE" in gamma else "gamma-normal"
    signal_col = "#10b981" if "EXPAN" in signal else "#ef4444" if "CONTR" in signal else "#8b5cf6"
    
    return render_template_string(f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>AI Trade Engine - Live Data</title>
        <meta http-equiv="refresh" content="20">
        <style>
            :root {{
                --bg-main: #0f172a;
                --bg-card: rgba(30, 41, 59, 0.7);
                --text-main: #f8fafc;
                --text-muted: #94a3b8;
                --accent-call: #10b981; /* Emerald Green */
                --accent-put: #ef4444; /* Rose Red */
                --accent-glow: #3b82f6; /* Blue Glow */
            }}
            
            body {{
                margin: 0;
                padding: 0;
                background-color: var(--bg-main);
                background-image: radial-gradient(circle at 15% 50%, rgba(59, 130, 246, 0.15), transparent 25%), 
                                  radial-gradient(circle at 85% 30%, rgba(16, 185, 129, 0.15), transparent 25%);
                color: var(--text-main);
                font-family: 'Inter', sans-serif;
                display: flex;
                flex-direction: column;
                align-items: center;
                min-height: 100vh;
            }}

            header {{
                margin-top: 3rem;
                text-align: center;
            }}

            h1 {{
                font-size: 2.5rem;
                font-weight: 800;
                margin-bottom: 0.5rem;
                background: linear-gradient(to right, #38bdf8, #818cf8);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                letter-spacing: -1px;
            }}

            .subtitle {{
                font-size: 0.9rem;
                color: var(--text-muted);
                text-transform: uppercase;
                letter-spacing: 2px;
                margin-bottom: 2rem;
            }}

            .dashboard {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
                gap: 1.5rem;
                width: 90%;
                max-width: 1000px;
            }}

            .card {{
                background: var(--bg-card);
                backdrop-filter: blur(12px);
                -webkit-backdrop-filter: blur(12px);
                border: 1px solid rgba(255, 255, 255, 0.05);
                border-radius: 16px;
                padding: 1.5rem;
                box-shadow: 0 10px 30px -10px rgba(0, 0, 0, 0.5);
                transition: transform 0.3s ease, box-shadow 0.3s ease;
            }}
            
            .card:hover {{
                transform: translateY(-5px);
                box-shadow: 0 20px 40px -15px rgba(0, 0, 0, 0.6);
            }}

            .card-title {{
                font-size: 0.85rem;
                color: var(--text-muted);
                text-transform: uppercase;
                font-weight: 600;
                letter-spacing: 1px;
                margin-bottom: 1rem;
            }}

            .metric {{
                font-size: 2.2rem;
                font-weight: 800;
                margin: 0;
            }}
            
            .metric.spot {{ color: #e2e8f0; }}
            .metric.ce {{ color: var(--accent-call); }}
            .metric.pe {{ color: var(--accent-put); }}

            .split-view {{
                display: flex;
                justify-content: space-between;
                align-items: center;
            }}
            
            .split-item {{
                text-align: center;
                flex: 1;
            }}
            
            .divider {{
                width: 1px;
                height: 40px;
                background: rgba(255,255,255,0.1);
                margin: 0 1rem;
            }}

            .gamma-card {{
                grid-column: 1 / -1;
                text-align: center;
                padding: 2rem;
            }}

            .gamma-active {{
                background: linear-gradient(135deg, rgba(239, 68, 68, 0.1), rgba(245, 158, 11, 0.1));
                border-color: rgba(245, 158, 11, 0.3);
                box-shadow: 0 0 30px rgba(245, 158, 11, 0.2);
                animation: pulse 2s infinite;
            }}

            .gamma-active h2 {{
                color: #f59e0b;
                font-size: 2.5rem;
                margin: 0;
                text-shadow: 0 0 20px rgba(245, 158, 11, 0.5);
            }}

            .gamma-normal h2 {{
                color: var(--text-muted);
                font-size: 2rem;
                margin: 0;
            }}

            @keyframes pulse {{
                0% {{ box-shadow: 0 0 20px rgba(245, 158, 11, 0.1); }}
                50% {{ box-shadow: 0 0 40px rgba(245, 158, 11, 0.4); }}
                100% {{ box-shadow: 0 0 20px rgba(245, 158, 11, 0.1); }}
            }}

            .error-banner {{
                display: {'block' if err else 'none'};
                background: rgba(239, 68, 68, 0.1);
                border-left: 4px solid var(--accent-put);
                padding: 1rem 1.5rem;
                border-radius: 8px;
                width: 90%;
                max-width: 1000px;
                margin-top: 2rem;
                color: #fca5a5;
            }}
        </style>
    </head>
    <body>

        <header>
            <h1>Nifty 50 Engine</h1>
            <div class="subtitle">Live Algorithmic Dashboard</div>
        </header>

        <div class="dashboard">
            <!-- Market Status -->
            <div class="card">
                <div class="card-title">Live Spot Price</div>
                <div class="metric spot">{spot}</div>
                <div style="margin-top: 0.5rem; color: var(--text-muted); font-size: 0.9rem;">
                    ATM Locked: <b>{atm}</b>
                </div>
            </div>

            <!-- Premium Prices -->
            <div class="card">
                <div class="card-title">Live Premiums (LTP)</div>
                <div class="split-view">
                    <div class="split-item">
                        <div style="font-size:0.8rem; color:var(--text-muted); margin-bottom:5px;">CE TICK</div>
                        <div class="metric ce">{ce}</div>
                    </div>
                    <div class="divider"></div>
                    <div class="split-item">
                        <div style="font-size:0.8rem; color:var(--text-muted); margin-bottom:5px;">PE TICK</div>
                        <div class="metric pe">{pe}</div>
                    </div>
                </div>
            </div>

            <!-- Straddle Flow -->
            <div class="card">
                <div class="card-title">Straddle Total</div>
                <div class="metric" style="color: #60a5fa;">{straddle}</div>
                <div style="margin-top: 0.5rem; font-size: 0.9rem;">
                    <span style="color: {signal_col}; font-weight: 600;">{signal}</span> 
                    <span style="color: var(--text-muted); margin-left: 8px;">({change}%)</span>
                </div>
            </div>

            <!-- Gamma Central -->
            <div class="card gamma-card {gamma_class}">
                <div class="card-title" style="margin-bottom: 0.5rem;">Gamma State</div>
                <h2>{gamma}</h2>
            </div>
        </div>

        <div class="error-banner">
            ⚠️ <b>System Notice:</b> {err}
        </div>

    </body>
    </html>
    """)


if __name__ == "__main__":
    app.run()
