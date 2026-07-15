from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timezone
import requests
import re
import os
import math
import urllib3

# Desactivar advertencias SSL
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = FastAPI(title="USDT/VES y BCV API")

# Middleware CORS para permitir peticiones desde cualquier origen
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

cache = {
    "last_update": None,
    "usdt_price": 0.00,
    "bcv_price": 0.00,
    "count": 0,
    "error": None
}

URL_BINANCE = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
HEADERS_BINANCE = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0"
}

def fetch_binance_prices(trade_type, pro_merchant_only, rows=15):
    """Consulta Binance P2P y devuelve la lista de precios de los anuncios encontrados,
    en el mismo orden en que Binance los entrega (mejor precio primero)."""
    payload = {
        "page": 1,
        "rows": rows,
        "payTypes": [],
        "asset": "USDT",
        "fiat": "VES",
        "tradeType": trade_type,
        "proMerchantAds": pro_merchant_only
    }
    r_bin = requests.post(URL_BINANCE, headers=HEADERS_BINANCE, json=payload, timeout=15)
    r_bin.raise_for_status()
    data = r_bin.json()
    ads = data.get("data", []) or []
    return [float(ad.get("adv", {}).get("price")) for ad in ads if ad.get("adv", {}).get("price")]


def fetch_data():
    error_msg = None

    # 1. Obtener precio Binance USDT (Venta de USDT)
    # Se usa el mercado general (sin filtrar por "Comerciante Pro"): ese filtro deja
    # a veces una muestra muy chica (3-4 anuncios) y poco representativa del precio
    # real, ya que suelen ser más conservadores que el mercado abierto.
    try:
        prices = fetch_binance_prices("SELL", pro_merchant_only=False)

        if prices:
            # Binance no garantiza que el orden de la respuesta sea por precio, así que
            # ordenamos explícitamente antes de tomar los "mejores N" para promediar.
            N = 7
            prices_sorted = sorted(prices, reverse=True)  # de mayor a menor (mejor para vender)
            top_n = prices_sorted[:N]
            avg_price = sum(top_n) / len(top_n)
            cache["usdt_price"] = round(avg_price, 4)
            cache["count"] = len(top_n)
        else:
            error_msg = "No se encontraron anuncios de venta"
    except Exception as e:
        error_msg = f"Error Binance: {str(e)}"

    # 2. Obtener precio Oficial BCV
    try:
        headers_bcv = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        r_bcv = requests.get("https://www.bcv.org.ve/", headers=headers_bcv, verify=False, timeout=15)
        r_bcv.raise_for_status()

        match = re.search(r'id="dolar"[\s\S]*?([\d]+,[\d]+)', r_bcv.text, re.IGNORECASE)

        if match:
            precio_str = match.group(1).replace(',', '.')
            cache["bcv_price"] = round(float(precio_str), 2)
        else:
            raise ValueError("No se detectó el precio en la web del BCV")

    except Exception as e:
        error_bcv = f"Error BCV: {str(e)}"
        error_msg = f"{error_msg} | {error_bcv}" if error_msg else error_bcv

    cache["error"] = error_msg
    cache["last_update"] = datetime.now(timezone.utc).isoformat()

    if not error_msg and cache["usdt_price"] and cache["bcv_price"]:
        try:
            notify_price_update(cache["bcv_price"], cache["usdt_price"])
        except Exception as e:
            print(f"[telegram] error notificando actualización: {e}")

# Configuración del scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(fetch_data, "interval", minutes=1)
scheduler.start()

@app.on_event("startup")
def startup_event():
    fetch_data()

@app.api_route("/", methods=["GET", "HEAD", "OPTIONS"])
def health_check():
    # Ruta simple para health checks de Render (o de cualquier monitor externo).
    # Acepta GET/HEAD/OPTIONS porque distintos monitores (UptimeRobot, etc.) usan
    # métodos distintos para revisar si el sitio está vivo.
    return {"status": "ok"}

@app.get("/v1/usdt")
def get_rates():
    return {
        "last_update": cache["last_update"],
        "usdt_price": cache["usdt_price"],
        "bcv_price": cache["bcv_price"],
        "ads_used": cache["count"],
        "error": cache["error"]
    }


# ============================================================
# TELEGRAM BOT — comandos privados que publican en el canal
# ============================================================

