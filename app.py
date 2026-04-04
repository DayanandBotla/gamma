import time
from flask import Flask, render_template_string, request, jsonify
from dhanhq import dhanhq
import urllib.request
import csv
import io
from datetime import datetime
import os
from dotenv import load_dotenv
from pathlib import Path

# ===== LOAD ENV =====
load_dotenv()

CLIENT_ID    = os.getenv("CLIENT_ID", "")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN", "")

# ===== GLOBALS =====
LAST_CE = 0
LAST_PE = 0
LAST_VALID_DATA = None
LAST_FETCH = 0

app  = Flask(__name__)
dhan = None

STRADDLE_HISTORY = []
SPOT_HISTORY     = []

DYNAMIC_CE_ID = None
DYNAMIC_PE_ID = None
LAST_ATM      = 22500

ENV_PATH = Path(".env")


def init_dhan():
    global dhan
    if CLIENT_ID and ACCESS_TOKEN:
        try:
            dhan = dhanhq(CLIENT_ID, ACCESS_TOKEN)
            print(f"✅ Dhan initialized | Client: {CLIENT_ID[:6]}***")
        except Exception as e:
            print(f"❌ Dhan init failed: {e}")
            dhan = None
    else:
        dhan = None


init_dhan()


# ===== SAVE .ENV =====
def save_env(client_id, access_token):
    ENV_PATH.write_text(f'CLIENT_ID="{client_id}"\nACCESS_TOKEN="{access_token}"\n')


# ===== SAFE FETCH =====
def safe_fetch(securities):
    global LAST_FETCH
    if time.time() - LAST_FETCH < 5:
        return None
    LAST_FETCH = time.time()
    if not dhan:
        return None
    try:
        res = dhan.quote_data(securities)
        if res.get("status") == "failure":
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
    rows   = []
    for row in reader:
        if row.get('SEM_INSTRUMENT_NAME') != 'OPTIDX':
            continue
        sym = str(row.get('SEM_CUSTOM_SYMBOL', ''))
        if not sym.startswith("NIFTY") or "BANK" in sym or "FIN" in sym or "MID" in sym:
            continue
        try:
            if float(row.get('SEM_STRIKE_PRICE', 0)) != float(atm):
                continue
        except:
            continue
        rows.append(row)

    rows.sort(key=lambda x: datetime.strptime(x['SEM_EXPIRY_DATE'].split()[0], "%Y-%m-%d"))
    expiry = rows[0]['SEM_EXPIRY_DATE']
    ce = pe = None
    for r in rows:
        if r['SEM_EXPIRY_DATE'] == expiry:
            if r['SEM_OPTION_TYPE'] == 'CE': ce = r['SEM_SMST_SECURITY_ID']
            elif r['SEM_OPTION_TYPE'] == 'PE': pe = r['SEM_SMST_SECURITY_ID']
    return ce, pe


# ===== FETCH =====
def fetch():
    global DYNAMIC_CE_ID, DYNAMIC_PE_ID, LAST_ATM
    global LAST_CE, LAST_PE, LAST_VALID_DATA

    if not dhan:
        return 0, LAST_ATM, 0, 0, 0, 0, "No credentials — update via Settings"

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
    except:
        if LAST_VALID_DATA:
            return LAST_VALID_DATA
        return 0, LAST_ATM, 0, 0, 0, 0, "Bad API data"

    ce    = data[str(DYNAMIC_CE_ID)]["last_price"]
    pe    = data[str(DYNAMIC_PE_ID)]["last_price"]
    ce_oi = data[str(DYNAMIC_CE_ID)]["oi"]
    pe_oi = data[str(DYNAMIC_PE_ID)]["oi"]

    LAST_CE = ce
    LAST_PE = pe

    spot    = LAST_ATM + (ce - pe)
    new_atm = get_atm(spot)

    if new_atm != LAST_ATM:
        LAST_ATM      = new_atm
        DYNAMIC_CE_ID = None

    result = (round(spot), LAST_ATM, ce, pe, ce_oi, pe_oi, "")
    LAST_VALID_DATA = result
    return result


