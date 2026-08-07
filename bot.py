"""
Telegram-бот для сбора расписания врачей — Гинекологи Рассвет
Данные сохраняются через Google Apps Script (не нужен service account).
"""

import os, re, logging, requests
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)

BOT_TOKEN   = os.environ.get("BOT_TOKEN", "7993383549:AAE8TWlgULmginVsQY5VZ8eRYwJxNGjCAUg")
SCRIPT_URL  = os.environ.get("SCRIPT_URL", "")   # заполнится после деплоя Apps Script

# ─── Локальный кэш (основное хранилище) ──────────────────────────────────────
_doctors: dict[int, str] = {}   # user_id → ФИО

# ─── Работа с Google Sheets через Apps Script (дополнительно) ─────────────────

def _post(action: str, **kwargs):
    if not SCRIPT_URL:
        return {"ok": False, "error": "no SCRIPT_URL"}
    payload = {"action": action, **kwargs}
    try:
        s = requests.Session()
        r = s.post(SCRIPT_URL, json=payload, timeout=15, allow_redirects=False)
        if r.status_code in (301, 302, 303, 307, 308):
            redirect_url = r.headers.get("Location", SCRIPT_URL)
            r = s.post(redirect_url, json=payload, timeout=15)
        return r.json()
    except Exception as e:
        logging.error(f"_post({action}) error: {e}")
        return {"ok": False, "error": str(e)}

def get_doctor(user_id: int) -> str | None:
    if user_id in _doctors:
        return _doctors[user_id]
    res = _post("get_doctor", telegram_id=user_id)
    fio = res.get("fio") if res.get("ok") else None
    if fio:
        _doctors[user_id] = fio
    return fio

def save_doctor(user_id: int, tg_name: str, fio: str):
    _doctors[user_id] = fio   # всегда в памяти
    _post("save_doctor", telegram_id=user_id, tg_name=tg_name, fio=fio)  # попытка в Sheets

def save_schedule(doctor, date_str, start, end, cabinet, raw):
    _post("save_schedule",
          timestamp=datetime.now().strftime("%d.%m.%Y %H:%M"),
          doctor=doctor, date=date_str, start=start,
          end=end, cabinet=cabinet, raw=raw)

def get_last(doctor: str):
    res = _post("get_last", doctor=doctor)
    return res.get("rows", []) if res.get("ok") else []

# ─── Парсер расписания ────────────────────────────────────────────────────────
_MONTHS = {
    "янв":1,"фев":2,"мар":3,"апр":4,"май":5,"июн":6,
    "июл":7,"авг":8,"сент":9,"сен":9,"окт":10,"ноя":11,"ноябр":11,"дек":12
}

def parse_schedule(text: str):
    low = text.lower()
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
            m = re.search(r"\b(\d{1,2})\s*[-–]\s*(\d{1,2})\b", text)
            if m and int(m.group(1)) < 24 and int(m.group(2)) < 24:
                start = f"{int(m.group(1)):02d}:00"
                end   = f"{int(m.group(2)):02d}:00"

    # Кабинет
    cabinet = None
    m = re.search(r"(?:каб(?:инет)?\.?\s*|к\.?\s*)(\d+)", low)
    if m:
        cabinet = f"Каб.{m.group(1)}"

    return date_str, start, end, cabinet

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
            "Отправьте своё расписание:\n"
            "• <b>15.09 10:00–15:00 каб20</b>\n"
            "• <b>15 сентября с 10 до 15 кабинет 21</b>\n\n"
            "/myname — изменить имя  |  /last — последние записи",
            parse_mode="HTML"
        )
        return ConversationHandler.END
    await update.message.reply_text(
        "👋 Привет! Бот клиники <b>Гинекологи Рассвет</b>.\n\n"
        "Введите ваши <b>Фамилию Имя Отчество</b> для расписания:",
        parse_mode="HTML"
    )
    return ASK_NAME

async def got_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    fio = update.message.text.strip()
    save_doctor(update.effective_user.id, update.effective_user.full_name, fio)
    await update.message.reply_text(
        f"✅ Записан как: <b>{fio}</b>\n\n"
        "Теперь присылайте расписание:\n"
        "• <b>15.09 10:00–15:00 каб20</b>",
        parse_mode="HTML"
    )
    return ConversationHandler.END

async def cmd_myname(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введите новое ФИО:")
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
    lines = ["📋 <b>Ваши последние записи:</b>"]
    for r in rows:
        lines.append(f"• {r[2]} {r[3]}–{r[4]} {r[5]}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid    = update.effective_user.id
    text   = update.message.text.strip()
    doctor = get_doctor(uid)

    if not doctor:
        await update.message.reply_text("Сначала представьтесь — напишите /start")
        return ConversationHandler.END

    date_str, start, end, cabinet = parse_schedule(text)

    if not date_str and not start:
        await update.message.reply_text(
            "Не смог распознать 🤔\n\n"
            "Примеры:\n"
            "• <b>15.09 10:00–15:00 каб20</b>\n"
            "• <b>15 сентября с 10 до 15 кабинет 21</b>",
            parse_mode="HTML"
        )
        return ConversationHandler.END

    if cabinet:
        save_schedule(doctor, date_str or "?", start or "?", end or "?", cabinet, text)
        await update.message.reply_text(
            f"✅ Записано!\n👤 {doctor}\n📅 {date_str}  ⏰ {start}–{end}\n🚪 {cabinet}"
        )
        return ConversationHandler.END

    ctx.user_data["pending"] = (doctor, date_str, start, end, text)
    await update.message.reply_text("Какой кабинет?", reply_markup=_CAB_KB)
    return ASK_CABINET

async def got_cabinet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw_cab = update.message.text.strip()
    pending = ctx.user_data.pop("pending", None)
    if not pending:
        await update.message.reply_text("Отправьте расписание заново.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END
    doctor, date_str, start, end, raw = pending
    m = re.search(r"\d+", raw_cab)
    cabinet = f"Каб.{m.group()}" if m else raw_cab
    save_schedule(doctor, date_str or "?", start or "?", end or "?", cabinet, raw)
    await update.message.reply_text(
        f"✅ Записано!\n👤 {doctor}\n📅 {date_str}  ⏰ {start}–{end}\n🚪 {cabinet}",
        reply_markup=ReplyKeyboardRemove()
    )
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Бот запущен ✅")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
