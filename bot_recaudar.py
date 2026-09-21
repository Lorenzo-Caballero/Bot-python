#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bot_recaudar.py — Recauda el saldo de jugadores INACTIVOS desde el panel.

ESTO SACA PLATA DE CUENTAS DE JUGADORES. Leé las salvaguardas antes de correrlo.

POR QUE VA POR LA API Y NO POR EL DOM (16/09/2026)
    Hasta hoy este script operaba el panel a mano: clickeaba el header para
    ordenar por saldo, clickeaba la flecha del paginador, leia el saldo de la
    celda, tocaba RETIRO/Todo/Confirmar. Todo eso pelea contra un React que no
    escucha los clicks de Playwright, y por eso "ordenar" y "paginar" fallaban
    intermitentemente (ocho commits intentandolo). El resto del proyecto -- las
    altas (crear_lote_por_fetch), los depositos y retiros del colector
    (_depositar_una, retirar_del_jugador) y el espejo de usuarios
    (sync_usuarios) -- ya dejaron el DOM y hablan con la API REST del panel. Era
    el ultimo que faltaba.

    Ahora el listado sale de la MISMA API que ya usa sync_usuarios
    (GET /agent_admin/user/?...), que trae id, username y balance de cada
    jugador. Con eso:
      * ORDENAR de mayor a menor es un sort en Python -- infalible, sin header
        que clickear, sin re-verificar en cada pagina.
      * PAGINAR y SALTAR es cortar una lista ordenada -- sin flechas.
      * RETIRAR es POST /agent_admin/user/{id}/payment/ {operation:1, amount},
        el MISMO endpoint que ya usa el colector para los retiros pedidos por
        chat (probado con plata real). Con deteccion del challenge del WAF.

QUE HACE
    1. Trae TODOS los jugadores por la API del panel.
    2. Los ordena por SALDO de mayor a menor (en Python).
    3. SALTEA los primeros `--saltar` * 50 de mayor saldo (4 -> 200): los que
       mas saldo tienen suelen ser los que acaban de cargar, y a esos no se los
       toca. El dry-run muestra la lista EXACTA que va a tocar: es ahi donde se
       confirma la seleccion, no en el numero de "saltar".
    4. Para cada objetivo (tras los filtros de abajo): POST de retiro por su
       saldo.

LAS SALVAGUARDAS, Y POR QUE
    a) DRY-RUN POR DEFECTO. Sin --si no retira un peso: lista lo que HARIA.
       Que la primera corrida sea inofensiva no es ceremonia -- es la unica
       forma de ver a quien iba a tocar antes de tocarlo.
    b) INACTIVIDAD DE VERDAD (--dias, 30 por defecto). "Los primeros 200" no es
       un criterio de inactividad: es una posicion en una lista que cambia
       sola. Por eso, antes de retirar, el bot le pregunta a NUESTRA base
       (api/inactivos.php) cuantos dias hace que cada uno no aparece, y saltea a
       los activos, a los que no conoce y a los que no tienen el dato. Con
       --sin-chequeo se puede apagar: no lo hagas.
    c) TOPES. --max (cuantos retiros por corrida) y --min-saldo (no tocar
       saldos chicos). Un bot que mueve plata sin techo es una mala noche.
    d) EL MONTO SALE DEL SALDO QUE INFORMA LA API, no de un scrape. Y como el
       objetivo son inactivos (30+ dias sin jugar), ese saldo no se mueve entre
       que se lee y se retira. Si aun asi la plataforma tuviera menos, el POST
       vuelve con status != 0 y se marca 'revisar' (no se reintenta: pudo haber
       entrado), nunca se retira de mas.

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
import math
import os
import sys
import time

from decimal import Decimal, ROUND_DOWN

import requests
from playwright.sync_api import sync_playwright

import bot_crear_jugador as bot
import alta_api

log = logging.getLogger("recaudar")

# Cuantos jugadores de mayor saldo salta cada unidad de --saltar. 50 = el
# tamaño de pagina de la API del panel (POR_PAGINA de sync_usuarios), asi
# "saltar 4" son las primeras 4 "paginas" de mayor saldo, como antes. El
# dry-run muestra a quien deja adentro, que es donde de verdad se confirma.
SALTO_POR_PAGINA = 50
# count por pagina al pedir el listado (mismo que sync_usuarios).
API_POR_PAGINA = 50
# Pausa entre paginas del GET: gentil con Cloudflare (el panel esta detras).
API_PAUSA_PAGINA = 0.4

