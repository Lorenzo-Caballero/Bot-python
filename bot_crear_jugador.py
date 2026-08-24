"""
Bot de creacion automatica de jugadores en agents.ganamos7.com

Flujo:
    login.php -> tabla jugadores -> cola_panel.php -> este bot -> panel

El bot pide los jugadores con  creado = 0,  llena el formulario "Crear jugador"
con los datos de la base (nombre_usuario, panel_password, correo, nombre,
apellido) y avisa el resultado. Cuando el alta sale bien, cola_panel.php pone
creado = 1 y borra la clave en claro.

Solo se inventa lo que venga vacio en la base, para que un registro incompleto
no frene la cola: ver completar_datos().

Instalacion:
    pip install -r requirements.txt
    python -m playwright install chromium

El login es automatico con PANEL_USER/PANEL_PASS del .env, y se re-loguea solo
si la sesion cae en medio del loop. Ningun modo pide ENTER para entrar: solo
frena a esperarte si el login automatico falla (captcha, 2FA, password vieja),
y ahi lo destrabas a mano en la misma ventana.

La ventana de Chrome se ve por defecto (con --headless se oculta) y va lenta a
proposito para que puedas seguir lo que hace. Con --mantener-abierto la ventana
queda viva al terminar, hasta que aprietes ENTER.

Diagnostico (ninguno crea jugadores de verdad):
    python bot_crear_jugador.py --probar-login    # valida las credenciales
    python bot_crear_jugador.py --inspeccionar    # vuelca inputs/botones reales
    python bot_crear_jugador.py --probar-form     # llena el form y NO envia

Operacion:
    python bot_crear_jugador.py --dry-run        # completa pero NO envia
    python bot_crear_jugador.py --once           # una pasada y sale
    python bot_crear_jugador.py                  # loop infinito (polling)
    python bot_crear_jugador.py --headless       # sin ventana, para el server

Archivo .env (esta en .gitignore, no subirlo):
    API_URL=https://mi-dominio.com/api/jugadores.php
    API_KEY=una-clave-larga-y-random
    PANEL_URL=https://agents.ganamos7.com/user/create-player
    LOGIN_URL=https://agents.ganamos7.com/
    PANEL_USER=...
    PANEL_PASS=...
    POLL_SEGUNDOS=30

Detalles del panel que costaron sangre (no romper):
  - No hay <form> en el login y los inputs no tienen `name`.
  - Los botones se apagan con la clase CSS `button_disable`, NO con el
    atributo `disabled`: is_enabled() de Playwright miente.
  - El login vive en la RAIZ (/), la URL nunca dice "login".
  - Detectar el login por un input password da falso positivo en el form de
    crear jugador, que tiene dos. Se detecta por el boton "Acceder".
"""

import argparse
import json
import logging
import os
import random
import re
import sys
import time
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv
from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%d/%m %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("bot")

SESION_FILE = Path("estado_sesion.json")
SESSION_STORAGE_FILE = Path("estado_session_storage.json")
# Las capturas van al volumen montado si existe (/datos en docker-compose).
# Antes caian en /app/capturas, que vive DENTRO del contenedor y no esta
# mapeado: se perdian en cada --force-recreate, justo cuando mas se necesitan
# para ver que mostraba el panel en el momento del fallo.
SHOTS = Path("/datos/capturas") if Path("/datos").is_dir() else Path("capturas")
SHOTS.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# DESTINO
# ---------------------------------------------------------------------------
PANEL_URL = os.environ.get(
    "PANEL_URL", "https://agents.ganamos7.com/user/create-player"
)
# La pantalla de login vive en la RAIZ, no en /login: el panel es un SPA y esa
# ruta no existe (te deja en una pagina sin el campo de password, y el bot falla
# con "No encontre los campos del login"). Mismo motivo por el que
# es_pantalla_login() busca el boton 'Acceder' en vez de mirar la URL.
LOGIN_URL = os.environ.get("LOGIN_URL", "https://agents.ganamos7.com/")

# Host del panel: se usa para distinguir el POST real de la telemetria
HOST_PANEL = "ganamos7.com"

# Credenciales del panel de agentes. NUNCA hardcodear aca: van en el .env,
# que esta en .gitignore. Si faltan, el bot cae al login manual (--login).
PANEL_USER = os.environ.get("PANEL_USER", "")
PANEL_PASS = os.environ.get("PANEL_PASS", "")


class SesionExpirada(Exception):
    """El panel nos devolvio al login en medio de la operacion."""


# ---------------------------------------------------------------------------
# SELECTORES  (sacados del outerHTML que pasaste)
# ---------------------------------------------------------------------------
# El <form> no se llama igual en los dos paneles: en agents.ganamos7.com lleva
# la clase create-player__form y en agents.ganamosonline.com el <form> viene
# PELADO, sin clase. Lo unico estable en los dos es el contenedor de campos que
# tiene adentro (.create-player__fields), asi que se reconoce por ahi.
FORM_SELS = [
    "form.create-player__form",          # panel nuevo
    "form:has(.create-player__fields)",  # panel viejo: el form no tiene clase
    "form:has(input[placeholder='Nombre de usuario'])",
    "form",                              # ultimo recurso
]
# Se sigue usando como prefijo de los selectores "preferidos" de cada campo.
# Cada campo ademas tiene alternativas SIN prefijo, que son las que salvan
# cuando el form no matchea (ver SEL).
FORM = FORM_SELS[0]

# Cada campo lleva VARIOS selectores, que se prueban en orden hasta que uno
# aparezca (ver primer_selector). El panel es un React sin `name` en algunos
# inputs, y ya nos paso que un cambio de markup dejara al bot tipeando en el
# campo equivocado -- o en ninguno -- sin un error claro.
#
# El orden es a proposito: primero `name` (lo mas estable cuando esta), despues
# el placeholder (visible en la pantalla, sobrevive a refactors de clases), y
# recien al final la posicion dentro de .create-player__fields, que es lo mas
# fragil porque se rompe si agregan un campo.
SEL = {
    # OJO: este input NO tiene atributo name, va por placeholder
    "usuario": [
        f"{FORM} input[placeholder='Nombre de usuario']",
        "input[placeholder='Nombre de usuario']",
        ".create-player__fields > div:nth-child(1) input",
    ],
    "email": [
        f"{FORM} input[name='email']",
        "input[name='email']",
        "input[placeholder='Correo electrónico']",
        ".create-player__fields > div:nth-child(2) input",
    ],
    "password": [
        f"{FORM} input[name='password']",
        "input[name='password']",
        "input[placeholder='Contraseña']",
        ".create-player__fields > div:nth-child(3) input",
    ],
    "confirm": [
        f"{FORM} input[name='confirmPassword']",
        "input[name='confirmPassword']",
        "input[placeholder='Confirmar contraseña']",
        ".create-player__fields > div:nth-child(5) input",
    ],
    "nombre": [
        f"{FORM} input[name='name']",
        "input[name='name']",
        "input[placeholder='Nombre']:not([placeholder*='usuario'])",
        ".create-player__fields > div:nth-child(4) input",
    ],
    "apellido": [
        f"{FORM} input[name='surname']",
        "input[name='surname']",
        "input[placeholder='Apellido']",
        ".create-player__fields > div:nth-child(6) input",
    ],
    "btn_crear": [
        f"{FORM} button[type='submit']",
        "form button[type='submit']",
        "button:has-text('CREAR JUGADOR')",
    ],
    "btn_cancel": [
        f"{FORM} button:has-text('Cancelar')",
        "form button:has-text('Cancelar')",
    ],
}


