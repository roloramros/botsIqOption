import os
import time
import sys
import uuid
from datetime import datetime, timedelta
from dotenv import load_dotenv
from iqoptionapi.stable_api import IQ_Option
import psycopg2
from psycopg2.extras import Json, execute_values

# ============================================
# CONFIGURACIÓN Y FUNCIONES AUXILIARES
# ============================================

def get_float_env(name: str, default: float = 0.0) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.replace(",", ".")
    try:
        return float(value)
    except ValueError:
        raise ValueError(f"Valor inválido para {name}: {value}")

def ema_series(values, period: int):
    """EMA clásica. Devuelve lista misma longitud que values."""
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

def rsi_series(values, period: int = 14):
    """RSI (Wilder). Devuelve lista misma longitud que values."""
    if period <= 0:
        raise ValueError("period must be > 0")
    n = len(values)
    out = [None] * n
    if n <= period:
        return out

    gains = []
    losses = []
    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        out[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        out[period] = 100.0 - (100.0 / (1.0 + rs))

    for i in range(period + 1, n):
        delta = values[i] - values[i - 1]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100.0 - (100.0 / (1.0 + rs))

    return out

def adx_series(highs, lows, closes, period: int = 14):
    """ADX (Wilder). Devuelve (adx, di_plus, di_minus) listas misma longitud."""
    if period <= 0:
        raise ValueError("period must be > 0")
    n = len(closes)
    adx = [None] * n
    di_plus = [None] * n
    di_minus = [None] * n
    if n <= period:
        return adx, di_plus, di_minus

    tr_list = [0.0] * n
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n

    for i in range(1, n):
        high = highs[i]
        low = lows[i]
        prev_close = closes[i - 1]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_list[i] = tr

        up_move = high - highs[i - 1]
        down_move = lows[i - 1] - low
        plus_dm[i] = up_move if up_move > down_move and up_move > 0 else 0.0
        minus_dm[i] = down_move if down_move > up_move and down_move > 0 else 0.0

    tr14 = sum(tr_list[1:period + 1])
    plus_dm14 = sum(plus_dm[1:period + 1])
    minus_dm14 = sum(minus_dm[1:period + 1])

    if tr14 != 0:
        di_plus[period] = 100.0 * (plus_dm14 / tr14)
        di_minus[period] = 100.0 * (minus_dm14 / tr14)
        dx = 100.0 * abs(di_plus[period] - di_minus[period]) / (di_plus[period] + di_minus[period]) if (di_plus[period] + di_minus[period]) != 0 else 0.0
    else:
        dx = 0.0

    dx_list = [None] * n
    dx_list[period] = dx

    for i in range(period + 1, n):
        tr14 = tr14 - (tr14 / period) + tr_list[i]
        plus_dm14 = plus_dm14 - (plus_dm14 / period) + plus_dm[i]
        minus_dm14 = minus_dm14 - (minus_dm14 / period) + minus_dm[i]

        if tr14 != 0:
            di_plus[i] = 100.0 * (plus_dm14 / tr14)
            di_minus[i] = 100.0 * (minus_dm14 / tr14)
            dx = 100.0 * abs(di_plus[i] - di_minus[i]) / (di_plus[i] + di_minus[i]) if (di_plus[i] + di_minus[i]) != 0 else 0.0
        else:
            di_plus[i] = 0.0
            di_minus[i] = 0.0
            dx = 0.0

        dx_list[i] = dx

    adx_start = period * 2
    if adx_start < n:
        adx_init = [v for v in dx_list[period:adx_start + 1] if v is not None]
        if adx_init:
            adx[adx_start] = sum(adx_init) / len(adx_init)

        for i in range(adx_start + 1, n):
            adx[i] = ((adx[i - 1] * (period - 1)) + dx_list[i]) / period if adx[i - 1] is not None else dx_list[i]

    return adx, di_plus, di_minus

# ============================================
# FUNCIONES DE ANÁLISIS (copiadas de tu bot)
# ============================================



def resample_candles_to_5m(candles_1m):
    """Agrupa velas de 1m en velas cerradas de 5m (ancladas por epoch)."""
    if not candles_1m:
        return []

    velas_5m = []
    bucket = None
    for c in candles_1m:
        ts = int(c["from"])
        slot = ts - (ts % 300)
        o = float(c["open"])
        h = float(c.get("max", c.get("high", c["close"])))
        l = float(c.get("min", c.get("low", c["open"])))
        cl = float(c["close"])

        if bucket is None or bucket["from"] != slot:
            if bucket is not None:
                velas_5m.append(bucket)
            bucket = {"from": slot, "open": o, "max": h, "min": l, "close": cl}
        else:
            bucket["max"] = max(bucket["max"], h)
            bucket["min"] = min(bucket["min"], l)
            bucket["close"] = cl

    if bucket is not None:
        velas_5m.append(bucket)

    return velas_5m


def build_ema5m_map_for_1m(candles_1m):
    """
    Devuelve mapa ts_1m -> (ema7_5m, ema14_5m, ema21_5m, ts_ref_5m).
    Usa la ?ltima vela 5m CERRADA respecto a cada vela 1m (sin look-ahead).
    """
    if not candles_1m:
        return {}

    velas_5m = resample_candles_to_5m(candles_1m)
    closes_5m = [float(c["close"]) for c in velas_5m]
    e7_5m = ema_series(closes_5m, 7)
    e14_5m = ema_series(closes_5m, 14)
    e21_5m = ema_series(closes_5m, 21)

    idx_by_slot = {int(v["from"]): i for i, v in enumerate(velas_5m)}
    ema_map = {}
    for c1m in candles_1m:
        ts = int(c1m["from"])
        slot = ts - (ts % 300)
        idx_5m = idx_by_slot.get(slot, -1) - 1
        e7 = e14 = e21 = None
        ts_ref = None

        if 0 <= idx_5m < len(velas_5m):
            ts_ref = int(velas_5m[idx_5m]["from"])
            e7 = e7_5m[idx_5m]
            e14 = e14_5m[idx_5m]
            e21 = e21_5m[idx_5m]

        ema_map[ts] = (e7, e14, e21, ts_ref)

    return ema_map

def check_call_context_debug(candles):
    """Versión simplificada sin prints para backtesting"""
    if len(candles) < 30:
        return False

    closes = [float(c["close"]) for c in candles]
    e7 = ema_series(closes, 7)
    e14 = ema_series(closes, 14)
    e21 = ema_series(closes, 21)

    last10 = candles[-10:]
    start_idx = len(candles) - 10

    for j, c in enumerate(last10):
        i = start_idx + j
        o = float(c["open"])
        cl = float(c["close"])
        lo = float(c.get("min", c.get("low", min(o, cl))))

        if e7[i] is None or e14[i] is None or e21[i] is None:
            return False

        # Regla 1: EMA7 > EMA14 > EMA21
        if not (e7[i] > e14[i] > e21[i]):
            return False

        # Regla 2: cuerpo por encima de EMA7
        if not (min(o, cl) > e7[i]):
            return False

        # Regla 3: mecha no toca EMA14
        if not (lo > e14[i]):
            return False

        # Regla 4: pendiente EMA21 positiva
        if i - 1 < 0 or e21[i - 1] is None:
            return False
        if not (e21[i] > e21[i - 1]):
            return False

    return True

def check_put_context_debug(candles):
    """Versión simplificada sin prints para backtesting"""
    if len(candles) < 30:
        return False

    closes = [float(c["close"]) for c in candles]
    e7 = ema_series(closes, 7)
    e14 = ema_series(closes, 14)
    e21 = ema_series(closes, 21)

    last10 = candles[-10:]
    start_idx = len(candles) - 10

    for j, c in enumerate(last10):
        i = start_idx + j
        o = float(c["open"])
        cl = float(c["close"])
        hi = float(c.get("max", c.get("high", max(o, cl))))

        if e7[i] is None or e14[i] is None or e21[i] is None:
            return False

        # Regla 1: EMA7 < EMA14 < EMA21
        if not (e7[i] < e14[i] < e21[i]):
            return False

        # Regla 2: cuerpo por debajo de EMA7
        if not (max(o, cl) < e7[i]):
            return False

        # Regla 3: mecha no toca EMA14
        if not (hi < e14[i]):
            return False

        # Regla 4: pendiente EMA21 negativa
        if i - 1 < 0 or e21[i - 1] is None:
            return False
        if not (e21[i] < e21[i - 1]):
            return False

    return True

def check_call_entry_debug(candles):
    """
    Versión para backtesting (sin prints)
    Retorna: (bool, str) - (entrada_valida, nombre_patron)
    """
    if len(candles) < 30:
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

    c_last = candles[-1]
    c_prev1 = candles[-2]
    c_prev2 = candles[-3] if len(candles) >= 3 else None

    i_last = len(candles) - 1
    i_prev1 = len(candles) - 2
    i_prev2 = len(candles) - 3 if len(candles) >= 3 else None

    if e7[i_last] is None or e14[i_last] is None:
        return False, None

    # Patrón A (3 velas: Bull + Bear + Bear)
    if c_prev2 is not None and not (is_doji(c_prev2) or is_doji(c_prev1) or is_doji(c_last)):
        if is_bull(c_prev2) and is_bear(c_prev1) and is_bear(c_last):
            c1_close = float(c_prev1["close"])
            c2_close = float(c_last["close"])
            c1_low = get_low(c_prev1)
            c2_low = get_low(c_last)

            if not (c1_close > e7[i_prev1]): return False, None
            if not (c2_close > e7[i_last]): return False, None
            if not (c1_low > e14[i_prev1]): return False, None
            if not (c2_low > e14[i_last]): return False, None

            return True, "A-B-B"

    # Patrón B (2 velas: Bear + Bull)
    if not (is_doji(c_prev1) or is_doji(c_last)):
        if is_bear(c_prev1) and is_bull(c_last):
            c1_close = float(c_prev1["close"])
            c2_close = float(c_last["close"])
            c1_low = get_low(c_prev1)
            c2_low = get_low(c_last)

            if not (c1_close > e7[i_prev1]): return False, None
            if not (c2_close > e7[i_last]): return False, None
            if not (c1_low > e14[i_prev1]): return False, None
            if not (c2_low > e14[i_last]): return False, None

            return True, "B-A"

    return False, None

def check_put_entry_debug(candles):
    """
    Versión para backtesting (sin prints)
    Retorna: (bool, str) - (entrada_valida, nombre_patron)
    """
    if len(candles) < 30:
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

    c_last = candles[-1]
    c_prev1 = candles[-2]
    c_prev2 = candles[-3] if len(candles) >= 3 else None

    i_last = len(candles) - 1
    i_prev1 = len(candles) - 2
    i_prev2 = len(candles) - 3 if len(candles) >= 3 else None

    if e7[i_last] is None or e14[i_last] is None:
        return False, None

    # Patrón A (3 velas: Bear + Bull + Bull)
    if c_prev2 is not None and not (is_doji(c_prev2) or is_doji(c_prev1) or is_doji(c_last)):
        if is_bear(c_prev2) and is_bull(c_prev1) and is_bull(c_last):
            c1_close = float(c_prev1["close"])
            c2_close = float(c_last["close"])
            c1_high = get_high(c_prev1)
            c2_high = get_high(c_last)

            if not (c1_close < e7[i_prev1]): return False, None
            if not (c2_close < e7[i_last]): return False, None
            if not (c1_high < e14[i_prev1]): return False, None
            if not (c2_high < e14[i_last]): return False, None

            return True, "B-A-A"

    # Patrón B (2 velas: Bull + Bear)
    if not (is_doji(c_prev1) or is_doji(c_last)):
        if is_bull(c_prev1) and is_bear(c_last):
            c1_close = float(c_prev1["close"])
            c2_close = float(c_last["close"])
            c1_high = get_high(c_prev1)
            c2_high = get_high(c_last)

            if not (c1_close < e7[i_prev1]): return False, None
            if not (c2_close < e7[i_last]): return False, None
            if not (c1_high < e14[i_prev1]): return False, None
            if not (c2_high < e14[i_last]): return False, None

            return True, "A-B"

    return False, None

# ============================================
# FUNCIONES DE BACKTESTING
# ============================================

def pedir_datos_backtesting_old():
    """
    Pide al usuario los parámetros para el backtesting
    """
    print("\n" + "=" * 50)
    print("📊 CONFIGURACIÓN DE BACKTESTING")
    print("=" * 50)
    
    # Pedir par
    while True:
        par = input("Par (ej: EURUSD-OTC, EURGBP-OTC): ").strip().upper()
        if par:
            break
        print("❌ El par no puede estar vacío")
    
    # Pedir fecha inicio
    while True:
        fecha_inicio_str = input("Fecha inicio (formato: YYYY-MM-DD HH:MM) [ej: 2024-01-01 08:00]: ").strip()
        try:
            fecha_inicio = datetime.strptime(fecha_inicio_str, "%Y-%m-%d %H:%M")
            break
        except ValueError:
            print("❌ Formato incorrecto. Usa: YYYY-MM-DD HH:MM")
    
    # Pedir fecha fin
    while True:
        fecha_fin_str = input("Fecha fin (formato: YYYY-MM-DD HH:MM) [ej: 2024-01-01 20:00]: ").strip()
        try:
            fecha_fin = datetime.strptime(fecha_fin_str, "%Y-%m-%d %H:%M")
            if fecha_fin > fecha_inicio:
                break
            else:
                print("❌ La fecha fin debe ser posterior a la fecha inicio")
        except ValueError:
            print("❌ Formato incorrecto. Usa: YYYY-MM-DD HH:MM")
    
    # Preguntar si guardar en BD
    while True:
        guardar = input("¿Guardar resultados en BD? (s/n): ").strip().lower()
        if guardar in ['s', 'n', 'si', 'no']:
            guardar_en_bd = guardar in ['s', 'si']
            break
        print("❌ Responde s o n")
    
    return par, fecha_inicio, fecha_fin, guardar_en_bd

# Reemplazo de la entrada por consola:
# solo se solicita la fecha (YYYY-MM-DD) y se fija el horario 05:00-18:00.
def pedir_datos_backtesting():
    """
    Pide al usuario la fecha para el backtesting (YYYY-MM-DD).
    El analisis se ejecuta entre 05:00 y 18:00.
    """
    print("\n" + "=" * 50)
    print("CONFIGURACION DE BACKTESTING")
    print("=" * 50)

    # Pares desde .env (lista separada por comas)
    assets_raw = os.getenv("IQ_ASSETS", "").strip()
    assets = [a.strip().upper() for a in assets_raw.split(",") if a.strip()]
    if not assets:
        raise ValueError("Falta IQ_ASSETS en .env (ej: EURUSD-OTC,EURGBP-OTC)")

    # Pedir fecha
    while True:
        fecha_str = input("Fecha de backtest (formato: YYYY-MM-DD) [ej: 2024-01-01]: ").strip()
        try:
            fecha_base = datetime.strptime(fecha_str, "%Y-%m-%d")
            break
        except ValueError:
            print("Formato incorrecto. Usa: YYYY-MM-DD")

    # Guardar en BD por .env (default: s)
    guardar_env = os.getenv("BACKTEST_GUARDAR_BD", "s").strip().lower()
    guardar_en_bd = guardar_env in ["s", "si", "y", "yes", "true", "1"]

    return assets, fecha_base.date(), guardar_en_bd


from datetime import datetime
import time


def obtener_velas_historicas(iq, par, timeframe, desde_timestamp, hasta_timestamp, count=1000):
    """
    Obtiene TODAS las velas en el rango [desde_timestamp, hasta_timestamp]
    iQOption.get_candles(par, timeframe, count, from_time) trae velas hacia ATRÁS desde from_time.
    """
    if desde_timestamp > hasta_timestamp:
        raise ValueError("desde_timestamp no puede ser mayor que hasta_timestamp")

    todas = []
    current_from = hasta_timestamp  # CLAVE: empezar desde el final del rango (lo más reciente)
    max_iteraciones = 1000          # ponlo alto si descargas mucho historial
    iteracion = 0

    while current_from >= desde_timestamp and iteracion < max_iteraciones:
        iteracion += 1

        try:
            velas = iq.get_candles(par, timeframe, count, current_from)
        except Exception as e:
            print(f"❌ Error obteniendo velas: {e}")
            break

        if not velas:
            break

        # Orden consistente
        velas = sorted(velas, key=lambda x: x["from"])

        primera = velas[0]["from"]
        ultima  = velas[-1]["from"]
        todas.extend(velas)

        # siguiente lote: más atrás todavía
        current_from = primera - 1

        # Corte correcto: cuando ya tocamos (o pasamos) el inicio del rango
        if primera <= desde_timestamp:
            break

        time.sleep(0.3)

    # Deduplicar por timestamp
    unicas = {}
    for v in todas:
        unicas[v["from"]] = v

    # Ordenar y filtrar rango exacto
    resultado = [unicas[k] for k in sorted(unicas.keys()) if desde_timestamp <= k <= hasta_timestamp]
    return resultado


def simular_entrada_backtesting(candles, indice_actual, direccion, patron):
    """
    Simula una entrada y determina si hubiera sido ganada o perdida
    """
    if indice_actual + 1 >= len(candles):
        return "error", 0.0
    
    vela_entrada = candles[indice_actual]
    vela_resultado = candles[indice_actual + 1]
    
    precio_entrada = float(vela_entrada["close"])
    
    if direccion == "call":
        if float(vela_resultado["close"]) > precio_entrada:
            return "win", 0.85
        elif float(vela_resultado["close"]) < precio_entrada:
            return "loss", -1.0
        else:
            return "equal", 0.0
    else:  # put
        if float(vela_resultado["close"]) < precio_entrada:
            return "win", 0.85
        elif float(vela_resultado["close"]) > precio_entrada:
            return "loss", -1.0
        else:
            return "equal", 0.0


def preparar_contexto_json(candles, num_velas=10):
    """
    Prepara el JSON con las últimas N velas y sus EMAs
    """
    closes = [float(c["close"]) for c in candles]
    highs = [float(c.get("max", c.get("high", c["close"]))) for c in candles]
    lows = [float(c.get("min", c.get("low", c["open"]))) for c in candles]
    e7 = ema_series(closes, 7)
    e14 = ema_series(closes, 14)
    e21 = ema_series(closes, 21)
    rsi14 = rsi_series(closes, 14)
    adx14, di_plus_14, di_minus_14 = adx_series(highs, lows, closes, 14)
    ema5m_map = build_ema5m_map_for_1m(candles)
    
    ultimas = candles[-num_velas:]
    start_idx = len(candles) - num_velas
    
    velas_json = []
    for i, vela in enumerate(ultimas):
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
            "rsi_14": round(rsi14[idx], 6) if rsi14[idx] is not None else None,
            "adx_14": round(adx14[idx], 6) if adx14[idx] is not None else None,
            "di_plus_14": round(di_plus_14[idx], 6) if di_plus_14[idx] is not None else None,
            "di_minus_14": round(di_minus_14[idx], 6) if di_minus_14[idx] is not None else None,
            "ema_rapida_5m": round(ema5m_map[int(vela["from"])][0], 6) if ema5m_map[int(vela["from"])][0] is not None else None,
            "ema_media_5m": round(ema5m_map[int(vela["from"])][1], 6) if ema5m_map[int(vela["from"])][1] is not None else None,
            "ema_lenta_5m": round(ema5m_map[int(vela["from"])][2], 6) if ema5m_map[int(vela["from"])][2] is not None else None,
            "ema_5m_ref_timestamp": ema5m_map[int(vela["from"])][3]
        })
    
    return {
        "velas": velas_json,
        "metadata": {
            "num_velas": len(velas_json),
            "timestamp_analisis": datetime.now().isoformat()
        }
    }


