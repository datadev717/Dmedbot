#!/usr/bin/env python3
"""
Tibbiy Bot v2 — Bemor kuzatuv tizimi
======================================
• Shifokor bemor ma'lumotlarini bosqichma-bosqich kiritadi
• 80 kun (test: 1 soat) o'tgach inline tugmali eslatma yuboriladi
• "Bog'landim" → yakunlandi | "Bog'lana olmadim" → 5 kun (test: 5 daqiqa) keyin qayta eslatma
• Multi-shifokor: har birining bemorlar alohida
• Admin: barcha shifokor va bemorlarni ko'radi, ruxsat beradi
"""

import sqlite3, json, time, threading, logging, requests
from datetime import datetime, timedelta

# ══════════════════════════════════════════
#  SOZLAMALAR  —  faqat shu yerni o'zgartiring
# ══════════════════════════════════════════
BOT_TOKEN = "YANGI_TOKENINGIZNI_SHU_YERGA_KIRITING"
ADMIN_ID  = 123456789          # Sizning Telegram ID (t.me/userinfobot dan oling)
API_URL   = f"https://api.telegram.org/bot{BOT_TOKEN}"

# TEST_MODE = True  → 80 kun = 1 soat (3600 s), qayta eslatma = 5 daqiqa (300 s)
# TEST_MODE = False → haqiqiy vaqt
TEST_MODE             = True
FIRST_NOTIFY_SECONDS  = 3_600       if TEST_MODE else 80 * 24 * 3600   # 1 soat / 80 kun
RETRY_NOTIFY_SECONDS  = 300         if TEST_MODE else  5 * 24 * 3600   #  5 daqiqa / 5 kun

