#!/usr/bin/env python3
"""
Genera le immagini del README (docs/img/*.png) usando l'app vera sul video di prova.
Linux:  xvfb-run -s "-screen 0 1120x780x24" python docs/genera_screenshot.py
Serve ImageMagick (comando `import`) per catturare la finestra.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer

QUI = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(QUI, ".."), os.path.join(QUI, "..", "tests")]
os.environ["HOME"] = tempfile.mkdtemp()          # impostazioni pulite

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import test_completo as T  # noqa: E402
import traduci_sottotitoli as P  # noqa: E402

ITALIANO = {
    "Bonjour, comment allez-vous ?": "Buongiorno, come state?",
    "Je suis très content de te voir.": "Sono molto contento di vederti.",
    "Nous partons demain matin.": "Partiamo domani mattina.",
    "Il fait très froid ce soir.": "Fa molto freddo stasera.",
    "Tu veux un café ?": "Vuoi un caffè?",
    "La musique commence.": "La musica comincia.",
    "C'est une belle journée.": "È una bella giornata.",
    "Attends-moi ici.": "Aspettami qui.",
    "Merci beaucoup, mon ami.": "Grazie mille, amico mio.",
    "Quelle heure est-il ?": "Che ore sono?",
}


def traduci(testo):
    import difflib
    migliore = max(ITALIANO, key=lambda k: difflib.SequenceMatcher(None, T.norm(k), T.norm(testo)).ratio())
    return ITALIANO[migliore]


class OllamaItaliano(T.FintoOllama):
    modelli = ["qwen2.5:7b"]

    def do_POST(self):
        dati = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        righe = re.findall(r"^(\d+)\. (.+)$", dati["prompt"], re.M)
        if righe:
            out = "\n".join(f"{n}. {traduci(t)}" for n, t in righe)
        else:
            out = traduci(dati["prompt"].split("tradurre:\n")[-1].split("\n")[0])
        self._json({"response": out})


def main():
    img_dir = os.path.join(QUI, "img")
    os.makedirs(img_dir, exist_ok=True)
    lavoro = tempfile.mkdtemp()
    video = os.path.join(lavoro, "film_francese.mp4")
    print("Creo il video di prova...", flush=True)
    T.crea_video(video)

    porta = T.porta_libera()
    srv = ThreadingHTTPServer(("127.0.0.1", porta), OllamaItaliano)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    elabora_orig = P.elabora
    P.elabora = lambda **kw: elabora_orig(url=f"http://127.0.0.1:{porta}", **kw)

    app = P.App()
    app.root.geometry("1120x780+0+0")
    scatti = {}

    def foto(nome):
        app.root.update()
        out = os.path.join(img_dir, nome)
        subprocess.run(["import", "-window", "root", "-crop", "1120x780+0+0", "+repage", out], check=True)
        print("  screenshot:", out, flush=True)

    def passo():
        fase = app.fase
        if "inizio" not in scatti:
            scatti["inizio"] = 1
            app.carica_video(video)
            app.root.after(1500, lambda: (foto("finestra.png"), app.avvia()))
        elif fase == "lettura" and app.trovati >= 4 and "lettura" not in scatti:
            scatti["lettura"] = 1
            foto("lettura.png")
        elif fase == "video" and "video" not in scatti and app.foto is not None and app.barra["value"] > 0.75:
            scatti["video"] = 1
            foto("creazione_video.png")
        elif app.risultato and "fine" not in scatti:
            scatti["fine"] = 1
            foto("fatto.png")
            app.root.after(500, app.root.destroy)
            return
        app.root.after(300, passo)

    app.root.after(500, passo)
    app.mainloop()

    # prima / dopo
    tradotto = os.path.join(lavoro, "film_francese_ITA.mp4")
    righe = []
    for sec in (9, 21, 28):
        coppia = []
        for v in (video, tradotto):
            cap = cv2.VideoCapture(v)
            cap.set(cv2.CAP_PROP_POS_MSEC, sec * 1000)
            ok, fr = cap.read()
            coppia.append(cv2.resize(fr, (640, 360), interpolation=cv2.INTER_AREA))
        sep = np.full((360, 12, 3), 255, np.uint8)
        righe.append(np.hstack([coppia[0], sep, coppia[1]]))
        righe.append(np.full((12, 1292, 3), 255, np.uint8))
    titolo = np.full((60, 1292, 3), 255, np.uint8)
    for x, t in ((230, "PRIMA (francese)"), (880, "DOPO (italiano)")):
        cv2.putText(titolo, t, (x, 42), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (60, 30, 30), 2, cv2.LINE_AA)
    cv2.imwrite(os.path.join(img_dir, "prima_dopo.png"), np.vstack([titolo] + righe[:-1]))
    print("  immagine:", os.path.join(img_dir, "prima_dopo.png"))


if __name__ == "__main__":
    main()
