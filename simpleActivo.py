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
import queue
import threading
import atexit


last_candle_time = 0
DEBUG_ACTIVE = True

DB_CONFIG = {
    "host": "69.169.102.33",
    "port": 5432,
    "database": "context_bot_db",
    "user": "rolo",
    "password": "EnzoDaniel*2023"
}

_db_write_queue = queue.Queue()
_db_writer_stop = threading.Event()
_db_writer_thread = None


def _connect_db():
    return psycopg2.connect(**DB_CONFIG)


def _execute_write(cursor, operation, payload):
    if operation == "guardar_operacion":
        cursor.execute(
            """
            INSERT INTO operaciones
            (id_conjunto_velas, fecha_operacion, par, direccion, patron, resultado, contexto)
            VALUES (%s, %s, %s, %s, %s, %s, %s);
            """,
            (
                payload["id_conjunto"],
                payload["fecha_op"],
                payload["par"],
                payload["direccion"],
                payload["patron"],
                payload["resultado"],
                Json(payload["contexto_json"])
            )
        )
        return

    if operation == "registrar_operacion_activa":
        cursor.execute(
            """
            INSERT INTO operaciones_activas (buy_id, asset, direction, is_active, expires_at, username)
            VALUES (%s, %s, %s, TRUE, %s, %s)
            ON CONFLICT (buy_id) DO NOTHING;
            """,
            (
                int(payload["buy_id"]),
                payload["asset"],
                payload["direction"],
                payload["expires_at"],
                payload["username"]
            )
        )
        return

    if operation == "cerrar_operacion_activa":
        cursor.execute(
            """
            UPDATE operaciones_activas
            SET
                is_active = FALSE,
                result = %s,
                profit = %s,
                closed_at = NOW()
            WHERE buy_id = %s
              AND is_active = TRUE;
            """,
            (
                payload["resultado"],
                float(payload["ganancia"]),
                int(payload["buy_id"])
            )
        )
        return

    if operation == "borrar_operaciones_usuario":
        cursor.execute(
            """
            DELETE FROM operaciones_activas
            WHERE username = %s;
            """,
            (payload["username"],)
        )
        return

    raise ValueError(f"Operacion de escritura desconocida: {operation}")


def _db_writer_loop():
    conn = None
    cursor = None
    while True:
        if _db_writer_stop.is_set() and _db_write_queue.empty():
            break

        try:
            operation, payload = _db_write_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        if operation is None:
            continue

        try:
            if conn is None or cursor is None or conn.closed:
                conn = _connect_db()
                cursor = conn.cursor()
            _execute_write(cursor, operation, payload)
            conn.commit()
        except Exception as e:
            if conn:
                conn.rollback()
            print(f"Error en escritura async '{operation}': {e}")
            try:
                if cursor:
                    cursor.close()
            except Exception:
                pass
            try:
                if conn:
                    conn.close()
            except Exception:
                pass
            conn = None
            cursor = None
        finally:
            _db_write_queue.task_done()

    try:
        if cursor:
            cursor.close()
    except Exception:
        pass
    try:
        if conn:
            conn.close()
    except Exception:
        pass


def start_db_writer():
    global _db_writer_thread
    if _db_writer_thread and _db_writer_thread.is_alive():
        return
    _db_writer_stop.clear()
    _db_writer_thread = threading.Thread(target=_db_writer_loop, name="db-writer", daemon=True)
    _db_writer_thread.start()


def stop_db_writer(timeout: float = 5.0):
    _db_writer_stop.set()
    if _db_writer_thread and _db_writer_thread.is_alive():
        _db_writer_thread.join(timeout=timeout)


def enqueue_db_write(operation, payload):
    _db_write_queue.put((operation, payload))


atexit.register(stop_db_writer)


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

    def ts(c): return datetime.fromtimestamp(c["from"]).strftime("%H:%M:%S")

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

    def ts(c): return datetime.fromtimestamp(c["from"]).strftime("%H:%M:%S")

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


