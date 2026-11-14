"""
Envía el email diario "Radar de mercados" con los datos del día y su
variación, con el diseño de marca de Octopus Energy España (tokens de color
de Coral, OE Web - Dark; tipografía Montserrat con Arial de respaldo, que es
lo que acaban mostrando Gmail y Outlook).

Rediseño del 14/11/2026: resumen con 3 cifras clave arriba, fecha real de
cada mercado en su cabecera, unidades en cada valor, futuros ordenados por
vencimiento, "sin cambio" en gris, y logo + Constantine como imágenes
adjuntas integradas (CID), que Gmail muestra de forma más fiable que las
imágenes en base64 dentro del HTML.

Requiere la variable de entorno GMAIL_APP_PASSWORD (contraseña de aplicación
de roberto.giner@octoenergy.com) y los ficheros assets/logo.png y
assets/constantine_growth.png.

Uso:
    python send_email.py [FECHA_ISO]            # envía
    python send_email.py FECHA_ISO --preview X  # solo genera X.html (no envía)

Si no se pasa FECHA_ISO (formato YYYY-MM-DD), usa el día de hoy en Madrid.
Lee el snapshot data/<FECHA_ISO>.json generado por scrape_markets.py; si no
existe, termina sin error, simplemente avisando. Los domingos no envía.
"""

import base64
import json
import os
import smtplib
import sys
from datetime import datetime
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

MADRID_TZ = ZoneInfo("Europe/Madrid")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
ASSETS_DIR = os.path.join(BASE_DIR, "assets")

GMAIL_USER = "roberto.giner@octoenergy.com"  # remitente real (cuenta de Gmail con contraseña de aplicación)
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
DESTINATARIOS = [
    "smt_spain@octoenergy.com",  # grupo de distribución (Google Group), no una cuenta con login
    "giampaolo.panizio@octopusenergy.es",
    "alberto.lopez.hernandez@octopusenergy.es",
    "andres.gilblanco@octoenergy.com",
    "oees-oms-ops@octoenergy.com",
]

# Imágenes integradas: (Content-ID, fichero en assets/)
IMAGENES = {
    "fondo_exterior": "fondo_exterior.png",  # #0A0020, fondo de todo el email
    "fondo_email": "fondo_email.png",        # #100030, caja central
    "logo": "logo.png",
    "constantine": "constantine_growth.png",  # pose "growth": junto a un gráfico
}

# --- Tokens Coral (OE Web - Dark) -------------------------------------------
# Colores sólidos (sin rgba). El fondo del email lleva además una imagen del
# mismo color (ver fondo()): el modo oscuro de Outlook aclara los colores de fondo.
NOCHE = "#0A0020"         # fondo exterior (más oscuro que Siphon)
SIPHON = "#100030"        # Background/Base/900 — fondo del email
HEMO = "#180048"          # Background/Base/500 — tablas
BLUEBERRY = "#2D1A83"     # Background/Secondary/900 — tarjetas de resumen
INK = "#5840FF"           # Background/Secondary/500 — contorno de tablas (2px)
ICE = "#F0FFFF"           # Contents nivel 1 — texto principal y cifras
DUSTY = "#D9DDFF"         # nivel 2 — texto secundario
HAZE = "#A49FC8"          # nivel 3 — etiquetas, unidades, "sin cambio"
VOLTAGE = "#60F0F8"       # nivel 6 — subidas y acento
SOHO = "#F050F8"          # Primary/500 — bajadas
WARN_BG = "#3A1648"       # Warning #EC8243 al 10 % sobre Hemocyanin, en sólido
WARN_BORDER = "#6B2F45"
WARN_TXT = "#FFA26B"      # Warning, contenido sobre /10
ROW_LINE = "#2A1466"      # separador entre filas
FONT = "'Montserrat', Arial, Helvetica, sans-serif"
NUM = "font-variant-numeric:tabular-nums;"


def fondo(color: str, imagen: str) -> str:
    """Fondo del email: color + imagen del mismo color. Outlook en modo oscuro
    aclara los colores de fondo, pero no toca las imágenes."""
    return f"background-color:{color};background-image:url('{imagen}');background-repeat:repeat;"

