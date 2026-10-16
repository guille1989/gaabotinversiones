"""Almacén de estado SQLite y exportación fiscal."""

from __future__ import annotations

import math
from collections.abc import Iterator
from pathlib import Path

import pandas as pd
import pytest

from gaainversiones.cuenta import Ejecucion, EstadoCuenta, Operacion, Posicion
from gaainversiones.estado.almacen import AlmacenEstado
from gaainversiones.fiscal import COLUMNAS_FISCALES, exportar_ejecuciones

FECHA = pd.Timestamp("2026-03-01 08:00", tz="UTC")


@pytest.fixture
def almacen(tmp_path: Path) -> Iterator[AlmacenEstado]:
    with AlmacenEstado(
        tmp_path / "estado.sqlite", modo="paper", exchange="kraken", simbolo="BTC/EUR"
    ) as a:
        yield a


def _ejecucion(lado: str = "compra", horas: int = 0, precio: float = 50_000.5) -> Ejecucion:
    return Ejecucion(
        FECHA + pd.Timedelta(hours=horas),
        lado,
        precio,
        0.01,
        2.0,
        "señal",
        50_000.0,
        f"id-{horas}",  # type: ignore[arg-type]
    )


def test_cuenta_ida_y_vuelta(almacen: AlmacenEstado) -> None:
    assert almacen.cargar_cuenta() is None
    cuenta = EstadoCuenta.nueva(1000)
    cuenta.registrar_cierre_vela(FECHA, 100)
    cuenta.posicion = Posicion(0.5, 100.0, 96.0, 106.0, FECHA, 0.2, "stop-1")
    cuenta.estado, cuenta.motivo_estado = "pausado", "pérdida diaria"
    almacen.guardar_cuenta(cuenta)
    leida = almacen.cargar_cuenta()
    assert leida == cuenta
    cuenta.posicion = None
    almacen.guardar_cuenta(cuenta)
    assert almacen.cargar_cuenta().posicion is None  # type: ignore[union-attr]


def test_transaccion_todo_o_nada(almacen: AlmacenEstado) -> None:
    with pytest.raises(RuntimeError), almacen.transaccion():
        almacen.registrar_ejecucion(_ejecucion())
        almacen.guardar_meta("ultima_vela", 123)
        raise RuntimeError("corte")
    assert almacen.ejecuciones().empty
    assert almacen.meta("ultima_vela") is None
    with almacen.transaccion():
        almacen.registrar_ejecucion(_ejecucion())
        with almacen.transaccion():  # anidada: se confirma con la exterior
            almacen.guardar_meta("ultima_vela", 123)
    assert len(almacen.ejecuciones()) == 1 and almacen.meta_entero("ultima_vela") == 123


def test_registros(almacen: AlmacenEstado) -> None:
    almacen.registrar_ejecucion(_ejecucion("compra", 0))
    almacen.registrar_ejecucion(_ejecucion("venta", 8, 51_000))
    operacion = Operacion(
        FECHA, 50_000.5, FECHA + pd.Timedelta(hours=8), 51_000, 0.01, 502, 4, 1.0, "x"
    )
    almacen.registrar_operacion(operacion)
    almacen.registrar_evento("hola", nivel="aviso")
    almacen.registrar_capital(FECHA, equity=1001, efectivo=500, precio=50_000, en_mercado=True)
    almacen.registrar_capital(FECHA, equity=1002, efectivo=500, precio=50_000, en_mercado=True)

    ejecuciones = almacen.ejecuciones()
    assert list(ejecuciones["lado"]) == ["compra", "venta"]
    assert ejecuciones["fecha"].iloc[1] == FECHA + pd.Timedelta(hours=8)
    assert ejecuciones["importe"].iloc[0] == pytest.approx(500.005)
    assert len(almacen.ejecuciones(desde=FECHA + pd.Timedelta(hours=1))) == 1
    assert almacen.operaciones()["resultado"].iloc[0] == 1.0
    assert almacen.eventos()["texto"].tolist() == ["hola"]
    curva = almacen.curva()
    assert len(curva) == 1 and curva["equity"].iloc[0] == 1002  # una fila por cierre


@pytest.mark.parametrize("formato", ["estandar", "es"])
def test_exportar_ejecuciones(almacen: AlmacenEstado, tmp_path: Path, formato: str) -> None:
    almacen.registrar_ejecucion(_ejecucion("compra", 0))
    almacen.registrar_ejecucion(_ejecucion("venta", 30, 51_000))
    ruta = tmp_path / "salida" / "ejecuciones.csv"
    filas = exportar_ejecuciones(
        almacen,
        ruta,
        hasta=FECHA + pd.Timedelta(days=1),
        formato=formato,  # type: ignore[arg-type]
    )
    assert filas == 1
    separador, decimal = (";", ",") if formato == "es" else (",", ".")
    tabla = pd.read_csv(ruta, sep=separador, decimal=decimal, encoding="utf-8-sig")
    assert list(tabla.columns) == COLUMNAS_FISCALES
    fila = tabla.iloc[0]
    assert fila["fecha_utc"] == "2026-03-01 08:00:00"
    assert (fila["exchange"], fila["par"], fila["tipo"], fila["modo"]) == (
        "kraken",
        "BTC/EUR",
        "compra",
        "paper",
    )
    assert math.isclose(fila["precio"], 50_000.5) and fila["moneda_comision"] == "EUR"


def test_simulador(almacen: AlmacenEstado) -> None:
    assert almacen.sim_saldos() is None
    almacen.guardar_sim_saldos({"BTC": 0.0, "EUR": 1000.0})
    almacen.guardar_sim_orden(
        "s1", cantidad=0.1, precio_stop=90, estado="abierta", creada=1, revisado_hasta=1
    )
    almacen.guardar_sim_orden(
        "s2", cantidad=0.1, precio_stop=90, estado="cancelada", creada=2, revisado_hasta=2
    )
    assert [o[0] for o in almacen.sim_ordenes_abiertas()] == ["s1"]
    ejecucion = _ejecucion()
    almacen.guardar_sim_ejecucion(ejecucion)
    assert almacen.sim_ejecucion(ejecucion.id_orden) == ejecucion
    assert almacen.sim_ejecucion("no-existe") is None
