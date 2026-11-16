#!/usr/bin/env python3
"""
Test completo end-to-end.

1. Crea un video di prova con sottotitoli francesi "impressi" che cambiano posizione
   (in basso / in alto / entrambi insieme) + un logo fisso nell'angolo.
2. Avvia un finto server Ollama (inizialmente SENZA modello, per provare anche il download).
3. Esegue il programma (lo script Python oppure l'app compilata con --eseguibile).
4. Controlla frase per frase il file .srt, la posizione (alto/basso), i tempi e il video finale.

Uso:
    python tests/test_completo.py
    python tests/test_completo.py --eseguibile "dist/Traduttore Sottotitoli.app/Contents/MacOS/Traduttore Sottotitoli"
"""
import argparse
import difflib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

QUI = os.path.dirname(os.path.abspath(__file__))
PROGRAMMA = os.path.join(QUI, "..", "traduci_sottotitoli.py")
W, H, FPS, DURATA = 1280, 720, 25, 60

# (testo, inizio, fine, posizione)
FRASI = [
    ("Bonjour, comment allez-vous ?", 1, 6, "basso"),
    ("Je suis très content de te voir.", 7, 12, "basso"),
    ("Nous partons demain matin.", 13, 18, "alto"),
    ("Il fait très froid ce soir.", 19, 24, "alto"),
    ("Tu veux un café ?", 25, 30, "basso"),
    ("La musique commence.", 26, 30, "alto"),          # insieme a quella sotto
    ("C'est une belle journée.", 31, 36, "alto"),
    ("Attends-moi ici.", 37, 42, "basso"),
    ("Merci beaucoup, mon ami.", 43, 48, "alto"),
    ("Quelle heure est-il ?", 49, 54, "basso"),
]


