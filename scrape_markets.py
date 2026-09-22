"""
scraper_energia_2td.py
Extrae precios de tarifa doméstica 2.0TD de las principales comercializadoras españolas.
Guarda un registro diario en Google Sheets (pestaña "historico").
"""

import json
import re
import asyncio
import datetime
import os
import requests
import gspread
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials
from playwright.async_api import async_playwright

SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "")
CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
ZENROWS_API_KEY = os.environ.get("ZENROWS_API_KEY", "")
SHEET_NAME = "historico"

# ─────────────────────────────────────────────
# ZENROWS — bypass anti-bot para webs bloqueadas por IP de datacenter
# (Iberdrola, Energya VM). El contenido de estas páginas ya viene en el
# HTML servido por el servidor (no es "JS dinámico" como se creía antes),
# el problema real es que la IP de los runners de GitHub Actions está
# bloqueada por el sistema anti-bot (tipo Akamai/PerimeterX/DataDome).
# "mode=auto" deja que ZenRows decida qué necesita (proxy/JS) para gastar
# el mínimo de créditos posible.
# ─────────────────────────────────────────────

def fetch_via_zenrows(url: str, marker: str = "", js_render: bool = False,
                      wait_for: str = "", wait_ms: int = 0, timeout: int = 60,
                      proxy_country: str = "", referer: str = "",
                      js_instructions: list = None) -> str | None:
    """Descarga el HTML de una URL a través de ZenRows (proxy premium + bypass anti-bot).
    Si se pasa `marker`, solo se considera válido el resultado si ese texto
    aparece en el HTML (para descartar páginas de verificación/challenge que
    también pueden ser largas pero no contienen el precio real).
    `js_render=True` hace que ZenRows abra un navegador real y ejecute el JS
    de la página (necesario en sitios que solo rellenan el precio tras
    establecer cookies de sesión/región en el cliente). `wait_for` es un
    selector CSS al que ZenRows espera antes de devolver el HTML, y `wait_ms`
    un margen fijo adicional en milisegundos (ambos solo con js_render=True).
    `proxy_country` fuerza una IP residencial de ese país (p.ej. "es").
    `referer` añade una cabecera Referer (algunos sitios la exigen para no
    bloquear la petición, ver error RESP001 de ZenRows).
    `js_instructions` (solo con js_render=True) es una lista de acciones
    tipo [{"click": "selector CSS"}, {"wait": 1000}] que ZenRows ejecuta en
    el navegador antes de devolver el HTML (p.ej. para seleccionar una
    pestaña o desplegar un bloque de precios oculto).
    Devuelve el HTML como string, o None si falla o no hay API key configurada."""
    if not ZENROWS_API_KEY:
        print("    [ZenRows] ZENROWS_API_KEY no configurada")
        return None
    try:
        params = {
            "url": url,
            "apikey": ZENROWS_API_KEY,
            "premium_proxy": "true",
            "antibot": "true",
        }
        if proxy_country:
            params["proxy_country"] = proxy_country
        headers = None
        if referer:
            params["custom_headers"] = "true"
            headers = {"Referer": referer}
        if js_render:
            params["js_render"] = "true"
            if wait_for:
                params["wait_for"] = wait_for
            if wait_ms:
                params["wait"] = str(wait_ms)
            if js_instructions:
                params["js_instructions"] = json.dumps(js_instructions)
            timeout = max(timeout, 140)  # el render JS puede tardar más
        resp = requests.get("https://api.zenrows.com/v1/", params=params, headers=headers, timeout=timeout)
        print(f"    [ZenRows] {url} (js_render={js_render}) -> status={resp.status_code} len={len(resp.text)}")
        if resp.status_code != 200:
            print(f"    [ZenRows] body[:300]={resp.text[:300]!r}")
            return None
        low = resp.text.lower()
        print(f"    [ZenRows] pistas: onetrust={'onetrust' in low} cookie={'cookie' in low} captcha={'captcha' in low}")
        if marker and marker not in resp.text:
            print(f"    [ZenRows] marcador '{marker}' NO encontrado en el HTML — posible challenge/bloqueo")
            print(f"    [ZenRows] body[:300]={resp.text[:300]!r}")
            return None
        return resp.text
    except Exception as e:
        print(f"    [ZenRows] Excepción: {str(e)[:200]}")
        return None

# ─────────────────────────────────────────────
# TARIFAS — una entrada por tarifa específica
# ─────────────────────────────────────────────
TARIFAS = [
    # ENDESA
    {"comercializadora": "Endesa", "tarifa": "Conecta de Endesa",
     "url": "https://www.endesa.com/es/luz-y-gas/luz/conecta-de-endesa",
     "tipo_esperado": "precio_unico_24h", "extractor": "endesa"},
    {"comercializadora": "Endesa", "tarifa": "Luz 24h Online",
     "url": "https://www.endesa.com/es/luz-y-gas/luz/one/tarifa-one-luz",
     "tipo_esperado": "precio_unico_24h", "extractor": "endesa"},
    {"comercializadora": "Endesa", "tarifa": "Conecta 3 Periodos",
     "url": "https://www.endesa.com/es/luz-y-gas/luz/one/tarifa-one-luz-3periodos",
     "tipo_esperado": "punta_llano_valle", "extractor": "endesa"},
    # IBERDROLA
    {"comercializadora": "Iberdrola", "tarifa": "Plan Online",
     "url": "https://www.iberdrola.es/luz/tarifas/plan-online",
     "tipo_esperado": "precio_unico_24h", "extractor": "iberdrola"},
    {"comercializadora": "Iberdrola", "tarifa": "Plan Online 3 Periodos",
     "url": "https://www.iberdrola.es/luz/tarifas/plan-online-tres-periodos",
     "tipo_esperado": "punta_llano_valle", "extractor": "iberdrola"},
    # HOLALUZ
    {"comercializadora": "Holaluz", "tarifa": "Tarifa Clasica",
     "url": "https://www.holaluz.com/luz/tarifas-luz",
     "tipo_esperado": "precio_unico_24h", "extractor": "holaluz"},
    {"comercializadora": "Holaluz", "tarifa": "Tarifa 3",
     "url": "https://www.holaluz.com/luz/tarifas-luz",
     "tipo_esperado": "punta_llano_valle", "extractor": "holaluz"},
    # NATURGY
    {"comercializadora": "Naturgy", "tarifa": "Por Uso Luz",
     "url": "https://www.naturgy.es/hogar/luz",
     "tipo_esperado": "precio_unico_24h", "extractor": "naturgy"},
    {"comercializadora": "Naturgy", "tarifa": "Noche Luz",
     "url": "https://www.naturgy.es/hogar/luz",
     "tipo_esperado": "punta_llano_valle", "extractor": "naturgy"},
    # REPSOL
    {"comercializadora": "Repsol", "tarifa": "Sin Horarios",
     "url": "https://www.repsol.es/particulares/hogar/luz-y-gas/tarifas/tarifa-sin-horarios/",
     "tipo_esperado": "precio_unico_24h", "extractor": "repsol"},
    {"comercializadora": "Repsol", "tarifa": "Discriminacion Horaria",
     "url": "https://www.repsol.es/particulares/hogar/luz-y-gas/tarifas/tarifa-discriminacion-horaria/",
     "tipo_esperado": "punta_llano_valle", "extractor": "repsol_dh"},
    # PLENITUDE
    {"comercializadora": "Plenitude", "tarifa": "Facil",
     "url": "https://eniplenitude.es/hogar/tarifas-luz/facil/",
     "tipo_esperado": "precio_unico_24h", "extractor": "generico"},
    # IMAGINA ENERGIA
    {"comercializadora": "Imagina Energia", "tarifa": "Sin Horas",
     "url": "https://imaginaenergia.com/tarifa-luz-sin-horas/",
     "tipo_esperado": "precio_unico_24h", "extractor": "imagina"},
    # GANA ENERGIA
    {"comercializadora": "Gana Energia", "tarifa": "24 Horas",
     "url": "https://ganaenergia.com/tarifas-luz/24-horas",
     "tipo_esperado": "precio_unico_24h", "extractor": "gana"},
    # FENIE ENERGIA
    {"comercializadora": "Fenie Energia", "tarifa": "Fijo Energetico 3P",
     "url": "https://www.fenieenergia.es/es/hogar/tarifas-de-luz/fijo-energetico-3p",
     "tipo_esperado": "punta_llano_valle", "extractor": "fenie"},
    {"comercializadora": "Fenie Energia", "tarifa": "Fijo Energetico 1P",
     "url": "https://www.fenieenergia.es/es/hogar/tarifas-de-luz/fijo-energetico-1p",
     "tipo_esperado": "precio_unico_24h", "extractor": "fenie"},
    # FACTOR ENERGIA
    {"comercializadora": "Factor Energia", "tarifa": "Precio Unico 24h",
     "url": "https://www.factorenergia.com/es/luz/tarifa-fija-de-luz-precio-unico/",
     "tipo_esperado": "precio_unico_24h", "extractor": "factor"},
    {"comercializadora": "Factor Energia", "tarifa": "Respira 3 Periodos",
     "url": "https://www.factorenergia.com/es/luz/tarifa-fija/",
     "tipo_esperado": "punta_llano_valle", "extractor": "factor"},
    # VISALIA ENERGIA
    {"comercializadora": "Visalia Energia", "tarifa": "Fijo 24 Horas",
     "url": "https://visalia.es/luz/fijo24horas/",
     "tipo_esperado": "precio_unico_24h", "extractor": "generico"},
    # ENERGYA VM
    {"comercializadora": "Energya VM", "tarifa": "Formula Fija 24H",
     "url": "https://www.energyavm.es/luz/formula-fija-24-horas-luz/",
     "tipo_esperado": "precio_unico_24h", "extractor": "energyavm"},
    {"comercializadora": "Energya VM", "tarifa": "Formula Fija 3 Periodos",
     "url": "https://www.energyavm.es/luz/formula-fija-3-periodos-luz/",
     "tipo_esperado": "punta_llano_valle", "extractor": "energyavm"},
    # TOTALENERGIES
    {"comercializadora": "TotalEnergies", "tarifa": "A tu Aire Siempre",
     "url": "https://www.totalenergies.es/contrata/?model=tarifasluz_all_m&cta=cta_main_atuaire_m",
     "tipo_esperado": "precio_unico_24h", "extractor": "totalenergies"},
    {"comercializadora": "TotalEnergies", "tarifa": "A tu Aire Programa tu Ahorro",
     "url": "https://www.totalenergies.es/contrata/programa-ahorro?model=tarifasluz_tuahorro_d&cta=cta_main_programaahorro_m",
     "tipo_esperado": "punta_llano_valle", "extractor": "totalenergies"},
    # OCTOPUS ENERGY
    {"comercializadora": "Octopus Energy", "tarifa": "Octopus Relax",
     "url": "https://octopusenergy.es/precios",
     "tipo_esperado": "precio_unico_24h", "extractor": "octopus"},
    {"comercializadora": "Octopus Energy", "tarifa": "Octopus 3",
     "url": "https://octopusenergy.es/precios",
     "tipo_esperado": "punta_llano_valle", "extractor": "octopus"},
    # NIBA
    {"comercializadora": "Niba", "tarifa": "Niba Zen",
     "url": "https://niba.es/luz-y-gas",
     "tipo_esperado": "precio_unico_24h", "extractor": "niba"},
    {"comercializadora": "Niba", "tarifa": "Niba Tres",
     "url": "https://niba.es/luz-y-gas",
     "tipo_esperado": "punta_llano_valle", "extractor": "niba"},
    # PODO
    {"comercializadora": "Podo", "tarifa": "Tarifa Luz Precio Unico 24h",
     "url": "https://www.mipodo.com/tarifas-luz/fija",
     "tipo_esperado": "precio_unico_24h", "extractor": "podo"},
]

# ─────────────────────────────────────────────
# UTILIDADES
# ─────────────────────────────────────────────

def es_precio_kwh(valor: float) -> bool:
    """€/kWh doméstico: entre 0.05 y 0.80"""
    return 0.05 < valor < 0.80

def es_precio_kw_dia(valor: float) -> bool:
    """€/kW·día potencia: entre 0.01 y 0.30"""
    return 0.01 < valor < 0.30

def limpiar(texto: str) -> float | None:
    try:
        return float(texto.replace(",", "."))
    except Exception:
        return None

def fmt(valor: float) -> str:
    return str(round(valor, 6))

def registro_vacio(comercializadora, tarifa, url, motivo):
    return {
        "fecha": datetime.date.today().isoformat(),
        "comercializadora": comercializadora,
        "tarifa": tarifa,
        "tipo_precio": "N/D",
        "potencia_p1_eur_kw_dia": None,
        "potencia_p2_eur_kw_dia": None,
        "energia_punta_eur_kwh": None,
        "energia_llano_eur_kwh": None,
        "energia_valle_eur_kwh": None,
        "energia_unico_eur_kwh": None,
        "url": url,
        "notas": motivo,
    }

async def cerrar_cookies(page):
    for sel in [
        "#onetrust-accept-btn-handler",
        "button:has-text('Aceptar todo')",
        "button:has-text('Aceptar')",
        "button:has-text('Acepto')",
        ".cc-btn.cc-allow",
        "button[class*='accept']",
    ]:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=2000):
                await btn.click()
                await page.wait_for_timeout(1500)
                break
        except Exception:
            pass


# ─────────────────────────────────────────────
# EXTRACTOR ENDESA
# Precios con dto (vigentes) + tachados. Potencia en €/kW·mes → /30 para dia
# ─────────────────────────────────────────────

async def extractor_endesa(page, t):
    url = t["url"]
    tipo = t["tipo_esperado"]
    try:
        await page.goto(url, timeout=60000, wait_until="domcontentloaded")
        await cerrar_cookies(page)
        # Endesa carga precios via JS — esperar al elemento de precio
        try:
            await page.wait_for_selector("text=€/kWh", timeout=12000)
        except Exception:
            pass
        await page.wait_for_timeout(3000)
        texto = await page.evaluate("() => document.body.innerText")

        # Endesa muestra: "0,128235€/kWh\n0,160294€/kWh" (vigente + tachado)
        # o para 3P: "0,094410€/kWh\n0,104900€/kWh" x3
        kwh_raw = re.findall(r"(\d+[.,]\d{3,6})\s*€/kWh", texto, re.IGNORECASE)
        # Potencia en €/kW (mensual ~2.849) → convertir a diario /30
        kw_raw = re.findall(r"(\d+[.,]\d{3,6})\s*€/kW(?!\s*h)(?!\s*·)(?!\s*año)(?!\s*dia)", texto, re.IGNORECASE)

        kwh_ok = [v for v in kwh_raw if es_precio_kwh(limpiar(v))]
        # Potencia mensual: valores entre 0.5 y 10 €/kW·mes
        kw_mensual = [v for v in kw_raw if 0.3 < (limpiar(v) or 0) < 15.0]
        kw_dia = [fmt(round(limpiar(v) / 30, 6)) for v in kw_mensual if limpiar(v)]

        if not kwh_ok:
            return registro_vacio(t["comercializadora"], t["tarifa"], url,
                                  "Sin precios €/kWh detectados")

        reg = registro_vacio(t["comercializadora"], t["tarifa"], url, "OK")

        if tipo == "punta_llano_valle":
            # Endesa muestra pares (vigente, tachado) para cada periodo
            # Orden: valle vigente, valle tachado, llano vigente, llano tachado, punta vigente, punta tachado
            # Cogemos posiciones 0, 2, 4 (los vigentes, menores)
            vigentes = kwh_ok[0::2] if len(kwh_ok) >= 6 else kwh_ok[:3]
            if len(vigentes) >= 3:
                reg["tipo_precio"] = "punta_llano_valle"
                # Endesa muestra valle primero, luego llano, luego punta
                reg["energia_valle_eur_kwh"]  = fmt(limpiar(vigentes[0]))
                reg["energia_llano_eur_kwh"]  = fmt(limpiar(vigentes[1]))
                reg["energia_punta_eur_kwh"]  = fmt(limpiar(vigentes[2]))
            else:
                reg["tipo_precio"] = "punta_llano_valle"
                reg["energia_valle_eur_kwh"]  = fmt(limpiar(kwh_ok[-3])) if len(kwh_ok) >= 3 else None
                reg["energia_llano_eur_kwh"]  = fmt(limpiar(kwh_ok[-2])) if len(kwh_ok) >= 2 else None
                reg["energia_punta_eur_kwh"]  = fmt(limpiar(kwh_ok[-1]))
        else:
            reg["tipo_precio"] = "precio_unico_24h"
            # Tomar el primer valor (vigente con dto, el menor)
            reg["energia_unico_eur_kwh"] = fmt(limpiar(kwh_ok[0]))

        if kw_dia:
            reg["potencia_p1_eur_kw_dia"] = kw_dia[0]
            if len(kw_dia) > 1:
                reg["potencia_p2_eur_kw_dia"] = kw_dia[1]
            reg["notas"] = "OK - potencia convertida de mensual a diaria"
        elif t["tarifa"] == "Conecta 3 Periodos":
            # Esta página no publica el precio de potencia (el dato solo
            # aparece en el PDF de condiciones de la tarifa, más abajo en
            # la web). Valor fijo confirmado manualmente: 26,88 €/kW·año,
            # igual para P1 y P2 → 26,88 / 365 = 0,073644 €/kW·día.
            reg["potencia_p1_eur_kw_dia"] = fmt(26.88 / 365)
            reg["potencia_p2_eur_kw_dia"] = fmt(26.88 / 365)
            reg["notas"] = "OK - potencia fija de 26,88 €/kW/año (dato del PDF de condiciones, no de la web)"
        return reg
    except Exception as e:
        return registro_vacio(t["comercializadora"], t["tarifa"], url, f"Error: {str(e)[:120]}")

# ─────────────────────────────────────────────

# ─────────────────────────────────────────────

# ─────────────────────────────────────────────

# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
# EXTRACTOR IMAGINA ENERGIA
# Muestra precio tachado + precio con 20% dto
# "Antes: 0,149 €/kWh  Ahora 20%: 0,119 €/kWh"
# Potencia: "Valle: 0,048 €/kW  Punta: 0,105 €/kW"
# ─────────────────────────────────────────────

async def extractor_imagina(page, t):
    url = t["url"]
    try:
        await page.goto(url, timeout=60000, wait_until="domcontentloaded")
        await cerrar_cookies(page)
        await page.wait_for_timeout(4000)
        texto = await page.evaluate("() => document.body.innerText")

        # Normalizar separadores
        texto = re.sub(r"€\s*/\s*kWh", "€/kWh", texto, flags=re.IGNORECASE)
        texto = re.sub(r"€\s*/\s*kW\b", "€/kW", texto, flags=re.IGNORECASE)

        kwh_raw = re.findall(r"(\d+[.,]\d{3,6})\s*€/kWh", texto, re.IGNORECASE)
        # Imagina muestra "0,048 €/kW" sin "día" — son valores diarios
        kw_raw  = re.findall(r"(\d+[.,]\d{3,6})\s*€/kW(?!h)", texto, re.IGNORECASE)

        kwh_ok = [v for v in kwh_raw if es_precio_kwh(limpiar(v))]
        kw_ok  = [v for v in kw_raw  if es_precio_kw_dia(limpiar(v))]

        if not kwh_ok:
            return registro_vacio(t["comercializadora"], t["tarifa"], url, "Sin precios detectados")

        reg = registro_vacio(t["comercializadora"], t["tarifa"], url, "OK")
        reg["tipo_precio"] = "precio_unico_24h"
        # Tomar el precio más bajo (vigente con 20% dto)
        reg["energia_unico_eur_kwh"] = fmt(min([limpiar(v) for v in kwh_ok]))
        if kw_ok:
            # Convenio de esta hoja: P1 = Punta, P2 = Valle. La web muestra
            # Valle primero y Punta segundo, así que se invierte el orden.
            reg["potencia_p1_eur_kw_dia"] = fmt(limpiar(kw_ok[1])) if len(kw_ok) > 1 else fmt(limpiar(kw_ok[0]))
            reg["potencia_p2_eur_kw_dia"] = fmt(limpiar(kw_ok[0]))
        return reg
    except Exception as e:
        return registro_vacio(t["comercializadora"], t["tarifa"], url, f"Error: {str(e)[:120]}")

# EXTRACTOR REPSOL
# Página con 2 variantes (con/sin asistente) — mismo precio o diferente
# Formato: "0,119900 €/kWh" y "0,090137 €/kW·día"
# Nos interesa el precio más bajo (tarifa con asistente, seleccionada por defecto)
# ─────────────────────────────────────────────

