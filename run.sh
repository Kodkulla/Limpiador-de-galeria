#!/usr/bin/env bash
# Lanza el Limpiador de Galería: prepara el entorno virtual, instala
# dependencias si hace falta y abre la app en el navegador por defecto.
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

VENV_DIR=".venv"
PORT="${PORT:-8501}"
URL="http://localhost:${PORT}"
REQ_FILE="requirements.txt"
REQ_STAMP="${VENV_DIR}/.requirements.sha256"

# 1. Python 3
if ! command -v python3 >/dev/null 2>&1; then
    echo "❌ No se encontró python3. Instálalo, p. ej.: sudo apt install python3 python3-venv"
    exit 1
fi

# 2. Entorno virtual
if [ ! -x "${VENV_DIR}/bin/python" ]; then
    echo "🔧 Creando entorno virtual en ${VENV_DIR}…"
    if ! python3 -m venv "${VENV_DIR}"; then
        rm -rf "${VENV_DIR}"
        echo "❌ No se pudo crear el entorno virtual."
        echo "   En Debian/Ubuntu instala: sudo apt install python3-venv"
        exit 1
    fi
fi
PY="${VENV_DIR}/bin/python"

# 3. Dependencias (solo si requirements.txt cambió o faltan paquetes)
CURRENT_HASH="$(sha256sum "${REQ_FILE}" | cut -d' ' -f1)"
if [ ! -f "${REQ_STAMP}" ] || [ "$(cat "${REQ_STAMP}")" != "${CURRENT_HASH}" ] \
   || ! "${PY}" -c "import streamlit, PIL, folium, streamlit_folium, imagehash" >/dev/null 2>&1; then
    echo "📦 Instalando dependencias…"
    "${PY}" -m pip install --upgrade pip >/dev/null
    "${PY}" -m pip install -r "${REQ_FILE}"
    echo "${CURRENT_HASH}" > "${REQ_STAMP}"
fi

# 4. exiftool (opcional, para fecha y GPS de vídeos)
if ! command -v exiftool >/dev/null 2>&1; then
    echo "ℹ️  exiftool no está instalado: los vídeos se verán, pero sin fecha ni GPS."
    echo "   Instálalo con: sudo apt install libimage-exiftool-perl"
fi

# 5. Abrir el navegador cuando el servidor responda
open_browser() {
    for _ in $(seq 1 60); do
        if "${PY}" -c "import urllib.request; urllib.request.urlopen('${URL}/_stcore/health', timeout=1)" >/dev/null 2>&1; then
            if command -v xdg-open >/dev/null 2>&1; then
                xdg-open "${URL}" >/dev/null 2>&1 || true
            else
                "${PY}" -m webbrowser "${URL}" >/dev/null 2>&1 || true
            fi
            return
        fi
        sleep 0.5
    done
}
open_browser &

echo "🚀 Iniciando Limpiador de Galería en ${URL}  (Ctrl+C para salir)"
exec "${PY}" -m streamlit run app.py \
    --server.port "${PORT}" \
    --server.headless true \
    --browser.gatherUsageStats false
