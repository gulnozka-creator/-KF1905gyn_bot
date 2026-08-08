"""
Telegram-бот для сбора расписания врачей — Клиника Фомина 1905 / Гинекологи Рассвет
"""

import os, re, logging, requests
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)

BOT_TOKEN  = os.environ.get("BOT_TOKEN", "7993383549:AAE8TWlgULmginVsQY5VZ8eRYwJxNGjCAUg")
SCRIPT_URL = os.environ.get("SCRIPT_URL", "")

# ─── Список врачей ────────────────────────────────────────────────────────────
DOCTORS = [
    "Елдашова Гульноза",
    "Ланько Анастасия",
    "Белогурова Екатерина",
    "Малышева Яна",
    "Райкова Анастасия",
    "Астаповская Ольга",
    "Семенова Милена",
    "Голубкова Наталья",
    "Савенко Юлия",
    "Фомина Мария",
    "Ахмедова Дженнет",
    "Тарасенко Юлия",
    "Стыкин Ярослав",
    "Ростовцева Оксана",
    "Глоба Юлия",
    "Слепцова Дарья",
    "Дубинин Андрей",
    "Титов Денис",
    "Кондаков Илья",
    "Орлов Олег",
    "Локтев Артем",
    "Бехтева Марина",
    "Маркарьян Даниил",
    "Лукьянов Александр",
]
DOCTORS_SET = set(DOCTORS)

# ─── Клавиатуры ───────────────────────────────────────────────────────────────
BTN_PLAN  = "📅 Моё расписание"
BTN_CHANGE = "✏️ Сменить врача"
BTN_BACK  = "← Назад"

MAIN_KB = ReplyKeyboardMarkup(
    [[BTN_PLAN, BTN_CHANGE]],
    resize_keyboard=True
)

def doctors_kb(show_back: bool = False):
    rows = []
    for i in range(0, len(DOCTORS), 2):
        rows.append(DOCTORS[i:i+2])
    if show_back:
        rows.append([BTN_BACK])
    return ReplyKeyboardMarkup(rows, one_time_keyboard=True, resize_keyboard=True)

# ─── Кабинеты по фамилии ─────────────────────────────────────────────────────
DOCTOR_CABINET = {
    "елдашова": "Каб.20",
    "ланько": "Каб.20", "белогурова": "Каб.20", "малышева": "Каб.20",
    "райкова": "Каб.20", "астаповская": "Каб.20", "семенова": "Каб.20",
    "голубкова": "Каб.20",
    "савенко": "Каб.21", "фомина": "Каб.21",
    "ахмедова": "Каб.20", "тарасенко": "Каб.20", "стыкин": "Каб.20",
    "ростовцева": "Каб.20", "глоба": "Каб.20", "слепцова": "Каб.20",
    "титов": "Каб.20", "кондаков": "Каб.20", "орлов": "Каб.20",
    "дубинин": "Каб.22", "локтев": "Каб.22", "бехтева": "Каб.22",
    "маркарьян": "Каб.23", "лукьянов": "Каб.23", "мустафаева": "Каб.23",
}

# ─── Хранение в памяти ────────────────────────────────────────────────────────
_cache: dict[int, str] = {}
_schedules: list = []

# ─── Apps Script ──────────────────────────────────────────────────────────────
def _call(action: str, **kwargs):
    if not SCRIPT_URL:
        return {"ok": False}
    import json as _j
    try:
        r = requests.get(SCRIPT_URL,
                         params={"action": action, "data": _j.dumps(kwargs, ensure_ascii=False)},
                         timeout=15)
        return r.json()
    except Exception as e:
        logging.error(f"_call {action}: {e}")
        return {"ok": False}

def get_doctor(user_id: int) -> str | None:
    if user_id in _cache:
        return _cache[user_id]
    res = _call("get_doctor", telegram_id=user_id)
    if res.get("ok") and res.get("fio"):
        _cache[user_id] = res["fio"]
        return res["fio"]
    return None

