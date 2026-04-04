"""
Gamma Blast Terminal — Nifty 50
FastAPI | Live ATM Options Monitor | Tight Multi-Filter Gamma Detection
Config editable via Frontend Settings Panel
"""

import os, json, time, csv, io, urllib.request
from datetime import datetime, date
from zoneinfo import ZoneInfo
from collections import deque
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from dhanhq import dhanhq
from dotenv import load_dotenv

load_dotenv()

# ── Credentials (env only — never hardcode) ──────────────────
CLIENT_ID    = os.getenv("CLIENT_ID", "")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN", "")

if not CLIENT_ID or not ACCESS_TOKEN:
    raise SystemExit("❌ Missing CLIENT_ID or ACCESS_TOKEN in .env")

# ── Config file (editable via FE) ────────────────────────────
CONFIG_PATH = Path("config.json")
DEFAULT_CONFIG = {
    # Gamma detection thresholds
    "gamma_straddle_velocity":  8,     # Min pts change per refresh to score
    "gamma_spot_acceleration":  20,    # Min spot move over 3 readings
    "gamma_imbalance_pts":      50,    # Min CE-PE premium gap
    "gamma_straddle_pct":       1.2,   # Min % straddle expansion (5 readings)
    "gamma_oi_ratio":           1.25,  # Min OI skew ratio (CE_OI/PE_OI)
    "gamma_score_threshold":    60,    # Score (0-100) to call GAMMA ACTIVE

    # Signal thresholds
    "signal_expansion_pct":     1.8,   # % straddle rise = EXPANSION
    "signal_contraction_pct":  -1.8,   # % straddle drop = CONTRACTION

    # Fetch interval
    "fetch_interval_sec":       5,     # API poll interval (min 5s)

    # Market hours (IST)
    "market_open":  "09:15",
    "market_close": "15:30",

    # History window
    "history_window": 60,              # Max data points to keep
}

def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text())
            return {**DEFAULT_CONFIG, **saved}
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()

def save_config(cfg: dict):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))

# ── State ─────────────────────────────────────────────────────
IST = ZoneInfo("Asia/Kolkata")
dhan = dhanhq(CLIENT_ID, ACCESS_TOKEN)

straddle_history: deque = deque(maxlen=60)
spot_history:     deque = deque(maxlen=60)
oi_history:       deque = deque(maxlen=60)
signal_log:       deque = deque(maxlen=100)

DYNAMIC_CE_ID   = None
DYNAMIC_PE_ID   = None
LAST_ATM        = 0
LAST_CE         = 0.0
LAST_PE         = 0.0
LAST_CE_OI      = 0
LAST_PE_OI      = 0
LAST_FETCH_TS   = 0.0
LAST_VALID_DATA = None
SCRIP_CACHE     = {}   # atm → (ce_id, pe_id, expiry)
SCRIP_CACHE_DATE = None

# ── Market hours ──────────────────────────────────────────────
def is_market_open(cfg: dict) -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:        # Saturday/Sunday
        return False
    t    = now.strftime("%H:%M")
    return cfg["market_open"] <= t <= cfg["market_close"]

# ── Scrip master (cached per day per ATM) ─────────────────────
def get_ids(atm: int) -> tuple[str, str, str]:
    global SCRIP_CACHE, SCRIP_CACHE_DATE
    today = date.today()

    if SCRIP_CACHE_DATE != today:
        SCRIP_CACHE.clear()
        SCRIP_CACHE_DATE = today

    if atm in SCRIP_CACHE:
        return SCRIP_CACHE[atm]

    print(f"[ScripMaster] Fetching IDs for ATM {atm}...")
    url = "https://images.dhan.co/api-data/api-scrip-master.csv"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        content = r.read().decode()

    reader = csv.DictReader(io.StringIO(content))
    rows = []
    for row in reader:
        if row.get("SEM_INSTRUMENT_NAME") != "OPTIDX":
            continue
        sym = str(row.get("SEM_CUSTOM_SYMBOL", ""))
        if not sym.startswith("NIFTY") or "BANK" in sym or "FIN" in sym or "MID" in sym:
            continue  # Nifty 50 only — exclude BankNifty, FinNifty, MidcpNifty
        try:
            if float(row.get("SEM_STRIKE_PRICE", 0)) != float(atm):
                continue
        except Exception:
            continue
        rows.append(row)

    rows.sort(key=lambda x: datetime.strptime(x["SEM_EXPIRY_DATE"].split()[0], "%Y-%m-%d"))
    if not rows:
        raise ValueError(f"No Nifty 50 contracts found for ATM {atm}")

    expiry = rows[0]["SEM_EXPIRY_DATE"]
    ce_id = pe_id = None
    for r in rows:
        if r["SEM_EXPIRY_DATE"] == expiry:
            if r["SEM_OPTION_TYPE"] == "CE":
                ce_id = r["SEM_SMST_SECURITY_ID"]
            elif r["SEM_OPTION_TYPE"] == "PE":
                pe_id = r["SEM_SMST_SECURITY_ID"]

    if not ce_id or not pe_id:
        raise ValueError(f"Missing CE/PE for ATM {atm}, expiry {expiry}")

    expiry_str = expiry.split()[0]
    SCRIP_CACHE[atm] = (ce_id, pe_id, expiry_str)
    print(f"[ScripMaster] ATM {atm} → CE:{ce_id} PE:{pe_id} Expiry:{expiry_str}")
    return ce_id, pe_id, expiry_str

# ── ATM helper ────────────────────────────────────────────────
def get_atm(spot: float) -> int:
    return round(spot / 50) * 50