# Este valor se configura como variable de entorno en Render
# (Environment), nunca escrito aquí en el código.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# Mismas constantes y fórmula que usa el HTML (monitor_brecha.html) — si cambias una,
# cambia la otra para que no se desincronicen.
AMOUNT_USD = 500
FIAT_FEE = 0.041
SERVICE_FEE = 0.005

CARD_FEES = {
    "bnc": 0.015,
    "bancamiga": 0.03,
    "banesco": 0.015,
    "bbva": 0.0,
    "bdv_debito": 0.015,
    "bdv_prepago": 0.025,
    "tesoro": 0.025,
}


def calc_tasa_final_real(bcv, card_fee, monto=AMOUNT_USD):
    usd_enviable = math.floor(monto / (1 + card_fee))
    usdt_recibido = usd_enviable * (1 - FIAT_FEE)
    total_pagado_bs = monto * bcv * (1 + SERVICE_FEE)
    return total_pagado_bs / usdt_recibido


def calc_gap(usdt, tasa_final_real):
    return ((usdt / tasa_final_real) - 1) * 100


def send_telegram_message(chat_id, text, thread_id=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if thread_id:
        payload["message_thread_id"] = thread_id

    resp = requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=10)
    # Log visible en Render -> pestaña Logs, para ver el motivo exacto si falla.
    print(f"[telegram] chat_id={chat_id} thread={thread_id!r} status={resp.status_code} body={resp.text}")


def build_reply(command):
    command = command.lower()

    bcv = cache["bcv_price"]
    usdt = cache["usdt_price"]

    if not bcv or not usdt:
        return "⚠️ Todavía no hay datos cargados, intenta en un minuto."

    if command == "/usdt":
        return (
            f"💵 <b>Tasas actuales</b>\n"
            f"BCV: {bcv:.2f} Bs/USD\n"
            f"USDT P2P: {usdt:.2f} Bs/USDT"
        )

    if command == "/brecha":
        gap = ((usdt / bcv) - 1) * 100
        return f"📊 <b>Brecha cambiaria</b>: {gap:.2f}%"

    if command == "/bdv":
        tasa_deb = calc_tasa_final_real(bcv, CARD_FEES["bdv_debito"])
        tasa_pre = calc_tasa_final_real(bcv, CARD_FEES["bdv_prepago"])
        return (
            f"🔴 <b>BDV</b>\n"
            f"Débito (1.5%): {calc_gap(usdt, tasa_deb):.2f}%  ({tasa_deb:.2f} Bs/USDT)\n"
            f"Prepago (2.5%): {calc_gap(usdt, tasa_pre):.2f}%  ({tasa_pre:.2f} Bs/USDT)"
        )

    labels = {
        "/bnc": ("BNC", "bnc"),
        "/bancamiga": ("Bancamiga", "bancamiga"),
        "/banesco": ("Banesco", "banesco"),
        "/bbva": ("BBVA", "bbva"),
        "/tesoro": ("Banco del Tesoro", "tesoro"),
    }
    if command in labels:
        name, key = labels[command]
        tasa = calc_tasa_final_real(bcv, CARD_FEES[key])
        gap = calc_gap(usdt, tasa)
        return f"🏦 <b>{name}</b>: {gap:.2f}%  ({tasa:.2f} Bs/USDT)"

    if command == "/todas":
        gap_general = ((usdt / bcv) - 1) * 100
        lines = [f"📊 <b>Brecha cambiaria general</b>: {gap_general:.2f}%", ""]
        for key, name in [("bnc", "BNC"), ("banesco", "Banesco"), ("bbva", "BBVA"), ("bancamiga", "Bancamiga"), ("tesoro", "Banco del Tesoro")]:
            tasa = calc_tasa_final_real(bcv, CARD_FEES[key])
            lines.append(f"{name}: {calc_gap(usdt, tasa):.2f}%")
        tasa_deb = calc_tasa_final_real(bcv, CARD_FEES["bdv_debito"])
        tasa_pre = calc_tasa_final_real(bcv, CARD_FEES["bdv_prepago"])
        lines.append(f"BDV Débito: {calc_gap(usdt, tasa_deb):.2f}%")
        lines.append(f"BDV Prepago: {calc_gap(usdt, tasa_pre):.2f}%")
        return "\n".join(lines)

    if command == "/ayuda":
        return (
            "📋 <b>Comandos disponibles</b>\n"
            "/usdt /brecha /bnc /bdv /bancamiga /banesco /bbva /tesoro /todas /ayuda"
        )

    return None  # comando no reconocido: no se responde nada


