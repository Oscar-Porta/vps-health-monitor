"""
==========================================================
Health Monitor — Comprobación diaria del sistema de trading
==========================================================
Verifica que todos los servicios están activos, sin errores,
y con los archivos en estado correcto. Envía un informe
resumen al canal técnico de Telegram.

Ejecución manual:
  python src/health_check.py

Ejecución automática (cron):
  0 5 * * * cd /home/trading/health_monitor && /home/trading/health_monitor/.venv/bin/python src/health_check.py
  (todos los días a las 07:00 Madrid = 05:00 UTC en verano)
"""

import os
import subprocess
import json
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dotenv import load_dotenv

# ─── Configuración ───────────────────────────────────────

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_TECH_CHAT_ID = os.getenv("TELEGRAM_TECH_CHAT_ID")
SIGNAL_PROJECT_DIR = os.getenv("SIGNAL_PROJECT_DIR", "/home/trading/trading_project_signal")
SERVICES = os.getenv("SERVICES", "trading-signal,trading-monitor,trading-dashboard").split(",")

MADRID_TZ = timezone(timedelta(hours=2))  # CEST (verano)

# Archivos críticos que deben ser escribibles por 'trading'
CRITICAL_FILES = [
    "outputs/live_signals_log.csv",
    "outputs/position_results.csv",
    "outputs/open_positions.json",
]


# ─── Utilidades ──────────────────────────────────────────

def run_cmd(cmd, timeout=10):
    """Ejecuta un comando del sistema y devuelve stdout."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return result.stdout.strip(), result.returncode
    except subprocess.TimeoutExpired:
        return "[TIMEOUT]", -1
    except Exception as e:
        return f"[ERROR: {e}]", -1


def enviar_telegram(mensaje):
    """Envía un mensaje al canal técnico de Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_TECH_CHAT_ID,
        "text": mensaje,
        "parse_mode": "HTML",
    }
    try:
        resp = requests.post(url, data=payload, timeout=10)
        return resp.json().get("ok", False)
    except Exception:
        return False


# ─── Checks individuales ────────────────────────────────

def check_servicios():
    """Comprueba que cada servicio systemd está activo."""
    resultados = []
    todos_ok = True

    for servicio in SERVICES:
        servicio = servicio.strip()
        stdout, code = run_cmd(f"systemctl is-active {servicio}")
        activo = stdout == "active"
        if not activo:
            todos_ok = False

        # Obtener uptime
        uptime_str = ""
        if activo:
            stdout2, _ = run_cmd(
                f"systemctl show {servicio} --property=ActiveEnterTimestamp --value"
            )
            if stdout2:
                uptime_str = f" (desde {stdout2})"

        estado = "OK" if activo else "CAIDO"
        resultados.append(f"  {'✅' if activo else '🔴'} {servicio}: {estado}{uptime_str}")

    return todos_ok, "\n".join(resultados)


def check_errores_recientes():
    """Busca errores en los logs de las últimas 24 horas."""
    errores_encontrados = []

    for servicio in SERVICES:
        servicio = servicio.strip()
        stdout, _ = run_cmd(
            f'journalctl -u {servicio} --since "24 hours ago" --no-pager '
            f'--grep "ERROR|PermissionError|Traceback|CRITICAL" 2>/dev/null | tail -5'
        )
        if stdout and "-- No entries --" not in stdout:
            errores_encontrados.append(f"  ⚠️ {servicio}:\n{stdout}")

    if not errores_encontrados:
        return True, "  ✅ Sin errores en las últimas 24h"
    else:
        return False, "\n".join(errores_encontrados)


def check_permisos_archivos():
    """Verifica que los archivos críticos son escribibles por 'trading'."""
    resultados = []
    todos_ok = True

    for archivo_rel in CRITICAL_FILES:
        archivo = Path(SIGNAL_PROJECT_DIR) / archivo_rel
        if not archivo.exists():
            resultados.append(f"  ⚠️ {archivo_rel}: NO EXISTE")
            todos_ok = False
            continue

        # Comprobar propietario
        stdout, _ = run_cmd(f"stat -c '%U' {archivo}")
        propietario = stdout
        ok = propietario == "trading"

        if not ok:
            todos_ok = False
            resultados.append(f"  🔴 {archivo_rel}: propietario={propietario} (debería ser trading)")
        else:
            resultados.append(f"  ✅ {archivo_rel}: OK")

    return todos_ok, "\n".join(resultados)


def check_ultima_senal():
    """Comprueba cuándo fue la última señal detectada (con o sin entrada)."""
    log_file = Path(SIGNAL_PROJECT_DIR) / "outputs" / "live_signal.log"
    if not log_file.exists():
        return True, "  ⚠️ No existe live_signal.log"

    # Buscar última línea de iteración
    stdout, _ = run_cmd(f'grep "SEÑALES EN VIVO" {log_file} | tail -1')
    if stdout:
        return True, f"  ℹ️ Última comprobación: {stdout[:80]}"
    else:
        return True, "  ⚠️ No se encontraron comprobaciones en el log"


def check_posiciones_abiertas():
    """Verifica el estado de open_positions.json."""
    archivo = Path(SIGNAL_PROJECT_DIR) / "outputs" / "open_positions.json"
    if not archivo.exists():
        return True, "  ⚠️ open_positions.json no existe"

    try:
        with open(archivo) as f:
            data = json.load(f)
        n = len(data) if isinstance(data, list) else 0
        return True, f"  ℹ️ Posiciones abiertas: {n}"
    except Exception as e:
        return False, f"  🔴 Error leyendo open_positions.json: {e}"


