import os
import psycopg2
from psycopg2.extras import Json
import time
from datetime import datetime, timedelta, timezone
import sys
from dotenv import load_dotenv
from iqoptionapi.stable_api import IQ_Option


def resumen_sesion_stop(username: str, saldo_inicial: float, saldo_actual: float, session_start: datetime):
    """
    Resumen por usuario cuando se active STOP_LOSS o STOP_WIN.
    """
    conn = None
    try:
        conn = psycopg2.connect(
            host="69.169.102.33",
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

def main():
    resumen = resumen_sesion_stop("rolo", 95.65, 90.03, datetime.now())
    msg =   (
            f"Resumen de la Sesión:\n" 
            f"📈Total de Operaciones: {resumen['total']}\n"   
            f"✅Ganadas: {resumen['ganadas']}\n"   
            f"❌Perdidas: {resumen['perdidas']}\n"
            f"💰Profit: {resumen['profit_sesion']}\n"
            )
    print(msg)

if __name__ == "__main__":
    main()            