MESES = {"Jan": "Ene", "Feb": "Feb", "Mar": "Mar", "Apr": "Abr", "May": "May", "Jun": "Jun",
         "Jul": "Jul", "Aug": "Ago", "Sep": "Sep", "Oct": "Oct", "Nov": "Nov", "Dec": "Dic"}
ORDEN_MES = {m: i for i, m in enumerate(MESES)}
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES_LARGOS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
                "septiembre", "octubre", "noviembre", "diciembre"]


def fmt_numero(valor, decimales=2) -> str:
    if valor is None:
        return "—"
    texto = f"{valor:,.{decimales}f}"
    # Formato español: punto de miles, coma decimal
    return texto.replace(",", "§").replace(".", ",").replace("§", ".")


def ddmm(fecha) -> str:
    """'2026-11-13' o '13/11/2026' o '13/11' -> '13/11'. Vacío si no hay fecha."""
    if not fecha:
        return ""
    s = str(fecha)
    if len(s) == 10 and s[4] == "-":
        return f"{s[8:10]}/{s[5:7]}"
    return s[:5]


def con_fecha(prefijo: str, fecha) -> str:
    f = ddmm(fecha)
    return f"{prefijo} {f}" if f else ""


def delta(fila: dict, decimales: int = 2) -> str:
    """Variación en 2 líneas: flecha + absoluta / porcentaje. Sube = Voltage,
    baja = Soho Lights, sin cambio = gris (antes salía '▲ +0,00')."""
    if fila.get("valor") is None:
        return ""
    a, p = fila.get("variacion_abs"), fila.get("variacion_pct")
    if a is None or p is None:
        return f'<span style="color:{HAZE};font-size:12px;">sin dato previo</span>'
    if round(a, decimales) == 0:
        return (f'<span style="color:{HAZE};font-size:13px;font-weight:700;{NUM}">= 0,00</span>'
                f'<br><span style="color:{HAZE};font-size:12px;{NUM}">0,0 %</span>')
    sube = a > 0
    color, flecha, signo = (VOLTAGE, "▲", "+") if sube else (SOHO, "▼", "−")
    return (f'<span style="color:{color};font-size:13px;font-weight:700;{NUM}white-space:nowrap;">'
            f'{flecha} {signo}{fmt_numero(abs(a), decimales)}</span>'
            f'<br><span style="color:{color};font-size:12px;{NUM}">{signo}{fmt_numero(abs(p), 1)} %</span>')


def kpi(titulo: str, fila: dict, unidad: str, meta: str) -> str:
    valor = fmt_numero(fila.get("valor"))
    return f"""
    <td class="kpi" width="33%" valign="top" style="padding:0 4px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" bgcolor="{BLUEBERRY}" style="background:{BLUEBERRY};border-radius:12px;">
        <tr><td class="kpi-in" style="padding:14px 14px 12px 14px;font-family:{FONT};">
          <div style="color:{HAZE};font-size:11px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;">{titulo}</div>
          <div class="kpi-val" style="color:{ICE};font-size:24px;font-weight:700;line-height:1.2;margin-top:6px;{NUM}white-space:nowrap;">{valor}</div>
          <div class="kpi-meta" style="color:{HAZE};font-size:11px;margin-bottom:8px;">{unidad}<br>{meta}</div>
          <div style="line-height:1.3;">{delta(fila)}</div>
        </td></tr>
      </table>
    </td>"""


def fila_html(nombre: str, fila: dict, unidad: str, nota: str = None, primera: bool = False) -> str:
    borde = "" if primera else f"border-top:1px solid {ROW_LINE};"
    nota_html = f'<br><span style="color:{HAZE};font-size:11px;">{nota}</span>' if nota else ""
    return f"""
        <tr>
          <td style="{borde}padding:10px 12px;font-family:{FONT};color:{DUSTY};font-size:14px;width:46%;">{nombre}{nota_html}</td>
          <td style="{borde}padding:10px 6px;font-family:{FONT};text-align:right;white-space:nowrap;width:28%;">
            <span style="color:{ICE};font-size:15px;font-weight:700;{NUM}">{fmt_numero(fila.get('valor'))}</span><br>
            <span style="color:{HAZE};font-size:11px;">{unidad}</span></td>
          <td style="{borde}padding:10px 12px;font-family:{FONT};text-align:right;line-height:1.35;width:26%;">{delta(fila)}</td>
        </tr>"""


