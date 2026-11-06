"""
WidyaTata AI - Sistem Informasi Tata Kelola Administrasi Akademik,
Kepanitiaan, dan Asisten Anggaran Cerdas (Bali).

Alur data: User -> Frontend -> Backend (Flask) -> Database (SQLite) -> AI/API (Gemini) -> Result
API Key TIDAK pernah ditulis di source code: dibaca dari environment variable GEMINI_API_KEY.
"""

import json
import os
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

load_dotenv()

# --------------------------------------------------------------------------
# Konfigurasi
# --------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "widyatata.db")

GEMINI_MODEL = "gemini-3.1-flash-lite"
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent"
)
GEMINI_TIMEOUT = 90  # detik

WITA = timezone(timedelta(hours=8))  # Waktu Indonesia Tengah (Bali)

KATEGORI_ORGANISASI = [
    "BEM/UKM Kampus",
    "OSIS SMA/SMK",
    "Pramuka/Dewan Kerja",
    "Komunitas Pemuda Bali",
]

DOC_TYPES = {
    "peminjaman_gedung": "Surat Peminjaman Gedung Kampus/Sekolah",
    "undangan_pemateri": "Surat Undangan Pemateri/Juri",
    "sponsorship": "Surat Permohonan Sponsorship",
    "lpj": "Ringkasan Laporan Pertanggungjawaban (LPJ)",
}

DOC_GUIDANCE = {
    "peminjaman_gedung": (
        "Surat resmi permohonan peminjaman gedung/aula/ruangan kampus atau sekolah. "
        "Jelaskan kebutuhan fasilitas, jadwal (termasuk setup dan bongkar), jumlah peserta, "
        "dan komitmen menjaga kebersihan serta ketertiban."
    ),
    "undangan_pemateri": (
        "Surat resmi undangan kepada pemateri/narasumber/juri. Jelaskan latar belakang acara, "
        "peran yang diminta, waktu dan tempat, serta informasi kontak panitia untuk konfirmasi."
    ),
    "sponsorship": (
        "Surat resmi permohonan sponsorship/dukungan dana. Jelaskan gambaran acara, target peserta, "
        "bentuk dukungan yang diharapkan, benefit bagi sponsor, dan kontak panitia."
    ),
    "lpj": (
        "Ringkasan Laporan Pertanggungjawaban (LPJ) kegiatan dengan struktur: Pendahuluan, "
        "Pelaksanaan Kegiatan, Realisasi Anggaran (bandingkan dengan pagu/RAB, hitung sisa atau "
        "kekurangan), Evaluasi (kendala dan keberhasilan), Penutup, serta daftar lampiran yang disarankan."
    ),
}

app = Flask(__name__)
app.json.sort_keys = False


