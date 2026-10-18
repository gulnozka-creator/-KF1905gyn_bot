"""
Telegram-бот для сбора расписания врачей — Клиника Фомина 1905
Гинекологическое отделение
v2.0 — с разделением основных/флекс, модерацией конфликтов, ручным открытием периодов
"""

import os, re, json, logging
import requests
from datetime import datetime, date, time as dtime
from telegram import (
    Update, ReplyKeyboardMarkup, ReplyKeyboardRemove,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes, ConversationHandler
)

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)

BOT_TOKEN      = os.environ.get("BOT_TOKEN", "")
SCRIPT_URL     = os.environ.get("SCRIPT_URL", "")
ADMIN_CHAT_ID  = int(os.environ.get("ADMIN_CHAT_ID", "303052844"))  # Елдашова Гульноза

# ─── Врачи ────────────────────────────────────────────────────────────────────

MAIN_DOCTORS = [
    "Елдашова Гульноза",
    "Ланько Анастасия",
    "Белогурова Екатерина",
    "Малышева Яна",
    "Райкова Анастасия",
    "Астаповская Ольга",
    "Савенко Юлия",
    "Глоба Юлия",
    "Слепцова Дарья",
    "Дубинин Андрей",
    "Орлов Олег",
    "Завьялова Инна",
    "Савоськина Лидия",
    "Чернушенко Артем",
]

FLEX_DOCTORS = [
    "Семенова Милена",
    "Голубкова Наталья",
    "Фомина Мария",
    "Ахмедова Дженнет",
    "Тарасенко Юлия",
    "Стыкин Ярослав",
    "Ростовцева Оксана",
    "Кондаков Илья",
    "Бехтева Марина",
    "Маркарьян Даниил",
    "Лукьянов Александр",
    "Саградян Карина",
    "Мехедова Ксения",
    "Ключникова Юлия",
    "Кочкарева Нина",
    "Бестаева Диана",
]

ALL_DOCTORS     = MAIN_DOCTORS + FLEX_DOCTORS
ALL_DOCTORS_SET = set(ALL_DOCTORS)
MAIN_SET        = set(MAIN_DOCTORS)
FLEX_SET        = set(FLEX_DOCTORS)

# ─── Периоды ──────────────────────────────────────────────────────────────────

PERIODS = {
    "dec2026":  {"label": "Декабрь 2026",            "month": 12, "year": 2026,
                 "date_from": "01.12", "date_to": "31.12"},
    "jan2027h": {"label": "Праздники 1–10 января 2027", "month": 1,  "year": 2027,
                 "date_from": "01.01", "date_to": "10.01"},
}

# ─── Месяцы ───────────────────────────────────────────────────────────────────

MONTH_RU = {
    1:"Январь", 2:"Февраль", 3:"Март", 4:"Апрель",
    5:"Май", 6:"Июнь", 7:"Июль", 8:"Август",
    9:"Сентябрь", 10:"Октябрь", 11:"Ноябрь", 12:"Декабрь"
}
MONTH_GEN = {
    1:"январь", 2:"февраль", 3:"март", 4:"апрель",
    5:"май", 6:"июнь", 7:"июль", 8:"август",
    9:"сентябрь", 10:"октябрь", 11:"ноябрь", 12:"декабрь"
}

def current_and_next():
    today = date.today()
    y, m = today.year, today.month
    if m == 12:
        return (y, m), (y + 1, 1)
    return (y, m), (y, m + 1)

def month_label(y: int, m: int) -> str:
    return f"{MONTH_RU[m]} {y}"

# ─── Кнопки общие ─────────────────────────────────────────────────────────────

BTN_ADD    = "➕ Добавить расписание"
BTN_FIX    = "✏️ Исправить расписание"
BTN_PLAN   = "📅 Моё расписание"
BTN_CHANGE = "🔄 Сменить врача"
BTN_BACK   = "← Назад"
BTN_CANCEL = "✖ Отмена"
BTN_DONE   = "✅ Готово"

MAIN_KB = ReplyKeyboardMarkup(
    [[BTN_ADD], [BTN_FIX, BTN_PLAN], [BTN_CHANGE]],
    resize_keyboard=True
)
CANCEL_KB = ReplyKeyboardMarkup([[BTN_CANCEL]], resize_keyboard=True)

