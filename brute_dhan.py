from dhanhq import dhanhq
dhan = dhanhq("1108455416", "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzUxMiJ9.eyJpc3MiOiJkaGFuIiwicGFydG5lcklkIjoiIiwiZXhwIjoxNzc0MzIwNDkzLCJpYXQiOjE3NzQyMzQwOTMsInRva2VuQ29uc3VtZXJUeXBlIjoiU0VMRiIsIndlYmhvb2tVcmwiOiIiLCJkaGFuQ2xpZW50SWQiOiIxMTA4NDU1NDE2In0.LZCZj6XnoIrz70tFY7HJ1nIED6JqeykyW_cuY6Yc53BZrTUNoP5iT21guZFVUu7jYyC3Y4z6-1LhGQk-a4L_Lw")

ids = ["13", "NIFTY", "NIFTY 50"]
segments = ["IDX_I", "NSE_FNO", "NFO", "NSE_INDEX"]
dates = ["24-Mar-2026", "26-Mar-2026", "2026-03-24", "24-03-2026"]

found = False
for i in ids:
    for s in segments:
        for d in dates:
            try:
                res = dhan.option_chain(i, s, d)
                if not res: continue
                status = res.get('status', 'failure')
                if status == 'success':
                    data = res.get('data', {})
                    if data:
                        print(f"✅ SUCCESS MAPPING! ID: '{i}', Segment: '{s}', Date: '{d}'")
                        print(f"Response: {str(res)[:100]}")
                        found = True
                        break
            except Exception as e:
                pass
        if found: break
    if found: break

if not found:
    print("None of the combinations worked. API might require a different structure.")
