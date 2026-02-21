import os
import sys
import time
import uuid
import platform
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
from psycopg2.extras import Json
from psycopg2.pool import SimpleConnectionPool
from dotenv import load_dotenv
from iqoptionapi.stable_api import IQ_Option

# =========================
# Objetivo del refactor:
# - Reducir al mínimo el tiempo entre el cierre de la vela y el buy()
# - Evitar trabajo pesado antes de colocar la orden
# - Reusar conexiones (DB/HTTP) y evitar recalcular EMAs varias veces por ciclo
# =========================

DEBUG_ACTIVE = True  # ponlo en False si quieres prioridad 100% a velocidad
SLEEP_AFTER_MINUTE_CLOSE = 0.20  # segundos después del cambio de minuto para leer velas cerradas


# -------- Utils --------
def clear_console() -> None:
    if platform.system() == "Windows":
        os.system("cls")
    else:
        os.system("clear")


def now_local() -> datetime:
    # el bot ya usa datetimes "naive" en varias partes; mantenemos coherencia
    return datetime.now()


def normalize_result(raw: str, profit: float) -> str:
    r = (raw or "").lower().strip()
    if r in ("loose", "lose", "loss"):
        return "loss"
    if r == "win":
        return "win"
    if r == "equal":
        return "equal"
    # fallback por profit
    if profit > 0:
        return "win"
    if profit < 0:
        return "loss"
    return "equal"


# -------- Telegram (reuso de sesión HTTP) --------
class TelegramClient:
    def __init__(self, token: str, chat_id: str):
        self.token = (token or "").strip()
        self.chat_id = (chat_id or "").strip()
        self._session = None

    def _get_session(self):
        if self._session is None:
            import requests  # lazy import
            self._session = requests.Session()
        return self._session

    def send(self, text: str) -> bool:
        if not self.token or not self.chat_id:
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            s = self._get_session()
            r = s.post(url, json=payload, timeout=5)
            if r.status_code != 200:
                return False
            j = r.json()
            return bool(j.get("ok", False))
        except Exception:
            return False


# -------- DB Pool (reuso conexiones) --------
class DB:
    def __init__(self, dsn: str, minconn: int = 1, maxconn: int = 4):
        self.pool = SimpleConnectionPool(minconn=minconn, maxconn=maxconn, dsn=dsn)

    def close(self):
        try:
            self.pool.closeall()
        except Exception:
            pass

    def _conn(self):
        return self.pool.getconn()

    def _put(self, conn):
        self.pool.putconn(conn)

    def exec(self, sql: str, params: Tuple[Any, ...] = ()) -> None:
        conn = self._conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
        finally:
            self._put(conn)

    def fetch_one(self, sql: str, params: Tuple[Any, ...] = ()) -> Optional[Tuple[Any, ...]]:
        conn = self._conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    return cur.fetchone()
        finally:
            self._put(conn)


# -------- Indicadores --------
def ema_series(values: List[float], period: int) -> List[Optional[float]]:
    """
    EMA clásica. Devuelve lista misma longitud que values.
    Arranca con SMA del primer 'period' y antes de eso pone None.
    """
    if period <= 0:
        raise ValueError("period must be > 0")
    n = len(values)
    out: List[Optional[float]] = [None] * n
    if n < period:
        return out

    k = 2 / (period + 1)
    sma = sum(values[:period]) / period
    out[period - 1] = sma
    prev = sma

    for i in range(period, n):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def get_low(c: Dict[str, Any]) -> float:
    if "min" in c:
        return float(c["min"])
    if "low" in c:
        return float(c["low"])
    return min(float(c["open"]), float(c["close"]))


def get_high(c: Dict[str, Any]) -> float:
    if "max" in c:
        return float(c["max"])
    if "high" in c:
        return float(c["high"])
    return max(float(c["open"]), float(c["close"]))


def is_bull(c: Dict[str, Any]) -> bool:
    return float(c["close"]) > float(c["open"])


def is_bear(c: Dict[str, Any]) -> bool:
    return float(c["close"]) < float(c["open"])


def is_doji(c: Dict[str, Any]) -> bool:
    return float(c["close"]) == float(c["open"])


