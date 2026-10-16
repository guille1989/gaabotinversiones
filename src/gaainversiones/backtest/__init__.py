"""Backtest: motor, métricas, optimización, walk-forward e informes."""

from gaainversiones.backtest.laboratorio import Laboratorio
from gaainversiones.backtest.metricas import Metricas, calcular_metricas, metricas_resultado
from gaainversiones.backtest.motor import ResultadoBacktest, comprar_y_mantener, ejecutar_backtest
from gaainversiones.backtest.walkforward import (
    ResultadoOptimizacion,
    ResultadoWalkForward,
    Ventana,
    generar_combinaciones,
    generar_ventanas,
    optimizar_is_oos,
    walk_forward,
)

__all__ = [
    "Laboratorio",
    "Metricas",
    "ResultadoBacktest",
    "ResultadoOptimizacion",
    "ResultadoWalkForward",
    "Ventana",
    "calcular_metricas",
    "comprar_y_mantener",
    "ejecutar_backtest",
    "generar_combinaciones",
    "generar_ventanas",
    "metricas_resultado",
    "optimizar_is_oos",
    "walk_forward",
]
