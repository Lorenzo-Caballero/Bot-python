# ganamos-bot

Bots de Playwright que operan el panel de agentes `agents.ganamosonline.com`,
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

## La cola (leer antes de dejarlo prendido)

`bot_crear_jugador.py` pide trabajo a `cola_panel.php`, que consulta la tabla
`jugadores`. **La migración 07 borró esa tabla.** Hoy el endpoint responde:

```
SQLSTATE[42S02]: Base table or view not found: 1146
Table 'u722310012_fauno888.jugadores' doesn't exist
```

Con eso, el contenedor levanta, se loguea bien al panel y sondea cada 30s sin
recibir nada nunca. Para que el bot vuelva a tener trabajo hace falta, del lado
del server (Hostinger, por FTP — no va en este repo):

1. una migración `13` que cree la cola de altas en el esquema actual, y
2. `cola_panel.php` apuntando a esa tabla nueva (el contrato con el bot —
   `?accion=ver|pendientes|marcar|liberar` — no hace falta tocarlo).

Mientras tanto, para verificar que todo lo demás funciona sin depender de la
cola:

```bash
docker compose run --rm creador python /app/bot_crear_jugador.py --probar-login
docker compose run --rm creador python /app/bot_crear_jugador.py --probar-api
```

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
