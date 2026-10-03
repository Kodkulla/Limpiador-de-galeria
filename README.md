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

### Instalación en un solo comando

```bash
bash instalar.sh
```

Detecta si usas **Fedora** (`dnf`) o **Debian/Ubuntu** (`apt`), instala los paquetes del
sistema (Python, exiftool, ffmpeg, jpegoptim, optipng, jpegtran y, si existe, oxipng),
prepara el entorno de Python y abre la app en el navegador. Te pedirá tu contraseña
para `sudo`.

Opciones:

- `bash instalar.sh --rpmfusion` (solo Fedora): instala además el **ffmpeg completo de
  RPM Fusion**, con H.264/H.265 (y los códecs con los que Firefox reproduce MP4).
  Sin esta opción se usa el `ffmpeg-free` de Fedora y la optimización de vídeo usa AV1.
- `bash instalar.sh --sin-abrir`: instala todo sin abrir la app.

### Uso diario

```bash
./run.sh
```

`run.sh` comprueba el entorno virtual `.venv/`, instala las librerías de Python si
cambió `requirements.txt` y abre la app (`http://localhost:8501`). Para usar otro
puerto: `PORT=8600 ./run.sh`. Ciérrala con `Ctrl+C` en la terminal.

### Instalación manual (alternativa)

**Fedora** (Python ya incluye `venv`):

```bash
sudo dnf install python3 perl-Image-ExifTool ffmpeg-free jpegoptim optipng libjpeg-turbo-utils
sudo dnf install oxipng   # opcional (si no está, se usa optipng)
# ffmpeg completo con H.264/H.265 (RPM Fusion), opcional:
sudo dnf install https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm
sudo dnf swap ffmpeg-free ffmpeg --allowerasing
```

**Debian / Ubuntu**:

```bash
sudo apt install python3 python3-venv libimage-exiftool-perl ffmpeg jpegoptim optipng libjpeg-turbo-progs
```

Después: `chmod +x run.sh && ./run.sh`.

Sin exiftool la app funciona igual, pero los vídeos aparecen sin fecha ni ubicación
(y quedan fuera si filtras por fechas sin incluir los «sin fecha»). Sin las
herramientas de compresión, solo deja de estar disponible el paso opcional 3️⃣.

## ¿No encuentras tus fotos? Diagnóstico

```bash
python3 diagnostico.py
```

Busca todos tus proyectos (en tu carpeta personal, la carpeta de la app y los discos
externos de `/run/media`), y por cada uno te dice **en qué carpeta exacta** están las
fotos clasificadas, cuántas hay por categoría, cuántas siguen sin clasificar en el origen
y si falta alguna. Además guarda un inventario CSV y una copia de seguridad del registro
en la carpeta del proyecto. **No mueve ni borra nada.**

- `python3 diagnostico.py /ruta/al/disco`: busca también en otra carpeta.
- `python3 diagnostico.py --todo`: busca en todo el sistema. Además de los proyectos,
  lista las carpetas con archivos ya renombrados por la app (`VAC_PLA_…`), aunque no
  tengan registro.
- `python3 diagnostico.py --reparar`: si a una foto clasificada le falta su copia en el
  proyecto pero el original sigue en el origen, la vuelve a copiar (nunca sobrescribe).

Nota: en versiones anteriores, si la carpeta de destino se escribía como ruta relativa
(sin `/` ni `~`), los proyectos se guardaban dentro de la carpeta de la app. Ahora la app
muestra siempre la ruta completa y el botón **📂 Abrir carpeta del proyecto**.

## Uso

Crea un **nuevo proyecto** (nombre, carpeta de origen, categorías iniciales, copiar
o mover y, opcionalmente, un rango de fechas) o **continúa** uno existente (si ya
tienes proyectos, esa pestaña aparece primero, con una tarjeta por proyecto).

**Las carpetas se eligen sin escribir rutas:** el botón **📂 Elegir carpeta…** abre la
ventana de tu escritorio (zenity en GNOME/Fedora, kdialog en KDE; `instalar.sh` instala
zenity). Si la ventana no aparece, puede haber quedado detrás del navegador. Como
alternativa, **🗂️ Explorar aquí** abre un explorador dentro de la app, con accesos
rápidos a tu carpeta personal, Escritorio, Imágenes y discos externos, y un botón para
crear una carpeta nueva. El trabajo
sigue tres pasos, que eliges en la barra lateral («Flujo de trabajo»):

1. **1️⃣ Similares**: primero eliminas clones y ráfagas. Las fotos que conservas
   siguen pendientes; las demás van a `_Descartadas/`. Pulsa «Terminar y pasar a
   revisión» cuando acabes.
2. **2️⃣ Revisión**: cada foto o vídeo se muestra en grande, con fecha de captura,
   nombre, resolución, tamaño, cámara y mapa OpenStreetMap si tiene GPS. Clasifica
   con los botones o el teclado; el progreso se guarda tras cada archivo.
3. **3️⃣ Optimizar** (opcional): compresión sin pérdida de calidad de lo ya organizado.

Durante la revisión, bajo la foto hay una **tira con las 2 anteriores y las 4 siguientes**
(con su estado: ⏳ pendiente, ✅ categoría o 🗑️ descartada). Pulsa cualquiera para saltar a ella.

### Editar o eliminar categorías

En la barra lateral, **✏️ Editar o eliminar categorías**:

- **Renombrar**: sus fotos pasan a la carpeta nueva y se renombran con el nuevo prefijo
  (`VAC_PLA_…` → `VAC_MAR_…`). Conserva su tecla `1`–`9`.
- **Eliminar** (pide confirmación): si tiene fotos, eliges si **vuelven a pendientes**
  para revisarlas de nuevo o pasan a **otra categoría** o a `_Descartadas`. No se borra
  ninguna foto. En modo copiar, «volver a pendientes» quita la copia del proyecto; el
  original sigue en el origen. La carpeta se elimina solo si queda vacía.

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

La búsqueda es automática: en una sola pasada prueba varios niveles de parecido y
etiqueta cada grupo como **🟢 Idénticas** (clones o recompresiones), **🟡 Casi idénticas**
o **🟠 Ráfaga** (parecidas y tomadas con pocos segundos de diferencia). Para evitar falsos
positivos exige que coincidan dos huellas distintas (phash y dhash) y, en las ráfagas,
la cercanía en el tiempo. La **sensibilidad** (Estricta / Normal / Amplia) ajusta todos
los umbrales a la vez.

Ves **un grupo cada vez**, con las fotos en grande, su resolución, tamaño, fecha y
nitidez. La app marca como **⭐ sugerida** la de más resolución; en ráfagas, la más
nítida (descarta las movidas). Tú decides:

| Tecla / botón | Acción |
|---|---|
| `1`–`9` · **⭐ Me quedo con esta** | Conserva esa foto y descarta las demás del grupo |
| **➕ También / ➖ Quitar** y luego `Enter` | Conservar varias del grupo |
| `N` · **No son duplicadas** | Conserva todas; el grupo no vuelve a aparecer |
| `←` / `→` | Grupo anterior / siguiente (saltar sin decidir) |
| **🔍** y `Esc` | Ver una foto en grande y cerrar |

Las descartadas van a `_Descartadas/` y las conservadas quedan **pendientes** para
clasificarlas en la revisión. Los grupos resueltos se recuerdan en el registro del
proyecto: no vuelven a salir aunque cierres la app.

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
reproduce MP4 normales (H.264), usa `bash instalar.sh --rpmfusion`
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