DB_PATH = "medical_bot.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ══════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS doctors (
                user_id   INTEGER PRIMARY KEY,
                username  TEXT DEFAULT '',
                full_name TEXT DEFAULT '',
                approved  INTEGER DEFAULT 0,
                joined_at TEXT
            );

            CREATE TABLE IF NOT EXISTS patients (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                doctor_id   INTEGER NOT NULL,
                last_name   TEXT DEFAULT '',
                first_name  TEXT DEFAULT '',
                birth_year  INTEGER,
                diagnosis   TEXT DEFAULT '',
                address     TEXT DEFAULT '',
                phone       TEXT DEFAULT '',
                added_at    TEXT,
                notify_at   TEXT,
                status      TEXT DEFAULT 'pending',
                -- pending → eslatma yuborilmagan
                -- notified → eslatma yuborildi, javob kutilmoqda
                -- contacted → bog'landi (yakunlandi)
                -- retry → bog'lana olmadi, qayta eslatma rejalashtirildi
                retry_at    TEXT,
                retry_count INTEGER DEFAULT 0,
                FOREIGN KEY(doctor_id) REFERENCES doctors(user_id)
            );

            CREATE TABLE IF NOT EXISTS sessions (
                user_id INTEGER PRIMARY KEY,
                state   TEXT,
                data    TEXT
            );
        """)
    log.info("DB tayyor.")

def conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

# ══════════════════════════════════════════
#  TELEGRAM YORDAMCHILARI
# ══════════════════════════════════════════
def api(method, **kwargs):
    try:
        r = requests.post(f"{API_URL}/{method}", json=kwargs, timeout=10)
        return r.json()
    except Exception as e:
        log.error(f"API {method}: {e}")
        return {}

def send(chat_id, text, reply_markup=None, parse_mode="HTML"):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    return api("sendMessage", **payload)

def edit_reply_markup(chat_id, message_id, reply_markup=None):
    payload = {"chat_id": chat_id, "message_id": message_id}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    else:
        payload["reply_markup"] = json.dumps({"inline_keyboard": []})
    return api("editMessageReplyMarkup", **payload)

def answer_cb(cb_id, text="✅"):
    api("answerCallbackQuery", callback_query_id=cb_id, text=text)

def get_updates(offset=0):
    try:
        r = requests.get(f"{API_URL}/getUpdates",
                         params={"offset": offset, "timeout": 30}, timeout=35)
        return r.json().get("result", [])
    except Exception as e:
        log.error(f"getUpdates: {e}")
        time.sleep(5)
        return []

# ══════════════════════════════════════════
#  SESSION
# ══════════════════════════════════════════
def get_sess(uid):
    with conn() as c:
        row = c.execute("SELECT state, data FROM sessions WHERE user_id=?", (uid,)).fetchone()
    return (row[0], json.loads(row[1])) if row else (None, {})

def set_sess(uid, state, data=None):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO sessions VALUES(?,?,?)",
                  (uid, state, json.dumps(data or {})))
        c.commit()

def del_sess(uid):
    with conn() as c:
        c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        c.commit()

# ══════════════════════════════════════════
#  SHIFOKOR
# ══════════════════════════════════════════
def get_doc(uid):
    with conn() as c:
        return c.execute("SELECT * FROM doctors WHERE user_id=?", (uid,)).fetchone()

def reg_doc(uid, uname, fname):
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO doctors VALUES(?,?,?,0,?)",
                  (uid, uname or "", fname, datetime.now().isoformat()))
        c.commit()

def approved(uid):
    if uid == ADMIN_ID: return True
    d = get_doc(uid)
    return bool(d and d[3] == 1)

def all_docs():
    with conn() as c:
        return c.execute("SELECT * FROM doctors ORDER BY joined_at DESC").fetchall()

# ══════════════════════════════════════════
#  BEMOR
# ══════════════════════════════════════════
def add_patient(doc_id, last_name, first_name, birth_year, diagnosis, address, phone):
    now = datetime.now()
    notify_at = (now + timedelta(seconds=FIRST_NOTIFY_SECONDS)).isoformat()
    with conn() as c:
        c.execute("""
            INSERT INTO patients
            (doctor_id,last_name,first_name,birth_year,diagnosis,address,phone,
             added_at,notify_at,status,retry_at,retry_count)
            VALUES(?,?,?,?,?,?,?,?,?,'pending',NULL,0)
        """, (doc_id, last_name, first_name, birth_year, diagnosis, address, phone,
              now.isoformat(), notify_at))
        c.commit()

def get_patients(doc_id):
    with conn() as c:
        return c.execute(
            "SELECT * FROM patients WHERE doctor_id=? ORDER BY added_at DESC", (doc_id,)
        ).fetchall()

def get_patient_by_id(pid):
    with conn() as c:
        return c.execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()

def all_patients_admin():
    with conn() as c:
        return c.execute("""
            SELECT p.*, d.full_name as dname FROM patients p
            JOIN doctors d ON p.doctor_id=d.user_id
            ORDER BY p.added_at DESC
        """).fetchall()

STATUS_LABELS = {
    "pending":   "⏳ Eslatma kutilmoqda",
    "notified":  "🔔 Eslatma yuborildi",
    "contacted": "✅ Bog'landi",
    "retry":     "🔄 Qayta eslatma rejalashtirildi",
}

def format_patient(p, show_doc=None):
    # p columns: 0=id,1=doctor_id,2=last_name,3=first_name,4=birth_year,
    #            5=diagnosis,6=address,7=phone,8=added_at,9=notify_at,
    #            10=status,11=retry_at,12=retry_count
    added   = p[8][:16].replace("T"," ") if p[8] else "-"
    notify  = p[9][:16].replace("T"," ") if p[9] else "-"
    status  = STATUS_LABELS.get(p[10], p[10])
    retry   = p[11][:16].replace("T"," ") if p[11] else "-"
    text = (
        f"👤 <b>{p[2]} {p[3]}</b>\n"
        f"🎂 Tug'ilgan yil: <b>{p[4]}</b>\n"
        f"🏥 Kasallik: {p[5]}\n"
        f"🏠 Manzil: {p[6]}\n"
        f"📞 Telefon: <b>{p[7]}</b>\n"
        f"📅 Kiritilgan: {added}\n"
        f"⏰ Eslatma: {notify}\n"
        f"📬 Holat: {status}"
    )
    if p[10] == "retry":
        text += f"\n🔁 Qayta eslatma: {retry}"
    if show_doc:
        text += f"\n🩺 Shifokor: {show_doc}"
    return text

# ══════════════════════════════════════════
#  KLAVIATURALAR
# ══════════════════════════════════════════
def menu_kb(uid):
    rows = [
        [{"text": "➕ Bemor qo'shish"}],
        [{"text": "📋 Mening bemorlarim"}],
    ]
    if uid == ADMIN_ID:
        rows += [
            [{"text": "👥 Shifokorlar ro'yxati"}],
            [{"text": "📊 Barcha bemorlar"}],
        ]
    return {"keyboard": rows, "resize_keyboard": True}

def cancel_kb():
    return {"keyboard": [[{"text": "❌ Bekor qilish"}]], "resize_keyboard": True}

def contact_inline_kb(patient_id):
    """Eslatma xabariga qo'shiladigan inline tugmalar"""
    return {"inline_keyboard": [[
        {"text": "✅ Bog'landim",      "callback_data": f"contacted:{patient_id}"},
        {"text": "❌ Bog'lana olmadim", "callback_data": f"retry:{patient_id}"},
    ]]}

