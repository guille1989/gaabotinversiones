"""Panel web de GaaInversiones (Streamlit). SOLO LECTURA: no envía órdenes ni toca el estado.

Se arranca con ``gaa panel`` (que pasa la configuración por variables de entorno):

- ``GAA_PANEL_CONFIG``: ruta de config.yaml (por defecto config/config.yaml).
- ``GAA_PANEL_MODO``: paper | real (por defecto paper).
- ``GAA_PANEL_SIN_RED``: si vale 1, la pestaña Mercado usa solo la caché local de velas.
"""

from __future__ import annotations

import os
from datetime import timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components

from gaainversiones.config import Config, cargar_config
from gaainversiones.datos import CacheVelas, cargar_velas
from gaainversiones.datos.fuentes import velas_a_dataframe
from gaainversiones.exchange import crear_exchange
from gaainversiones.fiscal.exportar import formato_fiscal
from gaainversiones.formato import formato_dinero, formato_pct
from gaainversiones.panel.datos import (
    html_autonomo,
    leer_estado,
    leer_tabla,
    listar_informes,
    niveles_estrategia,
)
from gaainversiones.tiempo import ahora_ms, timeframe_a_ms

AZUL, GRIS, VERDE, ROJO = "#2563eb", "#9ca3af", "#16a34a", "#dc2626"


@st.cache_resource
def _config(ruta: str | None) -> Config:
    return cargar_config(ruta)


def _layout(figura: go.Figure, altura: int = 380) -> go.Figure:
    figura.update_layout(
        template="plotly_white",
        height=altura,
        margin={"l": 10, "r": 10, "t": 30, "b": 10},
        legend={"orientation": "h", "y": 1.08, "x": 0},
        hovermode="x unified",
    )
    return figura


# --- Bot ----------------------------------------------------------------------------------


def seccion_bot(cfg: Config, modo: str) -> None:
    estado = leer_estado(cfg, modo, ahora_ms())
    if not estado.existe:
        st.info(f"Aún no hay estado en modo {modo}: arranca el bot con `gaa {modo}`.")
        return
    cot = cfg.mercado.cotizada
    if estado.en_marcha:
        st.success(f"Bot en marcha · último latido {estado.latido:%Y-%m-%d %H:%M:%S} UTC")
    else:
        st.warning(
            "El bot no está en marcha"
            + (f" (último latido {estado.latido:%Y-%m-%d %H:%M} UTC)" if estado.latido else "")
        )
    columnas = st.columns(4)
    columnas[0].metric(
        "Estado", estado.estado, help=estado.motivo or "Opera con normalidad", delta=None
    )
    columnas[1].metric("Capital", formato_dinero(estado.capital, cot))
    columnas[2].metric("Caída desde el máximo", formato_pct(estado.caida))
    if estado.posicion:
        latente = estado.latente
        columnas[3].metric(
            "Posición",
            f"{float(estado.posicion['cantidad']):.6f} {cfg.mercado.base}",  # type: ignore[arg-type]
            delta=formato_dinero(latente, cot, signo=True) if latente is not None else None,
        )
        st.caption(
            f"Entrada {formato_dinero(float(estado.posicion['precio_entrada']), cot)} · "  # type: ignore[arg-type]
            f"stop {formato_dinero(float(estado.posicion['stop']), cot)} · objetivo "  # type: ignore[arg-type]
            f"{formato_dinero(float(estado.posicion['objetivo']), cot)}"  # type: ignore[arg-type]
        )
    else:
        columnas[3].metric("Posición", "ninguna")
    if estado.motivo:
        st.caption(f"Motivo del estado: {estado.motivo}")
    if estado.ultima_vela_cierre is not None:
        st.caption(
            f"Última vela procesada: la que cerró el {estado.ultima_vela_cierre:%Y-%m-%d %H:%M} UTC"
        )

    ruta = cfg.ruta_db_estado(modo)
    capital = leer_tabla(ruta, "capital")
    if len(capital) < 2:
        st.caption("La curva de capital aparecerá cuando el bot haya procesado al menos dos velas.")
    else:
        base = float(capital["equity"].iloc[0])
        referencia = base * capital["precio"] / float(capital["precio"].iloc[0])
        figura = go.Figure()
        figura.add_scatter(
            x=capital["fecha"], y=referencia, name="Comprar y mantener", line={"color": GRIS}
        )
        figura.add_scatter(
            x=capital["fecha"], y=capital["equity"], name="Bot", line={"color": AZUL}
        )
        figura.update_yaxes(title=cot)
        st.plotly_chart(_layout(figura), width="stretch")
    eventos = leer_tabla(ruta, "eventos")
    if not eventos.empty:
        st.subheader("Últimos eventos")
        recientes = eventos.sort_values("fecha", ascending=False).head(15)
        st.dataframe(
            recientes[["fecha", "nivel", "texto"]],
            hide_index=True,
            width="stretch",
            column_config={
                "fecha": st.column_config.DatetimeColumn("Fecha (UTC)", format="YYYY-MM-DD HH:mm")
            },
        )


