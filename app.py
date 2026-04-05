"""
Gamma Blast Terminal — Nifty 50
FastAPI | Mobile Responsive | localStorage Token | 5-Filter Gamma Score
Port: 8002
"""
import os, time, csv, io, urllib.request, json
from datetime import datetime, date
from zoneinfo import ZoneInfo
from collections import deque
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from dhanhq import dhanhq

IST = ZoneInfo("Asia/Kolkata")
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── State ─────────────────────────────────────────────────────
dhan            = None
CLIENT_ID       = ""
straddle_hist   = deque(maxlen=60)
spot_hist       = deque(maxlen=60)
LAST_CE         = 0.0
LAST_PE         = 0.0
LAST_CE_OI      = 0
LAST_PE_OI      = 0
LAST_ATM        = 0
CE_ID           = None
PE_ID           = None
LAST_FETCH_TS   = 0.0
LAST_VALID      = None
SCRIP_CACHE     = {}
SCRIP_DATE      = None
event_log       = deque(maxlen=20)

# ── Config (tunable thresholds) ───────────────────────────────
CFG = {
    "vel_thresh":   8.0,   # F1 straddle velocity
    "acc_thresh":   20.0,  # F2 spot acceleration
    "imb_thresh":   50.0,  # F3 CE-PE imbalance
    "pct_thresh":   1.2,   # F4 straddle % over 5 ticks
    "oi_thresh":    1.25,  # F5 OI skew ratio
    "score_thresh": 60,    # min score to call ACTIVE
}

def ist_now(): return datetime.now(IST)
def is_market():
    t = ist_now()
    if t.weekday() >= 5: return False
    s = t.strftime("%H:%M")
    return "09:15" <= s <= "15:30"

def get_atm(spot): return round(spot / 50) * 50

def get_ids(atm):
    global SCRIP_CACHE, SCRIP_DATE
    today = date.today()
    if SCRIP_DATE != today: SCRIP_CACHE.clear(); SCRIP_DATE = today
    if atm in SCRIP_CACHE: return SCRIP_CACHE[atm]
    url = "https://images.dhan.co/api-data/api-scrip-master.csv"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        content = r.read().decode()
    rows = []
    for row in csv.DictReader(io.StringIO(content)):
        if row.get("SEM_INSTRUMENT_NAME") != "OPTIDX": continue
        sym = str(row.get("SEM_CUSTOM_SYMBOL", ""))
        if not sym.startswith("NIFTY") or "BANK" in sym or "FIN" in sym or "MID" in sym: continue
        try:
            if float(row.get("SEM_STRIKE_PRICE", 0)) != float(atm): continue
        except: continue
        rows.append(row)
    rows.sort(key=lambda x: datetime.strptime(x["SEM_EXPIRY_DATE"].split()[0], "%Y-%m-%d"))
    if not rows: raise ValueError(f"No contracts for ATM {atm}")
    expiry = rows[0]["SEM_EXPIRY_DATE"]
    ce_id = pe_id = None
    for r in rows:
        if r["SEM_EXPIRY_DATE"] == expiry:
            if r["SEM_OPTION_TYPE"] == "CE": ce_id = r["SEM_SMST_SECURITY_ID"]
            elif r["SEM_OPTION_TYPE"] == "PE": pe_id = r["SEM_SMST_SECURITY_ID"]
    SCRIP_CACHE[atm] = (ce_id, pe_id, expiry.split()[0])
    return ce_id, pe_id, expiry.split()[0]

