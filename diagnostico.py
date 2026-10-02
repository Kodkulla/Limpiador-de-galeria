#!/usr/bin/env python3
"""Diagnóstico del Limpiador de Galería: ¿dónde están mis fotos?

Busca todos los proyectos (proyecto_estado.json), comprueba cada archivo clasificado
y genera un inventario CSV. Por defecto SOLO LEE: no mueve, no renombra y no borra nada.

Uso:
    python3 diagnostico.py                 # busca en tu carpeta personal y discos externos
    python3 diagnostico.py /ruta/extra     # además, busca en otras carpetas
    python3 diagnostico.py --reparar       # vuelve a COPIAR al destino los archivos que falten
                                           # y cuyo original siga existiendo (nunca sobrescribe)
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
STATE_FILE = "proyecto_estado.json"
CONFIG = Path.home() / ".config" / "limpiador-galeria" / "config.json"
MEDIA_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".bmp", ".gif", ".heic", ".heif",
              ".mp4", ".mov", ".mkv", ".avi", ".m4v", ".3gp", ".webm"}
SKIP_DIRS = {".cache", ".local", ".var", ".mozilla", ".git", ".venv", "node_modules", "__pycache__"}


def resolve(p: str | Path) -> Path:
    """Las rutas relativas se guardaron respecto a la carpeta de la app."""
    p = Path(p).expanduser()
    return p if p.is_absolute() else (APP_DIR / p)


def find_projects(roots: list[Path]) -> list[Path]:
    found = set()
    for root in roots:
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            if STATE_FILE in filenames:
                found.add(Path(dirpath).resolve())
    return sorted(found)


def scan_pending(source: Path, decided: set[str], exclude: Path, recursive: bool) -> int:
    if not source.is_dir():
        return -1
    n = 0
    walker = source.rglob("*") if recursive else source.glob("*")
    for p in walker:
        try:
            if p.suffix.lower() not in MEDIA_EXTS or not p.is_file():
                continue
            rp = p.resolve()
        except OSError:
            continue
        if rp == exclude or exclude in rp.parents:
            continue
        if str(rp) not in decided:
            n += 1
    return n


def check_project(pdir: Path, repair: bool) -> None:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    state_path = pdir / STATE_FILE
    try:
        proj = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"\n❌ No se pudo leer {state_path}: {e}")
        return

    # Copia de seguridad del estado (archivo nuevo; no toca el original)
    backup = pdir / f"proyecto_estado.backup_{stamp}.json"
    shutil.copy2(state_path, backup)

    decisions = proj.get("decisions", {})
    mode = proj.get("mode", "copiar")
    source = resolve(proj.get("source", ""))
    cats = Counter(d.get("category", "?") for d in decisions.values())

    rows, ok, missing_recoverable, lost = [], 0, [], []
    for original, d in decisions.items():
        dest = resolve(d.get("dest", ""))
        orig_p = Path(original)
        dest_ok = dest.is_file()
        orig_ok = orig_p.is_file()
        if dest_ok:
            ok += 1
            estado = "en destino"
        elif orig_ok:
            missing_recoverable.append((orig_p, dest))
            estado = "falta en destino (el original existe)"
        else:
            lost.append((orig_p, dest))
            estado = "NO ENCONTRADO"
        rows.append({
            "categoria": d.get("category", ""), "estado": estado,
            "ruta_destino": str(dest), "archivo_original": str(orig_p),
            "original_aun_existe": "sí" if orig_ok else "no",
            "fecha_clasificacion": d.get("at", ""), "modo": d.get("mode", mode),
        })

    # Archivos dentro del proyecto que el JSON no menciona
    known = {str(resolve(d.get("dest", ""))) for d in decisions.values()}
    orphans = [p for p in pdir.rglob("*")
               if p.is_file() and p.suffix.lower() in MEDIA_EXTS and str(p) not in known
               and ".optimizacion_tmp" not in p.parts]

    pending = scan_pending(source, set(decisions), pdir.parent.resolve(), proj.get("recursive", True))

    inventory = pdir / f"inventario_{stamp}.csv"
    with open(inventory, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["estado"])
        w.writeheader()
        w.writerows(rows)

    print(f"\n{'=' * 70}\n📁 Proyecto: {pdir.name}")
    print(f"   Carpeta del proyecto (aquí están las fotos clasificadas):\n     {pdir}")
    print(f"   Origen: {source}   ·   Modo: {mode}")
    print(f"   Clasificadas según el registro: {len(decisions)}")
    for cat, n in sorted(cats.items()):
        print(f"     - {cat}: {n}  →  {pdir / cat}")
    print(f"   ✅ Presentes en la carpeta del proyecto: {ok}")
    if missing_recoverable:
        print(f"   ⚠️  Faltan en el proyecto, pero el original sigue en el origen: {len(missing_recoverable)}"
              + ("" if repair else "  (recuperables con --reparar)"))
    if lost:
        print(f"   ❌ No encontradas ni en el proyecto ni en el origen: {len(lost)} (ver CSV)")
    if orphans:
        print(f"   ℹ️  Archivos en la carpeta del proyecto que no están en el registro: {len(orphans)}")
    if pending > 0:
        print(f"   ⏳ Fotos/vídeos del origen todavía SIN CLASIFICAR: {pending}"
              "\n      (las que conservas en «Similares» siguen aquí hasta clasificarlas en «Revisión»)")
    elif pending < 0:
        print("   ⚠️  La carpeta de origen ya no existe o no es accesible.")
    if (pdir / ".optimizacion_tmp").is_dir():
        print("   ℹ️  Hay una optimización sin confirmar (.optimizacion_tmp): tus originales no se tocaron.")
    print(f"   📄 Inventario completo: {inventory}")
    print(f"   💾 Copia de seguridad del registro: {backup.name}")

    if repair and missing_recoverable:
        copied = 0
        for orig_p, dest in missing_recoverable:
            if dest.exists():
                continue  # nunca sobrescribir
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(orig_p, dest)
            copied += 1
        print(f"   🔧 Reparado: {copied} archivos copiados de nuevo al proyecto (los originales siguen intactos).")


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    repair = "--reparar" in sys.argv
    if "-h" in sys.argv or "--help" in sys.argv:
        print(__doc__)
        return

    print("🔎 Limpiador de Galería · diagnóstico (solo lectura" + (", con --reparar)" if repair else ")"))
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        print(f"   Último destino usado en la app: {resolve(cfg.get('last_base', '?'))}")
        print(f"   Último proyecto: {cfg.get('last_project', '?')} · último origen: {cfg.get('last_source', '?')}")
    except (OSError, ValueError):
        cfg = {}
        print("   (No hay configuración guardada de la app.)")

    roots = [Path.home(), APP_DIR, Path("/media"), Path("/mnt"), Path("/run/media")]
    roots += [Path(a).expanduser() for a in args]
    if cfg.get("last_base"):
        roots.append(resolve(cfg["last_base"]))
    print("   Buscando proyectos (puede tardar un poco)…")
    projects = find_projects(roots)
    if not projects:
        print("\n❌ No se encontró ningún proyecto (proyecto_estado.json)."
              "\n   Si guardaste en otro disco, pásalo como argumento: python3 diagnostico.py /ruta/al/disco")
        return
    for pdir in projects:
        check_project(pdir, repair)
    print(f"\n{'=' * 70}\nNo se ha borrado ni movido nada. Puedes abrir las carpetas indicadas arriba.")


if __name__ == "__main__":
    main()