# ══════════════════════════════════════════
#  BEMOR QO'SHISH OQIMI
# ══════════════════════════════════════════
STEPS = [
    ("last_name",   "👤 Bemor <b>familiyasini</b> kiriting:"),
    ("first_name",  "👤 Bemor <b>ismini</b> kiriting:"),
    ("birth_year",  "🎂 <b>Tug'ilgan yilini</b> kiriting (masalan: 1985):"),
    ("diagnosis",   "🏥 <b>Kasalligini</b> kiriting:"),
    ("address",     "🏠 <b>Yashash manzilini</b> kiriting:"),
    ("phone",       "📞 <b>Telefon raqamini</b> kiriting:"),
]
STEP_KEYS  = [s[0] for s in STEPS]
NEXT_STEP  = {STEP_KEYS[i]: STEP_KEYS[i+1] for i in range(len(STEP_KEYS)-1)}

def patient_flow(uid, text, state, data):
    current = state.replace("ap_", "")
    val = text.strip()

    # --- Validatsiya ---
    if current == "birth_year":
        try:
            y = int(val)
            assert 1900 < y <= datetime.now().year
        except:
            send(uid, "❌ Noto'g'ri yil! Masalan: <b>1985</b>. Qaytadan kiriting:", reply_markup=cancel_kb())
            return

    if current == "phone" and len(val) < 7:
        send(uid, "❌ Raqam noto'g'ri. Qaytadan kiriting:", reply_markup=cancel_kb())
        return

    data[current] = val

    if current in NEXT_STEP:
        nxt = NEXT_STEP[current]
        prompt = dict(STEPS)[nxt]
        set_sess(uid, f"ap_{nxt}", data)
        send(uid, prompt, reply_markup=cancel_kb())
    else:
        # Yakunlash — barcha maydonlar to'ldirildi
        _save_and_confirm(uid, data)

def _save_and_confirm(uid, data):
    add_patient(
        uid,
        data.get("last_name",""),
        data.get("first_name",""),
        int(data.get("birth_year", 0)),
        data.get("diagnosis",""),
        data.get("address",""),
        data.get("phone",""),
    )
    del_sess(uid)
    mode = "1 soatdan" if TEST_MODE else "80 kundan"
    preview = (
        f"✅ <b>Bemor muvaffaqiyatli qo'shildi!</b>\n\n"
        f"👤 {data.get('last_name','')} {data.get('first_name','')}\n"
        f"🎂 {data.get('birth_year','')}\n"
        f"🏥 {data.get('diagnosis','')}\n"
        f"🏠 {data.get('address','')}\n"
        f"📞 {data.get('phone','')}\n\n"
        f"⏰ <b>{mode} keyin</b> sizga eslatma yuboriladi."
    )
    send(uid, preview, reply_markup=menu_kb(uid))

