#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probar_alta.py — Prueba de ESCRITURA: ¿loguea Y crea un jugador en este dominio?

Es la prueba que de verdad decide si se puede mover el bot a un dominio nuevo.
La de solo lectura (probar_dominios.py) no alcanza: ganamos7 logueaba y leia
bien, pero el POST de ESCRITURA (deposito, alta) daba Unauthorized. Esto crea
UN jugador de verdad por el formulario, que es la unica forma de saberlo.

CREA UN JUGADOR REAL (uno solo, con prefijo "testio" para encontrarlo y
borrarlo despues). Si el dominio es nuestra misma cuenta, aparece en el panel;
se borra buscando "testio". No toca el bot de produccion: usa su propio
contexto y su propia sesion, aparte del contenedor que atiende la cola.

Corre DENTRO del contenedor del bot:

    docker cp bot/probar_alta.py ganamos-bot-creador:/app/
    docker exec ganamos-bot-creador python /app/probar_alta.py agents.ganamos.io

Sin argumento prueba agents.ganamos.io.
"""
import random
import sys

import bot_crear_jugador as bot
from playwright.sync_api import sync_playwright


def main() -> int:
    host = (sys.argv[1] if len(sys.argv) > 1 else "agents.ganamos.io").strip()
    base = f"https://{host}"

    # Apuntar el bot a este dominio (login y formulario salen de estas globales).
    bot.LOGIN_URL = f"{base}/"
    bot.PANEL_URL = f"{base}/user/create-player"
    bot.PANEL_API = f"{base}/api"
    bot.URL_LISTADO = f"{base}/users/all"
    bot.HOST_PANEL = bot._host_de(bot.PANEL_URL) or host

    sufijo = random.randint(100000, 999999)
    reg = {
        "id": "PRUEBA-IO",
        "usuario": f"testio{sufijo}",
        "password": "Prueba1234x",
        "email": f"testio{sufijo}@goldpaw.game",
        "nombre": "Prueba",
        "apellido": "Dominio",
    }

    print(f"Probando ESCRITURA en {host}")
    print(f"  Jugador de prueba: {reg['usuario']}  (borralo despues buscando 'testio')\n")

    with sync_playwright() as p:
        # Contexto FRESCO (con_sesion=False): login limpio contra ESTE dominio,
        # sin arrastrar la cookie del contenedor de produccion.
        browser, ctx = bot.nuevo_contexto(p, headless=True, con_sesion=False)
        try:
            page = ctx.new_page()
            page.set_default_timeout(20_000)

            # --- LOGIN ---
            try:
                ok = bot.login_automatico(page)
            except Exception as e:
                print(f"RESULTADO: login tiro excepcion -> {str(e).splitlines()[0][:140]}")
                return 1
            if not ok:
                print("RESULTADO: NO LOGUEO (ver capturas/login_*.png en el contenedor)")
                print("  -> nuestra cuenta no entra en este dominio, o hubo captcha/2FA/WAF.")
                return 1
            print("Login: OK")

            # --- ALTA REAL (dry_run=False crea el jugador) ---
            try:
                creado, msg = bot.crear_jugador(page, reg, dry_run=False)
            except Exception as e:
                print(f"RESULTADO: el alta tiro excepcion -> {str(e).splitlines()[0][:140]}")
                print("  (una excepcion NO es un alta hecha: nada se da por creado)")
                return 1

            print(f"\nRESULTADO en {host}:")
            if creado:
                print(f"  ESCRITURA OK -> jugador '{reg['usuario']}' creado. {msg}")
                print(f"  Confirmacion: buscalo en el panel. Si aparece, este dominio")
                print(f"  sirve para ESCRIBIR con nuestra cuenta. Despues borralo (buscar 'testio').")
                return 0
            else:
                print(f"  NO se creo. {msg}")
                print("  -> loguea/lee pero la escritura no paso. NO mover el bot aca.")
                print("  (es exactamente el sintoma de ganamos7: entra, pero el POST no.)")
                return 1
        finally:
            try: browser.close()
            except Exception: pass


if __name__ == "__main__":
    raise SystemExit(main())
