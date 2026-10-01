# 📸 Limpiador de Galería

Aplicación local con Streamlit para Linux que organiza y limpia fotos y vídeos en una
jerarquía de **Proyectos → Categorías**, con visor tipo slide, metadatos EXIF,
mapa de geolocalización, atajos de teclado y detección de fotos duplicadas o en ráfaga.

```
📁 Fotos_Organizadas/               ← carpeta de destino base
└── 📁 Vacaciones_2025/             ← un proyecto
    ├── 📁 Playa/                   ← VAC_PLA_20250714_153022.jpg …
    ├── 📁 Familia/
    ├── 📁 Salidas_Comida/
    ├── 📁 _Descartadas/            ← nunca se borra nada del disco
    └── 📄 proyecto_estado.json     ← progreso (para continuar otro día)
```

## Puesta en marcha

Requisitos: Python 3.10+ y `exiftool` (para leer la fecha y el GPS de los vídeos).
Las herramientas de compresión solo hacen falta para el paso opcional de optimización.

**Fedora** (Python ya incluye `venv`):

```bash
sudo dnf install python3 perl-Image-ExifTool
# Para el paso opcional de optimización:
sudo dnf install ffmpeg-free jpegoptim optipng libjpeg-turbo-utils
sudo dnf install oxipng   # opcional (si no está, se usa optipng)
```

El `ffmpeg-free` de Fedora no trae H.264 ni H.265. La app lo detecta y ofrece **AV1
(SVT-AV1)**, que comprime más y Firefox reproduce. Si además quieres H.264/H.265,
instala el ffmpeg completo de RPM Fusion:

```bash
sudo dnf install https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm
sudo dnf swap ffmpeg-free ffmpeg --allowerasing
```

**Debian / Ubuntu**:

```bash
sudo apt install python3 python3-venv libimage-exiftool-perl
sudo apt install ffmpeg jpegoptim optipng libjpeg-turbo-progs   # optimización
```

Sin exiftool la app funciona igual, pero los vídeos aparecen sin fecha ni ubicación
(y quedan fuera si filtras por fechas sin incluir los «sin fecha»).

```bash
chmod +x run.sh   # solo la primera vez
./run.sh
```

`run.sh` crea el entorno virtual `.venv/`, instala las dependencias (solo la
primera vez o cuando cambia `requirements.txt`) y abre la app en el navegador
(`http://localhost:8501`). Para usar otro puerto: `PORT=8600 ./run.sh`.
Ciérrala con `Ctrl+C` en la terminal.

## Uso

Crea un **nuevo proyecto** (nombre, carpeta de origen, categorías iniciales, copiar
o mover y, opcionalmente, un rango de fechas) o **continúa** uno existente. El trabajo
sigue tres pasos, que eliges en la barra lateral («Flujo de trabajo»):

1. **1️⃣ Similares**: primero eliminas clones y ráfagas. Las fotos que conservas
   siguen pendientes; las demás van a `_Descartadas/`. Pulsa «Terminar y pasar a
   revisión» cuando acabes.
2. **2️⃣ Revisión**: cada foto o vídeo se muestra en grande, con fecha de captura,
   nombre, resolución, tamaño, cámara y mapa OpenStreetMap si tiene GPS. Clasifica
   con los botones o el teclado; el progreso se guarda tras cada archivo.
3. **3️⃣ Optimizar** (opcional): compresión sin pérdida de calidad de lo ya organizado.

### Renombrado automático

Al clasificar, cada archivo se renombra como `[PRO]_[CAT]_[FECHA].ext`:

- `PRO` / `CAT`: tres primeras letras del proyecto y de la categoría, en mayúsculas
  (`DSC` para las descartadas).
- `FECHA`: captura EXIF/QuickTime en formato `YYYYMMDD_HHMMSS`; si no hay, la fecha de
  modificación del archivo; y si tampoco, `SINFECHA_YYYYMMDD` (hoy).
- Si ya existe un archivo con el mismo nombre (mismo segundo), se añade `_001`, `_002`…
- La extensión se pasa a minúsculas.

Ejemplo: `IMG_4021.JPG` → `Vacaciones_2025/Playa/VAC_PLA_20250714_153022.jpg`.
Al reclasificar se renombra con la nueva categoría; al deshacer (modo mover) vuelve
a su nombre y ruta originales.

### Vídeos

Se admiten `.mp4`, `.mov`, `.mkv`, `.avi`, `.m4v`, `.3gp` y `.webm`. Se previsualizan
con un reproductor en el visor y se clasifican igual que las fotos. La fecha
(`CreationDate`/`CreateDate`) y el GPS (QuickTime `GPSCoordinates`, de Android o iPhone)
se leen con exiftool, así que el mapa también funciona con vídeos del móvil.
Si el navegador no puede reproducir el formato, tienes dos botones bajo el reproductor:

