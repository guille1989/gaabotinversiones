"""Bucle en vivo en modo paper: paridad con el backtest, reinicios, errores y reconciliación."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
import yaml

from gaainversiones.backtest.motor import ejecutar_backtest
from gaainversiones.bot import (
    Bot,
    BotDetenido,
    BotYaEnMarcha,
    FicheroParada,
    VelaNoDisponible,
    escribir_parada,
)
from gaainversiones.bot.notificador import Nivel
from gaainversiones.config import Config, cargar_config
from gaainversiones.datos import FuenteCCXT
from gaainversiones.ejecucion.costes import ModeloCostes
from gaainversiones.ejecucion.paper import PaperBroker
from gaainversiones.estado.almacen import AlmacenEstado
from gaainversiones.estado.reconciliacion import ErrorReconciliacion
from gaainversiones.estrategias import crear_estrategia
from gaainversiones.riesgo.gestor import GestorRiesgo, LimitesMercado
from tests.utiles import (
    CONFIG_BASE,
    LIMITES,
    MEDIANOCHE,
    MercadoFalso,
    velas_aleatorias,
    velas_manuales,
)

RAIZ = Path(__file__).resolve().parents[1]
H4 = 4 * 3_600_000
T0 = int(MEDIANOCHE.value // 1_000_000)
LIMITES_MERCADO = LimitesMercado(0.00006, 0.45, 8)  # los que anuncia MercadoFalso
PLANA = (100.0, 101.0, 99.0, 100.0)


class NotificadorLista:
    def __init__(self) -> None:
        self.mensajes: list[tuple[str, Nivel]] = []

    def enviar(self, texto: str, *, nivel: Nivel = "info") -> None:
        self.mensajes.append((texto, nivel))


def _config(
    tmp_path: Path,
    estrategia: str,
    parametros: dict[str, Any],
    riesgo: dict[str, Any] | None = None,
    **bot: Any,
) -> Config:
    contenido = yaml.safe_load(CONFIG_BASE.read_text(encoding="utf-8"))
    contenido["estrategia"] = {"nombre": estrategia, "parametros": parametros}
    contenido["costes"]["deslizamiento_pb"] = 0
    contenido["riesgo"].update(riesgo or {})
    contenido["bot"].update(retraso_cierre_s=15, **bot)
    (tmp_path / "config").mkdir(exist_ok=True)
    ruta = tmp_path / "config" / "config.yaml"
    ruta.write_text(yaml.safe_dump(contenido, allow_unicode=True), encoding="utf-8")
    return cargar_config(ruta, entorno={})


class Entorno:
    """Bot en paper sobre un MercadoFalso, con reloj y almacén propios."""

    def __init__(self, tmp_path: Path, velas: pd.DataFrame, config: Config) -> None:
        self.config = config
        self.reloj = {"ms": T0}
        self.mercado = MercadoFalso(velas, reloj=self.reloj)
        self.ruta = tmp_path / "estado.sqlite"
        self.ruta_stop = tmp_path / "STOP"
        self.notificador = NotificadorLista()
        self.almacen: AlmacenEstado | None = None
        self.bot = self.nuevo_bot()

    def nuevo_bot(self, dormir: Callable[[float], None] | None = None) -> Bot:
        if self.almacen is not None:
            self.almacen.cerrar()
        self.almacen = AlmacenEstado(self.ruta, modo="paper", exchange="falso", simbolo="BTC/EUR")
        ahora = lambda: self.reloj["ms"]  # noqa: E731
        broker = PaperBroker(
            self.mercado,
            "BTC/EUR",
            ModeloCostes.desde_config(self.config.costes),
            self.almacen,
            capital_inicial=1000.0,
            limites_por_defecto=LIMITES,
            reloj=ahora,
            dormir=lambda s: None,
        )
        fuente = FuenteCCXT(self.mercado, reloj=ahora, dormir=lambda s: None, silenciosa=True)
        return Bot(
            self.config,
            broker,
            self.almacen,
            fuente,
            notificador=self.notificador,
            ordenes=[FicheroParada(self.ruta_stop)],
            reloj=ahora,
            dormir=dormir or (lambda s: None),
        )

    def en(self, vela: int, segundos: int = 20) -> None:
        """Pone el reloj ``segundos`` después de que empiece la vela ``vela``."""
        self.reloj["ms"] = T0 + vela * H4 + segundos * 1000

    def textos(self) -> str:
        return "\n".join(t for t, _ in self.notificador.mensajes)


@pytest.fixture
def entornos() -> Iterator[list[Entorno]]:
    creados: list[Entorno] = []
    yield creados
    for entorno in creados:
        if entorno.almacen is not None:
            entorno.almacen.cerrar()


@pytest.fixture
def crear(tmp_path: Path, entornos: list[Entorno]) -> Callable[..., Entorno]:
    def fabrica(
        velas: pd.DataFrame,
        estrategia: str,
        parametros: dict[str, Any],
        riesgo: dict[str, Any] | None = None,
        **bot: Any,
    ) -> Entorno:
        entorno = Entorno(tmp_path, velas, _config(tmp_path, estrategia, parametros, riesgo, **bot))
        entornos.append(entorno)
        return entorno

    return fabrica


def _velas(ohlc: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    return velas_manuales(ohlc, tf_ms=H4)


# --- Paridad con el backtest ----------------------------------------------------------------


def test_el_bot_en_paper_decide_igual_que_el_backtest(crear: Callable[..., Entorno]) -> None:
    """Mismas velas, misma estrategia y mismo riesgo: mismas operaciones y misma curva."""
    velas = velas_aleatorias(1000, tf_ms=H4, semilla=21, inicio=T0)  # ~5,5 meses
    parametros = {"media_rapida": 5, "media_lenta": 20, "rsi_periodo": 5}
    # Límites holgados para que la parada por caída no corte el escenario demasiado pronto.
    entorno = crear(
        velas, "cruce_medias_rsi", parametros, {"caida_max": 0.6, "perdida_diaria_max": 0.3}
    )
    inicio, fin = 60, len(velas) - 1

    entorno.en(inicio)
    entorno.bot.arrancar()
    entorno.bot.ciclo()  # procesa la vela inicio - 1
    for k in range(inicio, fin):
        entorno.en(k + 1)
        entorno.bot.ciclo()  # procesa la vela k

    config = entorno.config
    riesgo = GestorRiesgo(config.riesgo, ModeloCostes.desde_config(config.costes), LIMITES_MERCADO)
    backtest = ejecutar_backtest(
        velas,
        crear_estrategia("cruce_medias_rsi", parametros),
        riesgo,
        "4h",
        1000.0,
        desde=velas.index[inicio - 1],
        hasta=velas.index[fin],
    )

    def claves(tabla: pd.DataFrame) -> list[tuple[Any, ...]]:
        return [
            (
                f.fecha_entrada.floor("4h"),
                round(f.precio_entrada, 6),
                f.fecha_salida.floor("4h"),
                round(f.precio_salida, 6),
                round(f.cantidad, 8),
            )
            for f in tabla.itertuples()
        ]

    esperadas = backtest.operaciones_df()
    esperadas = esperadas[esperadas["motivo_salida"] != "fin del backtest"]
    assert len(esperadas) >= 10, "el escenario debe tener operaciones suficientes"
    assert {"stop-loss", "señal de venta"} <= set(esperadas["motivo_salida"])
    assert claves(entorno.almacen.operaciones()) == claves(esperadas)  # type: ignore[union-attr]

    curva_bot = entorno.almacen.curva()["equity"]  # type: ignore[union-attr]
    curva_bt = backtest.curva["equity"].iloc[:-1]  # la última incluye la liquidación final
    assert len(curva_bot) == len(curva_bt) + 1
    np.testing.assert_allclose(curva_bot.iloc[:-1].to_numpy(), curva_bt.to_numpy(), rtol=1e-9)


# --- Ciclos, reinicios y errores --------------------------------------------------------------


def _escenario_compra() -> pd.DataFrame:
    """Compra en la vela 15 (a 100, stop 96); la vela 20 cae a 90; luego, plano a 92."""
    return _velas([PLANA] * 20 + [(100.0, 101.0, 90.0, 92.0)] + [(92.0, 93.0, 91.0, 92.0)] * 9)


def test_cada_vela_se_procesa_una_sola_vez(crear: Callable[..., Entorno]) -> None:
    entorno = crear(_escenario_compra(), "senales_fijas_test", {"compras": [15]})
    entorno.en(16)
    entorno.bot.arrancar()
    entorno.bot.ciclo()
    entorno.en(16, segundos=600)
    entorno.bot.ciclo()  # misma vela: no se repite
    almacen = entorno.almacen
    assert almacen is not None
    assert almacen.meta_entero("ultima_vela") == T0 + 15 * H4
    assert len(almacen.curva()) == 1
    assert list(almacen.ejecuciones()["lado"]) == ["compra"]
    posicion = entorno.bot.cuenta.posicion  # type: ignore[union-attr]
    assert posicion is not None and posicion.stop == pytest.approx(96.0)
    assert [o.precio_stop for o in entorno.bot.broker.ordenes_stop()] == [pytest.approx(96.0)]
    assert "COMPRA" in entorno.textos()


def test_reinicio_recoge_el_stop_ejecutado_mientras_estaba_parado(
    crear: Callable[..., Entorno],
) -> None:
    entorno = crear(_escenario_compra(), "senales_fijas_test", {"compras": [15]})
    entorno.en(16)
    entorno.bot.arrancar()
    entorno.bot.ciclo()
    entorno.en(17)
    entorno.bot.ciclo()
    # Se corta la luz (sin despedida) y vuelve horas después de la caída de la vela 20.
    entorno.en(25)
    bot = entorno.nuevo_bot()
    bot.arrancar()
    almacen = entorno.almacen
    assert almacen is not None
    (operacion,) = almacen.operaciones().itertuples()
    assert operacion.motivo_salida == "stop-loss"
    assert operacion.precio_salida == pytest.approx(96.0)
    assert bot.cuenta.posicion is None  # type: ignore[union-attr]
    bot.ciclo()
    assert almacen.meta_entero("ultima_vela") == T0 + 24 * H4
    assert "7 velas sin procesar" in entorno.textos()


def test_reinicio_conserva_la_posicion_y_no_repite_la_vela(crear: Callable[..., Entorno]) -> None:
    entorno = crear(_escenario_compra(), "senales_fijas_test", {"compras": [15]})
    entorno.en(16)
    entorno.bot.arrancar()
    entorno.bot.ciclo()
    bot = entorno.nuevo_bot()
    with pytest.raises(BotYaEnMarcha):
        bot.arrancar()  # el latido es de hace segundos: otro proceso podría estar vivo
    bot.arrancar(forzar=True)
    assert bot.cuenta.posicion is not None  # type: ignore[union-attr]
    bot.ciclo()
    assert entorno.almacen is not None
    assert list(entorno.almacen.ejecuciones()["lado"]) == ["compra"]


def test_vela_aun_no_publicada(crear: Callable[..., Entorno]) -> None:
    entorno = crear(_escenario_compra(), "senales_fijas_test", {"compras": [15]})
    entorno.en(16)
    entorno.mercado.ocultar_ultimas = 1
    entorno.bot.arrancar()
    with pytest.raises(VelaNoDisponible):
        entorno.bot.ciclo()
    assert entorno.almacen is not None and entorno.almacen.meta("ultima_vela") is None
    entorno.mercado.ocultar_ultimas = 0
    entorno.bot.ciclo()
    assert entorno.almacen.meta_entero("ultima_vela") == T0 + 15 * H4


def test_errores_seguidos_detienen_el_bot_sin_dejar_estado_a_medias(
    crear: Callable[..., Entorno],
) -> None:
    entorno = crear(
        _escenario_compra(), "senales_fijas_test", {"compras": [15]}, max_errores_seguidos=2
    )
    entorno.en(16)
    entorno.mercado.fallos_libro = 1_000  # el libro no responde: la compra falla
    assert entorno.bot.ejecutar() is True  # terminó por errores: el vigilante reintentará
    almacen = entorno.almacen
    assert almacen is not None
    assert almacen.ejecuciones().empty and almacen.meta("ultima_vela") is None
    assert almacen.cargar_cuenta().posicion is None  # type: ignore[union-attr]
    assert "2 errores seguidos" in entorno.textos()
    assert almacen.meta_entero("latido") == 0  # libera el estado al terminar
    # Cuando el exchange vuelve, el siguiente arranque compra con normalidad.
    entorno.mercado.fallos_libro = 0
    bot = entorno.nuevo_bot()
    bot.ejecutar(max_ciclos=1)
    assert list(entorno.almacen.ejecuciones()["lado"]) == ["compra"]  # type: ignore[union-attr]


def test_reconciliacion_fallida_no_opera(crear: Callable[..., Entorno]) -> None:
    entorno = crear(_escenario_compra(), "senales_fijas_test", {"compras": [15]})
    entorno.en(10)
    entorno.bot.arrancar()
    assert entorno.almacen is not None
    entorno.almacen.guardar_sim_saldos({"BTC": 0.5, "EUR": 1000.0})  # BTC que el bot no conoce
    bot = entorno.nuevo_bot()
    with pytest.raises(ErrorReconciliacion, match="moneda base"):
        bot.arrancar(forzar=True)
    assert "NO opera" in entorno.textos()


def test_posicion_sin_stop_se_protege_al_arrancar(crear: Callable[..., Entorno]) -> None:
    entorno = crear(_escenario_compra(), "senales_fijas_test", {"compras": [15]})
    entorno.en(16)
    entorno.bot.arrancar()
    entorno.bot.ciclo()
    id_stop = entorno.bot.cuenta.posicion.id_orden_stop  # type: ignore[union-attr]
    entorno.bot.broker.cancelar_stop(id_stop)  # alguien lo canceló a mano
    bot = entorno.nuevo_bot()
    bot.arrancar(forzar=True)
    nuevo = bot.cuenta.posicion.id_orden_stop  # type: ignore[union-attr]
    assert nuevo != id_stop
    assert [o.id for o in bot.broker.ordenes_stop()] == [nuevo]
    assert "no tenía stop" in entorno.textos()


def test_historia_insuficiente_para_la_estrategia(tmp_path: Path) -> None:
    config = _config(tmp_path, "cruce_medias_rsi", {"media_lenta": 800, "media_rapida": 10})
    reloj = {"ms": T0}
    mercado = MercadoFalso(_velas([PLANA] * 5), reloj=reloj)
    with AlmacenEstado(
        tmp_path / "e.sqlite", modo="paper", exchange="f", simbolo="BTC/EUR"
    ) as almacen:
        broker = PaperBroker(
            mercado,
            "BTC/EUR",
            ModeloCostes.desde_config(config.costes),
            almacen,
            capital_inicial=1000,
            limites_por_defecto=LIMITES,
        )
        with pytest.raises(ValueError, match="velas_historia"):
            Bot(config, broker, almacen, FuenteCCXT(mercado))


# --- Kill switch y resúmenes ----------------------------------------------------------------


def _con_posicion(crear: Callable[..., Entorno]) -> Entorno:
    entorno = crear(_escenario_compra(), "senales_fijas_test", {"compras": [15]})
    entorno.en(16)
    entorno.bot.arrancar()
    entorno.bot.ciclo()  # compra a 100 con stop en 96
    return entorno


def test_kill_switch_parar_conserva_la_posicion_y_su_stop(crear: Callable[..., Entorno]) -> None:
    entorno = _con_posicion(crear)
    escribir_parada(entorno.ruta_stop, "parar")
    entorno.en(17)
    entorno.bot.ejecutar(forzar=True)  # el primer ciclo atiende la orden y el bucle termina
    almacen = entorno.almacen
    assert almacen is not None
    cuenta = almacen.cargar_cuenta()
    assert cuenta is not None and cuenta.estado == "detenido"
    assert "parada manual (fichero STOP)" in cuenta.motivo_estado
    assert cuenta.posicion is not None
    assert [o.id for o in entorno.bot.broker.ordenes_stop()] == [cuenta.posicion.id_orden_stop]
    assert almacen.meta_entero("ultima_vela") == T0 + 15 * H4  # no procesó más velas
    assert not entorno.ruta_stop.exists()
    assert "KILL SWITCH" in entorno.textos() and "conserva su stop" in entorno.textos()
    with pytest.raises(BotDetenido, match="gaa reanudar"):
        entorno.nuevo_bot().arrancar(forzar=True)


def test_kill_switch_liquidar_vende_la_posicion(crear: Callable[..., Entorno]) -> None:
    entorno = _con_posicion(crear)
    escribir_parada(entorno.ruta_stop, "liquidar")
    entorno.en(17)
    entorno.bot.ciclo()
    almacen = entorno.almacen
    assert almacen is not None
    (operacion,) = almacen.operaciones().itertuples()
    assert operacion.motivo_salida == "kill switch (fichero STOP)"
    assert entorno.bot.broker.ordenes_stop() == []
    assert entorno.bot.broker.saldos().base == pytest.approx(0)
    assert almacen.cargar_cuenta().estado == "detenido"  # type: ignore[union-attr]


def test_parada_con_el_bot_apagado(crear: Callable[..., Entorno]) -> None:
    entorno = _con_posicion(crear)
    entorno.en(30)
    bot = entorno.nuevo_bot()
    bot.parada_directa("liquidar", "comando gaa parar")
    assert entorno.almacen is not None
    motivos = list(entorno.almacen.operaciones()["motivo_salida"])
    # Antes de liquidar recoge el stop que saltó en la vela 20 mientras estaba apagado.
    assert motivos == ["stop-loss"]
    assert entorno.almacen.cargar_cuenta().estado == "detenido"  # type: ignore[union-attr]


def test_estado_y_resumen_diario(crear: Callable[..., Entorno]) -> None:
    entorno = _con_posicion(crear)
    texto = entorno.bot.resumen()
    assert "Estado: activo" in texto and "Posición: 2.09030100 BTC" in texto
    assert "stop 96,00 EUR" in texto
    # El reloj está en la vela 16 (2024-01-03 16:00 UTC): ya pasó la hora del resumen.
    resumenes = [t for t, _ in entorno.notificador.mensajes if t.startswith("Resumen diario")]
    assert len(resumenes) == 1
    entorno.en(16, segundos=3600)
    entorno.bot.ciclo()
    assert sum(t.startswith("Resumen diario") for t, _ in entorno.notificador.mensajes) == 1
    entorno.en(22)  # día siguiente, 16:00 UTC
    entorno.bot.ciclo()
    assert sum(t.startswith("Resumen diario") for t, _ in entorno.notificador.mensajes) == 2


def test_orden_estado_desde_una_fuente(crear: Callable[..., Entorno]) -> None:
    from gaainversiones.bot import OrdenManual

    entorno = _con_posicion(crear)

    class Buzon:
        def revisar(self) -> list[OrdenManual]:
            return [OrdenManual("estado", "Telegram")]

    entorno.bot.ordenes = [Buzon()]
    entorno.bot.ciclo()
    assert entorno.notificador.mensajes[-1][0].startswith("Modo paper")
    assert entorno.bot.cuenta.estado == "activo"  # type: ignore[union-attr]
