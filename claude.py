import os
import psycopg2
from psycopg2.extras import Json
import time
from datetime import datetime, timedelta, timezone
import sys
from dotenv import load_dotenv
from iqoptionapi.stable_api import IQ_Option

import uuid
import platform
import threading

DEBUG_ACTIVE = True


def get_float_env(name: str, default: float = 0.0) -> float:
    value = os.getenv(name)
    if value is None:
        return default

    # Por si alguien vuelve a poner coma
    value = value.replace(",", ".")
    try:
        return float(value)
    except ValueError:
        raise ValueError(f"Valor inválido para {name}: {value}")


def _send_telegram_text(token: str, chat_id: str, text: str) -> bool:
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        import requests
        r = requests.post(url, json=payload, timeout=10)
        return (r.status_code == 200 and r.json().get("ok", False))
    except Exception:
        return False


def clear_console():
    if platform.system() == "Windows":
        os.system("cls")
    else:
        os.system("clear")


def calculate_seconds_to_next_minute():
    now = datetime.now()
    seconds_current_minute = now.second
    microseconds = now.microsecond / 1_000_000
    seconds_to_next_minute = 60 - seconds_current_minute - microseconds
    seconds_to_next_minute += 0.1
    return max(0.1, seconds_to_next_minute)


def ema_series(values, period: int):
    """
    EMA clásica. Devuelve lista misma longitud que values.
    Arranca con SMA del primer 'period' y antes de eso pone None.
    """
    if period <= 0:
        raise ValueError("period must be > 0")
    n = len(values)
    out = [None] * n
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


def check_call_context_debug(candles):
    """
    candles: lista de velas CERRADAS, ordenadas viejo->nuevo.
    Valida contexto CALL en las últimas 10 velas y lo imprime en DEBUG.
    Reglas (por vela):
      1) EMA7 > EMA14 > EMA21
      2) cuerpo por encima de EMA7: min(open, close) > EMA7
      3) mecha no toca EMA14: low > EMA14
      4) pendiente EMA21 positiva: EMA21[i] > EMA21[i-1]
    """
    if len(candles) < 30:
        print("NO: Muy pocas velas para EMA21/validacion decente.")
        return False

    closes = [float(c["close"]) for c in candles]
    e7 = ema_series(closes, 7)
    e14 = ema_series(closes, 14)
    e21 = ema_series(closes, 21)

    last10 = candles[-10:]
    start_idx = len(candles) - 10

    all_ok = True

    #print("Condiciones para contexto CALL:")
    for j, c in enumerate(last10):
        i = start_idx + j  # índice real en arrays EMA
        o = float(c["open"])
        cl = float(c["close"])
        lo = float(c["min"]) if "min" in c else float(c.get("low", 0.0))  # por si tu API usa 'min'/'low'
        t = datetime.fromtimestamp(c["from"]).strftime("%H:%M:%S")

        # Si tu vela trae 'min' y 'max' (iqoptionapi a veces), normaliza:
        if "min" in c:
            lo = float(c["min"])
        elif "low" in c:
            lo = float(c["low"])

        # EMA disponibles?
        if e7[i] is None or e14[i] is None or e21[i] is None:
            motivo = "EMAS_NO_DISPONIBLES"
            ok = False
        else:
            motivo = "OK"
            ok = True

            # Regla 1
            if not (e7[i] > e14[i] > e21[i]):
                ok = False
                motivo = "EMA7>EMA14>EMA21 -> NO"

            # Regla 2
            if ok:
                body_low = min(o, cl)
                if not (body_low > e7[i]):
                    ok = False
                    motivo = "CUERPO TOCA O BAJO EMA7"

            # Regla 3 (mecha)
            if ok:
                if not (lo > e14[i]):
                    ok = False
                    motivo = "MECHA TOCA LA EMA14"

            # Regla 4 (pendiente EMA21)
            if ok:
                if i - 1 < 0 or e21[i - 1] is None:
                    ok = False
                    motivo = "EMA21_PREV_NO_DISPONIBLE"
                elif not (e21[i] > e21[i - 1]):
                    ok = False
                    motivo = "PENDIENTE EMA21 NO POSITIVA"

        status = "OK" if ok else f"NO ({motivo})"
        #print(f"#{j+1:02d} | {t} | OPEN={o:.6f} | CLOSE={cl:.6f} | LOW={lo:.6f} | {status}")

        if not ok:
            all_ok = False

    #print("\n")
    return all_ok


