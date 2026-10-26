#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probar_dominios.py — ¿Nuestra cuenta de agente funciona en estos dominios?

Prueba de SOLO LECTURA. Para cada dominio candidato:
  1. loguea con NUESTRAS credenciales (las mismas del bot: CRM o .env),
  2. lee la info del agente (GET /api/user/check) -> ID y usuario,
  3. lee una muestra de jugadores (GET /api/agent_admin/user/) -> cuantos ve.

NO crea cuentas, NO deposita, NO retira, NO toca plata. Solo GETs despues del
login. Es seguro correrlo en produccion.

QUE RESPONDE, y por que importa: el CLAUDE.md avisa que dos dominios pueden
servir el MISMO panel (mismo frontend) y sin embargo ser cuentas/backends
distintos -- o leer bien y fallar al escribir. Esto confirma lo primero (misma
cuenta) comparando el ID de agente que devuelve cada uno. Lo segundo (que las
ESCRITURAS anden) sigue necesitando un alta de prueba real: esto no lo cubre a
proposito, porque un alta de prueba crea un jugador de verdad.

Corre DENTRO del contenedor del bot (ahi estan Playwright, Chromium y las
credenciales):

    docker cp bot/probar_dominios.py ganamos-bot-creador:/app/
    docker exec ganamos-bot-creador python /app/probar_dominios.py

Dominios por defecto: los candidatos medidos el 25/10/2026. Se pueden pasar
otros como argumentos:

    docker exec ganamos-bot-creador python /app/probar_dominios.py agents.ganamos.io agents.ganamosbet.net
"""
import sys

import bot_crear_jugador as bot
from playwright.sync_api import sync_playwright

# El que usamos hoy va primero, como patron de comparacion: su ID de agente es
# el "correcto", y un candidato sirve si devuelve EL MISMO.
CANDIDATOS_DEFAULT = [
    "agents.ganamosonline.com",   # EN USO (referencia)
    "agents.ganamosbet.net",
    "agents.ganamos.net",
    "agents.ganamos.bet",
    "agents.ganamos.io",
]


def _get_json(ctx, url: str):
    """GET que devuelve (status, json|None). Comparte cookies con el login."""
    try:
        r = ctx.request.get(url, timeout=25_000)
        try:
            return r.status, r.json()
        except Exception:
            return r.status, None
    except Exception as e:
        return 0, {"_error": str(e)[:120]}


def probar_dominio(p, host: str) -> dict:
    """Loguea y lee en UN dominio. Devuelve un dict con el resultado."""
    base = f"https://{host}"
    # Apuntar las constantes del bot a este dominio: _login_con() usa LOGIN_URL,
    # y las lecturas usan PANEL_API. Se overridean antes de cada prueba.
    bot.LOGIN_URL = f"{base}/"
    bot.PANEL_URL = f"{base}/user/create-player"
    bot.PANEL_API = f"{base}/api"
    bot.URL_LISTADO = f"{base}/users/all"

    res = {"host": host, "login": False, "agente_id": None, "agente_user": None,
           "jugadores": None, "nota": ""}

    # Contexto FRESCO por dominio (con_sesion=False): cada login es limpio y la
    # cookie de uno no contamina al otro.
    browser, ctx = bot.nuevo_contexto(p, headless=True, con_sesion=False)
    try:
        page = ctx.new_page()
        try:
            ok = bot.login_automatico(page)
        except Exception as e:
            res["nota"] = "excepcion en login: " + str(e).splitlines()[0][:100]
            return res
        res["login"] = bool(ok)
        if not ok:
            res["nota"] = "no logueo (ver capturas/login_*.png en el contenedor)"
            return res

        # --- SOLO LECTURA de aca en adelante ---
        st, me = _get_json(ctx, f"{base}/api/user/check")
        if isinstance(me, dict):
            # La forma exacta puede variar; se buscan las claves usuales.
            data = me.get("result") if isinstance(me.get("result"), dict) else me
            res["agente_id"] = data.get("id") or data.get("user_id") or data.get("agent_id")
            res["agente_user"] = (data.get("username") or data.get("user")
                                  or data.get("name"))
        else:
            res["nota"] = f"/user/check status {st}"

        st2, lst = _get_json(ctx, f"{base}/api/agent_admin/user/?count=5&page=1")
        if isinstance(lst, dict):
            arr = lst.get("result") or lst.get("data") or lst.get("users")
            if isinstance(arr, list):
                res["jugadores"] = len(arr)
                muestra = [str(u.get("username") or u.get("name") or "?")
                           for u in arr[:3] if isinstance(u, dict)]
                if muestra:
                    res["nota"] = "muestra: " + ", ".join(muestra)
        elif not res["nota"]:
            res["nota"] = f"/agent_admin/user status {st2}"
        return res
    finally:
        try: browser.close()
        except Exception: pass


def main() -> int:
    hosts = sys.argv[1:] or CANDIDATOS_DEFAULT
    print("Prueba de SOLO LECTURA (login + leer agente + leer jugadores).")
    print("No crea cuentas ni toca plata.\n")

    filas = []
    with sync_playwright() as p:
        for h in hosts:
            print(f"-> probando {h} ...", flush=True)
            filas.append(probar_dominio(p, h))

    print("\n" + "=" * 78)
    print(f"{'DOMINIO':<28} {'LOGIN':<6} {'ID AGENTE':<12} {'USUARIO':<16} JUGADORES")
    print("-" * 78)
    for r in filas:
        print(f"{r['host']:<28} {('SI' if r['login'] else 'no'):<6} "
              f"{str(r['agente_id'] or '-'):<12} {str(r['agente_user'] or '-'):<16} "
              f"{r['jugadores'] if r['jugadores'] is not None else '-'}")
        if r["nota"]:
            print(f"{'':<28} {r['nota']}")

    # Veredicto: cuales devuelven el MISMO ID de agente que el de referencia
    # (el primer dominio que logueo y dio un id).
    ref = next((r for r in filas if r["login"] and r["agente_id"]), None)
    print("=" * 78)
    if not ref:
        print("Ningun dominio logueo con nuestra cuenta: revisa credenciales/capturas.")
        return 1
    print(f"Referencia: {ref['host']} -> agente {ref['agente_id']} ({ref['agente_user']})")
    iguales = [r["host"] for r in filas
               if r["login"] and str(r["agente_id"]) == str(ref["agente_id"])]
    distintos = [r["host"] for r in filas
                 if r["login"] and r["agente_id"] and str(r["agente_id"]) != str(ref["agente_id"])]
    print(f"MISMA cuenta ({ref['agente_id']}): {', '.join(iguales) or '-'}")
    if distintos:
        print(f"OTRA cuenta (NO usar): {', '.join(distintos)}")
    print("\nRecorda: esto prueba LECTURA con la misma cuenta. Que las ALTAS y los")
    print("DEPOSITOS funcionen por un dominio nuevo se confirma con un alta de")
    print("prueba real, no con esto.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
