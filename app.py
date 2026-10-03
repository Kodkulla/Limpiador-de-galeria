"""Limpiador de Galería: organiza fotos y vídeos por Proyecto / Categoría con Streamlit.

Estructura de salida:
    <destino_base>/<Proyecto>/<Categoría>/...
    <destino_base>/<Proyecto>/_Descartadas/...
    <destino_base>/<Proyecto>/proyecto_estado.json   (progreso)
"""

from __future__ import annotations

import base64
from collections import Counter
import hashlib
import json
import os
import re
import shutil
import subprocess
import unicodedata
from datetime import date, datetime, time
from io import BytesIO
from pathlib import Path

import folium
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from PIL import Image, ImageOps
from streamlit_folium import st_folium

import compresion as comp
import similares as sim
from selector_carpetas import folder_picker, quick_places

# --------------------------------------------------------------------------
# Constantes
# --------------------------------------------------------------------------
STATE_FILE = "proyecto_estado.json"
DISCARD_DIR = "_Descartadas"
DEFAULT_BASE = str(Path.home() / "Fotos_Organizadas")
DEFAULT_CATEGORIES = ["Playa", "Familia", "Salidas_Comida"]
CONFIG_PATH = Path.home() / ".config" / "limpiador-galeria" / "config.json"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".bmp", ".gif"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".3gp", ".webm"}
DISPLAY_MAX = (1800, 820)  # tamaño máximo de la imagen en el visor
EXIFTOOL = shutil.which("exiftool")

# Soporte opcional para fotos HEIC/HEIF de móviles (pip install pillow-heif)
try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
    IMAGE_EXTS |= {".heic", ".heif"}
except ImportError:
    pass
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS

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


@st.cache_data(show_spinner=False, max_entries=5000)
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
# Vídeos: metadatos con exiftool (los vídeos guardan fecha/GPS en átomos
# QuickTime/MP4, no en EXIF)
# --------------------------------------------------------------------------
EXIFTOOL_TAGS = [
    "-DateTimeOriginal", "-CreationDate", "-CreateDate", "-MediaCreateDate",
    "-GPSLatitude", "-GPSLongitude", "-GPSCoordinates",
    "-ImageWidth", "-ImageHeight", "-Rotation", "-Duration",
    "-Make", "-Model", "-AndroidModel", "-AndroidMake",
]


def is_video(path: str | Path) -> bool:
    return Path(path).suffix.lower() in VIDEO_EXTS


def _run_exiftool(paths: list[Path]) -> dict[str, dict]:
    """Ejecuta exiftool una sola vez para varios archivos y devuelve {ruta: tags}."""
    if not EXIFTOOL or not paths:
        return {}
    cmd = [
        EXIFTOOL, "-json", "-n", "-api", "QuickTimeUTC=1", "-charset", "filename=utf8",
        *EXIFTOOL_TAGS, "--", *map(str, paths),
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=60 + 2 * len(paths), check=False)
        data = json.loads(res.stdout.decode("utf-8", errors="replace") or "[]")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {}
    return {d.get("SourceFile"): d for d in data if isinstance(d, dict)}


def _parse_video_meta(raw: dict) -> dict:
    meta = {"date": None, "width": None, "height": None, "camera": None, "gps": None, "duration": None}
    # CreationDate (iPhone) ya incluye zona horaria; CreateDate se convierte desde UTC
    for tag in ("DateTimeOriginal", "CreationDate", "CreateDate", "MediaCreateDate"):
        dt = _parse_exif_date(raw.get(tag))
        if dt and dt.year > 1970:
            meta["date"] = dt.isoformat()
            break
    lat, lon = _to_float(raw.get("GPSLatitude")), _to_float(raw.get("GPSLongitude"))
    if (lat != lat or lon != lon) and raw.get("GPSCoordinates"):
        parts = re.split(r"[\s,]+", str(raw["GPSCoordinates"]).strip())
        if len(parts) >= 2:
            lat, lon = _to_float(parts[0]), _to_float(parts[1])
    if lat == lat and lon == lon and not (lat == 0 and lon == 0) and abs(lat) <= 90 and abs(lon) <= 180:
        meta["gps"] = (lat, lon)
    w, h = raw.get("ImageWidth"), raw.get("ImageHeight")
    if isinstance(w, (int, float)) and isinstance(h, (int, float)) and w and h:
        rot = _to_float(raw.get("Rotation"))
        if rot == rot and int(rot) % 360 in (90, 270):  # rot == rot descarta NaN (sin etiqueta)
            w, h = h, w
        meta["width"], meta["height"] = int(w), int(h)
    dur = _to_float(raw.get("Duration"))
    meta["duration"] = dur if dur == dur else None
    make = str(raw.get("Make") or raw.get("AndroidMake") or "").strip()
    model = str(raw.get("Model") or raw.get("AndroidModel") or "").strip()
    if model and make and model.lower().startswith(make.lower()):
        make = ""
    meta["camera"] = " ".join(x for x in (make, model) if x) or None
    return meta


@st.cache_resource
def _video_meta_cache() -> dict:
    return {}


def video_metadata_batch(paths: list[Path], progress=None) -> dict[str, dict]:
    """Metadatos de varios vídeos, en lotes de 100 para no lanzar un proceso por archivo."""
    cache = _video_meta_cache()
    out, missing = {}, []
    for p in paths:
        k = (str(p), p.stat().st_mtime)
        if k in cache:
            out[str(p)] = cache[k]
        else:
            missing.append(p)
    for i in range(0, len(missing), 100):
        chunk = missing[i : i + 100]
        if progress:
            progress.progress(i / len(missing), text=f"Leyendo metadatos de vídeos… {i}/{len(missing)}")
        raw = _run_exiftool(chunk)
        for p in chunk:
            meta = _parse_video_meta(raw.get(str(p), {}))
            if EXIFTOOL:  # sin exiftool no se cachea, por si se instala después
                cache[(str(p), p.stat().st_mtime)] = meta
            out[str(p)] = meta
    return out


def read_video_metadata(path: str) -> dict:
    return video_metadata_batch([Path(path)])[str(path)]


