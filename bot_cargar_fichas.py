"""
bot_cargar_fichas.py — Ejecuta en el panel de agentes las cargas de saldo que
                       encola el sitio (chatbot / CRM) en `acciones_saldo`.

    chatbot -> fichas_lib.php (descuenta coins) -> acciones_saldo
            -> acciones_cola.php -> ESTE BOT -> DEPOSITAR en el panel

Reusa el login y el navegador de bot_crear_jugador.py: NO abre una sesion
propia. Dos sesiones del mismo agente compiten entre si y el panel puede tirar
abajo la vieja al entrar de nuevo.

Correr:
    python bot_cargar_fichas.py --once --headless     # una pasada
    python bot_cargar_fichas.py --headless            # loop

O, mejor, dentro del mismo proceso que las altas (un solo navegador):
    python bot_crear_jugador.py --headless --con-fichas

.env:
    API_URL, API_KEY        los mismos del bot de altas
    ACCIONES_URL            opcional; por defecto sale de API_URL
    FICHAS_MODE=DRY_RUN     DRY_RUN (default) no aprieta el boton. LIVE deposita.
    FICHAS_POLL_SEGUNDOS=20

PORQUE ARRANCA EN DRY_RUN: acá se deposita plata real en cuentas de jugadores.
Un selector desactualizado en LIVE no tira una excepcion prolija, deposita en la
fila equivocada. Mirá una pasada en DRY_RUN antes de habilitarlo.
"""

import argparse
import os
import re
import sys
import time
import logging

import requests
from dotenv import load_dotenv
from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot_crear_jugador as bot

load_dotenv()

log = logging.getLogger("fichas")

URL_USUARIOS = "https://agents.ganamosonline.com/users/all"

# ---------------------------------------------------------------------------
# Selectores del panel
#
# Cada rol es una LISTA y se prueba en orden, porque el panel sirve dos layouts
# distintos (escritorio y mobile) con clases que no se parecen en nada, y no
# controlamos cual le toca al navegador del contenedor. Ultimo de cada lista:
# una version corta que sobrevive a los reacomodos del markup.
# ---------------------------------------------------------------------------
_CONT = "#root > div > div.app__wrapper > main > div.app__wrapper__content"

SEL_BUSCAR = [
    # El placeholder es lo mas estable que tiene esta pantalla: sobrevive al
    # cambio de layout y a los reacomodos del markup.
    'input[placeholder="Buscar Usuario"]',
    f"{_CONT} > div.users > div.users__filter > form > div:nth-child(1) > "
    "div.search-user-input > div > input",
    f"{_CONT} > div > div.users-mobile__search > div > div:nth-child(1) > "
    "div.search-user-input > div > input",
    ".search-user-input input",
]

# EL PASO QUE FALTABA. Tipear en el buscador no filtra nada: la tabla se entera
# recien cuando se aprieta "Aplicar Filtro". Sin esto, la primera fila sigue
# siendo la del jugador que ya estaba y el bot concluye que el usuario no existe.
SEL_APLICAR = [
    "button:has-text('Aplicar Filtro')",
    "a:has-text('Aplicar Filtro')",
    "[class*='filter'] button:has-text('Aplicar')",
    "button:has-text('Aplicar')",
]

# Desplegable de coincidencias mientras se tipea, cuando lo hay.
SEL_SUGERENCIA = [
    ".search-user-input__search-results > div",
]

# La tabla no es <table>, son divs. Solo se mira la PRIMERA fila.
SEL_FILA = [
    f"{_CONT} > div.users > div.users-table.users-table_tab_all > "
    "div.users-table__table > div.users-table__tbody > div:nth-child(1)",
    f"{_CONT} > div > div.users-table-mobile.users-table-mobile_tab_all > "
    "div.users-table-mobile__table > div.users-table-mobile__rows > div",
    ".users-table__tbody > div",
    ".users-table-mobile__rows > div",
]

# Relativos a la fila.
SEL_NOMBRE_EN_FILA = [
    "div.adm-bets-table-row-user-mobile__user-block-user > span",
    '[class*="user-block-user"] > span',
    "div:nth-child(1)",
]

