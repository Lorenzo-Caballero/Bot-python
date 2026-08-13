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
# Selectores del panel (verificados contra la pantalla real)
# ---------------------------------------------------------------------------
_CONT = ("#root > div > div.app__wrapper > main > div.app__wrapper__content")

SEL_BUSCAR = (f"{_CONT} > div.users > div.users__filter > form > "
              "div:nth-child(1) > div.search-user-input > div > input")

# La tabla no es <table>: son divs. La fila 1 es la unica que se toca.
_FILA1 = (f"{_CONT} > div.users > div.users-table.users-table_tab_all > "
          "div.users-table__table > div.users-table__tbody > div:nth-child(1)")

SEL_FILA_USUARIO   = f"{_FILA1} > div:nth-child(1)"
SEL_FILA_SALDO     = f"{_FILA1} > div:nth-child(2)"
SEL_FILA_DEPOSITAR = (f"{_FILA1} > div:nth-child(3) > div > "
                      "a.button.button_sizable_default.button_colors_default")

SEL_DEP_MONTO = (f"{_CONT} > div > div > div.deposit__top > div.deposit__inputs > "
                 "div:nth-child(1) > div > div > div > div > input")
SEL_DEP_CONFIRMAR = (f"{_CONT} > div > div > div.deposit__bottom > "
                     "button.button.button_sizable_low.button_colors_default")

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
def parse_monto(txt: str) -> float:
    """'1.440,40' -> 1440.4   El panel usa formato es-AR (punto=miles)."""
    limpio = re.sub(r"[^\d,.\-]", "", txt or "")
    limpio = limpio.replace(".", "").replace(",", ".")
    try:
        return float(limpio)
    except ValueError:
        return 0.0


def texto_usuario(page) -> str:
    """La celda trae el usuario y abajo la etiqueta PLAYER. Solo la 1ra linea."""
    crudo = page.locator(SEL_FILA_USUARIO).inner_text(timeout=5_000)
    for linea in (crudo or "").splitlines():
        if linea.strip():
            return linea.strip()
    return ""


def buscar(page, usuario: str, timeout_s: float = 12.0) -> float | None:
    """Filtra por `usuario` y espera a que la fila 1 sea EXACTAMENTE ese.

    Devuelve el saldo de la fila, o None si nunca aparecio.

    El match exacto es lo que evita el peor error posible: buscar 'fauno2' trae
    tambien 'fauno232' y 'fauno2999', y la fila 1 puede ser cualquiera de los
    tres. Depositar en el que no es no se deshace.
    """
    caja = page.locator(SEL_BUSCAR)
    caja.click(timeout=10_000)
    caja.fill("")
    # El panel avisa "introducir texto solo en minusculas" al lado del campo.
    caja.type(usuario.lower(), delay=40)

    limite = time.monotonic() + timeout_s
    while time.monotonic() < limite:
        try:
            if texto_usuario(page).lower() == usuario.lower():
                return parse_monto(page.locator(SEL_FILA_SALDO).inner_text(timeout=5_000))
        except (PWTimeout, PWError):
            pass                      # la tabla se esta redibujando
        page.wait_for_timeout(300)

    return None


def ir_a_usuarios(page) -> None:
    if URL_USUARIOS.rstrip("/") not in page.url.rstrip("/"):
        page.goto(URL_USUARIOS, wait_until="domcontentloaded")
    else:
        page.reload(wait_until="domcontentloaded")
    if bot.es_pantalla_login(page):
        raise bot.SesionExpirada(page.url)
    page.wait_for_selector(SEL_BUSCAR, timeout=20_000)


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

    saldo_antes = buscar(page, usuario)
    if saldo_antes is None:
        return "error", f"No encontre al usuario '{usuario}' en el panel"

    log.info("  %s: saldo actual %.2f, cargando %.2f", usuario, saldo_antes, monto)

    page.click(SEL_FILA_DEPOSITAR, timeout=10_000)
    page.wait_for_selector(SEL_DEP_MONTO, timeout=20_000)

    caja = page.locator(SEL_DEP_MONTO)
    caja.click()
    caja.fill("")
    # Entero si es entero: algunos campos rechazan el punto decimal.
    caja.type(str(int(monto)) if float(monto).is_integer() else str(monto), delay=40)

    if MODO != "LIVE":
        page.screenshot(path=str(bot.SHOTS / f"dryrun_fichas_{usuario}.png"))
        return "dry-run", f"[DRY_RUN] formulario listo con {monto}, NO se envio"

    page.click(SEL_DEP_CONFIRMAR, timeout=10_000)

    # La confirmacion de verdad es el SALDO, no un cartel: volvemos a buscar al
    # usuario y comparamos. Un toast puede decir "ok" y no haber acreditado.
    page.wait_for_timeout(2_500)
    try:
        ir_a_usuarios(page)
        saldo_despues = buscar(page, usuario)
    except (PWTimeout, PWError) as e:
        return "revisar", f"Deposito enviado pero no pude releer el saldo: {e}"

    if saldo_despues is None:
        return "revisar", "Deposito enviado pero el usuario no volvio a aparecer en la lista"

    delta = saldo_despues - saldo_antes

    if abs(delta - monto) < 0.01:
        return "hecha", f"Saldo {saldo_antes:.2f} -> {saldo_despues:.2f}"

    if abs(delta) < 0.01:
        return "error", f"El saldo no se movio ({saldo_antes:.2f}). El deposito no entro."

    # Ni lo esperado ni cero: puede ser que el jugador haya apostado en el medio.
    # No se toca la plata: lo mira una persona.
    return "revisar", (f"Saldo {saldo_antes:.2f} -> {saldo_despues:.2f} "
                       f"(esperaba +{monto:.2f}, dio +{delta:.2f})")


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