# ===== SIGNAL =====
def get_signal():
    if len(STRADDLE_HISTORY) < 5:
        return "WAIT", 0
    change = STRADDLE_HISTORY[-1] - STRADDLE_HISTORY[0]
    pct    = (change / STRADDLE_HISTORY[0]) * 100
    if pct > 2:   return "EXPANSION",   round(pct, 2)
    if pct < -2:  return "CONTRACTION", round(pct, 2)
    return "NEUTRAL", round(pct, 2)


# ===== GAMMA =====
def detect_gamma():
    if len(STRADDLE_HISTORY) < 3 or len(SPOT_HISTORY) < 3:
        return "NO DATA"
    speed     = STRADDLE_HISTORY[-1] - STRADDLE_HISTORY[-2]
    acc       = SPOT_HISTORY[-1] - SPOT_HISTORY[-3]
    imbalance = abs(LAST_CE - LAST_PE)
    if speed > 15 and abs(acc) > 30 and imbalance > 80:
        return "🔥 GAMMA ACTIVE"
    return "NORMAL"


# ===== TOKEN UPDATE ROUTE =====
@app.route("/update_credentials", methods=["POST"])
def update_credentials():
    global CLIENT_ID, ACCESS_TOKEN, DYNAMIC_CE_ID, DYNAMIC_PE_ID
    cid   = request.form.get("client_id", "").strip()
    token = request.form.get("access_token", "").strip()
    if not cid or not token:
        return jsonify({"status": "error", "message": "Both Client ID and Token required"})
    CLIENT_ID    = cid
    ACCESS_TOKEN = token
    os.environ["CLIENT_ID"]    = cid
    os.environ["ACCESS_TOKEN"] = token
    save_env(cid, token)
    DYNAMIC_CE_ID = None
    DYNAMIC_PE_ID = None
    init_dhan()
    return jsonify({"status": "success", "message": f"✅ Credentials saved | Client: {cid[:6]}***"})


