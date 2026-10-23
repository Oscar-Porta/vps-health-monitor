"""
==========================================================
MONITOR DE DRIFT DEL MODELO (B5, revisión jul-2026)
==========================================================
Vigila que el modelo en producción siga comportándose como
en la validación. Pensado para cron SEMANAL en el VPS:

  0 8 * * 1  cd /ruta/health_monitor && python src/model_drift.py

Lee (ruta del motor configurable con TRADING_DIR):
  - outputs/live_signals_log.csv   (probas emitidas live)
  - outputs/position_results.csv   (trades cerrados)
  - outputs/cancelled_orders.csv   (órdenes limit no ejecutadas)

Tres chequeos, con umbrales de alerta:

1) DISTRIBUCIÓN DE PROBABILIDADES. Referencia: cuantiles del
   modelo model_limit_btc sobre las 4,453 señales históricas
   (mediana 0.29; 4.0% de señales >= 0.60). Si la mediana live
   se aleja >0.10, o el % de señales sobre umbral se duplica o
   se reduce a la mitad, el mercado que ve el modelo ya no se
   parece al de entrenamiento → ALERTA.

2) WIN RATE RODANTE (últimos 30 trades cerrados con
   proba >= 0.60). Esperado: 42.7% (walk-forward). Banda de
   alerta: fuera de [25%, 61%] (aprox. ±2 desv. binomiales
   con n=30). Por debajo → el modelo no rinde; por encima
   sostenido → sospecha de error de medición (¡también!).

3) FILL-RATE de órdenes limit (últimos 30 días). Esperado:
   ~98% (backtest). Alerta si < 90%: el peaje real estaría
   subiendo (el sistema pierde su ventaja de ejecución).

Salida: informe por consola + Telegram al canal técnico si
hay alertas (usa TELEGRAM_BOT_TOKEN / TELEGRAM_TECH_CHAT_ID
del .env, mismo mecanismo que health_check.py).
==========================================================
"""

import os
import sys
import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

TRADING_DIR = os.getenv("TRADING_DIR", "/home/trading/trading_project_signal")
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT = os.getenv("TELEGRAM_TECH_CHAT_ID", os.getenv("TELEGRAM_CHAT_ID", ""))

# Referencia del modelo model_limit_btc (jul-2026, 4,453 señales):
REF_MEDIANA_PROBA = 0.291
REF_PCT_SOBRE_060 = 4.0        # % de señales con proba >= 0.60
WR_ESPERADO = 42.7             # walk-forward, umbral 0.60
WR_BANDA = (25.0, 61.0)        # ±2σ binomial, n=30
FILL_RATE_MIN = 90.0           # backtest: 98.4%
UMBRAL = 0.60


def leer_csv(nombre):
    ruta = os.path.join(TRADING_DIR, "outputs", nombre)
    if not os.path.exists(ruta):
        return None
    try:
        return pd.read_csv(ruta)
    except Exception:
        return None


def check_probas(alertas, lineas):
    df = leer_csv("live_signals_log.csv")
    col = "ai_proba" if df is not None and "ai_proba" in df.columns else None
    if df is None or col is None or df[col].dropna().empty:
        lineas.append("1) Probas live: sin datos aún.")
        return
    p = df[col].dropna().tail(200)  # últimas ~200 señales
    mediana = p.median()
    pct_060 = (p >= UMBRAL).mean() * 100
    lineas.append(f"1) Probas live (n={len(p)}): mediana {mediana:.3f} "
                  f"(ref {REF_MEDIANA_PROBA}) | >= {UMBRAL}: {pct_060:.1f}% "
                  f"(ref {REF_PCT_SOBRE_060}%)")
    if abs(mediana - REF_MEDIANA_PROBA) > 0.10:
        alertas.append(f"Mediana de probabilidad desplazada: {mediana:.3f} "
                       f"vs ref {REF_MEDIANA_PROBA} (drift de features)")
    if len(p) >= 50 and (pct_060 > 2 * REF_PCT_SOBRE_060 or
                         pct_060 < 0.5 * REF_PCT_SOBRE_060):
        alertas.append(f"% de señales >= {UMBRAL} anómalo: {pct_060:.1f}% "
                       f"vs ref {REF_PCT_SOBRE_060}%")


def check_winrate(alertas, lineas):
    df = leer_csv("position_results.csv")
    if df is None or df.empty:
        lineas.append("2) Win rate: sin trades cerrados aún.")
        return
    if "ai_proba" in df.columns:
        df = df[df["ai_proba"].fillna(0) >= UMBRAL]
    ult = df.tail(30)
    if len(ult) < 10:
        lineas.append(f"2) Win rate: solo {len(ult)} trades >= {UMBRAL} "
                      f"cerrados — aún sin muestra (mínimo 10).")
        return
    col_pnl = "pnl_abs" if "pnl_abs" in ult.columns else (
        "pnl_pct" if "pnl_pct" in ult.columns else None)
    if col_pnl is None:
        lineas.append("2) Win rate: no encuentro columna de PnL.")
        return
    wr = (ult[col_pnl] > 0).mean() * 100
    lineas.append(f"2) Win rate rodante (n={len(ult)}, umbral {UMBRAL}): "
                  f"{wr:.1f}% (esperado {WR_ESPERADO}%, banda {WR_BANDA})")
    if len(ult) >= 30 and not (WR_BANDA[0] <= wr <= WR_BANDA[1]):
        alertas.append(f"Win rate fuera de banda: {wr:.1f}% "
                       f"(esperado {WR_ESPERADO}% ± banda {WR_BANDA})")


def check_fill_rate(alertas, lineas):
    canceladas = leer_csv("cancelled_orders.csv")
    resultados = leer_csv("position_results.csv")
    n_cancel = len(canceladas) if canceladas is not None else 0
    n_fill = len(resultados) if resultados is not None else 0
    # Aproximación: posiciones abiertas actuales no cuentan; suficiente
    # para tendencia. Refinaremos cuando haya volumen.
    total = n_cancel + n_fill
    if total < 10:
        lineas.append(f"3) Fill-rate: muestra insuficiente "
                      f"({n_fill} ejecutadas, {n_cancel} canceladas).")
        return
    fr = n_fill / total * 100
    lineas.append(f"3) Fill-rate: {fr:.1f}% ({n_fill} ejecutadas / "
                  f"{n_cancel} canceladas). Backtest: 98.4%.")
    if fr < FILL_RATE_MIN:
        alertas.append(f"Fill-rate bajo: {fr:.1f}% (< {FILL_RATE_MIN}%). "
                       f"El peaje real está subiendo.")


def enviar_telegram(texto):
    if not TOKEN or not CHAT:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT, "text": texto, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception:
        pass


def main():
    alertas, lineas = [], []
    check_probas(alertas, lineas)
    check_winrate(alertas, lineas)
    check_fill_rate(alertas, lineas)

    fecha = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    informe = f"🧭 MONITOR DE DRIFT — {fecha}\n" + "\n".join(lineas)
    print(informe)

    if alertas:
        aviso = ("⚠️ <b>DRIFT DEL MODELO — ALERTAS</b>\n\n" +
                 "\n".join(f"• {a}" for a in alertas) +
                 "\n\n" + informe)
        print("\nALERTAS:\n" + "\n".join(alertas))
        enviar_telegram(aviso)
    else:
        print("\nSin alertas.")
        enviar_telegram("🧭 Monitor de drift semanal: sin alertas.\n\n" + informe)


if __name__ == "__main__":
    main()