# --- Operaciones --------------------------------------------------------------------------


def seccion_operaciones(cfg: Config, modo: str) -> None:
    ruta = cfg.ruta_db_estado(modo)
    operaciones = leer_tabla(ruta, "operaciones")
    ejecuciones = leer_tabla(ruta, "ejecuciones")
    if ejecuciones.empty:
        st.info("Todavía no hay operaciones.")
        return
    cot = cfg.mercado.cotizada
    if not operaciones.empty:
        columnas = st.columns(4)
        resultado = float(operaciones["resultado"].sum())
        columnas[0].metric("Operaciones cerradas", len(operaciones))
        columnas[1].metric(
            "Ganadoras", formato_pct(float((operaciones["resultado"] > 0).mean()), 0)
        )
        columnas[2].metric("Resultado neto", formato_dinero(resultado, cot, signo=True))
        columnas[3].metric(
            "Comisiones", formato_dinero(float(operaciones["comisiones"].sum()), cot)
        )
        st.subheader("Operaciones cerradas")
        tabla = operaciones.assign(
            rentabilidad=operaciones["resultado"] / operaciones["coste_entrada"]
        )
        st.dataframe(
            tabla[
                [
                    "fecha_entrada",
                    "precio_entrada",
                    "fecha_salida",
                    "precio_salida",
                    "cantidad",
                    "comisiones",
                    "resultado",
                    "rentabilidad",
                    "motivo_salida",
                ]
            ],
            hide_index=True,
            width="stretch",
            column_config={
                "fecha_entrada": st.column_config.DatetimeColumn(
                    "Entrada", format="YYYY-MM-DD HH:mm"
                ),
                "fecha_salida": st.column_config.DatetimeColumn(
                    "Salida", format="YYYY-MM-DD HH:mm"
                ),
                "rentabilidad": st.column_config.NumberColumn("Rentab.", format="percent"),
                "resultado": st.column_config.NumberColumn("Resultado", format="%.2f"),
                "motivo_salida": "Motivo",
            },
        )
    st.subheader("Ejecuciones (registro fiscal)")
    fechas = ejecuciones["fecha"]
    rango = st.date_input(
        "Periodo", (fechas.min().date(), fechas.max().date()), format="YYYY-MM-DD"
    )
    filtro = ejecuciones
    if isinstance(rango, tuple) and len(rango) == 2:  # mientras se elige, llega solo una fecha
        desde, hasta = rango
        filtro = ejecuciones[
            (fechas.dt.date >= desde) & (fechas.dt.date < hasta + timedelta(days=1))
        ]
    fiscal = formato_fiscal(filtro)
    st.dataframe(fiscal, hide_index=True, width="stretch")
    st.download_button(
        "Descargar CSV (Excel en español)",
        fiscal.to_csv(index=False, sep=";", decimal=",").encode("utf-8-sig"),
        file_name=f"ejecuciones_{modo}.csv",
        mime="text/csv",
    )


# --- Mercado ------------------------------------------------------------------------------


@st.cache_data(ttl=600, show_spinner="Descargando velas de Kraken…")
def _velas_exchange(exchange: str, simbolo: str, temporalidad: str) -> pd.DataFrame:
    cliente = crear_exchange(exchange)
    filas = cliente.fetch_ohlcv(simbolo, temporalidad, limit=720)
    tf = timeframe_a_ms(temporalidad)
    ahora = ahora_ms()
    return velas_a_dataframe([f for f in filas if f[0] + tf <= ahora])  # solo velas cerradas


def _velas_mercado(cfg: Config) -> tuple[pd.DataFrame, str]:
    m = cfg.mercado
    if os.environ.get("GAA_PANEL_SIN_RED") != "1":
        try:
            return _velas_exchange(m.exchange, m.simbolo, m.temporalidad), f"{m.exchange} (en vivo)"
        except Exception as e:  # sin red o exchange caído: caché local
            st.caption(f"No se pudo leer {m.exchange} ({type(e).__name__}); uso la caché local.")
    velas = cargar_velas(
        CacheVelas(cfg.ruta_db_velas), cfg.datos.fuente_historica, m.simbolo, m.temporalidad
    )
    return velas.tail(720), f"{cfg.datos.fuente_historica} (caché local)"