def check_put_context_debug(candles):
    """
    candles: lista de velas CERRADAS, ordenadas viejo->nuevo.
    Valida contexto PUT en las últimas 10 velas y lo imprime en DEBUG.
    Reglas (por vela):
      1) EMA7 < EMA14 < EMA21
      2) cuerpo por debajo de EMA7: max(open, close) < EMA7
      3) mecha no toca EMA14: high < EMA14
      4) pendiente EMA21 negativa: EMA21[i] < EMA21[i-1]
    """
    if len(candles) < 30:
        print("NO: Muy pocas velas para EMA21/validacion decente.")
        return False

    closes = [float(c["close"]) for c in candles]
    e7 = ema_series(closes, 7)
    e14 = ema_series(closes, 14)
    e21 = ema_series(closes, 21)

    last10 = candles[-10:]
    start_idx = len(candles) - 10

    all_ok = True

    #print("Condiciones para contexto PUT:")
    for j, c in enumerate(last10):
        i = start_idx + j  # índice real en arrays EMA
        o = float(c["open"])
        cl = float(c["close"])

        # iqoptionapi suele traer min/max; a veces low/high
        if "max" in c:
            hi = float(c["max"])
        elif "high" in c:
            hi = float(c["high"])
        else:
            hi = 0.0

        t = datetime.fromtimestamp(c["from"]).strftime("%H:%M:%S")

        # EMA disponibles?
        if e7[i] is None or e14[i] is None or e21[i] is None:
            motivo = "EMAS_NO_DISPONIBLES"
            ok = False
        else:
            motivo = "OK"
            ok = True

            # Regla 1
            if not (e7[i] < e14[i] < e21[i]):
                ok = False
                motivo = "EMA7<EMA14<EMA21 -> NO"

            # Regla 2
            if ok:
                body_high = max(o, cl)
                if not (body_high < e7[i]):
                    ok = False
                    motivo = "CUERPO CIERRA SOBRE EMA7"

            # Regla 3 (mecha)
            if ok:
                if not (hi < e14[i]):
                    ok = False
                    motivo = "MECHA TOCA LA EMA14"

            # Regla 4 (pendiente EMA21)
            if ok:
                if i - 1 < 0 or e21[i - 1] is None:
                    ok = False
                    motivo = "EMA21_PREV_NO_DISPONIBLE"
                elif not (e21[i] < e21[i - 1]):
                    ok = False
                    motivo = "PENDIENTE EMA21 NO NEGATIVA"

        status = "OK" if ok else f"NO ({motivo})"
        #print(f"#{j+1:02d} | {t} | OPEN={o:.6f} | CLOSE={cl:.6f} | HIGH={hi:.6f} | {status}")

        if not ok:
            all_ok = False

    return all_ok


