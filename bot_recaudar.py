#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bot_recaudar.py — Recauda el saldo de jugadores INACTIVOS desde el panel.

ESTO SACA PLATA DE CUENTAS DE JUGADORES. Leé las salvaguardas antes de correrlo.

QUE HACE
    1. Entra al listado de jugadores del panel (URL_LISTADO, del .env).
    2. Ordena por SALDO de mayor a menor.
    3. SALTEA las primeras `--saltar` paginas (4 por defecto): los que mas
       saldo tienen suelen ser los que acaban de cargar, y a esos no se los
       toca.
    4. De la pagina en la que quedo, para cada jugador: abre RETIRO, toca
       "Todo" y confirma.
    5. Vuelve al listado y RE-ORDENA: el panel limpia el orden despues de cada
       retiro.

LAS TRES SALVAGUARDAS, Y POR QUE
    a) DRY-RUN POR DEFECTO. Sin --si no retira un peso: lista lo que HARIA.
       Que la primera corrida sea inofensiva no es ceremonia -- es la unica
       forma de ver a quien iba a tocar antes de tocarlo.
    b) INACTIVIDAD DE VERDAD (--dias, 30 por defecto). "Pagina 4" no es un
       criterio de inactividad: es una posicion en una lista que cambia sola.
       El dia que haya menos jugadores, el primero de la pagina 4 puede ser
       alguien que cargo ayer. Por eso, antes de retirar, el bot le pregunta
       a NUESTRA base (api/inactivos.php) cuantos dias hace que cada uno no
       aparece, y saltea a los activos, a los que no conoce y a los que no
       tienen el dato. Con --sin-chequeo se puede apagar: no lo hagas.
    c) TOPES. --max (cuantos retiros por corrida) y --min-saldo (no tocar
       saldos chicos). Un bot que mueve plata sin techo es una mala noche.

USO
    python bot_recaudar.py                      # dry-run: muestra y no toca
    python bot_recaudar.py --si                 # recauda de verdad
    python bot_recaudar.py --si --dias 60 --max 5
    python bot_recaudar.py --si --saltar 6 --min-saldo 500

Reusa el login, la sesion y los helpers de bot_crear_jugador.py: una sola
forma de entrar al panel, un solo lugar donde arreglarla.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

import requests
from playwright.sync_api import sync_playwright
from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout

import bot_crear_jugador as bot

log = logging.getLogger("recaudar")

# ---------------------------------------------------------------------------
# SELECTORES del panel (capturados 12/9/2026 sobre agents.ganamos*.com).
#
# Cada uno lleva alternativas mas cortas: la cadena larga de #root > div > ...
# se rompe con cualquier reacomodo del markup, y cuando se rompe el bot no
# falla -- hace NADA en silencio, que con plata es peor. primer_selector()
# prueba en orden y usa el primero que aparezca.
# ---------------------------------------------------------------------------
BASE = ("#root > div > div.app__wrapper > main > div.app__wrapper__content "
        "> div.users > div.users-table.users-table_tab_all")

SEL_ORDEN_SALDO = [
    f"{BASE} > div.users-table__table > div.users-table__table-header > "
    "div.users-table__table-header-head.users-table__table-header-head_balance",
    ".users-table__table-header-head_balance",
]
SEL_PAGINA_ACTUAL = [
    f"{BASE} > div.users-table__paginator-wrapper > div.paginator-switcher > "
    "div > div.paginator-switcher__pages > div",
    ".paginator-switcher__pages > div",
]
SEL_SIGUIENTE = [
    f"{BASE} > div.users-table__paginator-wrapper > div.paginator-switcher > "
    "div > div:nth-child(4)",
    ".paginator-switcher > div > div:nth-child(4)",
]
SEL_FILAS = [
    f"{BASE} > div.users-table__table > div.users-table__tbody > div",
    ".users-table__tbody > div",
]
# Dentro de UNA fila (se busca relativo a ella, no desde #root).
SEL_FILA_USUARIO = ".adm-bets-table-row-user__td-data-user"
SEL_FILA_RETIRO  = "a.button.button_colors_full-transparent"