# --------------------------------------------------------------------------
# Database (SQLite)
# --------------------------------------------------------------------------
def now_str():
    return datetime.now(WITA).strftime("%Y-%m-%d %H:%M:%S")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Membuat 3 tabel secara otomatis jika belum ada."""
    with closing(get_db()) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS proposals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nama_kegiatan TEXT NOT NULL,
                organisasi TEXT NOT NULL,
                lokasi TEXT NOT NULL,
                peserta INTEGER NOT NULL,
                durasi INTEGER NOT NULL,
                total_pagu INTEGER NOT NULL,
                output_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS budget_audits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nama_kegiatan TEXT NOT NULL,
                input_anggaran TEXT NOT NULL,
                skor_kelayakan INTEGER NOT NULL,
                output_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                jenis TEXT NOT NULL,
                judul TEXT NOT NULL,
                nama_kegiatan TEXT NOT NULL,
                isi_dokumen TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        conn.commit()


# --------------------------------------------------------------------------
# Integrasi Gemini API (REST via requests)
# --------------------------------------------------------------------------
class AIError(Exception):
    """Error yang aman ditampilkan ke pengguna."""

    def __init__(self, message, status=502):
        super().__init__(message)
        self.message = message
        self.status = status


def parse_json_text(text):
    """Mengubah teks balasan model menjadi objek JSON (toleran terhadap code fence)."""
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                pass
    raise ValueError("Format JSON dari AI tidak dapat dibaca.")


def _gemini_request(prompt, system_instruction, max_tokens):
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key or api_key == "your_gemini_api_key_here":
        raise AIError(
            "GEMINI_API_KEY belum dikonfigurasi di server. Salin .env.example menjadi .env "
            "lalu isi API Key Gemini Anda.",
            503,
        )

    payload = {
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.4,
            "maxOutputTokens": max_tokens,
        },
    }

    # Prioritaskan varian Flash-Lite yang cepat dan bebas antrean 503
    candidate_models = [
        "gemini-3.1-flash-lite",
        "gemini-3.5-flash",
        "gemini-3.8-flash-lite",
        "gemini-3.8-flash",
    ]

    resp = None
    last_error_msg = ""

    for model_name in candidate_models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
        headers = {
            "Content-Type": "application/json",
            "X-goog-api-key": api_key,
        }

        try:
            resp = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=GEMINI_TIMEOUT,
            )

            # Jika berhasil (HTTP 200), langsung gunakan hasilnya
            if resp.status_code == 200:
                break

            # Catat pesan error jika ada
            try:
                err_data = resp.json()
                last_error_msg = err_data.get("error", {}).get("message", "")
            except Exception:
                last_error_msg = resp.text

            # Jika model sibuk (503/504), batas sesaat (429), atau not found (404), langsung coba model berikutnya
            if resp.status_code in (404, 429, 500, 502, 503, 504):
                time.sleep(0.5)
                continue
            else:
                break

        except requests.exceptions.RequestException:
            continue

        if resp is not None and resp.status_code == 200:
            break

    if resp is None or resp.status_code != 200:
        if resp and resp.status_code == 429:
            raise AIError("Kuota permintaan Gemini API habis. Tunggu sebentar lalu coba lagi.", 429)
        if resp and resp.status_code in (400, 401, 403):
            raise AIError(f"Permintaan ditolak ({resp.status_code}): {last_error_msg or 'periksa API Key Anda.'}", 502)
        raise AIError(f"Layanan AI sedang sibuk (kode {resp.status_code if resp else 504}): {last_error_msg or 'Silakan klik tombol sekali lagi.'}", 502)

    try:
        data = resp.json()
    except ValueError:
        raise AIError("Respons layanan AI tidak valid.", 502)

    feedback = data.get("promptFeedback", {})
    if feedback.get("blockReason"):
        raise AIError("Permintaan diblokir oleh filter keamanan AI. Ubah isi input Anda.", 422)

    candidates = data.get("candidates") or []
    if not candidates:
        raise AIError("AI tidak menghasilkan jawaban. Silakan coba lagi.", 502)

    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts)
    if not text.strip():
        raise AIError("AI mengembalikan jawaban kosong. Silakan coba lagi.", 502)
    return text


def call_gemini_json(prompt, system_instruction, max_tokens=8192):
    """Memanggil Gemini dan mengembalikan dict. Mengulang 1x jika JSON gagal di-parse."""
    last_error = None
    for _ in range(2):
        text = _gemini_request(prompt, system_instruction, max_tokens)
        try:
            result = parse_json_text(text)
            if isinstance(result, dict):
                return result
            last_error = ValueError("Struktur JSON bukan objek.")
        except ValueError as exc:
            last_error = exc
    raise AIError("AI mengembalikan format yang tidak valid setelah 2 percobaan (%s). Silakan coba lagi." % last_error, 502)


SYSTEM_INSTRUCTION = (
    "Kamu adalah konsultan administrasi kegiatan kemahasiswaan dan kesiswaan yang berpengalaman di Bali, "
    "Indonesia. Kamu memahami konteks lokal: banjar/desa adat, Tri Hita Karana, upacara dan banten, hari raya "
    "(Nyepi, Galungan, Kuningan, Saraswati), pecalang, serta kisaran harga barang dan jasa di Bali. "
    "Gunakan Bahasa Indonesia formal dan jelas. Semua nominal dalam Rupiah (bilangan bulat tanpa pemisah). "
    "Jawab HANYA dengan satu objek JSON valid sesuai skema yang diminta, tanpa teks lain dan tanpa markdown."
)


# --------------------------------------------------------------------------
# Utilitas
# --------------------------------------------------------------------------
def to_int(value, default=0):
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return int(round(value))
    digits = re.sub(r"[^\d-]", "", str(value or ""))
    try:
        return int(digits) if digits not in ("", "-") else default
    except ValueError:
        return default


def to_text(value, default=""):
    text = str(value).strip() if value is not None else ""
    return text or default


def to_text_list(value):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [to_text(v) for v in value if to_text(v)]


def normalize_rab(raw_items, pagu):
    """
    Membersihkan item RAB dari AI dan MEMASTIKAN total persis sama dengan pagu.
    Matematika dilakukan di Python (bukan oleh AI) agar presisi.
    """
    items = []
    for it in raw_items if isinstance(raw_items, list) else []:
        if not isinstance(it, dict):
            continue
        qty = max(to_int(it.get("kuantitas"), 1), 1)
        price = max(to_int(it.get("harga_satuan"), 0), 0)
        if price == 0:
            total = to_int(it.get("total_harga"), 0)
            price = total // qty if total > 0 else 0
        if price == 0:
            continue
        items.append(
            {
                "divisi": to_text(it.get("divisi"), "Umum"),
                "nama_item": to_text(it.get("nama_item"), "Item tanpa nama"),
                "kuantitas": qty,
                "satuan": to_text(it.get("satuan"), "paket"),
                "harga_satuan": price,
                "total_harga": qty * price,
            }
        )

    if not items:
        raise AIError("AI tidak menghasilkan item RAB yang valid. Silakan coba lagi.", 502)

    def recompute():
        for i in items:
            i["total_harga"] = i["kuantitas"] * i["harga_satuan"]
        return sum(i["total_harga"] for i in items)

    total = recompute()
    adjusted = False

    # Jika terlalu jauh dari pagu, skala ulang harga satuan (target 97% pagu, sisanya dana tak terduga).
    if total > pagu or total < pagu * 0.92:
        factor = (pagu * 0.97) / total
        for i in items:
            i["harga_satuan"] = max(int(round(i["harga_satuan"] * factor / 500.0)) * 500, 500)
        total = recompute()
        adjusted = True

    diff = pagu - total
    if diff < 0:  # pembulatan membuat sedikit melebihi pagu
        big = max(items, key=lambda i: i["total_harga"])
        cut = -(-(-diff) // big["kuantitas"])  # pembulatan ke atas
        big["harga_satuan"] = max(big["harga_satuan"] - cut, 500)
        total = recompute()
        diff = pagu - total
        adjusted = True

    if diff > 0:
        cont = next(
            (i for i in items if "tak terduga" in i["nama_item"].lower() and i["kuantitas"] == 1),
            None,
        )
        if cont:
            cont["harga_satuan"] += diff
        else:
            items.append(
                {
                    "divisi": "Umum",
                    "nama_item": "Dana Tak Terduga & Penyesuaian",
                    "kuantitas": 1,
                    "satuan": "paket",
                    "harga_satuan": diff,
                    "total_harga": diff,
                }
            )
            adjusted = True
        total = recompute()

    return items, total, adjusted


def json_error(message, status):
    return jsonify({"error": message}), status


def load_json_column(raw):
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


# --------------------------------------------------------------------------
# Prompt builders
# --------------------------------------------------------------------------
PROPOSAL_SCHEMA = """{
  "ringkasan_eksekutif": "string, 1-2 paragraf ringkas",
  "latar_belakang": "string, 2-3 paragraf dengan konteks Bali yang relevan (budaya, pendidikan, pariwisata, komunitas)",
  "tujuan": ["string", "... 3-5 tujuan SMART"],
  "rundown": [
    {"hari": 1, "waktu": "08.00 - 09.00 WITA", "kegiatan": "string", "keterangan": "string singkat"}
  ],
  "rab": [
    {"divisi": "string", "nama_item": "string", "kuantitas": 1, "satuan": "string",
     "harga_satuan": 0, "total_harga": 0}
  ],
  "catatan_kontekstual_bali": "string, 2-4 kalimat tentang perizinan banjar/desa adat, kalender hari raya, dan etika setempat"
}"""

AUDIT_SCHEMA = """{
  "skor_kelayakan": 0,
  "status_kelayakan": "salah satu: Sangat Layak | Layak | Layak dengan Perbaikan | Perlu Revisi Besar | Tidak Layak",
  "ringkasan": "string, 2-3 kalimat kesimpulan audit",
  "total_terhitung": 0,
  "analisis_efisiensi": [
    {"pos": "string", "estimasi_nominal": 0, "penilaian": "salah satu: Efisien | Wajar | Boros | Terlalu Rendah", "catatan": "string"}
  ],
  "item_terlewat": [
    {"item": "string", "urgensi": "salah satu: Tinggi | Sedang | Rendah", "alasan": "string", "estimasi_biaya": 0}
  ],
  "rekomendasi": [
    {"prioritas": 1, "saran": "string", "dampak": "string singkat"}
  ]
}"""

DOC_SCHEMA = """{
  "judul": "string, judul dokumen",
  "isi": "string, seluruh teks dokumen lengkap; gunakan karakter \\n untuk baris baru"
}"""


def build_proposal_prompt(d):
    return (
        "Susun proposal kegiatan beserta Rencana Anggaran Biaya (RAB) untuk data berikut:\n"
        "- Nama kegiatan: %(nama_kegiatan)s\n"
        "- Kategori organisasi: %(organisasi)s\n"
        "- Lokasi di Bali: %(lokasi)s\n"
        "- Jumlah peserta: %(peserta)d orang\n"
        "- Durasi: %(durasi)d hari\n"
        "- Total pagu anggaran: Rp %(total_pagu)d\n\n"
        "Aturan:\n"
        "1. Rundown mencakup seluruh hari (1 sampai %(durasi)d) dengan waktu dalam WITA.\n"
        "2. RAB terdiri dari 18-30 item yang dikelompokkan dalam divisi (misalnya Acara, Konsumsi, "
        "Perlengkapan & Dekorasi, Publikasi & Dokumentasi, Humas & Perizinan, Kesehatan & Keamanan, Umum).\n"
        "3. Wajib ada item realistis untuk: perizinan/punia ke banjar atau pihak tempat, konsumsi, P3K, "
        "dokumentasi, keamanan (pecalang/panitia), dan dana tak terduga sekitar 3-5 persen.\n"
        "4. Harga satuan realistis untuk Bali tahun 2026. total_harga = kuantitas x harga_satuan.\n"
        "5. Jumlah seluruh total_harga RAB harus sama dengan pagu Rp %(total_pagu)d.\n\n"
        "Skema JSON yang harus dikembalikan:\n" % d
    ) + PROPOSAL_SCHEMA


def build_audit_prompt(d):
    return (
        "Audit rencana anggaran kegiatan berikut secara kritis namun konstruktif.\n"
        "- Nama kegiatan: %s\n"
        "- Deskripsi singkat acara: %s\n"
        "- Rincian anggaran dari pengguna:\n\"\"\"\n%s\n\"\"\"\n\n"
        "Aturan:\n"
        "1. skor_kelayakan adalah bilangan bulat 0-100 (kelengkapan pos, kewajaran harga di Bali, "
        "keseimbangan alokasi, dan kesesuaian dengan skala acara).\n"
        "2. Hitung total_terhitung dari rincian yang diberikan.\n"
        "3. Deteksi item krusial yang terlewat, misalnya izin tempat/banjar, konsumsi dan banten pejati/"
        "persembahyangan bila relevan, P3K, dokumentasi, keamanan, asuransi, serta dana tak terduga.\n"
        "4. Beri 3-6 rekomendasi optimasi yang konkret dan berurutan berdasarkan prioritas.\n\n"
        "Skema JSON yang harus dikembalikan:\n" % (d["nama_kegiatan"], d["deskripsi"], d["rincian"])
    ) + AUDIT_SCHEMA


def build_doc_prompt(d):
    fields = [
        ("Jenis dokumen", DOC_TYPES[d["jenis"]]),
        ("Nama kegiatan", d["nama_kegiatan"]),
        ("Nama organisasi pengirim", d["organisasi"]),
        ("Penerima/pihak tujuan", d["penerima"]),
        ("Tanggal/waktu kegiatan", d["tanggal"]),
        ("Lokasi kegiatan", d["lokasi"]),
        ("Detail tambahan / data realisasi", d["detail"]),
    ]
    data_block = "\n".join("- %s: %s" % (k, v or "(tidak diisi)") for k, v in fields)
    return (
        "Buat dokumen administrasi organisasi berikut dalam Bahasa Indonesia formal.\n"
        + data_block
        + "\n\nPanduan jenis dokumen: "
        + DOC_GUIDANCE[d["jenis"]]
        + "\n\nAturan:\n"
        "1. Untuk surat: sertakan tempat dan tanggal (Bali), nomor, lampiran, perihal, salam pembuka, "
        "isi, penutup, serta blok tanda tangan Ketua dan Sekretaris.\n"
        "2. Gunakan placeholder dalam kurung siku, contoh [Nomor Surat] atau [Nama Ketua], untuk data yang tidak diberikan. "
        "Jangan mengarang data penting seperti nomor telepon, nama orang, atau nominal yang tidak tersedia.\n"
        "3. Jangan gunakan markdown. Gunakan baris kosong antar paragraf.\n\n"
        "Skema JSON yang harus dikembalikan:\n"
    ) + DOC_SCHEMA


# --------------------------------------------------------------------------
# Rute halaman & status
# --------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    key = os.getenv("GEMINI_API_KEY", "").strip()
    configured = bool(key) and key != "your_gemini_api_key_here"
    return jsonify({"ai_configured": configured, "model": GEMINI_MODEL})


# --------------------------------------------------------------------------
# 1. Smart Proposal & RAB Architect
# --------------------------------------------------------------------------
@app.route("/api/generate-proposal", methods=["POST"])
def generate_proposal():
    body = request.get_json(silent=True) or {}
    data = {
        "nama_kegiatan": to_text(body.get("nama_kegiatan"))[:150],
        "organisasi": to_text(body.get("organisasi")),
        "lokasi": to_text(body.get("lokasi"))[:100],
        "peserta": to_int(body.get("peserta")),
        "durasi": to_int(body.get("durasi")),
        "total_pagu": to_int(body.get("total_pagu")),
    }

    if not data["nama_kegiatan"]:
        return json_error("Nama kegiatan wajib diisi.", 400)
    if data["organisasi"] not in KATEGORI_ORGANISASI:
        return json_error("Kategori organisasi tidak valid.", 400)
    if not data["lokasi"]:
        return json_error("Lokasi di Bali wajib diisi.", 400)
    if not 1 <= data["peserta"] <= 100000:
        return json_error("Jumlah peserta harus antara 1 dan 100.000.", 400)
    if not 1 <= data["durasi"] <= 14:
        return json_error("Durasi harus antara 1 dan 14 hari.", 400)
    if data["total_pagu"] < 100000:
        return json_error("Total pagu anggaran minimal Rp 100.000.", 400)

    try:
        result = call_gemini_json(build_proposal_prompt(data), SYSTEM_INSTRUCTION)

        rab, total_rab, adjusted = normalize_rab(result.get("rab"), data["total_pagu"])
        rundown = []
        for r in result.get("rundown") if isinstance(result.get("rundown"), list) else []:
            if isinstance(r, dict):
                rundown.append(
                    {
                        "hari": max(to_int(r.get("hari"), 1), 1),
                        "waktu": to_text(r.get("waktu"), "-"),
                        "kegiatan": to_text(r.get("kegiatan"), "-"),
                        "keterangan": to_text(r.get("keterangan")),
                    }
                )

        clean = {
            "ringkasan_eksekutif": to_text(result.get("ringkasan_eksekutif"), "-"),
            "latar_belakang": to_text(result.get("latar_belakang"), "-"),
            "tujuan": to_text_list(result.get("tujuan")),
            "rundown": rundown,
            "rab": rab,
            "total_rab": total_rab,
            "catatan_kontekstual_bali": to_text(result.get("catatan_kontekstual_bali")),
            "catatan_penyesuaian": (
                "Harga satuan disesuaikan otomatis oleh sistem agar total RAB tepat sama dengan pagu."
                if adjusted
                else ""
            ),
        }

        created = now_str()
        with closing(get_db()) as conn:
            cur = conn.execute(
                "INSERT INTO proposals (nama_kegiatan, organisasi, lokasi, peserta, durasi, total_pagu, "
                "output_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    data["nama_kegiatan"],
                    data["organisasi"],
                    data["lokasi"],
                    data["peserta"],
                    data["durasi"],
                    data["total_pagu"],
                    json.dumps(clean, ensure_ascii=False),
                    created,
                ),
            )
            conn.commit()
            new_id = cur.lastrowid

        record = dict(data, id=new_id, created_at=created, result=clean)
        return jsonify({"ok": True, "record": record})
    except AIError as exc:
        return json_error(exc.message, exc.status)
    except Exception:
        app.logger.exception("Gagal membuat proposal")
        return json_error("Terjadi kesalahan internal saat membuat proposal.", 500)


# --------------------------------------------------------------------------
# 2. AI Budget & Feasibility Auditor
# --------------------------------------------------------------------------
@app.route("/api/audit-budget", methods=["POST"])
def audit_budget():
    body = request.get_json(silent=True) or {}
    data = {
        "nama_kegiatan": to_text(body.get("nama_kegiatan"))[:150],
        "deskripsi": to_text(body.get("deskripsi"))[:2000],
        "rincian": to_text(body.get("rincian"))[:8000],
    }

    if not data["nama_kegiatan"]:
        return json_error("Nama kegiatan wajib diisi.", 400)
    if len(data["rincian"]) < 20:
        return json_error("Rincian anggaran terlalu singkat untuk diaudit.", 400)

    try:
        result = call_gemini_json(build_audit_prompt(data), SYSTEM_INSTRUCTION)

        skor = min(max(to_int(result.get("skor_kelayakan"), 0), 0), 100)

        efisiensi = []
        for e in result.get("analisis_efisiensi") if isinstance(result.get("analisis_efisiensi"), list) else []:
            if isinstance(e, dict):
                efisiensi.append(
                    {
                        "pos": to_text(e.get("pos"), "-"),
                        "estimasi_nominal": to_int(e.get("estimasi_nominal")),
                        "penilaian": to_text(e.get("penilaian"), "Wajar"),
                        "catatan": to_text(e.get("catatan")),
                    }
                )

        terlewat = []
        for t in result.get("item_terlewat") if isinstance(result.get("item_terlewat"), list) else []:
            if isinstance(t, dict):
                terlewat.append(
                    {
                        "item": to_text(t.get("item"), "-"),
                        "urgensi": to_text(t.get("urgensi"), "Sedang"),
                        "alasan": to_text(t.get("alasan")),
                        "estimasi_biaya": to_int(t.get("estimasi_biaya")),
                    }
                )

        rekomendasi = []
        for idx, r in enumerate(result.get("rekomendasi") if isinstance(result.get("rekomendasi"), list) else [], 1):
            if isinstance(r, dict):
                rekomendasi.append(
                    {
                        "prioritas": to_int(r.get("prioritas"), idx),
                        "saran": to_text(r.get("saran"), "-"),
                        "dampak": to_text(r.get("dampak")),
                    }
                )

        clean = {
            "skor_kelayakan": skor,
            "status_kelayakan": to_text(result.get("status_kelayakan"), "Layak dengan Perbaikan"),
            "ringkasan": to_text(result.get("ringkasan"), "-"),
            "total_terhitung": to_int(result.get("total_terhitung")),
            "analisis_efisiensi": efisiensi,
            "item_terlewat": terlewat,
            "rekomendasi": rekomendasi,
        }

        created = now_str()
        stored_input = "Deskripsi: %s\n\n%s" % (data["deskripsi"] or "-", data["rincian"])
        with closing(get_db()) as conn:
            cur = conn.execute(
                "INSERT INTO budget_audits (nama_kegiatan, input_anggaran, skor_kelayakan, output_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (data["nama_kegiatan"], stored_input, skor, json.dumps(clean, ensure_ascii=False), created),
            )
            conn.commit()
            new_id = cur.lastrowid

        record = {
            "id": new_id,
            "nama_kegiatan": data["nama_kegiatan"],
            "input_anggaran": stored_input,
            "skor_kelayakan": skor,
            "created_at": created,
            "result": clean,
        }
        return jsonify({"ok": True, "record": record})
    except AIError as exc:
        return json_error(exc.message, exc.status)
    except Exception:
        app.logger.exception("Gagal mengaudit anggaran")
        return json_error("Terjadi kesalahan internal saat mengaudit anggaran.", 500)


# --------------------------------------------------------------------------
# 3. Smart Correspondence & LPJ Generator
# --------------------------------------------------------------------------
@app.route("/api/generate-doc", methods=["POST"])
def generate_doc():
    body = request.get_json(silent=True) or {}
    data = {
        "jenis": to_text(body.get("jenis")),
        "nama_kegiatan": to_text(body.get("nama_kegiatan"))[:150],
        "organisasi": to_text(body.get("organisasi"))[:150],
        "penerima": to_text(body.get("penerima"))[:200],
        "tanggal": to_text(body.get("tanggal"))[:100],
        "lokasi": to_text(body.get("lokasi"))[:150],
        "detail": to_text(body.get("detail"))[:4000],
    }

    if data["jenis"] not in DOC_TYPES:
        return json_error("Jenis dokumen tidak valid.", 400)
    if not data["nama_kegiatan"]:
        return json_error("Nama kegiatan wajib diisi.", 400)

    try:
        result = call_gemini_json(build_doc_prompt(data), SYSTEM_INSTRUCTION)
        judul = to_text(result.get("judul"), DOC_TYPES[data["jenis"]])
        isi = to_text(result.get("isi"))
        if not isi:
            raise AIError("AI tidak menghasilkan isi dokumen. Silakan coba lagi.", 502)

        created = now_str()
        with closing(get_db()) as conn:
            cur = conn.execute(
                "INSERT INTO documents (jenis, judul, nama_kegiatan, isi_dokumen, created_at) VALUES (?, ?, ?, ?, ?)",
                (data["jenis"], judul, data["nama_kegiatan"], isi, created),
            )
            conn.commit()
            new_id = cur.lastrowid

        record = {
            "id": new_id,
            "jenis": data["jenis"],
            "jenis_label": DOC_TYPES[data["jenis"]],
            "judul": judul,
            "nama_kegiatan": data["nama_kegiatan"],
            "isi_dokumen": isi,
            "created_at": created,
        }
        return jsonify({"ok": True, "record": record})
    except AIError as exc:
        return json_error(exc.message, exc.status)
    except Exception:
        app.logger.exception("Gagal membuat dokumen")
        return json_error("Terjadi kesalahan internal saat membuat dokumen.", 500)


# --------------------------------------------------------------------------
# 4. Dashboard Arsip & Analitik
# --------------------------------------------------------------------------
TABLES = {
    "proposal": "proposals",
    "proposals": "proposals",
    "audit": "budget_audits",
    "audits": "budget_audits",
    "document": "documents",
    "documents": "documents",
}


def shape_record(table, row):
    if table == "proposals":
        return {
            "id": row["id"],
            "nama_kegiatan": row["nama_kegiatan"],
            "organisasi": row["organisasi"],
            "lokasi": row["lokasi"],
            "peserta": row["peserta"],
            "durasi": row["durasi"],
            "total_pagu": row["total_pagu"],
            "created_at": row["created_at"],
            "result": load_json_column(row["output_json"]),
        }
    if table == "budget_audits":
        return {
            "id": row["id"],
            "nama_kegiatan": row["nama_kegiatan"],
            "input_anggaran": row["input_anggaran"],
            "skor_kelayakan": row["skor_kelayakan"],
            "created_at": row["created_at"],
            "result": load_json_column(row["output_json"]),
        }
    return {
        "id": row["id"],
        "jenis": row["jenis"],
        "jenis_label": DOC_TYPES.get(row["jenis"], row["jenis"]),
        "judul": row["judul"],
        "nama_kegiatan": row["nama_kegiatan"],
        "isi_dokumen": row["isi_dokumen"],
        "created_at": row["created_at"],
    }


@app.route("/api/history", methods=["GET"])
def get_history():
    try:
        with closing(get_db()) as conn:
            proposals = [
                dict(r)
                for r in conn.execute(
                    "SELECT id, nama_kegiatan, organisasi, lokasi, peserta, durasi, total_pagu, created_at "
                    "FROM proposals ORDER BY id DESC"
                )
            ]
            audits = []
            for r in conn.execute(
                "SELECT id, nama_kegiatan, skor_kelayakan, output_json, created_at FROM budget_audits ORDER BY id DESC"
            ):
                status = load_json_column(r["output_json"]).get("status_kelayakan", "-")
                audits.append(
                    {
                        "id": r["id"],
                        "nama_kegiatan": r["nama_kegiatan"],
                        "skor_kelayakan": r["skor_kelayakan"],
                        "status_kelayakan": status,
                        "created_at": r["created_at"],
                    }
                )
            documents = []
            for r in conn.execute(
                "SELECT id, jenis, judul, nama_kegiatan, created_at FROM documents ORDER BY id DESC"
            ):
                documents.append(
                    {
                        "id": r["id"],
                        "jenis": r["jenis"],
                        "jenis_label": DOC_TYPES.get(r["jenis"], r["jenis"]),
                        "judul": r["judul"],
                        "nama_kegiatan": r["nama_kegiatan"],
                        "created_at": r["created_at"],
                    }
                )
            by_org = [
                {"organisasi": r["organisasi"], "jumlah": r["jumlah"]}
                for r in conn.execute(
                    "SELECT organisasi, COUNT(*) AS jumlah FROM proposals GROUP BY organisasi ORDER BY jumlah DESC"
                )
            ]
            total_pagu = conn.execute("SELECT COALESCE(SUM(total_pagu), 0) FROM proposals").fetchone()[0]
            avg_skor = conn.execute("SELECT AVG(skor_kelayakan) FROM budget_audits").fetchone()[0]

        stats = {
            "total_proposals": len(proposals),
            "total_audits": len(audits),
            "total_documents": len(documents),
            "total_pagu": total_pagu,
            "avg_skor": round(avg_skor) if avg_skor is not None else None,
            "by_organisasi": by_org,
        }
        return jsonify({"ok": True, "proposals": proposals, "audits": audits, "documents": documents, "stats": stats})
    except Exception:
        app.logger.exception("Gagal memuat riwayat")
        return json_error("Gagal memuat arsip dari database.", 500)


@app.route("/api/history/<item_type>/<int:item_id>", methods=["GET"])
def get_history_item(item_type, item_id):
    table = TABLES.get(item_type)
    if not table:
        return json_error("Jenis arsip tidak dikenal.", 400)
    try:
        with closing(get_db()) as conn:
            row = conn.execute("SELECT * FROM %s WHERE id = ?" % table, (item_id,)).fetchone()
        if row is None:
            return json_error("Arsip tidak ditemukan.", 404)
        return jsonify({"ok": True, "record": shape_record(table, row)})
    except Exception:
        app.logger.exception("Gagal memuat detail arsip")
        return json_error("Gagal memuat detail arsip.", 500)


@app.route("/api/history/<item_type>/<int:item_id>", methods=["DELETE"])
def delete_history_item(item_type, item_id):
    table = TABLES.get(item_type)
    if not table:
        return json_error("Jenis arsip tidak dikenal.", 400)
    try:
        with closing(get_db()) as conn:
            cur = conn.execute("DELETE FROM %s WHERE id = ?" % table, (item_id,))
            conn.commit()
        if cur.rowcount == 0:
            return json_error("Arsip tidak ditemukan.", 404)
        return jsonify({"ok": True})
    except Exception:
        app.logger.exception("Gagal menghapus arsip")
        return json_error("Gagal menghapus arsip.", 500)


# --------------------------------------------------------------------------
# Error handler global (selalu JSON untuk /api/*)
# --------------------------------------------------------------------------
@app.errorhandler(404)
def handle_404(_err):
    if request.path.startswith("/api/"):
        return json_error("Endpoint tidak ditemukan.", 404)
    return "Halaman tidak ditemukan.", 404


@app.errorhandler(405)
def handle_405(_err):
    return json_error("Metode HTTP tidak diizinkan untuk endpoint ini.", 405)


@app.errorhandler(500)
def handle_500(_err):
    return json_error("Terjadi kesalahan internal pada server.", 500)


# Tabel dibuat otomatis saat aplikasi dimuat (juga saat dijalankan via gunicorn).
init_db()

if __name__ == "__main__":
    debug = os.getenv("FLASK_ENV", "").lower() == "development"
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=debug)
