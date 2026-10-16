"""Persistencia del estado del bot en SQLite.

Un fichero por modo (``data/estado_paper.sqlite``, ``data/estado_real.sqlite``), para que
el dinero simulado y el real nunca se mezclen. Guarda:

- la cuenta (efectivo, máximos de capital, estado del bot) y la posición abierta;
- el registro de **ejecuciones** (cada compra y venta: la base fiscal) y de operaciones;
- eventos (arranques, paradas por riesgo, errores) y la curva de capital por vela;
- en paper, el "exchange simulado": saldos, órdenes stop y ejecuciones por id de cliente.

Las escrituras que deben ir juntas se agrupan con ``transaccion()``: o se guardan todas o
ninguna, así un corte de luz no deja la cuenta a medias.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from types import TracebackType
from typing import Any

import pandas as pd

from gaainversiones.cuenta import Ejecucion, EstadoCuenta, Operacion, Posicion

_ESQUEMA = """
CREATE TABLE IF NOT EXISTS meta (
    clave TEXT PRIMARY KEY,
    valor TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cuenta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    efectivo REAL NOT NULL,
    equity_maximo REAL NOT NULL,
    equity_inicio_dia REAL NOT NULL,
    ultimo_equity REAL NOT NULL,
    dia TEXT,
    estado TEXT NOT NULL,
    motivo_estado TEXT NOT NULL,
    actualizado TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS posicion (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    cantidad REAL NOT NULL,
    precio_entrada REAL NOT NULL,
    stop REAL NOT NULL,
    objetivo REAL NOT NULL,
    fecha_entrada TEXT NOT NULL,
    comision_entrada REAL NOT NULL,
    id_orden_stop TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ejecuciones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fecha TEXT NOT NULL,
    modo TEXT NOT NULL,
    exchange TEXT NOT NULL,
    simbolo TEXT NOT NULL,
    lado TEXT NOT NULL,
    precio REAL NOT NULL,
    cantidad REAL NOT NULL,
    importe REAL NOT NULL,
    comision REAL NOT NULL,
    moneda_comision TEXT NOT NULL,
    motivo TEXT NOT NULL,
    precio_referencia REAL,
    id_orden TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operaciones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fecha_entrada TEXT NOT NULL,
    precio_entrada REAL NOT NULL,
    fecha_salida TEXT NOT NULL,
    precio_salida REAL NOT NULL,
    cantidad REAL NOT NULL,
    coste_entrada REAL NOT NULL,
    comisiones REAL NOT NULL,
    resultado REAL NOT NULL,
    motivo_salida TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eventos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fecha TEXT NOT NULL,
    nivel TEXT NOT NULL,
    texto TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS capital (
    fecha TEXT PRIMARY KEY,
    equity REAL NOT NULL,
    efectivo REAL NOT NULL,
    precio REAL NOT NULL,
    en_mercado INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sim_saldos (
    activo TEXT PRIMARY KEY,
    cantidad REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sim_ordenes (
    id TEXT PRIMARY KEY,
    cantidad REAL NOT NULL,
    precio_stop REAL NOT NULL,
    estado TEXT NOT NULL,
    creada INTEGER NOT NULL,
    revisado_hasta INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sim_ejecuciones (
    id_cliente TEXT PRIMARY KEY,
    fecha INTEGER NOT NULL,
    lado TEXT NOT NULL,
    precio REAL NOT NULL,
    cantidad REAL NOT NULL,
    comision REAL NOT NULL,
    motivo TEXT NOT NULL,
    precio_referencia REAL
);
"""


def _texto_fecha(fecha: pd.Timestamp) -> str:
    return pd.Timestamp(fecha).tz_convert("UTC").isoformat()


def _fecha(texto: str) -> pd.Timestamp:
    return pd.Timestamp(texto).tz_convert("UTC")


class AlmacenEstado:
    """Estado persistente de un modo de operación (paper o real)."""

    def __init__(self, ruta: Path, *, modo: str, exchange: str, simbolo: str) -> None:
        self.ruta = ruta
        self.modo = modo
        self.exchange = exchange
        self.simbolo = simbolo
        self.moneda_cotizada = simbolo.split("/")[1]
        ruta.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(ruta)
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.executescript(_ESQUEMA)
        self._profundidad = 0

    # --- Conexión y transacciones -------------------------------------------------------

    def cerrar(self) -> None:
        self._con.close()

    def __enter__(self) -> AlmacenEstado:
        return self

    def __exit__(
        self,
        tipo: type[BaseException] | None,
        error: BaseException | None,
        traza: TracebackType | None,
    ) -> None:
        self.cerrar()

    @contextmanager
    def transaccion(self) -> Iterator[None]:
        """Agrupa escrituras: se confirman todas al salir o ninguna si hay una excepción."""
        self._profundidad += 1
        try:
            yield
            if self._profundidad == 1:
                self._con.commit()
        except BaseException:
            if self._profundidad == 1:
                self._con.rollback()
            raise
        finally:
            self._profundidad -= 1

    def _escribir(self, sql: str, parametros: tuple[Any, ...] | list[Any] = ()) -> None:
        with self.transaccion():
            self._con.execute(sql, parametros)

    def _leer(self, sql: str, parametros: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        return self._con.execute(sql, parametros).fetchall()

    # --- Metadatos ----------------------------------------------------------------------

    def meta(self, clave: str) -> str | None:
        filas = self._leer("SELECT valor FROM meta WHERE clave = ?", (clave,))
        return str(filas[0][0]) if filas else None

    def guardar_meta(self, clave: str, valor: object) -> None:
        self._escribir(
            "INSERT INTO meta (clave, valor) VALUES (?, ?) "
            "ON CONFLICT (clave) DO UPDATE SET valor = excluded.valor",
            (clave, str(valor)),
        )

    def meta_entero(self, clave: str) -> int | None:
        valor = self.meta(clave)
        return int(valor) if valor is not None else None

    # --- Cuenta -------------------------------------------------------------------------

    def guardar_cuenta(self, cuenta: EstadoCuenta) -> None:
        with self.transaccion():
            self._escribir(
                "INSERT INTO cuenta (id, efectivo, equity_maximo, equity_inicio_dia, "
                "ultimo_equity, dia, estado, motivo_estado, actualizado) "
                "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (id) DO UPDATE SET efectivo = excluded.efectivo, "
                "equity_maximo = excluded.equity_maximo, "
                "equity_inicio_dia = excluded.equity_inicio_dia, "
                "ultimo_equity = excluded.ultimo_equity, dia = excluded.dia, "
                "estado = excluded.estado, motivo_estado = excluded.motivo_estado, "
                "actualizado = excluded.actualizado",
                (
                    cuenta.efectivo,
                    cuenta.equity_maximo,
                    cuenta.equity_inicio_dia,
                    cuenta.ultimo_equity,
                    cuenta.dia.isoformat() if cuenta.dia else None,
                    cuenta.estado,
                    cuenta.motivo_estado,
                    _texto_fecha(pd.Timestamp.now(tz="UTC")),
                ),
            )
            self._escribir("DELETE FROM posicion")
            p = cuenta.posicion
            if p is not None:
                self._escribir(
                    "INSERT INTO posicion (id, cantidad, precio_entrada, stop, objetivo, "
                    "fecha_entrada, comision_entrada, id_orden_stop) "
                    "VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        p.cantidad,
                        p.precio_entrada,
                        p.stop,
                        p.objetivo,
                        _texto_fecha(p.fecha_entrada),
                        p.comision_entrada,
                        p.id_orden_stop,
                    ),
                )

    def cargar_cuenta(self) -> EstadoCuenta | None:
        filas = self._leer(
            "SELECT efectivo, equity_maximo, equity_inicio_dia, ultimo_equity, dia, estado, "
            "motivo_estado FROM cuenta WHERE id = 1"
        )
        if not filas:
            return None
        efectivo, maximo, inicio_dia, ultimo, dia, estado, motivo = filas[0]
        cuenta = EstadoCuenta(
            efectivo=efectivo,
            equity_maximo=maximo,
            equity_inicio_dia=inicio_dia,
            ultimo_equity=ultimo,
            dia=date.fromisoformat(dia) if dia else None,
            estado=estado,
            motivo_estado=motivo,
        )
        posicion = self._leer(
            "SELECT cantidad, precio_entrada, stop, objetivo, fecha_entrada, comision_entrada, "
            "id_orden_stop FROM posicion WHERE id = 1"
        )
        if posicion:
            cantidad, entrada, stop, objetivo, fecha, comision, id_stop = posicion[0]
            cuenta.posicion = Posicion(
                cantidad, entrada, stop, objetivo, _fecha(fecha), comision, id_stop
            )
        return cuenta

    # --- Registro -----------------------------------------------------------------------

    def registrar_ejecucion(self, ejecucion: Ejecucion) -> None:
        self._escribir(
            "INSERT INTO ejecuciones (fecha, modo, exchange, simbolo, lado, precio, cantidad, "
            "importe, comision, moneda_comision, motivo, precio_referencia, id_orden) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _texto_fecha(ejecucion.fecha),
                self.modo,
                self.exchange,
                self.simbolo,
                ejecucion.lado,
                ejecucion.precio,
                ejecucion.cantidad,
                ejecucion.importe,
                ejecucion.comision,
                self.moneda_cotizada,
                ejecucion.motivo,
                ejecucion.precio_referencia,
                ejecucion.id_orden,
            ),
        )

    def registrar_operacion(self, operacion: Operacion) -> None:
        self._escribir(
            "INSERT INTO operaciones (fecha_entrada, precio_entrada, fecha_salida, precio_salida, "
            "cantidad, coste_entrada, comisiones, resultado, motivo_salida) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _texto_fecha(operacion.fecha_entrada),
                operacion.precio_entrada,
                _texto_fecha(operacion.fecha_salida),
                operacion.precio_salida,
                operacion.cantidad,
                operacion.coste_entrada,
                operacion.comisiones,
                operacion.resultado,
                operacion.motivo_salida,
            ),
        )

    def registrar_evento(self, texto: str, *, nivel: str = "info") -> None:
        self._escribir(
            "INSERT INTO eventos (fecha, nivel, texto) VALUES (?, ?, ?)",
            (_texto_fecha(pd.Timestamp.now(tz="UTC")), nivel, texto),
        )

    def registrar_capital(
        self,
        fecha: pd.Timestamp,
        *,
        equity: float,
        efectivo: float,
        precio: float,
        en_mercado: bool,
    ) -> None:
        self._escribir(
            "INSERT INTO capital (fecha, equity, efectivo, precio, en_mercado) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (fecha) DO UPDATE SET equity = excluded.equity, "
            "efectivo = excluded.efectivo, precio = excluded.precio, "
            "en_mercado = excluded.en_mercado",
            (_texto_fecha(fecha), equity, efectivo, precio, int(en_mercado)),
        )

    # --- Consultas ----------------------------------------------------------------------

    def _tabla(
        self, sql: str, parametros: tuple[Any, ...] = (), fechas: tuple[str, ...] = ()
    ) -> pd.DataFrame:
        cursor = self._con.execute(sql, parametros)
        columnas = [d[0] for d in cursor.description]
        tabla = pd.DataFrame(cursor.fetchall(), columns=columnas)
        for columna in fechas:
            tabla[columna] = pd.to_datetime(tabla[columna], utc=True, format="ISO8601")
        return tabla

    def ejecuciones(
        self, desde: pd.Timestamp | None = None, hasta: pd.Timestamp | None = None
    ) -> pd.DataFrame:
        """Ejecuciones con fecha en ``[desde, hasta)``, en orden cronológico."""
        tabla = self._tabla("SELECT * FROM ejecuciones ORDER BY id", fechas=("fecha",))
        if desde is not None:
            tabla = tabla[tabla["fecha"] >= desde]
        if hasta is not None:
            tabla = tabla[tabla["fecha"] < hasta]
        return tabla.reset_index(drop=True)

    def operaciones(self) -> pd.DataFrame:
        return self._tabla(
            "SELECT * FROM operaciones ORDER BY id", fechas=("fecha_entrada", "fecha_salida")
        )

    def eventos(self, ultimos: int = 20) -> pd.DataFrame:
        tabla = self._tabla(
            "SELECT * FROM (SELECT * FROM eventos ORDER BY id DESC LIMIT ?) ORDER BY id",
            (ultimos,),
            fechas=("fecha",),
        )
        return tabla

    def curva(self) -> pd.DataFrame:
        tabla = self._tabla("SELECT * FROM capital ORDER BY fecha", fechas=("fecha",))
        return tabla.set_index("fecha")

    # --- Exchange simulado (paper) ------------------------------------------------------

    def sim_saldos(self) -> dict[str, float] | None:
        filas = self._leer("SELECT activo, cantidad FROM sim_saldos")
        return {activo: float(cantidad) for activo, cantidad in filas} if filas else None

    def guardar_sim_saldos(self, saldos: dict[str, float]) -> None:
        with self.transaccion():
            for activo, cantidad in saldos.items():
                self._escribir(
                    "INSERT INTO sim_saldos (activo, cantidad) VALUES (?, ?) "
                    "ON CONFLICT (activo) DO UPDATE SET cantidad = excluded.cantidad",
                    (activo, cantidad),
                )

    def sim_ordenes_abiertas(self) -> list[tuple[str, float, float, int, int]]:
        """(id, cantidad, precio_stop, creada_ms, revisado_hasta_ms) de los stops abiertos."""
        return [
            (str(i), float(c), float(p), int(cr), int(r))
            for i, c, p, cr, r in self._leer(
                "SELECT id, cantidad, precio_stop, creada, revisado_hasta FROM sim_ordenes "
                "WHERE estado = 'abierta' ORDER BY creada"
            )
        ]

    def guardar_sim_orden(
        self,
        id_orden: str,
        *,
        cantidad: float,
        precio_stop: float,
        estado: str,
        creada: int,
        revisado_hasta: int,
    ) -> None:
        self._escribir(
            "INSERT INTO sim_ordenes (id, cantidad, precio_stop, estado, creada, revisado_hasta) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET estado = excluded.estado, "
            "revisado_hasta = excluded.revisado_hasta",
            (id_orden, cantidad, precio_stop, estado, creada, revisado_hasta),
        )

    def sim_ejecucion(self, id_cliente: str) -> Ejecucion | None:
        filas = self._leer(
            "SELECT fecha, lado, precio, cantidad, comision, motivo, precio_referencia "
            "FROM sim_ejecuciones WHERE id_cliente = ?",
            (id_cliente,),
        )
        if not filas:
            return None
        fecha, lado, precio, cantidad, comision, motivo, referencia = filas[0]
        return Ejecucion(
            pd.Timestamp(fecha, unit="ms", tz="UTC"),
            lado,
            precio,
            cantidad,
            comision,
            motivo,
            referencia,
            id_cliente,
        )

    def guardar_sim_ejecucion(self, ejecucion: Ejecucion) -> None:
        self._escribir(
            "INSERT INTO sim_ejecuciones (id_cliente, fecha, lado, precio, cantidad, comision, "
            "motivo, precio_referencia) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ejecucion.id_orden,
                int(ejecucion.fecha.value // 1_000_000),
                ejecucion.lado,
                ejecucion.precio,
                ejecucion.cantidad,
                ejecucion.comision,
                ejecucion.motivo,
                ejecucion.precio_referencia,
            ),
        )