# ===== UI =====
@app.route("/")
def home():
    spot, atm, ce, pe, co, po, err = fetch()

    if spot > 0:
        SPOT_HISTORY.append(spot)
        if len(SPOT_HISTORY) > 20: SPOT_HISTORY.pop(0)

    straddle = ce + pe
    if straddle > 0:
        STRADDLE_HISTORY.append(straddle)
        if len(STRADDLE_HISTORY) > 60: STRADDLE_HISTORY.pop(0)

    signal, change = get_signal()
    gamma          = detect_gamma()

    gamma_class  = "gamma-active" if "ACTIVE" in gamma else "gamma-normal"
    signal_col   = "#10b981" if "EXPAN" in signal else "#ef4444" if "CONTR" in signal else "#8b5cf6"
    has_creds    = bool(CLIENT_ID and ACCESS_TOKEN)
    masked_id    = (CLIENT_ID[:4] + "***") if CLIENT_ID else "Not set"

    return render_template_string(f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Gamma Blast — Nifty 50</title>
        <meta http-equiv="refresh" content="10">
        <style>
            :root {{
                --bg: #0f172a; --card: rgba(30,41,59,0.7);
                --text: #f8fafc; --muted: #94a3b8;
                --green: #10b981; --red: #ef4444;
                --blue: #3b82f6; --amber: #f59e0b;
            }}
            * {{ box-sizing: border-box; margin:0; padding:0; }}
            body {{
                background: var(--bg);
                background-image: radial-gradient(circle at 15% 50%, rgba(59,130,246,0.1), transparent 25%),
                                  radial-gradient(circle at 85% 30%, rgba(16,185,129,0.1), transparent 25%);
                color: var(--text);
                font-family: 'Segoe UI', sans-serif;
                min-height: 100vh;
                padding-bottom: 40px;
            }}
            header {{ padding: 24px 32px 16px; border-bottom: 1px solid rgba(255,255,255,0.05); display:flex; align-items:center; justify-content:space-between; }}
            h1 {{ font-size: 1.8rem; font-weight:800; background: linear-gradient(to right,#38bdf8,#818cf8); -webkit-background-clip:text; -webkit-text-fill-color:transparent; }}
            .cred-badge {{
                font-size: 12px; padding: 4px 12px; border-radius: 20px;
                background: {'rgba(16,185,129,0.15)' if has_creds else 'rgba(239,68,68,0.15)'};
                color: {'var(--green)' if has_creds else 'var(--red)'};
                border: 1px solid {'rgba(16,185,129,0.3)' if has_creds else 'rgba(239,68,68,0.3)'};
                cursor: pointer;
            }}
            .dashboard {{ display:grid; grid-template-columns: repeat(auto-fit,minmax(260px,1fr)); gap:1.2rem; width:90%; max-width:1000px; margin:24px auto 0; }}
            .card {{
                background: var(--card);
                backdrop-filter: blur(12px);
                border: 1px solid rgba(255,255,255,0.05);
                border-radius: 16px;
                padding: 1.5rem;
                box-shadow: 0 10px 30px -10px rgba(0,0,0,0.5);
            }}
            .card-title {{ font-size: 0.8rem; color: var(--muted); text-transform: uppercase; letter-spacing:1px; margin-bottom: 0.8rem; }}
            .metric {{ font-size: 2.2rem; font-weight:800; }}
            .metric.spot {{ color:#e2e8f0; }}
            .metric.ce   {{ color: var(--green); }}
            .metric.pe   {{ color: var(--red); }}
            .split-view  {{ display:flex; justify-content:space-between; align-items:center; }}
            .split-item  {{ text-align:center; flex:1; }}
            .divider     {{ width:1px; height:40px; background:rgba(255,255,255,0.1); margin:0 1rem; }}
            .gamma-card  {{ grid-column: 1 / -1; text-align:center; padding:2rem; }}
            .gamma-active {{ background: linear-gradient(135deg,rgba(239,68,68,0.1),rgba(245,158,11,0.1)); border-color:rgba(245,158,11,0.3); animation:pulse 2s infinite; }}
            .gamma-active h2 {{ color: var(--amber); font-size:2.5rem; text-shadow:0 0 20px rgba(245,158,11,0.5); }}
            .gamma-normal h2 {{ color: var(--muted); font-size:2rem; }}
            @keyframes pulse {{ 0%,100% {{ box-shadow:0 0 20px rgba(245,158,11,0.1); }} 50% {{ box-shadow:0 0 40px rgba(245,158,11,0.4); }} }}
            .error-banner {{ display:{'block' if err else 'none'}; background:rgba(239,68,68,0.1); border-left:4px solid var(--red); padding:1rem 1.5rem; border-radius:8px; width:90%; max-width:1000px; margin:16px auto 0; color:#fca5a5; }}

            /* Settings Modal */
            .overlay {{ position:fixed; inset:0; background:rgba(0,0,0,0.7); z-index:100; display:none; }}
            .overlay.show {{ display:flex; align-items:center; justify-content:center; }}
            .modal {{ background:#1e293b; border:1px solid rgba(255,255,255,0.1); border-radius:16px; padding:28px; width:420px; max-width:95vw; }}
            .modal h2 {{ font-size:1.2rem; margin-bottom:20px; color: var(--amber); }}
            .field {{ margin-bottom:16px; }}
            .field label {{ display:block; font-size:12px; color:var(--muted); margin-bottom:6px; text-transform:uppercase; letter-spacing:0.5px; }}
            .field input {{ width:100%; background:#0f172a; border:1px solid rgba(255,255,255,0.1); color:var(--text); padding:10px 12px; border-radius:8px; font-size:14px; }}
            .field input:focus {{ outline:none; border-color:var(--amber); }}
            .btn-row {{ display:flex; gap:10px; margin-top:20px; }}
            .btn {{ flex:1; padding:10px; border-radius:8px; font-size:14px; font-weight:700; cursor:pointer; border:none; }}
            .btn-save  {{ background:var(--amber); color:#000; }}
            .btn-close {{ background:rgba(255,255,255,0.05); color:var(--muted); }}
            .toast {{ margin-top:12px; padding:10px; border-radius:8px; font-size:13px; display:none; }}
            .toast.ok  {{ background:rgba(16,185,129,0.1); border:1px solid rgba(16,185,129,0.3); color:var(--green); }}
            .toast.err {{ background:rgba(239,68,68,0.1); border:1px solid rgba(239,68,68,0.3); color:var(--red); }}
        </style>
    </head>
    <body>

    <!-- Settings Modal -->
    <div class="overlay" id="overlay">
        <div class="modal">
            <h2>⚙️ Dhan Credentials</h2>
            <div class="field">
                <label>Client ID</label>
                <input type="text" id="inp_client_id" placeholder="Your Dhan Client ID" value="{masked_id if has_creds else ''}">
            </div>
            <div class="field">
                <label>Access Token (JWT)</label>
                <input type="password" id="inp_access_token" placeholder="Paste today's access token">
            </div>
            <div class="btn-row">
                <button class="btn btn-save" onclick="saveCredentials()">SAVE & APPLY</button>
                <button class="btn btn-close" onclick="closeModal()">CANCEL</button>
            </div>
            <div class="toast" id="credToast"></div>
        </div>
    </div>

    <header>
        <h1>⚡ Gamma Blast · Nifty 50</h1>
        <div class="cred-badge" onclick="openModal()">
            {'✅ ' + masked_id if has_creds else '❌ Set Credentials'}
        </div>
    </header>

    <div class="error-banner">⚠️ <b>Notice:</b> {err}</div>

    <div class="dashboard">
        <div class="card">
            <div class="card-title">Live Spot</div>
            <div class="metric spot">{spot}</div>
            <div style="margin-top:0.5rem;color:var(--muted);font-size:0.9rem;">ATM: <b>{atm}</b></div>
        </div>

        <div class="card">
            <div class="card-title">Premiums (LTP)</div>
            <div class="split-view">
                <div class="split-item">
                    <div style="font-size:0.8rem;color:var(--muted);margin-bottom:5px;">CE</div>
                    <div class="metric ce">{ce}</div>
                </div>
                <div class="divider"></div>
                <div class="split-item">
                    <div style="font-size:0.8rem;color:var(--muted);margin-bottom:5px;">PE</div>
                    <div class="metric pe">{pe}</div>
                </div>
            </div>
        </div>

        <div class="card">
            <div class="card-title">Straddle</div>
            <div class="metric" style="color:#60a5fa;">{straddle}</div>
            <div style="margin-top:0.5rem;font-size:0.9rem;">
                <span style="color:{signal_col};font-weight:600;">{signal}</span>
                <span style="color:var(--muted);margin-left:8px;">({change}%)</span>
            </div>
        </div>

        <div class="card gamma-card {gamma_class}">
            <div class="card-title">Gamma State</div>
            <h2>{gamma}</h2>
        </div>
    </div>

    <script>
        function openModal()  {{ document.getElementById('overlay').classList.add('show'); }}
        function closeModal() {{ document.getElementById('overlay').classList.remove('show'); }}

        function saveCredentials() {{
            const cid   = document.getElementById('inp_client_id').value.trim();
            const token = document.getElementById('inp_access_token').value.trim();
            const toast = document.getElementById('credToast');

            if (!cid || !token) {{
                toast.textContent = '❌ Both fields required';
                toast.className = 'toast err';
                return;
            }}

            fetch('update_credentials', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                body: 'client_id=' + encodeURIComponent(cid) + '&access_token=' + encodeURIComponent(token)
            }})
            .then(r => r.json())
            .then(d => {{
                toast.textContent = d.message;
                toast.className = 'toast ' + (d.status === 'success' ? 'ok' : 'err');
                if (d.status === 'success') setTimeout(() => location.reload(), 1500);
            }});
        }}

        document.getElementById('overlay').addEventListener('click', function(e) {{
            if (e.target === this) closeModal();
        }});
    </script>
    </body>
    </html>
    """)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8002, debug=False)
