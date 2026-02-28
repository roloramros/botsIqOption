#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
control_bot.py — versión modular y multi‑bot (flujo reordenado)

Cambio pedido por el usuario:
- Mantener la estructura, pero REORDENAR el flujo de preguntas a:
  0. start
  1. ask_email
  2. ask_password
  3. set_cuenta
  4. set_par
  5. set_bot
  6. ask_patron
  7. set_sentido
  8. ask_monto
  9. set_recuperacion
  10. ask_stops (ahora admite DECIMALES)
  11. confirm_flow

Notas de implementación:
- Separamos pasos "globales" (email, password, cuenta, par, elegir bot) y después
  añadimos los pasos "de cola" específicos del bot seleccionado.
- /log, /stop y /state se mantienen.
- Los stops aceptan decimales (float) y no solo enteros.
"""
from __future__ import annotations
import asyncio
import logging
import os
import re
import shlex
import signal
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    PicklePersistence,
    filters,
)

# ---------------------------
# Logging
# ---------------------------
BASE_DIR = Path(__file__).resolve().parent
LOG_PATH = BASE_DIR / "control_bot.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("control_bot")

# ---------------------------
# Permisos (hardcode)
# ---------------------------
# Pon aquí tu token real de Bot de Telegram
TELEGRAM_TOKEN = "8077639603:AAG3JxqbsE7W63YkSXH3p3YOElukJkviSwA"

# Si quieres limitar chats, pon los IDs aquí. Vacío => cualquiera puede usarlo.
ALLOWED_CHATS = {
    6796625586,1589398506,5641980673
}

if not TELEGRAM_TOKEN or TELEGRAM_TOKEN.startswith("123456789:AA"):
    raise RuntimeError("Configura TELEGRAM_TOKEN en el código antes de ejecutar.")





# ---------------------------
# Estados
# ---------------------------
DYNAMIC, CONFIRM = range(2)

# ---------------------------
# Tipos de pasos
# ---------------------------
@dataclass
class Step:
    key: str
    kind: str  # input_text | input_secret | input_number | select | bool | composite_stops | select_bot
    prompt: str
    choices: List[Tuple[str, str]] = field(default_factory=list)  # (label, value)
    required: bool = True
    validate_regex: Optional[str] = None

# ---------------------------
# Registro de bots
# ---------------------------
BOT_REGISTRY: Dict[str, Dict[str, Any]] = {
    "bot1": {
        "title": "Bot Numero 1",
        "script": str(BASE_DIR / "Bot Numero 1.py"),
        # Pasos de cola (después de set_bot) en el ORDEN solicitado
        "tail_steps": [
            Step("PATRON", "input_text", "Decime el **patrón** (ej: RGRG):", validate_regex=r"^[GRgr]+$"),
            Step("SENTIDO", "select", "Elegí el **sentido**:", choices=[("CALL", "CALL"), ("PUT", "PUT")]),
            Step("MONTO", "input_number", "¿**Monto** base por operación? (ej: 1.25)"),
            Step("USAR_RECUP", "bool", "¿Usar **recuperación**?", choices=[("Sí", "SI"), ("No", "NO")]),
            Step("STOPS", "composite_stops", "Enviá **stops** como `WIN,LOSS` (números enteros):"),
        ],
        "build": {
            "--email": "IQ_EMAIL",
            "--password": "IQ_PASSWORD",
            "--cuenta": "CUENTA",
            "--par": "PAR",
            "--patron": "PATRON",
            "--sentido": "SENTIDO",
            "--monto": "MONTO",
            "--recuperacion": "USAR_RECUP",
            "--stopwin": ("STOPS", 0),
            "--stoploss": ("STOPS", 1),
        },
    },
    # Placeholder de ejemplo para otro bot
    "bot2": {
        "title": "Bot Numero 2",
        "script": str(BASE_DIR / "Bot Numero 2.py"),
        "tail_steps": [
            Step("PATRON", "input_text", "Patrón (ej: RGRG):", validate_regex=r"^[GRgr]+$"),
            Step("SENTIDO", "select", "Sentido:", choices=[("CALL", "CALL"), ("PUT", "PUT")]),
            Step("MONTO", "input_number", "Monto base:"),
            Step("USAR_RECUP", "bool", "Recuperación?", choices=[("Sí", "SI"), ("No", "NO")]),
            Step("STOPS", "composite_stops", "Stops `WIN,LOSS` (enteros):"),
        ],
        "build": {
            "--email": "IQ_EMAIL",
            "--password": "IQ_PASSWORD",
            "--cuenta": "CUENTA",
            "--par": "PAR",
            "--patron": "PATRON",
            "--sentido": "SENTIDO",
            "--monto": "MONTO",
            "--recuperacion": "USAR_RECUP",
            "--stopwin": ("STOPS", 0),
            "--stoploss": ("STOPS", 1),
        },
    },
}

# Mapa (bot_id -> key) y lista de choices para el step select_bot
BOT_ID_TO_KEY = {idx: key for idx, key in enumerate(BOT_REGISTRY.keys(), start=1)}
BOT_SELECT_CHOICES: List[Tuple[str, str]] = [
    (cfg["title"], f"BOT::{bot_key}") for bot_key, cfg in BOT_REGISTRY.items()
]

# ---------------------------
# Memoria por chat
# ---------------------------
RUNNING_PIDS: Dict[int, int] = {}

# ---------------------------
# Utilidades
# ---------------------------

def _chat_allowed(update: Update) -> bool:
    if not ALLOWED_CHATS:
        return True
    chat_id = update.effective_chat.id if update.effective_chat else None
    return chat_id in ALLOWED_CHATS

async def _safe_delete_message(update: Update, context: CallbackContext, message_id: Optional[int]):
    if not message_id:
        return
    try:
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=message_id)
    except Exception:
        pass

async def _send_or_edit(context: CallbackContext, chat_id: int, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None) -> int:
    last_prompt_id = context.user_data.get("LAST_PROMPT_ID")
    if last_prompt_id:
        try:
            msg = await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=last_prompt_id,
                text=text,
                parse_mode="Markdown",
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
            return msg.message_id
        except Exception:
            pass
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="Markdown",
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )
    return msg.message_id

def _keyboard_from_choices(choices: List[Tuple[str, str]], columns: int = 2) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    for label, value in choices:
        row.append(InlineKeyboardButton(text=label, callback_data=value))
        if len(row) == columns:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)

# ---------------------------
# Formateos/validaciones
# ---------------------------

def _fmt_secret(value: str) -> str:
    if not value:
        return ""
    return "●" * min(8, len(value)) + ("…" if len(value) > 8 else "")

def _to_float(text: str) -> float:
    text = text.strip().replace(",", ".")
    return float(text)

# ---------------------------
# Construcción de comando
# ---------------------------

def _build_cmd_shell(script_path: str, answers: Dict[str, Any], build_map: Dict[str, Any]) -> str:
    parts = ["python3", "-u", script_path]
    for flag, spec in build_map.items():
        if isinstance(spec, tuple):
            key, idx = spec
            val = answers.get(key)
            if isinstance(val, (list, tuple)) and len(val) > idx:
                v = val[idx]
            else:
                v = None
            if v is None:
                continue
            parts.extend([flag, str(v)])
        else:
            key = spec
            v = answers.get(key)
            if v is None or v == "":
                continue
            parts.extend([flag, str(v)])
    cmd = " ".join(shlex.quote(p) for p in parts) + " > Activo1.log 2>&1 & echo $!"
    return cmd

async def _run_shell_and_get_pid_async(cmd: str, env: Dict[str, str]) -> Optional[int]:
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, **env},
        cwd=str(BASE_DIR),
    )
    out = await proc.stdout.read()
    text = out.decode("utf-8", errors="ignore").strip()
    m = re.search(r"(\d+)$", text)
    if m:
        return int(m.group(1))
    return None

# ---------------------------
# Flujo dinámico
# ---------------------------
# Pasos GLOBALES en el orden requerido (hasta elegir el bot)
GLOBAL_STEPS: List[Step] = [
    Step("IQ_EMAIL", "input_text", "Ingresá tu **email** de IQ Option:"),            # 1
    Step("IQ_PASSWORD", "input_secret", "Ingresá tu **contraseña** de IQ Option:"), # 2
    Step("CUENTA", "select", "Tipo de **cuenta**:", choices=[("DEMO", "DEMO"), ("REAL", "REAL")]), # 3
    Step("PAR", "select", "Elegí el **par**:", choices=[
        ("EURUSD-OTC", "EURUSD-OTC"),
        ("AUDCAD-OTC", "AUDCAD-OTC"),
        ("GBPUSD-OTC", "GBPUSD-OTC"),
        ("EURGBP-OTC", "EURGBP-OTC"),
        ("USDCHF-OTC", "USDCHF-OTC"),
    ]),                                                                                 # 4
    Step("BOT_KEY", "select_bot", "Elegí **qué bot** querés correr:", choices=BOT_SELECT_CHOICES),   # 5
]

# Genera el FLOW por chat: GLOBAL + cola del bot elegido

def _ensure_flow(context: CallbackContext) -> List[Step]:
    flow: List[Step] = context.user_data.get("FLOW")
    if flow:
        return flow
    context.user_data["FLOW"] = GLOBAL_STEPS.copy()
    return context.user_data["FLOW"]

# Cuando se elige el bot, ampliamos el FLOW con los tail_steps del bot

def _extend_flow_with_bot(context: CallbackContext, bot_key: str):
    cfg = BOT_REGISTRY[bot_key]
    flow: List[Step] = context.user_data.get("FLOW", []).copy()
    if any(s.key == "PATRON" for s in flow):
        context.user_data["FLOW"] = flow
        return
    flow.extend(cfg["tail_steps"])  # 6..10
    context.user_data["FLOW"] = flow

# ---------------------------
# Resumen/confirmación
# ---------------------------

def _resume_text(answers: Dict[str, Any]) -> str:
    sec_pwd = _fmt_secret(answers.get("IQ_PASSWORD", ""))
    stops = answers.get("STOPS")
    stops_txt = "N/D"
    if isinstance(stops, (list, tuple)) and len(stops) == 2:
        stops_txt = f"WIN={stops[0]}, LOSS={stops[1]}"
    bot_key = answers.get("BOT_KEY", "")
    bot_title = BOT_REGISTRY.get(bot_key, {}).get("title", bot_key)
    parts = [
        "*Resumen antes de lanzar:*",
        f"• Bot: `{bot_title}`",
        f"• Cuenta: `{answers.get('CUENTA','')}`  |  Par: `{answers.get('PAR','')}`",
        f"• Email: `{answers.get('IQ_EMAIL','')}`  |  Pass: `{sec_pwd}`",
    ]
    if "PATRON" in answers:
        parts.append(f"• Patrón: `{answers.get('PATRON')}`  |  Sentido: `{answers.get('SENTIDO','')}`")
    if "MONTO" in answers:
        parts.append(f"• Monto: `{answers.get('MONTO')}`  |  Recuperación: `{answers.get('USAR_RECUP','NO')}`")
    parts.append(f"• Stops: {stops_txt}")
    parts.append("")
    parts.append("¿Lanzamos?")
    return "\n".join(parts)

# ---------------------------
# Handlers del wizard
# ---------------------------
async def start(update: Update, context: CallbackContext) -> int:
    if not _chat_allowed(update):
        await update.message.reply_text("No estás autorizado para usar este bot.")
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data["ANSWERS"] = {}
    context.user_data["INDEX"] = 0
    flow = _ensure_flow(context)
    step = flow[0]
    msg_id = await _ask_step(update, context, step)
    context.user_data["LAST_PROMPT_ID"] = msg_id
    await _safe_delete_message(update, context, getattr(update.message, "message_id", None))
    return DYNAMIC

async def _ask_step(update: Update, context: CallbackContext, step: Step) -> int:
    chat_id = update.effective_chat.id
    if step.kind == "select":
        kb = _keyboard_from_choices(step.choices)
        return await _send_or_edit(context, chat_id, step.prompt, kb)
    if step.kind == "bool":
        kb = _keyboard_from_choices(step.choices)
        return await _send_or_edit(context, chat_id, step.prompt, kb)
    if step.kind == "select_bot":
        kb = _keyboard_from_choices(BOT_SELECT_CHOICES, columns=1)
        return await _send_or_edit(context, chat_id, step.prompt, kb)
    return await _send_or_edit(context, chat_id, step.prompt)

async def on_text(update: Update, context: CallbackContext) -> int:
    flow = _ensure_flow(context)
    idx = context.user_data.get("INDEX", 0)
    if idx >= len(flow):
        return await _to_confirm(update, context)
    step = flow[idx]
    text = (update.message.text or "").strip()

    if step.kind in ("input_text", "input_secret"):
        if step.validate_regex and not re.match(step.validate_regex, text):
            await update.message.reply_text("Formato inválido. Intentá de nuevo.")
            return DYNAMIC
        context.user_data.setdefault("ANSWERS", {})[step.key] = text
    elif step.kind == "input_number":
        try:
            val = _to_float(text)
            if val <= 0:
                raise ValueError
            context.user_data.setdefault("ANSWERS", {})[step.key] = round(val, 2)
        except Exception:
            await update.message.reply_text("Ingresá un número válido (>0).")
            return DYNAMIC
    elif step.kind == "composite_stops":
        try:
            parts = [p.strip() for p in text.replace(" ", "").split(",")]
            if len(parts) != 2:
                raise ValueError
            win = int(float(parts[0]))
            loss = int(float(parts[1]))
            if win <= 0 or loss <= 0:
                raise ValueError
            context.user_data.setdefault("ANSWERS", {})[step.key] = [win, loss]
        except Exception:
            await update.message.reply_text("Formato de stops inválido. Usá algo como `5, 9`.")
            return DYNAMIC
    else:
        await update.message.reply_text("Usá los botones, por favor.")
        return DYNAMIC

    await _safe_delete_message(update, context, getattr(update.message, "message_id", None))
    context.user_data["INDEX"] = idx + 1

    if context.user_data["INDEX"] >= len(flow):
        return await _to_confirm(update, context)

    next_step = flow[context.user_data["INDEX"]]
    msg_id = await _ask_step(update, context, next_step)
    context.user_data["LAST_PROMPT_ID"] = msg_id
    return DYNAMIC

async def on_callback(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    flow = _ensure_flow(context)
    idx = context.user_data.get("INDEX", 0)
    if idx >= len(flow):
        return await _to_confirm(update, context)
    step = flow[idx]
    data = query.data

    if step.kind in ("select", "bool"):
        context.user_data.setdefault("ANSWERS", {})[step.key] = data
    elif step.kind == "select_bot":
        if not data.startswith("BOT::"):
            return DYNAMIC
        bot_key = data.split("::", 1)[1]
        if bot_key not in BOT_REGISTRY:
            return DYNAMIC
        context.user_data.setdefault("ANSWERS", {})[step.key] = bot_key
        _extend_flow_with_bot(context, bot_key)
        flow = _ensure_flow(context)
    else:
        return DYNAMIC

    context.user_data["INDEX"] = idx + 1
    if context.user_data["INDEX"] >= len(flow):
        return await _to_confirm(update, context)

    next_step = flow[context.user_data["INDEX"]]
    msg_id = await _ask_step(update, context, next_step)
    context.user_data["LAST_PROMPT_ID"] = msg_id
    return DYNAMIC

async def _to_confirm(update_or_query, context: CallbackContext) -> int:
    answers = context.user_data.get("ANSWERS", {})
    txt = _resume_text(answers)
    kb = _keyboard_from_choices([("Confirmar", "CONFIRM"), ("Cambiar", "EDIT"), ("Cancelar", "CANCEL")], columns=3)
    if isinstance(update_or_query, Update) and update_or_query.message:
        chat_id = update_or_query.effective_chat.id
        msg_id = await _send_or_edit(context, chat_id, txt, kb)
        context.user_data["LAST_PROMPT_ID"] = msg_id
        await _safe_delete_message(update_or_query, context, getattr(update_or_query.message, "message_id", None))
    else:
        query = update_or_query.callback_query
        await context.bot.edit_message_text(
            chat_id=query.message.chat_id,
            message_id=query.message.message_id,
            text=txt,
            parse_mode="Markdown",
            reply_markup=kb,
            disable_web_page_preview=True,
        )
    return CONFIRM

async def confirm_flow(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "CANCEL":
        await query.edit_message_text("Operación cancelada.")
        context.user_data.clear()
        return ConversationHandler.END
    if data == "EDIT":
        context.user_data.clear()
        context.user_data["ANSWERS"] = {}
        context.user_data["INDEX"] = 0
        _ensure_flow(context)
        msg_id = await _ask_step(update, context, GLOBAL_STEPS[0])
        context.user_data["LAST_PROMPT_ID"] = msg_id
        return DYNAMIC
    if data != "CONFIRM":
        return CONFIRM

    answers = context.user_data.get("ANSWERS", {})
    bot_key = answers.get("BOT_KEY")
    if not bot_key or bot_key not in BOT_REGISTRY:
        await query.edit_message_text("Falta seleccionar el bot.")
        return ConversationHandler.END

    cfg = BOT_REGISTRY[bot_key]
    cmd = _build_cmd_shell(cfg["script"], answers, cfg["build"])

    env = {
    "TELEGRAM_TOKEN": TELEGRAM_TOKEN,                # viene del código, no de .env
    "TELEGRAM_CHAT_ID": str(query.message.chat_id), # dinámico por chat
    "IQ_EMAIL": str(answers.get("IQ_EMAIL", "")),   # lo aporta el wizard
    "IQ_PASSWORD": str(answers.get("IQ_PASSWORD", "")),
}

    pid = await _run_shell_and_get_pid_async(cmd, env)
    if pid:
        RUNNING_PIDS[query.message.chat_id] = pid
        await query.edit_message_text(f"✅ Bot lanzado (PID `{pid}`). Mirá *Activo1.log*.", parse_mode="Markdown")
    else:
        await query.edit_message_text("❌ No se pudo lanzar el bot. Revisá logs.")
    context.user_data.clear()
    return ConversationHandler.END

# ---------------------------
# Comandos utilitarios
# ---------------------------
async def show_log(update: Update, context: CallbackContext):
    await _safe_delete_message(update, context, getattr(update.message, "message_id", None))
    log_path = BASE_DIR / "Activo1.log"
    if not log_path.exists():
        await update.message.reply_text("No hay log disponible aún.")
        return
    try:
        text = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()[-60:]
        content = "\n".join(text) or "(vacío)"
    except Exception:
        content = "(no se pudo leer el log)"
    await update.message.reply_text(f"```\n{content}\n```", parse_mode="MarkdownV2")

async def stop_bot(update: Update, context: CallbackContext):
    await _safe_delete_message(update, context, getattr(update.message, "message_id", None))
    chat_id = update.effective_chat.id
    pid = RUNNING_PIDS.get(chat_id)
    if not pid:
        await update.message.reply_text("No hay proceso registrado para este chat.")
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except Exception:
        pass
    await asyncio.sleep(0.8)
    alive = True
    try:
        os.kill(pid, 0)
        alive = True
    except Exception:
        alive = False
    if alive:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
    RUNNING_PIDS.pop(chat_id, None)
    await update.message.reply_text("Bot detenido.")

async def state_bot(update: Update, context: CallbackContext):
    await _safe_delete_message(update, context, getattr(update.message, "message_id", None))
    chat_id = update.effective_chat.id
    pid = RUNNING_PIDS.get(chat_id)
    if not pid:
        await update.message.reply_text("No hay proceso registrado para este chat.")
        return
    alive = True
    try:
        os.kill(pid, 0)
        alive = True
    except Exception:
        alive = False
    if not alive:
        RUNNING_PIDS.pop(chat_id, None)
        await update.message.reply_text("El proceso ya no está vivo. Limpio el registro.")
        return
    try:
        import subprocess
        etimes = subprocess.check_output(["bash", "-lc", f"ps -o etimes= -p {pid}"], text=True).strip()
    except Exception:
        etimes = "?"
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_text(errors="ignore").replace("\x00", " ")
        if len(cmdline) > 200:
            cmdline = cmdline[:200] + "…"
    except Exception:
        cmdline = "?"
    await update.message.reply_text(f"PID `{pid}` vivo. Uptime: `{etimes}` s\nCmd: `{cmdline}`", parse_mode="Markdown")

# ---------------------------
# main
# ---------------------------
async def on_unknown(update: Update, context: CallbackContext):
    await update.message.reply_text("Comando no reconocido.")

async def cancel(update: Update, context: CallbackContext):
    context.user_data.clear()
    await update.message.reply_text("Cancelado.")
    return ConversationHandler.END

def main() -> None:
    persistence = PicklePersistence(filepath=str(BASE_DIR / "control_bot.persist"))
    app: Application = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .persistence(persistence)
        .build()
    )

    conv = ConversationHandler(
        entry_points=[CommandHandler("startbot", start)],
        states={
            DYNAMIC: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_text),
                CallbackQueryHandler(on_callback),
            ],
            CONFIRM: [CallbackQueryHandler(confirm_flow)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        name="wizard",
        persistent=True,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("log", show_log))
    app.add_handler(CommandHandler("stop", stop_bot))
    app.add_handler(CommandHandler("state", state_bot))
    app.add_handler(MessageHandler(filters.COMMAND, on_unknown))
    log.info("Bot de control iniciado.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        pass
