import os
import psycopg2
from psycopg2.extras import Json
import time
import threading
import queue
from datetime import datetime, timedelta, timezone
import sys
from dotenv import load_dotenv
from iqoptionapi.stable_api import IQ_Option
import uuid
import platform

# --- CONFIGURACIÓN ---
DEBUG_ACTIVE = False  # Cambiar a True solo para debugueo manual, False para producción rápida
VERBOSE_LOGS = False  # Elimina prints innecesarios en producción
CANDLE_COUNT = 150     # Reducido de 150 a 50 (suficiente para EMA21)
ENTRY_DELAY_SEC = 1   # Segundos de espera tras cerrar la vela antes de entrar (para asegurar cierre)

# Cola para procesar resultados en segundo plano
result_queue = queue.Queue()

def get_float_env(name: str, default: float = 0.0) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.replace(",", ".")
    try:
        return float(value)
    except ValueError:
        raise ValueError(f"Valor inválido para {name}: {value}")

def _send_telegram_text_async(token: str, chat_id: str, text: str):
    """Envía mensaje a Telegram en un hilo separado para no bloquear"""
    def _send():
        if not token or not chat_id:
            return
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = { "chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        try:
            import requests
            requests.post(url, json=payload, timeout=5)
        except Exception:
            pass
    threading.Thread(target=_send, daemon=True).start()

def ema_series(values, period: int):
    if period <= 0: raise ValueError("period must be > 0")
    n = len(values)
    out = [None] * n
    if n < period: return out
    k = 2 / (period + 1)
    sma = sum(values[:period]) / period
    out[period - 1] = sma
    prev = sma
    for i in range(period, n):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out

# --- LÓGICA DE SEÑALES (Optimizada sin prints) ---
def check_call_context(candles):
    if len(candles) < 30: return False
    closes = [float(c["close"]) for c in candles]
    e7 = ema_series(closes, 7)
    e14 = ema_series(closes, 14)
    e21 = ema_series(closes, 21)
    last10 = candles[-10:]
    start_idx = len(candles) - 10
    for j, c in enumerate(last10):
        i = start_idx + j
        if e7[i] is None or e14[i] is None or e21[i] is None: return False
        o, cl = float(c["open"]), float(c["close"])
        lo = float(c.get("min", c.get("low", 0.0)))
        if not (e7[i] > e14[i] > e21[i]): return False
        if not (min(o, cl) > e7[i]): return False
        if not (lo > e14[i]): return False
        if i - 1 >= 0 and e21[i] is not None and e21[i-1] is not None:
            if not (e21[i] > e21[i - 1]): return False
    return True

def check_put_context(candles):
    if len(candles) < 30: return False
    closes = [float(c["close"]) for c in candles]
    e7 = ema_series(closes, 7)
    e14 = ema_series(closes, 14)
    e21 = ema_series(closes, 21)
    last10 = candles[-10:]
    start_idx = len(candles) - 10
    for j, c in enumerate(last10):
        i = start_idx + j
        if e7[i] is None or e14[i] is None or e21[i] is None: return False
        o, cl = float(c["open"]), float(c["close"])
        hi = float(c.get("max", c.get("high", 0.0)))
        if not (e7[i] < e14[i] < e21[i]): return False
        if not (max(o, cl) < e7[i]): return False
        if not (hi < e14[i]): return False
        if i - 1 >= 0 and e21[i] is not None and e21[i-1] is not None:
            if not (e21[i] < e21[i - 1]): return False
    return True

def check_call_entry(candles):
    if len(candles) < 30: return False, None
    closes = [float(c["close"]) for c in candles]
    e7 = ema_series(closes, 7)
    e14 = ema_series(closes, 14)
    def get_low(c): return float(c.get("min", c.get("low", min(float(c["open"]), float(c["close"])))))
    def is_bull(c): return float(c["close"]) > float(c["open"])
    def is_bear(c): return float(c["close"]) < float(c["open"])
    def is_doji(c): return float(c["close"]) == float(c["open"])

    c_last, c_prev1 = candles[-1], candles[-2]
    c_prev2 = candles[-3] if len(candles) >= 3 else None
    i_last, i_prev1 = len(candles) - 1, len(candles) - 2
    i_prev2 = len(candles) - 3 if len(candles) >= 3 else None

    if e7[i_last] is None or e14[i_last] is None: return False, None

    # Patron A (3 velas: Bull + Bear + Bear)
    if c_prev2 and not (is_doji(c_prev2) or is_doji(c_prev1) or is_doji(c_last)):
        if is_bull(c_prev2) and is_bear(c_prev1) and is_bear(c_last):
            if (float(c_prev1["close"]) > e7[i_prev1] and float(c_last["close"]) > e7[i_last] and
                get_low(c_prev1) > e14[i_prev1] and get_low(c_last) > e14[i_last]):
                return True, "A-B-B"

    # Patron B (2 velas: Bear + Bull)
    if not (is_doji(c_prev1) or is_doji(c_last)):
        if is_bear(c_prev1) and is_bull(c_last):
            if (float(c_prev1["close"]) > e7[i_prev1] and float(c_last["close"]) > e7[i_last] and
                get_low(c_prev1) > e14[i_prev1] and get_low(c_last) > e14[i_last]):
                return True, "B-A"
    return False, None

def check_put_entry(candles):
    if len(candles) < 30: return False, None
    closes = [float(c["close"]) for c in candles]
    e7 = ema_series(closes, 7)
    e14 = ema_series(closes, 14)
    def get_high(c): return float(c.get("max", c.get("high", max(float(c["open"]), float(c["close"])))))
    def is_bull(c): return float(c["close"]) > float(c["open"])
    def is_bear(c): return float(c["close"]) < float(c["open"])
    def is_doji(c): return float(c["close"]) == float(c["open"])

    c_last, c_prev1 = candles[-1], candles[-2]
    c_prev2 = candles[-3] if len(candles) >= 3 else None
    i_last, i_prev1 = len(candles) - 1, len(candles) - 2
    i_prev2 = len(candles) - 3 if len(candles) >= 3 else None

    if e7[i_last] is None or e14[i_last] is None: return False, None

    # Patron A (3 velas: Bear + Bull + Bull)
    if c_prev2 and not (is_doji(c_prev2) or is_doji(c_prev1) or is_doji(c_last)):
        if is_bear(c_prev2) and is_bull(c_prev1) and is_bull(c_last):
            if (float(c_prev1["close"]) < e7[i_prev1] and float(c_last["close"]) < e7[i_last] and
                get_high(c_prev1) < e14[i_prev1] and get_high(c_last) < e14[i_last]):
                return True, "B-A-A"

    # Patron B (2 velas: Bull + Bear)
    if not (is_doji(c_prev1) or is_doji(c_last)):
        if is_bull(c_prev1) and is_bear(c_last):
            if (float(c_prev1["close"]) < e7[i_prev1] and float(c_last["close"]) < e7[i_last] and
                get_high(c_prev1) < e14[i_prev1] and get_high(c_last) < e14[i_last]):
                return True, "A-B"
    return False, None

# --- GESTIÓN DE BASE DE DATOS Y RESULTADOS (Background) ---
def db_operation_worker():
    """Procesa operaciones de DB y Telegram en segundo plano"""
    while True:
        try:
            task = result_queue.get(timeout=1)
            if task is None: break
            
            task_type = task.get('type')
            
            if task_type == 'SAVE_RESULT':
                _process_trade_result(task)
            elif task_type == 'REGISTER_ACTIVE':
                _register_active_db(task)
                
            result_queue.task_done()
        except queue.Empty:
            continue
        except Exception as e:
            if VERBOSE_LOGS: print(f"Error worker DB: {e}")

def _get_db_connection():
    return psycopg2.connect(
        host="163.245.214.198",
        database="context_bot_db",
        user="rolo",
        password="EnzoDaniel*2023"
    )

def _register_active_db(task):
    conn = None
    try:
        conn = _get_db_connection()
        cur = conn.cursor()
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=task['duration'], seconds=5)
        cur.execute("""
            INSERT INTO operaciones_activas (buy_id, asset, direction, is_active, expires_at, username)
            VALUES (%s, %s, %s, TRUE, %s, %s)
            ON CONFLICT (buy_id) DO NOTHING;
        """, (int(task['buy_id']), task['asset'], task['direction'], expires_at, task['username']))
        conn.commit()
        cur.close()
    except Exception as e:
        if VERBOSE_LOGS: print(f"Error registrando activa: {e}")
    finally:
        if conn: conn.close()

