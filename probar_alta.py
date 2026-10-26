#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probar_alta.py — Prueba de ESCRITURA: ¿loguea Y crea un jugador en este dominio?

Es la prueba que de verdad decide si se puede mover el bot a un dominio nuevo.
La de solo lectura (probar_dominios.py) no alcanza: ganamos7 logueaba y leia
bien, pero el POST de ESCRITURA (alta, deposito) daba Unauthorized. Esto crea
UN jugador de verdad por la API.

CREA POR LA API, NO POR EL FORMULARIO (POST a /api/agent_admin/user/), que es
como el bot crea el 99% de las veces (fast-path). Asi la prueba NO depende del
DOM del formulario -- que puede cambiar entre dominios (ganamosbet.net tiene
otra estructura que ganamosonline) -- y mide exactamente lo que importa: si la
cuenta puede ESCRIBIR por ese host.

CREA UN JUGADOR REAL (uno solo, prefijo "testio" para encontrarlo y borrarlo).
Si el dominio es nuestra misma cuenta, aparece en el panel; se borra buscando
"testio". No toca el bot de produccion: su propio contexto y sesion.

    docker cp bot/probar_alta.py ganamos-bot-creador:/app/
    docker exec ganamos-bot-creador python /app/probar_alta.py agents.ganamosbet.net

Sin argumento prueba agents.ganamos.io.
"""
import json
import random
import sys

import bot_crear_jugador as bot
import alta_api
from playwright.sync_api import sync_playwright


def main() -> int:
    host = (sys.argv[1] if len(sys.argv) > 1 else "agents.ganamos.io").strip()
    base = f"https://{host}"

    bot.LOGIN_URL = f"{base}/"
    bot.PANEL_URL = f"{base}/user/create-player"
    bot.PANEL_API = f"{base}/api"
    bot.URL_LISTADO = f"{base}/users/all"
    bot.HOST_PANEL = bot._host_de(bot.PANEL_URL) or host

    url_api = f"{base}/api/agent_admin/user/"
    sufijo = random.randint(100000, 999999)
    usuario = f"testio{sufijo}"
    clave = "Prueba1234x"
    # Payload SUPERSET que cubre las DOS versiones del panel que vimos:
    #  - la vieja (ganamosonline/ganamos.io): firstName/lastName/confirmPassword
    #  - la nueva (ganamos.co/.net/.bet/ganamos1): first_name/last_name + role:0
    # El bundle del panel nuevo confirma role 0 = player (6 = agent). Los campos
    # de mas los ignora cada backend; asi un solo payload crea en cualquiera.
    cuerpo = json.dumps({
        "username": usuario, "password": clave, "confirmPassword": clave,
        "email": f"{usuario}@goldpaw.game",
        "firstName": "Prueba", "lastName": "Dominio",
        "first_name": "Prueba", "last_name": "Dominio",
        "role": 0,
    }, ensure_ascii=False)

    print(f"Probando ESCRITURA (POST a la API) en {host}")
    print(f"  Jugador de prueba: {usuario}  (borralo despues buscando 'testio')\n")

    with sync_playwright() as p:
        browser, ctx = bot.nuevo_contexto(p, headless=True, con_sesion=False)
        try:
            page = ctx.new_page()
            page.set_default_timeout(20_000)

            # --- LOGIN (deja la cookie de sesion en el contexto) ---
            try:
                ok = bot.login_automatico(page)
            except Exception as e:
                print(f"RESULTADO: login tiro excepcion -> {str(e).splitlines()[0][:140]}")
                return 1
            if not ok:
                print("RESULTADO: NO LOGUEO (ver capturas/login_*.png).")
                print("  -> nuestra cuenta no entra aca, o hubo captcha/2FA/WAF.")
                return 1
            print("Login: OK")

            # --- ALTA POR API (comparte cookies con el login) ---
            req = page.context.request
            try:
                resp = req.fetch(url_api, method="POST", data=cuerpo,
                                 headers={"content-type": "application/json"},
                                 timeout=20_000, max_redirects=20)
                st = resp.status
                try: txt = resp.text()
                except Exception: txt = ""
                final_url = resp.url or ""
            except Exception as e:
                print(f"RESULTADO: el POST tiro excepcion -> {str(e).splitlines()[0][:140]}")
                return 1

            # Un challenge del WAF NO es un fallo de escritura: la request no
            # llego al backend. Se distingue para no confundirlo con Unauthorized.
            if alta_api.es_challenge(txt):
                print(f"\nRESULTADO en {host}: el WAF interpuso un challenge (HTTP {st}).")
                print("  No prueba ni a favor ni en contra: reintentá o probá otro rato.")
                return 2

            redir = bool(final_url) and not bot._mismo_endpoint(final_url, url_api)
            res, msg = alta_api.evaluar_respuesta(st, txt, redirected=redir, url_final=final_url)

            print(f"\nRESULTADO en {host}:")
            if res is True:
                print(f"  ESCRITURA OK -> jugador '{usuario}' creado por API. HTTP {st}. {msg}")
                print("  Confirmalo en el panel (buscá 'testio'). Si aparece, este dominio")
                print("  sirve para ESCRIBIR con nuestra cuenta. Despues borralo.")
                return 0
            elif res is False:
                # 'False' = el backend respondio pero NO creo (p.ej. nombre
                # tomado). No es un fallo del dominio: respondio bien.
                print(f"  El backend respondio pero no creo: {msg} (HTTP {st}).")
                print("  El dominio ESCRIBE (contesto el backend); fue este alta puntual.")
                return 0
            else:
                print(f"  NO se creo. HTTP {st}. {msg}")
                cuerpo_corto = (txt or "").replace("\n", " ")[:200]
                print(f"  cuerpo: {cuerpo_corto}")
                print("  -> si dice Unauthorized, es el sintoma de ganamos7: entra pero no escribe.")
                return 1
        finally:
            try: browser.close()
            except Exception: pass


if __name__ == "__main__":
    raise SystemExit(main())