def primer_selector(page, sels, timeout_ms: int = 8_000) -> str:
    """El primero de `sels` que exista en la pagina.

    Acepta un string suelto tambien, para no romper a los que ya llamaban con
    uno solo. Si ninguno aparece devuelve el ultimo, para que el error que
    lance Playwright nombre un selector de verdad y no uno inventado.
    """
    if isinstance(sels, str):
        return sels
    import time as _t
    limite = _t.monotonic() + timeout_ms / 1000
    while _t.monotonic() < limite:
        for sel in sels:
            try:
                if page.locator(sel).count() > 0:
                    return sel
            except Exception:
                pass
        _t.sleep(0.2)
    return sels[-1]

# Selectores del login, verificados contra la pagina real.
# Ojo: no hay <form>, los inputs no tienen name y el boton es type="button".
SEL_LOGIN = {
    "usuario": "input[placeholder='Nombre'], input.input_type_text",
    "password": "input[type='password']",
    "submit": "button:has-text('Acceder')",
}

# El panel NO usa el atributo `disabled`: apaga los botones con esta clase.
# Por eso is_enabled() de Playwright miente y hay que mirar el class.
CLASE_BOTON_APAGADO = "button_disable"

# El form vive en su propia URL (/user/create-player), asi que normalmente no
# hace falta clickear nada. Esto es el plan B para cuando se cae en el listado
# de usuarios sin el form: el boton "Crear usuario" del subheader.
# Solo se usa si tras cargar PANEL_URL no aparecio el <form>.
SEL_ABRIR_MODAL = (
    "#root > div > div.app__wrapper > main > div.subheader > "
    "div.subheader__buttons > a.button.button_sizable_default."
    "button_colors_transparent"
)

# Despues de "CREAR JUGADOR" el panel abre un modal: "Esta seguro, que quiere
# crear X?". Vive en su propio portal de React (#modal-root > div > div > div >
# div), fuera del <form>. Hasta que no se confirma, el POST no sale.
SEL_MODAL = "#modal-root"

# CUIDADO al elegir el boton: en ese modal el vistoso (violeta, lleno) es
# "Cancelar", y el que crea de verdad es el de contorno. Buscarlo por posicion,
# por "el boton primario" o por el primer <button> del modal CANCELA el alta.
# Va anclado (^...$) para no comerse el "CREAR JUGADOR" del formulario de atras.
RX_CONFIRMAR = re.compile(r"^\s*crear\s+jugador\s*$", re.I)

# Orden de tipeo. Importante: password ANTES de confirmPassword,
# asi la validacion de "coinciden" corre con el valor final.
ORDEN = [
    ("usuario",  "usuario"),
    ("email",    "email"),
    ("nombre",   "nombre"),
    ("apellido", "apellido"),
    ("password", "password"),
    ("confirm",  "password"),   # se repite la misma password
]

# Pistas para reconocer el POST que crea el jugador (case-insensitive).
PISTAS_POST_CREAR = ("player", "user", "create", "register", "signup")

# Dominios de tracking que NUNCA son la respuesta que buscamos.
HOSTS_RUIDO = (
    "clarity.ms", "google-analytics", "googletagmanager", "doubleclick",
    "facebook", "hotjar", "sentry", "bing.com", "gstatic", "cloudflareinsights",
)


# ---------------------------------------------------------------------------
# Datos inventados
# ---------------------------------------------------------------------------
NOMBRES = [
    "Lucas", "Martin", "Diego", "Javier", "Nicolas", "Federico", "Gonzalo",
    "Matias", "Sebastian", "Ezequiel", "Facundo", "Ignacio", "Tomas", "Bruno",
    "Julian", "Emiliano", "Rodrigo", "Agustin", "Damian", "Leandro", "Marcelo",
    "Sofia", "Valentina", "Camila", "Martina", "Lucia", "Julieta", "Florencia",
    "Agustina", "Carolina", "Micaela", "Daniela", "Romina", "Gabriela", "Ayelen",
    "Antonella", "Rocio", "Belen", "Milagros", "Victoria", "Paula", "Natalia",
]

APELLIDOS = [
    "Gomez", "Fernandez", "Rodriguez", "Lopez", "Martinez", "Perez", "Sanchez",
    "Romero", "Alvarez", "Torres", "Ruiz", "Ramirez", "Flores", "Benitez",
    "Acosta", "Medina", "Herrera", "Aguirre", "Pereyra", "Gutierrez", "Molina",
    "Silva", "Castro", "Rojas", "Ortiz", "Nunez", "Luna", "Juarez", "Cabrera",
    "Rios", "Morales", "Godoy", "Sosa", "Vega", "Campos", "Paz", "Ledesma",
]


def email_desde_usuario(usuario: str) -> str:
    """<usuario>@gmail.com, saneado a lo que Gmail acepta (letras, numeros, punto)."""
    base = re.sub(r"[^a-zA-Z0-9.]", "", str(usuario)).strip(".").lower()
    if len(base) < 6:                      # Gmail pide 6+ caracteres
        base = (base + "player")[:12]
    return f"{base}@gmail.com"


# El correo de la base es del estilo usuario123@goldpaw.game. Si el panel
# rechaza ese dominio, poner True y se manda usuario@gmail.com en su lugar.
# El correo de tu base no se toca: solo cambia lo que se tipea en el panel.
FORZAR_GMAIL = False


def completar_datos(reg: dict) -> dict:
    """Completa el registro con lo que falte para llenar el formulario.

    Los datos mandan desde la base (correo, nombre y apellido los genera
    login.php al registrar). Solo se inventa lo que venga vacio, para que un
    registro incompleto no frene la cola.

    El random se siembra con el usuario: si un registro se reintenta, le toca
    siempre el mismo nombre y apellido.
    """
    datos = dict(reg)
    usuario = str(datos.get("usuario", "")).strip()
    rnd = random.Random(usuario or str(datos.get("id", "")))

    correo_bd = str(datos.get("email") or "").strip()
    if FORZAR_GMAIL or not correo_bd:
        datos["email"] = email_desde_usuario(usuario)
    else:
        datos["email"] = correo_bd

    if not str(datos.get("nombre") or "").strip():
        datos["nombre"] = rnd.choice(NOMBRES)
    if not str(datos.get("apellido") or "").strip():
        datos["apellido"] = rnd.choice(APELLIDOS)

    return datos


