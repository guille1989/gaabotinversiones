"""Informes HTML de backtest y walk-forward, y tablas de métricas para la consola.

Cada informe es un único fichero HTML con gráficos interactivos (Plotly). La librería
de gráficos se guarda una sola vez como ``plotly.min.js`` en la carpeta de informes:
los informes funcionan sin conexión mientras sigan en esa carpeta.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.graph_objects as go
from jinja2 import Environment
from plotly.offline import get_plotlyjs

from gaainversiones import __version__
from gaainversiones.backtest.metricas import Metricas, metricas_resultado
from gaainversiones.backtest.motor import ResultadoBacktest
from gaainversiones.backtest.walkforward import ResultadoOptimizacion, ResultadoWalkForward
from gaainversiones.config import Config
from gaainversiones.cuenta import Operacion
from gaainversiones.formato import formato_entero, formato_eur, formato_numero, formato_pct

COLOR_ESTRATEGIA = "#2563eb"
COLOR_REFERENCIA = "#9ca3af"
COLOR_COMPRA = "#16a34a"
COLOR_VENTA = "#dc2626"


FILAS_METRICAS: list[tuple[str, str, Any]] = [
    ("capital_inicial", "Capital inicial", formato_eur),
    ("capital_final", "Capital final", formato_eur),
    ("rentabilidad_total", "Rentabilidad total", formato_pct),
    ("cagr", "Rentabilidad anual (CAGR)", formato_pct),
    ("caida_maxima", "Caída máxima", formato_pct),
    ("sharpe", "Sharpe", formato_numero),
    ("sortino", "Sortino", formato_numero),
    ("calmar", "Calmar (CAGR / caída)", formato_numero),
    ("operaciones", "Operaciones", formato_entero),
    ("pct_ganadoras", "% ganadoras", formato_pct),
    ("profit_factor", "Profit factor", formato_numero),
    ("ganancia_media", "Ganancia media", formato_eur),
    ("perdida_media", "Pérdida media", formato_eur),
    ("exposicion", "Tiempo invertido", formato_pct),
    ("comisiones", "Comisiones pagadas", formato_eur),
]


# Métricas que solo tienen sentido con operaciones (no para comprar y mantener).
_POR_OPERACION = {
    "operaciones",
    "pct_ganadoras",
    "profit_factor",
    "ganancia_media",
    "perdida_media",
    "comisiones",
}


def tabla_metricas(columnas: Mapping[str, Metricas | None]) -> list[list[str]]:
    """Filas [etiqueta, valor columna 1, valor columna 2, ...] ya formateadas."""
    filas = []
    for clave, etiqueta, formato in FILAS_METRICAS:
        fila = [etiqueta]
        for metricas in columnas.values():
            if metricas is None or (metricas.referencia and clave in _POR_OPERACION):
                fila.append("—")
            else:
                fila.append(formato(metricas.valor(clave)))
        filas.append(fila)
    return filas


def tabla_texto(columnas: Mapping[str, Metricas | None]) -> str:
    """Tabla de métricas alineada para la consola."""
    cabecera = ["", *columnas]
    filas = [cabecera, *tabla_metricas(columnas)]
    anchos = [max(len(f[i]) for f in filas) for i in range(len(cabecera))]
    lineas = []
    for n, fila in enumerate(filas):
        celdas = [fila[0].ljust(anchos[0])] + [
            c.rjust(a) for c, a in zip(fila[1:], anchos[1:], strict=True)
        ]
        lineas.append("  ".join(celdas))
        if n == 0:
            lineas.append("  ".join("-" * a for a in anchos))
    return "\n".join(lineas)


# --- Gráficos ------------------------------------------------------------------------------


def _diaria(serie: pd.Series) -> pd.Series:
    """Último valor de cada día: suficiente para los gráficos y mucho más ligero."""
    return serie.resample("1D").last().dropna()


def _layout(figura: go.Figure, titulo: str, altura: int = 420) -> go.Figure:
    figura.update_layout(
        title={"text": titulo, "x": 0, "xanchor": "left", "font": {"size": 16}},
        template="plotly_white",
        height=altura,
        margin={"l": 60, "r": 20, "t": 50, "b": 40},
        legend={"orientation": "h", "y": 1.02, "x": 1, "xanchor": "right", "yanchor": "bottom"},
        hovermode="x unified",
        font={"family": "system-ui, -apple-system, Segoe UI, Roboto, sans-serif"},
    )
    return figura


def figura_capital(
    equity: pd.Series, referencia: pd.Series, nombre: str = "Estrategia"
) -> go.Figure:
    figura = go.Figure()
    figura.add_scatter(
        x=_diaria(referencia).index,
        y=_diaria(referencia),
        name="Comprar y mantener",
        line={"color": COLOR_REFERENCIA, "width": 1.5},
    )
    figura.add_scatter(
        x=_diaria(equity).index,
        y=_diaria(equity),
        name=nombre,
        line={"color": COLOR_ESTRATEGIA, "width": 2},
    )
    figura.update_yaxes(type="log", title="EUR (escala logarítmica)")
    figura.update_layout(
        updatemenus=[
            {
                "type": "buttons",
                "direction": "left",
                "x": 1,
                "y": 1.14,
                "xanchor": "right",
                "showactive": True,
                "buttons": [
                    {"label": "Logarítmica", "method": "relayout", "args": [{"yaxis.type": "log"}]},
                    {"label": "Lineal", "method": "relayout", "args": [{"yaxis.type": "linear"}]},
                ],
            }
        ]
    )
    _layout(figura, "Curva de capital", 480)
    # Leyenda debajo: arriba a la derecha van los botones de escala.
    figura.update_layout(legend={"y": -0.08, "yanchor": "top", "x": 0, "xanchor": "left"})
    return figura


def figura_caidas(
    equity: pd.Series, referencia: pd.Series, nombre: str = "Estrategia"
) -> go.Figure:
    figura = go.Figure()
    for serie, etiqueta, color in (
        (referencia, "Comprar y mantener", COLOR_REFERENCIA),
        (equity, nombre, COLOR_ESTRATEGIA),
    ):
        diaria = _diaria(serie)
        caida = (diaria / diaria.cummax() - 1) * 100
        figura.add_scatter(
            x=caida.index, y=caida, name=etiqueta, fill="tozeroy", line={"color": color, "width": 1}
        )
    figura.update_yaxes(title="% desde el máximo", ticksuffix=" %")
    return _layout(figura, "Caídas desde el máximo", 300)


def figura_operaciones(precio: pd.Series, operaciones: Sequence[Operacion]) -> go.Figure:
    figura = go.Figure()
    diario = _diaria(precio)
    figura.add_scatter(
        x=diario.index, y=diario, name="Precio de cierre", line={"color": "#374151", "width": 1}
    )
    if operaciones:
        figura.add_scatter(
            x=[op.fecha_entrada for op in operaciones],
            y=[op.precio_entrada for op in operaciones],
            mode="markers",
            name="Compra",
            marker={"symbol": "triangle-up", "size": 9, "color": COLOR_COMPRA},
            hovertemplate="Compra %{y:,.2f} €<extra></extra>",
        )
        figura.add_scatter(
            x=[op.fecha_salida for op in operaciones],
            y=[op.precio_salida for op in operaciones],
            mode="markers",
            name="Venta",
            marker={"symbol": "triangle-down", "size": 9, "color": COLOR_VENTA},
            text=[
                f"{op.motivo_salida} · {formato_pct(op.rentabilidad)} · {formato_eur(op.resultado)}"
                for op in operaciones
            ],
            hovertemplate="Venta %{y:,.2f} €<br>%{text}<extra></extra>",
        )
    figura.update_yaxes(type="log", title="EUR (escala logarítmica)")
    return _layout(figura, "Precio y operaciones", 420)


# --- HTML ----------------------------------------------------------------------------------

PLANTILLA = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ titulo }}</title>
<style>
  :root {
    --fondo: #f6f7f9; --panel: #ffffff; --texto: #111827; --suave: #6b7280;
    --borde: #e5e7eb; --acento: #2563eb; --aviso-fondo: #fff7ed; --aviso-borde: #fdba74;
    --bien: #15803d; --mal: #b91c1c;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--fondo); color: var(--texto);
         font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  main { max-width: 1200px; margin: 0 auto; padding: 24px 16px 64px; }
  h1 { font-size: 26px; margin: 0 0 4px; }
  h2 { font-size: 18px; margin: 32px 0 12px; }
  .meta { color: var(--suave); margin: 0 0 16px; }
  .aviso { background: var(--aviso-fondo); border: 1px solid var(--aviso-borde);
           border-radius: 8px; padding: 10px 14px; margin: 12px 0; }
  .tarjetas { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; }
  .tarjeta { background: var(--panel); border: 1px solid var(--borde); border-radius: 10px; padding: 14px; }
  .tarjeta .etiqueta { color: var(--suave); font-size: 13px; }
  .tarjeta .valor { font-size: 24px; font-weight: 600; margin-top: 2px; font-variant-numeric: tabular-nums; }
  .tarjeta .ref { color: var(--suave); font-size: 13px; }
  .panel { background: var(--panel); border: 1px solid var(--borde); border-radius: 10px;
           padding: 8px; margin-bottom: 16px; overflow-x: auto; }
  table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
  th, td { padding: 6px 10px; border-bottom: 1px solid var(--borde); text-align: right; white-space: nowrap; }
  th:first-child, td:first-child { text-align: left; }
  thead th { color: var(--suave); font-weight: 600; font-size: 13px; position: sticky; top: 0; background: var(--panel); }
  .scroll { max-height: 480px; overflow: auto; }
  .positivo { color: var(--bien); } .negativo { color: var(--mal); }
  th.texto, td.texto { text-align: left; white-space: normal; min-width: 180px; }
  ul.notas { color: var(--suave); padding-left: 20px; }
  footer { color: var(--suave); font-size: 13px; margin-top: 40px; }
  code { background: #f3f4f6; padding: 1px 5px; border-radius: 4px; }
</style>
{{ plotly_js | safe }}
</head>
<body>
<main>
  <h1>{{ titulo }}</h1>
  <p class="meta">{{ subtitulo }}</p>
  {% for aviso in avisos %}<div class="aviso">{{ aviso | safe }}</div>{% endfor %}

  <div class="tarjetas">
  {% for t in tarjetas %}
    <div class="tarjeta"><div class="etiqueta">{{ t.etiqueta }}</div>
      <div class="valor">{{ t.valor }}</div><div class="ref">{{ t.referencia }}</div></div>
  {% endfor %}
  </div>

  <h2>Métricas</h2>
  <div class="panel"><table>
    <thead><tr><th></th>{% for c in columnas %}<th>{{ c }}</th>{% endfor %}</tr></thead>
    <tbody>{% for fila in metricas %}<tr>{% for celda in fila %}<td>{{ celda }}</td>{% endfor %}</tr>{% endfor %}</tbody>
  </table></div>

  {% for g in graficos %}<div class="panel">{{ g | safe }}</div>{% endfor %}

  {% for tabla in tablas %}
  <h2>{{ tabla.titulo }}</h2>
  {% if tabla.nota %}<p class="meta">{{ tabla.nota }}</p>{% endif %}
  <div class="panel scroll"><table>
    <thead><tr>{% for c in tabla.columnas %}<th{% if loop.index0 in tabla.texto %} class="texto"{% endif %}>{{ c }}</th>{% endfor %}</tr></thead>
    <tbody>{% for fila in tabla.filas %}<tr>{% for celda in fila %}<td class="{{ celda.clase }}{% if loop.index0 in tabla.texto %} texto{% endif %}">{{ celda.texto }}</td>{% endfor %}</tr>{% else %}<tr><td colspan="{{ tabla.columnas | length }}">Sin datos</td></tr>{% endfor %}</tbody>
  </table></div>
  {% endfor %}

  <h2>Cómo se ha simulado</h2>
  <ul class="notas">{% for nota in notas %}<li>{{ nota | safe }}</li>{% endfor %}</ul>

  <footer>Generado el {{ generado }} UTC con GaaInversiones {{ version }}.
    Resultados simulados sobre datos pasados: no garantizan resultados futuros.</footer>
</main>
</body>
</html>
"""


