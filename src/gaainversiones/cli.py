"""Línea de comandos: ``gaa <comando>``.

Ejemplos::

    gaa config                          # valida y muestra la configuración (sin secretos)
    gaa datos descargar                 # completa la caché de velas históricas
    gaa datos importar-kraken XBTEUR_240.csv
    gaa datos verificar                 # huecos y problemas de calidad de cada serie
    gaa backtest                        # backtest con informe HTML
    gaa backtest --param media_rapida=10 --desde 2020-01-01
    gaa optimizar                       # rejilla: in-sample frente a out-of-sample
    gaa walkforward                     # optimización rodante con informe HTML
    gaa paper                           # bot en vivo con dinero simulado (Ctrl+C para parar)
    gaa estado                          # capital, posición y últimos eventos
    gaa reanudar                        # reactiva un bot detenido por caída máxima
    gaa exportar --formato es           # ejecuciones a CSV (registro fiscal)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from gaainversiones import __version__
from gaainversiones.backtest import (
    Laboratorio,
    generar_combinaciones,
    generar_ventanas,
    metricas_resultado,
    optimizar_is_oos,
    walk_forward,
)
from gaainversiones.backtest.informe import (
    columnas_backtest,
    formato_pct,
    informe_backtest,
    informe_walkforward,
    tabla_optimizacion,
    tabla_texto,
)
from gaainversiones.comandos_vivo import registrar_comandos
from gaainversiones.config import Config, ErrorConfig, cargar_config
from gaainversiones.datos import (
    CacheVelas,
    FuenteCCXT,
    actualizar_cache,
    cargar_velas,
    importar_csv_kraken,
    inspeccionar,
)
from gaainversiones.exchange import crear_exchange
from gaainversiones.registro import configurar_registro
from gaainversiones.tiempo import fecha_a_ms, ms_a_fecha, timeframe_a_ms

log = logging.getLogger("gaainversiones.cli")

Comando = Callable[[Config, argparse.Namespace], int]


def _cmd_config(cfg: Config, args: argparse.Namespace) -> int:
    print(json.dumps(cfg.resumen(), indent=2, ensure_ascii=False))
    return 0


def _cmd_datos_descargar(cfg: Config, args: argparse.Namespace) -> int:
    fuente = FuenteCCXT(crear_exchange(args.fuente or cfg.datos.fuente_historica))
    resumen = actualizar_cache(
        CacheVelas(cfg.ruta_db_velas),
        fuente,
        args.simbolo or cfg.mercado.simbolo,
        args.temporalidad or cfg.mercado.temporalidad,
        fecha_a_ms(args.desde or cfg.datos.desde),
    )
    print(resumen.texto())
    return 1 if resumen.problemas else 0


def _cmd_datos_importar_kraken(cfg: Config, args: argparse.Namespace) -> int:
    resumen = importar_csv_kraken(
        CacheVelas(cfg.ruta_db_velas),
        Path(args.fichero),
        args.simbolo or cfg.mercado.simbolo,
        args.temporalidad,
    )
    print(resumen.texto())
    return 1 if resumen.problemas else 0


def _cmd_datos_verificar(cfg: Config, args: argparse.Namespace) -> int:
    cache = CacheVelas(cfg.ruta_db_velas)
    series = cache.series()
    if not series:
        print("La caché está vacía: ejecuta `gaa datos descargar`")
        return 1
    con_problemas = 0
    for exchange, simbolo, temporalidad, _ in series:
        resumen = inspeccionar(cache, exchange, simbolo, temporalidad)
        con_problemas += bool(resumen.problemas)
        print(resumen.texto() + "\n")
    return 1 if con_problemas else 0


def _fecha(texto: str | None) -> pd.Timestamp | None:
    return ms_a_fecha(fecha_a_ms(texto)) if texto else None


def _parametros(asignaciones: Sequence[str] | None) -> dict[str, Any]:
    """Convierte ``["media_rapida=10", "rsi_max_entrada=72.5"]`` en un diccionario."""
    parametros: dict[str, Any] = {}
    for asignacion in asignaciones or []:
        nombre, separador, valor = asignacion.partition("=")
        if not separador or not nombre:
            raise ValueError(f"Parámetro mal escrito: {asignacion!r} (usa nombre=valor)")
        parametros[nombre.strip()] = yaml.safe_load(valor)
    return parametros


def _laboratorio(cfg: Config) -> Laboratorio:
    velas = cargar_velas(
        CacheVelas(cfg.ruta_db_velas),
        cfg.datos.fuente_historica,
        cfg.mercado.simbolo,
        cfg.mercado.temporalidad,
        rellenar=cfg.datos.rellenar_huecos,
    )
    return Laboratorio.desde_config(cfg, velas)


def _periodo(cfg: Config, args: argparse.Namespace) -> tuple[pd.Timestamp, pd.Timestamp | None]:
    desde = _fecha(args.desde or cfg.backtest.desde or cfg.datos.desde)
    assert desde is not None
    return desde, _fecha(args.hasta or cfg.backtest.hasta)


def _ruta_informe(cfg: Config, tipo: str, nombre: str) -> Path:
    marca = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return cfg.dir_informes / f"{tipo}_{nombre}_{marca}.html"


def _cmd_backtest(cfg: Config, args: argparse.Namespace) -> int:
    lab = _laboratorio(cfg)
    if args.ignorar_caida_max:
        lab.detener_en_caida_max = False
    desde, hasta = _periodo(cfg, args)
    resultado = lab.backtest(_parametros(args.param), desde=desde, hasta=hasta)
    inicio_oos = _fecha(cfg.backtest.inicio_oos)
    print(f"{resultado.estrategia}  {cfg.mercado.simbolo} {cfg.mercado.temporalidad}")
    periodo = f"{resultado.inicio:%Y-%m-%d} a {resultado.fin:%Y-%m-%d}"
    print(f"{periodo} (OOS desde {inicio_oos:%Y-%m-%d})\n")
    print(tabla_texto(columnas_backtest(resultado, inicio_oos)))
    print(f"\nEventos de riesgo: {len(resultado.eventos)} · estado final: {resultado.estado_final}")
    if not args.sin_informe:
        ruta = informe_backtest(
            resultado, cfg, _ruta_informe(cfg, "backtest", resultado.nombre_estrategia), inicio_oos
        )
        resultado.operaciones_df().to_csv(ruta.with_suffix(".csv"), index=False)
        print(f"Informe: {ruta}\nOperaciones: {ruta.with_suffix('.csv')}")
    return 0


def _cmd_optimizar(cfg: Config, args: argparse.Namespace) -> int:
    lab = _laboratorio(cfg)
    desde, hasta = _periodo(cfg, args)
    inicio_oos = _fecha(cfg.backtest.inicio_oos)
    assert inicio_oos is not None
    metrica = cfg.backtest.metrica_optimizacion
    resultado = optimizar_is_oos(
        lab,
        generar_combinaciones(cfg.backtest.rejilla),
        desde=desde,
        inicio_oos=inicio_oos,
        hasta=hasta,
        metrica=metrica,
        operaciones_minimas=cfg.backtest.operaciones_minimas,
    )
    print(
        f"{len(resultado.filas)} combinaciones válidas de {resultado.combinaciones_probadas}, "
        f"ordenadas por {metrica} in-sample (antes de {inicio_oos:%Y-%m-%d}):\n"
    )
    print(tabla_optimizacion(resultado, args.top))
    print(f"* menos de {cfg.backtest.operaciones_minimas} operaciones in-sample: no elegible")
    _, referencia = metricas_resultado(lab.backtest(desde=inicio_oos, hasta=hasta))
    mejor = resultado.mejor
    if mejor and mejor.fuera:
        print(
            f"\nLa mejor in-sample ({', '.join(f'{k}={v}' for k, v in mejor.parametros.items())}) "
            f"obtuvo out-of-sample un CAGR de {formato_pct(mejor.fuera.cagr)} con caída máxima "
            f"{formato_pct(mejor.fuera.caida_maxima)}; comprar y mantener: "
            f"{formato_pct(referencia.cagr)} con caída {formato_pct(referencia.caida_maxima)}."
        )
    return 0


def _cmd_walkforward(cfg: Config, args: argparse.Namespace) -> int:
    lab = _laboratorio(cfg)
    desde, hasta = _periodo(cfg, args)
    duracion = pd.Timedelta(milliseconds=timeframe_a_ms(cfg.mercado.temporalidad))
    fin = hasta or (lab.velas.index[-1] + duracion)
    wf = cfg.backtest.walkforward
    ventanas = generar_ventanas(
        max(desde, lab.velas.index[0]), fin, wf.entrenamiento_meses, wf.prueba_meses
    )
    resultado = walk_forward(
        lab,
        generar_combinaciones(cfg.backtest.rejilla),
        ventanas,
        metrica=cfg.backtest.metrica_optimizacion,
        operaciones_minimas=cfg.backtest.operaciones_minimas,
    )
    print(
        f"Walk-forward {resultado.estrategia}: {len(ventanas)} ventanas "
        f"({wf.entrenamiento_meses} meses de entrenamiento, {wf.prueba_meses} de prueba), "
        f"{resultado.combinaciones_probadas} combinaciones por ventana\n"
    )
    for v in resultado.ventanas:
        elegida = (
            ", ".join(f"{k}={val}" for k, val in v.elegida.parametros.items())
            if v.elegida
            else "no opera"
        ) + (f"  [{v.nota}]" if v.nota else "")
        rentabilidad = formato_pct(v.capital_final / v.capital_inicial - 1)
        print(
            f"  {v.ventana.prueba_desde:%Y-%m-%d} a {v.ventana.prueba_hasta:%Y-%m-%d}  "
            f"{rentabilidad:>9}  {elegida}"
        )
    print()
    print(
        tabla_texto(
            {
                "Walk-forward": resultado.metricas,
                "Comprar y mantener": resultado.metricas_referencia,
            }
        )
    )
    if not args.sin_informe:
        ruta = informe_walkforward(
            resultado, cfg, _ruta_informe(cfg, "walkforward", resultado.estrategia)
        )
        print(f"\nInforme: {ruta}")
    return 0


def construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gaa", description="GaaInversiones: laboratorio de trading"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--config", default=None, help="fichero YAML (por defecto config/config.yaml)"
    )
    subparsers = parser.add_subparsers(dest="comando", required=True)

    sp = subparsers.add_parser("config", help="valida y muestra la configuración")
    sp.set_defaults(funcion=_cmd_config)

    datos = subparsers.add_parser("datos", help="velas históricas").add_subparsers(
        dest="accion", required=True
    )
    sp = datos.add_parser("descargar", help="completa la caché desde la API de un exchange")
    sp.add_argument("--fuente", help="exchange ccxt (por defecto datos.fuente_historica)")
    sp.add_argument("--simbolo", help="por defecto mercado.simbolo")
    sp.add_argument("--temporalidad", help="por defecto mercado.temporalidad")
    sp.add_argument("--desde", help="fecha inicial, p. ej. 2017-01-01 (por defecto datos.desde)")
    sp.set_defaults(funcion=_cmd_datos_descargar)

    sp = datos.add_parser("importar-kraken", help="importa un CSV del histórico oficial de Kraken")
    sp.add_argument("fichero", help="p. ej. XBTEUR_240.csv")
    sp.add_argument("--simbolo", help="por defecto mercado.simbolo")
    sp.add_argument("--temporalidad", help="por defecto se deduce del nombre del fichero")
    sp.set_defaults(funcion=_cmd_datos_importar_kraken)

    sp = datos.add_parser("verificar", help="huecos y problemas de calidad de la caché")
    sp.set_defaults(funcion=_cmd_datos_verificar)

    def periodo(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--desde", help="inicio, p. ej. 2019-01-01 (por defecto backtest.desde)")
        sp.add_argument("--hasta", help="fin exclusivo (por defecto la última vela)")

    sp = subparsers.add_parser("backtest", help="backtest de la estrategia configurada")
    periodo(sp)
    sp.add_argument(
        "--param",
        action="append",
        metavar="NOMBRE=VALOR",
        help="cambia un parámetro de la estrategia (repetible)",
    )
    sp.add_argument(
        "--ignorar-caida-max",
        action="store_true",
        help="no detener el bot por caída máxima (solo para estudiar la estrategia)",
    )
    sp.add_argument("--sin-informe", action="store_true", help="no generar el informe HTML")
    sp.set_defaults(funcion=_cmd_backtest)

    sp = subparsers.add_parser(
        "optimizar", help="rejilla de parámetros: in-sample frente a out-of-sample"
    )
    periodo(sp)
    sp.add_argument("--top", type=int, default=10, help="filas a mostrar (por defecto 10)")
    sp.set_defaults(funcion=_cmd_optimizar)

    sp = subparsers.add_parser("walkforward", help="optimización rodante y prueba fuera de muestra")
    periodo(sp)
    sp.add_argument("--sin-informe", action="store_true", help="no generar el informe HTML")
    sp.set_defaults(funcion=_cmd_walkforward)

    registrar_comandos(subparsers)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Punto de entrada del comando ``gaa``."""
    for flujo in (sys.stdout, sys.stderr):
        if hasattr(flujo, "reconfigure"):
            flujo.reconfigure(errors="replace")  # consolas Windows sin UTF-8
    args = construir_parser().parse_args(argv)
    try:
        cfg = cargar_config(args.config)
    except ErrorConfig as e:
        print(f"Error de configuración: {e}", file=sys.stderr)
        return 2
    configurar_registro(cfg.registro, cfg.dir_logs, cfg.secretos.valores())
    funcion: Comando = args.funcion
    try:
        return funcion(cfg, args)
    except KeyboardInterrupt:
        log.warning("Interrumpido por el usuario")
        return 130
    except ValueError as e:
        # Errores de uso o de datos (parámetros, caché vacía...): mensaje claro, sin traza.
        log.error("%s", e)
        log.debug("Detalle del error", exc_info=True)
        return 1
    except Exception:
        log.exception("Error no controlado en `%s`", args.comando)
        return 1


if __name__ == "__main__":
    sys.exit(main())
