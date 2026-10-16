"""Optimización de parámetros con separación in-sample / out-of-sample y walk-forward.

La trampa que se quiere evitar: probar muchas combinaciones de parámetros y quedarse con
la que mejor salió *en los mismos datos* con los que se mide. Eso siempre parece
bueno y casi nunca lo es.

- ``optimizar_is_oos``: elige con datos anteriores a ``inicio_oos`` y enseña cómo se
  comportan esas mismas combinaciones después, en datos que no se usaron para elegir.
- ``walk_forward``: repite eso de forma rodante. En cada ventana se elige la mejor
  combinación con los ``entrenamiento_meses`` anteriores y se opera con ella los
  ``prueba_meses`` siguientes. La curva final solo contiene periodos de prueba.
"""

from __future__ import annotations

import itertools
import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import pandas as pd

from gaainversiones.backtest.laboratorio import Laboratorio
from gaainversiones.backtest.metricas import Metricas, calcular_metricas, metricas_resultado
from gaainversiones.backtest.motor import ResultadoBacktest, comprar_y_mantener
from gaainversiones.cuenta import Operacion
from gaainversiones.tiempo import timeframe_a_ms

log = logging.getLogger(__name__)


def generar_combinaciones(rejilla: Mapping[str, Sequence[Any]]) -> list[dict[str, Any]]:
    """Producto cartesiano de la rejilla: ``{a: [1, 2], b: [3]}`` -> ``[{a:1,b:3}, {a:2,b:3}]``."""
    if not rejilla:
        return [{}]
    nombres = list(rejilla)
    return [
        dict(zip(nombres, valores, strict=True)) for valores in itertools.product(*rejilla.values())
    ]


@dataclass(frozen=True)
class Ventana:
    """Una ventana del walk-forward (fines exclusivos)."""

    entreno_desde: pd.Timestamp
    entreno_hasta: pd.Timestamp
    prueba_desde: pd.Timestamp
    prueba_hasta: pd.Timestamp


def generar_ventanas(
    inicio: pd.Timestamp, fin: pd.Timestamp, entrenamiento_meses: int, prueba_meses: int
) -> list[Ventana]:
    """Ventanas rodantes de entrenamiento de longitud fija seguidas de su periodo de prueba."""
    ventanas: list[Ventana] = []
    entreno_desde = inicio
    while True:
        entreno_hasta = entreno_desde + pd.DateOffset(months=entrenamiento_meses)
        if entreno_hasta >= fin:
            break
        prueba_hasta = min(entreno_hasta + pd.DateOffset(months=prueba_meses), fin)
        ventanas.append(Ventana(entreno_desde, entreno_hasta, entreno_hasta, prueba_hasta))
        entreno_desde = entreno_desde + pd.DateOffset(months=prueba_meses)
    return ventanas


@dataclass(frozen=True)
class ResultadoCombinacion:
    parametros: dict[str, Any]
    metricas: Metricas


def evaluar_combinaciones(
    lab: Laboratorio,
    combinaciones: Sequence[Mapping[str, Any]],
    *,
    desde: pd.Timestamp | None,
    hasta: pd.Timestamp | None,
) -> list[ResultadoCombinacion]:
    """Backtest de cada combinación en el periodo. Las combinaciones no válidas se omiten."""
    resultados = []
    for combinacion in combinaciones:
        try:
            resultado = lab.backtest(combinacion, desde=desde, hasta=hasta)
        except ValueError as e:
            log.debug("Combinación omitida %s: %s", combinacion, e)
            continue
        metricas, _ = metricas_resultado(resultado)
        resultados.append(ResultadoCombinacion(dict(combinacion), metricas))
    return resultados


def es_elegible(metricas: Metricas, metrica: str, operaciones_minimas: int) -> bool:
    valor = metricas.valor(metrica)
    return metricas.operaciones >= operaciones_minimas and not math.isnan(valor)


def elegir_mejor(
    resultados: Sequence[ResultadoCombinacion], metrica: str, operaciones_minimas: int
) -> ResultadoCombinacion | None:
    """La combinación con mejor ``metrica`` entre las que tienen suficientes operaciones."""
    elegibles = [r for r in resultados if es_elegible(r.metricas, metrica, operaciones_minimas)]
    return max(elegibles, key=lambda r: r.metricas.valor(metrica), default=None)


# --- In-sample / out-of-sample ------------------------------------------------------------


@dataclass(frozen=True)
class FilaOptimizacion:
    parametros: dict[str, Any]
    dentro: Metricas  # in-sample
    fuera: Metricas | None  # out-of-sample
    elegible: bool


@dataclass(frozen=True)
class ResultadoOptimizacion:
    filas: list[FilaOptimizacion]  # ordenadas por la métrica in-sample, de mejor a peor
    metrica: str
    inicio_oos: pd.Timestamp
    combinaciones_probadas: int

    @property
    def mejor(self) -> FilaOptimizacion | None:
        return next((f for f in self.filas if f.elegible), None)


def optimizar_is_oos(
    lab: Laboratorio,
    combinaciones: Sequence[Mapping[str, Any]],
    *,
    desde: pd.Timestamp | None,
    inicio_oos: pd.Timestamp,
    hasta: pd.Timestamp | None,
    metrica: str,
    operaciones_minimas: int,
) -> ResultadoOptimizacion:
    """Ordena las combinaciones por su resultado in-sample y mide cada una out-of-sample."""
    dentro = evaluar_combinaciones(lab, combinaciones, desde=desde, hasta=inicio_oos)
    filas = []
    for resultado in dentro:
        fuera = evaluar_combinaciones(lab, [resultado.parametros], desde=inicio_oos, hasta=hasta)
        filas.append(
            FilaOptimizacion(
                resultado.parametros,
                resultado.metricas,
                fuera[0].metricas if fuera else None,
                es_elegible(resultado.metricas, metrica, operaciones_minimas),
            )
        )

    def clave(fila: FilaOptimizacion) -> tuple[bool, float]:
        valor = fila.dentro.valor(metrica)
        return (fila.elegible, valor if not math.isnan(valor) else -math.inf)

    filas.sort(key=clave, reverse=True)
    return ResultadoOptimizacion(filas, metrica, inicio_oos, len(combinaciones))