def guardar_operacion_backtesting(id_conjunto, fecha_op, par, direccion, patron, resultado, contexto_json):
    """
    Guarda una operación de backtesting en PostgreSQL
    """
    try:
        conn = psycopg2.connect(
            host="69.169.102.33",
            database="context_bot_db",
            user="rolo",
            password="EnzoDaniel*2023"
        )
        cur = conn.cursor()
        
        insert_sql = """
            INSERT INTO operaciones_backtesting
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
        conn.close()
        return True
        
    except Exception as e:
        print(f"❌ Error guardando en BD: {e}")
        return False


def ejecutar_backtesting(iq, par, fecha_inicio, fecha_fin, guardar_en_bd=True):
    """
    Ejecuta backtesting para un par en un rango de fechas
    """
    ts_inicio = int(fecha_inicio.timestamp())
    ts_fin = int(fecha_fin.timestamp())
    
    print("\n" + "=" * 60)
    print(f"📊 BACKTESTING PARA {par}")

    # Obtener velas históricas
    velas = obtener_velas_historicas(iq, par, 60, ts_inicio, ts_fin)
    
    if len(velas) < 30:
        print("❌ No hay suficientes velas para analizar")
        return None
    
    # Estadísticas
    stats = {
        "total_ops": 0,
        "ganadas": 0,
        "perdidas": 0,
        "equal": 0,
        "patrones": {
            "A-B-B": {"intentos": 0, "ganadas": 0},
            "B-A": {"intentos": 0, "ganadas": 0},
            "B-A-A": {"intentos": 0, "ganadas": 0},
            "A-B": {"intentos": 0, "ganadas": 0}
        }
    }

    wait_betwween_oper = 0
    operaciones_guardar = []
    
    # Simular vela por vela
    for i in range(30, len(velas) - 1):  # -1 para dejar vela de resultado
        # Ventana de 150 velas (o menos al inicio)
        inicio_ventana = max(0, i - 149)
        ventana = velas[inicio_ventana:i+1]
        
        # Verificar contextos
        call_ctx = check_call_context_debug(ventana)
        put_ctx = check_put_context_debug(ventana)
        
        # Verificar entradas CALL
        if call_ctx:
            if wait_betwween_oper == 0:
                entrada_valida, patron = check_call_entry_debug(ventana)
                if entrada_valida:
                    wait_betwween_oper = 3
                    # Simular entrada
                    estado, ganancia = simular_entrada_backtesting(velas, i, "call", patron)
                    
                    # Actualizar estadísticas
                    stats["total_ops"] += 1
                    stats["patrones"][patron]["intentos"] += 1
                    
                    if estado == "win":
                        stats["ganadas"] += 1
                        stats["patrones"][patron]["ganadas"] += 1
                        resultado_texto = "✅ GANADA"
                    elif estado == "loss":
                        stats["perdidas"] += 1
                        resultado_texto = "❌ PERDIDA"
                    else:
                        stats["equal"] += 1
                        resultado_texto = "⚪ EQUAL"
                    
                    # Acumular para guardar en BD al final
                    if guardar_en_bd:
                        id_conjunto = str(uuid.uuid4())
                        fecha_op = datetime.fromtimestamp(velas[i]["from"])
                        contexto_json = preparar_contexto_json(ventana, 10)
                        operaciones_guardar.append((
                            id_conjunto, fecha_op, par, "call", patron,
                            estado, contexto_json
                        ))
        
        # Verificar entradas PUT
        if put_ctx:
            if wait_betwween_oper == 0:
                entrada_valida, patron = check_put_entry_debug(ventana)
                if entrada_valida:
                    wait_betwween_oper = 3
                    # Simular entrada
                    estado, ganancia = simular_entrada_backtesting(velas, i, "put", patron)
                    
                    # Actualizar estadísticas
                    stats["total_ops"] += 1
                    stats["patrones"][patron]["intentos"] += 1
                    
                    if estado == "win":
                        stats["ganadas"] += 1
                        stats["patrones"][patron]["ganadas"] += 1
                        resultado_texto = "✅ GANADA"
                    elif estado == "loss":
                        stats["perdidas"] += 1
                        resultado_texto = "❌ PERDIDA"
                    else:
                        stats["equal"] += 1
                        resultado_texto = "⚪ EQUAL"
                    
                    # Acumular para guardar en BD al final
                    if guardar_en_bd:
                        id_conjunto = str(uuid.uuid4())
                        fecha_op = datetime.fromtimestamp(velas[i]["from"])
                        contexto_json = preparar_contexto_json(ventana, 10)
                        operaciones_guardar.append((
                            id_conjunto, fecha_op, par, "put", patron,
                            estado, contexto_json
                        ))

        if wait_betwween_oper > 0:
            wait_betwween_oper = wait_betwween_oper - 1

    # Guardar en BD una sola vez al final
    if guardar_en_bd:
        guardar_operaciones_backtesting(operaciones_guardar)

    # Mostrar resultados finales
    if stats["total_ops"] > 0:
        win_rate = (stats["ganadas"] / stats["total_ops"]) * 100
        print(f"Total operaciones: {stats['total_ops']}")
        print(f"✅ Ganadas: {stats['ganadas']} ({win_rate:.2f}%)")
        print(f"❌ Perdidas: {stats['perdidas']} ({(stats['perdidas']/stats['total_ops']*100):.2f}%)")
        print(f"⚪ Equal: {stats['equal']} ({(stats['equal']/stats['total_ops']*100):.2f}%)")
        
        for patron, datos in stats["patrones"].items():
            if datos["intentos"] > 0:
                win_rate_patron = (datos["ganadas"] / datos["intentos"]) * 100
    else:
        print("❌ No se encontraron operaciones en el período")
    
    return stats

# ============================================
# FUNCIÓN PRINCIPAL
# ============================================

def main():
    load_dotenv()
    
    email = os.getenv("IQOPTION_EMAIL", "")
    password = os.getenv("IQOPTION_PASSWORD", "")
    
    if not email or not password:
        print("❌ Faltan credenciales en .env")
        return
    
     # Pedir datos al usuario
    assets, fecha_inicio_base, guardar_en_bd = pedir_datos_backtesting()

    fecha_hoy = datetime.now().date()
    fecha_hasta = fecha_hoy - timedelta(days=1)

    if fecha_inicio_base > fecha_hasta:
        print("❌ La fecha inicial debe ser anterior a hoy para poder analizar días cerrados.")
        return
    
    # Confirmar datos
    print("\n" + "=" * 50)
    print("📋 DATOS CONFIRMADOS:")
    print(f"Pares: {', '.join(assets)}")
    print(f"Rango diario: {fecha_inicio_base} a {fecha_hasta} (05:00-18:00)")
    print(f"Guardar en BD: {'Sí' if guardar_en_bd else 'No'}")
    print("=" * 50)
    
    input("Presiona Enter para comenzar...")
    
    Iq = IQ_Option(email, password)
    Iq.connect()
    print("✅ Conectado!")
    
    
    # Ejecutar backtesting por activo y por día (hasta ayer)
    fecha_actual = fecha_inicio_base
    while fecha_actual <= fecha_hasta:
        fecha_inicio = datetime.combine(fecha_actual, datetime.min.time()).replace(hour=5, minute=0, second=0, microsecond=0)
        fecha_fin = datetime.combine(fecha_actual, datetime.min.time()).replace(hour=18, minute=0, second=0, microsecond=0)

        print("\n" + "-" * 60)
        print(f"📅 Procesando día: {fecha_actual} (05:00-18:00)")

        for par in assets:
            resultados = ejecutar_backtesting(Iq, par, fecha_inicio, fecha_fin, guardar_en_bd)

        if fecha_actual < fecha_hasta:
            print("⏳ Esperando 60 segundos para iniciar el próximo día...")
            time.sleep(90)

        fecha_actual += timedelta(days=1)

    input("Presiona Enter para terminar...")            



def guardar_operaciones_backtesting(operaciones):
    """
    Guarda varias operaciones de backtesting en PostgreSQL en una sola peticion.
    operaciones: lista de tuplas (id_conjunto, fecha_op, par, direccion, patron, resultado, contexto_json)
    """
    if not operaciones:
        return True

    try:
        conn = psycopg2.connect(
            host="69.169.102.33",
            database="context_bot_db",
            user="rolo",
            password="EnzoDaniel*2023"
        )
        cur = conn.cursor()

        insert_sql = """
            INSERT INTO operaciones_backtesting
            (id_conjunto_velas, fecha_operacion, par, direccion, patron, resultado, contexto)
            VALUES %s;
        """

        data = [
            (id_conjunto, fecha_op, par, direccion, patron, resultado, Json(contexto_json))
            for (id_conjunto, fecha_op, par, direccion, patron, resultado, contexto_json)
            in operaciones
        ]

        execute_values(cur, insert_sql, data)

        conn.commit()
        cur.close()
        conn.close()
        return True

    except Exception as e:
        print(f"âŒ Error guardando en BD: {e}")
        return False

if __name__ == "__main__":
    main()
