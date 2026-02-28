# Bot_1.py
# analizador_patrones_iqoption.py

from iqoptionapi.stable_api import IQ_Option
from collections import defaultdict, Counter
from datetime import datetime, timedelta
from typing import List, Dict, Any, Callable, Tuple, Optional
import time
import sys

# =========================
# CONFIGURACIÓN (SIN CREDENCIALES AQUÍ)
# =========================

PARES = [
    "AUDCAD-OTC",
    "AUDCHF-OTC",
    "AUDUSD-OTC",
    "BCHUSD-OTC",
    "CADCHF-OTC",
    "EURAUD-OTC",
    "EURCAD-OTC",
    "EURCHF-OTC",
    "EURGBP-OTC",
    "EURUSD-OTC",
    "GBPAUD-OTC",
    "GBPCAD-OTC",
    "GBPCHF-OTC",
    "GBPNZD-OTC",
    "GBPUSD-OTC",
    "USDCAD-OTC",
    "USDCHF-OTC",
    "USDHKD-OTC",
    "USDNOK-OTC",
    "USDPLN-OTC",
    "USDSEK-OTC",
    "USDSGD-OTC",
    "USDTHB-OTC",
    "USDTRY-OTC",
    "USDZAR-OTC",
]

TIMEFRAME_SEGUNDOS = 60          # velas de 1 minuto
HORAS_HISTORICO = 10             # 10 horas atrás desde la primera corrida
MIN_OCURRENCIAS = 18             # mínimo de veces que debe aparecer el patrón
RANGO_WINRATE = (0.70, 0.80)     # entre 70% y 80%
MAX_CANDLES_POR_PETICION = 1000  # límite típico cómodo por llamada a IQ
NUM_PERDIDAS_CONSECUTIVAS = 0    # luego puedes poner 1, 2 o 3

# Monto por operación: se setea desde argv
TRADE_AMOUNT = 1.0
STOP_WIN = 1.0
STOP_LOSS = 1.0

# =========================
# TIPOS
# =========================

Candle = Dict[str, Any]
GetCandlesFunc = Callable[[str, datetime, datetime, int], List[Candle]]

# =========================
# UTIL: PARSEO DE MONTO
# =========================

def parse_amount_arg(s: str) -> float:
    """
    Acepta 1,5 o 1.5 o 2
    """
    if s is None:
        raise ValueError("Monto vacío")
    s = s.strip().replace(" ", "").replace(",", ".")
    if not s:
        raise ValueError("Monto vacío")
    # solo dígitos y 1 punto
    if any(c not in "0123456789." for c in s) or s.count(".") > 1:
        raise ValueError(f"Monto inválido: {s}")
    val = float(s)
    if val <= 0:
        raise ValueError("Monto debe ser > 0")
    return val

def usage_and_exit():
    print("Uso:")
    print("  python3 -u Bot_2.py <email> <password> <monto> <stop_win> <stop_loss>")
    print('Ejemplo:')
    print('  python3 -u Bot_2.py "correo@x.com" "miPass" 1,5 3 2')
    sys.exit(2)

def parse_stop_arg(s: str, nombre: str) -> float:
    valor = parse_amount_arg(s)
    if valor <= 0:
        raise ValueError(f"{nombre} debe ser > 0")
    return valor


# =========================
# LÓGICA DE COLORES
# =========================

def clasificar_color(vela: Candle) -> str:
    o = vela["open"]
    c = vela["close"]
    if c > o:
        return "V"
    elif c < o:
        return "R"
    else:
        return "G"

# =========================
# ESTADÍSTICAS + HISTORIAL
# =========================

def construir_estadisticas_y_historial(
    velas: List[Candle]
) -> Tuple[Dict[str, Counter], Dict[str, List[Dict[str, Any]]]]:
    stats: Dict[str, Counter] = defaultdict(Counter)
    historial: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    n = len(velas)
    if n < 6:
        return stats, historial

    colores = [clasificar_color(v) for v in velas]

    for i in range(n - 5):
        patron = "".join(colores[i:i + 5])
        resultado = colores[i + 5]
        vela_resultado = velas[i + 5]

        stats[patron][resultado] += 1
        stats[patron]["total"] += 1

        historial[patron].append({
            "timestamp": vela_resultado["timestamp"],
            "resultado": resultado,
        })

    return stats, historial


