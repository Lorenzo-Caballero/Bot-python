"""
t_recaudar.py — la lógica de recaudar por API, sin panel ni red.

Recaudar dejó de scrapear el DOM (16/09/2026): ahora lee el listado y retira
por la API del panel, y ordena/saltea en Python. Se prueba lo que puede sacar
plata mal:

  * el listado se ORDENA de mayor a menor y descarta filas sin id/usuario;
  * el retiro decide por el CUERPO, no por el 200 (igual que el depósito): el
    challenge del WAF se reintenta, un status != 0 NO cuenta como hecho, un
    cuerpo ilegible tampoco;
  * _items_de desenvuelve la respuesta en todas sus formas.

    python t_recaudar.py
"""
import sys

import bot_recaudar as R

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


# Nada de esperas reales en el test (el reintento del challenge duerme).
R.time.sleep = lambda *a, **k: None


class FakeResp:
    def __init__(self, texto, status=200):
        self._t = texto
        self.status = status
        self.ok = 200 <= status < 300

    def text(self):
        return self._t


class FakeReq:
    """Devuelve, en orden, las respuestas que se le cargan; repite la última."""
    def __init__(self, respuestas):
        self._r = list(respuestas)
        self.posts = []

    def _next(self):
        return self._r[len(self.posts) - 1] if len(self.posts) <= len(self._r) \
            else self._r[-1]

    def post(self, url, data=None, timeout=None):
        self.posts.append({"url": url, "data": data})
        idx = min(len(self.posts) - 1, len(self._r) - 1)
        return self._r[idx]

    def get(self, url, timeout=None):
        # traer_jugadores no se testea por acá (se monkeypatchea _get_api).
        raise NotImplementedError


class FakeCtx:
    def __init__(self, respuestas):
        self.request = FakeReq(respuestas)


CHALLENGE = ('<!DOCTYPE html><html><head><meta http-equiv="refresh" '
             'content="0;url=/exhk?..."></head><body>servicepipe</body></html>')

# ---------------------------------------------------------------------------
print("\n=== El retiro decide por el CUERPO, no por el 200 ===")

# status 0 = hecho de verdad
ctx = FakeCtx([FakeResp('{"status":0}')])
okr, det = R.retirar_por_api(ctx, 123, 5000)
chequear("2xx con status 0 -> retirado", okr is True, det)

# status != 0 = la plataforma lo entendió y lo rechazó: NO retirado
ctx = FakeCtx([FakeResp('{"status":501,"error_message":"saldo insuficiente"}')])
okr, det = R.retirar_por_api(ctx, 123, 5000)
chequear("2xx con status != 0 -> NO retirado", okr is False, det)
chequear("  y el detalle dice por qué", "no hizo el retiro" in det, det)

# 200 con HTML (challenge) que PERSISTE -> fallado, y se intentó 3 veces
ctx = FakeCtx([FakeResp(CHALLENGE)])
okr, det = R.retirar_por_api(ctx, 123, 5000)
chequear("challenge persistente -> NO retirado", okr is False, det)
chequear("  y reintentó 3 veces antes de rendirse", len(ctx.request.posts) == 3,
         f"posts={len(ctx.request.posts)}")

# challenge la 1ra, JSON bueno la 2da -> retirado (el reintento salvó)
ctx = FakeCtx([FakeResp(CHALLENGE), FakeResp('{"status":0}')])
okr, det = R.retirar_por_api(ctx, 123, 5000)
chequear("challenge y después status 0 -> retirado al reintento", okr is True, det)

# cuerpo ilegible en un 200 -> NO retirado (no se da plata por hecha)
ctx = FakeCtx([FakeResp("no soy json")])
okr, det = R.retirar_por_api(ctx, 123, 5000)
chequear("2xx ilegible -> NO retirado", okr is False, det)

# no-2xx -> NO retirado
ctx = FakeCtx([FakeResp('{"status":0}', status=500)])
okr, det = R.retirar_por_api(ctx, 123, 5000)
chequear("500 -> NO retirado", okr is False, det)

# EL MONTO NUNCA PUEDE SER MAYOR AL SALDO. Acá decía "el amount viaja entero
# (pesos), como el depósito" y chequeaba que 1234.9 se mandara como 1235. Esa
# suposición --que retirar y depositar son la misma operación porque comparten
# endpoint-- es la que rompió la recaudación: redondear hacia ARRIBA es
# inofensivo al depositar (entrás un centavo de más) y fatal al retirar, porque
# le pedís a la plataforma plata que el jugador no tiene. El 21/09/2026, con el
# log en pantalla: "belu5364: retirando $19" sobre un saldo de $18,60 ->
# {"status":501,"error_message":"Balance is insufficient"}. En una corrida
# fallaron los 20 de 20 por esto.
ctx = FakeCtx([FakeResp('{"status":0}')])
R.retirar_por_api(ctx, 77, 1234.9)
chequear("el monto NUNCA supera el saldo", ctx.request.posts[0]["data"]["amount"] <= 1234.9,
         str(ctx.request.posts[0]["data"]))
chequear("operation = 1 (retiro)", ctx.request.posts[0]["data"]["operation"] == 1)

# ---------------------------------------------------------------------------
print("\n=== El listado se ordena de mayor a menor ===")

