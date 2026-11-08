#!/usr/bin/env python3
"""
AEROSCAN AI — Dashboard Flask d'inspection drone (v7 = ta v6 + corrections)
Raspberry Pi 5 + Camera Module 3 (IMX708) + Roboflow + servo spray

PIPELINE
  1) FILTRE COULEUR HSV (rouge, orange, ocre, marron)
     -> toutes les zones de cette couleur sont TOUJOURS entourées d'un contour exact
        (jaune = candidat couleur).
  2) VALIDATION ROBOFLOW : appelée dès qu'il y a au moins une zone couleur.
     -> zone confirmée (contour ROUGE) si l'IA >= seuil la recouvre,
        sinon elle reste "non confirmée" (contour jaune) = pas de corrosion.
  -> alerte = zone confirmée sur CONFIRM_FRAMES frames consécutives -> servo spray.

Pages :
    /              dashboard
    /overlay_feed  vidéo AVEC contours dessinés côté serveur (pour vérifier)
    /mask_feed     ce que voit le filtre couleur (pour régler HSV_RANGES)

Lancement :
    source ~/myenv/bin/activate
    export ROBOFLOW_API_KEY="ta_cle"
    python3 app.py
"""

import atexit
import base64
import io
import os
import sys
import threading
import time
from collections import deque

import cv2
import numpy as np
import requests
from flask import Flask, Response, jsonify, render_template
from libcamera import controls
from picamera2 import Picamera2
from picamera2.encoders import MJPEGEncoder
from picamera2.outputs import FileOutput

from spray import SprayController

# ─── CONFIGURATION ───────────────────────────────────────
API_KEY            = os.environ.get("ROBOFLOW_API_KEY", "")

MODEL_URL          = "https://detect.roboflow.com/corrosion-rsbnq/1"
API_CONFIDENCE_MIN = 30      # filtre côté serveur Roboflow (%)
API_OVERLAP        = 30
CONFIDENCE_SEUIL   = 65.0    # seuil de validation IA (%) -> baisse si l'IA rate de la vraie rouille
CONFIRM_FRAMES     = 2       # frames consécutives confirmées avant alerte
API_MIN_INTERVAL   = 0.4     # secondes minimum entre deux appels IA

# Filtre HSV (OpenCV : H 0-179, S 0-255, V 0-255)
HSV_RANGES = [
    ((0,   80, 30), (30,  255, 255)),   # rouge / orange / ocre / marron foncé
    ((165, 80, 30), (179, 255, 255)),   # rouge (côté "boucle" de la teinte)
    # Marron CLAIR (peu saturé) : DÉSACTIVÉ car il allume aussi l'alu sous lampe chaude,
    # le carton, le bois, la peau. Décommente seulement si ta rouille claire n'est pas vue
    # (et vérifie que /mask_feed reste NOIR sur de l'alu propre) :
    # ((8,   40, 90), (28,   80, 240)),
]
HSV_MIN_AREA_PCT  = 0.3      # surface minimale d'une zone (% de l'image)
OPEN_KERNEL       = 3        # enlève le bruit
CLOSE_KERNEL      = 7        # relie les taches de rouille proches en une seule zone
BLUR_KERNEL       = 5        # flou avant HSV (0 = désactivé)
MAX_POLYGONS      = 15

CONTOUR_EPSILON   = 0.001    # 0 = tous les points (exact), 0.003 = plus simplifié
SERVER_DRAW       = True     # contours exacts dessinés DANS la vidéo (côté Pi) : plus de carrés
DRAW_AI_BOXES     = False    # True = affiche aussi les boîtes IA (carrés bleus) pour déboguer
FILL_ALPHA        = 0.25     # remplissage rouge transparent des zones confirmées (0 = aucun)

# Validation IA par recouvrement
MIN_OVERLAP       = 0.30     # part de la zone couleur couverte par une région IA
MIN_OVERLAP_AI    = 0.50     # OU part de la région IA située dans la zone couleur

FRAME_WIDTH        = 640
FRAME_HEIGHT       = 480
FRAME_RATE         = 30
LOOP_DELAY         = 0.05

HOST, PORT         = "0.0.0.0", 5000
# ─────────────────────────────────────────────────────────


class StreamingOutput(io.BufferedIOBase):
    def __init__(self):
        self.frame = None
        self.condition = threading.Condition()

    def write(self, buf):
        with self.condition:
            self.frame = bytes(buf)
            self.condition.notify_all()
        return len(buf)


