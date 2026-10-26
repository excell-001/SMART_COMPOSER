"""Dashboard monitoring IoT (Flask) untuk Vercel + database Supabase.

Alur: ESP32 --POST /api/data--> Flask (Vercel) --> Supabase --> dashboard.
Semua rahasia dibaca dari Environment Variables, jangan ditulis di kode:
  SUPABASE_URL     contoh: https://abcdxyz.supabase.co
  SUPABASE_KEY     Secret key (atau service_role key) dari Supabase
  DEVICE_API_KEY   kata sandi acak buatan kamu, dipakai ESP32 saat mengirim data
"""
import csv
import hmac
import io
import os
import time
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, Response, jsonify, render_template, request

# ====== KONFIGURASI ======
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
DEVICE_API_KEY = os.environ.get("DEVICE_API_KEY", "")

TABLE = "readings"
# nama di dashboard/JSON -> nama kolom di database
FIELDS = {"suhu": "suhu", "lembap": "lembap", "nilaiGas": "nilai_gas"}

REFRESH_SECONDS = 15   # dashboard memuat ulang data tiap sekian detik
ONLINE_WINDOW = 90     # device dianggap online kalau data terakhir lebih baru dari ini (detik)
PAGE = 1000            # batas baris per permintaan ke Supabase
MAX_CSV_ROWS = 20000   # batas baris untuk satu file CSV

# Zona waktu WIB (UTC+7) untuk tampilan jam
WIB = timezone(timedelta(hours=7))

app = Flask(__name__)

SELECT_COLS = "ts," + ",".join(FIELDS.values())


# ====== Helper Supabase ======
def sb_ready():
    return bool(SUPABASE_URL and SUPABASE_KEY)


def sb_headers(extra=None):
    h = {"apikey": SUPABASE_KEY, "Content-Type": "application/json"}
    # Key lama (service_role) berbentuk JWT dan perlu dikirim juga sebagai Bearer.
    # Key baru (sb_secret_...) cukup lewat header apikey.
    if SUPABASE_KEY.startswith("eyJ"):
        h["Authorization"] = f"Bearer {SUPABASE_KEY}"
    if extra:
        h.update(extra)
    return h


def sb_table_url():
    return f"{SUPABASE_URL}/rest/v1/{TABLE}"


def sb_select(extra_params):
    params = [("select", SELECT_COLS)] + extra_params
    r = requests.get(sb_table_url(), headers=sb_headers(), params=params, timeout=8)
    r.raise_for_status()
    return r.json()


def error_json(message, status):
    return jsonify({"error": message}), status


def not_configured():
    return error_json("SUPABASE_URL / SUPABASE_KEY belum di-set di server", 500)


# ====== Helper umum ======
def fmt_ts(ts):
    """Epoch -> '2026-10-25 22:39:10' dalam WIB."""
    return datetime.fromtimestamp(ts, WIB).strftime("%Y-%m-%d %H:%M:%S")


def row_to_dict(row):
    d = {"ts": row["ts"], "waktu": fmt_ts(row["ts"])}
    for name, col in FIELDS.items():
        d[name] = row.get(col)
    return d


def date_range(date_str):
    """'2026-10-25' -> (epoch awal hari WIB, epoch awal hari berikutnya)."""
    start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=WIB)
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())


def date_filter(date_str):
    """Parameter filter tanggal untuk Supabase. Lempar ValueError kalau format salah."""
    if not date_str:
        return []
    start, end = date_range(date_str)
    return [("ts", f"gte.{start}"), ("ts", f"lt.{end}")]


# ====== Halaman ======
@app.route("/")
def index():
    return render_template("index.html", poll_seconds=REFRESH_SECONDS)


@app.route("/api/health")
def api_health():
    """Cek cepat konfigurasi (tidak menampilkan isi rahasia)."""
    return jsonify(
        {
            "supabase_configured": sb_ready(),
            "device_key_configured": bool(DEVICE_API_KEY),
        }
    )


