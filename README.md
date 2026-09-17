# Radar diario de mercados energéticos (OMIE · MIBGAS · OMIP · Brent/TTF/CO2)

Scraper que se ejecuta automáticamente **todos los días a las 7:00h hora de España**
y guarda un snapshot de precios en este mismo repositorio (carpeta `data/`).

## Qué recoge

| Fuente | Datos |
|---|---|
| **OMIE** | Precio medio, máximo, mínimo y volumen del mercado diario español (`omie.es/es/spot-hoy`) |
| **MIBGAS** | Precio PVB D+1 del gas (`mibgas.es/es/market-results`) |
| **OMIP** | Spot SPEL BASE, futuros Q4-26, YR-27 ("Cal-27"), YR-28 ("Cal-28") y los meses en curva (rolling 3-6 meses) (`omip.pt/es/plazo-hoy`) |
| **Brent / TTF / CO2** | Precio actual y cierre anterior (Investing.com) |

## Puesta en marcha (una sola vez)

1. Crea un repositorio nuevo en GitHub (puede ser privado) y sube todo el contenido
   de esta carpeta tal cual (incluyendo `.github/workflows/daily-scrape.yml`).
2. En GitHub, ve a **Settings → Actions → General → Workflow permissions** y marca
   **"Read and write permissions"** (necesario para que el workflow pueda hacer
   commit de los datos nuevos).
3. Ve a la pestaña **Actions** del repo y lanza el workflow manualmente una vez
   ("Run workflow") para comprobar que todo funciona antes de esperar al cron
   automático.
4. A partir de ahí, se ejecutará solo cada día. Los resultados aparecerán en:
   - `data/YYYY-MM-DD.json` → snapshot completo de ese día
   - `data/history.csv` → histórico acumulado (una fila por variable y día)

## Ejecutarlo en local (para probar o depurar)

```bash
pip install -r requirements.txt
python scrape_markets.py --force
```

El flag `--force` salta la comprobación de "son las 7:00h en Madrid" para que
puedas probarlo a cualquier hora.

## Si algo deja de funcionar

Estas 4 webs son públicas pero pueden cambiar su HTML sin avisar. Si un campo
sale como `null` en el JSON:

1. Abre la URL correspondiente en el navegador y compara el texto visible con
   las expresiones regulares de `scrape_markets.py` (cada función `scrape_*`
   tiene comentarios explicando qué texto busca).
2. Ajusta la regex afectada y vuelve a lanzar `python scrape_markets.py --force`
   en local para comprobar que ya extrae el valor correcto.
3. Haz commit y push del cambio; el próximo cron ya usará la versión corregida.

## Notas importantes

- **OMIP**: la nomenclatura oficial de OMIP usa **YR** (Year) en vez de "Cal"
  para los contratos anuales. `YR-27` = lo que coloquialmente llamamos "Cal-27".
- **MIBGAS**: el precio "PVB D+1" se localiza buscando la fecha de mañana dentro
  del bloque "Diario" de la tabla pública. Si algún día la fecha no coincide
  (por ejemplo festivos o cambios de formato), el script coge automáticamente
  la segunda entrada del bloque como fallback.
- **Horario**: GitHub Actions cron va siempre en UTC y no sabe de cambios de
  hora. Por eso el workflow dispara dos veces (5:00 y 6:00 UTC) y es el propio
  script quien decide, mirando el reloj de verdad en `Europe/Madrid`, si debe
  ejecutarse o no.
