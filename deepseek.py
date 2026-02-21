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
from queue import Queue
import requests

last_candle_time = 0
DEBUG_ACTIVE = True

# Cache para EMAs y velas
candles_cache = []
ema_cache = {'7': [], '14': [], '21': []}
last_cache_update = 0

def get_float_env(name: str, default: float = 0.0) -> float:
    value = os.getenv(name)
    if value is None:
        return default
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
        r = requests.post(url, json=payload, timeout=10)
        return (r.status_code == 200 and r.json().get("ok", False))
    except Exception:
        return False

def clear_console():
    if platform.system() == "Windows":
        os.system("cls")
    else:
        os.system("clear")

def calculate_ms_to_next_candle():
    """Calcula milisegundos hasta el próximo minuto exacto"""
    now = datetime.now()
    ms_to_next = (60 - now.second) * 1000 - now.microsecond // 1000
    return max(10, ms_to_next)  # Mínimo 10ms

def update_ema_cache(closes):
    """Actualiza el cache de EMAs"""
    global ema_cache
    if len(closes) >= 21:
        ema_cache['7'] = ema_series(closes, 7)
        ema_cache['14'] = ema_series(closes, 14)
        ema_cache['21'] = ema_series(closes, 21)
    return ema_cache

def ema_series(values, period: int):
    """EMA clásica optimizada"""
    if period <= 0:
        raise ValueError("period must be > 0")
    n = len(values)
    out = [None] * n
    if n < period:
        return out
    
    k = 2 / (period + 1)
    sma = sum(values[-period:]) / period
    out[-1] = sma
    prev = sma
    
    for i in range(n-2, -1, -1):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out

# Funciones de validación optimizadas (versiones sin prints)
def check_call_context_fast(candles, e7, e14, e21):
    """Versión rápida de check_call_context_debug sin prints"""
    if len(candles) < 30:
        return False
    
    start_idx = len(candles) - 10
    
    for j in range(10):
        i = start_idx + j
        if e7[i] is None or e14[i] is None or e21[i] is None:
            return False
        
        c = candles[i]
        o = float(c["open"])
        cl = float(c["close"])
        lo = float(c.get("min", c.get("low", o)))
        
        # Regla 1: EMA7 > EMA14 > EMA21
        if not (e7[i] > e14[i] > e21[i]):
            return False
        
        # Regla 2: cuerpo por encima de EMA7
        if min(o, cl) <= e7[i]:
            return False
        
        # Regla 3: mecha no toca EMA14
        if lo <= e14[i]:
            return False
        
        # Regla 4: pendiente EMA21 positiva
        if i > 0 and e21[i-1] is not None:
            if e21[i] <= e21[i-1]:
                return False
    
    return True

def check_put_context_fast(candles, e7, e14, e21):
    """Versión rápida de check_put_context_debug sin prints"""
    if len(candles) < 30:
        return False
    
    start_idx = len(candles) - 10
    
    for j in range(10):
        i = start_idx + j
        if e7[i] is None or e14[i] is None or e21[i] is None:
            return False
        
        c = candles[i]
        o = float(c["open"])
        cl = float(c["close"])
        hi = float(c.get("max", c.get("high", cl)))
        
        # Regla 1: EMA7 < EMA14 < EMA21
        if not (e7[i] < e14[i] < e21[i]):
            return False
        
        # Regla 2: cuerpo por debajo de EMA7
        if max(o, cl) >= e7[i]:
            return False
        
        # Regla 3: mecha no toca EMA14
        if hi >= e14[i]:
            return False
        
        # Regla 4: pendiente EMA21 negativa
        if i > 0 and e21[i-1] is not None:
            if e21[i] >= e21[i-1]:
                return False
    
    return True