def construir_lista_blanca(
    stats: Dict[str, Counter],
    historial: Dict[str, List[Dict[str, Any]]],
    min_ocurrencias: int = MIN_OCURRENCIAS,
    rango_winrate: Tuple[float, float] = RANGO_WINRATE,
    num_perdidas_consecutivas: int = NUM_PERDIDAS_CONSECUTIVAS,
) -> List[Dict[str, Any]]:
    lista_blanca = []

    for patron, conteos in stats.items():
        if len(set(patron)) == 1:
            continue

        total = conteos.get("total", 0)
        if total < min_ocurrencias:
            continue

        wins_por_color = {
            "R": conteos.get("R", 0),
            "V": conteos.get("V", 0),
            "G": conteos.get("G", 0),
        }

        color_dominante, wins_max = max(wins_por_color.items(), key=lambda x: x[1])
        if wins_max == 0:
            continue

        winrate = wins_max / total
        if not (rango_winrate[0] <= winrate <= rango_winrate[1]):
            continue

        ocurrencias_patron = historial.get(patron, [])
        if not ocurrencias_patron:
            continue

        racha_perdidas = 0
        for occ in reversed(ocurrencias_patron):
            if occ["resultado"] == color_dominante:
                break
            racha_perdidas += 1

        if racha_perdidas < num_perdidas_consecutivas:
            continue

        ultimas_racha = ocurrencias_patron[-racha_perdidas:] if racha_perdidas > 0 else []

        lista_blanca.append({
            "patron": patron,
            "color_dominante": color_dominante,
            "winrate": winrate,
            "ocurrencias": total,
            "detalle": wins_por_color,
            "ultimas_ocurrencias": ultimas_racha,
            "racha_perdidas": racha_perdidas,
        })

    lista_blanca.sort(
        key=lambda x: (x["racha_perdidas"], x["winrate"], x["ocurrencias"]),
        reverse=True
    )
    return lista_blanca

# =========================
# IMPRESIÓN DE RESULTADOS
# =========================

def imprimir_lista_blanca(par: str, lista_blanca: List[Dict[str, Any]]) -> None:
    print("=" * 60)
    print(f"PAR: {par}")
    if not lista_blanca:
        print("  No hay patrones válidos (no cumplen filtros).")
        return

    for item in lista_blanca:
        patron = item["patron"]
        color = item["color_dominante"]
        winrate = item["winrate"]
        ocurrencias = item["ocurrencias"]
        detalle = item["detalle"]
        racha_perdidas = item["racha_perdidas"]

        print(f"  Patrón: {patron} -> Color dominante: {color}")
        print(f"    Winrate: {winrate:.2%} | Ocurrencias: {ocurrencias}")
        print(f"    Racha actual de pérdidas: {racha_perdidas}")
        print(f"    Detalle: R={detalle['R']}, V={detalle['V']}, G={detalle['G']}")
    print()

# =========================
# SINCRONIZACIÓN CON CIERRE DE VELAS
# =========================

def segundos_hasta_siguiente_cierre(timeframe_segundos: int) -> int:
    ahora = datetime.now()
    epoch = int(ahora.timestamp())
    resto = epoch % timeframe_segundos
    if resto == 0:
        return timeframe_segundos
    return timeframe_segundos - resto

# =========================
# IQ OPTION: CONEXIÓN Y CANDLES
# =========================

def conectar_iqoption(email: str, password: str) -> IQ_Option:
    iq = IQ_Option(email, password)
    iq.connect()
    if not iq.check_connect():
        raise RuntimeError("No se pudo conectar a IQ Option. Revisa email/password o conexión.")
    print("Conectado a IQ Option.")
    print(email)
    print(password)
    iq.change_balance("PRACTICE")
    return iq