# ---------------------------------------------------------------------------
# API PHP
# ---------------------------------------------------------------------------
PLACEHOLDERS = ("tu-dominio", "mi-dominio", "una-clave-larga-y-random", "TU_DOMINIO")


class ErrorConfig(Exception):
    """El .env no esta completo. No tiene sentido reintentar."""


class ErrorApi(Exception):
    """La API contesto, pero con un rechazo (no es un problema de red).

    Se separa de requests.RequestException a proposito: una cosa es "no llego
    el pedido" y otra muy distinta "llego y la API dijo que no". La segunda es
    siempre configuracion -- clave que no coincide, dominio sin cliente, ruta
    equivocada -- y el mensaje tiene que decirlo, no disfrazarse de caida."""


def sesion_http(key: str) -> requests.Session:
    """Session con reintentos, para las dos colas.

    Hostinger cierra las conexiones keep-alive ociosas. Como el bot sondea cada
    30s y reusa el socket, cada tanto le toca uno ya cerrado del otro lado y
    salta 'RemoteDisconnected: Remote end closed connection without response'.
    No es que la API este caida: es un socket muerto, y con reintentarlo alcanza.

    Se reintentan tambien los POST, al reves del default de urllib3. Los dos que
    hay -marcar de altas y marcar de saldo- solo cierran una accion que este
    abierta, asi que repetirlos no hace nada la segunda vez.
    """
    s = requests.Session()
    s.headers.update({"X-API-Key": key, "Accept": "application/json"})
    reintentos = Retry(
        total=3,
        backoff_factor=0.6,             # 0.6s, 1.2s, 2.4s
        status_forcelist=(502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adaptador = HTTPAdapter(max_retries=reintentos)
    s.mount("https://", adaptador)
    s.mount("http://", adaptador)
    return s


class ApiJugadores:
    def __init__(self):
        self.url = os.environ.get("API_URL", "")
        self.key = os.environ.get("API_KEY", "")

        # Fallar aca es mucho mas claro que 3 reintentos contra un dominio
        # que no existe: el error de SSL no dice nada sobre la causa real.
        if not self.url or not self.key:
            raise ErrorConfig("Faltan API_URL o API_KEY en el .env")
        for ph in PLACEHOLDERS:
            if ph in self.url or ph in self.key:
                raise ErrorConfig(
                    f"El .env todavia tiene el valor de ejemplo '{ph}'. "
                    "Pone tu dominio real en API_URL y la misma clave que "
                    "BOT_API_KEY del server en API_KEY."
                )

        self.s = sesion_http(self.key)

    def pendientes(self, limite: int = 10) -> list[dict]:
        """OJO: esto RECLAMA los registros (los pasa a 'procesando').
        Para mirar sin tocar nada, usar ver()."""
        r = self.s.get(self.url, params={"accion": "pendientes", "limite": limite}, timeout=20)
        r.raise_for_status()
        try:
            j = r.json()
        except ValueError:
            # HTML en vez de JSON = casi siempre el WAF o una pagina de error
            # del hosting. Sin esto, .json() explota con un traceback ilegible.
            raise ErrorApi(
                f"La API no devolvio JSON (primeros 120 chars): {r.text[:120]!r}"
            )
        # 200 con ok:false. Pasaba desapercibido: .get('datos', []) devolvia
        # lista vacia y el bot se quedaba "escuchando" contra una API que le
        # estaba diciendo que no, sin una sola linea en el log.
        if isinstance(j, dict) and j.get("ok") is False:
            raise ErrorApi(f"La API rechazo el pedido: {j.get('error', 'sin detalle')}")
        return j.get("datos", [])

    def diagnostico(self) -> None:
        """Loguea contra que cola esta hablando y que hay adentro.

        Corre una sola vez al arrancar. No reclama nada (usa 'ver')."""
        archivo = self.url.rsplit("/", 1)[-1].split("?")[0]
        log.info("Cola de altas: %s", self.url)
        if archivo != "altas_cola.php":
            log.warning(
                "  OJO: API_URL apunta a '%s', no a altas_cola.php. "
                "Las altas viven en altas_cola.php; con otro archivo el bot "
                "no va a ver nunca un pedido.", archivo
            )
        try:
            d = self.ver(limite=5)
        except ErrorApi as e:
            log.error("  No pude leer la cola: %s", e)
            return
        except requests.RequestException as e:
            log.error("  No pude leer la cola: %s", e)
            return

        r = d.get("resumen") or {}
        log.info("  En la cola: %s pendiente(s) con clave, %s en proceso, "
                 "%s con error, %s ya creadas",
                 r.get("listos", 0), r.get("tomados", 0),
                 r.get("con_error", 0), r.get("en_panel", 0))
        if not r.get("total"):
            log.warning(
                "  La cola esta VACIA. Si acabas de pedir un alta por el chat "
                "y no aparece, el chat esta escribiendo en OTRA base: la API "
                "elige el cliente por el dominio del pedido, y este bot entra "
                "por %s.", self.url.split("/")[2]
            )

    def ver(self, limite: int = 20) -> dict:
        """Espia la cola sin reclamar nada."""
        r = self.s.get(self.url, params={"accion": "ver", "limite": limite}, timeout=20)
        r.raise_for_status()
        return r.json()

    def liberar(self) -> int:
        """Devuelve a 'pendiente' los que quedaron en 'procesando'."""
        r = self.s.post(self.url, params={"accion": "liberar"}, timeout=20)
        r.raise_for_status()
        return int(r.json().get("liberados", 0))

    def marcar(self, registro_id, estado: str, mensaje: str = "") -> None:
        """estado: 'ok' | 'error'"""
        try:
            r = self.s.post(
                self.url,
                params={"accion": "marcar"},
                json={"id": registro_id, "estado": estado, "mensaje": mensaje[:500]},
                timeout=20,
            )
            r.raise_for_status()
        except requests.RequestException as e:
            log.error("No pude marcar el registro %s como %s: %s", registro_id, estado, e)


# ---------------------------------------------------------------------------
# Sesion (cookies + localStorage + sessionStorage)
# ---------------------------------------------------------------------------
def guardar_sesion(ctx, page) -> None:
    """storage_state() NO guarda sessionStorage, asi que lo volcamos aparte."""
    ctx.storage_state(path=str(SESION_FILE))
    try:
        crudo = page.evaluate("JSON.stringify(window.sessionStorage)")
        SESSION_STORAGE_FILE.write_text(crudo, encoding="utf-8")
    except PWError as e:
        log.warning("No pude leer sessionStorage: %s", e)


def restaurar_session_storage(ctx) -> None:
    if not SESSION_STORAGE_FILE.exists():
        return
    datos = SESSION_STORAGE_FILE.read_text(encoding="utf-8").strip() or "{}"
    ctx.add_init_script(
        "(() => { try {"
        f"  const d = {datos};"
        "   for (const k of Object.keys(d)) window.sessionStorage.setItem(k, d[k]);"
        "} catch (e) {} })();"
    )


# ---------------------------------------------------------------------------
# Helpers de la pagina
# ---------------------------------------------------------------------------
def tipear(page, selector, valor: str) -> None:
    """Escribe como humano para que React registre cada cambio.

    `selector` puede ser un string o una lista de alternativas (ver SEL)."""
    selector = primer_selector(page, selector)
    loc = page.locator(selector).first
    loc.wait_for(state="visible", timeout=10_000)
    loc.click()
    loc.fill("")                                   # limpia
    loc.press_sequentially(str(valor), delay=random.randint(30, 80))
    loc.press("Tab")                               # blur -> dispara validacion


def boton_habilitado(loc) -> bool:
    """is_enabled() no alcanza: el panel apaga con clase CSS, no con `disabled`."""
    try:
        if not loc.is_enabled():
            return False
        clases = loc.get_attribute("class") or ""
        return CLASE_BOTON_APAGADO not in clases
    except PWError:
        return False


def esperar_habilitado(page, loc, timeout_ms: int = 10_000) -> bool:
    limite = time.time() + timeout_ms / 1000
    while time.time() < limite:
        if boton_habilitado(loc):
            return True
        page.wait_for_timeout(250)
    return False


def esperar_boton_habilitado(page, timeout_ms: int = 10_000) -> bool:
    return esperar_habilitado(page, page.locator(primer_selector(page, SEL["btn_crear"])).first, timeout_ms)


def estado_campos(page) -> list[str]:
    """Devuelve 'placeholder=validState' de cada input, para diagnosticar.

    El panel marca validez con clases input_validState_N. No documente que
    significa cada N, asi que reportamos el crudo en vez de adivinar.
    """
    return page.eval_on_selector_all(
        f"{FORM} input",
        """els => els.map(e => {
            const st = [...e.classList].find(c => c.startsWith('input_validState_')) || 'sin-estado';
            return (e.placeholder || e.name) + '=' + st.replace('input_validState_', '');
        })""",
    )


def es_respuesta_de_creacion(resp) -> bool:
    """True si esta respuesta parece la del POST que crea el jugador."""
    if resp.request.method != "POST":
        return False
    url = resp.url.lower()
    if any(ruido in url for ruido in HOSTS_RUIDO):
        return False
    if HOST_PANEL not in url:
        return False
    return any(pista in url for pista in PISTAS_POST_CREAR)


def es_pantalla_login(page) -> bool:
    """Ojo: la pantalla de login vive en la RAIZ (/), la URL nunca dice 'login'.

    Tampoco sirve buscar un input password: el form de crear jugador tiene dos
    (clave y confirmacion) y daria falso positivo. El unico marcador exclusivo
    del login es el boton 'Acceder'.
    """
    try:
        return page.locator(SEL_LOGIN["submit"]).first.is_visible(timeout=2_000)
    except PWError:
        return False


def login_automatico(page) -> bool:
    """Loguea con PANEL_USER/PANEL_PASS del .env. False si no pudo."""
    if not PANEL_USER or not PANEL_PASS:
        log.error("Faltan PANEL_USER / PANEL_PASS en el .env")
        return False

    log.info("Logueando como %s ...", PANEL_USER)
    page.goto(LOGIN_URL, wait_until="domcontentloaded")

    try:
        tipear(page, SEL_LOGIN["usuario"] + " >> nth=0", PANEL_USER)
        tipear(page, SEL_LOGIN["password"] + " >> nth=0", PANEL_PASS)
    except (PWError, PWTimeout) as e:
        page.screenshot(path=str(SHOTS / "login_sin_campos.png"))
        log.error("No encontre los campos del login (%s). Mira capturas/login_sin_campos.png", e)
        return False

    btn = page.locator(SEL_LOGIN["submit"]).first
    if not esperar_habilitado(page, btn):
        page.screenshot(path=str(SHOTS / "login_boton_apagado.png"))
        log.error("El boton 'Acceder' siguio apagado. Revisa usuario/password.")
        return False

    try:
        btn.click()
    except (PWError, PWTimeout) as e:
        page.screenshot(path=str(SHOTS / "login_sin_click.png"))
        log.error("No pude clickear 'Acceder': %s", e)
        return False

    # Tras el click, esperamos a que desaparezca el boton de login
    try:
        page.locator(SEL_LOGIN["submit"]).first.wait_for(state="hidden", timeout=30_000)
    except (PWError, PWTimeout):
        pass

    if es_pantalla_login(page):
        page.screenshot(path=str(SHOTS / "login_fallido.png"))
        log.error("Segui en el login. Puede ser password incorrecta, captcha o 2FA. "
                  "Mira capturas/login_fallido.png y si hace falta corre --login")
        return False

    log.info("Login OK -> %s", page.url)
    return True


def abrir_formulario(page) -> None:
    # Siempre goto, nunca reload(): el panel es un SPA y despues de un alta
    # queda en /users/all. Recargar ESA ruta no reconstruye el formulario --
    # hay que navegar de nuevo a la del form. Y si ya estamos en la ruta
    # correcta, goto igual la rearma limpia para el alta siguiente.
    page.goto(PANEL_URL, wait_until="domcontentloaded")

    if es_pantalla_login(page):
        raise SesionExpirada(page.url)

    def form_a_la_vista(espera_ms: int) -> bool:
        """True si aparecio alguno de los FORM_SELS y esta visible."""
        sel = primer_selector(page, FORM_SELS, espera_ms)
        try:
            page.wait_for_selector(sel, timeout=espera_ms, state="visible")
            return True
        except PWTimeout:
            return False

    # El panel es un SPA: recien salido de domcontentloaded el <form> todavia
    # no existe, aunque la URL ya sea la del formulario. Por eso se ESPERA en
    # vez de preguntar por count(), que da 0 SIEMPRE en ese momento.
    if form_a_la_vista(10_000):
        return

    # No aparecio solo: recien ahi tiene sentido el boton "Crear usuario" del
    # listado. Best-effort: si el markup del subheader cambio, seguimos igual.
    if SEL_ABRIR_MODAL:
        boton = page.locator(SEL_ABRIR_MODAL)
        if boton.count() > 0:
            log.info("  el form no cargo solo, voy por 'Crear usuario'")
            try:
                boton.first.click(timeout=5_000)
            except (PWTimeout, PWError) as e:
                log.warning("No pude clickear 'Crear usuario': %s", e)
            if form_a_la_vista(10_000):
                return

    # Ultimo intento: navegacion limpia esperando a que la red se calme. Un SPA
    # que venia de otra ruta a veces necesita eso y no un domcontentloaded.
    log.info("  el form sigue sin aparecer, recargo la pagina entera")
    try:
        page.goto(PANEL_URL, wait_until="networkidle", timeout=25_000)
    except PWTimeout:
        pass
    if es_pantalla_login(page):
        raise SesionExpirada(page.url)
    if form_a_la_vista(15_000):
        return

    # Se rinde, pero diciendo DONDE quedo. "waiting for locator('form')" no
    # sirve para nada: lo util es la URL y una captura de lo que se ve.
    try:
        page.screenshot(path=str(SHOTS / "sin_formulario.png"))
    except Exception:
        pass
    raise RuntimeError(
        f"No aparecio el formulario de alta en {page.url} "
        f"(captura en {SHOTS}/sin_formulario.png)"
    )


def sesion_viva(page) -> bool:
    """Si nos deslogueo, la URL suele cambiar a /login."""
    try:
        page.goto(PANEL_URL, wait_until="domcontentloaded", timeout=30_000)
        return not es_pantalla_login(page)
    except PWTimeout:
        return False


def recuperar_pagina(page) -> None:
    """Deja la pagina lista para el proximo alta, pase lo que pase.

    Un alta que falla puede dejar el modal abierto, un overlay tapando todo o
    el formulario a medio llenar. El siguiente alta arranca ahi y se cuelga
    esperando algo que no va a pasar -- que es como el bot quedo "clavado"
    despues de un FALLO, sin volver a mirar la cola.

    Todo best-effort y con timeouts cortos: esto es limpieza, no puede ser lo
    que rompa el loop.
    """
    # 1) Cerrar el modal si quedo abierto (Escape es lo menos invasivo).
    try:
        # press() no acepta timeout (solo `delay`): pasarselo tira TypeError.
        page.keyboard.press("Escape")
    except Exception:
        pass
    # 2) Volver al formulario limpio. Si ni esto sale, el proximo alta lo
    #    intenta de nuevo por su cuenta.
    try:
        page.goto(PANEL_URL, wait_until="domcontentloaded", timeout=15_000)
    except Exception as e:
        log.debug("no pude recuperar la pagina: %s", e)


def errores_de_validacion(page) -> list[str]:
    """Los mensajes de error que el panel muestra JUNTO A LOS CAMPOS.

    Existe para el caso mas confuso: el bot llena el form, aprieta el boton, y
    no sale ningun POST. Eso casi siempre es el panel rechazando algo (usuario
    muy corto, clave que no cumple, correo repetido), pero desde afuera se veia
    como "no se detecto la respuesta del servidor", que no dice nada y manda a
    buscar el problema donde no esta.

    Devuelve [] si no encuentra nada. No lanza: es diagnostico, no puede tirar
    abajo el alta.
    """
    vistos = []
    # Los paneles React suelen marcar el error con una clase que contiene
    # "error", "invalid" o "helper". Se prueban varias y se filtra por texto
    # visible, porque muchas quedan en el DOM vacias hasta que hay error.
    for sel in ("[class*='error']", "[class*='invalid']", "[class*='helper']",
                "[role='alert']", ".create-player__fields [class*='message']"):
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 12)):
                try:
                    if not loc.nth(i).is_visible(timeout=500):
                        continue
                    t = (loc.nth(i).inner_text(timeout=500) or "").strip()
                except Exception:
                    continue
                # Los contenedores grandes traen media pantalla adentro: solo
                # sirve el texto corto, que es el mensajito del campo.
                if t and 3 < len(t) < 160 and t not in vistos:
                    vistos.append(t)
        except Exception:
            pass
    return vistos