def subtitulo(texto: str, primera: bool = False) -> str:
    borde = "" if primera else f"border-top:1px solid {ROW_LINE};"
    return f"""
        <tr><td colspan="3" style="{borde}padding:12px 12px 2px 12px;font-family:{FONT};color:{HAZE};font-size:11px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;">{texto}</td></tr>"""


def seccion(titulo: str, meta: str, filas_html: str, aviso: str = None) -> str:
    aviso_html = ""
    if aviso:
        aviso_html = f"""
    <tr><td style="padding:0 24px 8px 24px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" bgcolor="{WARN_BG}" style="background:{WARN_BG};border:1px solid {WARN_BORDER};border-radius:12px;">
        <tr><td style="padding:10px 14px;font-family:{FONT};color:{WARN_TXT};font-size:13px;line-height:1.45;">{aviso.replace('⚠️ ', '')}</td></tr>
      </table></td></tr>"""
    return f"""
    <tr><td style="padding:22px 24px 8px 24px;">
      <div style="font-family:{FONT};color:{ICE};font-size:16px;font-weight:700;">{titulo}</div>
      <div style="font-family:{FONT};color:{HAZE};font-size:12px;margin-top:2px;">{meta}</div></td></tr>
    {aviso_html}
    <tr><td style="padding:0 24px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" bgcolor="{HEMO}" style="background:{HEMO};border:2px solid {INK};border-radius:12px;table-layout:fixed;">
        {filas_html}
      </table></td></tr>"""


