#!/usr/bin/env bash
# Crea "Traduttore Sottotitoli.app" per l'architettura del Mac su cui gira
# (Intel o Apple Silicon). Usato anche da GitHub Actions.
set -euo pipefail
cd "$(dirname "$0")"
python3 -m pip install -r requirements-mac.txt "pyinstaller>=6.10"
rm -rf build dist
python3 -m PyInstaller --noconfirm --clean --windowed \
  --name "Traduttore Sottotitoli" \
  --osx-bundle-identifier io.github.basoxxx.translatefilm \
  --collect-all imageio_ffmpeg \
  --hidden-import objc --hidden-import Foundation --hidden-import Vision --hidden-import Quartz \
  --exclude-module easyocr --exclude-module torch --exclude-module torchvision \
  --exclude-module argostranslate --exclude-module matplotlib --exclude-module scipy \
  traduci_sottotitoli.py
APP="dist/Traduttore Sottotitoli.app"
# versione e nome leggibili nel Finder
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $(python3 -c 'import re;print(re.search(r"VERSIONE = \"(.+?)\"",open("traduci_sottotitoli.py").read()).group(1))')" "$APP/Contents/Info.plist"
# versione minima di macOS REALE: la più alta richiesta da un qualsiasi binario dentro l'app
MINIMO=$(find "$APP" -type f \( -perm -u+x -o -name "*.so" -o -name "*.dylib" \) -print0 \
  | xargs -0 otool -l 2>/dev/null \
  | awk '/LC_BUILD_VERSION/{b=1} b&&/minos/{print $2; b=0} /LC_VERSION_MIN_MACOSX/{v=1} v&&/version/{print $2; v=0}' \
  | sort -t. -k1,1n -k2,2n | tail -1)
MINIMO=${MINIMO:-12.0}
case "$MINIMO" in 1[0-1].*) MINIMO=12.0;; esac   # Vision con le lingue usate richiede almeno macOS 12
echo "$MINIMO" > dist/MINIMO.txt
/usr/libexec/PlistBuddy -c "Delete :LSMinimumSystemVersion" "$APP/Contents/Info.plist" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :LSMinimumSystemVersion string $MINIMO" "$APP/Contents/Info.plist"
echo "Versione minima di macOS richiesta: $MINIMO"
# firma ad-hoc (necessaria su Apple Silicon)
codesign --force --deep --sign - "$APP"
echo "Creato: $APP ($(uname -m))"