def esperar_y_ver_resultado(iq, order_id, duracion_min: int, candles, id_conjunto, par, direccion, patron, fecha_apertura):
    """
    Espera el resultado de la operacion y prepara los datos para guardar.
    Devuelve: (estado, ganancia, contexto_json, id_conjunto, fecha, par, direccion, patron)
    """
    if not order_id:
        return "error", 0.0, None, id_conjunto, datetime.now(), par, direccion, patron
    
    #margen = 5
    #time.sleep(duracion_min * 60 + margen)
    
    try:
        # Obtener resultado de iQOption
        res = iq.check_win_v4(order_id)
        estado, ganancia = None, None
        
        if isinstance(res, (list, tuple)) and len(res) >= 2:
            estado = str(res[0]).lower()
            ganancia = float(res[1])
        else:
            ganancia = float(res)
            estado = "win" if ganancia > 0 else ("equal" if ganancia == 0 else "loose")
        
        # Calcular EMAs para todas las velas
        closes = [float(c["close"]) for c in candles]
        e7 = ema_series(closes, 7)
        e14 = ema_series(closes, 14)
        e21 = ema_series(closes, 21)
        
        # Tomar las últimas 10 velas (que incluyen el patron)
        ultimas_10 = candles[-10:]
        start_idx = len(candles) - 10
        
        # Construir el array de velas para el JSON
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
        
        # Crear el JSON completo
        contexto_json = {
            "velas": velas_json,
            "metadata": {
                "num_velas": len(velas_json),
                "timestamp_analisis": fecha_apertura.isoformat()
            }
        }
        
        return estado, ganancia, contexto_json, id_conjunto, fecha_apertura, par, direccion, patron
        
    except Exception as e:
        #print(f"[{datetime.now().strftime('%d-%m %H:%M')}] Error al verificar resultado: {e}")
        return "error", 0.0, None, id_conjunto, datetime.now(), par, direccion, patron


def guardar_operacion(id_conjunto, fecha_op, par, direccion, patron, resultado, contexto_json):
    
    enqueue_db_write("guardar_operacion", {
        "id_conjunto": id_conjunto,
        "fecha_op": fecha_op,
        "par": par,
        "direccion": direccion,
        "patron": patron,
        "resultado": resultado,
        "contexto_json": contexto_json
    })


def registrar_operacion_activa(buy_id, asset, direction, duracion_min, username):
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=duracion_min, seconds=5)
    enqueue_db_write("registrar_operacion_activa", {
        "buy_id": buy_id,
        "asset": asset,
        "direction": direction,
        "expires_at": expires_at,
        "username": username
    })


def cerrar_operacion_activa(buy_id, resultado, ganancia=0.0):
    enqueue_db_write("cerrar_operacion_activa", {
        "buy_id": buy_id,
        "resultado": resultado,
        "ganancia": ganancia
    })


