"""Comandos de operación en vivo: ``gaa paper``, ``estado``, ``reanudar``, ``reiniciar-paper``
y ``exportar``."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Any, Literal

from gaainversiones.autorizacion import (
    FRASE_CONFIRMACION,
    autorizacion_vigente,
    conceder,
    revocar,
)
from gaainversiones.bot import Bot, BotDetenido, BotYaEnMarcha
from gaainversiones.bot.killswitch import FicheroParada, FuenteOrdenes, escribir_parada
from gaainversiones.bot.notificador import Notificador, NotificadorLog
from gaainversiones.bot.telegram import (
    ClienteTelegram,
    ComandosTelegram,
    NotificadorCompuesto,
    NotificadorTelegram,
)
from gaainversiones.config import Config
from gaainversiones.copias import copiar_bases
from gaainversiones.datos import FuenteCCXT
from gaainversiones.ejecucion.costes import ModeloCostes
from gaainversiones.ejecucion.paper import PaperBroker
from gaainversiones.ejecucion.real import RealBroker
from gaainversiones.estado.almacen import AlmacenEstado
from gaainversiones.estado.reconciliacion import ErrorReconciliacion
from gaainversiones.exchange import crear_exchange
from gaainversiones.fiscal import exportar_ejecuciones
from gaainversiones.formato import formato_dinero, formato_pct
from gaainversiones.riesgo.gestor import LimitesMercado
from gaainversiones.tiempo import ahora_ms, fecha_a_ms, ms_a_fecha, timeframe_a_ms

log = logging.getLogger(__name__)

# Códigos de salida de `gaa paper` / `gaa real` (los usa el vigilante para decidir si reinicia):
#   0  parada a petición (Ctrl+C, señal, kill switch): no reiniciar
#   1  error inesperado: reiniciar
#   2  no puede arrancar (detenido, estado que no cuadra, otro bot en marcha): no reiniciar
#   3  demasiados errores seguidos (p. ej. sin internet): reiniciar más tarde
SALIDA_NO_ARRANCA = 2
SALIDA_ERRORES = 3


def abrir_almacen(cfg: Config, modo: str) -> AlmacenEstado:
    return AlmacenEstado(
        cfg.ruta_db_estado(modo),
        modo=modo,
        exchange=cfg.mercado.exchange,
        simbolo=cfg.mercado.simbolo,
    )


def cliente_telegram(cfg: Config) -> ClienteTelegram | None:
    """Cliente de Telegram si hay token en .env (el chat id puede faltar)."""
    token = cfg.secretos.telegram_token
    if token is None:
        return None
    return ClienteTelegram(token.get_secret_value(), cfg.secretos.telegram_chat_id)


def avisos_y_ordenes(
    cfg: Config, almacen: AlmacenEstado, modo: str
) -> tuple[Notificador, list[FuenteOrdenes]]:
    """Notificador (log + Telegram si está configurado) y orígenes del kill switch."""
    ordenes: list[FuenteOrdenes] = [FicheroParada(cfg.ruta_stop)]
    cliente = cliente_telegram(cfg) if cfg.alertas.telegram else None
    if cliente is None or not cliente.chat_id:
        if cliente is not None:
            log.warning(
                "Hay token de Telegram pero falta GAA_TELEGRAM_CHAT_ID: sin avisos al móvil"
            )
        return NotificadorLog(), ordenes
    telegram = NotificadorTelegram(
        cliente,
        prefijo=f"GaaInversiones · {modo}",
        nivel_minimo=cfg.alertas.nivel_minimo,
        agrupar_errores_s=cfg.alertas.agrupar_errores_min * 60,
    )
    if cfg.alertas.comandos_telegram:
        ordenes.append(ComandosTelegram(cliente, cliente.chat_id, almacen))
    return NotificadorCompuesto(NotificadorLog(), telegram), ordenes


def crear_bot_paper(cfg: Config, almacen: AlmacenEstado) -> Bot:
    """Bot en modo paper contra la API pública del exchange configurado."""
    notificador, ordenes = avisos_y_ordenes(cfg, almacen, "paper")
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
    return Bot(
        cfg,
        broker,
        almacen,
        FuenteCCXT(exchange, silenciosa=True),
        notificador=notificador,
        ordenes=ordenes,
    )


def crear_bot_real(cfg: Config, almacen: AlmacenEstado) -> Bot:
    """Bot con dinero real. Solo debe llamarse tras comprobar el doble cerrojo."""
    if cfg.modo != "real" or not cfg.secretos.permitir_real:
        raise ValueError("Modo real no permitido por la configuración")
    assert cfg.secretos.api_key is not None and cfg.secretos.api_secret is not None
    notificador, ordenes = avisos_y_ordenes(cfg, almacen, "REAL")
    exchange = crear_exchange(
        cfg.mercado.exchange,
        api_key=cfg.secretos.api_key.get_secret_value(),
        api_secret=cfg.secretos.api_secret.get_secret_value(),
    )
    broker = RealBroker(
        exchange,
        cfg.mercado.simbolo,
        cfg.real,
        almacen,
        limites_por_defecto=LimitesMercado.desde_config(cfg.mercado),
    )
    return Bot(
        cfg,
        broker,
        almacen,
        FuenteCCXT(exchange, silenciosa=True),
        notificador=notificador,
        ordenes=ordenes,
    )


def crear_bot(cfg: Config, almacen: AlmacenEstado, modo: str) -> Bot:
    return crear_bot_real(cfg, almacen) if modo == "real" else crear_bot_paper(cfg, almacen)


def _pedir_confirmacion_real(cfg: Config, bot: Bot, entrada: Callable[[str], str] = input) -> bool:
    """Muestra lo que va a pasar y exige escribir la frase de confirmación."""
    saldos = bot.broker.saldos()
    cot, base = cfg.mercado.cotizada, cfg.mercado.base
    print("\n=== MODO REAL: se operará con dinero de verdad ===")
    print(f"Exchange: {cfg.mercado.exchange} · {cfg.mercado.simbolo} {cfg.mercado.temporalidad}")
    print(f"Estrategia: {bot.estrategia.describir()}")
    print(
        f"Saldos en el exchange: {formato_dinero(saldos.cotizada, cot)} · {saldos.base:.8f} {base}"
    )
    print(
        f"Capital máximo del bot: {formato_dinero(cfg.real.capital_maximo, cot)} · riesgo "
        f"{formato_pct(cfg.riesgo.riesgo_por_operacion)} por operación · caída máxima "
        f"{formato_pct(cfg.riesgo.caida_max, 0)}"
    )
    try:
        respuesta = entrada(f"Para continuar escribe exactamente: {FRASE_CONFIRMACION}\n> ")
    except EOFError:
        return False
    return respuesta.strip() == FRASE_CONFIRMACION


def cmd_real(cfg: Config, args: argparse.Namespace) -> int:
    if cfg.modo != "real":
        print(
            "El modo real exige `modo: real` en config.yaml, GAA_PERMITIR_REAL=si y las claves "
            "de API en .env, y confirmar a mano al arrancar. Revisa antes la lista de "
            "comprobación del README.",
            file=sys.stderr,
        )
        return 2
    vigente = autorizacion_vigente(cfg)
    if not vigente and not sys.stdin.isatty():
        print(
            "Falta la confirmación manual (o la configuración cambió desde la última): "
            "ejecuta `gaa real` en una terminal.",
            file=sys.stderr,
        )
        return 2
    with abrir_almacen(cfg, "real") as almacen:
        bot = crear_bot_real(cfg, almacen)
        if not vigente:
            if not _pedir_confirmacion_real(cfg, bot):
                print("Cancelado: el bot NO ha arrancado.")
                return 1
            conceder(cfg)
            almacen.registrar_evento("Modo real confirmado a mano", nivel="aviso")

        def parar(senal: int, marco: FrameType | None) -> None:
            bot.parar()

        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, parar)
        try:
            if bot.ejecutar():
                return SALIDA_ERRORES
        except (BotYaEnMarcha, BotDetenido, ErrorReconciliacion) as e:
            print(f"No se puede arrancar: {e}", file=sys.stderr)
            return SALIDA_NO_ARRANCA
        finally:
            cuenta = almacen.cargar_cuenta()
            if cuenta is not None and cuenta.estado == "detenido":
                revocar(cfg)  # tras una parada, volver a operar exige confirmar de nuevo
    return 0


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
                if bot.ejecutar(forzar=args.forzar):
                    return SALIDA_ERRORES
        except (BotYaEnMarcha, BotDetenido, ErrorReconciliacion) as e:
            print(f"No se puede arrancar: {e}", file=sys.stderr)
            return SALIDA_NO_ARRANCA
    return 0


def cmd_parar(cfg: Config, args: argparse.Namespace) -> int:
    """Kill switch: si el bot está en marcha le deja la orden; si no, actúa directamente."""
    accion: Literal["parar", "liquidar"] = "liquidar" if args.liquidar else "parar"
    ruta = cfg.ruta_db_estado(args.modo)
    if not ruta.exists():
        print(f"No hay estado en modo {args.modo}: no hay nada que parar.")
        return 0
    with abrir_almacen(cfg, args.modo) as almacen:
        en_marcha = _latido_reciente(almacen, cfg)
    if accion == "liquidar" and not _confirmar(
        "Se venderá TODA la posición a mercado y el bot quedará detenido.", "liquidar", args.si
    ):
        print("Cancelado.")
        return 1
    if args.modo == "real":
        revocar(cfg)
    if en_marcha:
        escribir_parada(cfg.ruta_stop, accion)
        print(
            f"Orden '{accion}' dejada en {cfg.ruta_stop}. El bot la ejecutará en menos de "
            f"{cfg.bot.intervalo_s} s; compruébalo con `gaa estado`."
        )
        return 0
    with abrir_almacen(cfg, args.modo) as almacen:
        crear_bot(cfg, almacen, args.modo).parada_directa(accion, "comando gaa parar")
    print(f"Bot detenido ({accion}). Para volver a operar: `gaa reanudar`.")
    return 0


def cmd_copia_seguridad(cfg: Config, args: argparse.Namespace) -> int:
    destino = Path(args.destino) if args.destino else cfg.raiz / "copias"
    carpeta = copiar_bases(cfg.dir_datos, destino, conservar=args.conservar)
    print(f"Copia creada en {carpeta} (se conservan las {args.conservar} más recientes)")
    return 0


def cmd_panel(cfg: Config, args: argparse.Namespace) -> int:
    """Arranca el panel web de solo lectura (Streamlit) en http://127.0.0.1:<puerto>."""
    app = Path(__file__).parent / "panel" / "app.py"
    ruta_config = Path(args.config) if args.config else cfg.raiz / "config" / "config.yaml"
    entorno = {
        **os.environ,
        "GAA_PANEL_CONFIG": str(ruta_config.resolve()),
        "GAA_PANEL_MODO": args.modo,
    }
    print(f"Panel en http://127.0.0.1:{args.puerto} (Ctrl+C para cerrarlo)")
    orden = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(app),
        "--server.address",
        "127.0.0.1",
        "--server.port",
        str(args.puerto),
        "--server.headless",
        "true" if args.sin_navegador else "false",
        "--browser.gatherUsageStats",
        "false",
        "--theme.base",
        "light",  # el diseño del panel es claro; así no depende del tema del sistema
        "--theme.primaryColor",
        "#2456a6",
    ]
    try:
        return subprocess.run(orden, env=entorno, cwd=cfg.raiz, check=False).returncode
    except KeyboardInterrupt:
        return 0


