#!/bin/sh
# Instala el proyecto en el directorio preparado por lightsail-inicio.sh.
# Registra servicios, pero no arranca el bot: primero hay que migrar su estado.
set -eu
test "$(id -u)" -eq 0
test -x /opt/gaainversiones/.venv/bin/python
test -f /opt/gaainversiones/requirements.txt

runuser -u gaa -- /opt/gaainversiones/.venv/bin/pip install \
    -r /opt/gaainversiones/requirements.txt
runuser -u gaa -- /opt/gaainversiones/.venv/bin/pip install \
    -e /opt/gaainversiones --no-deps
runuser -u gaa -- /opt/gaainversiones/.venv/bin/pip check

install -d -o gaa -g gaa -m 0750 \
    /opt/gaainversiones/data /opt/gaainversiones/logs /opt/gaainversiones/copias
if [ ! -e /opt/gaainversiones/.env ]; then
    install -o gaa -g gaa -m 0600 /opt/gaainversiones/.env.example /opt/gaainversiones/.env
fi
for unit in gaainversiones-paper.service gaainversiones-copia.service gaainversiones-copia.timer; do
    install -m 0644 "/opt/gaainversiones/deploy/linux/$unit" "/etc/systemd/system/$unit"
done
systemd-analyze verify /etc/systemd/system/gaainversiones-paper.service \
    /etc/systemd/system/gaainversiones-copia.service \
    /etc/systemd/system/gaainversiones-copia.timer
systemctl daemon-reload