def _celda(texto: str, valor: float | None = None) -> dict[str, str]:
    clase = ""
    if valor is not None and math.isfinite(valor) and valor != 0:
        clase = "positivo" if valor > 0 else "negativo"
    return {"texto": texto, "clase": clase}


def _tabla_operaciones(operaciones: Sequence[Operacion]) -> dict[str, Any]:
    filas = [
        [
            _celda(f"{op.fecha_entrada:%Y-%m-%d %H:%M}"),
            _celda(formato_eur(op.precio_entrada)),
            _celda(f"{op.fecha_salida:%Y-%m-%d %H:%M}"),
            _celda(formato_eur(op.precio_salida)),
            _celda(formato_numero(op.cantidad, 5)),
            _celda(formato_eur(op.comisiones)),
            _celda(formato_eur(op.resultado), op.resultado),
            _celda(formato_pct(op.rentabilidad, 2), op.rentabilidad),
            _celda(op.motivo_salida),
            _celda(formato_numero(op.duracion / pd.Timedelta(days=1), 1)),
        ]
        for op in operaciones
    ]
    return {
        "titulo": f"Operaciones ({len(operaciones)})",
        "nota": "Fechas en UTC. El resultado incluye las comisiones de entrada y salida.",
        "columnas": [
            "Entrada",
            "Precio",
            "Salida",
            "Precio",
            "Cantidad",
            "Comisiones",
            "Resultado",
            "Rentab.",
            "Motivo de salida",
            "Días",
        ],
        "texto": [8],
        "filas": filas,
    }