def fetch_data():
    global CE_ID, PE_ID, LAST_ATM, LAST_CE, LAST_PE, LAST_CE_OI, LAST_PE_OI
    global LAST_FETCH_TS, LAST_VALID
    if not dhan: return {"error": "No credentials — click ⚙ Settings"}
    if time.time() - LAST_FETCH_TS < 5: return LAST_VALID
    LAST_FETCH_TS = time.time()
    if LAST_ATM == 0: LAST_ATM = 24000
    if not CE_ID:
        try: CE_ID, PE_ID, _ = get_ids(LAST_ATM)
        except Exception as e: return {"error": str(e)}
    try:
        res = dhan.quote_data({"NSE_FNO": [int(CE_ID), int(PE_ID)]})
        if res.get("status") == "failure": return LAST_VALID or {"error": "API failure"}
        d = res["data"]["data"]["NSE_FNO"]
        ce = float(d[str(CE_ID)]["last_price"])
        pe = float(d[str(PE_ID)]["last_price"])
        ce_oi = int(d[str(CE_ID)].get("oi", 0))
        pe_oi = int(d[str(PE_ID)].get("oi", 0))
        LAST_CE = ce; LAST_PE = pe; LAST_CE_OI = ce_oi; LAST_PE_OI = pe_oi
        spot = LAST_ATM + (ce - pe)
        new_atm = get_atm(spot)
        if new_atm != LAST_ATM: LAST_ATM = new_atm; CE_ID = None
        _, _, expiry = SCRIP_CACHE.get(LAST_ATM, (None, None, "?"))
        result = {"spot": round(spot), "atm": LAST_ATM, "ce": round(ce, 2),
                  "pe": round(pe, 2), "ce_oi": ce_oi, "pe_oi": pe_oi,
                  "straddle": round(ce + pe, 2), "expiry": expiry,
                  "error": "", "ts": ist_now().strftime("%H:%M:%S")}
        LAST_VALID = result
        return result
    except Exception as e:
        return LAST_VALID or {"error": str(e)}

def gamma_score():
    score = 0; details = {}
    # F1 velocity
    v1 = (straddle_hist[-1] - straddle_hist[-2]) if len(straddle_hist) >= 2 else 0
    h1 = v1 >= CFG["vel_thresh"]; score += 20 if h1 else 0
    details["F1 Velocity"] = {"val": round(v1, 1), "thresh": CFG["vel_thresh"], "hit": h1}
    # F2 acceleration
    v2 = abs(spot_hist[-1] - spot_hist[-3]) if len(spot_hist) >= 3 else 0
    h2 = v2 >= CFG["acc_thresh"]; score += 20 if h2 else 0
    details["F2 Acceleration"] = {"val": round(v2, 1), "thresh": CFG["acc_thresh"], "hit": h2}
    # F3 imbalance
    v3 = abs(LAST_CE - LAST_PE)
    h3 = v3 >= CFG["imb_thresh"]; score += 20 if h3 else 0
    details["F3 Imbalance"] = {"val": round(v3, 1), "thresh": CFG["imb_thresh"], "hit": h3}
    # F4 pct
    v4 = ((straddle_hist[-1] - straddle_hist[-5]) / straddle_hist[-5] * 100) if len(straddle_hist) >= 5 and straddle_hist[-5] > 0 else 0
    h4 = v4 >= CFG["pct_thresh"]; score += 20 if h4 else 0
    details["F4 Expansion %"] = {"val": round(v4, 2), "thresh": CFG["pct_thresh"], "hit": h4}
    # F5 OI ratio
    v5 = (max(LAST_CE_OI, LAST_PE_OI) / min(LAST_CE_OI, LAST_PE_OI)) if min(LAST_CE_OI, LAST_PE_OI) > 0 else 0
    h5 = v5 >= CFG["oi_thresh"]; score += 20 if h5 else 0
    details["F5 OI Skew"] = {"val": round(v5, 2), "thresh": CFG["oi_thresh"], "hit": h5}
    active = score >= CFG["score_thresh"]
    state = "🔥 GAMMA ACTIVE" if active else ("⚡ BUILDING" if score >= 40 else "NORMAL")
    return {"score": score, "state": state, "active": active, "details": details}

def get_signal():
    if len(straddle_hist) < 5: return {"signal": "WAIT", "pct": 0}
    pct = (straddle_hist[-1] - straddle_hist[-5]) / straddle_hist[-5] * 100 if straddle_hist[-5] > 0 else 0
    if pct >= 1.8: return {"signal": "EXPANSION", "pct": round(pct, 2)}
    if pct <= -1.8: return {"signal": "CONTRACTION", "pct": round(pct, 2)}
    return {"signal": "NEUTRAL", "pct": round(pct, 2)}

