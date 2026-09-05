"""
t_alta_api.py — la logica del fast-path de altas, sin red ni navegador.

Se prueba lo que puede romper una cuenta o entregar credenciales falsas:
aprender la plantilla del POST real, rellenarla para otro jugador sin romper
el escapado, y decidir cuando una respuesta es 'creado' de verdad.

    python t_alta_api.py
"""
import json
import sys

import alta_api as A

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


# ---------------------------------------------------------------------------
print("\n=== Aprender la plantilla de un POST real ===")

# Un cuerpo JSON como el que manda el panel: claves cualesquiera, valores los
# que tipeamos. La plantilla no sabe de antemano como se llaman las claves.
valores = {"usuario": "martin23", "password": "aB3xClave",
           "email": "martin23@gmail.com", "nombre": "Martin", "apellido": "Gomez"}
cuerpo_real = json.dumps({
    "username": "martin23", "password": "aB3xClave", "confirmPassword": "aB3xClave",
    "email": "martin23@gmail.com", "firstName": "Martin", "lastName": "Gomez",
}, ensure_ascii=False)

pl = A.construir_plantilla(
    "https://agents.ganamosonline.com/api/agent_admin/user/",
    "POST", "application/json", cuerpo_real, valores)

chequear("aprende una plantilla", pl is not None)
chequear("guarda la url tal cual", pl and pl["url"].endswith("/agent_admin/user/"))
chequear("el usuario quedo como marcador", pl and "{{usuario}}" in pl["cuerpo"])
chequear("la password quedo como marcador (las DOS apariciones)",
         pl and pl["cuerpo"].count("{{password}}") == 2,
         pl["cuerpo"] if pl else "")
chequear("ya no queda el valor real en claro",
         pl and "martin23" not in pl["cuerpo"] and "aB3xClave" not in pl["cuerpo"])

# Sin usuario o sin password no se puede aprender: seria crear cuentas con
# datos de otro. Mejor None y seguir con el formulario.
chequear("sin password reconocible NO aprende",
         A.construir_plantilla("u", "POST", "application/json",
                               json.dumps({"username": "x"}), valores) is None)

# ---------------------------------------------------------------------------
print("\n=== Rellenar la plantilla para OTRO jugador ===")

reg = {"usuario": "sofia_9", "password": "Zk8mNoPq", "email": "sofia_9@gmail.com",
       "nombre": "Sofia", "apellido": "Ruiz"}
render = A.render_cuerpo(pl, reg)
d = json.loads(render)   # tiene que seguir siendo JSON valido

chequear("el render es JSON valido", isinstance(d, dict))
chequear("puso el usuario nuevo", d.get("username") == "sofia_9")
chequear("puso la password nueva en los dos lugares",
         d.get("password") == "Zk8mNoPq" and d.get("confirmPassword") == "Zk8mNoPq")
chequear("no quedo nada del jugador anterior", "martin" not in render.lower())
chequear("no quedan marcadores sin reemplazar", "{{" not in render)

# El escapado: un apellido con comilla no puede romper el JSON.
reg2 = dict(reg, apellido='O"Brien', nombre="José")
render2 = A.render_cuerpo(pl, reg2)
d2 = json.loads(render2)   # si el escapado fallara, esto explota
chequear("una comilla en un dato no rompe el JSON", d2.get("lastName") == 'O"Brien')
chequear("un acento sobrevive", d2.get("firstName") == "José")

# ---------------------------------------------------------------------------
print("\n=== Decidir si la respuesta es 'creado' ===")

r, _ = A.evaluar_respuesta(200, '{"result":{"id":123,"username":"sofia_9"}}')
chequear("2xx limpio = creado", r is True)

r, _ = A.evaluar_respuesta(201, "")
chequear("201 sin cuerpo = creado", r is True)

r, _ = A.evaluar_respuesta(400, '{"error":"User with username - already exist"}')
chequear("400 'already exist' = renombrar (False)", r is False)

r, _ = A.evaluar_respuesta(409, "username already taken")
chequear("409 'already taken' = renombrar (False)", r is False)

# 200 mentiroso: algunos paneles contestan 200 con success:false.
r, _ = A.evaluar_respuesta(200, '{"success":false,"error":"algo raro"}')
chequear("2xx que reporta error NO es creado", r is None)

r, _ = A.evaluar_respuesta(200, '{"success":false,"error":"already exist"}')
chequear("2xx+error de nombre existente = renombrar (False)", r is False)

# Lo dudoso NUNCA es creado: cae al formulario.
for st in (500, 502, 503, 401, 403):
    r, _ = A.evaluar_respuesta(st, "lo que sea")
    chequear(f"HTTP {st} = None (al formulario)", r is None)

r, _ = A.evaluar_respuesta(400, '{"error":"password too short"}')
chequear("400 por OTRA cosa (no nombre) = None, no False",
         r is None, "un 400 que no es 'ya existe' no se renombra a ciegas")

print(f"\n---------------------------------------\n{ok} OK, {fail} fallas")
sys.exit(1 if fail else 0)
