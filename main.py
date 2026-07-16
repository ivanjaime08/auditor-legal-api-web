# -*- coding: utf-8 -*-
"""
=====================================================================
  API DE ANÁLISIS LEGAL WEB (gancho para la landing)  ·  Iván Jaime
=====================================================================
Recibe una URL, la analiza con un navegador real (Playwright) y
devuelve en JSON: HTTPS, cookies antes del consentimiento,
rastreadores, textos legales y transferencias internacionales,
con el rango de sanción de cada punto.

NOVEDADES respecto a la versión anterior:
  · Transferencias internacionales: ahora LEE la política de privacidad
    y solo marca incumplimiento si usas servicios fuera del EEE Y NO los
    declaras. Antes marcaba "mal" siempre que detectaba Google, Meta, etc.
  · Endpoint /lead: captura el email del visitante, te lo envía a tu
    correo (persistencia fiable en Render Free) y manda el informe al lead.

Ejecutar en local:   uvicorn main:app --reload
En Render se arranca solo con el Procfile / Start Command.
"""

import os
import re
import csv
import ssl
import smtplib
import unicodedata
from datetime import datetime
from urllib.parse import urlparse
from email.message import EmailMessage

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from playwright.async_api import async_playwright

app = FastAPI(title="Auditor Legal Web - Iván Jaime")

# --- CORS: permite que tu web (ivanjaime.com) llame a esta API ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # en producción puedes limitarlo a https://ivanjaime.com
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------------------------------------------------------
# Clasificación de cookies (idéntica a tu cookies_check.py)
# ------------------------------------------------------------------
PATRONES_NO_NECESARIAS = [
    r"^(_ga($|_)|_gid$|_gat|__utm)", r"^_hj", r"^(_clck$|_clsk$)", r"^_pk_",
    r"^(_fbp$|_fbc$|fr$)", r"^(IDE|DSID|test_cookie)$", r"^_gcl_",
    r"^(NID|1P_JAR|ANID)$", r"^(_uetsid$|_uetvid$)", r"^(personalization_id|guest_id)",
    r"^(_ttp$|tt_)", r"^_pin_", r"^(VISITOR_INFO1_LIVE|YSC|yt-)", r"^(MUID|_uet)$",
]
TRACKERS = [
    "google-analytics.com", "googletagmanager.com", "doubleclick.net",
    "connect.facebook.net", "facebook.com/tr", "hotjar.com", "clarity.ms",
    "tiktok.com", "bat.bing.com", "pinterest.com",
]
PROVEEDORES_FUERA_EEE = {
    "google-analytics.com": "Google Analytics", "googletagmanager.com": "Google Tag Manager",
    "gstatic.com/recaptcha": "Google reCAPTCHA", "fonts.googleapis.com": "Google Fonts (remotas)",
    "fonts.gstatic.com": "Google Fonts (remotas)", "doubleclick.net": "Google Ads",
    "youtube.com": "YouTube (Google)", "connect.facebook.net": "Meta / Facebook Pixel",
    "facebook.com": "Meta / Facebook", "mailchimp.com": "Mailchimp", "hotjar.com": "Hotjar",
    "clarity.ms": "Microsoft Clarity", "tiktok.com": "TikTok", "vimeo.com": "Vimeo",
    "stripe.com": "Stripe", "paypal.com": "PayPal",
}

# Rango de sanción (mínimo, máximo) por punto -> rangos realistas para pyme,
# los mismos criterios que el informe de pago. Creíbles, no cifras teóricas.
SANCIONES = {
    "https":          (2000, 40000),
    "cookies":        (3000, 30000),
    "aviso_legal":    (600, 30000),
    "privacidad":     (2000, 50000),
    "cookies_pol":    (600, 15000),
    "transferencias": (2000, 40000),
}

# ------------------------------------------------------------------
# NUEVO · Detección de transferencias internacionales en la política
# ------------------------------------------------------------------
def _normalizar(texto):
    """Minúsculas + sin acentos + espacios colapsados, para comparar sin
    que fallen los acentos o las mayúsculas."""
    texto = (texto or "").lower()
    texto = unicodedata.normalize("NFD", texto)
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", texto)

