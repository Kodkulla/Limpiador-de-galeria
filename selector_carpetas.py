"""Selección de carpetas sin escribir rutas a mano.

1. Botón «Elegir carpeta…»: abre la ventana nativa del escritorio (zenity en GNOME,
   kdialog en KDE, yad o tkinter como respaldo). La app corre en tu propio equipo,
   así que la ventana aparece en tu escritorio.
2. Explorador integrado: navegar por carpetas con botones (siempre funciona).
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import streamlit as st

HOME = Path.home()


# --------------------------------------------------------------------------
# Lugares rápidos
# --------------------------------------------------------------------------
def _xdg_user_dirs() -> dict[str, Path]:
    """Lee ~/.config/user-dirs.dirs (Escritorio, Imágenes… según el idioma)."""
    out = {}
    try:
        text = (HOME / ".config" / "user-dirs.dirs").read_text(encoding="utf-8")
    except OSError:
        return out
    for m in re.finditer(r'^XDG_(\w+)_DIR="(.*)"', text, re.M):
        out[m.group(1)] = Path(m.group(2).replace("$HOME", str(HOME)))
    return out


def quick_places() -> list[tuple[str, Path]]:
    xdg = _xdg_user_dirs()
    places = [("🏠 Carpeta personal", HOME)]
    for key, icon, fallback in (("DESKTOP", "🖥️", "Escritorio"), ("PICTURES", "🖼️", "Imágenes"),
                                ("VIDEOS", "🎬", "Vídeos"), ("DOWNLOAD", "⬇️", "Descargas")):
        p = xdg.get(key, HOME / fallback)
        if p.is_dir() and p != HOME:
            places.append((f"{icon} {p.name}", p))
    user = os.environ.get("USER") or HOME.name
    for base in (Path("/run/media") / user, Path("/media") / user, Path("/mnt")):
        try:
            for d in sorted(base.iterdir()):
                if d.is_dir():
                    places.append((f"💽 {d.name}", d))
        except OSError:
            pass
    return places


# --------------------------------------------------------------------------
# Diálogo nativo
# --------------------------------------------------------------------------
def _has_display() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


_TK_SCRIPT = (
    "import sys, tkinter, tkinter.filedialog as fd\n"
    "r = tkinter.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
    "p = fd.askdirectory(initialdir=sys.argv[1], title=sys.argv[2], mustexist=False)\n"
    "print(p or '')\n"
)


def _dialog_commands(title: str, start: str) -> list[list[str]]:
    cmds = []
    kde = "KDE" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
    zenity = shutil.which("zenity")
    kdialog = shutil.which("kdialog")
    yad = shutil.which("yad")
    z = [zenity, "--file-selection", "--directory", f"--title={title}", f"--filename={start}/"] if zenity else None
    k = [kdialog, "--title", title, "--getexistingdirectory", start] if kdialog else None
    for c in ([k, z] if kde else [z, k]):
        if c:
            cmds.append(c)
    if yad:
        cmds.append([yad, "--file", "--directory", f"--title={title}", f"--filename={start}/"])
    cmds.append([sys.executable, "-c", _TK_SCRIPT, start, title])
    return cmds


def native_pick(title: str, start: str) -> tuple[str | None, str | None]:
    """Devuelve (ruta, error). ruta=None y error=None significa «cancelado»."""
    if not _has_display():
        return None, "No se detectó el escritorio gráfico para abrir la ventana; usa «Explorar aquí»."
    last_err = None
    for cmd in _dialog_commands(title, start):
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=900, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            last_err = str(e)
            continue
        path = res.stdout.strip().splitlines()[-1] if res.stdout.strip() else ""
        if res.returncode == 0 and path:
            return path, None
        err = res.stderr.strip()
        if res.returncode in (0, 1) and "display" not in err.lower():
            return None, None  # el usuario canceló (GTK puede imprimir avisos aunque se cancele)
        last_err = err.splitlines()[-1] if err else f"código {res.returncode}"
    return None, f"No se pudo abrir la ventana del sistema ({last_err}). Usa «Explorar aquí»."


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------
@st.cache_data(show_spinner=False, ttl=120, max_entries=200)
def count_media(path: str, exts: tuple[str, ...], recursive: bool) -> int:
    """Cuenta fotos/vídeos (con tope para no bloquear la interfaz en discos enormes)."""
    n, limit = 0, 200_000
    try:
        if recursive:
            for _, dirs, files in os.walk(path):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                n += sum(1 for f in files if os.path.splitext(f)[1].lower() in exts)
                if n > limit:
                    break
        else:
            n = sum(1 for e in os.scandir(path) if e.is_file() and os.path.splitext(e.name)[1].lower() in exts)
    except OSError:
        return 0
    return n


def _subdirs(path: Path) -> list[Path]:
    try:
        return sorted((e for e in path.iterdir() if e.is_dir() and not e.name.startswith(".")),
                      key=lambda p: p.name.lower())
    except OSError:
        return []


def _k(*parts: str) -> str:
    return hashlib.md5("|".join(parts).encode()).hexdigest()[:10]


# --------------------------------------------------------------------------
# Widget
# --------------------------------------------------------------------------
def folder_picker(label: str, key: str, default: str, *, help: str | None = None,
                  media_exts: tuple[str, ...] = (), allow_create: bool = False,
                  count_recursive: bool = True) -> str:
    """Muestra la carpeta elegida y botones para cambiarla. Devuelve la ruta absoluta."""
    ss = st.session_state
    if key not in ss:
        ss[key] = str(Path(default).expanduser())
    current = Path(ss[key])

    with st.container(border=True):
        st.markdown(f"**{label}**")
        if help:
            st.caption(help)
        c1, c2, c3 = st.columns([6, 2, 2], vertical_alignment="center")
        c1.code(str(current), language=None, wrap_lines=True)
        if c2.button("📂 Elegir carpeta…", key=f"{key}__native", type="primary", width="stretch",
                     help="Abre la ventana de tu escritorio para elegir la carpeta"):
            start = current if current.is_dir() else next((p for p in current.parents if p.is_dir()), HOME)
            with st.spinner("Elige la carpeta en la ventana que se abrió (puede quedar detrás del navegador)…"):
                path, err = native_pick(label.strip("*📁🗂️ "), str(start))
            if path:
                ss[key] = str(Path(path).resolve())
                ss[f"{key}__open"] = False
            elif err:
                ss[f"{key}__err"] = err
                ss[f"{key}__open"] = True
            st.rerun()
        is_open = ss.get(f"{key}__open", False)
        if c3.button("✖ Cerrar explorador" if is_open else "🗂️ Explorar aquí", key=f"{key}__toggle",
                     width="stretch", help="Navegar por las carpetas sin salir de la app"):
            ss[f"{key}__open"] = not is_open
            ss[f"{key}__cur"] = str(current if current.is_dir() else HOME)
            st.rerun()

        if err := ss.pop(f"{key}__err", None):
            st.warning(err)
        if not current.is_dir():
            st.caption("ℹ️ Esta carpeta todavía no existe: se creará automáticamente." if allow_create
                       else "⚠️ Esta carpeta no existe.")
        elif media_exts:
            n = count_media(str(current), media_exts, count_recursive)
            st.caption(f"📷 {n:,} fotos/vídeos encontrados".replace(",", ".")
                       + (" (incluidas subcarpetas)" if count_recursive else ""))

        if ss.get(f"{key}__open"):
            _browser(key, media_exts, allow_create)
    return ss[key]


def _browser(key: str, media_exts: tuple[str, ...], allow_create: bool) -> None:
    ss = st.session_state
    cur = Path(ss.get(f"{key}__cur") or HOME)
    if not cur.is_dir():
        cur = HOME

    def go(p: Path) -> None:
        ss[f"{key}__cur"] = str(p)
        st.rerun()

    st.markdown("---")
    places = quick_places()
    cols = st.columns(min(len(places), 6))
    for i, (name, p) in enumerate(places):
        if cols[i % len(cols)].button(name, key=f"{key}__pl_{_k(str(p))}", width="stretch"):
            go(p)

    b1, b2 = st.columns([1, 5], vertical_alignment="center")
    if b1.button("⬆️ Subir", key=f"{key}__up", width="stretch", disabled=cur == cur.parent):
        go(cur.parent)
    here = count_media(str(cur), media_exts, False) if media_exts else 0
    b2.markdown(f"📁 **{cur}**" + (f"  ·  📷 {here} aquí" if media_exts else ""))

    subdirs = _subdirs(cur)
    if len(subdirs) > 24:
        flt = st.text_input("Filtrar carpetas", key=f"{key}__flt_{_k(str(cur))}",
                            placeholder="Escribe parte del nombre…")
        if flt:
            subdirs = [d for d in subdirs if flt.lower() in d.name.lower()]
    if not subdirs:
        st.caption("(No hay subcarpetas)")
    shown = subdirs[:90]
    grid = st.columns(3)
    for i, d in enumerate(shown):
        if grid[i % 3].button(f"📁 {d.name}", key=f"{key}__d_{_k(str(d))}", width="stretch"):
            go(d)
    if len(subdirs) > len(shown):
        st.caption(f"… y {len(subdirs) - len(shown)} carpetas más (usa el filtro).")

    u1, u2 = st.columns([2, 3], vertical_alignment="bottom")
    if u1.button(f"✅ Usar «{cur.name or cur}»", key=f"{key}__use", type="primary", width="stretch"):
        ss[key] = str(cur.resolve())
        ss[f"{key}__open"] = False
        st.rerun()
    if allow_create:
        with u2.form(f"{key}__mkform", clear_on_submit=True, border=False):
            n1, n2 = st.columns([3, 1], vertical_alignment="bottom")
            new = n1.text_input("Nueva carpeta aquí", placeholder="Fotos_Organizadas")
            if n2.form_submit_button("➕ Crear") and new.strip():
                name = re.sub(r'[\\/:*?"<>|\x00]', "", new.strip())
                if name:
                    target = cur / name
                    try:
                        target.mkdir(parents=True, exist_ok=True)
                        go(target)
                    except OSError as e:
                        st.error(f"No se pudo crear la carpeta: {e}")