SEL_DEPOSITAR_EN_FILA = [
    "div:nth-child(3) > div > a.button.button_colors_default",
    "div.adm-bets-table-row-user-mobile__buttons > a.button.button_colors_default",
    "a.button.button_colors_default",
]

SEL_DEP_MONTO = [
    f"{_CONT} > div > div > div.deposit__top > div.deposit__inputs > "
    "div:nth-child(1) > div > div > div > div > input",
    f"{_CONT} > div > div.deposit-mobile__inputs > div:nth-child(1) > "
    "div > div > div > div > input",
    'input[placeholder="Cantidad"]',
    '[class*="deposit"][class*="inputs"] input',
]

# El boton que deposita dice "DEPÓSITO" y es el lleno; al lado esta "CANCELAR".
# Por eso primero se busca por TEXTO y recien despues por clase.
SEL_DEP_CONFIRMAR = [
    'button:has-text("Depósito")',
    'button:has-text("Deposito")',
    f"{_CONT} > div > div > div.deposit__bottom > button.button.button_colors_default",
    f"{_CONT} > div > div.deposit-mobile__buttons > button.button.button_colors_default",
    '[class*="deposit"][class*="buttons"] button.button_colors_default',
]

# Si el panel pide confirmar en un modal, como hace con el alta de jugadores.
RX_MODAL_DEPOSITO = re.compile(r"^\s*(dep[oó]sito|depositar|confirmar|aceptar|s[ií])\s*$", re.I)

# Al elegir depositar, el panel navega a /user/deposit/<id-del-jugador>.
# Es la confirmacion de que se abrio la pantalla del jugador correcto.
RX_URL_DEPOSITO = re.compile(r"/user/deposit/(\d+)")

MODO = os.environ.get("FICHAS_MODE", "DRY_RUN").upper()
POLL = int(os.environ.get("FICHAS_POLL_SEGUNDOS", 20))


# ---------------------------------------------------------------------------
# Cola
# ---------------------------------------------------------------------------
def url_acciones() -> str:
    """De API_URL (.../api/altas_cola.php) sacamos .../api/acciones_cola.php."""
    propia = os.environ.get("ACCIONES_URL", "").strip()
    if propia:
        return propia
    base = os.environ.get("API_URL", "")
    if "/api/" in base:
        return base.rsplit("/api/", 1)[0] + "/api/acciones_cola.php"
    return base.rsplit("/", 1)[0] + "/acciones_cola.php"


class ApiAcciones:
    def __init__(self):
        self.url = url_acciones()
        self.key = os.environ.get("API_KEY", "")
        if not self.url or not self.key:
            raise bot.ErrorConfig("Faltan API_URL/ACCIONES_URL o API_KEY en el .env")
        self.s = requests.Session()
        self.s.headers.update({"X-API-Key": self.key, "Accept": "application/json"})

    def pendientes(self, limite: int = 10) -> list[dict]:
        """OJO: esto RECLAMA las acciones (las pasa a 'procesando')."""
        r = self.s.get(self.url, params={"accion": "pendientes", "limite": limite}, timeout=25)
        r.raise_for_status()
        return r.json().get("datos", [])

    def marcar(self, accion_id: int, estado: str, mensaje: str = "") -> None:
        """estado: 'hecha' | 'error' (devuelve las fichas) | 'revisar' (no devuelve)."""
        try:
            r = self.s.post(self.url, params={"accion": "marcar"},
                            json={"id": accion_id, "estado": estado, "mensaje": mensaje[:300]},
                            timeout=25)
            r.raise_for_status()
        except requests.RequestException as e:
            # Si esto falla, la accion queda 'procesando' y a los 15 min el
            # server la manda a 'revisar'. Es lo correcto: ya la ejecutamos, y
            # nadie mas puede tomarla mientras tanto.
            log.error("No pude marcar la accion %s como %s: %s", accion_id, estado, e)

    def liberar(self) -> int:
        r = self.s.post(self.url, params={"accion": "liberar"}, timeout=25)
        r.raise_for_status()
        return int(r.json().get("liberadas", 0))


