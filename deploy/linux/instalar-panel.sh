#!/bin/sh
# Ejecutar con sudo desde una copia del proyecto o del paquete del panel.
set -eu

origen=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
destino=/opt/gaainversiones

for archivo in app.py datos.py simulacion.py; do
    install -o gaa -g gaa -m 0644 "$origen/src/gaainversiones/panel/$archivo" "$destino/src/gaainversiones/panel/$archivo"
done
if [ -d "$origen/informes" ]; then
    install -d -o gaa -g gaa -m 0750 "$destino/informes"
    find "$origen/informes" -maxdepth 1 -type f \( -name '*.html' -o -name 'plotly.min.js' \) -exec install -o gaa -g gaa -m 0640 {} "$destino/informes/" \;
fi
install -m 0644 "$origen/deploy/linux/gaainversiones-panel.service" /etc/systemd/system/gaainversiones-panel.service
systemd-analyze verify /etc/systemd/system/gaainversiones-panel.service
systemctl daemon-reload
systemctl enable gaainversiones-panel.service
systemctl restart gaainversiones-panel.service

# Hasta que exista un certificado, HTTP solo admite el desafío de Let's Encrypt.
# El panel sigue escuchando exclusivamente en loopback.
install -d -m 0755 /var/lib/letsencrypt/.well-known/acme-challenge
if [ ! -f /etc/letsencrypt/live/bot.theinnoapp.com/fullchain.pem ]; then
    cat > /etc/nginx/sites-available/gaainversiones <<'NGINX'
server {
    listen 80 default_server;
    server_name bot.theinnoapp.com;
    server_tokens off;
    location ^~ /.well-known/acme-challenge/ {
        root /var/lib/letsencrypt;
    }
    location / { return 404; }
}
NGINX
else
    test -s /etc/nginx/gaainversiones.htpasswd
    install -m 0644 "$origen/deploy/linux/panel-nginx.conf" /etc/nginx/sites-available/gaainversiones
fi
ln -sfn /etc/nginx/sites-available/gaainversiones /etc/nginx/sites-enabled/gaainversiones
if [ -L /etc/nginx/sites-enabled/default ]; then
    unlink /etc/nginx/sites-enabled/default
fi
nginx -t
systemctl enable nginx
systemctl reload nginx
curl --fail --retry 10 --retry-delay 1 --retry-connrefused http://127.0.0.1:8501/_stcore/health