def _notas_metodo(config: Config) -> list[str]:
    c, r = config.costes, config.riesgo
    return [
        "Las señales se calculan al cierre de cada vela y se ejecutan a mercado en la "
        "<b>apertura de la vela siguiente</b>.",
        f"Cada ejecución a mercado paga comisión taker del {formato_pct(c.comision_taker, 2)} "
        f"y {formato_numero(c.deslizamiento_pb, 1)} pb de deslizamiento.",
        f"Tamaño: se arriesga el {formato_pct(r.riesgo_por_operacion)} del capital por operación "
        f"(costes incluidos), con stop a {formato_numero(r.sl_atr, 1)}×ATR({r.atr_periodo}) y "
        f"objetivo a {formato_numero(r.tp_atr, 1)}×ATR. Exposición máxima: "
        f"{formato_pct(r.exposicion_max, 0)} del capital.",
        "El stop salta dentro de la vela (vive en el exchange); si la vela abre por debajo, "
        "se vende a la apertura. El objetivo se comprueba al cierre y se vende en la apertura "
        "siguiente. Si en una vela se tocan ambos, cuenta el stop.",
        f"Pérdida diaria ≥ {formato_pct(r.perdida_diaria_max, 0)}: se cierra y se pausa hasta el "
        f"día siguiente. Caída ≥ {formato_pct(r.caida_max, 0)} desde el máximo: se cierra y el bot "
        + (
            "se detiene definitivamente."
            if config.backtest.detener_en_caida_max
            else "<b>NO</b> se detiene (desactivado para estudio)."
        ),
        f"Datos: velas de {config.mercado.temporalidad} de <code>{config.datos.fuente_historica}"
        f"</code>. Sharpe y Sortino con rendimientos diarios, 365 días/año y tasa libre de riesgo 0.",
    ]