@app.post("/api/connect")
async def connect(request: Request):
    global dhan, CLIENT_ID, CE_ID, PE_ID, LAST_ATM
    body = await request.json()
    cid = body.get("client_id", "").strip()
    tok = body.get("access_token", "").strip()
    if not cid or not tok:
        return JSONResponse({"ok": False, "msg": "Both Client ID and Token required"})
    try:
        dhan = dhanhq(cid, tok)
        CLIENT_ID = cid; CE_ID = None; LAST_ATM = 0
        return JSONResponse({"ok": True, "msg": f"Connected | Client {cid[:6]}***"})
    except Exception as e:
        return JSONResponse({"ok": False, "msg": str(e)})

@app.get("/api/data")
async def api_data():
    d = fetch_data()
    if d and d.get("spot", 0) > 0:
        spot_hist.append(d["spot"])
        straddle_hist.append(d["straddle"])
    g = gamma_score()
    sig = get_signal()
    if g["active"]:
        event_log.appendleft({"ts": ist_now().strftime("%H:%M:%S"),
                               "event": "GAMMA ACTIVE", "score": g["score"],
                               "spot": d.get("spot", 0) if d else 0})
    return JSONResponse({**(d or {}), "gamma": g, "signal": sig,
                         "market": is_market(), "log": list(event_log)[:8],
                         "connected": dhan is not None})

@app.get("/", response_class=HTMLResponse)
async def root():
    return HTML

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Gamma Blast · Nifty 50</title>
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Space+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>
:root{--bg:#080c14;--s:#0e1525;--c:#131d30;--b:#1e2d45;--t:#d0ddf0;--m:#4a5f80;
--g:#00e676;--r:#ff1744;--a:#ffab00;--bl:#4fc3f7;--cy:#26c6da;}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent;}
body{background:var(--bg);color:var(--t);font-family:'Space Mono',monospace;font-size:13px;min-height:100vh;}
/* HEADER */
.hdr{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;
  background:var(--s);border-bottom:1px solid var(--b);position:sticky;top:0;z-index:50;}
.logo{font-family:'Bebas Neue';font-size:22px;letter-spacing:3px;
  background:linear-gradient(90deg,var(--a),var(--r));-webkit-background-clip:text;-webkit-text-fill-color:transparent;}
.hdr-r{display:flex;gap:8px;align-items:center;}
.pill{padding:3px 10px;border-radius:20px;font-size:11px;font-weight:700;cursor:pointer;border:none;}
.pill-g{background:rgba(0,230,118,.12);color:var(--g);border:1px solid rgba(0,230,118,.3);}
.pill-r{background:rgba(255,23,68,.12);color:var(--r);border:1px solid rgba(255,23,68,.3);}
.pill-m{background:rgba(255,171,0,.12);color:var(--a);border:1px solid rgba(255,171,0,.3);}
/* GAMMA HERO */
.hero{text-align:center;padding:24px 16px 16px;position:relative;}
.hero.active{background:radial-gradient(ellipse at center,rgba(255,171,0,.12),transparent 70%);}
.hero.build{background:radial-gradient(ellipse at center,rgba(79,195,247,.08),transparent 70%);}
.g-state{font-family:'Bebas Neue';font-size:44px;letter-spacing:5px;line-height:1;transition:.3s;}
.g-state.active{color:var(--a);text-shadow:0 0 30px rgba(255,171,0,.5);animation:flk 2s infinite;}
.g-state.build{color:var(--bl);}
.g-state.normal{color:var(--m);}
@keyframes flk{0%,100%{opacity:1}93%{opacity:.8}94%{opacity:1}}
.score-row{display:flex;align-items:center;justify-content:center;gap:12px;margin-top:10px;}
.score-track{width:160px;height:5px;background:var(--b);border-radius:3px;overflow:hidden;}
.score-fill{height:100%;border-radius:3px;transition:width .5s;}
.score-num{font-size:16px;font-weight:700;min-width:40px;}
.dots{display:flex;justify-content:center;gap:6px;margin-top:8px;}
.dot{width:9px;height:9px;border-radius:50%;background:var(--b);transition:.3s;position:relative;}
.dot.hit{background:var(--g);box-shadow:0 0 8px var(--g);}
.dot-lbl{font-size:9px;color:var(--m);text-align:center;margin-top:2px;}
/* GRID */
.grid{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--b);}
@media(min-width:600px){.grid{grid-template-columns:repeat(4,1fr);}}
.cell{background:var(--c);padding:14px 16px;}
.cl{font-size:9px;text-transform:uppercase;letter-spacing:1.5px;color:var(--m);margin-bottom:5px;}
.cv{font-size:26px;font-weight:700;font-family:'Bebas Neue';letter-spacing:1px;line-height:1;}
.cv.spot{color:var(--t)}.cv.ce{color:var(--g)}.cv.pe{color:var(--r)}.cv.str{color:var(--cy);}
.cs{font-size:11px;color:var(--m);margin-top:3px;}
/* SIGNAL BAR */
.sig-bar{display:flex;align-items:center;gap:12px;padding:10px 16px;background:var(--s);
  flex-wrap:wrap;border-bottom:1px solid var(--b);}