def check_call_entry_debug(candles):
    """
    Entrada CALL (DEBUG):
    Valida dos patrones posibles:

    Patron A (3 velas): Alcista + Bajista + Bajista
      - Las 2 bajistas: cierran > EMA7 y low > EMA14

    Patron B (2 velas): Bajista + Alcista
      - Ambas velas: cierran > EMA7 y low > EMA14
    """
    if len(candles) < 30:
        print("ENTRADA CALL: NO (muy pocas velas)")
        return False, None

    closes = [float(c["close"]) for c in candles]
    e7 = ema_series(closes, 7)
    e14 = ema_series(closes, 14)

    def get_low(c):
        if "min" in c: return float(c["min"])
        if "low" in c: return float(c["low"])
        return min(float(c["open"]), float(c["close"]))

    def is_bull(c): return float(c["close"]) > float(c["open"])
    def is_bear(c): return float(c["close"]) < float(c["open"])
    def is_doji(c): return float(c["close"]) == float(c["open"])

    # Obtener últimas velas necesarias
    c_last = candles[-1]
    c_prev1 = candles[-2]
    c_prev2 = candles[-3] if len(candles) >= 3 else None

    i_last = len(candles) - 1
    i_prev1 = len(candles) - 2
    i_prev2 = len(candles) - 3 if len(candles) >= 3 else None

    # Verificar EMAs disponibles
    if e7[i_last] is None or e14[i_last] is None:
        print("ENTRADA CALL: NO (EMAs no disponibles aún)")
        return False, None

    # --- Patron A (3 velas: Bull + Bear + Bear) ---
    if c_prev2 is not None and not (is_doji(c_prev2) or is_doji(c_prev1) or is_doji(c_last)):
        if is_bull(c_prev2) and is_bear(c_prev1) and is_bear(c_last):

            # Validar condiciones para las 2 bajistas (c_prev1 y c_last)
            c1_close = float(c_prev1["close"])
            c2_close = float(c_last["close"])
            c1_low = get_low(c_prev1)
            c2_low = get_low(c_last)

            if not (c1_close > e7[i_prev1]):
                return False, None
            if not (c2_close > e7[i_last]):
                return False, None
            if not (c1_low > e14[i_prev1]):
                return False, None
            if not (c2_low > e14[i_last]):
                return False, None

            return True, "A-B-B"

    # --- Patron B (2 velas: Bear + Bull) ---
    if not (is_doji(c_prev1) or is_doji(c_last)):
        if is_bear(c_prev1) and is_bull(c_last):

            # Validar condiciones para AMBAS velas
            c1_close = float(c_prev1["close"])
            c2_close = float(c_last["close"])
            c1_low = get_low(c_prev1)
            c2_low = get_low(c_last)

            if not (c1_close > e7[i_prev1]):
                return False, None
            if not (c2_close > e7[i_last]):
                return False, None
            if not (c1_low > e14[i_prev1]):
                return False, None
            if not (c2_low > e14[i_last]):
                return False, None

            return True, "B-A"

    return False, None


def check_put_entry_debug(candles):
    """
    Entrada PUT (DEBUG):
    Valida dos patrones posibles:

    Patron A (3 velas): Bajista + Alcista + Alcista
      - Las 2 alcistas: cierran < EMA7 y high < EMA14

    Patron B (2 velas): Alcista + Bajista
      - Ambas velas: cierran < EMA7 y high < EMA14
    """
    if len(candles) < 30:
        print("ENTRADA PUT: NO (muy pocas velas)")
        return False, None

    closes = [float(c["close"]) for c in candles]
    e7 = ema_series(closes, 7)
    e14 = ema_series(closes, 14)

    def get_high(c):
        if "max" in c: return float(c["max"])
        if "high" in c: return float(c["high"])
        return max(float(c["open"]), float(c["close"]))

    def is_bull(c): return float(c["close"]) > float(c["open"])
    def is_bear(c): return float(c["close"]) < float(c["open"])
    def is_doji(c): return float(c["close"]) == float(c["open"])

    # Obtener últimas velas necesarias
    c_last = candles[-1]
    c_prev1 = candles[-2]
    c_prev2 = candles[-3] if len(candles) >= 3 else None

    i_last = len(candles) - 1
    i_prev1 = len(candles) - 2
    i_prev2 = len(candles) - 3 if len(candles) >= 3 else None

    # Verificar EMAs disponibles
    if e7[i_last] is None or e14[i_last] is None:
        print("ENTRADA PUT: NO (EMAs no disponibles aún)")
        return False, None

    # --- Patron A (3 velas: Bear + Bull + Bull) ---
    if c_prev2 is not None and not (is_doji(c_prev2) or is_doji(c_prev1) or is_doji(c_last)):
        if is_bear(c_prev2) and is_bull(c_prev1) and is_bull(c_last):

            # Validar condiciones para las 2 alcistas (c_prev1 y c_last)
            c1_close = float(c_prev1["close"])
            c2_close = float(c_last["close"])
            c1_high = get_high(c_prev1)
            c2_high = get_high(c_last)

            if not (c1_close < e7[i_prev1]):
                return False, None
            if not (c2_close < e7[i_last]):
                return False, None
            if not (c1_high < e14[i_prev1]):
                return False, None
            if not (c2_high < e14[i_last]):
                return False, None

            return True, "B-A-A"

    # --- Patron B (2 velas: Bull + Bear) ---
    if not (is_doji(c_prev1) or is_doji(c_last)):
        if is_bull(c_prev1) and is_bear(c_last):

            # Validar condiciones para AMBAS velas
            c1_close = float(c_prev1["close"])
            c2_close = float(c_last["close"])
            c1_high = get_high(c_prev1)
            c2_high = get_high(c_last)

            if not (c1_close < e7[i_prev1]):
                return False, None
            if not (c2_close < e7[i_last]):
                return False, None
            if not (c1_high < e14[i_prev1]):
                return False, None
            if not (c2_high < e14[i_last]):
                return False, None

            return True, "A-B"

    return False, None