SEL_TODO = [
    "#root > div > div.app__wrapper > main > div.app__wrapper__content > div > "
    "div > div.withdrawal__top > div.withdrawal__input-block > div > "
    "div.withdrawal__all-btn > button",
    ".withdrawal__all-btn > button",
]
SEL_CONFIRMAR = [
    "#root > div > div.app__wrapper > main > div.app__wrapper__content > div > "
    "div > div.withdrawal__bottom > button.button.button_sizable_low.button_colors_default",
    ".withdrawal__bottom > button.button_colors_default",
]


def _num(txt: str) -> float:
    """'23.000,50' / '23,000.50' / '$ 1.234' -> float. 0.0 si no hay numero."""
    t = "".join(c for c in (txt or "") if c.isdigit() or c in ".,-")
    if not t:
        return 0.0
    # El ultimo separador es el decimal; los otros son de miles.
    ic, ip = t.rfind(","), t.rfind(".")
    if ic > ip:
        t = t.replace(".", "").replace(",", ".")
    else:
        t = t.replace(",", "")
    try:
        return float(t)
    except ValueError:
        return 0.0


def ordenar_por_saldo(page) -> bool:
    """Deja el listado ordenado por saldo de MAYOR a MENOR.

    El header alterna asc/desc en cada click, asi que no alcanza con clickear:
    hay que MIRAR como quedo. Se lee el saldo de la primera y la ultima fila y,
    si quedo ascendente, se clickea otra vez. Sin esto, un dia el bot recauda
    exactamente al reves de lo que queres.
    """
    try:
        sel = bot.primer_selector(page, SEL_ORDEN_SALDO, 10_000)
    except Exception as e:
        log.error("no encuentro el header de saldo (%s)", e)
        return False

    for intento in (1, 2):
        try:
            page.locator(sel).first.click(timeout=8_000)
        except (PWError, PWTimeout) as e:
            log.error("no pude clickear el orden por saldo: %s", e)
            return False
        page.wait_for_timeout(1_200)
        saldos = _saldos_de_la_pagina(page)
        if len(saldos) < 2:
            return True                      # una sola fila: nada que ordenar
        if saldos[0] >= saldos[-1]:
            log.info("  orden: saldo de mayor a menor (%.2f ... %.2f)",
                     saldos[0], saldos[-1])
            return True
        log.info("  quedo ascendente, clickeo de nuevo")
    log.error("no logre dejarlo de mayor a menor")
    return False


def _saldos_de_la_pagina(page) -> list[float]:
    try:
        filas = page.locator(bot.primer_selector(page, SEL_FILAS, 8_000))
        out = []
        for i in range(min(filas.count(), 20)):
            celdas = filas.nth(i).locator("div")
            txt = ""
            for j in range(min(celdas.count(), 4)):
                t = (celdas.nth(j).inner_text(timeout=2_000) or "").strip()
                if any(c.isdigit() for c in t) and "/" not in t:
                    txt = t
                    break
            out.append(_num(txt))
        return out
    except Exception:
        return []


def pagina_actual(page) -> str:
    """Etiqueta de la pagina, solo para el log. NO se usa para decidir si
    avanzo: SEL_PAGINA_ACTUAL es el CONTENEDOR de los numeros ('1 2 3 4...'),
    que dice lo mismo en todas las paginas -- por eso saltar_paginas compara
    el CONTENIDO de las filas, no esto."""
    try:
        sel = bot.primer_selector(page, SEL_PAGINA_ACTUAL, 5_000)
        return (page.locator(sel).first.inner_text(timeout=3_000) or "?").strip().replace("\n", " ")
    except Exception:
        return "?"