.sig-lbl{font-size:10px;text-transform:uppercase;letter-spacing:1px;color:var(--m);}
.sig-v{font-size:14px;font-weight:700;}
.sig-v.exp{color:var(--g)}.sig-v.con{color:var(--r)}.sig-v.neu{color:var(--m)}.sig-v.wait{color:var(--m);}
/* OI */
.oi-row{display:grid;grid-template-columns:1fr 60px 1fr;align-items:center;
  padding:12px 16px;background:var(--c);border-bottom:1px solid var(--b);}
.oi-side{}
.oi-lbl{font-size:9px;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px;}
.oi-track{height:4px;background:var(--b);border-radius:2px;overflow:hidden;margin-bottom:4px;}
.oi-fill-g{height:100%;background:var(--g);border-radius:2px;transition:.5s;}
.oi-fill-r{height:100%;background:var(--r);border-radius:2px;transition:.5s;}
.oi-num{font-size:13px;font-weight:700;}
.oi-mid{text-align:center;}
.oi-ratio{font-size:15px;font-weight:700;color:var(--a);}
/* BOTTOM */
.bottom{display:grid;grid-template-columns:1fr;gap:1px;background:var(--b);}
@media(min-width:600px){.bottom{grid-template-columns:1fr 1fr;}}
.panel{background:var(--c);padding:14px 16px;}
.p-title{font-size:9px;text-transform:uppercase;letter-spacing:2px;color:var(--m);margin-bottom:10px;}
.log-item{display:flex;gap:8px;padding:5px 0;border-bottom:1px solid rgba(30,45,69,.5);}
.log-ts{color:var(--m);font-size:10px;min-width:55px;}
.log-ev{color:var(--a);font-size:11px;flex:1;}
.log-sc{color:var(--m);font-size:10px;}
.fi{display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid rgba(30,45,69,.5);}
.fn{font-size:10px;color:var(--m);}
.fv{font-size:11px;color:var(--t);}
.fb{font-size:10px;font-weight:700;padding:2px 7px;border-radius:3px;}
.fb.hit{background:rgba(0,230,118,.12);color:var(--g);}
.fb.miss{background:rgba(30,45,69,.5);color:var(--m);}
/* MODAL */
.ov{position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:100;display:none;align-items:center;justify-content:center;}
.ov.show{display:flex;}
.modal{background:var(--s);border:1px solid var(--b);border-radius:12px;padding:24px;width:360px;max-width:95vw;}
.modal h3{font-family:'Bebas Neue';font-size:22px;letter-spacing:2px;color:var(--a);margin-bottom:18px;}
.f{margin-bottom:14px;}
.f label{display:block;font-size:10px;text-transform:uppercase;letter-spacing:1px;color:var(--m);margin-bottom:5px;}
.f input{width:100%;background:var(--bg);border:1px solid var(--b);color:var(--t);
  padding:9px 11px;border-radius:6px;font-family:'Space Mono';font-size:13px;}
.f input:focus{outline:none;border-color:var(--a);}
.f .hint{font-size:10px;color:var(--m);margin-top:3px;}
.btn-row{display:flex;gap:8px;margin-top:18px;}
.btn{flex:1;padding:10px;border:none;border-radius:6px;font-family:'Space Mono';
  font-size:12px;font-weight:700;cursor:pointer;letter-spacing:.5px;}