app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True      # index.html modifié -> pris en compte sans redémarrer
APP_STARTED = str(int(time.time()))              # change à chaque redémarrage du serveur
output = StreamingOutput()
picam2 = None
session = requests.Session()
last_mask_jpg = None
last_overlay_jpg = None
last_zones = []
last_res = None

state = {
    "status": "starting",   # starting | healthy | probable | corrosion | paused | error
    "confidence": 0.0,
    "count": 0,             # nb de zones confirmées
    "polygons": [],         # [{"pts": [[x,y],...], "kind": "confirmed|candidate", "conf": float}]
    "zones": [],            # polygones réels (usage interne, non envoyé au dashboard)
    "candidate": False,
    "hsv_pct": 0.0,
    "stage": "color",       # color | color+ai
    "last_update": None,
    "latency_ms": 0,
    "fps": 0.0,
    "frames": 0,
    "alerts": 0,
    "paused": False,
    "error": None,
}
state_lock = threading.Lock()
history = deque(maxlen=CONFIRM_FRAMES)
stamps = deque(maxlen=10)
spray = SprayController()


def app_version():
    """Identifiant de version : redémarrage du serveur OU modification de index.html."""
    try:
        mt = int(os.path.getmtime(os.path.join(app.root_path, "templates", "index.html")))
    except OSError:
        mt = 0
    return f"{APP_STARTED}-{mt}"


def public_state():
    d = dict(state)
    d.pop("zones", None)      # JSON plus léger : le dashboard n'en a pas besoin
    d["config"] = {
        "threshold": CONFIDENCE_SEUIL,
        "confirm": CONFIRM_FRAMES,
        "width": FRAME_WIDTH,
        "height": FRAME_HEIGHT,
    }
    d["spray"] = spray.snapshot()
    d["version"] = app_version()      # le navigateur recharge la page quand ça change
    return d


# ─── ÉTAPE 1 : FILTRE COULEUR ────────────────────────────
_K_OPEN = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (OPEN_KERNEL, OPEN_KERNEL))
_K_CLOSE = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (CLOSE_KERNEL, CLOSE_KERNEL))


def hsv_mask(bgr):
    src = cv2.GaussianBlur(bgr, (BLUR_KERNEL, BLUR_KERNEL), 0) if BLUR_KERNEL >= 3 else bgr
    hsv = cv2.cvtColor(src, cv2.COLOR_BGR2HSV)
    mask = np.zeros(hsv.shape[:2], np.uint8)
    for lo, hi in HSV_RANGES:
        mask |= cv2.inRange(hsv, np.array(lo, np.uint8), np.array(hi, np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, _K_OPEN)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, _K_CLOSE)
    return mask


def color_contours(mask):
    """Contours complets des zones couleur, du plus grand au plus petit."""
    h, w = mask.shape
    min_area = w * h * HSV_MIN_AREA_PCT / 100.0
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    cnts = [c for c in cnts if cv2.contourArea(c) >= min_area]
    cnts.sort(key=cv2.contourArea, reverse=True)
    return cnts[:MAX_POLYGONS]


def contour_to_poly(c, W, H):
    if CONTOUR_EPSILON > 0:
        c = cv2.approxPolyDP(c, CONTOUR_EPSILON * cv2.arcLength(c, True), True)
    if len(c) < 3:
        return None
    return [[round(float(p[0][0]) / W, 4), round(float(p[0][1]) / H, 4)] for p in c]


