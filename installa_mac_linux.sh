#!/usr/bin/env bash
# Installazione "da sorgente" (in alternativa all'app già pronta per Mac).
set -e
cd "$(dirname "$0")"
echo "=== Installazione Traduttore Sottotitoli ==="
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
if [ "$(uname)" = "Darwin" ]; then
  pip install -r requirements-mac.txt
  if [ ! -d /Applications/Ollama.app ] && ! command -v ollama >/dev/null; then
    if command -v brew >/dev/null; then brew install --cask ollama; else
      echo "Scarica Ollama da https://ollama.com/download e installalo."; fi
  fi
else
  pip install -r requirements.txt
  command -v ollama >/dev/null || curl -fsSL https://ollama.com/install.sh | sh
  python3 -c "import tkinter" 2>/dev/null || sudo apt-get install -y python3-tk
fi
echo "Fatto! Avvia con: ./avvia_mac_linux.sh  (il modello AI si scarica da solo al primo uso)"