def _process_trade_result(task):
    """Espera el resultado, guarda y notifica"""
    iq = task['iq_instance']
    order_id = task['order_id']
    candles = task['candles']
    id_conjunto = task['id_conjunto']
    par = task['par']
    direccion = task['direccion']
    patron = task['patron']
    fecha_apertura = task['fecha_apertura']
    telegram_token = task['telegram_token']
    chat_id = task['chat_id']
    username = task['username']

    estado, ganancia = "error", 0.0
    try:
        # Reintentos limitados para check_win
        for _ in range(10):
            res = iq.check_win_v4(order_id)
            if isinstance(res, (list, tuple)) and len(res) >= 2:
                estado = str(res[0]).lower()
                ganancia = float(res[1])
            else:
                ganancia = float(res)
                estado = "win" if ganancia > 0 else ("equal" if ganancia == 0 else "loss")
            
            if estado not in ['pending', 'open', '']:
                break
            time.sleep(2)
    except Exception as e:
        if VERBOSE_LOGS: print(f"Error check_win: {e}")

    if estado in ("loose", "lose"): estado = "loss"

    # Guardar en histórico
    conn = None
    try:
        conn = _get_db_connection()
        cur = conn.cursor()
        # Calcular EMAs para contexto
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
                 "open": float(vela["open"]),
                 "close": float(vela["close"]),
                 "ema_rapida": round(e7[idx], 6) if e7[idx] else None,
                 "ema_media": round(e14[idx], 6) if e14[idx] else None,
                 "ema_lenta": round(e21[idx], 6) if e21[idx] else None
            })
        contexto_json = { "velas": velas_json, "metadata": { "timestamp_analisis": fecha_apertura.isoformat() } }
        
        cur.execute("""
            INSERT INTO operaciones (id_conjunto_velas, fecha_operacion, par, direccion, patron, resultado, contexto)
            VALUES (%s, %s, %s, %s, %s, %s, %s);
        """, (id_conjunto, fecha_apertura, par, direccion, patron, estado, Json(contexto_json)))
        conn.commit()
        cur.close()
        
        # Actualizar operación activa
        conn2 = _get_db_connection()
        cur2 = conn2.cursor()
        cur2.execute("""
            UPDATE operaciones_activas SET is_active = FALSE, result = %s, profit = %s, closed_at = NOW()
            WHERE buy_id = %s AND is_active = TRUE;
        """, (estado, ganancia, int(order_id)))
        conn2.commit()
        cur2.close()
        conn2.close()

        # Notificar Telegram
        profit_sesion = _calculate_session_profit(username)
        emoji = "✅" if estado == "win" else ("❌" if estado == "loss" else "⚪")
        msg = (f"Par: {par}\nVelas: {patron}\nResultado: {emoji} {estado.upper()}\n"
               f"Profit: ${ganancia:.2f}\nSesión: ${profit_sesion:+.2f}")
        _send_telegram_text_async(telegram_token, chat_id, msg)

    except Exception as e:
        if VERBOSE_LOGS: print(f"Error guardando resultado: {e}")
    finally:
        if conn: conn.close()