def cmd_telegram_prueba(cfg: Config, args: argparse.Namespace) -> int:
    cliente = cliente_telegram(cfg)
    if cliente is None or not cliente.chat_id:
        print(
            "Falta GAA_TELEGRAM_TOKEN y/o GAA_TELEGRAM_CHAT_ID en .env "
            "(el chat id lo da `gaa telegram-chatid`).",
            file=sys.stderr,
        )
        return 1
    if cliente.enviar("✅ GaaInversiones: mensaje de prueba. Los avisos del bot llegarán aquí."):
        print("Mensaje enviado: revisa Telegram.")
        return 0
    print("No se pudo enviar (revisa el token, el chat id y el log).", file=sys.stderr)
    return 1


def cmd_telegram_chatid(cfg: Config, args: argparse.Namespace) -> int:
    cliente = cliente_telegram(cfg)
    if cliente is None:
        print("Falta GAA_TELEGRAM_TOKEN en .env.", file=sys.stderr)
        return 1
    chats: dict[str, str] = {}
    for actualizacion in cliente.actualizaciones():
        chat = (actualizacion.get("message") or {}).get("chat") or {}
        if "id" in chat:
            nombre = chat.get("username") or chat.get("first_name") or chat.get("title") or ""
            chats[str(chat["id"])] = str(nombre)
    if not chats:
        print("No hay mensajes: escribe cualquier cosa a tu bot en Telegram y repite el comando.")
        return 1
    for chat_id, nombre in chats.items():
        print(f"chat id: {chat_id}  ({nombre})")
    print("Copia tu chat id en .env como GAA_TELEGRAM_CHAT_ID=...")
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
        if args.modo == "real":
            revocar(cfg)
            print("En modo real tendrás que volver a confirmar a mano al arrancar `gaa real`.")
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

    sp = subparsers.add_parser("real", help="bot con DINERO REAL (exige doble cerrojo)")
    sp.set_defaults(funcion=cmd_real)

    sp = subparsers.add_parser("parar", help="kill switch: detiene el bot (mantiene el stop)")
    modo(sp)
    sp.add_argument(
        "--liquidar", action="store_true", help="además vende toda la posición a mercado"
    )
    sp.add_argument("--si", action="store_true", help="no pedir confirmación")
    sp.set_defaults(funcion=cmd_parar)

    sp = subparsers.add_parser("panel", help="panel web de solo lectura en el navegador")
    modo(sp)
    sp.add_argument("--puerto", type=int, default=8501, help="puerto local (8501)")
    sp.add_argument("--sin-navegador", action="store_true", help="no abrir el navegador")
    sp.set_defaults(funcion=cmd_panel)

    sp = subparsers.add_parser("copia-seguridad", help="copia el estado, el registro y las velas")
    sp.add_argument("--destino", help="carpeta de copias (por defecto copias/ en el proyecto)")
    sp.add_argument("--conservar", type=int, default=14, help="copias a conservar (14)")
    sp.set_defaults(funcion=cmd_copia_seguridad)

    sp = subparsers.add_parser("telegram-prueba", help="envía un mensaje de prueba a Telegram")
    sp.set_defaults(funcion=cmd_telegram_prueba)

    sp = subparsers.add_parser("telegram-chatid", help="muestra tu chat id de Telegram")
    sp.set_defaults(funcion=cmd_telegram_chatid)

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