def save_doctor(user_id: int, tg_name: str, fio: str):
    _cache[user_id] = fio
    _call("save_doctor", telegram_id=user_id, tg_name=tg_name, fio=fio)

def save_schedule(doctor, date_str, start, end, cabinet, raw):
    ts = datetime.now().strftime("%d.%m.%Y %H:%M")
    for s in _schedules:
        if s["doctor"] == doctor and s["date"] == date_str:
            s.update({"timestamp": ts, "start": start, "end": end, "cabinet": cabinet, "raw": raw})
            break
    else:
        _schedules.append({"timestamp": ts, "doctor": doctor, "date": date_str,
                            "start": start, "end": end, "cabinet": cabinet, "raw": raw})
    _call("save_schedule", timestamp=ts, doctor=doctor, date=date_str,
          start=start, end=end, cabinet=cabinet, raw=raw)

def get_my_plan(doctor: str) -> list:
    rows = [s for s in _schedules if s["doctor"] == doctor]
    if rows:
        return rows
    res = _call("get_last", doctor=doctor)
    if res.get("ok"):
        return [{"date": r[2], "start": r[3], "end": r[4]} for r in res.get("rows", [])]
    return []

# ─── Парсер расписания ────────────────────────────────────────────────────────
_MONTHS = {
    "янв":1,"фев":2,"мар":3,"апр":4,"май":5,"июн":6,
    "июл":7,"авг":8,"сент":9,"сен":9,"окт":10,"ноя":11,"ноябр":11,"дек":12
}

def parse_one(text: str):
    low = text.lower().strip()
    if not low:
        return None, None, None
    date_str = None
    m = re.search(r"\b(\d{1,2})[./](\d{1,2})\b", text)
    if m:
        date_str = f"{int(m.group(1)):02d}.{int(m.group(2)):02d}"
    else:
        pat = r"\b(\d{1,2})\s+(" + "|".join(_MONTHS) + r")\w*"
        m = re.search(pat, low)
        if m:
            mn = next((k for k in _MONTHS if m.group(2).startswith(k)), None)
            if mn:
                date_str = f"{int(m.group(1)):02d}.{_MONTHS[mn]:02d}"
    start = end = None
    m = re.search(r"(\d{1,2}):(\d{2})\s*[-–]\s*(\d{1,2}):(\d{2})", text)
    if m:
        start = f"{int(m.group(1)):02d}:{m.group(2)}"
        end   = f"{int(m.group(3)):02d}:{m.group(4)}"
    else:
        m = re.search(r"\bс\s+(\d{1,2})(?::(\d{2}))?\s+до\s+(\d{1,2})(?::(\d{2}))?", low)
        if m:
            start = f"{int(m.group(1)):02d}:{m.group(2) or '00'}"
            end   = f"{int(m.group(3)):02d}:{m.group(4) or '00'}"
        else:
            m = re.search(r"(?<![./\d])(\d{1,2})\s*[-–]\s*(\d{1,2})(?![./\d])", text)
            if m and int(m.group(1)) < 24 and int(m.group(2)) < 24:
                start = f"{int(m.group(1)):02d}:00"
                end   = f"{int(m.group(2)):02d}:00"
    return date_str, start, end

def parse_schedule(text: str) -> list:
    segments = re.split(r",|\n", text)
    results = []
    last_time = (None, None)
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        date_str, start, end = parse_one(seg)
        if start and end:
            last_time = (start, end)
        elif date_str and not start:
            start, end = last_time
        if date_str or start:
            results.append((date_str, start, end))
    return results

# ─── Состояния ────────────────────────────────────────────────────────────────
ASK_NAME = 0

def _welcome(name: str) -> str:
    return (
        f"Привет, {name.split()[0]}! 👋\n\n"
        "Отправьте своё расписание — одну дату или весь месяц через запятую:\n"
        "• <b>15.09 10:00–15:00</b>\n"
        "• <b>01.09 09:00-15:00, 08.09 09:00-15:00, 15.09 10:00-15:00</b>"
    )