# ─── ÉTAPE 2 : VALIDATION IA ─────────────────────────────
def analyser(data: bytes) -> dict:
    try:
        resp = session.post(
            MODEL_URL,
            params={"api_key": API_KEY, "confidence": API_CONFIDENCE_MIN, "overlap": API_OVERLAP},
            data=base64.b64encode(data).decode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
        resp.raise_for_status()
        j = resp.json()
        img = j.get("image", {})
        W = float(img.get("width", FRAME_WIDTH)) or FRAME_WIDTH
        H = float(img.get("height", FRAME_HEIGHT)) or FRAME_HEIGHT

        best, boxes = 0.0, []
        for p in j.get("predictions", []):
            if "corrosion" not in str(p.get("class", "")).lower():
                continue
            conf = p.get("confidence", 0) * 100
            best = max(best, conf)
            b = {
                "x": p["x"] / W, "y": p["y"] / H,
                "w": p["width"] / W, "h": p["height"] / H,
                "conf": round(conf, 1),
            }
            pts = p.get("points")            # modèle de segmentation seulement
            if pts:
                b["poly"] = [[pt["x"] / W, pt["y"] / H] for pt in pts]
            boxes.append(b)
        return {"confidence": round(best, 2), "boxes": boxes}

    except requests.exceptions.Timeout:
        return {"erreur": "API timeout (>10 s)"}
    except requests.exceptions.ConnectionError:
        return {"erreur": "Cannot reach Roboflow (no internet?)"}
    except requests.exceptions.HTTPError as e:
        return {"erreur": f"API error: {e.response.status_code} (check API key / model URL)"}
    except Exception as e:
        return {"erreur": str(e)}


def ai_region(b, W, H):
    m = np.zeros((H, W), np.uint8)
    if b.get("poly"):
        pts = np.array([[x * W, y * H] for x, y in b["poly"]], np.int32)
        cv2.fillPoly(m, [pts], 255)
    else:
        x1, x2 = int((b["x"] - b["w"] / 2) * W), int((b["x"] + b["w"] / 2) * W)
        y1, y2 = int((b["y"] - b["h"] / 2) * H), int((b["y"] + b["h"] / 2) * H)
        cv2.rectangle(m, (x1, y1), (x2, y2), 255, -1)
    return m


def build_zones(cnts, res, W, H):
    """
    Une entrée par zone couleur (contour exact). 'res' = None -> tout est candidat.
    kind = confirmed si une région IA >= CONFIDENCE_SEUIL recouvre la zone, sinon candidate.
    conf = meilleure confiance IA qui recouvre la zone (info, même sous le seuil).
    """
    regions = []
    if res:
        for b in res["boxes"]:
            reg = ai_region(b, W, H)
            regions.append((b["conf"], reg, max(cv2.countNonZero(reg), 1)))

    zones = []
    for c in cnts:
        pts = contour_to_poly(c, W, H)
        if not pts:
            continue
        best = 0.0
        if regions:
            comp = np.zeros((H, W), np.uint8)
            cv2.drawContours(comp, [c], -1, 255, -1)
            area = max(cv2.countNonZero(comp), 1)
            for conf, reg, reg_area in regions:
                inter = cv2.countNonZero(cv2.bitwise_and(comp, reg))
                if inter / area >= MIN_OVERLAP or inter / reg_area >= MIN_OVERLAP_AI:
                    best = max(best, conf)
        zones.append({"pts": pts,
                      "kind": "confirmed" if best >= CONFIDENCE_SEUIL else "candidate",
                      "conf": round(best, 1),
                      "_cnt": c})
    return zones


def strip(zones):
    return [{k: v for k, v in z.items() if k != "_cnt"} for z in zones]


def shown(zones):
    """Polygones envoyés au dashboard : vides si le serveur dessine déjà les contours."""
    return [] if SERVER_DRAW else strip(zones)


def draw_overlay(bgr, zones, res=None):
    """Dessine les contours EXACTS des zones (rouge = corrosion confirmée, jaune = non confirmée)."""
    img = bgr.copy()
    H, W = img.shape[:2]
    if DRAW_AI_BOXES and res:
        for b in res["boxes"]:
            x1, x2 = int((b["x"] - b["w"] / 2) * W), int((b["x"] + b["w"] / 2) * W)
            y1, y2 = int((b["y"] - b["h"] / 2) * H), int((b["y"] + b["h"] / 2) * H)
            cv2.rectangle(img, (x1, y1), (x2, y2), (255, 128, 0), 1)
    if FILL_ALPHA > 0:
        fill = img.copy()
        for z in zones:
            if z["kind"] == "confirmed":
                cv2.drawContours(fill, [z["_cnt"]], -1, (0, 0, 255), -1)
        img = cv2.addWeighted(fill, FILL_ALPHA, img, 1 - FILL_ALPHA, 0)
    for z in zones:
        ok = z["kind"] == "confirmed"
        color = (0, 0, 255) if ok else (0, 200, 255)
        cv2.drawContours(img, [z["_cnt"]], -1, color, 2 if ok else 1, cv2.LINE_AA)
        x, y, _, _ = cv2.boundingRect(z["_cnt"])
        if ok:
            label = f"CORROSION {z['conf']:.0f}%"
        elif z["conf"]:
            label = f"non confirme {z['conf']:.0f}%"
        else:
            label = "candidat"
        cv2.putText(img, label, (x, max(12, y - 4)), cv2.FONT_HERSHEY_SIMPLEX,
                    0.45, color, 1, cv2.LINE_AA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 75])
    return buf.tobytes() if ok else None