- **🦊 Vista previa WebM**: crea una copia ligera VP9 a 720p en
  `~/.cache/limpiador-galeria/previews/` y la muestra en su lugar. El original no se toca.
  Puedes borrar esa carpeta cuando quieras.
- **▶️ Abrir en reproductor**: abre el archivo con el reproductor del sistema (`xdg-open`).
Los vídeos de más de 500 MB piden confirmación antes de cargarse en el navegador.

### 1️⃣ Limpieza de similares

- Calcula un *hash perceptual* (`phash` o `dhash`) de cada foto pendiente
  (opcionalmente también de las ya clasificadas).
- Con **Tolerancia** (distancia Hamming 0–10) eliges entre clones exactos (0),
  ráfagas (4–6) o fotos simplemente parecidas (8–10).
- Muestra cada grupo lado a lado con resolución, tamaño y fecha, y marca la de
  mejor calidad (mayor resolución y tamaño).
- **Conservar N y descartar el resto** aplica las casillas marcadas; **⭐ Solo esta**
  resuelve el grupo con un clic. Las descartadas van a `_Descartadas/` y las conservadas
  quedan pendientes para clasificarlas en la revisión.
- **No son duplicadas** oculta ese grupo para siempre (se guarda en el JSON del proyecto).

### 3️⃣ Optimización y compresión (opcional)

Paso final e independiente: no se toca nada salvo que tú lo confirmes. Puedes pulsar
**«Omitir este paso»** y dejar los archivos tal cual.

- **Alcance**: todo el proyecto o solo las categorías que elijas; fotos, vídeos o ambos.
- **JPEG**: *sin pérdida* (jpegoptim/jpegtran reoptimizan la codificación; los píxeles
  quedan idénticos) o *visualmente sin pérdida* (`jpegoptim -m92`: solo recomprime las
  fotos guardadas con más calidad). Opcionalmente quita metadatos redundantes
  (comentarios, XMP, IPTC); el EXIF con fecha, GPS y orientación se conserva siempre.
- **PNG**: oxipng → optipng → Pillow (todos sin pérdida). Opción de convertir
  PNG/BMP/TIFF a **WebP sin pérdida** si pesa menos.
- **Vídeo**: ffmpeg con H.265 (CRF 22) o H.264 (CRF 18), preset ajustable. Conserva
  la pista de audio (la copia; si no se puede, AAC 192k), la rotación, la fecha y el GPS.
  Omite los vídeos HDR y, opcionalmente, los que ya están en HEVC/AV1/VP9.
- **Seguridad**: todo se procesa primero en `[Proyecto]/.optimizacion_tmp/`. Ves una
  tabla con el antes y el después de cada archivo (tamaño y % de ahorro), el espacio
  total inicial, final y liberado, y un **comparador con cortina deslizante** (con zoom
  al 100 % y selección de fotograma en vídeos). Solo al pulsar **«Confirmar y aplicar
  compresión»** se reemplazan los originales (puedes desmarcar archivos); los que no
  reducen peso se conservan siempre. «Descartar resultados» borra la carpeta temporal.
- Si se interrumpe (por ejemplo, al cerrar la pestaña), el progreso queda guardado y
  puedes **continuar** después.

### Firefox

`run.sh` abre la app en tu navegador por defecto (Firefox incluido).
Firefox no reproduce vídeos **HEVC/H.265** (los del iPhone), **AVI** ni **MKV con
H.264**; para esos usa **🦊 Vista previa WebM**. En Fedora, si Firefox tampoco
reproduce MP4 normales (H.264), instala los códecs completos de RPM Fusion (ver arriba)
o usa igualmente la vista previa WebM. Si optimizas vídeos para verlos en Firefox,
elige **AV1** en lugar de H.265.

### Atajos de teclado

| Tecla | Acción |
|-------|--------|
| `←` / `→` | Foto anterior / siguiente |
| `Enter` | Aceptar y clasificar en la categoría seleccionada |
| `1`–`9` | Clasificar directamente en la categoría N |
| `X` | Descartar (a `_Descartadas/`) |
| `Z` | Deshacer: la foto vuelve a quedar pendiente |
| `N` | Ir a la siguiente foto pendiente |
| `Inicio` | Primera foto |

Los atajos no se activan mientras escribes en un campo de texto.

### Notas

- **Copiar** deja intacta la carpeta de origen; **Mover** la va vaciando.
  Deshacer devuelve la foto a su ruta original (modo mover) o borra la copia (modo copiar).
- Puedes volver atrás y reclasificar una foto: el archivo se mueve a la nueva categoría.
- Si dos fotos tienen el mismo nombre, se renombra la segunda (`foto_1.jpg`).
- Si la carpeta de destino está dentro de la de origen, se ignora al escanear.
- El mapa necesita conexión a internet (teselas de OpenStreetMap).
- Fotos HEIC/HEIF de iPhone: `.venv/bin/pip install pillow-heif` y se reconocerán automáticamente.