def _firma_filas(page) -> str:
    """Los primeros nombres de la pagina actual, para saber si REALMENTE
    cambio al pasar de pagina. Es infalible: no depende de como el panel
    dibuje el paginador (que fue lo que rompio la deteccion por el numero)."""
    js = jugadores_de_la_pagina(page)
    return "|".join(j["usuario"] for j in js[:3])


def saltar_paginas(page, cuantas: int) -> bool:
    """Avanza `cuantas` paginas. False si alguna no avanzo (se quedo sin)."""
    for n in range(cuantas):
        antes = _firma_filas(page)
        try:
            sel = bot.primer_selector(page, SEL_SIGUIENTE, 6_000)
            page.locator(sel).first.click(timeout=6_000)
        except (PWError, PWTimeout) as e:
            log.error("no pude clickear 'siguiente' (%s)", e)
            return False
        # Esperar a que el contenido CAMBIE (no solo un timeout fijo): el panel
        # tarda distinto cada vez, y leer antes de que repinte daria un falso
        # "no cambio". Hasta ~6s.
        cambio = False
        for _ in range(12):
            page.wait_for_timeout(500)
            if _firma_filas(page) != antes:
                cambio = True
                break
        if not cambio:
            log.warning("la pagina no cambio: no hay mas paginas (o el boton "
                        "'siguiente' no respondio)")
            return False
        log.info("  pagina -> %s", pagina_actual(page))
    return True


def jugadores_de_la_pagina(page) -> list[dict]:
    """[{'i': indice, 'usuario': str, 'saldo': float}] de la pagina actual."""
    out: list[dict] = []
    try:
        filas = page.locator(bot.primer_selector(page, SEL_FILAS, 8_000))
        total = filas.count()
    except Exception as e:
        log.error("no pude leer las filas: %s", e)
        return out

    for i in range(total):
        fila = filas.nth(i)
        try:
            usuario = (fila.locator(SEL_FILA_USUARIO).first
                       .inner_text(timeout=3_000) or "").strip()
        except Exception:
            usuario = ""
        if not usuario:
            continue
        saldo = 0.0
        try:
            celdas = fila.locator("div")
            for j in range(min(celdas.count(), 4)):
                t = (celdas.nth(j).inner_text(timeout=1_500) or "").strip()
                if t and any(c.isdigit() for c in t) and "/" not in t and usuario not in t:
                    saldo = _num(t)
                    break
        except Exception:
            pass
        out.append({"i": i, "usuario": usuario.splitlines()[0].strip(), "saldo": saldo})
    return out


def filtrar_inactivos(usuarios: list[str], dias: int) -> tuple[set[str], dict]:
    """Le pregunta a NUESTRA base quienes llevan >= `dias` sin aparecer.

    Devuelve (set de inactivos, detalle). Ante CUALQUIER problema devuelve el
    set vacio: si no se puede confirmar la inactividad, no se retira nada. El
    modo seguro de una duda con plata es no hacer nada.
    """
    api_url = os.environ.get("API_URL", "")
    api_key = os.environ.get("API_KEY", "")
    if not api_url or not api_key:
        log.error("faltan API_URL/API_KEY: no puedo confirmar inactividad")
        return set(), {}
    url = api_url.split("?")[0].rsplit("/", 1)[0] + "/inactivos.php"
    try:
        r = requests.post(
            url, params={"accion": "filtrar"},
            headers={"X-API-Key": api_key, "User-Agent": bot.UA},
            json={"usuarios": usuarios, "dias": dias}, timeout=20,
        )
        r.raise_for_status()
        d = r.json()
    except Exception as e:
        log.error("no pude consultar %s (%s): no se retira nada", url, e)
        return set(), {}
    if not d.get("ok"):
        log.error("la API rechazo el pedido: %s", d.get("error", "sin detalle"))
        return set(), {}

    inact = {x["usuario"]: x for x in d.get("inactivos", [])}
    for x in d.get("activos", []):
        log.info("    saltea %s: activo hace %s dia(s)", x["usuario"], x["dias_inactivo"])
    for u in d.get("desconocidos", []):
        log.info("    saltea %s: no esta en nuestro espejo", u)
    for u in d.get("sin_dato", []):
        log.info("    saltea %s: sin dato de actividad", u)
    return set(inact), inact