def resultado_visual(page) -> tuple[bool | None, str]:
    """Plan B cuando no se detecto el POST: leer la pantalla.

    Devuelve (True | False | None, detalle). None = no se pudo determinar.
    """
    if PANEL_URL.rstrip("/") not in page.url.rstrip("/"):
        return True, f"redirigio a {page.url}"

    texto = ""
    try:
        texto = (page.locator("body").inner_text(timeout=3_000) or "").lower()
    except PWError:
        pass

    for palabra in ("creado con exito", "creado correctamente", "exitosamente",
                    "successfully created", "jugador creado"):
        if palabra in texto:
            return True, f"mensaje en pantalla: '{palabra}'"

    for palabra in ("ya existe", "already exists", "error", "invalid", "invalido"):
        if palabra in texto:
            return False, f"mensaje en pantalla: '{palabra}'"

    return None, "sin señales en pantalla"


def confirmar_modal(page, timeout_ms: int = 8_000, rx: re.Pattern = None) -> str:
    """Aprieta el boton de confirmar del modal (por defecto, "Crear Jugador").

    `rx` es el texto EXACTO del boton que confirma. Lo usa tambien el bot de
    fichas, con su propio texto: los dos modales del panel tienen al lado un
    "Cancelar", asi que nunca se elige por posicion.

    Se prueban tres formas en orden porque no sabemos con que esta hecho el
    boton: <button>, algo con role=button, o un div cualquiera con ese texto.
    Todas acotadas a #modal-root y filtradas por RX_CONFIRMAR, nunca por
    posicion (ver el comentario de RX_CONFIRMAR: al lado esta "Cancelar").

    No lanza: si no aparece el modal devuelve "sin modal", porque puede ser que
    el POST haya salido con el primer click y eso no es un error.
    """
    rx = rx or RX_CONFIRMAR
    limite = time.monotonic() + timeout_ms / 1000
    modal = page.locator(SEL_MODAL)

    while time.monotonic() < limite:
        for como, loc in (
            ("button", modal.locator("button").filter(has_text=rx)),
            ("rol",    modal.get_by_role("button", name=rx)),
            ("texto",  modal.get_by_text(rx)),
        ):
            try:
                if loc.count() == 0:
                    continue
                loc.first.click(timeout=3_000)
                return f"modal confirmado por {como}"
            except (PWTimeout, PWError):
                continue        # todavia no esta pintado, o cambio abajo
        page.wait_for_timeout(250)

    return "sin modal"