async def extractor_repsol(page, t):
    url = t["url"]
    try:
        await page.goto(url, timeout=60000, wait_until="domcontentloaded")
        await cerrar_cookies(page)
        await page.wait_for_timeout(4000)
        texto = await page.evaluate("() => document.body.innerText")

        kwh_raw = re.findall(r"(\d+[.,]\d{3,6})\s*€/kWh", texto, re.IGNORECASE)
        kw_raw  = re.findall(r"(\d+[.,]\d{3,6})\s*€/kW\s*(?:·?\s*día|·?\s*dia|/día|/dia)", texto, re.IGNORECASE)

        kwh_ok = [v for v in kwh_raw if es_precio_kwh(limpiar(v))]
        kw_ok  = [v for v in kw_raw  if es_precio_kw_dia(limpiar(v))]

        if not kwh_ok:
            return registro_vacio(t["comercializadora"], t["tarifa"], url, "Sin precios detectados")

        reg = registro_vacio(t["comercializadora"], t["tarifa"], url, "OK")
        reg["tipo_precio"] = "precio_unico_24h"
        # Tomar el precio más bajo (tarifa con asistente = la seleccionada)
        reg["energia_unico_eur_kwh"] = fmt(min([limpiar(v) for v in kwh_ok]))
        if kw_ok:
            reg["potencia_p1_eur_kw_dia"] = fmt(limpiar(kw_ok[0]))
            if len(kw_ok) > 1:
                reg["potencia_p2_eur_kw_dia"] = fmt(limpiar(kw_ok[1]))
        return reg
    except Exception as e:
        return registro_vacio(t["comercializadora"], t["tarifa"], url, f"Error: {str(e)[:120]}")

# ─────────────────────────────────────────────
# EXTRACTOR REPSOL — DISCRIMINACIÓN HORARIA (3 periodos)
# Misma web/plantilla que "Sin Horarios". La página repite el bloque de
# precios íntegro dos veces (HTML duplicado, no son 2 variantes distintas
# con/sin Asistente como en "Sin Horarios"). Nos quedamos con los valores
# ÚNICOS, preservando el orden de aparición: los 3 primeros son energía
# (punta > llano > valle, de mayor a menor) y los 2 siguientes son
# potencia (P1 > P2).
# ─────────────────────────────────────────────

async def extractor_repsol_dh(page, t):
    url = t["url"]
    try:
        await page.goto(url, timeout=60000, wait_until="domcontentloaded")
        await cerrar_cookies(page)
        await page.wait_for_timeout(4000)
        texto = await page.evaluate("() => document.body.innerText")

        kwh_raw = re.findall(r"(\d+[.,]\d{3,6})\s*€/kWh", texto, re.IGNORECASE)
        kw_raw  = re.findall(r"(\d+[.,]\d{3,6})\s*€/kW\s*(?:·?\s*día|·?\s*dia|/día|/dia)", texto, re.IGNORECASE)

        kwh_ok = [v for v in kwh_raw if es_precio_kwh(limpiar(v))]
        kw_ok  = [v for v in kw_raw  if es_precio_kw_dia(limpiar(v))]

        if not kwh_ok:
            return registro_vacio(t["comercializadora"], t["tarifa"], url,
                                  "Sin precios €/kWh detectados")

        reg = registro_vacio(t["comercializadora"], t["tarifa"], url, "OK")
        reg["tipo_precio"] = "punta_llano_valle"

        def unicos_en_orden(valores):
            vistos = []
            for v in valores:
                if v not in vistos:
                    vistos.append(v)
            return vistos

        valores = unicos_en_orden([limpiar(v) for v in kwh_ok])
        kw_valores = unicos_en_orden([limpiar(v) for v in kw_ok])

        if len(valores) < 3:
            return registro_vacio(t["comercializadora"], t["tarifa"], url,
                                  f"Solo {len(valores)} precios €/kWh únicos detectados, se esperaban 3")

        # Orden descendente = punta, llano, valle (convención habitual: punta
        # es el periodo más caro, valle el más barato)
        punta, llano, valle = sorted(valores, reverse=True)[:3]
        reg["energia_punta_eur_kwh"] = fmt(punta)
        reg["energia_llano_eur_kwh"] = fmt(llano)
        reg["energia_valle_eur_kwh"] = fmt(valle)

        if kw_valores:
            # P1 (punta) suele ser el precio de potencia más alto
            kw_ordenados = sorted(kw_valores, reverse=True)
            reg["potencia_p1_eur_kw_dia"] = fmt(kw_ordenados[0])
            if len(kw_ordenados) > 1:
                reg["potencia_p2_eur_kw_dia"] = fmt(kw_ordenados[1])
        else:
            reg["notas"] = "OK - sin precio de potencia detectado, revisar"
        return reg
    except Exception as e:
        return registro_vacio(t["comercializadora"], t["tarifa"], url, f"Error: {str(e)[:120]}")

# EXTRACTOR NATURGY
# Página con 3 tarifas: Por Uso (24h), Plana (cuota fija), Noche (3P)
# Formato: "0,112000 €/kWh"
# ─────────────────────────────────────────────

async def extractor_naturgy(page, t):
    url = t["url"]
    tipo = t["tipo_esperado"]
    try:
        await page.goto(url, timeout=60000, wait_until="domcontentloaded")
        await cerrar_cookies(page)
        await page.wait_for_timeout(4000)
        texto = await page.evaluate("() => document.body.innerText")

        kwh_raw = re.findall(r"(\d+[.,]\d{3,6})\s*€/kWh", texto, re.IGNORECASE)
        kw_raw  = re.findall(r"(\d+[.,]\d{3,6})\s*€/kW\s*(?:día|dia)?(?!h)", texto, re.IGNORECASE)

        kwh_ok = [v for v in kwh_raw if es_precio_kwh(limpiar(v))]
        kw_ok  = [v for v in kw_raw  if es_precio_kw_dia(limpiar(v))]

        # Naturgy página muestra en orden:
        # [Por Uso 24h: 0.112000, Noche Valle: 0.073900, Noche Llano: 0.109200, Noche Punta: 0.182200]
        if not kwh_ok:
            return registro_vacio(t["comercializadora"], t["tarifa"], url, "Sin precios detectados")

        reg = registro_vacio(t["comercializadora"], t["tarifa"], url, "OK")

        if tipo == "punta_llano_valle":
            # Noche Luz: últimos 3 valores (valle, llano, punta)
            periodos = kwh_ok[-3:] if len(kwh_ok) >= 3 else kwh_ok
            if len(periodos) >= 3:
                reg["tipo_precio"] = "punta_llano_valle"
                reg["energia_valle_eur_kwh"] = fmt(limpiar(periodos[0]))
                reg["energia_llano_eur_kwh"] = fmt(limpiar(periodos[1]))
                reg["energia_punta_eur_kwh"] = fmt(limpiar(periodos[2]))
            else:
                return registro_vacio(t["comercializadora"], t["tarifa"], url,
                                      "No se encontraron 3 precios para Noche Luz")
        else:
            # Por Uso Luz: primer valor de la página
            reg["tipo_precio"] = "precio_unico_24h"
            reg["energia_unico_eur_kwh"] = fmt(limpiar(kwh_ok[0]))

        if kw_ok:
            reg["potencia_p1_eur_kw_dia"] = fmt(limpiar(kw_ok[0]))
            if len(kw_ok) > 1:
                reg["potencia_p2_eur_kw_dia"] = fmt(limpiar(kw_ok[1]))
        return reg
    except Exception as e:
        return registro_vacio(t["comercializadora"], t["tarifa"], url, f"Error: {str(e)[:120]}")

# EXTRACTOR HOLALUZ
# Página con 3 tarifas: Justa (plana), Clásica (24h), Tarifa 3 (P/L/V)
# Clásica muestra precio normal + precio online (el vigente, en recuadro)
# ─────────────────────────────────────────────

async def extractor_holaluz(page, t):
    url = t["url"]
    tipo = t["tipo_esperado"]
    try:
        await page.goto(url, timeout=60000, wait_until="domcontentloaded")
        await cerrar_cookies(page)

        # Espera activa hasta que haya varios precios cargados (hasta ~8s),
        # en vez de un tiempo fijo — la página necesita cargar 5 valores en
        # total (2 de Clásica + 3 de Tarifa 3) y un tiempo fijo corto puede
        # no ser suficiente algunas veces (visto en producción 21 sep 2026).
        for _ in range(16):
            n_precios = await page.evaluate(
                r"""() => (document.body.innerText.match(/\d[.,]\d{2,6}\s*€\s*\/?\s*kWh/gi) || []).length"""
            )
            if n_precios >= 5:
                break
            await page.wait_for_timeout(500)
        texto = await page.evaluate("() => document.body.innerText")

        # Normalizar "€/kWh" y "€/kW dia"
        texto = re.sub(r"€\s*/\s*kWh", "€/kWh", texto, flags=re.IGNORECASE)
        texto = re.sub(r"€\s*/\s*kW\b", "€/kW", texto, flags=re.IGNORECASE)

        kwh_raw = re.findall(r"(\d+[.,]\d{3,6})\s*€/kWh", texto, re.IGNORECASE)
        kw_raw  = re.findall(r"(\d+[.,]\d{3,6})\s*€/kW\s*(?:día|dia|d[ií]a)?", texto, re.IGNORECASE)

        kwh_ok = [v for v in kwh_raw if es_precio_kwh(limpiar(v))]
        kw_ok  = [v for v in kw_raw  if es_precio_kw_dia(limpiar(v))]

        # Holaluz página muestra en orden:
        # [Clásica normal, Clásica online, Punta, Llano, Valle]
        # kwh_ok = [0.147, 0.115, 0.206, 0.137, 0.113]

        if not kwh_ok:
            return registro_vacio(t["comercializadora"], t["tarifa"], url, "Sin precios detectados")

        reg = registro_vacio(t["comercializadora"], t["tarifa"], url, "OK")

        if tipo == "punta_llano_valle":
            # Tarifa 3: últimos 3 valores (punta, llano, valle)
            periodos = kwh_ok[-3:] if len(kwh_ok) >= 3 else kwh_ok
            if len(periodos) >= 3:
                reg["tipo_precio"] = "punta_llano_valle"
                reg["energia_punta_eur_kwh"] = fmt(limpiar(periodos[0]))
                reg["energia_llano_eur_kwh"]  = fmt(limpiar(periodos[1]))
                reg["energia_valle_eur_kwh"]  = fmt(limpiar(periodos[2]))
            else:
                return registro_vacio(t["comercializadora"], t["tarifa"], url,
                                      "No se encontraron 3 precios para Tarifa 3")
        else:
            # Tarifa Clásica: buscar precio online (el menor entre los primeros 2)
            clasica = kwh_ok[:2] if len(kwh_ok) >= 2 else kwh_ok
            precio_online = min(clasica, key=lambda v: limpiar(v))
            reg["tipo_precio"] = "precio_unico_24h"
            reg["energia_unico_eur_kwh"] = fmt(limpiar(precio_online))

        if kw_ok:
            reg["potencia_p1_eur_kw_dia"] = fmt(limpiar(kw_ok[0]))
            if len(kw_ok) > 1:
                reg["potencia_p2_eur_kw_dia"] = fmt(limpiar(kw_ok[1]))
        return reg
    except Exception as e:
        return registro_vacio(t["comercializadora"], t["tarifa"], url, f"Error: {str(e)[:120]}")

# EXTRACTOR IBERDROLA (espera carga dinámica)
# ─────────────────────────────────────────────

def _extraer_precios_div_iberdrola(html: str):
    """Aísla el div id='precios-dinamicos' con BeautifulSoup y extrae los
    precios SOLO de su texto (no de toda la página), para evitar falsos
    positivos con números sueltos de otras zonas de la web."""
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return [], []
    div = soup.find(id="precios-dinamicos")
    if not div:
        return [], []
    texto = div.get_text(separator=" ", strip=True)
    texto = re.sub(r"€\s*/\s*kWh", "€/kWh", texto, flags=re.IGNORECASE)
    texto = re.sub(r"€\s*/\s*kW\b", "€/kW", texto, flags=re.IGNORECASE)
    kwh_raw = re.findall(r"(\d+[.,]\d{3,6})\s*€/kWh", texto, re.IGNORECASE)
    kw_raw = re.findall(r"(\d+[.,]\d{3,6})\s*€/kW(?!h)", texto, re.IGNORECASE)
    kwh_vals = [limpiar(v) for v in kwh_raw if es_precio_kwh(limpiar(v) or 0)]
    kw_vals = [limpiar(v) for v in kw_raw if es_precio_kw_dia(limpiar(v) or 0)]
    return kwh_vals, kw_vals


async def extractor_iberdrola(page, t):
    url = t["url"]
    tipo = t["tipo_esperado"]
    try:
        # 1) ZenRows con js_render (la web necesita ejecutar JS/establecer
        #    cookies de sesión antes de rellenar el precio en el HTML).
        html_content = fetch_via_zenrows(
            url, marker="precios-dinamicos",
            js_render=True, wait_for="#precios-dinamicos", wait_ms=4000,
        )
        origen = "ZenRows"
        kwh_texts, kw_texts = [], []
        if html_content:
            kwh_texts, kw_texts = _extraer_precios_div_iberdrola(html_content)

        # 2) Fallback: HTML pre-descargado por curl en el workflow, si existe
        if not kwh_texts:
            import os as _os
            cache_file = ("iberdrola_cache/plan-online-3p.html"
                          if ("tres-periodos" in url or "3p" in url.lower())
                          else "iberdrola_cache/plan-online.html")
            if _os.path.exists(cache_file):
                with open(cache_file, "r", encoding="utf-8", errors="ignore") as f:
                    cache_html = f.read()
                if len(cache_html) > 10000:
                    kwh_texts, kw_texts = _extraer_precios_div_iberdrola(cache_html)
                    if not kwh_texts:
                        # El div puede no tener id en el HTML de curl; probar
                        # también el barrido genérico de números como red de
                        # seguridad final antes de recurrir a Playwright.
                        todos = re.findall(r"(\d+[.,]\d{4,6})", cache_html)
                        seen = set()
                        for m in todos:
                            val = float(m.replace(",", "."))
                            if val not in seen:
                                seen.add(val)
                                if 0.05 < val < 0.80:
                                    kwh_texts.append(val)
                                elif 0.01 < val < 0.30:
                                    kw_texts.append(val)
                    if kwh_texts:
                        origen = "curl cache"
                        print(f"    [Iberdrola] ZenRows sin resultado, usando HTML cacheado ({len(cache_html)} chars)")

        # 3) Último recurso: Playwright directo (rara vez funciona por el anti-bot)
        if not kwh_texts:
            origen = "Playwright"
            print(f"    [Iberdrola] ZenRows y cache sin resultado, usando Playwright")
            await page.goto(url, timeout=60000, wait_until="domcontentloaded")
            await cerrar_cookies(page)
            try:
                await page.wait_for_selector("#precios-dinamicos", timeout=20000)
            except Exception:
                pass
            await page.wait_for_timeout(2000)
            resultado = await page.evaluate("""() => {
                const div = document.getElementById('precios-dinamicos');
                if (!div) return {kwh: [], kw: []};
                const kwh = [];
                const kw = [];
                const matches = div.innerText.match(/\\d+[,.]\\d{4,6}/g) || [];
                matches.forEach(m => {
                    const val = parseFloat(m.replace(',', '.'));
                    if (val > 0.05 && val < 0.80) kwh.push(val);
                    else if (val > 0.01 && val < 0.30) kw.push(val);
                });
                return {kwh: [...new Set(kwh)], kw: [...new Set(kw)]};
            }""")
            kwh_texts = resultado.get("kwh", [])
            kw_texts = resultado.get("kw", [])

        if not kwh_texts:
            return registro_vacio(t["comercializadora"], t["tarifa"], url,
                                  "Iberdrola: sin precios (ZenRows/cache/Playwright fallaron)")

        reg = registro_vacio(t["comercializadora"], t["tarifa"], url, f"OK ({origen})")
        reg["tipo_precio"] = tipo

        if tipo == "punta_llano_valle" and len(kwh_texts) >= 3:
            # Orden real en la web de Iberdrola: Valle, Llano, Punta
            reg["energia_valle_eur_kwh"] = fmt(kwh_texts[0])
            reg["energia_llano_eur_kwh"] = fmt(kwh_texts[1])
            reg["energia_punta_eur_kwh"] = fmt(kwh_texts[2])
        elif tipo == "punta_llano_valle" and len(kwh_texts) == 2:
            reg["energia_valle_eur_kwh"] = fmt(kwh_texts[0])
            reg["energia_punta_eur_kwh"] = fmt(kwh_texts[1])
            reg["notas"] = f"OK ({origen}) - solo punta/valle detectados"
        else:
            reg["energia_unico_eur_kwh"] = fmt(kwh_texts[0])

        if kw_texts:
            # Iberdrola siempre muestra la potencia en orden Valle, Punta,
            # pero el convenio de esta hoja es P1 = Punta (mayor), P2 = Valle
            # (menor) — se invierte el orden respecto a como aparece en la web.
            if len(kw_texts) > 1:
                reg["potencia_p1_eur_kw_dia"] = fmt(kw_texts[-1])
                reg["potencia_p2_eur_kw_dia"] = fmt(kw_texts[0])
            else:
                reg["potencia_p1_eur_kw_dia"] = fmt(kw_texts[0])
        return reg
    except Exception as e:
        return registro_vacio(t["comercializadora"], t["tarifa"], url, f"Error: {str(e)[:120]}")

# ─────────────────────────────────────────────
# EXTRACTOR GANA ENERGIA
# ─────────────────────────────────────────────

def _parsear_gana_bs4(html: str):
    """Misma lógica que la versión Playwright (mirar el texto de la unidad
    adyacente, no solo el rango numérico) pero sobre HTML crudo con
    BeautifulSoup, para poder usarla con el HTML que devuelve ZenRows.
    Se empieza a mirar a partir del <p> con el encabezado 'Energía' (la
    sección real de precios), porque antes hay una frase de marketing
    ("Compensamos tus excedentes a 0,06 €/kWh") que también contiene un
    valor en €/kWh pero no es el precio de consumo."""
    soup = BeautifulSoup(html, "html.parser")
    ps = soup.find_all("p")
    inicio = 0
    for i, p in enumerate(ps):
        if p.get_text(strip=True).lower() == "energía":
            inicio = i
            break
    kwh_vals, kw_vals = [], []
    for i in range(inicio, len(ps)):
        txt = ps[i].get_text(strip=True)
        next_txt = ps[i + 1].get_text(strip=True) if i + 1 < len(ps) else ""
        num = re.sub(r"[^0-9.]", "", txt.replace(",", "."))
        try:
            val = float(num)
        except Exception:
            continue
        if not val:
            continue
        es_kwh = "kwh" in next_txt.lower() or "kwh" in txt.lower()
        es_kw = not es_kwh and ("kw" in next_txt.lower() or "kw" in txt.lower())
        if es_kwh:
            kwh_vals.append(val)
        elif es_kw:
            kw_vals.append(val)
    return kwh_vals, kw_vals

async def extractor_gana(page, t):
    url = t["url"]
    try:
        # 1) ZenRows con js_render — la web devuelve 403 sin sesión/cookies
        #    establecidas (mismo patrón que Iberdrola). Se añade
        #    proxy_country="es" y un Referer, siguiendo las recomendaciones
        #    oficiales de ZenRows para el error RESP001 (Could Not Get Content).
        origen = "ZenRows"
        kwh_raw, kw_raw = [], []
        html_content = fetch_via_zenrows(
            url, js_render=True, wait_ms=6000,
            proxy_country="es", referer="https://www.google.com",
        )
        if html_content:
            kwh_raw, kw_raw = _parsear_gana_bs4(html_content)

        # 2) Último recurso: Playwright directo
        if not kwh_raw:
            origen = "Playwright"
            print("    [Gana Energia] ZenRows sin resultado, usando Playwright")
            await page.goto(url, timeout=60000, wait_until="domcontentloaded")
            await cerrar_cookies(page)

            # Espera activa: reintentar hasta que el encabezado "Energía"
            # exista de verdad en el DOM (hasta ~10s), en vez de un tiempo
            # fijo — si la página tarda más de lo habitual en cargar y el
            # encabezado aún no existe, el código caería por defecto al
            # principio de la página y cogería el precio de "excedentes"
            # en vez del precio real (visto en producción el 20-21 sep 2026).
            for _ in range(20):
                encontrado = await page.evaluate("""() => {
                    const ps = [...document.querySelectorAll('p')];
                    return ps.some(p => p.innerText.trim().toLowerCase() === 'energía');
                }""")
                if encontrado:
                    break
                await page.wait_for_timeout(500)

            # Gana Energia: precio en un elemento (p.ej. <p class="text-lg...">)
            # y la unidad ("/kWh" o "kW / día") en un elemento hermano separado.
            # IMPORTANTE: no se puede distinguir energía de potencia solo por
            # el rango numérico (p.ej. 0,089 encaja tanto en el rango de
            # €/kWh como en el de €/kW·día) — hay que mirar el texto de la
            # unidad adyacente.
            resultado = await page.evaluate("""() => {
                const result = {kwh: [], kw: []};
                const allP = Array.from(document.querySelectorAll('p'));
                // Ignorar todo antes del encabezado "Energía": hay una frase
                // de marketing sobre excedentes con un valor en €/kWh que no
                // es el precio de consumo.
                let inicio = allP.findIndex(p => p.innerText.trim().toLowerCase() === 'energía');
                if (inicio === -1) inicio = 0;
                for (let i = inicio; i < allP.length; i++) {
                    const txt = allP[i].innerText.trim();
                    const next = allP[i+1] ? allP[i+1].innerText.trim() : '';
                    const num = txt.replace(',', '.').replace(/[^0-9.]/g, '');
                    const val = parseFloat(num);
                    if (!val) continue;
                    const esKwh = next.toLowerCase().includes('kwh') || txt.toLowerCase().includes('kwh');
                    const esKw = !esKwh && (next.toLowerCase().includes('kw') || txt.toLowerCase().includes('kw'));
                    if (esKwh) result.kwh.push(val);
                    else if (esKw) result.kw.push(val);
                }
                return result;
            }""")
            kwh_raw = resultado.get("kwh", [])
            kw_raw = resultado.get("kw", [])

        kwh_vals = [v for v in kwh_raw if es_precio_kwh(v)]
        kw_vals  = [v for v in kw_raw  if es_precio_kw_dia(v)]

        if not kwh_vals:
            return registro_vacio(t["comercializadora"], t["tarifa"], url,
                                  "Sin precios detectados (ZenRows/Playwright fallaron)")

        reg = registro_vacio(t["comercializadora"], t["tarifa"], url, f"OK ({origen})")
        reg["tipo_precio"] = "precio_unico_24h"
        reg["energia_unico_eur_kwh"] = fmt(kwh_vals[0])
        if kw_vals:
            reg["potencia_p1_eur_kw_dia"] = fmt(kw_vals[0])
            if len(kw_vals) > 1:
                reg["potencia_p2_eur_kw_dia"] = fmt(kw_vals[1])
        return reg
    except Exception as e:
        return registro_vacio(t["comercializadora"], t["tarifa"], url, f"Error: {str(e)[:120]}")