def seccion_mercado(cfg: Config, modo: str) -> None:
    try:
        velas, origen = _velas_mercado(cfg)
    except ValueError as e:
        st.info(f"No hay velas que mostrar: {e}")
        return
    if velas.empty:
        st.info("No hay velas que mostrar.")
        return
    estrategia = cfg.estrategia
    lineas = niveles_estrategia(velas, estrategia.nombre, dict(estrategia.parametros))
    dias = st.select_slider("Velas a mostrar", options=[90, 180, 365, 720], value=365)
    tramo = velas.tail(dias)
    figura = go.Figure()
    figura.add_candlestick(
        x=tramo.index,
        open=tramo["apertura"],
        high=tramo["maximo"],
        low=tramo["minimo"],
        close=tramo["cierre"],
        name=cfg.mercado.simbolo,
        increasing_line_color=VERDE,
        decreasing_line_color=ROJO,
    )
    estilos = [
        {"color": AZUL, "width": 2},
        {"color": VERDE, "dash": "dash"},
        {"color": ROJO, "dash": "dash"},
    ]
    for estilo, columna in zip(estilos, lineas.columns, strict=False):
        figura.add_scatter(
            x=tramo.index, y=lineas[columna].loc[tramo.index], name=columna, line=estilo
        )
    ejecuciones = leer_tabla(cfg.ruta_db_estado(modo), "ejecuciones")
    if not ejecuciones.empty:
        for lado, color, simbolo in (
            ("compra", VERDE, "triangle-up"),
            ("venta", ROJO, "triangle-down"),
        ):
            parte = ejecuciones[ejecuciones["lado"] == lado]
            figura.add_scatter(
                x=parte["fecha"],
                y=parte["precio"],
                mode="markers",
                name=f"Bot: {lado}",
                marker={"symbol": simbolo, "size": 11, "color": color},
            )
    figura.update_layout(xaxis_rangeslider_visible=False)
    st.plotly_chart(_layout(figura, 520), width="stretch")

    cierre = float(velas["cierre"].iloc[-1])
    cot = cfg.mercado.cotizada
    cerro = velas.index[-1] + pd.Timedelta(milliseconds=timeframe_a_ms(cfg.mercado.temporalidad))
    st.caption(
        f"Datos: {origen}. Última vela cerrada: la de {velas.index[-1]:%Y-%m-%d %H:%M} "
        f"(cerró el {cerro:%Y-%m-%d %H:%M} UTC)."
    )
    if "Entrada (banda superior)" in lineas:
        entrada = float(lineas["Entrada (banda superior)"].iloc[-1])
        salida = float(lineas["Salida (banda inferior)"].iloc[-1])
        media = float(lineas[lineas.columns[0]].iloc[-1])
        columnas = st.columns(3)
        columnas[0].metric(
            "Cierre",
            formato_dinero(cierre, cot),
            formato_pct(cierre / media - 1, signo=True) + " vs media",
        )
        columnas[1].metric("Banda de entrada", formato_dinero(entrada, cot))
        columnas[2].metric("Banda de salida", formato_dinero(salida, cot))
        if cierre > entrada:
            st.info(
                "El precio está por encima de la banda de entrada. La estrategia compra en el "
                "**cruce** al alza: si no tiene posición, esperará a que el cierre baje de la "
                f"banda de salida ({formato_dinero(salida, cot)}) y vuelva a superar la de entrada."
            )
        elif cierre < salida:
            st.info(
                f"El precio está por debajo de la banda de salida. Comprará cuando un cierre "
                f"supere la banda de entrada (hoy {formato_dinero(entrada, cot)})."
            )
        else:
            st.info("El precio está entre las dos bandas: no hay señal.")


# --- Backtests ----------------------------------------------------------------------------


def seccion_backtests(cfg: Config) -> None:
    informes = listar_informes(cfg.dir_informes)
    if not informes:
        st.info("No hay informes: genera uno con `gaa backtest` o `gaa walkforward`.")
        return
    elegido = st.selectbox("Informe", informes, format_func=lambda i: i.titulo)
    if elegido is not None:
        st.caption(f"Fichero: {elegido.ruta}")
        components.html(html_autonomo(elegido.ruta), height=1600, scrolling=True)


# --- Página -------------------------------------------------------------------------------


def main() -> None:
    st.set_page_config(page_title="GaaInversiones", page_icon="📈", layout="wide")
    cfg = _config(os.environ.get("GAA_PANEL_CONFIG"))
    modo = os.environ.get("GAA_PANEL_MODO", "paper")
    st.title("GaaInversiones")
    st.caption(
        f"Modo **{modo}** · {cfg.mercado.simbolo} {cfg.mercado.temporalidad} · estrategia "
        f"`{cfg.estrategia.nombre}` · panel de solo lectura (se actualiza cada 30 s)"
    )
    bot, operaciones, mercado, backtests = st.tabs(["Bot", "Operaciones", "Mercado", "Backtests"])
    with bot:
        st.fragment(run_every=30)(seccion_bot)(cfg, modo)
    with operaciones:
        seccion_operaciones(cfg, modo)
    with mercado:
        seccion_mercado(cfg, modo)
    with backtests:
        seccion_backtests(cfg)
    st.caption(
        "Para actuar sobre el bot usa la terminal (`gaa parar`, `gaa reanudar`) o Telegram "
        "(/estado, /parar). Fechas en UTC."
    )


main()
