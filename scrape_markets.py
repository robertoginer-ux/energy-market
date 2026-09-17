"""
Scraper diario de mercados energéticos: OMIE, MIBGAS, OMIP y Brent/TTF (Yahoo Finance).
CO2 (EUA) queda pendiente — ver nota más abajo en scrape_co2_safe().

Diseñado para ejecutarse vía GitHub Actions todos los días a las 7:00h hora de España
(ver .github/workflows/daily-scrape.yml). También se puede ejecutar en local con:

    pip install -r requirements.txt
    python scrape_markets.py --force

El script:
  1. Descarga cada página/endpoint público (sin login, sin API key).
  2. Extrae los valores mediante expresiones regulares (OMIE/MIBGAS/OMIP) o el
     JSON público de Yahoo Finance (Brent/TTF).
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


# ---------------------------------------------------------------------------
# OMIP
# ---------------------------------------------------------------------------
def scrape_omip() -> dict:
    url = "https://www.omip.pt/es/plazo-hoy"
    text = get_text(url)

    # Nos quedamos solo con el bloque de electricidad España (FTB), que es el
    # primero de la página, antes de que empiece el bloque de Portugal (PTEL BASE).
    idx_fin = text.find("PTEL BASE")
    ftb_section = text[:idx_fin] if idx_fin != -1 else text

    def buscar_precio(etiqueta: str):
        m = re.search(re.escape(etiqueta) + r"\s+€([\-\d.,]+)", ftb_section)
        return to_float(m.group(1)) if m else None

    spel_base_spot = buscar_precio("SPEL BASE")
    q4_26 = buscar_precio("Q4-26")
    yr_27 = buscar_precio("YR-27")
    yr_28 = buscar_precio("YR-28")

    # Meses individuales cotizando actualmente (rolling, típicamente 3-6 meses vista)
    meses_regex = re.findall(r"\b([A-Z][a-z]{2}-\d{2})\s+€([\-\d.,]+)", ftb_section)
    # Quitamos duplicados manteniendo el primer valor de cada mes
    meses = {}
    for mes, precio in meses_regex:
        if mes not in meses:
            meses[mes] = to_float(precio)

    return {
        "fuente": "OMIP",
        "spel_base_spot": spel_base_spot,
        "q4_26": q4_26,
        "yr_27": yr_27,  # equivalente a "Cal-27"
        "yr_28": yr_28,  # equivalente a "Cal-28"
        "meses": meses,  # dict {"Oct-26": 138.75, "Nov-26": 156.0, ...}
        "url": url,
    }


# ---------------------------------------------------------------------------
# Brent y TTF vía Yahoo Finance
# ---------------------------------------------------------------------------
# Investing.com bloquea (403) las peticiones desde IPs de datacenter como las
# de GitHub Actions. Yahoo Finance expone un endpoint JSON público (no
# oficial, pero ampliamente usado) que es mucho más permisivo.
YAHOO_SOURCES = {
    "Brent": "BZ=F",       # Brent Crude Oil Last Day Financial Futures (USD/barril)
    "TTF": "TTF=F",        # Dutch TTF Natural Gas Calendar (EUR/MWh)
}


def scrape_yahoo_asset(nombre: str, symbol: str) -> dict:
    from urllib.parse import quote

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol, safe='')}"
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    result = (data.get("chart") or {}).get("result") or []
    if not result:
        raise ValueError(f"Yahoo Finance no devolvió datos para {symbol}: {data.get('chart', {}).get('error')}")

    meta = result[0].get("meta", {})

    return {
        "fuente": "Yahoo Finance",
        "activo": nombre,
        "ticker": symbol,
        "precio_actual": meta.get("regularMarketPrice"),
        "precio_cierre_anterior": meta.get("previousClose") or meta.get("chartPreviousClose"),
        "moneda": meta.get("currency"),
        "url": f"https://finance.yahoo.com/quote/{symbol}/",
    }


def scrape_yahoo() -> list:
    resultados = []
    for nombre, symbol in YAHOO_SOURCES.items():
        try:
            resultados.append(scrape_yahoo_asset(nombre, symbol))
        except Exception as e:
            print(f"[AVISO] Fallo al scrapear Yahoo Finance/{nombre}: {e}")
            resultados.append({"fuente": "Yahoo Finance", "activo": nombre, "error": str(e), "ticker": symbol})
    return resultados


# ---------------------------------------------------------------------------
# CO2 (EUA) — PENDIENTE
# ---------------------------------------------------------------------------
# Investing.com bloquea (403) a nivel de IP/edge las peticiones desde GitHub
# Actions, tanto con requests simple como con un navegador real (Playwright) —
# no es un reto de JavaScript que se pueda "resolver", es un bloqueo de la
# infraestructura de red. Yahoo Finance tampoco tiene gratis el contrato
# exacto (^ICEEUA es un índice en puntos, no el precio en €/tonelada).
#
# Opciones para retomar esto en el futuro, si se quiere:
#   1. Un servicio de scraping con proxies residenciales (ScraperAPI,
#      ScrapingBee...), que sí sortean bloqueos por IP de datacenter.
#   2. Barchart.com tiene el mismo contrato exacto (ticker CKZ26 = ICE EUA
#      Futures Dec'26) pero su precio se carga por JavaScript, así que
#      requeriría también un navegador real — sin garantía de que no
#      bloqueen igual.
#
# Por ahora dejamos el campo marcado explícitamente como pendiente en vez de
# fallar silenciosamente o inventar un número.
def scrape_co2_safe() -> dict:
    return {
        "fuente": "Pendiente",
        "activo": "CO2",
        "precio_actual": None,
        "nota": "Investing.com bloquea IPs de datacenter (GitHub Actions); sin fuente gratuita alternativa confirmada todavía.",
    }


def scrape_con_fallback(nombre_fuente: str, funcion):
    """Ejecuta una función de scraping y, si falla, devuelve un dict de error
    en vez de interrumpir todo el script (para que las demás fuentes se
    guarden igualmente)."""
    try:
        return funcion()
    except Exception as e:
        print(f"[AVISO] Fallo al scrapear {nombre_fuente}: {e}")
        return {"fuente": nombre_fuente, "error": str(e)}


# ---------------------------------------------------------------------------
# Guardado de resultados
# ---------------------------------------------------------------------------
def guardar_snapshot(resultado: dict, fecha_iso: str):
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, f"{fecha_iso}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(resultado, f, ensure_ascii=False, indent=2)
    print(f"[OK] Snapshot guardado en {path}")


def append_history(resultado: dict, fecha_iso: str):
    """Añade filas planas (una por dato relevante) a data/history.csv"""
    path = os.path.join(DATA_DIR, "history.csv")
    existe = os.path.exists(path)

    filas = []
    omie = resultado["omie"]
    filas.append(["OMIE", "precio_medio_es", omie.get("precio_medio_es")])
    filas.append(["OMIE", "precio_maximo_es", omie.get("precio_maximo_es")])
    filas.append(["OMIE", "precio_minimo_es", omie.get("precio_minimo_es")])
    filas.append(["OMIE", "volumen_gwh_es", omie.get("volumen_gwh_es")])

    mibgas = resultado["mibgas"]
    filas.append(["MIBGAS", "pvb_d1", mibgas.get("precio_eur_mwh")])

    omip = resultado["omip"]
    filas.append(["OMIP", "spel_base_spot", omip.get("spel_base_spot")])
    filas.append(["OMIP", "q4_26", omip.get("q4_26")])
    filas.append(["OMIP", "yr_27", omip.get("yr_27")])
    filas.append(["OMIP", "yr_28", omip.get("yr_28")])
    for mes, precio in omip.get("meses", {}).items():
        filas.append(["OMIP", f"mes_{mes}", precio])

    for activo in resultado["yahoo"]:
        filas.append(["Yahoo", activo["activo"], activo.get("precio_actual")])

    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not existe:
            writer.writerow(["fecha", "fuente", "variable", "valor"])
        for fuente, variable, valor in filas:
            writer.writerow([fecha_iso, fuente, variable, valor])
    print(f"[OK] {len(filas)} filas añadidas a {path}")


# ---------------------------------------------------------------------------
# Control de horario (Madrid 7:00h, con margen para el cron en UTC)
# ---------------------------------------------------------------------------
def es_hora_de_ejecutar(forzar: bool) -> bool:
    if forzar:
        return True
    ahora_madrid = datetime.now(MADRID_TZ)
    # El workflow dispara el cron a las 5:00 y 6:00 UTC para cubrir el cambio de
    # hora (CET/CEST). Solo continuamos si son las 7 en punto (rango 6:45-7:15)
    # hora de Madrid, para no duplicar la ejecución.
    return ahora_madrid.hour == 7 and ahora_madrid.minute < 30


def main():
    forzar = "--force" in sys.argv or os.environ.get("FORCE_RUN") == "1"

    if not es_hora_de_ejecutar(forzar):
        print("No son las 7:00h en Madrid todavía (o ya ha pasado el margen). Saliendo.")
        return

    fecha_iso = datetime.now(MADRID_TZ).date().isoformat()

    resultado = {
        "fecha": fecha_iso,
        "omie": scrape_con_fallback("OMIE", scrape_omie),
        "mibgas": scrape_con_fallback("MIBGAS", scrape_mibgas),
        "omip": scrape_con_fallback("OMIP", scrape_omip),
        "yahoo": scrape_yahoo() + [scrape_co2_safe()],
    }

    guardar_snapshot(resultado, fecha_iso)
    append_history(resultado, fecha_iso)


if __name__ == "__main__":
    main()
