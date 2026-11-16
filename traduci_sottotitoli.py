#!/usr/bin/env python3
"""
Traduttore di Sottotitoli "impressi" nel video (hardsub) -> Italiano
=====================================================================

1. Controlla da solo dove compaiono i sottotitoli: in basso, in alto, oppure
   un po' in basso e un po' in alto a seconda della scena (li segue entrambi).
   Ignora le scritte fisse e quelle negli angoli (loghi, orologi, punteggi...).
2. Legge il testo con l'OCR locale: su Mac quello di sistema (Apple Vision, veloce
   e preciso), su Windows/Linux EasyOCR.
3. Traduce in italiano con un'AI locale (Ollama, es. qwen2.5) oppure Argos.
4. Copre il sottotitolo originale e ci scrive sopra quello italiano,
   mantenendo l'audio (ffmpeg). Salva anche il file .srt italiano.

Pensato per video lunghi (anche 2 ore e oltre):
 - stima iniziale del tempo e tempo rimanente durante il lavoro
 - salvataggio automatico: se lo interrompi (o si spegne il PC) riparte da dove era
 - traduzione a blocchi e OCR solo quando la scritta cambia

Uso:
    python traduci_sottotitoli.py                    (apre la finestra grafica)
    python traduci_sottotitoli.py video.mp4 --lingua fr --prova 120   (prova sui primi 2 minuti)
    python traduci_sottotitoli.py video.mp4 --lingua fr
    python traduci_sottotitoli.py video.mp4 --lingua fr --posizione basso --modo inpaint
"""

import argparse
import difflib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

LINGUE = {
    "fr": "francese", "en": "inglese", "es": "spagnolo", "de": "tedesco",
    "pt": "portoghese", "nl": "olandese", "ro": "rumeno", "pl": "polacco",
    "ru": "russo", "ja": "giapponese", "ko": "coreano", "ch_sim": "cinese",
    "ar": "arabo", "tr": "turco",
}
CODICI_ARGOS = {"ch_sim": "zh"}
CODICI_VISION = {
    "fr": "fr-FR", "en": "en-US", "es": "es-ES", "de": "de-DE", "pt": "pt-BR", "nl": "nl-NL",
    "ro": "ro-RO", "pl": "pl-PL", "ru": "ru-RU", "ja": "ja-JP", "ko": "ko-KR", "ch_sim": "zh-Hans",
    "ar": "ar-SA", "tr": "tr-TR",
}
VERSIONE = "1.1.0"
FILE_IMPOSTAZIONI = os.path.join(os.path.expanduser("~"), ".traduttore_sottotitoli.json")
MAX_LARGHEZZA_OCR = 1280      # i video più grandi (es. 4K) vengono rimpiccioliti per l'OCR
BLOCCO_TRADUZIONE = 8         # sottotitoli tradotti per ogni richiesta all'AI
SOGLIA_LUMINOSO = 190
MAX_RIUSI = 4                 # dopo N riusi consecutivi rifà comunque l'OCR (sicurezza)

LOG = print                   # sostituito dalla finestra grafica
EVENTO = None                 # la finestra grafica riceve qui fasi, anteprime, traduzioni
FERMA = threading.Event()     # premuto "Interrompi"


def notifica(tipo, *dati):
    if EVENTO:
        try:
            EVENTO(tipo, *dati)
        except Exception:
            pass


