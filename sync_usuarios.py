"""
sync_usuarios.py — Lee TODOS los usuarios del panel de ganamos (con su saldo)
                   y los guarda en la tabla `usuarios` via usuarios_sync.php.

Reusa el login de bot_crear_jugador.py (Playwright) para tener la sesion, y
despues pagina la API JSON del panel (rapido y estable, sin scrapear el DOM).

Correr:
    python sync_usuarios.py             # una pasada
    python sync_usuarios.py --loop 300  # cada 300s (espejo casi en vivo)
    python sync_usuarios.py --headless

.env necesita (ya los tenes):
    PANEL_USER, PANEL_PASS      -> para loguearse al panel
    API_URL, API_KEY            -> API_KEY = BOT_API_KEY del server

La URL para guardar se deriva de API_URL (misma carpeta /api/).
"""

import argparse
import json
import os
import sys
import time
import logging
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot_crear_jugador as bot

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%d/%m %H:%M:%S",
    handlers=[logging.StreamHandler(), logging.FileHandler("sync_usuarios.log", encoding="utf-8")],
)
log = logging.getLogger("sync")

# Del MISMO lugar que el login (bot_crear_jugador -> PANEL_URL). Estaban
# hardcodeadas en agents.ganamos7.com: si el .env apunta a otro panel, el
# espejo terminaba copiando los jugadores de una instalacion distinta de
# aquella donde el alta los crea, y `usuarios` quedaba sin ellos.
PANEL_API = bot.PANEL_API
USERS_URL = bot.URL_LISTADO
POR_PAGINA = 50          # cuantos usuarios por pagina de la API
LOTE_POST = 300          # cuantos mandamos por request a nuestro server
PAUSA_PAGINA = 0.4       # segundos entre paginas (gentil con el WAF)


def url_guardado() -> str:
    """De API_URL (.../gp-api/altas_cola.php) sacamos .../gp-api/usuarios_sync.php

    La rama "/api/" quedo del hosting viejo, donde la API colgaba de ahi. Con el
    prefijo /gp-api/ del VPS no matchea (el guion corta la subcadena) y cae en el
    rsplit generico, que reemplaza solo el nombre del archivo: igual da la ruta
    buena, sea cual sea el prefijo.
    """
    base = os.environ.get("API_URL", "")
    if "/api/" in base:
        return base.rsplit("/api/", 1)[0] + "/api/usuarios_sync.php"
    return base.rsplit("/", 1)[0] + "/usuarios_sync.php"