# -------- Contextos / Entradas (sin prints para velocidad) --------
def call_context_ok(candles: List[Dict[str, Any]], e7, e14, e21) -> bool:
    if len(candles) < 30:
        return False
    last10 = candles[-10:]
    start_idx = len(candles) - 10

    for j, c in enumerate(last10):
        i = start_idx + j
        if e7[i] is None or e14[i] is None or e21[i] is None:
            return False

        o = float(c["open"])
        cl = float(c["close"])
        lo = get_low(c)

        if not (e7[i] > e14[i] > e21[i]):
            return False
        if min(o, cl) <= e7[i]:
            return False
        if lo <= e14[i]:
            return False
        if i - 1 < 0 or e21[i - 1] is None:
            return False
        if not (e21[i] > e21[i - 1]):
            return False

    return True


def put_context_ok(candles: List[Dict[str, Any]], e7, e14, e21) -> bool:
    if len(candles) < 30:
        return False
    last10 = candles[-10:]
    start_idx = len(candles) - 10

    for j, c in enumerate(last10):
        i = start_idx + j
        if e7[i] is None or e14[i] is None or e21[i] is None:
            return False

        o = float(c["open"])
        cl = float(c["close"])
        hi = get_high(c)

        if not (e7[i] < e14[i] < e21[i]):
            return False
        if max(o, cl) >= e7[i]:
            return False
        if hi >= e14[i]:
            return False
        if i - 1 < 0 or e21[i - 1] is None:
            return False
        if not (e21[i] < e21[i - 1]):
            return False

    return True


def call_entry_ok(candles: List[Dict[str, Any]], e7, e14) -> Tuple[bool, Optional[str]]:
    if len(candles) < 30:
        return False, None

    c_last = candles[-1]
    c_prev1 = candles[-2]
    c_prev2 = candles[-3] if len(candles) >= 3 else None

    i_last = len(candles) - 1
    i_prev1 = len(candles) - 2
    i_prev2 = len(candles) - 3 if len(candles) >= 3 else None

    if e7[i_last] is None or e14[i_last] is None:
        return False, None

    # Patron A: Bull + Bear + Bear (las 2 bajistas por encima de EMA7 y low > EMA14)
    if c_prev2 is not None and not (is_doji(c_prev2) or is_doji(c_prev1) or is_doji(c_last)):
        if is_bull(c_prev2) and is_bear(c_prev1) and is_bear(c_last):
            c1_close = float(c_prev1["close"])
            c2_close = float(c_last["close"])
            c1_low = get_low(c_prev1)
            c2_low = get_low(c_last)
            if c1_close <= e7[i_prev1] or c2_close <= e7[i_last]:
                return False, None
            if c1_low <= e14[i_prev1] or c2_low <= e14[i_last]:
                return False, None
            return True, "A-B-B"

    # Patron B: Bear + Bull (ambas velas por encima EMA7 y low > EMA14)
    if not (is_doji(c_prev1) or is_doji(c_last)):
        if is_bear(c_prev1) and is_bull(c_last):
            c1_close = float(c_prev1["close"])
            c2_close = float(c_last["close"])
            c1_low = get_low(c_prev1)
            c2_low = get_low(c_last)
            if c1_close <= e7[i_prev1] or c2_close <= e7[i_last]:
                return False, None
            if c1_low <= e14[i_prev1] or c2_low <= e14[i_last]:
                return False, None
            return True, "B-A"

    return False, None