# ── Safe Dhan fetch ───────────────────────────────────────────
def safe_fetch(securities: dict):
    global LAST_FETCH_TS
    cfg = load_config()
    min_interval = max(cfg["fetch_interval_sec"], 5)

    if time.time() - LAST_FETCH_TS < min_interval:
        return None

    LAST_FETCH_TS = time.time()
    try:
        res = dhan.quote_data(securities)
        if res.get("status") == "failure":
            print("[Dhan] API failure:", res)
            return None
        return res
    except Exception as e:
        print("[Dhan] Exception:", e)
        return None

# ── Main fetch ────────────────────────────────────────────────
def fetch_data():
    global DYNAMIC_CE_ID, DYNAMIC_PE_ID, LAST_ATM
    global LAST_CE, LAST_PE, LAST_CE_OI, LAST_PE_OI, LAST_VALID_DATA

    # Bootstrap ATM if not set
    if LAST_ATM == 0:
        LAST_ATM = 24000  # reasonable starting point — will auto-correct

    if not DYNAMIC_CE_ID:
        try:
            DYNAMIC_CE_ID, DYNAMIC_PE_ID, expiry = get_ids(LAST_ATM)
        except Exception as e:
            print("[fetch_data] get_ids failed:", e)
            return LAST_VALID_DATA or _empty_result("ScripMaster error")

    sec = {"NSE_FNO": [int(DYNAMIC_CE_ID), int(DYNAMIC_PE_ID)]}
    res = safe_fetch(sec)

    if not res:
        return LAST_VALID_DATA or _empty_result("Rate limited / API issue")

    try:
        data = res["data"]["data"]["NSE_FNO"]
    except Exception:
        print("[fetch_data] Bad data format:", res)
        return LAST_VALID_DATA or _empty_result("Bad API response")

    try:
        ce      = float(data[str(DYNAMIC_CE_ID)]["last_price"])
        pe      = float(data[str(DYNAMIC_PE_ID)]["last_price"])
        ce_oi   = int(data[str(DYNAMIC_CE_ID)].get("oi", 0))
        pe_oi   = int(data[str(DYNAMIC_PE_ID)].get("oi", 0))
        ce_vol  = int(data[str(DYNAMIC_CE_ID)].get("volume", 0))
        pe_vol  = int(data[str(DYNAMIC_PE_ID)].get("volume", 0))
    except Exception as e:
        print("[fetch_data] Parse error:", e)
        return LAST_VALID_DATA or _empty_result("Parse error")

    LAST_CE    = ce
    LAST_PE    = pe
    LAST_CE_OI = ce_oi
    LAST_PE_OI = pe_oi

    # Derive spot from put-call parity: spot ≈ ATM + (CE - PE)
    spot    = LAST_ATM + (ce - pe)
    new_atm = get_atm(spot)

    if new_atm != LAST_ATM:
        print(f"[ATM Shift] {LAST_ATM} → {new_atm}")
        LAST_ATM      = new_atm
        DYNAMIC_CE_ID = None   # will refetch on next call

    _, _, expiry_str = SCRIP_CACHE.get(LAST_ATM, (None, None, "?"))

    result = {
        "spot":     round(spot),
        "atm":      LAST_ATM,
        "ce":       round(ce, 2),
        "pe":       round(pe, 2),
        "ce_oi":    ce_oi,
        "pe_oi":    pe_oi,
        "ce_vol":   ce_vol,
        "pe_vol":   pe_vol,
        "straddle": round(ce + pe, 2),
        "expiry":   expiry_str,
        "error":    "",
        "ts":       datetime.now(IST).strftime("%H:%M:%S"),
    }
    LAST_VALID_DATA = result
    return result

def _empty_result(error: str) -> dict:
    return {
        "spot": 0, "atm": LAST_ATM, "ce": 0, "pe": 0,
        "ce_oi": 0, "pe_oi": 0, "ce_vol": 0, "pe_vol": 0,
        "straddle": 0, "expiry": "?", "error": error,
        "ts": datetime.now(IST).strftime("%H:%M:%S"),
    }

