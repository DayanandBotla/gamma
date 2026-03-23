import urllib.request
import csv
import io

url = "https://images.dhan.co/api-data/api-scrip-master.csv"
print("Downloading Dhan Master CSV...")

try:
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req) as response:
        content = response.read()
        
    print("Download complete. Parsing headers...")
    reader = csv.reader(io.StringIO(content.decode('utf-8')))
    headers = next(reader)
    print("HEADERS:")
    print(headers)
    
    print("\nFirst row:")
    print(next(reader))
except Exception as e:
    print(f"Error: {e}")
