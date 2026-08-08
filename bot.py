"""
Telegram-бот для сбора расписания врачей — Клиника Фомина 1905 / Гинекологи Рассвет
"""

import os, re, logging, requests
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, Bot
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)

BOT_TOKEN    = os.environ.get("BOT_TOKEN", "")
SCRIPT_URL   = os.environ.get("SCRIPT_URL", "")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "303052844")   # ← ваш Telegram chat ID (добавить в Railway)

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

# ─── Кнопки ───────────────────────────────────────────────────────────────────
BTN_ADD    = "➕ Добавить расписание"
BTN_FIX    = "✏️ Исправить расписание"
BTN_PLAN   = "📅 Моё расписание"
BTN_CHANGE = "🔄 Сменить врача"
BTN_BACK   = "← Назад"
BTN_CANCEL = "✖ Отмена"

MAIN_KB = ReplyKeyboardMarkup(
    [[BTN_ADD], [BTN_FIX, BTN_PLAN], [BTN_CHANGE]],
    resize_keyboard=True
)

def doctors_kb():
    rows = []
    for i in range(0, len(DOCTORS), 2):
        rows.append(DOCTORS[i:i+2])
    rows.append([BTN_BACK])
    return ReplyKeyboardMarkup(rows, one_time_keyboard=True, resize_keyboard=True)

CANCEL_KB = ReplyKeyboardMarkup([[BTN_CANCEL]], resize_keyboard=True)

# ─── Память и Apps Script ─────────────────────────────────────────────────────
_cache: dict[int, str] = {}
_schedules: list = []

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

def save_schedule(doctor: str, date_str: str, start: str, end: str, raw: str, is_fix: bool = False) -> bool:
    """Сохраняет запись. Возвращает True если это замена существующей записи."""
    ts = datetime.now().strftime("%d.%m.%Y %H:%M")
    replaced = False
    for s in _schedules:
        if s["doctor"] == doctor and s["date"] == date_str:
            s.update({"timestamp": ts, "start": start, "end": end, "raw": raw})
            replaced = True
            break
    if not replaced:
        _schedules.append({"timestamp": ts, "doctor": doctor, "date": date_str,
                            "start": start, "end": end, "raw": raw})
    res = _call("save_schedule", timestamp=ts, doctor=doctor, date=date_str,
                start=start, end=end, cabinet="—", raw=raw)
    # Если Apps Script говорит что перезаписал — тоже считаем как замену
    if res.get("updated"):
        replaced = True
    return replaced

def get_my_plan(doctor: str) -> list:
    rows = [s for s in _schedules if s["doctor"] == doctor]
    if rows:
        # Дедупликация по дате — оставляем последнюю запись
        seen: dict[str, dict] = {}
        for s in rows:
            seen[s["date"]] = s
        return list(seen.values())
    res = _call("get_last", doctor=doctor)
    if res.get("ok") and res.get("rows"):
        seen: dict[str, dict] = {}
        for r in res["rows"]:
            date = str(r[2])
            seen[date] = {"doctor": doctor, "date": date, "start": str(r[3]), "end": str(r[4])}
        loaded = list(seen.values())
        _schedules.extend(loaded)
        return loaded
    return []

# ─── Уведомление заведующей ───────────────────────────────────────────────────
async def notify_admin(app: Application, doctor: str, entries: list, is_fix: bool):
    if not ADMIN_CHAT_ID:
        return
    action = "✏️ Исправил(а)" if is_fix else "➕ Добавил(а)"
    lines = [f"🔔 <b>{doctor}</b> {action} расписание:"]
    for date_str, start, end, replaced in entries:
        mark = " (замена)" if replaced else ""
        lines.append(f"• {date_str}  ⏰ {start}–{end}{mark}")
    try:
        await app.bot.send_message(chat_id=ADMIN_CHAT_ID, text="\n".join(lines), parse_mode="HTML")
    except Exception as e:
        logging.error(f"notify_admin: {e}")

# ─── Парсер ───────────────────────────────────────────────────────────────────
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

def _plan_text(doctor: str) -> str:
    rows = get_my_plan(doctor)
    if not rows:
        return "У вас пока нет записей."
    def sort_key(s):
        try:
            d, mo = s["date"].split(".")
            return (int(mo), int(d))
        except Exception:
            return (99, 99)
    rows = sorted(rows, key=sort_key)
    lines = [f"📅 <b>Ваше расписание — {doctor}:</b>"]
    for s in rows:
        lines.append(f"• {s['date']}  ⏰ {s['start']}–{s['end']}")
    return "\n".join(lines)

# ─── Состояния ConversationHandler ────────────────────────────────────────────
CHOOSING_NAME, ADDING, FIXING = range(3)

def _main_menu(name: str) -> str:
    return f"Привет, {name.split()[0]}! 👋 Что хотите сделать?"

