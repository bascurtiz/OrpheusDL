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
pkg install -y python git libjpeg-turbo ffmpeg deno cmake

# -------------------------------
# CLONE MAIN REPO
# -------------------------------
echo "[*] Cloning OrpheusDL..."

git clone https://github.com/bascurtiz/OrpheusDL
cd OrpheusDL || exit

# -------------------------------
# DOWNLOAD SPOTIFY.DLL
# -------------------------------
echo "[*] Downloading Spotify.dll..."

if command -v curl >/dev/null 2>&1; then
    if ! curl -fsSL "http://orpheusdl-gui.x10.mx/Spotify.dll" -o "Spotify.dll"; then
        echo "[FATAL] Failed to download Spotify.dll"
        exit 1
    fi
else
    echo "[FATAL] curl is not available to download Spotify.dll"
    exit 1
fi

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
    TMP_WRITE_DIR="${TMPDIR:-$HOME/.cache}"
    mkdir -p "$TMP_WRITE_DIR"
    CONSTRAINTS_FILE="$TMP_WRITE_DIR/orpheus-termux-constraints.txt"
    cat > "$CONSTRAINTS_FILE" <<'EOF'
pydantic<2
EOF
    REQ_CMD="$REQ_CMD -c $CONSTRAINTS_FILE"
fi

if ! eval "$REQ_CMD"; then
    echo "[!] Full requirements install failed."
    echo "[!] Attempting to install core runtime dependencies needed for startup..."
fi

# Force the required unplayplay build to avoid stale preinstalled versions.
echo "[*] Cleaning stale unplayplay metadata..."
python -c "import shutil,site; from pathlib import Path; roots=[Path(p) for p in site.getsitepackages()]; roots+=[Path(site.getusersitepackages())] if site.ENABLE_USER_SITE else []; [shutil.rmtree(e, ignore_errors=True) or print('[cleanup] Removed', e) for r in roots if r.is_dir() for e in r.iterdir() if e.name.startswith('unplayplay-0.0.8')]"
echo "[*] Forcing unplayplay==0.0.9..."
pip uninstall -y unplayplay >/dev/null 2>&1 || true
pip install --no-cache-dir --upgrade --force-reinstall "unplayplay==0.0.9"
UNPLAYPLAY_VERSION="$(pip show unplayplay 2>/dev/null | awk -F': ' '/^Version:/{print $2}')"
if [ "$UNPLAYPLAY_VERSION" != "0.0.9" ]; then
    echo "[FATAL] Expected unplayplay 0.0.9 but found '${UNPLAYPLAY_VERSION:-not installed}'."
    exit 1
fi

# Ensure core HTTP/runtime deps are always present even if the full install fails.
# This avoids the common "Missing dependency: requests" fatal error on startup.
pip install --upgrade requests urllib3 flask certifi pillow mutagen || {
    echo "[FATAL] Failed to install core runtime dependencies."
    exit 1
}

python -c "import requests, urllib3, flask, certifi, mutagen; from PIL import Image" || {
    echo "[FATAL] Core dependencies are still missing after install attempt."
    echo "Try: pip install --upgrade requests urllib3 flask certifi pillow mutagen"
    exit 1
}

# -------------------------------
# INSTALL LIBRESPOT
# -------------------------------
echo "[*] Installing librespot..."

mkdir -p vendor/librespot
pip install --no-deps --upgrade --target vendor/librespot git+https://github.com/kokarare1212/librespot-python

# -------------------------------
# INITIAL SETUP
# -------------------------------
echo "[*] Running initial setup..."

python orpheus.py settings refresh

# -------------------------------
# FIX CERTS
# -------------------------------
echo "[*] Updating certifi..."

pip install --upgrade --ignore-installed certifi || pip install --force-reinstall --no-deps certifi

# -------------------------------
# INSTALL MODULES
# -------------------------------
echo "[*] Installing modules..."

mkdir -p modules

git clone https://github.com/bascurtiz/orpheusdl-amazonmusic modules/amazonmusic
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