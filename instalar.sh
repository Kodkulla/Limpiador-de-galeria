#!/usr/bin/env bash
# Instala todo lo necesario con un solo comando y abre la app:
#   paquetes del sistema (dnf en Fedora, apt en Debian/Ubuntu) + entorno Python.
#
# Uso:
#   bash instalar.sh               # instala y abre la app
#   bash instalar.sh --rpmfusion   # Fedora: además, ffmpeg completo (H.264/H.265)
#   bash instalar.sh --sin-abrir   # solo instala
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

RPMFUSION=0
ABRIR=1
for arg in "$@"; do
    case "$arg" in
        --rpmfusion) RPMFUSION=1 ;;
        --sin-abrir) ABRIR=0 ;;
        -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "❌ Opción desconocida: $arg (usa --help)"; exit 1 ;;
    esac
done

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    SUDO="sudo"
    echo "🔑 Se pedirá tu contraseña para instalar paquetes del sistema."
fi

if command -v dnf >/dev/null 2>&1; then
    echo "📦 Fedora detectado: instalando paquetes con dnf…"
    PKGS=(python3 perl-Image-ExifTool jpegoptim optipng libjpeg-turbo-utils zenity)
    # Si ya hay un ffmpeg (p. ej. el completo de RPM Fusion) no se instala ffmpeg-free encima
    command -v ffmpeg >/dev/null 2>&1 || PKGS+=(ffmpeg-free)
    $SUDO dnf install -y "${PKGS[@]}"
    # Opcional: si no está en los repositorios, la app usa optipng
    $SUDO dnf install -y oxipng >/dev/null 2>&1 \
        || echo "ℹ️  oxipng no está disponible; se usará optipng para los PNG."

    if [ "$RPMFUSION" -eq 1 ]; then
        echo "📦 Instalando el ffmpeg completo de RPM Fusion (H.264/H.265)…"
        if ! rpm -q rpmfusion-free-release >/dev/null 2>&1; then
            $SUDO dnf install -y \
                "https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm"
        fi
        if rpm -q ffmpeg-free >/dev/null 2>&1; then
            $SUDO dnf swap -y ffmpeg-free ffmpeg --allowerasing
        else
            $SUDO dnf install -y ffmpeg
        fi
    fi
elif command -v apt-get >/dev/null 2>&1; then
    echo "📦 Debian/Ubuntu detectado: instalando paquetes con apt…"
    $SUDO apt-get update
    $SUDO apt-get install -y python3 python3-venv libimage-exiftool-perl \
        ffmpeg jpegoptim optipng libjpeg-turbo-progs zenity
    $SUDO apt-get install -y oxipng >/dev/null 2>&1 \
        || echo "ℹ️  oxipng no está disponible; se usará optipng para los PNG."
    if [ "$RPMFUSION" -eq 1 ]; then
        echo "ℹ️  --rpmfusion solo aplica a Fedora (en Debian/Ubuntu ffmpeg ya trae H.264/H.265)."
    fi
else
    echo "❌ No se reconoce el gestor de paquetes (se esperaba dnf o apt)."
    echo "   Instala a mano: python3, exiftool, ffmpeg, jpegoptim, optipng."
    exit 1
fi

chmod +x run.sh
echo "✅ Paquetes del sistema listos."

if [ "$ABRIR" -eq 1 ]; then
    # run.sh crea el entorno virtual, instala las librerías de Python y abre el navegador
    exec ./run.sh
else
    echo "🐍 Preparando el entorno de Python…"
    python3 -m venv .venv
    .venv/bin/python -m pip install --upgrade pip >/dev/null
    .venv/bin/python -m pip install -r requirements.txt
    sha256sum requirements.txt | cut -d' ' -f1 > .venv/.requirements.sha256
    echo "✅ Todo instalado. Abre la app con: ./run.sh"
fi
