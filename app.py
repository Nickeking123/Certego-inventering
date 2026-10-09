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
import io
import zipfile
import hashlib
import datetime
from flask import Flask, request, jsonify, send_from_directory, Response

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", ROOT)
APP_CODE = os.environ.get("APP_CODE", "").strip()
os.makedirs(DATA_DIR, exist_ok=True)
DB = os.path.join(DATA_DIR, "certego_inventering.db")
PHOTODIR = os.path.join(DATA_DIR, "photos")
RITARKIV = os.path.join(PHOTODIR, "ritarkiv")  # tidigare/borttagna ritningar – raderas aldrig av appen
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
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-App-Code"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS,DELETE"
    return resp


@app.before_request
def _gate():
    if not APP_CODE:
        return
    if request.method == "OPTIONS":
        return
    p = request.path or ""
    if p.startswith("/api/"):
        if request.headers.get("X-App-Code", "") != APP_CODE:
            r = cors(jsonify({"error": "unauthorized"}))
            r.status_code = 401
            return r


@app.route("/api/auth")
def api_auth():
    return cors(jsonify({"ok": True}))


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


# ---------------------------------------------------------------------------
# Ritningar (id RIT_<område>-<hus>): versionskontroll och arkiv.
# En ritning skrivs aldrig över tyst: finns en annan version krävs replace=true,
# och den gamla flyttas då till photos/ritarkiv/. Samma sak vid borttagning.
# ---------------------------------------------------------------------------
_sha_cache = {}


def file_sha(path):
    st = os.stat(path)
    hit = _sha_cache.get(path)
    if hit and hit[0] == st.st_size and hit[1] == st.st_mtime_ns:
        return hit[2]
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    v = h.hexdigest()[:16]
    _sha_cache[path] = (st.st_size, st.st_mtime_ns, v)
    return v


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def read_json(p):
    try:
        with open(p, encoding="utf-8") as fh:
            v = json.load(fh)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def write_json(p, v):
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(v, fh, ensure_ascii=False)
    os.replace(tmp, p)


def write_bytes(p, raw):
    tmp = p + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(raw)
    os.replace(tmp, p)


def rit_meta_path(pid):
    return os.path.join(PHOTODIR, pid + ".meta.json")


def clean_meta(m):
    out = {}
    if isinstance(m, dict):
        for k in ("fil", "importerad", "kalla"):
            v = m.get(k)
            if isinstance(v, str):
                out[k] = v[:200]
    return out


def archive_bytes(pid, raw, meta, reason):
    """Spara en version i arkivet. Samma innehåll sparas bara en gång."""
    os.makedirs(RITARKIV, exist_ok=True)
    sha = hashlib.sha256(raw).hexdigest()[:16]
    for f in os.listdir(RITARKIV):
        if f.startswith(pid + "__") and f.endswith("_" + sha + ".jpg"):
            return f[:-4]
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    ver = "%s__%s_%s" % (pid, stamp, sha)
    write_bytes(os.path.join(RITARKIV, ver + ".jpg"), raw)
    m = dict(meta or {})
    m.update({"sha": sha, "arkiverad": now_iso(), "orsak": reason})
    write_json(os.path.join(RITARKIV, ver + ".meta.json"), m)
    return ver


def archive_current(pid, reason):
    p = os.path.join(PHOTODIR, pid + ".jpg")
    if not os.path.exists(p):
        return None
    with open(p, "rb") as fh:
        raw = fh.read()
    return archive_bytes(pid, raw, read_json(rit_meta_path(pid)), reason)


def arkiv_counts():
    c = {}
    if os.path.isdir(RITARKIV):
        for f in os.listdir(RITARKIV):
            if f.endswith(".jpg") and "__" in f:
                pid = f.split("__", 1)[0]
                c[pid] = c.get(pid, 0) + 1
    return c