def check_call_entry_fast(candles, e7, e14):
    """Versión rápida de check_call_entry_debug sin prints"""
    if len(candles) < 30:
        return False, None
    
    def get_low(c):
        if "min" in c: return float(c["min"])
        if "low" in c: return float(c["low"])
        return min(float(c["open"]), float(c["close"]))
    
    def is_bull(c): return float(c["close"]) > float(c["open"])
    def is_bear(c): return float(c["close"]) < float(c["open"])
    def is_doji(c): return float(c["close"]) == float(c["open"])
    
    c_last = candles[-1]
    c_prev1 = candles[-2]
    c_prev2 = candles[-3] if len(candles) >= 3 else None
    
    i_last = len(candles) - 1
    i_prev1 = len(candles) - 2
    i_prev2 = len(candles) - 3 if len(candles) >= 3 else None
    
    if e7[i_last] is None or e14[i_last] is None:
        return False, None
    
    # Patron A (3 velas: Bull + Bear + Bear)
    if c_prev2 is not None and not (is_doji(c_prev2) or is_doji(c_prev1) or is_doji(c_last)):
        if is_bull(c_prev2) and is_bear(c_prev1) and is_bear(c_last):
            if (float(c_prev1["close"]) > e7[i_prev1] and 
                float(c_last["close"]) > e7[i_last] and
                get_low(c_prev1) > e14[i_prev1] and 
                get_low(c_last) > e14[i_last]):
                return True, "A-B-B"
    
    # Patron B (2 velas: Bear + Bull)
    if not (is_doji(c_prev1) or is_doji(c_last)):
        if is_bear(c_prev1) and is_bull(c_last):
            if (float(c_prev1["close"]) > e7[i_prev1] and 
                float(c_last["close"]) > e7[i_last] and
                get_low(c_prev1) > e14[i_prev1] and 
                get_low(c_last) > e14[i_last]):
                return True, "B-A"
    
    return False, None

def check_put_entry_fast(candles, e7, e14):
    """Versión rápida de check_put_entry_debug sin prints"""
    if len(candles) < 30:
        return False, None
    
    def get_high(c):
        if "max" in c: return float(c["max"])
        if "high" in c: return float(c["high"])
        return max(float(c["open"]), float(c["close"]))
    
    def is_bull(c): return float(c["close"]) > float(c["open"])
    def is_bear(c): return float(c["close"]) < float(c["open"])
    def is_doji(c): return float(c["close"]) == float(c["open"])
    
    c_last = candles[-1]
    c_prev1 = candles[-2]
    c_prev2 = candles[-3] if len(candles) >= 3 else None
    
    i_last = len(candles) - 1
    i_prev1 = len(candles) - 2
    i_prev2 = len(candles) - 3 if len(candles) >= 3 else None
    
    if e7[i_last] is None or e14[i_last] is None:
        return False, None
    
    # Patron A (3 velas: Bear + Bull + Bull)
    if c_prev2 is not None and not (is_doji(c_prev2) or is_doji(c_prev1) or is_doji(c_last)):
        if is_bear(c_prev2) and is_bull(c_prev1) and is_bull(c_last):
            if (float(c_prev1["close"]) < e7[i_prev1] and 
                float(c_last["close"]) < e7[i_last] and
                get_high(c_prev1) < e14[i_prev1] and 
                get_high(c_last) < e14[i_last]):
                return True, "B-A-A"
    
    # Patron B (2 velas: Bull + Bear)
    if not (is_doji(c_prev1) or is_doji(c_last)):
        if is_bull(c_prev1) and is_bear(c_last):
            if (float(c_prev1["close"]) < e7[i_prev1] and 
                float(c_last["close"]) < e7[i_last] and
                get_high(c_prev1) < e14[i_prev1] and 
                get_high(c_last) < e14[i_last]):
                return True, "A-B"
    
    return False, None

# Funciones de BD (sin cambios significativos)
def guardar_operacion(id_conjunto, fecha_op, par, direccion, patron, resultado, contexto_json):
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
            Json(contexto_json)
        ))
        
        conn.commit()
        cur.close()
        print(f"✅ Operacion guardada en BD: {patron} - {resultado}")
        
    except Exception as e:
        print(f"❌ Error guardando operacion: {e}")
        if conn:
            conn.rollback()
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
        print(f"Error registrando operacion activa: {e}")
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
        cur.execute("DELETE FROM operaciones_activas WHERE username = %s;", (username,))
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
            "total": 0, "ganadas": 0, "perdidas": 0,
            "iguales": 0, "errores": 0,
            "profit_sesion": round(float(saldo_actual) - float(saldo_inicial), 2),
        }
    finally:
        if conn:
            conn.close()

def revisar_stops_si_libre(Iq, saldo_inicial, STOP_WIN, STOP_LOSS, USUARIO):
    if hay_operacion_activa(USUARIO):
        return None
    saldo_actual = Iq.get_balance()
    if saldo_actual >= saldo_inicial + STOP_WIN:
        return "STOP_WIN"
    if saldo_actual <= saldo_inicial - STOP_LOSS:
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