def esperar_salida_del_form(page, timeout_ms: int = 20_000) -> str:
    """Espera a que el panel se vaya del formulario. Devuelve la URL, o "".

    Cuando el alta sale bien, el panel navega al listado (/users/all). Esa es
    la señal REAL de exito y llega en segundos.

    El POST, en cambio, casi nunca se detecta: expect_response se come sus 30
    segundos enteros y recien despues miramos la pantalla. Eso hacia que cada
    alta -incluso las que salian bien- tardara medio minuto, y que un fallo
    tardara lo mismo antes de poder reintentar.
    """
    limite = time.monotonic() + timeout_ms / 1000
    base = PANEL_URL.rstrip("/")
    while time.monotonic() < limite:
        try:
            url = page.url
        except Exception:
            url = ""
        if url and base not in url.rstrip("/"):
            return url
        page.wait_for_timeout(300)
    return ""


def enviar_formulario(page, reg: dict) -> tuple[bool | None, str]:
    """Aprieta crear, confirma el modal y espera una señal.

    Devuelve (True|False|None, detalle). None = ni exito ni error reconocible;
    el caller decide si reintenta.
    """
    detalle_modal = ""
    try:
        with page.expect_response(es_respuesta_de_creacion, timeout=12_000) as info:
            page.click(primer_selector(page, SEL["btn_crear"]))
            # El POST NO sale con ese click: primero hay que confirmar el modal.
            # Va adentro del expect_response a proposito -- afuera, el bot
            # esperaria una respuesta que todavia nadie disparo.
            detalle_modal = confirmar_modal(page)
            log.info("  %s", detalle_modal)
        resp = info.value
        if resp.ok:
            return True, f"HTTP {resp.status} {resp.url}"
        if 300 <= resp.status < 400:
            return True, f"HTTP {resp.status} redirect a {resp.url}"
        return False, f"HTTP {resp.status}"
    except PWTimeout:
        pass

    # No se detecto el POST. La señal buena es que el panel se haya ido del
    # formulario: cuando el alta entra, navega al listado.
    url = esperar_salida_del_form(page)
    if url:
        return True, f"redirigio a {url} ({detalle_modal or 'sin modal'})"

    errores = errores_de_validacion(page)
    if errores:
        return False, "El panel rechazo el formulario: " + " | ".join(errores[:3])

    return None, f"sin señales en pantalla ({detalle_modal or 'no se llego al modal'})"