.btn-save{background:var(--a);color:#000;}
.btn-cls{background:var(--b);color:var(--m);}
.toast{margin-top:12px;padding:9px;border-radius:6px;font-size:12px;display:none;text-align:center;}
.toast.ok{background:rgba(0,230,118,.1);border:1px solid rgba(0,230,118,.3);color:var(--g);}
.toast.err{background:rgba(255,23,68,.1);border:1px solid rgba(255,23,68,.3);color:var(--r);}
.err-bar{padding:8px 16px;background:rgba(255,23,68,.08);border-bottom:1px solid rgba(255,23,68,.2);
  color:#ff8a80;font-size:12px;display:none;}
.err-bar.show{display:block;}
</style>
</head>
<body>
<div class="ov" id="ov">
  <div class="modal">
    <h3>⚙ CREDENTIALS</h3>
    <div class="f">
      <label>Dhan Client ID</label>
      <input type="text" id="inp_cid" placeholder="Your Client ID">
    </div>
    <div class="f">
      <label>Access Token (JWT)</label>
      <input type="password" id="inp_tok" placeholder="Today's token">
      <div class="hint">Stored in browser only — clears daily</div>
    </div>
    <div class="btn-row">
      <button class="btn btn-save" onclick="saveCreds()">CONNECT</button>
      <button class="btn btn-cls" onclick="closeModal()">CANCEL</button>
    </div>
    <div class="toast" id="toast"></div>
  </div>
</div>

<div class="hdr">
  <div class="logo">⚡ GAMMA BLAST</div>
  <div class="hdr-r">
    <span id="mktPill" class="pill pill-r">CLOSED</span>
    <button id="credBtn" class="pill pill-m" onclick="openModal()">⚙ SET CREDS</button>
  </div>
</div>

<div class="err-bar" id="errBar"></div>

<div class="hero" id="hero">
  <div class="g-state normal" id="gState">LOADING...</div>
  <div class="score-row">
    <span style="font-size:10px;color:var(--m)">SCORE</span>
    <div class="score-track"><div class="score-fill" id="sFill" style="width:0;background:var(--m)"></div></div>
    <span class="score-num" id="sNum" style="color:var(--m)">0</span>
  </div>
  <div class="dots" id="dotsRow">
    <div><div class="dot" id="d1"></div><div class="dot-lbl">VEL</div></div>
    <div><div class="dot" id="d2"></div><div class="dot-lbl">ACC</div></div>
    <div><div class="dot" id="d3"></div><div class="dot-lbl">IMB</div></div>
    <div><div class="dot" id="d4"></div><div class="dot-lbl">PCT</div></div>
    <div><div class="dot" id="d5"></div><div class="dot-lbl">OI</div></div>
  </div>
</div>

<div class="sig-bar">
  <span class="sig-lbl">Signal</span><span class="sig-v wait" id="sigV">WAIT</span>
  <span id="sigPct" style="font-size:11px;color:var(--m)"></span>
  <span style="color:var(--b)">|</span>
  <span class="sig-lbl">ATM</span><span style="font-size:14px;font-weight:700;color:var(--cy)" id="atmV">—</span>
  <span style="color:var(--b)">|</span>
  <span class="sig-lbl">Expiry</span><span style="font-size:11px;color:var(--m)" id="expV">—</span>
  <span style="margin-left:auto;font-size:11px;color:var(--m)" id="tsV">—</span>
</div>

<div class="grid">
  <div class="cell"><div class="cl">Nifty 50 Spot</div><div class="cv spot" id="spotV">—</div></div>
  <div class="cell"><div class="cl">CE Premium</div><div class="cv ce" id="ceV">—</div><div class="cs" id="ceSub"></div></div>
  <div class="cell"><div class="cl">PE Premium</div><div class="cv pe" id="peV">—</div><div class="cs" id="peSub"></div></div>
  <div class="cell"><div class="cl">Straddle</div><div class="cv str" id="strV">—</div><div class="cs" id="strSub"></div></div>
</div>

<div class="oi-row">
  <div class="oi-side">
    <div class="oi-lbl" style="color:var(--g)">CE Open Interest</div>
    <div class="oi-track"><div class="oi-fill-g" id="ceOiBar" style="width:50%"></div></div>
    <div class="oi-num" style="color:var(--g)" id="ceOiV">—</div>
  </div>
  <div class="oi-mid"><div class="oi-ratio" id="oiRatio">—</div><div style="font-size:9px;color:var(--m)">OI RATIO</div></div>
  <div class="oi-side" style="text-align:right">
    <div class="oi-lbl" style="color:var(--r)">PE Open Interest</div>
    <div class="oi-track"><div class="oi-fill-r" id="peOiBar" style="width:50%"></div></div>
    <div class="oi-num" style="color:var(--r)" id="peOiV">—</div>
  </div>
</div>

<div class="bottom">
  <div class="panel">
    <div class="p-title">📋 Event Log</div>
    <div id="logList"><div style="color:var(--m);font-size:12px">Waiting for signals...</div></div>
  </div>
  <div class="panel">
    <div class="p-title">🔬 Filter Detail</div>
    <div id="fDetail"></div>
  </div>
</div>

<script>
// ── localStorage credentials (keyed by IST date) ──────────────
function istDate(){
  return new Date().toLocaleString('en-CA',{timeZone:'Asia/Kolkata'}).slice(0,10);
}
const CRED_KEY = 'dhan_gamma_' + istDate();

function loadCreds(){
  try{ const s=localStorage.getItem(CRED_KEY); return s?JSON.parse(s):null; }catch(e){return null;}
}
function persistCreds(cid,tok){
  try{
    localStorage.setItem(CRED_KEY,JSON.stringify({cid,tok}));
    // Remove old keys
    for(let i=localStorage.length-1;i>=0;i--){
      const k=localStorage.key(i);
      if(k&&k.startsWith('dhan_gamma_')&&k!==CRED_KEY) localStorage.removeItem(k);
    }
  }catch(e){}
}

function openModal(){
  const c=loadCreds();
  if(c){document.getElementById('inp_cid').value=c.cid||'';}
  document.getElementById('ov').classList.add('show');
}
function closeModal(){document.getElementById('ov').classList.remove('show');}

async function saveCreds(){
  const cid=document.getElementById('inp_cid').value.trim();
  const tok=document.getElementById('inp_tok').value.trim();
  const t=document.getElementById('toast');
  if(!cid||!tok){t.textContent='Both fields required';t.className='toast err';return;}
  try{
    const r=await fetch('api/connect',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({client_id:cid,access_token:tok})});
    const d=await r.json();
    t.textContent=d.msg; t.className='toast '+(d.ok?'ok':'err');
    if(d.ok){persistCreds(cid,tok);setTimeout(()=>closeModal(),1200);}
  }catch(e){t.textContent='Connection failed';t.className='toast err';}
}

// ── Auto connect on load ──────────────────────────────────────
async function autoConnect(){
  const c=loadCreds();
  if(!c) return;
  try{
    const r=await fetch('api/connect',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({client_id:c.cid,access_token:c.tok})});
    const d=await r.json();
    if(d.ok) updateCredBtn(c.cid,true);
  }catch(e){}
}
function updateCredBtn(cid,ok){
  const b=document.getElementById('credBtn');
  b.textContent=ok?('✅ '+cid.slice(0,6)+'***'):'⚙ SET CREDS';
  b.className='pill '+(ok?'pill-g':'pill-m');
}

