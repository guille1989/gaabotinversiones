"""Comandos de operación en vivo: ``gaa paper``, ``estado``, ``reanudar``, ``reiniciar-paper``
y ``exportar``."""

from __future__ import annotations

import argparse
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Any

from gaainversiones.bot import Bot, BotYaEnMarcha
from gaainversiones.config import Config
from gaainversiones.datos import FuenteCCXT
from gaainversiones.ejecucion.costes import ModeloCostes
from gaainversiones.ejecucion.paper import PaperBroker
from gaainversiones.estado.almacen import AlmacenEstado
from gaainversiones.estado.reconciliacion import ErrorReconciliacion
from gaainversiones.exchange import crear_exchange
from gaainversiones.fiscal import exportar_ejecuciones
from gaainversiones.formato import formato_dinero, formato_pct
from gaainversiones.riesgo.gestor import LimitesMercado
from gaainversiones.tiempo import ahora_ms, fecha_a_ms, ms_a_fecha, timeframe_a_ms


def abrir_almacen(cfg: Config, modo: str) -> AlmacenEstado:
    return AlmacenEstado(
        cfg.ruta_db_estado(modo),
        modo=modo,
        exchange=cfg.mercado.exchange,
        simbolo=cfg.mercado.simbolo,
    )


def crear_bot_paper(cfg: Config, almacen: AlmacenEstado) -> Bot:
    """Bot en modo paper contra la API pública del exchange configurado."""
    exchange = crear_exchange(cfg.mercado.exchange)
    broker = PaperBroker(
        exchange,
        cfg.mercado.simbolo,
        ModeloCostes.desde_config(cfg.costes),
        almacen,
        capital_inicial=cfg.paper.capital_inicial,
        limites_por_defecto=LimitesMercado.desde_config(cfg.mercado),
        profundidad=cfg.paper.profundidad_libro,
    )
    return Bot(cfg, broker, almacen, FuenteCCXT(exchange, silenciosa=True))


def _confirmar(pregunta: str, palabra: str, ya_confirmado: bool) -> bool:
    if ya_confirmado:
        return True
    try:
        respuesta = input(f"{pregunta} Escribe '{palabra}' para continuar: ")
    except EOFError:
        return False
    return respuesta.strip().lower() == palabra


def _latido_reciente(almacen: AlmacenEstado, cfg: Config) -> bool:
    latido = almacen.meta_entero("latido")
    return bool(latido) and ahora_ms() - (latido or 0) < 3 * cfg.bot.intervalo_s * 1000


def cmd_paper(cfg: Config, args: argparse.Namespace) -> int:
    with abrir_almacen(cfg, "paper") as almacen:
        bot = crear_bot_paper(cfg, almacen)

        def parar(senal: int, marco: FrameType | None) -> None:
            bot.parar()

        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, parar)  # systemd / docker stop
        try:
            if args.una_vez:
                bot.arrancar(forzar=args.forzar)
                try:
                    bot.ciclo()
                finally:
                    almacen.guardar_meta("latido", 0)
            else:
                print(
                    f"Bot en modo paper con {cfg.mercado.simbolo} {cfg.mercado.temporalidad}. "
                    "Ctrl+C para parar."
                )
                bot.ejecutar(forzar=args.forzar)
        except (BotYaEnMarcha, ErrorReconciliacion) as e:
            print(f"No se puede arrancar: {e}", file=sys.stderr)
            return 2
    return 0


def _fecha_ms(valor: str | None) -> str:
    return f"{ms_a_fecha(int(valor)):%Y-%m-%d %H:%M} UTC" if valor and valor != "0" else "—"