# ─────────────────────────────────────────────
# EXTRACTOR FENIE (filtra valores anuales €/kW·año)
# ─────────────────────────────────────────────

async def extractor_fenie(page, t):
    url = t["url"]
    tipo = t["tipo_esperado"]
    try:
        await page.goto(url, timeout=60000, wait_until="domcontentloaded")
        await cerrar_cookies(page)
        await page.wait_for_timeout(4000)
        texto = await page.evaluate("() => document.body.innerText")

        # Fenie muestra "0,195314 €/ kWh" (con espacio entre € y /) y "0,123346 €/kW día"
        # Normalizar espacio en €/ kWh -> €/kWh
        texto = re.sub(r"€\s*/\s*kWh", "€/kWh", texto, flags=re.IGNORECASE)
        texto = re.sub(r"€\s*/\s*kW", "€/kW", texto, flags=re.IGNORECASE)

        kwh_raw = re.findall(r"(\d+[.,]\d{3,6})\s*€/kWh", texto, re.IGNORECASE)
        # Potencia: buscar €/kW día explícitamente
        kw_raw  = re.findall(r"(\d+[.,]\d{3,6})\s*€/kW\s*(?:día|dia)", texto, re.IGNORECASE)
        if not kw_raw:
            kw_raw = re.findall(r"(\d+[.,]\d{3,6})\s*€/kW(?!h)", texto, re.IGNORECASE)

        kwh_ok = [v for v in kwh_raw if es_precio_kwh(limpiar(v))]
        kw_ok  = [v for v in kw_raw  if es_precio_kw_dia(limpiar(v))]

        if not kwh_ok:
            return registro_vacio(t["comercializadora"], t["tarifa"], url, "Sin precios €/kWh detectados")

        reg = registro_vacio(t["comercializadora"], t["tarifa"], url, "OK")
        reg["tipo_precio"] = tipo

        if tipo == "punta_llano_valle" and len(kwh_ok) >= 3:
            reg["energia_punta_eur_kwh"] = fmt(limpiar(kwh_ok[0]))
            reg["energia_llano_eur_kwh"] = fmt(limpiar(kwh_ok[1]))
            reg["energia_valle_eur_kwh"] = fmt(limpiar(kwh_ok[2]))
        else:
            reg["energia_unico_eur_kwh"] = fmt(limpiar(kwh_ok[0]))
            reg["tipo_precio"] = "precio_unico_24h"

        if kw_ok:
            reg["potencia_p1_eur_kw_dia"] = fmt(limpiar(kw_ok[0]))
            if len(kw_ok) > 1:
                reg["potencia_p2_eur_kw_dia"] = fmt(limpiar(kw_ok[1]))
        return reg
    except Exception as e:
        return registro_vacio(t["comercializadora"], t["tarifa"], url, f"Error: {str(e)[:120]}")


# ─────────────────────────────────────────────
# EXTRACTOR FACTOR ENERGIA
# Página con 3 tarifas: Respira (3P), Variable (indexada), Precio Único (24h)
# Formato: "0,1321 € / kWh" con espacios
# ─────────────────────────────────────────────

async def extractor_factor(page, t):
    url = t["url"]
    tipo = t["tipo_esperado"]
    try:
        await page.goto(url, timeout=60000, wait_until="domcontentloaded")
        await cerrar_cookies(page)
        await page.wait_for_timeout(4000)
        texto = await page.evaluate("() => document.body.innerText")

        # Estas páginas de Factor Energia muestran un precio "con descuento
        # e impuestos incluidos" como cifra destacada, pero el texto legal
        # da explícitamente el precio BASE "sin descuentos ni impuestos" y
        # el % de descuento permanente ("X% dto · Mismos precios todo el
        # año"). Calculamos base × (1 - descuento%) para obtener el precio
        # sin impuestos pero CON el descuento permanente aplicado, que es
        # el convenio usado en el resto de esta hoja. Se ignora el 10%
        # adicional de la primera anualidad mencionado aparte (es temporal).
        m_dto = re.search(r"(\d+[.,]?\d*)\s*%\s*dto", texto, re.IGNORECASE)
        m_energia = re.search(
            r"energ[ií]a sin descuentos ni impuestos:\s*Valle:?\s*(\d+[.,]\d+)\s*€/kWh;?\s*"
            r"Llano:?\s*(\d+[.,]\d+)\s*€/kWh;?\s*Punta:?\s*(\d+[.,]\d+)\s*€/kWh",
            texto, re.IGNORECASE)
        m_potencia = re.search(
            r"potencia sin descuentos ni impuestos:\s*Valle\s*(\d+[.,]\d+)\s*€/kW\s*d[ií]a\s*y\s*Punta\s*(\d+[.,]\d+)\s*€/kW",
            texto, re.IGNORECASE)

        if not (m_dto and m_energia):
            return registro_vacio(t["comercializadora"], t["tarifa"], url,
                                  "Factor Energia: no se encontró el texto legal con precios base")

        descuento = limpiar(m_dto.group(1)) / 100
        valle_e, llano_e, punta_e = (limpiar(m_energia.group(i)) for i in (1, 2, 3))
        valle_e, llano_e, punta_e = (v * (1 - descuento) for v in (valle_e, llano_e, punta_e))

        reg = registro_vacio(t["comercializadora"], t["tarifa"], url,
                             f"OK - precio base sin impuestos con {m_dto.group(1)}% dto aplicado")
        reg["tipo_precio"] = tipo

        if tipo == "punta_llano_valle":
            reg["energia_valle_eur_kwh"] = fmt(valle_e)
            reg["energia_llano_eur_kwh"] = fmt(llano_e)
            reg["energia_punta_eur_kwh"] = fmt(punta_e)
        else:
            # Precio único: los 3 periodos tienen el mismo valor en la web
            reg["energia_unico_eur_kwh"] = fmt(punta_e)

        if m_potencia:
            valle_p, punta_p = (limpiar(m_potencia.group(i)) for i in (1, 2))
            valle_p, punta_p = valle_p * (1 - descuento), punta_p * (1 - descuento)
            reg["potencia_p1_eur_kw_dia"] = fmt(punta_p)
            reg["potencia_p2_eur_kw_dia"] = fmt(valle_p)
        return reg
    except Exception as e:
        return registro_vacio(t["comercializadora"], t["tarifa"], url, f"Error: {str(e)[:120]}")

# ─────────────────────────────────────────────
# EXTRACTOR ENERGYA VM
# ─────────────────────────────────────────────

async def extractor_energyavm(page, t):
    url = t["url"]
    tipo = t["tipo_esperado"]
    try:
        origen = "ZenRows"
        kwh_vals, kw_anual = [], []

        # 1) ZenRows (bypass anti-bot) — el precio ya viene en el HTML servido
        #    por el servidor en el atributo "price" de span.price_prodcut_vm:
        #    <span class="price_prodcut_vm" tipo="termino_e" price="0.104325">
        html_content = fetch_via_zenrows(url, marker="price_prodcut_vm")
        if html_content:
            span_tags = re.findall(r"<span\s+class=\"price_prodcut_vm\"[^>]*>", html_content)
            seen = set()
            for tag in span_tags:
                tipo_m = re.search(r'tipo="([^"]+)"', tag)
                price_m = re.search(r'price="([^"]+)"', tag)
                if not tipo_m or not price_m:
                    continue
                try:
                    price = float(price_m.group(1))
                except Exception:
                    continue
                if not price or price in seen:
                    continue
                seen.add(price)
                tipo_attr = tipo_m.group(1)
                if "termino_e" in tipo_attr and 0.05 < price < 0.80:
                    kwh_vals.append(price)
                elif "termino_p" in tipo_attr and price > 0:
                    kw_anual.append(price)

        # 2) Último recurso: Playwright directo (rara vez funciona por el anti-bot)
        if not kwh_vals:
            origen = "Playwright"
            print("    [Energya VM] ZenRows sin resultado, usando Playwright")
            await page.goto(url, timeout=60000, wait_until="domcontentloaded")
            await cerrar_cookies(page)
            # Scroll para activar carga de precios y esperar spans
            await page.evaluate("() => window.scrollTo(0, 500)")
            await page.wait_for_timeout(3000)
            try:
                await page.wait_for_selector("span.price_prodcut_vm", timeout=15000)
            except Exception:
                pass
            await page.wait_for_timeout(2000)

            precios_e = await page.evaluate("""() => {
                const spans = document.querySelectorAll('span.price_prodcut_vm');
                const energia = [];
                const potencia = [];
                spans.forEach(s => {
                    const tipo = s.getAttribute('tipo') || '';
                    const price = parseFloat(s.getAttribute('price') || '0');
                    if (!price) return;
                    if (tipo.includes('termino_e') && price > 0.05 && price < 0.80) {
                        energia.push(price);
                    } else if (tipo.includes('termino_p') && price > 0) {
                        potencia.push(price);
                    }
                });
                return {energia, potencia};
            }""")
            kwh_vals = precios_e.get("energia", [])
            kw_anual = precios_e.get("potencia", [])

        # Convertir potencia anual -> diaria
        kw_dia = []
        seen = set()
        for val in kw_anual:
            if val not in seen:
                seen.add(val)
                if val > 0.3:  # es €/kW·año
                    kw_dia.append(round(val / 365, 6))
                elif es_precio_kw_dia(val):
                    kw_dia.append(val)

        if not kwh_vals:
            return registro_vacio(t["comercializadora"], t["tarifa"], url,
                                  "Sin precios en atributo price (ZenRows/Playwright fallaron)")

        reg = registro_vacio(t["comercializadora"], t["tarifa"], url, f"OK ({origen})")

        if tipo == "punta_llano_valle" and len(kwh_vals) >= 3:
            reg["tipo_precio"] = "punta_llano_valle"
            reg["energia_punta_eur_kwh"] = fmt(kwh_vals[0])
            reg["energia_llano_eur_kwh"]  = fmt(kwh_vals[1])
            reg["energia_valle_eur_kwh"]  = fmt(kwh_vals[2])
        else:
            reg["tipo_precio"] = "precio_unico_24h"
            reg["energia_unico_eur_kwh"] = fmt(kwh_vals[0])

        if kw_dia:
            reg["potencia_p1_eur_kw_dia"] = fmt(kw_dia[0])
            if len(kw_dia) > 1:
                reg["potencia_p2_eur_kw_dia"] = fmt(kw_dia[1])
            reg["notas"] = f"OK ({origen}) - potencia convertida de anual a diaria"
        return reg
    except Exception as e:
        return registro_vacio(t["comercializadora"], t["tarifa"], url, f"Error: {str(e)[:120]}")



# ─────────────────────────────────────────────
# EXTRACTOR TOTALENERGIES
# Ambas tarifas usan la página del embudo de contratación ("/contrata/...")
# en vez de la landing pública, porque ahí sí se muestra el precio de
# potencia. El precio de energía que muestran ya lleva aplicado el 2% de
# descuento del servicio "Facilita" que viene preseleccionado por defecto
# en ese flujo (p.ej. 0.0999 × 0.98 = 0.097902 en "A tu Aire Siempre"), a
# diferencia del resto de tarifas de este scraper que reflejan el precio
# base publicado sin bundles adicionales. La página de "Programa tu Ahorro"
# etiqueta explícitamente los 3 periodos en orden Punta, Llano, Valle
# (orden estándar, igual que la mayoría de las otras webs).
# ─────────────────────────────────────────────

def _texto_visible(html: str) -> str:
    """Convierte HTML crudo en texto plano (equivalente a innerText),
    para poder aplicar los mismos regex que usábamos sobre page.evaluate()."""
    try:
        soup = BeautifulSoup(html, "html.parser")
        return soup.get_text(separator=" ", strip=True)
    except Exception:
        return ""


def _parsear_precios_texto(texto: str):
    """Extrae listas de precios de energía (€/kWh) y potencia (€/kW) de un
    texto plano ya aplanado (sin etiquetas HTML)."""
    texto = re.sub(r"€\s*/\s*kWh", "€/kWh", texto, flags=re.IGNORECASE)
    texto = re.sub(r"€\s*/\s*kW\b", "€/kW", texto, flags=re.IGNORECASE)
    kwh_raw = re.findall(r"(\d+[.,]\d{2,6})\s*€/kWh", texto, re.IGNORECASE)
    kw_raw  = re.findall(r"(\d+[.,]\d{2,6})\s*€/kW(?!h)", texto, re.IGNORECASE)
    kwh_ok = [v for v in kwh_raw if es_precio_kwh(limpiar(v))]
    kw_ok  = [v for v in kw_raw  if es_precio_kw_dia(limpiar(v))]
    return kwh_ok, kw_ok


async def extractor_totalenergies(page, t):
    url = t["url"]
    tipo = t["tipo_esperado"]
    try:
        origen = "ZenRows"
        kwh_ok, kw_ok = [], []

        # 1) ZenRows con js_render — la página es una SPA que calcula el
        #    precio con una llamada interna tras cargar. Desde el 17 sep
        #    2026 empezó a bloquear también a ZenRows (antes bastaba con
        #    js_render+wait_ms); se añade proxy_country="es" y un Referer,
        #    la misma combinación que resolvió el bloqueo de Gana Energía.
        html_content = fetch_via_zenrows(
            url, js_render=True, wait_ms=9000,
            proxy_country="es", referer="https://www.google.com",
        )
        if html_content:
            kwh_ok, kw_ok = _parsear_precios_texto(_texto_visible(html_content))

        # 2) Último recurso: Playwright directo, con espera activa (polling)
        #    en vez de un sleep fijo: la SPA tarda un tiempo variable en
        #    calcular el precio, y un timeout fijo corto puede llegar antes
        #    de que aparezca (carrera contra el tiempo, vista en producción).
        if not kwh_ok:
            origen = "Playwright"
            print(f"    [TotalEnergies] ZenRows sin resultado, usando Playwright")
            await page.goto(url, timeout=60000, wait_until="domcontentloaded",
                           referer="https://www.google.com")
            await cerrar_cookies(page)
            texto = ""
            for _ in range(30):  # hasta ~15s en total (30 x 500ms)
                texto = await page.evaluate("() => document.body.innerText")
                if re.search(r"\d[.,]\d{2,6}\s*€\s*/\s*kWh", texto, re.IGNORECASE):
                    break
                await page.wait_for_timeout(500)
            kwh_ok, kw_ok = _parsear_precios_texto(texto)

        if not kwh_ok:
            return registro_vacio(t["comercializadora"], t["tarifa"], url,
                                  "TotalEnergies: sin precios (ZenRows/Playwright fallaron)")

        reg = registro_vacio(t["comercializadora"], t["tarifa"], url, f"OK ({origen})")

        if tipo == "punta_llano_valle" and len(kwh_ok) >= 3:
            # Orden en la página (etiquetado explícito): Punta, Llano, Valle
            reg["tipo_precio"] = "punta_llano_valle"
            reg["energia_punta_eur_kwh"] = fmt(limpiar(kwh_ok[0]))
            reg["energia_llano_eur_kwh"] = fmt(limpiar(kwh_ok[1]))
            reg["energia_valle_eur_kwh"] = fmt(limpiar(kwh_ok[2]))
        else:
            reg["tipo_precio"] = "precio_unico_24h"
            reg["energia_unico_eur_kwh"] = fmt(limpiar(kwh_ok[0]))

        if kw_ok:
            reg["potencia_p1_eur_kw_dia"] = fmt(limpiar(kw_ok[0]))
            if len(kw_ok) > 1:
                reg["potencia_p2_eur_kw_dia"] = fmt(limpiar(kw_ok[1]))
        return reg
    except Exception as e:
        return registro_vacio(t["comercializadora"], t["tarifa"], url, f"Error: {str(e)[:120]}")

# ─────────────────────────────────────────────
# EXTRACTOR OCTOPUS ENERGY
# Una sola página con 3 tarifas: Octopus Flexi (indexada a mercado, se
# ignora igual que la "Variable" de Factor Energia), Octopus Relax (24h)
# y Octopus 3 (3 periodos). Usamos índices negativos (desde el final) para
# no depender de cuántos valores muestre Flexi: Relax siempre aporta los
# últimos 4 valores de potencia menos los 2 de Octopus 3, y Octopus 3
# siempre son los 3 últimos valores de energía y los 2 últimos de potencia.
# El toggle "Con impuestos" de la web está desactivado por defecto, así
# que los valores que se leen ya son sin impuestos.
# ─────────────────────────────────────────────

async def extractor_octopus(page, t):
    url = t["url"]
    tipo = t["tipo_esperado"]
    try:
        await page.goto(url, timeout=60000, wait_until="domcontentloaded")
        await cerrar_cookies(page)

        # Espera activa hasta que aparezca un precio real (hasta ~8s), en vez
        # de un tiempo fijo — vistos fallos puntuales de timing en producción.
        for _ in range(16):
            listo = await page.evaluate(
                r"""() => /\d[.,]\d{2,3}\s*€\/kWh/.test(document.body.innerText)"""
            )
            if listo:
                break
            await page.wait_for_timeout(500)

        # Aislar el contenido de CADA tarjeta subiendo por el árbol del DOM
        # desde su título ("Octopus 3" / "Octopus Relax") hasta justo el
        # nivel en el que el texto deja de mezclarse con las otras tarjetas.
        # Esto es mucho más robusto que cortar el texto plano por posición:
        # no depende del orden en que Octopus muestre las tarjetas (lo ha
        # cambiado varias veces) ni de dónde corte cualquier límite fijo
        # (p.ej. la sección de FAQ), porque usa la estructura real del HTML
        # en vez de asumir un orden o una longitud de texto.
        titulo = "Octopus 3" if tipo == "punta_llano_valle" else "Octopus Relax"
        otros = [x for x in ["Octopus 3", "Octopus Relax", "Octopus Flexi"] if x != titulo]

        texto_tarjeta = await page.evaluate(
            """([titulo, otros]) => {
                function contenidoTarjeta(tituloObjetivo, otrosTitulos){
                    const candidatos = [...document.querySelectorAll('*')]
                        .filter(e => e.children.length === 0 && e.textContent.trim() === tituloObjetivo);
                    if (!candidatos.length) return '';
                    let cur = candidatos[0];
                    while (cur.parentElement) {
                        const textoPadre = cur.parentElement.textContent;
                        if (otrosTitulos.some(t => textoPadre.includes(t))) {
                            return cur.textContent;
                        }
                        cur = cur.parentElement;
                    }
                    return cur.textContent;
                }
                return contenidoTarjeta(titulo, otros);
            }""",
            [titulo, otros],
        )

        if not texto_tarjeta:
            return registro_vacio(t["comercializadora"], t["tarifa"], url,
                                  f"Octopus: no se encontró la tarjeta '{titulo}' en el DOM")

        seg = re.sub(r"€\s*/\s*kWh", "€/kWh", texto_tarjeta, flags=re.IGNORECASE)
        seg = re.sub(r"€\s*/\s*kW\s*/\s*d[ií]a", "€/kW/día", seg, flags=re.IGNORECASE)
        seg = re.sub(r"€\s*/\s*kW\s*/\s*mes", "€/kW/mes", seg, flags=re.IGNORECASE)
        kwh_raw = re.findall(r"(\d+[.,]\d{2,6})\s*€/kWh", seg, re.IGNORECASE)
        kw_dia_raw = re.findall(r"(\d+[.,]\d{2,6})\s*€/kW/d[ií]a", seg, re.IGNORECASE)
        kw_mes_raw = re.findall(r"(\d+[.,]\d{2,6})\s*€/kW/mes", seg, re.IGNORECASE)

        kwh_ok = [limpiar(v) for v in kwh_raw if es_precio_kwh(limpiar(v))]

        # Interpretación dinámica de la potencia — sin ningún valor fijo,
        # para que se adapte sola si Octopus vuelve a cambiar el precio o
        # el formato en que lo muestra:
        #  · Si aparecen 2 valores €/kW/día distintos → ya son el precio de
        #    cada periodo por separado (formato usado antiguamente por
        #    Octopus): se usan tal cual, uno para P1 y otro para P2.
        #  · Si aparece 1 solo valor (€/kW/día o €/kW/mes) bajo "Potencia
        #    fija" → es el TOTAL combinado de los dos periodos, no el
        #    precio de cada uno (confirmado 22 sep 2026): hay que repartirlo
        #    entre los 2 periodos (÷2) y, si es mensual, pasarlo a diario
        #    (÷30, la propia web indica "calculado para 30 días").
        #  · Si aparecen 2 valores €/kW/mes distintos → cada uno ya es el
        #    total mensual de su propio periodo: solo hace falta pasarlo a
        #    diario (÷30), sin repartir entre 2.
        valores_dia = [v for v in (limpiar(x) for x in kw_dia_raw) if v is not None and v > 0]
        valores_mes = [v for v in (limpiar(x) for x in kw_mes_raw) if v is not None and v > 0]

        p1_dia = p2_dia = None
        if len(valores_dia) >= 2:
            p1_dia, p2_dia = valores_dia[0], valores_dia[1]
        elif len(valores_dia) == 1:
            p1_dia = p2_dia = round(valores_dia[0] / 2, 6)
        elif len(valores_mes) >= 2:
            p1_dia = round(valores_mes[0] / 30, 6)
            p2_dia = round(valores_mes[1] / 30, 6)
        elif len(valores_mes) == 1:
            p1_dia = p2_dia = round(valores_mes[0] / 2 / 30, 6)

        reg = registro_vacio(t["comercializadora"], t["tarifa"], url, "OK")

        if tipo == "punta_llano_valle":
            if len(kwh_ok) < 3:
                return registro_vacio(t["comercializadora"], t["tarifa"], url,
                                      f"Octopus 3: precios de energía insuficientes (kwh={len(kwh_ok)})")
            reg["tipo_precio"] = "punta_llano_valle"
            reg["energia_punta_eur_kwh"] = fmt(kwh_ok[0])
            reg["energia_llano_eur_kwh"] = fmt(kwh_ok[1])
            reg["energia_valle_eur_kwh"] = fmt(kwh_ok[2])
        else:
            if not kwh_ok:
                return registro_vacio(t["comercializadora"], t["tarifa"], url,
                                      "Octopus Relax: precio de energía no encontrado")
            reg["tipo_precio"] = "precio_unico_24h"
            reg["energia_unico_eur_kwh"] = fmt(kwh_ok[0])

        if p1_dia is not None:
            reg["potencia_p1_eur_kw_dia"] = fmt(p1_dia)
            reg["potencia_p2_eur_kw_dia"] = fmt(p2_dia)
        else:
            reg["notas"] = "OK - sin precio de potencia detectado, revisar"
        return reg
    except Exception as e:
        return registro_vacio(t["comercializadora"], t["tarifa"], url, f"Error: {str(e)[:120]}")