# ══════════════════════════════════════════
#  START / REGISTER
# ══════════════════════════════════════════
def handle_start(uid, uname, fname):
    doc = get_doc(uid)
    if not doc:
        reg_doc(uid, uname, fname)
        if uid == ADMIN_ID:
            with conn() as c:
                c.execute("UPDATE doctors SET approved=1 WHERE user_id=?", (uid,))
                c.commit()
            mode = "TEST (1 soat / 5 daqiqa)" if TEST_MODE else "ISHCHI (80 kun / 5 kun)"
            send(uid, f"👨‍💻 <b>Admin sifatida xush kelibsiz!</b>\n\nBot rejimi: <b>{mode}</b>",
                 reply_markup=menu_kb(uid))
        else:
            send(uid,
                 f"👋 Salom, <b>{fname}</b>!\n\n"
                 "Tibbiy bot tizimiga xush kelibsiz.\n"
                 "⏳ Admin tasdig'ini kuting — tasdiqlangach xabar olasiz.")
            send(ADMIN_ID,
                 f"🔔 <b>Yangi shifokor!</b>\n\n"
                 f"👤 {fname}\n🆔 {uid}\n📎 @{uname or '-'}\n\n"
                 f"Tasdiqlash: /approve_{uid}\n"
                 f"Rad etish: /reject_{uid}")
    else:
        if approved(uid):
            send(uid, f"✅ Xush kelibsiz, <b>{fname}</b>!", reply_markup=menu_kb(uid))
        else:
            s = {0:"⏳ Kutmoqda", 2:"❌ Rad etilgan"}.get(doc[3], "Noma'lum")
            send(uid, f"Sizning so'rovingiz holati: <b>{s}</b>")

