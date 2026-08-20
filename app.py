#!/usr/bin/env python3
"""
Certego inventering – lokal server med delad data
==========================================
Serverar appen OCH tar emot synk från flera enheter. Delad data sparas i en
SQLite-fil (certego_inventering.db) bredvid den här filen.

Installation (en gång):
    pip install flask

Kör:
    python app.py
    -> öppna http://localhost:8000 på datorn

Synk: i appen (fliken Översikt -> Server & synk) lämnas serveradressen TOM när
appen öppnas från den här servern. Tryck "Synka mot server" – din lokala data
skickas upp, slås ihop med serverns, och den sammanslagna datan kommer tillbaka.
Nyaste ändring vinner per objekt; Dörr-id reserveras och återanvänds aldrig.

Backup: kopiera filen certego_inventering.db.

OBS om telefoner ska nå servern över nätverket OCH ha offline-läge:
offline (PWA) kräver https. Kör då servern bakom https (intern certifikat via IT,
eller verktyg som Caddy/mkcert). Över vanlig http funkar synk men inte offline-
installation på telefon. På din egen dator (localhost) funkar allt direkt.
"""
import json
import os
import sqlite3
import threading
import base64
import re
from flask import Flask, request, jsonify, send_from_directory, Response

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", ROOT)
os.makedirs(DATA_DIR, exist_ok=True)
DB = os.path.join(DATA_DIR, "certego_inventering.db")
PHOTODIR = os.path.join(DATA_DIR, "photos")
PORT = int(os.environ.get("PORT", "8000"))

app = Flask(__name__)
lock = threading.Lock()


def db():
    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
    return c


def load_bundle():
    c = db()
    row = c.execute("SELECT v FROM kv WHERE k='bundle'").fetchone()
    c.close()
    if row:
        try:
            return json.loads(row[0])
        except Exception:
            pass
    return {"objects": [], "didPool": {}, "didReserved": {}, "equip": []}


def save_bundle(b):
    c = db()
    c.execute(
        "INSERT INTO kv(k,v) VALUES('bundle',?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (json.dumps(b, ensure_ascii=False),),
    )
    c.commit()
    c.close()


def merge(server, inc):
    """Slå ihop inkommande klientdata i serverns – samma logik som i appen."""
    objs = {o["key"]: o for o in server.get("objects", []) if o.get("key")}
    for io in inc.get("objects", []):
        k = io.get("key")
        if not k:
            continue
        ex = objs.get(k)
        if ex is None or (io.get("ts") or "") > (ex.get("ts") or ""):
            objs[k] = io
    server["objects"] = list(objs.values())

    res = server.get("didReserved", {}) or {}
    for k in (inc.get("didReserved", {}) or {}):
        res[k] = True
    for o in server["objects"]:
        if o.get("di"):
            res[o["di"]] = True
    server["didReserved"] = res

    pool = server.get("didPool", {}) or {}
    for k, arr in (inc.get("didPool", {}) or {}).items():
        cur = pool.get(k, [])
        seen = set(cur)
        pool[k] = cur + [x for x in arr if x not in seen]
    server["didPool"] = pool

    eq = {e["key"]: e for e in server.get("equip", []) if e.get("key")}
    for ie in inc.get("equip", []):
        k = ie.get("key")
        if not k:
            continue
        ex = eq.get(k)
        if ex is None or (ie.get("ts") or "") > (ex.get("ts") or ""):
            eq[k] = ie
    server["equip"] = list(eq.values())
    return server


def cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return resp


@app.route("/api/sync", methods=["POST", "OPTIONS"])
def api_sync():
    if request.method == "OPTIONS":
        return cors(Response())
    inc = request.get_json(force=True, silent=True) or {}
    with lock:
        bundle = merge(load_bundle(), inc)
        save_bundle(bundle)
    return cors(jsonify(bundle))


@app.route("/api/state")
def api_state():
    return cors(jsonify(load_bundle()))


def safe_id(s):
    return re.sub(r"[^A-Za-z0-9_-]", "", str(s))


@app.route("/api/photos")
def api_photos():
    ids = []
    if os.path.isdir(PHOTODIR):
        ids = [f[:-4] for f in os.listdir(PHOTODIR) if f.endswith(".jpg")]
    return cors(jsonify({"ids": ids}))


@app.route("/api/photo", methods=["POST", "OPTIONS"])
def api_photo_post():
    if request.method == "OPTIONS":
        return cors(Response())
    d = request.get_json(force=True, silent=True) or {}
    pid = safe_id(d.get("id", ""))
    data = d.get("data", "")
    if not pid or "," not in data:
        return cors(Response(status=400))
    try:
        raw = base64.b64decode(data.split(",", 1)[1])
    except Exception:
        return cors(Response(status=400))
    os.makedirs(PHOTODIR, exist_ok=True)
    with open(os.path.join(PHOTODIR, pid + ".jpg"), "wb") as fh:
        fh.write(raw)
    return cors(jsonify({"ok": True, "id": pid}))


@app.route("/api/photo/<pid>")
def api_photo_get(pid):
    pid = safe_id(pid)
    if not os.path.exists(os.path.join(PHOTODIR, pid + ".jpg")):
        return Response(status=404)
    return cors(send_from_directory(PHOTODIR, pid + ".jpg"))


@app.route("/")
def index():
    return send_from_directory(ROOT, "index.html")


@app.route("/<path:p>")
def files(p):
    if p.startswith("api/"):
        return Response(status=404)
    return send_from_directory(ROOT, p)


if __name__ == "__main__":
    import socket
    def lan_ip():
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"
    print("Certego inventering-server körs:")
    print("  På datorn:              http://localhost:%d" % PORT)
    print("  På telefon (samma WiFi): http://%s:%d" % (lan_ip(), PORT))
    print("Delad data sparas i:", DB)
    print("Tryck Ctrl+C för att avsluta.")
    app.run(host="0.0.0.0", port=PORT)