# --- Walk-forward -------------------------------------------------------------------------


@dataclass(frozen=True)
class ResultadoVentana:
    ventana: Ventana
    elegida: ResultadoCombinacion | None
    validas: int
    prueba: ResultadoBacktest | None
    capital_inicial: float
    capital_final: float
    nota: str = ""


@dataclass(frozen=True)
class ResultadoWalkForward:
    ventanas: list[ResultadoVentana]
    curva: pd.DataFrame  # periodos de prueba encadenados: equity, en_mercado
    referencia: pd.Series  # comprar y mantener en el mismo tramo
    operaciones: list[Operacion]
    metricas: Metricas
    metricas_referencia: Metricas
    metrica: str
    combinaciones_probadas: int
    capital_inicial: float
    estrategia: str
    didactica: bool


def walk_forward(
    lab: Laboratorio,
    combinaciones: Sequence[Mapping[str, Any]],
    ventanas: Sequence[Ventana],
    *,
    metrica: str,
    operaciones_minimas: int,
) -> ResultadoWalkForward:
    """Ejecuta el walk-forward encadenando el capital de una ventana de prueba a la siguiente.

    Si en una ventana ninguna combinación es elegible (pocas operaciones), en su periodo
    de prueba no se opera: el capital queda plano. Cada periodo de prueba es un backtest
    que liquida la posición al terminar; la caída máxima se mide desde el máximo de todo
    el recorrido y, si salta, el bot ya no vuelve a operar (como en vivo).
    """
    if not ventanas:
        raise ValueError("No hay ventanas: el periodo es más corto que el de entrenamiento")
    capital = equity_maximo = lab.capital_inicial
    detenido = False
    trozos: list[pd.DataFrame] = []
    operaciones: list[Operacion] = []
    resultados: list[ResultadoVentana] = []
    duracion = pd.Timedelta(milliseconds=timeframe_a_ms(lab.temporalidad))

    for ventana in ventanas:
        evaluadas: list[ResultadoCombinacion] = []
        elegida: ResultadoCombinacion | None = None
        nota = "detenido por caída máxima" if detenido else ""
        if not detenido:
            evaluadas = evaluar_combinaciones(
                lab, combinaciones, desde=ventana.entreno_desde, hasta=ventana.entreno_hasta
            )
            elegida = elegir_mejor(evaluadas, metrica, operaciones_minimas)
            if elegida is None:
                nota = "ninguna combinación elegible"
        capital_antes = capital
        prueba: ResultadoBacktest | None = None
        if elegida is not None:
            # La caída se mide desde el máximo de todo el walk-forward, como haría el bot.
            prueba = lab.backtest(
                elegida.parametros,
                desde=ventana.prueba_desde,
                hasta=ventana.prueba_hasta,
                capital=capital,
                equity_maximo=equity_maximo,
            )
            trozo = prueba.curva[["equity", "en_mercado"]]
            capital = prueba.capital_final
            equity_maximo = max(equity_maximo, float(prueba.curva["equity"].max()))
            operaciones.extend(prueba.operaciones)
            if prueba.estado_final == "detenido":
                detenido, nota = True, "se detiene por caída máxima"
        else:
            aperturas = lab.velas.index[
                (lab.velas.index >= ventana.prueba_desde) & (lab.velas.index < ventana.prueba_hasta)
            ]
            trozo = pd.DataFrame(
                {"equity": capital, "en_mercado": False},
                index=pd.DatetimeIndex(aperturas + duracion, name="fecha_cierre"),
            )
        trozos.append(trozo)
        resultados.append(
            ResultadoVentana(ventana, elegida, len(evaluadas), prueba, capital_antes, capital, nota)
        )
        log.info(
            "Ventana %s-%s: %s -> capital %.2f %s",
            ventana.prueba_desde.date(),
            ventana.prueba_hasta.date(),
            elegida.parametros if elegida else "-",
            capital,
            nota,
        )

    curva = pd.concat(trozos)
    inicio = ventanas[0].prueba_desde
    tramo = lab.velas[(lab.velas.index >= inicio) & (lab.velas.index < ventanas[-1].prueba_hasta)]
    referencia = pd.Series(
        comprar_y_mantener(
            float(tramo["apertura"].iloc[0]),
            tramo["cierre"].to_numpy(),
            lab.capital_inicial,
            lab.riesgo.costes,
        ),
        index=curva.index,
        name="comprar_y_mantener",
    )
    estrategia = lab.estrategia()
    return ResultadoWalkForward(
        ventanas=resultados,
        curva=curva,
        referencia=referencia,
        operaciones=operaciones,
        metricas=calcular_metricas(
            curva["equity"], lab.capital_inicial, inicio, operaciones, curva["en_mercado"]
        ),
        metricas_referencia=calcular_metricas(
            referencia,
            lab.capital_inicial,
            inicio,
            en_mercado=pd.Series(True, index=curva.index),
            referencia=True,
        ),
        metrica=metrica,
        combinaciones_probadas=len(combinaciones),
        capital_inicial=lab.capital_inicial,
        estrategia=estrategia.nombre,
        didactica=estrategia.didactica,
    )