# ── Gamma Detection (Multi-Filter Score) ─────────────────────
def compute_gamma_score(cfg: dict) -> dict:
    """
    5-filter gamma scoring system. Each filter = 20 pts (max 100).
    Score >= threshold → GAMMA ACTIVE.

    Filters:
      F1 — Straddle velocity   : rapid premium expansion per tick
      F2 — Spot acceleration   : spot moving fast in one direction
      F3 — Premium imbalance   : CE vs PE gap (directional pressure)
      F4 — Straddle % change   : cumulative expansion over 5 readings
      F5 — OI pressure         : OI ratio skewed (smart money positioning)
    """
    score   = 0
    details = {}

    # F1 — Straddle velocity
    f1_val = 0
    if len(straddle_history) >= 2:
        f1_val = straddle_history[-1] - straddle_history[-2]
        if f1_val >= cfg["gamma_straddle_velocity"]:
            score += 20
    details["f1_velocity"] = {"val": round(f1_val, 2), "threshold": cfg["gamma_straddle_velocity"], "hit": f1_val >= cfg["gamma_straddle_velocity"]}

    # F2 — Spot acceleration
    f2_val = 0
    if len(spot_history) >= 3:
        f2_val = abs(spot_history[-1] - spot_history[-3])
        if f2_val >= cfg["gamma_spot_acceleration"]:
            score += 20
    details["f2_acceleration"] = {"val": round(f2_val, 2), "threshold": cfg["gamma_spot_acceleration"], "hit": f2_val >= cfg["gamma_spot_acceleration"]}

    # F3 — Premium imbalance
    f3_val = abs(LAST_CE - LAST_PE)
    if f3_val >= cfg["gamma_imbalance_pts"]:
        score += 20
    details["f3_imbalance"] = {"val": round(f3_val, 2), "threshold": cfg["gamma_imbalance_pts"], "hit": f3_val >= cfg["gamma_imbalance_pts"]}

    # F4 — Straddle % expansion
    f4_val = 0
    if len(straddle_history) >= 5 and straddle_history[0] > 0:
        f4_val = ((straddle_history[-1] - straddle_history[-5]) / straddle_history[-5]) * 100
        if f4_val >= cfg["gamma_straddle_pct"]:
            score += 20
    details["f4_pct_change"] = {"val": round(f4_val, 2), "threshold": cfg["gamma_straddle_pct"], "hit": f4_val >= cfg["gamma_straddle_pct"]}

    # F5 — OI pressure ratio
    f5_val = 0
    if LAST_CE_OI > 0 and LAST_PE_OI > 0:
        ratio    = max(LAST_CE_OI, LAST_PE_OI) / min(LAST_CE_OI, LAST_PE_OI)
        f5_val   = round(ratio, 2)
        if f5_val >= cfg["gamma_oi_ratio"]:
            score += 20
    details["f5_oi_ratio"] = {"val": f5_val, "threshold": cfg["gamma_oi_ratio"], "hit": f5_val >= cfg["gamma_oi_ratio"]}

    active = score >= cfg["gamma_score_threshold"]
    state  = "🔥 GAMMA ACTIVE" if active else ("⚡ BUILDING" if score >= 40 else "NORMAL")

    return {
        "score":   score,
        "state":   state,
        "active":  active,
        "filters": details,
        "filters_hit": sum(1 for f in details.values() if f["hit"]),
    }

# ── Signal ────────────────────────────────────────────────────
def get_signal(cfg: dict) -> dict:
    if len(straddle_history) < 5:
        return {"signal": "WAIT", "change_pct": 0, "direction": "neutral"}

    base   = straddle_history[-5]
    latest = straddle_history[-1]
    pct    = ((latest - base) / base) * 100 if base > 0 else 0

    if pct >= cfg["signal_expansion_pct"]:
        return {"signal": "EXPANSION", "change_pct": round(pct, 2), "direction": "up"}
    elif pct <= cfg["signal_contraction_pct"]:
        return {"signal": "CONTRACTION", "change_pct": round(pct, 2), "direction": "down"}
    return {"signal": "NEUTRAL", "change_pct": round(pct, 2), "direction": "neutral"}

# ── FastAPI app ───────────────────────────────────────────────
app = FastAPI(title="Gamma Blast Terminal")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/api/data")
async def api_data():
    cfg    = load_config()
    market = is_market_open(cfg)

    data = fetch_data()

    if data["spot"] > 0:
        spot_history.append(data["spot"])
    if data["straddle"] > 0:
        straddle_history.append(data["straddle"])
    if data["ce_oi"] > 0 or data["pe_oi"] > 0:
        oi_history.append({"ce": data["ce_oi"], "pe": data["pe_oi"]})

    gamma  = compute_gamma_score(cfg)
    signal = get_signal(cfg)

    # Log significant events
    if gamma["active"]:
        signal_log.appendleft({
            "ts":    data["ts"],
            "event": "GAMMA ACTIVE",
            "score": gamma["score"],
            "spot":  data["spot"],
        })

    return JSONResponse({
        **data,
        "gamma":       gamma,
        "signal":      signal,
        "market_open": market,
        "history": {
            "straddle": list(straddle_history)[-30:],
            "spot":     list(spot_history)[-30:],
        },
        "log": list(signal_log)[:10],
    })

@app.get("/api/config")
async def get_config():
    return JSONResponse(load_config())

@app.post("/api/config")
async def update_config(request: Request):
    try:
        body   = await request.json()
        cfg    = load_config()
        # Only update known keys, validate types
        for k, v in body.items():
            if k in DEFAULT_CONFIG:
                expected = type(DEFAULT_CONFIG[k])
                cfg[k]   = expected(v)
        save_config(cfg)
        return JSONResponse({"status": "ok", "config": cfg})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)

@app.get("/api/config/reset")
async def reset_config():
    save_config(DEFAULT_CONFIG.copy())
    return JSONResponse({"status": "reset", "config": DEFAULT_CONFIG})

# ── Frontend ──────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gamma Blast — Nifty 50</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Bebas+Neue&display=swap" rel="stylesheet">
<style>
:root {
  --bg:       #080b12;
  --surface:  #0e1320;
  --card:     #111827;
  --border:   #1f2937;
  --text:     #e2e8f0;
  --muted:    #4b5563;
  --green:    #10b981;
  --red:      #ef4444;
  --amber:    #f59e0b;
  --blue:     #3b82f6;
  --cyan:     #06b6d4;
  --glow-g:   rgba(16,185,129,0.15);
  --glow-r:   rgba(239,68,68,0.15);
  --glow-a:   rgba(245,158,11,0.25);
}
* { box-sizing:border-box; margin:0; padding:0; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: 'Space Mono', monospace;
  font-size: 13px;
  min-height: 100vh;
  overflow-x: hidden;
}

