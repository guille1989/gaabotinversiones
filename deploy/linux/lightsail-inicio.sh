#!/bin/sh
# Preparación inicial de Ubuntu 24.04 para GaaInversiones.
# Se ejecuta como root mediante los datos de usuario de Lightsail.
# No contiene secretos ni arranca el bot: el estado se migra por separado.
# Lightsail añade su propio encabezado y puede ejecutar los datos de usuario con sh.
set -eu
export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y --no-install-recommends \
    ca-certificates git python3 python3-pip python3-venv

if ! id -u gaa >/dev/null 2>&1; then
    useradd --system --create-home --home-dir /home/gaa --shell /usr/sbin/nologin gaa
fi
install -d -o gaa -g gaa -m 0750 /opt/gaainversiones
if [ ! -x /opt/gaainversiones/.venv/bin/python ]; then
    runuser -u gaa -- python3 -m venv /opt/gaainversiones/.venv
fi
timedatectl set-timezone UTC
printf 'GaaInversiones: sistema preparado el %s\n' "$(date -u +%FT%TZ)" \
    > /var/log/gaainversiones-preparado.log
