#!/data/data/com.termux/files/usr/bin/bash

echo "=== OrpheusDL Termux Installer (VENV) ==="

set -u

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
REQ_CMD="pip install --upgrade --ignore-installed --prefer-binary -r requirements.txt"

# Termux/Python 3.13 can fail building pydantic-core from source (Rust target issue).
# Use a compatibility constraint to keep startup dependencies installable.
if [ -n "${TERMUX_VERSION:-}" ] || uname -a | grep -qi "android"; then
    echo "[*] Detected Termux/Android. Applying compatibility constraints..."
    CONSTRAINTS_FILE="/tmp/orpheus-termux-constraints.txt"
    cat > "$CONSTRAINTS_FILE" <<'EOF'
pydantic<2
EOF
    REQ_CMD="$REQ_CMD -c $CONSTRAINTS_FILE"
fi

if ! eval "$REQ_CMD"; then
    echo "[!] Full requirements install failed."
    echo "[!] Attempting to install core runtime dependencies needed for startup..."
fi

# Ensure core HTTP/runtime deps are always present even if the full install fails.
# This avoids the common "Missing dependency: requests" fatal error on startup.
pip install --upgrade requests urllib3 flask certifi || {
    echo "[FATAL] Failed to install core runtime dependencies."
    exit 1
}

python -c "import requests, urllib3, flask, certifi" || {
    echo "[FATAL] Core dependencies are still missing after install attempt."
    echo "Try: pip install --upgrade requests urllib3 flask certifi"
    exit 1
}

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