/* ── SCAN LINE effect ─────────────────── */
body::before {
  content: '';
  position: fixed; inset: 0;
  background: repeating-linear-gradient(
    0deg,
    transparent,
    transparent 2px,
    rgba(0,0,0,0.03) 2px,
    rgba(0,0,0,0.03) 4px
  );
  pointer-events: none;
  z-index: 9999;
}

/* ── HEADER ───────────────────────────── */
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 24px;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
}
.logo {
  font-family: 'Bebas Neue', sans-serif;
  font-size: 28px;
  letter-spacing: 3px;
  background: linear-gradient(90deg, var(--amber), var(--red));
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
}
.header-right { display: flex; align-items: center; gap: 12px; }
.market-pill {
  padding: 4px 12px;
  border-radius: 20px;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 1px;
  text-transform: uppercase;
}
.market-pill.open  { background: rgba(16,185,129,0.15); color: var(--green); border: 1px solid rgba(16,185,129,0.3); }
.market-pill.closed { background: rgba(239,68,68,0.15); color: var(--red);   border: 1px solid rgba(239,68,68,0.3); }
.settings-btn {
  background: var(--card);
  border: 1px solid var(--border);
  color: var(--text);
  padding: 6px 14px;
  border-radius: 6px;
  cursor: pointer;
  font-family: 'Space Mono', monospace;
  font-size: 12px;
  transition: all 0.2s;
}
.settings-btn:hover { border-color: var(--amber); color: var(--amber); }

/* ── GAMMA HERO ───────────────────────── */
.gamma-hero {
  text-align: center;
  padding: 32px 24px 20px;
  position: relative;
  transition: all 0.4s;
}
.gamma-hero.active {
  background: radial-gradient(ellipse at center, var(--glow-a) 0%, transparent 70%);
}
.gamma-hero.building {
  background: radial-gradient(ellipse at center, rgba(59,130,246,0.1) 0%, transparent 70%);
}
.gamma-state {
  font-family: 'Bebas Neue', sans-serif;
  font-size: 56px;
  letter-spacing: 6px;
  line-height: 1;
  transition: color 0.3s;
}
.gamma-state.active   { color: var(--amber); text-shadow: 0 0 40px rgba(245,158,11,0.6); animation: flicker 2s infinite; }
.gamma-state.building { color: var(--blue);  text-shadow: 0 0 20px rgba(59,130,246,0.4); }
.gamma-state.normal   { color: var(--muted); }

@keyframes flicker {
  0%,100% { opacity:1; }
  92%      { opacity:1; }
  93%      { opacity:0.85; }
  94%      { opacity:1; }
}

/* Score bar */
.score-row { display: flex; align-items: center; justify-content: center; gap: 16px; margin-top: 12px; }
.score-label { color: var(--muted); font-size: 11px; letter-spacing: 1px; }
.score-bar-wrap { width: 200px; height: 6px; background: var(--border); border-radius: 3px; overflow: hidden; }
.score-bar { height: 100%; border-radius: 3px; transition: width 0.5s ease; }
.score-val { font-size: 18px; font-weight: 700; min-width: 36px; }

/* Filter dots */
.filter-row { display: flex; justify-content: center; gap: 8px; margin-top: 10px; }
.f-dot {
  width: 10px; height: 10px; border-radius: 50%;
  background: var(--border);
  transition: all 0.3s;
  position: relative;
}
.f-dot.hit { background: var(--green); box-shadow: 0 0 8px var(--green); }
.f-dot::after {
  content: attr(data-label);
  position: absolute;
  bottom: -18px;
  left: 50%;
  transform: translateX(-50%);
  font-size: 9px;
  color: var(--muted);
  white-space: nowrap;
  letter-spacing: 0.5px;
}

/* ── GRID ─────────────────────────────── */
.grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 1px;
  background: var(--border);
  border-top: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
}
@media (max-width: 768px) { .grid { grid-template-columns: repeat(2,1fr); } }

.cell {
  background: var(--card);
  padding: 18px 20px;
}
.cell-label {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 1.5px;
  color: var(--muted);
  margin-bottom: 6px;
}
.cell-val {
  font-size: 28px;
  font-weight: 700;
  font-family: 'Bebas Neue', sans-serif;
  letter-spacing: 1px;
  line-height: 1;
}
.cell-val.spot     { color: var(--text); }
.cell-val.ce-val   { color: var(--green); }
.cell-val.pe-val   { color: var(--red); }
.cell-val.straddle { color: var(--cyan); }
.cell-sub { font-size: 11px; color: var(--muted); margin-top: 4px; }

/* ── SIGNAL BAR ───────────────────────── */
.signal-bar {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 12px 24px;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
}
.sig-label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 1px; }
.sig-val { font-size: 14px; font-weight: 700; letter-spacing: 1px; }
.sig-val.expansion   { color: var(--green); }
.sig-val.contraction { color: var(--red); }
.sig-val.neutral     { color: var(--muted); }
.sig-val.wait        { color: var(--muted); }
.sig-pct { font-size: 12px; color: var(--muted); }
.sig-sep { color: var(--border); }
.expiry-tag {
  margin-left: auto;
  font-size: 11px;
  color: var(--muted);
  background: var(--card);
  padding: 3px 10px;
  border-radius: 4px;
  border: 1px solid var(--border);
}