def doctors_kb(doctor_list=None):
    docs = doctor_list or ALL_DOCTORS
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

(
    CHOOSING_NAME,
    ADDING,
    FIXING,
    CHOOSING_MONTH,

    # Флекс-флоу
    FLEX_CHOOSING_DATES,
    FLEX_ENTERING_START,
    FLEX_ENTERING_END,
    FLEX_CONFIRM,

    # Админ
    ADMIN_OPEN_PERIOD,
    ADMIN_CONFLICT_WAIT,
) = range(10)

# ─── Apps Script ──────────────────────────────────────────────────────────────

_cache: dict[int, str] = {}

def _call(action: str, **kwargs):
    if not SCRIPT_URL:
        return {"ok": False}
    try:
        r = requests.get(
            SCRIPT_URL,
            params={"action": action, "data": json.dumps(kwargs, ensure_ascii=False)},
            timeout=15
        )
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

def save_schedule(doctor: str, date_str: str, start: str, end: str,
                  raw: str, period_key: str = "") -> bool:
    ts = datetime.now().strftime("%d.%m.%Y %H:%M")
    res = _call("save_schedule", timestamp=ts, doctor=doctor, date=date_str,
                start=start, end=end, raw=raw, period=period_key)
    return bool(res.get("updated"))

def get_submitted_main(period_key: str) -> set:
    res = _call("get_submitted_doctors", period=period_key)
    return set(res.get("doctors", [])) if res.get("ok") else set()

def get_submitted_flex(period_key: str) -> set:
    res = _call("get_submitted_flex", period=period_key)
    return set(res.get("doctors", [])) if res.get("ok") else set()

def get_all_registered() -> list:
    res = _call("get_all_doctors")
    return res.get("rows", []) if res.get("ok") else []

def get_red_yellow_days(period_key: str) -> dict:
    """Возвращает {'red': ['03.12', ...], 'yellow': ['07.12', ...]}"""
    res = _call("get_red_yellow_days", period=period_key)
    if res.get("ok"):
        return {"red": res.get("red", []), "yellow": res.get("yellow", [])}
    return {"red": [], "yellow": []}

def check_conflicts(period_key: str) -> list:
    """Возвращает список конфликтов [{'date': '05.12', 'cab': 'Каб.20', 'doctors': [...], 'times': [...]}]"""
    res = _call("check_conflicts", period=period_key)
    return res.get("conflicts", []) if res.get("ok") else []

def mark_period_open(period_key: str):
    _call("mark_period_open", period=period_key)

def get_open_period() -> str | None:
    """Возвращает ключ текущего открытого периода или None."""
    res = _call("get_open_period")
    return res.get("period") if res.get("ok") else None

# ─── Парсер расписания ────────────────────────────────────────────────────────

_MONTHS_P = {
    "янв":1,"фев":2,"мар":3,"апр":4,"май":5,"июн":6,
    "июл":7,"авг":8,"сент":9,"сен":9,"окт":10,"ноя":11,"дек":12
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

def parse_time_str(s: str):
    """'10:00' → (10, 0) или None"""
    s = s.strip()
    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.match(r"^(\d{1,2})$", s)
    if m:
        return int(m.group(1)), 0
    return None

# ─── Уведомление координатора ─────────────────────────────────────────────────

async def notify_admin(app: Application, text: str, reply_markup=None):
    try:
        await app.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=text,
            parse_mode="HTML",
            reply_markup=reply_markup
        )
    except Exception as e:
        logging.error(f"notify_admin: {e}")