def retirar_uno(page, indice: int, usuario: str, dry: bool) -> tuple[bool, str]:
    """Abre RETIRO de esa fila, toca 'Todo' y confirma. (ok, detalle)."""
    try:
        filas = page.locator(bot.primer_selector(page, SEL_FILAS, 8_000))
        fila = filas.nth(indice)
        # El nombre se re-verifica CONTRA LA FILA que vamos a tocar: entre que
        # se leyo la lista y este click el panel pudo repaginar, y retirarle a
        # otro por un indice viejo no tiene vuelta atras.
        actual = (fila.locator(SEL_FILA_USUARIO).first
                  .inner_text(timeout=4_000) or "").strip().splitlines()[0].strip()
        if actual != usuario:
            return False, f"la fila {indice} ahora es '{actual}', no '{usuario}'"
        fila.locator(SEL_FILA_RETIRO).first.click(timeout=8_000)
    except (PWError, PWTimeout) as e:
        return False, f"no pude abrir el retiro: {e}"

    page.wait_for_timeout(2_000)      # el panel tarda en montar la pantalla

    if "/withdrawal/" not in page.url:
        return False, f"no llegue a la pantalla de retiro (url {page.url})"

    try:
        sel_todo = bot.primer_selector(page, SEL_TODO, 8_000)
        page.locator(sel_todo).first.click(timeout=6_000)
    except (PWError, PWTimeout) as e:
        return False, f"no pude tocar 'Todo': {e}"
    page.wait_for_timeout(600)

    if dry:
        return True, "DRY-RUN: no confirmo"

    try:
        sel_ok = bot.primer_selector(page, SEL_CONFIRMAR, 8_000)
        btn = page.locator(sel_ok).first
        if not bot.esperar_habilitado(page, btn):
            return False, "el boton RETIRO siguio apagado (¿saldo 0?)"
        btn.click(timeout=8_000)
    except (PWError, PWTimeout) as e:
        return False, f"no pude confirmar el retiro: {e}"

    page.wait_for_timeout(2_000)
    return True, "retirado"