def _avisos(didactica: bool) -> list[str]:
    if not didactica:
        return []
    return [
        "<b>Estrategia didáctica.</b> Sirve para probar la infraestructura, no para "
        "operar: no hay motivo para esperar que gane dinero."
    ]


def _escribir(ruta: Path, contexto: dict[str, Any]) -> Path:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    libreria = ruta.parent / "plotly.min.js"
    if not libreria.exists():
        libreria.write_text(get_plotlyjs(), encoding="utf-8")
    contexto.update(
        plotly_js='<script src="plotly.min.js"></script>',
        generado=datetime.now(UTC).strftime("%Y-%m-%d %H:%M"),
        version=__version__,
    )
    html = Environment(autoescape=True).from_string(PLANTILLA).render(**contexto)
    ruta.write_text(html, encoding="utf-8")
    return ruta


def _html_figura(figura: go.Figure) -> str:
    return str(
        figura.to_html(full_html=False, include_plotlyjs=False, config={"displaylogo": False})
    )


def _tarjetas(m: Metricas, ref: Metricas) -> list[dict[str, str]]:
    return [
        {
            "etiqueta": "Capital final",
            "valor": formato_eur(m.capital_final),
            "referencia": f"Comprar y mantener: {formato_eur(ref.capital_final)}",
        },
        {
            "etiqueta": "Rentabilidad anual",
            "valor": formato_pct(m.cagr),
            "referencia": f"Comprar y mantener: {formato_pct(ref.cagr)}",
        },
        {
            "etiqueta": "Caída máxima",
            "valor": formato_pct(m.caida_maxima),
            "referencia": f"Comprar y mantener: {formato_pct(ref.caida_maxima)}",
        },
        {
            "etiqueta": "Sharpe",
            "valor": formato_numero(m.sharpe),
            "referencia": f"Comprar y mantener: {formato_numero(ref.sharpe)}",
        },
        {
            "etiqueta": "Operaciones",
            "valor": formato_entero(m.operaciones),
            "referencia": f"{formato_pct(m.pct_ganadoras)} ganadoras",
        },
    ]