# ─── /start ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    # Координатор
    if uid == ADMIN_CHAT_ID:
        await update.message.reply_text(
            "👩‍💼 <b>Панель координатора</b>\n\n"
            "/открыть — открыть сбор расписания\n"
            "/статус — кто подал / не подал\n"
            "/конфликты — проверить конфликты\n"
            "/напомнить — разослать напоминание сейчас",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END

    name = get_doctor(uid)
    if name:
        await update.message.reply_text(
            f"Привет, {name.split()[0]}! 👋 Что хотите сделать?",
            reply_markup=MAIN_KB
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "👋 Привет! Это бот Клиники Фомина 1905.\n\nВыберите себя из списка:",
        reply_markup=doctors_kb()
    )
    return CHOOSING_NAME

# ─── Выбор врача ──────────────────────────────────────────────────────────────

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

# ─── Добавить расписание ──────────────────────────────────────────────────────

async def btn_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    doctor = get_doctor(update.effective_user.id)
    if not doctor:
        await update.message.reply_text("Сначала выберите себя — нажмите /start")
        return ConversationHandler.END

    period_key = get_open_period()
    if not period_key:
        await update.message.reply_text(
            "⏳ Сбор расписания пока не открыт. Координатор откроет его и вы получите уведомление."
        )
        return ConversationHandler.END

    period = PERIODS.get(period_key, {})
    ctx.user_data["period_key"] = period_key
    ctx.user_data["is_fix"] = False

    # Флекс-врачи → отдельный флоу
    if doctor in FLEX_SET:
        return await _start_flex_flow(update, ctx, doctor, period_key, period)

    # Основной врач
    await update.message.reply_text(
        f"📅 Открыт сбор расписания: <b>{period.get('label', '')}</b>\n\n"
        "Отправьте расписание — даты через запятую или каждую с новой строки:\n"
        "• <b>15.12 10:00–14:40</b>\n"
        "• <b>01.12 09:00-15:00, 08.12 09:00-15:00</b>",
        parse_mode="HTML",
        reply_markup=CANCEL_KB
    )
    return ADDING

# ─── Флекс-флоу: выбор дат ───────────────────────────────────────────────────

async def _start_flex_flow(update, ctx, doctor, period_key, period):
    ryd = get_red_yellow_days(period_key)
    red    = ryd.get("red", [])
    yellow = ryd.get("yellow", [])

    if not red and not yellow:
        await update.message.reply_text(
            "✅ Отличные новости — все дни уже закрыты! Помощь не нужна.",
            reply_markup=MAIN_KB
        )
        return ConversationHandler.END

    ctx.user_data["flex_selected"]  = []   # [{date, slot}]
    ctx.user_data["flex_pending"]   = []   # список дат для ввода времени
    ctx.user_data["flex_entries"]   = []   # [{date, start, end}]
    ctx.user_data["flex_cur_date"]  = None
    ctx.user_data["flex_cur_start"] = None

    # Формируем текст
    lines = [f"📋 <b>{period.get('label', '')} — нужна помощь:</b>\n"]
    if red:
        lines.append("🔴 <b>Красные дни</b> (нет гинеколога):")
        lines.extend(f"  • {d}" for d in red)
    if yellow:
        lines.append("\n🟡 <b>Жёлтые дни</b> (1 гинеколог):")
        lines.extend(f"  • {d}" for d in yellow)
    lines.append("\nВыбери дни, когда можешь выйти:")

    # Кнопки: дата + утро / дата + вечер
    all_days = red + yellow
    buttons  = []
    for d in all_days:
        buttons.append([
            InlineKeyboardButton(f"🌅 {d} утро", callback_data=f"flex_date:{d}:утро"),
            InlineKeyboardButton(f"🌆 {d} вечер", callback_data=f"flex_date:{d}:вечер"),
        ])
    buttons.append([InlineKeyboardButton("✅ Готово с выбором", callback_data="flex_done_select")])

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return FLEX_CHOOSING_DATES

async def flex_date_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "flex_done_select":
        selected = ctx.user_data.get("flex_selected", [])
        if not selected:
            await query.answer("Выберите хотя бы один день!", show_alert=True)
            return FLEX_CHOOSING_DATES

        # Начинаем собирать время
        ctx.user_data["flex_pending"] = list(selected)
        ctx.user_data["flex_entries"] = []
        return await _ask_next_start(update, ctx)

    if data.startswith("flex_date:"):
        _, d, slot = data.split(":", 2)
        selected = ctx.user_data.setdefault("flex_selected", [])

        item = {"date": d, "slot": slot}
        # Toggle
        existing = next((x for x in selected if x["date"] == d and x["slot"] == slot), None)
        if existing:
            selected.remove(existing)
            await query.answer(f"❌ {d} {slot} убран")
        else:
            selected.append(item)
            await query.answer(f"✅ {d} {slot} выбран")
        return FLEX_CHOOSING_DATES

    return FLEX_CHOOSING_DATES

async def _ask_next_start(update, ctx):
    pending = ctx.user_data.get("flex_pending", [])
    if not pending:
        return await _flex_show_summary(update, ctx)

    item = pending[0]
    ctx.user_data["flex_cur_date"] = item["date"]
    ctx.user_data["flex_cur_slot"] = item["slot"]

    slot_hint = "09:00 или 10:00" if item["slot"] == "утро" else "15:00 или 16:00"
    msg = (
        f"📅 <b>{item['date']} ({item['slot']})</b>\n\n"
        f"Во сколько начнёшь? (например: {slot_hint})"
    )
    if update.callback_query:
        await update.callback_query.message.reply_text(msg, parse_mode="HTML",
                                                        reply_markup=CANCEL_KB)
    else:
        await update.message.reply_text(msg, parse_mode="HTML", reply_markup=CANCEL_KB)
    return FLEX_ENTERING_START

async def flex_enter_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        await update.message.reply_text("Отменено.", reply_markup=MAIN_KB)
        return ConversationHandler.END

    parsed = parse_time_str(text)
    if not parsed:
        await update.message.reply_text(
            "Не понял время 🤔 Введи в формате <b>10:00</b> или <b>10</b>",
            parse_mode="HTML"
        )
        return FLEX_ENTERING_START

    h, mi = parsed
    ctx.user_data["flex_cur_start"] = f"{h:02d}:{mi:02d}"

    cur_date = ctx.user_data["flex_cur_date"]
    cur_slot = ctx.user_data["flex_cur_slot"]
    slot_hint = "14:00 или 15:00" if cur_slot == "утро" else "20:00 или 21:00"

    await update.message.reply_text(
        f"📅 <b>{cur_date} ({cur_slot})</b> — начало: {ctx.user_data['flex_cur_start']}\n\n"
        f"Во сколько заканчиваешь? (например: {slot_hint})",
        parse_mode="HTML"
    )
    return FLEX_ENTERING_END

async def flex_enter_end(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BTN_CANCEL:
        await update.message.reply_text("Отменено.", reply_markup=MAIN_KB)
        return ConversationHandler.END

    parsed = parse_time_str(text)
    if not parsed:
        await update.message.reply_text(
            "Не понял время 🤔 Введи в формате <b>14:40</b> или <b>15</b>",
            parse_mode="HTML"
        )
        return FLEX_ENTERING_END

    h, mi = parsed
    end_str   = f"{h:02d}:{mi:02d}"
    start_str = ctx.user_data["flex_cur_start"]
    cur_date  = ctx.user_data["flex_cur_date"]

    ctx.user_data["flex_entries"].append({
        "date": cur_date,
        "start": start_str,
        "end": end_str,
    })
    ctx.user_data["flex_pending"].pop(0)

    return await _ask_next_start(update, ctx)

async def _flex_show_summary(update, ctx):
    entries = ctx.user_data.get("flex_entries", [])
    doctor  = get_doctor(update.effective_user.id if update.effective_user else
                         update.callback_query.from_user.id)

    lines = ["📋 <b>Проверь расписание:</b>\n"]
    for e in entries:
        lines.append(f"• {e['date']} — {e['start']}–{e['end']}")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Подтвердить", callback_data="flex_confirm")],
        [InlineKeyboardButton("✏️ Начать заново", callback_data="flex_restart")],
    ])

    msg = update.callback_query.message if update.callback_query else update.message
    await msg.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)
    return FLEX_CONFIRM