// ── Format helpers ────────────────────────────────────────────
function fmtOI(v){
  if(v>=1e7) return (v/1e7).toFixed(1)+'Cr';
  if(v>=1e5) return (v/1e5).toFixed(1)+'L';
  return v.toLocaleString('en-IN');
}
let prevStr=0;

// ── Main poll ─────────────────────────────────────────────────
async function poll(){
  try{
    const r=await fetch('api/data');
    const d=await r.json();

    // Market pill
    const mp=document.getElementById('mktPill');
    mp.textContent=d.market?'LIVE':'CLOSED';
    mp.className='pill '+(d.market?'pill-g':'pill-r');

    // Cred status
    updateCredBtn(d.connected?'connected':'',d.connected);

    // Error
    const eb=document.getElementById('errBar');
    if(d.error){eb.textContent='⚠ '+d.error;eb.classList.add('show');}
    else eb.classList.remove('show');

    // Gamma hero
    const g=d.gamma||{};
    const hero=document.getElementById('hero');
    const gs=document.getElementById('gState');
    const sf=document.getElementById('sFill');
    const sn=document.getElementById('sNum');
    gs.textContent=g.state||'—';
    const sc=g.score||0;
    const cls=g.active?'active':sc>=40?'build':'normal';
    gs.className='g-state '+cls;
    hero.className='hero '+cls;
    sf.style.width=sc+'%';
    sf.style.background=sc>=60?'var(--a)':sc>=40?'var(--bl)':'var(--m)';
    sn.textContent=sc+'/100';
    sn.style.color=sc>=60?'var(--a)':sc>=40?'var(--bl)':'var(--m)';

    // Dots
    const det=g.details||{};
    const dkeys=Object.values(det);
    ['d1','d2','d3','d4','d5'].forEach((id,i)=>{
      document.getElementById(id).className='dot'+(dkeys[i]?.hit?' hit':'');
    });

    // Filter detail
    const fd=document.getElementById('fDetail');
    fd.innerHTML=Object.entries(det).map(([k,v])=>`
      <div class="fi">
        <span class="fn">${k}</span>
        <div style="display:flex;gap:8px;align-items:center">
          <span class="fv">${v.val} / ${v.thresh}</span>
          <span class="fb ${v.hit?'hit':'miss'}">${v.hit?'HIT':'MISS'}</span>
        </div>
      </div>`).join('');

    // Signal
    const sig=d.signal||{};
    const sv=document.getElementById('sigV');
    const sigMap={EXPANSION:'exp',CONTRACTION:'con',NEUTRAL:'neu',WAIT:'wait'};
    sv.textContent=sig.signal||'WAIT';
    sv.className='sig-v '+(sigMap[sig.signal]||'wait');
    document.getElementById('sigPct').textContent=sig.pct?sig.pct+'%':'';

    // Prices
    if(d.spot){
      document.getElementById('spotV').textContent=d.spot.toLocaleString('en-IN');
      document.getElementById('atmV').textContent=d.atm?.toLocaleString('en-IN')||'—';
      document.getElementById('ceV').textContent=d.ce;
      document.getElementById('peV').textContent=d.pe;
      document.getElementById('strV').textContent=d.straddle;
      const chg=d.straddle-prevStr;
      const sub=document.getElementById('strSub');
      if(prevStr>0){sub.textContent=(chg>0?'+':'')+chg.toFixed(1)+' pts';
        sub.style.color=chg>0?'var(--g)':chg<0?'var(--r)':'var(--m)';}
      prevStr=d.straddle;
      document.getElementById('expV').textContent=d.expiry||'—';
      document.getElementById('tsV').textContent=d.ts||'';
    }

    // OI
    if(d.ce_oi||d.pe_oi){
      const tot=(d.ce_oi+d.pe_oi)||1;
      document.getElementById('ceOiV').textContent=fmtOI(d.ce_oi);
      document.getElementById('peOiV').textContent=fmtOI(d.pe_oi);
      document.getElementById('ceOiBar').style.width=(d.ce_oi/tot*100)+'%';
      document.getElementById('peOiBar').style.width=(d.pe_oi/tot*100)+'%';
      const ratio=d.ce_oi>d.pe_oi?(d.ce_oi/d.pe_oi).toFixed(2)+':1 CE':(d.pe_oi/d.ce_oi).toFixed(2)+':1 PE';
      document.getElementById('oiRatio').textContent=ratio;
    }

    // Log
    const ll=document.getElementById('logList');
    if(d.log&&d.log.length){
      ll.innerHTML=d.log.map(l=>`<div class="log-item">
        <span class="log-ts">${l.ts}</span>
        <span class="log-ev">${l.event}</span>
        <span class="log-sc">${l.score}/100</span>
      </div>`).join('');
    }
  }catch(e){console.error(e);}
}

// Click outside modal to close
document.getElementById('ov').addEventListener('click',function(e){if(e.target===this)closeModal();});

autoConnect().then(()=>{poll();setInterval(poll,5000);});
</script>
</body>
</html>"""
