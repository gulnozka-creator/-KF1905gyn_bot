"""
Telegram-бот для сбора расписания врачей — Клиника Фомина 1905 / Гинекологи Рассвет
Данные сохраняются через Google Apps Script.
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

# ─── Кабинеты по фамилии ──────────────────────────────────────────────────────
# Амбулаторные гинекологи → Каб.20
# Гинекологи-хирурги → Каб.20
# Урологи → Каб.22 | Проктологи → Каб.23 | Детский гинеколог → Каб.21
DOCTOR_CABINET = {
    # Заведующая — приоритет Каб.20
    "елдашова":    "Каб.20",

    # Амбулаторные гинекологи — Каб.20 (ротация)
    "ланько":      "Каб.20",
    "белогурова":  "Каб.20",
    "малышева":    "Каб.20",
    "райкова":     "Каб.20",
    "астаповская": "Каб.20",
    "семенова":    "Каб.20",
    "голубкова":   "Каб.20",

    # Каб.21 — детский + взрослый
    "савенко":     "Каб.21",
    "фомина":      "Каб.21",

    # Гинекологи-хирурги — гибко, по умолчанию Каб.20
    "ахмедова":    "Каб.20",
    "тарасенко":   "Каб.20",
    "стыкин":      "Каб.20",
    "ростовцева":  "Каб.20",
    "глоба":       "Каб.20",
    "слепцова":    "Каб.20",
    "титов":       "Каб.20",
    "кондаков":    "Каб.20",
    "орлов":       "Каб.20",

    # Дубинин — предпочтительно Каб.22
    "дубинин":     "Каб.22",

    # Каб.22
    "локтев":      "Каб.22",
    "бехтева":     "Каб.22",

    # Проктологи — только Каб.23
    "маркарьян":   "Каб.23",
    "лукьянов":    "Каб.23",
    "мустафаева":  "Каб.23",
}

def get_cabinet_by_name(full_name: str) -> str | None:
    """Определить кабинет по первому слову (фамилии) в имени."""
    surname = full_name.strip().split()[0].lower() if full_name.strip() else ""
    return DOCTOR_CABINET.get(surname)

# ─── Хранение в памяти (резерв на случай недоступности Apps Script) ───────────
_cache: dict[int, str] = {}   # user_id → ФИО
_schedules: list = []          # все записи сессии

# ─── Работа с Google Sheets через Apps Script ─────────────────────────────────

def _call(action: str, **kwargs):
    """GET-запрос к Apps Script."""
    if not SCRIPT_URL:
        return {"ok": False, "error": "no SCRIPT_URL"}
    import json as _json
    params = {"action": action, "data": _json.dumps(kwargs, ensure_ascii=False)}
    r = requests.get(SCRIPT_URL, params=params, timeout=15)
    return r.json()

def get_doctor(user_id: int) -> str | None:
    # Сначала — из памяти (быстро и надёжно)
    if user_id in _cache:
        return _cache[user_id]
    # Потом — из таблицы
    try:
        res = _call("get_doctor", telegram_id=user_id)
        if res.get("ok") and res.get("fio"):
            _cache[user_id] = res["fio"]
            return res["fio"]
    except Exception as e:
        logging.error(f"get_doctor error: {e}")
    return None

def save_doctor(user_id: int, tg_name: str, fio: str):
    _cache[user_id] = fio  # всегда в память
    try:
        _call("save_doctor", telegram_id=user_id, tg_name=tg_name, fio=fio)
    except Exception as e:
        logging.error(f"save_doctor sheets error: {e}")

def save_schedule(doctor, date_str, start, end, cabinet, raw):
    ts = datetime.now().strftime("%d.%m.%Y %H:%M")
    # В памяти — перезаписываем если та же дата у того же врача
    for s in _schedules:
        if s["doctor"] == doctor and s["date"] == date_str:
            s.update({"timestamp": ts, "start": start, "end": end,
                       "cabinet": cabinet, "raw": raw})
            break
    else:
        _schedules.append({"timestamp": ts, "doctor": doctor, "date": date_str,
                            "start": start, "end": end, "cabinet": cabinet, "raw": raw})
    # В таблицу
    try:
        _call("save_schedule", timestamp=ts, doctor=doctor, date=date_str,
              start=start, end=end, cabinet=cabinet, raw=raw)
    except Exception as e:
        logging.error(f"save_schedule sheets error: {e}")

def get_last(doctor: str):
    # Из памяти
    rows = [s for s in _schedules if s["doctor"] == doctor]
    if rows:
        return [[s["timestamp"], s["doctor"], s["date"],
                 s["start"], s["end"], s["cabinet"]] for s in rows[-5:]]
    # Из таблицы
    try:
        res = _call("get_last", doctor=doctor)
        return res.get("rows", []) if res.get("ok") else []
    except Exception:
        return []

# ─── Парсер расписания ────────────────────────────────────────────────────────
_MONTHS = {
    "янв":1,"фев":2,"мар":3,"апр":4,"май":5,"июн":6,
    "июл":7,"авг":8,"сент":9,"сен":9,"окт":10,"ноя":11,"ноябр":11,"дек":12
}

def parse_one(text: str):
    """Парсит ОДНУ запись: возвращает (date_str, start, end) или (None,None,None)."""
    low = text.lower().strip()
    if not low:
        return None, None, None

    # Дата: 15.09 / 15/09 / 15 сентября
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

    # Время: 10:00-15:00 / с 10 до 15 / 10-15
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
            # "09-15" но не "01.09" — ищем только часы
            m = re.search(r"(?<![./\d])(\d{1,2})\s*[-–]\s*(\d{1,2})(?![./\d])", text)
            if m and int(m.group(1)) < 24 and int(m.group(2)) < 24:
                start = f"{int(m.group(1)):02d}:00"
                end   = f"{int(m.group(2)):02d}:00"

    return date_str, start, end

def parse_schedule(text: str) -> list:
    """Парсит одно или несколько расписаний из одного сообщения.
    Возвращает список (date_str, start, end).
    Поддерживает разделение запятой или переносом строки.
    """
    # Разбиваем по запятой или переносу строки
    segments = re.split(r",|\n", text)
    results = []
    last_time = (None, None)  # запоминаем время для строк "дата - то же время"

    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        date_str, start, end = parse_one(seg)
        if start and end:
            last_time = (start, end)
        elif date_str and not start:
            # Дата есть, время нет — берём последнее известное время
            start, end = last_time
        if date_str or start:
            results.append((date_str, start, end))

    return results

# ─── Состояния ────────────────────────────────────────────────────────────────
ASK_NAME, ASK_CABINET = range(2)
_CAB_KB = ReplyKeyboardMarkup(
    [["Каб.20", "Каб.21"], ["Каб.22", "Каб.23"]],
    one_time_keyboard=True, resize_keyboard=True
)

# ─── Хэндлеры ────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    name = get_doctor(uid)
    if name:
        await update.message.reply_text(
            f"Привет, {name.split()[0]}! 👋\n\n"
            "Отправьте своё расписание — одну дату или сразу весь месяц через запятую:\n"
            "• <b>15.09 10:00–15:00</b>\n"
            "• <b>01.09 09:00-15:00, 08.09 09:00-15:00, 15.09 10:00-15:00</b>\n\n"
            "/myplan — моё расписание  |  /myname — изменить имя",
            parse_mode="HTML"
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "👋 Привет! Это бот Клиники Фомина 1905 г!\n"
        "Я помогу тебе сформировать запись 📅\n\n"
        "Введите своё <b>Имя и Фамилию</b>:\n"
        "(например: <i>Юлия Савенко</i>)",
        parse_mode="HTML"
    )
    return ASK_NAME

async def got_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    save_doctor(update.effective_user.id, update.effective_user.full_name, name)

    await update.message.reply_text(
        f"✅ Записан как: <b>{name}</b>\n\n"
        "Теперь присылайте расписание — одну дату или весь месяц через запятую:\n"
        "• <b>15.09 10:00–15:00</b>\n"
        "• <b>01.09 09:00-15:00, 08.09 09:00-15:00, 15.09 10:00-15:00</b>",
        parse_mode="HTML"
    )
    return ConversationHandler.END

async def cmd_myname(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введите новое Имя и Фамилию:")
    return ASK_NAME

async def cmd_last(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    doctor = get_doctor(update.effective_user.id)
    if not doctor:
        await update.message.reply_text("Вы не зарегистрированы. Напишите /start")
        return
    rows = get_last(doctor)
    if not rows:
        await update.message.reply_text("У вас пока нет записей.")
        return
    lines = ["📋 <b>Последние записи:</b>"]
    for r in rows:
        lines.append(f"• {r[2]}  {r[3]}–{r[4]}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def cmd_myplan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    doctor = get_doctor(update.effective_user.id)
    if not doctor:
        await update.message.reply_text("Вы не зарегистрированы. Напишите /start")
        return
    rows = [s for s in _schedules if s["doctor"] == doctor]
    if not rows:
        await update.message.reply_text("У вас пока нет записей.")
        return
    # Сортируем по дате
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
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid    = update.effective_user.id
    text   = update.message.text.strip()
    doctor = get_doctor(uid)

    if not doctor:
        await update.message.reply_text("Сначала представьтесь — напишите /start")
        return ConversationHandler.END

    entries = parse_schedule(text)

    if not entries:
        await update.message.reply_text(
            "Не смог распознать дату/время 🤔\n\n"
            "Пожалуйста, укажите конкретную дату:\n"
            "• <b>15.09 10:00–15:00</b>\n"
            "• <b>15.09 09:00-15:00, 22.09 09:00-15:00</b> (несколько дат через запятую)",
            parse_mode="HTML"
        )
        return ConversationHandler.END

    # Сохраняем все записи (кабинет назначается позже заведующей)
    saved = []
    for date_str, start, end in entries:
        if date_str or start:
            save_schedule(doctor, date_str or "?", start or "?", end or "?", "—", text)
            saved.append(f"📅 {date_str}  ⏰ {start}–{end}")

    if saved:
        lines = [f"✅ Записано {len(saved)} дн.  👤 {doctor}"] + saved
        await update.message.reply_text("\n".join(lines))
    else:
        await update.message.reply_text("Не удалось распознать даты, попробуйте ещё раз.")

    return ConversationHandler.END

async def got_cabinet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw_cab = update.message.text.strip()
    pending = ctx.user_data.pop("pending_multi", None)
    if not pending:
        await update.message.reply_text("Отправьте расписание заново.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END
    doctor, entries, raw = pending
    m = re.search(r"\d+", raw_cab)
    cabinet = f"Каб.{m.group()}" if m else raw_cab
    saved = []
    for date_str, start, end in entries:
        if date_str or start:
            save_schedule(doctor, date_str or "?", start or "?", end or "?", cabinet, raw)
            saved.append(f"📅 {date_str}  ⏰ {start}–{end}")
    lines = [f"✅ Записано {len(saved)} дн. — 🚪 {cabinet}  👤 {doctor}"] + saved
    await update.message.reply_text("\n".join(lines), reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# ─── Запуск ───────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start), CommandHandler("myname", cmd_myname)],
        states={
            ASK_NAME:    [MessageHandler(filters.TEXT & ~filters.COMMAND, got_name)],
            ASK_CABINET: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_cabinet)],
        },
        fallbacks=[CommandHandler("start", cmd_start)],
        allow_reentry=True,
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("last", cmd_last))
    app.add_handler(CommandHandler("myplan", cmd_myplan))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Бот запущен ✅")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