def hay_operacion_activa(username: str) -> bool:
    conn = None
    try:
        conn = _connect_db()
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
        conn = _connect_db()
        cur = conn.cursor()

        cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE result IS NOT NULL) AS total_cerradas,
                COUNT(*) FILTER (WHERE result = 'win')      AS ganadas,
                COUNT(*) FILTER (WHERE result = 'loss')     AS perdidas,
                COUNT(*) FILTER (WHERE result = 'equal')    AS iguales,
                COUNT(*) FILTER (WHERE result = 'error')    AS errores
            FROM operaciones_activas
            WHERE username = %s;
        """, (username,))

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
        conn = _connect_db()
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


def procesar_resultado_operacion_async(
    iq,
    buy_id,
    last_cicle_candles,
    id_conjunto,
    selected_asset,
    direccion,
    patron,
    fecha_op,
    usuario,
    telegram_token,
    telegram_chat_id,
    operacion_en_curso,
):
    try:
        estado, ganancia, contexto_json, id_conjunto, fecha_op, par, direccion, patron = esperar_y_ver_resultado(
            iq,
            buy_id,
            1,
            last_cicle_candles,
            id_conjunto,
            selected_asset,
            direccion,
            patron,
            fecha_op,
        )
        if estado in ("loose", "loss", "lose"):
            estado = "loss"

        cerrar_operacion_activa(buy_id, estado, ganancia)
        guardar_operacion(id_conjunto, fecha_op, par, direccion, patron, estado, contexto_json)
        profit = calcular_profit_acumulado(usuario)

        if estado == "win" and ganancia > 0:
            msg = (
                f"Par: {selected_asset}\n"
                f"Velas Previas: {patron}\n"
                f"Resultado: ✅ WIN\n"
                f"Profit: +${ganancia:.2f}\n"
                f"Profit de la sesion: ${profit:+.2f}\n"
            )
        elif estado in ("loose", "loss", "lose") or ganancia < 0:
            msg = (
                f"Par: {selected_asset}\n"
                f"Velas Previas: {patron}\n"
                f"Resultado: ❌ LOSS\n"
                f"Profit: -${abs(ganancia):.2f}\n"
                f"Profit de la sesion: ${profit:+.2f}\n"
            )
        else:
            msg = (
                f"Par: {selected_asset}\n"
                f"Velas Previas: {patron}\n"
                f"Resultado: ⚪ EQUAL\n"
                f"Profit: ${ganancia:.2f}\n"
                f"Profit de la sesion: ${profit:+.2f}\n"
            )

        _send_telegram_text(telegram_token, telegram_chat_id, msg)
    except Exception as e:
        print(f"Error procesando resultado async ({buy_id}): {e}")
    finally:
        operacion_en_curso.clear()


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

    
    #selected_asset = "EURUSD-OTC"
    #modo_operacion = "Automatico"
    selected_asset = sys.argv[1]
    modo_operacion = sys.argv[2]


    # --- LOOP PRINCIPAL ---
    Iq = IQ_Option(email, password)
    Iq.connect()
    Iq.change_balance("PRACTICE")
    saldo_inicial = Iq.get_balance()
    global last_candle_time

    call_ctx_active = False
    put_ctx_active = False
    operacion_en_curso = threading.Event()
    start_db_writer()
    enqueue_db_write("borrar_operaciones_usuario", {"username": USUARIO})
    print(f"Monitoreando Activo: {selected_asset}")
    candles_cache = Iq.get_candles(selected_asset, 60, 151, time.time())
    candles_cache = sorted(candles_cache, key=lambda x: x["from"])
    candles_cache = candles_cache[:-1]

    while True:
        recent_candles = Iq.get_candles(selected_asset, 60, 2, time.time())
        recent_candles = sorted(recent_candles, key=lambda x: x["from"])
        recent_candles = recent_candles[:-1]

        if recent_candles:
            last_closed_candle = recent_candles[-1]
            if not candles_cache or last_closed_candle["from"] > candles_cache[-1]["from"]:
                candles_cache.append(last_closed_candle)
                if len(candles_cache) > 150:
                    candles_cache = candles_cache[-150:]
            elif last_closed_candle["from"] == candles_cache[-1]["from"]:
                candles_cache[-1] = last_closed_candle

        candles = candles_cache
        last_cicle_candles = candles[:-1]

        if candles:
            current_candle = candles[-1]
            candle_time = current_candle["from"]
            if candle_time != last_candle_time:
                print(f"Ultima Vela Cerrada: {datetime.fromtimestamp(candle_time).strftime('%Y-%m-%d %H:%M:%S')}")
                #Comprobamos Contexto para entradas CALL
                call_ctx = check_call_context_debug(candles)
                if call_ctx:
                    if not call_ctx_active and modo_operacion == "Escaner":
                        call_ctx_active = True
                        msg = (f"✅ Contexto Alcista activado en: {selected_asset}")
                        ok = _send_telegram_text(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, msg)
                    if modo_operacion == "Automatico":
                        if wait_betwween_oper == 0 and not operacion_en_curso.is_set():
                            entrada_valida, patron = check_call_entry_debug(candles)
                            if entrada_valida:
                                wait_betwween_oper = 3
                                ok, buy_id = Iq.buy(MONTO_OPERACIONES, selected_asset, "call", 1)
                                if not ok or not buy_id:
                                    msg = (f"Error al poner la operacion en: {selected_asset}")
                                    ok = _send_telegram_text(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, msg)
                                else:
                                    id_conjunto = str(uuid.uuid4())
                                    fecha_op = datetime.now()
                                    operacion_en_curso.set()
                                    registrar_operacion_activa(buy_id, selected_asset, "call", 1, USUARIO)
                                    threading.Thread(
                                        target=procesar_resultado_operacion_async,
                                        args=(
                                            Iq,
                                            buy_id,
                                            list(last_cicle_candles),
                                            id_conjunto,
                                            selected_asset,
                                            "call",
                                            patron,
                                            fecha_op,
                                            USUARIO,
                                            TELEGRAM_TOKEN,
                                            TELEGRAM_CHAT_ID,
                                            operacion_en_curso,
                                        ),
                                        daemon=True,
                                    ).start()
                                    msg = (f"Operacion 📈 activa en: {selected_asset}\n")
                                    ok = _send_telegram_text(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, msg)
                elif not call_ctx and call_ctx_active:
                    call_ctx_active = False
            
                #Comprobamos Contexto para entradas PUT
                put_ctx = check_put_context_debug(candles)
                put_ctx = True
                if put_ctx:
                    if not put_ctx_active and modo_operacion == "Escaner":
                        msg = (f"✅ Contexto Bajista activado en: {selected_asset}")
                        ok = _send_telegram_text(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, msg)
                    if modo_operacion == "Automatico":    
                        if wait_betwween_oper == 0 and not operacion_en_curso.is_set():
                            entrada_valida, patron = check_put_entry_debug(candles)
                            if entrada_valida:
                                wait_betwween_oper = 3
                                ok, buy_id = Iq.buy(MONTO_OPERACIONES, selected_asset, "put", 1)
                                if not ok or not buy_id:
                                    msg = (f"Error al poner la operacion en: {selected_asset}")
                                    ok = _send_telegram_text(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, msg)
                                else:
                                    id_conjunto = str(uuid.uuid4())
                                    fecha_op = datetime.now()
                                    operacion_en_curso.set()
                                    registrar_operacion_activa(buy_id, selected_asset, "put", 1, USUARIO)
                                    threading.Thread(
                                        target=procesar_resultado_operacion_async,
                                        args=(
                                            Iq,
                                            buy_id,
                                            list(last_cicle_candles),
                                            id_conjunto,
                                            selected_asset,
                                            "put",
                                            patron,
                                            fecha_op,
                                            USUARIO,
                                            TELEGRAM_TOKEN,
                                            TELEGRAM_CHAT_ID,
                                            operacion_en_curso,
                                        ),
                                        daemon=True,
                                    ).start()
                                    msg = (f"Operacion 📉 activa en: {selected_asset}\n")
                                    ok = _send_telegram_text(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, msg)
                
                elif not put_ctx and put_ctx_active:
                    put_ctx_active = False

                last_candle_time = candle_time
                if modo_operacion == "Automatico":
                    if wait_betwween_oper > 0:
                        wait_betwween_oper = wait_betwween_oper - 1
                    motivo = revisar_stops_si_libre(Iq, saldo_inicial, STOP_WIN, STOP_LOSS, USUARIO)
                    if motivo == "STOP_WIN":
                        break
                    elif motivo == "STOP_LOSS":
                        break


                sleep_time = calculate_seconds_to_next_minute() - 2        
                if sleep_time < 0:
                    sleep_time = 0
                time.sleep(sleep_time)


    #Si llegamos aqui es que alcanzamos un STOP, vamos a imprimir el resumen
    resumen = resumen_sesion_stop(USUARIO,saldo_inicial,Iq.get_balance(),datetime.now())
    msg =   (
            f"Sesión Finalizada:\n" 
            f"📈Total de Operaciones: {resumen['total']}\n"   
            f"✅Ganadas: {resumen['ganadas']}\n"   
            f"❌Perdidas: {resumen['perdidas']}\n"
            f"💰Profit: {resumen['profit_sesion']}\n"
            )
    ok = _send_telegram_text(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, msg)
    stop_db_writer()
    #Para VPS
    os.system("pkill -f simpleActivo.py")
    #Para Windows
    os.system("taskkill /F /IM simpleActivo.exe")


if __name__ == "__main__":
    main()

