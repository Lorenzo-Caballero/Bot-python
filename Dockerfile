# Bot de alta de jugadores en agents.ganamosonline.com (Playwright + Chromium).
#
# Se usa python:slim + `playwright install --with-deps` en vez de la imagen
# oficial de Playwright a proposito: asi la version del navegador SIEMPRE
# coincide con la del paquete que quedo instalado por requirements.txt, sin
# tener que acertarle a un tag tipo v1.49.1-jammy cada vez que se sube la lib.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    TZ=America/Argentina/Buenos_Aires

WORKDIR /app

# requirements primero: mientras no cambie, el layer pesado (Chromium + libs
# del sistema, ~500MB) sale de cache y el rebuild por tocar el .py es de segundos.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
 && playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*

COPY bot_crear_jugador.py bot_cargar_fichas.py sync_usuarios.py alta_api.py ./

# El codigo escribe con rutas RELATIVAS al directorio actual: estado_sesion.json,
# estado_session_storage.json, bot.log y capturas/. Por eso el WORKDIR de
# ejecucion es /datos (el volumen) y NO /app: asi todo lo que se escribe cae en
# el volumen y el volumen no tapa el codigo. Los scripts se invocan por ruta
# absoluta, y sync_usuarios.py encuentra igual el import de bot_crear_jugador
# porque mete su propia carpeta en sys.path.
RUN mkdir -p /datos
WORKDIR /datos
VOLUME ["/datos"]

# --headless es obligatorio en el contenedor: no hay servidor grafico.
CMD ["python", "/app/bot_crear_jugador.py", "--headless"]