# Mapeo opcional "canal -> tema fijo". Si un canal aparece aquí, el bot SIEMPRE
# responde en ese tema específico, sin importar desde qué tema del canal le escriban.
# Si un canal NO aparece aquí, se usa el comportamiento por defecto (responde en el
# mismo tema donde llegó el comando). Formato en Render (una sola línea, separado por comas):
#   -1003906849602:2,-1009876543210:5
TELEGRAM_CHANNEL_TOPICS_RAW = os.environ.get("TELEGRAM_CHANNEL_TOPICS", "")


def parse_channel_topics(raw):
    mapping = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        chat_id, topic_id = pair.split(":", 1)
        chat_id, topic_id = chat_id.strip(), topic_id.strip()
        if chat_id and topic_id.lstrip("-").isdigit():
            mapping[chat_id] = int(topic_id)
    return mapping


CHANNEL_TOPICS = parse_channel_topics(TELEGRAM_CHANNEL_TOPICS_RAW)

# Destino de las notificaciones AUTOMÁTICAS (cada 1 min), separado del enrutamiento
# de comandos (CHANNEL_TOPICS) para poder cambiar uno sin afectar el otro.
# Mismo formato: "chat_id:tema_id,chat_id:tema_id". Si no se configura, usa
# CHANNEL_TOPICS como respaldo (para no perder las notificaciones por defecto).
TELEGRAM_NOTIFY_TOPICS_RAW = os.environ.get("TELEGRAM_NOTIFY_TOPICS", "")
NOTIFY_TOPICS = parse_channel_topics(TELEGRAM_NOTIFY_TOPICS_RAW) or CHANNEL_TOPICS


# ------------------------------------------------------------
# Notificación automática cada vez que cambia el precio de USDT
# ------------------------------------------------------------
last_notified_usdt_price = None


def format_arrow(delta_pct):
    if delta_pct > 0:
        return "🟢⬆️"
    if delta_pct < 0:
        return "🔴⬇️"
    return "⚪➖"


def notify_price_update(bcv, usdt):
    """Envía la actualización a TODOS los canales/temas configurados en
    NOTIFY_TOPICS. No requiere que nadie escriba ningún comando."""
    global last_notified_usdt_price

    if not NOTIFY_TOPICS:
        return  # nada configurado, no hay a dónde avisar

    if last_notified_usdt_price:
        delta_pct = ((usdt / last_notified_usdt_price) - 1) * 100
        variacion_txt = f"{format_arrow(delta_pct)} {delta_pct:+.2f}%"
    else:
        variacion_txt = "⚪ primera lectura"

    gap = ((usdt / bcv) - 1) * 100

    message = (
        f"💵 USDT: {usdt:.2f} Bs. ({variacion_txt})\n"
        f"📐 Brecha: {gap:.2f}%"
    )

    for chat_id, topic_id in NOTIFY_TOPICS.items():
        send_telegram_message(chat_id, message, thread_id=topic_id)

    last_notified_usdt_price = usdt


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    update = await request.json()
    message = update.get("message") or {}
    text = (message.get("text") or "").strip().split("@")[0]  # quita @NombreDelBot si viene
    from_id = str(message.get("from", {}).get("id", ""))
    origin_chat_id = message.get("chat", {}).get("id")
    origin_thread_id = message.get("message_thread_id")  # presente solo si el chat tiene Temas

    # Si este canal tiene un tema fijo configurado, se usa ese en vez del de origen.
    target_thread_id = CHANNEL_TOPICS.get(str(origin_chat_id), origin_thread_id)

    print(f"[telegram] recibido: text={text!r} from_id={from_id} chat_id={origin_chat_id} "
          f"thread_origen={origin_thread_id} thread_destino={target_thread_id}")

    if not text.startswith("/"):
        return {"ok": True}

    reply = build_reply(text)
    if reply:
        send_telegram_message(origin_chat_id, reply, thread_id=target_thread_id)
    return {"ok": True}
