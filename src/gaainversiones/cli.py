"""Línea de comandos: ``gaa <comando>``.

Ejemplos::

    gaa config                          # valida y muestra la configuración (sin secretos)
    gaa datos descargar                 # completa la caché de velas históricas
    gaa datos importar-kraken XBTEUR_240.csv
    gaa datos verificar                 # huecos y problemas de calidad de cada serie
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from gaainversiones import __version__
from gaainversiones.config import Config, ErrorConfig, cargar_config
from gaainversiones.datos import (
    CacheVelas,
    FuenteCCXT,
    actualizar_cache,
    importar_csv_kraken,
    inspeccionar,
)
from gaainversiones.exchange import crear_exchange
from gaainversiones.registro import configurar_registro
from gaainversiones.tiempo import fecha_a_ms

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
    except Exception:
        log.exception("Error no controlado en `%s`", args.comando)
        return 1


if __name__ == "__main__":
    sys.exit(main())
