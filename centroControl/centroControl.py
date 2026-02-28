# centroControl.py
# Flujo actual:
# /start -> Email -> Password -> Activo/Patrón (Manual/Auto) -> Modo (Operación Única/Stops)
# -> Monto por operación (decimal con , o .) -> Resumen -> (Lanzar / Cancelar)
#
# Lanzamiento:
# - Si (Automático + Operación Única) => ejecuta Bot_1.py con: email, password, amount
#   usando: nohup python3 -u Bot_1.py ... > logs/Bot_1_<timestamp>.log 2>&1 & echo $!
# - Si no, no lanza (placeholder para más preguntas)

import os
import logging
import shlex
import subprocess
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

load_dotenv(dotenv_path=Path(__file__).with_name(".env"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("centroControl")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
ALLOWED_CHATS_RAW = os.getenv("ALLOWED_CHATS", "").strip()
ALLOWED_CHATS = {int(x.strip()) for x in ALLOWED_CHATS_RAW.split(",") if x.strip().isdigit()}

# Ruta del script a lanzar en VPS
BASE_DIR = Path(__file__).parent.resolve()
BOT1_PATH = str(BASE_DIR / "Bot_1.py")

if not TELEGRAM_TOKEN:
    raise RuntimeError("Falta TELEGRAM_TOKEN en el .env")

IQ_EMAIL, IQ_PASSWORD, PICK_INPUT_MODE, WORK_MODE, AMOUNT, CONFIRM = range(6)


def _is_allowed(update: Update) -> bool:
    chat_id = update.effective_chat.id if update.effective_chat else None
    if not ALLOWED_CHATS:
        return True
    return chat_id in ALLOWED_CHATS if chat_id is not None else False


async def _safe_delete_message_by_id(chat_id: int, message_id: int, context: ContextTypes.DEFAULT_TYPE):
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


async def _safe_delete_user_message(update: Update):
    try:
        if update.message:
            await update.message.delete()
    except Exception:
        pass


async def _delete_last_bot_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id if update.effective_chat else None
    last_id = context.user_data.get("_last_bot_msg_id")
    if chat_id and last_id:
        await _safe_delete_message_by_id(chat_id, int(last_id), context)
    context.user_data["_last_bot_msg_id"] = None


async def _send_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None):
    msg = await update.effective_chat.send_message(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup,
    )
    context.user_data["_last_bot_msg_id"] = msg.message_id
    return msg


def _input_mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Introducir Activo/Patrón Manual", callback_data="pick:manual")],
            [InlineKeyboardButton("Buscar Automático", callback_data="pick:auto")],
        ]
    )


def _work_mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Operacion Unica", callback_data="mode:single")],
            [InlineKeyboardButton("Usar Stops", callback_data="mode:stops")],
        ]
    )


def _confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🚀 Lanzar Bot", callback_data="confirm:launch")],
            [InlineKeyboardButton("❌ Cancelar", callback_data="confirm:cancel")],
        ]
    )


def _parse_amount(text: str):
    """
    Acepta: 2 | 1,5 | 1.5
    Rechaza: letras, múltiples puntos, 0 o negativos.
    Devuelve float.
    """
    s = (text or "").strip()
    s = s.replace(" ", "").replace(",", ".")
    if not s:
        return None
    if any(c not in "0123456789." for c in s) or s.count(".") > 1:
        return None
    try:
        val = float(s)
    except Exception:
        return None
    if val <= 0:
        return None
    return val


def _amount_to_str(val: float) -> str:
    # Argumento para el script: usa punto.
    return f"{val:.10f}".rstrip("0").rstrip(".")


def _build_summary_md(user_data: dict) -> str:
    email = user_data.get("iq_email") or "-"
    pick_mode = user_data.get("pick_input_mode") or "-"
    work_mode = user_data.get("work_mode") or "-"
    amount = user_data.get("amount")
    amount_txt = (f"{amount}".replace(".", ",") if amount is not None else "-")

    return (
        "✅ **Resumen de configuración**\n\n"
        f"- **Email:** `{email}`\n"
        f"- **Activo/Patrón:** **{pick_mode}**\n"
        f"- **Modo de trabajo:** **{work_mode}**\n"
        f"- **Monto por operación:** **{amount_txt}**\n\n"
        "¿Qué hacemos?"
    )