async def flex_confirm_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    if data == "flex_restart":
        doctor     = get_doctor(query.from_user.id)
        period_key = ctx.user_data.get("period_key", get_open_period())
        period     = PERIODS.get(period_key, {})
        return await _start_flex_flow(update, ctx, doctor, period_key, period)

    if data == "flex_confirm":
        doctor     = get_doctor(query.from_user.id)
        period_key = ctx.user_data.get("period_key", get_open_period())
        entries    = ctx.user_data.get("flex_entries", [])

        # Сохраняем в Sheets
        for e in entries:
            save_schedule(doctor, e["date"], e["start"], e["end"],
                          f"{e['date']} {e['start']}-{e['end']}", period_key)

        await query.message.reply_text(
            "✅ Расписание отправлено координатору. После проверки ты получишь уведомление.",
            reply_markup=MAIN_KB
        )

        # Уведомляем координатора
        period = PERIODS.get(period_key, {})
        lines  = [f"📋 <b>{doctor}</b> подала(л) расписание ({period.get('label', '')}):\n"]
        for e in entries:
            lines.append(f"• {e['date']} — {e['start']}–{e['end']}")
        await notify_admin(ctx.application, "\n".join(lines))

        # Пересчитываем и уведомляем остальных флекс-врачей
        ctx.application.create_task(
            _notify_remaining_flex(ctx.application, period_key, skip_doctor=doctor)
        )
        return ConversationHandler.END

    return FLEX_CONFIRM

