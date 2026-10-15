# GaaInversiones

Laboratorio de trading automático de criptomonedas en Python. El objetivo no es tener
una estrategia ganadora de partida, sino una infraestructura fiable para **probar
estrategias y descartar las malas antes de arriesgar dinero**: datos limpios, backtest
honesto, paper trading realista y, solo si los resultados lo justifican, operativa real.

> Estado: **fase 1 de 6** (configuración, logging, datos e indicadores).

## Requisitos

- Python 3.11 o superior
- Git

## Instalación

```bash
git clone <repo> gaaInversiones
cd gaaInversiones
python -m venv .venv
# Windows:      .venv\Scripts\activate
# Linux/macOS:  source .venv/bin/activate
pip install -e ".[dev]"      # desarrollo (incluye pytest, ruff, mypy)
# o, en un servidor:  pip install -r requirements.txt && pip install -e . --no-deps
cp .env.example .env         # y rellénalo
```

## Configuración

- **`config/config.yaml`**: todos los parámetros (mercado, datos, estrategia, riesgo,
  costes, logging). Se validan al arrancar; un valor fuera de rango o un campo mal
  escrito detiene el programa indicando qué está mal. `gaa config` muestra la
  configuración efectiva.
- **`.env`**: solo secretos (claves de API, Telegram) y el cerrojo del modo real.
  Está en `.gitignore`; nunca se sube al repositorio. Los secretos nunca se imprimen:
  el logging los sustituye por `***`, también en trazas de error y en mensajes de
  librerías de terceros.

### Claves de API: SIN permiso de retirada

En Kraken (*Settings → API → Create API key*) marca solo:

- Consultar fondos (*Query Funds*)
- Consultar órdenes y operaciones abiertas y cerradas (*Query Open/Closed Orders & Trades*)
- Crear y modificar órdenes (*Create & Modify Orders*)
- Cancelar órdenes (*Cancel & Close Orders*)

**Nunca** marques *Withdraw Funds* (retirar) ni permisos de depósito. Si el bot corre en
un servidor con IP fija, restringe la clave a esa IP. Así, aunque alguien robara la
clave, no podría sacar dinero de la cuenta.

En modo paper no hacen falta claves: los precios se leen de la API pública.

### Modo real (doble cerrojo)

El modo por defecto es `paper`. Para operar con dinero real hacen falta las tres cosas:
`modo: real` en `config.yaml`, `GAA_PERMITIR_REAL=si` en `.env` y una confirmación manual
al arrancar (fase 5).

## Datos históricos

```bash
gaa datos descargar                  # completa la caché desde datos.desde hasta la última vela cerrada
gaa datos descargar --fuente kraken  # usa otro exchange como fuente
gaa datos importar-kraken XBTEUR_240.csv
gaa datos verificar                  # extensión, huecos y problemas de calidad de cada serie
```

- Las velas se guardan en `data/velas.sqlite`, una serie por (exchange, símbolo,
  temporalidad). Volver a descargar solo pide lo que falta; la clave primaria impide
  duplicados.
- Solo se guardan **velas cerradas**: la vela en curso se descarta siempre.
- **Por qué Bitstamp por defecto:** la API de velas de Kraken solo devuelve las últimas
  720 velas (unos 4 meses en 4h), así que no sirve para años de historia. Bitstamp
  ofrece BTC/EUR desde 2016 por API. Comparado con Kraken en las 720 velas comunes, la
  diferencia mediana de cierre es de 1 pb y la correlación de rendimientos de 0,999.
- **Para usar datos de Kraken:** descarga el histórico OHLCVT oficial desde la web de
  soporte de Kraken (*Downloadable historical OHLCVT data*), impórtalo con
  `gaa datos importar-kraken` (la temporalidad se deduce del nombre: `_240` = 4h), pon
  `fuente_historica: kraken` y `gaa datos descargar` lo mantendrá al día con la API
  (siempre que no pasen más de 720 velas entre actualizaciones).
- Los **huecos** (periodos sin velas) se detectan y se informan. Al cargar datos para un
  backtest se rellenan con velas planas de volumen 0 marcadas como `sintetica`.

## Tests

```bash
pytest              # sin red: exchange falso y datos sintéticos
pytest --red        # incluye pruebas contra las APIs reales de Bitstamp y Kraken
ruff check src tests && mypy
```

## Estructura

```
config/config.yaml        parámetros
src/gaainversiones/
  config.py               carga y validación (pydantic) + secretos
  registro.py             logging consola + fichero rotativo, UTC, secretos enmascarados
  tiempo.py               temporalidades y fechas (UTC, milisegundos)
  exchange.py             clientes ccxt y reintentos ante errores de red
  indicadores.py          SMA, EMA, RSI y ATR (Wilder), causales
  datos/                  fuentes (ccxt, CSV Kraken), caché SQLite, calidad, servicio
  cli.py                  comando `gaa`
tests/
```

## Hoja de ruta

1. ✅ Estructura, configuración, datos e indicadores
2. Estrategia de ejemplo, capa de riesgo y motor de backtest con informe HTML
3. PaperBroker, estado persistente y bucle en vivo en modo paper
4. Alertas por Telegram y kill switch
5. RealBroker (desactivado) con tests sobre un exchange simulado
6. Despliegue 24/7 (systemd / Docker) y lista de comprobación previa a operar en real