# ══════════════════════════════════════════
#  CALLBACK HANDLER (bog'landim / bog'lana olmadim)
# ══════════════════════════════════════════
def handle_callback(cb):
    uid     = cb["from"]["id"]
    cb_id   = cb["id"]
    data    = cb.get("data","")
    msg_id  = cb["message"]["message_id"]
    chat_id = cb["message"]["chat"]["id"]

    if not data or ":" not in data:
        answer_cb(cb_id, "Noma'lum buyruq")
        return

    action, pid_str = data.split(":", 1)
    pid = int(pid_str)
    patient = get_patient_by_id(pid)

    if not patient:
        answer_cb(cb_id, "Bemor topilmadi")
        return

    # Faqat o'sha shifokor yoki admin
    if uid != patient[1] and uid != ADMIN_ID:
        answer_cb(cb_id, "❌ Ruxsatingiz yo'q")
        return

    # Allaqachon yakunlangan bo'lsa
    if patient[10] == "contacted":
        answer_cb(cb_id, "✅ Bu bemor allaqachon bog'langan deb belgilangan")
        edit_reply_markup(chat_id, msg_id)
        return

    if action == "contacted":
        with conn() as c:
            c.execute("UPDATE patients SET status='contacted' WHERE id=?", (pid,))
            c.commit()
        answer_cb(cb_id, "✅ Qayd etildi!")
        edit_reply_markup(chat_id, msg_id)  # Tugmalarni olib tashlash
        p = get_patient_by_id(pid)
        send(chat_id,
             f"✅ <b>Bog'landi deb belgilandi</b>\n\n"
             f"👤 {p[2]} {p[3]} bilan bog'lanish tasdiqlandi.\n"
             f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        log.info(f"Contacted: patient {pid} by doctor {uid}")

    elif action == "retry":
        retry_at = (datetime.now() + timedelta(seconds=RETRY_NOTIFY_SECONDS)).isoformat()
        with conn() as c:
            c.execute("""UPDATE patients SET status='retry', retry_at=?,
                         retry_count=retry_count+1 WHERE id=?""", (retry_at, pid))
            c.commit()
        p = get_patient_by_id(pid)
        retry_label = "5 daqiqadan" if TEST_MODE else "5 kundan"
        answer_cb(cb_id, f"🔄 {retry_label} keyin qayta eslatiladi")
        edit_reply_markup(chat_id, msg_id)
        send(chat_id,
             f"🔄 <b>Qayta eslatma rejalashtirildi</b>\n\n"
             f"👤 {p[2]} {p[3]}\n"
             f"📞 {p[7]}\n\n"
             f"⏰ <b>{retry_label} keyin</b> yana eslatiladi.")
        log.info(f"Retry: patient {pid} by doctor {uid}, retry_at={retry_at}")

# ══════════════════════════════════════════
#  XABAR HANDLER
# ══════════════════════════════════════════
def handle_message(msg):
    uid   = msg["from"]["id"]
    uname = msg["from"].get("username","")
    fname = (msg["from"].get("first_name","")+" "+msg["from"].get("last_name","")).strip()
    text  = msg.get("text","").strip()

    state, sdata = get_sess(uid)

    # /start
    if text.startswith("/start"):
        del_sess(uid)
        handle_start(uid, uname, fname)
        return

    # Admin buyruqlari
    if uid == ADMIN_ID:
        if text.startswith("/approve_"):
            try:
                tid = int(text.split("_",1)[1])
                with conn() as c:
                    c.execute("UPDATE doctors SET approved=1 WHERE user_id=?", (tid,))
                    c.commit()
                d = get_doc(tid)
                send(uid, f"✅ {d[2] if d else tid} tasdiqlandi.")
                send(tid, "✅ <b>Tabriklaymiz!</b> Siz tizimga qo'shildingiz!\n\n/start bosing.",
                     reply_markup=menu_kb(tid))
            except Exception as e:
                send(uid, f"Xato: {e}")
            return

        if text.startswith("/reject_"):
            try:
                tid = int(text.split("_",1)[1])
                with conn() as c:
                    c.execute("UPDATE doctors SET approved=2 WHERE user_id=?", (tid,))
                    c.commit()
                d = get_doc(tid)
                send(uid, f"❌ {d[2] if d else tid} rad etildi.")
                send(tid, "❌ So'rovingiz rad etildi. Qo'shimcha ma'lumot uchun admin bilan bog'laning.")
            except Exception as e:
                send(uid, f"Xato: {e}")
            return

    # Ro'yxatdan o'tmagan
    if not get_doc(uid):
        send(uid, "Iltimos /start bosing.")
        return

    # Tasdiqlanmagan
    if not approved(uid):
        send(uid, "⏳ Hisobingiz hali tasdiqlanmagan. Admin xabar beradi.")
        return

    # Bekor qilish
    if text == "❌ Bekor qilish":
        del_sess(uid)
        send(uid, "Bekor qilindi.", reply_markup=menu_kb(uid))
        return

    # Bemor qo'shish oqimi
    if state and state.startswith("ap_"):
        patient_flow(uid, text, state, sdata)
        return

    # ── MENYU ──
    if text == "➕ Bemor qo'shish":
        set_sess(uid, "ap_last_name", {})
        send(uid, dict(STEPS)["last_name"], reply_markup=cancel_kb())

    elif text == "📋 Mening bemorlarim":
        pts = get_patients(uid)
        if not pts:
            send(uid, "📭 Sizda hali bemor yo'q.", reply_markup=menu_kb(uid))
        else:
            send(uid, f"📋 <b>Sizning bemorlaringiz — {len(pts)} nafar</b>",
                 reply_markup=menu_kb(uid))
            for p in pts:
                send(uid, format_patient(p))

    elif text == "👥 Shifokorlar ro'yxati" and uid == ADMIN_ID:
        docs = all_docs()
        if not docs:
            send(uid, "Hech kim ro'yxatdan o'tmagan.")
            return
        sm = {0:"⏳ Kutmoqda", 1:"✅ Tasdiqlangan", 2:"❌ Rad etilgan"}
        lines = [f"👥 <b>Shifokorlar — {len(docs)} nafar</b>"]
        for d in docs:
            uid2, uname2, fname2, appr, joined = d
            bcount = len(get_patients(uid2))
            line = (f"\n{sm.get(appr,'?')} <b>{fname2}</b> (@{uname2 or '-'})\n"
                    f"   🆔 {uid2} | 📅 {joined[:10]} | 👥 {bcount} bemor")
            if appr == 0:
                line += f"\n   👉 /approve_{uid2}  |  /reject_{uid2}"
            lines.append(line)
        send(uid, "\n".join(lines), reply_markup=menu_kb(uid))

    elif text == "📊 Barcha bemorlar" and uid == ADMIN_ID:
        pts = all_patients_admin()
        if not pts:
            send(uid, "📭 Hech qanday bemor yo'q.")
            return
        send(uid, f"📊 <b>Jami bemorlar: {len(pts)} nafar</b>", reply_markup=menu_kb(uid))
        for p in pts:
            dname = p[-1] if len(p) > 13 else "?"
            send(uid, format_patient(p, show_doc=dname))

    else:
        send(uid, "Iltimos, tugmalardan foydalaning. 👇", reply_markup=menu_kb(uid))

# ══════════════════════════════════════════
#  ESLATMA SERVISI (background thread)
# ══════════════════════════════════════════
def notify_worker():
    log.info("Eslatma xizmati ishga tushdi.")
    while True:
        try:
            now = datetime.now().isoformat()

            with conn() as c:
                # 1) Birinchi eslatma (pending → notified)
                pending = c.execute(
                    "SELECT id, doctor_id, last_name, first_name, diagnosis, phone "
                    "FROM patients WHERE status='pending' AND notify_at<=?", (now,)
                ).fetchall()

                # 2) Qayta eslatma (retry → notified)
                retries = c.execute(
                    "SELECT id, doctor_id, last_name, first_name, diagnosis, phone, retry_count "
                    "FROM patients WHERE status='retry' AND retry_at<=?", (now,)
                ).fetchall()

            mode_label = "1 SOAT (test)" if TEST_MODE else "80 KUN"

            for row in pending:
                pid, doc_id, lname, fname, diag, phone = row
                _send_notify(pid, doc_id, lname, fname, diag, phone, mode_label, is_retry=False)

            for row in retries:
                pid, doc_id, lname, fname, diag, phone, rcnt = row
                _send_notify(pid, doc_id, lname, fname, diag, phone,
                             f"QAYTA ESLATMA #{rcnt+1}", is_retry=True)

        except Exception as e:
            log.error(f"notify_worker: {e}")

        time.sleep(30)  # 30 sekunda

def _send_notify(pid, doc_id, lname, fname, diag, phone, label, is_retry):
    msg = (
        f"{'🔄' if is_retry else '🔔'} <b>ESLATMA — {label}</b>\n\n"
        f"👤 Bemor: <b>{lname} {fname}</b>\n"
        f"🏥 Kasallik: {diag}\n"
        f"📞 Telefon: <b>{phone}</b>\n\n"
        f"Iltimos, bemor bilan bog'laning va natijani belgilang:"
    )
    result = send(doc_id, msg, reply_markup=contact_inline_kb(pid))
    if result and result.get("ok"):
        with conn() as c:
            c.execute("UPDATE patients SET status='notified' WHERE id=?", (pid,))
            c.commit()
        log.info(f"Eslatma yuborildi → patient {pid} → doctor {doc_id} [{label}]")
    else:
        log.warning(f"Eslatma yuborilmadi: patient={pid} result={result}")

# ══════════════════════════════════════════
#  ASOSIY POLLING
# ══════════════════════════════════════════
def main():
    init_db()
    mode = "TEST (1 soat / 5 daqiqa)" if TEST_MODE else "ISHCHI (80 kun / 5 kun)"
    log.info(f"Bot ishga tushdi. Rejim: {mode}")

    t = threading.Thread(target=notify_worker, daemon=True)
    t.start()

    offset = 0
    while True:
        updates = get_updates(offset)
        for upd in updates:
            offset = upd["update_id"] + 1
            try:
                if "message" in upd and "text" in upd["message"]:
                    handle_message(upd["message"])
                elif "callback_query" in upd:
                    handle_callback(upd["callback_query"])
            except Exception as e:
                log.error(f"Update xato: {e}", exc_info=True)

if __name__ == "__main__":
    main()
