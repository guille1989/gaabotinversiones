"""Reconciliación al arrancar: ¿lo que el bot cree coincide con lo que hay en el exchange?

Si no coincide, el bot no opera: avisa y espera a que una persona lo revise. Nunca
"arregla" saldos por su cuenta. La única corrección automática es proteger una
posición que se ha quedado sin stop-loss en el exchange.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from gaainversiones.cuenta import EstadoCuenta
from gaainversiones.ejecucion.base import Broker


class ErrorReconciliacion(Exception):
    """El estado guardado y el del exchange no cuadran."""


@dataclass
class ResultadoReconciliacion:
    discrepancias: list[str] = field(default_factory=list)
    falta_stop: bool = False

    @property
    def ok(self) -> bool:
        return not self.discrepancias


def reconciliar(
    cuenta: EstadoCuenta, broker: Broker, *, cuenta_exclusiva: bool
) -> ResultadoReconciliacion:
    """Compara la cuenta del bot con los saldos y órdenes del broker.

    Args:
        cuenta_exclusiva: True si todo el saldo de la moneda cotizada es del bot (paper);
            entonces el efectivo debe coincidir. En real basta con que haya al menos el
            efectivo que el bot cree tener.
    """
    resultado = ResultadoReconciliacion()
    saldos = broker.saldos()
    limites = broker.limites()
    tolerancia = 10 * 10 ** (-limites.decimales_cantidad)

    posicion = cuenta.posicion
    esperada = posicion.cantidad if posicion else 0.0
    if abs(saldos.base - esperada) > tolerancia:
        resultado.discrepancias.append(
            f"Saldo de la moneda base: el exchange tiene {saldos.base:.8f} y el bot "
            f"espera {esperada:.8f}"
        )
    if cuenta_exclusiva and abs(saldos.cotizada - cuenta.efectivo) > 0.01:
        resultado.discrepancias.append(
            f"Efectivo: el exchange tiene {saldos.cotizada:.2f} y el bot espera "
            f"{cuenta.efectivo:.2f}"
        )
    elif not cuenta_exclusiva and saldos.cotizada + 0.01 < cuenta.efectivo:
        resultado.discrepancias.append(
            f"Falta efectivo: el exchange tiene {saldos.cotizada:.2f} y el bot espera "
            f"al menos {cuenta.efectivo:.2f}"
        )

    ids = {orden.id for orden in broker.ordenes_stop()}
    propias = {posicion.id_orden_stop} if posicion and posicion.id_orden_stop else set()
    if posicion is not None and not (propias & ids):
        resultado.falta_stop = True
    desconocidas = ids - propias
    if desconocidas:
        resultado.discrepancias.append(
            f"Órdenes stop que el bot no reconoce: {', '.join(sorted(desconocidas))}"
        )
    return resultado