def esperar_y_ver_resultado(iq, order_id, duracion_min: int, candles, id_conjunto, par, direccion, patron, fecha_apertura):
    """Versión optimizada de esperar_y_ver_resultado"""
    if not order_id:
        return "error", 0.0, None, id_conjunto, datetime.now(), par, direccion, patron
    
    # Esperar justo lo necesario (duración + 2 segundos de margen)
    time.sleep(duracion_min * 60 + 2)
    
    try:
        res = iq.check_win_v4(order_id)
        estado, ganancia = None, None
        
        if isinstance(res, (list, tuple)) and len(res) >= 2:
            estado = str(res[0]).lower()
            ganancia = float(res[1])
        else:
            ganancia = float(res)
            estado = "win" if ganancia > 0 else ("equal" if ganancia == 0 else "loss")
        
        # Preparar JSON de contexto
        closes = [float(c["close"]) for c in candles]
        e7 = ema_series(closes, 7)
        e14 = ema_series(closes, 14)
        e21 = ema_series(closes, 21)
        
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
        
        contexto_json = {
            "velas": velas_json,
            "metadata": {
                "num_velas": len(velas_json),
                "timestamp_analisis": fecha_apertura.isoformat()
            }
        }
        
        return estado, ganancia, contexto_json, id_conjunto, fecha_apertura, par, direccion, patron
        
    except Exception as e:
        print(f"[{datetime.now().strftime('%d-%m %H:%M')}] Error al verificar resultado: {e}")
        return "error", 0.0, None, id_conjunto, datetime.now(), par, direccion, patron

