"""
Telegram-бот для сбора расписания врачей — Клиника Фомина 1905
Гинекологическое отделение
"""

import os, re, logging, requests
from datetime import datetime, date, time as dtime
from calendar import monthrange
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, Bot
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)

BOT_TOKEN  = os.environ.get("BOT_TOKEN", "")
SCRIPT_URL = os.environ.get("SCRIPT_URL", "")

# ─── Заведующая отделением ─────────────────────────────────────────────────────
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "303052844")   # Елдашова Гульноза

# ─── Врачи гинекологического отделения ────────────────────────────────────────
GYNO_DOCTORS = [
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
    # Новые врачи «закрытия пробелов»
    "Завьялова Инна",
    "Саградян Карина",
    "Мехедова Ксения",
]

ALL_DOCTORS_SET = set(GYNO_DOCTORS)

# Врачи, которые заполняют «красные и жёлтые» смены — им предлагаются слоты
FLEX_DOCTORS = {"Завьялова Инна", "Саградян Карина", "Мехедова Ксения"}

# ─── Месяцы ───────────────────────────────────────────────────────────────────
MONTH_RU = {
    1:"Январь", 2:"Февраль", 3:"Март", 4:"Апрель",
    5:"Май", 6:"Июнь", 7:"Июль", 8:"Август",
    9:"Сентябрь", 10:"Октябрь", 11:"Ноябрь", 12:"Декабрь"
}
MONTH_GEN = {   # родительный падеж для «расписание на …»
    1:"январь", 2:"февраль", 3:"март", 4:"апрель",
    5:"май", 6:"июнь", 7:"июль", 8:"август",
    9:"сентябрь", 10:"октябрь", 11:"ноябрь", 12:"декабрь"
}

def current_and_next():
    """Возвращает (текущий, следующий) как (year, month)."""
    today = date.today()
    y, m = today.year, today.month
    if m == 12:
        return (y, m), (y + 1, 1)
    return (y, m), (y, m + 1)

def month_label(y: int, m: int) -> str:
    return f"{MONTH_RU[m]} {y}"

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
CANCEL_KB = ReplyKeyboardMarkup([[BTN_CANCEL]], resize_keyboard=True)

def doctors_kb():
    docs = GYNO_DOCTORS
    rows = []
    for i in range(0, len(docs), 2):
        rows.append(docs[i:i+2])
    rows.append([BTN_BACK])
    return ReplyKeyboardMarkup(rows, one_time_keyboard=True, resize_keyboard=True)

def month_kb():
    (cy, cm), (ny, nm) = current_and_next()
    return ReplyKeyboardMarkup(
        [[month_label(cy, cm)], [month_label(ny, nm)], [BTN_BACK]],
        one_time_keyboard=True, resize_keyboard=True
    )

# ─── Состояния ────────────────────────────────────────────────────────────────
CHOOSING_NAME, ADDING, FIXING, CHOOSING_MONTH = range(4)

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
    _call("save_doctor", telegram_id=user_id, tg_name=tg_name, fio=fio, dept="gyno")

def save_schedule(doctor: str, date_str: str, start: str, end: str, raw: str, is_fix: bool = False) -> bool:
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
    if res.get("updated"):
        replaced = True
    return replaced

def get_my_plan(doctor: str, year: int = None, month: int = None) -> list:
    rows = [s for s in _schedules if s["doctor"] == doctor]
    if not rows:
        res = _call("get_last", doctor=doctor)
        if res.get("ok") and res.get("rows"):
            seen: dict[str, dict] = {}
            for r in res["rows"]:
                d = str(r[2])
                seen[d] = {"doctor": doctor, "date": d, "start": str(r[3]), "end": str(r[4])}
            rows = list(seen.values())
            _schedules.extend(rows)

    if year and month:
        suffix = f".{month:02d}"
        rows = [r for r in rows if str(r.get("date", "")).endswith(suffix)]

    seen: dict[str, dict] = {}
    for s in rows:
        seen[s["date"]] = s
    return list(seen.values())

def _plan_text(doctor: str, year: int = None, month: int = None) -> str:
    rows = get_my_plan(doctor, year, month)
    if not rows:
        label = f" на {MONTH_GEN[month]}" if month else ""
        return f"У вас пока нет записей{label}."
    def sort_key(s):
        try:
            d, mo = s["date"].split(".")
            return (int(mo), int(d))
        except Exception:
            return (99, 99)
    rows = sorted(rows, key=sort_key)
    header = f"{MONTH_RU[month]} {year}" if month else "все месяцы"
    lines = [f"📅 <b>Расписание {doctor} — {header}:</b>"]
    for s in rows:
        lines.append(f"• {s['date']}  ⏰ {s['start']}–{s['end']}")
    return "\n".join(lines)