def anteprima(frame, larghezza=640):
    """Copia rimpicciolita RGB di un fotogramma, da mostrare nella finestra."""
    h, w = frame.shape[:2]
    scala = larghezza / w
    piccolo = cv2.resize(frame, (larghezza, max(1, int(h * scala))), interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(piccolo, cv2.COLOR_BGR2RGB), scala


class Interrotto(Exception):
    pass


# --------------------------------------------------------------------------
# Utilità
# --------------------------------------------------------------------------
def fmt_durata(s):
    s = int(max(0, s))
    if s >= 3600:
        return f"{s // 3600} h {s % 3600 // 60:02d} min"
    if s >= 60:
        return f"{s // 60} min {s % 60:02d} s"
    return f"{s} s"


def fmt_tempo(t, srt=False):
    ms = int(round(max(0, t) * 1000))
    hh, ms = divmod(ms, 3600000)
    mm, ms = divmod(ms, 60000)
    ss, ms = divmod(ms, 1000)
    return f"{hh:02}:{mm:02}:{ss:02},{ms:03}" if srt else f"{hh:02}:{mm:02}:{ss:02}"


def normalizza(t):
    return "".join(c.lower() for c in t if c.isalnum())


def simili(a, b, soglia=0.75):
    a, b = normalizza(a), normalizza(b)
    if not a or not b:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() > soglia


def conf_sufficiente(testo, conf, conf_min):
    """EasyOCR dà confidenze basse alle righe lunghe anche quando le legge bene."""
    n = len(normalizza(testo))
    return n >= 2 and conf >= (conf_min * 0.5 if n >= 10 else conf_min)


def maschera_testo(img):
    """Pixel che sembrano lettere di sottotitolo: chiari (bianco/giallo) con scuro vicino
    (contorno o ombra). Le zone chiare "piene" (cielo, muri) non contano."""
    g = img.max(axis=2)
    chiaro = g > SOGLIA_LUMINOSO
    scuro = cv2.dilate((g < 90).astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
    return chiaro & scuro


def attraversa_centro(x1, x2, larghezza):
    """I sottotitoli sono centrati: scarta scritte negli angoli (orologi, loghi, punteggi)."""
    return x1 < larghezza * 0.6 and x2 > larghezza * 0.4


def sostituisci_file(sorgente, destinazione, tentativi=10):
    """os.replace con nuovi tentativi (su Windows antivirus/OneDrive/lettori video
    possono tenere il file bloccato per qualche istante)."""
    for k in range(tentativi):
        try:
            os.replace(sorgente, destinazione)
            return True
        except PermissionError:
            if k == tentativi - 1:
                return False
            time.sleep(0.5)


class Cronometro:
    """Calcola il tempo rimanente di una fase e aggiorna barra/log."""

    def __init__(self, nome, progresso, base, peso, f0=0.0):
        self.nome, self.progresso, self.base, self.peso, self.f0 = nome, progresso, base, peso, f0
        self.t0 = time.time()
        self.ultimo_log = self.t0
        self.ultimo_gui = 0

    def aggiorna(self, f):
        ora = time.time()
        f = min(1.0, max(0.0, f))
        trascorso = ora - self.t0
        if f > self.f0 and trascorso > 3:
            rimanente = trascorso * (1 - f) / (f - self.f0)
            msg = f"{self.nome}: {f * 100:.0f}%  -  mancano circa {fmt_durata(rimanente)}"
        else:
            msg = f"{self.nome}: {f * 100:.0f}%  -  calcolo tempo rimanente..."
        if self.progresso and ora - self.ultimo_gui > 0.5:
            self.progresso(self.base + self.peso * f, msg)
            self.ultimo_gui = ora
        if ora - self.ultimo_log > 60:
            LOG(msg)
            self.ultimo_log = ora


def controlla_stop():
    if FERMA.is_set():
        raise Interrotto()


# --------------------------------------------------------------------------
# Strutture dati, informazioni video e salvataggio progressi
# --------------------------------------------------------------------------
@dataclass
class Sottotitolo:
    inizio: float
    fine: float
    zona: str = "basso"
    testi: list = field(default_factory=list)   # [testo, confidenza]
    box: list = None                            # [x1, y1, x2, y2] nel frame
    traduzione: str = ""

    @property
    def testo(self):
        # la lettura migliore: confidenza alta ma anche completa (un frammento corto con
        # confidenza alta non deve battere la frase intera)
        return (max(self.testi, key=lambda t: t[1] * len(normalizza(t[0])))[0]
                if self.testi else "")

    def unisci_box(self, b):
        if self.box is None:
            self.box = list(b)
        else:
            self.box = [min(self.box[0], b[0]), min(self.box[1], b[1]),
                        max(self.box[2], b[2]), max(self.box[3], b[3])]


@dataclass
class Zona:
    nome: str
    y0: int
    y1: int


def trova_ffmpeg():
    """ffmpeg di sistema, oppure quello incluso nel pacchetto imageio-ffmpeg
    (le app Mac non vedono il PATH del terminale, quindi lo includiamo)."""
    if os.environ.get("TRADUTTORE_FFMPEG") != "incluso":
        for c in (shutil.which("ffmpeg"), "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
            if c and os.path.isfile(c):
                return c
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def durata_video(video):
    ff = trova_ffmpeg()
    if not ff:
        return None
    try:
        r = subprocess.run([ff, "-hide_banner", "-i", video], capture_output=True, text=True,
                           errors="ignore", timeout=60)
        m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", r.stderr)
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else None
    except Exception:
        return None


def leggi_info(video):
    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        raise RuntimeError(f"Impossibile aprire il video: {video}")
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    tot = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    ok, _ = cap.read()
    cap.release()
    if not ok or w <= 0 or h <= 0:
        raise RuntimeError("Il video si apre ma i fotogrammi non sono leggibili "
                           "(formato/codec non supportato?). Prova a convertirlo in MP4 H.264.")
    if not fps or fps <= 0 or fps > 240:
        fps = 25.0
        LOG("ATTENZIONE: fps del video non leggibili, uso 25.")
    if tot <= 1:
        durata = durata_video(video)
        if not durata:
            raise RuntimeError("Non riesco a capire quanto dura il video (converti il video in MP4).")
        tot = int(durata * fps)
        LOG(f"Numero di fotogrammi non presente nel file: stimato {tot} dalla durata.")
    return w, h, fps, tot


class Progresso:
    """File <video>_ITA_progresso.json: permette di riprendere un lavoro interrotto."""

    def __init__(self, video, info, impostazioni, suffisso=""):
        self.percorso = os.path.splitext(video)[0] + f"_ITA{suffisso}_progresso.json"
        w, h, fps, tot = info
        self.firma = {"frames": tot, "w": w, "h": h, "byte": os.path.getsize(video),
                      "impostazioni": impostazioni}
        self.dati = {"firma": self.firma}
        if not os.path.exists(self.percorso):
            return
        try:
            with open(self.percorso, encoding="utf-8") as f:
                d = json.load(f)
        except Exception as e:
            LOG(f"ATTENZIONE: file dei progressi danneggiato ({e}): ricomincio da capo.")
            sostituisci_file(self.percorso, self.percorso + ".bak")
            return
        if d.get("firma") == self.firma:
            self.dati = d
            LOG("Trovato un lavoro interrotto su questo video: riprendo da dove era rimasto.")
        elif {k: v for k, v in d.get("firma", {}).items() if k != "impostazioni"} == \
                {k: v for k, v in self.firma.items() if k != "impostazioni"}:
            LOG("Trovato un lavoro interrotto, ma con impostazioni diverse (lingua/posizione/...): "
                "ricomincio da capo. Le traduzioni già fatte vengono riusate.")
            self.dati["traduzioni"] = d.get("traduzioni", {})

    def salva(self):
        tmp = self.percorso + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.dati, f, ensure_ascii=False)
        if not sostituisci_file(tmp, self.percorso):
            LOG("ATTENZIONE: impossibile aggiornare il file dei progressi (bloccato da un altro programma).")

    def elimina(self):
        for p in (self.percorso, self.percorso + ".tmp"):
            try:
                os.remove(p)
            except OSError:
                pass


def apri_a(video, frame):
    """Apre il video posizionato su `frame`, verificando che il salto sia preciso."""
    cap = cv2.VideoCapture(video)
    if frame <= 0:
        return cap
    cap.set(cv2.CAP_PROP_POS_FRAMES, frame)
    if abs(cap.get(cv2.CAP_PROP_POS_FRAMES) - frame) > 1:
        LOG("Il salto nel video non è preciso con questo formato: avanzo fotogramma per fotogramma...")
        cap.release()
        cap = cv2.VideoCapture(video)
        for _ in range(frame):
            if not cap.grab():
                break
    return cap


# --------------------------------------------------------------------------
# 1) OCR
# --------------------------------------------------------------------------
class OcrVision:
    """OCR di sistema del Mac (Apple Vision, usa il Neural Engine). Stessa interfaccia
    di easyocr.Reader.readtext: [(bbox 4 punti, testo, confidenza)]."""

    def __init__(self, lingua):
        import objc
        import Vision
        from Foundation import NSData
        self.objc, self.Vision, self.NSData = objc, Vision, NSData
        codice = CODICI_VISION.get(lingua)
        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        supportate, _ = req.supportedRecognitionLanguagesAndReturnError_(None)
        supportate = [str(x) for x in (supportate or [])]
        if not codice or codice not in supportate:
            raise RuntimeError(f"la lingua '{lingua}' non è supportata dall'OCR del Mac "
                               f"(supportate: {', '.join(supportate)})")
        self.lingue = [codice] + (["en-US"] if codice != "en-US" else [])

    def readtext(self, img, detail=1, paragraph=False):
        H, W = img.shape[:2]
        ok, buf = cv2.imencode(".png", img)
        if not ok:
            return []
        out = []
        with self.objc.autorelease_pool():      # evita che la memoria cresca nei video lunghi
            data = self.NSData.dataWithBytes_length_(buf.tobytes(), len(buf))
            handler = self.Vision.VNImageRequestHandler.alloc().initWithData_options_(data, None)
            req = self.Vision.VNRecognizeTextRequest.alloc().init()
            req.setRecognitionLevel_(self.Vision.VNRequestTextRecognitionLevelAccurate)
            req.setUsesLanguageCorrection_(True)
            req.setRecognitionLanguages_(self.lingue)
            riuscito, errore = handler.performRequests_error_([req], None)
            if not riuscito:
                raise RuntimeError(f"OCR del Mac non riuscito: {errore}")
            for oss in req.results() or []:
                candidati = oss.topCandidates_(1)
                if not candidati:
                    continue
                c = candidati[0]
                bb = oss.boundingBox()           # coordinate 0-1 con origine in basso a sinistra
                x0, w = bb.origin.x * W, bb.size.width * W
                y0, h = (1 - bb.origin.y - bb.size.height) * H, bb.size.height * H
                out.append(([[x0, y0], [x0 + w, y0], [x0 + w, y0 + h], [x0, y0 + h]],
                            str(c.string()), float(c.confidence())))
        return out


def crea_easyocr(lingua):
    import easyocr
    try:
        import torch
        gpu = torch.cuda.is_available()
    except Exception:
        gpu = False
    langs = list(dict.fromkeys([lingua, "en"]))
    LOG(f"OCR: EasyOCR ({', '.join(langs)}) - scheda video NVIDIA: {'sì' if gpu else 'no (più lento)'}")
    return easyocr.Reader(langs, gpu=gpu, verbose=False)


def crea_ocr(lingua, motore_ocr="auto"):
    errore_vision = None
    if motore_ocr == "vision" or (motore_ocr == "auto" and sys.platform == "darwin"):
        try:
            r = OcrVision(lingua)
            LOG(f"OCR: Apple Vision (sistema del Mac) - lingua {r.lingue[0]}")
            return r
        except Exception as e:
            if motore_ocr == "vision":
                raise RuntimeError(f"OCR del Mac non disponibile: {e}") from None
            errore_vision = e
            LOG(f"OCR del Mac non utilizzabile ({e}): provo EasyOCR...")
    try:
        return crea_easyocr(lingua)
    except ImportError:
        if errore_vision:
            raise RuntimeError(f"L'OCR del Mac non può leggere questa lingua ({errore_vision}). "
                               f"Aggiorna macOS: le versioni più recenti supportano più lingue.") from None
        raise RuntimeError("Nessun OCR disponibile: installa EasyOCR (pip install easyocr).") from None


def ocr_area(reader, crop, conf_min, statici=()):
    """Ritorna (testo, confidenza media, box nel crop) oppure None."""
    righe = []
    for bbox, testo, conf in reader.readtext(crop, detail=1, paragraph=False):
        testo = testo.strip()
        if not conf_sufficiente(testo, conf, conf_min):
            continue
        if any(simili(testo, s) for s in statici):
            continue          # logo / scritta fissa
        xs = [p[0] for p in bbox]
        ys = [p[1] for p in bbox]
        if not attraversa_centro(min(xs), max(xs), crop.shape[1]):
            continue
        righe.append((min(xs), min(ys), max(xs), max(ys), testo, conf))
    if not righe:
        return None
    righe.sort(key=lambda r: ((r[1] + r[3]) / 2, r[0]))
    gruppi, attuale = [], [righe[0]]
    for r in righe[1:]:
        hh = attuale[-1][3] - attuale[-1][1]
        if abs((r[1] + r[3]) / 2 - (attuale[-1][1] + attuale[-1][3]) / 2) < hh * 0.6:
            attuale.append(r)
        else:
            gruppi.append(attuale)
            attuale = [r]
    gruppi.append(attuale)
    testo = " ".join(" ".join(x[4] for x in sorted(g, key=lambda r: r[0])) for g in gruppi)
    conf = float(np.mean([r[5] for r in righe]))
    box = [min(r[0] for r in righe), min(r[1] for r in righe),
           max(r[2] for r in righe), max(r[3] for r in righe)]
    return testo, conf, [int(v) for v in box]


def raggruppa_testi(elenco):
    """Raggruppa testi simili. elenco: [(testo, chiave)] -> [[testo, set(chiavi)]]"""
    gruppi = []
    for testo, chiave in elenco:
        for g in gruppi:
            if simili(g[0], testo):
                g[1].add(chiave)
                break
        else:
            gruppi.append([testo, {chiave}])
    return gruppi


def analizza_video(video, reader, info, conf_min, progresso=None):
    """Pre-analisi su ~40 fotogrammi sparsi: dove stanno i sottotitoli, scritte fisse,
    velocità del PC (per la stima dei tempi)."""
    w, h, fps, tot = info
    scala_ocr = min(1.0, MAX_LARGHEZZA_OCR / w)
    scala = min(1.0, 960 / w)       # più piccolo: qui basta capire DOVE sta il testo
    n = int(np.clip(tot / fps / 2, 10, 40))
    campioni = np.unique(np.linspace(tot * 0.02, tot * 0.98, n).astype(int))
    LOG(f"Analisi preliminare su {len(campioni)} fotogrammi sparsi nel video...")

    cap = cv2.VideoCapture(video)
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(tot * 0.5))
    t = time.time()
    letti = 0
    for _ in range(120):
        if not cap.grab():
            break
        letti += 1
    vel_dec = letti / max(1e-3, time.time() - t) if letti else 50.0

    ril, tempi = [], []
    for k, f in enumerate(campioni):
        controlla_stop()
        if progresso:
            progresso(0.0, f"Analisi preliminare: {k + 1}/{len(campioni)}")
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(f))
        ok, fr = cap.read()
        if not ok:
            continue
        piccolo = cv2.resize(fr, None, fx=scala, fy=scala, interpolation=cv2.INTER_AREA) if scala < 1 else fr
        sh = piccolo.shape[0]
        lum = maschera_testo(piccolo)
        t = time.time()
        risultati = reader.readtext(piccolo, detail=1, paragraph=False)
        tempi.append(time.time() - t)
        for bbox, testo, conf in risultati:
            testo = testo.strip()
            if not conf_sufficiente(testo, conf, max(conf_min, 0.4)):
                continue
            xs = [int(p[0]) for p in bbox]
            ys = [int(p[1]) for p in bbox]
            if not attraversa_centro(min(xs), max(xs), piccolo.shape[1]):
                continue
            y1n, y2n = min(ys) / sh, max(ys) / sh
            cy = (y1n + y2n) / 2
            banda = "alto" if cy < 0.4 else "basso" if cy > 0.6 else None
            if banda is None:
                continue
            roi = lum[max(0, min(ys)):max(ys), max(0, min(xs)):max(xs)]
            chiaro = float(np.mean(roi)) if roi.size else 0.0
            ril.append({"f": int(f), "banda": banda, "y1": y1n, "y2": y2n,
                        "testo": testo, "lum": chiaro})
    cap.release()
    if not tempi:
        raise RuntimeError("Non riesco a leggere i fotogrammi del video per l'analisi.")
    n_ok = len(tempi)

    # scritte fisse: stesso testo presente in molti campioni sparsi su gran parte del video
    # (un vero sottotitolo dura pochi secondi, un logo resta per minuti)
    statici = []
    for testo, frames in raggruppa_testi([(r["testo"], r["f"]) for r in ril]):
        arco = (max(frames) - min(frames)) / fps
        if len(frames) >= max(4, 0.4 * n_ok) and arco >= max(30.0, 0.5 * tot / fps):
            statici.append(testo)
    if statici:
        LOG(f"Scritte fisse ignorate (loghi ecc.): {', '.join(statici)}")

    bande, lum_vals, con_testo = {}, [], set()
    for banda in ("basso", "alto"):
        rr = [r for r in ril if r["banda"] == banda
              and not any(simili(r["testo"], s) for s in statici)]
        distinti = raggruppa_testi([(r["testo"], r["f"]) for r in rr])
        if len(distinti) >= 2:
            lh = float(np.median([r["y2"] - r["y1"] for r in rr]))
            if banda == "basso":
                y0 = max(0.5, float(np.percentile([r["y1"] for r in rr], 5)) - 1.5 * lh)
                bande[banda] = [y0, 1.0]
            else:
                y1 = min(0.5, float(np.percentile([r["y2"] for r in rr], 95)) + 1.5 * lh)
                bande[banda] = [0.0, y1]
            lum_vals += [r["lum"] for r in rr]
            con_testo |= {r["f"] for r in rr}
        LOG(f"  sottotitoli {banda}: {'TROVATI' if banda in bande else 'non visti'} "
            f"({len(distinti)} frasi diverse nei campioni)")

    return {
        "bande": bande,
        "statici": statici,
        "t_ocr": float(np.mean(tempi)) * (scala_ocr / scala) ** 2,
        "vel_dec": vel_dec,
        "frac_testo": len(con_testo) / n_ok,
        "luminosi": bool(lum_vals) and float(np.median(lum_vals)) > 0.03,
    }