# ─────────────────────────────────────────────
# EXTRACTOR NIBA
# Una sola página con 3 tarifas: niba Zen (24h), niba Tres (3P) y niba Flex
# (indexada a mercado, se ignora igual que Octopus Flexi / Factor Variable).
# Orden en la página: Zen, Tres, Flex. El toggle "Ver precios con
# impuestos" está desactivado por defecto, así que los valores ya son sin
# impuestos.
# ─────────────────────────────────────────────

async def extractor_niba(page, t):
    url = t["url"]
    tipo = t["tipo_esperado"]
    try:
        await page.goto(url, timeout=60000, wait_until="domcontentloaded")
        await cerrar_cookies(page)
        await page.wait_for_timeout(3000)
        texto = await page.evaluate("() => document.body.innerText")

        # Cortar el texto por los títulos de cada tarifa en vez de contar
        # posiciones sobre la lista global de precios: un valor de potencia
        # casi cero (p.ej. niba Flex) puede caer fuera del rango válido y
        # descuadrar cualquier conteo de posiciones entre tarifas.
        idx_tres = texto.find("niba Tres")
        idx_flex = texto.find("niba Flex")
        if idx_tres == -1 or idx_flex == -1 or idx_flex < idx_tres:
            return registro_vacio(t["comercializadora"], t["tarifa"], url,
                                  "Niba: no se encontraron los títulos de las tarifas en la página")
        texto_zen = texto[:idx_tres]
        texto_tres = texto[idx_tres:idx_flex]

        def parsear(segmento):
            seg = re.sub(r"€\s*/\s*kWh", "€/kWh", segmento, flags=re.IGNORECASE)
            seg = re.sub(r"€\s*/\s*kW\s*d[ií]a", "€/kW día", seg, flags=re.IGNORECASE)
            kwh_raw = re.findall(r"(\d+[.,]\d{2,6})\s*€/kWh", seg, re.IGNORECASE)
            kw_raw  = re.findall(r"(\d+[.,]\d{2,6})\s*€/kW\s*d[ií]a", seg, re.IGNORECASE)
            kwh_ok = [limpiar(v) for v in kwh_raw if es_precio_kwh(limpiar(v))]
            kw_ok  = [limpiar(v) for v in kw_raw  if es_precio_kw_dia(limpiar(v))]
            return kwh_ok, kw_ok

        reg = registro_vacio(t["comercializadora"], t["tarifa"], url, "OK")

        if tipo == "punta_llano_valle":
            kwh_ok, kw_ok = parsear(texto_tres)
            if len(kwh_ok) < 3 or len(kw_ok) < 2:
                return registro_vacio(t["comercializadora"], t["tarifa"], url,
                                      f"niba Tres: precios insuficientes (kwh={len(kwh_ok)}, kw={len(kw_ok)})")
            reg["tipo_precio"] = "punta_llano_valle"
            reg["energia_valle_eur_kwh"] = fmt(kwh_ok[0])
            reg["energia_llano_eur_kwh"] = fmt(kwh_ok[1])
            reg["energia_punta_eur_kwh"] = fmt(kwh_ok[2])
            reg["potencia_p1_eur_kw_dia"] = fmt(kw_ok[1])  # punta
            reg["potencia_p2_eur_kw_dia"] = fmt(kw_ok[0])  # valle
        else:
            kwh_ok, kw_ok = parsear(texto_zen)
            if not kwh_ok or len(kw_ok) < 2:
                return registro_vacio(t["comercializadora"], t["tarifa"], url,
                                      "niba Zen: precios no encontrados")
            reg["tipo_precio"] = "precio_unico_24h"
            reg["energia_unico_eur_kwh"] = fmt(kwh_ok[0])
            reg["potencia_p1_eur_kw_dia"] = fmt(kw_ok[1])  # punta
            reg["potencia_p2_eur_kw_dia"] = fmt(kw_ok[0])  # valle
        return reg
    except Exception as e:
        return registro_vacio(t["comercializadora"], t["tarifa"], url, f"Error: {str(e)[:120]}")

# ─────────────────────────────────────────────
# EXTRACTOR PODO
# Tarifa única 24h. La web NO usa "/" entre € y kWh/kW·día (aparecen en
# líneas separadas: "0.108 € kWh"), así que necesita un patrón propio en
# vez del genérico. La propia web indica que los precios son sin impuestos.
# ─────────────────────────────────────────────

async def extractor_podo(page, t):
    url = t["url"]
    try:
        await page.goto(url, timeout=60000, wait_until="domcontentloaded")
        await cerrar_cookies(page)
        await page.wait_for_timeout(3000)
        texto = await page.evaluate("() => document.body.innerText")

        kwh_raw = re.findall(r"(\d+[.,]\d{2,6})\s*€\s*kWh", texto, re.IGNORECASE)
        kw_raw  = re.findall(r"(\d+[.,]\d{2,6})\s*€\s*kW\s*/?\s*d[ií]a", texto, re.IGNORECASE)

        kwh_ok = [v for v in kwh_raw if es_precio_kwh(limpiar(v))]
        kw_ok  = [v for v in kw_raw  if es_precio_kw_dia(limpiar(v))]

        if not kwh_ok:
            return registro_vacio(t["comercializadora"], t["tarifa"], url, "Sin precios detectados")

        reg = registro_vacio(t["comercializadora"], t["tarifa"], url, "OK")
        reg["tipo_precio"] = "precio_unico_24h"
        reg["energia_unico_eur_kwh"] = fmt(limpiar(kwh_ok[0]))
        if kw_ok:
            reg["potencia_p1_eur_kw_dia"] = fmt(limpiar(kw_ok[0]))
            if len(kw_ok) > 1:
                reg["potencia_p2_eur_kw_dia"] = fmt(limpiar(kw_ok[1]))
        return reg
    except Exception as e:
        return registro_vacio(t["comercializadora"], t["tarifa"], url, f"Error: {str(e)[:120]}")

# ─────────────────────────────────────────────
# EXTRACTOR GENÉRICO (con filtros estrictos)
# ─────────────────────────────────────────────

async def extractor_generico(page, t):
    url = t["url"]
    tipo = t["tipo_esperado"]
    try:
        await page.goto(url, timeout=60000, wait_until="domcontentloaded")
        await cerrar_cookies(page)
        await page.wait_for_timeout(4000)
        texto = await page.evaluate("() => document.body.innerText")

        kwh_raw = re.findall(
            r"(\d+[.,]\d{3,6})\s*(?:€\s*/\s*kWh|€/kWh|euros?/kWh)",
            texto, re.IGNORECASE
        )
        if not kwh_raw:
            kwh_raw = re.findall(
                r"(\d+[.,]\d{4,6})[^\d]{0,15}(?:€\s*/\s*kWh|€/kWh|kWh)",
                texto, re.IGNORECASE
            )

        # Potencia: buscar €/kW·día explícitamente
        kw_raw = re.findall(
            r"(\d+[.,]\d{3,6})\s*€/kW\s*(?:día|dia|·día|·dia|/día|/dia)",
            texto, re.IGNORECASE
        )
        if not kw_raw:
            kw_raw = re.findall(
                r"(\d+[.,]\d{3,6})\s*€/kW(?!h)",
                texto, re.IGNORECASE
            )

        # Aplicar filtros estrictos
        kwh_ok = [v for v in kwh_raw if es_precio_kwh(limpiar(v))]
        kw_ok  = [v for v in kw_raw  if es_precio_kw_dia(limpiar(v))]

        if not kwh_ok:
            return registro_vacio(t["comercializadora"], t["tarifa"], url,
                                  "Sin precios €/kWh detectados")

        reg = registro_vacio(t["comercializadora"], t["tarifa"], url, "OK")
        reg["tipo_precio"] = tipo

        if tipo == "punta_llano_valle" and len(kwh_ok) >= 3:
            reg["energia_punta_eur_kwh"] = fmt(limpiar(kwh_ok[0]))
            reg["energia_llano_eur_kwh"] = fmt(limpiar(kwh_ok[1]))
            reg["energia_valle_eur_kwh"] = fmt(limpiar(kwh_ok[2]))
        elif tipo == "punta_llano_valle" and len(kwh_ok) == 2:
            reg["energia_punta_eur_kwh"] = fmt(limpiar(kwh_ok[0]))
            reg["energia_valle_eur_kwh"] = fmt(limpiar(kwh_ok[1]))
            reg["notas"] = "Solo 2 precios (punta/valle)"
        else:
            reg["energia_unico_eur_kwh"] = fmt(limpiar(kwh_ok[0]))
            reg["tipo_precio"] = "precio_unico_24h"

        if kw_ok:
            reg["potencia_p1_eur_kw_dia"] = fmt(limpiar(kw_ok[0]))
            if len(kw_ok) > 1:
                reg["potencia_p2_eur_kw_dia"] = fmt(limpiar(kw_ok[1]))
            else:
                # Un único valor de potencia en la página: mismo precio
                # para P1 y P2 (no dejar P2 en blanco).
                reg["potencia_p2_eur_kw_dia"] = fmt(limpiar(kw_ok[0]))
        return reg
    except Exception as e:
        return registro_vacio(t["comercializadora"], t["tarifa"], url, f"Error: {str(e)[:120]}")

# ─────────────────────────────────────────────
# MAPA DE EXTRACTORES
# ─────────────────────────────────────────────

EXTRACTORES = {
    "generico":  extractor_generico,
    "endesa":    extractor_endesa,
    "holaluz":   extractor_holaluz,
    "naturgy":   extractor_naturgy,
    "repsol":    extractor_repsol,
    "repsol_dh": extractor_repsol_dh,
    "imagina":   extractor_imagina,
    "iberdrola": extractor_iberdrola,
    "gana":      extractor_gana,
    "fenie":     extractor_fenie,
    "factor":    extractor_factor,
    "energyavm": extractor_energyavm,
    "totalenergies": extractor_totalenergies,
    "octopus":   extractor_octopus,
    "niba":      extractor_niba,
    "podo":      extractor_podo,
}

# ─────────────────────────────────────────────
# SCRAPING PRINCIPAL
# ─────────────────────────────────────────────

async def scrape_all():
    todos = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--window-size=1280,900",
            ]
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="es-ES",
            viewport={"width": 1280, "height": 900},
            extra_http_headers={
                "Accept-Language": "es-ES,es;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            }
        )
        # Eliminar firma de webdriver que detectan los anti-bots
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3]});
            Object.defineProperty(navigator, 'languages', {get: () => ['es-ES','es']});
            window.chrome = {runtime: {}};
        """)
        for t in TARIFAS:
            print(f"  [{t['comercializadora']}] {t['tarifa']} ...")
            page = await context.new_page()
            try:
                fn = EXTRACTORES[t["extractor"]]
                reg = await fn(page, t)
                todos.append(reg)
                kwh = (reg.get("energia_unico_eur_kwh") or
                       reg.get("energia_punta_eur_kwh") or "N/D")
                print(f"    → {reg['notas']} | €/kWh: {kwh}")
                # DEBUG: imprimir fragmento del texto para diagnóstico
                if reg["notas"] != "OK" or kwh == "N/D":
                    try:
                        debug_page = await context.new_page()
                        await debug_page.goto(t["url"], timeout=60000, wait_until="domcontentloaded")
                        await debug_page.wait_for_timeout(5000)
                        debug_texto = await debug_page.evaluate("() => document.body.innerText")
                        # Buscar fragmento con precio
                        import re as _re
                        fragmento = ""
                        for linea in debug_texto.split("\n"):
                            if any(x in linea.lower() for x in ["kwh","€/kw","energía","energia","potencia","precio"]):
                                fragmento += linea.strip() + " | "
                        print(f"    DEBUG precio lines: {fragmento[:400]}")
                        await debug_page.close()
                    except Exception as de:
                        print(f"    DEBUG error: {de}")
            except Exception as e:
                todos.append(registro_vacio(
                    t["comercializadora"], t["tarifa"], t["url"],
                    f"Error inesperado: {e}"
                ))
            finally:
                await page.close()
        await browser.close()
    return todos

# ─────────────────────────────────────────────
# GOOGLE SHEETS
# ─────────────────────────────────────────────

CABECERAS = [
    "fecha", "comercializadora", "tarifa", "tipo_precio",
    "potencia_p1_eur_kw_dia", "potencia_p2_eur_kw_dia",
    "energia_punta_eur_kwh", "energia_llano_eur_kwh",
    "energia_valle_eur_kwh", "energia_unico_eur_kwh",
    "variacion_pct", "euros_al_anio", "diferencia_eur_anual",
    "url", "notas"
]

# ─────────────────────────────────────────────
# COLUMNAS COMPARATIVAS (K, L, M)
# Supuestos fijos de consumo anual para el cálculo de "Euros al año":
# potencia contratada 4,5 kW, consumo Punta 810 kWh, Llano 970 kWh,
# Valle 1450 kWh (total 3.230 kWh/año). La potencia (€/kW·día) se
# anualiza multiplicando por 365 días.
# ─────────────────────────────────────────────

POTENCIA_CONTRATADA_KW = 4.5
DIAS_ANIO = 365
CONSUMO_PUNTA_KWH = 810
CONSUMO_LLANO_KWH = 970
CONSUMO_VALLE_KWH = 1450
CONSUMO_TOTAL_KWH = CONSUMO_PUNTA_KWH + CONSUMO_LLANO_KWH + CONSUMO_VALLE_KWH  # 3230

def _num(v):
    """Convierte un valor de registro (string, número o None/'') a float, o None si no es válido."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except Exception:
        return None

def fmt2(v) -> str:
    return str(round(v, 2))

def calcular_euros_al_anio(reg):
    """Devuelve (euros_al_anio, sin_potencia) para un registro.
    euros_al_anio es None si faltan datos de energía imprescindibles.
    sin_potencia es True si no había precio de potencia (se calculó solo
    con energía, tal y como se decidió para tarifas sin ese dato)."""
    p1 = _num(reg.get("potencia_p1_eur_kw_dia"))
    p2 = _num(reg.get("potencia_p2_eur_kw_dia"))
    sin_potencia = p1 is None and p2 is None
    coste_potencia = ((p1 or 0) + (p2 or 0)) * POTENCIA_CONTRATADA_KW * DIAS_ANIO

    if reg.get("tipo_precio") == "punta_llano_valle":
        punta = _num(reg.get("energia_punta_eur_kwh"))
        llano = _num(reg.get("energia_llano_eur_kwh"))
        valle = _num(reg.get("energia_valle_eur_kwh"))
        if None in (punta, llano, valle):
            return None, sin_potencia
        coste_energia = punta * CONSUMO_PUNTA_KWH + llano * CONSUMO_LLANO_KWH + valle * CONSUMO_VALLE_KWH
    else:
        unico = _num(reg.get("energia_unico_eur_kwh"))
        if unico is None:
            return None, sin_potencia
        coste_energia = unico * CONSUMO_TOTAL_KWH

    return round(coste_energia + coste_potencia, 2), sin_potencia

def calcular_precio_medio(reg):
    """Precio medio €/kWh ponderado con el mismo perfil de consumo usado en
    'Euros al año' (810/970/1450 kWh). Para tarifas 24h es directamente el
    precio único. Sirve para poder comparar en la misma unidad (K) tarifas
    24h contra Octopus Relax y tarifas P/L/V contra Octopus 3."""
    if reg.get("tipo_precio") == "punta_llano_valle":
        punta = _num(reg.get("energia_punta_eur_kwh"))
        llano = _num(reg.get("energia_llano_eur_kwh"))
        valle = _num(reg.get("energia_valle_eur_kwh"))
        if None in (punta, llano, valle):
            return None
        return (punta * CONSUMO_PUNTA_KWH + llano * CONSUMO_LLANO_KWH
                + valle * CONSUMO_VALLE_KWH) / CONSUMO_TOTAL_KWH
    return _num(reg.get("energia_unico_eur_kwh"))

def calcular_columnas_comparativas(registros):
    """Rellena variacion_pct, euros_al_anio y diferencia_eur_anual en cada
    registro, usando como referencia Octopus Relax (para tarifas 24h) y
    Octopus 3 (para tarifas punta_llano_valle) DE ESA MISMA EJECUCIÓN."""
    octopus_relax = next((r for r in registros
                          if r["comercializadora"] == "Octopus Energy" and r["tarifa"] == "Octopus Relax"), None)
    octopus_3 = next((r for r in registros
                      if r["comercializadora"] == "Octopus Energy" and r["tarifa"] == "Octopus 3"), None)

    precio_medio_relax = calcular_precio_medio(octopus_relax) if octopus_relax else None
    precio_medio_octopus3 = calcular_precio_medio(octopus_3) if octopus_3 else None
    euros_relax, _ = calcular_euros_al_anio(octopus_relax) if octopus_relax else (None, False)
    euros_octopus3, _ = calcular_euros_al_anio(octopus_3) if octopus_3 else (None, False)

    for r in registros:
        r["variacion_pct"] = ""
        r["euros_al_anio"] = ""
        r["diferencia_eur_anual"] = ""

        euros, sin_potencia = calcular_euros_al_anio(r)
        if euros is not None:
            r["euros_al_anio"] = fmt2(euros)
            if sin_potencia:
                r["notas"] = f'{r["notas"]} (Euros/año calculado solo con energía, sin potencia)'

        precio_propio = calcular_precio_medio(r)

        if r["tipo_precio"] == "precio_unico_24h":
            if precio_propio is not None and precio_medio_relax:
                r["variacion_pct"] = fmt2((precio_propio - precio_medio_relax) / precio_medio_relax * 100)
            euros_ref = euros_relax
        elif r["tipo_precio"] == "punta_llano_valle":
            if precio_propio is not None and precio_medio_octopus3:
                r["variacion_pct"] = fmt2((precio_propio - precio_medio_octopus3) / precio_medio_octopus3 * 100)
            euros_ref = euros_octopus3
        else:
            euros_ref = None

        if euros is not None and euros_ref is not None:
            r["diferencia_eur_anual"] = fmt2(euros - euros_ref)

    return registros

def guardar_en_sheets(registros):
    creds_dict = json.loads(CREDENTIALS_JSON)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=[
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive"
        ]
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_ID)
    try:
        ws = sh.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=SHEET_NAME, rows="5000", cols="20")
        ws.append_row(CABECERAS)

    fecha_hoy = datetime.date.today().isoformat()
    ultimo_por_tarifa = _obtener_ultimo_valor_por_tarifa(ws, fecha_hoy)
    cambios = detectar_cambios(registros, ultimo_por_tarifa, CAMPOS_COMPARABLES_LUZ, "luz")
    if cambios:
        guardar_cambios_en_sheets(sh, cambios)

    filas = [[str(r.get(c, "") or "") for c in CABECERAS] for r in registros]
    if filas:
        ws.append_rows(filas, value_input_option="RAW", table_range="A1")
        print(f"  ✓ {len(filas)} filas añadidas a Google Sheets")
    return cambios