def procesar_resultado_operacion(result_queue, Iq, buy_id, candles, id_conjunto, selected_asset, direccion, patron, fecha_op, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, USUARIO):
    """Procesa el resultado de una operación en segundo plano"""
    estado, ganancia, contexto_json, _, _, _, _, _ = esperar_y_ver_resultado(
        Iq, buy_id, 1, candles, id_conjunto, selected_asset, direccion, patron, fecha_op
    )
    
    if estado in ("loose", "loss", "lose"):
        estado = "loss"
    
    cerrar_operacion_activa(buy_id, estado, ganancia)
    guardar_operacion(id_conjunto, fecha_op, selected_asset, direccion, patron, estado, contexto_json)
    
    profit = calcular_profit_acumulado(USUARIO)
    
    if estado == "win" and ganancia > 0:
        msg = (f"Par: {selected_asset}\nVelas Previas: {patron}\nResultado: ✅ WIN\n"
               f"Profit: +${ganancia:.2f}\nProfit de la sesion: ${profit:+.2f}\n")
    elif estado in ("loose", "loss", "lose") or ganancia < 0:
        msg = (f"Par: {selected_asset}\nVelas Previas: {patron}\nResultado: ❌ LOSS\n"
               f"Profit: -${abs(ganancia):.2f}\nProfit de la sesion: ${profit:+.2f}\n")
    else:
        msg = (f"Par: {selected_asset}\nVelas Previas: {patron}\nResultado: ⚪ EQUAL\n"
               f"Profit: ${ganancia:.2f}\nProfit de la sesion: ${profit:+.2f}\n")
    
    _send_telegram_text(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, msg)
    result_queue.put(("done", None))

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
    
    if not email or not password:
        raise RuntimeError("Faltan IQOPTION_EMAIL y/o IQOPTION_PASSWORD en el .env.")
    
    if len(sys.argv) < 3:
        raise RuntimeError("Debes pasar el activo y modo como parámetros. Ej: python3 simpleActivo.py EURGBP-OTC Automatico")
    
    selected_asset = sys.argv[1]
    modo_operacion = sys.argv[2]
    
    # Conectar a IQ Option
    Iq = IQ_Option(email, password)
    Iq.connect()
    Iq.change_balance("PRACTICE")
    saldo_inicial = Iq.get_balance()
    
    global last_candle_time, candles_cache, ema_cache
    
    call_ctx_active = False
    put_ctx_active = False
    operacion_en_curso = False
    result_queue = Queue()
    
    borrar_operaciones_usuario(USUARIO)
    
    # Precargar velas iniciales
    candles_cache = Iq.get_candles(selected_asset, 60, 150, time.time())
    candles_cache = sorted(candles_cache, key=lambda x: x["from"])[:-1]
    
    while True:
        try:
            # Sincronización precisa con el cierre de vela
            ms_to_wait = calculate_ms_to_next_candle()
            if ms_to_wait > 50:  # Si falta más de 50ms, esperamos
                time.sleep(ms_to_wait / 1000)
            
            # Momento exacto del cierre de vela
            current_time = time.time()
            new_candles = Iq.get_candles(selected_asset, 60, 2, current_time)
            
            if new_candles:
                # Actualizar cache de velas
                latest_candle = new_candles[-1]
                if not candles_cache or latest_candle["from"] != candles_cache[-1]["from"]:
                    # Nueva vela detectada
                    candles_cache.append(latest_candle)
                    if len(candles_cache) > 150:
                        candles_cache = candles_cache[-150:]
                    
                    # Actualizar EMAs
                    closes = [float(c["close"]) for c in candles_cache]
                    ema_cache = update_ema_cache(closes)
                    
                    # Limpiar consola y mostrar info
                    clear_console()
                    print(f"Monitoreando activo: {selected_asset} - Modo: {modo_operacion}")
                    print(f"Hora: {datetime.now().strftime('%H:%M:%S')}")
                    
                    # Si hay operación en curso, no procesar nuevas entradas
                    if operacion_en_curso:
                        continue
                    
                    # Verificar stops si no hay operación activa
                    if modo_operacion == "Automatico":
                        motivo_stop = revisar_stops_si_libre(Iq, saldo_inicial, STOP_WIN, STOP_LOSS, USUARIO)
                        if motivo_stop:
                            break
                    
                    # Verificar contextos y entradas (usando versiones rápidas)
                    e7 = ema_cache['7']
                    e14 = ema_cache['14']
                    e21 = ema_cache['21']
                    
                    # CALL
                    call_ctx = check_call_context_fast(candles_cache, e7, e14, e21)
                    if call_ctx:
                        if not call_ctx_active and modo_operacion == "Escaner":
                            call_ctx_active = True
                            _send_telegram_text(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, 
                                               f"✅ Contexto Alcista activado en: {selected_asset}")
                        
                        if modo_operacion == "Automatico" and not operacion_en_curso:
                            entrada_valida, patron = check_call_entry_fast(candles_cache, e7, e14)
                            if entrada_valida:
                                # Ejecutar compra inmediatamente
                                ok, buy_id = Iq.buy(MONTO_OPERACIONES, selected_asset, "call", 1)
                                if ok and buy_id:
                                    operacion_en_curso = True
                                    registrar_operacion_activa(buy_id, selected_asset, "call", 1, USUARIO)
                                    id_conjunto = str(uuid.uuid4())
                                    fecha_op = datetime.now()
                                    
                                    _send_telegram_text(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, 
                                                       f"Operación 📈 activa en: {selected_asset}")
                                    
                                    # Iniciar hilo para procesar resultado
                                    threading.Thread(
                                        target=procesar_resultado_operacion,
                                        args=(result_queue, Iq, buy_id, candles_cache[:-1], 
                                              id_conjunto, selected_asset, "call", patron, 
                                              fecha_op, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, USUARIO),
                                        daemon=True
                                    ).start()
                    
                    elif not call_ctx and call_ctx_active:
                        call_ctx_active = False
                    
                    # PUT
                    put_ctx = check_put_context_fast(candles_cache, e7, e14, e21)
                    if put_ctx:
                        if not put_ctx_active and modo_operacion == "Escaner":
                            put_ctx_active = True
                            _send_telegram_text(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, 
                                               f"✅ Contexto Bajista activado en: {selected_asset}")
                        
                        if modo_operacion == "Automatico" and not operacion_en_curso:
                            entrada_valida, patron = check_put_entry_fast(candles_cache, e7, e14)
                            if entrada_valida:
                                ok, buy_id = Iq.buy(MONTO_OPERACIONES, selected_asset, "put", 1)
                                if ok and buy_id:
                                    operacion_en_curso = True
                                    registrar_operacion_activa(buy_id, selected_asset, "put", 1, USUARIO)
                                    id_conjunto = str(uuid.uuid4())
                                    fecha_op = datetime.now()
                                    
                                    _send_telegram_text(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, 
                                                       f"Operación 📉 activa en: {selected_asset}")
                                    
                                    # Iniciar hilo para procesar resultado
                                    threading.Thread(
                                        target=procesar_resultado_operacion,
                                        args=(result_queue, Iq, buy_id, candles_cache[:-1], 
                                              id_conjunto, selected_asset, "put", patron, 
                                              fecha_op, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, USUARIO),
                                        daemon=True
                                    ).start()
                    
                    elif not put_ctx and put_ctx_active:
                        put_ctx_active = False
                    
                    # Verificar si terminó alguna operación
                    if not result_queue.empty():
                        result_queue.get()
                        operacion_en_curso = False
            
        except Exception as e:
            print(f"Error en loop principal: {e}")
            time.sleep(1)
    
    # Resumen final
    resumen = resumen_sesion_stop(USUARIO, saldo_inicial, Iq.get_balance(), datetime.now())
    msg = (f"Resumen de la Sesión:\n📈Total de Operaciones: {resumen['total']}\n"
           f"✅Ganadas: {resumen['ganadas']}\n❌Perdidas: {resumen['perdidas']}\n"
           f"💰Profit: {resumen['profit_sesion']}\n")
    _send_telegram_text(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, msg)
    os.system("pkill -f simpleActivo.py")

if __name__ == "__main__":
    main()