def scegli_zone(analisi, posizione, zona, h):
    """Restituisce le fasce del fotogramma da controllare (mai sovrapposte)."""
    predef = {"basso": [max(0.5, 1 - zona), 1.0], "alto": [0.0, min(0.5, zona * 0.85)]}
    # "auto": controlla SEMPRE sia sopra che sotto, perché la posizione può cambiare
    # da scena a scena. Se la pre-analisi li ha visti usa l'altezza misurata (se più ampia).
    nomi = [posizione] if posizione in ("basso", "alto") else ["basso", "alto"]
    zone = []
    for n in nomi:
        a, b = predef[n]
        if n in analisi["bande"]:
            ma, mb = analisi["bande"][n]
            a, b = (max(0.5, min(a, ma)), 1.0) if n == "basso" else (0.0, min(0.5, max(b, mb)))
        zone.append(Zona(n, int(a * h), int(b * h)))
    return zone


class LettoreZona:
    """Decide se serve davvero l'OCR: se la scritta nella fascia non è cambiata
    rispetto al controllo precedente riusa il risultato (molto più veloce)."""

    def __init__(self, reader, analisi, conf_min):
        self.reader, self.conf_min = reader, conf_min
        self.statici = analisi["statici"]
        self.luminosi = analisi["luminosi"]
        self.prec, self.ris = None, None
        self.riusi = 0
        self.ocr_fatti = 0
        self.stessa_scritta = False   # True se la scritta (pixel) è uguale al controllo precedente

    def _riusa(self):
        self.riusi += 1
        return self.ris

    def leggi(self, crop):
        self.stessa_scritta = False
        hh, ww = crop.shape[:2]
        centro = crop[:, int(ww * 0.15):int(ww * 0.85)]
        piccolo = (max(1, centro.shape[1] // 2), max(1, hh // 2))
        if self.luminosi:
            m = cv2.resize(maschera_testo(centro).astype(np.float32), piccolo,
                           interpolation=cv2.INTER_AREA) > 0.15
            if m.sum() < 40:                      # niente testo chiaro: nessun sottotitolo
                self.prec, self.ris, self.riusi = m, None, 0
                return None
            if self.prec is not None and self.prec.shape == m.shape:
                unione = (m | self.prec).sum()
                if unione and (m ^ self.prec).sum() / unione < 0.08:
                    self.stessa_scritta = True
                    if self.riusi < MAX_RIUSI:
                        return self._riusa()
        else:
            m = cv2.resize(cv2.cvtColor(centro, cv2.COLOR_BGR2GRAY), piccolo,
                           interpolation=cv2.INTER_AREA).astype(np.int16)
            if self.prec is not None and self.prec.shape == m.shape:
                # guarda i pixel cambiati in modo netto, non la media (il testo è una piccola parte)
                if np.mean(np.abs(m - self.prec) > 25) < 0.003:
                    self.stessa_scritta = True
                    if self.riusi < MAX_RIUSI:
                        return self._riusa()
        self.prec, self.riusi = m, 0
        self.ris = ocr_area(self.reader, crop, self.conf_min, self.statici)
        self.ocr_fatti += 1
        return self.ris


def trova_sottotitoli(video, get_reader, info, analisi, zone, intervallo, conf_min,
                      prog, progresso):
    w, h, fps, tot = info
    d = prog.dati
    if d.get("ocr_completo"):
        LOG("Lettura dei sottotitoli già completata in precedenza.")
        return [Sottotitolo(**s) for s in d["sottotitoli"]]

    passo = max(1, int(round(fps * intervallo)))
    meta = passo / fps / 2          # metà del vero intervallo tra due controlli
    scala = min(1.0, MAX_LARGHEZZA_OCR / w)
    sottotitoli = [Sottotitolo(**s) for s in d.get("sottotitoli", [])]
    correnti = {z.nome: None for z in zone}
    for nome, s in d.get("correnti", {}).items():
        if nome in correnti:
            correnti[nome] = Sottotitolo(**s)
    mancati = {z.nome: 0 for z in zone}
    i = d.get("riprendi_da", 0)
    i_iniziale = i

    if i > 0:
        LOG(f"Riprendo la lettura da {fmt_tempo(i / fps)}.")
    cap = apri_a(video, i)
    LOG(f"Leggo i sottotitoli ({', '.join(z.nome for z in zone)}) ogni {passo / fps:.2f}s...")

    reader = get_reader()
    lettori = {z.nome: LettoreZona(reader, analisi, conf_min) for z in zone}
    cron = Cronometro("Lettura sottotitoli", progresso, 0.0, 0.5, f0=i / tot)
    ultimo_salv = time.time()
    ultima_anteprima = 0.0

    def salva_checkpoint(pross):
        # salva sia i sottotitoli finiti che quelli ancora "aperti": alla ripresa si
        # continua esattamente dal prossimo controllo, senza doppioni
        d["riprendi_da"] = int(pross)
        d["sottotitoli"] = [asdict(s) for s in sottotitoli]
        d["correnti"] = {k: asdict(c) for k, c in correnti.items() if c}
        prog.salva()

    pross = i
    try:
        while i < tot:
            if i % passo == 0:
                ok, frame = cap.read()
                if not ok:
                    break
                t = i / fps
                for z in zone:
                    crop = frame[z.y0:z.y1]
                    if scala < 1:
                        crop = cv2.resize(crop, None, fx=scala, fy=scala, interpolation=cv2.INTER_AREA)
                    lettore = lettori[z.nome]
                    ris = lettore.leggi(crop)
                    cor = correnti[z.nome]
                    if ris:
                        testo, conf, b = ris
                        box = [int(b[0] / scala), int(b[1] / scala) + z.y0,
                               int(b[2] / scala), int(b[3] / scala) + z.y0]
                        # stesso sottotitolo se il testo è simile, oppure se i pixel della
                        # scritta sono identici e l'OCR l'ha solo letta in modo diverso
                        if cor and (simili(cor.testo, testo) or
                                    (lettore.stessa_scritta and mancati[z.nome] == 0)):
                            cor.fine = t + meta
                            cor.testi.append([testo, conf])
                            cor.unisci_box(box)
                        else:
                            if cor:
                                sottotitoli.append(cor)
                            cor = Sottotitolo(max(0.0, t - meta), t + meta, z.nome, [[testo, conf]])
                            cor.unisci_box(box)
                        mancati[z.nome] = 0
                    elif cor:
                        mancati[z.nome] += 1          # tollera un controllo "perso" dall'OCR
                        if mancati[z.nome] >= 2:
                            sottotitoli.append(cor)
                            cor = None
                    correnti[z.nome] = cor
                if EVENTO and time.time() - ultima_anteprima > 2:
                    ultima_anteprima = time.time()
                    img, sc = anteprima(frame)
                    for z in zone:      # fasce controllate
                        cv2.rectangle(img, (2, int(z.y0 * sc) + 2), (img.shape[1] - 3, int(z.y1 * sc) - 3),
                                      (120, 160, 255), 1)
                    for c in correnti.values():     # sottotitoli riconosciuti in questo momento
                        if c and c.box:
                            b = [int(v * sc) for v in c.box]
                            cv2.rectangle(img, (b[0] - 4, b[1] - 4), (b[2] + 4, b[3] + 4), (40, 220, 90), 2)
                    notifica("frame", img)
                    notifica("trovati", len(sottotitoli) + sum(1 for c in correnti.values() if c))
                pross = i + passo
                cron.aggiorna(i / tot)
                if time.time() - ultimo_salv > 30:
                    salva_checkpoint(pross)
                    ultimo_salv = time.time()
                controlla_stop()
            elif not cap.grab():
                break
            i += 1
    except BaseException:
        cap.release()
        salva_checkpoint(pross)
        raise
    cap.release()

    if i == i_iniziale:
        raise RuntimeError("Impossibile leggere i fotogrammi del video (codec non supportato?).")
    if i < tot * 0.97 and tot - i > fps * 5:
        salva_checkpoint(pross)
        raise RuntimeError(f"La lettura del video si è fermata a {fmt_tempo(i / fps)} su "
                           f"{fmt_tempo(tot / fps)}: il file sembra danneggiato. Prova a convertirlo "
                           f"(es. con HandBrake o: ffmpeg -i video -c:v libx264 -c:a copy nuovo.mp4).")

    sottotitoli += [c for c in correnti.values() if c]
    # elimina "lampi" visti una sola volta con bassa confidenza (di solito rumore)
    sottotitoli = [s for s in sottotitoli if len(s.testi) > 1 or s.testi[0][1] > max(conf_min, 0.6)]
    sottotitoli.sort(key=lambda s: s.inizio)
    n_alto = sum(s.zona == "alto" for s in sottotitoli)
    LOG(f"Trovati {len(sottotitoli)} sottotitoli ({len(sottotitoli) - n_alto} in basso, {n_alto} in alto). "
        f"OCR eseguito {sum(l.ocr_fatti for l in lettori.values())} volte.")
    d["sottotitoli"] = [asdict(s) for s in sottotitoli]
    d["correnti"] = {}
    d["ocr_completo"] = True
    prog.salva()
    return sottotitoli


# --------------------------------------------------------------------------
# 2) Traduzione con AI locale
# --------------------------------------------------------------------------
def ollama_disponibile(url):
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/tags", timeout=5) as r:
            return json.load(r)
    except Exception:
        return None


def url_locale(url):
    return any(h in url for h in ("localhost", "127.0.0.1", "[::1]"))


def trova_ollama():
    candidati = [shutil.which("ollama"), "/usr/local/bin/ollama", "/opt/homebrew/bin/ollama",
                 "/Applications/Ollama.app/Contents/Resources/ollama",
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama.exe")]
    return next((c for c in candidati if c and os.path.isfile(c)), None)


def avvia_ollama(url):
    """True se Ollama risponde (avviandolo se è installato ma spento)."""
    if ollama_disponibile(url):
        return True
    if not url_locale(url):
        return False
    m = re.search(r":(\d+)", url.split("//")[-1])
    porta = m.group(1) if m else "11434"
    if sys.platform == "darwin" and os.path.isdir("/Applications/Ollama.app") and porta == "11434":
        LOG("Ollama è spento: lo avvio...")
        subprocess.Popen(["open", "-a", "Ollama"])
    else:
        exe = trova_ollama()
        if not exe:
            return False
        LOG("Ollama è spento: lo avvio...")
        extra = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}
                 if os.name == "nt" else {"start_new_session": True})
        env = dict(os.environ, OLLAMA_HOST=f"127.0.0.1:{porta}")
        subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, **extra)
    for _ in range(40):
        controlla_stop()
        time.sleep(1)
        if ollama_disponibile(url):
            return True
    return False