# Señales (ya normalizadas) de que la política SÍ informa de transferencias.
SENALES_TRANSFERENCIAS = [
    "data privacy framework",
    "marco de privacidad de datos",
    "clausulas contractuales tipo",
    "standard contractual clauses",
    "transferencia internacional",
    "transferencias internacionales",
    "fuera del espacio economico europeo",
    "fuera del eee",
    "articulos 44",
    "articulo 44",
]
_CLAVES_HREF = ["privacidad", "privacy", "proteccion-datos", "proteccion_de_datos",
                "data-protection", "datos-personales"]
_CLAVES_TEXTO = ["privacidad", "proteccion de datos", "privacy", "datos personales"]


def politica_declara_transferencias(texto):
    """Devuelve True si el texto de la política menciona transferencias
    internacionales con garantías válidas."""
    t = _normalizar(texto)
    return any(s in t for s in SENALES_TRANSFERENCIAS)


async def encontrar_url_politica(pagina):
    """Busca en los enlaces de la página el que apunta a la política de
    privacidad. Devuelve la URL o None."""
    try:
        enlaces = await pagina.eval_on_selector_all(
            "a[href]",
            "els => els.map(a => ({href: a.href, text: a.textContent.trim()}))"
        )
    except Exception:
        return None
    for a in enlaces:
        href_n = _normalizar(a["href"])
        text_n = _normalizar(a["text"])
        if any(k in href_n for k in _CLAVES_HREF) or any(k in text_n for k in _CLAVES_TEXTO):
            return a["href"]
    return None


# ------------------------------------------------------------------
# NUEVO · Captura de leads (email + envío)
# ------------------------------------------------------------------
CSV_LEADS = "leads.csv"   # OJO: disco EFÍMERO en Render Free. El email a ti
                          # mismo (abajo) es la copia que nunca se pierde.
_RE_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def email_valido(e):
    return bool(_RE_EMAIL.match(e or ""))


def guardar_lead_csv(url, email, problemas, resumen, ip):
    nuevo = not os.path.exists(CSV_LEADS)
    try:
        with open(CSV_LEADS, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if nuevo:
                w.writerow(["fecha", "email", "url", "problemas", "resumen", "ip"])
            w.writerow([datetime.now().isoformat(timespec="seconds"),
                        email, url, problemas, resumen, ip])
    except Exception as e:
        print("No se pudo escribir el CSV:", e)


def enviar_email(destinatario, asunto, cuerpo):
    """Envía por SMTP de OVH. Si no hay credenciales en el entorno, no falla."""
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ.get("SMTP_USER")
    passwd = os.environ.get("SMTP_PASS")
    if not (host and user and passwd):
        print("SMTP no configurado; email omitido.")
        return False
    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = destinatario
    msg["Subject"] = asunto
    msg.set_content(cuerpo)
    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=20) as s:
            s.login(user, passwd)
            s.send_message(msg)
        return True
    except Exception as e:
        print("Error enviando email:", e)
        return False


def avisar_lead_a_ivan(url, email, problemas, resumen):
    destino = os.environ.get("LEAD_DEST", "ivan@ivanjaime.com")
    cuerpo = (f"Nuevo lead del analizador legal:\n\n"
              f"Email:      {email}\n"
              f"Web:        {url}\n"
              f"Problemas:  {problemas}\n"
              f"Resumen:    {resumen}\n"
              f"Fecha:      {datetime.now():%d/%m/%Y %H:%M}\n\n"
              f"Escríbele en 48-72 h con el detalle y la oferta de auditoría.")
    enviar_email(destino, f"[LEAD] {email} - {problemas} problemas", cuerpo)