def trova_font():
    for c in ["/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/Library/Fonts/Arial Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "C:/Windows/Fonts/arialbd.ttf"]:
        if os.path.exists(c):
            return c
    raise SystemExit("font non trovato per creare il video di prova")


def trova_ffmpeg():
    import shutil
    if shutil.which("ffmpeg"):
        return shutil.which("ffmpeg")
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def crea_video(percorso):
    font = ImageFont.truetype(trova_font(), 44)
    logo_font = ImageFont.truetype(trova_font(), 28)
    # sfondo in movimento con zone chiare (per mettere alla prova il rilevamento)
    yy, xx = np.mgrid[0:H, 0:W]
    grezzo = os.path.join(os.path.dirname(percorso), "grezzo.avi")
    out = cv2.VideoWriter(grezzo, cv2.VideoWriter_fourcc(*"MJPG"), FPS, (W, H))
    for i in range(DURATA * FPS):
        t = i / FPS
        b = (127 + 100 * np.sin(xx / 90 + t)).astype(np.uint8)
        g = (127 + 100 * np.sin(yy / 70 - t * 0.7)).astype(np.uint8)
        r = (127 + 100 * np.sin((xx + yy) / 150 + t * 0.3)).astype(np.uint8)
        img = Image.fromarray(np.dstack([r, g, b]))
        d = ImageDraw.Draw(img)
        d.text((25, 18), "TV5 MONDE", font=logo_font, fill="white", stroke_width=2, stroke_fill="black")
        d.text((W - 160, 18), f"{int(t // 60):02d}:{int(t % 60):02d}", font=logo_font, fill="white",
               stroke_width=2, stroke_fill="black")      # orologio nell'angolo: va ignorato
        for testo, a, z, pos in FRASI:
            if a <= t < z:
                larg = d.textlength(testo, font=font)
                y = 60 if pos == "alto" else H - 110
                colore = "yellow" if testo.startswith("La musique") else "white"
                d.text(((W - larg) / 2, y), testo, font=font, fill=colore, stroke_width=3, stroke_fill="black")
        out.write(cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR))
    out.release()
    ff = trova_ffmpeg()
    r = subprocess.run([ff, "-y", "-loglevel", "error", "-i", grezzo, "-f", "lavfi", "-i",
                        f"sine=frequency=440:duration={DURATA}", "-map", "0:v", "-map", "1:a",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", percorso])
    if r.returncode != 0:      # ffmpeg senza lavfi: video senza audio
        subprocess.run([ff, "-y", "-loglevel", "error", "-i", grezzo, "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", percorso], check=True)
    os.remove(grezzo)


class FintoOllama(BaseHTTPRequestHandler):
    modelli = []        # all'inizio nessun modello: il programma deve scaricarlo

    def log_message(self, *a):
        pass

    def _json(self, o):
        b = json.dumps(o).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self._json({"models": [{"name": m} for m in self.modelli]})

    def do_POST(self):
        dati = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/api/pull":
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            for k in range(0, 101, 25):
                riga = {"status": "downloading", "total": 100, "completed": k}
                self.wfile.write((json.dumps(riga) + "\n").encode())
            FintoOllama.modelli.append(dati["model"])
            self.wfile.write(b'{"status":"success"}\n')
            return
        p = dati["prompt"]
        righe = re.findall(r"^(\d+)\. (.+)$", p, re.M)
        if righe:
            out = "\n".join(f"{n}. [IT] {t}" for n, t in righe)
        else:
            out = "[IT] " + p.split("tradurre:\n")[-1].split("\n")[0]
        self._json({"response": out})


def porta_libera():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def norm(t):
    return "".join(c.lower() for c in t if c.isalnum())


def leggi_srt(percorso):
    voci = []
    for blocco in open(percorso, encoding="utf-8").read().strip().split("\n\n"):
        r = blocco.split("\n")
        a, b = r[1].split(" --> ")
        sec = lambda x: int(x[:2]) * 3600 + int(x[3:5]) * 60 + float(x[6:].replace(",", "."))
        testo = " ".join(r[2:])
        voci.append((sec(a), sec(b), testo.replace("{\\an8}", ""), "alto" if "{\\an8}" in testo else "basso"))
    return voci


def durata(video):
    r = subprocess.run([trova_ffmpeg(), "-hide_banner", "-i", video], capture_output=True, text=True)
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", r.stderr)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)), "Audio:" in r.stderr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--eseguibile", help="app compilata da provare (altrimenti lo script Python)")
    ap.add_argument("--ocr", default="auto")
    ap.add_argument("--cartella", help="dove creare i file (default: cartella temporanea)")
    a = ap.parse_args()

    cartella = a.cartella or tempfile.mkdtemp(prefix="test_traduttore_")
    os.makedirs(cartella, exist_ok=True)
    video = os.path.join(cartella, "prova_fr.mp4")
    print("Creo il video di prova...", flush=True)
    crea_video(video)

    porta = porta_libera()
    server = ThreadingHTTPServer(("127.0.0.1", porta), FintoOllama)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    cmd = [a.eseguibile] if a.eseguibile else [sys.executable, PROGRAMMA]
    cmd += [video, "--lingua", "fr", "--url", f"http://127.0.0.1:{porta}", "--ocr", a.ocr]
    print("Eseguo:", " ".join(cmd), flush=True)
    r = subprocess.run(cmd)
    errori = []
    if r.returncode != 0:
        errori.append(f"il programma è uscito con codice {r.returncode}")

    srt = os.path.join(cartella, "prova_fr_ITA.srt")
    mp4 = os.path.join(cartella, "prova_fr_ITA.mp4")
    if not os.path.exists(srt):
        raise SystemExit("FALLITO: nessun file .srt creato\n" + "\n".join(errori))
    voci = leggi_srt(srt)
    print(f"\n{len(voci)} sottotitoli nel file .srt (attesi {len(FRASI)}):")
    trovate = 0
    for testo, ini, fin, pos in FRASI:
        migliore = max(voci, key=lambda v: difflib.SequenceMatcher(None, norm(v[2]), norm("[IT] " + testo)).ratio()
                       if min(v[1], fin) > max(v[0], ini) and v[3] == pos else 0)
        somi = difflib.SequenceMatcher(None, norm(migliore[2]), norm("[IT] " + testo)).ratio()
        ok = (somi > 0.75 and migliore[3] == pos and abs(migliore[0] - ini) < 0.8
              and abs(migliore[1] - fin) < 0.8)
        trovate += ok
        print(f"  {'OK ' if ok else 'NO '} [{pos:5s}] {ini:>4.1f}-{fin:<4.1f}s  {testo!r:38s} -> "
              f"{migliore[2]!r} ({migliore[0]:.1f}-{migliore[1]:.1f}s, {migliore[3]})")
    if trovate < len(FRASI) - 1:
        errori.append(f"riconosciute correttamente solo {trovate}/{len(FRASI)} frasi")
    if len(voci) > len(FRASI) + 2:
        errori.append(f"troppi sottotitoli ({len(voci)}): doppioni o rumore")
    if any("TV5" in v[2] or re.search(r"\d\d:\d\d", v[2]) for v in voci):
        errori.append("logo/orologio scambiati per sottotitoli")

    if not os.path.exists(mp4):
        errori.append("video tradotto non creato")
    else:
        d, audio = durata(mp4)
        print(f"\nVideo tradotto: {d:.1f}s, audio: {'sì' if audio else 'no'}")
        if abs(d - DURATA) > 1:
            errori.append(f"durata del video sbagliata: {d:.1f}s invece di {DURATA}s")
        # nel fotogramma a 15s la scritta in alto deve essere stata sostituita
        cap = cv2.VideoCapture(mp4)
        cap.set(cv2.CAP_PROP_POS_MSEC, 15000)
        ok, fr = cap.read()
        cap0 = cv2.VideoCapture(video)
        cap0.set(cv2.CAP_PROP_POS_MSEC, 15000)
        ok0, fr0 = cap0.read()
        if ok and ok0:
            diff_alto = float(np.mean(cv2.absdiff(fr[40:140, 300:980], fr0[40:140, 300:980])))
            diff_centro = float(np.mean(cv2.absdiff(fr[300:420], fr0[300:420])))
            print(f"Differenza zona sottotitolo: {diff_alto:.1f}  /  zona centrale: {diff_centro:.1f}")
            if diff_alto < 15:
                errori.append("il sottotitolo originale in alto non sembra sostituito")
            if diff_centro > 8:
                errori.append("il centro dell'immagine è stato alterato")

    if os.path.exists(os.path.join(cartella, "prova_fr_ITA_progresso.json")):
        errori.append("il file dei progressi non è stato rimosso alla fine")

    print()
    if errori:
        print("TEST FALLITO:\n - " + "\n - ".join(errori))
        sys.exit(1)
    print(f"TEST SUPERATO ({trovate}/{len(FRASI)} frasi corrette)")


if __name__ == "__main__":
    main()
