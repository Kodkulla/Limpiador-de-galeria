"""Motores de optimización sin pérdida / visualmente sin pérdida.

Cada función escribe el resultado en un archivo temporal y nunca toca el
original: reemplazarlo es responsabilidad de quien llama (tras confirmar).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image

TOOL_NAMES = ("jpegoptim", "jpegtran", "oxipng", "optipng", "ffmpeg", "ffprobe", "exiftool")
TOOLS = {name: shutil.which(name) for name in TOOL_NAMES}


# --------------------------------------------------------------------------
# Distribución: comandos de instalación (Fedora/dnf o Debian-Ubuntu/apt)
# --------------------------------------------------------------------------
def _os_release() -> dict:
    try:
        lines = Path("/etc/os-release").read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    return dict(ln.split("=", 1) for ln in lines if "=" in ln)


_OS = _os_release()
_OS_IDS = {_OS.get("ID", "").strip('"')} | set(_OS.get("ID_LIKE", "").strip('"').split())
IS_FEDORA = bool(_OS_IDS & {"fedora", "rhel", "centos"}) or (shutil.which("dnf") is not None
                                                              and shutil.which("apt-get") is None)
PACKAGES = {  # herramienta -> (paquete apt, paquete dnf)
    "exiftool": ("libimage-exiftool-perl", "perl-Image-ExifTool"),
    "jpegoptim": ("jpegoptim", "jpegoptim"),
    "jpegtran": ("libjpeg-turbo-progs", "libjpeg-turbo-utils"),
    "optipng": ("optipng", "optipng"),
    "oxipng": ("oxipng", "oxipng"),
    "ffmpeg": ("ffmpeg", "ffmpeg-free"),
}


def install_cmd(*tools: str) -> str:
    """`sudo dnf install …` o `sudo apt install …` según la distribución."""
    pkgs = list(dict.fromkeys(PACKAGES[t][1 if IS_FEDORA else 0] for t in tools))
    return f"sudo {'dnf' if IS_FEDORA else 'apt'} install {' '.join(pkgs)}"


# Fedora trae «ffmpeg-free», sin H.264/H.265: la versión completa está en RPM Fusion
RPMFUSION_CMD = (
    "sudo dnf install https://mirrors.rpmfusion.org/free/fedora/"
    "rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm\n"
    "sudo dnf swap ffmpeg-free ffmpeg --allowerasing"
)


# --------------------------------------------------------------------------
# Códecs de vídeo disponibles en el ffmpeg instalado
# --------------------------------------------------------------------------
def _ffmpeg_encoders() -> set[str]:
    if not TOOLS["ffmpeg"]:
        return set()
    try:
        res = subprocess.run([TOOLS["ffmpeg"], "-hide_banner", "-encoders"],
                             capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return set()
    names = set()
    for line in res.stdout.decode(errors="replace").splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAS":
            names.add(parts[1])
    return names


ENCODERS = _ffmpeg_encoders()

# códec -> etiqueta, CRF por defecto, rango de CRF, presets (rápido/medio/lento)
VIDEO_CODECS = {
    "libx265": ("H.265 / HEVC (más compresión; Firefox no lo reproduce)", 22, (14, 30),
                {"fast": "fast", "medium": "medium", "slow": "slow"}),
    "libsvtav1": ("AV1 / SVT-AV1 (máxima compresión; Firefox lo reproduce)", 28, (18, 40),
                  {"fast": "10", "medium": "8", "slow": "6"}),
    "libx264": ("H.264 / AVC (máxima compatibilidad)", 18, (14, 28),
                {"fast": "fast", "medium": "medium", "slow": "slow"}),
}


def available_video_codecs() -> list[str]:
    return [c for c in VIDEO_CODECS if c in ENCODERS]

JPEG_EXTS = {".jpg", ".jpeg"}
PNG_EXTS = {".png"}
WEBP_SOURCE_EXTS = {".png", ".bmp", ".tif", ".tiff"}  # candidatas a WebP sin pérdida
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".3gp", ".webm"}
VIDEO_KEEP_CONTAINER = {".mp4", ".mov", ".m4v", ".mkv"}  # el resto se reempaqueta en .mp4
HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}


@dataclass
class Options:
    jpeg_mode: str = "lossless"  # "lossless" | "visual"
    jpeg_quality: int = 92  # solo en modo "visual"
    strip_redundant: bool = True  # comentarios, XMP, IPTC (EXIF con fecha/GPS se conserva)
    png_to_webp: bool = False
    video_codec: str = "libx265"  # "libx265" | "libsvtav1" | "libx264"
    video_crf: int = 22
    video_preset: str = "medium"
    skip_hevc: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


def is_compressible(path: Path) -> bool:
    ext = path.suffix.lower()
    return ext in JPEG_EXTS | WEBP_SOURCE_EXTS | VIDEO_EXTS


def media_kind(path: Path) -> str:
    return "video" if path.suffix.lower() in VIDEO_EXTS else "foto"


def _run(cmd: list[str], stdout=None, timeout: float | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, stdout=stdout if stdout is not None else subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=timeout, check=False,
    )


def _err(res: subprocess.CompletedProcess) -> str:
    lines = res.stderr.decode("utf-8", errors="replace").strip().splitlines()
    return lines[-1] if lines else f"código {res.returncode}"


# --------------------------------------------------------------------------
# Fotos
# --------------------------------------------------------------------------
def _jpeg(src: Path, out: Path, opts: Options) -> str:
    strip = ["--strip-com", "--strip-iptc", "--strip-xmp"] if opts.strip_redundant else ["--strip-none"]
    if TOOLS["jpegoptim"]:
        # --force: escribe siempre la salida (si no reduce, se detecta después como "sin_ahorro")
        cmd = [TOOLS["jpegoptim"], "--quiet", "--force", "--stdout", "--all-progressive", *strip]
        if opts.jpeg_mode == "visual":
            cmd.append(f"-m{opts.jpeg_quality}")
        with open(out, "wb") as fh:
            res = _run([*cmd, str(src)], stdout=fh, timeout=300)
        if res.returncode != 0 or out.stat().st_size == 0:
            raise RuntimeError(f"jpegoptim: {_err(res)}")
        return "jpegoptim" + (f" -m{opts.jpeg_quality}" if opts.jpeg_mode == "visual" else "")
    if opts.jpeg_mode == "lossless" and TOOLS["jpegtran"]:
        res = _run([TOOLS["jpegtran"], "-copy", "all", "-optimize", "-progressive",
                    "-outfile", str(out), str(src)], timeout=300)
        if res.returncode != 0:
            raise RuntimeError(f"jpegtran: {_err(res)}")
        return "jpegtran"
    if opts.jpeg_mode == "visual":  # Pillow como último recurso (solo modo visual)
        with Image.open(src) as img:
            params = {k: img.info[k] for k in ("exif", "icc_profile") if img.info.get(k)}
            img.save(out, "JPEG", quality=opts.jpeg_quality, optimize=True, progressive=True,
                     subsampling="keep", **params)
        return f"Pillow q{opts.jpeg_quality}"
    raise RuntimeError(f"falta jpegoptim o jpegtran ({install_cmd('jpegoptim')})")


def _png(src: Path, out: Path, opts: Options) -> str:
    if TOOLS["oxipng"]:
        cmd = [TOOLS["oxipng"], "-q", "-o", "4"]
        if opts.strip_redundant:
            cmd += ["--strip", "safe"]
        res = _run([*cmd, "--out", str(out), str(src)], timeout=600)
        if res.returncode != 0:
            raise RuntimeError(f"oxipng: {_err(res)}")
        return "oxipng"
    if TOOLS["optipng"]:
        res = _run([TOOLS["optipng"], "-quiet", "-o2", "-preserve", "-out", str(out), str(src)], timeout=600)
        if res.returncode != 0:
            raise RuntimeError(f"optipng: {_err(res)}")
        return "optipng"
    with Image.open(src) as img:  # Pillow: recompresión zlib, también sin pérdida
        params = {k: img.info[k] for k in ("exif", "icc_profile") if img.info.get(k)}
        img.save(out, "PNG", optimize=True, **params)
    return "Pillow (PNG optimize)"


def _webp_lossless(src: Path, out: Path) -> str:
    with Image.open(src) as img:
        if getattr(img, "n_frames", 1) > 1:
            raise RuntimeError("imagen multipágina/animada")
        params = {k: img.info[k] for k in ("exif", "icc_profile") if img.info.get(k)}
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA" if "A" in img.getbands() or img.mode == "P" else "RGB")
        img.save(out, "WEBP", lossless=True, quality=100, method=6, **params)
    return "WebP sin pérdida (Pillow)"


# --------------------------------------------------------------------------
# Vídeos
# --------------------------------------------------------------------------
def probe_video(path: Path) -> dict:
    """Códec, rotación y función de transferencia (HDR) del primer stream de vídeo."""
    if not TOOLS["ffprobe"]:
        return {}
    res = _run([
        TOOLS["ffprobe"], "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,width,height,color_transfer:stream_side_data=rotation",
        "-of", "json", str(path),
    ], timeout=60)
    try:
        stream = json.loads(res.stdout or b"{}").get("streams", [{}])[0]
    except (ValueError, IndexError):
        return {}
    rotation = 0
    for sd in stream.get("side_data_list", []) or []:
        if "rotation" in sd:
            rotation = int(sd["rotation"]) % 360
    return {
        "codec": stream.get("codec_name"),
        "width": stream.get("width"),
        "height": stream.get("height"),
        "transfer": stream.get("color_transfer"),
        "rotation": rotation,
    }


class Skip(Exception):
    """El archivo se omite a propósito (no es un error)."""


def _video(src: Path, out: Path, opts: Options) -> str:
    if not TOOLS["ffmpeg"]:
        raise RuntimeError(f"falta ffmpeg ({install_cmd('ffmpeg')})")
    if opts.video_codec not in ENCODERS:
        raise RuntimeError(f"este ffmpeg no incluye el codificador {opts.video_codec}")
    info = probe_video(src)
    if info.get("transfer") in HDR_TRANSFERS:
        raise Skip("vídeo HDR: se deja intacto para no perder el rango dinámico")
    if opts.skip_hevc and info.get("codec") in ("hevc", "av1", "vp9"):
        raise Skip(f"ya está en {info['codec'].upper()} (códec eficiente)")

    preset = VIDEO_CODECS[opts.video_codec][3].get(opts.video_preset, opts.video_preset)
    codec_args = ["-c:v", opts.video_codec, "-crf", str(opts.video_crf), "-preset", preset]
    if opts.video_codec == "libx265":
        codec_args += ["-x265-params", "log-level=error"]
        if out.suffix in (".mp4", ".mov", ".m4v"):
            codec_args += ["-tag:v", "hvc1"]  # compatibilidad con Apple/QuickTime
    mux_args = ["-movflags", "+faststart+use_metadata_tags"] if out.suffix in (".mp4", ".mov", ".m4v") else []

    def encode(autorotate: bool, audio: list[str]) -> subprocess.CompletedProcess:
        cmd = [TOOLS["ffmpeg"], "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
        if not autorotate:
            cmd.append("-noautorotate")  # conserva la etiqueta de rotación original
        cmd += ["-i", str(src), "-map", "0:v:0", "-map", "0:a?", *codec_args, *audio,
                "-map_metadata", "0", *mux_args, str(out)]
        return _run(cmd)

    # Audio: copiarlo tal cual; si no se puede (o cambia el contenedor), AAC y, si este
    # ffmpeg no trae AAC, Opus.
    audio_options = []
    if out.suffix == src.suffix.lower():
        audio_options.append((["-c:a", "copy"], "audio copiado"))
    if "aac" in ENCODERS:
        audio_options.append((["-c:a", "aac", "-b:a", "192k"], "audio AAC 192k"))
    if "libopus" in ENCODERS:
        audio_options.append((["-c:a", "libopus", "-b:a", "160k"], "audio Opus 160k"))
    if not audio_options:
        audio_options.append((["-c:a", "copy"], "audio copiado"))
    res = None
    for audio, audio_note in audio_options:
        res = encode(False, audio)
        if res.returncode == 0:
            break
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg: {_err(res)}")

    # Verificar rotación: si esta versión de ffmpeg perdió la etiqueta, se gira la imagen
    out_info = probe_video(out)
    if info and out_info and out_info.get("rotation") != info.get("rotation"):
        res = encode(True, audio)
        if res.returncode != 0:
            raise RuntimeError(f"ffmpeg: {_err(res)}")

    _copy_video_tags(src, out)
    return f"ffmpeg {opts.video_codec} CRF {opts.video_crf} ({audio_note})"


def _copy_video_tags(src: Path, out: Path) -> None:
    """Refuerza fecha y GPS (QuickTime Keys/UserData) con exiftool si está disponible."""
    if not TOOLS["exiftool"]:
        return
    _run([
        TOOLS["exiftool"], "-q", "-q", "-overwrite_original", "-tagsFromFile", str(src),
        "-QuickTime:CreateDate", "-QuickTime:ModifyDate", "-Keys:All", "-UserData:GPSCoordinates",
        "-UserData:Make", "-UserData:Model", str(out),
    ], timeout=120)


# --------------------------------------------------------------------------
# API pública
# --------------------------------------------------------------------------
def compress_file(src: Path, tmp_dir: Path, opts: Options, tag: str) -> dict:
    """Comprime `src` en `tmp_dir`. Devuelve un dict con el resultado.

    status: "ok" (más pequeño), "sin_ahorro", "omitido" o "error".
    """
    tmp_dir.mkdir(parents=True, exist_ok=True)
    ext = src.suffix.lower()
    orig = src.stat()
    result = {
        "src": str(src), "orig_size": orig.st_size, "orig_mtime": orig.st_mtime,
        "kind": media_kind(src), "tmp": None, "new_size": orig.st_size,
        "new_ext": ext, "tool": "", "status": "error", "msg": "",
    }
    candidates: list[tuple[Path, str]] = []
    try:
        if ext in JPEG_EXTS:
            out = tmp_dir / f"{tag}{ext}"
            candidates.append((out, _jpeg(src, out, opts)))
        elif ext in WEBP_SOURCE_EXTS:
            if ext in PNG_EXTS:
                out = tmp_dir / f"{tag}{ext}"
                candidates.append((out, _png(src, out, opts)))
            if opts.png_to_webp:
                out = tmp_dir / f"{tag}.webp"
                try:
                    candidates.append((out, _webp_lossless(src, out)))
                except Exception as e:  # noqa: BLE001
                    if not candidates:
                        raise
                    result["msg"] = f"WebP: {e}"
            if not candidates:
                raise Skip("activa «Convertir a WebP sin pérdida» para este formato")
        elif ext in VIDEO_EXTS:
            keep = VIDEO_KEEP_CONTAINER - ({".mov"} if opts.video_codec == "libsvtav1" else set())
            out = tmp_dir / f"{tag}{ext if ext in keep else '.mp4'}"
            candidates.append((out, _video(src, out, opts)))
        else:
            raise Skip("formato no soportado")
    except Skip as e:
        result.update(status="omitido", msg=str(e))
    except Exception as e:  # noqa: BLE001
        result.update(status="error", msg=str(e))

    # Elegir el candidato más pequeño; borrar el resto
    valid = [(p, t) for p, t in candidates if p.exists() and p.stat().st_size > 0]
    best = min(valid, key=lambda c: c[0].stat().st_size, default=None)
    for p, _ in candidates:
        if p.exists() and (best is None or p != best[0]):
            p.unlink()
    if best is None:
        return result
    size = best[0].stat().st_size
    result.update(tool=best[1], new_size=size, new_ext=best[0].suffix.lower())
    if size < orig.st_size:
        result.update(status="ok", tmp=str(best[0]))
    else:
        best[0].unlink()
        result.update(status="sin_ahorro", new_size=orig.st_size, new_ext=ext,
                      msg="no reduce peso: se conserva el original")
    return result


def extract_frame(path: Path, seconds: float) -> bytes | None:
    """Fotograma PNG de un vídeo (con la rotación aplicada) para comparar."""
    if not TOOLS["ffmpeg"]:
        return None
    res = _run([
        TOOLS["ffmpeg"], "-hide_banner", "-loglevel", "error", "-nostdin", "-ss", f"{seconds:.2f}",
        "-i", str(path), "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-",
    ], timeout=120)
    return res.stdout or None


def video_duration(path: Path) -> float:
    if not TOOLS["ffprobe"]:
        return 0.0
    res = _run([TOOLS["ffprobe"], "-v", "error", "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1", str(path)], timeout=60)
    try:
        return float(res.stdout.decode().strip())
    except ValueError:
        return 0.0


# --------------------------------------------------------------------------
# Vista previa para el navegador (Firefox no reproduce HEVC, AVI ni MKV H.264)
# --------------------------------------------------------------------------
PREVIEW_DIR = Path.home() / ".cache" / "limpiador-galeria" / "previews"


def preview_path(src: Path) -> Path:
    stt = src.stat()
    key = hashlib.md5(f"{src.resolve()}|{stt.st_size}|{stt.st_mtime}".encode()).hexdigest()
    return PREVIEW_DIR / f"{key}.webm"


def can_make_preview() -> bool:
    return "libvpx-vp9" in ENCODERS


def make_browser_preview(src: Path) -> Path:
    """Crea (una vez) una copia WebM/VP9 a 720p, reproducible en cualquier navegador."""
    out = preview_path(src)
    if out.exists():
        return out
    if not can_make_preview():
        raise RuntimeError("este ffmpeg no incluye el codificador VP9 (libvpx-vp9)")
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    part = out.with_suffix(".part.webm")
    audio = ["-c:a", "libopus", "-b:a", "96k"] if "libopus" in ENCODERS else ["-an"]
    res = _run([
        TOOLS["ffmpeg"], "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(src),
        "-map", "0:v:0", "-map", "0:a?", "-vf", "scale=-2:'min(720,ih)'",
        "-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-row-mt", "1",
        "-b:v", "2M", *audio, str(part),
    ])
    if res.returncode != 0:
        part.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg: {_err(res)}")
    part.rename(out)
    return out