def items_de(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("users", "items", "result", "data"):
            v = data.get(k)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                r = items_de(v)
                if r is not None:
                    return r
    return None


def normalizar(u: dict) -> dict:
    return {
        "id": u.get("id"),
        "username": u.get("username"),
        "balance": u.get("balance") or 0,
        "bonus": u.get("bonus_balance") or u.get("bonus") or 0,   # por si algun dia viene
        "total_deposits": u.get("total_deposits") or 0,
        "role": u.get("role"),
        "is_banned": bool(u.get("is_banned")),
        "creation_date": u.get("creation_date"),
    }


def mostrar_campos(u: dict) -> None:
    """
    Imprime TODO lo que devuelve el panel para un jugador.

    normalizar() se queda con 8 campos y tira el resto, asi que sin esto no hay
    forma de saber que mas hay disponible. Sirve para dos preguntas concretas:
    si el panel informa la ULTIMA CONEXION (y entonces el login se detecta desde
    acá, sin depender del navegador del jugador), y si trae el email.
    """
    print("\n--- campos que devuelve el panel para un jugador ---")
    for k in sorted(u.keys()):
        v = u[k]
        if isinstance(v, (dict, list)):
            v = f"<{type(v).__name__}>"
        print(f"  {k:28} = {v}")
    print("---------------------------------------------------\n")

    pistas = [k for k in u
              if any(t in k.lower() for t in ("last", "login", "seen", "activ", "online", "mail"))]
    if pistas:
        print("Campos que podrian servir:", ", ".join(pistas))
    else:
        print("No hay ningun campo de ultima conexion ni de email.")


def traer_todos(ctx, solo_campos: bool = False) -> list[dict]:
    """Pagina la API del panel y devuelve la lista completa de usuarios."""
    me = ctx.request.get(f"{PANEL_API}/user/check").json()
    agent_id = me["result"]["id"]
    log.info("Agente %s (id %s)", me["result"]["username"], agent_id)

    todos, pag = [], 0
    while pag < 500:
        url = (f"{PANEL_API}/agent_admin/user/?count={POR_PAGINA}&page={pag}"
               f"&user_id={agent_id}&is_banned=false&is_direct_structure=false")
        data = ctx.request.get(url).json()
        items = items_de(data) or []
        if not items:
            break
        if solo_campos:
            mostrar_campos(items[0])
            return []
        todos.extend(normalizar(u) for u in items)
        pag += 1
        if pag % 10 == 0:
            log.info("  ...%d usuarios (%d paginas)", len(todos), pag)
        time.sleep(PAUSA_PAGINA)
    log.info("Total leido: %d usuarios en %d paginas", len(todos), pag)
    return todos


def _es_challenge(texto: str) -> bool:
    """La pagina de challenge del WAF de Hostinger."""
    t = (texto or "").lower()
    return "just a moment" in t or "checking your browser" in t


def _origen(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}/"


def _pasar_waf(page, origen: str) -> bool:
    """Carga el dominio con el navegador real para que resuelva el challenge JS
    del WAF y quede la cookie de clearance en el contexto. Devuelve True solo si
    realmente llego a una pagina real (no el challenge)."""
    try:
        page.goto(origen, wait_until="domcontentloaded", timeout=60_000)
    except Exception as e:
        log.error("No pude abrir %s: %s", origen, e)
        return False
    for _ in range(20):                       # el challenge se auto-resuelve en unos segundos
        titulo = (page.title() or "").lower()
        if "just a moment" not in titulo and "checking your browser" not in titulo:
            return True
        page.wait_for_timeout(1500)
    return False


def _post_navegador(ctx, dest: str, key: str, lote: list[dict]) -> dict:
    """POST via APIRequestContext del navegador: reusa las cookies del contexto
    (incluida la clearance del WAF) y NO sufre CORS (no es un fetch del DOM)."""
    r = ctx.request.post(
        dest,
        headers={"X-API-Key": key},
        data={"usuarios": lote},          # dict -> Playwright lo manda como JSON
        timeout=60_000,
    )
    return {"status": r.status, "text": r.text()[:800]}


def guardar(usuarios: list[dict], ctx=None, page=None) -> None:
    """Manda los usuarios en lotes a usuarios_sync.php.

    Intenta primero con requests (rapido). Si el WAF de Hostinger devuelve su
    challenge de JavaScript, reintenta a traves del navegador Playwright, que
    si lo resuelve (es un navegador real y ya tiene la cookie de clearance).
    """
    dest = url_guardado()
    key = os.environ["API_KEY"]

    # --- 1) intento rapido con requests ---
    # Connect timeout corto (8s): si el WAF deja la conexion colgada, saltamos
    # al navegador enseguida en vez de esperar 60s. (connect, read).
    s = requests.Session()
    s.headers.update({"X-API-Key": key, "Content-Type": "application/json"})
    try:
        r = s.post(dest, json={"usuarios": usuarios[:LOTE_POST]}, timeout=(8, 60))
        if r.status_code == 200 and not _es_challenge(r.text):
            total = int(r.json().get("guardados", 0))
            for i in range(LOTE_POST, len(usuarios), LOTE_POST):
                rr = s.post(dest, json={"usuarios": usuarios[i:i + LOTE_POST]}, timeout=60)
                rr.raise_for_status()
                total += int(rr.json().get("guardados", 0))
            log.info("Guardados en la base: %d (via requests)", total)
            return
        if _es_challenge(r.text) or r.status_code == 403:
            log.info("El WAF de Hostinger pide challenge; guardo via navegador...")
        else:
            r.raise_for_status()
    except requests.RequestException as e:
        log.info("requests fallo (%s); pruebo via navegador...", e)

    # --- 2) via navegador (pasa el WAF) ---
    if ctx is None or page is None:
        log.error("El WAF bloquea requests y no hay navegador abierto para pasarlo.")
        return
    if not _pasar_waf(page, _origen(dest)):
        log.error("No pude pasar el challenge del WAF de Hostinger con el navegador.")
        return

    total = 0
    for i in range(0, len(usuarios), LOTE_POST):
        lote = usuarios[i:i + LOTE_POST]
        res = _post_navegador(ctx, dest, key, lote)
        if res.get("status") != 200:
            log.error("Guardado rechazado (HTTP %s): %s", res.get("status"), res.get("text", "")[:200])
            continue
        try:
            total += int(json.loads(res["text"]).get("guardados", 0))
        except (ValueError, KeyError):
            log.error("Respuesta rara del server: %s", res.get("text", "")[:200])
    log.info("Guardados en la base: %d (via navegador)", total)


def una_pasada(headless: bool, solo_campos: bool = False) -> int:
    with sync_playwright() as p:
        browser, ctx = bot.nuevo_contexto(p, headless=headless, con_sesion=True)
        page = ctx.new_page()
        page.set_default_timeout(30_000)
        try:
            page.goto(USERS_URL, wait_until="domcontentloaded", timeout=45_000)
        except Exception:
            pass
        page.wait_for_timeout(3000)
        if bot.es_pantalla_login(page):
            log.info("Sesion vencida, relogueando...")
            if not bot.login_automatico(page):
                log.error("No pude loguear. Corre: python bot_crear_jugador.py --login")
                browser.close()
                return 1
            bot.guardar_sesion(ctx, page)

        usuarios = traer_todos(ctx, solo_campos=solo_campos)

        if solo_campos:
            browser.close()
            return 0

        if usuarios:
            # Guardamos con el navegador AUN abierto: si el WAF de Hostinger
            # pide challenge, lo pasamos con el navegador real (requests no puede).
            guardar(usuarios, ctx=ctx, page=page)

        browser.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--loop", type=int, metavar="SEG",
                    help="repetir cada SEG segundos (espejo casi en vivo)")
    ap.add_argument("--campos", action="store_true",
                    help="mostrar todos los campos que devuelve el panel para un "
                         "jugador y salir (no guarda nada)")
    args = ap.parse_args()

    # --campos solo lee el panel, no le manda nada a la API propia.
    if args.campos:
        return una_pasada(args.headless, solo_campos=True)

    if not os.environ.get("API_URL") or not os.environ.get("API_KEY"):
        log.error("Faltan API_URL / API_KEY en el .env")
        return 1

    while True:
        try:
            una_pasada(args.headless)
        except Exception as e:
            log.exception("Error en la pasada: %s", e)
        if not args.loop:
            break
        log.info("Espero %ds para la proxima pasada...", args.loop)
        time.sleep(args.loop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