def _calculate_session_profit(username):
    conn = None
    try:
        conn = _get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT COALESCE(SUM(profit), 0) FROM operaciones_activas WHERE username = %s AND is_active = FALSE;", (username,))
        res = cur.fetchone()[0]
        cur.close()
        return round(float(res), 2)
    except: return 0.0
    finally:
        if conn: conn.close()

def revisar_stops(Iq, saldo_inicial, STOP_WIN, STOP_LOSS, USUARIO):
    # Verificación rápida sin DB para stops globales
    saldo_actual = Iq.get_balance()
    if saldo_actual >= saldo_inicial + STOP_WIN: return "STOP_WIN"
    if saldo_actual <= saldo_inicial - STOP_LOSS: return "STOP_LOSS"
    return None

def main():
    load_dotenv()
    email = os.getenv("IQOPTION_EMAIL", "")
    password = os.getenv("IQOPTION_PASSWORD", "")
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    STOP_LOSS = get_float_env("IQ_STOPLOSS", 0)
    STOP_WIN = get_float_env("IQ_STOPWIN", 0)
    MONTO_OPERACIONES = get_float_env("MONTO_OPERACIONES", 0)
    USUARIO = os.getenv("USUARIO", "").strip()

    if not email or not password:
        raise RuntimeError("Faltan credenciales en .env")
    if len(sys.argv) < 2:
        raise RuntimeError("Uso: python3 simpleActivo.py ACTIVO MODO (ej: EURUSD-OTC Automatico)")

    selected_asset = sys.argv[1]
    modo_operacion = sys.argv[2]

    # Iniciar hilo worker para DB/Telegram
    worker_thread = threading.Thread(target=db_operation_worker, daemon=True)
    worker_thread.start()

    Iq = IQ_Option(email, password)
    Iq.connect()
    Iq.change_balance("PRACTICE")
    saldo_inicial = Iq.get_balance()
    
    # Variables de estado
    last_candle_time = 0
    call_ctx_active = False
    put_ctx_active = False
    wait_between_oper = 0
    
    # Notificar inicio
    _send_telegram_text_async(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, f"🤖 Bot iniciado en {selected_asset} ({modo_operacion})")

    try:
        while True:
            # 1. Control de Stops (Rápido)
            if modo_operacion == "Automatico":
                motivo = revisar_stops(Iq, saldo_inicial, STOP_WIN, STOP_LOSS, USUARIO)
                if motivo:
                    _send_telegram_text_async(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, f"🛑 {motivo} alcanzado. Cerrando.")
                    break
            
            # 2. Control de Tiempo para Entrada (Precisión)
            now = datetime.now()
            current_second = now.second
            current_minute_ts = int(now.timestamp()) - current_second
            
            # Esperar activamente cerca del cierre de vela (segundo 58 en adelante)
            if current_second >= 58:
                time.sleep(0.5) # Polling rápido
                continue
            
            # Ejecutar en segundo 1 o 2 de la nueva vela (para asegurar cierre API)
            if current_second != ENTRY_DELAY_SEC:
                time.sleep(0.2)
                continue

            # 3. Fetch Velas (Optimizado)
            try:
                candles = Iq.get_candles(selected_asset, 60, CANDLE_COUNT, time.time())
                if not candles: 
                    time.sleep(1)
                    continue
                candles = sorted(candles, key=lambda x: x["from"])
                # Asegurar que la última vela esté cerrada
                if candles[-1]["from"] >= current_minute_ts:
                    candles = candles[:-1] # Descartar vela actual abierta
                
                if len(candles) < 30:
                    time.sleep(1)
                    continue

                last_closed_ts = candles[-1]["from"]
                if last_closed_ts == last_candle_time:
                    time.sleep(1) # Ya procesamos esta vela
                    continue
                
                last_candle_time = last_closed_ts
                
                # 4. Validación de Señal (Sin prints)
                entry_taken = False
                
                if modo_operacion == "Automatico" and wait_between_oper <= 0:
                    # CALL
                    if check_call_context(candles):
                        if not call_ctx_active:
                            call_ctx_active = True
                            _send_telegram_text_async(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, f"🟢 Contexto CALL activo: {selected_asset}")
                        
                        entry, patron = check_call_entry(candles)
                        if entry:
                            ok, buy_id = Iq.buy(MONTO_OPERACIONES, selected_asset, "call", 1)
                            if ok and buy_id:
                                entry_taken = True
                                direction = "call"
                                wait_between_oper = 3 # Cooldown
                                id_conjunto = str(uuid.uuid4())
                                fecha_op = datetime.now()
                                
                                # Registrar en DB (Async)
                                result_queue.put({
                                    'type': 'REGISTER_ACTIVE',
                                    'buy_id': buy_id, 'asset': selected_asset, 
                                    'direction': direction, 'duration': 1, 'username': USUARIO
                                })
                                # Programar verificación de resultado (Async)
                                # Se hace en otro hilo o se deja para el worker si pasamos el ID
                                # Para simplificar, lanzamos thread específico para este trade
                                threading.Thread(target=_process_trade_result, kwargs={
                                    'iq_instance': Iq, 'order_id': buy_id, 'candles': candles,
                                    'id_conjunto': id_conjunto, 'par': selected_asset,
                                    'direccion': direction, 'patron': patron,
                                    'fecha_apertura': fecha_op, 'telegram_token': TELEGRAM_TOKEN,
                                    'chat_id': TELEGRAM_CHAT_ID, 'username': USUARIO
                                }, daemon=True).start()
                                
                                _send_telegram_text_async(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, f"🚀 Entrada CALL {patron} en {selected_asset}")
                    
                    elif not check_call_context(candles) and call_ctx_active:
                        call_ctx_active = False

                    # PUT
                    if not entry_taken and check_put_context(candles):
                        if not put_ctx_active:
                            put_ctx_active = True
                            _send_telegram_text_async(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, f"🔴 Contexto PUT activo: {selected_asset}")
                        
                        entry, patron = check_put_entry(candles)
                        if entry:
                            ok, buy_id = Iq.buy(MONTO_OPERACIONES, selected_asset, "put", 1)
                            if ok and buy_id:
                                entry_taken = True
                                direction = "put"
                                wait_between_oper = 3
                                id_conjunto = str(uuid.uuid4())
                                fecha_op = datetime.now()
                                
                                result_queue.put({
                                    'type': 'REGISTER_ACTIVE',
                                    'buy_id': buy_id, 'asset': selected_asset, 
                                    'direction': direction, 'duration': 1, 'username': USUARIO
                                })
                                threading.Thread(target=_process_trade_result, kwargs={
                                    'iq_instance': Iq, 'order_id': buy_id, 'candles': candles,
                                    'id_conjunto': id_conjunto, 'par': selected_asset,
                                    'direccion': direction, 'patron': patron,
                                    'fecha_apertura': fecha_op, 'telegram_token': TELEGRAM_TOKEN,
                                    'chat_id': TELEGRAM_CHAT_ID, 'username': USUARIO
                                }, daemon=True).start()
                                
                                _send_telegram_text_async(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, f"🚀 Entrada PUT {patron} en {selected_asset}")
                    
                    elif not check_put_context(candles) and put_ctx_active:
                        put_ctx_active = False
                
                if wait_between_oper > 0:
                    wait_between_oper -= 1

            except Exception as e:
                if VERBOSE_LOGS: print(f"Error en loop principal: {e}")
                time.sleep(2)

    except KeyboardInterrupt:
        print("\nDeteniendo bot...")
    finally:
        result_queue.put(None) # Stop worker
        _send_telegram_text_async(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, "🛑 Bot detenido manualmente.")
        os.system("pkill -f simpleActivo.py")

if __name__ == "__main__":
    main()