# ---------------------------------------------------------------------------
# Carga de un jugador
# ---------------------------------------------------------------------------
def crear_jugador(page, reg: dict, dry_run: bool = False) -> tuple[bool, str]:
    datos = completar_datos(reg)
    log.info("  datos -> email=%s nombre=%s %s",
             datos["email"], datos["nombre"], datos["apellido"])

    abrir_formulario(page)

    for sel_key, dato_key in ORDEN:
        valor = datos.get(dato_key)
        if not valor:
            return False, f"Falta el dato '{dato_key}' en el registro"
        tipear(page, SEL[sel_key], valor)

    if not esperar_boton_habilitado(page):
        estados = estado_campos(page)
        page.screenshot(path=str(SHOTS / f"invalido_{reg['id']}.png"))
        return False, f"El boton nunca se habilito. Estado de los campos: {estados}"

    if dry_run:
        page.screenshot(path=str(SHOTS / f"dryrun_{reg['id']}.png"))
        log.info("[DRY-RUN] Formulario completo y valido, NO se envia")
        return True, "dry-run"

    # Enviar. Si no hay ninguna señal -ni POST, ni redireccion, ni error en
    # pantalla- se reintenta UNA vez: ese caso resulto ser intermitente (dos
    # altas seguidas, la primera entro y la segunda no, con el mismo codigo y
    # el mismo panel). Un reintento inmediato sale mas barato que dejarlo en
    # error y que el jugador espere a que la cola lo tome de nuevo.
    ok, detalle = enviar_formulario(page, reg)

    if ok is None:
        page.screenshot(path=str(SHOTS / f"sin_respuesta_{reg['id']}.png"))
        try:
            html = page.locator(primer_selector(page, FORM_SELS, 2_000)).first.inner_html(timeout=3_000)
            (SHOTS / f"form_{reg['id']}.html").write_text(html, encoding="utf-8")
        except Exception:
            pass

        log.warning("  sin señal (%s). Reintento una vez.", detalle)
        abrir_formulario(page)
        for sel_key, dato_key in ORDEN:
            tipear(page, SEL[sel_key], datos[dato_key])
        if not esperar_boton_habilitado(page):
            return False, "En el reintento el boton nunca se habilito"
        ok, detalle = enviar_formulario(page, reg)

        if ok is None:
            # Sigue sin saberse. Se devuelve error para que la cola lo tome de
            # nuevo (intentos < MAX_INTENTOS), y NO se da por creada la cuenta:
            # de ahi salen credenciales que no abren nada.
            return False, f"Sin señal tras reintentar: {detalle}"

    return ok, detalle



# ---------------------------------------------------------------------------
# Navegador
# ---------------------------------------------------------------------------
ARGS_CHROMIUM = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def nuevo_contexto(p, headless: bool, con_sesion: bool):
    browser = p.chromium.launch(
        headless=headless,
        args=ARGS_CHROMIUM,
        slow_mo=0 if headless else 100,
    )
    ctx = browser.new_context(
        storage_state=str(SESION_FILE) if con_sesion and SESION_FILE.exists() else None,
        viewport={"width": 1440, "height": 900},
        user_agent=UA,
        locale="es-AR",
        timezone_id="America/Argentina/Buenos_Aires",
    )
    if con_sesion:
        restaurar_session_storage(ctx)
    return browser, ctx


# ---------------------------------------------------------------------------
# Modos auxiliares
# ---------------------------------------------------------------------------
def esperar_cierre(headless: bool, mantener: bool) -> None:
    """Deja la ventana abierta hasta que el usuario apriete ENTER."""
    if headless or not mantener:
        return
    try:
        input("\n>>> Ventana abierta para que mires. ENTER aca para cerrarla...\n")
    except (EOFError, KeyboardInterrupt):
        pass


def modo_login(headless: bool = False, mantener: bool = False) -> int:
    """Entra al panel y guarda la sesion.

    Si hay PANEL_USER/PANEL_PASS en el .env se loguea solo, sin pedir ENTER.
    Solo frena a esperarte si el login automatico falla (captcha, 2FA,
    password cambiada), asi podes destrabarlo a mano en la misma ventana.
    """
    with sync_playwright() as p:
        browser, ctx = nuevo_contexto(p, headless=headless, con_sesion=False)
        page = ctx.new_page()
        page.set_default_timeout(15_000)

        automatico = bool(PANEL_USER and PANEL_PASS) and login_automatico(page)

        if not automatico:
            if PANEL_USER and PANEL_PASS:
                log.warning("El login automatico fallo, seguilo a mano en la ventana")
            else:
                log.info("No hay PANEL_USER/PANEL_PASS en el .env, login manual")
                page.goto(LOGIN_URL)
            if headless:
                log.error("Sin ventana no podes loguearte a mano. Saca --headless")
                browser.close()
                return 1
            print(f"\n>>> Logueate en la ventana del navegador ({LOGIN_URL})")
            input(">>> Cuando estes adentro del panel, apreta ENTER aca...\n")

        guardar_sesion(ctx, page)
        log.info("Sesion guardada en %s y %s", SESION_FILE, SESSION_STORAGE_FILE)

        # Lo dejamos parado en el formulario, listo para mirar
        page.goto(PANEL_URL, wait_until="domcontentloaded")
        esperar_cierre(headless, mantener)
        browser.close()
    return 0