def put_entry_ok(candles: List[Dict[str, Any]], e7, e14) -> Tuple[bool, Optional[str]]:
    if len(candles) < 30:
        return False, None

    c_last = candles[-1]
    c_prev1 = candles[-2]
    c_prev2 = candles[-3] if len(candles) >= 3 else None

    i_last = len(candles) - 1
    i_prev1 = len(candles) - 2
    i_prev2 = len(candles) - 3 if len(candles) >= 3 else None

    if e7[i_last] is None or e14[i_last] is None:
        return False, None

    # Patron A: Bear + Bull + Bull (las 2 alcistas por debajo EMA7 y high < EMA14)
    if c_prev2 is not None and not (is_doji(c_prev2) or is_doji(c_prev1) or is_doji(c_last)):
        if is_bear(c_prev2) and is_bull(c_prev1) and is_bull(c_last):
            c1_close = float(c_prev1["close"])
            c2_close = float(c_last["close"])
            c1_high = get_high(c_prev1)
            c2_high = get_high(c_last)
            if c1_close >= e7[i_prev1] or c2_close >= e7[i_last]:
                return False, None
            if c1_high >= e14[i_prev1] or c2_high >= e14[i_last]:
                return False, None
            return True, "B-A-A"

    # Patron B: Bull + Bear (ambas por debajo EMA7 y high < EMA14)
    if not (is_doji(c_prev1) or is_doji(c_last)):
        if is_bull(c_prev1) and is_bear(c_last):
            c1_close = float(c_prev1["close"])
            c2_close = float(c_last["close"])
            c1_high = get_high(c_prev1)
            c2_high = get_high(c_last)
            if c1_close >= e7[i_prev1] or c2_close >= e7[i_last]:
                return False, None
            if c1_high >= e14[i_prev1] or c2_high >= e14[i_last]:
                return False, None
            return True, "A-B"

    return False, None


# -------- Debug prints (mantienen tu salida pero no estorban el buy) --------
def debug_print_call_context(candles, e7, e14, e21) -> None:
    if len(candles) < 30:
        print("NO: Muy pocas velas para EMA21/validacion decente.")
        return
    last10 = candles[-10:]
    start_idx = len(candles) - 10
    print("Condiciones para contexto CALL:")
    for j, c in enumerate(last10):
        i = start_idx + j
        o = float(c["open"])
        cl = float(c["close"])
        lo = get_low(c)
        t = datetime.fromtimestamp(c["from"]).strftime("%H:%M:%S")

        ok = True
        motivo = "OK"
        if e7[i] is None or e14[i] is None or e21[i] is None:
            ok = False
            motivo = "EMAS_NO_DISPONIBLES"
        else:
            if not (e7[i] > e14[i] > e21[i]):
                ok = False
                motivo = "EMA7>EMA14>EMA21 -> NO"
            if ok and min(o, cl) <= e7[i]:
                ok = False
                motivo = "CUERPO TOCA O BAJO EMA7"
            if ok and lo <= e14[i]:
                ok = False
                motivo = "MECHA TOCA LA EMA14"
            if ok:
                if i - 1 < 0 or e21[i - 1] is None:
                    ok = False
                    motivo = "EMA21_PREV_NO_DISPONIBLE"
                elif not (e21[i] > e21[i - 1]):
                    ok = False
                    motivo = "PENDIENTE EMA21 NO POSITIVA"

        status = "OK" if ok else f"NO ({motivo})"
        print(f"#{j+1:02d} | {t} | OPEN={o:.6f} | CLOSE={cl:.6f} | LOW={lo:.6f} | {status}")
    print("")


def debug_print_put_context(candles, e7, e14, e21) -> None:
    if len(candles) < 30:
        print("NO: Muy pocas velas para EMA21/validacion decente.")
        return
    last10 = candles[-10:]
    start_idx = len(candles) - 10
    print("Condiciones para contexto PUT:")
    for j, c in enumerate(last10):
        i = start_idx + j
        o = float(c["open"])
        cl = float(c["close"])
        hi = get_high(c)
        t = datetime.fromtimestamp(c["from"]).strftime("%H:%M:%S")

        ok = True
        motivo = "OK"
        if e7[i] is None or e14[i] is None or e21[i] is None:
            ok = False
            motivo = "EMAS_NO_DISPONIBLES"
        else:
            if not (e7[i] < e14[i] < e21[i]):
                ok = False
                motivo = "EMA7<EMA14<EMA21 -> NO"
            if ok and max(o, cl) >= e7[i]:
                ok = False
                motivo = "CUERPO CIERRA SOBRE EMA7"
            if ok and hi >= e14[i]:
                ok = False
                motivo = "MECHA TOCA LA EMA14"
            if ok:
                if i - 1 < 0 or e21[i - 1] is None:
                    ok = False
                    motivo = "EMA21_PREV_NO_DISPONIBLE"
                elif not (e21[i] < e21[i - 1]):
                    ok = False
                    motivo = "PENDIENTE EMA21 NO NEGATIVA"

        status = "OK" if ok else f"NO ({motivo})"
        print(f"#{j+1:02d} | {t} | OPEN={o:.6f} | CLOSE={cl:.6f} | HIGH={hi:.6f} | {status}")
    print("")