def _get_api(ctx, url, intentos: int = 4, espera: float = 2.0):
    """GET a la API del panel con reintentos y deteccion del challenge del WAF.

    El panel corta la conexion seguido ('socket hang up') y Cloudflare a veces
    responde el challenge (HTML). Las dos cosas se reintentan: un challenge NO
    es un dato, es 'volve a preguntar'. Mismo criterio que sync_usuarios y el
    colector. Devuelve el JSON, o lanza si no se pudo tras los reintentos."""
    ultimo = None
    for i in range(intentos):
        try:
            r = ctx.request.get(url, timeout=60_000)
            txt = r.text()
            if alta_api.es_challenge(txt):
                raise RuntimeError("challenge del WAF (Cloudflare)")
            import json as _json
            return _json.loads(txt)
        except Exception as e:
            ultimo = e
            log.warning("  GET fallo (intento %d/%d): %s",
                        i + 1, intentos, str(e).splitlines()[0][:120])
            if i < intentos - 1:
                time.sleep(espera * (i + 1))
    raise ultimo


def traer_jugadores(ctx) -> list[dict]:
    """TODOS los jugadores del agente, por la API del panel, ORDENADOS por
    saldo de mayor a menor. [{'id', 'usuario', 'saldo'}].

    Misma consulta que sync_usuarios.traer_todos (misma cuenta, mismos
    jugadores). El orden lo hace Python -- no hay header que clickear ni que
    re-verificar en cada pagina. Los baneados quedan afuera (is_banned=false),
    como en el sync."""
    me = _get_api(ctx, f"{bot.PANEL_API}/user/check")
    agent_id = me["result"]["id"]
    log.info("Agente %s (id %s)", me["result"].get("username", "?"), agent_id)

    todos, pag = [], 0
    while pag < 1000:
        url = (f"{bot.PANEL_API}/agent_admin/user/?count={API_POR_PAGINA}&page={pag}"
               f"&user_id={agent_id}&is_banned=false&is_direct_structure=false")
        data = _get_api(ctx, url)
        items = _items_de(data) or []
        if not items:
            break
        for u in items:
            uid = u.get("id")
            usuario = (u.get("username") or "").strip()
            saldo = u.get("balance") or 0
            if uid is None or usuario == "":
                continue
            try:
                saldo = float(saldo)
            except (TypeError, ValueError):
                saldo = 0.0
            todos.append({"id": uid, "usuario": usuario, "saldo": saldo})
        pag += 1
        if pag % 10 == 0:
            log.info("  ...%d jugadores (%d paginas)", len(todos), pag)
        time.sleep(API_PAUSA_PAGINA)

    todos.sort(key=lambda j: j["saldo"], reverse=True)
    log.info("Total: %d jugadores, ordenados por saldo (mayor $%.0f -> menor $%.0f)",
             len(todos), todos[0]["saldo"] if todos else 0,
             todos[-1]["saldo"] if todos else 0)
    return todos