# ---------------------------------------------------------------------------
# Lectura de la tabla
# ---------------------------------------------------------------------------
def primero(page, selectores: list[str], timeout_ms: int = 8_000, raiz=None):
    """El primer selector de la lista que exista y se vea. None si ninguno.

    Es lo que permite bancar los dos layouts del panel sin saber de antemano
    cual nos toco: se prueban todos hasta que uno aparece.
    """
    limite = time.monotonic() + timeout_ms / 1000
    while True:
        for s in selectores:
            try:
                loc = (raiz or page).locator(s)
                if loc.count() > 0 and loc.first.is_visible():
                    return loc.first
            except (PWTimeout, PWError):
                pass
        if time.monotonic() >= limite:
            return None
        page.wait_for_timeout(200)


def montos_de(texto: str) -> list[float]:
    """Todos los numeros con pinta de plata que haya en un texto.

    Se sacan TODOS en vez de leer una celda puntual porque en el layout mobile
    el saldo no tiene un selector propio: viene mezclado en la fila. Despues se
    compara la lista de antes contra la de despues.
    """
    montos = []
    for crudo in re.findall(r"-?\d[\d.,]*", texto or ""):
        limpio = crudo.replace(".", "").replace(",", ".")
        try:
            montos.append(float(limpio))
        except ValueError:
            pass
    return montos


def poner_monto(page, caja, monto: float) -> str:
    """Escribe el monto y VERIFICA releyendo el campo.

    Devuelve "" si quedo bien, o el motivo si no.

    Se relee a proposito: es un input controlado por React y hay pantallas donde
    el type() no dispara el onChange, asi que el campo queda vacio aunque las
    teclas hayan llegado. Apretar "Depositar" con el campo vacio -o con la mitad
    del numero- es plata mal puesta, y no se deshace.
    """
    texto = str(int(monto)) if float(monto).is_integer() else str(monto)
    leido = ""

    # Dos formas distintas: type() manda teclas de verdad (lo que mas se parece
    # a una persona) y fill() setea el valor y dispara el evento a mano.
    for metodo in ("type", "fill"):
        try:
            caja.click(timeout=5_000)
            caja.fill("")
            if metodo == "type":
                caja.type(texto, delay=60)
            else:
                caja.fill(texto)
            page.wait_for_timeout(400)

            leido = (caja.input_value(timeout=3_000) or "").strip()
            # El campo puede formatear solo: "1.000", "1,000", "1000".
            if montos_de(leido)[:1] == [float(monto)]:
                return ""
            log.info("  el campo quedo en '%s' con %s(), reintento", leido, metodo)
        except (PWTimeout, PWError) as e:
            log.info("  fallo %s() en el campo del monto: %s", metodo, e)

    return f"el campo del monto quedo en '{leido}', no en '{texto}'"


def nombre_de_fila(page, fila) -> str:
    """El nombre de usuario de una fila, sin la etiqueta PLAYER ni el saldo."""
    for sel in SEL_NOMBRE_EN_FILA:
        try:
            n = fila.locator(sel)
            if n.count() == 0:
                continue
            for linea in (n.first.inner_text(timeout=3_000) or "").splitlines():
                if linea.strip():
                    return linea.strip()
        except (PWTimeout, PWError):
            continue
    # Ultimo recurso: la primera linea de la fila entera.
    try:
        for linea in (fila.inner_text(timeout=3_000) or "").splitlines():
            if linea.strip():
                return linea.strip()
    except (PWTimeout, PWError):
        pass
    return ""


