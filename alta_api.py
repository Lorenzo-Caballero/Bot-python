"""
alta_api.py — crear jugadores por la API del panel, en paralelo y en ~1s.

POR QUE EXISTE
El camino con Playwright (bot_crear_jugador.crear_jugador) llena el formulario
del panel: hasta 30 s por alta, y de a UNA -- la API sync de Playwright es de
un solo hilo. Con varios pedidos juntos, el ultimo esperaba minutos, justo
cuando del otro lado hay alguien recien llegado de un anuncio mirando "creando
tu cuenta".

La plataforma tiene API REST: sync_usuarios ya la LEE por HTTP y ejecutar_cargas
DEPOSITA con POST /api/agent_admin/user/{id}/payment/. Crear es un POST a esa
misma coleccion. Disparado DENTRO del navegador ya logueado (page.evaluate +
Promise.all), muchos altas salen a la vez, cada uno tarda ~1s, y no hay que
adivinar como pasar Cloudflare: lo cruza el mismo Chrome real que ya opera el
panel, con la cookie de sesion y en el mismo origen (sin CORS).

COMO SE APRENDE EL FORMATO (sin hardcodearlo)
No se fija el cuerpo del POST: cada panel puede pedir claves distintas. La
PRIMERA alta que pasa por el formulario deja capturado el POST real (url +
cuerpo) y de ahi sale una PLANTILLA con los valores reemplazados por marcadores
({{usuario}}, {{password}}, ...). Las siguientes se mandan directo con esa
plantilla. Si el panel cambia su API, el fast-path falla, se cae al formulario
y la plantilla se re-aprende sola.

SEGURIDAD
El fast-path NUNCA da por creada un alta salvo señal 2xx clara. Cualquier duda
--timeout, 5xx, un cuerpo que no se entiende-- devuelve None, y el que llama
cae al formulario, que ademas verifica contra el listado. Asi nunca se entregan
credenciales de una cuenta que capaz no existe (el peor error de este sistema).

Este modulo es LOGICA PURA a proposito (sin red, sin Playwright): se puede
testear entero con t_alta_api.py. El disparo real de los fetch vive en
bot_crear_jugador.py, que es quien tiene el navegador.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse


# Los datos del registro que pueden aparecer en el cuerpo del POST. El orden no
# importa para render, pero para APRENDER la plantilla se reemplaza por longitud
# (ver construir_plantilla): un valor corto no puede comerse un pedazo de otro.
CAMPOS = ("usuario", "password", "email", "nombre", "apellido")

# Marcador en la plantilla: {{usuario}}, {{password}}, ... Es un texto que no
# puede aparecer en un JSON legitimo, asi que sustituirlo no rompe nada.
def _tok(campo: str) -> str:
    return "{{" + campo + "}}"


def endpoint_probable(panel_url: str) -> str:
    """El POST de creacion, DERIVADO del panel. Es solo una pista para el
    diagnostico: la url que se usa de verdad sale de la captura del formulario,
    porque es la unica que se sabe correcta. Mismo criterio que sync_usuarios,
    que arma PANEL_API = {raiz}/api."""
    try:
        u = urlparse(panel_url)
        raiz = f"{u.scheme}://{u.netloc}"
    except Exception:
        raiz = ""
    return f"{raiz}/api/agent_admin/user/"


def _escapado_json(valor: str) -> str:
    """El valor tal como queda DENTRO de un string JSON (sin las comillas).
    json.dumps escapa comillas, barras y unicode igual que lo hizo el panel al
    armar su request, asi que buscar/insertar en esta forma respeta el escapado
    en las dos puntas."""
    return json.dumps(str(valor), ensure_ascii=False)[1:-1]


def construir_plantilla(url: str, metodo: str, content_type: str,
                        cuerpo: str, valores: dict) -> dict | None:
    """De un POST de creacion REAL saca una plantilla reutilizable.

    `valores` son los strings EXACTOS que se tipearon en el formulario
    (usuario, password, email, nombre, apellido). Se los busca en el cuerpo y
    se los reemplaza por su marcador, de mas largo a mas corto para que uno
    corto no pise parte de otro (una password que contiene el nombre, por
    ejemplo).

    Devuelve None si no encuentra al menos usuario Y password: sin esos dos no
    hay forma de rellenar el cuerpo para otro jugador, y una plantilla a medias
    crearia cuentas con los datos de otro. Mejor seguir con el formulario.
    """
    if not cuerpo or not url:
        return None
    es_json = "json" in (content_type or "").lower() or _parece_json(cuerpo)

    plantilla_cuerpo = cuerpo
    encontrados = set()
    # Mas largos primero: evita que reemplazar "ana" rompa "susana".
    for campo in sorted(CAMPOS, key=lambda c: -len(str(valores.get(c, "")))):
        valor = str(valores.get(campo, ""))
        if valor == "":
            continue
        aguja = _escapado_json(valor) if es_json else valor
        if aguja and aguja in plantilla_cuerpo:
            plantilla_cuerpo = plantilla_cuerpo.replace(aguja, _tok(campo))
            encontrados.add(campo)

    if "usuario" not in encontrados or "password" not in encontrados:
        return None

    return {
        "metodo": (metodo or "POST").upper(),
        "url": url,
        "content_type": content_type or "application/json",
        "es_json": es_json,
        "cuerpo": plantilla_cuerpo,
        "campos": sorted(encontrados),
    }


def _parece_json(cuerpo: str) -> bool:
    t = (cuerpo or "").lstrip()
    return t.startswith("{") or t.startswith("[")


def render_cuerpo(plantilla: dict, reg: dict) -> str:
    """Rellena la plantilla con los datos de ESTE registro.

    Se inserta el valor con el mismo escapado que tenia el original, asi una
    comilla o un acento en un nombre no rompe el JSON. Un marcador cuyo dato no
    esta en el registro se reemplaza por vacio, no se deja crudo: un
    '{{apellido}}' literal viajando al panel es peor que un apellido vacio.
    """
    es_json = plantilla.get("es_json", True)
    out = plantilla.get("cuerpo", "")
    for campo in CAMPOS:
        valor = str(reg.get(campo, "") or "")
        rep = _escapado_json(valor) if es_json else valor
        out = out.replace(_tok(campo), rep)
    # Por las dudas: cualquier marcador que haya quedado (un campo que la
    # plantilla usaba y este registro no trae) se limpia.
    out = re.sub(r"\{\{[a-z_]+\}\}", "", out)
    return out


# Marcas de que el panel RECHAZO por nombre ya existente: eso NO es un error de
# red, es "elegi otro nombre", y la cola lo resuelve renombrando. Se distingue
# del resto para que el que llama sepa que el formulario tampoco va a poder con
# el mismo nombre (evita una vuelta al pedo).
_RX_YA_EXISTE = re.compile(r"already\s*exist|ya\s*existe|username.*tak|exists", re.I)


def evaluar_respuesta(status: int, texto: str) -> tuple[bool | None, str]:
    """Que hacer con la respuesta del POST de creacion.

    Devuelve (resultado, detalle):
      True  -> creado. SOLO con 2xx y sin marca de error en el cuerpo.
      False -> rechazado por nombre ya existente: hay que renombrar (lo hace
               la cola). Volver a mandar el MISMO nombre no sirve.
      None  -> no se sabe: 5xx, timeout, un cuerpo que no se entiende. El que
               llama cae al formulario, que verifica contra el listado.

    La regla de oro es la misma del formulario: ante la duda, NUNCA 'creado'.
    Un falso 'creado' entrega credenciales de una cuenta que no existe.
    """
    t = (texto or "")
    tl = t.lower()

    if 200 <= status < 300:
        # 2xx con un cuerpo que grita error igual (algunos paneles contestan
        # 200 y {"success":false}). Ante esa contradiccion, no arriesgar.
        if '"success":false' in tl.replace(" ", "") or '"error"' in tl or '"errors"' in tl:
            if _RX_YA_EXISTE.search(t):
                return False, "nombre ya existente (2xx con error)"
            return None, f"2xx pero el cuerpo reporta error: {t[:120]}"
        return True, f"HTTP {status}"

    if status in (400, 409, 422) and _RX_YA_EXISTE.search(t):
        return False, "el panel dice que el nombre ya existe"

    # 401/403 = sesion caida o WAF: que el formulario re-loguee y reintente.
    if status in (401, 403):
        return None, f"HTTP {status} (sesion/WAF): al formulario"

    return None, f"HTTP {status}: {t[:120]}"
