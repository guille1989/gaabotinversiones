"""Panel web de GaaInversiones (Streamlit). SOLO LECTURA: no envía órdenes ni toca el estado.

Se arranca con ``gaa panel`` (que pasa la configuración por variables de entorno):

- ``GAA_PANEL_CONFIG``: ruta de config.yaml (por defecto config/config.yaml).
- ``GAA_PANEL_MODO``: paper | real (por defecto paper).
- ``GAA_PANEL_SIN_RED``: si vale 1, la pestaña Mercado usa solo la caché local de velas.
"""

from __future__ import annotations

import os
from datetime import timedelta
from html import escape

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components

from gaainversiones.config import Config, cargar_config
from gaainversiones.datos.fuentes import velas_a_dataframe
from gaainversiones.exchange import crear_exchange
from gaainversiones.fiscal.exportar import formato_fiscal
from gaainversiones.formato import formato_dinero, formato_pct
from gaainversiones.panel.datos import (
    EstadoBot,
    html_autonomo,
    leer_estado,
    leer_tabla,
    leer_velas_cache,
    listar_informes,
    niveles_estrategia,
)
from gaainversiones.tiempo import ahora_ms, timeframe_a_ms

AZUL, GRIS, VERDE, ROJO = "#2456a6", "#94a3b8", "#059669", "#dc2626"

ESTILOS = """
<style>
.stApp {background: #f4f7fb; color: #172b4d;}
[data-testid="stHeader"] {background: rgba(244,247,251,.95);}
.block-container {max-width: 1240px; padding-top: 2rem; padding-bottom: 2rem;}
h1 {letter-spacing: -.045em; font-weight: 750 !important; color: #102a43;}
h2, h3 {letter-spacing: -.02em; color: #17324d;}
[data-testid="stMetric"] {
  background: white; border: 1px solid #dfe7f0; border-radius: 14px;
  padding: 18px; box-shadow: 0 3px 12px rgba(16,42,67,.035);
}
[data-testid="stMetricLabel"] {color: #52657b; font-size: .87rem;}
[data-testid="stMetricValue"] {color: #102a43; font-size: 1.8rem;}
[data-testid="stSidebar"] {background: #eaf0f7; border-right: 1px solid #dae3ef;}
[data-testid="stSidebar"] .block-container {padding-top: 2rem;}
[data-testid="stPlotlyChart"] {
  border: 1px solid #dfe7f0; border-radius: 14px; overflow: hidden; background: white;
}
[data-testid="stDataFrame"] {border-radius: 12px; overflow: hidden;}
.marca {font-size: .78rem; font-weight: 700; letter-spacing: .13em; color: #52657b;}
.cabecera {display:flex; justify-content:space-between; align-items:center;
  gap:1rem; margin:1rem 0;}
.modo {background:#fff2d6; color:#775000; border:1px solid #f0d592; border-radius:999px;
  padding:.4rem .8rem; font-size:.8rem; font-weight:650;}
.modo.real {background:#fee2e2; color:#991b1b; border-color:#fecaca;}
.estado {border:1px solid #dce5ef; border-left:5px solid #059669; background:#fff;
  padding:1.2rem 1.4rem; border-radius:14px; margin:.5rem 0 1rem;}
.estado.aviso {border-left-color:#d97706;}
.estado h2 {font-size:1.35rem; margin:0 0 .35rem;}
.estado p {margin:0; color:#52657b; line-height:1.6;}
.paso {color:#2456a6; font-size:.75rem; font-weight:750;
  letter-spacing:.08em; margin:1.6rem 0 .4rem;}
[data-testid="stVerticalBlockBorderWrapper"] {background:white; border-radius:14px;}
div[role="radiogroup"] {gap: .4rem; margin: .5rem 0 1rem; flex-wrap: wrap;}
div[role="radiogroup"] label {
  background: white; border: 1px solid #dce5ef; border-radius: 10px; padding: .45rem .75rem;
}
div[role="radiogroup"] label:has(input:checked) {background:#e6efff; border-color:#2456a6;}
@media (max-width: 1000px) {
  [data-testid="stHorizontalBlock"]:has([data-testid="stMetric"]) {flex-wrap: wrap;}
  [data-testid="stHorizontalBlock"]:has([data-testid="stMetric"]) > [data-testid="stColumn"] {
    flex: 1 1 calc(50% - 1rem); min-width: 0;
  }
}
@media (max-width: 640px) {
  .block-container {padding: 1.2rem .9rem 2rem;}
  h1 {font-size: 2rem !important;}
  [data-testid="stMetric"] {padding: 12px;}
  [data-testid="stMetricValue"] {font-size: 1.45rem;}
  div[role="radiogroup"] {gap: .2rem;}
  div[role="radiogroup"] label {padding: .3rem .45rem;}
}
@media (max-width: 480px) {
  [data-testid="stMetricValue"] {font-size: 1.15rem;}
}
</style>
"""


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
        font={"family": "Arial, sans-serif", "color": "#52657b"},
        paper_bgcolor="white",
        plot_bgcolor="white",
    )
    return figura