# ─── Хэндлеры ────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    name = get_doctor(uid)
    if name:
        await update.message.reply_text(_main_menu(name), reply_markup=MAIN_KB)
        return ConversationHandler.END
    await update.message.reply_text(
        "👋 Привет! Это бот Клиники Фомина 1905 г!\n"
        "Я помогу тебе сформировать запись 📅\n\n"
        "Выберите себя из списка:",
        reply_markup=doctors_kb()
    )
    return CHOOSING_NAME

# ── Выбор врача из списка ──
async def chosen_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    uid  = update.effective_user.id

    if text == BTN_BACK:
        name = get_doctor(uid)
        if name:
            await update.message.reply_text(_main_menu(name), reply_markup=MAIN_KB)
            return ConversationHandler.END
        await update.message.reply_text("Выберите себя из списка:", reply_markup=doctors_kb())
        return CHOOSING_NAME

    if text not in DOCTORS_SET:
        await update.message.reply_text(
            "Пожалуйста, нажмите своё имя на кнопке ниже:",
            reply_markup=doctors_kb()
        )
        return CHOOSING_NAME

    save_doctor(uid, update.effective_user.full_name, text)
    await update.message.reply_text(
        f"✅ Вы выбрали: <b>{text}</b>\n\nЧто хотите сделать?",
        parse_mode="HTML",
        reply_markup=MAIN_KB
    )
    return ConversationHandler.END

# ── Кнопка «Добавить расписание» ──
async def btn_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    doctor = get_doctor(update.effective_user.id)
    if not doctor:
        await update.message.reply_text("Сначала выберите себя — нажмите /start")
        return ConversationHandler.END
    ctx.user_data["is_fix"] = False
    await update.message.reply_text(
        "Отправьте расписание — одну дату или весь месяц через запятую:\n"
        "• <b>15.09 10:00–15:00</b>\n"
        "• <b>01.09 09:00-15:00, 08.09 09:00-15:00, 15.09 10:00-15:00</b>",
        parse_mode="HTML",
        reply_markup=CANCEL_KB
    )
    return ADDING

# ── Приём расписания (и добавление, и исправление) ──
async def receive_schedule(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text   = update.message.text.strip()
    doctor = get_doctor(update.effective_user.id)
    is_fix = ctx.user_data.get("is_fix", False)

    if text == BTN_CANCEL:
        await update.message.reply_text("Отменено.", reply_markup=MAIN_KB)
        return ConversationHandler.END

    entries = parse_schedule(text)
    if not entries:
        await update.message.reply_text(
            "Не смог распознать дату/время 🤔\n"
            "Попробуйте: <b>15.09 10:00–15:00</b>",
            parse_mode="HTML"
        )
        return ADDING if not is_fix else FIXING

    saved = []
    notify_entries = []
    for date_str, start, end in entries:
        if date_str or start:
            replaced = save_schedule(doctor, date_str or "?", start or "?", end or "?", text, is_fix)
            mark = " (замена)" if replaced else ""
            saved.append(f"• {date_str}  ⏰ {start}–{end}{mark}")
            notify_entries.append((date_str, start, end, replaced))

    lines = [f"✅ Записано {len(saved)} дн.  👤 {doctor}"] + saved
    await update.message.reply_text("\n".join(lines), reply_markup=MAIN_KB)

    # Уведомление заведующей
    await notify_admin(ctx.application, doctor, notify_entries, is_fix)

    return ConversationHandler.END

# ── Кнопка «Исправить расписание» ──
async def btn_fix(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    doctor = get_doctor(update.effective_user.id)
    if not doctor:
        await update.message.reply_text("Сначала выберите себя — нажмите /start")
        return ConversationHandler.END
    ctx.user_data["is_fix"] = True
    plan = _plan_text(doctor)
    await update.message.reply_text(
        f"{plan}\n\n"
        "Отправьте дату с новым временем — запись заменится:\n"
        "• <b>15.09 11:00–16:00</b>",
        parse_mode="HTML",
        reply_markup=CANCEL_KB
    )
    return FIXING

# ── Кнопка «Моё расписание» ──
async def btn_plan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    doctor = get_doctor(update.effective_user.id)
    if not doctor:
        await update.message.reply_text("Сначала выберите себя — нажмите /start")
        return ConversationHandler.END
    await update.message.reply_text(_plan_text(doctor), parse_mode="HTML", reply_markup=MAIN_KB)
    return ConversationHandler.END

# ── Кнопка «Сменить врача» ──
async def btn_change(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Выберите себя из списка:", reply_markup=doctors_kb())
    return CHOOSING_NAME

# ─── Запуск ───────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_ADD)}$"),    btn_add),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_FIX)}$"),    btn_fix),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_PLAN)}$"),   btn_plan),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_CHANGE)}$"), btn_change),
        ],
        states={
            CHOOSING_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, chosen_name)],
            ADDING:        [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_schedule)],
            FIXING:        [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_schedule)],
        },
        fallbacks=[CommandHandler("start", cmd_start)],
        allow_reentry=True,
    )
    app.add_handler(conv)
    print("Бот запущен ✅")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