/* ── OI ROW ───────────────────────────── */
.oi-row {
  display: grid;
  grid-template-columns: 1fr 60px 1fr;
  gap: 0;
  padding: 14px 24px;
  background: var(--card);
  border-bottom: 1px solid var(--border);
  align-items: center;
}
.oi-bar-wrap { display: flex; flex-direction: column; gap: 4px; }
.oi-label { font-size: 10px; letter-spacing: 1px; text-transform: uppercase; color: var(--muted); }
.oi-track { height: 5px; background: var(--border); border-radius: 3px; overflow: hidden; }
.oi-fill-ce { height: 100%; background: var(--green); border-radius: 3px; transition: width 0.5s; }
.oi-fill-pe { height: 100%; background: var(--red);   border-radius: 3px; transition: width 0.5s; }
.oi-num { font-size: 13px; font-weight: 700; }
.oi-num.ce { color: var(--green); }
.oi-num.pe { color: var(--red); }
.oi-mid { text-align: center; }
.oi-ratio-val { font-size: 16px; font-weight: 700; color: var(--amber); }
.oi-ratio-label { font-size: 9px; color: var(--muted); letter-spacing: 1px; }

/* ── BOTTOM ROW ───────────────────────── */
.bottom { display: grid; grid-template-columns: 1fr 1fr; gap: 1px; background: var(--border); }
@media (max-width: 600px) { .bottom { grid-template-columns: 1fr; } }

/* Log panel */
.log-panel { background: var(--card); padding: 16px 20px; }
.log-title { font-size: 10px; text-transform: uppercase; letter-spacing: 2px; color: var(--muted); margin-bottom: 10px; }
.log-item { display: flex; gap: 10px; padding: 5px 0; border-bottom: 1px solid var(--border); }
.log-ts { color: var(--muted); font-size: 11px; min-width: 65px; }
.log-event { color: var(--amber); font-size: 11px; flex: 1; }
.log-score { color: var(--muted); font-size: 11px; }
.log-empty { color: var(--muted); font-size: 12px; font-style: italic; }

/* Filter detail panel */
.filter-panel { background: var(--card); padding: 16px 20px; }
.filter-item {
  display: flex; align-items: center; justify-content: space-between;
  padding: 7px 0;
  border-bottom: 1px solid var(--border);
}
.filter-name { font-size: 11px; color: var(--muted); }
.filter-vals { font-size: 11px; display: flex; gap: 10px; align-items: center; }
.filter-current { color: var(--text); }
.filter-thresh  { color: var(--muted); }
.filter-badge {
  font-size: 10px; font-weight: 700; padding: 2px 7px;
  border-radius: 3px; letter-spacing: 0.5px;
}
.filter-badge.hit  { background: rgba(16,185,129,0.15); color: var(--green); }
.filter-badge.miss { background: rgba(75,85,99,0.2);    color: var(--muted); }

/* ── ERROR ────────────────────────────── */
.error-bar {
  padding: 8px 24px;
  background: rgba(239,68,68,0.08);
  border-bottom: 1px solid rgba(239,68,68,0.2);
  color: #fca5a5;
  font-size: 12px;
  display: none;
}
.error-bar.show { display: block; }

/* ── UPDATE pulse ─────────────────────── */
.ts-dot {
  display: inline-block;
  width: 7px; height: 7px;
  background: var(--green);
  border-radius: 50%;
  margin-right: 6px;
  animation: blink 1s ease-in-out;
}
@keyframes blink {
  0% { opacity:1; } 50% { opacity:0.2; } 100% { opacity:1; }
}

