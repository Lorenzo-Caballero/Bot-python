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

print("\n" + "-" * 39)
print(f"{ok} OK, {fail} fallas")
raise SystemExit(1 if fail else 0)