def columnas_backtest(
    resultado: ResultadoBacktest, inicio_oos: pd.Timestamp | None
) -> dict[str, Metricas | None]:
    """Métricas del periodo completo y, si procede, separadas en in-sample y out-of-sample."""
    total, total_ref = metricas_resultado(resultado)
    columnas: dict[str, Metricas | None] = {"Estrategia": total, "Comprar y mantener": total_ref}
    if inicio_oos is not None and resultado.inicio < inicio_oos < resultado.fin:
        dentro, _ = metricas_resultado(resultado, hasta=inicio_oos)
        fuera, fuera_ref = metricas_resultado(resultado, desde=inicio_oos)
        columnas["Estrategia IS"] = dentro
        columnas["Estrategia OOS"] = fuera
        columnas["Comprar y mantener OOS"] = fuera_ref
    return columnas


def informe_backtest(
    resultado: ResultadoBacktest, config: Config, ruta: Path, inicio_oos: pd.Timestamp | None
) -> Path:
    """Genera el informe HTML de un backtest."""
    columnas = columnas_backtest(resultado, inicio_oos)
    total, total_ref = columnas["Estrategia"], columnas["Comprar y mantener"]
    assert total is not None and total_ref is not None
    equity = resultado.curva["equity"]
    tablas = []
    if resultado.eventos:
        tablas.append(
            {
                "titulo": f"Eventos de riesgo ({len(resultado.eventos)})",
                "nota": "",
                "columnas": ["Fecha", "Evento"],
                "texto": [1],
                "filas": [[_celda(f"{f:%Y-%m-%d %H:%M}"), _celda(e)] for f, e in resultado.eventos],
            }
        )
    tablas.append(_tabla_operaciones(resultado.operaciones))
    avisos = _avisos(resultado.didactica)
    if "Estrategia IS" in columnas and inicio_oos is not None:
        avisos.append(
            f"IS (in-sample) = antes del {inicio_oos:%Y-%m-%d}; OOS (out-of-sample) = desde esa "
            "fecha. Si los parámetros se eligieron mirando el IS, solo el OOS es una prueba honesta."
        )
    if resultado.estado_final == "detenido":
        avisos.append(
            "<b>El bot terminó detenido por la caída máxima.</b> Mira los eventos de riesgo."
        )
    contexto = {
        "titulo": f"Backtest · {resultado.nombre_estrategia}",
        "subtitulo": (
            f"{resultado.estrategia} · {config.mercado.simbolo} {resultado.temporalidad} · "
            f"{resultado.inicio:%Y-%m-%d} a {resultado.fin:%Y-%m-%d} · "
            f"capital inicial {formato_eur(resultado.capital_inicial)}"
        ),
        "avisos": avisos,
        "tarjetas": _tarjetas(total, total_ref),
        "columnas": list(columnas),
        "metricas": tabla_metricas(columnas),
        "graficos": [
            _html_figura(figura_capital(equity, resultado.referencia)),
            _html_figura(figura_caidas(equity, resultado.referencia)),
            _html_figura(figura_operaciones(resultado.curva["precio"], resultado.operaciones)),
        ],
        "tablas": tablas,
        "notas": _notas_metodo(config),
    }
    return _escribir(ruta, contexto)


