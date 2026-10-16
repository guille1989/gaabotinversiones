"""RealBroker y cerrojo del modo real, contra un Kraken simulado (nunca se toca el real)."""

from __future__ import annotations

import copy
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import ccxt
import pytest
import yaml

from gaainversiones.autorizacion import (
    FRASE_CONFIRMACION,
    autorizacion_vigente,
    conceder,
    huella,
    revocar,
)
from gaainversiones.bot import Bot
from gaainversiones.cli import main
from gaainversiones.comandos_vivo import _pedir_confirmacion_real
from gaainversiones.config import Config, ConfigReal, cargar_config
from gaainversiones.datos import FuenteCCXT
from gaainversiones.ejecucion.base import ErrorBroker
from gaainversiones.ejecucion.real import RealBroker, id_exchange
from gaainversiones.estado.almacen import AlmacenEstado
from tests.utiles import LIMITES, MEDIANOCHE, MercadoFalso, velas_manuales

RAIZ = Path(__file__).resolve().parents[1]
H4 = 4 * 3_600_000
T0 = int(MEDIANOCHE.value // 1_000_000)
CLAVES = {"GAA_API_KEY": "clave-de-prueba-1234", "GAA_API_SECRET": "secreto-de-prueba-5678"}


class KrakenFalso:
    """Imita las llamadas privadas de ccxt para Kraken: órdenes, saldos y stops."""

    id = "kraken"
    precisionMode = ccxt.TICK_SIZE

    def __init__(self, reloj: dict[str, int], *, eur: float = 1000.0) -> None:
        self.reloj = reloj
        self.saldos = {"EUR": eur, "BTC": 0.0}
        self.compra, self.venta = 99.9, 100.1
        self.ordenes: dict[str, dict[str, Any]] = {}
        self.llenado_limite: list[float] = []  # fracción que se llena de cada orden límite
        self.fallos_al_crear = 0  # crea la orden y aun así responde con un timeout

    def load_markets(self) -> dict[str, Any]:
        return {}

    def market(self, symbol: str) -> dict[str, Any]:
        return {
            "limits": {"amount": {"min": 0.00006}, "cost": {"min": 0.45}},
            "precision": {"amount": 1e-8},
        }

    def fetch_order_book(self, symbol: str, limit: int | None = None) -> dict[str, Any]:
        return {"bids": [[self.compra, 50.0]], "asks": [[self.venta, 50.0]]}

    def fetch_balance(self) -> dict[str, Any]:
        return {"total": dict(self.saldos)}

    def _llenar(self, orden: dict[str, Any], cantidad: float, precio: float) -> None:
        coste = cantidad * precio
        comision = coste * 0.004
        signo = 1 if orden["side"] == "buy" else -1
        self.saldos["BTC"] += signo * cantidad
        self.saldos["EUR"] -= signo * coste + comision
        orden.update(
            filled=cantidad, cost=coste, average=precio, fee={"cost": comision, "currency": "EUR"}
        )

    def create_order(
        self,
        symbol: str,
        type: str,
        side: str,
        amount: float,
        price: float | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        params = params or {}
        orden: dict[str, Any] = {
            "id": f"O{len(self.ordenes) + 1}",
            "clientOrderId": params.get("clientOrderId"),
            "type": type,
            "side": side,
            "amount": amount,
            "price": price,
            "filled": 0.0,
            "cost": 0.0,
            "average": None,
            "fee": None,
            "status": "open",
            "timestamp": self.reloj["ms"],
            "stopLossPrice": params.get("stopLossPrice"),
        }
        precio = self.venta if side == "buy" else self.compra
        if orden["stopLossPrice"] is None:
            fraccion = (
                1.0 if type == "market" or not self.llenado_limite else self.llenado_limite.pop(0)
            )
            if fraccion > 0:
                self._llenar(orden, amount * fraccion, precio)
            if fraccion == 1.0:
                orden["status"] = "closed"
        self.ordenes[orden["id"]] = orden
        if self.fallos_al_crear > 0:
            self.fallos_al_crear -= 1
            raise ccxt.RequestTimeout("timeout")
        return copy.deepcopy(orden)

    def fetch_order(self, id: str, symbol: str | None = None) -> dict[str, Any]:
        if id not in self.ordenes:
            raise ccxt.OrderNotFound(id)
        return copy.deepcopy(self.ordenes[id])

    def cancel_order(self, id: str, symbol: str | None = None) -> dict[str, Any]:
        orden = self.ordenes.get(id)
        if orden is None or orden["status"] != "open":
            raise ccxt.OrderNotFound(id)
        orden["status"] = "canceled"
        return copy.deepcopy(orden)

    def fetch_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        return [copy.deepcopy(o) for o in self.ordenes.values() if o["status"] == "open"]

    def fetch_closed_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        return [copy.deepcopy(o) for o in self.ordenes.values() if o["status"] != "open"]

    def ejecutar_stop(self, id: str, precio: float) -> None:
        orden = self.ordenes[id]
        self._llenar(orden, orden["amount"], precio)
        orden.update(status="closed", lastTradeTimestamp=self.reloj["ms"])

    def tipos_creados(self) -> list[str]:
        return [
            o["type"] if o["stopLossPrice"] is None else "stop-loss" for o in self.ordenes.values()
        ]


@pytest.fixture
def almacen(tmp_path: Path) -> Iterator[AlmacenEstado]:
    with AlmacenEstado(
        tmp_path / "real.sqlite", modo="real", exchange="kraken", simbolo="BTC/EUR"
    ) as a:
        yield a


def _broker(
    almacen: AlmacenEstado, **config: Any
) -> tuple[RealBroker, KrakenFalso, dict[str, int]]:
    reloj = {"ms": T0 + 20_000}
    kraken = KrakenFalso(reloj)

    def dormir(segundos: float) -> None:
        reloj["ms"] += int(segundos * 1000)

    broker = RealBroker(
        kraken,
        "BTC/EUR",
        ConfigReal(**config),
        almacen,
        limites_por_defecto=LIMITES,
        reloj=lambda: reloj["ms"],
        dormir=dormir,
    )
    return broker, kraken, reloj


def test_compra_con_orden_limite(almacen: AlmacenEstado) -> None:
    broker, kraken, _ = _broker(almacen)
    ejecucion = broker.comprar_mercado(1.0, motivo="señal", id_cliente="real-1-compra")
    (orden,) = kraken.ordenes.values()
    assert orden["type"] == "limit" and orden["price"] == pytest.approx(100.1 * 1.001)
    assert orden["clientOrderId"] == id_exchange("real-1-compra-0")
    assert ejecucion.precio == pytest.approx(100.1)  # se llena al mejor precio, no al tope
    assert ejecucion.comision == pytest.approx(100.1 * 0.004)
    assert ejecucion.precio_referencia == pytest.approx(100.0)
    assert broker.saldos().base == pytest.approx(1.0)


def test_llenado_parcial_se_completa_con_otra_orden(almacen: AlmacenEstado) -> None:
    broker, kraken, _ = _broker(almacen)
    kraken.llenado_limite = [0.4, 1.0]
    ejecucion = broker.comprar_mercado(1.0, motivo="x", id_cliente="c")
    assert ejecucion.cantidad == pytest.approx(1.0)
    primera, segunda = kraken.ordenes.values()
    assert primera["status"] == "canceled" and primera["filled"] == pytest.approx(0.4)
    assert segunda["amount"] == pytest.approx(0.6)


def test_sin_llenarse_acaba_a_mercado(almacen: AlmacenEstado) -> None:
    broker, kraken, _ = _broker(almacen)
    kraken.llenado_limite = [0, 0, 0]
    ejecucion = broker.comprar_mercado(1.0, motivo="x", id_cliente="c")
    assert kraken.tipos_creados() == ["limit", "limit", "limit", "market"]
    assert ejecucion.cantidad == pytest.approx(1.0)


def test_sin_llenarse_y_sin_mercado_falla(almacen: AlmacenEstado) -> None:
    broker, kraken, _ = _broker(almacen, mercado_si_no_se_llena=False)
    kraken.llenado_limite = [0, 0, 0]
    with pytest.raises(ErrorBroker, match="no se ejecutó"):
        broker.comprar_mercado(1.0, motivo="x", id_cliente="c")


def test_ordenes_a_mercado(almacen: AlmacenEstado) -> None:
    broker, kraken, _ = _broker(almacen, tipo_orden="mercado")
    broker.comprar_mercado(1.0, motivo="x", id_cliente="c")
    assert kraken.tipos_creados() == ["market"]


def test_un_timeout_no_duplica_la_orden(almacen: AlmacenEstado) -> None:
    broker, kraken, _ = _broker(almacen)
    kraken.fallos_al_crear = 1  # la orden llega al exchange pero la respuesta se pierde
    ejecucion = broker.comprar_mercado(1.0, motivo="x", id_cliente="c")
    assert len(kraken.ordenes) == 1 and ejecucion.cantidad == pytest.approx(1.0)
    # Repetir la misma orden (p. ej. tras un reinicio) tampoco duplica nada.
    repetida = broker.comprar_mercado(1.0, motivo="x", id_cliente="c")
    assert len(kraken.ordenes) == 1 and repetida.precio == ejecucion.precio


def test_venta(almacen: AlmacenEstado) -> None:
    broker, _, _ = _broker(almacen)
    broker.comprar_mercado(1.0, motivo="x", id_cliente="c")
    venta = broker.vender_mercado(1.0, motivo="señal", id_cliente="v")
    assert venta.lado == "venta" and venta.precio == pytest.approx(99.9)
    assert broker.saldos().base == pytest.approx(0)


def test_stop_en_el_exchange(almacen: AlmacenEstado) -> None:
    broker, kraken, _ = _broker(almacen)
    broker.comprar_mercado(1.0, motivo="x", id_cliente="c")
    stop = broker.colocar_stop(1.0, 96.0, id_cliente="c-stop")
    orden = kraken.ordenes[stop.id]
    assert orden["stopLossPrice"] == 96.0 and orden["side"] == "sell"
    assert [s.id for s in broker.ordenes_stop()] == [stop.id]
    assert broker.revisar_stops() == []
    kraken.ejecutar_stop(stop.id, 95.5)
    ((ejecutado, ejecucion),) = broker.revisar_stops()
    assert ejecutado.id == stop.id and ejecucion.precio == pytest.approx(95.5)
    assert ejecucion.motivo == "stop-loss" and ejecucion.comision == pytest.approx(95.5 * 0.004)
    assert broker.revisar_stops() == []  # ya no se vigila


def test_stop_cancelado_fuera_del_bot(almacen: AlmacenEstado) -> None:
    broker, kraken, _ = _broker(almacen)
    stop = broker.colocar_stop(1.0, 96.0, id_cliente="s")
    kraken.cancel_order(stop.id)
    assert broker.revisar_stops() == []
    assert broker.stops_perdidos() == [stop.id]
    broker.cancelar_stop("no-existe")  # no falla


def test_capital_limitado_a_lo_asignado(almacen: AlmacenEstado) -> None:
    broker, _, _ = _broker(almacen, capital_maximo=300)
    assert broker.capital_para_cuenta_nueva() == 300


# --- Bot completo con el broker real simulado -------------------------------------------------


def _config(tmp_path: Path, **cambios: Any) -> Config:
    contenido = yaml.safe_load((RAIZ / "config" / "config.yaml").read_text(encoding="utf-8"))
    contenido["estrategia"] = {"nombre": "senales_fijas_test", "parametros": {"compras": [15]}}
    for seccion, valores in cambios.items():
        if isinstance(valores, dict):
            contenido[seccion].update(valores)
        else:
            contenido[seccion] = valores
    (tmp_path / "config").mkdir(exist_ok=True)
    ruta = tmp_path / "config" / "config.yaml"
    ruta.write_text(yaml.safe_dump(contenido, allow_unicode=True), encoding="utf-8")
    return cargar_config(ruta, entorno={**CLAVES, "GAA_PERMITIR_REAL": "si"})


def test_bot_con_broker_real(tmp_path: Path, almacen: AlmacenEstado) -> None:
    config = _config(tmp_path)
    broker, kraken, reloj = _broker(almacen, capital_maximo=500)
    plana = (100.0, 101.0, 99.0, 100.0)
    velas = MercadoFalso(velas_manuales([plana] * 30, tf_ms=H4), reloj=reloj)
    bot = Bot(
        config,
        broker,
        almacen,
        FuenteCCXT(velas, reloj=lambda: reloj["ms"], dormir=lambda s: None, silenciosa=True),
        reloj=lambda: reloj["ms"],
    )
    reloj["ms"] = T0 + 16 * H4 + 20_000
    bot.arrancar()
    assert bot.cuenta is not None and bot.cuenta.efectivo == 500  # no 1.000: lo asignado
    bot.ciclo()
    posicion = bot.cuenta.posicion
    assert posicion is not None and posicion.id_orden_stop in kraken.ordenes
    assert kraken.tipos_creados() == ["limit", "stop-loss"]
    # El exchange ejecuta el stop; el bot lo recoge en la siguiente pasada.
    kraken.ejecutar_stop(posicion.id_orden_stop, 96.0)
    reloj["ms"] += 30_000
    bot.ciclo()
    assert bot.cuenta.posicion is None
    assert list(almacen.operaciones()["motivo_salida"]) == ["stop-loss"]


# --- Cerrojo y autorización ---------------------------------------------------------------------


def test_huella_cambia_con_la_configuracion_y_la_clave(tmp_path: Path) -> None:
    base = _config(tmp_path, modo="real")
    assert huella(base) == huella(_config(tmp_path, modo="real"))
    assert huella(base) != huella(
        _config(tmp_path, modo="real", riesgo={"riesgo_por_operacion": 0.02})
    )
    otra_clave = cargar_config(
        tmp_path / "config" / "config.yaml",
        entorno={**CLAVES, "GAA_API_KEY": "otra-clave-999", "GAA_PERMITIR_REAL": "si"},
    )
    assert huella(base) != huella(otra_clave)


def test_conceder_y_revocar(tmp_path: Path) -> None:
    config = _config(tmp_path, modo="real")
    assert not autorizacion_vigente(config)
    conceder(config)
    assert autorizacion_vigente(config)
    assert not autorizacion_vigente(_config(tmp_path, modo="real", real={"capital_maximo": 900}))
    revocar(config)
    assert not autorizacion_vigente(config)


def test_confirmacion_exige_la_frase_exacta(tmp_path: Path) -> None:
    config = _config(tmp_path, modo="real")
    falso = SimpleNamespace(
        broker=SimpleNamespace(saldos=lambda: SimpleNamespace(base=0.0, cotizada=1000.0)),
        estrategia=SimpleNamespace(describir=lambda: "x"),
    )
    assert _pedir_confirmacion_real(config, falso, lambda _: FRASE_CONFIRMACION)  # type: ignore[arg-type]
    assert not _pedir_confirmacion_real(config, falso, lambda _: "operar con dinero real")  # type: ignore[arg-type]
    assert not _pedir_confirmacion_real(config, falso, lambda _: "si")  # type: ignore[arg-type]


def test_gaa_real_en_modo_paper_no_arranca(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _config(tmp_path)  # modo paper
    assert main(["--config", str(tmp_path / "config" / "config.yaml"), "real"]) == 2
    assert "modo: real" in capsys.readouterr().err


def test_gaa_real_sin_terminal_ni_autorizacion_no_arranca(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _config(tmp_path, modo="real")
    for clave, valor in {**CLAVES, "GAA_PERMITIR_REAL": "si"}.items():
        monkeypatch.setenv(clave, valor)
    assert main(["--config", str(tmp_path / "config" / "config.yaml"), "real"]) == 2
    assert "confirmación manual" in capsys.readouterr().err
