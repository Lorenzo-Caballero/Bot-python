"""
t_en_vuelo.py — Las altas reclamadas no pueden quedar huerfanas 15 minutos.

EL PROBLEMA QUE CUIDA. El watchdog mata el proceso con os._exit cuando el loop
se cuelga (page.evaluate de Playwright no tiene timeout). Lo que este proceso
tenia RECLAMADO queda en 'procesando', y la cola recien lo rescata a los 15
minutos -- MINUTOS_ZOMBIE en api/altas_cola.php.

Para el jugador eso no se ve como un error: se ve como que la cuenta "tarda".
No hay nada rojo en ningun lado, el bot dice "escuchando", la cola dice "1 en
proceso" y listo. Paso el 7/10/2026: el alta entro 19:49:36, el watchdog la
dejo huerfana a los 92s y recien volvia a tomarse 20:06.

LO QUE NO SE PUEDE HACER para arreglarlo es el liberar GLOBAL: con dos
instancias vivas (un deploy solapado) le devuelve a la cola lo que la otra
esta creando en ese momento, y el alta sale dos veces. Por eso hay que saber
cuales son las propias, que es lo que este archivo prueba.

    python t_en_vuelo.py
"""
import bot_crear_jugador as B

ok = 0
fail = 0


def chequear(q, cond, det=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  OK    {q}")
    else:
        fail += 1
        print(f"  FALLA {q}   {det}")


class SesionFalsa:
    """Responde como la cola, sin red. Guarda lo que se le mando."""

    def __init__(self, datos=None):
        self.datos = datos or []
        self.liberados = None      # el cuerpo del ultimo POST a ?accion=liberar
        self.timeout_liberar = None

    class _R:
        def __init__(self, j):
            self._j = j

        def raise_for_status(self):
            pass

        def json(self):
            return self._j

    def get(self, url, params=None, timeout=None):
        return self._R({"ok": True, "datos": self.datos})

    def post(self, url, params=None, json=None, timeout=None, **kw):
        if (params or {}).get("accion") == "liberar":
            self.liberados = json
            self.timeout_liberar = timeout
            return self._R({"ok": True, "liberados": len((json or {}).get("ids", []))})
        return self._R({"ok": True})


def api_con(datos=None):
    api = B.ApiJugadores.__new__(B.ApiJugadores)   # sin __init__: no queremos env ni red
    api.s = SesionFalsa(datos)
    api.url = "http://cola.test/altas_cola.php"
    return api


def limpiar():
    with B._EN_VUELO_LOCK:
        B.EN_VUELO.clear()


# ---------------------------------------------------------------------------
print("\n=== 1. Reclamar anota; marcar borra ===")
limpiar()
api = api_con([{"id": 101, "usuario": "holaAna1"}, {"id": 102, "usuario": "holaBeto2"}])
datos = api.pendientes(10)
chequear("devuelve las dos altas", len(datos) == 2)
chequear("y las dos quedan anotadas como propias", B.EN_VUELO == {101, 102}, str(B.EN_VUELO))

api.marcar(101, "ok", usuario="holaAna1")
chequear("la resuelta sale del registro", B.EN_VUELO == {102}, str(B.EN_VUELO))
api.marcar(102, "error", "no se pudo")
chequear("un error tambien la resuelve (no se devuelve dos veces)",
         B.EN_VUELO == set(), str(B.EN_VUELO))

# ---------------------------------------------------------------------------
print("\n=== 2. Lo que devuelve el watchdog son SOLO las propias ===")
"""El liberar global le saca a la otra instancia lo que esta creando, y esa
   alta sale dos veces. El cuerpo tiene que llevar ids SIEMPRE."""
limpiar()
api = api_con([{"id": 201}, {"id": 202}])
api.pendientes(10)
api.liberar(sorted(B.EN_VUELO), timeout=8)
chequear("manda ids explicitos, nunca el liberar global",
         api.s.liberados == {"ids": [201, 202]}, str(api.s.liberados))
chequear("y las saca del registro", B.EN_VUELO == set())
chequear("con timeout corto: de ahi se sale muriendo igual",
         api.s.timeout_liberar == 8, str(api.s.timeout_liberar))

# ---------------------------------------------------------------------------
print("\n=== 3. Una marcada NO se devuelve ademas por el watchdog ===")
"""Es la diferencia entre 'tarda' y 'se creo dos veces'. Si marcar() dejara el
   id adentro, el watchdog la pondria 'pendiente' con intentos=0 sobre una que
   el panel ya creo."""
limpiar()
api = api_con([{"id": 301}, {"id": 302}])
api.pendientes(10)
api.marcar(301, "ok", usuario="holaCeci3")
with B._EN_VUELO_LOCK:
    mios = sorted(B.EN_VUELO)
chequear("solo queda la que sigue sin resolver", mios == [302], str(mios))

# ---------------------------------------------------------------------------
print("\n=== 4. El watchdog las devuelve ANTES de morir ===")
"""Chequeo estructural: el os._exit del watchdog tiene que venir despues del
   liberar. Al reves no sirve de nada -- el proceso ya murio."""
src = open(B.__file__.replace(".pyc", ".py"), encoding="utf-8").read()
wd = src[src.index("def _watchdog()"):]
wd = wd[:wd.index("os._exit(1)") + len("os._exit(1)")]
chequear("el watchdog libera antes de os._exit", "api.liberar(" in wd)
chequear("y pasa SUS ids, no el liberar global", "api.liberar(mios" in wd)
chequear("si no puede, lo dice y muere igual (no se cuelga)",
         "except Exception" in wd and "Vuelven solas en 15 min" in wd)

# ---------------------------------------------------------------------------
print("\n=== 5. El watchdog dice DONDE se colgo ===")
"""El 7/10/2026 un cuelgue de 92s se reporto como "page.evaluate colgado?" --el
   texto fijo del mensaje-- cuando el fast-path habia dejado de usar
   page.evaluate un mes antes (6/9/2026, migrado a context.request). El unico
   dato que daba el watchdog señalaba a un culpable imposible, y ubicar el
   cuelgue de verdad costo reconstruirlo restando segundos entre dos lineas.

   La fase dice DONDE estaba el loop, que es un hecho. La causa no la sabe
   nadie en ese momento, y por eso el mensaje ya no la nombra."""
B._latir("probando")
chequear("el latido recuerda la fase", B.WD_FASE[0] == "probando", B.WD_FASE[0])
B._latir()
chequear("un latido sin fase no la borra (sigue siendo el ultimo lugar conocido)",
         B.WD_FASE[0] == "probando", B.WD_FASE[0])

chequear("el watchdog imprime la fase", "WD_FASE[0]" in wd)
chequear("y ya no acusa a page.evaluate, que no se usa desde el 6/9/2026",
         "page.evaluate colgado" not in wd,
         "una pista falsa cuesta una investigacion entera")

"""Las fases tienen que cubrir los caminos donde el alta puede colgarse: si
   una queda sin etiquetar, el watchdog reporta la anterior y vuelve a
   mandar a mirar al lugar equivocado."""
for fase in ["sondeando la cola de altas", "fast-path: POST al panel",
             "fast-path: armando el alta", "triage del fast-path", "formulario, alta"]:
    chequear(f"hay fase para '{fase}'", fase in src)

# ---------------------------------------------------------------------------
print("\n=== 6. El challenge del WAF se despeja en el NAVEGADOR ===")
"""Medido el 7/10/2026: de las 21:23 en adelante, el WAF desafio el 100% de
   las requests y los CINCO reintentos del fast-path fallaron, uno atras del
   otro, en cada alta. Ninguno paso nunca.

   La razon es que context.request NO EJECUTA JAVASCRIPT: lleva la cookie de
   clearance que ya tiene, pero no puede conseguir una nueva -- y el challenge
   de ServicePipe es exactamente una pagina con JS que hay que correr para
   obtenerla. Dormir y reintentar manda la MISMA request sin cookie y recibe
   el MISMO challenge. Mientras el WAF desafiaba de a ratos no se notaba; con
   el 100%, las altas dejaron de salir (445 y 446 en 3-6s, de la 447 en
   adelante ninguna)."""

CHALLENGE = ('<!DOCTYPE html><html><head><meta http-equiv="Content-Type">'
             '<noscript><meta http-equiv="refresh" content="0; url=/exhk123">')
SANO = "<!DOCTYPE html><html><body>Crear jugador</body></html>"


class PaginaFalsa:
    """Un Chromium de mentira: cuenta los goto y decide que ve despues."""

    def __init__(self, vistas, falla_goto=False):
        self.vistas = list(vistas)     # que devuelve content() en cada llamada
        self.gotos = 0
        self.esperas = []
        self.falla_goto = falla_goto

    def goto(self, url, **kw):
        self.gotos += 1
        if self.falla_goto:
            raise RuntimeError("Timeout 30000ms exceeded")

    def wait_for_timeout(self, ms):
        self.esperas.append(ms)

    def content(self):
        return self.vistas.pop(0) if self.vistas else SANO


pag = PaginaFalsa([SANO])
chequear("si el navegador ya no ve el challenge, queda despejado",
         B.despejar_waf(pag) is True)
chequear("y recarga el panel una sola vez", pag.gotos == 1, str(pag.gotos))

"""Le tiene que dar tiempo REAL de correr el JS: si contesta al instante,
   estaria diciendo que la cookie se renovo sin haber esperado a que pase."""
chequear("espera antes de declararlo despejado", pag.esperas == [2500], str(pag.esperas))

pag = PaginaFalsa([CHALLENGE, CHALLENGE])
chequear("si el challenge sigue en pantalla, NO finge que se renovo",
         B.despejar_waf(pag) is False)
chequear("y lo reintenta con esperas crecientes antes de rendirse",
         pag.esperas == [2500, 5000], str(pag.esperas))

chequear("un goto que falla no tumba el alta (best-effort)",
         B.despejar_waf(PaginaFalsa([], falla_goto=True)) is False)

print("\n=== 7. Cuando se despeja y cuando no ===")
loop = src[src.index("_WAF_INTENTOS = 5"):]
loop = loop[:loop.index("except Exception as e:")]

"""EL PRIMER REINTENTO ES GRATIS. Con el WAF desafiando de a ratos la request
   siguiente pasa sola; pagar un goto de hasta 30s ahi seria cambiar un
   problema de a ratos por una demora en todas."""
chequear("no se despeja en el primer reintento",
         "if _intento_waf >= 1 and despejar_waf(page):" in loop,
         "despejar siempre costaria 30s en challenges que se van solos")

"""Despues de despejar, reintentar YA: la cookie esta fresca y dormir encima
   solo suma demora a un alta que el jugador esta esperando."""
chequear("tras despejar reintenta sin dormir", "continue" in loop)
chequear("y si no se pudo despejar, sigue la espera creciente de siempre",
         "time.sleep(1.5 * (_intento_waf + 1))" in loop)

"""LA GUARDA QUE HACE SEGURO REINTENTAR UNA ESCRITURA. Un alta es una
   escritura: repetirla a ciegas crearia dos jugadores. Solo se reintenta
   cuando es_challenge() dice que si, y un challenge PRUEBA que la request no
   llego al backend."""
chequear("solo se reintenta si es un challenge, nunca a ciegas",
         "if not alta_api.es_challenge(txt) or _intento_waf == _WAF_INTENTOS - 1:" in loop
         and loop.index("es_challenge(txt)") < loop.index("despejar_waf(page)"),
         "sin esa guarda, reintentar un alta crea dos jugadores")

print("\n" + "-" * 39)
print(f"{ok} OK, {fail} fallas")
raise SystemExit(1 if fail else 0)