def guardar_operacion(id_conjunto, fecha_op, par, direccion, patron, resultado, contexto_json):
    """
    Guarda una operacion completa en PostgreSQL
    """
    conn = None
    try:
        conn = psycopg2.connect(
            host="163.245.214.198",
            database="context_bot_db",
            user="rolo",
            password="EnzoDaniel*2023"
        )
        cur = conn.cursor()

        insert_sql = """
            INSERT INTO operaciones
            (id_conjunto_velas, fecha_operacion, par, direccion, patron, resultado, contexto)
            VALUES (%s, %s, %s, %s, %s, %s, %s);
        """

        cur.execute(insert_sql, (
            id_conjunto,
            fecha_op,
            par,
            direccion,
            patron,
            resultado,
            Json(contexto_json)  # psycopg2 lo convierte a JSONB
        ))

        conn.commit()
        cur.close()
        print(f"✅ Operacion guardada en BD: {patron} - {resultado}")

    except psycopg2.Error as e:
        print(f"❌ Error de base de datos: {e}")
        if conn:
            conn.rollback()
    except Exception as e:
        print(f"❌ Error general: {e}")
    finally:
        if conn:
            conn.close()


def registrar_operacion_activa(buy_id, asset, direction, duracion_min, username):
    conn = None
    try:
        conn = psycopg2.connect(
            host="163.245.214.198",
            database="context_bot_db",
            user="rolo",
            password="EnzoDaniel*2023"
        )
        cur = conn.cursor()

        expires_at = datetime.now(timezone.utc) + timedelta(minutes=duracion_min, seconds=5)

        cur.execute("""
            INSERT INTO operaciones_activas (buy_id, asset, direction, is_active, expires_at, username)
            VALUES (%s, %s, %s, TRUE, %s, %s)
            ON CONFLICT (buy_id) DO NOTHING;
        """, (int(buy_id), asset, direction, expires_at, username))

        conn.commit()
        cur.close()

    except Exception as e:
        if conn:
            conn.rollback()
        print(f"Error registrando operacion activa: {e}")  # sin tilde

    finally:
        if conn:
            conn.close()


def cerrar_operacion_activa(buy_id, resultado, ganancia=0.0):
    conn = None
    try:
        conn = psycopg2.connect(
            host="163.245.214.198",
            database="context_bot_db",
            user="rolo",
            password="EnzoDaniel*2023"
        )
        cur = conn.cursor()

        cur.execute("""
            UPDATE operaciones_activas
            SET
                is_active = FALSE,
                result = %s,
                profit = %s,
                closed_at = NOW()
            WHERE buy_id = %s
              AND is_active = TRUE;
        """, (resultado, ganancia, int(buy_id)))

        conn.commit()
        cur.close()

    except Exception as e:
        if conn:
            conn.rollback()
        print(f"Error cerrando operacion: {e}")

    finally:
        if conn:
            conn.close()


def borrar_operaciones_usuario(username: str):
    conn = None
    try:
        conn = psycopg2.connect(
            host="163.245.214.198",
            port=5432,
            database="context_bot_db",
            user="rolo",
            password="EnzoDaniel*2023"
        )
        cur = conn.cursor()

        cur.execute("""
            DELETE FROM operaciones_activas
            WHERE username = %s;
        """, (username,))

        conn.commit()
        cur.close()
        print(f"Operaciones del usuario {username} eliminadas correctamente.")

    except Exception as e:
        if conn:
            conn.rollback()
        print(f"Error borrando registros: {e}")

    finally:
        if conn:
            conn.close()