async def _notify_remaining_flex(app: Application, period_key: str, skip_doctor: str):
    """После подачи одного флекс-врача шлём обновлённый список остальным."""
    import asyncio
    await asyncio.sleep(2)  # небольшая задержка чтобы Sheets успел обновиться

    ryd    = get_red_yellow_days(period_key)
    red    = ryd.get("red", [])
    yellow = ryd.get("yellow", [])

    if not red and not yellow:
        return  # все закрыто — не беспокоим

    period    = PERIODS.get(period_key, {})
    submitted = get_submitted_flex(period_key)
    all_reg   = get_all_registered()

    lines = [f"🔄 <b>Обновление — {period.get('label', '')}:</b>\n"]
    if red:
        lines.append("🔴 Ещё нужна помощь (нет гинеколога):")
        lines.extend(f"  • {d}" for d in red)
    if yellow:
        lines.append("🟡 Можно усилить (1 гинеколог):")
        lines.extend(f"  • {d}" for d in yellow)
    lines.append("\nЕсли можешь выйти — нажми /start → ➕ Добавить расписание")

    text = "\n".join(lines)

    for row in all_reg:
        try:
            tg_id = int(row[0])
            fio   = str(row[1])
            if fio == skip_doctor:
                continue
            if fio not in FLEX_SET:
                continue
            if fio in submitted:
                continue
            await app.bot.send_message(chat_id=tg_id, text=text, parse_mode="HTML")
        except Exception as e:
            logging.warning(f"_notify_remaining_flex {row}: {e}")

# ─── Приём расписания (основные врачи) ───────────────────────────────────────