def buscar(page, usuario: str, timeout_s: float = 15.0):
    """Filtra por `usuario` y devuelve la fila SOLO si es EXACTAMENTE ese.

    El match exacto evita el peor error posible: buscar 'fauno2' trae tambien
    'fauno232' y 'fauno2999', y la primera fila puede ser cualquiera de los
    tres. Depositar en el que no es no se deshace.
    """
    caja = primero(page, SEL_BUSCAR, 15_000)
    if caja is None:
        log.warning("  no encontre el buscador de usuarios en el panel")
        return None

    caja.click(timeout=10_000)
    caja.fill("")
    # El panel aclara "introducir texto solo en minusculas" al lado del campo.
    caja.type(usuario.lower(), delay=50)

    # Si aparece el desplegable de coincidencias, elegir la opcion ya deja el
    # filtro puesto y ahorra el paso siguiente.
    sug = primero(page, SEL_SUGERENCIA, 3_000)
    if sug is not None:
        try:
            sug.click(timeout=4_000)
        except (PWTimeout, PWError) as e:
            log.info("  la sugerencia no se dejo clickear (%s), sigo igual", e)

    # "Aplicar Filtro": sin esto la tabla ni se entera de lo que tipeamos.
    aplicar = primero(page, SEL_APLICAR, 4_000)
    if aplicar is not None:
        try:
            aplicar.click(timeout=6_000)
        except (PWTimeout, PWError) as e:
            log.warning("  no pude apretar 'Aplicar Filtro': %s", e)
    else:
        # En escritorio el buscador vive dentro de un <form>: Enter lo envia.
        log.info("  no vi 'Aplicar Filtro', pruebo con Enter")
        try:
            caja.press("Enter", timeout=4_000)
        except (PWTimeout, PWError):
            pass

    limite = time.monotonic() + timeout_s
    visto = ""
    while time.monotonic() < limite:
        fila = primero(page, SEL_FILA, 1_000)
        if fila is not None:
            visto = nombre_de_fila(page, fila)
            if visto.lower() == usuario.lower():
                return fila
        page.wait_for_timeout(300)

    log.warning("  la primera fila quedo en '%s', no en '%s'", visto or "(vacia)", usuario)
    # Sin esto hay que adivinar si fallo el filtro, la busqueda o el layout.
    try:
        page.screenshot(path=str(bot.SHOTS / f"fichas_sin_fila_{usuario}.png"))
    except (PWTimeout, PWError):
        pass
    return None


def ir_a_usuarios(page) -> None:
    if URL_USUARIOS.rstrip("/") not in page.url.rstrip("/"):
        page.goto(URL_USUARIOS, wait_until="domcontentloaded")
    else:
        page.reload(wait_until="domcontentloaded")
    if bot.es_pantalla_login(page):
        raise bot.SesionExpirada(page.url)
    if primero(page, SEL_BUSCAR, 20_000) is None:
        raise PWTimeout("No cargo el listado de usuarios del panel")


