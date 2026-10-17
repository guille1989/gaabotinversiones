#!/bin/sh
# Ejecutar con sudo cuando el DNS de bot.theinnoapp.com apunte a esta instancia.
set -eu
origen=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
test -s /etc/nginx/gaainversiones.htpasswd

python3 - <<'PY'
import socket
ips = {r[4][0] for r in socket.getaddrinfo('bot.theinnoapp.com', 443, socket.AF_INET)}
if ips != {'54.205.214.211'}:
    raise SystemExit('DNS pendiente: bot.theinnoapp.com debe resolver únicamente a 54.205.214.211')
PY

install -d -m 0755 /etc/letsencrypt/renewal-hooks/deploy
cat > /etc/letsencrypt/renewal-hooks/deploy/reload-nginx <<'HOOK'
#!/bin/sh
set -eu
/usr/sbin/nginx -t
/usr/bin/systemctl reload nginx
HOOK
chmod 0755 /etc/letsencrypt/renewal-hooks/deploy/reload-nginx

certbot certonly --webroot --webroot-path /var/lib/letsencrypt \
    --non-interactive --agree-tos --register-unsafely-without-email \
    -d bot.theinnoapp.com
install -m 0644 "$origen/panel-nginx.conf" /etc/nginx/sites-available/gaainversiones
nginx -t
systemctl reload nginx
systemctl enable --now certbot.timer
curl --silent --output /dev/null --write-out 'Sin credenciales: HTTP %{http_code}\n' https://bot.theinnoapp.com/
