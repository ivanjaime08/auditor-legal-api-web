# -*- coding: utf-8 -*-
"""
=====================================================================
  API DE ANÁLISIS LEGAL WEB (gancho para la landing)  ·  Iván Jaime
=====================================================================
Recibe una URL, la analiza con un navegador real (Playwright) y
devuelve en JSON: HTTPS, cookies antes del consentimiento,
rastreadores, textos legales y transferencias internacionales,
con el rango de sanción de cada punto.

Ejecutar en local:   uvicorn main:app --reload
En Render se arranca solo con el Procfile / Start Command.
"""

import re
from urllib.parse import urlparse

from fastapi import FastAPI
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


def es_no_necesaria(nombre):
    return any(re.match(p, nombre, re.IGNORECASE) for p in PATRONES_NO_NECESARIAS)


class Peticion(BaseModel):
    url: str


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
        punto("transferencias", "Transferencias internacionales", len(transferencias) == 0,
              "No se detectan servicios fuera del EEE.",
              f"Usa servicios fuera del EEE ({', '.join(sorted(transferencias))}); debe declararse en la política."),
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