def recaudar(args, reporte: dict | None = None) -> int:
    # `reporte` (opcional): el demonio pasa un dict y esta funcion lo llena con
    # {objetivo, retirados, total, fallados, detalle} para reportarlo al CRM.
    # main() (uso por consola) pasa None y solo mira el log.
    if reporte is None:
        reporte = {}
    reporte.setdefault("detalle", [])
    with sync_playwright() as p:
        browser, ctx = bot.nuevo_contexto(p, headless=args.headless, con_sesion=True)
        page = ctx.new_page()
        page.set_default_timeout(15_000)

        if not bot.sesion_viva(page):
            log.info("sin sesion valida, intento login...")
            if not bot.login_automatico(page) or not bot.sesion_viva(page):
                log.error("no pude entrar al panel")
                browser.close()
                return 1
            bot.guardar_sesion(ctx, page)

        page.goto(bot.URL_LISTADO, wait_until="domcontentloaded")
        page.wait_for_timeout(1_500)

        if not ordenar_por_saldo(page):
            browser.close()
            return 1
        if args.saltar and not saltar_paginas(page, args.saltar):
            log.error("no pude saltar %d pagina(s): corto para no tocar a los "
                      "de mas saldo (que son los que acaban de cargar)", args.saltar)
            browser.close()
            return 1

        jugadores = jugadores_de_la_pagina(page)
        if not jugadores:
            log.warning("la pagina %s no tiene jugadores", pagina_actual(page))
            browser.close()
            return 0

        log.info("pagina %s: %d jugador(es)", pagina_actual(page), len(jugadores))

        # Saldo minimo primero (barato) y despues la inactividad (una consulta).
        candidatos = [j for j in jugadores if j["saldo"] >= args.min_saldo]
        for j in jugadores:
            if j["saldo"] < args.min_saldo:
                log.info("    saltea %s: saldo %.2f < minimo %.2f",
                         j["usuario"], j["saldo"], args.min_saldo)

        if args.sin_chequeo:
            log.warning("!! --sin-chequeo: NO se verifica inactividad contra la base")
            permitidos = {j["usuario"] for j in candidatos}
        else:
            permitidos, _ = filtrar_inactivos([j["usuario"] for j in candidatos], args.dias)

        objetivo = [j for j in candidatos if j["usuario"] in permitidos][: args.max]
        total = sum(j["saldo"] for j in objetivo)
        reporte["objetivo"] = [{"usuario": j["usuario"], "saldo": j["saldo"]} for j in objetivo]
        reporte["total_objetivo"] = total

        log.info("")
        log.info("=== %s: %d jugador(es), $%.2f en total ===",
                 "A RECAUDAR" if args.si else "DRY-RUN (no se toca nada)",
                 len(objetivo), total)
        for j in objetivo:
            log.info("    %-28s $%.2f", j["usuario"], j["saldo"])
        if not objetivo:
            log.info("    (nadie cumple las condiciones)")
            browser.close()
            return 0
        if not args.si:
            log.info("")
            log.info("Esto fue una PRUEBA. Si es lo que queres, agrega --si")
            browser.close()
            return 0

        hechos, fallados, recaudado = 0, 0, 0.0
        for j in objetivo:
            log.info("-> %s ($%.2f)", j["usuario"], j["saldo"])
            # Cada retiro arranca del listado recien ordenado: el panel pierde
            # el orden al volver, y los indices de fila cambian con el.
            page.goto(bot.URL_LISTADO, wait_until="domcontentloaded")
            page.wait_for_timeout(1_200)
            if not ordenar_por_saldo(page) or (args.saltar and not saltar_paginas(page, args.saltar)):
                log.error("   no pude volver a la pagina: corto")
                break
            vivos = jugadores_de_la_pagina(page)
            fila = next((v for v in vivos if v["usuario"] == j["usuario"]), None)
            if fila is None:
                log.warning("   ya no esta en esta pagina, lo dejo para la proxima")
                continue
            ok, detalle = retirar_uno(page, fila["i"], j["usuario"], dry=False)
            if ok:
                hechos += 1
                recaudado += fila["saldo"]
                log.info("   OK  %s", detalle)
            else:
                fallados += 1
                log.warning("   FALLO %s", detalle)
            reporte["detalle"].append({
                "usuario": j["usuario"], "saldo": fila["saldo"],
                "ok": ok, "detalle": detalle,
            })
            time.sleep(1.0)

        reporte["retirados"] = hechos
        reporte["total"] = recaudado
        reporte["fallados"] = fallados
        log.info("")
        log.info("=== Listo: %d retirado(s) por $%.2f, %d fallado(s) ===",
                 hechos, recaudado, fallados)
        browser.close()
        return 0 if fallados == 0 else 1


def _url_cola(api_url: str) -> str:
    """De .../altas_cola.php (o cualquier .php del API) -> .../recaudar_cola.php."""
    return api_url.split("?")[0].rsplit("/", 1)[0] + "/recaudar_cola.php"


def _ns(**kw):
    """Un objeto tipo argparse liviano, para reusar recaudar() desde el demonio."""
    from types import SimpleNamespace
    return SimpleNamespace(**kw)