def check_disco():
    """Comprueba espacio en disco disponible."""
    stdout, _ = run_cmd("df -h / | tail -1 | awk '{print $5}'")
    uso = stdout.replace("%", "")
    try:
        uso_num = int(uso)
        ok = uso_num < 90
        emoji = "✅" if ok else "🔴"
        return ok, f"  {emoji} Disco usado: {stdout}"
    except ValueError:
        return True, f"  ⚠️ No se pudo leer uso de disco: {stdout}"


def check_memoria():
    """Comprueba uso de memoria RAM."""
    stdout, _ = run_cmd("free -m | grep Mem | awk '{printf \"%.0f\", $3/$2*100}'")
    try:
        uso_num = int(stdout)
        ok = uso_num < 90
        emoji = "✅" if ok else "🔴"
        return ok, f"  {emoji} RAM usada: {uso_num}%"
    except ValueError:
        return True, f"  ⚠️ No se pudo leer uso de RAM"


def check_binance_api():
    """Verifica que la API de Binance responde (market data)."""
    try:
        resp = requests.get(
            "https://api.binance.com/api/v3/ping",
            timeout=10,
        )
        ok = resp.status_code == 200
        emoji = "✅" if ok else "🔴"
        return ok, f"  {emoji} Binance API: {'OK' if ok else f'HTTP {resp.status_code}'}"
    except Exception as e:
        return False, f"  🔴 Binance API: sin respuesta ({e})"


def check_supabase():
    """
    Verifica que Supabase responde Y lo mantiene despierto.

    Doble propósito (revisión jul-2026): los proyectos gratuitos de
    Supabase se PAUSAN tras 7 días sin actividad. Esta consulta diaria
    (a) confirma que la BD responde y avisa si no, y (b) al consultarla,
    reinicia el contador de inactividad → evita que se pause.

    Usa una lectura trivial vía REST (1 fila) con `requests`, sin
    necesidad de la librería supabase. Requiere en el .env:
      SUPABASE_URL=https://<proyecto>.supabase.co
      SUPABASE_KEY=<anon o service_role key>
    Si no están configuradas, se omite sin marcar problema.
    """
    url = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        return True, "  ⚪ Supabase: no configurado (omitido)"
    try:
        resp = requests.get(
            f"{url}/rest/v1/signals",
            params={"select": "id", "limit": 1},
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            timeout=10,
        )
        if resp.status_code == 200:
            return True, "  ✅ Supabase: OK (keepalive diario)"
        return False, (f"  🔴 Supabase: HTTP {resp.status_code} "
                       f"(¿proyecto pausado? revisa el dashboard)")
    except Exception as e:
        return False, f"  🔴 Supabase: sin respuesta ({e})"


# ─── Informe principal ──────────────────────────────────

def ejecutar_health_check():
    """Ejecuta todos los checks y genera el informe."""
    ahora = datetime.now(MADRID_TZ).strftime("%Y-%m-%d %H:%M")
    problemas = 0
    secciones = []

    # 1. Servicios
    ok, detalle = check_servicios()
    if not ok:
        problemas += 1
    secciones.append(f"<b>1. Servicios systemd</b>\n{detalle}")

    # 2. Errores recientes
    ok, detalle = check_errores_recientes()
    if not ok:
        problemas += 1
    secciones.append(f"<b>2. Errores (últimas 24h)</b>\n{detalle}")

    # 3. Permisos
    ok, detalle = check_permisos_archivos()
    if not ok:
        problemas += 1
    secciones.append(f"<b>3. Permisos archivos</b>\n{detalle}")

    # 4. Última señal
    ok, detalle = check_ultima_senal()
    if not ok:
        problemas += 1
    secciones.append(f"<b>4. Última actividad</b>\n{detalle}")

    # 5. Posiciones
    ok, detalle = check_posiciones_abiertas()
    if not ok:
        problemas += 1
    secciones.append(f"<b>5. Posiciones</b>\n{detalle}")

    # 6. Disco
    ok, detalle = check_disco()
    if not ok:
        problemas += 1
    secciones.append(f"<b>6. Disco</b>\n{detalle}")

    # 7. RAM
    ok, detalle = check_memoria()
    if not ok:
        problemas += 1
    secciones.append(f"<b>7. Memoria</b>\n{detalle}")

    # 8. Binance
    ok, detalle = check_binance_api()
    if not ok:
        problemas += 1
    secciones.append(f"<b>8. Binance API</b>\n{detalle}")

    # 9. Supabase (chequeo + keepalive anti-pausa)
    ok, detalle = check_supabase()
    if not ok:
        problemas += 1
    secciones.append(f"<b>9. Supabase</b>\n{detalle}")

    # Cabecera
    if problemas == 0:
        cabecera = f"✅ <b>HEALTH CHECK — {ahora}</b>\nTodo OK"
    else:
        cabecera = f"🔴 <b>HEALTH CHECK — {ahora}</b>\n{problemas} problema(s) detectado(s)"

    informe = cabecera + "\n\n" + "\n\n".join(secciones)

    # Enviar
    enviado = enviar_telegram(informe)
    estado = "enviado" if enviado else "ERROR al enviar"
    print(f"[{ahora}] Health check completado: {problemas} problemas — {estado}")

    return problemas


# ─── Ejecución ───────────────────────────────────────────

if __name__ == "__main__":
    problemas = ejecutar_health_check()
    exit(0 if problemas == 0 else 1)