def informe_walkforward(resultado: ResultadoWalkForward, config: Config, ruta: Path) -> Path:
    """Genera el informe HTML de un walk-forward."""
    m, ref = resultado.metricas, resultado.metricas_referencia
    filas_ventanas = []
    for v in resultado.ventanas:
        rentabilidad = v.capital_final / v.capital_inicial - 1
        filas_ventanas.append(
            [
                _celda(f"{v.ventana.entreno_desde:%Y-%m} a {v.ventana.entreno_hasta:%Y-%m}"),
                _celda(f"{v.ventana.prueba_desde:%Y-%m-%d} a {v.ventana.prueba_hasta:%Y-%m-%d}"),
                _celda(
                    ", ".join(f"{k}={val}" for k, val in v.elegida.parametros.items())
                    + (f" ({v.nota})" if v.nota else "")
                    if v.elegida
                    else f"no opera: {v.nota}"
                ),
                _celda(
                    formato_numero(v.elegida.metricas.valor(resultado.metrica))
                    if v.elegida
                    else "—"
                ),
                _celda(formato_entero(len(v.prueba.operaciones)) if v.prueba else "0"),
                _celda(formato_pct(rentabilidad), rentabilidad),
            ]
        )
    avisos = _avisos(resultado.didactica)
    avisos.append(
        f"Se probaron <b>{resultado.combinaciones_probadas} combinaciones</b> en cada ventana. "
        "Cuantas más se prueban, más probable es que la elegida brille por casualidad; "
        "lo que cuenta es la curva de los periodos de prueba."
    )
    wf = config.backtest.walkforward
    contexto = {
        "titulo": f"Walk-forward · {resultado.estrategia}",
        "subtitulo": (
            f"{config.mercado.simbolo} {config.mercado.temporalidad} · entrenamiento "
            f"{wf.entrenamiento_meses} meses, prueba {wf.prueba_meses} meses · elegida por "
            f"{resultado.metrica} · {len(resultado.ventanas)} ventanas · capital inicial "
            f"{formato_eur(resultado.capital_inicial)}"
        ),
        "avisos": avisos,
        "tarjetas": _tarjetas(m, ref),
        "columnas": ["Walk-forward (solo periodos de prueba)", "Comprar y mantener"],
        "metricas": tabla_metricas({"wf": m, "ref": ref}),
        "graficos": [
            _html_figura(
                figura_capital(resultado.curva["equity"], resultado.referencia, "Walk-forward")
            ),
            _html_figura(
                figura_caidas(resultado.curva["equity"], resultado.referencia, "Walk-forward")
            ),
        ],
        "tablas": [
            {
                "titulo": "Ventanas",
                "nota": "Cada fila: parámetros elegidos con el entrenamiento y su resultado en la prueba.",
                "columnas": [
                    "Entrenamiento",
                    "Prueba",
                    "Parámetros elegidos",
                    f"{resultado.metrica} (entreno)",
                    "Operaciones",
                    "Rentab. prueba",
                ],
                "texto": [2],
                "filas": filas_ventanas,
            },
            _tabla_operaciones(resultado.operaciones),
        ],
        "notas": _notas_metodo(config),
    }
    return _escribir(ruta, contexto)


def tabla_optimizacion(resultado: ResultadoOptimizacion, limite: int = 10) -> str:
    """Ranking in-sample frente a resultado out-of-sample, para la consola."""
    metrica = resultado.metrica
    filas = [
        ["#", "Parámetros", f"{metrica} IS", "CAGR IS", f"{metrica} OOS", "CAGR OOS", "Ops OOS"]
    ]
    for n, fila in enumerate(resultado.filas[:limite], start=1):
        fuera = fila.fuera
        filas.append(
            [
                str(n) + ("" if fila.elegible else "*"),
                ", ".join(f"{k}={v}" for k, v in fila.parametros.items()),
                formato_numero(fila.dentro.valor(metrica)),
                formato_pct(fila.dentro.cagr),
                formato_numero(fuera.valor(metrica)) if fuera else "—",
                formato_pct(fuera.cagr) if fuera else "—",
                formato_entero(fuera.operaciones) if fuera else "—",
            ]
        )
    anchos = [max(len(f[i]) for f in filas) for i in range(len(filas[0]))]
    lineas = [
        "  ".join(
            c.ljust(a) if i < 2 else c.rjust(a)
            for i, (c, a) in enumerate(zip(f, anchos, strict=True))
        )
        for f in filas
    ]
    lineas.insert(1, "  ".join("-" * a for a in anchos))
    return "\n".join(lineas)