# -------- Operación: contexto JSON --------
def build_context_json(candles: List[Dict[str, Any]], e7, e14, e21, fecha_apertura: datetime) -> Dict[str, Any]:
    # Tomar últimas 10 velas
    ultimas_10 = candles[-10:]
    start_idx = len(candles) - 10

    velas_json = []
    for i, vela in enumerate(ultimas_10):
        idx = start_idx + i
        velas_json.append({
            "timestamp": vela["from"],
            "datetime": datetime.fromtimestamp(vela["from"]).strftime("%Y-%m-%d %H:%M:%S"),
            "open": float(vela["open"]),
            "high": float(vela.get("max", vela.get("high", vela["close"]))),
            "low": float(vela.get("min", vela.get("low", vela["open"]))),
            "close": float(vela["close"]),
            "ema_rapida": round(e7[idx], 6) if e7[idx] is not None else None,
            "ema_media": round(e14[idx], 6) if e14[idx] is not None else None,
            "ema_lenta": round(e21[idx], 6) if e21[idx] is not None else None
        })

    return {
        "velas": velas_json,
        "metadata": {
            "num_velas": len(velas_json),
            "timestamp_analisis": fecha_apertura.isoformat()
        }
    }


# -------- DB operaciones (mismas funcionalidades, más rápido) --------
def borrar_operaciones_usuario(db: DB, username: str) -> None:
    db.exec("DELETE FROM operaciones_activas WHERE username = %s;", (username,))


def hay_operacion_activa(db: DB, username: str) -> bool:
    row = db.fetch_one("""
        SELECT EXISTS(
            SELECT 1
            FROM operaciones_activas
            WHERE is_active = TRUE
              AND username = %s
        );
    """, (username,))
    return bool(row[0]) if row else False


def registrar_operacion_activa(db: DB, buy_id: int, asset: str, direction: str, duracion_min: int, username: str) -> None:
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=duracion_min, seconds=5)
    db.exec("""
        INSERT INTO operaciones_activas (buy_id, asset, direction, is_active, expires_at, username)
        VALUES (%s, %s, %s, TRUE, %s, %s)
        ON CONFLICT (buy_id) DO NOTHING;
    """, (int(buy_id), asset, direction, expires_at, username))


def cerrar_operacion_activa(db: DB, buy_id: int, resultado: str, ganancia: float) -> None:
    db.exec("""
        UPDATE operaciones_activas
        SET
            is_active = FALSE,
            result = %s,
            profit = %s,
            closed_at = NOW()
        WHERE buy_id = %s
          AND is_active = TRUE;
    """, (resultado, float(ganancia), int(buy_id)))


def guardar_operacion(db: DB, id_conjunto: str, fecha_op: datetime, par: str, direccion: str,
                     patron: str, resultado: str, contexto_json: Dict[str, Any]) -> None:
    db.exec("""
        INSERT INTO operaciones
        (id_conjunto_velas, fecha_operacion, par, direccion, patron, resultado, contexto)
        VALUES (%s, %s, %s, %s, %s, %s, %s);
    """, (
        id_conjunto,
        fecha_op,
        par,
        direccion,
        patron,
        resultado,
        Json(contexto_json),
    ))


def calcular_profit_acumulado(db: DB, username: str) -> float:
    # Tu código original usa profit_amount, pero también actualizas "profit".
    # Intentamos "profit" primero y si falla por columna inexistente, caemos a profit_amount.
    try:
        row = db.fetch_one("""
            SELECT COALESCE(SUM(profit), 0)
            FROM operaciones_activas
            WHERE username = %s
              AND is_active = FALSE;
        """, (username,))
        return round(float(row[0] if row else 0.0), 2)
    except psycopg2.Error:
        row = db.fetch_one("""
            SELECT COALESCE(SUM(profit_amount), 0)
            FROM operaciones_activas
            WHERE username = %s
              AND is_active = FALSE;
        """, (username,))
        return round(float(row[0] if row else 0.0), 2)