# ─── Хэндлеры ────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    name = get_doctor(uid)
    if name:
        await update.message.reply_text(_welcome(name), parse_mode="HTML", reply_markup=MAIN_KB)
        return ConversationHandler.END
    await update.message.reply_text(
        "👋 Привет! Это бот Клиники Фомина 1905 г!\n"
        "Я помогу тебе сформировать запись 📅\n\n"
        "Выберите себя из списка:",
        reply_markup=doctors_kb(show_back=False)
    )
    return ASK_NAME

async def got_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    uid  = update.effective_user.id

    if text == BTN_BACK:
        name = get_doctor(uid)
        if name:
            await update.message.reply_text(_welcome(name), parse_mode="HTML", reply_markup=MAIN_KB)
        else:
            await update.message.reply_text("Выберите себя из списка:", reply_markup=doctors_kb())
        return ConversationHandler.END if name else ASK_NAME

    if text not in DOCTORS_SET:
        await update.message.reply_text(
            "Пожалуйста, выберите имя из списка ниже:",
            reply_markup=doctors_kb(show_back=bool(get_doctor(uid)))
        )
        return ASK_NAME

    save_doctor(uid, update.effective_user.full_name, text)
    await update.message.reply_text(
        f"✅ Вы выбрали: <b>{text}</b>\n\n"
        "Теперь присылайте расписание:",
        parse_mode="HTML",
        reply_markup=MAIN_KB
    )
    return ConversationHandler.END

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    text = update.message.text.strip()

    # ── Кнопки главного меню ──
    if text == BTN_PLAN:
        doctor = get_doctor(uid)
        if not doctor:
            await update.message.reply_text("Сначала выберите себя — нажмите /start")
            return
        rows = get_my_plan(doctor)
        if not rows:
            await update.message.reply_text("У вас пока нет записей.", reply_markup=MAIN_KB)
            return
        def sort_key(s):
            try:
                d, m = s["date"].split(".")
                return (int(m), int(d))
            except Exception:
                return (99, 99)
        rows = sorted(rows, key=sort_key)
        lines = [f"📅 <b>Ваше расписание — {doctor}:</b>"]
        for s in rows:
            lines.append(f"• {s['date']}  ⏰ {s['start']}–{s['end']}")
        lines.append("\nЧтобы изменить дату — отправьте её заново с новым временем.")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=MAIN_KB)
        return

    if text == BTN_CHANGE:
        await update.message.reply_text(
            "Выберите себя из списка:",
            reply_markup=doctors_kb(show_back=True)
        )
        ctx.user_data["changing_name"] = True
        return

    # ── Расписание ──
    doctor = get_doctor(uid)
    if not doctor:
        await update.message.reply_text("Сначала выберите себя — нажмите /start")
        return

    entries = parse_schedule(text)
    if not entries:
        await update.message.reply_text(
            "Не смог распознать дату/время 🤔\n\n"
            "Примеры:\n"
            "• <b>15.09 10:00–15:00</b>\n"
            "• <b>01.09 09:00-15:00, 08.09 09:00-15:00</b>",
            parse_mode="HTML",
            reply_markup=MAIN_KB
        )
        return

    saved = []
    for date_str, start, end in entries:
        if date_str or start:
            save_schedule(doctor, date_str or "?", start or "?", end or "?", "—", text)
            saved.append(f"• {date_str}  ⏰ {start}–{end}")

    if saved:
        lines = [f"✅ Записано {len(saved)} дн.  👤 {doctor}"] + saved
        await update.message.reply_text("\n".join(lines), reply_markup=MAIN_KB)
    else:
        await update.message.reply_text("Не удалось распознать даты, попробуйте ещё раз.", reply_markup=MAIN_KB)

# ─── Запуск ───────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # ConversationHandler только для первичной регистрации
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_name)],
        },
        fallbacks=[CommandHandler("start", cmd_start)],
        allow_reentry=True,
    )
    app.add_handler(conv)

    # Всё остальное (кнопки меню + расписание)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Бот запущен ✅")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