async def receive_schedule(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text   = update.message.text.strip()
    doctor = get_doctor(update.effective_user.id)
    is_fix = ctx.user_data.get("is_fix", False)

    if text == BTN_CANCEL:
        await update.message.reply_text("Отменено.", reply_markup=MAIN_KB)
        return ConversationHandler.END

    period_key = ctx.user_data.get("period_key", get_open_period() or "")
    entries    = parse_schedule(text)

    if not entries:
        await update.message.reply_text(
            "Не смог распознать дату/время 🤔\n"
            "Попробуйте: <b>15.12 10:00–15:00</b>",
            parse_mode="HTML"
        )
        return ADDING if not is_fix else FIXING

    saved = []
    for date_str, start, end in entries:
        if date_str and start and end:
            replaced = save_schedule(doctor, date_str, start, end, text, period_key)
            mark = " (замена)" if replaced else ""
            saved.append(f"• {date_str}  ⏰ {start}–{end}{mark}")

    if not saved:
        await update.message.reply_text(
            "Не удалось разобрать записи. Попробуйте ещё раз.\nПример: <b>15.12 10:00–15:00</b>",
            parse_mode="HTML"
        )
        return ADDING if not is_fix else FIXING

    period = PERIODS.get(period_key, {})
    lines  = [f"✅ Записано {len(saved)} дн.  👤 {doctor}  📅 {period.get('label', '')}"] + saved
    await update.message.reply_text("\n".join(lines), reply_markup=MAIN_KB)

    # Уведомление координатору
    action = "✏️ Исправил(а)" if is_fix else "➕ Добавил(а)"
    admin_lines = [f"🔔 <b>{doctor}</b> {action} расписание ({period.get('label', '')}):"] + saved
    await notify_admin(ctx.application, "\n".join(admin_lines))

    return ConversationHandler.END

# ─── Исправить расписание ─────────────────────────────────────────────────────

async def btn_fix(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    doctor = get_doctor(update.effective_user.id)
    if not doctor:
        await update.message.reply_text("Сначала выберите себя — нажмите /start")
        return ConversationHandler.END

    period_key = get_open_period()
    if not period_key:
        await update.message.reply_text("Сбор расписания пока не открыт.")
        return ConversationHandler.END

    ctx.user_data["is_fix"]      = True
    ctx.user_data["period_key"]  = period_key
    period = PERIODS.get(period_key, {})

    await update.message.reply_text(
        f"Отправьте дату с новым временем — запись заменится:\n"
        f"• <b>15.12 11:00–16:00</b>\n\n"
        f"(Период: {period.get('label', '')})",
        parse_mode="HTML",
        reply_markup=CANCEL_KB
    )
    return FIXING

# ─── Моё расписание ──────────────────────────────────────────────────────────

async def btn_plan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    doctor = get_doctor(update.effective_user.id)
    if not doctor:
        await update.message.reply_text("Сначала выберите себя — нажмите /start")
        return ConversationHandler.END
    await update.message.reply_text(
        "За какой месяц показать расписание?",
        reply_markup=month_kb()
    )
    return CHOOSING_MONTH

async def chosen_month(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text   = update.message.text.strip()
    doctor = get_doctor(update.effective_user.id)

    if text == BTN_BACK:
        await update.message.reply_text(
            f"Привет, {doctor.split()[0]}! 👋 Что хотите сделать?",
            reply_markup=MAIN_KB
        )
        return ConversationHandler.END

    (cy, cm), (ny, nm) = current_and_next()
    if text == month_label(cy, cm):
        y, m = cy, cm
    elif text == month_label(ny, nm):
        y, m = ny, nm
    else:
        await update.message.reply_text(
            "Пожалуйста, выберите месяц из списка:", reply_markup=month_kb()
        )
        return CHOOSING_MONTH

    res = _call("get_last", doctor=doctor, month=m, year=y)
    if not res.get("ok") or not res.get("rows"):
        await update.message.reply_text(
            f"У вас пока нет записей за {MONTH_GEN[m]} {y}.",
            reply_markup=MAIN_KB
        )
        return ConversationHandler.END

    lines = [f"📅 <b>Расписание {doctor} — {MONTH_RU[m]} {y}:</b>"]
    for r in res["rows"]:
        lines.append(f"• {r[2]}  ⏰ {r[3]}–{r[4]}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=MAIN_KB)
    return ConversationHandler.END

# ─── Сменить врача ───────────────────────────────────────────────────────────

async def btn_change(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Выберите себя из списка:", reply_markup=doctors_kb()
    )
    return CHOOSING_NAME

# ─── АДМИН: /открыть ─────────────────────────────────────────────────────────

async def cmd_open(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Декабрь 2026",              callback_data="open:dec2026")],
        [InlineKeyboardButton("🎄 Праздники 1–10 января 2027", callback_data="open:jan2027h")],
    ])
    await update.message.reply_text(
        "Для какого периода открываем сбор расписания?",
        reply_markup=kb
    )

async def open_period_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ADMIN_CHAT_ID:
        await query.answer()
        return

    await query.answer()
    period_key = query.data.split(":", 1)[1]
    period     = PERIODS.get(period_key)
    if not period:
        await query.message.reply_text("Неизвестный период.")
        return

    mark_period_open(period_key)

    # Рассылка всем зарегистрированным врачам
    all_reg = get_all_registered()
    count   = 0
    for row in all_reg:
        try:
            tg_id = int(row[0])
            fio   = str(row[1])
            if fio not in ALL_DOCTORS_SET:
                continue
            if fio in FLEX_SET:
                continue  # флекс-врачи получат позже (после 10-го)

            icon = "🎄" if period_key == "jan2027h" else "📅"
            text = (
                f"{icon} <b>Открыт сбор расписания: {period['label']}</b>\n\n"
                f"Пожалуйста, подайте расписание через бот.\n"
                f"Нажмите ➕ <b>Добавить расписание</b>"
            )
            await ctx.bot.send_message(chat_id=tg_id, text=text, parse_mode="HTML")
            count += 1
        except Exception as e:
            logging.warning(f"open_period_callback {row}: {e}")

    await query.message.reply_text(
        f"✅ Период <b>{period['label']}</b> открыт.\n"
        f"Уведомление отправлено {count} врачам.\n\n"
        f"Напоминания будут приходить каждый день тем, кто не подал.",
        parse_mode="HTML"
    )

# ─── АДМИН: /статус ──────────────────────────────────────────────────────────

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return

    period_key = get_open_period()
    if not period_key:
        await update.message.reply_text("Нет открытого периода.")
        return

    period    = PERIODS.get(period_key, {})
    submitted = get_submitted_main(period_key)

    done     = [d for d in MAIN_DOCTORS if d in submitted]
    not_done = [d for d in MAIN_DOCTORS if d not in submitted]

    lines = [f"📊 <b>Статус — {period.get('label', '')}:</b>\n"]
    lines.append(f"✅ Подали ({len(done)}):")
    for d in done:
        lines.append(f"  • {d}")
    lines.append(f"\n⏳ Не подали ({len(not_done)}):")
    for d in not_done:
        lines.append(f"  • {d}")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

# ─── АДМИН: /конфликты ───────────────────────────────────────────────────────

async def cmd_conflicts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return

    period_key = get_open_period()
    if not period_key:
        await update.message.reply_text("Нет открытого периода.")
        return

    period    = PERIODS.get(period_key, {})
    conflicts = check_conflicts(period_key)

    if not conflicts:
        # Нет конфликтов — предлагаем отправить флекс-врачам
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📨 Отправить флекс-врачам", callback_data=f"send_flex:{period_key}")
        ]])
        await update.message.reply_text(
            f"✅ Конфликтов нет — расписание <b>{period.get('label', '')}</b> чистое!\n\n"
            "Можно открывать для флекс-врачей?",
            parse_mode="HTML",
            reply_markup=kb
        )
        return

    lines = [f"⚠️ <b>Конфликты в расписании {period.get('label', '')}:</b>\n"]
    for c in conflicts:
        lines.append(f"• <b>{c.get('date', '')} — {c.get('cab', '')}</b>")
        doctors_list = c.get("doctors", [])
        times_list   = c.get("times", [])
        for i, doc in enumerate(doctors_list):
            t = times_list[i] if i < len(times_list) else "?"
            lines.append(f"    — {doc}: {t}")

    lines.append("\n\nПожалуйста, исправьте конфликты в таблице и нажмите кнопку:")
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Готово, проверить снова", callback_data=f"recheck:{period_key}")
    ]])
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)

