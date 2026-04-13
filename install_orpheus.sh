#!/data/data/com.termux/files/usr/bin/bash

echo "=== OrpheusDL Termux Installer (VENV) ==="

# -------------------------------
# CLEAN OLD INSTALL
# -------------------------------
echo "[*] Cleaning old installation..."

rm -rf OrpheusDL

# -------------------------------
# UPDATE & INSTALL BASE PACKAGES
# -------------------------------
echo "[*] Installing base packages..."

pkg update -y && pkg upgrade -y
pkg install -y python git libjpeg-turbo ffmpeg deno

# -------------------------------
# CLONE MAIN REPO
# -------------------------------
echo "[*] Cloning OrpheusDL..."

git clone https://github.com/bascurtiz/OrpheusDL
cd OrpheusDL || exit

# -------------------------------
# CREATE VENV
# -------------------------------
echo "[*] Creating virtual environment..."

python -m venv venv
source venv/bin/activate

# -------------------------------
# INSTALL REQUIREMENTS
# -------------------------------
echo "[*] Installing requirements..."

pip install --upgrade pip
pip install --upgrade --ignore-installed -r requirements.txt

# -------------------------------
# INSTALL LIBRESPOT
# -------------------------------
echo "[*] Installing librespot..."

mkdir -p vendor/librespot
pip install --no-deps --target vendor/librespot git+https://github.com/kokarare1212/librespot-python

# -------------------------------
# INITIAL SETUP
# -------------------------------
echo "[*] Running initial setup..."

python orpheus.py settings refresh

# -------------------------------
# FIX CERTS
# -------------------------------
echo "[*] Updating certifi..."

pip install --upgrade certifi

# -------------------------------
# INSTALL MODULES
# -------------------------------
echo "[*] Installing modules..."

mkdir -p modules

git clone https://github.com/bascurtiz/orpheusdl-applemusic modules/applemusic
git clone https://github.com/bascurtiz/orpheusdl-beatport modules/beatport
git clone https://github.com/bascurtiz/orpheusdl-beatsource modules/beatsource
git clone https://github.com/bascurtiz/orpheusdl-deezer modules/deezer
git clone https://github.com/bascurtiz/orpheusdl-qobuz modules/qobuz
git clone https://github.com/bascurtiz/orpheusdl-soundcloud modules/soundcloud
git clone https://github.com/bascurtiz/orpheusdl-spotify modules/spotify
git clone --recurse-submodules https://github.com/bascurtiz/orpheusdl-tidal modules/tidal
git clone https://github.com/bascurtiz/orpheusdl-youtube modules/youtube

# -------------------------------
# TERMUX STORAGE
# -------------------------------
echo "[*] Setting up storage..."
termux-setup-storage

# -------------------------------
# RUN APP
# -------------------------------
echo "[*] Starting OrpheusDL..."

python orpheus.py

# -------------------------------
# FUTURE RUN INSTRUCTIONS
# -------------------------------
echo ""
echo "=== HOW TO RUN LATER ==="
echo " "
echo "cd OrpheusDL && source venv/bin/activate && python webui.py"
echo " "