def save_rit(pid, raw, d):
    meta = clean_meta(d.get("meta"))
    sha = hashlib.sha256(raw).hexdigest()[:16]
    with lock:
        os.makedirs(PHOTODIR, exist_ok=True)
        if d.get("archiveOnly"):
            ver = archive_bytes(pid, raw, meta, "sparad från enhet")
            return jsonify({"ok": True, "archived": ver, "sha": sha})
        dst = os.path.join(PHOTODIR, pid + ".jpg")
        if os.path.exists(dst):
            cur = file_sha(dst)
            if cur == sha:
                return jsonify({"ok": True, "sha": sha, "same": True})
            if not d.get("replace"):
                r = jsonify({"error": "exists", "sha": cur})
                r.status_code = 409
                return r
            archive_current(pid, "ersatt")
        write_bytes(dst, raw)
        meta.update({"uppladdad": now_iso(), "sha": sha})
        write_json(rit_meta_path(pid), meta)
        return jsonify({"ok": True, "sha": sha})


@app.route("/api/ritinfo")
def api_ritinfo():
    out = {}
    arch = arkiv_counts()
    if os.path.isdir(PHOTODIR):
        for f in os.listdir(PHOTODIR):
            if f.startswith("RIT_") and f.endswith(".jpg"):
                pid = f[:-4]
                p = os.path.join(PHOTODIR, f)
                try:
                    out[pid] = {"sha": file_sha(p), "size": os.path.getsize(p),
                                "meta": read_json(rit_meta_path(pid)), "arkiv": arch.get(pid, 0)}
                except OSError:
                    pass
    return cors(jsonify(out))


@app.route("/api/ritarkiv")
def api_ritarkiv_all():
    return cors(jsonify({"keys": arkiv_counts()}))


@app.route("/api/ritarkiv/<pid>")
def api_ritarkiv_list(pid):
    pid = safe_id(pid)
    vs = []
    if os.path.isdir(RITARKIV):
        for f in sorted(os.listdir(RITARKIV), reverse=True):
            if f.startswith(pid + "__") and f.endswith(".jpg"):
                ver = f[:-4]
                m = read_json(os.path.join(RITARKIV, ver + ".meta.json"))
                m["ver"] = ver
                vs.append(m)
    return cors(jsonify({"versions": vs}))


@app.route("/api/ritarkiv/<pid>/<ver>")
def api_ritarkiv_get(pid, ver):
    pid, ver = safe_id(pid), safe_id(ver)
    if not ver.startswith(pid + "__") or not os.path.exists(os.path.join(RITARKIV, ver + ".jpg")):
        return Response(status=404)
    return cors(send_from_directory(RITARKIV, ver + ".jpg"))


@app.route("/api/ritarkiv/restore", methods=["POST", "OPTIONS"])
def api_ritarkiv_restore():
    if request.method == "OPTIONS":
        return cors(Response())
    d = request.get_json(force=True, silent=True) or {}
    pid, ver = safe_id(d.get("id", "")), safe_id(d.get("ver", ""))
    src = os.path.join(RITARKIV, ver + ".jpg")
    if not pid.startswith("RIT_") or not ver.startswith(pid + "__") or not os.path.exists(src):
        return cors(Response(status=404))
    with lock:
        archive_current(pid, "ersatt vid återställning")
        with open(src, "rb") as fh:
            raw = fh.read()
        os.makedirs(PHOTODIR, exist_ok=True)
        write_bytes(os.path.join(PHOTODIR, pid + ".jpg"), raw)
        sha = hashlib.sha256(raw).hexdigest()[:16]
        meta = clean_meta(read_json(os.path.join(RITARKIV, ver + ".meta.json")))
        meta.update({"uppladdad": now_iso(), "sha": sha, "aterstalld_fran": ver})
        write_json(rit_meta_path(pid), meta)
    return cors(jsonify({"ok": True, "sha": sha}))


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
    if pid.startswith("RIT_"):
        return cors(save_rit(pid, raw, d))
    os.makedirs(PHOTODIR, exist_ok=True)
    with open(os.path.join(PHOTODIR, pid + ".jpg"), "wb") as fh:
        fh.write(raw)
    return cors(jsonify({"ok": True, "id": pid}))


