"""PaperBroker: ejecución contra el libro, saldos, stops simulados y persistencia."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pandas as pd
import pytest

from gaainversiones.ejecucion.base import SaldoInsuficiente
from gaainversiones.ejecucion.paper import PaperBroker
from gaainversiones.estado.almacen import AlmacenEstado
from gaainversiones.riesgo.gestor import LimitesMercado
from tests.utiles import COSTES, LIMITES, MEDIANOCHE, MercadoFalso, velas_manuales

H4 = 4 * 3_600_000
T0 = int(MEDIANOCHE.value // 1_000_000)
PLANA = (100.0, 101.0, 99.0, 100.0)


@pytest.fixture
def almacen(tmp_path: Path) -> Iterator[AlmacenEstado]:
    with AlmacenEstado(
        tmp_path / "e.sqlite", modo="paper", exchange="falso", simbolo="BTC/EUR"
    ) as a:
        yield a


def _broker(
    almacen: AlmacenEstado,
    velas: list[tuple[float, float, float, float]] | None = None,
    **kwargs: object,
) -> tuple[PaperBroker, MercadoFalso, dict[str, int]]:
    reloj = {"ms": T0 + 20_000}
    mercado = MercadoFalso(velas_manuales(velas or [PLANA] * 10, tf_ms=H4), reloj=reloj, **kwargs)  # type: ignore[arg-type]
    broker = PaperBroker(
        mercado,
        "BTC/EUR",
        COSTES,
        almacen,
        capital_inicial=1000.0,
        limites_por_defecto=LIMITES,
        reloj=lambda: reloj["ms"],
        dormir=lambda s: None,
    )
    return broker, mercado, reloj


def test_compra_recorre_el_libro(almacen: AlmacenEstado) -> None:
    libro = {"bids": [[99.0, 5.0]], "asks": [[100.0, 0.5], [101.0, 1.0]]}
    broker, _, _ = _broker(almacen, libro=libro)
    ejecucion = broker.comprar_mercado(1.0, motivo="señal", id_cliente="c1")
    assert ejecucion.precio == pytest.approx(100.5)  # 0,5 a 100 y 0,5 a 101
    assert ejecucion.precio_referencia == pytest.approx(99.5)  # punto medio
    assert ejecucion.comision == pytest.approx(100.5 * 0.004)
    saldos = broker.saldos()
    assert saldos.base == pytest.approx(1.0)
    assert saldos.cotizada == pytest.approx(1000 - 100.5 - 100.5 * 0.004)


def test_libro_insuficiente_valora_el_resto_con_deslizamiento(almacen: AlmacenEstado) -> None:
    broker, _, _ = _broker(almacen, libro={"bids": [[99.0, 5.0]], "asks": [[100.0, 0.1]]})
    ejecucion = broker.comprar_mercado(0.3, motivo="x", id_cliente="c1")
    assert ejecucion.precio == pytest.approx((0.1 * 100 + 0.2 * 100 * 1.0005) / 0.3)


def test_orden_idempotente_por_id_de_cliente(almacen: AlmacenEstado) -> None:
    broker, _, _ = _broker(almacen)
    primera = broker.comprar_mercado(1.0, motivo="x", id_cliente="c1")
    saldos = broker.saldos()
    assert broker.comprar_mercado(1.0, motivo="x", id_cliente="c1") == primera
    assert broker.saldos() == saldos


def test_saldo_insuficiente(almacen: AlmacenEstado) -> None:
    broker, _, _ = _broker(almacen)
    with pytest.raises(SaldoInsuficiente):
        broker.comprar_mercado(20.0, motivo="x", id_cliente="c1")  # 2.000 EUR > 1.000
    with pytest.raises(SaldoInsuficiente):
        broker.vender_mercado(0.1, motivo="x", id_cliente="v1")


def test_el_stop_bloquea_el_saldo(almacen: AlmacenEstado) -> None:
    broker, _, _ = _broker(almacen)
    broker.comprar_mercado(1.0, motivo="x", id_cliente="c1")
    stop = broker.colocar_stop(1.0, 96.0, id_cliente="s1")
    assert broker.ordenes_stop() == [stop]
    with pytest.raises(SaldoInsuficiente, match="stop"):
        broker.vender_mercado(1.0, motivo="x", id_cliente="v1")
    broker.cancelar_stop("s1")
    assert broker.ordenes_stop() == []
    venta = broker.vender_mercado(1.0, motivo="x", id_cliente="v1")
    assert venta.precio == pytest.approx(100.0)
    assert broker.saldos().base == pytest.approx(0)


def _con_stop(
    almacen: AlmacenEstado, velas: list[tuple[float, float, float, float]], **kwargs: object
) -> tuple[PaperBroker, dict[str, int]]:
    broker, _, reloj = _broker(almacen, velas, **kwargs)
    broker.comprar_mercado(1.0, motivo="x", id_cliente="c1")
    broker.colocar_stop(1.0, 96.0, id_cliente="s1")
    return broker, reloj


def test_stop_salta_al_precio_del_stop(almacen: AlmacenEstado) -> None:
    velas = [PLANA, PLANA, (99.0, 100.0, 94.0, 95.0), PLANA]
    broker, reloj = _con_stop(almacen, velas)
    reloj["ms"] = T0 + 2 * H4  # empieza la vela 2: solo su primer minuto (plano a 99)
    assert broker.revisar_stops() == []
    reloj["ms"] = T0 + 2 * H4 + 5 * 60_000
    ((orden, ejecucion),) = broker.revisar_stops()
    assert orden.id == "s1" and ejecucion.motivo == "stop-loss"
    assert ejecucion.precio == pytest.approx(96.0 * (1 - 0.0005))
    assert ejecucion.fecha == pd.Timestamp(T0 + 2 * H4 + 60_000, unit="ms", tz="UTC")
    assert broker.ordenes_stop() == [] and broker.saldos().base == pytest.approx(0)
    assert broker.revisar_stops() == []  # no se ejecuta dos veces


def test_stop_con_hueco_se_ejecuta_a_la_apertura(almacen: AlmacenEstado) -> None:
    velas = [PLANA, PLANA, (90.0, 91.0, 89.0, 90.0), PLANA]
    broker, reloj = _con_stop(almacen, velas)
    reloj["ms"] = T0 + 3 * H4
    ((_, ejecucion),) = broker.revisar_stops()
    assert ejecucion.precio == pytest.approx(90.0 * (1 - 0.0005))


def test_revisar_hasta_un_instante(almacen: AlmacenEstado) -> None:
    velas = [PLANA, PLANA, (99.0, 100.0, 94.0, 95.0), PLANA]
    broker, reloj = _con_stop(almacen, velas)
    reloj["ms"] = T0 + 3 * H4
    assert broker.revisar_stops(hasta_ms=T0 + 2 * H4) == []  # la caída es posterior
    assert len(broker.revisar_stops()) == 1


def test_minuto_de_colocacion_solo_cuenta_si_cierra_por_debajo(almacen: AlmacenEstado) -> None:
    # La orden se coloca a los 90 s de la vela 0, dentro de su segundo minuto, cuyo mínimo
    # (94) pudo ser anterior a la colocación; como cierra por encima, no salta.
    velas = [(100.0, 101.0, 94.0, 100.0), PLANA, PLANA]
    broker, _, reloj = _broker(almacen, velas)
    reloj["ms"] = T0 + 90_000
    broker.comprar_mercado(1.0, motivo="x", id_cliente="c1")
    broker.colocar_stop(1.0, 96.0, id_cliente="s1")
    reloj["ms"] = T0 + 2 * H4
    assert broker.revisar_stops() == []


def test_bot_parado_mas_de_lo_que_cubren_los_minutos(almacen: AlmacenEstado) -> None:
    velas = [PLANA] * 6 + [(99.0, 100.0, 94.0, 95.0)] + [PLANA] * 3
    broker, reloj = _con_stop(almacen, velas, max_minutos=60)
    reloj["ms"] = T0 + 9 * H4  # 36 h después: los minutos solo cubren la última hora
    ((_, ejecucion),) = broker.revisar_stops()
    assert ejecucion.precio == pytest.approx(96.0 * (1 - 0.0005))
    assert ejecucion.fecha == pd.Timestamp(T0 + 6 * H4, unit="ms", tz="UTC")  # vela de 1 h


def test_persistencia_entre_instancias(almacen: AlmacenEstado) -> None:
    broker, _, _ = _broker(almacen)
    broker.comprar_mercado(1.0, motivo="x", id_cliente="c1")
    broker.colocar_stop(1.0, 96.0, id_cliente="s1")
    otro, _, _ = _broker(almacen)  # el capital inicial no se vuelve a sumar
    assert otro.saldos() == broker.saldos()
    assert [o.id for o in otro.ordenes_stop()] == ["s1"]


def test_limites_del_mercado(almacen: AlmacenEstado) -> None:
    broker, mercado, _ = _broker(almacen)
    assert broker.limites() == LimitesMercado(0.00006, 0.45, 8)
    roto, mercado_roto, _ = _broker(almacen)
    mercado_roto.mercado_roto = True
    assert roto.limites() == LIMITES  # si falla, los de config