def construir_html(resultado: dict, src_imagenes: dict = None) -> str:
    """src_imagenes: {"logo": src, "constantine": src}. Por defecto 'cid:…'
    (imágenes adjuntas al email); para previsualizar se pasan data: URIs."""
    src = src_imagenes or {cid: f"cid:{cid}" for cid in IMAGENES}
    fecha = datetime.fromisoformat(resultado["fecha"]).date()
    fecha_larga = f"{DIAS[fecha.weekday()].capitalize()}, {fecha.day} de {MESES_LARGOS[fecha.month - 1]} de {fecha.year}"

    filas = {(f["fuente"], f["variable"]): f for f in resultado.get("filas", [])}

    def F(fuente, variable):
        return filas.get((fuente, variable), {"valor": None})

    omie = resultado.get("omie") or {}
    mibgas = resultado.get("mibgas") or {}
    omip = resultado.get("omip") or {}
    ent_omie = ddmm(omie.get("fecha_texto"))
    ent_gas = ddmm(mibgas.get("fecha_entrega"))
    cierre_omip = ddmm(omip.get("fecha_precios"))

    kpis = (kpi("Luz hoy", F("OMIE", "precio_medio_es"), "€/MWh", con_fecha("OMIE ·", ent_omie) or "OMIE")
            + kpi("Gas D+1", F("MIBGAS", "pvb_d1"), "€/MWh", con_fecha("MIBGAS ·", ent_gas) or "MIBGAS")
            + kpi("Cal-27", F("OMIP", "yr_27"), "€/MWh", con_fecha("OMIP ·", cierre_omip) or "OMIP"))

    # Electricidad, mercado diario (OMIE)
    s_omie = (fila_html("Precio medio", F("OMIE", "precio_medio_es"), "€/MWh", primera=True)
              + fila_html("Máximo", F("OMIE", "precio_maximo_es"), "€/MWh")
              + fila_html("Mínimo", F("OMIE", "precio_minimo_es"), "€/MWh")
              + fila_html("Energía negociada", F("OMIE", "volumen_gwh_es"), "GWh"))

    # Gas, mercado diario (MIBGAS)
    s_gas = fila_html("PVB día siguiente", F("MIBGAS", "pvb_d1"), "€/MWh", primera=True)

    # Futuros (OMIP), por vencimiento: meses, trimestres, años. El spot SPEL
    # no se muestra: es el mismo precio medio que ya da OMIE.
    meses = sorted(
        [f for (fu, v), f in filas.items() if fu == "OMIP" and v.startswith("mes_")],
        key=lambda f: (f["variable"][-2:], ORDEN_MES.get(f["variable"][4:7], 99)),
    )
    trimestres = sorted(
        [f for (fu, v), f in filas.items() if fu == "OMIP" and v.startswith("trimestre_")],
        key=lambda f: (f["variable"][-2:], f["variable"]),
    )
    s_omip = ""
    if meses:
        s_omip += subtitulo("Meses", primera=True)
        s_omip += "".join(
            fila_html(f"{MESES.get(f['variable'][4:7], f['variable'][4:7])}-{f['variable'][-2:]}", f, "€/MWh", primera=(i == 0))
            for i, f in enumerate(meses)
        )
    if trimestres:
        s_omip += subtitulo("Trimestres", primera=not s_omip)
        s_omip += "".join(
            fila_html(f["variable"][len("trimestre_"):], f, "€/MWh", primera=(i == 0))
            for i, f in enumerate(trimestres)
        )
    s_omip += subtitulo("Años", primera=not s_omip)
    s_omip += fila_html("Cal-27", F("OMIP", "yr_27"), "€/MWh", primera=True)
    s_omip += fila_html("Cal-28", F("OMIP", "yr_28"), "€/MWh")
    aviso_omip = next((f.get("aviso") for (fu, _), f in filas.items() if fu == "OMIP" and f.get("aviso")), None)

    # Internacional (Yahoo Finance + EEX). Si no hay subasta de CO2 ese día,
    # se indica y se muestra el último precio conocido.
    co2 = F("EEX", "CO2")
    nota_co2 = None
    if co2.get("valor") is None and co2.get("valor_anterior") is not None:
        nota_co2 = f"Sin subasta hoy · último {fmt_numero(co2['valor_anterior'])} €/t"
    elif co2.get("valor") is not None and co2.get("fecha_dato") and ddmm(co2["fecha_dato"].replace("-", "/")) != fecha.strftime("%d/%m"):
        nota_co2 = f"Subasta del {ddmm(co2['fecha_dato'].replace('-', '/'))}"
    s_int = (fila_html("Brent", F("Yahoo", "Brent"), "$/barril", primera=True)
             + fila_html("TTF (gas europeo)", F("Yahoo", "TTF"), "€/MWh")
             + fila_html("CO2 (EUA)", co2, "€/t", nota=nota_co2))

    secciones = (
        seccion("Electricidad · mercado diario", con_fecha("OMIE · entrega", ent_omie) or "OMIE", s_omie)
        + seccion("Gas · mercado diario", con_fecha("MIBGAS · entrega", ent_gas) or "MIBGAS", s_gas)
        + seccion("Futuros de electricidad", con_fecha("OMIP · cierre", cierre_omip) or "OMIP", s_omip, aviso_omip)
        + seccion("Internacional", "Yahoo Finance · EEX", s_int)
    )

    return f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="color-scheme" content="light dark"><meta name="supported-color-schemes" content="light dark">
<link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;500;700&display=swap" rel="stylesheet">
<title>Radar de mercados</title>
<style>
  :root {{ color-scheme: light dark; supported-color-schemes: light dark; }}
  @media (max-width:480px) {{
    .kpi {{ padding:0 3px !important; }}
    .kpi-in {{ padding:12px 10px 10px 10px !important; }}
    .kpi-val {{ font-size:19px !important; }}
    .kpi-meta {{ font-size:10px !important; }}
  }}