/* ── SETTINGS DRAWER ─────────────────── */
.overlay {
  position: fixed; inset: 0;
  background: rgba(0,0,0,0.7);
  z-index: 100;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.3s;
}
.overlay.open { opacity: 1; pointer-events: all; }
.drawer {
  position: fixed;
  top: 0; right: -420px;
  width: 420px; height: 100vh;
  background: var(--surface);
  border-left: 1px solid var(--border);
  z-index: 101;
  overflow-y: auto;
  transition: right 0.3s cubic-bezier(0.4,0,0.2,1);
  padding: 24px;
}
.drawer.open { right: 0; }
.drawer-header {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 24px;
}
.drawer-title {
  font-family: 'Bebas Neue', sans-serif;
  font-size: 22px;
  letter-spacing: 3px;
  color: var(--amber);
}
.close-btn {
  background: var(--card); border: 1px solid var(--border);
  color: var(--text); width: 32px; height: 32px;
  border-radius: 6px; cursor: pointer; font-size: 16px;
  display: flex; align-items: center; justify-content: center;
}
.section-head {
  font-size: 10px; text-transform: uppercase;
  letter-spacing: 2px; color: var(--amber);
  margin: 20px 0 10px;
  padding-bottom: 6px;
  border-bottom: 1px solid var(--border);
}
.field { margin-bottom: 14px; }
.field label {
  display: block; font-size: 11px;
  color: var(--muted); margin-bottom: 5px;
  letter-spacing: 0.5px;
}
.field input {
  width: 100%;
  background: var(--card);
  border: 1px solid var(--border);
  color: var(--text);
  padding: 8px 10px;
  border-radius: 6px;
  font-family: 'Space Mono', monospace;
  font-size: 13px;
  transition: border-color 0.2s;
}
.field input:focus { outline: none; border-color: var(--amber); }
.field .hint { font-size: 10px; color: var(--muted); margin-top: 3px; }
.btn-row { display: flex; gap: 10px; margin-top: 24px; }
.btn {
  flex: 1; padding: 10px; border-radius: 6px;
  font-family: 'Space Mono', monospace;
  font-size: 12px; font-weight: 700;
  cursor: pointer; border: none;
  letter-spacing: 0.5px; transition: all 0.2s;
}
.btn-save { background: var(--amber); color: #000; }
.btn-save:hover { opacity: 0.85; }
.btn-reset { background: var(--card); color: var(--muted); border: 1px solid var(--border); }
.btn-reset:hover { border-color: var(--red); color: var(--red); }
.save-toast {
  margin-top: 12px; padding: 10px; border-radius: 6px;
  background: rgba(16,185,129,0.1); border: 1px solid rgba(16,185,129,0.3);
  color: var(--green); font-size: 12px; text-align: center;
  display: none;
}
.save-toast.show { display: block; }
</style>
</head>
<body>

<div class="overlay" id="overlay" onclick="closeSettings()"></div>

<!-- Settings Drawer -->
<div class="drawer" id="drawer">
  <div class="drawer-header">
    <div class="drawer-title">CONFIG</div>
    <button class="close-btn" onclick="closeSettings()">✕</button>
  </div>

  <div class="section-head">🔥 Gamma Detection</div>
  <div class="field">
    <label>Straddle Velocity Threshold (pts/tick)</label>
    <input type="number" id="cfg_gamma_straddle_velocity" step="0.5">
    <div class="hint">Min premium change per refresh to score. Default: 8</div>
  </div>
  <div class="field">
    <label>Spot Acceleration (pts over 3 readings)</label>
    <input type="number" id="cfg_gamma_spot_acceleration" step="1">
    <div class="hint">Spot must move this fast. Default: 20</div>
  </div>
  <div class="field">
    <label>Premium Imbalance (CE-PE gap, pts)</label>
    <input type="number" id="cfg_gamma_imbalance_pts" step="5">
    <div class="hint">Directional premium skew. Default: 50</div>
  </div>
  <div class="field">
    <label>Straddle % Expansion (5 readings)</label>
    <input type="number" id="cfg_gamma_straddle_pct" step="0.1">
    <div class="hint">Cumulative expansion filter. Default: 1.2</div>
  </div>
  <div class="field">
    <label>OI Skew Ratio</label>
    <input type="number" id="cfg_gamma_oi_ratio" step="0.05">
    <div class="hint">CE_OI/PE_OI or inverse. Default: 1.25</div>
  </div>
  <div class="field">
    <label>Score Threshold to Call ACTIVE (0–100)</label>
    <input type="number" id="cfg_gamma_score_threshold" step="5" min="20" max="100">
    <div class="hint">5 filters × 20pts each. Default: 60 (3/5 filters)</div>
  </div>

  <div class="section-head">📊 Signal Thresholds</div>
  <div class="field">
    <label>Expansion Signal %</label>
    <input type="number" id="cfg_signal_expansion_pct" step="0.1">
    <div class="hint">Straddle rise % = EXPANSION signal. Default: 1.8</div>
  </div>
  <div class="field">
    <label>Contraction Signal % (negative)</label>
    <input type="number" id="cfg_signal_contraction_pct" step="0.1">
    <div class="hint">Straddle drop %. Default: -1.8</div>
  </div>

  <div class="section-head">⚙️ System</div>
  <div class="field">
    <label>Fetch Interval (seconds, min 5)</label>
    <input type="number" id="cfg_fetch_interval_sec" step="1" min="5">
    <div class="hint">API poll rate. Dhan rate limit safe at 5s+</div>
  </div>
  <div class="field">
    <label>Market Open (IST, HH:MM)</label>
    <input type="text" id="cfg_market_open" placeholder="09:15">
  </div>
  <div class="field">
    <label>Market Close (IST, HH:MM)</label>
    <input type="text" id="cfg_market_close" placeholder="15:30">
  </div>

  <div class="btn-row">
    <button class="btn btn-save" onclick="saveConfig()">SAVE CONFIG</button>
    <button class="btn btn-reset" onclick="resetConfig()">RESET DEFAULT</button>
  </div>
  <div class="save-toast" id="saveToast">✅ Config saved & applied</div>
</div>

<!-- HEADER -->
<div class="header">
  <div class="logo">⚡ GAMMA BLAST</div>
  <div class="header-right">
    <div id="marketPill" class="market-pill closed">CLOSED</div>
    <div id="clockDisplay" style="color:var(--muted);font-size:11px"></div>
    <button class="settings-btn" onclick="openSettings()">⚙ SETTINGS</button>
  </div>
</div>

<!-- ERROR BAR -->
<div class="error-bar" id="errorBar"></div>

<!-- GAMMA HERO -->
<div class="gamma-hero" id="gammaHero">
  <div class="gamma-state normal" id="gammaState">LOADING...</div>
  <div class="score-row">
    <div class="score-label">GAMMA SCORE</div>
    <div class="score-bar-wrap">
      <div class="score-bar" id="scoreBar" style="width:0%;background:var(--muted)"></div>
    </div>
    <div class="score-val" id="scoreVal" style="color:var(--muted)">0/100</div>
  </div>
  <div class="filter-row" id="filterDots">
    <div class="f-dot" id="fd1" data-label="VEL"></div>
    <div class="f-dot" id="fd2" data-label="ACC"></div>
    <div class="f-dot" id="fd3" data-label="IMB"></div>
    <div class="f-dot" id="fd4" data-label="PCT"></div>
    <div class="f-dot" id="fd5" data-label="OI"></div>
  </div>
  <div style="margin-top:22px"></div>
</div>

<!-- SIGNAL BAR -->
<div class="signal-bar">
  <div class="sig-label">Signal</div>
  <div class="sig-val neutral" id="signalVal">WAIT</div>
  <div class="sig-pct" id="signalPct"></div>
  <div class="sig-sep">|</div>
  <div class="sig-label">ATM</div>
  <div style="font-size:14px;font-weight:700;color:var(--cyan)" id="atmVal">—</div>
  <div class="sig-sep">|</div>
  <div class="sig-label">Updated</div>
  <div style="font-size:11px;color:var(--muted)" id="tsVal">—</div>
  <div class="expiry-tag" id="expiryTag">Expiry: —</div>
</div>

<!-- PRICE GRID -->
<div class="grid">
  <div class="cell">
    <div class="cell-label">Nifty 50 Spot</div>
    <div class="cell-val spot" id="spotVal">—</div>
    <div class="cell-sub" id="spotSub"></div>
  </div>
  <div class="cell">
    <div class="cell-label">CE Premium</div>
    <div class="cell-val ce-val" id="ceVal">—</div>
    <div class="cell-sub" id="ceSub">Vol: —</div>
  </div>
  <div class="cell">
    <div class="cell-label">PE Premium</div>
    <div class="cell-val pe-val" id="peVal">—</div>
    <div class="cell-sub" id="peSub">Vol: —</div>
  </div>
  <div class="cell">
    <div class="cell-label">Straddle</div>
    <div class="cell-val straddle" id="straddleVal">—</div>
    <div class="cell-sub" id="straddleSub"></div>
  </div>
</div>

<!-- OI ROW -->
<div class="oi-row">
  <div class="oi-bar-wrap">
    <div class="oi-label" style="color:var(--green)">CE Open Interest</div>
    <div class="oi-track"><div class="oi-fill-ce" id="ceOiBar" style="width:50%"></div></div>
    <div class="oi-num ce" id="ceOiVal">—</div>
  </div>
  <div class="oi-mid">
    <div class="oi-ratio-val" id="oiRatioVal">—</div>
    <div class="oi-ratio-label">OI RATIO</div>
  </div>
  <div class="oi-bar-wrap" style="text-align:right">
    <div class="oi-label" style="color:var(--red)">PE Open Interest</div>
    <div class="oi-track"><div class="oi-fill-pe" id="peOiBar" style="width:50%"></div></div>
    <div class="oi-num pe" id="peOiVal">—</div>
  </div>
</div>

<!-- BOTTOM -->
<div class="bottom">
  <!-- Gamma Log -->
  <div class="log-panel">
    <div class="log-title">📋 Event Log</div>
    <div id="logList"><div class="log-empty">Waiting for signals...</div></div>
  </div>

  <!-- Filter Detail -->
  <div class="filter-panel">
    <div class="log-title">🔬 Filter Detail</div>
    <div id="filterDetail">
      <div class="filter-item"><div class="filter-name">F1 Straddle Velocity</div><div class="filter-vals"><span class="filter-current" id="fv1">—</span><span class="filter-badge miss" id="fb1">MISS</span></div></div>
      <div class="filter-item"><div class="filter-name">F2 Spot Acceleration</div><div class="filter-vals"><span class="filter-current" id="fv2">—</span><span class="filter-badge miss" id="fb2">MISS</span></div></div>
      <div class="filter-item"><div class="filter-name">F3 Premium Imbalance</div><div class="filter-vals"><span class="filter-current" id="fv3">—</span><span class="filter-badge miss" id="fb3">MISS</span></div></div>
      <div class="filter-item"><div class="filter-name">F4 Straddle % (5 ticks)</div><div class="filter-vals"><span class="filter-current" id="fv4">—</span><span class="filter-badge miss" id="fb4">MISS</span></div></div>
      <div class="filter-item"><div class="filter-name">F5 OI Skew Ratio</div><div class="filter-vals"><span class="filter-current" id="fv5">—</span><span class="filter-badge miss" id="fb5">MISS</span></div></div>
    </div>
  </div>
</div>

<script>
let config = {};
let prevStraddle = 0;

// ── Clock ─────────────────────────────
function updateClock() {
  const now = new Date().toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false });
  document.getElementById('clockDisplay').textContent = now + ' IST';
}
setInterval(updateClock, 1000);
updateClock();