def guardar_json_local(registros):
    fecha = datetime.date.today().isoformat()
    os.makedirs("data", exist_ok=True)
    with open(f"data/precios_{fecha}.json", "w", encoding="utf-8") as f:
        json.dump(registros, f, ensure_ascii=False, indent=2)
    print(f"  ✓ Backup JSON guardado")

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────


# ═════════════════════════════════════════════
# MÓDULO GAS (RL1 / RL2)
# ═════════════════════════════════════════════
# Supuestos fijos de consumo anual para "euros al año":
# RL1 = 3.800 kWh/año, RL2 = 7.500 kWh/año. El término fijo de todas las
# webs revisadas viene en €/mes, así que se anualiza ×12.
# Referencia de comparación (columnas F/G/H): Octopus Gas RL1 / RL2.
#
# Nota importante sobre "RL2": varias comercializadoras (Naturgy, Holaluz,
# Plenitude) subdividen el tramo oficial RL2 (5.000-50.000 kWh/año) en sus
# propios sub-tramos comerciales "RL.2" (5.000-15.000) y "RL.3"
# (15.000-50.000). Por decisión explícita (17 sep 2026), usamos siempre el
# precio que la propia web etiqueta literalmente como "RL2", aunque su
# rango real no cubra los 7.500 kWh/año del supuesto — con el consumo
# corregido a 7.500 kWh esto ya no genera inconsistencia real, porque cae
# dentro de ese tramo "RL2" en todas las webs revisadas.
# ═════════════════════════════════════════════

CONSUMO_RL1_KWH = 3800
CONSUMO_RL2_KWH = 7500
MESES_ANIO = 12

def es_precio_gas_variable(valor) -> bool:
    """€/kWh de gas: entre 0.03 y 0.30 (más bajo que la energía eléctrica)."""
    return valor is not None and 0.03 < valor < 0.30

def es_precio_gas_fijo_mensual(valor) -> bool:
    """Término fijo de gas: entre 1 y 50 €/mes."""
    return valor is not None and 1 < valor < 50

def registro_vacio_gas(comercializadora, tarifa, tipo_precio, url, notas):
    return {
        "fecha": datetime.date.today().isoformat(),
        "comercializadora": comercializadora,
        "tarifa": tarifa,
        "tipo_precio": tipo_precio,
        "termino_fijo_eur_mes": "",
        "termino_variable_eur_kwh": "",
        "variacion_pct": "",
        "euros_al_anio": "",
        "diferencia_eur_anual": "",
        "url": url,
        "notas": notas,
    }

CABECERAS_GAS = [
    "fecha", "comercializadora", "tarifa", "tipo_precio",
    "termino_fijo_eur_mes", "termino_variable_eur_kwh",
    "variacion_pct", "euros_al_anio", "diferencia_eur_anual",
    "url", "notas",
]

# ─────────────────────────────────────────────
# TARIFAS_GAS — una entrada por cada RL1/RL2 de cada tarifa
# ─────────────────────────────────────────────

TARIFAS_GAS = [
    # NATURGY
    {"comercializadora": "Naturgy", "tarifa": "Tarifa Por Uso Gas RL1",
     "url": "https://www.naturgy.es/hogar/gas/tarifa_por_uso_gas",
     "tipo_precio": "RL1", "extractor": "naturgy_gas"},
    {"comercializadora": "Naturgy", "tarifa": "Tarifa Por Uso Gas RL2",
     "url": "https://www.naturgy.es/hogar/gas/tarifa_por_uso_gas",
     "tipo_precio": "RL2", "extractor": "naturgy_gas"},
    # ENDESA — dos tarifas distintas en la misma página
    {"comercializadora": "Endesa", "tarifa": "Tarifa Gas 10% Dto RL1",
     "url": "https://www.endesa.com/es/luz-y-gas/gas",
     "tipo_precio": "RL1", "extractor": "endesa_gas_dto"},
    {"comercializadora": "Endesa", "tarifa": "Tarifa Gas 10% Dto RL2",
     "url": "https://www.endesa.com/es/luz-y-gas/gas",
     "tipo_precio": "RL2", "extractor": "endesa_gas_dto"},
    {"comercializadora": "Endesa", "tarifa": "Conecta Gas RL1",
     "url": "https://www.endesa.com/es/luz-y-gas/gas",
     "tipo_precio": "RL1", "extractor": "endesa_gas_conecta"},
    {"comercializadora": "Endesa", "tarifa": "Conecta Gas RL2",
     "url": "https://www.endesa.com/es/luz-y-gas/gas",
     "tipo_precio": "RL2", "extractor": "endesa_gas_conecta"},
    # IBERDROLA
    {"comercializadora": "Iberdrola", "tarifa": "Plan Gas Hogar RL1",
     "url": "https://www.iberdrola.es/gas/tarifas/plan-gas-hogar",
     "tipo_precio": "RL1", "extractor": "iberdrola_gas"},
    {"comercializadora": "Iberdrola", "tarifa": "Plan Gas Hogar RL2",
     "url": "https://www.iberdrola.es/gas/tarifas/plan-gas-hogar",
     "tipo_precio": "RL2", "extractor": "iberdrola_gas"},
    # REPSOL — URLs ya separadas por RL
    {"comercializadora": "Repsol", "tarifa": "Tarifa Gas RL1",
     "url": "https://www.repsol.es/particulares/hogar/luz-y-gas/tarifas/tarifa-gas-rl1/",
     "tipo_precio": "RL1", "extractor": "repsol_gas"},
    {"comercializadora": "Repsol", "tarifa": "Tarifa Gas RL2",
     "url": "https://www.repsol.es/particulares/hogar/luz-y-gas/tarifas/tarifa-gas-rl2/",
     "tipo_precio": "RL2", "extractor": "repsol_gas"},
    # TOTALENERGIES
    {"comercializadora": "TotalEnergies", "tarifa": "A tu Aire Gas RL1",
     "url": "https://www.totalenergies.es/es/hogares/tarifas-gas/a-tu-aire",
     "tipo_precio": "RL1", "extractor": "totalenergies_gas"},
    {"comercializadora": "TotalEnergies", "tarifa": "A tu Aire Gas RL2",
     "url": "https://www.totalenergies.es/es/hogares/tarifas-gas/a-tu-aire",
     "tipo_precio": "RL2", "extractor": "totalenergies_gas"},
    # PLENITUDE
    {"comercializadora": "Plenitude", "tarifa": "Facil Plus Gas RL1",
     "url": "https://eniplenitude.es/hogar/tarifas-gas/",
     "tipo_precio": "RL1", "extractor": "plenitude_gas"},
    {"comercializadora": "Plenitude", "tarifa": "Facil Plus Gas RL2",
     "url": "https://eniplenitude.es/hogar/tarifas-gas/",
     "tipo_precio": "RL2", "extractor": "plenitude_gas"},
    # HOLALUZ
    {"comercializadora": "Holaluz", "tarifa": "Tarifa Gas Natural RL1",
     "url": "https://www.holaluz.com/tarifa-gas-natural",
     "tipo_precio": "RL1", "extractor": "holaluz_gas"},
    {"comercializadora": "Holaluz", "tarifa": "Tarifa Gas Natural RL2",
     "url": "https://www.holaluz.com/tarifa-gas-natural",
     "tipo_precio": "RL2", "extractor": "holaluz_gas"},
    # OCTOPUS ENERGY (base de comparación)
    {"comercializadora": "Octopus Energy", "tarifa": "Octopus Gas RL1",
     "url": "https://octopusenergy.es/precios",
     "tipo_precio": "RL1", "extractor": "octopus_gas"},
    {"comercializadora": "Octopus Energy", "tarifa": "Octopus Gas RL2",
     "url": "https://octopusenergy.es/precios",
     "tipo_precio": "RL2", "extractor": "octopus_gas"},
    # FENIE ENERGIA
    {"comercializadora": "Fenie Energia", "tarifa": "Fijo RL1 Gas RL1",
     "url": "https://www.fenieenergia.es/es/hogar/tarifas-de-gas/rl1",
     "tipo_precio": "RL1", "extractor": "fenie_gas"},
    {"comercializadora": "Fenie Energia", "tarifa": "Fijo RL1 Gas RL2",
     "url": "https://www.fenieenergia.es/es/hogar/tarifas-de-gas/rl1",
     "tipo_precio": "RL2", "extractor": "fenie_gas"},
    # NIBA
    {"comercializadora": "Niba", "tarifa": "Niba Gas RL1",
     "url": "https://niba.es/luz-y-gas",
     "tipo_precio": "RL1", "extractor": "niba_gas"},
    {"comercializadora": "Niba", "tarifa": "Niba Gas RL2",
     "url": "https://niba.es/luz-y-gas",
     "tipo_precio": "RL2", "extractor": "niba_gas"},
]

# ─────────────────────────────────────────────
# EXTRACTOR NATURGY GAS
# Bloques "RL.1 ... Término fijo Término variable\n<fijo> €/mes <var> €/kWh"
# ─────────────────────────────────────────────

async def extractor_naturgy_gas(page, t):
    url = t["url"]
    tipo = t["tipo_precio"]
    try:
        await page.goto(url, timeout=60000, wait_until="domcontentloaded")
        await cerrar_cookies(page)
        await page.wait_for_timeout(3000)
        texto = await page.evaluate("() => document.body.innerText")

        etiqueta = "RL.1" if tipo == "RL1" else "RL.2"
        patron = re.compile(
            re.escape(etiqueta) + r"[^\n]*\n.*?(\d+[.,]\d+)\s*€/mes\s*(\d+[.,]\d+)\s*€/kWh",
            re.IGNORECASE | re.DOTALL,
        )
        m = patron.search(texto)
        if not m:
            return registro_vacio_gas(t["comercializadora"], t["tarifa"], tipo, url,
                                      f"Naturgy gas: no se encontró el bloque {etiqueta}")
        reg = registro_vacio_gas(t["comercializadora"], t["tarifa"], tipo, url, "OK")
        reg["termino_fijo_eur_mes"] = fmt(limpiar(m.group(1)))
        reg["termino_variable_eur_kwh"] = fmt(limpiar(m.group(2)))
        return reg
    except Exception as e:
        return registro_vacio_gas(t["comercializadora"], t["tarifa"], tipo, url, f"Error: {str(e)[:120]}")

# ─────────────────────────────────────────────
# EXTRACTORES ENDESA GAS
# Dos tarifas en la misma página; el precio por RL está oculto tras un
# "leer más" que hay que expandir con clic. El término fijo (7,181 €/mes)
# es el mismo para ambas tarifas y no varía por RL.
# ─────────────────────────────────────────────

async def _endesa_gas_texto_expandido(page, url):
    await page.goto(url, timeout=60000, wait_until="domcontentloaded")
    await cerrar_cookies(page)
    await page.wait_for_timeout(3000)
    await page.evaluate("""() => {
        document.querySelectorAll('*').forEach(el => {
            if (el.children.length === 0 && /leer más/i.test(el.textContent||'')) el.click();
        });
    }""")
    await page.wait_for_timeout(1500)
    return await page.evaluate("() => document.body.innerText")

async def extractor_endesa_gas_dto(page, t):
    url = t["url"]
    tipo = t["tipo_precio"]
    try:
        texto = await _endesa_gas_texto_expandido(page, url)
        m_rl1 = re.search(
            r"En gas RL1 el precio base sin impuestos es\s*(\d+[.,]\d+)\s*€/kWh\s*y\s*(\d+[.,]\d+)\s*€/kWh con",
            texto, re.IGNORECASE)
        m_rl2 = re.search(
            r"El precio base de gas RL2 es\s*(\d+[.,]\d+)\s*€/kWh\s*y\s*(\d+[.,]\d+)\s*€/kWh con",
            texto, re.IGNORECASE)
        m_fijo = re.search(r"(\d+[.,]\d+)\s*€/mes", texto, re.IGNORECASE)
        m = m_rl1 if tipo == "RL1" else m_rl2
        if not m or not m_fijo:
            return registro_vacio_gas(t["comercializadora"], t["tarifa"], tipo, url,
                                      "Endesa gas (10% dto): precios no encontrados")
        reg = registro_vacio_gas(t["comercializadora"], t["tarifa"], tipo, url, "OK")
        reg["termino_variable_eur_kwh"] = fmt(limpiar(m.group(2)))  # valor "con descuento"
        reg["termino_fijo_eur_mes"] = fmt(limpiar(m_fijo.group(1)))
        return reg
    except Exception as e:
        return registro_vacio_gas(t["comercializadora"], t["tarifa"], tipo, url, f"Error: {str(e)[:120]}")

async def extractor_endesa_gas_conecta(page, t):
    url = t["url"]
    tipo = t["tipo_precio"]
    try:
        texto = await _endesa_gas_texto_expandido(page, url)
        m_rl1 = re.search(r"En gas RL1 es\s*(\d+[.,]\d+)\s*€/kWh", texto, re.IGNORECASE)
        m_rl2 = re.search(r"En gas RL2 es\s*(\d+[.,]\d+)\s*€/kWh", texto, re.IGNORECASE)
        m_fijo = re.search(r"(\d+[.,]\d+)\s*€/mes", texto, re.IGNORECASE)
        m = m_rl1 if tipo == "RL1" else m_rl2
        if not m or not m_fijo:
            return registro_vacio_gas(t["comercializadora"], t["tarifa"], tipo, url,
                                      "Endesa gas (Conecta): precios no encontrados")
        reg = registro_vacio_gas(t["comercializadora"], t["tarifa"], tipo, url, "OK")
        reg["termino_variable_eur_kwh"] = fmt(limpiar(m.group(1)))
        reg["termino_fijo_eur_mes"] = fmt(limpiar(m_fijo.group(1)))
        return reg
    except Exception as e:
        return registro_vacio_gas(t["comercializadora"], t["tarifa"], tipo, url, f"Error: {str(e)[:120]}")

# ─────────────────────────────────────────────
# EXTRACTOR IBERDROLA GAS
# Requiere clic en la pestaña "Tarifa RL.1"/"Tarifa RL.2" y luego en
# "Ver más información" para que aparezca el div #precios-dinamicos.
# ─────────────────────────────────────────────

async def extractor_iberdrola_gas(page, t):
    url = t["url"]
    tipo = t["tipo_precio"]
    try:
        # 1) ZenRows con js_render + js_instructions: hace clic en la pestaña
        #    RL2 (RL1 ya viene seleccionada por defecto) y en "Ver más
        #    información" (#ver-detalle) antes de devolver el HTML. Mismo
        #    bloqueo por IP que en la luz de Iberdrola, misma solución.
        origen = "ZenRows"
        clic_rl2_js = (
            "document.querySelectorAll('button.btn-potencia').forEach(b => "
            "{ if (b.querySelector('span.llamas2')) b.click(); })"
        )
        clic_detalle_js = "const b=document.getElementById('ver-detalle'); if (b) b.click();"
        if tipo == "RL2":
            instrucciones = [
                {"wait": 1500},
                {"evaluate": clic_rl2_js},
                {"wait": 1000},
                {"evaluate": clic_detalle_js},
                {"wait": 1500},
            ]
        else:
            instrucciones = [
                {"wait": 1500},
                {"evaluate": clic_detalle_js},
                {"wait": 1500},
            ]
        html_content = fetch_via_zenrows(url, js_render=True, js_instructions=instrucciones)

        fijo = variable = None
        if html_content:
            soup = BeautifulSoup(html_content, "html.parser")
            div = soup.find(id="precios-dinamicos")
            if div:
                texto_div = div.get_text(separator=" ", strip=True)
                m_fijo = re.search(r"(\d+[.,]\d+)\s*€/mes", texto_div)
                m_var = re.search(r"(\d+[.,]\d+)\s*€/kWh", texto_div)
                if m_fijo:
                    fijo = limpiar(m_fijo.group(1))
                if m_var:
                    variable = limpiar(m_var.group(1))

        # 2) Último recurso: Playwright directo (rara vez funciona por el anti-bot)
        if fijo is None or variable is None:
            origen = "Playwright"
            print(f"    [Iberdrola gas] ZenRows sin resultado, usando Playwright")
            await page.goto(url, timeout=60000, wait_until="domcontentloaded")
            await cerrar_cookies(page)
            await page.wait_for_timeout(2500)

            etiqueta = "Tarifa RL.1" if tipo == "RL1" else "Tarifa RL.2"
            await page.evaluate("""(etiqueta) => {
                const tab = [...document.querySelectorAll('button,[role="tab"],a')]
                    .find(el => el.textContent.trim() === etiqueta);
                if (tab) tab.click();
            }""", etiqueta)
            await page.wait_for_timeout(1000)

            await page.evaluate("""() => {
                const btn = document.getElementById('ver-detalle');
                if (btn) btn.click();
            }""")
            await page.wait_for_timeout(1500)

            div_text = await page.evaluate("""() => {
                const div = document.getElementById('precios-dinamicos');
                return div ? div.innerText : '';
            }""")
            m_fijo = re.search(r"(\d+[.,]\d+)\s*€/mes", div_text, re.IGNORECASE)
            m_var = re.search(r"(\d+[.,]\d+)\s*€/kWh", div_text, re.IGNORECASE)
            if m_fijo:
                fijo = limpiar(m_fijo.group(1))
            if m_var:
                variable = limpiar(m_var.group(1))

        if fijo is None or variable is None:
            return registro_vacio_gas(t["comercializadora"], t["tarifa"], tipo, url,
                                      "Iberdrola gas: no se encontraron los precios (ZenRows/Playwright fallaron)")
        reg = registro_vacio_gas(t["comercializadora"], t["tarifa"], tipo, url, f"OK ({origen})")
        reg["termino_fijo_eur_mes"] = fmt(fijo)
        reg["termino_variable_eur_kwh"] = fmt(variable)
        return reg
    except Exception as e:
        return registro_vacio_gas(t["comercializadora"], t["tarifa"], tipo, url, f"Error: {str(e)[:120]}")

# ─────────────────────────────────────────────
# EXTRACTOR REPSOL GAS
# Igual patrón que el extractor de luz: 2 variantes (con/sin asistente) en
# la misma página, nos quedamos con el precio mínimo de cada par.
# ─────────────────────────────────────────────

async def extractor_repsol_gas(page, t):
    url = t["url"]
    tipo = t["tipo_precio"]
    try:
        await page.goto(url, timeout=60000, wait_until="domcontentloaded")
        await cerrar_cookies(page)
        await page.wait_for_timeout(4000)
        texto = await page.evaluate("() => document.body.innerText")

        kwh_raw = re.findall(r"(\d+[.,]\d{3,6})\s*€/kWh", texto, re.IGNORECASE)
        mes_raw = re.findall(r"(\d+[.,]\d{2,6})\s*€\s*/?\s*mes", texto, re.IGNORECASE)

        kwh_ok = [v for v in (limpiar(x) for x in kwh_raw) if es_precio_gas_variable(v)]
        mes_ok = [v for v in (limpiar(x) for x in mes_raw) if es_precio_gas_fijo_mensual(v)]

        if not kwh_ok or not mes_ok:
            return registro_vacio_gas(t["comercializadora"], t["tarifa"], tipo, url,
                                      "Repsol gas: precios no detectados")
        reg = registro_vacio_gas(t["comercializadora"], t["tarifa"], tipo, url, "OK")
        reg["termino_variable_eur_kwh"] = fmt(min(kwh_ok))
        reg["termino_fijo_eur_mes"] = fmt(min(mes_ok))
        return reg
    except Exception as e:
        return registro_vacio_gas(t["comercializadora"], t["tarifa"], tipo, url, f"Error: {str(e)[:120]}")

# ─────────────────────────────────────────────
# EXTRACTOR TOTALENERGIES GAS
# RL1 y RL2 en la misma página, con clic en "Ver más información" para
# revelar el término fijo de cada bloque.
# ─────────────────────────────────────────────

