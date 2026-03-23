import time
from flask import Flask, render_template_string
from dhanhq import dhanhq
import urllib.request
import csv
import io
from datetime import datetime

app = Flask(__name__)


# ===== CONFIG (USE ENV — DON’T HARDCORE TOKENS AGAIN) =====
CLIENT_ID = "1108455416"
ACCESS_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzUxMiJ9.eyJpc3MiOiJkaGFuIiwicGFydG5lcklkIjoiIiwiZXhwIjoxNzc0MzIwNDkzLCJpYXQiOjE3NzQyMzQwOTMsInRva2VuQ29uc3VtZXJUeXBlIjoiU0VMRiIsIndlYmhvb2tVcmwiOiIiLCJkaGFuQ2xpZW50SWQiOiIxMTA4NDU1NDE2In0.LZCZj6XnoIrz70tFY7HJ1nIED6JqeykyW_cuY6Yc53BZrTUNoP5iT21guZFVUu7jYyC3Y4z6-1LhGQk-a4L_Lw"


dhan = dhanhq(CLIENT_ID, ACCESS_TOKEN)

STRADDLE_HISTORY = []
DYNAMIC_CE_ID = None
DYNAMIC_PE_ID = None
LAST_ATM = 22500
LAST_FETCH = 0


# ===== SAFE FETCH =====
def safe_fetch(securities):
    global LAST_FETCH

    if time.time() - LAST_FETCH < 2:
        return None

    LAST_FETCH = time.time()

    try:
        res = dhan.quote_data(securities)
        if res.get("status") == "failure":
            return None
        return res
    except:
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


# ===== FETCH DATA =====
def fetch():
    global DYNAMIC_CE_ID, DYNAMIC_PE_ID, LAST_ATM

    if not DYNAMIC_CE_ID:
        DYNAMIC_CE_ID, DYNAMIC_PE_ID = get_ids(LAST_ATM)

    sec = {"NSE_FNO": [int(DYNAMIC_CE_ID), int(DYNAMIC_PE_ID)]}

    res = safe_fetch(sec)

    if not res:
        return 0, LAST_ATM, 0, 0, 0, 0, "API issue"

    data = res["data"]["data"]["NSE_FNO"]

    ce = data[str(DYNAMIC_CE_ID)]["last_price"]
    pe = data[str(DYNAMIC_PE_ID)]["last_price"]

    ce_oi = data[str(DYNAMIC_CE_ID)]["oi"]
    pe_oi = data[str(DYNAMIC_PE_ID)]["oi"]

    # ===== DERIVED SPOT =====
    spot = LAST_ATM + (ce - pe)

    new_atm = get_atm(spot)

    if new_atm != LAST_ATM:
        LAST_ATM = new_atm
        DYNAMIC_CE_ID = None

    return round(spot), LAST_ATM, ce, pe, ce_oi, pe_oi, ""


# ===== STRADDLE =====
def calc_straddle(c, p):
    return c + p


def get_signal():
    if len(STRADDLE_HISTORY) < 5:
        return "WAIT", 0

    change = STRADDLE_HISTORY[-1] - STRADDLE_HISTORY[0]
    pct = (change / STRADDLE_HISTORY[0]) * 100

    if pct > 2:
        return "EXPANSION", round(pct, 2)
    elif pct < -2:
        return "CONTRACTION", round(pct, 2)
    else:
        return "NEUTRAL", round(pct, 2)


# ===== DIRECTION =====
def get_direction(c, p, co, po):
    if c > p and po > co:
        return "CE", 80
    elif p > c and co > po:
        return "PE", 80
    else:
        return "NONE", 50


# ===== FINAL TRADE ENGINE =====
def get_trade(signal, direction):
    if signal == "EXPANSION" and direction == "CE":
        return "BUY CE"
    elif signal == "EXPANSION" and direction == "PE":
        return "BUY PE"
    else:
        return "NO TRADE"


# ===== UI =====
@app.route("/")
def home():
    spot, atm, ce, pe, co, po, err = fetch()

    straddle = calc_straddle(ce, pe)

    if straddle > 0:
        STRADDLE_HISTORY.append(straddle)
        if len(STRADDLE_HISTORY) > 60:
            STRADDLE_HISTORY.pop(0)

    signal, change = get_signal()
    direction, conf = get_direction(ce, pe, co, po)
    trade = get_trade(signal, direction)

    return render_template_string(f"""
    <h2>AI Trading Engine</h2>

    <p><b>Spot:</b> {spot}</p>
    <p><b>ATM:</b> {atm}</p>

    <p><b>CE:</b> {ce} | <b>PE:</b> {pe}</p>

    <p><b>Straddle:</b> {straddle}</p>
    <p><b>Change:</b> {change}%</p>
    <p><b>Signal:</b> {signal}</p>

    <p><b>Direction:</b> {direction}</p>
    <p><b>Confidence:</b> {conf}%</p>

    <h2 style="color:green;">{trade}</h2>

    <p style="color:red;">{err}</p>

    <meta http-equiv="refresh" content="15">
    """)


if __name__ == "__main__":
    app.run(debug=True)