# ---------------------------------------------------------------------------
# La carga
# ---------------------------------------------------------------------------
def cargar_en_panel(page, usuario: str, monto: float) -> tuple[str, str]:
    """Deposita `monto` en la cuenta de `usuario`.

    Devuelve (estado, detalle) donde estado es 'hecha' | 'error' | 'revisar'.

    'revisar' es el estado importante: significa "no puedo afirmar si entro o
    no". Ahi el server NO devuelve las fichas y NO se reintenta, porque las dos
    cosas cuestan plata si la carga en realidad si entro.
    """
    ir_a_usuarios(page)

    fila = buscar(page, usuario)
    if fila is None:
        return "error", f"No encontre al usuario '{usuario}' en el panel"

    try:
        antes = montos_de(fila.inner_text(timeout=5_000))
    except (PWTimeout, PWError):
        antes = []

    boton = primero(page, SEL_DEPOSITAR_EN_FILA, 8_000, raiz=fila)
    if boton is None:
        page.screenshot(path=str(bot.SHOTS / f"fichas_sin_boton_{usuario}.png"))
        return "error", f"Encontre a '{usuario}' pero no el boton de depositar en su fila"

    boton.click(timeout=10_000)

    # El panel navega a /user/deposit/<id>. Que llegue ahi confirma que se
    # abrio la pantalla de UN jugador; el cual, lo garantiza el match exacto.
    try:
        page.wait_for_url(RX_URL_DEPOSITO, timeout=20_000)
    except PWTimeout:
        page.screenshot(path=str(bot.SHOTS / f"fichas_sin_deposito_{usuario}.png"))
        return "error", f"No se abrio la pantalla de deposito (segui en {page.url})"

    id_panel = (RX_URL_DEPOSITO.search(page.url) or [None, "?"])[1]
    log.info("  %s -> pantalla de deposito (id %s), cargando %g", usuario, id_panel, monto)

    caja = primero(page, SEL_DEP_MONTO, 20_000)
    if caja is None:
        page.screenshot(path=str(bot.SHOTS / f"fichas_sin_input_{usuario}.png"))
        return "error", "No encontre el campo del monto en la pantalla de deposito"

    problema = poner_monto(page, caja, monto)
    if problema:
        page.screenshot(path=str(bot.SHOTS / f"fichas_monto_{usuario}.png"))
        # Nada que revisar: no se llego a apretar Depositar, no se movio un peso.
        return "error", problema

    if MODO != "LIVE":
        page.screenshot(path=str(bot.SHOTS / f"dryrun_fichas_{usuario}.png"))
        return "dry-run", f"[DRY_RUN] campo cargado con {monto:g} para {usuario}, NO se envio"

    confirmar = primero(page, SEL_DEP_CONFIRMAR, 10_000)
    if confirmar is None:
        page.screenshot(path=str(bot.SHOTS / f"fichas_sin_confirmar_{usuario}.png"))
        return "error", "No encontre el boton de depositar"

    confirmar.click(timeout=10_000)

    # El panel pide confirmar en un modal cuando se crea un jugador; si tambien
    # lo hace aca y nadie lo aprieta, el deposito no entra nunca. Si no hay
    # modal, esto devuelve "sin modal" y no toca nada.
    log.info("  %s", bot.confirmar_modal(page, 6_000, RX_MODAL_DEPOSITO))

    # La confirmacion de verdad es el SALDO, no un cartel: volvemos al listado y
    # comparamos. Un toast puede decir "ok" y no haber acreditado nada.
    page.wait_for_timeout(2_500)
    try:
        ir_a_usuarios(page)
        fila2 = buscar(page, usuario)
    except (PWTimeout, PWError) as e:
        return "revisar", f"Deposito enviado pero no pude releer el saldo: {e}"

    if fila2 is None:
        return "revisar", "Deposito enviado pero el usuario no volvio a aparecer en la lista"

    try:
        despues = montos_de(fila2.inner_text(timeout=5_000))
    except (PWTimeout, PWError):
        return "revisar", "Deposito enviado pero no pude leer la fila despues"

    # Se compara la fila entera, y no una celda fija, porque en mobile el saldo
    # no tiene selector propio y viene mezclado con el resto.
    #
    # Primero por POSICION: si la fila tiene los mismos campos que antes, el que
    # subio el monto es el saldo, y no hay lugar a dudas. La comparacion suelta
    # (cualquiera con cualquiera) queda de respaldo, pero puede dar un falso
    # positivo: en la fila tambien hay numeros del nombre y de la fecha.
    if len(antes) == len(despues):
        for i, a in enumerate(antes):
            if abs((despues[i] - a) - monto) < 0.01:
                return "hecha", f"Saldo {a:.2f} -> {despues[i]:.2f}"

    for a in antes:
        for d in despues:
            if abs((d - a) - monto) < 0.01:
                return "hecha", f"Saldo {a:.2f} -> {d:.2f} (por coincidencia de monto)"

    if antes and antes == despues:
        return "error", "Ningun numero de la fila se movio. El deposito no entro."

    # Ni lo esperado ni "no paso nada": puede haber apostado en el medio.
    # No se toca la plata: lo mira una persona.
    return "revisar", (f"No pude confirmar el deposito de {monto}. "
                       f"Fila antes {antes} / despues {despues}")


