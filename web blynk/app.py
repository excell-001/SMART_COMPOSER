import csv
import io
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, Response, jsonify, render_template, request

# ====== KONFIGURASI ======
# Jangan taruh token di kode. Set lewat environment variable:
#   Linux/Mac : export BLYNK_TOKEN="token_kamu"
#   Windows   : set BLYNK_TOKEN=token_kamu
BLYNK_TOKEN = os.environ.get("BLYNK_TOKEN", "")
# Sesuaikan dengan region di dashboard Blynk (blynk.cloud, sgp1.blynk.cloud, dll)
BLYNK_HOST = os.environ.get("BLYNK_HOST", "blynk.cloud")

# Nama tampilan -> virtual pin di Blynk. Ubah sesuai datastream kamu.
PINS = {"suhu": "v0", "lembap": "v1", "nilaiGas": "v2"}
FIELDS = list(PINS.keys())

POLL_SECONDS = 10  # jangan terlalu cepat supaya tidak kena rate limit
DB_FILE = "data.db"

# Zona waktu WIB (UTC+7) untuk tampilan jam di log
WIB = timezone(timedelta(hours=7))

app = Flask(__name__)


def db():
    return sqlite3.connect(DB_FILE)


def init_db():
    cols = ", ".join(f"{f} REAL" for f in FIELDS)
    with db() as conn:
        existing = [r[1] for r in conn.execute("PRAGMA table_info(readings)")]
        # Kalau tabel lama punya kolom beda (misal temperature/humidity),
        # simpan sebagai cadangan lalu buat tabel baru.
        if existing and any(f not in existing for f in FIELDS):
            conn.execute(f"ALTER TABLE readings RENAME TO readings_old_{int(time.time())}")
        conn.execute(f"CREATE TABLE IF NOT EXISTS readings (ts INTEGER NOT NULL, {cols})")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(ts)")


def fmt_ts(ts):
    """Epoch -> '2026-10-25 22:39:10' dalam WIB."""
    return datetime.fromtimestamp(ts, WIB).strftime("%Y-%m-%d %H:%M:%S")


def row_to_dict(row):
    d = {"ts": row[0], "waktu": fmt_ts(row[0])}
    for i, f in enumerate(FIELDS):
        d[f] = row[i + 1]
    return d


def date_range(date_str):
    """'2026-10-25' -> (epoch awal hari WIB, epoch awal hari berikutnya)."""
    start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=WIB)
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())


def fetch_blynk():
    """Ambil nilai terbaru semua pin sekaligus dari Blynk."""
    pin_query = "&".join(PINS.values())
    url = f"https://{BLYNK_HOST}/external/api/get?token={BLYNK_TOKEN}&{pin_query}"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    data = r.json()  # contoh: {"v0": 28.5, "v1": 70, "v2": 300}
    return {name: data.get(pin) for name, pin in PINS.items()}


def is_device_online():
    url = f"https://{BLYNK_HOST}/external/api/isHardwareConnected?token={BLYNK_TOKEN}"
    try:
        r = requests.get(url, timeout=10)
        return r.text.strip().lower() == "true"
    except requests.RequestException:
        return False


def poll_loop():
    """Thread latar belakang: ambil data tiap POLL_SECONDS lalu simpan ke SQLite."""
    placeholders = ", ".join("?" for _ in range(len(FIELDS) + 1))
    insert_sql = f"INSERT INTO readings (ts, {', '.join(FIELDS)}) VALUES ({placeholders})"
    while True:
        try:
            values = fetch_blynk()
            if any(v is not None for v in values.values()):
                with db() as conn:
                    conn.execute(
                        insert_sql, (int(time.time()), *[values[f] for f in FIELDS])
                    )
        except Exception as e:
            print("Gagal ambil data Blynk:", e)
        time.sleep(POLL_SECONDS)


@app.route("/")
def index():
    return render_template("index.html", poll_seconds=POLL_SECONDS)


@app.route("/api/latest")
def api_latest():
    with db() as conn:
        row = conn.execute(
            f"SELECT ts, {', '.join(FIELDS)} FROM readings ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    if not row:
        return jsonify({"ready": False})
    data = row_to_dict(row)
    data["ready"] = True
    data["online"] = is_device_online()
    return jsonify(data)


@app.route("/api/history")
def api_history():
    """100 data terakhir untuk grafik (urut dari lama ke baru)."""
    with db() as conn:
        rows = conn.execute(
            f"SELECT ts, {', '.join(FIELDS)} FROM readings ORDER BY ts DESC LIMIT 100"
        ).fetchall()
    rows.reverse()
    return jsonify([row_to_dict(r) for r in rows])


@app.route("/api/log")
def api_log():
    """Log data lengkap dengan jam. Parameter: ?date=2026-10-25&limit=200 (terbaru dulu)."""
    limit = min(request.args.get("limit", default=200, type=int), 2000)
    date_str = request.args.get("date", "").strip()

    sql = f"SELECT ts, {', '.join(FIELDS)} FROM readings"
    params = []
    if date_str:
        try:
            start, end = date_range(date_str)
        except ValueError:
            return jsonify({"error": "Format tanggal harus YYYY-MM-DD"}), 400
        sql += " WHERE ts >= ? AND ts < ?"
        params += [start, end]
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)

    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return jsonify([row_to_dict(r) for r in rows])


@app.route("/log.csv")
def log_csv():
    """Download log sebagai CSV. Parameter opsional: ?date=2026-10-25"""
    date_str = request.args.get("date", "").strip()

    sql = f"SELECT ts, {', '.join(FIELDS)} FROM readings"
    params = []
    if date_str:
        try:
            start, end = date_range(date_str)
        except ValueError:
            return "Format tanggal harus YYYY-MM-DD", 400
        sql += " WHERE ts >= ? AND ts < ?"
        params += [start, end]
    sql += " ORDER BY ts ASC"

    with db() as conn:
        rows = conn.execute(sql, params).fetchall()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["waktu_wib", *FIELDS])
    for r in rows:
        writer.writerow([fmt_ts(r[0]), *r[1:]])

    filename = f"log_{date_str or 'semua'}.csv"
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


if __name__ == "__main__":
    if not BLYNK_TOKEN:
        raise SystemExit("BLYNK_TOKEN belum di-set. Lihat bagian KONFIGURASI di app.py")
    init_db()
    threading.Thread(target=poll_loop, daemon=True).start()
    # use_reloader=False supaya thread polling tidak jalan dobel
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)