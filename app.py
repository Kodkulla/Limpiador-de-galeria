"""Limpiador de Galería: organiza fotos por Proyecto / Categoría con Streamlit.

Estructura de salida:
    <destino_base>/<Proyecto>/<Categoría>/...
    <destino_base>/<Proyecto>/_Descartadas/...
    <destino_base>/<Proyecto>/proyecto_estado.json   (progreso)
"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import date, datetime, time
from io import BytesIO
from pathlib import Path

import folium
import streamlit as st
from PIL import Image, ImageOps
from streamlit_folium import st_folium

# --------------------------------------------------------------------------
# Constantes
# --------------------------------------------------------------------------
STATE_FILE = "proyecto_estado.json"
DISCARD_DIR = "_Descartadas"
DEFAULT_BASE = str(Path.home() / "Fotos_Organizadas")
DEFAULT_CATEGORIES = ["Playa", "Familia", "Salidas_Comida"]
CONFIG_PATH = Path.home() / ".config" / "limpiador-galeria" / "config.json"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".bmp", ".gif"}
DISPLAY_MAX = (1800, 820)  # tamaño máximo de la imagen en el visor

# Soporte opcional para fotos HEIC/HEIF de móviles (pip install pillow-heif)
try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
    IMAGE_EXTS |= {".heic", ".heif"}
except ImportError:
    pass

# Tags EXIF
TAG_EXIF_IFD = 0x8769
TAG_GPS_IFD = 0x8825
TAG_DATETIME_ORIGINAL = 36867
TAG_DATETIME = 306
TAG_MAKE = 271
TAG_MODEL = 272

st.set_page_config(page_title="Limpiador de Galería", page_icon="📸", layout="wide")


# --------------------------------------------------------------------------
# Utilidades de configuración global y estado del proyecto
# --------------------------------------------------------------------------
def load_global_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_global_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def project_dir(proj: dict) -> Path:
    return Path(proj["base_dest"]) / proj["name"]


def save_project(proj: dict) -> None:
    pdir = project_dir(proj)
    pdir.mkdir(parents=True, exist_ok=True)
    proj["updated"] = datetime.now().isoformat(timespec="seconds")
    atomic_write_json(pdir / STATE_FILE, proj)


def load_project(pdir: Path) -> dict:
    proj = json.loads((pdir / STATE_FILE).read_text(encoding="utf-8"))
    # La carpeta manda: si el usuario movió el proyecto, se actualizan las rutas
    proj["name"] = pdir.name
    proj["base_dest"] = str(pdir.parent)
    proj.setdefault("decisions", {})
    proj.setdefault("categories", [])
    proj.setdefault("mode", "copiar")
    proj.setdefault("recursive", True)
    proj.setdefault("include_undated", True)
    return proj


def list_projects(base: str) -> list[str]:
    b = Path(base).expanduser()
    if not b.is_dir():
        return []
    return sorted(p.name for p in b.iterdir() if (p / STATE_FILE).is_file())


def safe_name(name: str) -> str:
    """Convierte un texto en un nombre de carpeta válido."""
    name = name.strip().replace(" ", "_")
    name = re.sub(r'[\\/:*?"<>|\x00]', "", name)
    return name.strip(".")


def ensure_category_dirs(proj: dict) -> None:
    pdir = project_dir(proj)
    for cat in proj["categories"] + [DISCARD_DIR]:
        (pdir / cat).mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------
# EXIF
# --------------------------------------------------------------------------
def _parse_exif_date(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, bytes):
        value = value.decode(errors="ignore")
    value = str(value).strip().replace("\x00", "")
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M", "%Y:%m:%d"):
        try:
            return datetime.strptime(value[: len(datetime.now().strftime(fmt))], fmt)
        except ValueError:
            continue
    return None


@st.cache_data(show_spinner=False, max_entries=20000)
def read_capture_date(path: str, mtime: float) -> str | None:
    """Fecha DateTimeOriginal (ISO) o None. `mtime` invalida la caché."""
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            dt = _parse_exif_date(exif.get_ifd(TAG_EXIF_IFD).get(TAG_DATETIME_ORIGINAL))
            if dt is None:
                dt = _parse_exif_date(exif.get(TAG_DATETIME))
    except Exception:
        return None
    return dt.isoformat() if dt else None


def _to_float(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError, ZeroDivisionError):
        return float("nan")


def _dms_to_deg(dms, ref) -> float | None:
    try:
        d, m, s = (_to_float(v) for v in dms)
    except (TypeError, ValueError):
        return None
    deg = d + m / 60 + s / 3600
    if deg != deg:  # NaN
        return None
    if isinstance(ref, bytes):
        ref = ref.decode(errors="ignore")
    if str(ref).strip().upper() in ("S", "W"):
        deg = -deg
    return deg


@st.cache_data(show_spinner=False, max_entries=500)
def read_metadata(path: str, mtime: float) -> dict:
    meta = {"date": None, "width": None, "height": None, "camera": None, "gps": None}
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            w, h = img.size
            orientation = exif.get(0x0112, 1)
            if orientation in (5, 6, 7, 8):
                w, h = h, w
            meta["width"], meta["height"] = w, h
            ifd = exif.get_ifd(TAG_EXIF_IFD)
            dt = _parse_exif_date(ifd.get(TAG_DATETIME_ORIGINAL)) or _parse_exif_date(
                exif.get(TAG_DATETIME)
            )
            meta["date"] = dt.isoformat() if dt else None
            make = str(exif.get(TAG_MAKE, "") or "").strip("\x00 ")
            model = str(exif.get(TAG_MODEL, "") or "").strip("\x00 ")
            if model and make and model.lower().startswith(make.lower()):
                make = ""
            meta["camera"] = " ".join(x for x in (make, model) if x) or None
            gps = exif.get_ifd(TAG_GPS_IFD)
            if gps and 2 in gps and 4 in gps:
                lat = _dms_to_deg(gps[2], gps.get(1, "N"))
                lon = _dms_to_deg(gps[4], gps.get(3, "E"))
                if lat is not None and lon is not None and not (lat == 0 and lon == 0):
                    meta["gps"] = (lat, lon)
    except Exception:
        pass
    return meta


@st.cache_data(show_spinner=False, max_entries=30)
def display_bytes(path: str, mtime: float) -> bytes | None:
    """Imagen orientada y reducida para el visor (JPEG en memoria)."""
    try:
        with Image.open(path) as img:
            img = ImageOps.exif_transpose(img)
            img.thumbnail(DISPLAY_MAX)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=88)
            return buf.getvalue()
    except Exception:
        return None


# --------------------------------------------------------------------------
# Escaneo de la carpeta de origen y construcción de la cola de revisión
# --------------------------------------------------------------------------
def scan_source(proj: dict) -> list[dict]:
    src = Path(proj["source"]).expanduser()
    exclude = Path(proj["base_dest"]).expanduser().resolve()
    walker = src.rglob("*") if proj.get("recursive", True) else src.glob("*")
    files = []
    for p in walker:
        if p.suffix.lower() not in IMAGE_EXTS or not p.is_file():
            continue
        rp = p.resolve()
        if rp == exclude or exclude in rp.parents:  # no re-escanear lo ya organizado
            continue
        files.append(rp)

    d_from = date.fromisoformat(proj["date_from"]) if proj.get("date_from") else None
    d_to = date.fromisoformat(proj["date_to"]) if proj.get("date_to") else None
    out = []
    progress = st.progress(0.0, text="Leyendo fechas EXIF…") if len(files) > 50 else None
    for i, p in enumerate(files):
        dt_iso = read_capture_date(str(p), p.stat().st_mtime)
        dt = datetime.fromisoformat(dt_iso) if dt_iso else None
        if dt is None:
            if (d_from or d_to) and not proj.get("include_undated", True):
                continue
        else:
            if d_from and dt.date() < d_from:
                continue
            if d_to and dt.date() > d_to:
                continue
        out.append({"key": str(p), "date": dt_iso})
        if progress and i % 25 == 0:
            progress.progress(i / len(files), text=f"Leyendo fechas EXIF… {i}/{len(files)}")
    if progress:
        progress.empty()
    return out


def build_items(scan: list[dict], decisions: dict) -> list[dict]:
    """Une fotos pendientes (en origen) y fotos ya clasificadas (en destino)."""
    items = {}
    for s in scan:
        if s["key"] not in decisions:
            items[s["key"]] = {"key": s["key"], "path": s["key"], "date": s["date"]}
    for key, d in decisions.items():
        items[key] = {"key": key, "path": d["dest"], "date": d.get("date"), "decision": d}
    return sorted(
        items.values(),
        key=lambda it: (it["date"] is None, it["date"] or "", Path(it["key"]).name.lower()),
    )


# --------------------------------------------------------------------------
# Operaciones de archivo
# --------------------------------------------------------------------------
def unique_path(target: Path) -> Path:
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    n = 1
    while True:
        cand = target.with_name(f"{stem}_{n}{suffix}")
        if not cand.exists():
            return cand
        n += 1


def classify(proj: dict, item: dict, category: str) -> None:
    folder = project_dir(proj) / category
    folder.mkdir(parents=True, exist_ok=True)
    prev = item.get("decision")
    current = Path(item["path"])

    if prev:  # reclasificación: se mueve el archivo que ya está en el proyecto
        if prev["category"] == category:
            return
        if not current.exists():
            raise FileNotFoundError(f"No se encuentra {current}")
        dest = unique_path(folder / current.name)
        shutil.move(str(current), str(dest))
    else:
        dest = unique_path(folder / current.name)
        if proj["mode"] == "mover":
            shutil.move(str(current), str(dest))
        else:
            shutil.copy2(str(current), str(dest))

    proj["decisions"][item["key"]] = {
        "category": category,
        "dest": str(dest),
        "date": item.get("date"),
        "mode": prev["mode"] if prev else proj["mode"],
        "at": datetime.now().isoformat(timespec="seconds"),
    }
    save_project(proj)


def unclassify(proj: dict, item: dict) -> None:
    """Deshace la decisión: devuelve el archivo al origen o borra la copia."""
    d = item["decision"]
    dest = Path(d["dest"])
    pdir = project_dir(proj).resolve()
    if dest.exists():
        if d.get("mode") == "mover":
            original = Path(item["key"])
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dest), str(unique_path(original)))
        elif pdir in dest.resolve().parents:  # solo borra copias propias del proyecto
            dest.unlink()
    del proj["decisions"][item["key"]]
    save_project(proj)


# --------------------------------------------------------------------------
# Estado de sesión
# --------------------------------------------------------------------------
ss = st.session_state
ss.setdefault("project", None)
ss.setdefault("scan", None)
ss.setdefault("idx", 0)
ss.setdefault("flash", None)
gcfg = load_global_config()


def open_project(proj: dict) -> None:
    ss.project = proj
    ss.scan = None
    ss.idx = 0
    ss.jump_to_pending = True
    ensure_category_dirs(proj)
    gcfg["last_base"] = proj["base_dest"]
    gcfg["last_project"] = proj["name"]
    save_global_config(gcfg)


def close_project() -> None:
    ss.project = None
    ss.scan = None
    ss.idx = 0


def next_pending(items: list[dict], start: int) -> int | None:
    n = len(items)
    for off in range(n):
        j = (start + off) % n
        if "decision" not in items[j]:
            return j
    return None


# --------------------------------------------------------------------------
# Pantalla 1: crear / abrir proyecto
# --------------------------------------------------------------------------
def date_filter_inputs(prefix: str, proj: dict | None = None) -> tuple[str | None, str | None, bool]:
    proj = proj or {}
    use = st.checkbox(
        "Filtrar por rango de fechas (EXIF DateTimeOriginal)",
        value=bool(proj.get("date_from") or proj.get("date_to")),
        key=f"{prefix}_usedate",
    )
    if not use:
        return None, None, proj.get("include_undated", True)
    c1, c2 = st.columns(2)
    d_from = c1.date_input(
        "Desde",
        value=date.fromisoformat(proj["date_from"]) if proj.get("date_from") else date(date.today().year, 1, 1),
        key=f"{prefix}_from",
        format="DD/MM/YYYY",
    )
    d_to = c2.date_input(
        "Hasta",
        value=date.fromisoformat(proj["date_to"]) if proj.get("date_to") else date.today(),
        key=f"{prefix}_to",
        format="DD/MM/YYYY",
    )
    undated = st.checkbox(
        "Incluir también fotos sin fecha EXIF",
        value=proj.get("include_undated", False),
        key=f"{prefix}_undated",
    )
    return d_from.isoformat(), d_to.isoformat(), undated


def setup_screen() -> None:
    st.title("📸 Limpiador de Galería")
    st.caption("Organiza tus fotos en Proyectos y Subcategorías sin borrar nada del disco.")

    base = st.text_input(
        "📁 Carpeta de destino base",
        value=gcfg.get("last_base", DEFAULT_BASE),
        help="Aquí se creará una carpeta por cada proyecto.",
    )
    base = str(Path(base).expanduser())
    existing = list_projects(base)

    tab_new, tab_open = st.tabs(["➕ Nuevo proyecto", f"📂 Continuar proyecto ({len(existing)})"])

    with tab_open:
        if not existing:
            st.info("No hay proyectos en esta carpeta de destino todavía.")
        else:
            last = gcfg.get("last_project")
            sel = st.selectbox(
                "Proyecto", existing, index=existing.index(last) if last in existing else 0
            )
            try:
                preview = load_project(Path(base) / sel)
                st.caption(
                    f"Origen: `{preview.get('source')}` · Categorías: "
                    f"{', '.join(preview['categories'])} · Clasificadas: {len(preview['decisions'])}"
                )
            except (OSError, ValueError) as e:
                preview = None
                st.error(f"No se pudo leer el estado del proyecto: {e}")
            if st.button("Abrir proyecto", type="primary", disabled=preview is None):
                open_project(preview)
                st.rerun()

    with tab_new:
        name = st.text_input("Nombre del proyecto", placeholder="Vacaciones_2025")
        source = st.text_input(
            "Carpeta de origen (fotos sin clasificar)",
            value=gcfg.get("last_source", str(Path.home() / "Imágenes")),
        )
        recursive = st.checkbox("Incluir subcarpetas del origen", value=True)
        cats_txt = st.text_area(
            "Categorías iniciales (una por línea o separadas por comas)",
            value="\n".join(DEFAULT_CATEGORIES),
            height=120,
        )
        mode = st.radio(
            "¿Qué hacer con cada foto clasificada?",
            ["copiar", "mover"],
            format_func=lambda m: "Copiar (el origen queda intacto)" if m == "copiar" else "Mover (vacía el origen)",
            horizontal=True,
        )
        d_from, d_to, undated = date_filter_inputs("new")

        if st.button("Crear proyecto", type="primary"):
            pname = safe_name(name)
            src = Path(source).expanduser()
            cats = []
            for c in re.split(r"[,\n]", cats_txt):
                c = safe_name(c)
                if c and c != DISCARD_DIR and c not in cats:
                    cats.append(c)
            errors = []
            if not pname:
                errors.append("Escribe un nombre de proyecto.")
            elif pname in existing:
                errors.append("Ya existe un proyecto con ese nombre; ábrelo en la pestaña «Continuar».")
            if not src.is_dir():
                errors.append(f"La carpeta de origen no existe: `{src}`")
            if not cats:
                errors.append("Define al menos una categoría.")
            if d_from and d_to and d_from > d_to:
                errors.append("La fecha inicial es posterior a la final.")
            for e in errors:
                st.error(e)
            if not errors:
                proj = {
                    "name": pname,
                    "source": str(src),
                    "base_dest": base,
                    "categories": cats,
                    "mode": mode,
                    "recursive": recursive,
                    "date_from": d_from,
                    "date_to": d_to,
                    "include_undated": undated,
                    "created": datetime.now().isoformat(timespec="seconds"),
                    "decisions": {},
                }
                gcfg["last_source"] = str(src)
                save_project(proj)
                open_project(proj)
                st.rerun()


# --------------------------------------------------------------------------
# Pantalla 2: revisión
# --------------------------------------------------------------------------
def sidebar_project(proj: dict) -> None:
    sb = st.sidebar
    sb.markdown(f"### 📁 {proj['name']}")
    home = str(Path.home())
    short = lambda p: str(p).replace(home, "~", 1)  # noqa: E731
    sb.caption(f"Destino: `{short(project_dir(proj))}`\n\nOrigen: `{short(proj['source'])}`")

    with sb.form("add_cat", clear_on_submit=True, border=False):
        c1, c2 = st.columns([3, 1], vertical_alignment="bottom")
        new_cat = c1.text_input("Nueva categoría", placeholder="Ej. Paisajes")
        if c2.form_submit_button("➕") and new_cat.strip():
            c = safe_name(new_cat)
            if not c or c == DISCARD_DIR:
                ss.flash = ("error", "Nombre de categoría no válido.")
            elif c in proj["categories"]:
                ss.flash = ("warning", f"La categoría «{c}» ya existe.")
            else:
                proj["categories"].append(c)
                ensure_category_dirs(proj)
                save_project(proj)
                ss.flash = ("success", f"Categoría «{c}» añadida.")
            st.rerun()

    with sb.expander("⚙️ Ajustes del proyecto"):
        mode = st.radio(
            "Acción al clasificar", ["copiar", "mover"],
            index=0 if proj["mode"] == "copiar" else 1, horizontal=True,
        )
        d_from, d_to, undated = date_filter_inputs("edit", proj)
        recursive = st.checkbox("Incluir subcarpetas", value=proj.get("recursive", True))
        if st.button("Guardar ajustes y re-escanear", width="stretch"):
            if d_from and d_to and d_from > d_to:
                st.error("La fecha inicial es posterior a la final.")
            else:
                proj.update(mode=mode, date_from=d_from, date_to=d_to,
                            include_undated=undated, recursive=recursive)
                save_project(proj)
                ss.scan = None
                st.rerun()

    c1, c2 = sb.columns(2)
    if c1.button("🔄 Re-escanear", width="stretch", help="Vuelve a leer la carpeta de origen"):
        ss.scan = None
        st.rerun()
    if c2.button("🚪 Cerrar", width="stretch"):
        close_project()
        st.rerun()
    sb.divider()


def sidebar_metadata(item: dict) -> None:
    sb = st.sidebar
    path = Path(item["path"])
    sb.markdown("### 🧾 Metadatos")
    if not path.exists():
        sb.warning("Archivo no encontrado.")
        return
    stat = path.stat()
    meta = read_metadata(str(path), stat.st_mtime)
    if meta["date"]:
        dt = datetime.fromisoformat(meta["date"])
        sb.markdown(f"**📅 Captura:** {dt.strftime('%d/%m/%Y %H:%M:%S')}")
    else:
        sb.markdown("**📅 Captura:** _sin fecha EXIF_")
    sb.markdown(f"**📄 Archivo original:** `{Path(item['key']).name}`")
    if meta["width"]:
        mp = meta["width"] * meta["height"] / 1e6
        sb.markdown(f"**🖼️ Resolución:** {meta['width']} × {meta['height']} px ({mp:.1f} MP)")
    sb.markdown(f"**💾 Tamaño:** {stat.st_size / (1024 * 1024):.2f} MB")
    if meta["camera"]:
        sb.markdown(f"**📷 Cámara:** {meta['camera']}")

    sb.markdown("### 🗺️ Ubicación")
    if meta["gps"]:
        lat, lon = meta["gps"]
        m = folium.Map(location=[lat, lon], zoom_start=14, tiles="OpenStreetMap")
        folium.Marker([lat, lon], tooltip=f"{lat:.6f}, {lon:.6f}").add_to(m)
        with sb:
            st_folium(m, height=260, use_container_width=True, returned_objects=[], key=f"map_{abs(hash(item['key'])) % 10**9}")
        sb.caption(
            f"[{lat:.6f}, {lon:.6f}](https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=16/{lat}/{lon})"
        )
    else:
        sb.info("📍 Sin datos de geolocalización")


def stats_header(proj: dict, items: list[dict]) -> None:
    total = len(items)
    reviewed = sum(1 for it in items if "decision" in it)
    st.progress(reviewed / total if total else 0.0, text=f"**Revisadas: {reviewed} / {total}**")
    counts = {c: 0 for c in proj["categories"] + [DISCARD_DIR]}
    for d in proj["decisions"].values():
        counts[d["category"]] = counts.get(d["category"], 0) + 1
    cols = st.columns(len(counts) + 1)
    cols[0].metric("⏳ Pendientes", total - reviewed)
    for col, (cat, n) in zip(cols[1:], counts.items()):
        col.metric(("🗑️ Descartadas" if cat == DISCARD_DIR else f"📁 {cat}"), n)


def review_screen() -> None:
    proj = ss.project
    sidebar_project(proj)

    if ss.flash:
        kind, msg = ss.flash
        getattr(st, kind)(msg)
        ss.flash = None

    if ss.scan is None:
        if not Path(proj["source"]).is_dir():
            st.warning(f"La carpeta de origen no existe: `{proj['source']}`. Solo se muestran las ya clasificadas.")
            ss.scan = []
        else:
            with st.spinner("Escaneando carpeta de origen…"):
                ss.scan = scan_source(proj)

    items = build_items(ss.scan, proj["decisions"])
    stats_header(proj, items)

    if not items:
        st.info("No se encontraron fotos en la carpeta de origen con los filtros actuales.")
        return

    if ss.pop("jump_to_pending", False):
        np_ = next_pending(items, 0)
        ss.idx = np_ if np_ is not None else 0
    ss.idx = max(0, min(ss.idx, len(items) - 1))
    item = items[ss.idx]
    decision = item.get("decision")

    sidebar_metadata(item)

    # ---- Navegación ----
    nav = st.columns([1, 1, 3, 1, 1], vertical_alignment="center")
    if nav[0].button("⏮ Primera", width="stretch", shortcut="Home"):
        ss.idx = 0
        st.rerun()
    if nav[1].button("⬅️ Anterior", width="stretch", shortcut="Left"):
        ss.idx = (ss.idx - 1) % len(items)
        st.rerun()
    status = (
        ("🗑️ Descartada" if decision["category"] == DISCARD_DIR else f"✅ En «{decision['category']}»")
        if decision else "⏳ Pendiente"
    )
    nav[2].markdown(
        f"<div style='text-align:center'><b>Foto {ss.idx + 1} / {len(items)}</b> · "
        f"<code>{Path(item['key']).name}</code> · {status}</div>",
        unsafe_allow_html=True,
    )
    if nav[3].button("Siguiente ➡️", width="stretch", shortcut="Right"):
        ss.idx = (ss.idx + 1) % len(items)
        st.rerun()
    if nav[4].button("⏭ Pendiente", width="stretch", shortcut="N", help="Ir a la siguiente foto sin clasificar"):
        np_ = next_pending(items, ss.idx + 1)
        if np_ is None:
            ss.flash = ("success", "🎉 No quedan fotos pendientes.")
        else:
            ss.idx = np_
        st.rerun()

    # ---- Visor ----
    path = Path(item["path"])
    data = display_bytes(str(path), path.stat().st_mtime) if path.exists() else None
    if data:
        _, mid, _ = st.columns([1, 10, 1])
        with mid:
            st.markdown(
                "<style>[data-testid='stImage'] img{display:block;margin:auto;max-height:72vh;"
                "width:auto!important;max-width:100%;object-fit:contain;border-radius:6px}</style>",
                unsafe_allow_html=True,
            )
            st.image(data, width="stretch")
    elif path.exists():
        st.error("No se pudo abrir esta imagen (formato no soportado o archivo dañado).")
    else:
        st.error(f"El archivo ya no existe: `{path}`")

    # ---- Acciones ----
    def act(fn, *args, msg: str):
        try:
            fn(proj, item, *args)
        except Exception as e:  # noqa: BLE001 - se muestra al usuario
            ss.flash = ("error", f"Error: {e}")
            st.rerun()
        ss.flash = ("toast", msg)
        items_after = build_items(ss.scan, proj["decisions"])
        np_ = next_pending(items_after, ss.idx + 1)
        if np_ is not None:
            ss.idx = np_
        else:
            ss.flash = ("success", "🎉 ¡Todas las fotos están revisadas!")
        st.rerun()

    cats = proj["categories"]
    default_cat = decision["category"] if decision and decision["category"] in cats else ss.get("last_cat", cats[0])
    a1, a2, a3, a4 = st.columns([3, 2, 2, 1], vertical_alignment="bottom")
    chosen = a1.selectbox(
        "Categoría", cats, index=cats.index(default_cat) if default_cat in cats else 0,
        key=f"cat_{ss.idx}",
    )
    if a2.button("✅ Aceptar y Clasificar", type="primary", width="stretch", shortcut="Enter"):
        ss.last_cat = chosen
        act(classify, chosen, msg=f"Clasificada en «{chosen}»")
    if a3.button("🗑️ Descartar", width="stretch", shortcut="X"):
        act(classify, DISCARD_DIR, msg="Descartada")
    if a4.button("↩️", width="stretch", shortcut="Z", disabled=decision is None,
                 help="Deshacer: vuelve a dejar la foto como pendiente"):
        try:
            unclassify(proj, item)
            ss.flash = ("toast", "Foto marcada de nuevo como pendiente")
        except Exception as e:  # noqa: BLE001
            ss.flash = ("error", f"Error: {e}")
        st.rerun()

    st.caption("Clasificación rápida (teclas 1-9):")
    quick = st.columns(min(len(cats), 9) or 1)
    for i, cat in enumerate(cats[:9]):
        if quick[i].button(f"{cat}", key=f"quick_{cat}", width="stretch", shortcut=str(i + 1)):
            ss.last_cat = cat
            act(classify, cat, msg=f"Clasificada en «{cat}»")

    st.caption(
        "⌨️ **Atajos:** ← / → navegar · **Enter** aceptar con la categoría elegida · "
        "**1-9** clasificar directo · **X** descartar · **Z** deshacer · **N** siguiente pendiente · "
        "**Inicio** primera foto"
    )


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
if ss.flash and ss.flash[0] == "toast":
    st.toast(ss.flash[1], icon="✅")
    ss.flash = None

if ss.project is None:
    setup_screen()
else:
    review_screen()