def resumen_sesion_stop(db: DB, username: str, saldo_inicial: float, saldo_actual: float, session_start: datetime) -> Dict[str, Any]:
    row = db.fetch_one("""
        SELECT
            COUNT(*) FILTER (WHERE result IS NOT NULL) AS total_cerradas,
            COUNT(*) FILTER (WHERE result = 'win')      AS ganadas,
            COUNT(*) FILTER (WHERE result = 'loss')     AS perdidas,
            COUNT(*) FILTER (WHERE result = 'equal')    AS iguales,
            COUNT(*) FILTER (WHERE result = 'error')    AS errores
        FROM operaciones_activas
        WHERE created_at >= %s
          AND username = %s;
    """, (session_start, username))

    if not row:
        row = (0, 0, 0, 0, 0)

    total_cerradas, ganadas, perdidas, iguales, errores = row
    profit_sesion = float(saldo_actual) - float(saldo_inicial)

    return {
        "total": int(total_cerradas or 0),
        "ganadas": int(ganadas or 0),
        "perdidas": int(perdidas or 0),
        "iguales": int(iguales or 0),
        "errores": int(errores or 0),
        "profit_sesion": round(profit_sesion, 2),
    }


def revisar_stops_si_libre(iq: IQ_Option, db: DB, saldo_inicial: float, stop_win: float, stop_loss: float, username: str) -> Optional[str]:
    if hay_operacion_activa(db, username):
        return None
    saldo_actual = iq.get_balance()
    if saldo_actual >= (saldo_inicial + stop_win):
        return "STOP_WIN"
    if saldo_actual <= (saldo_inicial - stop_loss):
        return "STOP_LOSS"
    return None