async def extractor_totalenergies_gas(page, t):
    url = t["url"]
    tipo = t["tipo_precio"]
    try:
        etiqueta = "Precio tarifa Gas RL1" if tipo == "RL1" else "Precio tarifa Gas RL2"
        siguiente = "Precio tarifa Gas RL2" if tipo == "RL1" else "Contrata online"

        def _extraer_bloque(texto):
            idx = texto.find(etiqueta)
            if idx == -1:
                return None, None
            idx_fin = texto.find(siguiente, idx + 1)
            bloque = texto[idx: idx_fin if idx_fin != -1 else idx + 400]
            m_var = re.search(r"(\d+[.,]\d+)\s*€/kWh", bloque, re.IGNORECASE)
            m_fijo = re.search(r"(\d+[.,]\d+)\s*€/mes", bloque, re.IGNORECASE)
            return (limpiar(m_var.group(1)) if m_var else None,
                    limpiar(m_fijo.group(1)) if m_fijo else None)

        # 1) ZenRows con js_render — mismo patrón que la luz de TotalEnergies
        #    (proxy_country + Referer, ya que bloquea también a ZenRows sin
        #    esos parámetros). Además hay que desplegar el término fijo con
        #    clic en "Ver más información" — hay 2 botones idénticos (mismo
        #    class, sin id único), así que se usa "evaluate" para hacer clic
        #    en todos a la vez en vez de "click" (que solo pincha el primero).
        origen = "ZenRows"
        variable = fijo = None
        html_content = fetch_via_zenrows(
            url, js_render=True,
            proxy_country="es", referer="https://www.google.com",
            js_instructions=[
                {"wait": 2000},
                {"evaluate": "document.querySelectorAll('.js-contenido-plegado-toggle').forEach(el => el.click())"},
                {"wait": 2000},
            ],
        )
        if html_content:
            variable, fijo = _extraer_bloque(_texto_visible(html_content))

        # 2) Último recurso: Playwright directo, con espera activa (polling)
        #    en vez de un sleep fijo — la SPA puede tardar un tiempo
        #    variable en calcular el precio (visto en la luz de TotalEnergies).
        if variable is None or fijo is None:
            origen = "Playwright"
            print(f"    [TotalEnergies gas] ZenRows sin resultado, usando Playwright")
            await page.goto(url, timeout=60000, wait_until="domcontentloaded",
                           referer="https://www.google.com")
            await cerrar_cookies(page)
            await page.evaluate("""() => {
                document.querySelectorAll('button,a').forEach(el => {
                    if (/Ver más información/i.test(el.textContent||'')) el.click();
                });
            }""")
            texto = ""
            for _ in range(30):  # hasta ~15s en total (30 x 500ms)
                texto = await page.evaluate("() => document.body.innerText")
                if re.search(r"\d[.,]\d{2,6}\s*€\s*/\s*kWh", texto, re.IGNORECASE):
                    break
                await page.wait_for_timeout(500)
            variable, fijo = _extraer_bloque(texto)

        if variable is None or fijo is None:
            return registro_vacio_gas(t["comercializadora"], t["tarifa"], tipo, url,
                                      "TotalEnergies gas: precios no encontrados (ZenRows/Playwright fallaron)")
        reg = registro_vacio_gas(t["comercializadora"], t["tarifa"], tipo, url, f"OK ({origen})")
        reg["termino_variable_eur_kwh"] = fmt(variable)
        reg["termino_fijo_eur_mes"] = fmt(fijo)
        return reg
    except Exception as e:
        return registro_vacio_gas(t["comercializadora"], t["tarifa"], tipo, url, f"Error: {str(e)[:120]}")

# ─────────────────────────────────────────────
# EXTRACTOR PLENITUDE GAS
# Requiere clic en "Ver condiciones de la tarifa" (enlace Vue.js — hace
# falta disparar eventos de ratón reales, un .click() simple no basta)
# para abrir un modal con la tabla completa RL.1/RL.2/RL.3, sin y con
# impuestos. Usamos la PRIMERA aparición del bloque (tabla "Sin impuestos").
# ─────────────────────────────────────────────

async def extractor_plenitude_gas(page, t):
    url = t["url"]
    tipo = t["tipo_precio"]
    try:
        await page.goto(url, timeout=60000, wait_until="domcontentloaded")
        await cerrar_cookies(page)
        await page.wait_for_timeout(3000)
        await page.evaluate("""() => {
            const link = [...document.querySelectorAll('a,button')]
                .find(el => /Ver condiciones de la tarifa/i.test(el.textContent||''));
            if (link) {
                ['mousedown','mouseup','click'].forEach(type => {
                    link.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true}));
                });
            }
        }""")
        await page.wait_for_timeout(1500)
        texto = await page.evaluate("() => document.body.innerText")

        etiqueta = "(RL.1)" if tipo == "RL1" else "(RL.2)"
        m = re.search(re.escape(etiqueta) + r"\s*(\d+[.,]\d+)\s*(\d+[.,]\d+)", texto)
        if not m:
            return registro_vacio_gas(t["comercializadora"], t["tarifa"], tipo, url,
                                      f"Plenitude gas: no se encontró el bloque {etiqueta}")
        reg = registro_vacio_gas(t["comercializadora"], t["tarifa"], tipo, url, "OK")
        reg["termino_fijo_eur_mes"] = fmt(limpiar(m.group(1)))
        reg["termino_variable_eur_kwh"] = fmt(limpiar(m.group(2)))
        return reg
    except Exception as e:
        return registro_vacio_gas(t["comercializadora"], t["tarifa"], tipo, url, f"Error: {str(e)[:120]}")

# ─────────────────────────────────────────────
# EXTRACTOR HOLALUZ GAS
# Sin interacción: dos bloques de texto estáticos ("fijo" y "variable"),
# cada uno con las etiquetas RL1/RL2/RL3 repetidas.
# ─────────────────────────────────────────────

async def extractor_holaluz_gas(page, t):
    url = t["url"]
    tipo = t["tipo_precio"]
    try:
        await page.goto(url, timeout=60000, wait_until="domcontentloaded")
        await cerrar_cookies(page)
        await page.wait_for_timeout(3000)
        texto = await page.evaluate("() => document.body.innerText")

        idx_fijo = texto.find("Precio término fijo sin impuestos")
        idx_var = texto.find("Precio término variable sin impuestos")
        if idx_fijo == -1 or idx_var == -1:
            return registro_vacio_gas(t["comercializadora"], t["tarifa"], tipo, url,
                                      "Holaluz gas: no se encontraron las secciones de precio")
        bloque_fijo = texto[idx_fijo:idx_var]
        bloque_var = texto[idx_var:idx_var + 800]

        etiqueta = "RL1" if tipo == "RL1" else "RL2"
        m_fijo = re.search(re.escape(etiqueta) + r"\s*(\d+[.,]\d+)\s*€/mes", bloque_fijo)
        m_var = re.search(re.escape(etiqueta) + r"\s*(\d+[.,]\d+)\s*€/kWh", bloque_var)
        if not (m_fijo and m_var):
            return registro_vacio_gas(t["comercializadora"], t["tarifa"], tipo, url,
                                      f"Holaluz gas: no se encontró el precio para {etiqueta}")
        reg = registro_vacio_gas(t["comercializadora"], t["tarifa"], tipo, url, "OK")
        reg["termino_fijo_eur_mes"] = fmt(limpiar(m_fijo.group(1)))
        reg["termino_variable_eur_kwh"] = fmt(limpiar(m_var.group(1)))
        return reg
    except Exception as e:
        return registro_vacio_gas(t["comercializadora"], t["tarifa"], tipo, url, f"Error: {str(e)[:120]}")

# ─────────────────────────────────────────────
# EXTRACTOR OCTOPUS GAS
# Requiere clic en la pestaña "Gas" y luego en "Más info" (el popup revela
# RL1/RL2/RL3; la tarjeta principal solo muestra RL1).
# ─────────────────────────────────────────────

async def extractor_octopus_gas(page, t):
    url = t["url"]
    tipo = t["tipo_precio"]
    try:
        await page.goto(url, timeout=60000, wait_until="domcontentloaded")
        await cerrar_cookies(page)
        await page.wait_for_timeout(2500)
        await page.evaluate("""() => {
            const gasBtn = [...document.querySelectorAll('button,a')]
                .find(el => el.textContent.trim() === 'Gas');
            if (gasBtn) gasBtn.click();
        }""")
        await page.wait_for_timeout(1500)
        await page.evaluate("""() => {
            const info = [...document.querySelectorAll('button,a')]
                .find(el => /Más info/i.test(el.textContent||''));
            if (info) info.click();
        }""")
        await page.wait_for_timeout(1500)
        texto = await page.evaluate("() => document.body.innerText")

        etiqueta = "Gas RL1" if tipo == "RL1" else "Gas RL2"
        siguiente = "Gas RL2" if tipo == "RL1" else "Gas RL3"
        idx = texto.find(etiqueta)
        if idx == -1:
            return registro_vacio_gas(t["comercializadora"], t["tarifa"], tipo, url,
                                      f"Octopus gas: no se encontró '{etiqueta}'")
        idx_fin = texto.find(siguiente, idx + 1)
        bloque = texto[idx: idx_fin if idx_fin != -1 else idx + 300]

        m_fijo = re.search(r"(\d+[.,]\d+)\s*€/mes", bloque, re.IGNORECASE)
        m_var = re.search(r"(\d+[.,]\d+)\s*€/kWh", bloque, re.IGNORECASE)
        if not (m_fijo and m_var):
            return registro_vacio_gas(t["comercializadora"], t["tarifa"], tipo, url,
                                      "Octopus gas: precios no encontrados")
        reg = registro_vacio_gas(t["comercializadora"], t["tarifa"], tipo, url, "OK")
        reg["termino_fijo_eur_mes"] = fmt(limpiar(m_fijo.group(1)))
        reg["termino_variable_eur_kwh"] = fmt(limpiar(m_var.group(1)))
        return reg
    except Exception as e:
        return registro_vacio_gas(t["comercializadora"], t["tarifa"], tipo, url, f"Error: {str(e)[:120]}")

# ─────────────────────────────────────────────
# EXTRACTOR FENIE ENERGIA GAS
# Requiere clic en el interruptor "Tengo la calefacción con gas"
# (input.heating-switch) para pasar de RL1 a RL2.
# NOTA: la web etiqueta el precio como "con impuestos incluidos", pero al
# contrastarlo con la página de condiciones/impuestos (que da los importes
# con IVA/IGIC ya aplicados) el número no cuadra como precio final — encaja
# mejor como precio BASE sin impuestos. Se usa tal cual aparece en la web,
# igual que con el resto de comercializadoras.
# ─────────────────────────────────────────────

async def extractor_fenie_gas(page, t):
    url = t["url"]
    tipo = t["tipo_precio"]
    try:
        await page.goto(url, timeout=60000, wait_until="domcontentloaded")
        await cerrar_cookies(page)
        await page.wait_for_timeout(2500)
        if tipo == "RL2":
            await page.evaluate("""() => {
                const input = document.querySelector('input.heating-switch');
                if (input) input.click();
            }""")
            await page.wait_for_timeout(1200)
        texto = await page.evaluate("() => document.body.innerText")

        idx = texto.find("Tengo la calefacción con gas")
        bloque = texto[idx: idx + 300] if idx != -1 else texto
        m_var = re.search(r"(\d+[.,]\d+)\s*€/\s*kWh", bloque, re.IGNORECASE)
        m_fijo = re.search(r"(\d+[.,]\d+)\s*€/mes", bloque, re.IGNORECASE)
        if not (m_var and m_fijo):
            return registro_vacio_gas(t["comercializadora"], t["tarifa"], tipo, url,
                                      "Fenie gas: precios no encontrados")
        reg = registro_vacio_gas(t["comercializadora"], t["tarifa"], tipo, url, "OK")
        reg["termino_variable_eur_kwh"] = fmt(limpiar(m_var.group(1)))
        reg["termino_fijo_eur_mes"] = fmt(limpiar(m_fijo.group(1)))
        return reg
    except Exception as e:
        return registro_vacio_gas(t["comercializadora"], t["tarifa"], tipo, url, f"Error: {str(e)[:120]}")

# ─────────────────────────────────────────────
# EXTRACTOR NIBA GAS
# No requiere ninguna interacción: el modal con las 3 tarifas (RL1/RL2/RL3)
# ya está en el HTML de la página desde la carga inicial, solo oculto
# visualmente (class="modal hidden").
# ─────────────────────────────────────────────

async def extractor_niba_gas(page, t):
    url = t["url"]
    tipo = t["tipo_precio"]
    try:
        await page.goto(url, timeout=60000, wait_until="domcontentloaded")
        await cerrar_cookies(page)
        await page.wait_for_timeout(2500)
        texto = await page.evaluate("""() => {
            const modals = [...document.querySelectorAll('[class*="modal" i]')];
            const target = modals.find(m => /Tarifas de gas/i.test(m.textContent||'') && /RL1/.test(m.textContent||''));
            return target ? target.textContent : '';
        }""")
        if not texto:
            return registro_vacio_gas(t["comercializadora"], t["tarifa"], tipo, url,
                                      "Niba gas: no se encontró el modal de tarifas")

        etiqueta = "GAS RL1" if tipo == "RL1" else "GAS RL2"
        m = re.search(
            re.escape(etiqueta) + r".*?Término fijo\s*\((\d+[.,]\d+)\s*€/mes\).*?"
            r"Término variable\s*\((\d+[.,]\d+)\s*€/kWh\)",
            texto, re.IGNORECASE | re.DOTALL,
        )
        if not m:
            return registro_vacio_gas(t["comercializadora"], t["tarifa"], tipo, url,
                                      f"Niba gas: no se encontró el bloque {etiqueta}")
        reg = registro_vacio_gas(t["comercializadora"], t["tarifa"], tipo, url, "OK")
        reg["termino_fijo_eur_mes"] = fmt(limpiar(m.group(1)))
        reg["termino_variable_eur_kwh"] = fmt(limpiar(m.group(2)))
        return reg
    except Exception as e:
        return registro_vacio_gas(t["comercializadora"], t["tarifa"], tipo, url, f"Error: {str(e)[:120]}")

EXTRACTORES_GAS = {
    "naturgy_gas": extractor_naturgy_gas,
    "endesa_gas_dto": extractor_endesa_gas_dto,
    "endesa_gas_conecta": extractor_endesa_gas_conecta,
    "iberdrola_gas": extractor_iberdrola_gas,
    "repsol_gas": extractor_repsol_gas,
    "totalenergies_gas": extractor_totalenergies_gas,
    "plenitude_gas": extractor_plenitude_gas,
    "holaluz_gas": extractor_holaluz_gas,
    "octopus_gas": extractor_octopus_gas,
    "fenie_gas": extractor_fenie_gas,
    "niba_gas": extractor_niba_gas,
}

# ─────────────────────────────────────────────
# COLUMNAS COMPARATIVAS DE GAS (F, G, H)
# ─────────────────────────────────────────────

def calcular_euros_al_anio_gas(reg):
    fijo = _num(reg.get("termino_fijo_eur_mes"))
    variable = _num(reg.get("termino_variable_eur_kwh"))
    if fijo is None or variable is None:
        return None
    consumo = CONSUMO_RL1_KWH if reg.get("tipo_precio") == "RL1" else CONSUMO_RL2_KWH
    return round(fijo * MESES_ANIO + variable * consumo, 2)

def calcular_columnas_comparativas_gas(registros):
    """Rellena variacion_pct, euros_al_anio y diferencia_eur_anual en cada
    registro de gas, usando Octopus Gas RL1/RL2 como referencia."""
    octopus_rl1 = next((r for r in registros
                        if r["comercializadora"] == "Octopus Energy" and r["tipo_precio"] == "RL1"), None)
    octopus_rl2 = next((r for r in registros
                        if r["comercializadora"] == "Octopus Energy" and r["tipo_precio"] == "RL2"), None)

    var_octopus_rl1 = _num(octopus_rl1.get("termino_variable_eur_kwh")) if octopus_rl1 else None
    var_octopus_rl2 = _num(octopus_rl2.get("termino_variable_eur_kwh")) if octopus_rl2 else None
    euros_octopus_rl1 = calcular_euros_al_anio_gas(octopus_rl1) if octopus_rl1 else None
    euros_octopus_rl2 = calcular_euros_al_anio_gas(octopus_rl2) if octopus_rl2 else None

    for r in registros:
        r["variacion_pct"] = ""
        r["euros_al_anio"] = ""
        r["diferencia_eur_anual"] = ""

        euros = calcular_euros_al_anio_gas(r)
        if euros is not None:
            r["euros_al_anio"] = fmt2(euros)

        variable_propio = _num(r.get("termino_variable_eur_kwh"))
        if r["tipo_precio"] == "RL1":
            var_ref, euros_ref = var_octopus_rl1, euros_octopus_rl1
        else:
            var_ref, euros_ref = var_octopus_rl2, euros_octopus_rl2

        if variable_propio is not None and var_ref:
            r["variacion_pct"] = fmt2((variable_propio - var_ref) / var_ref * 100)
        if euros is not None and euros_ref is not None:
            r["diferencia_eur_anual"] = fmt2(euros - euros_ref)

    return registros

# ─────────────────────────────────────────────
# SCRAPING PRINCIPAL DE GAS
# ─────────────────────────────────────────────