// ── Format numbers ─────────────────────
function fmtOI(v) {
  if (v >= 1e7) return (v/1e7).toFixed(2) + 'Cr';
  if (v >= 1e5) return (v/1e5).toFixed(2) + 'L';
  return v.toLocaleString('en-IN');
}

// ── Main data update ───────────────────
async function fetchData() {
  try {
    const res  = await fetch('/api/data');
    const d    = await res.json();

    // Market pill
    const pill = document.getElementById('marketPill');
    pill.textContent = d.market_open ? 'LIVE' : 'CLOSED';
    pill.className   = 'market-pill ' + (d.market_open ? 'open' : 'closed');

    // Error bar
    const eb = document.getElementById('errorBar');
    if (d.error) { eb.textContent = '⚠ ' + d.error; eb.classList.add('show'); }
    else { eb.classList.remove('show'); }

    // Gamma hero
    const g = d.gamma;
    const heroEl    = document.getElementById('gammaHero');
    const stateEl   = document.getElementById('gammaState');
    const scoreBar  = document.getElementById('scoreBar');
    const scoreVal  = document.getElementById('scoreVal');

    stateEl.textContent = g.state;
    stateEl.className   = 'gamma-state ' + (g.active ? 'active' : g.score >= 40 ? 'building' : 'normal');
    heroEl.className    = 'gamma-hero '  + (g.active ? 'active' : g.score >= 40 ? 'building' : '');

    const pct = g.score;
    scoreBar.style.width = pct + '%';
    scoreBar.style.background = pct >= 60 ? 'var(--amber)' : pct >= 40 ? 'var(--blue)' : 'var(--muted)';
    scoreVal.textContent  = pct + '/100';
    scoreVal.style.color  = pct >= 60 ? 'var(--amber)' : pct >= 40 ? 'var(--blue)' : 'var(--muted)';

    // Filter dots
    const fkeys = Object.values(g.filters);
    ['fd1','fd2','fd3','fd4','fd5'].forEach((id,i) => {
      const el = document.getElementById(id);
      el.className = 'f-dot ' + (fkeys[i]?.hit ? 'hit' : '');
    });

    // Filter detail
    const fnames = Object.keys(g.filters);
    fnames.forEach((k, i) => {
      const f   = g.filters[k];
      const idx = i + 1;
      const fv  = document.getElementById('fv' + idx);
      const fb  = document.getElementById('fb' + idx);
      if (fv) fv.textContent = f.val + ' / ' + f.threshold;
      if (fb) {
        fb.textContent  = f.hit ? 'HIT' : 'MISS';
        fb.className    = 'filter-badge ' + (f.hit ? 'hit' : 'miss');
      }
    });

    // Signal
    const sig  = d.signal;
    const sigEl = document.getElementById('signalVal');
    sigEl.textContent = sig.signal;
    sigEl.className   = 'sig-val ' + sig.direction.replace('up','expansion').replace('down','contraction');
    document.getElementById('signalPct').textContent = sig.change_pct !== 0 ? sig.change_pct + '%' : '';

    // Prices
    if (d.spot) {
      document.getElementById('spotVal').textContent     = d.spot.toLocaleString('en-IN');
      document.getElementById('atmVal').textContent      = d.atm.toLocaleString('en-IN');
      document.getElementById('ceVal').textContent       = d.ce;
      document.getElementById('peVal').textContent       = d.pe;
      document.getElementById('straddleVal').textContent = d.straddle;

      // Straddle change indicator
      if (prevStraddle > 0) {
        const chg = d.straddle - prevStraddle;
        const sign = chg > 0 ? '+' : '';
        document.getElementById('straddleSub').textContent = sign + chg.toFixed(1) + ' this tick';
        document.getElementById('straddleSub').style.color = chg > 0 ? 'var(--green)' : chg < 0 ? 'var(--red)' : 'var(--muted)';
      }
      prevStraddle = d.straddle;

      document.getElementById('ceSub').textContent = 'Vol: ' + fmtOI(d.ce_vol);
      document.getElementById('peSub').textContent = 'Vol: ' + fmtOI(d.pe_vol);
      document.getElementById('spotSub').textContent = 'Expiry: ' + d.expiry;
    }

    // OI
    if (d.ce_oi > 0 || d.pe_oi > 0) {
      const total = d.ce_oi + d.pe_oi || 1;
      document.getElementById('ceOiVal').textContent = fmtOI(d.ce_oi);
      document.getElementById('peOiVal').textContent = fmtOI(d.pe_oi);
      document.getElementById('ceOiBar').style.width = (d.ce_oi / total * 100) + '%';
      document.getElementById('peOiBar').style.width = (d.pe_oi / total * 100) + '%';
      const ratio = d.ce_oi > d.pe_oi ? (d.ce_oi / d.pe_oi).toFixed(2) + ':1 CE' : (d.pe_oi / d.ce_oi).toFixed(2) + ':1 PE';
      document.getElementById('oiRatioVal').textContent = ratio;
    }

    // Expiry + timestamp
    document.getElementById('expiryTag').textContent = 'Expiry: ' + (d.expiry || '?');
    document.getElementById('tsVal').innerHTML = '<span class="ts-dot"></span>' + d.ts;

    // Event log
    const logEl = document.getElementById('logList');
    if (d.log && d.log.length > 0) {
      logEl.innerHTML = d.log.map(l => `
        <div class="log-item">
          <div class="log-ts">${l.ts}</div>
          <div class="log-event">${l.event}</div>
          <div class="log-score">${l.score}/100 @ ${l.spot}</div>
        </div>`).join('');
    }

  } catch(e) {
    console.error('Fetch error:', e);
  }
}

