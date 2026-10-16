"""Compara muchas variantes (estrategia × ejecución × riesgo × stop) con walk-forward.

Uso (desde la raíz del proyecto):

    .venv\\Scripts\\python.exe scripts\\comparar_variantes.py [salida.csv]

Cada variante se evalúa SOLO en periodos no vistos (walk-forward: 36 meses de
entrenamiento, 6 de prueba). Aun así, con muchas variantes alguna saldrá bien por azar:
la mejor de la tabla es una candidata, no una garantía.
"""

from __future__ import annotations

import itertools
import sys
import time
from pathlib import Path

import pandas as pd

from gaainversiones.backtest import (
    Laboratorio,
    generar_combinaciones,
    generar_ventanas,
    walk_forward,
)
from gaainversiones.config import (
    ConfigBacktest,
    ConfigCostes,
    ConfigEstrategia,
    ConfigMercado,
    ConfigRiesgo,
    cargar_config,
)
from gaainversiones.datos import CacheVelas, cargar_velas

REJILLAS = {
    "tendencia_media": {"periodo": [50, 100, 150, 200], "banda": [0, 0.02]},
    "ruptura_donchian": {"entrada": [20, 40, 55, 100], "salida": [10, 20]},
    "cruce_medias_rsi": {
        "media_rapida": [10, 20, 30],
        "media_lenta": [50, 100, 200],
        "rsi_max_entrada": [65, 70, 75],
    },
}
EJECUCION = {
    "taker": {"comision_taker": 0.008, "deslizamiento_pb": 5},
    # Aproximación optimista: todas las ejecuciones como maker y sin deslizamiento.
    "maker": {"comision_taker": 0.004, "deslizamiento_pb": 0},
}
RIESGOS = [0.02, 0.03, 0.05]
STOPS = [2.0, 2.5, 4.0]


def main(salida: Path) -> None:
    base = cargar_config()
    mercado = ConfigMercado(**{**base.mercado.model_dump(), "temporalidad": "1d"})
    velas = cargar_velas(
        CacheVelas(base.ruta_db_velas), base.datos.fuente_historica, mercado.simbolo, "1d"
    )
    fin = velas.index[-1] + pd.Timedelta(days=1)
    ventanas = generar_ventanas(velas.index[0], fin, 36, 6)
    filas = []
    variantes = list(itertools.product(REJILLAS, EJECUCION, RIESGOS, STOPS))
    inicio = time.time()
    referencia = None
    for n, (estrategia, ejecucion, riesgo, stop) in enumerate(variantes, start=1):
        cfg = base.model_copy(
            update={
                "mercado": mercado,
                "estrategia": ConfigEstrategia(nombre=estrategia, parametros={}),
                "costes": ConfigCostes(**{**base.costes.model_dump(), **EJECUCION[ejecucion]}),
                "riesgo": ConfigRiesgo(
                    **{
                        **base.riesgo.model_dump(),
                        "riesgo_por_operacion": riesgo,
                        "perdida_diaria_max": max(0.05, riesgo + 0.01),
                        "sl_atr": stop,
                        "tp_atr": 0,
                        "caida_max": 0.30,
                    }
                ),
                "backtest": ConfigBacktest(
                    **{**base.backtest.model_dump(), "detener_en_caida_max": True}
                ),
            }
        )
        resultado = walk_forward(
            Laboratorio.desde_config(cfg, velas),
            generar_combinaciones(REJILLAS[estrategia]),
            ventanas,
            metrica="sharpe",
            operaciones_minimas=5,
        )
        m = resultado.metricas
        referencia = resultado.metricas_referencia
        detenida = any("caída máxima" in v.nota for v in resultado.ventanas)
        filas.append(
            {
                "estrategia": estrategia,
                "ejecucion": ejecucion,
                "riesgo": riesgo,
                "stop_atr": stop,
                "capital_final": m.capital_final,
                "cagr": m.cagr,
                "caida_maxima": m.caida_maxima,
                "sharpe": m.sharpe,
                "calmar": m.calmar,
                "operaciones": m.operaciones,
                "exposicion": m.exposicion,
                "comisiones": m.comisiones,
                "detenida_por_caida": detenida,
            }
        )
        print(
            f"[{n:2d}/{len(variantes)}] {estrategia:17s} {ejecucion:5s} riesgo {riesgo:.0%} "
            f"stop {stop}: {m.capital_final:9.2f} EUR  CAGR {m.cagr:6.1%}  caída "
            f"{m.caida_maxima:6.1%}  Sharpe {m.sharpe:5.2f}  ({time.time() - inicio:.0f} s)",
            flush=True,
        )
    tabla = pd.DataFrame(filas).sort_values("capital_final", ascending=False)
    tabla.to_csv(salida, index=False)
    assert referencia is not None
    print(
        f"\nComprar y mantener (mismo periodo): {referencia.capital_final:.2f} EUR  CAGR "
        f"{referencia.cagr:.1%}  caída {referencia.caida_maxima:.1%}  "
        f"Sharpe {referencia.sharpe:.2f}"
    )
    print(f"Resultados en {salida}")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("informes/comparacion_variantes.csv"))