def open_with_system(path: str) -> None:
    opener = shutil.which("xdg-open")
    if opener:
        subprocess.Popen([opener, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --------------------------------------------------------------------------
# Escaneo de la carpeta de origen y construcción de la cola de revisión
# --------------------------------------------------------------------------
def scan_source(proj: dict) -> list[dict]:
    src = Path(proj["source"]).expanduser()
    exclude = Path(proj["base_dest"]).expanduser().resolve()
    walker = src.rglob("*") if proj.get("recursive", True) else src.glob("*")
    files = []
    for p in walker:
        if p.suffix.lower() not in MEDIA_EXTS or not p.is_file():
            continue
        rp = p.resolve()
        if rp == exclude or exclude in rp.parents:  # no re-escanear lo ya organizado
            continue
        files.append(rp)

    d_from = date.fromisoformat(proj["date_from"]) if proj.get("date_from") else None
    d_to = date.fromisoformat(proj["date_to"]) if proj.get("date_to") else None
    out = []
    progress = st.progress(0.0, text="Leyendo fechas EXIF…") if len(files) > 50 else None
    videos = video_metadata_batch([p for p in files if is_video(p)], progress)
    for i, p in enumerate(files):
        if is_video(p):
            dt_iso = videos[str(p)]["date"]
        else:
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
        cand = target.with_name(f"{stem}_{n:03d}{suffix}")
        if not cand.exists():
            return cand
        n += 1


def _abbr(text: str) -> str:
    """Tres primeras letras/dígitos en mayúsculas, sin tildes (Vacaciones_2025 -> VAC)."""
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return (re.sub(r"[^0-9A-Za-z]", "", plain)[:3] or "XXX").upper()


def capture_stamp(item: dict) -> str:
    """YYYYMMDD_HHMMSS de la fecha de captura; si no hay, de st_mtime; si no, SINFECHA_hoy."""
    if item.get("date"):
        return datetime.fromisoformat(item["date"]).strftime("%Y%m%d_%H%M%S")
    try:
        return datetime.fromtimestamp(Path(item["path"]).stat().st_mtime).strftime("%Y%m%d_%H%M%S")
    except (OSError, ValueError):
        return "SINFECHA_" + date.today().strftime("%Y%m%d")


def standard_name(proj: dict, item: dict, category: str) -> str:
    """[PRO]_[CAT]_[FECHA].ext  (el sufijo _001, _002… lo añade unique_path si hay colisión)."""
    cat = "DSC" if category == DISCARD_DIR else _abbr(category)
    ext = Path(item["path"]).suffix.lower()
    return f"{_abbr(proj['name'])}_{cat}_{capture_stamp(item)}{ext}"


def classify(proj: dict, item: dict, category: str) -> None:
    folder = project_dir(proj) / category
    folder.mkdir(parents=True, exist_ok=True)
    prev = item.get("decision")
    current = Path(item["path"])
    new_name = standard_name(proj, item, category)

    if prev:  # reclasificación: se mueve (y renombra) el archivo que ya está en el proyecto
        if prev["category"] == category:
            return
        if not current.exists():
            raise FileNotFoundError(f"No se encuentra {current}")
        dest = unique_path(folder / new_name)
        shutil.move(str(current), str(dest))
    else:
        dest = unique_path(folder / new_name)
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


def _decision_items(proj: dict, category: str) -> list[dict]:
    return [{"key": k, "path": d["dest"], "date": d.get("date"), "decision": d}
            for k, d in list(proj["decisions"].items()) if d["category"] == category]


def _remove_category(proj: dict, category: str) -> None:
    proj["categories"] = [c for c in proj["categories"] if c != category]
    try:
        (project_dir(proj) / category).rmdir()  # solo si quedó vacía; nunca borra archivos
    except OSError:
        pass
    save_project(proj)


def delete_category(proj: dict, category: str, target: str | None) -> list[str]:
    """Elimina una categoría. Sus fotos pasan a `target` (otra categoría) o, si es None,
    vuelven a pendientes. No se borra ninguna foto."""
    errors = []
    for item in _decision_items(proj, category):
        try:
            if target is None:
                unclassify(proj, item)
            else:
                classify(proj, item, target)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{Path(item['path']).name}: {e}")
    if not errors:  # si algo falló, la categoría se mantiene para no esconder esas fotos
        _remove_category(proj, category)
    return errors


def rename_category(proj: dict, old: str, new: str) -> list[str]:
    """Renombra moviendo las fotos a la carpeta nueva (y con el nuevo prefijo en el nombre)."""
    cats = proj["categories"]
    cats.insert(cats.index(old), new)  # misma posición: conserva su tecla 1-9
    ensure_category_dirs(proj)
    return delete_category(proj, old, new)


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
    for k in ("sim_result", "sim_sel", "sim_zoom", "sim_idx"):  # resultados de otro proyecto
        ss.pop(k, None)
    ss["_goto_view"] = "revision" if proj.get("similar_done") else "similares"
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


def _default_base() -> str:
    base = Path(gcfg.get("last_base", DEFAULT_BASE)).expanduser()
    if not base.is_absolute():  # configuraciones antiguas con ruta relativa
        base = Path.cwd() / base
    return str(base.resolve())


def _default_source() -> str:
    last = gcfg.get("last_source")
    if last and Path(last).is_dir():
        return last
    pictures = next((p for name, p in quick_places() if name.startswith("🖼️")), Path.home())
    return str(pictures)


def setup_screen() -> None:
    st.title("📸 Limpiador de Galería")
    st.caption("Organiza tus fotos y vídeos en proyectos y categorías, sin borrar nada del disco.")

    base = folder_picker(
        "📁 Carpeta de destino (donde se guardan los proyectos)", "pick_base", _default_base(),
        help="Dentro se crea una carpeta por proyecto, con una subcarpeta por categoría.",
        allow_create=True,
    )
    existing = list_projects(base)

    labels = ["➕ Nuevo proyecto", f"📂 Continuar proyecto ({len(existing)})"]
    tabs = st.tabs(labels[::-1] if existing else labels)
    tab_open, tab_new = (tabs[0], tabs[1]) if existing else (tabs[1], tabs[0])

    with tab_open:
        if not existing:
            st.info("No hay proyectos en esta carpeta de destino todavía.")
        last = gcfg.get("last_project")
        for name in sorted(existing, key=lambda n: (n != last, n.lower())):
            try:
                preview = load_project(Path(base) / name)
            except (OSError, ValueError) as e:
                st.error(f"No se pudo leer el proyecto «{name}»: {e}")
                continue
            with st.container(border=True):
                c1, c2 = st.columns([5, 1], vertical_alignment="center")
                cats = Counter(d["category"] for d in preview["decisions"].values())
                c1.markdown(f"### 📁 {name}" + ("  ·  _último usado_" if name == last else ""))
                c1.caption(
                    f"Origen: `{preview.get('source')}`  \n"
                    f"Clasificadas: **{len(preview['decisions'])}** · "
                    + " · ".join(f"{('🗑️ ' if c == DISCARD_DIR else '')}{c}: {n}" for c, n in cats.items())
                    + (f"  \nÚltima modificación: {preview['updated'].replace('T', ' ')}" if preview.get("updated") else "")
                )
                if c2.button("Abrir ▶", key=f"open_{name}", type="primary", width="stretch"):
                    open_project(preview)
                    st.rerun()

    with tab_new:
        name = st.text_input("Nombre del proyecto", placeholder="Vacaciones_2025")
        source = folder_picker(
            "🖼️ Carpeta de origen (fotos sin clasificar)", "pick_source", _default_source(),
            media_exts=tuple(MEDIA_EXTS),
        )
        recursive = st.checkbox("Incluir subcarpetas del origen", value=True)
        cats_txt = st.text_area(
            "Categorías iniciales (una por línea o separadas por comas)",
            value="\n".join(DEFAULT_CATEGORIES),
            height=110,
        )
        mode = st.radio(
            "¿Qué hacer con cada foto clasificada?",
            ["copiar", "mover"],
            format_func=lambda m: "Copiar (el origen queda intacto)" if m == "copiar" else "Mover (vacía el origen)",
            horizontal=True,
        )
        d_from, d_to, undated = date_filter_inputs("new")

        pname_preview = safe_name(name)
        if pname_preview:
            st.caption(f"📍 Las fotos clasificadas se guardarán en: `{Path(base) / pname_preview}`")
        if st.button("Crear proyecto", type="primary"):
            pname = pname_preview
            src = Path(source)
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
            elif Path(base).resolve() == src.resolve():
                errors.append("La carpeta de destino no puede ser la misma que la de origen; "
                              "elige otra (por ejemplo, tu carpeta personal → Fotos_Organizadas).")
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
    sb.caption(f"Destino (aquí se guardan las fotos clasificadas):\n\n`{project_dir(proj).resolve()}`"
               f"\n\nOrigen: `{short(proj['source'])}`")
    if sb.button("📂 Abrir carpeta del proyecto", width="stretch"):
        open_with_system(str(project_dir(proj).resolve()))

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

    with sb.expander("✏️ Editar o eliminar categorías"):
        counts = Counter(d["category"] for d in proj["decisions"].values())
        cats = proj["categories"]
        cat = st.selectbox("Categoría", cats, key="cat_edit_sel",
                           format_func=lambda c: f"{c} ({counts.get(c, 0)} fotos)")
        action = st.radio("Acción", ["renombrar", "eliminar"], horizontal=True, key="cat_edit_action",
                          format_func=lambda a: "✏️ Renombrar" if a == "renombrar" else "🗑️ Eliminar")
        n = counts.get(cat, 0)
        if action == "renombrar":
            new = safe_name(st.text_input("Nuevo nombre", key="cat_edit_new"))
            if n and new:
                st.caption(f"Sus {n} fotos pasarán a la carpeta «{new}» y se renombrarán "
                           f"({_abbr(proj['name'])}_{_abbr(new)}_…).")
            if st.button("✏️ Renombrar", width="stretch", disabled=not new or new == cat):
                if new == DISCARD_DIR or new in cats:
                    ss.flash = ("error", f"Ya existe una categoría «{new}» o el nombre no es válido.")
                else:
                    with st.spinner(f"Moviendo {n} fotos…"):
                        errs = rename_category(proj, cat, new)
                    if ss.get("last_cat") == cat:
                        ss.last_cat = new
                    ss.flash = (("warning", "Renombrada con errores: " + "; ".join(errs[:5])) if errs
                                else ("success", f"Categoría «{cat}» renombrada a «{new}»."))
                st.rerun()
        else:
            target = None
            if n:
                options = ["__pendiente__"] + [c for c in cats if c != cat] + [DISCARD_DIR]
                choice = st.selectbox(
                    f"¿Qué hacer con sus {n} fotos?", options, key="cat_edit_target",
                    format_func=lambda c: "⏳ Volver a pendientes (revisarlas de nuevo)" if c == "__pendiente__"
                    else ("🗑️ Mover a _Descartadas" if c == DISCARD_DIR else f"📁 Mover a «{c}»"),
                )
                target = None if choice == "__pendiente__" else choice
                st.caption("No se borra ninguna foto: solo cambian de carpeta"
                           + (" (en modo copiar, «volver a pendientes» quita la copia; "
                              "el original sigue en el origen)." if proj["mode"] == "copiar" else "."))
            confirm = st.checkbox(f"Sí, eliminar «{cat}»", key=f"cat_edit_confirm_{cat}")
            if st.button("🗑️ Eliminar categoría", width="stretch", type="primary",
                         disabled=not confirm or len(cats) == 1,
                         help="Debe quedar al menos una categoría" if len(cats) == 1 else None):
                with st.spinner(f"Moviendo {n} fotos…"):
                    errs = delete_category(proj, cat, target)
                if ss.get("last_cat") == cat:
                    ss.pop("last_cat")
                ss.scan = None  # en modo mover, las que vuelven al origen deben re-escanearse
                ss.flash = (("warning", "No se pudo eliminar del todo: " + "; ".join(errs[:5])) if errs
                            else ("success", f"Categoría «{cat}» eliminada."))
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
    video = is_video(path)
    if video:
        meta = read_video_metadata(str(path))
        if not EXIFTOOL:
            sb.warning(
                "Para leer fecha y GPS de vídeos instala exiftool:\n\n"
                f"`{comp.install_cmd('exiftool')}`"
            )
    else:
        meta = read_metadata(str(path), stat.st_mtime)
    if meta["date"]:
        dt = datetime.fromisoformat(meta["date"])
        sb.markdown(f"**📅 Captura:** {dt.strftime('%d/%m/%Y %H:%M:%S')}")
    else:
        sb.markdown(f"**📅 Captura:** _sin fecha {'en metadatos' if video else 'EXIF'}_")
    sb.markdown(f"**📄 Archivo original:** `{Path(item['key']).name}`")
    if item.get("decision"):
        sb.markdown(f"**🏷️ Nombre final:** `{path.name}`")
    if meta["width"]:
        mp = meta["width"] * meta["height"] / 1e6
        sb.markdown(f"**🖼️ Resolución:** {meta['width']} × {meta['height']} px ({mp:.1f} MP)")
    if meta.get("duration"):
        mins, secs = divmod(int(round(meta["duration"])), 60)
        sb.markdown(f"**⏱️ Duración:** {mins}:{secs:02d}")
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


STRIP_SIZE = (240, 180)  # miniaturas de la tira «anteriores / siguientes»


@st.cache_data(show_spinner=False, max_entries=3000)
def strip_thumb(path: str, mtime: float) -> bytes | None:
    """Miniatura de tamaño fijo (con bandas) para que la tira quede alineada."""
    try:
        if is_video(path):
            data = comp.extract_frame(Path(path), 1.0) or comp.extract_frame(Path(path), 0.0)
            if not data:
                return None
            img = Image.open(BytesIO(data))
        else:
            img = Image.open(path)
            img.draft("RGB", (STRIP_SIZE[0] * 2, STRIP_SIZE[1] * 2))
            img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
        img.thumbnail(STRIP_SIZE)
        canvas = Image.new("RGB", STRIP_SIZE, (30, 32, 38))
        canvas.paste(img, ((STRIP_SIZE[0] - img.width) // 2, (STRIP_SIZE[1] - img.height) // 2))
        buf = BytesIO()
        canvas.save(buf, format="JPEG", quality=80)
        return buf.getvalue()
    except Exception:
        return None


def filmstrip(items: list[dict], idx: int, before: int = 2, after: int = 4) -> None:
    """Las `before` anteriores, la actual y las `after` siguientes; clic para saltar."""
    cols = st.columns(before + 1 + after)
    for col, off in zip(cols, range(-before, after + 1)):
        j = idx + off
        if not 0 <= j < len(items):
            continue
        it = items[j]
        p = Path(it["path"])
        dec = it.get("decision")
        badge = "⏳" if not dec else ("🗑️" if dec["category"] == DISCARD_DIR else f"✅ {dec['category']}")
        with col:
            st.markdown(
                f"<div style='height:4px;border-radius:2px;margin-bottom:3px;"
                f"background:{'#2563eb' if off == 0 else 'transparent'}'></div>", unsafe_allow_html=True)
            thumb = strip_thumb(str(p), p.stat().st_mtime) if p.exists() else None
            if thumb:
                st.image(thumb, width="stretch")
            else:
                st.caption("(sin vista previa)")
            icon = "🎬 " if is_video(p) else ""
            if off == 0:
                st.markdown(f"<div style='text-align:center;font-size:.85rem'><b>▶ {icon}{j + 1} · actual</b>"
                            f"<br>{badge}</div>", unsafe_allow_html=True)
            elif st.button(f"{icon}{j + 1} · {badge}", key=f"strip_{j}", width="stretch",
                           help=Path(it["key"]).name):
                ss.idx = j
                st.rerun()


VIDEO_MIME = {".webm": "video/webm", ".mkv": "video/webm", ".avi": "video/x-msvideo"}
VIDEO_EMBED_MAX_MB = 500  # por encima se pide confirmación (st.video carga el archivo en memoria)


def video_player(path: Path) -> None:
    size_mb = path.stat().st_size / (1024 * 1024)
    _, mid, _ = st.columns([1, 10, 1])
    with mid:
        st.markdown("<style>[data-testid='stVideo']{max-height:72vh}</style>", unsafe_allow_html=True)
        preview = comp.preview_path(path)
        if preview.exists():
            st.video(str(preview), format="video/webm")
            st.caption("Mostrando la vista previa WebM (720p) generada para el navegador; "
                       "el archivo original no se modifica.")
        elif size_mb <= VIDEO_EMBED_MAX_MB or st.checkbox(
            f"Vídeo grande ({size_mb:.0f} MB): cargar en el navegador de todos modos",
            key=f"bigvid_{path}",
        ):
            st.video(str(path), format=VIDEO_MIME.get(path.suffix.lower(), "video/mp4"))
        c1, c2, c3 = st.columns([3, 2, 2], vertical_alignment="center")
        c1.caption(
            "¿No se reproduce? Firefox no admite HEVC/H.265 (iPhone), AVI ni MKV con H.264: "
            "genera una vista previa WebM o ábrelo con el reproductor del sistema."
        )
        if not preview.exists() and c2.button(
            "🦊 Vista previa WebM", width="stretch", key="mk_preview",
            disabled=not comp.can_make_preview(),
            help="Crea una copia ligera VP9 en ~/.cache/limpiador-galeria (se reproduce en cualquier navegador)",
        ):
            with st.spinner("Generando vista previa…"):
                try:
                    comp.make_browser_preview(path)
                except Exception as e:  # noqa: BLE001
                    ss.flash = ("error", f"No se pudo generar la vista previa: {e}")
            st.rerun()
        if c3.button("▶️ Abrir en reproductor", width="stretch", key="open_sys"):
            open_with_system(str(path))


# --------------------------------------------------------------------------
# Limpieza de similares (perceptual hashing)
# --------------------------------------------------------------------------
SIM_THUMB = (900, 700)  # miniaturas grandes en la vista de un grupo


@st.cache_data(show_spinner=False, max_entries=60000)
def image_signature(path: str, mtime: float) -> dict | None:
    return sim.signature(path)


@st.cache_data(show_spinner=False, max_entries=300)
def sim_thumb(path: str, mtime: float) -> bytes | None:
    try:
        with Image.open(path) as img:
            img.draft("RGB", (SIM_THUMB[0] * 2, SIM_THUMB[1] * 2))
            img = ImageOps.exif_transpose(img)
            img.thumbnail(SIM_THUMB)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=85)
            return buf.getvalue()
    except Exception:
        return None


def _wkey(key: str) -> str:
    return hashlib.md5(key.encode()).hexdigest()[:12]


def apply_group(proj: dict, group: list[dict], keep: set[str]) -> list[str]:
    """Descarta las fotos del grupo que no están en `keep`.

    Las conservadas no se mueven: siguen pendientes para clasificarlas en la revisión.
    """
    errors = []
    for it in group:
        if it["key"] in keep:
            continue
        try:
            classify(proj, it, DISCARD_DIR)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{Path(it['key']).name}: {e}")
    return errors


def _sim_candidates(items: list[dict], include_done: bool) -> list[dict]:
    return [
        it for it in items
        if not is_video(it["path"])
        and (not it.get("decision") or (include_done and it["decision"]["category"] != DISCARD_DIR))
        and Path(it["path"]).exists()
    ]


def run_similar_search(items: list[dict], sens: str, include_done: bool) -> dict:
    """Calcula huellas (con caché) y agrupa. Se guarda en sesión: no se recalcula en cada clic."""
    cands = _sim_candidates(items, include_done)
    progress = st.progress(0.0, text="Analizando fotos…") if len(cands) > 20 else None
    keys, ph, dh, times = [], [], [], []
    for i, it in enumerate(cands):
        sig = image_signature(it["path"], Path(it["path"]).stat().st_mtime)
        if sig:
            keys.append(it["key"])
            ph.append(sig["phash"])
            dh.append(sig["dhash"])
            times.append(datetime.fromisoformat(it["date"]).timestamp() if it.get("date") else None)
        if progress and i % 25 == 0:
            progress.progress(i / len(cands), text=f"Analizando fotos… {i}/{len(cands)}")
    if progress:
        progress.empty()
    groups = sim.find_groups(ph, dh, times, sim.SENSITIVITY[sens])
    order = {k: i for i, k in enumerate(keys)}
    out = []
    for g in groups:
        members = [keys[i] for i in g["members"]]
        out.append({"keys": members, "level": g["level"], "dist": g["dist"]})
    # Primero las más seguras (idénticas), y dentro de cada nivel en orden cronológico
    out.sort(key=lambda g: (g["level"], min(order[k] for k in g["keys"])))
    return {"params": (sens, include_done), "groups": out, "analyzed": len(keys)}


def similar_view(proj: dict, items: list[dict]) -> None:
    h1, h2 = st.columns([3, 1], vertical_alignment="center")
    h1.subheader("1️⃣ Limpieza de similares")
    if h2.button("Terminar y pasar a revisión ➡️", type="primary", width="stretch",
                 help="Marca este paso como hecho; puedes volver cuando quieras"):
        proj["similar_done"] = True
        save_project(proj)
        go_to("revision")
    st.caption(
        "La app busca sola clones y ráfagas con varios niveles de parecido. Tú eliges con qué foto "
        "quedarte en cada grupo; las demás van a `_Descartadas` (no se borra nada). Las que conservas "
        "siguen **pendientes** para clasificarlas en la revisión."
    )

    c1, c2, c3 = st.columns([3, 2, 1], vertical_alignment="bottom")
    sens = c1.segmented_control("Sensibilidad", list(sim.SENSITIVITY), default="normal", key="sim_sens",
                                required=True, format_func=lambda s: sim.SENSITIVITY_LABELS[s],
                                help="Estricta: solo clones claros · Normal: clones, casi idénticas y ráfagas "
                                     "(recomendada) · Amplia: encuentra más ráfagas, pero revisa más grupos") \
        or "normal"
    include_done = c2.checkbox("Incluir ya clasificadas", value=False, key="sim_done",
                               help="Por defecto solo se comparan las fotos pendientes.")
    again = c3.button("🔄 Buscar de nuevo", width="stretch")

    res = ss.get("sim_result")
    if again or res is None or res["params"] != (sens, include_done):
        res = ss.sim_result = run_similar_search(items, sens, include_done)
        ss.sim_idx = 0
        ss.sim_sel = {}

    # Grupos vigentes: se quitan las fotos ya descartadas/clasificadas y los grupos ignorados
    by_key = {it["key"]: it for it in _sim_candidates(items, include_done)}
    ignored = {frozenset(g) for g in proj.get("similar_ignored", [])}
    live = []
    for g in res["groups"]:
        members = [by_key[k] for k in g["keys"] if k in by_key]
        if len(members) > 1 and frozenset(m["key"] for m in members) not in ignored:
            live.append({**g, "members": members})

    if not live:
        st.success(f"✨ No quedan grupos de fotos similares ({res['analyzed']} fotos analizadas). "
                   "Puedes pasar a la revisión.")
        return

    counts = Counter(g["level"] for g in live)
    f1, f2 = st.columns([3, 2], vertical_alignment="bottom")
    flt = f1.segmented_control(
        "Mostrar", ["todos", 0, 1, 2], default="todos", key="sim_filter", required=True,
        format_func=lambda v: f"Todos ({len(live)})" if v == "todos"
        else f"{sim.LEVELS[v][0]} {sim.LEVELS[v][1]} ({counts.get(v, 0)})",
    )
    shown = live if flt in (None, "todos") else [g for g in live if g["level"] == flt]
    f2.caption(f"{sum(len(g['members']) for g in live)} fotos en {len(live)} grupos · "
               f"{res['analyzed']} fotos analizadas")
    if not shown:
        st.info("No hay grupos de este tipo.")
        return

    ss.sim_idx = max(0, min(ss.get("sim_idx", 0), len(shown) - 1))
    group = shown[ss.sim_idx]
    members = group["members"]
    gid = _wkey("|".join(sorted(m["key"] for m in members)))

    # Datos de cada foto y sugerencia de la mejor
    info = []
    for m in members:
        stt = Path(m["path"]).stat()
        meta = read_metadata(m["path"], stt.st_mtime)
        sig = image_signature(m["path"], stt.st_mtime) or {"sharp": 0}
        mp = (meta["width"] or 0) * (meta["height"] or 0) / 1e6
        info.append({"meta": meta, "size": stt.st_size, "mp": mp, "sharp": sig["sharp"], "mtime": stt.st_mtime})
    best = sim.best_member(info)
    max_sharp = max(i["sharp"] for i in info) or 1
    sel = ss.setdefault("sim_sel", {}).setdefault(gid, {members[best]["key"]})

    # ---- Cabecera y navegación ----
    icon, label = sim.LEVELS[group["level"]]
    n1, n2, n3 = st.columns([1, 4, 1], vertical_alignment="center")
    if n1.button("◀ Anterior", width="stretch", shortcut="Left", disabled=ss.sim_idx == 0):
        ss.sim_idx -= 1
        st.rerun()
    n2.markdown(
        f"<div style='text-align:center;font-size:1.15rem'><b>Grupo {ss.sim_idx + 1} de {len(shown)}</b>"
        f" · {icon} {label} · {len(members)} fotos</div>", unsafe_allow_html=True)
    if n3.button("Siguiente ▶", width="stretch", shortcut="Right", disabled=ss.sim_idx >= len(shown) - 1):
        ss.sim_idx += 1
        st.rerun()

    def finish(keep: set[str], msg: str) -> None:
        errs = apply_group(proj, members, keep)
        if len(keep) > 1:  # las que conservaste juntas no deben volver a aparecer como grupo
            proj.setdefault("similar_ignored", []).append(sorted(keep))
            save_project(proj)
        ss.sim_sel.pop(gid, None)
        ss.flash = ("error", "; ".join(errs)) if errs else ("toast", msg)
        st.rerun()  # el grupo desaparece de la lista y aparece el siguiente en su lugar

    # ---- Ampliación ----
    zoom = ss.get("sim_zoom")
    if zoom in {m["key"] for m in members}:
        zm = next(m for m in members if m["key"] == zoom)
        with st.container(border=True):
            z1, z2 = st.columns([5, 1], vertical_alignment="center")
            z1.markdown(f"🔍 **{Path(zm['key']).name}**")
            if z2.button("✖ Cerrar", key="sim_zoom_close", width="stretch", shortcut="Esc"):
                ss.sim_zoom = None
                st.rerun()
            data = display_bytes(zm["path"], Path(zm["path"]).stat().st_mtime)
            if data:
                st.image(data, width="stretch")

    # ---- Fotos del grupo ----
    per_row = 2 if len(members) <= 2 else (3 if len(members) in (3, 6, 9) else 4)
    for row in range(0, len(members), per_row):
        cols = st.columns(per_row)
        for col, idx in zip(cols, range(row, min(row + per_row, len(members)))):
            m, inf = members[idx], info[idx]
            keep = m["key"] in sel
            with col.container(border=True):
                st.markdown(
                    f"<div style='text-align:center;font-weight:600;padding:2px;border-radius:6px;"
                    f"background:{'#16a34a33' if keep else '#dc262622'}'>"
                    f"{'✅ SE CONSERVA' if keep else '🗑️ Se descarta'}"
                    f"{' · ⭐ sugerida' if idx == best else ''}</div>", unsafe_allow_html=True)
                thumb = sim_thumb(m["path"], inf["mtime"])
                if thumb:
                    st.image(thumb, width="stretch")
                meta = inf["meta"]
                dt = datetime.fromisoformat(meta["date"]).strftime("%d/%m/%Y %H:%M:%S") if meta["date"] else "sin fecha"
                res_txt = f"{meta['width']}×{meta['height']}" if meta["width"] else "?"
                st.caption(
                    f"**[{idx + 1}]** `{Path(m['key']).name}`  \n"
                    f"🖼️ {res_txt} · 💾 {inf['size'] / (1024 * 1024):.2f} MB  \n📅 {dt}  \n"
                    f"🔎 Nitidez: {inf['sharp'] / max_sharp * 100:.0f} %"
                )
                b1, b2, b3 = st.columns([3, 2, 1])
                if b1.button(f"⭐ Me quedo con esta", key=f"only_{gid}_{idx}", type="primary",
                             width="stretch", shortcut=str(idx + 1) if idx < 9 else None,
                             help="Conserva solo esta foto y descarta las demás del grupo"):
                    finish({m["key"]}, f"Te quedaste con {Path(m['key']).name}")
                if b2.button("➖ Quitar" if keep else "➕ También", key=f"tog_{gid}_{idx}", width="stretch",
                             help="Marca o desmarca esta foto para conservarla junto a otras"):
                    sel.symmetric_difference_update({m["key"]})
                    st.rerun()
                if b3.button("🔍", key=f"zoom_{gid}_{idx}", width="stretch", help="Ver en grande"):
                    ss.sim_zoom = m["key"]
                    st.rerun()

    # ---- Acciones del grupo ----
    n_disc = len(members) - len(sel)
    a1, a2 = st.columns([3, 2])
    if a1.button(f"✅ Aplicar: conservar {len(sel)} y descartar {n_disc}", type="primary", width="stretch",
                 shortcut="Enter", disabled=not sel or not n_disc, key=f"apply_{gid}"):
        finish(set(sel), f"Grupo resuelto: {len(sel)} conservada(s), {n_disc} descartada(s)")
    if a2.button("🙈 No son duplicadas (conservar todas)", width="stretch", shortcut="N", key=f"ign_{gid}"):
        proj.setdefault("similar_ignored", []).append(sorted(m["key"] for m in members))
        save_project(proj)
        ss.sim_sel.pop(gid, None)
        ss.flash = ("toast", "Grupo marcado como no duplicado")
        st.rerun()
    st.caption("⌨️ **1-9** me quedo con esa foto · **Enter** aplicar selección · **N** no son duplicadas · "
               "**← / →** grupo anterior / siguiente · Las conservadas siguen pendientes para la revisión.")


# --------------------------------------------------------------------------
# 3. Optimización y compresión (opcional)
# --------------------------------------------------------------------------
OPT_DIR = ".optimizacion_tmp"  # dentro del proyecto: mismo disco, reemplazo atómico
OPT_RESULTS = "resultados.json"
CMP_MAX = (1000, 650)  # tamaño del comparador antes/después
MB = 1024 * 1024
STATUS_LABELS = {
    "ok": "✅ Reducido", "sin_ahorro": "➖ Sin ahorro (se conserva)",
    "omitido": "⏭️ Omitido", "error": "❌ Error",
}


def fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB"):
        if abs(n) < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.2f} GB"


def opt_dir(proj: dict) -> Path:
    return project_dir(proj) / OPT_DIR


def load_opt_run(proj: dict) -> dict | None:
    try:
        return json.loads((opt_dir(proj) / OPT_RESULTS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_opt_run(proj: dict, run: dict) -> None:
    opt_dir(proj).mkdir(parents=True, exist_ok=True)
    atomic_write_json(opt_dir(proj) / OPT_RESULTS, run)


def discard_opt_run(proj: dict) -> None:
    shutil.rmtree(opt_dir(proj), ignore_errors=True)


def files_in_scope(proj: dict, cats: set[str], kinds: set[str], reprocess: bool) -> list[dict]:
    """Archivos ya organizados (dentro del proyecto) que se pueden comprimir."""
    optimized = proj.get("optimized", {})
    out = []
    for d in proj["decisions"].values():
        p = Path(d["dest"])
        if d["category"] not in cats or not p.is_file() or not comp.is_compressible(p):
            continue
        if comp.media_kind(p) not in kinds:
            continue
        size = p.stat().st_size
        if not reprocess and optimized.get(str(p)) == size:
            continue  # ya optimizado y sin cambios desde entonces
        out.append({"path": str(p), "category": d["category"], "size": size, "kind": comp.media_kind(p)})
    return sorted(out, key=lambda f: f["path"])


def process_opt_run(proj: dict, run: dict) -> None:
    """Procesa la cola guardando tras cada archivo (si se interrumpe, se puede continuar)."""
    opts = comp.Options(**run["options"])
    total = len(run["queue"])
    pending = [p for p in run["queue"] if p not in run["results"]]
    bar = st.progress(0.0)
    for src in pending:
        done = len(run["results"])
        bar.progress(done / total, text=f"Comprimiendo {done + 1}/{total}: {Path(src).name}…")
        srcp = Path(src)
        if srcp.is_file():
            res = comp.compress_file(srcp, opt_dir(proj), opts, tag=f"{done:05d}")
        else:
            res = {"src": src, "orig_size": 0, "orig_mtime": 0, "kind": comp.media_kind(srcp), "tmp": None,
                   "new_size": 0, "new_ext": srcp.suffix.lower(), "tool": "", "status": "error",
                   "msg": "el archivo ya no existe"}
        run["results"][src] = res
        save_opt_run(proj, run)
    bar.empty()


def apply_opt_run(proj: dict, run: dict, selected: set[str]) -> tuple[int, int, list[str]]:
    """Reemplaza los originales seleccionados por su versión comprimida."""
    applied, saved, errors = 0, 0, []
    by_dest = {d["dest"]: k for k, d in proj["decisions"].items()}
    optimized = proj.setdefault("optimized", {})
    for src, r in run["results"].items():
        if r["status"] != "ok" or src not in selected:
            continue
        srcp, tmp = Path(src), Path(r["tmp"])
        try:
            stt = srcp.stat()
            if stt.st_size != r["orig_size"] or abs(stt.st_mtime - r["orig_mtime"]) > 1:
                raise RuntimeError("el original cambió después de comprimir; no se reemplaza")
            if not tmp.is_file():
                raise RuntimeError("falta el archivo comprimido")
            if r["new_ext"] == srcp.suffix.lower():
                target = srcp
                os.replace(tmp, target)  # atómico: mismo sistema de archivos
            else:  # p. ej. PNG -> WebP o AVI -> MP4
                target = unique_path(srcp.with_suffix(r["new_ext"]))
                shutil.move(str(tmp), str(target))
                srcp.unlink()
                if src in by_dest:
                    proj["decisions"][by_dest[src]]["dest"] = str(target)
            os.utime(target, (stt.st_atime, stt.st_mtime))  # conserva la fecha de modificación
            optimized.pop(src, None)
            optimized[str(target)] = target.stat().st_size
            applied += 1
            saved += r["orig_size"] - r["new_size"]
        except Exception as e:  # noqa: BLE001
            errors.append(f"{srcp.name}: {e}")
    proj.setdefault("optimization_log", []).append(
        {"at": datetime.now().isoformat(timespec="seconds"), "files": applied, "saved_bytes": saved}
    )
    save_project(proj)
    discard_opt_run(proj)
    return applied, saved, errors


@st.cache_data(show_spinner=False, max_entries=40)
def cached_frame(path: str, mtime: float, seconds: float) -> bytes | None:
    return comp.extract_frame(Path(path), seconds)


def _cmp_images(before: bytes, after: bytes, zoom: bool) -> tuple[str, str, tuple[int, int]] | None:
    """Prepara ambas imágenes con el mismo encuadre y las devuelve en base64 (PNG)."""
    try:
        a = ImageOps.exif_transpose(Image.open(BytesIO(before))).convert("RGB")
        b = ImageOps.exif_transpose(Image.open(BytesIO(after))).convert("RGB")
    except Exception:
        return None
    if b.size != a.size:
        b = b.resize(a.size)
    if zoom:  # recorte central al 100 %: los artefactos se ven sin el suavizado del escalado
        w, h = min(CMP_MAX[0], a.width), min(CMP_MAX[1], a.height)
        box = ((a.width - w) // 2, (a.height - h) // 2, (a.width - w) // 2 + w, (a.height - h) // 2 + h)
        a, b = a.crop(box), b.crop(box)
    else:
        a.thumbnail(CMP_MAX)
        b = b.resize(a.size)
    enc = []
    for img in (a, b):
        buf = BytesIO()
        img.save(buf, "PNG")
        enc.append(base64.b64encode(buf.getvalue()).decode())
    return enc[0], enc[1], a.size


def compare_slider(before: bytes | None, after: bytes | None, zoom: bool) -> None:
    prepared = _cmp_images(before, after, zoom) if before and after else None
    if not prepared:
        st.warning("No se pudo generar la vista previa de este archivo.")
        return
    b64_before, b64_after, (w, h) = prepared
    html = f"""
<style>
 body{{margin:0}}
 .cmp{{position:relative;max-width:{w}px;margin:auto;user-select:none;font-family:sans-serif}}
 .cmp img{{display:block;width:100%;height:auto}}
 .cmp .before{{position:absolute;inset:0;clip-path:inset(0 50% 0 0)}}
 .cmp .line{{position:absolute;top:0;bottom:0;left:50%;width:2px;background:#fff;
             box-shadow:0 0 4px #000;pointer-events:none}}
 .cmp input{{position:absolute;inset:0;width:100%;height:100%;opacity:0;cursor:ew-resize;margin:0}}
 .tag{{position:absolute;top:8px;padding:2px 8px;background:rgba(0,0,0,.6);color:#fff;
       border-radius:4px;font-size:13px;pointer-events:none}}
</style>
<div class="cmp">
 <img src="data:image/png;base64,{b64_after}">
 <img class="before" id="b" src="data:image/png;base64,{b64_before}">
 <div class="line" id="l"></div>
 <span class="tag" style="left:8px">◀ Antes (original)</span>
 <span class="tag" style="right:8px">Después (comprimido) ▶</span>
 <input type="range" min="0" max="100" value="50" step="0.1" aria-label="Comparar"
  oninput="document.getElementById('b').style.clipPath='inset(0 '+(100-this.value)+'% 0 0)';
           document.getElementById('l').style.left=this.value+'%'">
</div>"""
    components.html(html, height=h + 8)
    st.caption("Arrastra sobre la imagen para mover la cortina entre el original y el comprimido.")


def tools_panel() -> None:
    t = comp.TOOLS
    ok = lambda name: "✅" if t[name] else "❌"  # noqa: E731
    with st.expander("🧰 Herramientas detectadas en el sistema", expanded=not t["ffmpeg"] or not t["jpegoptim"]):
        st.markdown(
            f"- {ok('jpegoptim')} **jpegoptim** (JPEG) · respaldo {ok('jpegtran')} jpegtran\n"
            f"- {ok('oxipng')} **oxipng** (PNG) · respaldo {ok('optipng')} optipng · si no hay ninguno, Pillow\n"
            f"- ✅ **Pillow** (WebP sin pérdida)\n"
            f"- {ok('ffmpeg')} **ffmpeg** / {ok('ffprobe')} ffprobe (vídeos)\n"
            f"- {ok('exiftool')} **exiftool** (refuerza fecha/GPS de los vídeos)"
        )
        codecs = comp.available_video_codecs()
        if t["ffmpeg"]:
            st.markdown("Códecs de vídeo disponibles: " + (", ".join(
                comp.VIDEO_CODECS[c][0].split(" (")[0] for c in codecs) or "ninguno"))
        st.markdown("Todo de una vez (en la carpeta de la app): `bash instalar.sh`"
                    + (" · con H.264/H.265: `bash instalar.sh --rpmfusion`" if comp.IS_FEDORA else "")
                    + ". O a mano:")
        st.code(comp.install_cmd("ffmpeg", "jpegoptim", "optipng", "jpegtran", "exiftool") + "\n"
                + comp.install_cmd("oxipng") + "   # opcional", language="bash")
        if comp.IS_FEDORA and not {"libx264", "libx265"} & set(codecs):
            st.info("El ffmpeg de Fedora («ffmpeg-free») no incluye H.264 ni H.265; puedes usar AV1, "
                    "que comprime más y Firefox lo reproduce. Para tener H.264/H.265, instala el "
                    "ffmpeg completo de RPM Fusion:")
            st.code(comp.RPMFUSION_CMD, language="bash")


def optimization_view(proj: dict) -> None:
    st.subheader("3️⃣ Optimización y compresión (opcional)")
    st.caption(
        "Paso final e independiente. **Nada se modifica hasta que pulses «Confirmar y aplicar "
        "compresión»**: primero se procesa todo en una carpeta temporal para que compares el antes y "
        "el después. Si un archivo no reduce su peso, se conserva el original."
    )
    tools_panel()
    run = load_opt_run(proj)
    if run is None:
        opt_config(proj)
    else:
        opt_results(proj, run)


def opt_config(proj: dict) -> None:
    log = proj.get("optimization_log", [])
    if log:
        st.success(
            f"Optimizaciones ya aplicadas: {sum(x['files'] for x in log)} archivos · "
            f"{fmt_size(sum(x['saved_bytes'] for x in log))} liberados en total."
        )
    if proj.get("optimization_skipped"):
        st.info("Marcaste este paso como omitido: tus archivos están intactos. Puedes ejecutarlo cuando quieras.")

    all_cats = proj["categories"] + [DISCARD_DIR]
    label = lambda c: "🗑️ _Descartadas" if c == DISCARD_DIR else c  # noqa: E731
    scope = st.radio("Alcance", ["todo", "categorias"], horizontal=True, key="opt_scope",
                     format_func=lambda v: "Todo el proyecto" if v == "todo" else "Solo categorías específicas")
    if scope == "todo":
        cats = all_cats
    else:
        cats = st.multiselect("Categorías a comprimir", all_cats, default=proj["categories"][:1],
                              format_func=label, key="opt_cats")
    k1, k2, k3 = st.columns(3)
    do_photos = k1.checkbox("📷 Fotos", value=True, key="opt_photos")
    do_videos = k2.checkbox("🎬 Vídeos", value=True, key="opt_videos")
    reprocess = k3.checkbox("Reprocesar archivos ya optimizados", value=False, key="opt_re")

    col_f, col_v = st.columns(2)
    with col_f.container(border=True):
        st.markdown("**📷 Fotos**")
        jpeg_mode = st.radio(
            "JPEG", ["lossless", "visual"], key="opt_jpeg_mode",
            format_func=lambda m: "Sin pérdida: optimiza la codificación, píxeles idénticos"
            if m == "lossless" else "Visualmente sin pérdida: limita la calidad máxima",
        )
        jpeg_q = st.slider("Calidad máxima JPEG", 85, 98, 92, key="opt_jpeg_q",
                           disabled=jpeg_mode == "lossless",
                           help="Solo recomprime las fotos guardadas con más calidad que este valor.")
        strip = st.checkbox("Eliminar metadatos redundantes (comentarios, XMP, IPTC)", value=True,
                            key="opt_strip", help="El EXIF con fecha, GPS y orientación se conserva siempre.")
        webp = st.checkbox("Convertir PNG/BMP/TIFF a WebP sin pérdida (si pesa menos)", value=False,
                           key="opt_webp", help="Píxeles idénticos, pero la extensión cambia a .webp.")
    with col_v.container(border=True):
        st.markdown("**🎬 Vídeos**")
        codecs = comp.available_video_codecs()
        if not codecs:
            st.warning("Tu ffmpeg no tiene codificadores H.265, AV1 ni H.264 (o no está instalado): "
                       "los vídeos se omitirán.")
            codecs = list(comp.VIDEO_CODECS)
        codec = st.selectbox("Códec", codecs, key="opt_codec",
                             format_func=lambda c: comp.VIDEO_CODECS[c][0])
        _, crf_default, (crf_min, crf_max), _ = comp.VIDEO_CODECS[codec]
        crf = st.slider("CRF (menor = más calidad)", crf_min, crf_max, crf_default,
                        key=f"opt_crf_{codec}",
                        help="Visualmente indistinguible: ~18 en H.264, ~22 en H.265 y ~28 en AV1.")
        preset = st.select_slider("Preset", ["fast", "medium", "slow"], value="medium", key="opt_preset",
                                  help="Más lento = archivos algo más pequeños con la misma calidad.")
        skip_hevc = st.checkbox("Omitir vídeos que ya están en HEVC/AV1/VP9", value=True, key="opt_skiphevc")
        st.caption("Se conservan la pista de audio, la rotación, la fecha y el GPS. Los vídeos HDR se dejan intactos.")

    kinds = ({"foto"} if do_photos else set()) | ({"video"} if do_videos else set())
    files = files_in_scope(proj, set(cats), kinds, reprocess)
    n_vid = sum(f["kind"] == "video" for f in files)
    st.markdown(
        f"**{len(files)} archivos** en el alcance ({len(files) - n_vid} fotos, {n_vid} vídeos) · "
        f"{fmt_size(sum(f['size'] for f in files))}"
    )
    if n_vid:
        st.caption("⏳ Recomprimir vídeo lleva tiempo (de segundos a varios minutos por archivo).")

    b1, b2 = st.columns([2, 1])
    if b1.button("🚀 Comprimir en carpeta temporal (vista previa)", type="primary",
                 disabled=not files, width="stretch"):
        opts = comp.Options(
            jpeg_mode=jpeg_mode, jpeg_quality=jpeg_q, strip_redundant=strip, png_to_webp=webp,
            video_codec=codec, video_crf=crf, video_preset=preset, skip_hevc=skip_hevc,
        )
        run = {
            "created": datetime.now().isoformat(timespec="seconds"),
            "options": opts.to_dict(),
            "queue": [f["path"] for f in files],
            "categories": {f["path"]: f["category"] for f in files},
            "results": {},
        }
        save_opt_run(proj, run)
        process_opt_run(proj, run)
        st.rerun()
    if b2.button("⏭️ Omitir este paso (no tocar nada)", width="stretch"):
        proj["optimization_skipped"] = True
        save_project(proj)
        ss.flash = ("success", "Optimización omitida: tus archivos quedan intactos.")
        st.rerun()


def opt_results(proj: dict, run: dict) -> None:
    pending = [p for p in run["queue"] if p not in run["results"]]
    if pending:
        st.warning(f"La compresión se interrumpió: faltan {len(pending)} de {len(run['queue'])} archivos.")
        if st.button("▶️ Continuar compresión", type="primary"):
            process_opt_run(proj, run)
            st.rerun()

    results = list(run["results"].values())
    ok = [r for r in results if r["status"] == "ok"]
    orig_total = sum(r["orig_size"] for r in results)
    final_total = sum(r["new_size"] if r["status"] == "ok" else r["orig_size"] for r in results)
    saved_total = orig_total - final_total
    pct = saved_total / orig_total * 100 if orig_total else 0
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("💾 Espacio total inicial", fmt_size(orig_total))
    m2.metric("📦 Espacio final", fmt_size(final_total), delta=f"-{pct:.1f} %", delta_color="inverse")
    m3.metric("🎉 Espacio total liberado", fmt_size(saved_total))
    m4.metric("Archivos reducidos", f"{len(ok)} / {len(results)}")

    st.markdown("#### 📋 Antes y después, archivo por archivo")
    srcs = list(run["results"])
    rows = []
    for src in srcs:
        r = run["results"][src]
        name = Path(src).name
        if r["status"] == "ok" and r["new_ext"] != Path(src).suffix.lower():
            name += f" → {r['new_ext']}"
        rows.append({
            "Aplicar": r["status"] == "ok",
            "Archivo": name,
            "Categoría": run["categories"].get(src, ""),
            "Tipo": "🎬" if r["kind"] == "video" else "📷",
            "Original (MB)": r["orig_size"] / MB,
            "Comprimido (MB)": (r["new_size"] if r["status"] == "ok" else r["orig_size"]) / MB,
            "Ahorro (%)": (1 - r["new_size"] / r["orig_size"]) * 100 if r["status"] == "ok" else 0.0,
            "Estado": STATUS_LABELS.get(r["status"], r["status"]),
            "Detalle": r["tool"] if r["status"] == "ok" else (r["msg"] or r["tool"]),
        })
    if not rows:
        rows_df = pd.DataFrame(columns=["Aplicar"])
    edited = rows_df if not rows else st.data_editor(
        pd.DataFrame(rows), hide_index=True, width="stretch", key="opt_table",
        disabled=[c for c in rows[0] if c != "Aplicar"] if rows else True,
        column_config={
            "Aplicar": st.column_config.CheckboxColumn(help="Desmarca para conservar el original"),
            "Original (MB)": st.column_config.NumberColumn(format="%.2f"),
            "Comprimido (MB)": st.column_config.NumberColumn(format="%.2f"),
            "Ahorro (%)": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.1f %%"),
        },
    )
    selected = {src for src, keep in zip(srcs, edited["Aplicar"]) if keep and run["results"][src]["status"] == "ok"}

    if ok:
        st.markdown("#### 🔍 Comparador antes / después")
        c1, c2 = st.columns([3, 1], vertical_alignment="bottom")
        sel = c1.selectbox("Archivo", [r["src"] for r in ok], key="opt_cmp",
                           format_func=lambda s: f"{Path(s).name}  (−{(1 - run['results'][s]['new_size'] / run['results'][s]['orig_size']) * 100:.1f} %)")
        zoom = c2.toggle("🔎 Zoom 100 %", key="opt_zoom", help="Recorte central sin escalar para ver detalles finos")
        r = run["results"][sel]
        src_p, tmp_p = Path(sel), Path(r["tmp"])
        if not (src_p.is_file() and tmp_p.is_file()):
            st.warning("Falta el original o el archivo comprimido.")
        elif r["kind"] == "video":
            dur = comp.video_duration(src_p)
            t = st.slider("Fotograma a comparar (segundos)", 0.0, max(dur - 0.1, 0.1),
                          min(1.0, dur / 2), 0.1, key=f"opt_t_{_wkey(sel)}") if dur > 0.2 else 0.0
            compare_slider(cached_frame(str(src_p), src_p.stat().st_mtime, t),
                           cached_frame(str(tmp_p), tmp_p.stat().st_mtime, t), zoom)
            with st.expander("▶️ Reproducir ambos vídeos"):
                v1, v2 = st.columns(2)
                for col, vp, label in ((v1, src_p, f"Original · {fmt_size(r['orig_size'])}"),
                                       (v2, tmp_p, f"Comprimido · {fmt_size(r['new_size'])}")):
                    prev = comp.preview_path(vp)
                    col.caption(label + (" · vista previa WebM 720p" if prev.exists() else ""))
                    col.video(str(prev if prev.exists() else vp),
                              format="video/webm" if prev.exists() else "video/mp4")
                st.caption("Las vistas previas WebM sirven para comprobar que se reproduce bien; para "
                           "juzgar la calidad usa el comparador de fotogramas de arriba (resolución completa).")
                if st.button("🦊 Generar vistas previas WebM (para Firefox)", key="cmp_preview",
                             disabled=not comp.can_make_preview()):
                    with st.spinner("Generando vistas previas…"):
                        try:
                            comp.make_browser_preview(src_p)
                            comp.make_browser_preview(tmp_p)
                        except Exception as e:  # noqa: BLE001
                            st.error(f"No se pudo generar la vista previa: {e}")
                    st.rerun()
        else:
            compare_slider(src_p.read_bytes(), tmp_p.read_bytes(), zoom)

    saved_sel = sum(run["results"][s]["orig_size"] - run["results"][s]["new_size"] for s in selected)
    b1, b2 = st.columns([2, 1])
    if b1.button(f"✅ Confirmar y aplicar compresión ({len(selected)} archivos · libera {fmt_size(saved_sel)})",
                 type="primary", disabled=not selected, width="stretch"):
        with st.spinner("Reemplazando archivos…"):
            applied, saved, errors = apply_opt_run(proj, run, selected)
        msg = f"Compresión aplicada a {applied} archivos: {fmt_size(saved)} liberados."
        ss.flash = ("warning", msg + " Errores: " + "; ".join(errors)) if errors else ("success", msg)
        st.rerun()
    if b2.button("🗑️ Descartar resultados (dejar todo intacto)", width="stretch"):
        discard_opt_run(proj)
        ss.flash = ("info", "Resultados descartados: no se modificó ningún archivo.")
        st.rerun()


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


VIEW_LABELS = {"similares": "1️⃣ Similares", "revision": "2️⃣ Revisión", "optimizacion": "3️⃣ Optimizar"}


def go_to(view: str) -> None:
    ss["_goto_view"] = view
    st.rerun()


def project_screen() -> None:
    proj = ss.project
    sidebar_project(proj)
    if "_goto_view" in ss:  # cambio de paso pedido por un botón (antes de crear el widget)
        ss.view = ss.pop("_goto_view")
    view = st.sidebar.segmented_control(
        "Flujo de trabajo", list(VIEW_LABELS), default="similares", key="view", required=True,
        format_func=VIEW_LABELS.get, width="stretch",
    ) or "similares"

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
        st.info("No se encontraron fotos ni vídeos en la carpeta de origen con los filtros actuales.")
        return

    if view == "similares":
        similar_view(proj, items)
    elif view == "optimizacion":
        optimization_view(proj)
    else:
        review_view(proj, items)


def review_view(proj: dict, items: list[dict]) -> None:
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
        f"<div style='text-align:center'><b>{'🎬 Vídeo' if is_video(item['path']) else 'Foto'} "
        f"{ss.idx + 1} / {len(items)}</b> · "
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
    if path.exists() and is_video(path):
        video_player(path)
    elif (data := display_bytes(str(path), path.stat().st_mtime) if path.exists() else None):
        _, mid, _ = st.columns([1, 10, 1])
        with mid.container(key="viewer"):
            st.markdown(
                "<style>.st-key-viewer [data-testid='stImage'] img{display:block;margin:auto;max-height:68vh;"
                "width:auto!important;max-width:100%;object-fit:contain;border-radius:6px}</style>",
                unsafe_allow_html=True,
            )
            st.image(data, width="stretch")
    elif path.exists():
        st.error("No se pudo abrir esta imagen (formato no soportado o archivo dañado).")
    else:
        st.error(f"El archivo ya no existe: `{path}`")

    # ---- Anteriores / siguientes ----
    filmstrip(items, ss.idx)

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
            ss.flash = ("success", "🎉 ¡Todo revisado! Si quieres, pasa al paso 3️⃣ Optimizar (opcional).")
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
    project_screen()