// ── Settings ────────────────────────────
async function openSettings() {
  const res = await fetch('/api/config');
  config = await res.json();
  // Populate fields
  Object.keys(config).forEach(k => {
    const el = document.getElementById('cfg_' + k);
    if (el) el.value = config[k];
  });
  document.getElementById('overlay').classList.add('open');
  document.getElementById('drawer').classList.add('open');
}

function closeSettings() {
  document.getElementById('overlay').classList.remove('open');
  document.getElementById('drawer').classList.remove('open');
}

async function saveConfig() {
  const body = {};
  Object.keys(config).forEach(k => {
    const el = document.getElementById('cfg_' + k);
    if (el) body[k] = el.value;
  });
  const res = await fetch('/api/config', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  const data = await res.json();
  const toast = document.getElementById('saveToast');
  toast.textContent = data.status === 'ok' ? '✅ Config saved & applied' : '❌ Error: ' + data.message;
  toast.classList.add('show');
  setTimeout(() => toast.classList.remove('show'), 3000);
}

async function resetConfig() {
  if (!confirm('Reset all settings to default?')) return;
  await fetch('/api/config/reset');
  await openSettings();
}

// ── Poll loop ─────────────────────────
fetchData();
setInterval(fetchData, 5000);
</script>
</body>
</html>"""

@app.get("/", response_class=HTMLResponse)
async def root():
    return HTML
