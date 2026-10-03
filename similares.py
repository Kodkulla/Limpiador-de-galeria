"""Detección automática de fotos similares (clones y ráfagas).

En una sola pasada se prueban varios umbrales y se clasifica cada grupo:
  0 🟢 Idénticas       – clones o recompresiones de la misma foto
  1 🟡 Casi idénticas  – mismo encuadre con cambios mínimos
  2 🟠 Ráfaga          – parecidas y tomadas con pocos segundos de diferencia
Para reducir falsos positivos se exige que coincidan dos huellas distintas
(phash y dhash) y, en las ráfagas, que estén cerca en el tiempo.
"""

from __future__ import annotations

import imagehash
import numpy as np
from PIL import Image, ImageOps

LEVELS = {
    0: ("🟢", "Idénticas"),
    1: ("🟡", "Casi idénticas"),
    2: ("🟠", "Ráfaga / muy parecidas"),
}

# Umbrales (distancia Hamming sobre hashes de 64 bits) por sensibilidad
SENSITIVITY = {
    "estricta": {"identical": 0, "near": 4, "burst": 8, "burst_dhash": 12, "window": 10},
    "normal": {"identical": 2, "near": 6, "burst": 12, "burst_dhash": 16, "window": 30},
    "amplia": {"identical": 3, "near": 9, "burst": 16, "burst_dhash": 20, "window": 120},
}
SENSITIVITY_LABELS = {"estricta": "Estricta", "normal": "Normal ⭐", "amplia": "Amplia"}


def signature(path: str) -> dict | None:
    """phash, dhash y nitidez de una imagen (una sola decodificación)."""
    try:
        with Image.open(path) as img:
            img.draft("RGB", (512, 512))  # decodificación rápida de JPEG grandes
            img = ImageOps.exif_transpose(img)
            img.thumbnail((512, 512))
            gray = img.convert("L")
            return {
                "phash": str(imagehash.phash(gray)),
                "dhash": str(imagehash.dhash(gray)),
                "sharp": sharpness(gray),
            }
    except Exception:
        return None


def sharpness(gray: Image.Image) -> float:
    """Varianza del laplaciano: más alto = más nítida (sirve para elegir en ráfagas)."""
    g = np.asarray(gray, dtype=np.float32)
    if g.shape[0] < 3 or g.shape[1] < 3:
        return 0.0
    lap = -4 * g[1:-1, 1:-1] + g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:]
    return float(lap.var())


def _popcount(arr: np.ndarray) -> np.ndarray:
    if hasattr(np, "bitwise_count"):
        return np.bitwise_count(arr)
    return np.unpackbits(arr.view(np.uint8)).reshape(-1, 64).sum(axis=1)


def find_groups(phashes: list[str], dhashes: list[str], times: list[float | None],
                params: dict) -> list[dict]:
    """Agrupa índices similares. Devuelve [{"members": [i…], "level": 0|1|2, "dist": int}]."""
    n = len(phashes)
    if n < 2:
        return []
    ph = np.array([int(h, 16) for h in phashes], dtype=np.uint64)
    dh = np.array([int(h, 16) for h in dhashes], dtype=np.uint64)
    t = np.array([np.nan if x is None else x for x in times], dtype=np.float64)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    edges = []
    p = params
    for i in range(n - 1):
        dp = _popcount(ph[i + 1:] ^ ph[i])
        dd = _popcount(dh[i + 1:] ^ dh[i])
        l0 = (dp <= p["identical"]) & (dd <= p["identical"] + 2)
        l1 = (dp <= p["near"]) & (dd <= p["near"] + 4)
        with np.errstate(invalid="ignore"):
            close = np.abs(t[i + 1:] - t[i]) <= p["window"]  # NaN (sin fecha) -> False
        l2 = (dp <= p["burst"]) & (dd <= p["burst_dhash"]) & close
        for j in np.nonzero(l0 | l1 | l2)[0]:
            k = i + 1 + int(j)
            level = 0 if l0[j] else (1 if l1[j] else 2)
            edges.append((i, k, level, int(dp[j])))
            ri, rk = find(i), find(k)
            if ri != rk:
                parent[rk] = ri

    groups: dict[int, dict] = {}
    for i in range(n):
        groups.setdefault(find(i), {"members": [], "level": 0, "dist": 0})["members"].append(i)
    for i, k, level, dist in edges:
        g = groups[find(i)]
        g["level"] = max(g["level"], level)  # el grupo se etiqueta por su enlace más flojo
        g["dist"] = max(g["dist"], dist)
    return [g for g in groups.values() if len(g["members"]) > 1]


def best_member(members: list[dict]) -> int:
    """Índice sugerido para conservar.

    1. Solo compiten las de mayor resolución (≥ 90 % de la mayor).
    2. Si la nitidez difiere claramente (> 30 %, típico de fotos movidas en una ráfaga),
       gana la más nítida.
    3. Si no, gana la más pesada (menos comprimida). La nitidez no decide aquí porque
       los artefactos de compresión la inflan.
    """
    max_mp = max(m["mp"] for m in members) or 1
    cands = [i for i, m in enumerate(members) if m["mp"] >= 0.9 * max_mp]
    sharp = [members[i]["sharp"] for i in cands]
    if min(sharp) > 0 and max(sharp) / min(sharp) > 1.3:
        return max(cands, key=lambda i: (members[i]["sharp"], members[i]["size"]))
    return max(cands, key=lambda i: (members[i]["size"], members[i]["sharp"]))
