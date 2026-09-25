"""
Envía el email diario con los datos de mercados y su variación, con el
diseño de marca de Octopus Energy España (fondo oscuro, Montserrat, logo
real + Constantine, acentos Voltage/Hot Pink).

Requiere la variable de entorno GMAIL_APP_PASSWORD (contraseña de aplicación
de roberto.giner@octoenergy.com). Los destinatarios son el grupo de
distribución smt_spain@octoenergy.com más varias direcciones individuales.

Requiere también los ficheros assets/logo.png y assets/constantine_casual.png
(se commitean al repo junto al script; no hace falta descargarlos en cada
ejecución).

Uso:
    python send_email.py [FECHA_ISO]

Si no se pasa FECHA_ISO (formato YYYY-MM-DD), usa el día de hoy en Madrid.
Lee el snapshot data/<FECHA_ISO>.json generado por scrape_markets.py — si no
existe, termina sin error, simplemente avisando.
"""

import base64
import json
import os
import smtplib
import sys
from datetime import datetime
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

# Ancho de la columna de etiquetas EN PORCENTAJE (no en px fijos), igual en
# todas las secciones. Con px fijos, en pantallas estrechas (móvil) casi no
# queda sitio para las columnas de valor/variación y el texto se parte
# carácter a carácter. En %, las 3 columnas escalan proporcionalmente sea
# cual sea el ancho real de la pantalla.
LABEL_WIDTH_PCT = 44
VALUE_WIDTH_PCT = 28
VARIACION_WIDTH_PCT = 28

# Nombres bonitos para las variables, para que el email sea legible sin jerga
ETIQUETAS = {
    "precio_medio_es": "Precio medio España",
    "precio_maximo_es": "Precio máximo España",
    "precio_minimo_es": "Precio mínimo España",
    "volumen_gwh_es": "Volumen negociado (GWh)",
    "pvb_d1": "PVB D+1",
    "spel_base_spot": "Spot Base (SPEL)",
    "q4_26": "Futuro Q4-26",
    "yr_27": "Futuro Cal-27 (YR-27)",
    "yr_28": "Futuro Cal-28 (YR-28)",
    "Brent": "Brent",
    "TTF": "TTF",
    "CO2": "CO2 (EUA)",
}


def etiqueta(variable: str) -> str:
    if variable in ETIQUETAS:
        return ETIQUETAS[variable]
    if variable.startswith("mes_"):
        return f"Futuro {variable[4:]}"
    return variable


def fmt_numero(valor, decimales=2) -> str:
    if valor is None:
        return "—"
    texto = f"{valor:,.{decimales}f}"
    # Formato español: punto de miles, coma decimal
    return texto.replace(",", "§").replace(".", ",").replace("§", ".")


def cargar_logo_b64() -> str:
    """Lee el logo en PNG (no SVG: los clientes de email no soportan bien el
    SVG inline, y llega a romper el email por completo) y lo codifica en
    base64 para incrustarlo como <img>, igual que Constantine."""
    path = os.path.join(ASSETS_DIR, "logo.png")
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def cargar_constantine_b64() -> str:
    """Lee la mascota (pose 'casual', con café) y la codifica en base64 para
    poder incrustarla directamente en el HTML del email."""
    path = os.path.join(ASSETS_DIR, "constantine_casual.png")
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def variacion_celda(fila: dict, fecha_iso_hoy: str) -> str:
    abs_ = fila.get("variacion_abs")
    pct = fila.get("variacion_pct")
    if abs_ is None or pct is None:
        html = '<span style="color:#A49FC6;font-size:13px;">Sin dato previo</span>'
    else:
        positivo = abs_ >= 0
        # Hot Pink (#FF039F) para bajadas: acento de alto contraste que usa
        # el equipo en reportes operativos internos (más contundente que el
        # Soho Lights estándar, pensado para marketing/cliente).
        color = "#06F0FB" if positivo else "#FF039F"
        flecha = "▲" if positivo else "▼"
        signo = "+" if positivo else ""
        html = (
            f'<span style="color:{color};font-weight:700;">'
            f"{flecha} {signo}{fmt_numero(abs_)}<br>({signo}{fmt_numero(pct, 1)}%)</span>"
        )

    fecha_dato = fila.get("fecha_dato")  # formato DD-MM-YYYY
    if fecha_dato:
        try:
            dd, mm, yyyy = fecha_dato.split("-")
            iso_dato = f"{yyyy}-{mm}-{dd}"
        except ValueError:
            iso_dato = None
        if iso_dato and iso_dato != fecha_iso_hoy:
            # La fuente (p.ej. EEX en fin de semana/festivo) puede no tener
            # subasta ese día; dejamos claro de qué día es realmente el dato.
            html += f'<br><span style="color:#A49FC6;font-size:11px;">dato del {dd}/{mm}</span>'

    return html


