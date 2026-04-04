#!/bin/bash
# ============================================================
# deploy.sh — Gamma Blast Terminal Setup on Hetzner VPS
# Run from: /root/gamma/
# ============================================================
set -e

echo ""
echo "======================================================"
echo "  Gamma Blast Terminal — Deploy"
echo "======================================================"

# 1. Virtualenv
if [ ! -d "venv" ]; then
  echo "[1] Creating virtualenv..."
  python3 -m venv venv
else
  echo "[1] Virtualenv exists."
fi

source venv/bin/activate

# 2. Dependencies
echo "[2] Installing dependencies..."
pip install -q -r requirements.txt

# 3. .env check
if [ ! -f ".env" ]; then
  cp .env.example .env
  echo ""
  echo "  ⚠️  .env created from template."
  echo "  Edit it now: nano .env"
  echo "  Then re-run this script."
  exit 1
fi

if grep -q "your_dhan" .env; then
  echo ""
  echo "  ⚠️  Fill in CLIENT_ID and ACCESS_TOKEN in .env first!"
  echo "  Run: nano .env"
  exit 1
fi

echo "[3] Credentials found ✅"

# 4. Systemd service
echo "[4] Installing systemd service..."
cp gamma_blast.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable gamma_blast
systemctl restart gamma_blast

sleep 2
echo ""
systemctl status gamma_blast --no-pager

echo ""
echo "======================================================"
echo "  Gamma Blast is LIVE on port 8001"
echo "  URL: http://$(curl -s ifconfig.me):8001"
echo ""
echo "  Manage:"
echo "    systemctl status  gamma_blast"
echo "    systemctl restart gamma_blast"
echo "    journalctl -u gamma_blast -f"
echo "======================================================"
