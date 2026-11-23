# AgentOffice — Guía paso a paso para preparar e instalar un servidor Linux

Esta guía está pensada para alguien **sin experiencia en Linux ni en administración de sistemas**. Explica qué hay que
preparar a mano (dos cuentas externas y dos credenciales), por qué, y cómo lanzar el instalador automático que hace el
resto. Cada paso indica qué resultado esperar y cómo comprobarlo.

La referencia técnica (rutas, unidades systemd, actualización, migración) está en [DEPLOYMENT.md](DEPLOYMENT.md). Si
algo de esta guía y de esa referencia no coincide, es un error: abre un issue.

---

## A. Qué vamos a instalar

**AgentOffice** es un servidor pequeño (un programa Python con una base de datos SQLite) donde los agentes de IA de
un equipo se coordinan: canales, mensajes, tareas y una oficina 3D para verlo. Lo instalaremos en **un solo
ordenador Linux** (una Raspberry Pi, un mini-PC o un servidor virtual).

Términos que aparecerán:

| Término | Qué es |
|---|---|
| **Tailscale** | Servicio que crea una red privada cifrada entre tus dispositivos, aunque estén en casas u oficinas distintas. |
| **tailnet** | Tu red privada de Tailscale: el conjunto de dispositivos y personas que la comparten. |
| **auth key** | Credencial de un solo propósito que permite a un servidor unirse a la tailnet sin usar tu contraseña personal. |
| **tag** (etiqueta) | Nombre como `tag:agentoffice-server` que identifica al servidor como "máquina del equipo" en lugar de "dispositivo de una persona". Las reglas de acceso se escriben sobre etiquetas. |
| **MagicDNS** | Función de Tailscale que da a cada dispositivo un nombre fácil (`agentoffice.tu-red.ts.net`) en lugar de una IP. |
| **HTTPS / certificado TLS** | El candado del navegador: cifra la conexión y demuestra que hablas con el servidor correcto. Tailscale obtiene y renueva el certificado (de Let's Encrypt) automáticamente. |
| **Healthchecks.io** | Servicio externo que te avisa (correo, Signal, Telegram…) si tu servidor deja de dar señales de vida. |
| **heartbeat** | La "señal de vida": cada 5 minutos el servidor comprueba que AgentOffice funciona y, solo si funciona, avisa a Healthchecks.io. |
| **monitor / check** | En Healthchecks.io, la ficha que espera los heartbeats. Si no llega ninguno a tiempo, envía la alerta. |
| **API key** | Credencial que permite a un programa (el instalador) gestionar tu cuenta de Healthchecks.io en tu nombre. |

```text
   Portátiles del equipo (con Tailscale)                         Internet
   ┌──────────────┐  ┌──────────────┐                    ┌───────────────────┐
   │ participante │  │ participante │                    │  Healthchecks.io  │◄─┐ heartbeat cada 5 min
   └──────┬───────┘  └──────┬───────┘                    └─────────┬─────────┘  │ (solo si todo va bien)
          │  red privada cifrada (tailnet)                         │ alerta     │
          └────────┬────────┘                                      ▼ si falta   │
                   ▼                                          tu correo/móvil   │
   ┌──────────────────── Servidor Linux ───────────────────────────────────────┼──┐
   │ Tailscale Serve: https://agentoffice.<tailnet>.ts.net  (certificado TLS)  │  │
   │        │                                                                  │  │
   │        ▼ solo loopback                                                    │  │
   │  AgentOffice 127.0.0.1:8080 ── SQLite /var/lib/agent-office ── backups     │  │
   │  timer de salud (5 min) ───────────────────────────────────────────────────┘  │
   └───────────────────────────────────────────────────────────────────────────────┘
```

**El instalador hace automáticamente:** comprobar el sistema, instalar paquetes, conseguir Python ≥ 3.12, instalar
AgentOffice y sus servicios, instalar Tailscale y unir el servidor a la tailnet, activar HTTPS privado, crear el
monitor en Healthchecks.io, programar backups locales diarios y, al final, **verificar todo** e imprimir un informe.
Si lo ejecutas otra vez, solo hace lo que falte.

**Tú decides:** quién puede entrar en la red privada, a quién avisar si el servidor cae, el nombre del servidor y si
quieres (opcional) recibir eventos de GitHub.

AgentOffice **nunca** queda expuesto a Internet: solo los dispositivos de tu tailnet llegan a él.

## B. Requisitos previos

**Obligatorios**

| Requisito | Detalle |
|---|---|
| Sistema | Cualquier Linux de 64 bits con systemd. **Probados:** Ubuntu 24.04 o posterior, Debian 12 o 13 y Raspberry Pi OS **64 bits**. En otras distribuciones el instalador funciona, pero avisa (`WARN`) de que no está probada; si la distribución no usa `apt` o Tailscale no publica repositorio para ella, tendrás que instalar a mano Tailscale ([instrucciones oficiales](https://tailscale.com/kb/1031/install-linux)) y los paquetes básicos (`git`, `curl`, `python3`, `iproute2`). |
| Arquitectura | 64 bits: `x86_64` (PC) o `aarch64` (Raspberry Pi 3B+/4/5, Zero 2 W con SO de 64 bits). Los sistemas de 32 bits (incluido un sistema de 32 bits sobre un núcleo de 64) no están soportados. |
| Permisos | Una cuenta que pueda usar `sudo` (administrador). |
| Internet | Salida a Internet (paquetes, Tailscale, Healthchecks.io). No hace falta abrir puertos en el router. |
| Disco | Al menos 1 GiB libre en `/opt` y en `/var/lib` (el instalador lo comprueba). |
| Cuentas | Una cuenta de [Tailscale](https://login.tailscale.com/start) y una de [Healthchecks.io](https://healthchecks.io/). Las dos tienen planes gratuitos suficientes. |

**Recomendados**

- 1 GB de RAM o más (se han medido ~43 MB de uso del backend; 512 MB es el mínimo con margen).
- Si usas una Raspberry Pi, pon los datos en un **SSD o disco USB** en lugar de la tarjeta SD: SQLite escribe con
  frecuencia y las tarjetas SD se desgastan y corrompen con más facilidad.
- Conexión por cable y alimentación estable.

Para saber qué sistema tienes:

```bash
cat /etc/os-release | head -3     # nombre y versión
uname -m                          # x86_64 o aarch64 = 64 bits
df -h / /var                      # espacio libre
```

## C. Crear y preparar Tailscale

Documentación oficial: [registro](https://login.tailscale.com/start) ·
[consola de administración](https://login.tailscale.com/admin) · [auth keys](https://tailscale.com/kb/1085/auth-keys) ·
[etiquetas](https://tailscale.com/kb/1068/tags) · [políticas de acceso](https://tailscale.com/kb/1018/acls)
([sintaxis](https://tailscale.com/kb/1337/policy-syntax)) · [DNS](https://tailscale.com/kb/1054/dns) ·
[MagicDNS](https://tailscale.com/kb/1081/magicdns) · [HTTPS](https://tailscale.com/kb/1153/enabling-https) ·
[aprobación de dispositivos](https://tailscale.com/kb/1099/device-approval).

1. **Crear o acceder a una tailnet.** Entra en <https://login.tailscale.com/start> con la cuenta del equipo (Google,
   Microsoft, GitHub…). La primera vez se crea tu tailnet. Su nombre aparece arriba en la consola (p. ej.
   `example.com` o `tu-usuario.github`). Apúntalo: sirve para el campo opcional `network.tailnet`.

2. **Preparar la etiqueta `tag:agentoffice-server`.** En la consola, abre **Access controls**
   (<https://login.tailscale.com/admin/acls/file>) y añade la etiqueta a `tagOwners`. Así indicas quién puede crear
   dispositivos con esa etiqueta (los administradores):

   ```json
   "tagOwners": {
     "tag:agentoffice-server": ["autogroup:admin"]
   }
   ```

3. **Definir quién podrá acceder al servidor.** En el mismo archivo, permite a los miembros del equipo llegar al
   puerto HTTPS del servidor (y nada más):

   ```json
   "grants": [
     { "src": ["autogroup:member"], "dst": ["tag:agentoffice-server"], "ip": ["tcp:443"] }
   ]
   ```

   Si tu política ya tiene una regla que permite todo a todos, el servidor funcionará igualmente; restringirla es
   recomendable pero no obligatorio. Guarda con **Save**. La consola valida la sintaxis antes de aceptar.

4. **Habilitar MagicDNS y HTTPS.** En **DNS** (<https://login.tailscale.com/admin/dns>): comprueba que **MagicDNS**
   está activado y pulsa **Enable HTTPS** en *HTTPS Certificates*. Sin esto no hay nombre `*.ts.net` ni certificado.
   Ten en cuenta que el nombre del servidor y de tu tailnet quedarán registrados en el registro público de
   certificados (Certificate Transparency); no incluyas datos sensibles en el nombre.

5. **Crear una auth key para el servidor.** En **Settings → Keys** (<https://login.tailscale.com/admin/settings/keys>)
   pulsa **Generate auth key** y marca:
   - **Tags**: activado, con `tag:agentoffice-server`. Un dispositivo etiquetado pertenece al equipo, no a tu
     usuario, y su clave de dispositivo no caduca por defecto, lo adecuado para un servidor.
   - **Pre-approved**: activado si tu tailnet exige aprobar dispositivos; si no, el servidor quedará "pendiente de
     aprobación" y tendrás que aprobarlo a mano en **Machines**.
   - **Ephemeral**: **desactivado** (un servidor efímero desaparecería de la red al apagarse).
   - **Expiration**: la mínima razonable (p. ej. 1 día): solo se usa durante la instalación.

6. **Un solo uso o reutilizable.** Una clave **de un solo uso** (Reusable desactivado) solo sirve para registrar un
   dispositivo: es la opción más segura y la recomendada. Una **reutilizable** puede registrar muchos dispositivos
   mientras no caduque: si se filtra, cualquiera podría meter máquinas en tu red. Úsala solo si vas a instalar varias
   veces seguidas y revócala al terminar.

7. **Copiar la credencial sin exponerla.** La consola muestra la clave (`tskey-auth-…`) **una sola vez**. Cópiala
   con el botón de copiar; no la pegues en chats, correos, issues ni capturas de pantalla.

8. **Guardarla temporalmente de forma segura.** La pegarás directamente en el archivo de secretos del apartado F, en
   el propio servidor. No la guardes en notas sincronizadas ni en el historial de la terminal.

> El instalador **nunca** pide tu contraseña de Tailscale ni inicia sesión con tu cuenta personal: solo usa la auth
> key, leída desde un archivo protegido.
>
> Las decisiones administrativas iniciales (crear la tailnet, escribir la política, activar HTTPS, aprobar
> dispositivos si no usas claves pre-aprobadas) son tuyas: el instalador no puede ni debe asumirlas. Si falta alguna,
> se detiene y te dice cuál.

## D. Preparar Healthchecks.io

Documentación oficial: [inicio y registro](https://healthchecks.io/) · [documentación](https://healthchecks.io/docs/) ·
[proyectos y equipos](https://healthchecks.io/docs/projects_teams/) ·
[notificaciones](https://healthchecks.io/docs/configuring_notifications/) ·
[configurar checks](https://healthchecks.io/docs/configuring_checks/) ·
[Management API v3 y API keys](https://healthchecks.io/docs/api/).

1. **Crear la cuenta** en <https://healthchecks.io/> (botón *Sign Up*, plan gratuito).
2. **Seleccionar o crear el proyecto.** La cuenta empieza con un proyecto por defecto. Puedes usarlo o crear uno
   llamado, p. ej., "AgentOffice". El instalador trabaja dentro del proyecto al que pertenece la API key.
3. **Configurar un destinatario de alertas.** Dentro del proyecto, abre **Integrations** y añade al menos uno:
   *Email* es lo más sencillo; también hay Signal, Telegram, Slack, etc.
4. **Verificar el destinatario.** Healthchecks.io envía un correo de confirmación a la dirección añadida: ábrelo y
   confírmala. En *Integrations* puedes pulsar **Test!** para recibir una alerta de prueba. Si no hay ningún
   destinatario, el instalador se **niega** a continuar: un monitor que no avisa a nadie no sirve.
5. **Obtener una Management API key.** En **Settings** del proyecto, sección **API Access**, pulsa **Create API key**.
   Debe ser la clave normal (lectura y escritura), **no** la *read-only*: el instalador necesita crear el check.
6. **Guardarla temporalmente** igual que la de Tailscale: irá al archivo de secretos del apartado F.
7. **El instalador crea el check automáticamente**, llamado `agentoffice-production`, con periodo de 5 minutos,
   gracia de 10 minutos y todos los destinatarios del proyecto asignados. Guarda en el servidor solo la *ping URL*
   del check, nunca la API key.
8. **No crees el monitor a mano.** Si ya existe un check con ese nombre que no creó el instalador, este no lo toca
   (para no estropear algo tuyo) salvo que pongas `adopt_existing = true` en la configuración.

## E. Preparar el archivo de configuración

La configuración vive en `/etc/agent-office/deployment.toml` (formato TOML: `clave = valor`, texto entre comillas).
No contiene secretos. Cualquier sección o clave desconocida es un error, de modo que una errata no pasa desapercibida.
Ejemplo completo para producción (es el contenido de `deployment/deployment.example.toml`):

```toml
[deployment]
mode = "production"
hostname = "agentoffice"

[application]
bind_host = "127.0.0.1"
port = 8080
git_ref = "HEAD"

[python]
version = "3.12"
interpreter = ""

[network]
provider = "tailscale"
tag = "tag:agentoffice-server"
tailnet = ""
https = true
https_port = 443
public_access = false

[monitoring]
provider = "healthchecks"
api_url = "https://healthchecks.io"
interval_seconds = 300
grace_seconds = 600
check_name = "agentoffice-production"
tags = "agentoffice"
channels = "*"
adopt_existing = false

[backup]
local_enabled = true

[github_webhook]
enabled = false
port = 8081
funnel_port = 8443
```

| Campo | Significado | Por defecto | Cuándo cambiarlo | Si es incorrecto |
|---|---|---|---|---|
| `deployment.mode` | `production` (Tailscale + Healthchecks.io obligatorios) o `development` (solo local, sin servicios externos). | `production` | Solo para pruebas locales. | Valor desconocido → error de validación. |
| `deployment.hostname` | Nombre del servidor en la tailnet → `https://<hostname>.<tailnet>.ts.net`. | `agentoffice` | Si ya tienes otro dispositivo con ese nombre o quieres otro. | Letras minúsculas, números y guiones; si no, error. Si el servidor ya estaba en la tailnet se conserva su identidad. |
| `application.bind_host` | Dirección donde escucha el backend. | `127.0.0.1` | Nunca: es fijo por seguridad. | Otro valor → rechazado. |
| `application.port` | Puerto local del backend (detrás de Tailscale Serve). | `8080` | Si el 8080 ya está ocupado. | Si no coincide con una instalación existente, se detiene sin tocar nada. |
| `application.git_ref` | Versión del repositorio a instalar la primera vez. | `HEAD` | Para fijar una etiqueta (`v0.2.0`). Las actualizaciones se hacen con `update.sh`. | Referencia inexistente → `install.sh` falla y no cambia nada. |
| `python.version` | Python a instalar si el sistema no tiene ≥ 3.12 (Debian 12). | `3.12` | Normalmente nunca (`3.12`, `3.13` o `3.14`). | Otro valor → error de validación. |
| `python.interpreter` | Ruta absoluta para forzar un Python concreto. | vacío (detectar) | Solo si sabes que quieres uno concreto. | Si no sirve (versión, SQLite, venv) → error explicando por qué. |
| `network.provider` | Red privada. | `tailscale` | No (en `development` se usa `none`). | — |
| `network.tag` | Etiqueta del servidor (apartado C). | `tag:agentoffice-server` | Si usas otra etiqueta en tu política. | Si no está en `tagOwners` o en la auth key → Tailscale la rechaza y el instalador lo explica. |
| `network.tailnet` | Comprobación de seguridad: nombre de tu tailnet o su sufijo `tailXXXX.ts.net`. | vacío (sin comprobar) | Recomendado si el servidor pudiera estar ya en otra tailnet. | Si el servidor pertenece a otra tailnet, se detiene sin cambiarla. |
| `network.https` | HTTPS privado con certificado de Tailscale. | `true` | Nunca: es fijo. | `false` → rechazado. |
| `network.https_port` | Puerto HTTPS en la tailnet. | `443` | Si ya usas el 443 en ese servidor para otra cosa. | Si ese puerto ya sirve otra cosa en Tailscale Serve, no se sobrescribe y se detiene. |
| `network.public_access` | Publicar en Internet. | `false` | Nunca: es fijo. | `true` → rechazado. |
| `monitoring.provider` | Monitorización externa. | `healthchecks` | No (en `development`: `none`). | — |
| `monitoring.api_url` | API de Healthchecks.io. | `https://healthchecks.io` | Solo si usas una instancia propia de Healthchecks. | Debe ser `https://`. |
| `monitoring.interval_seconds` | Cada cuánto se espera un heartbeat. | `300` | Más alto si quieres menos avisos. | Menos de 300 → rechazado (el timer local va cada 5 min). |
| `monitoring.grace_seconds` | Margen antes de alertar. | `600` | Más alto en redes inestables. | Fuera de rango → rechazado. |
| `monitoring.check_name` | Nombre del check. | `agentoffice-production` | Si tienes varios servidores en el mismo proyecto. | Caracteres no permitidos → rechazado. |
| `monitoring.tags` | Etiquetas del check en Healthchecks.io. | `agentoffice` | Opcional. | — |
| `monitoring.channels` | A quién avisar: `*` = todos los destinatarios del proyecto, o nombres separados por comas. | `*` | Para avisar solo a algunos. | Un nombre inexistente → error con la lista de disponibles. |
| `monitoring.adopt_existing` | Gestionar un check con ese nombre que no creó el instalador. | `false` | Si creaste el check a mano y quieres que el instalador lo ajuste. | Con `false`, un check ajeno no se modifica y se detiene. |
| `backup.local_enabled` | Backup local diario (03:30, se guardan 14). | `true` | Nunca: es fijo. | `false` → rechazado. |
| `github_webhook.enabled` | Opcional: recibir eventos de GitHub (único punto publicado en Internet, ver [DEPLOYMENT.md](DEPLOYMENT.md#github-webhook-tailscale-funnel-webhook-only-listener)). | `false` | Si quieres que los eventos de un repositorio lleguen a un canal. | Requiere producción con Tailscale. |
| `github_webhook.port` | Puerto local del receptor de webhooks (solo atiende `POST /api/github/webhook`). | `8081` | Si está ocupado. | Igual al de la API → rechazado. |
| `github_webhook.funnel_port` | Puerto público del webhook (Tailscale Funnel solo admite 443, 8443 o 10000). | `8443` | Si el 8443 ya está en uso. | Igual a `https_port` u otro valor → rechazado. |
| `[[github_webhook.repositories]]` | Un bloque por repositorio: `repo = "org/repo"` y opcional `channel`. | ninguno | Al activar el webhook. | Formato `org/repo` incorrecto → rechazado. |

Si el archivo tiene errores, el instalador los lista **todos a la vez** y no cambia nada.

Alternativa sin editar archivos: `sudo deployment/install.sh --wizard` hace las preguntas, escribe este archivo,
pide los secretos sin mostrarlos en pantalla y lanza el despliegue.

## F. Preparar las credenciales

Las credenciales van en un archivo JSON aparte, **nunca** en la línea de comandos (quedaría en el historial y en la
lista de procesos). Contiene:

| Clave | Contenido | ¿Obligatoria? | Después de instalar |
|---|---|---|---|
| `tailscale_auth_key` | La auth key del apartado C (`tskey-auth-…`). | Sí, si el servidor aún no está en la tailnet. | Ya no hace falta: revócala si era reutilizable (las de un solo uso quedan gastadas). |
| `healthchecks_api_key` | La API key del apartado D. | Sí, si aún no hay check configurado. | Ya no hace falta para funcionar; puedes revocarla. Si la conservas, `--verify` comprueba también el check remoto. |
| `github_token` | Solo con webhook: token *fine-grained* con permiso **Webhooks: read and write** sobre los repositorios. | No | Revócalo cuando quieras; solo se usa para crear o actualizar el webhook y comprobar sus entregas. |
| `github_webhook_secret` | Solo con webhook: secreto de firma. Vacío = se genera uno aleatorio. | No | Se guarda en el archivo protegido del servidor. |

Crea el archivo **en el servidor**, propiedad de root y con permisos `600` (solo root puede leerlo):

```bash
sudo install -m 600 -o root -g root /opt/agent-office/src/deployment/bootstrap-secrets.example.json /root/agentoffice-secrets.json
sudo nano /root/agentoffice-secrets.json      # sustituye cada REPLACE_ME; guarda con Ctrl+O, Intro, Ctrl+X
sudo stat -c '%a %U %n' /root/agentoffice-secrets.json
```

Resultado esperado de `stat`: `600 root /root/agentoffice-secrets.json`. Ejemplo de contenido (valores **ficticios**):

```json
{
  "tailscale_auth_key": "tskey-auth-EJEMPLO-NO-REAL",
  "healthchecks_api_key": "EJEMPLO-NO-REAL",
  "github_webhook_secret": "",
  "github_token": ""
}
```

- Si el archivo lo pueden leer otros usuarios (permisos distintos de `600`/`400`), es un enlace simbólico o pertenece a
  otro usuario, el instalador se **niega** a usarlo y te dice cómo corregirlo (`chmod 600 …`).
- El instalador **nunca** modifica ni borra tu archivo; los valores nunca aparecen en pantalla, logs ni informes.
- **Nunca lo subas a Git.** El repositorio ya ignora `deployment/bootstrap-secrets.json` y `deployment.toml`, pero lo
  más seguro es guardarlo fuera del repositorio, como en el ejemplo.
- Alternativa: variables de entorno `AO_TAILSCALE_AUTH_KEY`, `AO_HEALTHCHECKS_API_KEY`, `AO_GITHUB_TOKEN` y
  `AO_GITHUB_WEBHOOK_SECRET` (`sudo` las descarta salvo que uses `sudo --preserve-env=AO_TAILSCALE_AUTH_KEY,…`).
  Pensadas para automatización; a mano, usa el archivo.
- Cuando el informe final diga `SUCCESS`, borra el archivo (`sudo shred -u /root/agentoffice-secrets.json`) y revoca
  las claves que ya no necesites.

**Si una credencial se expone** (la pegaste en un chat, en un commit…):

| Credencial | Cómo revocarla o rotarla |
|---|---|
| Auth key de Tailscale | Consola → Settings → Keys → *Revoke*. El servidor ya registrado sigue funcionando. |
| API key de Healthchecks.io | Proyecto → Settings → API Access → revoca y crea otra. El servidor sigue enviando heartbeats con su ping URL. |
| Token de GitHub | GitHub → Settings → Developer settings → Personal access tokens → *Revoke*. |
| Token de administrador de AgentOffice | Ver [DEPLOYMENT.md § Tokens](DEPLOYMENT.md#tokens). |

## G. Ejecutar el instalador

Todos los comandos se ejecutan en el servidor, en una terminal (por SSH o directamente).

**1. Obtener el repositorio**

```bash
sudo apt update && sudo apt install -y git
sudo git clone https://github.com/0xAnbu/AgentOffice /opt/agent-office/src
cd /opt/agent-office/src
```

**2. Preparar la configuración** (apartado E) y las credenciales (apartado F):

```bash
sudo install -d -m 750 /etc/agent-office
sudo install -m 640 deployment/deployment.example.toml /etc/agent-office/deployment.toml
sudo nano /etc/agent-office/deployment.toml
```

**3. Comprobar requisitos** (no cambia nada: sistema, configuración, credenciales y acceso a los servicios externos):

```bash
sudo deployment/preflight.sh --config /etc/agent-office/deployment.toml --secrets-file /root/agentoffice-secrets.json
```

Esperado: una línea por comprobación con `PASS` (o `WARN`, informativo) y al final `CHECK: OK (nothing was changed)`.
Cualquier `FAIL` dice qué falta; corrígelo y repite.

**4. Ver el plan** (`--dry-run`: lista lo que haría, sin cambiar nada ni crear nada en los servicios externos):

```bash
sudo deployment/deploy.sh --dry-run --config /etc/agent-office/deployment.toml --secrets-file /root/agentoffice-secrets.json
```

**5. Desplegar:**

```bash
sudo deployment/deploy.sh --config /etc/agent-office/deployment.toml --secrets-file /root/agentoffice-secrets.json
```

Tarda unos minutos (la primera vez descarga paquetes y dependencias). Cada paso aparece en pantalla con
`== <fase>` y las acciones realizadas con `DO`. Si algo falla, se detiene en esa fase con `ERROR:` y una línea `HINT:`
que dice cómo resolverlo. Al volver a ejecutar el mismo comando, **continúa** desde donde quedó.

**6. Consultar el informe final.** Al terminar se imprime el *AGENTOFFICE DEPLOYMENT REPORT*, que también se guarda
en `/etc/agent-office/bootstrap-report.txt`:

```text
Tailscale:         PASS         agentoffice.example.ts.net in example.com, tag:agentoffice-server
TLS Certificate:   PASS         valid for agentoffice.example.ts.net, issuer Let's Encrypt, ...
API:               PASS         via Serve: /api/health 200, /api/me without token 401, with admin token 200
WebSocket:         PASS         wss handshake with a single-use ticket ok
Healthchecks.io:   PASS         check 'agentoffice-production' status up, integrations assigned
Local backups:     PASS         timer active; latest agent-office-....db.gz (sha256 ok)
...
Deployment:        SUCCESS
```

- `SUCCESS`: todo lo obligatorio está verificado.
- `INCOMPLETE`: algo obligatorio no se pudo comprobar (`NOT_VERIFIED`); el informe dice qué.
- `FAILED`: algo obligatorio falla (`FAIL`); el informe dice qué.

Las líneas *MagicDNS (server)* y *Other devices* son informativas: el servidor no puede comprobar por sí mismo que
otros dispositivos lleguen a él (ver apartado L).

El **token de administrador** de AgentOffice no se imprime en una instalación desatendida. Está en
`/etc/agent-office/agent-office.env`. Para verlo: `sudo sed -n 's/^AGENTLINK_ADMIN_TOKEN=//p' /etc/agent-office/agent-office.env`.
Guárdalo en tu gestor de contraseñas.

## H. Comprobar AgentOffice

- **Acceso por HTTPS:** desde un portátil con Tailscale conectado a la misma tailnet, abre
  `https://agentoffice.<tu-tailnet>.ts.net` (la *Private URL* del informe). Debe cargar sin avisos de seguridad.
- **Certificado:** el candado del navegador debe indicar un certificado válido emitido por Let's Encrypt para ese
  nombre. Desde una terminal del portátil (sin `-k`, que desactivaría la verificación):

  ```bash
  curl -fsS https://agentoffice.<tu-tailnet>.ts.net/api/health      # → {"ok": true, ...}
  ```

- **Servicios** (en el servidor):

  ```bash
  systemctl status agent-office --no-pager
  systemctl list-timers 'agent-office*' --no-pager
  tailscale serve status
  ```

- **Logs:** `sudo journalctl -u agent-office -n 50` (servicio), `sudo journalctl -u agent-office-healthcheck -n 10`
  (heartbeats) y `sudo less /var/log/agent-office/bootstrap.log` (instalador).
- **Backups locales:** `sudo ls -l /var/backups/agent-office/`: debe haber al menos un `agent-office-<fecha>.db.gz`
  con su `.sha256`.
- **Healthchecks.io:** en el proyecto, el check `agentoffice-production` debe estar en verde (*up*) y mostrar un ping
  reciente.
- **Verificación completa en cualquier momento** (no cambia nada):

  ```bash
  sudo deployment/verify-live.sh
  ```

- **Primera conexión de un agente:** crea una credencial de agente y conecta el cliente siguiendo
  [AGENT_INSTALLATION.md](AGENT_INSTALLATION.md), con `--server https://agentoffice.<tu-tailnet>.ts.net`.

## I. Incorporar participantes

Hay tres cosas distintas, y cada participante necesita las tres:

1. **Acceso a la red privada (Tailscale).** Invita a la persona a tu tailnet
   ([invitar usuarios](https://tailscale.com/kb/1371/invite-users)) o comparte solo el servidor con su propia tailnet
   ([compartir dispositivos](https://tailscale.com/kb/1084/sharing)). La persona instala Tailscale en su portátil
   ([instalación](https://tailscale.com/kb/1017/install)) e inicia sesión. Con la regla del apartado C.3, los
   miembros solo llegan al puerto 443 del servidor.
2. **Credencial y permisos en AgentOffice.** Estar en la tailnet **no** da acceso a los datos: cada llamada a la API
   exige un token. El administrador crea un token por agente
   ([AGENT_INSTALLATION.md §0](AGENT_INSTALLATION.md#0-the-admin-creates-a-credential-per-agent)) y se lo entrega por
   un canal seguro.
3. **Configuración del agente de IA en su ordenador.** La persona instala el cliente y conecta su runtime
   (Claude Code, Codex, …) siguiendo [AGENT_INSTALLATION.md](AGENT_INSTALLATION.md).

Para retirar a alguien: quítalo de la tailnet en la consola de Tailscale **y** revoca sus tokens en AgentOffice.

## J. Problemas frecuentes

Cada error del instalador termina con una línea `HINT:` que indica la solución concreta. Los más comunes:

| Síntoma (mensaje) | Causa | Solución |
|---|---|---|
| `Tailscale rejected the auth key (invalid, expired, revoked or a one-off key already used)` | Clave mal copiada, caducada, revocada o de un solo uso ya gastada. | Genera otra (apartado C.5), ponla en el archivo de secretos y repite. |
| `Tailscale refused the tag tag:…` | La etiqueta no está en `tagOwners`, o la clave se creó sin esa etiqueta. | Añádela a la política (C.2) y crea la clave con la etiqueta. |
| `this device is registered but waiting for approval in the Tailscale admin console` | Tu tailnet exige aprobar dispositivos y la clave no era *pre-approved*. | Consola → Machines → aprueba el servidor y repite. La próxima vez usa una clave pre-aprobada. |
| `Tailscale is logged in but stopped (someone ran tailscale down)` | Alguien desconectó Tailscale a propósito. | `sudo tailscale up` si debe volver a estar conectado; el instalador no lo fuerza. |
| `this device belongs to tailnet '…', not the configured '…'` | El servidor ya estaba en otra tailnet. | Corrige `network.tailnet` o, si de verdad quieres cambiar de red, ver K. |
| `MagicDNS is disabled` / `HTTPS certificates are not enabled in this tailnet` | Falta el paso C.4. | Activa MagicDNS y HTTPS en la página DNS de la consola y repite. |
| Informe: `TLS Certificate: FAIL` | El certificado no es válido para el nombre, o aún se está emitiendo. | Espera un minuto y ejecuta `sudo deployment/verify-live.sh`. Si persiste, revisa C.4. La verificación TLS nunca se desactiva. |
| `Tailscale Serve conflict: …; not overwritten` | Ese puerto HTTPS ya publica otra cosa en el servidor. | Cambia `network.https_port` o retira la configuración anterior tú mismo. |
| `no compatible Python after provisioning` / `python.interpreter is not usable` | Python < 3.12 o sin `venv`/SQLite adecuados. | Deja `python.interpreter` vacío para que el instalador provisione uno; en Debian 12 lo hace con `uv`. |
| `the Healthchecks.io project has no integrations: an alert would reach nobody` | No hay destinatarios en el proyecto. | Añade y verifica uno (D.3–D.4) y repite. |
| `Healthchecks.io rejected the API key (HTTP 401)` (o 403) | Clave errónea, revocada o *read-only*. | Crea una API key normal (D.5). No se reintenta para no bloquear tu cuenta. |
| `a check named 'agentoffice-production' exists but was not created by this bootstrap` | Ya creaste uno a mano. | Bórralo, cambia `check_name` o pon `adopt_existing = true`. |
| `agent-office.service is not healthy` / `install.sh failed` | El servicio no arranca. | `sudo journalctl -u agent-office -n 50`; ver [TROUBLESHOOTING.md](TROUBLESHOOTING.md). |
| Instalación interrumpida (corte de luz, Ctrl+C, error de red) | — | Ejecuta el mismo comando otra vez: el estado está en `/etc/agent-office/bootstrap-state.json` y continúa sin repetir lo hecho. |
| Reinstalar sobre un sistema existente | — | Es seguro: conserva la base de datos, los tokens, la identidad de Tailscale y el check. Si el puerto configurado no coincide con el instalado, se detiene sin tocar nada. |
| `secrets file … has permissions 644: readable by other users` | Permisos inseguros. | `sudo chmod 600 /root/agentoffice-secrets.json`. |
| `run as root (sudo)` | Faltó `sudo`. | Repite con `sudo`. |
| `Platform: WARN … is not a tested distribution` | Tu distribución no es una de las probadas. | Es solo un aviso: el instalador continúa. |
| `Tailscale install: FAIL … no official apt repository` | Tailscale no está instalado y no hay repositorio oficial para tu distribución. | Instálalo siguiendo <https://tailscale.com/kb/1031/install-linux> y repite. |
| `required commands not found: …` | Sistema sin `apt` al que le faltan comandos básicos. | Instálalos con el gestor de paquetes de tu distribución (`dnf`, `zypper`, `pacman`…) y repite. |
| `… libc: the pinned uv build needs glibc` | Sistema con musl (p. ej. Alpine) sin Python ≥ 3.12. | Instala Python 3.12+ con el gestor de paquetes o indica `python.interpreter`. |

## K. Mantenimiento

- **Verificar el estado:** `sudo /opt/agent-office/src/deployment/verify-live.sh`: informe PASS/FAIL/NOT_VERIFIED,
  sin cambiar nada.
- **Repetir el bootstrap:** el mismo comando del paso G.5. Es idempotente: si todo está bien, no cambia nada (ni
  credenciales, ni base de datos, ni el check, ni Tailscale). Ya no necesita el archivo de secretos.
- **Actualizar AgentOffice:** `sudo /opt/agent-office/current/deployment/update.sh` (hace backup previo y vuelve
  atrás solo si la nueva versión no arranca). Detalles en [DEPLOYMENT.md](DEPLOYMENT.md#update-and-rollback).
- **Restaurar un backup local:**
  `sudo /opt/agent-office/current/deployment/restore.sh /var/backups/agent-office/agent-office-<fecha>.db.gz`
  (verifica el archivo antes de tocar nada y pide confirmación; detalles en [DEPLOYMENT.md](DEPLOYMENT.md#backup-and-restore)).
- **Revocar credenciales:** ver la tabla del apartado F. Para sacar el servidor de la tailnet: consola → Machines →
  *Remove*.
- **Consultar errores:** `sudo journalctl -u agent-office -n 100`, `/var/log/agent-office/bootstrap.log`,
  `/etc/agent-office/bootstrap-report.txt` y el estado por fases en `/etc/agent-office/bootstrap-state.json`.
- **Si cambia la tailnet:** el instalador no mueve un servidor de una tailnet a otra por su cuenta. Hazlo a mano:
  `sudo tailscale logout`, actualiza `network.tailnet` y la auth key (de la nueva tailnet), y repite G.5. La URL
  cambia: cada participante debe volver a conectar su cliente con la nueva (`agentlink connect --server …`); los
  tokens siguen siendo válidos.

## L. Limitaciones conocidas

**Conectividad externa.** El despliegue **no** incluye una segunda comprobación desde otro dispositivo. Todas las
verificaciones se hacen desde el propio servidor, conectando a su IP de Tailscale con el nombre y el certificado
reales. Una comprobación local correcta **no demuestra** que todos los participantes puedan acceder: su política de
acceso, su cliente Tailscale o su DNS pueden fallar. Compruébalo a mano una vez desde un portátil de un participante:

```bash
tailscale status | grep agentoffice                                   # el servidor aparece en la red
curl -fsS https://agentoffice.<tu-tailnet>.ts.net/api/health          # → {"ok": true, ...}, sin -k
```

Y desde un dispositivo **fuera** de la tailnet (p. ej. el móvil con Tailscale desactivado), la misma URL **no** debe
responder.

**Backups.** Las copias son **locales**: están en el mismo disco que la base de datos
(`/var/backups/agent-office`). Si se pierde o se estropea ese almacenamiento (tarjeta SD corrupta, disco averiado,
robo), **se puede perder toda la información**. Este despliegue no automatiza copias externas. Se recomienda
añadir en el futuro copias externas **cifradas** fuera del servidor, con herramientas como
[Restic](https://restic.net/) ([documentación](https://restic.readthedocs.io/en/stable/)). Mientras tanto, copia de
vez en cuando el último `.db.gz` y su `.sha256` a otro equipo.

**Validación.** Esta automatización se ha probado de extremo a extremo con servicios simulados y unidades systemd
reales. Aún **no** se ha ejecutado contra Tailscale y Healthchecks.io reales ni en una Raspberry Pi real: ver
[evidencias](evidence/automated-deployment-validation.md).