# ─── BOUCLE DE DÉTECTION ─────────────────────────────────
def detection_loop():
    global last_mask_jpg, last_overlay_jpg, last_zones, last_res
    last_api = 0.0
    last_print_t, last_print_status = 0.0, None

    while True:
        with state_lock:
            paused = state["paused"]
        if paused:
            time.sleep(0.3)
            continue

        with output.condition:
            output.condition.wait(timeout=2)
            frame = output.frame
        if frame is None:
            continue

        t0 = time.time()
        bgr = cv2.imdecode(np.frombuffer(frame, np.uint8), cv2.IMREAD_COLOR)
        if bgr is None:
            continue

        # ── Étape 1 : filtre couleur ──
        mask = hsv_mask(bgr)
        H, W = mask.shape
        cov = 100.0 * cv2.countNonZero(mask) / (W * H)
        cnts = color_contours(mask)
        candidate = bool(cnts)

        ok, buf = cv2.imencode(".jpg", cv2.bitwise_and(bgr, bgr, mask=mask),
                               [cv2.IMWRITE_JPEG_QUALITY, 70])
        if ok:
            last_mask_jpg = buf.tobytes()

        # Par défaut : toutes les zones couleur sont affichées (candidates)
        zones = build_zones(cnts, None, W, H)
        res, conf, stage = None, 0.0, "color"

        # ── Étape 2 : validation IA (dès qu'il y a une zone couleur) ──
        if candidate:
            wait = API_MIN_INTERVAL - (time.time() - last_api)
            if wait > 0:
                time.sleep(wait)
            t0 = time.time()
            last_api = t0
            res = analyser(frame)

            if "erreur" in res:
                with state_lock:
                    if not state["paused"]:
                        state.update(status="error", error=res["erreur"], polygons=shown(zones),
                                     zones=strip(zones), count=0, confidence=0.0, candidate=True,
                                     hsv_pct=round(cov, 1))
                last_zones, last_res = zones, None
                last_overlay_jpg = draw_overlay(bgr, zones, None) or last_overlay_jpg
                print(f"[IA] ⚠️  {res['erreur']}")
                spray.force_release()
                time.sleep(1)
                continue

            stage = "color+ai"
            zones = build_zones(cnts, res, W, H)
            confirmed = [z["conf"] for z in zones if z["kind"] == "confirmed"]
            conf = max(confirmed) if confirmed else res["confidence"]

        last_zones, last_res = zones, res
        last_overlay_jpg = draw_overlay(bgr, zones, res) or last_overlay_jpg

        lat = (time.time() - t0) * 1000
        n_conf = sum(1 for z in zones if z["kind"] == "confirmed")
        corr = n_conf > 0

        with state_lock:
            if state["paused"]:
                continue
            history.append(corr)
            alert = len(history) == CONFIRM_FRAMES and all(history)
            status = "corrosion" if alert else ("probable" if corr else "healthy")

            now = time.time()
            stamps.append(now)
            fps = (len(stamps) - 1) / (stamps[-1] - stamps[0]) if len(stamps) > 1 and stamps[-1] > stamps[0] else 0.0

            state.update(
                status=status, error=None, confidence=conf, count=n_conf,
                polygons=shown(zones), zones=strip(zones),
                candidate=candidate, hsv_pct=round(cov, 1), stage=stage,
                last_update=now, latency_ms=round(lat), fps=round(fps, 1),
                frames=state["frames"] + 1,
                alerts=state["alerts"] + (1 if alert else 0),
            )

        spray.update(status)

        if status != last_print_status or now - last_print_t >= 1.0:
            last_print_status, last_print_t = status, now
            if alert:
                print(f"⚠️  [ LIVE ] CORROSION DÉTECTÉE --> IA : {conf:.2f}%  (couleur {cov:.1f}%)")
            elif corr:
                print(f"⏳ [ LIVE ] corrosion probable ({conf:.1f}%) — confirmation...")
            elif candidate:
                print(f"🔎 [ LIVE ] {len(zones)} zone(s) couleur {cov:.1f}% — IA : {conf:.1f}% (non confirmé)")
            else:
                print(f"✅ [ LIVE ] STRUCTURE SAINE  (couleur {cov:.1f}%)")

        time.sleep(LOOP_DELAY)