def get_candles_iqoption(
    iq: IQ_Option,
    par: str,
    desde: datetime,
    hasta: datetime,
    timeframe_segundos: int
) -> List[Candle]:
    total_segundos = int((hasta - desde).total_seconds())
    if total_segundos <= 0:
        return []

    num_candles = total_segundos // timeframe_segundos
    if num_candles <= 0:
        num_candles = 1

    end_time = int(hasta.timestamp())
    todas_las_crudas = []
    restantes = num_candles

    while restantes > 0:
        batch = min(restantes, MAX_CANDLES_POR_PETICION)
        data = iq.get_candles(par, timeframe_segundos, batch, end_time)

        if not data:
            break

        todas_las_crudas.extend(data)

        oldest_from = min(c["from"] for c in data)
        end_time = oldest_from - timeframe_segundos

        restantes -= len(data)

        if len(data) < batch:
            break

    if not todas_las_crudas:
        return []

    todas_las_crudas = sorted(todas_las_crudas, key=lambda c: c["from"])

    desde_ts = int(desde.timestamp())
    hasta_ts = int(hasta.timestamp())

    velas: List[Candle] = []
    for c in todas_las_crudas:
        ts = c["from"]
        if ts < desde_ts or ts > hasta_ts:
            continue
        vela = {
            "timestamp": datetime.fromtimestamp(ts),
            "open": c["open"],
            "close": c["close"],
            "high": c["max"],
            "low": c["min"],
        }
        velas.append(vela)

    velas.sort(key=lambda v: v["timestamp"])
    return velas

# =========================
# ANÁLISIS POR PAR
# =========================

def analizar_par(
    par: str,
    hora_inicial: datetime,
    get_candles: GetCandlesFunc
) -> Optional[Dict[str, Any]]:
    ahora = datetime.now()
    velas = get_candles(par, hora_inicial, ahora, TIMEFRAME_SEGUNDOS)

    if not velas:
        print(f"[{par}] Sin velas en el intervalo {hora_inicial} -> {ahora}")
        return None

    stats, historial = construir_estadisticas_y_historial(velas)
    lista_blanca = construir_lista_blanca(stats, historial)
    imprimir_lista_blanca(par, lista_blanca)

    if not lista_blanca:
        return None

    mejor_patron_par = dict(lista_blanca[0])
    mejor_patron_par["par"] = par
    return mejor_patron_par

# =========================
# MONITOREO DEL PATRÓN SELECCIONADO
# =========================

def monitorizar_patron(
    iq: IQ_Option,
    candidato: Dict[str, Any]
) -> Optional[float]:
    par = candidato["par"]
    patron_objetivo = candidato["patron"]
    color_dominante = candidato["color_dominante"]
    racha = candidato.get("racha_perdidas", 0)

    print("#" * 60)
    print("MODO MONITOREO ACTIVADO")
    print(f"Par seleccionado: {par}")
    print(f"Patrón objetivo: {patron_objetivo}")
    print(f"Color dominante: {color_dominante}")
    print(f"Racha de pérdidas actual: {racha}")
    print(f"Importe operación: {TRADE_AMOUNT} USD")
    print("#" * 60)

    while True:
        espera = segundos_hasta_siguiente_cierre(TIMEFRAME_SEGUNDOS)
        time.sleep(espera)

        ahora = datetime.now()
        data = iq.get_candles(par, TIMEFRAME_SEGUNDOS, 5, int(ahora.timestamp()))
        if not data or len(data) < 5:
            print(f"[MONITOR] No se pudieron obtener velas de {par}.")
            continue

        data_ordenada = sorted(data, key=lambda c: c["from"])

        velas = [{
            "timestamp": datetime.fromtimestamp(c["from"]),
            "open": c["open"],
            "close": c["close"],
            "high": c["max"],
            "low": c["min"],
        } for c in data_ordenada]

        colores = [clasificar_color(v) for v in velas]
        patron_actual = "".join(colores)

        if patron_actual == patron_objetivo:
            print(f"[MONITOR] ¡Patrón encontrado! Ejecutando operación...")

            if color_dominante == "V":
                direction = "call"
            elif color_dominante == "R":
                direction = "put"
            else:
                print("[MONITOR] Color dominante G (doji) — no operamos.")
                return None

            monto = float(TRADE_AMOUNT)
            exp = 1  # expiración = 1 minuto

            try:
                status, order_id = iq.buy(monto, par, direction, exp)
                if status is False:
                    print("[MONITOR] ERROR al enviar operación.")
                    return None
                else:
                    print(f"[MONITOR] OPERACIÓN ENVIADA → {par} | {direction.upper()} | ${monto} | exp: 1m")
                    resultado = esperar_resultado_operacion(iq, order_id)
                    if resultado is None:
                        print("[MONITOR] No se pudo obtener el resultado de la operación.")
                        return None

                    print(f"[MONITOR] Resultado operación (PnL): {resultado:.2f} USD")
                    return resultado

            except Exception as e:
                print(f"[MONITOR] EXCEPCIÓN EN OPERACIÓN: {e}")

            