# ====== Terima data dari ESP32 ======
@app.route("/api/data", methods=["POST"])
def api_data():
    if not DEVICE_API_KEY:
        return error_json("DEVICE_API_KEY belum di-set di server", 500)
    if not sb_ready():
        return not_configured()

    sent = request.headers.get("X-API-Key", "")
    if not hmac.compare_digest(sent.encode(), DEVICE_API_KEY.encode()):
        return error_json("API key salah", 401)

    body = request.get_json(silent=True) or {}
    row = {"ts": int(time.time())}
    got_value = False
    for name, col in FIELDS.items():
        v = body.get(name)
        valid = isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) < 1e9
        row[col] = float(v) if valid else None
        got_value = got_value or valid
    if not got_value:
        return error_json("Tidak ada nilai sensor yang valid", 400)

    try:
        r = requests.post(
            sb_table_url(),
            headers=sb_headers({"Prefer": "return=minimal"}),
            json=row,
            timeout=8,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        print("Gagal simpan ke Supabase:", e)
        return error_json("Gagal menyimpan ke database", 502)
    return jsonify({"ok": True, "ts": row["ts"]}), 201


# ====== Data untuk dashboard ======
@app.route("/api/latest")
def api_latest():
    if not sb_ready():
        return not_configured()
    try:
        rows = sb_select([("order", "ts.desc,id.desc"), ("limit", "1")])
    except requests.RequestException as e:
        print("Gagal baca Supabase:", e)
        return error_json("Gagal membaca database", 502)
    if not rows:
        return jsonify({"ready": False})
    data = row_to_dict(rows[0])
    data["ready"] = True
    data["online"] = (time.time() - rows[0]["ts"]) <= ONLINE_WINDOW
    return jsonify(data)


@app.route("/api/history")
def api_history():
    """100 data terakhir untuk grafik (urut dari lama ke baru)."""
    if not sb_ready():
        return not_configured()
    try:
        rows = sb_select([("order", "ts.desc,id.desc"), ("limit", "100")])
    except requests.RequestException as e:
        print("Gagal baca Supabase:", e)
        return error_json("Gagal membaca database", 502)
    rows.reverse()
    return jsonify([row_to_dict(r) for r in rows])


@app.route("/api/log")
def api_log():
    """Log data berjam. Parameter: ?date=2026-10-25&limit=200 (terbaru dulu)."""
    if not sb_ready():
        return not_configured()
    limit = max(1, min(request.args.get("limit", default=200, type=int), PAGE))
    date_str = request.args.get("date", "").strip()
    try:
        filt = date_filter(date_str)
    except ValueError:
        return error_json("Format tanggal harus YYYY-MM-DD", 400)
    try:
        rows = sb_select(filt + [("order", "ts.desc,id.desc"), ("limit", str(limit))])
    except requests.RequestException as e:
        print("Gagal baca Supabase:", e)
        return error_json("Gagal membaca database", 502)
    return jsonify([row_to_dict(r) for r in rows])


@app.route("/log.csv")
def log_csv():
    """Download log sebagai CSV. Parameter opsional: ?date=2026-10-25"""
    if not sb_ready():
        return not_configured()
    date_str = request.args.get("date", "").strip()
    try:
        filt = date_filter(date_str)
    except ValueError:
        return error_json("Format tanggal harus YYYY-MM-DD", 400)

    all_rows = []
    try:
        offset = 0
        while len(all_rows) < MAX_CSV_ROWS:
            page = sb_select(
                filt
                + [
                    ("order", "ts.asc,id.asc"),
                    ("limit", str(PAGE)),
                    ("offset", str(offset)),
                ]
            )
            all_rows.extend(page)
            if len(page) < PAGE:
                break
            offset += PAGE
    except requests.RequestException as e:
        print("Gagal baca Supabase:", e)
        return error_json("Gagal membaca database", 502)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["waktu_wib", *FIELDS.keys()])
    for r in all_rows:
        writer.writerow([fmt_ts(r["ts"]), *[r.get(col) for col in FIELDS.values()]])

    filename = f"log_{date_str or 'semua'}.csv"
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


if __name__ == "__main__":
    # Hanya untuk uji lokal. Di Vercel, aplikasi dijalankan otomatis lewat variabel `app`.
    app.run(host="127.0.0.1", port=5000)