# ─── ROUTES ──────────────────────────────────────────────
@app.get("/")
def index():
    resp = app.make_response(render_template("index.html"))
    resp.headers["Cache-Control"] = "no-store"      # jamais de vieille page en cache
    return resp


def mjpeg_generator():
    """Flux vidéo principal. Si SERVER_DRAW : contours exacts dessinés sur chaque frame."""
    while True:
        with output.condition:
            output.condition.wait(timeout=5)
            frame = output.frame
        if frame is None:
            continue
        if SERVER_DRAW and last_zones and not state["paused"]:
            bgr = cv2.imdecode(np.frombuffer(frame, np.uint8), cv2.IMREAD_COLOR)
            if bgr is not None:
                jpg = draw_overlay(bgr, last_zones, last_res)
                if jpg:
                    frame = jpg
        yield (b"--FRAME\r\nContent-Type: image/jpeg\r\nContent-Length: "
               + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")


def jpg_stream(getter):
    last = None
    while True:
        jpg = getter()
        if jpg is not None and jpg is not last:
            last = jpg
            yield (b"--FRAME\r\nContent-Type: image/jpeg\r\nContent-Length: "
                   + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
        time.sleep(0.1)


_MJPEG = "multipart/x-mixed-replace; boundary=FRAME"


@app.get("/video_feed")
def video_feed():
    return Response(mjpeg_generator(), mimetype=_MJPEG, headers={"Cache-Control": "no-store"})


@app.get("/mask_feed")
def mask_feed():
    """Ce que voit le filtre couleur (pour régler HSV_RANGES)."""
    return Response(jpg_stream(lambda: last_mask_jpg), mimetype=_MJPEG,
                    headers={"Cache-Control": "no-store"})


@app.get("/overlay_feed")
def overlay_feed():
    """Vidéo avec les contours exacts dessinés côté serveur."""
    return Response(jpg_stream(lambda: last_overlay_jpg), mimetype=_MJPEG,
                    headers={"Cache-Control": "no-store"})


@app.get("/api/status")
def api_status():
    with state_lock:
        return jsonify(public_state())


@app.post("/api/toggle")
def api_toggle():
    with state_lock:
        state["paused"] = not state["paused"]
        if state["paused"]:
            state.update(status="paused", polygons=[], count=0, confidence=0.0)
            spray.force_release()
        else:
            history.clear()
            state.update(status="starting", error=None)
        return jsonify(public_state())


@app.post("/api/reset")
def api_reset():
    with state_lock:
        history.clear()
        stamps.clear()
        state.update(frames=0, alerts=0, fps=0.0, latency_ms=0)
        return jsonify(public_state())


@app.post("/api/spray/arm")
def api_spray_arm():
    spray.set_armed(not spray.armed)
    with state_lock:
        return jsonify(public_state())


@app.post("/api/spray/test")
def api_spray_test():
    spray.test()
    with state_lock:
        return jsonify(public_state())


@app.get("/snapshot.jpg")
def snapshot():
    with output.condition:
        frame = output.frame
    if frame is None:
        return ("No frame available yet", 503)
    name = time.strftime("inspection_%Y%m%d_%H%M%S.jpg")
    return Response(frame, mimetype="image/jpeg",
                    headers={"Content-Disposition": f"attachment; filename={name}"})


# ─── MAIN ────────────────────────────────────────────────
def main():
    global picam2
    if not API_KEY:
        sys.exit('❌ Variable ROBOFLOW_API_KEY manquante : export ROBOFLOW_API_KEY="ta_cle"')

    picam2 = Picamera2()
    picam2.configure(picam2.create_video_configuration(
        main={"size": (FRAME_WIDTH, FRAME_HEIGHT)},
        controls={"FrameRate": FRAME_RATE},
    ))
    picam2.start_recording(MJPEGEncoder(), FileOutput(output))
    try:
        picam2.set_controls({"AfMode": controls.AfModeEnum.Continuous})
    except Exception as e:
        print(f"[CAM] Autofocus non disponible : {e}")
    atexit.register(lambda: picam2.stop_recording())
    atexit.register(spray.shutdown)

    threading.Thread(target=detection_loop, daemon=True).start()

    print(f"\n🚁 AeroScan AI en ligne → http://<IP_DU_PI>:{PORT}   "
          f"(contours : /overlay_feed — filtre couleur : /mask_feed)\n")
    app.run(host=HOST, port=PORT, threaded=True, debug=False)


if __name__ == "__main__":
    main()
