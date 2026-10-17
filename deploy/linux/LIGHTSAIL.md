# GaaInversiones en Lightsail

Servidor creado el 17 de octubre de 2026:

| Dato | Valor |
| --- | --- |
| Instancia | `gaa-bot-paper` |
| Región y zona | Virginia, `us-east-1a` |
| Sistema | Ubuntu 24.04 LTS, Python 3.12 |
| Plan | `small_3_0`: 2 GB RAM, 2 vCPU, 60 GB SSD, IPv4 |
| Coste base | 12 USD/mes, antes de impuestos |
| IP fija | `54.205.214.211` (`gaa-bot-paper-ip`) |
| Proyecto | `/opt/gaainversiones`, usuario `gaa` |
| Modo | Paper; no se activa la operativa con dinero real |

No se han contratado snapshots ni otros complementos de pago. La IP fija adjunta
está incluida en el plan. La instancia anterior `Node-js-1` se conserva.

## Acceso

Desde PowerShell, dentro de la carpeta del proyecto:

```powershell
ssh -i .deploy-private/gaa-lightsail-20261017 -o UserKnownHostsFile=.deploy-private/known_hosts ubuntu@54.205.214.211
```

La clave privada se guarda en `.deploy-private/`, excluida de Git y con permisos
limitados al usuario de Windows y SYSTEM. El puerto 22 admite la IP pública desde
la que se realizó la instalación y el terminal SSH de la consola de Lightsail.
Si cambia la IP del PC, actualizar la regla SSH desde la consola de Lightsail.

## Estado y servicios

El traslado se completó el 17 de octubre de 2026. El servicio de paper arrancó en
AWS a las 07:25 UTC (09:25 en Madrid), con 1.000 EUR simulados, sin posición y sin
operaciones cerradas. La tarea local `GaaInversiones-paper` está deshabilitada y
el bot local terminó de forma ordenada. Se comprobó que su latido quedó en cero
y que el latido de AWS avanza. No arrancar una segunda copia en el PC.

Se verificaron las dependencias, el acceso a las velas públicas de Kraken, la
integridad de SQLite y las huellas SHA-256 del paquete y de ambas bases copiadas.
La configuración de estrategia conserva la misma huella que la del PC.

Consultar en el servidor:

```bash
sudo systemctl status gaainversiones-paper --no-pager
sudo journalctl -u gaainversiones-paper -n 30 --no-pager
sudo -u gaa sh -c 'cd /opt/gaainversiones && .venv/bin/gaa estado'
```

`gaa estado` ejecutado en el PC sigue consultando la base de datos del PC.
El panel que estuviera abierto en Windows también sigue mostrando el estado local.

## Traslado del paper

1. Comprobar que el proyecto instalado recibe velas de Kraken y que sus dependencias
   y servicios son válidos.
2. Desactivar `GaaInversiones-paper` en el Programador de tareas de Windows. Este
   cambio requiere permisos de administrador en este PC.
3. Solicitar la parada ordenada del paper local y comprobar que su latido sea cero.
4. Crear copias coherentes mediante la API de backup de SQLite. Migrar únicamente
   `estado_paper.sqlite` y `velas.sqlite`; no trasladar autorizaciones del modo real.
5. Conservar solo las credenciales de Telegram necesarias para los avisos del paper.
   Mantener `GAA_PERMITIR_REAL=no` y no copiar claves de trading del exchange.
6. Verificar las copias y conservar el capital, posiciones, operaciones y curva de
   capital. Reanudar mediante el comando del proyecto solo si la parada fue manual.
7. Activar el servicio de paper y comprobar que el latido avanza y que la tarea local
   permanece deshabilitada.

En esta migración el estado previo verificado contiene 1.000 EUR simulados, máximo
de capital de 1.000 EUR, ninguna posición y ninguna operación cerrada. Reanudar una
cuenta con otras cifras exige revisar antes el efecto sobre su máximo de riesgo.

## Copias de seguridad

Los servicios `gaainversiones-copia.service` y `gaainversiones-copia.timer` realizan
copias coherentes diarias a las 03:30 UTC y conservan 14, en `copias/` del servidor.
El temporizador está habilitado y la primera copia terminó correctamente. No son
snapshots de AWS y no añaden otro recurso facturable, pero siguen en el mismo disco:
por sí solas no protegen frente a la pérdida de la instancia.

Se guardó además una copia inicial sin credenciales fuera del servidor, en el PC:
`copias/aws-inicial-20261017.tgz`, verificando que su SHA-256 coincide con el origen.
Las copias externas periódicas no están automatizadas.