def _items_de(data):
    """La lista de usuarios adentro de la respuesta, en cualquiera de sus
    formas (la API envuelve en result/users/items/data). Igual que
    sync_usuarios.items_de."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("users", "items", "result", "data"):
            v = data.get(k)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                r = _items_de(v)
                if r is not None:
                    return r
    return None


def monto_retirable(monto: float) -> float:
    """El monto a pedir: NUNCA mas de lo que el jugador tiene.

    EL BUG (21/09/2026, encontrado con el log en pantalla que se agrego ese
    mismo dia). Esto mandaba `int(round(monto))`, o sea que REDONDEABA HACIA
    ARRIBA y le pedia a la plataforma mas plata de la que habia:

        belu5364: retirando $19      <- su saldo era $18,60
        ERROR {"status":501,"error_message":"Balance is insufficient"}

    Explica exactamente lo que se veia. En la corrida #15, de 10 jugadores:
    los de $18,60 y $18,55 fallaron (round -> 19) y los de $18,50 y $18,40
    salieron (round -> 18, porque Python redondea .5 al par). En la #14
    fallaron los 20: todos tenian entre $17,50 y $17,55, y round() los llevaba
    a 18.

    Redondear hacia arriba es inofensivo al DEPOSITAR --le entras un centavo
    de mas-- y fatal al RETIRAR. No son la misma operacion aunque compartan
    endpoint.

    Se trunca a 2 decimales hacia abajo: se lleva todo lo que se pueda sin
    pasarse nunca. Si la plataforma no aceptara decimales, retirar_por_api
    reintenta con el entero (ver ahi).

    CON Decimal Y NO CON floor(monto*100)/100, que es lo primero que uno
    escribe: en binario 18.40*100 es 1839.9999... y el floor se come un
    centavo de CADA retiro. Sobre cien jugadores eso es plata que quedo en
    cuentas que se estaban vaciando, y del lado nuestro no se ve.
    """
    if monto is None:
        return 0.0
    v = Decimal(str(monto))
    if v <= 0:
        return 0.0
    return float(v.quantize(Decimal("0.01"), rounding=ROUND_DOWN))


def retirar_por_api(ctx, id_ganamos: int, monto: float) -> tuple[bool, str]:
    """Retira `monto` del saldo del jugador por la API del panel. (ok, detalle).

    MISMO endpoint y misma logica que colector/aprobar_cargas.retirar_del_jugador
    (operation=1 = retiro, probado con plata real): el resultado viene en el
    CUERPO, no en el codigo HTTP (el panel contesta 200 igual cuando falla).

      * challenge del WAF -> reintento (no llego al backend, es seguro); si
        persiste, se da por fallado sin reintentar mas.
      * 2xx con status 0 -> retirado.
      * 2xx con status != 0, o cuerpo ilegible, o !2xx -> NO retirado, y NO se
        reintenta: pudo haber entrado, repetirlo seria sacar dos veces.
    """
    url = f"{bot.PANEL_API}/agent_admin/user/{int(id_ganamos)}/payment/"
    cuerpo = ""
    r = None
    pedido = monto_retirable(monto)
    for i in range(3):
        try:
            r = ctx.request.post(url, data={"operation": 1, "amount": pedido},
                                 timeout=45_000)
        except Exception as e:
            return False, f"no se pudo confirmar el retiro ({e})"
        try:
            cuerpo = r.text()
        except Exception:
            cuerpo = ""
        if not alta_api.es_challenge(cuerpo):
            break
        if i == 2:
            return False, f"el WAF corto el retiro (challenge persistente) | {cuerpo[:200]}"
        time.sleep(1.5 * (i + 1))

    corto = cuerpo[:250]
    if not r.ok:
        return False, f"el panel respondio {r.status} | {corto}"
    try:
        import json as _json
        d = _json.loads(cuerpo)
    except Exception:
        return False, f"respuesta ilegible del panel ({r.status}) | {corto}"
    if not isinstance(d, dict) or d.get("status") not in (0, "0"):
        msg = (d.get("error_message") if isinstance(d, dict) else "") or ""

        # SI RECHAZO UN MONTO CON CENTAVOS, se reintenta con el entero.
        # No se sabe si la plataforma acepta decimales --el saldo los tiene,
        # pero el deposito siempre viajo entero-- asi que en vez de suponerlo
        # se prueba: primero el monto exacto, y si lo rechaza, el entero de
        # abajo. Peor seria elegir el entero de entrada y dejar los centavos
        # de cada jugador sin retirar.
        #
        # ESTE ES EL UNICO REINTENTO DE UNA ESCRITURA EN TODO EL PROYECTO, y
        # se permite porque el rechazo es EXPLICITO y determinista: status 501
        # con su mensaje prueba que la plataforma NO movio un peso. La regla
        # que sigue en pie es la otra: una respuesta AMBIGUA (ilegible, 5xx,
        # timeout) no se reintenta nunca, porque ahi si pudo haber entrado.
        if pedido != int(pedido):
            entero = int(math.floor(pedido))
            if entero > 0:
                log.info("   reintento con el entero ($%d): la plataforma no tomo los centavos", entero)
                try:
                    r2 = ctx.request.post(url, data={"operation": 1, "amount": entero}, timeout=45_000)
                    c2 = r2.text()
                    d2 = _json.loads(c2)
                    if isinstance(d2, dict) and d2.get("status") in (0, "0"):
                        return True, f"retirado por API ({r2.status}) por ${entero} (sin los centavos)"
                    msg2 = (d2.get("error_message") if isinstance(d2, dict) else "") or ""
                    return False, f"la plataforma no hizo el retiro {msg2 or msg} | {c2[:250]}".strip()
                except Exception as e:
                    # Ambiguo: NO se vuelve a intentar (pudo haber entrado).
                    return False, f"no se pudo confirmar el reintento entero ({e})"
        return False, f"la plataforma no hizo el retiro {msg} | {corto}".strip()
    return True, f"retirado por API ({r.status}) por ${pedido:.2f}"


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


# Cuantas lineas de log se conservan para la pantalla. Es una ventana, no un
# archivo: el log completo sigue yendo a stdout del contenedor. 200 lineas
# entran de sobra en el campo `resultado` (60 KB) y cubren una corrida entera.
LOG_MAX = 200


def _avisar(reporte: dict, linea: str = "", *, fase: str = "", paso: str = "") -> None:
    """Anota el avance y lo MANDA al CRM, para que la pantalla lo muestre mientras pasa.

    POR QUE EXISTE (pedido del dueno, 21/09/2026): *"que desde el frontend vaya
    mostrando el proceso, retiro por retiro, que se vea reflejado en el momento"*
    y *"si hay algun log de error tambien mostrarlo en pantalla asi depuramos"*.

    Antes este bot escribia UNA vez, al terminar: una recaudacion de 25
    jugadores eran varios minutos de "En curso..." en el CRM y despues todo
    junto. Con plata de por medio, no ver que esta pasando es lo peor de las dos
    opciones -- y cuando fallaba, el motivo quedaba en el log del contenedor,
    que hay que ir a buscar por SSH.

    BEST-EFFORT ABSOLUTO: si el POST falla, se sigue recaudando. Un reporte de
    progreso que pueda abortar una corrida a medio camino seria peor que no
    tener reporte -- quedarian jugadores con el saldo retirado y una fila sin
    cerrar.
    """
    if linea:
        hora = time.strftime("%H:%M:%S")
        reporte.setdefault("log", []).append(f"{hora} · {linea}")
        # Se recorta por el principio: lo ultimo que paso es lo que se mira.
        if len(reporte["log"]) > LOG_MAX:
            reporte["log"] = reporte["log"][-LOG_MAX:]
    if fase:
        reporte["fase"] = fase
    if paso:
        reporte["paso"] = paso

    rid = reporte.get("_id")
    if not rid:
        return          # corrida por consola (sin CRM detras): solo el log
    api_url = os.environ.get("API_URL", "")
    api_key = os.environ.get("API_KEY", "")
    if not api_url or not api_key:
        return
    try:
        # `_id` es de uso interno: no viaja al CRM.
        cuerpo = {k: v for k, v in reporte.items() if k != "_id"}
        requests.post(
            _url_cola(api_url), params={"accion": "avance"},
            headers={"X-API-Key": api_key, "User-Agent": bot.UA},
            json={"id": rid, "resultado": cuerpo}, timeout=8,
        )
    except Exception:
        pass    # ver arriba: nunca frena la recaudacion


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

        # La sesion se valida con el navegador (la cookie de login vive ahi);
        # despues TODO va por ctx.request, que comparte esa cookie -- sin tocar
        # el DOM. Igual que sync_usuarios y el colector.
        if not bot.sesion_viva(page):
            log.info("sin sesion valida, intento login...")
            if not bot.login_automatico(page) or not bot.sesion_viva(page):
                log.error("no pude entrar al panel")
                browser.close()
                return 1
            bot.guardar_sesion(ctx, page)

        _avisar(reporte, "buscando jugadores en el panel...",
                fase="buscando", paso="Trayendo el listado del panel")
        try:
            jugadores = traer_jugadores(ctx)
        except Exception as e:
            log.error("no pude leer el listado por la API: %s", e)
            _avisar(reporte, f"ERROR al leer el listado: {e}", fase="error")
            browser.close()
            return 1
        if not jugadores:
            log.warning("el panel no devolvio jugadores")
            _avisar(reporte, "el panel no devolvio jugadores", fase="error")
            browser.close()
            return 0
        _avisar(reporte, f"{len(jugadores)} jugadores traidos; ordeno por saldo",
                paso="Ordenando por saldo y filtrando")

        # SALTAR los primeros de mayor saldo (los que suelen acabar de cargar).
        # Es cortar la lista ya ordenada: sin flechas, sin re-verificar orden.
        # EN JUGADORES, no en "paginas" (21/09/2026). "Pagina" significaba una
        # cosa en el panel --donde el operador elige 10, 25 o 50 por pagina-- y
        # otra aca, que usaba el tamaño de la API (50). Mirando la pagina 4 del
        # panel se veian saldos de $75 y este bot, con el mismo "4", saltaba
        # 200 y tocaba los de $17: los dos numeros correctos, hablando de cosas
        # distintas. El jugador es la unidad que no cambia de significado.
        # `saltar` (paginas) sigue andando para quien lo llame por consola.
        saltar_n = getattr(args, "saltar_jug", None)
        if saltar_n is None:
            saltar_n = max(0, args.saltar) * SALTO_POR_PAGINA
        saltar_n = max(0, int(saltar_n))
        if saltar_n:
            log.info("salteo los %d jugador(es) de mayor saldo", saltar_n)
        restantes = jugadores[saltar_n:]
        if not restantes:
            log.warning("tras saltar %d no queda nadie (hay %d jugadores en total)",
                        saltar_n, len(jugadores))
            browser.close()
            return 0

        log.info("de mayor saldo entre los que quedan: %s",
                 ", ".join(f"{j['usuario']}=${j['saldo']:.0f}" for j in restantes[:8]))

        # Saldo minimo primero (barato) y despues la inactividad (una consulta).
        candidatos = [j for j in restantes if j["saldo"] >= args.min_saldo]
        bajos = len(restantes) - len(candidatos)
        if bajos:
            log.info("    %d salteado(s) por saldo < minimo $%.0f", bajos, args.min_saldo)

        if args.sin_chequeo:
            log.warning("!! --sin-chequeo: NO se verifica inactividad contra la base")
            permitidos = {j["usuario"] for j in candidatos}
        else:
            # Solo se consulta la inactividad de los primeros candidatos que
            # podrian entrar (tope * un colchon), no de miles: inactivos.php
            # recibe una lista acotada y la seleccion final respeta el orden.
            tope_consulta = max(args.max * 3, args.max + 20)
            permitidos, _ = filtrar_inactivos(
                [j["usuario"] for j in candidatos[:tope_consulta]], args.dias)

        objetivo = [j for j in candidatos if j["usuario"] in permitidos][: args.max]
        total = sum(j["saldo"] for j in objetivo)
        reporte["objetivo"] = [{"usuario": j["usuario"], "saldo": j["saldo"]} for j in objetivo]
        reporte["total_objetivo"] = total

        _avisar(reporte,
                f"{len(objetivo)} jugador(es) seleccionados, ${total:,.0f} en total"
                .replace(",", "."),
                paso=("A recaudar" if args.si else "Prueba: no se toca nada"))
        log.info("")
        log.info("=== %s: %d jugador(es), $%.2f en total ===",
                 "A RECAUDAR" if args.si else "DRY-RUN (no se toca nada)",
                 len(objetivo), total)
        for j in objetivo:
            log.info("    %-28s $%.2f", j["usuario"], j["saldo"])
        if not objetivo:
            log.info("    (nadie cumple las condiciones)")
            _avisar(reporte, "nadie cumple las condiciones", fase="listo")
            browser.close()
            return 0
        if not args.si:
            log.info("")
            log.info("Esto fue una PRUEBA. Si es lo que queres, agrega --si")
            _avisar(reporte, "prueba terminada (no se retiro nada)", fase="listo")
            browser.close()
            return 0

        hechos, fallados, recaudado = 0, 0, 0.0
        reporte["de"] = len(objetivo)
        _avisar(reporte, f"empiezo a retirar de {len(objetivo)} jugador(es)",
                fase="retirando", paso="Retirando")
        for n, j in enumerate(objetivo, 1):
            log.info("-> %s (id %s, $%.2f)", j["usuario"], j["id"], j["saldo"])
            # El "voy por este" se manda ANTES del POST: si el retiro se cuelga
            # --el panel tarda, el WAF desafia-- la pantalla muestra en quien
            # se colgo, que es justo lo que hace falta para depurarlo.
            reporte["hechos"] = n - 1
            _avisar(reporte, f"{j['usuario']}: retirando ${j['saldo']:,.0f}".replace(",", "."),
                    paso=f"Retirando {n} de {len(objetivo)}")
            ok, detalle = retirar_por_api(ctx, int(j["id"]), j["saldo"])
            if ok:
                hechos += 1
                recaudado += j["saldo"]
                log.info("   OK  %s", detalle)
                _avisar(reporte, f"{j['usuario']}: OK, ${j['saldo']:,.0f} retirados".replace(",", "."))
            else:
                fallados += 1
                log.warning("   FALLO %s", detalle)
                # El motivo COMPLETO va a la pantalla: es lo que se necesita
                # para saber si fue el WAF, la sesion o el saldo.
                _avisar(reporte, f"{j['usuario']}: ERROR - {detalle}")
            reporte["detalle"].append({
                "usuario": j["usuario"], "saldo": j["saldo"],
                "ok": ok, "detalle": detalle,
            })
            reporte["hechos"] = n
            reporte["retirados"] = hechos
            reporte["total"] = recaudado
            reporte["fallados"] = fallados
            _avisar(reporte)      # la lista parcial, tras cada retiro
            time.sleep(1.0)

        reporte["retirados"] = hechos
        reporte["total"] = recaudado
        reporte["fallados"] = fallados
        _avisar(reporte,
                f"listo: {hechos} retirado(s), {fallados} fallado(s)", fase="listo",
                paso="Terminada")
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
    log.info("demonio de recaudacion escuchando %s (cada %ds) [version %s]",
             url, poll, os.environ.get("BOT_VERSION", "desconocido"))

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

        # `_id` es lo que habilita el reporte EN VIVO: con el, _avisar()
        # manda cada paso a recaudar_cola.php?accion=avance y el CRM lo
        # muestra mientras pasa. Una corrida por consola no lo trae y solo
        # escribe en el log, como siempre.
        reporte: dict = {"_id": pid}
        estado, mensaje = "hecha", None
        try:
            args = _ns(si=not pedido["dry_run"], dias=pedido["dias"],
                       saltar=pedido["saltar"],
                       # La cola manda los jugadores a saltear; un server viejo
                       # no lo trae y se cae a `saltar * 50`, como antes.
                       saltar_jug=pedido.get("saltar_jug"),
                       max=pedido["tope"],
                       min_saldo=pedido["min_saldo"], sin_chequeo=False,
                       headless=headless)
            rc = recaudar(args, reporte)
            if rc != 0 and reporte.get("fallados", 0) > 0:
                mensaje = f"{reporte.get('fallados')} retiro(s) fallaron"
        except Exception as e:
            log.exception("pedido #%d exploto", pid)
            estado, mensaje = "error", str(e)[:255]
            # Que el motivo llegue a la pantalla del CRM: es la unica forma de
            # depurar sin entrar por SSH al contenedor.
            try:
                _avisar(reporte, f"EXCEPCION: {e}", fase="error")
            except Exception:
                pass

        try:
            s.post(url, params={"accion": "marcar"},
                   json={"id": pid, "estado": estado,
                         "resultado": {k: v for k, v in reporte.items() if k != "_id"},
                         "mensaje": mensaje}, timeout=20).raise_for_status()
            log.info("== pedido #%d -> %s ==", pid, estado)
        except Exception as e:
            log.error("no pude marcar el pedido #%d: %s", pid, e)
        time.sleep(poll)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Recauda el saldo de jugadores inactivos desde el panel.")
    ap.add_argument("--demonio", action="store_true",
                    help="escucha la cola del CRM y ejecuta los pedidos (para el VPS)")
    ap.add_argument("--saltar-jug", type=int, default=None, dest="saltar_jug",
                    help="saltear los N jugadores de mayor saldo (gana sobre --saltar)")
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