def cmd_estado(cfg: Config, args: argparse.Namespace) -> int:
    ruta = cfg.ruta_db_estado(args.modo)
    if not ruta.exists():
        print(f"Aún no hay estado en modo {args.modo}: arranca el bot con `gaa {args.modo}`")
        return 1
    with abrir_almacen(cfg, args.modo) as almacen:
        cuenta = almacen.cargar_cuenta()
        if cuenta is None:
            print("El estado está vacío.")
            return 1
        cot, base = cfg.mercado.cotizada, cfg.mercado.base
        en_marcha = "sí" if _latido_reciente(almacen, cfg) else "no"
        print(
            f"Modo {args.modo} · {cfg.mercado.simbolo} {cfg.mercado.temporalidad} · "
            f"{cfg.estrategia.nombre}"
        )
        print(f"Bot en marcha: {en_marcha} (último latido {_fecha_ms(almacen.meta('latido'))})")
        estado = cuenta.estado + (f" ({cuenta.motivo_estado})" if cuenta.motivo_estado else "")
        print(f"Estado: {estado}")
        ultima = almacen.meta_entero("ultima_vela")
        cierre = (
            f"{ms_a_fecha(ultima + timeframe_a_ms(cfg.mercado.temporalidad)):%Y-%m-%d %H:%M} UTC"
            if ultima
            else "—"
        )
        print(f"Última vela procesada: la que cerró el {cierre}")
        caida = cuenta.ultimo_equity / cuenta.equity_maximo - 1 if cuenta.equity_maximo else 0.0
        print(
            f"Capital al último cierre: {formato_dinero(cuenta.ultimo_equity, cot)} · efectivo "
            f"{formato_dinero(cuenta.efectivo, cot)} · máximo "
            f"{formato_dinero(cuenta.equity_maximo, cot)} (caída actual {formato_pct(caida)})"
        )
        posicion = cuenta.posicion
        if posicion is None:
            print("Posición: ninguna")
        else:
            print(
                f"Posición: {posicion.cantidad:.8f} {base} desde "
                f"{posicion.fecha_entrada:%Y-%m-%d %H:%M} a "
                f"{formato_dinero(posicion.precio_entrada, cot)} · stop "
                f"{formato_dinero(posicion.stop, cot)} · objetivo "
                f"{formato_dinero(posicion.objetivo, cot)}"
            )
        operaciones = almacen.operaciones()
        if operaciones.empty:
            print("Operaciones cerradas: ninguna")
        else:
            ganadoras = float((operaciones["resultado"] > 0).mean())
            print(
                f"Operaciones cerradas: {len(operaciones)} · {formato_pct(ganadoras, 0)} ganadoras"
                f" · resultado {formato_dinero(operaciones['resultado'].sum(), cot, signo=True)}"
                f" · comisiones {formato_dinero(operaciones['comisiones'].sum(), cot)}"
            )
        eventos = almacen.eventos(args.eventos)
        if not eventos.empty:
            print("\nÚltimos eventos:")
            for _, fila in eventos.iterrows():
                print(f"  {fila['fecha']:%Y-%m-%d %H:%M} [{fila['nivel']}] {fila['texto']}")
    return 0


def cmd_reanudar(cfg: Config, args: argparse.Namespace) -> int:
    with abrir_almacen(cfg, args.modo) as almacen:
        if _latido_reciente(almacen, cfg):
            print("El bot está en marcha: detenlo antes de reanudar.", file=sys.stderr)
            return 1
        cuenta = almacen.cargar_cuenta()
        if cuenta is None:
            print("No hay estado que reanudar.", file=sys.stderr)
            return 1
        if cuenta.estado == "activo":
            print("El bot ya está activo.")
            return 0
        pregunta = (
            f"El bot está {cuenta.estado}: {cuenta.motivo_estado}. Al reanudar, la caída máxima se "
            f"medirá desde el capital actual "
            f"({formato_dinero(cuenta.ultimo_equity, cfg.mercado.cotizada)})."
        )
        if not _confirmar(pregunta, "reanudar", args.si):
            print("Cancelado.")
            return 1
        anterior = f"{cuenta.estado} ({cuenta.motivo_estado})"
        cuenta.estado, cuenta.motivo_estado = "activo", ""
        cuenta.equity_maximo = cuenta.equity_inicio_dia = cuenta.ultimo_equity
        almacen.guardar_cuenta(cuenta)
        almacen.registrar_evento(f"Reanudado a mano; antes estaba {anterior}", nivel="aviso")
        print("Bot reanudado: volverá a operar en la próxima vela.")
    return 0