</style></head>
<body style="margin:0;padding:0;{fondo(NOCHE, src['fondo_exterior'])}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" bgcolor="{NOCHE}" background="{src['fondo_exterior']}" style="{fondo(NOCHE, src['fondo_exterior'])}">
<tr><td align="center" style="padding:24px 8px;">
  <table role="presentation" width="640" cellpadding="0" cellspacing="0" bgcolor="{SIPHON}" background="{src['fondo_email']}" style="max-width:640px;width:100%;{fondo(SIPHON, src['fondo_email'])}border-radius:16px;">
    <tr><td style="padding:24px 24px 0 24px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
        <td><img src="{src['logo']}" alt="octopus energy" height="24" style="height:24px;width:auto;display:block;border:0;"></td>
        <td align="right" style="font-family:{FONT};color:{HAZE};font-size:12px;">Uso interno</td>
      </tr></table></td></tr>
    <tr><td style="padding:20px 24px 4px 24px;font-family:{FONT};">
      <div style="color:{ICE};font-size:24px;font-weight:700;line-height:1.25;">Radar de mercados</div>
      <div style="color:{DUSTY};font-size:14px;margin-top:4px;">{fecha_larga}</div>
      <div style="width:48px;height:3px;background:{VOLTAGE};margin-top:14px;font-size:0;line-height:0;">&nbsp;</div>
    </td></tr>
    <tr><td style="padding:16px 20px 4px 20px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>{kpis}</tr></table>
    </td></tr>
    {secciones}
    <tr><td style="padding:24px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
        <td valign="bottom" style="font-family:{FONT};color:{HAZE};font-size:12px;line-height:1.6;">
          Variación respecto al dato anterior. <span style="color:{VOLTAGE};">▲ sube</span> · <span style="color:{SOHO};">▼ baja</span><br>
          Fuentes: omie.es · mibgas.es · omip.pt · Yahoo Finance · eex.com<br>
          Envío automático de lunes a sábado, 7:45h.
        </td>
        <td width="64" align="right" valign="bottom"><img src="{src['constantine']}" alt="" width="56" style="width:56px;height:auto;display:block;border:0;"></td>
      </tr></table></td></tr>
  </table>
</td></tr></table>
</body></html>"""


def cargar_snapshot(fecha_iso: str):
    path = os.path.join(DATA_DIR, f"{fecha_iso}.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def enviar(html: str, fecha_iso: str):
    if not GMAIL_APP_PASSWORD:
        raise RuntimeError("Falta la variable de entorno GMAIL_APP_PASSWORD")

    fecha_fmt = datetime.fromisoformat(fecha_iso).strftime("%d/%m/%Y")
    # multipart/related: el HTML y las imágenes que referencia con cid:
    msg = MIMEMultipart("related")
    msg["Subject"] = f"Radar de mercados energéticos – {fecha_fmt}"
    msg["From"] = GMAIL_USER
    msg["To"] = ", ".join(DESTINATARIOS)
    msg.attach(MIMEText(html, "html", "utf-8"))
    for cid, fichero in IMAGENES.items():
        with open(os.path.join(ASSETS_DIR, fichero), "rb") as f:
            img = MIMEImage(f.read(), _subtype="png")
        img.add_header("Content-ID", f"<{cid}>")
        img.add_header("Content-Disposition", "inline", filename=fichero)
        msg.attach(img)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, DESTINATARIOS, msg.as_string())
    print(f"[OK] Email enviado desde {GMAIL_USER} a {', '.join(DESTINATARIOS)}")


def src_data_uri() -> dict:
    """Imágenes como data: URI, solo para previsualizar en el navegador."""
    out = {}
    for cid, fichero in IMAGENES.items():
        with open(os.path.join(ASSETS_DIR, fichero), "rb") as f:
            out[cid] = "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")
    return out


def main():
    args = [a for a in sys.argv[1:]]
    preview = None
    if "--preview" in args:
        i = args.index("--preview")
        preview = args[i + 1]
        del args[i:i + 2]
    fecha_iso = args[0] if args else datetime.now(MADRID_TZ).date().isoformat()

    resultado = cargar_snapshot(fecha_iso)
    if resultado is None:
        print(f"[AVISO] No existe snapshot para {fecha_iso}. No se envía email.")
        return

    if preview:
        with open(preview, "w", encoding="utf-8") as f:
            f.write(construir_html(resultado, src_data_uri()))
        print(f"[OK] Vista previa guardada en {preview} (no se ha enviado nada)")
        return

    # Los domingos no se envía email (decisión de Roberto, 14/11/2026): el
    # sábado no cotizan los futuros y OMIE/MIBGAS aportan poco. El scraper,
    # el Google Sheet y el commit de datos siguen ejecutándose ese día.
    if datetime.fromisoformat(fecha_iso).weekday() == 6:
        print(f"[INFO] {fecha_iso} es domingo: no se envía email.")
        return

    enviar(construir_html(resultado), fecha_iso)


if __name__ == "__main__":
    main()