def modo_probar_api() -> int:
    """Verifica la conexion con cola_panel.php. No abre el navegador."""
    try:
        api = ApiJugadores()
    except ErrorConfig as e:
        log.error("%s", e)
        return 1

    log.info("Consultando %s ...", api.url)
    try:
        # 'ver' NO reclama: mirar la cola no debe vaciarla
        estado = api.ver(20)
    except requests.exceptions.SSLError as e:
        log.error("Fallo el SSL. Suele ser el dominio mal escrito en API_URL, "
                  "o el certificado vencido. Detalle: %s", e)
        return 1
    except requests.exceptions.ConnectionError as e:
        log.error("No se pudo conectar. Revisa el dominio y que el server este "
                  "arriba. Detalle: %s", e)
        return 1
    except requests.exceptions.HTTPError as e:
        codigo = e.response.status_code if e.response is not None else "?"
        cuerpo = ""
        try:
            cuerpo = (e.response.text or "")[:300] if e.response is not None else ""
        except Exception:
            pass
        if codigo == 401:
            log.error("HTTP 401: la API_KEY del .env no coincide con el "
                      "BOT_API_KEY del server.")
        elif codigo == 404 and "Accion desconocida" in cuerpo:
            log.error("El cola_panel.php del server es viejo y no conoce la "
                      "accion 'ver'. Volve a subirlo.")
        elif codigo == 404:
            log.error("HTTP 404: la ruta de API_URL no existe. Fijate donde "
                      "quedo cola_panel.php al subirlo.")
        elif codigo == 500:
            log.error("HTTP 500: revisa el error_log de PHP. Suele ser que "
                      "falta correr la migracion SQL o que db.php no conecta.")
        else:
            log.error("HTTP %s: %s", codigo, cuerpo or e)
        return 1
    except ValueError:
        log.error("La respuesta no es JSON valido. Probablemente PHP escupio "
                  "un warning antes del json_encode. Abri la URL en el navegador.")
        return 1
    except requests.RequestException as e:
        log.error("Fallo la consulta: %s", e)
        return 1

    r = estado.get("resumen", {})
    log.info("Conexion OK (solo lectura, no se reclamo nada).")
    log.info("Total %s | en el panel %s | listos para el bot %s | "
             "tomados %s | con error %s | sin clave %s",
             r.get("total"), r.get("en_panel"), r.get("listos"),
             r.get("tomados"), r.get("con_error"), r.get("sin_clave"))

    for f in estado.get("datos", []):
        marca = {1: "ya esta en el panel"}.get(int(f.get("creado", 0) or 0), "")
        if not marca:
            est = f.get("panel_estado")
            if not int(f.get("tiene_clave", 0) or 0):
                marca = "sin clave en claro: tiene que volver a entrar a la landing"
            elif est == "procesando":
                marca = "tomado, vuelve solo en 15 min (o corre --liberar)"
            elif est == "error":
                marca = f"error: {f.get('panel_mensaje') or 'sin detalle'}"
            else:
                marca = "listo para el bot"
        log.info("  id=%-3s %-14s %-32s %s %s",
                 f.get("id"), f.get("usuario"), f.get("email"),
                 f"{f.get('nombre')} {f.get('apellido')}", f"-> {marca}")

    if not int(r.get("listos", 0) or 0):
        if int(r.get("tomados", 0) or 0):
            log.warning("No hay nada listo pero si %s tomado(s). Corre: "
                        "python bot_crear_jugador.py --liberar",
                        r.get("tomados"))
        else:
            log.info("La cola esta vacia. Registra un usuario en tu landing "
                     "y proba de nuevo.")
    return 0


def modo_liberar() -> int:
    """Devuelve a 'pendiente' los registros que quedaron en 'procesando'."""
    try:
        api = ApiJugadores()
    except ErrorConfig as e:
        log.error("%s", e)
        return 1
    try:
        n = api.liberar()
    except requests.RequestException as e:
        log.error("No pude liberar: %s", e)
        return 1
    log.info("%d registro(s) devuelto(s) a la cola.", n)
    return 0


def modo_probar_login(headless: bool = False, mantener: bool = False) -> int:
    """Prueba PANEL_USER/PANEL_PASS del .env y guarda la sesion si funciona."""
    with sync_playwright() as p:
        browser, ctx = nuevo_contexto(p, headless=headless, con_sesion=False)
        page = ctx.new_page()
        page.set_default_timeout(15_000)

        if not login_automatico(page):
            esperar_cierre(headless, mantener)
            browser.close()
            return 1

        guardar_sesion(ctx, page)
        log.info("Sesion guardada en %s", SESION_FILE)
        esperar_cierre(headless, mantener)
        browser.close()
    return 0


def modo_probar_form(headless: bool = False, mantener: bool = False) -> int:
    """Llena el formulario con datos de prueba y NO envia.

    Sirve para validar selectores, generacion de datos y que el boton se
    habilite, sin crear un jugador de verdad ni depender de la API PHP.
    Ademas loguea las llamadas XHR del panel para descubrir el endpoint real.
    """
    if not SESION_FILE.exists() and not (PANEL_USER and PANEL_PASS):
        log.error("Corre primero: python bot_crear_jugador.py --login")
        return 1

    # Imita exactamente la forma en que cola_panel.php devuelve una fila,
    # incluido el dominio @goldpaw.game que genera login.php.
    sufijo = random.randint(10_000, 99_999)
    reg = {
        "id": "PRUEBA",
        "usuario": f"testbot{sufijo}",
        "password": "Prueba1234x",
        "email": f"testbot{sufijo}@goldpaw.game",
        "nombre": "Rurik",
        "apellido": "Kovacs",
    }

    with sync_playwright() as p:
        browser, ctx = nuevo_contexto(p, headless=headless, con_sesion=True)
        page = ctx.new_page()
        page.set_default_timeout(15_000)

        vistos: set[str] = set()

        def anotar(req):
            if req.resource_type in ("xhr", "fetch") and HOST_PANEL in req.url:
                clave = f"{req.method} {req.url.split('?')[0]}"
                if clave not in vistos:
                    vistos.add(clave)

        page.on("request", anotar)

        if not sesion_viva(page):
            if not login_automatico(page) or not sesion_viva(page):
                esperar_cierre(headless, mantener)
                browser.close()
                return 1
            guardar_sesion(ctx, page)

        ok, msg = crear_jugador(page, reg, dry_run=True)
        log.info("Resultado: %s -> %s", "OK" if ok else "FALLO", msg)
        log.info("Estado final de los campos: %s", estado_campos(page))

        print("\n=== LLAMADAS XHR/FETCH DEL PANEL ===")
        print("(busca aca el endpoint que crea el jugador)")
        for c in sorted(vistos):
            print(" -", c)

        esperar_cierre(headless, mantener)
        browser.close()
    return 0 if ok else 1


