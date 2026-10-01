# 📸 Limpiador de Galería

Aplicación local con Streamlit para Linux que organiza y limpia fotos y vídeos en una
jerarquía de **Proyectos → Categorías**, con visor tipo slide, metadatos EXIF,
mapa de geolocalización, atajos de teclado y detección de fotos duplicadas o en ráfaga.

```
📁 Fotos_Organizadas/               ← carpeta de destino base
└── 📁 Vacaciones_2025/             ← un proyecto
    ├── 📁 Playa/
    ├── 📁 Familia/
    ├── 📁 Salidas_Comida/
    ├── 📁 _Descartadas/            ← nunca se borra nada del disco
    └── 📄 proyecto_estado.json     ← progreso (para continuar otro día)
```

## Puesta en marcha

Requisitos: Python 3.10+ con `venv`, y `exiftool` para leer la fecha y el GPS de los vídeos:

```bash
sudo apt install python3 python3-venv libimage-exiftool-perl
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

1. **Nuevo proyecto**: nombre, carpeta de origen, categorías iniciales,
   copiar o mover y, si quieres, un rango de fechas (EXIF `DateTimeOriginal`).
   **Continuar proyecto**: elige uno de los existentes en la carpeta de destino.
2. **Revisión**: cada foto se muestra en grande; en la barra lateral ves la fecha
   de captura, el nombre original, la resolución, el tamaño, la cámara y un mapa
   OpenStreetMap si la foto tiene GPS.
3. Clasifica con los botones o el teclado. El progreso se guarda tras cada foto.
   Puedes añadir categorías en cualquier momento desde la barra lateral.

### Vídeos

Se admiten `.mp4`, `.mov`, `.mkv`, `.avi`, `.m4v`, `.3gp` y `.webm`. Se previsualizan
con un reproductor en el visor y se clasifican igual que las fotos. La fecha
(`CreationDate`/`CreateDate`) y el GPS (QuickTime `GPSCoordinates`, de Android o iPhone)
se leen con exiftool, así que el mapa también funciona con vídeos del móvil.
Si el navegador no puede reproducir el formato (AVI, algunos MKV, HEVC/H.265),
usa el botón **Abrir en reproductor**, que abre el archivo con el reproductor del sistema.
Los vídeos de más de 500 MB piden confirmación antes de cargarse en el navegador.

### Limpieza de similares

En la barra lateral, **Vista → 🧬 Similares**:

- Calcula un *hash perceptual* (`phash` o `dhash`) de cada foto pendiente
  (opcionalmente también de las ya clasificadas).
- Con **Tolerancia** (distancia Hamming 0–10) eliges entre clones exactos (0),
  ráfagas (4–6) o fotos simplemente parecidas (8–10).
- Muestra cada grupo lado a lado con resolución, tamaño y fecha, y marca la de
  mejor calidad (mayor resolución y tamaño).
- **Conservar N en «Categoría» y descartar el resto** aplica las casillas marcadas;
  **⭐ Solo esta** resuelve el grupo con un clic. Las demás van a `_Descartadas/`.
- **No son duplicadas** oculta ese grupo para siempre (se guarda en el JSON del proyecto).

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
