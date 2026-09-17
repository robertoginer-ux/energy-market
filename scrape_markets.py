"""
Scraper diario de mercados energéticos: OMIE, MIBGAS, OMIP, Brent/TTF (Yahoo Finance)
y CO2/EUA (Investing.com vía navegador real).

Diseñado para ejecutarse vía GitHub Actions todos los días a las 7:00h hora de España
(ver .github/workflows/daily-scrape.yml). También se puede ejecutar en local con:

    pip install -r requirements.txt
    playwright install --with-deps chromium
    python scrape_markets.py --force

El script:
  1. Descarga cada página/endpoint público (sin login, sin API key).
  2. Extrae los valores mediante expresiones regulares (OMIE/MIBGAS/OMIP), el JSON
     público de Yahoo Finance (Brent/TTF), o un navegador headless (CO2/Investing.com).
  3. Guarda un snapshot diario en data/YYYY-MM-DD.json
  4. Añade una fila resumen a data/history.csv (uno por fuente/producto)

IMPORTANTE: estas páginas son HTML público que puede cambiar de estructura en cualquier
momento. Si algún valor sale como None, lo primero es volver a mirar el texto real de la
página y ajustar la regex correspondiente.
"""

import csv
import json
import os
import re
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

MADRID_TZ = ZoneInfo("Europe/Madrid")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
TIMEOUT = 30

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def to_float(value: str):
    """Convierte '1.234,56' (formato español) o '1234.56' a float."""
    if value is None:
        return None
    value = value.strip()
    if "," in value and "." in value:
        value = value.replace(".", "").replace(",", ".")
    elif "," in value:
        value = value.replace(",", ".")
    try:
        return float(value)
    except ValueError:
        return None


def get_text(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    return soup.get_text(" ", strip=True)


# ---------------------------------------------------------------------------
# OMIE
# ---------------------------------------------------------------------------
def scrape_omie() -> dict:
    url = "https://www.omie.es/es/spot-hoy"
    text = get_text(url)

    m_fecha = re.search(r"para el (\d{1,2}\s+[A-Za-zÀ-ÿ]+)", text)
    m_es = re.search(
        r"Precio medio España\s+([\d.,]+)\s*€/MWh\s*Máximo\s+([\d.,]+)\s*€/MWh\s*"
        r"Mínimo\s+([\d.,]+)\s*€/MWh",
        text,
    )
    m_vol = re.search(r"Volumen negociado España\s+([\d.,]+)", text)

    return {
        "fuente": "OMIE",
        "fecha_texto": m_fecha.group(1) if m_fecha else None,
        "precio_medio_es": to_float(m_es.group(1)) if m_es else None,
        "precio_maximo_es": to_float(m_es.group(2)) if m_es else None,
        "precio_minimo_es": to_float(m_es.group(3)) if m_es else None,
        "volumen_gwh_es": to_float(m_vol.group(1)) if m_vol else None,
        "url": url,
    }


# ---------------------------------------------------------------------------
# MIBGAS
# ---------------------------------------------------------------------------
def scrape_mibgas() -> dict:
    url = "https://www.mibgas.es/es/market-results"
    text = get_text(url)

    # Bloque "Diario" contiene 3 pares fecha;precio (hoy, D+1, D+2 aprox.)
    m_diario_block = re.search(r"Diario\s+(.*?)Fin de semana", text)
    diario_pairs = []
    if m_diario_block:
        diario_pairs = re.findall(r"(\d{2}/\d{2})\s+([\d.,]+)", m_diario_block.group(1))

    m_intradiario = re.search(r"Intradiario\s+(\d{2}/\d{2})\s+([\d.,]+)", text)

    hoy = datetime.now(MADRID_TZ).date()
    manana = hoy + timedelta(days=1)
    manana_str = manana.strftime("%d/%m")

    precio_pvb_d1 = None
    fecha_pvb_d1 = None
    for fecha_str, precio_str in diario_pairs:
        if fecha_str == manana_str:
            precio_pvb_d1 = to_float(precio_str)
            fecha_pvb_d1 = fecha_str
            break
    # Fallback: si no se encuentra la fecha exacta de mañana, coge la 2ª entrada del bloque
    if precio_pvb_d1 is None and len(diario_pairs) >= 2:
        fecha_pvb_d1, precio_str = diario_pairs[1]
        precio_pvb_d1 = to_float(precio_str)

    return {
        "fuente": "MIBGAS",
        "producto": "PVB D+1",
        "fecha_entrega": fecha_pvb_d1,
        "precio_eur_mwh": precio_pvb_d1,
        "precio_intradiario_hoy": to_float(m_intradiario.group(2)) if m_intradiario else None,
        "diario_raw": diario_pairs,  # para depurar si algo falla
        "url": url,
    }


# --------------------------------------------
