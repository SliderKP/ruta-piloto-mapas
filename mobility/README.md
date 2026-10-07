# Datos y provisionado de movilidad

Esta carpeta prepara la integración de modos de viaje con procedencia y vigencia
comprobables. El bundle histórico del README raíz sigue siendo **OSRM auto**;
`manifest.json` y sus hashes no cambian. Agregar infraestructura o un validador
no convierte ese bundle en mapas de bici, moto o transporte público.

`providers.example.json` describe fuentes, contratos y artefactos pendientes.
Es un ejemplo público sin secretos, endpoints operativos ni disponibilidad
simulada. No contiene cuentas o recorridos de la aplicación.

## Fuentes y disponibilidad comprobada

| Fuente | Uso previsto | Estado al consultar el 7/10/2026 |
| --- | --- | --- |
| OpenStreetMap/Geofabrik | Calles, sentidos, acceso por modo, POIs y enlaces a pie | Fuente cartográfica comunitaria; requiere recorte/importación y validación de cada generación |
| BA Data: API Transporte Público | GTFS/posiciones/actualizaciones de colectivos, trenes y subtes | El publicador anuncia suspensión de datasets API y GTFS; no habilitar como fuente vigente |

La ficha oficial de [BA Data](https://data.buenosaires.gob.ar/es/dataset/api-transporte-publico),
actualizada el **17/6/2026**, comunica que está revisando esos servicios y
anunciará su reanudación. Por eso no se descargó ni presentó un archivo histórico
como horario actual. La ficha declara CC-BY-2.5-AR; cada nueva fuente y descarga
debe conservar términos efectivos, URL pública, fecha de consulta y hash.
La fuente vial es [Geofabrik Argentina](https://download.geofabrik.de/south-america/argentina.html),
con [atribución y licencia OSM](https://www.openstreetmap.org/copyright).

## Admisión de GTFS local

`scripts/validate_gtfs.py` valida un ZIP local con Python 3.10 o posterior.
Utiliza sólo librería estándar; `zoneinfo` necesita una base IANA del sistema.
En Windows, si falta, usar un entorno aislado con `tzdata` o configurar
`PYTHONTZPATH` a datos IANA existentes. Una zona desconocida o una base ausente
rechazan el feed; nunca asumir UTC para horarios argentinos.

```powershell
python scripts/validate_gtfs.py mobility/incoming/operator.gtfs.zip `
  --source-url https://operator.example/public/operator.gtfs.zip `
  --license CC-BY-4.0 `
  --consulted-at 2026-10-07T15:00:00Z `
  --as-of 2026-10-07 `
  --output .outputs/operator-validation.json
```

La URL del comando es un **ejemplo**, no una descarga disponible. Usar URL y
licencia verificadas del operador. El script no hace red ni extrae archivos.
`--as-of` es la fecha de servicio a admitir; por defecto usa hoy UTC. Si se
prepara cerca de medianoche, fijar explícitamente la fecha del servicio local.
`--consulted-at` exige offset/zona, y el reporte lo conserva en UTC. La licencia
es declarada por el operador del proceso; registrar un texto no acredita
permisos de redistribución que la fuente no otorgue.

Verifica:

- ZIP en raíz, sin rutas, miembros duplicados/ambiguos, cifrado ni symlinks;
  límites de 128 archivos, 256 MiB por archivo/comprimido, 768 MiB total y
  expansión máxima 200:1. No descomprime sobre el filesystem.
- Tablas de agencias, paradas, rutas, viajes y horarios; `calendar.txt` o
  `calendar_dates.txt`. CSV UTF-8/BOM, columnas, claves simples y compuestas,
  referencias de agencias/rutas/viajes/paradas/servicios y padres de paradas.
- Zona IANA de agencia, coordenadas finitas dentro de un rectángulo argentino
  conservador y bbox real de los puntos. El rectángulo no es una frontera
  administrativa ni demuestra cobertura nacional.
- Tipos GTFS básicos 0..7, 11 y 12, sin convertir tipos desconocidos en bus o
  tren. Llegadas/salidas/secuencia coherentes, dos paradas como mínimo por viaje,
  primera/última con horario; permite interpolación intermedia explícita y
  horas posteriores a 24 como `25:30:00`.
- Fechas, días de semana, altas/bajas de `calendar_dates` y `feed_info`. Una
  excepción que elimina servicio no cuenta como vigencia; el último día
  efectivo determina el horizonte. Rechaza feeds históricos, futuros o sin
  viajes con servicio en `--as-of`.

La lectura CSV es incremental y las claves/horarios se comprueban en SQLite
temporal, con caché de 8 MiB y cierre antes de limpiar el staging. Se admiten
hasta tres millones de filas por tabla y campos de hasta 8.192 caracteres.
El reporte JSON incluye `schema_version`, `status`, `ready_for_graph`, hash y
tamaño del ZIP, procedencia, cobertura/modos, zonas, conteos y horizonte.
`coverage.modes` incluye sólo modos con viajes activos en la fecha; los demás
modos presentes en el calendario quedan en `scheduled_modes`, sin anunciar un
tren futuro como servicio ferroviario disponible hoy.
`status=available` significa que pasó esta admisión para la fecha solicitada;
no acredita vehículos circulando, tarifas, puntualidad, cobertura futura ni
que OTP haya cargado el grafo. El CLI sale **0** al admitir y **2** al rechazar,
con `errors[].code` y mensaje legible. No reemplaza el ZIP fuente por el reporte.

Es un validador acotado de feeds de **paradas fijas**, no una certificación
completa de extensiones GTFS Flex, tarifas, formas o todos los `route_type`
extendidos. Antes de publicar complementar con el
[validador canónico MobilityData](https://github.com/MobilityData/gtfs-validator),
carga real del grafo y consultas representativas. El contrato básico deriva de
la [especificación GTFS Schedule](https://gtfs.org/documentation/schedule/reference/).

## Grafo de transporte público separado

OpenTripPlanner integra GTFS y OSM. La integración de la aplicación apunta al
contrato [GTFS GraphQL de OTP 2.8](https://docs.opentripplanner.org/en/v2.8.0/apis/GTFS-GraphQL-API/),
ruta `/otp/gtfs/v1`, configurada únicamente en servidor mediante
`OTP_GTFS_GRAPHQL_URL`. Los IDs de aplicación son `public_transport` y `train`;
`train` requiere servicio ferroviario real y no se rellena con bus.
Confirmar versión/schema de la instancia antes de
habilitarla; aquí no se instaló una imagen ni se declaró un servicio operativo.

Preparación reproducible:

1. Obtener un GTFS **actual y autorizado**, validarlo para la fecha de servicio
   y conservar ZIP, reporte, licencia, atribución y hash. Comprobar la vigencia
   otra vez antes de activar una generación; un reporte anterior no evita que
   el calendario expire.
2. Preparar un único recorte OSM que cubra el feed y accesos peatonales. Fijar
   hashes del PBF, versión exacta del runtime, configuración y esquema esperado.
   Mantener `feed_id` estable por fuente y los `route_id`, `trip_id`, `stop_id`
   originales; separar namespaces entre operadores.
3. Construir fuera de la API con las opciones OTP `--build --save`; guardar
   `graph.obj`, su SHA256, fuentes y reporte de validación como generación nueva.
   La [guía oficial](https://docs.opentripplanner.org/en/v2.8.0/Basic-Tutorial/)
   requiere Java 21 y distingue preparación y carga con `--load`.
4. Cargar el grafo en un servicio independiente, medir memoria/latencia y probar
   bus, tren y subte donde existan, trasbordos, accesos a pie, cambio de fecha,
   ausencia de servicio y caducidad. Confirmar licencias antes de redistribuir
   el grafo y fuentes. Mantener la generación anterior para rollback.
5. Habilitar el endpoint sólo después de probar el contrato y la vigencia.
   Sin fuente actual/grafo/proveedor, mostrar indisponibilidad; no sustituir
   transporte público por un itinerario de auto. GTFS estático conserva
   horarios programados; tiempo real requiere GTFS-RT vigente adicional.

La API de aplicación presupuestada en **256 MiB** no debe construir ni cargar
OTP. Los datos de OTP y su preparación necesitan un servicio con recursos
medidos: el [presupuesto oficial](https://docs.opentripplanner.org/en/v2.8.0/System-Requirements/)
depende de región y densidad y puede superar varios GiB. No se contratan hosts,
se prometen tiempos ni se asigna un presupuesto de memoria sin medir el grafo.

## Auto, moto, bici y caminata

Con Valhalla usar costings explícitos `auto`, `motorcycle`, `bicycle` y
`pedestrian`, asociados respectivamente a los IDs de aplicación `car`,
`motorcycle`, `bicycle` y `walk`, con acceso/sentidos/superficie y restricciones
del grafo real.
Preparar y verificar los tiles/configuraciones fuera de la API. Un costing
compartiendo tiles sigue requiriendo pruebas por modo; no es suficiente cambiar
el texto del botón. [Contrato oficial Valhalla](https://valhalla.github.io/valhalla/api/route/api-reference/).

Si se elige OSRM como alternativa, cada perfil requiere su propia extracción,
partición/customización y servicio del grafo correspondiente. El nombre de la
ruta HTTP no cambia el perfil que se extrajo; un grafo `car` no resuelve bici o
moto por renombrarlo. Conservar hash del perfil y conjunto MLD completo por modo.
[Preparación OSRM](https://github.com/Project-OSRM/osrm-backend/blob/v6.0.0/README.md).

Comprobar casos de contramano, ciclovía, sendero sólo peatonal, acceso privado,
autopista prohibida para bici, restricciones de moto, límites y ETA por modo.
Sólo declarar disponible la región/perfil efectivamente probado. Datos OSM
comunitarios no garantizan restricciones correctas o cobertura total.

## Verificación realizada

17 pruebas automatizadas aprobadas del validador, con feeds sintéticos públicos:
vigencia, días/excepciones, claves y referencias, zonas, horarios >24, rutas de
ZIP, duplicados, expansión/tamaños, coordenadas, procedencia y CLI/reporte.
No se descargó GTFS, no se construyó/cargó OTP/Valhalla y no se modificó el
bundle OSRM histórico. Pruebas: `python -m unittest discover -s tests -v`.