# -------- Timing: despertar justo después del cierre de minuto --------
def sleep_until_next_minute(close_offset_sec: float = SLEEP_AFTER_MINUTE_CLOSE) -> None:
    now_ts = time.time()
    next_minute = (int(now_ts // 60) + 1) * 60
    target = next_minute + close_offset_sec
    sleep_s = target - now_ts
    if sleep_s > 0:
        time.sleep(sleep_s)


@dataclass
class ActiveTrade:
    buy_id: int
    asset: str
    direction: str
    duration_min: int
    patron: str
    id_conjunto: str
    fecha_op: datetime
    # snapshot de velas cerradas + EMAs para armar el JSON sin recalcular luego
    candles_snapshot: List[Dict[str, Any]]
    e7: List[Optional[float]]
    e14: List[Optional[float]]
    e21: List[Optional[float]]
    expires_ts: float


def main() -> None:
    load_dotenv()

    email = os.getenv("IQOPTION_EMAIL", "").strip()
    password = os.getenv("IQOPTION_PASSWORD", "").strip()

    TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    STOP_LOSS = float(os.getenv("IQ_STOPLOSS", 0))
    STOP_WIN = float(os.getenv("IQ_STOPWIN", 0))
    MONTO_OPERACIONES = float(os.getenv("MONTO_OPERACIONES", 0))
    USUARIO = os.getenv("USUARIO", "").strip()

    if not email or not password:
        raise RuntimeError("Faltan IQOPTION_EMAIL y/o IQOPTION_PASSWORD en el .env.")

    if len(sys.argv) < 3:
        raise RuntimeError("Uso: python3 simpleActivo.py <ACTIVO> <Escaner|Automatico>")

    selected_asset = sys.argv[1]
    modo_operacion = sys.argv[2]

    # DSN DB (igual que tu código original)
    dsn = "host=163.245.214.198 port=5432 dbname=context_bot_db user=rolo password=EnzoDaniel*2023"

    telegram = TelegramClient(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID)
    db = DB(dsn=dsn, minconn=1, maxconn=4)

    # Conexión IQ
    iq = IQ_Option(email, password)
    iq.connect()
    iq.change_balance("PRACTICE")

    saldo_inicial = iq.get_balance()

    # Estado local (evita I/O antes del buy)
    last_closed_candle_from = 0
    call_ctx_active = False
    put_ctx_active = False
    cooldown_ticks = 0  # mismo concepto que wait_betwween_oper, pero controlado por vela nueva
    active_trade: Optional[ActiveTrade] = None

    # Limpieza inicial
    borrar_operaciones_usuario(db, USUARIO)

    # Loop principal: siempre sincronizado al minuto
    try:
        while True:
            # 1) Dormir hasta justo después de cierre (reduce delay de entrada)
            sleep_until_next_minute()

            # 2) Si hay trade activo, solo chequea resultado cuando ya venció (no bloquea cada ciclo)
            if active_trade and time.time() >= active_trade.expires_ts:
                try:
                    res = iq.check_win_v4(active_trade.buy_id)
                    estado_raw, ganancia = None, None
                    if isinstance(res, (list, tuple)) and len(res) >= 2:
                        estado_raw = str(res[0])
                        ganancia = float(res[1])
                    else:
                        ganancia = float(res)
                        estado_raw = "win" if ganancia > 0 else ("equal" if ganancia == 0 else "loss")

                    estado = normalize_result(estado_raw, ganancia)

                    cerrar_operacion_activa(db, active_trade.buy_id, estado, ganancia)
                    contexto_json = build_context_json(
                        active_trade.candles_snapshot,
                        active_trade.e7,
                        active_trade.e14,
                        active_trade.e21,
                        active_trade.fecha_op,
                    )
                    guardar_operacion(
                        db,
                        active_trade.id_conjunto,
                        active_trade.fecha_op,
                        active_trade.asset,
                        active_trade.direction,
                        active_trade.patron,
                        estado,
                        contexto_json,
                    )

                    profit = calcular_profit_acumulado(db, USUARIO)

                    if estado == "win" and ganancia > 0:
                        msg = (
                            f"Par: {active_trade.asset}\n"
                            f"Velas Previas: {active_trade.patron}\n"
                            f"Resultado: ✅ WIN\n"
                            f"Profit: +${ganancia:.2f}\n"
                            f"Profit de la sesion: ${profit:+.2f}\n"
                        )
                    elif estado == "loss" or ganancia < 0:
                        msg = (
                            f"Par: {active_trade.asset}\n"
                            f"Velas Previas: {active_trade.patron}\n"
                            f"Resultado: ❌ LOSS\n"
                            f"Profit: -${abs(ganancia):.2f}\n"
                            f"Profit de la sesion: ${profit:+.2f}\n"
                        )
                    else:
                        msg = (
                            f"Par: {active_trade.asset}\n"
                            f"Velas Previas: {active_trade.patron}\n"
                            f"Resultado: ⚪ EQUAL\n"
                            f"Profit: ${ganancia:.2f}\n"
                            f"Profit de la sesion: ${profit:+.2f}\n"
                        )
                    telegram.send(msg)
                except Exception as e:
                    print(f"[{now_local().strftime('%d-%m %H:%M')}] Error al verificar resultado: {e}")
                finally:
                    active_trade = None  # libera para operar otra vez

            # 3) Leer velas cerradas (sin sleeps extra)
            candles = iq.get_candles(selected_asset, 60, 150, time.time())
            candles = sorted(candles, key=lambda x: x["from"])

            # iqoptionapi suele traer la última "en formación" al final; quítala
            if len(candles) >= 2:
                candles = candles[:-1]

            if not candles:
                continue

            last_closed = candles[-1]
            closed_from = int(last_closed["from"])

            # Asegurar que solo procesamos una vez por vela cerrada
            if closed_from == last_closed_candle_from:
                continue

            last_closed_candle_from = closed_from

            # 4) EMAs una sola vez por ciclo
            closes = [float(c["close"]) for c in candles]
            e7 = ema_series(closes, 7)
            e14 = ema_series(closes, 14)
            e21 = ema_series(closes, 21)

            # 5) Debug/monitor (NO antes del buy)
            if DEBUG_ACTIVE:
                clear_console()
                print(f"Monitoreando activo: {selected_asset} | modo: {modo_operacion} | vela cerrada: {datetime.fromtimestamp(closed_from).strftime('%H:%M:%S')}")

            # 6) Contextos
            call_ctx = call_context_ok(candles, e7, e14, e21)
            put_ctx = put_context_ok(candles, e7, e14, e21)

            if DEBUG_ACTIVE:
                # prints largos solo si estás debugueando
                if call_ctx:
                    debug_print_call_context(candles, e7, e14, e21)
                if put_ctx:
                    debug_print_put_context(candles, e7, e14, e21)

            # 7) Notificaciones contexto en modo Escaner
            if modo_operacion == "Escaner":
                if call_ctx and not call_ctx_active:
                    call_ctx_active = True
                    telegram.send(f"✅ Contexto Alcista activado en: {selected_asset}")
                if (not call_ctx) and call_ctx_active:
                    call_ctx_active = False

                if put_ctx and not put_ctx_active:
                    put_ctx_active = True
                    telegram.send(f"✅ Contexto Bajista activado en: {selected_asset}")
                if (not put_ctx) and put_ctx_active:
                    put_ctx_active = False

            # 8) Modo Automático: entrada lo antes posible (prioridad absoluta al buy())
            if modo_operacion == "Automatico":
                # cooldown por velas (equivalente a tu wait_betwween_oper)
                if cooldown_ticks > 0:
                    cooldown_ticks -= 1

                # no operes si hay trade activo en memoria o en DB
                if active_trade is None and cooldown_ticks == 0 and (not hay_operacion_activa(db, USUARIO)):
                    # CALL
                    if call_ctx:
                        ok_entry, patron = call_entry_ok(candles, e7, e14)
                        if ok_entry and patron:
                            # BUY inmediatamente: no hagas DB/telegram antes
                            ok, buy_id = iq.buy(MONTO_OPERACIONES, selected_asset, "call", 1)
                            if not ok or not buy_id:
                                telegram.send(f"Error al poner la operacion en: {selected_asset}")
                            else:
                                # después del buy: registra
                                registrar_operacion_activa(db, int(buy_id), selected_asset, "call", 1, USUARIO)
                                id_conjunto = str(uuid.uuid4())
                                fecha_op = now_local()
                                telegram.send(f"Operacion 📈 activa en: {selected_asset}\n")
                                cooldown_ticks = 3  # mismo comportamiento
                                active_trade = ActiveTrade(
                                    buy_id=int(buy_id),
                                    asset=selected_asset,
                                    direction="call",
                                    duration_min=1,
                                    patron=patron,
                                    id_conjunto=id_conjunto,
                                    fecha_op=fecha_op,
                                    candles_snapshot=candles.copy(),
                                    e7=e7,
                                    e14=e14,
                                    e21=e21,
                                    expires_ts=time.time() + 60 + 5,
                                )

                    # PUT (solo si no se abrió CALL arriba)
                    if active_trade is None and put_ctx:
                        ok_entry, patron = put_entry_ok(candles, e7, e14)
                        if ok_entry and patron:
                            ok, buy_id = iq.buy(MONTO_OPERACIONES, selected_asset, "put", 1)
                            if not ok or not buy_id:
                                telegram.send(f"Error al poner la operacion en: {selected_asset}")
                            else:
                                registrar_operacion_activa(db, int(buy_id), selected_asset, "put", 1, USUARIO)
                                id_conjunto = str(uuid.uuid4())
                                fecha_op = now_local()
                                telegram.send(f"Operacion 📉 activa en: {selected_asset}\n")
                                cooldown_ticks = 3
                                active_trade = ActiveTrade(
                                    buy_id=int(buy_id),
                                    asset=selected_asset,
                                    direction="put",
                                    duration_min=1,
                                    patron=patron,
                                    id_conjunto=id_conjunto,
                                    fecha_op=fecha_op,
                                    candles_snapshot=candles.copy(),
                                    e7=e7,
                                    e14=e14,
                                    e21=e21,
                                    expires_ts=time.time() + 60 + 5,
                                )

                # Stops (solo cuando no hay trade abierto)
                motivo = revisar_stops_si_libre(iq, db, saldo_inicial, STOP_WIN, STOP_LOSS, USUARIO)
                if motivo in ("STOP_WIN", "STOP_LOSS"):
                    break

        # Si llegamos aquí fue por STOP
        resumen = resumen_sesion_stop(db, USUARIO, saldo_inicial, iq.get_balance(), now_local())
        msg = (
            f"Resumen de la Sesión:\n"
            f"📈Total de Operaciones: {resumen['total']}\n"
            f"✅Ganadas: {resumen['ganadas']}\n"
            f"❌Perdidas: {resumen['perdidas']}\n"
            f"💰Profit: {resumen['profit_sesion']}\n"
        )
        telegram.send(msg)

    finally:
        db.close()
        # mantengo tu comportamiento final
        os.system("pkill -f simpleActivo.py")


if __name__ == "__main__":
    main()