async def scrape_all_gas():
    todos = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--window-size=1280,900",
            ]
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="es-ES",
            viewport={"width": 1280, "height": 900},
            extra_http_headers={
                "Accept-Language": "es-ES,es;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            }
        )
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3]});
            Object.defineProperty(navigator, 'languages', {get: () => ['es-ES','es']});
            window.chrome = {runtime: {}};
        """)
        for t in TARIFAS_GAS:
            print(f"  [GAS][{t['comercializadora']}] {t['tarifa']} ...")
            page = await context.new_page()
            try:
                fn = EXTRACTORES_GAS[t["extractor"]]
                reg = await fn(page, t)
                todos.append(reg)
                print(f"    → {reg['notas']} | fijo: {reg.get('termino_fijo_eur_mes') or 'N/D'} "
                      f"€/mes | variable: {reg.get('termino_variable_eur_kwh') or 'N/D'} €/kWh")
            except Exception as e:
                todos.append(registro_vacio_gas(
                    t["comercializadora"], t["tarifa"], t["tipo_precio"], t["url"],
                    f"Error inesperado: {e}"
                ))
            finally:
                await page.close()
        await browser.close()
    return todos

def guardar_json_local_gas(registros):
    fecha = datetime.date.today().isoformat()
    os.makedirs("data", exist_ok=True)
    with open(f"data/gas_{fecha}.json", "w", encoding="utf-8") as f:
        json.dump(registros, f, ensure_ascii=False, indent=2)
    print(f"  ✓ Backup JSON de gas guardado")

def guardar_en_sheets_gas(registros):
    creds_dict = json.loads(CREDENTIALS_JSON)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=[
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive"
        ]
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_ID)
    try:
        ws = sh.worksheet("gas")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="gas", rows="2000", cols="15")
        ws.append_row(CABECERAS_GAS)

    fecha_hoy = datetime.date.today().isoformat()
    ultimo_por_tarifa = _obtener_ultimo_valor_por_tarifa(ws, fecha_hoy)
    cambios = detectar_cambios(registros, ultimo_por_tarifa, CAMPOS_COMPARABLES_GAS, "gas")
    if cambios:
        guardar_cambios_en_sheets(sh, cambios)

    filas = [[str(r.get(c, "") or "") for c in CABECERAS_GAS] for r in registros]
    if filas:
        ws.append_rows(filas, value_input_option="RAW", table_range="A1")
        print(f"  ✓ {len(filas)} filas de gas añadidas a Google Sheets")
    return cambios



# ═════════════════════════════════════════════
# MÓDULO DE DETECCIÓN DE CAMBIOS DE PRECIO
# ═════════════════════════════════════════════
# Compara los precios de hoy con los de la ejecución anterior (misma
# comercializadora+tarifa) y, si algo cambia:
#   1) Añade una fila a la pestaña "Cambios" del Sheet.
#   2) Envía un aviso instantáneo por Telegram.
# ═════════════════════════════════════════════

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

CAMPOS_COMPARABLES_LUZ = [
    "potencia_p1_eur_kw_dia", "potencia_p2_eur_kw_dia",
    "energia_punta_eur_kwh", "energia_llano_eur_kwh",
    "energia_valle_eur_kwh", "energia_unico_eur_kwh",
]
CAMPOS_COMPARABLES_GAS = ["termino_fijo_eur_mes", "termino_variable_eur_kwh"]

NOMBRES_CAMPO = {
    "potencia_p1_eur_kw_dia": "Potencia P1 (€/kW·día)",
    "potencia_p2_eur_kw_dia": "Potencia P2 (€/kW·día)",
    "energia_punta_eur_kwh": "Energía Punta (€/kWh)",
    "energia_llano_eur_kwh": "Energía Llano (€/kWh)",
    "energia_valle_eur_kwh": "Energía Valle (€/kWh)",
    "energia_unico_eur_kwh": "Energía Único (€/kWh)",
    "termino_fijo_eur_mes": "Término Fijo (€/mes)",
    "termino_variable_eur_kwh": "Término Variable (€/kWh)",
}

CABECERAS_CAMBIOS = [
    "fecha", "tipo_energia", "comercializadora", "tarifa",
    "campo", "valor_anterior", "valor_nuevo",
]

def _obtener_ultimo_valor_por_tarifa(ws, fecha_hoy):
    """Lee todas las filas ya existentes en el worksheet (antes de añadir
    las de hoy) y devuelve, para cada (comercializadora, tarifa), el
    diccionario de campos de la fila más reciente ANTERIOR a fecha_hoy."""
    try:
        valores = ws.get_all_values()
    except Exception as e:
        print(f"  ⚠ No se pudo leer el histórico para comparar cambios: {e}")
        return {}
    if len(valores) < 2:
        return {}
    header = valores[0]
    idx = {campo: header.index(campo) for campo in header}
    if "fecha" not in idx or "comercializadora" not in idx or "tarifa" not in idx:
        return {}
    ultimo = {}
    for fila in valores[1:]:
        if len(fila) < len(header):
            fila = fila + [""] * (len(header) - len(fila))
        fecha_fila = fila[idx["fecha"]]
        if not fecha_fila or fecha_fila >= fecha_hoy:
            continue
        clave = (fila[idx["comercializadora"]], fila[idx["tarifa"]])
        if clave not in ultimo or fecha_fila > ultimo[clave]["__fecha__"]:
            registro = {campo: fila[i] for campo, i in idx.items()}
            registro["__fecha__"] = fecha_fila
            ultimo[clave] = registro
    return ultimo

def detectar_cambios(registros_hoy, ultimo_por_tarifa, campos, tipo_energia):
    cambios = []
    for r in registros_hoy:
        clave = (r.get("comercializadora"), r.get("tarifa"))
        anterior = ultimo_por_tarifa.get(clave)
        if not anterior:
            continue  # primera vez que se ve esta tarifa, no hay con qué comparar
        for campo in campos:
            v_nuevo = str(r.get(campo) or "").strip()
            v_viejo = str(anterior.get(campo) or "").strip()
            if v_nuevo and v_viejo and v_nuevo != v_viejo:
                cambios.append({
                    "fecha": r.get("fecha"),
                    "tipo_energia": tipo_energia,
                    "comercializadora": r.get("comercializadora"),
                    "tarifa": r.get("tarifa"),
                    "campo": campo,
                    "valor_anterior": v_viejo,
                    "valor_nuevo": v_nuevo,
                })
    return cambios

def guardar_cambios_en_sheets(sh, cambios):
    if not cambios:
        return
    try:
        ws = sh.worksheet("Cambios")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="Cambios", rows="2000", cols="10")
        ws.append_row(CABECERAS_CAMBIOS)
    filas = [[c["fecha"], c["tipo_energia"], c["comercializadora"], c["tarifa"],
              c["campo"], c["valor_anterior"], c["valor_nuevo"]] for c in cambios]
    ws.append_rows(filas, value_input_option="RAW", table_range="A1")
    print(f"  ✓ {len(filas)} cambio(s) de precio registrados en la pestaña Cambios")

def enviar_telegram(mensaje: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("  (Telegram no configurado — se omite la notificación)")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "HTML"},
            timeout=15,
        )
        if resp.status_code == 200:
            print("  ✓ Notificación de Telegram enviada")
        else:
            print(f"  ⚠ Error enviando Telegram: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        print(f"  ⚠ Excepción enviando Telegram: {e}")

def formatear_mensaje_cambios(cambios):
    if not cambios:
        return None
    lineas = [f"🔔 <b>{len(cambios)} cambio(s) de precio detectado(s)</b> — {datetime.date.today().isoformat()}\n"]
    for c in cambios:
        campo_legible = NOMBRES_CAMPO.get(c["campo"], c["campo"])
        emoji = "⚡" if c["tipo_energia"] == "luz" else "🔥"
        lineas.append(
            f"{emoji} <b>{c['comercializadora']}</b> — {c['tarifa']}\n"
            f"    {campo_legible}: {c['valor_anterior']} → {c['valor_nuevo']}"
        )
    return "\n".join(lineas)



# ═════════════════════════════════════════════
# CORRECCIÓN PUNTUAL — potencia de Octopus del 22 sep 2026
# ═════════════════════════════════════════════
# Un bug en el extractor de Octopus asignó por error el total combinado de
# potencia (0,24 €/kW/día) tanto a P1 como a P2, en vez de repartirlo
# correctamente (0,1216 €/kW/día cada uno, confirmado por Roberto). Como
# Octopus es la referencia de comparación del resto de tarifas, esto
# también descuadró la "diferencia en euros" (columna M) de todas las
# demás filas del 22 sep 2026. Esta función detecta y corrige esas filas
# la primera vez que se ejecuta; si ya está corregido, no encuentra nada
# que hacer y no toca nada.
# ═════════════════════════════════════════════

def reparar_historico_octopus_potencia():
    if not (SPREADSHEET_ID and CREDENTIALS_JSON):
        return
    FECHA_AFECTADA = "2026-09-22"
    POTENCIA_CORRECTA = 0.1216
    try:
        creds_dict = json.loads(CREDENTIALS_JSON)
        creds = Credentials.from_service_account_info(
            creds_dict,
            scopes=["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"],
        )
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(SPREADSHEET_ID)
        ws = sh.worksheet(SHEET_NAME)
        valores = ws.get_all_values()
        if len(valores) < 2:
            return
        header = valores[0]
        idx = {c: i for i, c in enumerate(header)}
        necesarios = ["fecha", "comercializadora", "tarifa", "tipo_precio",
                      "potencia_p1_eur_kw_dia", "potencia_p2_eur_kw_dia",
                      "energia_punta_eur_kwh", "energia_llano_eur_kwh",
                      "energia_valle_eur_kwh", "energia_unico_eur_kwh",
                      "euros al año", "variacion euros anual"]
        if not all(c in idx for c in necesarios):
            print("  ⚠ Reparación histórica: no se encontraron todas las columnas esperadas, se omite")
            return

        filas_octopus_malas = []
        for i, fila in enumerate(valores[1:], start=2):
            if len(fila) <= idx["comercializadora"]:
                continue
            if fila[idx["fecha"]] != FECHA_AFECTADA or fila[idx["comercializadora"]] != "Octopus Energy":
                continue
            p1_txt = fila[idx["potencia_p1_eur_kw_dia"]] if len(fila) > idx["potencia_p1_eur_kw_dia"] else ""
            p1 = _num(p1_txt)
            if p1 is not None and abs(p1 - 0.24) < 0.001:
                filas_octopus_malas.append(i)

        if not filas_octopus_malas:
            return  # ya corregido (o nunca llegó a fallar) — no hay nada que hacer

        print(f"  ⚠ Reparación histórica: corrigiendo {len(filas_octopus_malas)} fila(s) de Octopus "
              f"del {FECHA_AFECTADA} (potencia 0,24 → {POTENCIA_CORRECTA} €/kW/día)")

        euros_octopus_corregidos = {}
        for num_fila in filas_octopus_malas:
            fila = valores[num_fila - 1]
            tarifa = fila[idx["tarifa"]]
            reg_tmp = {
                "tipo_precio": fila[idx["tipo_precio"]],
                "potencia_p1_eur_kw_dia": str(POTENCIA_CORRECTA),
                "potencia_p2_eur_kw_dia": str(POTENCIA_CORRECTA),
                "energia_punta_eur_kwh": fila[idx["energia_punta_eur_kwh"]],
                "energia_llano_eur_kwh": fila[idx["energia_llano_eur_kwh"]],
                "energia_valle_eur_kwh": fila[idx["energia_valle_eur_kwh"]],
                "energia_unico_eur_kwh": fila[idx["energia_unico_eur_kwh"]],
            }
            nuevo_euros, _ = calcular_euros_al_anio(reg_tmp)
            euros_octopus_corregidos[tarifa] = nuevo_euros

            ws.update_cell(num_fila, idx["potencia_p1_eur_kw_dia"] + 1, str(POTENCIA_CORRECTA))
            ws.update_cell(num_fila, idx["potencia_p2_eur_kw_dia"] + 1, str(POTENCIA_CORRECTA))
            if nuevo_euros is not None:
                ws.update_cell(num_fila, idx["euros al año"] + 1, fmt2(nuevo_euros))
            print(f"    ✓ {tarifa}: potencia → {POTENCIA_CORRECTA} €/kW/día, euros/año → {nuevo_euros}")

        # La "diferencia en euros" del resto de tarifas de ese mismo día se
        # calculó contra el euros/año erróneo de Octopus — se recalcula.
        col_diff = idx["variacion euros anual"] + 1
        actualizados = 0
        for i, fila in enumerate(valores[1:], start=2):
            if fila[idx["fecha"]] != FECHA_AFECTADA or fila[idx["comercializadora"]] == "Octopus Energy":
                continue
            tarifa_ref = "Octopus 3" if fila[idx["tipo_precio"]] == "punta_llano_valle" else "Octopus Relax"
            euros_ref = euros_octopus_corregidos.get(tarifa_ref)
            euros_propio = _num(fila[idx["euros al año"]]) if len(fila) > idx["euros al año"] else None
            if euros_ref is None or euros_propio is None:
                continue
            ws.update_cell(i, col_diff, fmt2(euros_propio - euros_ref))
            actualizados += 1
        print(f"  ✓ Reparación histórica: recalculada la diferencia en euros de {actualizados} tarifa(s) más")
    except Exception as e:
        print(f"  ⚠ No se pudo aplicar la reparación histórica puntual: {e}")



# ═════════════════════════════════════════════
# MÓDULO DASHBOARD (generación estática para Alexandria)
# ═════════════════════════════════════════════
# Genera el dashboard.html (con los datos históricos completos ya
# incrustados) cada vez que corre el scraper, para poder publicarlo en
# Alexandria automáticamente sin que haga falta regenerarlo a mano.

DASHBOARD_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Scraper de Precios Competencia</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@500;600;700;800&family=JetBrains+Mono:wght@400;500;600;700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<style>
/* Paleta de marca Octopus Energy España (skill octopus-ui / design-tokens) */
:root {
  --siphon: #0d0030;
  --hemocyanin: #18004a;
  --blueberry: #2d1a83;
  --ink: #5840ff;
  --blue-tang: #6675f6;
  --voltage: #06f0fb;
  --ice: #fcffff;
  --dusty-sky: #dcddff;
  --purple-haze: #a49fc6;
  --hot-pink: #ff039f;
  --hot-pink-alt: #ff0276;
  --sparkline-teal: #00c6f3;

  --bg: var(--siphon);
  --surface: var(--hemocyanin);
  --surface-2: var(--blueberry);
  --border: rgba(88, 64, 255, 0.4);
  --border-soft: rgba(164, 159, 198, 0.18);
  --text: var(--ice);
  --text-muted: var(--dusty-sky);
  --text-faint: var(--purple-haze);

  --pink: var(--hot-pink);
  --pink-soft: rgba(255, 3, 159, 0.14);
  --teal: var(--sparkline-teal);
  --teal-soft: rgba(0, 198, 243, 0.14);
  --coral: var(--hot-pink-alt);
  --coral-soft: rgba(255, 2, 118, 0.14);

  --radius-lg: 18px;
  --radius-md: 12px;
  --radius-sm: 8px;
  --mono: 'JetBrains Mono', ui-monospace, monospace;
  --head: 'Montserrat', ui-sans-serif, sans-serif;
  --body: 'Inter', ui-sans-serif, sans-serif;
}
* { box-sizing: border-box; }
html { scroll-padding-top: env(safe-area-inset-top, 0px); }
html, body {
  height: 100%;
  margin: 0;
  background: var(--bg);
  background-image:
    radial-gradient(ellipse 900px 500px at 15% -10%, rgba(255,3,159,0.10), transparent 60%),
    radial-gradient(ellipse 700px 500px at 100% 0%, rgba(0,198,243,0.07), transparent 55%);
  background-attachment: fixed;
  color: var(--text);
  font-family: var(--body);
  -webkit-font-smoothing: antialiased;
  padding-top: env(safe-area-inset-top, 0px);
  padding-bottom: env(safe-area-inset-bottom, 0px);
}
body { min-height: 100%; }
a { color: inherit; }
::selection { background: var(--pink-soft); }

.wrap { max-width: 1180px; margin: 0 auto; padding: 28px 20px 80px; }

/* ---------- Header ---------- */
.topbar {
  position: sticky; top: env(safe-area-inset-top, 0px); z-index: 40;
  background: rgba(13, 0, 48, 0.82);
  backdrop-filter: blur(14px); -webkit-backdrop-filter: blur(14px);
  border-bottom: 1px solid var(--border-soft);
}
.topbar-inner {
  max-width: 1180px; margin: 0 auto; padding: 16px 20px;
  display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap;
}
.brand { display: flex; align-items: baseline; gap: 10px; }
.brand-mark { font-family: var(--head); font-weight: 700; font-size: 17px; letter-spacing: -0.01em; }
.brand-mark span { color: var(--pink); }
.brand-sub { font-size: 12.5px; color: var(--text-muted); font-family: var(--mono); }
.energy-toggle {
  display: inline-flex; background: var(--surface); border: 1px solid var(--border);
  border-radius: 999px; padding: 3px; gap: 2px;
}
.energy-toggle button {
  border: none; background: transparent; color: var(--text-muted);
  font-family: var(--head); font-weight: 600; font-size: 14px;
  padding: 8px 20px; border-radius: 999px; cursor: pointer; transition: color .15s ease;
}
.energy-toggle button.active { background: var(--pink); color: #fff; }
.energy-toggle button:not(.active):hover { color: var(--text); }

/* ---------- Layout sections ---------- */
section { margin-top: 34px; }
.section-head {
  display: flex; align-items: baseline; justify-content: space-between; gap: 12px;
  margin-bottom: 14px; flex-wrap: wrap;
}
.section-title { font-family: var(--head); font-weight: 700; font-size: 20px; letter-spacing: -0.01em; }
.section-note { font-size: 12.5px; color: var(--text-faint); font-family: var(--mono); }
.card {
  background: linear-gradient(180deg, var(--surface), var(--bg));
  border: 1px solid var(--border-soft);
  border-radius: var(--radius-lg);
  padding: 22px;
}

/* ---------- Sub-toggle (RL1/RL2 en Gas) ---------- */
.subtoggle {
  display: inline-flex; background: var(--bg); border: 1px solid var(--border-soft);
  border-radius: 999px; padding: 3px; gap: 2px;
}
.subtoggle button {
  border: none; background: transparent; color: var(--text-muted);
  font-family: var(--mono); font-weight: 600; font-size: 12px;
  padding: 6px 14px; border-radius: 999px; cursor: pointer;
}
.subtoggle button.active { background: var(--surface-2); color: var(--text); }

/* ---------- Ranking (hero) ---------- */
.rank-row {
  display: grid; grid-template-columns: 168px 1fr 112px; align-items: center; gap: 14px;
  padding: 9px 0; border-bottom: 1px solid var(--border-soft);
}
.rank-row:last-child { border-bottom: none; }
.rank-name { font-size: 13.5px; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rank-tar {
  display: block; font-size: 11px; color: var(--text-faint); font-family: var(--mono);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.rank-track { position: relative; height: 22px; background: var(--border-soft); border-radius: 6px; overflow: hidden; }
.rank-mid { position: absolute; left: 50%; top: 0; bottom: 0; width: 1px; background: var(--text-faint); opacity: .5; }
.rank-fill { position: absolute; top: 2px; bottom: 2px; border-radius: 5px; }
.rank-fill.up { background: linear-gradient(90deg, var(--coral), #ff5fa8); left: 50%; }
.rank-fill.down { background: linear-gradient(90deg, #00a8d6, var(--teal)); right: 50%; }
.rank-pct { font-family: var(--mono); text-align: right; line-height: 1.25; }
.rank-pct.up .rank-eur { color: var(--coral); }
.rank-pct.down .rank-eur { color: var(--teal); }
.rank-eur { font-size: 14px; font-weight: 700; font-variant-numeric: tabular-nums; }
.rank-pctnum { font-size: 11px; color: var(--text-faint); margin-top: 1px; font-variant-numeric: tabular-nums; }
.rank-legend { display: flex; gap: 18px; margin-top: 16px; font-size: 12px; color: var(--text-muted); font-family: var(--mono); }
.rank-legend span { display: inline-flex; align-items: center; gap: 6px; }
.dot { width: 8px; height: 8px; border-radius: 50%; }

/* ---------- Evolución (vs Octopus) ---------- */
.evo-controls { display: flex; align-items: center; justify-content: space-between; gap: 18px; margin-bottom: 20px; flex-wrap: wrap; }
.evo-select {
  background: var(--surface-2); color: var(--text); border: 1px solid var(--border-soft);
  border-radius: 10px; padding: 11px 14px; font-family: var(--body); font-size: 13.5px;
  min-width: 280px; cursor: pointer; max-width: 100%;
}
.evo-select:focus-visible { outline: 2px solid var(--voltage); outline-offset: 2px; }
.evo-stat { text-align: right; font-family: var(--mono); }
.evo-stat-big { font-size: 22px; font-weight: 700; font-variant-numeric: tabular-nums; }
.evo-stat-big.up { color: var(--coral); }
.evo-stat-big.down { color: var(--teal); }
.evo-stat-sub { font-size: 12px; color: var(--text-muted); margin-top: 2px; }
.evo-legend { display: flex; gap: 20px; margin-top: 14px; font-size: 12px; color: var(--text-muted); font-family: var(--mono); }
.evo-legend span { display: inline-flex; align-items: center; gap: 6px; }
.evo-swatch { width: 16px; height: 3px; border-radius: 2px; }
.chart-canvas-wrap { position: relative; height: 340px; }

/* ---------- Table ---------- */
.table-scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 10px 12px; text-align: right; white-space: nowrap; border-bottom: 1px solid var(--border-soft); }
th:nth-child(1), td:nth-child(1), th:nth-child(2), td:nth-child(2) { text-align: left; }
th {
  font-family: var(--head); font-weight: 600; font-size: 11.5px; color: var(--text-muted);
  cursor: pointer; user-select: none; position: sticky; top: 0; background: var(--surface);
}
th:hover { color: var(--text); }
th .arrow { opacity: .5; margin-left: 4px; font-size: 10px; }
td { font-family: var(--mono); font-variant-numeric: tabular-nums; }
td.name-cell { font-family: var(--body); font-weight: 500; }
tr.octopus-row td { color: var(--pink); font-weight: 600; }
tr.octopus-row { background: var(--pink-soft); }
.pill { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11px; font-family: var(--mono); }
.pill.up { background: var(--coral-soft); color: var(--coral); }
.pill.down { background: var(--teal-soft); color: var(--teal); }
.muted-cell { color: var(--text-faint); }

/* ---------- Cambios feed ---------- */
.feed-item { padding: 12px 0; border-bottom: 1px solid var(--border-soft); }
.feed-item:last-child { border-bottom: none; }
.feed-top { display: flex; justify-content: space-between; gap: 8px; align-items: baseline; }
.feed-com { font-weight: 600; font-size: 13.5px; }
.feed-date { font-size: 11px; color: var(--text-faint); font-family: var(--mono); white-space: nowrap; }
.feed-tar { font-size: 12px; color: var(--text-muted); margin-top: 1px; }
.feed-change { margin-top: 6px; font-family: var(--mono); font-size: 12.5px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.feed-arrow { color: var(--text-faint); }
.feed-new { font-weight: 600; }
.feed-new.up { color: var(--coral); }
.feed-new.down { color: var(--teal); }
.feed-empty { color: var(--text-faint); font-size: 13px; padding: 8px 0; }

.two-col { display: grid; grid-template-columns: 1.55fr 1fr; gap: 20px; align-items: start; }

footer {
  margin-top: 48px; padding-top: 20px; border-top: 1px solid var(--border-soft);
  color: var(--text-faint); font-size: 11.5px; font-family: var(--mono);
  display: flex; justify-content: space-between; flex-wrap: wrap; gap: 8px;
}

@media (max-width: 860px) {
  .two-col { grid-template-columns: 1fr; }
  .rank-row { grid-template-columns: 110px 1fr 90px; }
}
</style>
</head>
<body>

<div class="topbar">
  <div class="topbar-inner">
    <div class="brand">
      <span class="brand-mark">scraper de precios <span>competencia</span></span>
      <span class="brand-sub" id="lastUpdated">—</span>
    </div>
    <div class="energy-toggle">
      <button id="btnLuz" class="active">⚡ Luz</button>
      <button id="btnGas">🔥 Gas</button>
    </div>
  </div>
</div>

<div class="wrap">

  <section id="secRanking">
    <div class="section-head">
      <div>
        <div class="section-title">Comparativa de precios con Octopus</div>
      </div>
      <div style="display:flex; align-items:center; gap:14px;">
        <div class="subtoggle" id="rlToggle" style="display:none;">
          <button data-rl="RL1" class="active">RL1</button>
          <button data-rl="RL2">RL2</button>
        </div>
        <div class="section-note" id="rankingNote"></div>
      </div>
    </div>
    <div class="card">
      <div id="rankingBody"></div>
      <div class="rank-legend">
        <span><span class="dot" style="background:var(--teal)"></span>Octopus es más barato</span>
        <span><span class="dot" style="background:var(--coral)"></span>El competidor es más barato</span>
      </div>
    </div>
  </section>

  <section id="secEvolucion">
    <div class="section-head">
      <div class="section-title">Evolución frente a Octopus</div>
      <div class="section-note">€/año estimado, por fecha de captura</div>
    </div>
    <div class="card">
      <div class="evo-controls">
        <select class="evo-select" id="evoSelect"></select>
        <div class="evo-stat" id="evoStat"></div>
      </div>
      <div class="chart-canvas-wrap"><canvas id="evoChart"></canvas></div>
      <div class="evo-legend">
        <span><span class="evo-swatch" style="background:var(--pink)"></span><span id="evoLegendSel">Tarifa seleccionada</span></span>
        <span><span class="evo-swatch" style="background:var(--teal); opacity:.8"></span>Octopus (referencia)</span>
      </div>
    </div>
  </section>

  <section class="two-col">
    <div id="secTabla">
      <div class="section-head">
        <div class="section-title">Tarifas de hoy</div>
        <div class="section-note" id="tablaNote"></div>
      </div>
      <div class="card table-scroll">
        <table id="dataTable">
          <thead><tr id="tableHead"></tr></thead>
          <tbody id="tableBody"></tbody>
        </table>
      </div>
    </div>

    <div id="secCambios">
      <div class="section-head"><div class="section-title">Cambios recientes</div></div>
      <div class="card" id="cambiosBody"></div>
    </div>
  </section>

  <footer>
    <span>Datos internos OEES · scraper diario 2.0TD</span>
    <span id="footRange"></span>
  </footer>
</div>

<script>
const LUZ = __LUZ_JSON__;
const GAS = __GAS_JSON__;
const CAMBIOS = __CAMBIOS_JSON__;
</script>
<script>
__APP_JS__
</script>
</body>
</html>
"""