def demonio(headless: bool, poll: int) -> int:
    """Pollea la cola del CRM (recaudar_cola.php) y ejecuta cada pedido.

    El agente toca «Recaudar» en el CRM -> se encola una fila -> este loop la
    reclama y corre recaudar() con esos topes, reportando el resultado. La
    cola ya garantiza UNA a la vez (accion=pendientes no entrega otra mientras
    haya una en 'procesando'), asi que no hay dos recaudaciones pisandose.
    Best-effort de punta a punta: un pedido que explota se marca 'error' con
    el motivo, y el loop sigue con el siguiente.
    """
    api_url = os.environ.get("API_URL", "")
    api_key = os.environ.get("API_KEY", "")
    if not api_url or not api_key:
        log.error("faltan API_URL/API_KEY en el .env: el demonio no puede consultar la cola")
        return 1
    url = _url_cola(api_url)
    s = requests.Session()
    s.headers.update({"X-API-Key": api_key, "User-Agent": bot.UA})
    log.info("demonio de recaudacion escuchando %s (cada %ds)", url, poll)

    while True:
        pedido = None
        try:
            r = s.get(url, params={"accion": "pendientes"}, timeout=20)
            r.raise_for_status()
            pedido = (r.json() or {}).get("datos")
        except Exception as e:
            log.error("no pude leer la cola (%s): %s", url, e)

        if not pedido:
            time.sleep(poll)
            continue

        pid = int(pedido["id"])
        log.info("== pedido #%d: %s dias=%s saltar=%s tope=%s min=%s (por %s) ==",
                 pid, "REAL" if not pedido["dry_run"] else "PRUEBA",
                 pedido["dias"], pedido["saltar"], pedido["tope"],
                 pedido["min_saldo"], pedido.get("pedido_por", "?"))

        reporte: dict = {}
        estado, mensaje = "hecha", None
        try:
            args = _ns(si=not pedido["dry_run"], dias=pedido["dias"],
                       saltar=pedido["saltar"], max=pedido["tope"],
                       min_saldo=pedido["min_saldo"], sin_chequeo=False,
                       headless=headless)
            rc = recaudar(args, reporte)
            if rc != 0 and reporte.get("fallados", 0) > 0:
                mensaje = f"{reporte.get('fallados')} retiro(s) fallaron"
        except Exception as e:
            log.exception("pedido #%d exploto", pid)
            estado, mensaje = "error", str(e)[:255]

        try:
            s.post(url, params={"accion": "marcar"},
                   json={"id": pid, "estado": estado,
                         "resultado": reporte, "mensaje": mensaje}, timeout=20).raise_for_status()
            log.info("== pedido #%d -> %s ==", pid, estado)
        except Exception as e:
            log.error("no pude marcar el pedido #%d: %s", pid, e)
        time.sleep(poll)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Recauda el saldo de jugadores inactivos desde el panel.")
    ap.add_argument("--demonio", action="store_true",
                    help="escucha la cola del CRM y ejecuta los pedidos (para el VPS)")
    ap.add_argument("--poll", type=int, default=20,
                    help="cada cuantos segundos revisa la cola, en --demonio (20)")
    ap.add_argument("--si", action="store_true",
                    help="RETIRA DE VERDAD (sin esto es una prueba y no toca nada)")
    ap.add_argument("--dias", type=int, default=30,
                    help="dias sin actividad para considerarlo inactivo (30)")
    ap.add_argument("--saltar", type=int, default=4,
                    help="paginas a saltear tras ordenar por saldo desc (4)")
    ap.add_argument("--max", type=int, default=10,
                    help="tope de retiros por corrida (10)")
    ap.add_argument("--min-saldo", type=float, default=100.0, dest="min_saldo",
                    help="no tocar saldos menores a esto (100)")
    ap.add_argument("--sin-chequeo", action="store_true",
                    help="NO verificar inactividad contra la base (peligroso)")
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    if args.demonio:
        return demonio(args.headless, args.poll)
    if args.si and args.sin_chequeo:
        log.warning("=" * 66)
        log.warning("  --si + --sin-chequeo: vas a retirar SIN confirmar que")
        log.warning("  esten inactivos. Te quedan 5 segundos para cortar.")
        log.warning("=" * 66)
        time.sleep(5)
    return recaudar(args)


if __name__ == "__main__":
    sys.exit(main())