def nome_modello(x):
    """'qwen2.5' -> 'qwen2.5:latest', 'library/Qwen2.5:7B ' -> 'qwen2.5:7b'"""
    x = x.strip().lower()
    for pre in ("registry.ollama.ai/", "library/"):
        if x.startswith(pre):
            x = x[len(pre):]
    return x if ":" in x.rsplit("/", 1)[-1] else x + ":latest"


def modello_presente(info, modello):
    nomi = {nome_modello(m.get("name") or m.get("model", "")) for m in info.get("models", [])}
    return nome_modello(modello) in nomi


def scarica_modello(modello, url, progresso=None):
    """Scarica il modello AI da Ollama mostrando l'avanzamento (solo la prima volta)."""
    LOG(f"Il modello '{modello}' non è ancora sul computer: lo scarico ora (solo la prima volta, "
        f"alcuni GB: può richiedere un po').")
    dati = json.dumps({"model": modello.strip(), "stream": True}).encode()
    req = urllib.request.Request(url.rstrip("/") + "/api/pull", data=dati,
                                 headers={"Content-Type": "application/json"})
    righe = queue.Queue()

    def leggi():
        # in un thread a parte: così "Interrompi" funziona anche se Ollama resta in silenzio
        # a lungo (es. mentre verifica il file scaricato)
        try:
            with urllib.request.urlopen(req, timeout=3600) as r:
                for riga in r:
                    righe.put(("riga", riga))
            righe.put(("fine", None))
        except urllib.error.HTTPError as e:
            righe.put(("errore", f"{e.code}: {e.read().decode(errors='ignore')[:200]}"))
        except Exception as e:
            righe.put(("errore", str(e)))

    threading.Thread(target=leggi, daemon=True).start()
    ultimo = 0
    while True:
        controlla_stop()
        try:
            tipo, riga = righe.get(timeout=0.5)
        except queue.Empty:
            continue
        if tipo == "errore":
            raise RuntimeError(f"download del modello fallito ({riga})")
        if tipo == "fine":
            return
        if not riga.strip():
            continue
        j = json.loads(riga)
        if "error" in j:
            raise RuntimeError(f"download del modello fallito: {j['error']}")
        tot, fatto = j.get("total"), j.get("completed")
        if tot and fatto and progresso and time.time() - ultimo > 0.5:
            progresso(fatto / tot * 0.05, f"Scarico il modello AI: {fatto / tot * 100:.0f}% "
                                          f"di {tot / 1e9:.1f} GB")
            ultimo = time.time()
        if j.get("status") == "success":
            LOG("Modello scaricato.")
            return


def prepara_ollama(url, modello, progresso=None, obbligatorio=True):
    """Controlla (e se serve avvia Ollama e scarica il modello). Con obbligatorio=False
    segnala soltanto il problema senza fermare il lavoro."""
    try:
        if not avvia_ollama(url):
            raise RuntimeError("Ollama non risponde. Se non è installato scaricalo da ollama.com, "
                               "poi aprilo e rilancia: il lavoro fatto finora è salvato.")
        info = ollama_disponibile(url) or {}
        if not modello_presente(info, modello):
            scarica_modello(modello, url, progresso)
            if not modello_presente(ollama_disponibile(url) or {}, modello):
                raise RuntimeError(f"Il modello '{modello}' non risulta installato in Ollama. "
                                   f"Controlla il nome (es. qwen2.5:7b).")
    except Interrotto:
        raise
    except Exception as e:
        if obbligatorio:
            raise
        LOG(f"ATTENZIONE: {e}\n  (continuo a leggere i sottotitoli; la traduzione servirà dopo)")
        return False
    return True


def chiedi_ollama(prompt, modello, url):
    dati = json.dumps({"model": modello, "prompt": prompt, "stream": False, "keep_alive": "30m",
                       "options": {"temperature": 0.2, "num_ctx": 4096}}).encode()
    req = urllib.request.Request(url.rstrip("/") + "/api/generate", data=dati,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            risposta = json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Ollama ha risposto con errore {e.code}: "
                           f"{e.read().decode(errors='ignore')[:200]}") from None
    if "response" not in risposta:
        raise RuntimeError(f"Risposta inattesa da Ollama: {str(risposta)[:200]}")
    out = risposta["response"]
    if "</think>" in out:
        out = out.split("</think>")[-1]
    return out.strip()


def pulisci(t):
    t = t.strip().strip("*").strip().strip('"«»“”').strip()
    for pre in ("Traduzione italiana:", "Traduzione:"):
        if t.lower().startswith(pre.lower()):
            t = t[len(pre):].strip()
    return t


def intestazione(lingua, contesto):
    nome = LINGUE.get(lingua, lingua)
    ctx = "\n".join(contesto[-3:])
    return (f"Sei un traduttore professionista di sottotitoli. Traduci dal {nome} all'italiano. "
            f"Il testo viene da un OCR e può contenere piccoli errori di lettera: correggili in base "
            f"al senso. Traduzioni brevi e naturali, come veri sottotitoli.\n"
            + (f"\nSottotitoli precedenti (solo contesto, non tradurli):\n{ctx}\n" if ctx else ""))


def traduci_blocco_ollama(testi, lingua, modello, url, contesto):
    righe = "\n".join(f"{k}. {t}" for k, t in enumerate(testi, 1))
    prompt = (intestazione(lingua, contesto)
              + f"\nTraduci questi {len(testi)} sottotitoli numerati. Rispondi SOLO con le traduzioni, "
                f"una per riga, con lo stesso numero davanti (es. '1. ...'), senza commenti.\n\n"
              + righe + "\n\nTraduzioni italiane:")
    trovate = {}
    for riga in chiedi_ollama(prompt, modello, url).splitlines():
        m = re.match(r"\s*\**\s*(\d+)\s*[.):\-]\**\s*(.+)", riga)
        if m and pulisci(m.group(2)):
            trovate.setdefault(int(m.group(1)), pulisci(m.group(2)))   # tiene la prima risposta
    if all(k in trovate for k in range(1, len(testi) + 1)):
        return [trovate[k] for k in range(1, len(testi) + 1)]
    return None


def traduci_ollama(testo, lingua, modello, url, contesto):
    prompt = (intestazione(lingua, contesto)
              + f"\nRispondi SOLO con la traduzione italiana, senza virgolette né spiegazioni.\n"
                f"\nSottotitolo da tradurre:\n{testo}\n\nTraduzione italiana:")
    out = pulisci(chiedi_ollama(prompt, modello, url))
    out = " ".join(r.strip() for r in out.splitlines() if r.strip())
    if not out:
        raise RuntimeError("risposta vuota dal modello")
    return out


def traduci_argos(testo, lingua):
    import argostranslate.translate as at
    src = CODICI_ARGOS.get(lingua, lingua)
    lingue = {l.code: l for l in at.get_installed_languages()}
    if src not in lingue or "it" not in lingue or lingue[src].get_translation(lingue["it"]) is None:
        raise RuntimeError(f"Argos: pacchetto lingua {src}->it non installato "
                           f"(vedi LEGGIMI.txt, sezione Argos).")
    out = lingue[src].get_translation(lingue["it"]).translate(testo).strip()
    if not out:
        raise RuntimeError("risposta vuota da Argos")
    return out