async def conflicts_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ADMIN_CHAT_ID:
        await query.answer()
        return

    await query.answer()
    data = query.data

    if data.startswith("recheck:"):
        period_key = data.split(":", 1)[1]
        period     = PERIODS.get(period_key, {})
        conflicts  = check_conflicts(period_key)

        if conflicts:
            lines = [f"⚠️ Ещё есть конфликты:\n"]
            for c in conflicts:
                lines.append(f"• {c.get('date', '')} — {c.get('cab', '')}")
                for i, doc in enumerate(c.get("doctors", [])):
                    t = c.get("times", [])[i] if i < len(c.get("times", [])) else "?"
                    lines.append(f"    — {doc}: {t}")
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Готово, проверить снова", callback_data=f"recheck:{period_key}")
            ]])
            await query.message.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)
        else:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("📨 Отправить флекс-врачам", callback_data=f"send_flex:{period_key}")
            ]])
            await query.message.reply_text(
                "✅ Конфликтов нет! Открываем для флекс-врачей?",
                reply_markup=kb
            )

    elif data.startswith("send_flex:"):
        period_key = data.split(":", 1)[1]
        await _send_flex_broadcast(ctx.application, period_key, query.message)

async def _send_flex_broadcast(app: Application, period_key: str, admin_msg=None):
    """Рассылаем флекс-врачам список красных/жёлтых дней."""
    ryd    = get_red_yellow_days(period_key)
    red    = ryd.get("red", [])
    yellow = ryd.get("yellow", [])
    period = PERIODS.get(period_key, {})

    if not red and not yellow:
        if admin_msg:
            await admin_msg.reply_text("✅ Все дни закрыты — флекс-врачам писать не нужно.")
        return

    lines = [f"📋 <b>{period.get('label', '')} — нужна помощь:</b>\n"]
    if red:
        lines.append("🔴 <b>Нет гинеколога:</b>")
        lines.extend(f"  • {d}" for d in red)
    if yellow:
        lines.append("\n🟡 <b>Один гинеколог (можно усилить):</b>")
        lines.extend(f"  • {d}" for d in yellow)
    lines.append("\nЕсли можешь выйти — нажми /start → ➕ Добавить расписание")

    text    = "\n".join(lines)
    all_reg = get_all_registered()
    count   = 0

    for row in all_reg:
        try:
            tg_id = int(row[0])
            fio   = str(row[1])
            if fio not in FLEX_SET:
                continue
            await app.bot.send_message(chat_id=tg_id, text=text, parse_mode="HTML")
            count += 1
        except Exception as e:
            logging.warning(f"_send_flex_broadcast {row}: {e}")

    if admin_msg:
        await admin_msg.reply_text(
            f"✅ Список отправлен {count} флекс-врачам.",
            parse_mode="HTML"
        )