def enviar_informe_al_lead(email, url, problemas, resumen):
    cuerpo = (
        f"Hola,\n\n"
        f"Gracias por comprobar {url} con el analizador legal.\n\n"
        f"Resultado orientativo: {problemas} posible(s) incumplimiento(s).\n"
        f"Puntos senalados: {resumen}\n\n"
        f"Esto es solo una comprobacion automatica y superficial. La auditoria "
        f"completa la hago yo a mano y revisa 99 puntos (RGPD, LSSI, cookies, "
        f"accesibilidad y la normativa de tu sector), con el detalle de cada "
        f"fallo, su norma, su sancion y como corregirlo. Cuesta 115 EUR + IVA y, "
        f"si luego me encargas la correccion, se descuenta integra.\n\n"
        f"Quieres que le eche un vistazo serio a tu web? Responde a este correo "
        f"o escribeme por WhatsApp al 646 33 18 99.\n\n"
        f"Un saludo,\n"
        f"Ivan Jaime - ivanjaime.com\n"
        f"Autor del manual 'Normativa legal en paginas web y tiendas online 2026'\n"
    )
    enviar_email(email, "Tu comprobacion legal orientativa - Ivan Jaime", cuerpo)


# ------------------------------------------------------------------
def es_no_necesaria(nombre):
    return any(re.match(p, nombre, re.IGNORECASE) for p in PATRONES_NO_NECESARIAS)


class Peticion(BaseModel):
    url: str


class Lead(BaseModel):
    url: str
    email: str
    consentimiento: bool = False
    problemas: int = 0
    resumen: str = ""


def normaliza(url):
    url = (url or "").strip()
    if not url.startswith("http"):
        url = "https://" + url
    return url