def traduci_tutti(sottotitoli, lingua, motore, modello, url, prog, progresso):
    if motore == "ollama":
        prepara_ollama(url, modello, None, obbligatorio=True)
    d = prog.dati
    chiave = f"{lingua}|{motore}|{nome_modello(modello) if motore == 'ollama' else ''}"
    cache = d.setdefault("traduzioni", {}).setdefault(chiave, {})

    ordine = list(dict.fromkeys(s.testo for s in sottotitoli))
    posizione = {t: k for k, t in enumerate(ordine)}
    da_fare = [t for t in ordine if t not in cache]
    LOG(f"Traduco {len(da_fare)} frasi con {motore}"
        f"{' (' + modello + ')' if motore == 'ollama' else ''}"
        + (f" - {len(ordine) - len(da_fare)} già tradotte in precedenza" if len(da_fare) < len(ordine) else ""))
    cron = Cronometro("Traduzione", progresso, 0.5, 0.1)
    passo = BLOCCO_TRADUZIONE if motore == "ollama" else 1
    errori_di_fila = 0
    for k in range(0, len(da_fare), passo):
        controlla_stop()
        blocco = da_fare[k:k + passo]
        pos = posizione[blocco[0]]
        contesto = ordine[max(0, pos - 3):pos]
        try:
            if motore == "ollama":
                ris = traduci_blocco_ollama(blocco, lingua, modello, url, contesto)
                if ris is None:   # risposta non nel formato giusto: una per una
                    ris = [traduci_ollama(t, lingua, modello, url, contesto) for t in blocco]
            else:
                ris = [traduci_argos(t, lingua) for t in blocco]
            errori_di_fila = 0
        except Interrotto:
            raise
        except Exception as e:
            errori_di_fila += 1
            LOG(f"  errore di traduzione: {e}")
            if errori_di_fila >= 3:
                raise RuntimeError(f"La traduzione fallisce ripetutamente ({e}). Controlla Ollama/il "
                                   f"modello e rilancia: il lavoro fatto finora è salvato.") from None
            continue          # non salvato: verrà ritentato
        for orig, trad in zip(blocco, ris):
            cache[orig] = trad
            LOG(f"  {orig}  ->  {trad}")
            notifica("traduzione", orig, trad)
        prog.salva()
        cron.aggiorna((k + len(blocco)) / max(1, len(da_fare)))

    mancanti = [t for t in ordine if t not in cache]
    if mancanti:
        raise RuntimeError(f"{len(mancanti)} frasi non sono state tradotte a causa di errori. "
                           f"Rilancia lo stesso video per ritentare solo quelle.")
    for s in sottotitoli:
        s.traduzione = cache[s.testo]