def esperar_resultado_operacion(iq: IQ_Option, order_id: Any, timeout_segundos: int = 180) -> Optional[float]:
    inicio = time.time()
    while (time.time() - inicio) <= timeout_segundos:
        try:
            res = iq.check_win_v4(order_id)
        except Exception:
            time.sleep(2)
            continue

        if res is None:
            time.sleep(2)
            continue

        try:
            if isinstance(res, (list, tuple)) and len(res) >= 2:
                return float(res[1])
            return float(res)
        except (TypeError, ValueError):
            time.sleep(2)

    return None            

# =========================
# BUCLE PRINCIPAL
# =========================

def bucle_analisis(iq: IQ_Option) -> None:
    hora_inicial: Optional[datetime] = None
    profit_sesion = 0.0

    def _get_candles_wrapper(par: str, desde: datetime, hasta: datetime, tf: int) -> List[Candle]:
        return get_candles_iqoption(iq, par, desde, hasta, tf)

    while True:
        ahora = datetime.now()

        if hora_inicial is None:
            hora_inicial = ahora - timedelta(hours=HORAS_HISTORICO)
            print(f"Hora inicial fijada en: {hora_inicial}")

        print("\n" + "#" * 60)
        mejor_candidato_global: Optional[Dict[str, Any]] = None

        for par in PARES:
            candidato_par = analizar_par(par, hora_inicial, _get_candles_wrapper)
            if candidato_par is None:
                continue

            if mejor_candidato_global is None:
                mejor_candidato_global = candidato_par
            else:
                if (
                    candidato_par["racha_perdidas"] > mejor_candidato_global["racha_perdidas"]
                    or (
                        candidato_par["racha_perdidas"] == mejor_candidato_global["racha_perdidas"]
                        and (
                            candidato_par["winrate"] > mejor_candidato_global["winrate"]
                            or (
                                candidato_par["winrate"] == mejor_candidato_global["winrate"]
                                and candidato_par["ocurrencias"] > mejor_candidato_global["ocurrencias"]
                            )
                        )
                    )
                ):
                    mejor_candidato_global = candidato_par

        if mejor_candidato_global is None:
            print("No hay ningún patrón candidato en ningún par. Esperando a la próxima vela para reescanear...")
            espera = segundos_hasta_siguiente_cierre(TIMEFRAME_SEGUNDOS)
            time.sleep(espera)
            continue

        pnl = monitorizar_patron(iq, mejor_candidato_global)
        if pnl is not None:
            profit_sesion += pnl
            print(f"[SESIÓN] Profit acumulado: {profit_sesion:.2f} USD")

            if profit_sesion >= STOP_WIN:
                print(f"[SESIÓN] STOP WIN alcanzado ({STOP_WIN:.2f} USD). Finalizando.")
                return

            if profit_sesion <= -STOP_LOSS:
                print(f"[SESIÓN] STOP LOSS alcanzado (-{STOP_LOSS:.2f} USD). Finalizando.")
                return

        espera = segundos_hasta_siguiente_cierre(TIMEFRAME_SEGUNDOS)
        print(f"[POST-OPERACIÓN] Reanudando fase de escaneo en {espera} s...")
        time.sleep(espera)

# =========================
# MAIN
# =========================

if __name__ == "__main__":
    # argv: Bot_2.py email password amount stop_win stop_loss
    if len(sys.argv) < 6:
        usage_and_exit()

    email = sys.argv[1]
    password = sys.argv[2]
    try:
        TRADE_AMOUNT = parse_amount_arg(sys.argv[3])
        STOP_WIN = parse_stop_arg(sys.argv[4], "STOP_WIN")
        STOP_LOSS = parse_stop_arg(sys.argv[5], "STOP_LOSS")
    except Exception as e:
        print(f"Error en parámetros: {e}")
        usage_and_exit()

    iq = conectar_iqoption(email, password)
    bucle_analisis(iq)