# ─── Уведомление заведующей ───────────────────────────────────────────────────
async def notify_admin(app: Application, doctor: str, entries: list, is_fix: bool):
    if not ADMIN_CHAT_ID:
        return
    action = "✏️ Исправил(а)" if is_fix else "➕ Добавил(а)"
    lines = [f"🔔 <b>{doctor}</b> {action} расписание (🩺 Гинекология):"]
    for date_str, start, end, replaced in entries:
        mark = " (замена)" if replaced else ""
        lines.append(f"• {date_str}  ⏰ {start}–{end}{mark}")
    try:
        await app.bot.send_message(chat_id=ADMIN_CHAT_ID, text="\n".join(lines), parse_mode="HTML")
    except Exception as e:
        logging.error(f"notify_admin: {e}")

# ─── Парсер расписания ─────────────────────────────────────────────────────────
_MONTHS_P = {
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
        pat = r"\b(\d{1,2})\s+(" + "|".join(_MONTHS_P) + r")\w*"
        m = re.search(pat, low)
        if m:
            mn = next((k for k in _MONTHS_P if m.group(2).startswith(k)), None)
            if mn:
                date_str = f"{int(m.group(1)):02d}.{_MONTHS_P[mn]:02d}"
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

# ─── Слоты для flex-врачей (красные/жёлтые смены) ────────────────────────────
def get_flex_slots() -> str:
    """
    Возвращает текстовый список рекомендованных смен для Завьялова/Саградян/Мехедова.
    При интеграции с Apps Script — заменить на _call("get_flex_slots").
    Пока возвращает актуальный список для октября (обновляйте каждый месяц).
    """
    res = _call("get_flex_slots")
    if res.get("ok") and res.get("slots"):
        return res["slots"]
    # Fallback — заглушка, пока Apps Script не настроен
    return (
        "Актуальные смены будут загружены автоматически.\n"
        "Напишите заведующей для уточнения расписания."
    )

# ─── Напоминания (5–10 числа каждого месяца) ──────────────────────────────────
async def send_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Ежедневно в 12:00 — напоминает тем, кто не подал расписание на след. месяц."""
    today = date.today()
    if today.day < 5 or today.day > 10:
        return

    _, (ny, nm) = current_and_next()
    next_month_label = f"{MONTH_GEN[nm]} {ny}"

    # Получаем список тех, кто уже подал
    res = _call("get_submitted_doctors", month=nm, year=ny)
    submitted = set(res.get("doctors", [])) if res.get("ok") else set()

    # Получаем все пары telegram_id → ФИО
    res2 = _call("get_all_doctors")
    if not res2.get("ok") or not res2.get("rows"):
        return

    text = (
        f"👋 Добрый день!\n\n"
        f"Напоминаем, что расписание на <b>{next_month_label}</b> ещё не подано.\n\n"
        f"Пожалуйста, найдите минутку и отправьте его через бот — это займёт буквально пару минут 🙏\n\n"
        f"Просто нажмите <b>«➕ Добавить расписание»</b>."
    )

    for row in res2["rows"]:
        try:
            tg_id = int(row[0])
            fio   = str(row[1])
            if fio in submitted or fio in FLEX_DOCTORS:
                continue   # уже подал или это flex-врач (им пишем отдельно)
            if fio not in ALL_DOCTORS_SET:
                continue
            await context.bot.send_message(chat_id=tg_id, text=text, parse_mode="HTML")
        except Exception as e:
            logging.warning(f"Напоминание {row}: {e}")

# ─── Хэндлеры ─────────────────────────────────────────────────────────────────

def _main_menu(name: str) -> str:
    return f"Привет, {name.split()[0]}! 👋 Что хотите сделать?"

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    name = get_doctor(uid)
    if name:
        await update.message.reply_text(_main_menu(name), reply_markup=MAIN_KB)
        return ConversationHandler.END
    await update.message.reply_text(
        "👋 Привет! Это бот Клиники Фомина 1905.\n\n"
        "Выберите себя из списка:",
        reply_markup=doctors_kb()
    )
    return CHOOSING_NAME

# ── Выбор врача ──
async def chosen_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    uid  = update.effective_user.id

    if text == BTN_BACK or text not in ALL_DOCTORS_SET:
        await update.message.reply_text(
            "Пожалуйста, нажмите своё имя на кнопке:",
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

# ── «Добавить расписание» ──
async def btn_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    doctor = get_doctor(update.effective_user.id)
    if not doctor:
        await update.message.reply_text("Сначала выберите себя — нажмите /start")
        return ConversationHandler.END
    ctx.user_data["is_fix"] = False

    if doctor in FLEX_DOCTORS:
        # Показываем рекомендованные слоты
        slots_text = get_flex_slots()
        await update.message.reply_text(
            f"📋 <b>Доступные смены для записи:</b>\n\n{slots_text}\n\n"
            "Выберите удобную смену и отправьте в формате:\n"
            "<b>10.10 15:00–21:00</b>",
            parse_mode="HTML",
            reply_markup=CANCEL_KB
        )
    else:
        await update.message.reply_text(
            "Отправьте расписание — одну дату или весь месяц через запятую:\n"
            "• <b>15.10 10:00–15:00</b>\n"
            "• <b>01.10 09:00-15:00, 08.10 09:00-15:00, 15.10 10:00-15:00</b>",
            parse_mode="HTML",
            reply_markup=CANCEL_KB
        )
    return ADDING

# ── Приём расписания ──
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
            "Попробуйте: <b>15.10 10:00–15:00</b>",
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
    await notify_admin(ctx.application, doctor, notify_entries, is_fix)
    return ConversationHandler.END

# ── «Исправить расписание» ──
async def btn_fix(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    doctor = get_doctor(update.effective_user.id)
    if not doctor:
        await update.message.reply_text("Сначала выберите себя — нажмите /start")
        return ConversationHandler.END
    ctx.user_data["is_fix"] = True
    (cy, cm), _ = current_and_next()
    plan = _plan_text(doctor, cy, cm)
    await update.message.reply_text(
        f"{plan}\n\n"
        "Отправьте дату с новым временем — запись заменится:\n"
        "• <b>15.10 11:00–16:00</b>",
        parse_mode="HTML",
        reply_markup=CANCEL_KB
    )
    return FIXING

# ── «Моё расписание» — сначала выбор месяца ──
async def btn_plan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    doctor = get_doctor(update.effective_user.id)
    if not doctor:
        await update.message.reply_text("Сначала выберите себя — нажмите /start")
        return ConversationHandler.END
    (cy, cm), (ny, nm) = current_and_next()
    await update.message.reply_text(
        "За какой месяц показать расписание?",
        reply_markup=month_kb()
    )
    return CHOOSING_MONTH

async def chosen_month(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text   = update.message.text.strip()
    doctor = get_doctor(update.effective_user.id)

    if text == BTN_BACK:
        await update.message.reply_text(_main_menu(doctor), reply_markup=MAIN_KB)
        return ConversationHandler.END

    (cy, cm), (ny, nm) = current_and_next()
    if text == month_label(cy, cm):
        y, m = cy, cm
    elif text == month_label(ny, nm):
        y, m = ny, nm
    else:
        await update.message.reply_text(
            "Пожалуйста, выберите месяц из списка ниже:",
            reply_markup=month_kb()
        )
        return CHOOSING_MONTH

    await update.message.reply_text(
        _plan_text(doctor, y, m),
        parse_mode="HTML",
        reply_markup=MAIN_KB
    )
    return ConversationHandler.END

# ── «Сменить врача» ──
async def btn_change(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Выберите себя из списка:",
        reply_markup=doctors_kb()
    )
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
            CHOOSING_NAME:  [MessageHandler(filters.TEXT & ~filters.COMMAND, chosen_name)],
            ADDING:         [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_schedule)],
            FIXING:         [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_schedule)],
            CHOOSING_MONTH: [MessageHandler(filters.TEXT & ~filters.COMMAND, chosen_month)],
        },
        fallbacks=[CommandHandler("start", cmd_start)],
        allow_reentry=True,
    )
    app.add_handler(conv)

    # Напоминания каждый день в 12:00 (с 5 по 10 число — проверяется внутри функции)
    app.job_queue.run_daily(
        send_reminders,
        time=dtime(12, 0, 0),
        name="daily_reminder"
    )

    print("Бот запущен ✅")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