def _launch_bot1(email: str, password: str, amount: float):
    """
    Lanza Bot_1.py en background usando nohup y python3 -u
    Logs en la misma carpeta.
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = str(BASE_DIR / f"Bot_1_{ts}.log")

    q_email = shlex.quote(email)
    q_pass = shlex.quote(password)
    q_amount = shlex.quote(_amount_to_str(amount))
    q_script = shlex.quote(BOT1_PATH)
    q_log = shlex.quote(log_file)

    cmd = f"nohup python3 -u {q_script} {q_email} {q_pass} {q_amount} > {q_log} 2>&1 & echo $!"
    out = subprocess.check_output(["bash", "-lc", cmd], text=True).strip()
    pid = int(out)

    return pid, log_file


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        await update.message.reply_text("No autorizado.")
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data["iq_email"] = None
    context.user_data["iq_password"] = None
    context.user_data["pick_input_mode"] = None
    context.user_data["work_mode"] = None
    context.user_data["amount"] = None
    context.user_data["_last_bot_msg_id"] = None

    await _safe_delete_user_message(update)
    await _send_prompt(update, context, "Ingresá tu **email** de IQ Option:")
    return IQ_EMAIL


async def capture_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return ConversationHandler.END

    await _delete_last_bot_prompt(update, context)

    email = (update.message.text or "").strip()
    await _safe_delete_user_message(update)

    if not email or "@" not in email:
        await _send_prompt(update, context, "Email inválido. Probá de nuevo:")
        return IQ_EMAIL

    context.user_data["iq_email"] = email
    await _send_prompt(update, context, "Ingresá tu **contraseña** de IQ Option:")
    return IQ_PASSWORD


async def capture_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return ConversationHandler.END

    await _delete_last_bot_prompt(update, context)

    pwd = (update.message.text or "").strip()
    await _safe_delete_user_message(update)

    if not pwd:
        await _send_prompt(update, context, "Contraseña vacía. Probá de nuevo:")
        return IQ_PASSWORD

    context.user_data["iq_password"] = pwd
    await _send_prompt(
        update,
        context,
        "¿Cómo vamos a definir **Activo/Patrón**?",
        reply_markup=_input_mode_keyboard(),
    )
    return PICK_INPUT_MODE


async def choose_pick_input_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return ConversationHandler.END

    await _delete_last_bot_prompt(update, context)

    query = update.callback_query
    await query.answer()

    data = query.data or ""
    if data == "pick:manual":
        context.user_data["pick_input_mode"] = "Manual"
    elif data == "pick:auto":
        context.user_data["pick_input_mode"] = "Automático"
    else:
        await _send_prompt(update, context, "Opción inválida. Elegí de nuevo:", reply_markup=_input_mode_keyboard())
        return PICK_INPUT_MODE

    await _send_prompt(
        update,
        context,
        "Elegí el **modo de trabajo**:",
        reply_markup=_work_mode_keyboard(),
    )
    return WORK_MODE


async def choose_work_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return ConversationHandler.END

    await _delete_last_bot_prompt(update, context)

    query = update.callback_query
    await query.answer()

    data = query.data or ""
    if data == "mode:single":
        context.user_data["work_mode"] = "Operacion Unica"
    elif data == "mode:stops":
        context.user_data["work_mode"] = "Usar Stops"
    else:
        await _send_prompt(update, context, "Opción inválida. Elegí de nuevo:", reply_markup=_work_mode_keyboard())
        return WORK_MODE

    await _send_prompt(update, context, "Ingresá el **monto por operación** (ej: `1,5` o `1.5`):")
    return AMOUNT


async def capture_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return ConversationHandler.END

    await _delete_last_bot_prompt(update, context)

    raw = (update.message.text or "").strip()
    await _safe_delete_user_message(update)

    val = _parse_amount(raw)
    if val is None:
        await _send_prompt(update, context, "Monto inválido. Poné un número > 0 (ej: `2`, `1,5`, `1.5`):")
        return AMOUNT

    context.user_data["amount"] = val

    await _send_prompt(
        update,
        context,
        _build_summary_md(context.user_data),
        reply_markup=_confirm_keyboard(),
    )
    return CONFIRM


async def confirm_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return ConversationHandler.END

    await _delete_last_bot_prompt(update, context)

    query = update.callback_query
    await query.answer()

    data = query.data or ""
    if data == "confirm:cancel":
        context.user_data.clear()
        await query.message.chat.send_message("Cancelado.")
        return ConversationHandler.END

    if data != "confirm:launch":
        await _send_prompt(update, context, "Opción inválida.", reply_markup=_confirm_keyboard())
        return CONFIRM

    pick_mode = context.user_data.get("pick_input_mode")
    work_mode = context.user_data.get("work_mode")

    # Solo lanzamos Bot_1.py en esta combinación:
    if pick_mode == "Automático" and work_mode == "Operacion Unica":
        email = context.user_data.get("iq_email") or ""
        password = context.user_data.get("iq_password") or ""
        amount = context.user_data.get("amount")

        if not email or not password or amount is None:
            await query.message.chat.send_message("Faltan datos para lanzar (email/pass/monto).")
            return ConversationHandler.END

        try:
            pid, log_file = _launch_bot1(email=email, password=password, amount=float(amount))
            # No mostramos contraseña. Solo PID y log.
            await query.message.chat.send_message(
                f"✅ Bot_1 lanzado.\nPID: {pid}\nLog: {log_file}"
            )
        except Exception as e:
            await query.message.chat.send_message(f"❌ Error lanzando Bot_1: {e}")
        finally:
            # Limpia RAM
            context.user_data.clear()

        return ConversationHandler.END

    # Cualquier otra combinación: placeholder para seguir añadiendo preguntas
    await query.message.chat.send_message(
        "Esta combinación requiere más preguntas. (Seguimos aquí en el siguiente paso)."
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    if update.message:
        await update.message.reply_text("Cancelado.")
    return ConversationHandler.END


def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            IQ_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, capture_email)],
            IQ_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, capture_password)],
            PICK_INPUT_MODE: [CallbackQueryHandler(choose_pick_input_mode, pattern=r"^pick:(manual|auto)$")],
            WORK_MODE: [CallbackQueryHandler(choose_work_mode, pattern=r"^mode:(single|stops)$")],
            AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, capture_amount)],
            CONFIRM: [CallbackQueryHandler(confirm_action, pattern=r"^confirm:(launch|cancel)$")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()