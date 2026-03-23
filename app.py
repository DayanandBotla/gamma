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

    return render_template_string(f"""
    <h2>AI Trading Engine</h2>

    <p>Spot: {spot}</p>
    <p>ATM: {atm}</p>

    <p>CE: {ce} | PE: {pe}</p>

    <p>Straddle: {straddle}</p>
    <p>Signal: {signal}</p>

    <h3>{gamma}</h3>

    <p style="color:red;">{err}</p>

    <meta http-equiv="refresh" content="30">
    """)


if __name__ == "__main__":
    app.run()