def cmd_reiniciar_paper(cfg: Config, args: argparse.Namespace) -> int:
    ruta = cfg.ruta_db_estado("paper")
    if not ruta.exists():
        print("No hay estado de paper que borrar.")
        return 0
    with abrir_almacen(cfg, "paper") as almacen:
        if _latido_reciente(almacen, cfg):
            print("El bot en paper está en marcha: detenlo antes.", file=sys.stderr)
            return 1
    pregunta = (
        "Se borrará TODO el estado de paper (cuenta, operaciones y registro) y se empezará "
        f"de nuevo con {formato_dinero(cfg.paper.capital_inicial, cfg.mercado.cotizada)}."
    )
    if not _confirmar(pregunta, "borrar", args.si):
        print("Cancelado.")
        return 1
    for sufijo in ("", "-wal", "-shm"):
        Path(f"{ruta}{sufijo}").unlink(missing_ok=True)
    print("Estado de paper borrado.")
    return 0


def cmd_exportar(cfg: Config, args: argparse.Namespace) -> int:
    ruta_db = cfg.ruta_db_estado(args.modo)
    if not ruta_db.exists():
        print(f"No hay registro en modo {args.modo}.", file=sys.stderr)
        return 1
    desde = ms_a_fecha(fecha_a_ms(args.desde)) if args.desde else None
    hasta = ms_a_fecha(fecha_a_ms(args.hasta)) if args.hasta else None
    salida = (
        Path(args.salida)
        if args.salida
        else cfg.raiz
        / "exportaciones"
        / f"ejecuciones_{args.modo}_{datetime.now(UTC):%Y%m%d-%H%M%S}.csv"
    )
    with abrir_almacen(cfg, args.modo) as almacen:
        filas = exportar_ejecuciones(
            almacen, salida, desde=desde, hasta=hasta, formato=args.formato
        )
    print(f"{filas} ejecuciones exportadas a {salida}")
    return 0


def registrar_comandos(subparsers: Any) -> None:
    """Añade los comandos en vivo al parser de ``gaa``."""
    sp = subparsers.add_parser("paper", help="arranca el bot en modo paper (dinero simulado)")
    sp.add_argument("--una-vez", action="store_true", help="hace una sola pasada y termina")
    sp.add_argument("--forzar", action="store_true", help="arranca aunque haya un latido reciente")
    sp.set_defaults(funcion=cmd_paper)

    def modo(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--modo", choices=["paper", "real"], default="paper")

    sp = subparsers.add_parser("estado", help="estado del bot: capital, posición, eventos")
    modo(sp)
    sp.add_argument("--eventos", type=int, default=8, help="eventos a mostrar (por defecto 8)")
    sp.set_defaults(funcion=cmd_estado)

    sp = subparsers.add_parser("reanudar", help="reactiva un bot detenido por riesgo")
    modo(sp)
    sp.add_argument("--si", action="store_true", help="no pedir confirmación")
    sp.set_defaults(funcion=cmd_reanudar)

    sp = subparsers.add_parser("reiniciar-paper", help="borra el estado de paper y empieza de cero")
    sp.add_argument("--si", action="store_true", help="no pedir confirmación")
    sp.set_defaults(funcion=cmd_reiniciar_paper)

    sp = subparsers.add_parser("exportar", help="exporta las ejecuciones a CSV (registro fiscal)")
    modo(sp)
    sp.add_argument("--desde", help="fecha inicial (incluida), p. ej. 2026-01-01")
    sp.add_argument("--hasta", help="fecha final (excluida), p. ej. 2027-01-01")
    sp.add_argument("--salida", help="ruta del CSV (por defecto exportaciones/...)")
    sp.add_argument(
        "--formato",
        choices=["estandar", "es"],
        default="estandar",
        help="es: separador ';' y coma decimal (Excel en español)",
    )
    sp.set_defaults(funcion=cmd_exportar)


__all__ = ["abrir_almacen", "crear_bot_paper", "registrar_comandos"]