def hay_operacion_activa(username: str) -> bool:
    conn = None
    try:
        conn = psycopg2.connect(
            host="163.245.214.198",
            database="context_bot_db",
            user="rolo",
            password="EnzoDaniel*2023"
        )
        cur = conn.cursor()

        cur.execute("""
            SELECT EXISTS(
                SELECT 1
                FROM operaciones_activas
                WHERE is_active = TRUE
                  AND username = %s
            );
        """, (username,))

        existe = cur.fetchone()[0]
        cur.close()
        return bool(existe)

    except Exception as e:
        print("Error consultando operacion activa:", e)
        return False

    finally:
        if conn:
            conn.close()


def resumen_sesion_stop(username: str, saldo_inicial: float, saldo_actual: float, session_start: datetime):
    """
    Resumen por usuario cuando se active STOP_LOSS o STOP_WIN.
    """
    conn = None
    try:
        conn = psycopg2.connect(
            host="163.245.214.198",
            database="context_bot_db",
            user="rolo",
            password="EnzoDaniel*2023"
        )
        cur = conn.cursor()

        cur.execute("""
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

        total_cerradas, ganadas, perdidas, iguales, errores = cur.fetchone()
        cur.close()

        profit_sesion = float(saldo_actual) - float(saldo_inicial)

        return {
            "total": int(total_cerradas or 0),
            "ganadas": int(ganadas or 0),
            "perdidas": int(perdidas or 0),
            "iguales": int(iguales or 0),
            "errores": int(errores or 0),
            "profit_sesion": round(profit_sesion, 2),
        }

    except Exception as e:
        print(f"Error resumen_sesion_stop: {e}")
        return {
            "total": 100,
            "ganadas": 100,
            "perdidas": 100,
            "iguales": 100,
            "errores": 100,
            "profit_sesion": round(float(saldo_actual) - float(saldo_inicial), 2),
        }

    finally:
        if conn:
            conn.close()


def revisar_stops_si_libre(Iq, saldo_inicial, STOP_WIN, STOP_LOSS, USUARIO):
    """
    Devuelve:
      - "STOP_WIN" si alcanzo el objetivo
      - "STOP_LOSS" si alcanzo el límite
      - None si no se alcanzo nada o si hay operacion activa
    """
    if hay_operacion_activa(USUARIO):
        return None  # NO revisamos stops con trade abierto

    saldo_actual = Iq.get_balance()
    saldo_positivo = saldo_inicial + STOP_WIN
    saldo_negativo = saldo_inicial - STOP_LOSS

    if saldo_actual >= saldo_positivo:
        return "STOP_WIN"

    if saldo_actual <= saldo_negativo:
        return "STOP_LOSS"

    return None


def calcular_profit_acumulado(username: str) -> float:
    conn = None
    try:
        conn = psycopg2.connect(
            host="163.245.214.198",
            database="context_bot_db",
            user="rolo",
            password="EnzoDaniel*2023"
        )
        cur = conn.cursor()

        cur.execute("""
            SELECT COALESCE(SUM(profit), 0)
            FROM operaciones_activas
            WHERE username = %s
              AND is_active = FALSE;
        """, (username,))

        profit = cur.fetchone()[0]
        cur.close()

        return round(float(profit), 2)

    except Exception as e:
        print(f"Error calculando profit: {e}")
        return 0.0
    finally:
        if conn:
            conn.close()


def _build_contexto_json(candles_snapshot, fecha_apertura):
    """Construye el JSON de contexto a partir de un snapshot de velas."""
    closes = [float(c["close"]) for c in candles_snapshot]
    e7 = ema_series(closes, 7)
    e14 = ema_series(closes, 14)
    e21 = ema_series(closes, 21)

    ultimas_10 = candles_snapshot[-10:]
    start_idx = len(candles_snapshot) - 10

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
            "ema_lenta": round(e21[idx], 6) if e21[idx] is not None else None,
        })

    return {
        "velas": velas_json,
        "metadata": {
            "num_velas": len(velas_json),
            "timestamp_analisis": fecha_apertura.isoformat(),
        }
    }


def process_operation_result(iq, iq_lock, buy_id, candles_snapshot, id_conjunto,
                              par, direccion, patron, fecha_op,
                              telegram_token, telegram_chat_id, usuario):
    """
    Thread background: verifica el resultado de la operacion, guarda en BD
    y notifica por Telegram. No bloquea el loop principal.
    """
    try:
        with iq_lock:
            res = iq.check_win_v4(buy_id)

        estado, ganancia = None, None
        if isinstance(res, (list, tuple)) and len(res) >= 2:
            estado = str(res[0]).lower()
            ganancia = float(res[1])
        else:
            ganancia = float(res)
            estado = "win" if ganancia > 0 else ("equal" if ganancia == 0 else "loose")

        if estado in ("loose", "loss", "lose"):
            estado = "loss"

        contexto_json = _build_contexto_json(candles_snapshot, fecha_op)

        cerrar_operacion_activa(buy_id, estado, ganancia)
        guardar_operacion(id_conjunto, fecha_op, par, direccion, patron, estado, contexto_json)
        profit = calcular_profit_acumulado(usuario)

        if estado == "win" and ganancia > 0:
            msg = (
                f"Par: {par}\n"
                f"Velas Previas: {patron}\n"
                f"Resultado: ✅ WIN\n"
                f"Profit: +${ganancia:.2f}\n"
                f"Profit de la sesion: ${profit:+.2f}\n"
            )
        elif estado == "loss" or ganancia < 0:
            msg = (
                f"Par: {par}\n"
                f"Velas Previas: {patron}\n"
                f"Resultado: ❌ LOSS\n"
                f"Profit: -${abs(ganancia):.2f}\n"
                f"Profit de la sesion: ${profit:+.2f}\n"
            )
        else:
            msg = (
                f"Par: {par}\n"
                f"Velas Previas: {patron}\n"
                f"Resultado: ⚪ EQUAL\n"
                f"Profit: ${ganancia:.2f}\n"
                f"Profit de la sesion: ${profit:+.2f}\n"
            )

        _send_telegram_text(telegram_token, telegram_chat_id, msg)

    except Exception as e:
        print(f"[{datetime.now().strftime('%d-%m %H:%M')}] Error al verificar resultado: {e}")


def main():
    load_dotenv()

    email = os.getenv("IQOPTION_EMAIL", "")
    password = os.getenv("IQOPTION_PASSWORD", "")

    TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    STOP_LOSS = float(os.getenv("IQ_STOPLOSS", 0))
    STOP_WIN = float(os.getenv("IQ_STOPWIN", 0))
    MONTO_OPERACIONES = float(os.getenv("MONTO_OPERACIONES", 0))
    USUARIO = os.getenv("USUARIO", "").strip()

    wait_betwween_oper = 2

    if not email or not password:
        raise RuntimeError("Faltan IQOPTION_EMAIL y/o IQOPTION_PASSWORD en el .env.")

    if len(sys.argv) < 2:
        raise RuntimeError("Debes pasar el activo como parámetro. Ej: python3 pullback.py EURGBP-OTC")

    selected_asset = sys.argv[1]
    modo_operacion = sys.argv[2]

    Iq = IQ_Option(email, password)
    Iq.connect()
    Iq.change_balance("PRACTICE")
    saldo_inicial = Iq.get_balance()

    # Lock para serializar llamadas concurrentes a la API de IQ Option
    # (el thread de resultados y el loop principal pueden coincidir)
    iq_lock = threading.Lock()

    call_ctx_active = False
    put_ctx_active = False
    borrar_operaciones_usuario(USUARIO)

    # --- Fetch inicial completo para poblar el cache ---
    with iq_lock:
        raw = Iq.get_candles(selected_asset, 60, 150, time.time())
    candle_cache = sorted(raw, key=lambda x: x["from"])[:-1]  # excluir vela en formacion
    last_candle_time = candle_cache[-1]["from"]

    # --- LOOP PRINCIPAL ---
    while True:

        # Dormir hasta ~0.5s antes del cierre del minuto para reducir polls innecesarios
        sleep_time = calculate_seconds_to_next_minute() - 0.5
        if sleep_time > 0:
            time.sleep(sleep_time)

        # Polling ligero: solo 3 velas hasta detectar la nueva vela cerrada
        while True:
            with iq_lock:
                recent = Iq.get_candles(selected_asset, 60, 3, time.time())
            recent = sorted(recent, key=lambda x: x["from"])[:-1]  # excluir vela en formacion
            if recent and recent[-1]["from"] != last_candle_time:
                # Nueva vela detectada: actualizar cache incrementalmente
                new_candles = [c for c in recent if c["from"] > last_candle_time]
                candle_cache.extend(new_candles)
                candle_cache = candle_cache[-150:]
                last_candle_time = candle_cache[-1]["from"]
                break
            time.sleep(0.1)

        # Nueva vela confirmada: verificar condiciones y operar sin demora
        candles = candle_cache
        clear_console()
        print(f"Monitoreando activo: {selected_asset}")

        # --- Contexto CALL ---
        call_ctx = check_call_context_debug(candles)
        if call_ctx:
            if not call_ctx_active and modo_operacion == "Escaner":
                call_ctx_active = True
                _send_telegram_text(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
                                    f"✅ Contexto Alcista activado en: {selected_asset}")
            if modo_operacion == "Automatico":
                if wait_betwween_oper == 0:
                    entrada_valida, patron = check_call_entry_debug(candles)
                    direccion = "call"
                    if entrada_valida:
                        wait_betwween_oper = 3
                        with iq_lock:
                            ok, buy_id = Iq.buy(MONTO_OPERACIONES, selected_asset, "call", 1)
                        if not ok or not buy_id:
                            _send_telegram_text(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
                                                f"Error al poner la operacion en: {selected_asset}")
                        else:
                            fecha_op = datetime.now()
                            id_conjunto = str(uuid.uuid4())
                            registrar_operacion_activa(buy_id, selected_asset, "call", 1, USUARIO)
                            _send_telegram_text(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
                                                f"Operacion 📈 activa en: {selected_asset}\n")
                            # Procesar resultado en background sin bloquear el loop
                            threading.Thread(
                                target=process_operation_result,
                                args=(Iq, iq_lock, buy_id, list(candles), id_conjunto,
                                      selected_asset, "call", patron, fecha_op,
                                      TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, USUARIO),
                                daemon=True
                            ).start()
        elif not call_ctx and call_ctx_active:
            call_ctx_active = False

        # --- Contexto PUT ---
        put_ctx = check_put_context_debug(candles)
        if put_ctx:
            if not put_ctx_active and modo_operacion == "Escaner":
                put_ctx_active = True
                _send_telegram_text(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
                                    f"✅ Contexto Bajista activado en: {selected_asset}")
            if modo_operacion == "Automatico":
                if wait_betwween_oper == 0:
                    entrada_valida, patron = check_put_entry_debug(candles)
                    direccion = "put"
                    if entrada_valida:
                        wait_betwween_oper = 3
                        with iq_lock:
                            ok, buy_id = Iq.buy(MONTO_OPERACIONES, selected_asset, "put", 1)
                        if not ok or not buy_id:
                            _send_telegram_text(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
                                                f"Error al poner la operacion en: {selected_asset}")
                        else:
                            fecha_op = datetime.now()
                            id_conjunto = str(uuid.uuid4())
                            registrar_operacion_activa(buy_id, selected_asset, "put", 1, USUARIO)
                            _send_telegram_text(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
                                                f"Operacion 📉 activa en: {selected_asset}\n")
                            # Procesar resultado en background sin bloquear el loop
                            threading.Thread(
                                target=process_operation_result,
                                args=(Iq, iq_lock, buy_id, list(candles), id_conjunto,
                                      selected_asset, "put", patron, fecha_op,
                                      TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, USUARIO),
                                daemon=True
                            ).start()
        elif not put_ctx and put_ctx_active:
            put_ctx_active = False

        if modo_operacion == "Automatico":
            if wait_betwween_oper > 0:
                wait_betwween_oper -= 1
            motivo = revisar_stops_si_libre(Iq, saldo_inicial, STOP_WIN, STOP_LOSS, USUARIO)
            if motivo == "STOP_WIN" or motivo == "STOP_LOSS":
                break

    # Si llegamos aqui es que alcanzamos un STOP, vamos a imprimir el resumen
    resumen = resumen_sesion_stop(USUARIO, saldo_inicial, Iq.get_balance(), datetime.now())
    msg = (
        f"Resumen de la Sesión:\n"
        f"📈Total de Operaciones: {resumen['total']}\n"
        f"✅Ganadas: {resumen['ganadas']}\n"
        f"❌Perdidas: {resumen['perdidas']}\n"
        f"💰Profit: {resumen['profit_sesion']}\n"
    )
    _send_telegram_text(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, msg)
    os.system("pkill -f simpleActivo.py")


if __name__ == "__main__":
    main()