def construir_seccion(nombre: str, filas: list) -> str:
    filas_html = ""
    for fila in filas:
        valor_txt = fmt_numero(fila["valor"]) if fila["valor"] is not None else "—"
        filas_html += f"""
        <tr style="border-bottom:1px solid rgba(88,64,255,0.2);">
          <td style="padding:11px 10px;color:#DCDDFF;font-size:15px;width:{LABEL_WIDTH_PCT}%;">{etiqueta(fila['variable'])}</td>
          <td style="padding:11px 8px;color:#FCFFFF;font-size:16px;font-weight:700;text-align:right;white-space:nowrap;width:{VALUE_WIDTH_PCT}%;">{valor_txt}</td>
          <td style="padding:11px 10px;font-size:14px;text-align:right;width:{VARIACION_WIDTH_PCT}%;">{variacion_celda(fila, fila.get('_fecha_iso_hoy'))}</td>
        </tr>"""

    return f"""
    <tr>
      <td style="padding:22px 24px 6px 24px;">
        <table role="presentation" cellpadding="0" cellspacing="0"><tr>
          <td style="width:4px;height:14px;background:#FF039F;border-radius:2px;"></td>
          <td style="padding-left:8px;color:#DCDDFF;font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;">{nombre}</td>
        </tr></table>
      </td>
    </tr>
    <tr>
      <td style="padding:0 24px 8px 24px;">
        <table width="100%" cellpadding="0" cellspacing="0" style="background:rgba(45,26,131,0.6);border:1px solid rgba(88,64,255,0.3);border-radius:12px;overflow:hidden;table-layout:fixed;">
          <colgroup>
            <col style="width:{LABEL_WIDTH_PCT}%;">
            <col style="width:{VALUE_WIDTH_PCT}%;">
            <col style="width:{VARIACION_WIDTH_PCT}%;">
          </colgroup>
          {filas_html}
        </table>
      </td>
    </tr>"""


def construir_html(resultado: dict) -> str:
    fecha_iso = resultado["fecha"]
    fecha_fmt = datetime.fromisoformat(fecha_iso).strftime("%d/%m/%Y")
    filas = resultado.get("filas", [])

    # Le pasamos la fecha de hoy a cada fila para poder detectar desfases
    # (ver variacion_celda) sin cambiar la firma de construir_seccion.
    for fila in filas:
        fila["_fecha_iso_hoy"] = fecha_iso

    grupos = {}
    for fila in filas:
        grupos.setdefault(fila["fuente"], []).append(fila)

    orden_fuentes = ["OMIE", "MIBGAS", "OMIP", "Yahoo", "EEX"]
    secciones_html = "".join(
        construir_seccion(fuente, grupos[fuente]) for fuente in orden_fuentes if fuente in grupos
    )

    logo_b64 = cargar_logo_b64()
    constantine_b64 = cargar_constantine_b64()

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<title>Radar de mercados energéticos</title>
</head>
<body style="margin:0;padding:0;background:#0D0030;font-family:'Montserrat',Arial,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#0D0030;padding:24px 0;">
    <tr>
      <td align="center">
        <table role="presentation" width="640" cellpadding="0" cellspacing="0" style="max-width:640px;width:100%;background:linear-gradient(135deg,#18004A 0%,#0D0030 100%);border-radius:16px;overflow:hidden;">
          <tr>
            <td style="padding:26px 32px 18px 32px;border-bottom:1px solid rgba(88,64,255,0.25);">
              <table width="100%" role="presentation"><tr>
                <td><img src="data:image/png;base64,{logo_b64}" alt="octopus energy" style="height:26px;width:auto;display:block;border:0;"></td>
                <td align="right" style="color:#A49FC6;font-size:13px;vertical-align:middle;">{fecha_fmt}</td>
              </tr></table>
              <div style="width:56px;height:3px;background:linear-gradient(90deg,#5840FF 0%,#FF039F 100%);margin-top:16px;"></div>
            </td>
          </tr>
          <tr>
            <td style="padding:22px 32px 4px 32px;">
              <h1 style="color:#FCFFFF;font-size:21px;font-weight:700;margin:0 0 4px 0;">Radar diario de mercados energéticos</h1>
              <p style="color:#DCDDFF;font-size:14px;margin:0;">OMIE · MIBGAS · OMIP · Brent · TTF, con variación respecto al día anterior</p>
            </td>
          </tr>
          {secciones_html}
          <tr>
            <td style="padding:20px 32px 32px 32px;position:relative;">
              <p style="color:#A49FC6;font-size:12px;margin:0;line-height:1.5;">
                Generado automáticamente cada día a las 7:00h.<br>
                Fuentes: omie.es · mibgas.es · omip.pt · Yahoo Finance · eex.com.
              </p>
              <img src="data:image/png;base64,{constantine_b64}" style="position:absolute;right:20px;bottom:6px;width:64px;height:auto;" alt="Constantine">
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


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
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Radar de mercados energéticos – {fecha_fmt}"
    msg["From"] = GMAIL_USER
    msg["To"] = ", ".join(DESTINATARIOS)
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, DESTINATARIOS, msg.as_string())
    print(f"[OK] Email enviado desde {GMAIL_USER} a {', '.join(DESTINATARIOS)}")


def main():
    fecha_iso = sys.argv[1] if len(sys.argv) > 1 else datetime.now(MADRID_TZ).date().isoformat()

    resultado = cargar_snapshot(fecha_iso)
    if resultado is None:
        print(f"[AVISO] No existe snapshot para {fecha_iso}. No se envía email.")
        return

    html = construir_html(resultado)
    enviar(html, fecha_iso)


if __name__ == "__main__":
    main()
