"""
Scraper diario de mercados energéticos: OMIE, MIBGAS, OMIP, Brent/TTF (Yahoo Finance)
y CO2/EUA (Sendeco2).

Diseñado para ejecutarse vía GitHub Actions todos los días (ver
.github/workflows/daily-scrape.yml). También se puede ejecutar en local con:

    pip install -r requirements.txt
    python scrape_markets.py --force

El script:
  1. Descarga cada página/endpoint público (sin login, sin API key).
  2. Extrae los valores mediante expresiones regulares (OMIE/MIBGAS/OMIP), el
     JSON público de Yahoo Finance (Brent/TTF), o el CSV público de Sendeco2 (CO2).
  3. Calcula la variación (absoluta y %) de cada variable respecto al día
     anterior, usando el histórico ya guardado.
  4. Guarda un snapshot diario en data/YYYY-MM-DD.json (incluye "filas", la
     lista con valor + variación de cada variable — la usan también
     update_google_sheet.py y send_email.py).
  5. Añade esas mismas filas a data/history.csv.

Solo se ejecuta una vez al día: si ya existe el snapshot de hoy, no repite
(esto es importante porque GitHub Actions puede retrasar el cron varias
horas, y no queremos que eso impida la ejecución del día).

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
        r"Precio medio España\s+([\-\d.,]+)\s*€/MWh\s*Máximo\s+([\-\d.,]+)\s*€/MWh\s*"
        r"Mínimo\s+([\-\d.,]+)\s*€/MWh",
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
# CO2 (EUA) vía Sendeco2
# ---------------------------------------------------------------------------
# sendeco2.com publica un CSV público con el precio diario de referencia del
# EUA (derechos de emisión), sin bloqueos de IP ni necesidad de navegador.
# No es exactamente el mismo contrato que investing.com (CFI2Z6, un futuro
# concreto), sino el precio de referencia diario que usa el mercado, pero es
# la fuente gratuita más fiable que hemos encontrado.
def scrape_co2() -> dict:
    year = datetime.now(MADRID_TZ).year
    url = f"https://www.sendeco2.com/site_sendeco/service/download-csv.php?year={year}"
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    resp.encoding = "iso-8859-15"

    lineas = [l for l in resp.text.strip().splitlines() if l.strip()]
    filas_csv = lineas[1:]  # se salta la cabecera "Fecha;EUA;CER;SPREAD..."
    if not filas_csv:
        raise ValueError("El CSV de Sendeco2 no tiene filas de datos")

    ultima = filas_csv[-1].split(";")
    fecha_dato = ultima[0]  # formato DD-MM-YYYY
    precio_eua = to_float(ultima[1])

    return {
        "fuente": "Sendeco2",
        "activo": "CO2",
        "precio_actual": precio_eua,
        "fecha_dato": fecha_dato,
        "url": "https://www.sendeco2.com/es/precios-co2",
    }


def scrape_co2_safe() -> dict:
    try:
        return scrape_co2()
    except Exception as e:
        print(f"[AVISO] Fallo al scrapear CO2 (Sendeco2): {e}")
        return {"fuente": "Sendeco2", "activo": "CO2", "precio_actual": None, "error": str(e)}


def col_to_letter(idx: int) -> str:
    """0 -> 'A', 1 -> 'B', ..., 25 -> 'Z', 26 -> 'AA', ..."""
    idx += 1
    letters = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def migrar_historico_si_hace_falta():
    """Si history.csv ya existe en el formato antiguo (sin columnas de
    variación), lo reescribe añadiendo esas 2 columnas vacías para las filas
    históricas, sin perder los datos ya guardados."""
    path = os.path.join(DATA_DIR, "history.csv")
    if not os.path.exists(path):
        return
    with open(path, newline="", encoding="utf-8") as f:
        filas = list(csv.reader(f))
    if not filas:
        return
    cabecera_nueva = ["fecha", "fuente", "variable", "valor", "variacion_abs", "variacion_pct"]
    if filas[0] == cabecera_nueva:
        return  # ya está migrado
    nuevas = [cabecera_nueva]
    for fila in filas[1:]:
        fila = fila + [""] * (6 - len(fila))
        nuevas.append(fila[:6])
    with open(path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(nuevas)
    print("[OK] history.csv migrado al nuevo formato (con columnas de variación)")


def cargar_historico() -> dict:
    """Carga history.csv en un dict {(fuente, variable): [(fecha, valor), ...]}."""
    path = os.path.join(DATA_DIR, "history.csv")
    historico = {}
    if not os.path.exists(path):
        return historico
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                valor = float(row["valor"]) if row.get("valor") else None
            except ValueError:
                valor = None
            historico.setdefault((row["fuente"], row["variable"]), []).append((row["fecha"], valor))
    return historico


def valor_anterior(historico: dict, fuente: str, variable: str, fecha_hoy: str):
    """Valor más reciente de (fuente, variable) con fecha anterior a fecha_hoy."""
    registros = [(f, v) for f, v in historico.get((fuente, variable), []) if f < fecha_hoy and v is not None]
    if not registros:
        return None
    registros.sort(key=lambda x: x[0])
    return registros[-1][1]


def calcular_variacion(valor_hoy, valor_ayer):
    if valor_hoy is None or valor_ayer is None:
        return None, None
    abs_ = valor_hoy - valor_ayer
    pct = (abs_ / valor_ayer * 100) if valor_ayer else None
    return abs_, pct


def construir_filas(resultado_fuentes: dict, historico: dict, fecha_iso: str) -> list:
    """Lista plana [{fuente, variable, valor, valor_anterior, variacion_abs,
    variacion_pct}, ...] — la usan el CSV histórico, el snapshot JSON, el
    Google Sheet y el email, todos a partir de la misma fuente de verdad."""
    filas = []

    def agregar(fuente, variable, valor):
        v_ayer = valor_anterior(historico, fuente, variable, fecha_iso)
        v_abs, v_pct = calcular_variacion(valor, v_ayer)
        filas.append(
            {
                "fuente": fuente,
                "variable": variable,
                "valor": valor,
                "valor_anterior": v_ayer,
                "variacion_abs": v_abs,
                "variacion_pct": v_pct,
            }
        )

    omie = resultado_fuentes["omie"]
    agregar("OMIE", "precio_medio_es", omie.get("precio_medio_es"))
    agregar("OMIE", "precio_maximo_es", omie.get("precio_maximo_es"))
    agregar("OMIE", "precio_minimo_es", omie.get("precio_minimo_es"))
    agregar("OMIE", "volumen_gwh_es", omie.get("volumen_gwh_es"))

    mibgas = resultado_fuentes["mibgas"]
    agregar("MIBGAS", "pvb_d1", mibgas.get("precio_eur_mwh"))

    omip = resultado_fuentes["omip"]
    agregar("OMIP", "spel_base_spot", omip.get("spel_base_spot"))
    agregar("OMIP", "q4_26", omip.get("q4_26"))
    agregar("OMIP", "yr_27", omip.get("yr_27"))
    agregar("OMIP", "yr_28", omip.get("yr_28"))
    for mes, precio in omip.get("meses", {}).items():
        agregar("OMIP", f"mes_{mes}", precio)

    for activo in resultado_fuentes["yahoo"]:
        agregar("Yahoo", activo["activo"], activo.get("precio_actual"))

    co2 = resultado_fuentes["co2"]
    agregar("Sendeco2", "CO2", co2.get("precio_actual"))

    return filas


# ---------------------------------------------------------------------------
# Guardado de resultados
# ---------------------------------------------------------------------------
def scrape_con_fallback(nombre_fuente: str, funcion):
    """Ejecuta una función de scraping y, si falla, devuelve un dict de error
    en vez de interrumpir todo el script (para que las demás fuentes se
    guarden igualmente)."""
    try:
        return funcion()
    except Exception as e:
        print(f"[AVISO] Fallo al scrapear {nombre_fuente}: {e}")
        return {"fuente": nombre_fuente, "error": str(e)}


def guardar_snapshot(resultado: dict, fecha_iso: str):
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, f"{fecha_iso}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(resultado, f, ensure_ascii=False, indent=2)
    print(f"[OK] Snapshot guardado en {path}")


def append_history(filas: list, fecha_iso: str):
    """Añade filas planas (una por variable) a data/history.csv, incluyendo
    la variación absoluta y porcentual respecto al día anterior."""
    path = os.path.join(DATA_DIR, "history.csv")
    existe = os.path.exists(path)

    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not existe:
            writer.writerow(["fecha", "fuente", "variable", "valor", "variacion_abs", "variacion_pct"])
        for fila in filas:
            writer.writerow(
                [
                    fecha_iso,
                    fila["fuente"],
                    fila["variable"],
                    fila["valor"],
                    fila["variacion_abs"],
                    fila["variacion_pct"],
                ]
            )
    print(f"[OK] {len(filas)} filas añadidas a {path}")


# ---------------------------------------------------------------------------
# Control de ejecución: una vez al día, sea cuando sea que dispare el cron
# ---------------------------------------------------------------------------
# GitHub Actions puede retrasar los cron varias horas en repos con poca
# actividad (es un comportamiento documentado de GitHub, no un fallo
# nuestro). Antes comprobábamos "¿son las 7:00h en Madrid?", pero si el cron
# se disparaba tarde (p.ej. a las 11:17h), el script se cancelaba pensando
# que aún no tocaba, y ese día no se ejecutaba nada. Ahora, en su lugar,
# comprobamos simplemente si ya existe un snapshot de hoy: si no existe,
# ejecutamos (sea la hora que sea); si ya existe, no repetimos (para evitar
# duplicar el email si los dos cron del día llegan a disparase el mismo día).
def ya_se_ejecuto_hoy(fecha_iso: str) -> bool:
    return os.path.exists(os.path.join(DATA_DIR, f"{fecha_iso}.json"))


def main():
    forzar = "--force" in sys.argv or os.environ.get("FORCE_RUN") == "1"

    fecha_iso = datetime.now(MADRID_TZ).date().isoformat()

    if not forzar and ya_se_ejecuto_hoy(fecha_iso):
        print(f"[INFO] Ya se generó el snapshot de hoy ({fecha_iso}); no se repite. Saliendo.")
        return

    migrar_historico_si_hace_falta()
    historico = cargar_historico()

    resultado_fuentes = {
        "omie": scrape_con_fallback("OMIE", scrape_omie),
        "mibgas": scrape_con_fallback("MIBGAS", scrape_mibgas),
        "omip": scrape_con_fallback("OMIP", scrape_omip),
        "yahoo": scrape_yahoo(),
        "co2": scrape_co2_safe(),
    }

    filas = construir_filas(resultado_fuentes, historico, fecha_iso)

    resultado = {"fecha": fecha_iso, **resultado_fuentes, "filas": filas}

    guardar_snapshot(resultado, fecha_iso)
    append_history(filas, fecha_iso)


if __name__ == "__main__":
    main()
