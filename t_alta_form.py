"""t_alta_form.py — Llenar el formulario no puede colgarse 15 s por campo.

EL INCIDENTE (Nahuel, 01/10/2026): *"la creacion de usuario estaba lenta"*.
Un alta quedo 141 minutos con 8 intentos, y el mensaje que dejaba era:

    Excepcion: Locator.click: Timeout 15000ms exceeded

LO QUE PASABA. `tipear()` hacia `loc.click()` antes de escribir, sin timeout
propio, o sea con el default de 15 s. `click()` le exige a Playwright que el
elemento sea ACCIONABLE: visible, quieto, habilitado y -- la que muerde -- que
no lo tape nadie. Cualquier cosa del panel encima (un cartel, un spinner, un
modal cerrandose, el overlay del WAF) dejaba al bot esperando el timeout
ENTERO. Por campo. Un alta con cinco campos se iba a mas de un minuto antes de
abortar, y despues el backoff la mandaba a esperar.

Y el error no ayudaba: "Locator.click: Timeout" manda a revisar el selector,
que estaba bien. El elemento se encontraba; no se podia TOCAR.

EL ARREGLO. Para tipear alcanza con ENFOCAR, y `focus()` no exige
accionabilidad. Se sigue intentando el click primero --hay paneles que abren
el desplegable recien al click-- pero con 3 s: si en 3 s no se puede, algo lo
esta tapando y enfocar resuelve igual.

    python t_alta_form.py
"""
import pathlib
import re
import sys

ok = 0
fail = 0


def chequear(q: str, c: bool, d: str = "") -> None:
    global ok, fail
    if c:
        ok += 1
        print(f"  OK    {q}")
    else:
        fail += 1
        print(f"  FALLA {q}   {d}")


src = (pathlib.Path(__file__).parent / "bot_crear_jugador.py").read_text(encoding="utf-8")

# Solo el cuerpo de tipear(), que es donde vive el problema.
m = re.search(r"def tipear\(page, selector, valor: str\) -> None:(.*?)\ndef ", src, re.S)
if not m:
    print("  FALLA no encontre tipear() en bot_crear_jugador.py")
    sys.exit(1)
cuerpo = m.group(1)

print("=== 1. Ningun campo puede costar 15 segundos ===")
# El click pelado hereda el default de 15 s (page.set_default_timeout(15_000)).
chequear(
    "tipear() ya no hace un click sin timeout propio",
    not re.search(r"^\s*loc\.click\(\)\s*$", cuerpo, re.M),
    "loc.click() hereda los 15 s del default y los paga por CADA campo",
)
chequear(
    "el click lleva un timeout corto",
    "loc.click(timeout=3_000)" in cuerpo,
    "3 s alcanza para una pagina sana; mas que eso es que algo lo tapa",
)

print("\n=== 2. Si algo lo tapa, se enfoca y se sigue ===")
chequear(
    "hay fallback a focus()",
    "loc.focus()" in cuerpo,
    "focus() no exige que el elemento sea accionable: para tipear alcanza",
)
chequear(
    "y atrapa las dos excepciones de Playwright",
    re.search(r"except \(PWTimeout, PWError\)", cuerpo) is not None,
    "un elemento tapado puede tirar Error, no solo TimeoutError",
)
chequear(
    "el fallback esta DESPUES del click, no en vez de el",
    cuerpo.index("loc.click(timeout=3_000)") < cuerpo.index("loc.focus()"),
    "hay paneles que abren el desplegable recien al click: el intento se conserva",
)

print("\n=== 3. Lo que hace que React registre sigue intacto ===")
# El valor no se setea por JS: se tipea tecla por tecla. Si eso se cambiara por
# un fill() a secas, React no registraria y el panel mandaria campos vacios.
chequear(
    "se sigue escribiendo tecla por tecla",
    "press_sequentially" in cuerpo,
    "un fill() a secas no dispara los eventos que React escucha",
)
chequear(
    "y se dispara el blur al final",
    'loc.press("Tab")' in cuerpo,
    "sin el blur no corre la validacion del formulario",
)
chequear(
    "se sigue limpiando el campo antes de escribir",
    'loc.fill("")' in cuerpo,
)

print("\n" + "-" * 39)
print(f"{ok} OK, {fail} fallas")
sys.exit(1 if fail else 0)
