"""
Actualiza el Google Sheet de seguimiento de mercados con los datos del día
(valor + variación respecto al día anterior).

Requiere la variable de entorno GOOGLE_SERVICE_ACCOUNT_JSON con el contenido
completo del JSON de la cuenta de servicio de Google Cloud.

Uso:
    python update_google_sheet.py [FECHA_ISO]

Si no se pasa FECHA_ISO (formato YYYY-MM-DD), usa el día de hoy en Madrid.
Lee el snapshot data/<FECHA_ISO>.json generado por scrape_markets.py — si no
existe (por ejemplo porque el scraper no llegó a ejecutarse a las 7:00h),
termina sin error, simplemente avisando.
"""

import json
import os
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

MADRID_TZ = ZoneInfo("Europe/Madrid")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# ID del Google Sheet (de la URL: /spreadsheets/d/<ID>/edit)
SPREADSHEET_ID = "1dP12qfP1lUvlGEPcHtKKH_p7gh6hbamBzCosyTQF7LE"

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DATE_RE = re.compile(r"^\d{2}-\d{2}-\d{4}$")


def col_to_letter(idx: int) -> str:
    """0 -> 'A', 1 -> 'B', ..., 25 -> 'Z', 26 -> 'AA', ..."""
    idx += 1
    letters = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def fmt_variacion(fila: dict) -> str:
    abs_ = fila.get("variacion_abs")
    pct = fila.get("variacion_pct")
    if abs_ is None or pct is None:
        return ""
    signo = "+" if abs_ >= 0 else ""
    return f"{signo}{abs_:.2f} ({signo}{pct:.1f}%)"


def get_service():
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds)


def main():
    fecha_iso = sys.argv[1] if len(sys.argv) > 1 else datetime.now(MADRID_TZ).date().isoformat()

    snapshot_path = os.path.join(DATA_DIR, f"{fecha_iso}.json")
    if not os.path.exists(snapshot_path):
        print(f"[AVISO] No existe {snapshot_path} (el scraper no llegó a ejecutarse hoy). Nada que actualizar.")
        return

    with open(snapshot_path, encoding="utf-8") as f:
        resultado = json.load(f)
    filas = resultado.get("filas", [])
    if not filas:
        print("[AVISO] El snapshot no tiene filas. Nada que actualizar.")
        return

    fecha_ddmmyyyy = datetime.fromisoformat(fecha_iso).strftime("%d-%m-%Y")

    service = get_service()

    meta = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    sheet_title = meta["sheets"][0]["properties"]["title"]

    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=SPREADSHEET_ID, range=f"'{sheet_title}'!A1:ZZ500")
        .execute()
    )
    grid = result.get("values", [])

    # 1) Localizar la fila de cabecera (la que contiene fechas DD-MM-YYYY)
    header_row_idx = None
    for i, row in enumerate(grid):
        if any(DATE_RE.match(cell) for cell in row if cell):
            header_row_idx = i
            break
    if header_row_idx is None:
        raise RuntimeError(
            "No se encontró ninguna fila con fechas (formato DD-MM-YYYY) en el Sheet. "
            "Revisa que la estructura no haya cambiado."
        )
    header = grid[header_row_idx]

    updates = []  # [{"range": ..., "values": [[valor]]}, ...]

    # 2) Localizar (o crear) la columna de hoy
    col_valor = next((j for j, cell in enumerate(header) if cell == fecha_ddmmyyyy), None)
    if col_valor is None:
        col_valor = len(header)
        col_variacion = col_valor + 1
        updates.append(
            {"range": f"'{sheet_title}'!{col_to_letter(col_valor)}{header_row_idx + 1}", "values": [[fecha_ddmmyyyy]]}
        )
        updates.append(
            {"range": f"'{sheet_title}'!{col_to_letter(col_variacion)}{header_row_idx + 1}", "values": [["variación"]]}
        )
        print(f"[INFO] La fecha {fecha_ddmmyyyy} no existía en el Sheet; se añaden columnas nuevas.")
    else:
        col_variacion = col_valor + 1

    # 3) Localizar filas existentes por (fuente, variable)
    filas_indice = {}
    ultima_fila_con_datos = header_row_idx
    for i in range(header_row_idx + 1, len(grid)):
        row = grid[i]
        if len(row) >= 2 and row[0] and row[1]:
            filas_indice[(row[0], row[1])] = i
            ultima_fila_con_datos = i
        elif row and row[0]:
            ultima_fila_con_datos = i

    siguiente_fila_nueva = max(ultima_fila_con_datos, len(grid) - 1) + 1

    # 4) Preparar las celdas a escribir
    for fila in filas:
        clave = (fila["fuente"], fila["variable"])
        if clave in filas_indice:
            fila_idx = filas_indice[clave]
        else:
            fila_idx = siguiente_fila_nueva
            siguiente_fila_nueva += 1
            updates.append({"range": f"'{sheet_title}'!A{fila_idx + 1}", "values": [[fila["fuente"]]]})
            updates.append({"range": f"'{sheet_title}'!B{fila_idx + 1}", "values": [[fila["variable"]]]})
            print(f"[INFO] Fila nueva añadida para {fila['fuente']} / {fila['variable']}")

        if fila.get("valor") is not None:
            updates.append(
                {"range": f"'{sheet_title}'!{col_to_letter(col_valor)}{fila_idx + 1}", "values": [[fila["valor"]]]}
            )
        variacion_texto = fmt_variacion(fila)
        if variacion_texto:
            updates.append(
                {
                    "range": f"'{sheet_title}'!{col_to_letter(col_variacion)}{fila_idx + 1}",
                    "values": [[variacion_texto]],
                }
            )

    if not updates:
        print("[AVISO] No había nada que actualizar.")
        return

    service.spreadsheets().values().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"valueInputOption": "USER_ENTERED", "data": updates},
    ).execute()
    print(f"[OK] {len(updates)} celdas actualizadas en el Google Sheet para el {fecha_ddmmyyyy}")


if __name__ == "__main__":
    main()