def modo_inspeccionar(headless: bool = False) -> int:
    """Vuelca el HTML real del formulario para verificar los selectores."""
    if not SESION_FILE.exists():
        log.error("Corre primero: python bot_crear_jugador.py --login")
        return 1

    with sync_playwright() as p:
        browser, ctx = nuevo_contexto(p, headless=headless, con_sesion=True)
        page = ctx.new_page()
        page.goto(PANEL_URL, wait_until="networkidle")

        if es_pantalla_login(page):
            log.warning("Sesion vencida, relogueando...")
            if login_automatico(page):
                guardar_sesion(ctx, page)
                page.goto(PANEL_URL, wait_until="networkidle")

        print("\n=== URL FINAL ===")
        print(page.url)

        print("\n=== FORMULARIOS EN LA PAGINA ===")
        for f in page.eval_on_selector_all(
            "form", "els => els.map(e => e.className || '(sin clase)')"
        ):
            print(" -", f)

        print("\n=== INPUTS ===")
        campos = page.eval_on_selector_all(
            "input, select, textarea",
            """els => els.map(e => ({
                tag: e.tagName, type: e.type, name: e.name,
                id: e.id, placeholder: e.placeholder, clases: e.className
            }))""",
        )
        for c in campos:
            print(json.dumps(c, ensure_ascii=False))

        print("\n=== BOTONES ===")
        for b in page.eval_on_selector_all(
            "button",
            "els => els.map(e => JSON.stringify({type: e.type, txt: e.innerText.trim(), cls: e.className}))",
        ):
            print(" -", b)

        destino = SHOTS / "inspeccion.png"
        page.screenshot(path=str(destino), full_page=True)
        print(f"\nCaptura guardada en {destino}")

        esperar_cierre(headless, mantener=True)
        browser.close()
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true", help="guardar sesion manualmente")
    ap.add_argument("--probar-api", action="store_true",
                    help="ver la cola sin reclamar nada ni abrir el navegador")
    ap.add_argument("--liberar", action="store_true",
                    help="devolver a la cola los registros trabados en 'procesando'")
    ap.add_argument("--probar-login", action="store_true",
                    help="probar el login automatico con PANEL_USER/PANEL_PASS")
    ap.add_argument("--inspeccionar", action="store_true", help="volcar el HTML del form")
    ap.add_argument("--probar-form", action="store_true",
                    help="llenar el form con datos de prueba sin enviar")
    ap.add_argument("--once", action="store_true", help="una sola pasada")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="completa pero no envia")
    ap.add_argument("--lote", type=int, default=10, help="registros por pasada")
    ap.add_argument("--con-fichas", action="store_true",
                    help="en la misma pasada, ejecutar las cargas de saldo pendientes")
    ap.add_argument("--mantener-abierto", action="store_true",
                    help="al terminar deja la ventana abierta hasta que aprietes ENTER")
    args = ap.parse_args()

    if args.probar_api:
        return modo_probar_api()

    if args.liberar:
        return modo_liberar()

    if args.login:
        return modo_login(args.headless, args.mantener_abierto)

    if args.probar_login:
        return modo_probar_login(args.headless, args.mantener_abierto)

    if args.inspeccionar:
        return modo_inspeccionar(args.headless)

    if args.probar_form:
        return modo_probar_form(args.headless, args.mantener_abierto)

    if not SESION_FILE.exists() and not (PANEL_USER and PANEL_PASS):
        log.error("No existe %s ni hay PANEL_USER/PANEL_PASS en el .env. "
                  "Corre: python %s --login", SESION_FILE, Path(sys.argv[0]).name)
        return 1

    try:
        api = ApiJugadores()
    except ErrorConfig as e:
        log.error("%s", e)
        return 1

    poll = int(os.environ.get("POLL_SEGUNDOS", 30))

    with sync_playwright() as p:
        browser, ctx = nuevo_contexto(p, headless=args.headless, con_sesion=True)
        page = ctx.new_page()
        page.set_default_timeout(15_000)

        if not sesion_viva(page):
            log.info("Sin sesion valida, intento login automatico...")
            if not login_automatico(page) or not sesion_viva(page):
                log.error("No pude entrar al panel. Corre: python %s --login",
                          Path(sys.argv[0]).name)
                browser.close()
                return 1
            guardar_sesion(ctx, page)

        log.info("Sesion OK en %s. Escuchando la base cada %ss...", PANEL_URL, poll)

        # Foto de la cola al arrancar. Es el diagnostico que mas veces faltó:
        # con el bot logueado y "escuchando", no habia forma de saber si la
        # cola estaba vacia de verdad o si estaba mirando la cola equivocada
        # (otro dominio => otro cliente => otra base). Ahora lo dice.
        api.diagnostico()

        ultimo_latido = time.monotonic()
        try:
            while True:
                try:
                    lote = api.pendientes(args.lote)
                except ErrorApi as e:
                    # La API contesto, pero dijo que no. Es config, no red:
                    # clave que no coincide, dominio sin cliente, ruta mal.
                    log.error("%s", e)
                    lote = []
                except requests.RequestException as e:
                    log.error("API caida o inaccesible: %s", e)
                    lote = []

                if lote:
                    log.info("%d registro(s) pendiente(s)", len(lote))

                for reg in lote:
                    etiqueta = f"{reg.get('id')} / {reg.get('usuario')}"
                    log.info("Creando jugador %s", etiqueta)
                    try:
                        ok, msg = crear_jugador(page, reg, args.dry_run)
                    except SesionExpirada:
                        log.warning("Sesion caida, reintentando login...")
                        if not login_automatico(page):
                            log.error("No pude re-loguear. Corto para no perder registros.")
                            api.marcar(reg["id"], "error", "Sesion caida, sin re-login")
                            return 1
                        guardar_sesion(ctx, page)
                        try:
                            ok, msg = crear_jugador(page, reg, args.dry_run)
                        except Exception as e:
                            log.exception("Excepcion tras re-login en %s", etiqueta)
                            ok, msg = False, f"Excepcion: {e}"
                    except Exception as e:
                        log.exception("Excepcion en %s", etiqueta)
                        ok, msg = False, f"Excepcion: {e}"

                    if ok:
                        log.info("  OK  -> %s", msg)
                    else:
                        log.warning("  FALLO -> %s", msg)

                    if not args.dry_run:
                        api.marcar(reg["id"], "ok" if ok else "error", msg)

                    # Que el proximo alta arranque en un estado conocido. Sin
                    # esto, un fallo que deja el modal abierto se lleva puestas
                    # a todas las que vienen atras.
                    recuperar_pagina(page)

                    time.sleep(random.uniform(2, 4))   # respirar entre cargas

                # Cargas de saldo, en el MISMO navegador. Dos procesos con la
                # misma cuenta de agente se pisan la sesion, asi que conviene
                # que las dos tareas compartan este login.
                if args.con_fichas:
                    # Import adentro a proposito: bot_cargar_fichas nos importa
                    # a nosotros, y arriba seria una dependencia circular.
                    import bot_cargar_fichas as fichas
                    try:
                        fichas.procesar_pendientes(page)
                    except SesionExpirada:
                        log.warning("Sesion caida durante las fichas, re-logueando...")
                        if not login_automatico(page):
                            log.error("No pude re-loguear. Corto.")
                            return 1
                        guardar_sesion(ctx, page)
                    except Exception:
                        # Una carga que falla no puede tumbar el loop de altas.
                        log.exception("Error procesando la cola de saldo")

                if args.once:
                    break

                # Latido cada ~10 min. El loop no loguea nada cuando la cola
                # esta vacia, asi que "todo tranquilo" y "el bot se colgo" se
                # veian igual en pantalla -- y esperar 30 s mirando un log
                # quieto no dice cual de las dos es.
                ahora = time.monotonic()
                if ahora - ultimo_latido >= 600:
                    ultimo_latido = ahora
                    log.info("escuchando (cola vacia)")

                time.sleep(poll)

        except KeyboardInterrupt:
            log.info("Cortado por el usuario")
        finally:
            esperar_cierre(args.headless, args.mantener_abierto)
            browser.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