# --- Bot ----------------------------------------------------------------------------------


def dinero(valor: float | None, moneda: str, *, signo: bool = False) -> str:
    return formato_dinero(valor, moneda, signo=signo).replace(" EUR", " €")


def fecha_local(fecha: pd.Timestamp) -> str:
    return fecha.tz_convert("Europe/Madrid").strftime("%d/%m/%Y a las %H:%M")


def paso(numero: str, titulo: str) -> None:
    st.markdown(f'<div class="paso">{numero} / {escape(titulo)}</div>', unsafe_allow_html=True)


def seccion_bot(cfg: Config, modo: str, estado: EstadoBot) -> None:
    if not estado.existe:
        st.info("El robot todavía no ha registrado actividad en este modo.")
        return
    cot = cfg.mercado.cotizada
    ruta = cfg.ruta_db_estado(modo)
    operaciones = leer_tabla(ruta, "operaciones")
    resultado = float(operaciones["resultado"].sum()) if not operaciones.empty else 0.0
    paso("01", "TU DINERO")
    columnas = st.columns(2)
    columnas[0].metric(
        "Dinero de la cuenta",
        dinero(estado.capital, cot),
        help="Efectivo más el valor de Bitcoin al último cierre procesado por el robot.",
    )
    columnas[1].metric(
        "Ganado o perdido",
        dinero(resultado, cot, signo=True),
        help="Operaciones terminadas, con comisiones descontadas. Excluye la compra abierta.",
    )
    st.caption(
        f"{len(operaciones)} operaciones cerradas · "
        f"{dinero(estado.efectivo, cot)} disponibles para comprar. "
        "Los resultados se muestran antes de impuestos."
    )
    paso("02", "QUÉ ESTÁ HACIENDO")
    if estado.posicion:
        with st.container(border=True):
            st.subheader(f"Tiene una compra de {cfg.mercado.base} abierta")
            st.write("El robot mantiene la compra y vigila las condiciones de salida.")
            columnas = st.columns(2)
            columnas[0].metric(
                "Resultado de la compra abierta", dinero(estado.latente, cot, signo=True)
            )
            columnas[1].metric(
                "Precio de compra",
                dinero(float(estado.posicion["precio_entrada"]), cot),  # type: ignore[arg-type]
            )
            st.caption(
                "El resultado abierto cambia con el precio; todavía no se ha realizado la venta."
            )
            st.write(
                f"**Salida de protección (stop):** {dinero(float(estado.posicion['stop']), cot)}. "  # type: ignore[arg-type]
                "Si el precio llega a ese nivel, el robot intenta cerrar la compra."
            )
            if cfg.riesgo.tp_atr > 0:
                st.write(
                    f"**Objetivo de venta:** {dinero(float(estado.posicion['objetivo']), cot)}."  # type: ignore[arg-type]
                )
    else:
        with st.container(border=True):
            st.subheader("No hay ninguna compra abierta")
            if estado.en_marcha and estado.estado == "activo":
                st.write("Tu dinero está disponible. El robot espera una señal de compra.")
                st.caption("Puede pasar días sin comprar: espera a que se cumplan sus reglas.")
            else:
                st.write(
                    "El dinero está disponible. Revisa el aviso de estado antes de esperar compras."
                )
    with st.expander("¿Cuándo toma la siguiente decisión?"):
        tf = timeframe_a_ms(cfg.mercado.temporalidad)
        proximo = pd.Timestamp((ahora_ms() // tf + 1) * tf, unit="ms", tz="UTC")
        st.write(
            f"Próximo cierre del mercado que revisará: **{fecha_local(proximo)} (hora de Madrid)**."
        )
        st.write(
            "La estrategia decide al terminar cada periodo de precio. Entre cierres, el robot "
            "revisa sus protecciones. Actualizar esta página no provoca compras."
        )
        if estado.ultima_vela_cierre is not None:
            st.caption(
                f"Último cierre procesado: {fecha_local(estado.ultima_vela_cierre)} · Madrid."
            )
        if not estado.en_marcha or estado.estado != "activo":
            st.warning(
                "Es el horario previsto. El robot debe estar conectado y habilitado para operar."
            )

    paso("03", "CÓMO VA TU CUENTA")
    capital = leer_tabla(ruta, "capital")
    if len(capital) < 2:
        st.info("Todavía hay pocos datos. La gráfica aparecerá después de dos cierres del mercado.")
    else:
        st.subheader("Evolución de tu dinero")
        comparar = st.checkbox("Comparar con haber comprado Bitcoin y mantenerlo", value=False)
        figura = go.Figure()
        fechas = capital["fecha"].dt.tz_convert("Europe/Madrid")
        if comparar:
            base = float(capital["equity"].iloc[0])
            referencia = base * capital["precio"] / float(capital["precio"].iloc[0])
            figura.add_scatter(
                x=fechas, y=referencia, name="Comprar Bitcoin y mantener", line={"color": GRIS}
            )
        figura.add_scatter(
            x=fechas,
            y=capital["equity"],
            name="Tu cuenta",
            line={"color": AZUL, "width": 3},
        )
        figura.update_yaxes(title="Euros" if cot == "EUR" else cot, rangemode="normal")
        figura.update_xaxes(title="Hora de Madrid")
        st.plotly_chart(_layout(figura, 300), width="stretch", config={"displayModeBar": False})
        if not estado.posicion and operaciones.empty:
            st.caption("La cuenta sigue sin operaciones. Por eso el saldo se mantiene igual.")
    with st.expander("Ver la estrategia y los límites de pérdida"):
        st.write(f"**Mercado:** {cfg.mercado.simbolo} en {cfg.mercado.exchange.capitalize()}.")
        if cfg.estrategia.nombre == "tendencia_media":
            periodo = cfg.estrategia.parametros.get("periodo", 100)
            st.write(
                f"Compara el precio con su media de **{periodo} periodos**. "
                "Compra al cruzar la banda superior; vende al cruzar la inferior "
                "o al activarse su protección."
            )
        else:
            st.write(f"**Estrategia:** {cfg.estrategia.nombre.replace('_', ' ')}.")
        st.write(
            f"**Riesgo previsto por compra:** {formato_pct(cfg.riesgo.riesgo_por_operacion)} "
            "del dinero de la cuenta."
        )
        st.write(
            f"**Pérdida diaria límite:** {formato_pct(cfg.riesgo.perdida_diaria_max)}; "
            "pausa hasta el siguiente día UTC."
        )
        st.write(
            f"**Caída límite desde el máximo:** {formato_pct(cfg.riesgo.caida_max)}; "
            "requiere reanudar a mano."
        )
        st.progress(
            min(max(-estado.caida / cfg.riesgo.caida_max, 0.0), 1.0),
            text=f"Caída actual desde el máximo: {formato_pct(max(0.0, -estado.caida))}",
        )
    eventos = leer_tabla(ruta, "eventos")
    if not eventos.empty:
        with st.expander("Registro técnico y avisos"):
            recientes = eventos.sort_values("fecha", ascending=False).head(15).copy()
            recientes["fecha"] = recientes["fecha"].dt.tz_convert("Europe/Madrid")
            st.dataframe(
                recientes[["fecha", "nivel", "texto"]],
                hide_index=True,
                width="stretch",
                column_config={
                    "fecha": st.column_config.DatetimeColumn(
                        "Fecha · Madrid", format="DD/MM/YYYY HH:mm"
                    ),
                    "nivel": "Tipo",
                    "texto": "Detalle",
                },
            )


# --- Operaciones --------------------------------------------------------------------------


def seccion_operaciones(cfg: Config, modo: str) -> None:
    st.subheader("Tus compras y ventas")
    st.caption("Aquí aparecen las operaciones del robot, con sus comisiones y resultados.")
    ruta = cfg.ruta_db_estado(modo)
    operaciones = leer_tabla(ruta, "operaciones")
    ejecuciones = leer_tabla(ruta, "ejecuciones")
    if ejecuciones.empty:
        st.info("Todavía no ha comprado ni vendido. Cuando haga su primera compra, la verás aquí.")
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
    st.subheader("Movimientos y descarga")
    st.caption(
        "Una operación puede incluir varios movimientos. Este registro sirve "
        "para revisar comisiones y preparar tus impuestos."
    )
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


@st.cache_data(ttl=30, show_spinner="Consultando el mercado…")
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
    velas = leer_velas_cache(cfg)
    return velas.tail(720), f"{cfg.datos.fuente_historica} (caché local)"


def seccion_mercado(cfg: Config, modo: str) -> None:
    st.subheader(f"El precio de {cfg.mercado.base}")
    st.write(
        "El gráfico muestra el precio y las referencias de la estrategia. "
        "Sus niveles no garantizan que el robot vaya a operar."
    )
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
    dias = st.select_slider("Periodos de precio a mostrar", options=[90, 180, 365, 720], value=90)
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
        f"Datos: {origen}. Cada vela resume un periodo {cfg.mercado.temporalidad}. "
        f"Último periodo cerrado: {fecha_local(cerro)} · Madrid. No es el precio de este instante."
    )
    if "Entrada (banda superior)" in lineas:
        entrada = float(lineas["Entrada (banda superior)"].iloc[-1])
        salida = float(lineas["Salida (banda inferior)"].iloc[-1])
        media = float(lineas[lineas.columns[0]].iloc[-1])
        columnas = st.columns(3)
        columnas[0].metric(
            "Último cierre",
            formato_dinero(cierre, cot),
            formato_pct(cierre / media - 1, signo=True) + " vs media",
        )
        columnas[1].metric("Banda de entrada", formato_dinero(entrada, cot))
        columnas[2].metric("Banda de salida", formato_dinero(salida, cot))
        if cierre > entrada:
            st.info(
                "El último cierre está por encima de la banda de compra. Estar arriba no basta: "
                "una nueva señal exige pasar de estar dentro o debajo de esa banda a superarla. "
                "El estado y los límites de la cuenta también deciden si puede comprar."
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
    st.subheader("Pruebas con precios del pasado")
    st.write(
        "Estos informes simulan una estrategia con datos del pasado. "
        "Sus ganancias no son las de tu cuenta actual."
    )
    informes = listar_informes(cfg.dir_informes)
    if not informes:
        st.info("Todavía no hay informes históricos disponibles en este servidor.")
        return
    st.caption(
        "Simulaciones históricas con la configuración de cada informe. "
        "Pueden diferir de la estrategia actual y no predicen ganancias futuras."
    )
    elegido = st.selectbox(
        "Elegir una prueba",
        informes,
        format_func=lambda i: (
            f"{i.fecha:%d/%m/%Y %H:%M} UTC · "
            f"{'Prueba por etapas' if i.tipo == 'walkforward' else 'Simulación histórica'} · "
            f"{i.estrategia.replace('_', ' ')}"
        ),
    )
    if elegido is not None:
        st.caption(f"{elegido.tipo.capitalize()} · {elegido.fecha:%d/%m/%Y %H:%M} UTC")
        components.html(html_autonomo(elegido.ruta), height=1600, scrolling=True)


# --- Página -------------------------------------------------------------------------------


def vista_actual(cfg: Config, modo: str, vista: str) -> None:
    estado = leer_estado(cfg, modo, ahora_ms())
    if estado.en_marcha and estado.estado == "activo":
        titulo = "Tu robot está funcionando"
        detalle = (
            "Vigila una compra abierta."
            if estado.posicion
            else "Está conectado y esperando una oportunidad de compra."
        )
        aviso = False
    elif estado.en_marcha:
        titulo = "El robot está conectado, pero no puede abrir compras"
        detalle = (
            estado.motivo or f"Estado guardado: {estado.estado}. Revisa sus límites o los avisos."
        )
        aviso = True
    else:
        titulo = "No hay conexión reciente con el robot"
        detalle = (
            "Los datos son el último estado guardado. No confirman que el robot siga operando."
        )
        aviso = True
    st.markdown(
        f'<div class="estado{" aviso" if aviso else ""}"><h2>{escape(titulo)}</h2>'
        f"<p>{escape(detalle)}</p></div>",
        unsafe_allow_html=True,
    )
    actualizado = pd.Timestamp(ahora_ms(), unit="ms", tz="UTC").tz_convert("Europe/Madrid")
    st.caption(
        f"Actualizado a las {actualizado:%H:%M:%S} · hora de Madrid · actualización cada 30 s"
    )
    if vista == "Mi robot":
        seccion_bot(cfg, modo, estado)
    elif vista == "Historial":
        seccion_operaciones(cfg, modo)
    elif vista == "Precio y estrategia":
        seccion_mercado(cfg, modo)
    else:
        seccion_backtests(cfg)


def main() -> None:
    st.set_page_config(
        page_title="Mi robot · GaaInversiones",
        page_icon="📈",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    st.markdown(ESTILOS, unsafe_allow_html=True)
    cfg = _config(os.environ.get("GAA_PANEL_CONFIG"))
    modo = os.environ.get("GAA_PANEL_MODO", "paper")
    st.markdown(
        '<div class="cabecera"><span class="marca">GAA / INVERSIONES</span>'
        f'<span class="modo{" real" if modo == "real" else ""}">'
        f"{'Dinero real' if modo == 'real' else 'Simulación · Sin dinero real'}</span></div>",
        unsafe_allow_html=True,
    )
    st.title("Tu robot, de un vistazo")
    if modo == "paper":
        st.caption("Cuenta de prueba con dinero simulado · Ningún euro real está invertido aquí.")
    else:
        st.caption("Cuenta con dinero real · Estado, saldo y operaciones de tu robot.")
    vista = st.radio(
        "Sección",
        ["Mi robot", "Historial", "Precio y estrategia", "Simulaciones"],
        horizontal=True,
        key="vista_panel",
        label_visibility="collapsed",
    )
    st.fragment(run_every=None if vista == "Simulaciones" else 30)(vista_actual)(cfg, modo, vista)
    st.divider()
    st.caption(
        "Panel de solo lectura. Gestiona el robot desde Telegram. "
        "El registro fiscal y los informes históricos conservan sus fechas UTC."
    )


main()