# ─── АДМИН: /напомнить ───────────────────────────────────────────────────────

async def cmd_remind(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return

    period_key = get_open_period()
    if not period_key:
        await update.message.reply_text("Нет открытого периода — сначала откройте сбор (/открыть).")
        return

    period    = PERIODS.get(period_key, {})
    submitted = get_submitted_main(period_key)
    all_reg   = get_all_registered()
    count     = 0

    for row in all_reg:
        try:
            tg_id = int(row[0])
            fio   = str(row[1])
            if fio not in MAIN_SET:
                continue
            if fio in submitted:
                continue
            text = (
                f"👋 Напоминание!\n\n"
                f"Расписание на <b>{period.get('label', '')}</b> ещё не подано.\n"
                f"Пожалуйста, найдите минуту и отправьте через бот 🙏\n\n"
                f"Нажмите ➕ <b>Добавить расписание</b>"
            )
            await ctx.bot.send_message(chat_id=tg_id, text=text, parse_mode="HTML")
            count += 1
        except Exception as e:
            logging.warning(f"cmd_remind {row}: {e}")

    await update.message.reply_text(
        f"✅ Напоминание отправлено {count} врачам (кто ещё не подал)."
    )

# ─── Автонапоминания (5–10 числа) ────────────────────────────────────────────

async def send_reminders(context: ContextTypes.DEFAULT_TYPE):
    today = date.today()
    if today.day < 5 or today.day > 10:
        return

    period_key = get_open_period()
    if not period_key:
        return

    period    = PERIODS.get(period_key, {})
    submitted = get_submitted_main(period_key)
    all_reg   = get_all_registered()

    for row in all_reg:
        try:
            tg_id = int(row[0])
            fio   = str(row[1])
            if fio not in MAIN_SET:
                continue
            if fio in submitted:
                continue
            text = (
                f"👋 Добрый день!\n\n"
                f"Напоминаем: расписание на <b>{period.get('label', '')}</b> ещё не подано.\n"
                f"Пожалуйста, найдите минутку 🙏\n\n"
                f"Нажмите ➕ <b>Добавить расписание</b>"
            )
            await context.bot.send_message(chat_id=tg_id, text=text, parse_mode="HTML")
        except Exception as e:
            logging.warning(f"send_reminders {row}: {e}")

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
            CHOOSING_NAME:       [MessageHandler(filters.TEXT & ~filters.COMMAND, chosen_name)],
            ADDING:              [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_schedule)],
            FIXING:              [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_schedule)],
            CHOOSING_MONTH:      [MessageHandler(filters.TEXT & ~filters.COMMAND, chosen_month)],
            FLEX_CHOOSING_DATES: [CallbackQueryHandler(flex_date_callback)],
            FLEX_ENTERING_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, flex_enter_start)],
            FLEX_ENTERING_END:   [MessageHandler(filters.TEXT & ~filters.COMMAND, flex_enter_end)],
            FLEX_CONFIRM:        [CallbackQueryHandler(flex_confirm_callback)],
        },
        fallbacks=[CommandHandler("start", cmd_start)],
        allow_reentry=True,
    )
    app.add_handler(conv)

    # Команды координатора (вне ConversationHandler)
    app.add_handler(CommandHandler("открыть",   cmd_open))
    app.add_handler(CommandHandler("статус",    cmd_status))
    app.add_handler(CommandHandler("конфликты", cmd_conflicts))
    app.add_handler(CommandHandler("напомнить", cmd_remind))

    # Callback-кнопки координатора
    app.add_handler(CallbackQueryHandler(open_period_callback,  pattern=r"^open:"))
    app.add_handler(CallbackQueryHandler(conflicts_callback,    pattern=r"^(recheck:|send_flex:)"))

    # Автонапоминания каждый день в 12:00
    app.job_queue.run_daily(
        send_reminders,
        time=dtime(12, 0, 0),
        name="daily_reminder"
    )

    print("Бот запущен ✅")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