@app.route("/api/photo/<pid>", methods=["GET", "DELETE"])
def api_photo_get(pid):
    pid = safe_id(pid)
    _p = os.path.join(PHOTODIR, pid + ".jpg")
    if request.method == "DELETE":
        if pid.startswith("RIT_"):
            # Ritningar arkiveras i stället för att raderas, så de kan återställas.
            with lock:
                archive_current(pid, "borttagen")
                for p in (_p, rit_meta_path(pid)):
                    if os.path.exists(p):
                        os.remove(p)
            return cors(jsonify({"deleted": True, "archived": True}))
        if os.path.exists(_p):
            os.remove(_p)
        return cors(jsonify({"deleted": True}))
    if not os.path.exists(_p):
        return Response(status=404)
    return cors(send_from_directory(PHOTODIR, pid + ".jpg"))


def photo_map():
    """photoid -> (omr, filnamnsbas). Objekt: di-index. Utrustning: omr-löpnr."""
    b = load_bundle()
    mp = {}
    for o in b.get("objects", []):
        omr = (o.get("omr") or "").strip()
        di = (o.get("di") or o.get("key") or "").strip()
        for i, pid in enumerate(o.get("photos") or []):
            base = di if di else safe_id(pid)
            mp[safe_id(pid)] = (omr, "%s-%d" % (base, i + 1))
    seq = {}
    for e in b.get("equip", []):
        omr = (e.get("omr") or "").strip()
        for pid in (e.get("photos") or []):
            seq[omr] = seq.get(omr, 0) + 1
            mp[safe_id(pid)] = (omr, "%s-%d" % (omr, seq[omr]))
    return mp


@app.route("/api/photos/areas")
def api_photos_areas():
    mp = photo_map()
    counts = {}
    if os.path.isdir(PHOTODIR):
        have = {f[:-4] for f in os.listdir(PHOTODIR) if f.endswith(".jpg")}
        for pid, (omr, base) in mp.items():
            if pid in have and omr:
                counts[omr] = counts.get(omr, 0) + 1
    return cors(jsonify({"areas": counts}))


@app.route("/api/photos/export")
def api_photos_export():
    omr = (request.args.get("omr") or "").strip()
    mp = photo_map()
    buf = io.BytesIO()
    n = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if os.path.isdir(PHOTODIR):
            for f in os.listdir(PHOTODIR):
                if not f.endswith(".jpg"):
                    continue
                pid = f[:-4]
                info = mp.get(pid)
                if not info:
                    continue
                fomr, base = info
                if omr and fomr != omr:
                    continue
                z.write(os.path.join(PHOTODIR, f), base + ".jpg")
                n += 1
    buf.seek(0)
    fn = "foton_%s.zip" % (omr or "alla")
    resp = Response(buf.read(), mimetype="application/zip")
    resp.headers["Content-Disposition"] = "attachment; filename=%s" % fn
    resp.headers["X-Photo-Count"] = str(n)
    return cors(resp)


@app.route("/api/photos/clear", methods=["POST", "OPTIONS"])
def api_photos_clear():
    if request.method == "OPTIONS":
        return cors(Response())
    d = request.get_json(force=True, silent=True) or {}
    omr = (d.get("omr") or "").strip()
    if not omr:
        return cors(Response(status=400))
    mp = photo_map()
    n = 0
    if os.path.isdir(PHOTODIR):
        for pid, (fomr, base) in mp.items():
            if fomr == omr:
                p = os.path.join(PHOTODIR, pid + ".jpg")
                if os.path.exists(p):
                    os.remove(p)
                    n += 1
    return cors(jsonify({"cleared": n, "omr": omr}))


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