# ---------------------------------------------------------------------------
# Una pasada
# ---------------------------------------------------------------------------
def procesar_pendientes(page, api: ApiAcciones | None = None, limite: int = 10) -> int:
    """Ejecuta las acciones que haya. Devuelve cuantas proceso.

    Pensada para llamarse desde el loop de bot_crear_jugador.py, reusando su
    navegador. No lanza por errores de red: la cola se vuelve a mirar despues.
    """
    api = api or ApiAcciones()

    try:
        lote = api.pendientes(limite)
    except requests.RequestException as e:
        log.error("Cola de saldo inaccesible: %s", e)
        return 0

    if not lote:
        return 0

    log.info("%d accion(es) de saldo pendiente(s)", len(lote))

    for acc in lote:
        etiqueta = f"{acc.get('id')} / {acc.get('usuario')} / {acc.get('tipo')} {acc.get('monto')}"

        if acc.get("tipo") != "cargar":
            log.warning("  %s -> el retiro todavia no esta automatizado", etiqueta)
            api.marcar(acc["id"], "revisar", "Retiro: hacerlo a mano en el panel")
            continue

        log.info("Ejecutando %s", etiqueta)
        try:
            estado, detalle = cargar_en_panel(page, str(acc["usuario"]), float(acc["monto"]))
        except bot.SesionExpirada:
            # La sesion se cayo: NO se marca nada. La accion queda 'procesando'
            # y a los 15 min el server la manda a 'revisar'. Marcarla como error
            # aca seria devolverle las fichas a alguien que quizas ya cobro.
            log.warning("  sesion caida en el medio de %s", etiqueta)
            raise
        except Exception as e:
            log.exception("  excepcion en %s", etiqueta)
            try:
                page.screenshot(path=str(bot.SHOTS / f"fichas_error_{acc['id']}.png"))
            except Exception:
                pass
            # No sabemos en que punto se corto: puede haber depositado.
            api.marcar(acc["id"], "revisar", f"Excepcion: {e}")
            continue

        if estado == "dry-run":
            log.info("  %s", detalle)
            continue                      # se liberan todas juntas al final

        nivel = log.info if estado == "hecha" else log.warning
        nivel("  %s -> %s", estado.upper(), detalle)
        api.marcar(acc["id"], estado, detalle)

        time.sleep(1.5)                   # respirar entre operaciones

    if MODO != "LIVE":
        # En DRY_RUN devolvemos a la cola lo que reclamamos, si no queda trabado
        # en 'procesando' y nadie lo ejecuta nunca.
        try:
            log.info("[DRY_RUN] devuelvo %d accion(es) a la cola", api.liberar())
        except requests.RequestException as e:
            log.error("No pude liberar la cola: %s", e)

    return len(lote)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="una sola pasada")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--lote", type=int, default=10)
    args = ap.parse_args()

    if MODO != "LIVE":
        log.warning("MODO %s: se completa el formulario pero NO se deposita. "
                    "Para depositar de verdad: FICHAS_MODE=LIVE", MODO)

    try:
        api = ApiAcciones()
    except bot.ErrorConfig as e:
        log.error("%s", e)
        return 1

    with sync_playwright() as p:
        browser, ctx = bot.nuevo_contexto(p, headless=args.headless, con_sesion=True)
        page = ctx.new_page()
        page.set_default_timeout(15_000)

        if not bot.sesion_viva(page):
            log.info("Sin sesion valida, intento login automatico...")
            if not bot.login_automatico(page) or not bot.sesion_viva(page):
                log.error("No pude entrar al panel.")
                browser.close()
                return 1
            bot.guardar_sesion(ctx, page)

        log.info("Sesion OK. Escuchando la cola de saldo cada %ss...", POLL)

        try:
            while True:
                try:
                    procesar_pendientes(page, api, args.lote)
                except bot.SesionExpirada:
                    log.warning("Sesion caida, re-logueando...")
                    if not bot.login_automatico(page):
                        log.error("No pude re-loguear.")
                        return 1
                    bot.guardar_sesion(ctx, page)

                if args.once:
                    break
                time.sleep(POLL)
        except KeyboardInterrupt:
            log.info("Cortado por el usuario")
        finally:
            browser.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