DASHBOARD_APP_JS = """/* ===== Utilidades ===== */
function num(v){
  if(v===null||v===undefined||v==='') return null;
  const n = typeof v==='number'? v : parseFloat(String(v).replace(',','.'));
  return isNaN(n) ? null : n;
}
function ultimaFecha(data){
  return data.reduce((max,r)=> (r.fecha > max ? r.fecha : max), data[0]?data[0].fecha:'');
}
function claveTarifa(r){ return r.com + ' · ' + r.tar; }
function deduplicar(filas){
  const mapa = new Map();
  filas.forEach(r => mapa.set(r.fecha+'|'+r.com+'|'+r.tar, r));
  return [...mapa.values()];
}
function datosDeHoy(data, fecha){
  return deduplicar(data.filter(r=>r.fecha===fecha));
}
function fechaConRanking(data, filtroTipo){
  const fechas = [...new Set(data.map(r=>r.fecha))].sort().reverse();
  for(const f of fechas){
    const filas = datosDeHoy(data, f).filter(r=> !filtroTipo || r.tipo===filtroTipo);
    if(filas.some(r=> num(r.diff_eur)!==null)) return f;
  }
  return fechas[0] || '';
}

const state = { modo: 'luz', rlFiltro: 'RL1', evoSeleccion: null };

/* ===== Ranking ===== */
function renderRanking(){
  const data = state.modo==='luz'? LUZ : GAS;
  const esGas = state.modo==='gas';
  document.getElementById('rlToggle').style.display = esGas ? 'inline-flex' : 'none';

  const filtroTipo = esGas ? state.rlFiltro : null;
  const fecha = fechaConRanking(data, filtroTipo);
  const fechaTabla = ultimaFecha(data);
  document.getElementById('rankingNote').textContent = fecha !== fechaTabla
    ? fecha + ' (última con comparación disponible)'
    : fecha;

  let hoy = datosDeHoy(data, fecha);
  if(filtroTipo) hoy = hoy.filter(r=> r.tipo === filtroTipo);

  const filas = hoy
    .filter(r => r.com !== 'Octopus Energy' && num(r.diff_eur) !== null)
    .map(r => ({...r, vp: num(r.var_pct), de: num(r.diff_eur)}))
    .sort((a,b)=> a.de - b.de);

  const maxAbs = Math.max(50, ...filas.map(f=>Math.abs(f.de)));
  const cont = document.getElementById('rankingBody');
  cont.innerHTML = '';
  filas.forEach(f=>{
    const esNeg = f.de < 0;
    const widthPct = Math.min(50, Math.abs(f.de)/maxAbs*50);
    const eurTxt = (f.de>0?'+':'') + Math.round(f.de).toLocaleString('es-ES') + ' €';
    const pctTxt = f.vp===null ? '' : (f.vp>0?'+':'')+f.vp.toFixed(1)+'% vs Octopus';
    const row = document.createElement('div');
    row.className = 'rank-row';
    row.innerHTML =
      '<div><div class="rank-name">'+f.com+'</div><span class="rank-tar">'+f.tar+'</span></div>'+
      '<div class="rank-track"><div class="rank-mid"></div>'+
      '<div class="rank-fill '+(esNeg?'up':'down')+'" style="'+(esNeg?'right':'left')+':50%; width:'+widthPct+'%"></div></div>'+
      '<div class="rank-pct '+(esNeg?'up':'down')+'">'+
        '<div class="rank-eur">'+eurTxt+'</div>'+
        '<div class="rank-pctnum">'+pctTxt+'</div>'+
      '</div>';
    cont.appendChild(row);
  });
  if(!filas.length){ cont.innerHTML = '<div class="feed-empty">Sin datos comparables para '+(filtroTipo||'hoy')+'.</div>'; }
}

/* ===== Tabla ===== */
const LUZ_COLS = [
  {key:'com', label:'Comercializadora', type:'text'},
  {key:'tar', label:'Tarifa', type:'text'},
  {key:'tipo', label:'Tipo', type:'text'},
  {key:'p1', label:'Pot. P1', type:'num', dec:4},
  {key:'p2', label:'Pot. P2', type:'num', dec:4},
  {key:'punta', label:'Punta', type:'num', dec:4},
  {key:'llano', label:'Llano', type:'num', dec:4},
  {key:'valle', label:'Valle', type:'num', dec:4},
  {key:'unico', label:'Único', type:'num', dec:4},
  {key:'var_pct', label:'Var. %', type:'pct'},
  {key:'euros_anio', label:'€/año', type:'euro'},
];
const GAS_COLS = [
  {key:'com', label:'Comercializadora', type:'text'},
  {key:'tar', label:'Tarifa', type:'text'},
  {key:'tipo', label:'Tipo', type:'text'},
  {key:'fijo', label:'Fijo €/mes', type:'num', dec:2},
  {key:'variable', label:'Variable €/kWh', type:'num', dec:4},
  {key:'var_pct', label:'Var. %', type:'pct'},
  {key:'euros_anio', label:'€/año', type:'euro'},
];
const NUM_KEYS = ['p1','p2','punta','llano','valle','unico','fijo','variable','euros_anio','var_pct'];
let sortState = {col:'com', dir:1};

function renderTable(){
  const data = state.modo==='luz'? LUZ : GAS;
  const cols = state.modo==='luz'? LUZ_COLS : GAS_COLS;
  const fecha = ultimaFecha(data);
  document.getElementById('tablaNote').textContent = fecha;
  let filas = datosDeHoy(data, fecha).slice();

  filas.sort((a,b)=>{
    const col = sortState.col;
    let va, vb;
    if(NUM_KEYS.includes(col)){ va = num(a[col]); vb = num(b[col]); va = va===null? -Infinity: va; vb = vb===null? -Infinity: vb; }
    else { va = a[col]||''; vb = b[col]||''; }
    if(va<vb) return -1*sortState.dir;
    if(va>vb) return 1*sortState.dir;
    return 0;
  });

  const thead = document.getElementById('tableHead');
  thead.innerHTML = cols.map(c=>'<th data-col="'+c.key+'">'+c.label+(sortState.col===c.key? '<span class="arrow">'+(sortState.dir===1?'▲':'▼')+'</span>':'')+'</th>').join('');
  thead.querySelectorAll('th').forEach(th=>{
    th.onclick = ()=>{
      const col = th.dataset.col;
      if(sortState.col===col) sortState.dir *= -1; else { sortState.col=col; sortState.dir=1; }
      renderTable();
    };
  });

  const tbody = document.getElementById('tableBody');
  tbody.innerHTML = filas.map(f=>{
    const esOctopus = f.com==='Octopus Energy';
    const cells = cols.map(c=>{
      const v = f[c.key];
      if(c.type==='text') return '<td class="'+(c.key==='com'?'name-cell':'')+'">'+(v||'—')+'</td>';
      if(c.type==='pct'){
        const n = num(v);
        if(n===null) return '<td class="muted-cell">—</td>';
        const cls = n<0? 'up':'down';
        return '<td><span class="pill '+cls+'">'+(n>0?'+':'')+n.toFixed(1)+'%</span></td>';
      }
      if(c.type==='euro'){
        const n = num(v);
        return '<td>'+(n===null? '—' : Math.round(n).toLocaleString('es-ES')+' €')+'</td>';
      }
      const n = num(v);
      return '<td>'+(n===null? '<span class="muted-cell">—</span>' : n.toFixed(c.dec||4))+'</td>';
    }).join('');
    return '<tr class="'+(esOctopus?'octopus-row':'')+'">'+cells+'</tr>';
  }).join('');
}

/* ===== Cambios recientes ===== */
const CAMPO_LABELS = {
  potencia_p1_eur_kw_dia: 'Potencia P1', potencia_p2_eur_kw_dia: 'Potencia P2',
  energia_punta_eur_kwh: 'Energía Punta', energia_llano_eur_kwh: 'Energía Llano',
  energia_valle_eur_kwh: 'Energía Valle', energia_unico_eur_kwh: 'Energía Único',
  termino_fijo_eur_mes: 'Término Fijo', termino_variable_eur_kwh: 'Término Variable',
};
function renderCambios(){
  const filas = CAMBIOS.filter(c=>c.tipo===state.modo).sort((a,b)=> b.fecha.localeCompare(a.fecha)).slice(0,15);
  const cont = document.getElementById('cambiosBody');
  if(!filas.length){ cont.innerHTML = '<div class="feed-empty">Sin cambios registrados todavía.</div>'; return; }
  cont.innerHTML = filas.map(f=>{
    const antes = num(f.antes), despues = num(f.despues);
    const subio = (despues!==null && antes!==null && despues>antes);
    return '<div class="feed-item">'+
      '<div class="feed-top"><span class="feed-com">'+f.com+'</span><span class="feed-date">'+f.fecha+'</span></div>'+
      '<div class="feed-tar">'+f.tar+' · '+(CAMPO_LABELS[f.campo]||f.campo)+'</div>'+
      '<div class="feed-change"><span>'+f.antes+'</span><span class="feed-arrow">→</span><span class="feed-new '+(subio?'up':'down')+'">'+f.despues+'</span></div>'+
    '</div>';
  }).join('');
}

/* ===== Evolución frente a Octopus (una tarifa a la vez) ===== */
function referenciaPara(r){
  if(state.modo==='luz'){
    return r.tipo === 'punta_llano_valle' ? 'Octopus Energy · Octopus 3' : 'Octopus Energy · Octopus Relax';
  }
  return r.tipo === 'RL2' ? 'Octopus Energy · Octopus Gas RL2' : 'Octopus Energy · Octopus Gas RL1';
}

function construirSelectorEvo(){
  const data = deduplicar(state.modo==='luz'? LUZ : GAS);
  const combos = [...new Map(data.map(r=>[claveTarifa(r), r])).values()]
    .filter(r => r.com !== 'Octopus Energy')
    .sort((a,b)=> a.com.localeCompare(b.com) || a.tar.localeCompare(b.tar));

  const sel = document.getElementById('evoSelect');
  sel.innerHTML = combos.map(r=> '<option value="'+claveTarifa(r).replace(/"/g,'&quot;')+'">'+r.com+' — '+r.tar+'</option>').join('');

  if(!state.evoSeleccion || !combos.some(r=>claveTarifa(r)===state.evoSeleccion)){
    state.evoSeleccion = combos.length ? claveTarifa(combos[0]) : null;
  }
  sel.value = state.evoSeleccion;
  sel.onchange = ()=>{ state.evoSeleccion = sel.value; renderEvoChart(); };
}

let evoChartInstance = null;
function renderEvoChart(){
  const data = deduplicar(state.modo==='luz'? LUZ : GAS);
  const fechas = [...new Set(data.map(r=>r.fecha))].sort();
  const clave = state.evoSeleccion;
  const statEl = document.getElementById('evoStat');
  const legendEl = document.getElementById('evoLegendSel');

  if(!clave){
    statEl.innerHTML = '';
    if(evoChartInstance){ evoChartInstance.destroy(); evoChartInstance = null; }
    return;
  }
  legendEl.textContent = clave.split(' · ')[1] || clave;

  const serieSel = [], serieRef = [];
  let ultimoDiff = null, ultimoPct = null, ultimaFechaConDatos = null;

  fechas.forEach(f=>{
    const fila = data.find(r=> claveTarifa(r)===clave && r.fecha===f);
    const valorSel = fila ? num(fila.euros_anio) : null;
    let valorRef = null;
    if(fila){
      const filaRef = data.find(r=> claveTarifa(r)===referenciaPara(fila) && r.fecha===f);
      valorRef = filaRef ? num(filaRef.euros_anio) : null;
    }
    serieSel.push(valorSel);
    serieRef.push(valorRef);
    if(valorSel!==null && valorRef!==null){
      ultimoDiff = valorSel - valorRef;
      ultimoPct = fila ? num(fila.var_pct) : null;
      ultimaFechaConDatos = f;
    }
  });

  if(ultimoDiff===null){
    statEl.innerHTML = '<div class="evo-stat-sub">Sin datos comparables todavía</div>';
  } else {
    const esNeg = ultimoDiff < 0;
    statEl.innerHTML =
      '<div class="evo-stat-big '+(esNeg?'up':'down')+'">'+(ultimoDiff>0?'+':'')+Math.round(ultimoDiff).toLocaleString('es-ES')+' €/año</div>'+
      '<div class="evo-stat-sub">'+(ultimoPct!==null? (ultimoPct>0?'+':'')+ultimoPct.toFixed(1)+'% vs Octopus · ':'')+ultimaFechaConDatos+'</div>';
  }

  if(evoChartInstance) evoChartInstance.destroy();
  const ctx = document.getElementById('evoChart').getContext('2d');
  evoChartInstance = new Chart(ctx, {
    type: 'line',
    data: {
      labels: fechas,
      datasets: [
        {
          label: clave.split(' · ')[0]+' · '+clave.split(' · ')[1],
          data: serieSel, borderColor: '#ff039f', backgroundColor: 'rgba(255,3,159,0.16)',
          fill: '+1', spanGaps: true, tension: 0.25, pointRadius: 2, borderWidth: 2.5,
        },
        {
          label: 'Octopus (referencia)',
          data: serieRef, borderColor: '#00c6f3', backgroundColor: 'transparent',
          borderDash: [5,4], fill: false, spanGaps: true, tension: 0.25, pointRadius: 2, borderWidth: 2,
        },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: { backgroundColor: '#18004a', borderColor:'rgba(164,159,198,0.3)', borderWidth:1 },
      },
      scales: {
        x: { ticks:{color:'#a49fc6', maxRotation:0, autoSkip:true, maxTicksLimit:9, font:{family:"'JetBrains Mono'", size:10}}, grid:{color:'rgba(164,159,198,0.12)'} },
        y: { ticks:{color:'#a49fc6', font:{family:"'JetBrains Mono'", size:10}, callback:(v)=>v+' €'}, grid:{color:'rgba(164,159,198,0.12)'} }
      }
    }
  });
}

/* ===== Orquestación ===== */
function actualizarFooter(){
  const data = state.modo==='luz'? LUZ: GAS;
  const fechas = [...new Set(data.map(r=>r.fecha))].sort();
  document.getElementById('lastUpdated').textContent = 'última captura: ' + fechas[fechas.length-1];
  document.getElementById('footRange').textContent = fechas[0] + ' → ' + fechas[fechas.length-1] + ' · ' + fechas.length + ' días';
}
function actualizarTodo(){
  renderRanking();
  renderTable();
  renderCambios();
  construirSelectorEvo();
  renderEvoChart();
  actualizarFooter();
}

document.getElementById('btnLuz').addEventListener('click', ()=>{
  state.modo='luz';
  document.getElementById('btnLuz').classList.add('active');
  document.getElementById('btnGas').classList.remove('active');
  sortState = {col:'com', dir:1};
  state.evoSeleccion = null;
  actualizarTodo();
});
document.getElementById('btnGas').addEventListener('click', ()=>{
  state.modo='gas';
  document.getElementById('btnGas').classList.add('active');
  document.getElementById('btnLuz').classList.remove('active');
  sortState = {col:'com', dir:1};
  state.evoSeleccion = null;
  actualizarTodo();
});
document.querySelectorAll('#rlToggle button').forEach(btn=>{
  btn.addEventListener('click', ()=>{
    state.rlFiltro = btn.dataset.rl;
    document.querySelectorAll('#rlToggle button').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
    renderRanking();
  });
});

actualizarTodo();
"""

CAMPOS_LUZ_DASHBOARD = {
    "comercializadora": "com", "tarifa": "tar", "tipo_precio": "tipo",
    "potencia_p1_eur_kw_dia": "p1", "potencia_p2_eur_kw_dia": "p2",
    "energia_punta_eur_kwh": "punta", "energia_llano_eur_kwh": "llano",
    "energia_valle_eur_kwh": "valle", "energia_unico_eur_kwh": "unico",
    "Variacion en porcentaje": "var_pct", "euros al año": "euros_anio",
    "variacion euros anual": "diff_eur", "notas": "notas",
}
CAMPOS_GAS_DASHBOARD = {
    "comercializadora": "com", "tarifa": "tar", "tipo_precio": "tipo",
    "termino fijo": "fijo", "termino variable": "variable",
    "Variacion en porcentaje": "var_pct", "euros al año": "euros_anio",
    "variacion euros anual": "diff_eur", "notas": "notas",
}
CAMPOS_CAMBIOS_DASHBOARD = {
    "tipo_energia": "tipo", "comercializadora": "com", "tarifa": "tar",
    "campo": "campo", "valor_anterior": "antes", "valor_nuevo": "despues",
}

def _leer_hoja_para_dashboard(sh, nombre_hoja, mapeo_campos):
    """Lee una pestaña completa del Sheet y la convierte a la lista de
    diccionarios (con los nombres de campo cortos que usa el dashboard)."""
    try:
        ws = sh.worksheet(nombre_hoja)
    except gspread.WorksheetNotFound:
        return []
    valores = ws.get_all_values()
    if len(valores) < 2:
        return []
    header = valores[0]
    idx = {h: i for i, h in enumerate(header)}
    registros = []
    for fila in valores[1:]:
        if not any(fila):
            continue
        if len(fila) < len(header):
            fila = fila + [""] * (len(header) - len(fila))
        fecha = fila[idx.get("fecha", 0)] if "fecha" in idx else ""
        if not fecha:
            continue
        d = {"fecha": fecha}
        for campo_origen, campo_destino in mapeo_campos.items():
            if campo_origen in idx:
                d[campo_destino] = fila[idx[campo_origen]] or None
        registros.append(d)
    return registros

def generar_dashboard_html():
    """Lee el histórico completo de luz/gas/cambios del Sheet y genera el
    dashboard.html final (con los datos ya incrustados), listo para
    publicar en Alexandria. Devuelve la ruta del archivo generado, o None
    si no se pudo generar (p.ej. faltan credenciales)."""
    if not (SPREADSHEET_ID and CREDENTIALS_JSON):
        print("  (Dashboard: faltan credenciales/ID de Sheet, se omite)")
        return None
    try:
        creds_dict = json.loads(CREDENTIALS_JSON)
        creds = Credentials.from_service_account_info(
            creds_dict,
            scopes=["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"],
        )
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(SPREADSHEET_ID)

        luz = _leer_hoja_para_dashboard(sh, SHEET_NAME, CAMPOS_LUZ_DASHBOARD)
        gas = _leer_hoja_para_dashboard(sh, "gas", CAMPOS_GAS_DASHBOARD)
        cambios = _leer_hoja_para_dashboard(sh, "Cambios", CAMPOS_CAMBIOS_DASHBOARD)

        html = DASHBOARD_HTML_TEMPLATE
        html = html.replace("__LUZ_JSON__", json.dumps(luz, ensure_ascii=False))
        html = html.replace("__GAS_JSON__", json.dumps(gas, ensure_ascii=False))
        html = html.replace("__CAMBIOS_JSON__", json.dumps(cambios, ensure_ascii=False))
        html = html.replace("__APP_JS__", DASHBOARD_APP_JS)

        os.makedirs("dashboard_dist", exist_ok=True)
        ruta = os.path.join("dashboard_dist", "index.html")
        with open(ruta, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"  ✓ Dashboard generado en {ruta} ({len(luz)} filas luz, {len(gas)} filas gas, {len(cambios)} cambios)")
        return ruta
    except Exception as e:
        print(f"  ⚠ No se pudo generar el dashboard: {e}")
        return None


async def main():
    print(f"\n{'='*55}")
    print(f"  Scraper Precios 2.0TD — {datetime.date.today()}")
    print(f"{'='*55}\n")
    reparar_historico_octopus_potencia()
    registros = await scrape_all()
    registros = calcular_columnas_comparativas(registros)
    ok = sum(1 for r in registros if r["notas"] == "OK")
    print(f"\nResultado: {ok}/{len(registros)} tarifas OK")
    guardar_json_local(registros)
    cambios_luz = []
    if SPREADSHEET_ID and CREDENTIALS_JSON:
        cambios_luz = guardar_en_sheets(registros) or []

    print(f"\n{'='*55}")
    print(f"  Scraper Gas RL1/RL2 — {datetime.date.today()}")
    print(f"{'='*55}\n")
    registros_gas = await scrape_all_gas()
    registros_gas = calcular_columnas_comparativas_gas(registros_gas)
    ok_gas = sum(1 for r in registros_gas if r["notas"] == "OK")
    print(f"\nResultado gas: {ok_gas}/{len(registros_gas)} tarifas OK")
    guardar_json_local_gas(registros_gas)
    cambios_gas = []
    if SPREADSHEET_ID and CREDENTIALS_JSON:
        cambios_gas = guardar_en_sheets_gas(registros_gas) or []

    todos_los_cambios = cambios_luz + cambios_gas
    if todos_los_cambios:
        print(f"\n🔔 {len(todos_los_cambios)} cambio(s) de precio detectado(s) respecto a la ejecución anterior")
        enviar_telegram(formatear_mensaje_cambios(todos_los_cambios))
    else:
        print("\nSin cambios de precio respecto a la ejecución anterior.")

    print(f"\n{'='*55}")
    print(f"  Generando dashboard — {datetime.date.today()}")
    print(f"{'='*55}\n")
    generar_dashboard_html()

    print("\n✅ Completado.\n")

if __name__ == "__main__":
    asyncio.run(main())
