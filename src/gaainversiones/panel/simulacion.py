"""Simulación histórica para el panel: ejecuta el backtest y lo traduce a lenguaje llano.

Todo es de SOLO LECTURA: las velas salen de la caché local sin tocarla y el resultado
no se guarda en ningún sitio.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from gaainversiones.backtest import Laboratorio, metricas_resultado
from gaainversiones.backtest.metricas import Metricas
from gaainversiones.backtest.motor import ResultadoBacktest
from gaainversiones.config import Config
from gaainversiones.datos.calidad import rellenar_huecos, validar_velas
from gaainversiones.formato import formato_pct
from gaainversiones.panel.datos import leer_velas_cache

# Periodos que ofrece el panel: etiqueta -> años hacia atrás (None = todo el histórico).
PERIODOS: dict[str, int | None] = {
    "Todo el histórico": None,
    "Últimos 5 años": 5,
    "Últimos 2 años": 2,
}

# Explicación de cada métrica en lenguaje llano (clave = etiqueta de FILAS_METRICAS).
GLOSARIO: dict[str, str] = {
    "Capital inicial": "El dinero con el que empieza la simulación.",
    "Capital final": "Lo que tendrías al terminar el periodo, con comisiones y costes ya restados.",
    "Rentabilidad total": "Cuánto ha crecido (o bajado) el dinero entre el principio y el final.",
    "Rentabilidad anual (CAGR)": (
        "Lo que habrías ganado de media cada año, con el interés compuesto. "
        "Permite comparar periodos de distinta duración."
    ),
    "Caída máxima": (
        "La peor bajada desde un máximo hasta el mínimo siguiente. "
        "Es lo que habrías visto en pantalla en el peor momento: -30 % significa "
        "que 1.000 € llegaron a ser 700 €."
    ),
    "Sharpe": (
        "Ganancia por cada unidad de riesgo (vaivenes diarios). Por encima de 1 se "
        "considera buena; por debajo de 0,5, floja."
    ),
    "Sortino": "Como el Sharpe, pero solo penaliza los vaivenes a la baja.",
    "Calmar (CAGR / caída)": (
        "Rentabilidad anual dividida entre la peor bajada. Cuanto más alto, "
        "más ganas por cada punto de susto."
    ),
    "Operaciones": "Compras completas (con su venta) que hizo el robot en el periodo.",
    "% ganadoras": (
        "De cada 10 operaciones, cuántas acabaron en ganancia. Puede ser baja y aun así "
        "rentable si las ganadoras ganan mucho más de lo que pierden las perdedoras."
    ),
    "Profit factor": (
        "Euros ganados por cada euro perdido. Por encima de 1 las ganancias superan a las pérdidas."
    ),
    "Ganancia media": "Lo que gana de media una operación ganadora.",
    "Pérdida media": "Lo que pierde de media una operación perdedora.",
    "Tiempo invertido": (
        "Porcentaje del tiempo con una compra abierta. El resto del tiempo el dinero "
        "está en euros, sin riesgo de mercado."
    ),
    "Comisiones pagadas": "Total pagado al exchange en comisiones de compra y venta.",
}

# Nombres entendibles de las estrategias y de los tipos de informe.
NOMBRES_ESTRATEGIA: dict[str, str] = {
    "tendencia_media": "Tendencia con media móvil",
    "cruce_medias_rsi": "Cruce de medias con RSI",
    "reversion_rsi": "Reversión con RSI",
    "ruptura_donchian": "Ruptura de máximos",
}


def nombre_estrategia(nombre: str) -> str:
    return NOMBRES_ESTRATEGIA.get(nombre, nombre.replace("_", " ").capitalize())


@dataclass(frozen=True)
class Simulacion:
    resultado: ResultadoBacktest
    estrategia: Metricas
    referencia: Metricas


def velas_historicas(config: Config) -> pd.DataFrame:
    """Todas las velas de la caché, validadas y con los huecos rellenados.

    Raises:
        ValueError: si la caché no tiene velas o tienen problemas de calidad.
    """
    velas = leer_velas_cache(config, limite=10_000_000)
    if velas.empty:
        raise ValueError("No hay precios históricos guardados en este servidor.")
    velas = velas.sort_index()
    problemas = validar_velas(velas, config.mercado.temporalidad)
    if problemas:
        raise ValueError(f"Los precios históricos tienen problemas: {'; '.join(problemas)}")
    if config.datos.rellenar_huecos:
        return rellenar_huecos(velas, config.mercado.temporalidad)
    return velas.assign(sintetica=False)


def simular(config: Config, anios: int | None) -> Simulacion:
    """Backtest de la estrategia de la configuración sobre los últimos ``anios`` años."""
    lab = Laboratorio.desde_config(config, velas_historicas(config))
    desde = None
    if anios is not None:
        primera, ultima = lab.velas.index[0], lab.velas.index[-1]
        desde = max(primera, ultima - pd.DateOffset(years=anios))
    resultado = lab.backtest(desde=desde)
    estrategia, referencia = metricas_resultado(resultado)
    return Simulacion(resultado, estrategia, referencia)


def veredicto(m: Metricas, ref: Metricas) -> tuple[str, str, str]:
    """Conclusión en lenguaje llano: (titular, explicación, nivel ``success|info|warning``).

    Compara con comprar y mantener en ganancia final y en peor bajada.
    """
    gana_mas = m.capital_final > ref.capital_final
    menos_riesgo = abs(m.caida_maxima) < abs(ref.caida_maxima)
    if gana_mas and menos_riesgo:
        titular, nivel = "Ganó más que mantener y con menos riesgo", "success"
        detalle = (
            f"Terminó con {formato_pct(m.rentabilidad_total)} frente a "
            f"{formato_pct(ref.rentabilidad_total)} de mantener, y su peor bajada fue "
            f"{formato_pct(m.caida_maxima)} frente a {formato_pct(ref.caida_maxima)}."
        )
    elif menos_riesgo:
        veces = abs(ref.caida_maxima) / abs(m.caida_maxima) if m.caida_maxima else math.inf
        titular, nivel = "Ganó menos que mantener, pero con bajadas mucho menores", "info"
        detalle = (
            f"Su peor bajada fue {formato_pct(m.caida_maxima)}; comprar y mantener llegó a "
            f"{formato_pct(ref.caida_maxima)}"
            + (f" (unas {veces:.1f} veces más)".replace(".", ",") if math.isfinite(veces) else "")
            + ". El robot cambia parte de la ganancia por pasar menos sustos: "
            "sale del mercado cuando la tendencia se rompe."
        )
    elif gana_mas:
        titular, nivel = "Ganó más que mantener, pero con bajadas mayores", "warning"
        detalle = (
            f"Su peor bajada fue {formato_pct(m.caida_maxima)} frente a "
            f"{formato_pct(ref.caida_maxima)} de mantener. Más ganancia a costa de más riesgo."
        )
    else:
        titular, nivel = "Peor que comprar y mantener, también en riesgo", "warning"
        detalle = (
            f"Ganó {formato_pct(m.rentabilidad_total)} frente a "
            f"{formato_pct(ref.rentabilidad_total)} de mantener, sin reducir las bajadas."
        )
    if m.operaciones < 20:
        detalle += (
            f" Ojo: solo hubo {m.operaciones} operaciones; con tan pocas, el resultado "
            "puede deberse a la suerte."
        )
    return titular, detalle, nivel
