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
/usr/libexec/PlistBuddy -c "Add :LSMinimumSystemVersion string 12.0" "$APP/Contents/Info.plist" 2>/dev/null || true
# firma ad-hoc (necessaria su Apple Silicon)
codesign --force --deep --sign - "$APP"
echo "Creato: $APP ($(uname -m))"