```bash
sudo systemctl list-timers gaainversiones-copia.timer --no-pager
sudo systemctl start gaainversiones-copia.service
```

## Preparación inicial

`lightsail-inicio.sh` prepara Python y el usuario `gaa`. Usa sintaxis POSIX porque
Lightsail añade un encabezado propio y puede interpretar el script con `sh`.
`instalar-proyecto.sh` instala las versiones fijadas, valida las dependencias y
registra los servicios sin arrancarlos antes de migrar el estado.

## Panel de seguimiento

El servicio `gaainversiones-panel.service` está instalado y habilitado. Ejecuta
Streamlit en `127.0.0.1:8501`, separado del paper, con el sistema de archivos en
solo lectura salvo su caché privada. Las consultas SQLite usan `mode=ro`, también
para las velas. Resumen, operaciones y mercado se actualizan cada 30 segundos;
solo se ejecuta la vista seleccionada. Los informes conservan su fecha y la
configuración original de cada simulación.

La portada simplificada muestra estado, saldo, resultado de operaciones terminadas
y compra abierta. La siguiente revisión se explica por horario (Madrid), sin
confundir la actualización de la página con una orden de compra. Los límites y el
registro técnico se despliegan a petición; la comparación con comprar y mantener
está desactivada inicialmente. Las simulaciones se presentan aparte del historial
de la cuenta. Once pruebas cubren las vistas y los estados activo, pausado,
detenido y sin conexión reciente.

El 17/10/2026 se verificaron el panel por túnel SSH, el latido del bot, ocho pruebas
del panel y los análisis Ruff y mypy. El puerto 8501 sigue cerrado al público.
HTTP redirige al acceso HTTPS, salvo el desafío ACME. Los puertos 80 y 443 están
habilitados en Lightsail; la restricción previa de SSH se conserva.

Destino acordado: `https://bot.theinnoapp.com`. El DNS público está delegado a
`ns1.vercel-dns.com` y `ns2.vercel-dns.com`, aunque el registro del dominio pueda
estar en GoDaddy. **No cambiar nameservers, `@`, `www` ni los demás registros de
la web existente.** Crear únicamente `A bot → 54.205.214.211` en el proveedor DNS
autoritativo, comprobando antes cualquier registro explícito `bot` y eliminando
solo un eventual conflicto de ese mismo nombre. No añadir AAAA: esta configuración
del panel utiliza IPv4.

El registro `A bot` ya resuelve a AWS y HTTPS está activo, con certificado válido
hasta el 15/01/2027 y renovación automática habilitada. El usuario del panel es
`gaa`; la contraseña aleatoria se conserva en el PC,
`.deploy-private/panel-acceso.txt`, fuera de Git. El servidor guarda su hash bcrypt
en `/etc/nginx/gaainversiones.htpasswd`, con acceso limitado a root y Nginx.
No abrir sesiones con esa contraseña mediante HTTP sin TLS.

Para actualizar, ejecutar `sudo sh deploy/linux/instalar-panel.sh` desde una copia
que contenga el código del panel. Cuando el DNS esté propagado:

```bash
sudo sh deploy/linux/activar-panel-https.sh
sudo certbot renew --dry-run --run-deploy-hooks --no-random-sleep-on-renew
```

El segundo script comprueba primero el destino DNS, solicita un certificado de
Let's Encrypt, activa Nginx con autenticación para todas las rutas del panel y
habilita `certbot.timer`. Las renovaciones recargan Nginx mediante un deploy hook.
El certificado se emite sin correo de contacto; la renovación depende del
temporizador del servidor. No se añade otro recurso AWS facturable.

Se verificaron TLS válido, 401 sin credenciales y con una contraseña incorrecta,
200 con el acceso correcto y WebSocket 101 autenticado (401 sin acceso). El latido
paper continúa avanzando. `theinnoapp.com` y `www.theinnoapp.com` siguen respondiendo
con la página de InnoApp (HTTP 200), conservando su destino en Vercel.
La simulación de renovación terminó correctamente y ejecutó el hook de recarga
de Nginx. La espera aleatoria se omite solo en esta comprobación manual.

Acceso de mantenimiento sin publicar Streamlit:

```powershell
ssh -i .deploy-private/gaa-lightsail-20261017 -o UserKnownHostsFile=.deploy-private/known_hosts -N -L 127.0.0.1:18501:127.0.0.1:8501 ubuntu@54.205.214.211
```

Abrir `http://127.0.0.1:18501` mientras ese túnel permanezca activo.