# --------------------------------------------------------------------------
# 3) Riscrittura sul video
# --------------------------------------------------------------------------
def trova_font():
    candidati = [
        "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf", "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    return next((c for c in candidati if os.path.exists(c)), None)


def carica_font(percorso, dim):
    if percorso:
        return ImageFont.truetype(percorso, dim)
    try:
        return ImageFont.load_default(size=dim)      # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


def a_capo(testo, font, larghezza_max, draw):
    parole, righe, riga = testo.split(), [], ""
    for p in parole:
        prova = (riga + " " + p).strip()
        if draw.textlength(prova, font=font) <= larghezza_max or not riga:
            riga = prova
        else:
            righe.append(riga)
            riga = p
    if riga:
        righe.append(riga)
    return righe or [""]


def prepara_overlay(s, w, h, percorso_font):
    """Immagine RGBA col testo italiano + posizione dove incollarla."""
    altezza_box = s.box[3] - s.box[1]
    rif = min(w, h)                      # funziona anche con video verticali
    dim = int(np.clip(altezza_box * 0.8, rif * 0.035, rif * 0.06))
    font = carica_font(percorso_font, dim)
    tmp = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    righe = a_capo(s.traduzione, font, w * 0.9, tmp)
    bordo = max(2, dim // 12)
    interl = int(dim * 1.25)
    larg = int(max(tmp.textlength(r, font=font) for r in righe)) + bordo * 4
    alt = interl * len(righe) + bordo * 4
    img = Image.new("RGBA", (max(1, larg), alt), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for k, r in enumerate(righe):
        lx = (larg - d.textlength(r, font=font)) / 2
        d.text((lx, bordo * 2 + k * interl), r, font=font, fill=(255, 255, 255, 255),
               stroke_width=bordo, stroke_fill=(0, 0, 0, 255))
    rgba = np.array(img)
    cx = (s.box[0] + s.box[2]) // 2
    x = int(np.clip(cx - larg // 2, 0, max(0, w - larg)))
    if s.zona == "alto":          # in alto: allinea alla parte alta della scritta originale
        y = int(np.clip(s.box[1] - bordo * 2, 0, max(0, h - alt)))
    else:                         # in basso: allinea alla parte bassa
        y = int(np.clip(s.box[3] - alt + bordo * 2, 0, max(0, h - alt)))
    return rgba, x, y


def cancella_originale(frame, box, modo):
    h, w = frame.shape[:2]
    pad = max(6, (box[3] - box[1]) // 6)
    x1, y1 = max(0, box[0] - pad), max(0, box[1] - pad)
    x2, y2 = min(w, box[2] + pad), min(h, box[3] + pad)
    roi = frame[y1:y2, x1:x2]
    if roi.size == 0:
        return
    if modo == "inpaint":
        grigio = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        chiaro = cv2.threshold(grigio, 170, 255, cv2.THRESH_BINARY)[1]
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        giallo = cv2.inRange(hsv, (15, 80, 150), (40, 255, 255))
        maschera = cv2.dilate(cv2.bitwise_or(chiaro, giallo), np.ones((3, 3), np.uint8), iterations=3)
        frame[y1:y2, x1:x2] = cv2.inpaint(roi, maschera, 5, cv2.INPAINT_TELEA)
    else:
        # sfocatura forte ma veloce (rimpicciolisci, sfoca, ingrandisci): le lettere
        # originali spariscono del tutto invece di restare visibili in trasparenza
        rh, rw = roi.shape[:2]
        mini = cv2.resize(roi, (max(1, rw // 8), max(1, rh // 8)), interpolation=cv2.INTER_AREA)
        mini = cv2.GaussianBlur(mini, (0, 0), 3)
        sfocato = cv2.resize(mini, (rw, rh), interpolation=cv2.INTER_LINEAR)
        # "box": fascia scura semitrasparente (si intravede la scena, non il vecchio testo)
        frame[y1:y2, x1:x2] = sfocato if modo == "sfoca" else (sfocato * 0.25).astype(np.uint8)


def incolla(frame, rgba, x, y):
    oh, ow = rgba.shape[:2]
    h, w = frame.shape[:2]
    ow, oh = min(ow, w - x), min(oh, h - y)
    if ow <= 0 or oh <= 0:
        return
    sopra = rgba[:oh, :ow]
    alpha = sopra[:, :, 3:4].astype(np.float32) / 255.0
    bgr = sopra[:, :, 2::-1].astype(np.float32)
    zona = frame[y:y + oh, x:x + ow].astype(np.float32)
    frame[y:y + oh, x:x + ow] = (bgr * alpha + zona * (1 - alpha)).astype(np.uint8)


def riscrivi_video(video, uscita, sottotitoli, info, modo, progresso):
    w, h, fps, tot = info
    font = trova_font()
    subs = sorted(sottotitoli, key=lambda s: s.inizio)
    tmp_uscita = os.path.splitext(uscita)[0] + "_incompleto.mp4"
    cap = cv2.VideoCapture(video)
    log_ffmpeg = None

    ffmpeg = trova_ffmpeg()
    if ffmpeg:
        # i fotogrammi vanno direttamente a ffmpeg (H.264) insieme all'audio originale:
        # nessun file temporaneo enorme e una sola compressione.
        cmd = [ffmpeg, "-y", "-loglevel", "error",
               "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}", "-r", f"{fps:.6f}", "-i", "-",
               "-i", video, "-map", "0:v:0", "-map", "1:a?",
               "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",          # H.264 vuole dimensioni pari
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "192k", "-t", f"{tot / fps:.3f}",
               "-movflags", "+faststart", tmp_uscita]
        log_ffmpeg = tempfile.TemporaryFile()
        extra = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
                 else {"start_new_session": True})   # Ctrl+C non deve arrivare a ffmpeg
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=log_ffmpeg, **extra)
        scrivi = lambda fr: proc.stdin.write(np.ascontiguousarray(fr).data)
    else:
        LOG("ATTENZIONE: ffmpeg non installato -> il video sarà SENZA audio.")
        proc = None
        out = cv2.VideoWriter(tmp_uscita, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
        if not out.isOpened():
            raise RuntimeError("Impossibile creare il video di uscita (installa ffmpeg).")
        scrivi = out.write

    LOG("Creo il video con i sottotitoli italiani...")
    cron = Cronometro("Creazione video", progresso, 0.6, 0.4)
    attivi, overlay, ptr, i = [], {}, 0, 0
    fine_regolare = pipe_rotta = False
    ultima_anteprima = 0.0
    try:
        while True:
            ok, frame = cap.read() if i < tot else (False, None)
            if not ok:
                fine_regolare = True
                break
            t = i / fps
            while ptr < len(subs) and subs[ptr].inizio <= t:
                attivi.append(ptr)
                ptr += 1
            ancora = [k for k in attivi if subs[k].fine > t]
            for k in set(attivi) - set(ancora):
                overlay.pop(k, None)
            attivi = ancora
            for k in attivi:
                cancella_originale(frame, subs[k].box, modo)
            for k in attivi:
                if k not in overlay:
                    overlay[k] = prepara_overlay(subs[k], w, h, font)
                incolla(frame, *overlay[k])
            if EVENTO and attivi and time.time() - ultima_anteprima > 1.5:
                ultima_anteprima = time.time()
                notifica("frame", anteprima(frame)[0])
            try:
                scrivi(frame)
            except OSError:          # BrokenPipe (Linux/Mac) o EINVAL (Windows): ffmpeg è uscito
                pipe_rotta = True
                break
            i += 1
            if i % 25 == 0:
                cron.aggiorna(i / tot)
                controlla_stop()
    except KeyboardInterrupt:
        FERMA.set()
        raise
    finally:
        cap.release()
        if proc:
            try:
                proc.stdin.close()
            except OSError:
                pipe_rotta = True
            proc.wait()
            log_ffmpeg.seek(0)
            err = log_ffmpeg.read().decode(errors="ignore").strip()
            log_ffmpeg.close()
        else:
            out.release()
        if not fine_regolare:
            try:
                os.remove(tmp_uscita)
            except OSError:
                pass

    if proc and (proc.returncode != 0 or pipe_rotta):
        raise RuntimeError(f"ffmpeg ha dato errore: {err[-600:] or 'sconosciuto'}")
    if i == 0:
        raise RuntimeError("Nessun fotogramma letto durante la creazione del video.")
    if i < tot * 0.97 and tot - i > fps * 5:
        LOG(f"ATTENZIONE: il video originale si è interrotto a {fmt_tempo(i / fps)} su "
            f"{fmt_tempo(tot / fps)} (file danneggiato?): il risultato è più corto.")
    if not sostituisci_file(tmp_uscita, uscita):
        LOG(f"ATTENZIONE: non posso sovrascrivere {uscita} (è aperto in un altro programma?). "
            f"Il video finito è comunque qui: {tmp_uscita}")
        return tmp_uscita
    return uscita


# --------------------------------------------------------------------------
# SRT e stima tempi
# --------------------------------------------------------------------------
def salva_srt(sottotitoli, percorso, campo="traduzione"):
    with open(percorso, "w", encoding="utf-8") as f:
        n = 0
        for s in sorted(sottotitoli, key=lambda s: s.inizio):
            testo = (s.traduzione if campo == "traduzione" else s.testo).strip()
            if not testo:
                continue
            n += 1
            if s.zona == "alto":
                testo = "{\\an8}" + testo     # nei lettori video compare in alto
            f.write(f"{n}\n{fmt_tempo(s.inizio, True)} --> {fmt_tempo(s.fine, True)}\n{testo}\n\n")


def stima_tempi(info, analisi, zone, intervallo, motore, solo_srt):
    w, h, fps, tot = info
    durata = tot / fps
    campioni = tot / max(1, round(fps * intervallo))
    frac_area = sum(z.y1 - z.y0 for z in zone) / h
    t_ocr = analisi["t_ocr"] * max(0.3, frac_area)
    n_subs = max(1, durata * analisi["frac_testo"] / 3.0)
    if analisi["luminosi"]:
        n_ocr = n_subs * 3 + campioni * len(zone) * 0.1
    else:
        n_ocr = campioni * len(zone) * 0.6
    t_dec = tot / max(1.0, analisi["vel_dec"])
    fasi = {
        "lettura sottotitoli": t_dec + n_ocr * t_ocr,
        "traduzione": n_subs * (0.8 if motore == "ollama" else 0.3),
        "creazione video": 0 if solo_srt else t_dec * 3,
    }
    LOG(f"Durata video: {fmt_durata(durata)} - circa {int(n_subs)} sottotitoli previsti.")
    LOG("STIMA INDICATIVA dei tempi (verrà aggiornata durante il lavoro):")
    for k, v in fasi.items():
        if v:
            LOG(f"   {k:22s} ~ {fmt_durata(v)}")
    LOG(f"   {'TOTALE':22s} ~ {fmt_durata(sum(fasi.values()))}")


# --------------------------------------------------------------------------
# Programma principale
# --------------------------------------------------------------------------
def elabora(video, lingua="fr", motore="ollama", modello="qwen2.5:7b",
            url="http://localhost:11434", modo="box", intervallo=0.5, posizione="auto",
            zona=0.35, conf_min=0.35, uscita=None, solo_srt=False, progresso=None,
            prova=0, motore_ocr="auto"):
    """Ritorna il percorso del risultato, None se interrotto/nessun sottotitolo.
    Gli errori veri sollevano un'eccezione con un messaggio chiaro.
    prova=N: elabora solo i primi N secondi (per controllare il risultato in fretta)."""
    FERMA.clear()
    t_inizio = time.time()
    modello = (modello or "").strip()
    info = leggi_info(video)
    w, h, fps, tot = info
    LOG(f"Traduttore Sottotitoli {VERSIONE}")
    LOG(f"Video: {os.path.basename(video)}  {w}x{h}  {fps:.2f} fps  {fmt_tempo(tot / fps)}")
    suffisso = ""
    if prova and prova * fps < tot:
        tot = max(1, int(prova * fps))
        info = (w, h, fps, tot)
        suffisso = "_prova"
        LOG(f"MODALITÀ PROVA: elaboro solo i primi {fmt_durata(prova)}.")
    base = os.path.splitext(video)[0]
    uscita = uscita or f"{base}_ITA{suffisso}.mp4"
    srt_ita, srt_orig = f"{base}_ITA{suffisso}.srt", f"{base}_originale{suffisso}.srt"

    prog = Progresso(video, info, [lingua, posizione, zona, conf_min, intervallo], suffisso)
    reader = []

    def get_reader():
        if not reader:
            reader.append(crea_ocr(lingua, motore_ocr))
        return reader[0]

    try:
        if motore == "ollama" and not prog.dati.get("ocr_completo"):
            # controlla SUBITO Ollama e il modello (meglio saperlo ora che dopo un'ora di lettura)
            prepara_ollama(url, modello, progresso, obbligatorio=False)
        notifica("fase", "analisi")
        if "analisi" not in prog.dati:
            prog.dati["analisi"] = analizza_video(video, get_reader(), info, conf_min, progresso)
            prog.salva()
        analisi = prog.dati["analisi"]
        zone = scegli_zone(analisi, posizione, zona, h)
        LOG("Fasce controllate: " + ", ".join(
            f"{z.nome} ({z.y0 * 100 // h}%-{z.y1 * 100 // h}% dell'altezza)" for z in zone))
        if not prog.dati.get("ocr_completo"):
            stima_tempi(info, analisi, zone, intervallo, motore, solo_srt)

        notifica("fase", "lettura")
        sottotitoli = trova_sottotitoli(video, get_reader, info, analisi, zone, intervallo,
                                        conf_min, prog, progresso)
        if not sottotitoli:
            LOG("Nessun sottotitolo trovato. Se ci sono, prova ad abbassare la confidenza (--conf 0.2) "
                "o ad allargare la fascia (--zona 0.5).")
            prog.elimina()
            return None
        notifica("trovati", len(sottotitoli))
        salva_srt(sottotitoli, srt_orig, campo="testo")
        notifica("fase", "traduzione")
        traduci_tutti(sottotitoli, lingua, motore, modello, url, prog, progresso)
        salva_srt(sottotitoli, srt_ita)
        LOG(f"Sottotitoli italiani salvati: {srt_ita}")
        risultato = srt_ita
        if not solo_srt:
            notifica("fase", "video")
            risultato = riscrivi_video(video, uscita, sottotitoli, info, modo, progresso)
            LOG(f"Video tradotto: {risultato}")
        prog.elimina()
        notifica("fase", "fatto")
        LOG(f"FATTO! Tempo impiegato: {fmt_durata(time.time() - t_inizio)}")
        if progresso:
            progresso(1.0, "Finito!")
        return risultato
    except (Interrotto, KeyboardInterrupt):
        LOG("Interrotto. I progressi sono salvati: rilancia lo stesso video per riprendere.")
        if progresso:
            progresso(0, "Interrotto - rilancia lo stesso video per riprendere")
        return None


def carica_impostazioni():
    try:
        with open(FILE_IMPOSTAZIONI, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def salva_impostazioni(d):
    try:
        with open(FILE_IMPOSTAZIONI, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def mostra_nel_finder(percorso):
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", percorso])
        elif os.name == "nt":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(percorso)])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(percorso) or "."])
    except Exception:
        pass


class App:
    """Finestra principale."""
    C = dict(sfondo="#f2f3f8", card="#ffffff", bordo="#e2e5ee", testo="#111827", tenue="#6b7280",
             accento="#4f46e5", accento_scuro="#3730a3", accento_chiaro="#eef2ff", ok="#16a34a",
             errore="#dc2626", testata="#1e1b4b", testata2="#c7d2fe", schermo="#0f172a")
    FASI = [("analisi", "Analisi"), ("lettura", "Lettura"), ("traduzione", "Traduzione"),
            ("video", "Video")]
    MODELLI = ["qwen2.5:7b", "qwen2.5:3b", "qwen2.5:14b", "llama3.1:8b", "gemma2:9b", "mistral-nemo"]
    PREVIEW = (600, 338)

    def __init__(self):
        import tkinter as tk
        from tkinter import filedialog, font as tkfont, messagebox, ttk
        from PIL import Image as PILImage, ImageTk
        self.tk, self.ttk, self.fd, self.mb = tk, ttk, filedialog, messagebox
        self.PILImage, self.ImageTk = PILImage, ImageTk
        self.imp = carica_impostazioni()
        self.root = tk.Tk()
        self.root.title("Traduttore Sottotitoli")
        self.root.geometry("1120x800")
        self.root.minsize(1020, 740)
        self.root.configure(bg=self.C["sfondo"])
        fam = tkfont.nametofont("TkDefaultFont").actual("family")
        self.font = lambda dim, grassetto=False: (fam, dim, "bold" if grassetto else "normal")
        self.eventi = queue.Queue()     # il lavoro gira in un altro thread: la finestra legge da qui
        self.lavoratore = None
        self.risultato = None
        self.foto = None
        self.fase = None
        self.info_video = None
        self.etichette = {f"{v.capitalize()} ({k})": k for k, v in LINGUE.items()}
        self._stile()
        self._costruisci()
        self.root.protocol("WM_DELETE_WINDOW", self.chiudi)
        if self.imp.get("video") and os.path.isfile(self.imp["video"]):
            self.carica_video(self.imp["video"])
        self._leggi_eventi()

    # ---------------------------------------------------------------- aspetto
    def _stile(self):
        C, st = self.C, self.ttk.Style(self.root)
        st.theme_use("clam")
        st.configure(".", background=C["card"], foreground=C["testo"], font=self.font(12))
        st.configure("TCombobox", fieldbackground="#f9fafb", background="#f9fafb", bordercolor=C["bordo"],
                     arrowcolor=C["tenue"], padding=4)
        st.map("TCombobox", fieldbackground=[("readonly", "#f9fafb")], bordercolor=[("focus", C["accento"])])
        st.configure("TCheckbutton", background=C["card"], font=self.font(12))
        st.map("TCheckbutton", background=[("active", C["card"])],
               indicatorcolor=[("selected", C["accento"]), ("!selected", "#ffffff")])
        st.configure("Primario.TButton", background=C["accento"], foreground="#ffffff", borderwidth=0,
                     focusthickness=0, font=self.font(15, True), padding=(18, 11))
        st.map("Primario.TButton", background=[("disabled", "#c7c9d9"), ("active", C["accento_scuro"])],
               foreground=[("disabled", "#f3f4f6")])
        st.configure("Secondario.TButton", background=C["accento_chiaro"], foreground=C["accento_scuro"],
                     borderwidth=0, focusthickness=0, font=self.font(12, True), padding=(12, 8))
        st.map("Secondario.TButton", background=[("disabled", "#f3f4f6"), ("active", "#e0e7ff")],
               foreground=[("disabled", "#b0b4c3")])
        st.configure("Barra.Horizontal.TProgressbar", troughcolor="#e5e7eb", background=C["accento"],
                     bordercolor="#e5e7eb", lightcolor=C["accento"], darkcolor=C["accento"], thickness=12)
        st.configure("Ok.Horizontal.TProgressbar", troughcolor="#e5e7eb", background=C["ok"],
                     bordercolor="#e5e7eb", lightcolor=C["ok"], darkcolor=C["ok"], thickness=12)

    def _card(self, genitore, numero, titolo):
        tk, C = self.tk, self.C
        esterno = tk.Frame(genitore, bg=C["card"], highlightbackground=C["bordo"], highlightthickness=1)
        testa = tk.Frame(esterno, bg=C["card"])
        testa.pack(fill="x", padx=18, pady=(12, 4))
        if numero:
            tk.Label(testa, text=str(numero), bg=C["accento"], fg="#ffffff", font=self.font(11, True),
                     width=2).pack(side="left", padx=(0, 10))
        tk.Label(testa, text=titolo, bg=C["card"], fg=C["testo"], font=self.font(14, True)).pack(side="left")
        dentro = tk.Frame(esterno, bg=C["card"])
        dentro.pack(fill="both", expand=True, padx=18, pady=(2, 14))
        return esterno, dentro

    def _costruisci(self):
        tk, ttk, C = self.tk, self.ttk, self.C
        # testata
        testata = tk.Frame(self.root, bg=C["testata"])
        testata.pack(fill="x")
        tk.Label(testata, text="🎬  Traduttore Sottotitoli", bg=C["testata"], fg="#ffffff",
                 font=self.font(19, True)).pack(side="left", padx=(22, 12), pady=(12, 12))
        tk.Label(testata, text="sottotitoli impressi nel video  →  italiano  ·  AI sul tuo computer",
                 bg=C["testata"], fg=C["testata2"], font=self.font(12)).pack(side="left", pady=(17, 12))
        tk.Label(testata, text=f"v{VERSIONE}", bg=C["testata"], fg=C["testata2"],
                 font=self.font(11)).pack(side="right", padx=22)

        corpo = tk.Frame(self.root, bg=C["sfondo"])
        corpo.pack(fill="both", expand=True, padx=18, pady=(14, 8))
        corpo.columnconfigure(0, minsize=400)
        corpo.columnconfigure(1, weight=1)
        corpo.rowconfigure(0, weight=1)
        sx = tk.Frame(corpo, bg=C["sfondo"])
        sx.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        dx = tk.Frame(corpo, bg=C["sfondo"])
        dx.grid(row=0, column=1, sticky="nsew")

        # 1 - video
        c1, d1 = self._card(sx, 1, "Video")
        c1.pack(fill="x")
        ttk.Button(d1, text="Scegli video…", style="Secondario.TButton", command=self.scegli).pack(anchor="w")
        self.l_nome = tk.Label(d1, text="Nessun video scelto", bg=C["card"], fg=C["testo"],
                               font=self.font(12, True), anchor="w", justify="left", wraplength=360)
        self.l_nome.pack(fill="x", pady=(8, 0))
        self.l_info = tk.Label(d1, text="mp4, mkv, mov, avi…", bg=C["card"], fg=C["tenue"],
                               font=self.font(11), anchor="w")
        self.l_info.pack(fill="x")

        # 2 - impostazioni
        c2, d2 = self._card(sx, 2, "Impostazioni")
        c2.pack(fill="x", pady=12)
        imp = self.imp
        inv = {v: k for k, v in self.etichette.items()}
        self.v_lingua = tk.StringVar(value=inv.get(imp.get("lingua", "fr"), "Francese (fr)"))
        self.v_pos = tk.StringVar(value=imp.get("posizione", "auto"))
        self.v_modo = tk.StringVar(value=imp.get("modo", "box"))
        self.v_motore = tk.StringVar(value=imp.get("motore", "ollama"))
        self.v_modello = tk.StringVar(value=imp.get("modello", "qwen2.5:7b"))
        self.v_prova = tk.BooleanVar(value=False)
        self.v_srt = tk.BooleanVar(value=False)
        righe = [
            ("Lingua dei sottotitoli", ttk.Combobox(d2, textvariable=self.v_lingua, state="readonly",
                                                     values=list(self.etichette), width=20)),
            ("Posizione", ttk.Combobox(d2, textvariable=self.v_pos, state="readonly", width=20,
                                       values=["auto", "basso", "alto"])),
            ("Copri l'originale con", ttk.Combobox(d2, textvariable=self.v_modo, state="readonly", width=20,
                                                   values=["box", "inpaint", "sfoca"])),
            ("Modello AI (Ollama)", ttk.Combobox(d2, textvariable=self.v_modello, width=20,
                                                 values=self.MODELLI)),
        ]
        for r, (testo, w) in enumerate(righe):
            tk.Label(d2, text=testo, bg=C["card"], fg=C["tenue"], font=self.font(12)).grid(
                row=r, column=0, sticky="w", pady=3, padx=(0, 12))
            w.grid(row=r, column=1, sticky="ew", pady=3)
        d2.columnconfigure(1, weight=1)
        tk.Label(d2, text="auto = sopra e sotto, anche se cambia scena  ·  8 GB di RAM → qwen2.5:3b",
                 bg=C["card"], fg=C["tenue"], font=self.font(10), justify="left").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(2, 6))
        ttk.Checkbutton(d2, text="Prova sui primi 2 minuti", variable=self.v_prova).grid(
            row=5, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(d2, text="Solo file .srt (non rifare il video)", variable=self.v_srt).grid(
            row=6, column=0, columnspan=2, sticky="w")

        # 3 - avvio
        c3, d3 = self._card(sx, 3, "Avvio")
        c3.pack(fill="x")
        self.b_avvia = ttk.Button(d3, text="▶   Traduci in italiano", style="Primario.TButton", command=self.avvia)
        self.b_avvia.pack(fill="x")
        riga = tk.Frame(d3, bg=C["card"])
        riga.pack(fill="x", pady=(8, 0))
        self.b_ferma = ttk.Button(riga, text="■  Interrompi", style="Secondario.TButton",
                                  command=self.ferma, state="disabled")
        self.b_ferma.pack(side="left", expand=True, fill="x", padx=(0, 5))
        self.b_mostra = ttk.Button(riga, text="📂  Mostra risultato", style="Secondario.TButton",
                                   command=self.mostra, state="disabled")
        self.b_mostra.pack(side="left", expand=True, fill="x", padx=(5, 0))

        # anteprima + avanzamento
        c4, d4 = self._card(dx, None, "Anteprima")
        c4.pack(fill="both", expand=True)
        pw, ph = self.PREVIEW
        self.schermo = tk.Canvas(d4, width=pw, height=ph, bg=C["schermo"], highlightthickness=0)
        self.schermo.pack(fill="x")
        self._schermo_vuoto("Scegli un video per vedere l'anteprima")

        passi = tk.Frame(d4, bg=C["card"])
        passi.pack(fill="x", pady=(14, 6))
        self.l_passi = {}
        for k, (chiave, nome) in enumerate(self.FASI):
            f = tk.Frame(passi, bg=C["card"])
            f.pack(side="left", expand=True, fill="x")
            pallino = tk.Label(f, text=str(k + 1), width=2, bg="#e5e7eb", fg=C["tenue"], font=self.font(11, True))
            pallino.pack(side="left")
            nome_l = tk.Label(f, text=nome, bg=C["card"], fg=C["tenue"], font=self.font(12))
            nome_l.pack(side="left", padx=6)
            self.l_passi[chiave] = (pallino, nome_l)
        self.barra = ttk.Progressbar(d4, maximum=1.0, style="Barra.Horizontal.TProgressbar")
        self.barra.pack(fill="x", pady=(6, 10))
        self.l_stato = tk.Label(d4, text="Pronto", bg=C["card"], fg=C["testo"], font=self.font(14, True),
                                anchor="w")
        self.l_stato.pack(fill="x")
        self.l_dettaglio = tk.Label(d4, text="", bg=C["card"], fg=C["tenue"], font=self.font(11),
                                    anchor="w", justify="left", wraplength=600)
        self.l_dettaglio.pack(fill="x", pady=(2, 0))

        # dettagli tecnici (nascosti)
        basso = tk.Frame(self.root, bg=C["sfondo"])
        basso.pack(fill="x", padx=18, pady=(0, 10))
        self.b_log = tk.Label(basso, text="▸ Dettagli tecnici", bg=C["sfondo"], fg=C["tenue"],
                              font=self.font(11), cursor="hand2")
        self.b_log.pack(anchor="w")
        self.b_log.bind("<Button-1>", lambda e: self._mostra_log())
        self.log = tk.Text(basso, height=9, bg="#ffffff", fg=C["testo"], relief="flat",
                           highlightbackground=C["bordo"], highlightthickness=1, font=("Menlo", 10)
                           if sys.platform == "darwin" else ("Consolas", 10) if os.name == "nt" else ("Monospace", 9))
        self.log_visibile = False

    # ---------------------------------------------------------------- aiuti
    def _mostra_log(self, forza=None):
        self.log_visibile = (not self.log_visibile) if forza is None else forza
        if self.log_visibile:
            self.log.pack(fill="x", pady=(6, 0))
            self.b_log.configure(text="▾ Dettagli tecnici")
        else:
            self.log.pack_forget()
            self.b_log.configure(text="▸ Dettagli tecnici")

    def _schermo_vuoto(self, testo):
        pw, ph = self.PREVIEW
        self.schermo.delete("all")
        self.schermo.create_text(pw // 2, ph // 2, text=testo, fill="#94a3b8", font=self.font(13))

    def _mostra_immagine(self, rgb):
        pw, ph = self.PREVIEW
        w = self.schermo.winfo_width() if self.schermo.winfo_width() > 10 else pw
        h, wi = rgb.shape[:2]
        scala = min(w / wi, ph / h)
        img = self.PILImage.fromarray(rgb).resize((max(1, int(wi * scala)), max(1, int(h * scala))))
        self.foto = self.ImageTk.PhotoImage(img)
        self.schermo.delete("all")
        self.schermo.create_image(w // 2, ph // 2, image=self.foto)

    def _imposta_fase(self, fase, errore=False):
        C = self.C
        chiavi = [k for k, _ in self.FASI]
        attuale = len(chiavi) if fase == "fatto" else (chiavi.index(fase) if fase in chiavi else -1)
        self.fase = fase
        for k, chiave in enumerate(chiavi):
            pallino, nome = self.l_passi[chiave]
            if k < attuale:
                pallino.configure(text="✓", bg=C["ok"], fg="#ffffff")
                nome.configure(fg=C["ok"], font=self.font(12))
            elif k == attuale:
                pallino.configure(text=str(k + 1), bg=C["errore"] if errore else C["accento"], fg="#ffffff")
                nome.configure(fg=C["errore"] if errore else C["accento"], font=self.font(12, True))
            else:
                pallino.configure(text=str(k + 1), bg="#e5e7eb", fg=C["tenue"])
                nome.configure(fg=C["tenue"], font=self.font(12))

    # ---------------------------------------------------------------- azioni
    def scegli(self):
        cartella = os.path.dirname(self.imp.get("video", "")) or os.path.expanduser("~")
        scelto = self.fd.askopenfilename(initialdir=cartella, filetypes=[
            ("Video", "*.mp4 *.mkv *.avi *.mov *.webm *.m4v *.ts"), ("Tutti i file", "*.*")])
        if scelto:
            self.carica_video(scelto)

    def carica_video(self, percorso):
        try:
            w, h, fps, tot = leggi_info(percorso)
        except Exception as e:
            self.mb.showerror("Video non leggibile", str(e))
            return
        self.info_video = percorso
        self.imp["video"] = percorso
        self.l_nome.configure(text=os.path.basename(percorso))
        self.l_info.configure(text=f"{w}×{h}  ·  {fps:.2f} fps  ·  durata {fmt_tempo(tot / fps)}")
        cap = cv2.VideoCapture(percorso)
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(tot * 0.1))
        ok, fr = cap.read()
        cap.release()
        if ok:
            self._mostra_immagine(anteprima(fr)[0])
        self.l_stato.configure(text="Pronto", fg=self.C["testo"])
        self.l_dettaglio.configure(text="")
        self.barra.configure(value=0, style="Barra.Horizontal.TProgressbar")
        self._imposta_fase(None)
        self.b_mostra.configure(state="disabled")

    def avvia(self):
        global LOG, EVENTO
        if not self.info_video or not os.path.isfile(self.info_video):
            self.mb.showinfo("Manca il video", "Scegli prima un video.")
            return
        lingua = self.etichette.get(self.v_lingua.get(), "fr")
        modello = self.v_modello.get().strip() or "qwen2.5:7b"
        self.imp.update({"lingua": lingua, "motore": self.v_motore.get(), "modello": modello,
                         "modo": self.v_modo.get(), "posizione": self.v_pos.get()})
        salva_impostazioni(self.imp)
        for b, st in ((self.b_avvia, "disabled"), (self.b_ferma, "normal"), (self.b_mostra, "disabled")):
            b.configure(state=st)
        self.log.delete("1.0", "end")
        self.risultato = None
        self.barra.configure(value=0, style="Barra.Horizontal.TProgressbar")
        self.l_stato.configure(text="Avvio…", fg=self.C["testo"])
        self.l_dettaglio.configure(text="")
        self.trovati, self.ultima = 0, ""
        LOG = lambda m: self.eventi.put(("log", m))
        EVENTO = lambda *a: self.eventi.put(a)
        argomenti = dict(video=self.info_video, lingua=lingua, motore=self.v_motore.get(), modello=modello,
                         modo=self.v_modo.get(), posizione=self.v_pos.get(), solo_srt=self.v_srt.get(),
                         prova=120 if self.v_prova.get() else 0)

        def lavoro():
            try:
                r = elabora(progresso=lambda p, m=None: self.eventi.put(("prog", p, m)), **argomenti)
                self.eventi.put(("fine", r))
            except Exception as e:
                self.eventi.put(("log", f"\nERRORE: {e}\n\nDettagli tecnici:\n{traceback.format_exc()}"))
                self.eventi.put(("errore", str(e)))

        self.lavoratore = threading.Thread(target=lavoro, daemon=True)
        self.lavoratore.start()

    def ferma(self):
        FERMA.set()
        self.l_stato.configure(text="Interruzione in corso (salvo i progressi)…")

    def mostra(self):
        if self.risultato:
            mostra_nel_finder(self.risultato)

    def chiudi(self):
        if self.lavoratore and self.lavoratore.is_alive():
            if not self.mb.askyesno("Chiudere?", "Il lavoro è in corso. Interrompo e salvo i progressi?"):
                return
            FERMA.set()
            self.l_stato.configure(text="Salvo i progressi e chiudo…")

            def aspetta():
                if self.lavoratore.is_alive():
                    self.root.after(200, aspetta)
                else:
                    self.root.destroy()
            aspetta()
        else:
            self.root.destroy()

    def _dettaglio(self):
        parti = []
        if self.trovati:
            parti.append(f"Sottotitoli trovati: {self.trovati}")
        if self.ultima:
            parti.append(self.ultima)
        self.l_dettaglio.configure(text="     ".join(parti[:1]) + ("\n" + parti[1] if len(parti) > 1 else ""))

    def _leggi_eventi(self):
        C = self.C
        try:
            for _ in range(200):
                tipo, *a = self.eventi.get_nowait()
                if tipo == "log":
                    self.log.insert("end", str(a[0]) + "\n")
                    self.log.see("end")
                elif tipo == "prog":
                    self.barra.configure(value=a[0])
                    if a[1]:
                        self.l_stato.configure(text=a[1])
                elif tipo == "fase":
                    self._imposta_fase(a[0])
                elif tipo == "frame":
                    self._mostra_immagine(a[0])
                elif tipo == "trovati":
                    self.trovati = a[0]
                    self._dettaglio()
                elif tipo == "traduzione":
                    o, t = (x if len(x) < 60 else x[:57] + "…" for x in a)
                    self.ultima = f"«{o}»  →  «{t}»"
                    self._dettaglio()
                elif tipo == "fine":
                    self.risultato = a[0]
                    self.b_avvia.configure(state="normal")
                    self.b_ferma.configure(state="disabled")
                    if a[0]:
                        self.b_mostra.configure(state="normal")
                        self.barra.configure(value=1.0, style="Ok.Horizontal.TProgressbar")
                        self.l_stato.configure(text="✓  Fatto! Il video tradotto è pronto.", fg=C["ok"])
                    elif FERMA.is_set():
                        self.l_stato.configure(text="Interrotto: rilancia lo stesso video per riprendere.",
                                               fg=C["testo"])
                    else:
                        self.l_stato.configure(text="Nessun sottotitolo trovato in questo video.", fg=C["testo"])
                elif tipo == "errore":
                    self._imposta_fase(self.fase, errore=True)
                    self.b_avvia.configure(state="normal")
                    self.b_ferma.configure(state="disabled")
                    self.l_stato.configure(text="Si è verificato un problema", fg=C["errore"])
                    self.l_dettaglio.configure(text=a[0])
                    self._mostra_log(True)
        except queue.Empty:
            pass
        self.root.after(80, self._leggi_eventi)

    def mainloop(self):
        self.root.mainloop()


def gui():
    App().mainloop()


def main():
    # app compilata (finestra): stdout/stderr possono non esistere
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    for flusso in (sys.stdout, sys.stderr):      # console Windows: niente crash su caratteri speciali
        try:
            flusso.reconfigure(errors="replace")
        except Exception:
            pass
    # macOS vecchi passano "-psn_..." quando l'app si apre dal Finder
    argv = [a for a in sys.argv[1:] if not a.startswith("-psn_")]
    if not argv:
        return gui()
    p = argparse.ArgumentParser(description="Traduce in italiano i sottotitoli impressi in un video.")
    p.add_argument("video")
    p.add_argument("--lingua", default="fr", choices=list(LINGUE), help="lingua dei sottotitoli")
    p.add_argument("--posizione", default="auto", choices=["auto", "basso", "alto"],
                   help="auto = controlla sia sopra che sotto (default)")
    p.add_argument("--motore", default="ollama", choices=["ollama", "argos"])
    p.add_argument("--modello", default="qwen2.5:7b", help="modello Ollama")
    p.add_argument("--url", default="http://localhost:11434", help="indirizzo Ollama")
    p.add_argument("--ocr", default="auto", choices=["auto", "vision", "easyocr"],
                   help="motore OCR (auto: Apple Vision su Mac, EasyOCR altrove)")
    p.add_argument("--modo", default="box", choices=["box", "inpaint", "sfoca"],
                   help="come coprire il sottotitolo originale")
    p.add_argument("--intervallo", type=float, default=0.5, help="secondi tra un controllo e l'altro")
    p.add_argument("--zona", type=float, default=0.35, help="altezza minima della fascia controllata (0-1)")
    p.add_argument("--conf", type=float, default=0.35, help="confidenza minima OCR (0-1)")
    p.add_argument("--prova", type=float, default=0, metavar="SECONDI",
                   help="elabora solo i primi SECONDI del video (prova veloce)")
    p.add_argument("--uscita", help="file video di uscita")
    p.add_argument("--solo-srt", action="store_true", help="crea solo il file .srt italiano")
    p.add_argument("--versione", action="version", version=VERSIONE)
    a = p.parse_args(argv)
    if a.prova < 0 or not 0 < a.intervallo <= 5 or not 0 < a.zona <= 0.5 or not 0 < a.conf < 1:
        p.error("valori non validi: --prova >= 0, 0 < --intervallo <= 5, 0 < --zona <= 0.5, 0 < --conf < 1")
    if not os.path.isfile(a.video):
        p.error(f"file non trovato: {a.video}")
    try:
        ris = elabora(a.video, a.lingua, a.motore, a.modello, a.url, a.modo, a.intervallo, a.posizione,
                      a.zona, a.conf, a.uscita, a.solo_srt, prova=a.prova, motore_ocr=a.ocr)
    except Exception as e:
        print(f"\nERRORE: {e}", file=sys.stderr)
        sys.exit(2)
    sys.exit(0 if ris else 1)


if __name__ == "__main__":
    main()
