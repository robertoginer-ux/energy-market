"""
Envía el email diario con los datos de mercados y su variación, con el
diseño de marca de Octopus Energy España (fondo oscuro, Montserrat, acentos
Voltage/Soho Lights).

Requiere la variable de entorno GMAIL_APP_PASSWORD (contraseña de aplicación
de roberto.giner@octoenergy.com). El destinatario es el grupo de
distribución smt_spain@octoenergy.com.

Uso:
    python send_email.py [FECHA_ISO]

Si no se pasa FECHA_ISO (formato YYYY-MM-DD), usa el día de hoy en Madrid.
Lee el snapshot data/<FECHA_ISO>.json generado por scrape_markets.py — si no
existe, termina sin error, simplemente avisando.
"""

import json
import os
import smtplib
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

MADRID_TZ = ZoneInfo("Europe/Madrid")
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

GMAIL_USER = "roberto.giner@octoenergy.com"  # remitente real (cuenta de Gmail con contraseña de aplicación)
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
DESTINATARIO = "smt_spain@octoenergy.com"  # grupo de distribución (Google Group), no una cuenta con login

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


def variacion_celda(fila: dict) -> str:
    abs_ = fila.get("variacion_abs")
    pct = fila.get("variacion_pct")
    if abs_ is None or pct is None:
        return '<span style="color:#A49FC6;font-size:12px;">Sin dato previo</span>'
    positivo = abs_ >= 0
    color = "#06F0FB" if positivo else "#F05DFB"
    flecha = "▲" if positivo else "▼"
    signo = "+" if positivo else ""
    return (
        f'<span style="color:{color};font-weight:600;white-space:nowrap;">'
        f"{flecha} {signo}{fmt_numero(abs_)} ({signo}{fmt_numero(pct, 1)}%)</span>"
    )


def construir_html(resultado: dict) -> str:
    fecha_iso = resultado["fecha"]
    fecha_fmt = datetime.fromisoformat(fecha_iso).strftime("%d/%m/%Y")
    filas = resultado.get("filas", [])

    # Agrupamos por fuente para que el email tenga secciones claras
    grupos = {}
    for fila in filas:
        grupos.setdefault(fila["fuente"], []).append(fila)

    orden_fuentes = ["OMIE", "MIBGAS", "OMIP", "Yahoo"]
    secciones_html = ""
    for fuente in orden_fuentes:
        if fuente not in grupos:
            continue
        filas_html = ""
        for fila in grupos[fuente]:
            filas_html += f"""
            <tr style="border-bottom:1px solid rgba(88,64,255,0.2);">
              <td style="padding:9px 12px;color:#DCDDFF;font-size:13px;">{etiqueta(fila['variable'])}</td>
              <td style="padding:9px 12px;color:#FCFFFF;font-size:14px;font-weight:600;text-align:right;">{fmt_numero(fila['valor']) if fila['valor'] is not None else '—'}</td>
              <td style="padding:9px 12px;font-size:12px;text-align:right;">{variacion_celda(fila)}</td>
            </tr>"""

        secciones_html += f"""
        <tr>
          <td style="padding:20px 24px 6px 24px;">
            <div style="color:#A49FC6;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.08em;">{fuente}</div>
          </td>
        </tr>
        <tr>
          <td style="padding:0 24px 8px 24px;">
            <table width="100%" cellpadding="0" cellspacing="0" style="background:rgba(45,26,131,0.6);border:1px solid rgba(88,64,255,0.25);border-radius:12px;overflow:hidden;">
              {filas_html}
            </table>
          </td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;500;600;700&display=swap" rel="stylesheet">
<title>Radar de mercados energéticos</title>
</head>
<body style="margin:0;padding:0;background:#0D0030;font-family:'Montserrat',Arial,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#0D0030;padding:24px 0;">
    <tr>
      <td align="center">
        <table role="presentation" width="640" cellpadding="0" cellspacing="0" style="max-width:640px;width:100%;background:linear-gradient(135deg,#18004A 0%,#0D0030 100%);border-radius:16px;overflow:hidden;">
          <tr>
            <td style="padding:28px 32px 18px 32px;border-bottom:1px solid rgba(88,64,255,0.25);">
              <table width="100%" role="presentation"><tr>
                <td style="color:#FCFFFF;font-size:20px;font-weight:700;">octopus energy</td>
                <td align="right" style="color:#A49FC6;font-size:12px;">{fecha_fmt}</td>
              </tr></table>
              <div style="width:56px;height:2px;background:#06F0FB;margin-top:14px;"></div>
            </td>
          </tr>
          <tr>
            <td style="padding:22px 32px 4px 32px;">
              <h1 style="color:#FCFFFF;font-size:19px;font-weight:600;margin:0 0 4px 0;">Radar diario de mercados energéticos</h1>
              <p style="color:#DCDDFF;font-size:13px;margin:0;">OMIE · MIBGAS · OMIP · Brent · TTF, con variación respecto al día anterior</p>
            </td>
          </tr>
          {secciones_html}
          <tr>
            <td style="padding:18px 32px 28px 32px;">
              <p style="color:#A49FC6;font-size:11px;margin:0;line-height:1.5;">
                Generado automáticamente cada día a las 7:00h.<br>
                Fuentes: omie.es · mibgas.es · omip.pt · Yahoo Finance.
              </p>
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
    msg["To"] = DESTINATARIO
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, [DESTINATARIO], msg.as_string())
    print(f"[OK] Email enviado desde {GMAIL_USER} a {DESTINATARIO}")


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