PAGINAS = {
    "check": {"result": {"id": 999, "username": "AGENTE"}},
    0: {"result": {"users": [
        {"id": 1, "username": "holajuan188", "balance": 300},
        {"id": 2, "username": "holaana2", "balance": 9000},
        {"id": 3, "username": "holaz", "balance": 0},          # entra igual, saldo 0
    ]}},
    1: {"result": {"users": [
        {"id": 4, "username": "holasvero888", "balance": 1500},
        {"id": 5, "username": "", "balance": 500},              # sin usuario: se descarta
        {"id": None, "username": "holanone", "balance": 700},   # sin id: se descarta
    ]}},
    2: {"result": {"users": []}},   # fin
}


def fake_get_api(ctx, url, intentos=4, espera=2.0):
    if "/user/check" in url:
        return PAGINAS["check"]
    import re
    m = re.search(r"page=(\d+)", url)
    return PAGINAS[int(m.group(1))]


R._get_api = fake_get_api
js = R.traer_jugadores(FakeCtx([]))
saldos = [j["saldo"] for j in js]
chequear("orden de mayor a menor", saldos == sorted(saldos, reverse=True), str(saldos))
chequear("el de mayor saldo primero", js[0]["usuario"] == "holaana2", js[0]["usuario"])
chequear("descarta filas sin id o sin usuario", len(js) == 4, str([j['usuario'] for j in js]))
chequear("trae id, usuario y saldo", all({"id", "usuario", "saldo"} <= set(j) for j in js))

# ---------------------------------------------------------------------------
print("\n=== _items_de desenvuelve todas las formas ===")
chequear("lista pelada", R._items_de([{"a": 1}]) == [{"a": 1}])
chequear("envuelto en result", R._items_de({"result": [{"a": 1}]}) == [{"a": 1}])
chequear("result -> users", R._items_de({"result": {"users": [{"a": 1}]}}) == [{"a": 1}])
chequear("items", R._items_de({"items": [{"a": 1}]}) == [{"a": 1}])
chequear("nada reconocible -> None", R._items_de({"raro": 1}) is None)

print("\n" + "-" * 39)
print(f"{ok} OK, {fail} fallas")
# ---------------------------------------------------------------------------
print("")
print("=== El monto del retiro NUNCA puede ser mayor al saldo ===")
for saldo in [18.60, 18.55, 18.50, 18.40, 17.55, 17.50, 16.90, 75.60, 66.0,
              0.01, 1234.567, 3.999]:
    pedido = R.monto_retirable(saldo)
    chequear("$%s: pide $%s (nunca de mas)" % (saldo, pedido), pedido <= saldo,
             "pediria %s sobre un saldo de %s" % (pedido, saldo))

# Y NO SE COME CENTAVOS, que es el error de la version obvia:
# floor(18.40*100)/100 da 18.39 en binario, y eso es plata que queda en una
# cuenta que se estaba vaciando, sin que nada lo diga.
for x in [18.40, 16.90, 75.60, 0.10]:
    chequear("no pierde el centavo por punto flotante (%s)" % x,
             R.monto_retirable(x) == x, str(R.monto_retirable(x)))

chequear("un saldo en 0 pide 0", R.monto_retirable(0) == 0.0)
chequear("un saldo negativo pide 0 (nunca un retiro al reves)",
         R.monto_retirable(-5) == 0.0)
chequear("None pide 0", R.monto_retirable(None) == 0.0)
chequear("los milicentavos se truncan hacia ABAJO",
         R.monto_retirable(9.999) == 9.99, str(R.monto_retirable(9.999)))

# Si la plataforma rechaza el monto con centavos, se reintenta con el entero
# de abajo. Es el UNICO reintento de una escritura del proyecto, y se permite
# porque el rechazo es EXPLICITO: status != 0 con cuerpo legible prueba que no
# movio un peso. Una respuesta AMBIGUA no se reintenta nunca.
ctx = FakeCtx([FakeResp('{"status":501,"error_message":"Balance is insufficient"}'),
               FakeResp('{"status":0}')])
okr, det = R.retirar_por_api(ctx, 55, 18.60)
chequear("rechazo con centavos -> reintenta con el entero", okr is True, det)
chequear("   y ese intento fue por el entero de ABAJO",
         ctx.request.posts[1]["data"]["amount"] == 18,
         str(ctx.request.posts[1]["data"]))

ctx = FakeCtx([FakeResp('{"status":501,"error_message":"Balance is insufficient"}'),
               FakeResp('{"status":0}')])
okr, det = R.retirar_por_api(ctx, 55, 20.0)
chequear("un entero rechazado NO se reintenta", okr is False, det)
chequear("   (un solo POST)", len(ctx.request.posts) == 1, str(len(ctx.request.posts)))

# ---------------------------------------------------------------------------
print("")
print("=== 'Saltar' se cuenta en JUGADORES, no en paginas del panel ===")
# "Pagina" significaba una cosa en el panel --donde el operador elige 10, 25 o
# 50 por pagina-- y otra aca (50 fijo, el tamaño de la API). Mirando la pagina
# 4 del panel se veian saldos de $75 y el bot, con el mismo "4", saltaba 200 y
# tocaba los de $17: los dos numeros correctos, hablando de cosas distintas.
_src = open(R.__file__, encoding="utf-8").read()
chequear("el bot acepta saltar_jug", "saltar_jug" in _src)
chequear("y lo prefiere sobre las paginas",
         'saltar_n = getattr(args, "saltar_jug", None)' in _src)
chequear("con respaldo a saltar*50 para un server viejo",
         "saltar_n = max(0, args.saltar) * SALTO_POR_PAGINA" in _src)

print("")
print("-" * 39)
print("%d OK, %d fallas" % (ok, fail))
sys.exit(0 if fail == 0 else 1)
