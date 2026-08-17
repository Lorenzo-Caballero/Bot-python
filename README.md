# ganamos-bot

Bots de Playwright que operan el panel de agentes `agents.ganamos7.com`,
empaquetados para correr en un VPS con Docker.

| Servicio | Script | Qué hace |
|---|---|---|
| `creador` (default) | `bot_crear_jugador.py` | Sondea la cola de la API propia y da de alta jugadores en el panel. |
| `sync` (opcional) | `sync_usuarios.py` | Espeja todos los usuarios del panel (con su saldo) a la tabla `usuarios`. |

Los dos comparten imagen: el sync importa el login de `bot_crear_jugador.py`.

---

## Deploy en el VPS

```bash
git clone <tu-repo> ganamos-bot
cd ganamos-bot

cp .env.example .env
nano .env                      # completar credenciales

mkdir -p datos                 # volumen: sesión, logs y capturas
docker compose build
docker compose up -d
docker compose logs -f creador
```

Para levantar además el espejo de usuarios:

```bash
docker compose --profile sync up -d
```

Actualizar después de un cambio:

```bash
git pull && docker compose up -d --build
```

---

## La sesión del panel (lo primero que se rompe)

Dentro del contenedor no hay pantalla ni forma de destrabar un login a mano. El
bot intenta loguearse solo con `PANEL_USER` / `PANEL_PASS`, pero si el panel
tira captcha, 2FA o cambió la password, el contenedor no arranca.

El plan seguro es **grabar la sesión en tu máquina y subirla**:

```bash
# En local, con ventana visible:
cd datos && python ../bot_crear_jugador.py --login
```

Eso deja `estado_sesion.json` y `estado_session_storage.json` en `datos/`.
Copiá esa carpeta al VPS (`scp -r datos usuario@vps:~/ganamos-bot/`) y el
contenedor arranca con la sesión ya hecha.

> `datos/` es exactamente el directorio de trabajo del contenedor: correr los
> scripts parado ahí reproduce en local el mismo layout que en Docker.

**Ojo con la IP:** la sesión se grabó desde tu conexión y se va a usar desde el
datacenter del VPS. Si el panel ata la sesión a la IP, va a pedir login de
nuevo. Mirá los logs del primer arranque antes de darlo por hecho.

---

## La cola (hay que subirla a Hostinger antes de arrancar)

El bot no sale a buscar trabajo solo: lo pide por HTTP a la API propia. El
endpoint viejo (`cola_panel.php`) consulta la tabla `jugadores`, que **la
migración 07 borró**, así que hoy responde:

```
SQLSTATE[42S02]: Base table or view not found: 1146
Table 'u722310012_fauno888.jugadores' doesn't exist
```

El reemplazo ya está escrito y vive en el repo del proyecto, no en este:

| Archivo | Dónde va |
|---|---|
| `api/sql/13_cola_altas.sql` | correr una vez en phpMyAdmin (crea la tabla `altas`) |
| `api/altas_cola.php` | subir por FTP a `public_html/api/` |

El contrato HTTP es idéntico al viejo, así que **el bot no cambia**: solo apuntá
`API_URL` a `.../api/altas_cola.php`.

Sin eso, el contenedor levanta, se loguea bien al panel y sondea cada 30s sin
recibir nada nunca.

Para verificar sin abrir el navegador ni reclamar registros:

```bash
docker compose run --rm creador python /app/bot_crear_jugador.py --probar-api
docker compose run --rm creador python /app/bot_crear_jugador.py --probar-login
```

Encolar un alta de prueba (la password va en claro porque el bot la **tipea**
en el formulario del panel; se borra sola cuando el alta se confirma):

```bash
curl -X POST 'https://TU-DOMINIO/api/altas_cola.php?accion=encolar' \
  -H 'X-API-Key: LA_MISMA_QUE_BOT_API_KEY' \
  -H 'Content-Type: application/json' \
  -H 'User-Agent: Mozilla/5.0' \
  -d '{"usuario":"pruebabot01","password":"Prueba1234","origen":"crm"}'
```

El `User-Agent` de navegador no es opcional: el WAF de Hostinger corta los POST
que no lo parecen.

---

## Comandos útiles

```bash
docker compose logs -f creador          # ver qué está haciendo
docker compose restart creador
docker compose down                     # bajar todo
docker compose run --rm creador python /app/bot_crear_jugador.py --once --dry-run
```

Modos de diagnóstico del bot (ninguno crea jugadores):
`--probar-login`, `--probar-api`, `--probar-form`, `--inspeccionar`, `--dry-run`.

Cuando algo falla, el bot deja capturas de pantalla en `datos/capturas/` y el
log completo en `datos/bot.log`.

---

## Notas de la imagen

- Base `python:3.12-slim` + `playwright install --with-deps chromium`, no la
  imagen oficial de Playwright: así el navegador siempre coincide con la
  versión pineada en `requirements.txt`.
- `shm_size: 1gb` — con los 64MB por defecto de Docker, Chromium se cuelga.
- `init: true` — Chromium deja procesos hijos que Python (PID 1) no cosecha.
- El código vive en `/app` y el working dir es `/datos` (el volumen), porque
  los scripts escriben con rutas relativas.
- El `.env` **no** entra a la imagen (está en `.dockerignore`): se inyecta en
  runtime con `env_file`. Copiarlo en un `COPY` lo dejaría grabado en un layer
  para siempre, aunque después lo borres.
