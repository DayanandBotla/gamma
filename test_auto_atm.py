from dhanhq import dhanhq
import urllib.request
import csv
import io
from datetime import datetime
import time
import os

# ===== CONFIG (USE ENV — DON’T HARDCORE TOKENS AGAIN) =====
CLIENT_ID = "1108455416"
ACCESS_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzUxMiJ9.eyJpc3MiOiJkaGFuIiwicGFydG5lcklkIjoiIiwiZXhwIjoxNzc0MzIwNDkzLCJpYXQiOjE3NzQyMzQwOTMsInRva2VuQ29uc3VtZXJUeXBlIjoiU0VMRiIsIndlYmhvb2tVcmwiOiIiLCJkaGFuQ2xpZW50SWQiOiIxMTA4NDU1NDE2In0.LZCZj6XnoIrz70tFY7HJ1nIED6JqeykyW_cuY6Yc53BZrTUNoP5iT21guZFVUu7jYyC3Y4z6-1LhGQk-a4L_Lw"

dhan = dhanhq(CLIENT_ID, ACCESS_TOKEN)


# ===== SAFE API CALL =====
def safe_quote_fetch(securities):
    for attempt in range(3):
        try:
            response = dhan.quote_data(securities)
            print("RAW RESPONSE:", response)

            # detect auth failure
            if response.get("status") == "failure":
                print("⚠️ API Failure — retrying...")
                time.sleep(1)
                continue

            return response

        except Exception as e:
            print("Error:", e)
            time.sleep(1)

    return None


# ===== ATM CALC =====
def get_atm_strike(spot):
    return round(spot / 50) * 50


# ===== GET CE/PE IDS =====
def get_atm_security_ids(atm):
    url = "https://images.dhan.co/api-data/api-scrip-master.csv"

    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response:
            content = response.read().decode('utf-8')

        reader = csv.DictReader(io.StringIO(content))
        filtered = []

        for row in reader:
            if row.get('SEM_INSTRUMENT_NAME') != 'OPTIDX':
                continue

            custom = str(row.get('SEM_CUSTOM_SYMBOL', ''))

            if not custom.startswith("NIFTY"):
                continue

            if any(x in custom for x in ["BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"]):
                continue

            try:
                strike = float(row.get('SEM_STRIKE_PRICE', 0))
                if strike != float(atm):
                    continue
            except:
                continue

            filtered.append(row)

        if not filtered:
            print("❌ No options found")
            return None, None

        filtered.sort(
            key=lambda x: datetime.strptime(
                x['SEM_EXPIRY_DATE'].split()[0],
                "%Y-%m-%d"
            )
        )

        expiry = filtered[0]['SEM_EXPIRY_DATE']
        print("✅ Expiry:", expiry)

        ce_id, pe_id = None, None

        for row in filtered:
            if row['SEM_EXPIRY_DATE'] == expiry:
                if row['SEM_OPTION_TYPE'] == 'CE':
                    ce_id = row['SEM_SMST_SECURITY_ID']
                elif row['SEM_OPTION_TYPE'] == 'PE':
                    pe_id = row['SEM_SMST_SECURITY_ID']

        return ce_id, pe_id

    except Exception as e:
        print("CSV Error:", e)
        return None, None


# ===== FETCH LIVE DATA =====
def fetch_option_data(ce_id, pe_id):
    # Dhan API strictly requires "NSE_FNO" for Options quote_data. 
    # "NFO" is only for order placement and will always return an empty {} response!
    seg = "NSE_FNO"
    print(f"\n🔍 Fetching Live Quotes from {seg}...")

    securities = {seg: [str(ce_id), str(pe_id)]}
    
    # Do exactly ONE safe API call. If rate-limited, do NOT spam retries every 1 second 
    # or Dhan will permanently block the API account.
    response = dhan.quote_data(securities)
    
    if not response:
        print("❌ Empty response from Dhan API")
        return 0, 0, 0, 0, 0

    status = response.get("status")
    
    if status == "failure":
        error_dict = response.get("data", {}).get("data", {})
        if "805" in error_dict:
            print("\n🚨 CRITICAL HIT: 805 Too Many Requests! 🚨")
            print("You have been temporarily rate-limited. Wait exactly 5-10 minutes.")
            print("Do NOT loop retries—it resets the ban timer!")
        else:
            print("❌ API Failure:", response)
        return 0, 0, 0, 0, 0

    data = response.get("data", {}).get(seg, {})

    if not data:
        print("❌ No data object returned despite success.")
        return 0, 0, 0, 0, 0

    ce_data = data.get(str(ce_id), {})
    pe_data = data.get(str(pe_id), {})

    ce_ltp = ce_data.get("last_price", 0)
    pe_ltp = pe_data.get("last_price", 0)

    ce_oi = ce_data.get("oi", 0)
    pe_oi = pe_data.get("oi", 0)

    # 🔥 key trick
    spot = ce_data.get("underlying_price", 0)

    if ce_ltp > 0 or pe_ltp > 0:
        print(f"✅ SUCCESS from {seg}")
        return spot, ce_ltp, pe_ltp, ce_oi, pe_oi

    print("❌ Values returned were zero.")
    return 0, 0, 0, 0, 0


# ===== MAIN =====
if __name__ == "__main__":

    # Step 1: temp ATM
    temp_spot = 23550
    atm = get_atm_strike(temp_spot)

    print("Initial ATM:", atm)

    # Step 2: get IDs
    ce_id, pe_id = get_atm_security_ids(atm)

    print("CE ID:", ce_id)
    print("PE ID:", pe_id)

    if not ce_id or not pe_id:
        print("❌ Failed to get IDs")
        exit()

    # Step 3: fetch live data
    spot, ce_ltp, pe_ltp, ce_oi, pe_oi = fetch_option_data(ce_id, pe_id)

    print("\n===== FINAL OUTPUT =====")
    print("Spot:", spot)
    print("CE LTP:", ce_ltp, "| PE LTP:", pe_ltp)
    print("CE OI:", ce_oi, "| PE OI:", pe_oi)

    # Step 4: recalc ATM
    if spot > 0:
        atm = get_atm_strike(spot)
        print("Corrected ATM:", atm)