async def analizar_web(url):
    url = normaliza(url)
    dom = urlparse(url).netloc.lower().replace("www.", "")
    trackers = set()
    transferencias = set()

    async with async_playwright() as p:
        navegador = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        contexto = await navegador.new_context(locale="es-ES")
        pagina = await contexto.new_page()

        def on_request(req):
            for t in TRACKERS:
                if t in req.url:
                    trackers.add(t)
            for clave, prov in PROVEEDORES_FUERA_EEE.items():
                if clave in req.url:
                    transferencias.add(prov)
        pagina.on("request", on_request)

        estado_https = url.startswith("https://")
        try:
            resp = await pagina.goto(url, wait_until="networkidle", timeout=35000)
            if resp:
                estado_https = resp.url.startswith("https://")
        except Exception:
            pass
        await pagina.wait_for_timeout(2000)

        # Cookies antes de aceptar
        cookies = await contexto.cookies()
        cookies_no_nec = [c["name"] for c in cookies if es_no_necesaria(c["name"])]

        # Texto de la página para buscar enlaces legales
        try:
            html = (await pagina.content()).lower()
        except Exception:
            html = ""

        # --- NUEVO: localizar y leer la política de privacidad ---
        # (necesario para saber si las transferencias están DECLARADAS)
        url_politica = await encontrar_url_politica(pagina)
        texto_politica = ""
        if url_politica:
            try:
                p2 = await contexto.new_page()   # pestaña aparte: no ensucia trackers
                await p2.goto(url_politica, wait_until="domcontentloaded", timeout=30000)
                texto_politica = await p2.inner_text("body")
                await p2.close()
            except Exception:
                texto_politica = ""

        await navegador.close()

    def tiene(textos):
        return any(t in html for t in textos)

    aviso = tiene(["aviso legal", "aviso-legal", "avisolegal"])
    privacidad = tiene(["política de privacidad", "politica de privacidad", "privacy", "privacidad"])
    cookies_pol = tiene(["política de cookies", "politica de cookies", "cookie policy", "/cookies"])

    # ---- Construir resultados ----
    def punto(id_, titulo, ok, detalle_ok, detalle_mal, revisar=False):
        if revisar:
            estado = "revisar"
        else:
            estado = "ok" if ok else "mal"
        smin, smax = SANCIONES.get(id_, (0, 0))
        return {
            "id": id_, "titulo": titulo, "estado": estado,
            "detalle": detalle_ok if ok else detalle_mal,
            "sancion_min": smin, "sancion_max": smax,
        }

    # --- NUEVO: transferencias con lógica de dos pasos ---
    # Paso 1: ¿usa servicios fuera del EEE?  Paso 2: ¿los declara la política?
    declara = politica_declara_transferencias(texto_politica)
    smin_t, smax_t = SANCIONES["transferencias"]
    lista_serv = ", ".join(sorted(transferencias))

    if len(transferencias) == 0:
        res_transf = {
            "id": "transferencias", "titulo": "Transferencias internacionales",
            "estado": "ok", "sancion_min": 0, "sancion_max": 0,
            "detalle": "No se detectan servicios que envíen datos fuera del EEE.",
        }
    elif declara:
        res_transf = {
            "id": "transferencias", "titulo": "Transferencias internacionales",
            "estado": "ok", "sancion_min": 0, "sancion_max": 0,
            "detalle": (f"Usa servicios fuera del EEE ({lista_serv}) y la política de "
                        f"privacidad los declara con garantías válidas "
                        f"(Data Privacy Framework o Cláusulas Contractuales Tipo)."),
        }
    else:
        motivo = "no hay política de privacidad accesible" if not url_politica \
                 else "la política de privacidad no lo declara"
        res_transf = {
            "id": "transferencias", "titulo": "Transferencias internacionales",
            "estado": "mal", "sancion_min": smin_t, "sancion_max": smax_t,
            "detalle": (f"Usa servicios fuera del EEE ({lista_serv}) pero {motivo} "
                        f"ni menciona las garantías (art. 44 RGPD)."),
        }

    resultados = [
        punto("https", "Conexión segura (HTTPS)", estado_https,
              "La web carga de forma segura con HTTPS.",
              "La web NO usa HTTPS. Los datos viajan sin cifrar."),
        punto("cookies", "Cookies antes del consentimiento", len(cookies_no_nec) == 0,
              "No se detectan cookies no necesarias antes de aceptar.",
              f"Instala {len(cookies_no_nec)} cookie(s) no necesarias antes de aceptar (incumple LSSI art. 22)."),
        punto("aviso_legal", "Aviso legal", aviso,
              "Se detecta un enlace de Aviso legal.",
              "No se detecta el Aviso legal (revisar).", revisar=not aviso),
        punto("privacidad", "Política de privacidad", privacidad,
              "Se detecta la Política de privacidad.",
              "No se detecta la Política de privacidad (revisar).", revisar=not privacidad),
        punto("cookies_pol", "Política de cookies", cookies_pol,
              "Se detecta la Política de cookies.",
              "No se detecta la Política de cookies (revisar).", revisar=not cookies_pol),
        res_transf,
    ]

    problemas = sum(1 for r in resultados if r["estado"] == "mal")
    total_min = sum(r["sancion_min"] for r in resultados if r["estado"] == "mal")
    total_max = sum(r["sancion_max"] for r in resultados if r["estado"] == "mal")

    return {
        "url": url,
        "problemas": problemas,
        "total_min": total_min,
        "total_max": total_max,
        "resultados": resultados,
        "trackers": sorted(trackers),
    }


@app.get("/")
def raiz():
    return {"ok": True, "servicio": "Auditor Legal Web - Iván Jaime"}


@app.post("/analizar")
async def analizar(pet: Peticion):
    try:
        return await analizar_web(pet.url)
    except Exception as e:
        return {"error": True, "mensaje": f"No se pudo analizar la web: {e}"}


@app.post("/lead")
async def registrar_lead(lead: Lead, request: Request):
    # 1) Sin consentimiento NO se guarda nada (tú vendes cumplimiento).
    if not lead.consentimiento:
        return {"ok": False, "mensaje": "Falta el consentimiento."}
    if not email_valido(lead.email):
        return {"ok": False, "mensaje": "Email no válido."}

    ip = request.client.host if request.client else ""

    # 2) Persistencia fiable: aviso a ti + CSV best-effort.
    avisar_lead_a_ivan(lead.url, lead.email, lead.problemas, lead.resumen)
    guardar_lead_csv(lead.url, lead.email, lead.problemas, lead.resumen, ip)

    # 3) Informe automático al interesado.
    enviar_informe_al_lead(lead.email, lead.url, lead.problemas, lead.resumen)

    return {"ok": True}
