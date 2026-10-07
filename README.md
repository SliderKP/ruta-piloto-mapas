# Mapas públicos AMBA para Ruta Piloto

Para nuevos modos y horarios: [provisionado de movilidad y validación GTFS](mobility/README.md).
El bundle histórico descrito abajo conserva su grafo de auto y sus hashes originales.

Artefactos cartográficos derivados exclusivamente de datos públicos OpenStreetMap/Geofabrik. No contiene código de la aplicación, cuentas, secretos, domicilios privados de participantes ni viajes.

© OpenStreetMap contributors. Base de datos y derivados bajo [Open Database License 1.0](https://opendatacommons.org/licenses/odbl/1-0/); atribución y acceso a datos fuente: [OpenStreetMap copyright](https://www.openstreetmap.org/copyright).

Fuente: https://download.geofabrik.de/south-america/argentina-latest.osm.pbf, última modificación indicada 2026-09-30 23:12:02 GMT, consulta 2026-10-02 14:23:02 UTC. SHA256 del PBF Argentina original: `db84c9685d761b127a9fefdec3a736e20bcb12341a156add614a7e5dd50c87a5`.

Recorte bbox AMBA `-58.9,-35.1,-58.15,-34.35`, estrategia osmium complete_ways. El recorte fuente se incluye en cada bundle para ofrecer los datos del derivado bajo la misma licencia. Información comunitaria sin validación oficial, sin tráfico en tiempo real ni garantía de restricciones correctas.

OSRM v6.0.0, algoritmo MLD, perfil car con weight_name duration. Peajes conservados como clase excluible; no evitados por defecto. Perfil exacto utilizado en `car-duration.lua`, bajo licencia BSD de Project OSRM incluida en `LICENSE-OSRM.txt`. Librerías estándar del perfil: [OSRM v6.0.0 profiles/lib](https://github.com/Project-OSRM/osrm-backend/tree/v6.0.0/profiles/lib). Preparación: osrm-extract -p /opt/car.lua; osrm-partition; osrm-customize.

SQLite geográfico: 217172 vías y 602063 puntos de búsqueda públicos OSM. Metadata y fuente incluidos. Las direcciones que figuran en OSM son datos públicos del mapa, no datos de usuarios de Ruta Piloto.

Release `amba-2026-09-30-osrm6-duration-v1`: `public-maps.tar.gz`, 332583768 bytes, 30 archivos, 729626589 bytes sin comprimir. SHA256:

```
2bdf77a9caccbbd2ce9f40fd8a296bed9a3eae5ca15e6d33c0bbc50297cc8e80
```

El bundle incluye mapsource/amba.osm.pbf, procedencia ready.json, todos los amba.osrm.*, geodata/map.sqlite, metadata.json y manifest SHA256 individual. No copiar solo un archivo OSRM. Comprobar el hash antes de importar; los marcadores de preparación se crean tras verificar todo el contenido. El repositorio ofrece datos; no incluye credenciales ni acceso al servidor.
