"""Offline, bounded admission checks for Argentine fixed-stop GTFS Schedule.

Uses Python's standard library and the host IANA timezone database. It never
downloads, extracts ZIP members, or builds a router graph.
"""
import argparse
import csv
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import hashlib
import io
import json
import math
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
from urllib.parse import urlsplit
import zipfile
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


MAX_FILES = 128
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 768 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
MAX_ROWS = 3000000
MAX_FIELD_CHARS = 8192
ARGENTINA_BBOX = [-74.0, -56.0, -53.0, -21.0]
MODES = {0: 'TRAM', 1: 'SUBWAY', 2: 'RAIL', 3: 'BUS', 4: 'FERRY',
         5: 'CABLE_CAR', 6: 'GONDOLA', 7: 'FUNICULAR', 11: 'TROLLEYBUS', 12: 'MONORAIL'}
REQUIRED = {
    'agency.txt': ('agency_name', 'agency_url', 'agency_timezone'),
    'stops.txt': ('stop_id', 'stop_name', 'stop_lat', 'stop_lon'),
    'routes.txt': ('route_id', 'route_type'),
    'trips.txt': ('route_id', 'service_id', 'trip_id'),
    'stop_times.txt': ('trip_id', 'arrival_time', 'departure_time', 'stop_id', 'stop_sequence'),
    'calendar.txt': ('service_id', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday',
                     'saturday', 'sunday', 'start_date', 'end_date'),
    'calendar_dates.txt': ('service_id', 'date', 'exception_type'),
    'feed_info.txt': ('feed_publisher_name', 'feed_publisher_url', 'feed_lang'),
}


class InvalidFeed(Exception):
    def __init__(self, code, message, filename=None, row=None):
        self.issue = {'code': code, 'message': message}
        if filename:
            self.issue['file'] = filename
        if row:
            self.issue['row'] = row
        super().__init__(message)


def reject(code, message, filename=None, row=None):
    raise InvalidFeed(code, message, filename, row)


def public_url(value):
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError
    except ValueError:
        reject('invalid_source_url', 'La procedencia requiere una URL HTTP(S) pública, sin credenciales ni fragmento.')
    return value


def gtfs_date(value, filename, row):
    try:
        if not re.fullmatch(r'[0-9]{8}', value):
            raise ValueError
        return datetime.strptime(value, '%Y%m%d').date()
    except ValueError:
        reject('invalid_date', 'Fecha GTFS inválida; usar YYYYMMDD.', filename, row)


def gtfs_time(value, filename, row):
    if not value:
        return None
    match = re.fullmatch(r'([0-9]{1,3}):([0-9]{2}):([0-9]{2})', value)
    if not match or int(match[2]) > 59 or int(match[3]) > 59:
        reject('invalid_time', 'Hora GTFS inválida; HH:MM:SS admite horas posteriores a 24.', filename, row)
    return int(match[1])*3600 + int(match[2])*60 + int(match[3])


def number(value, filename, row, field):
    try:
        result = float(value)
        if not math.isfinite(result):
            raise ValueError
        return result
    except ValueError:
        reject('invalid_coordinate', f'{field} debe ser una coordenada finita.', filename, row)


def integer(value, filename, row, field, choices=None, minimum=0):
    if not re.fullmatch(r'[0-9]+', value):
        reject('invalid_integer', f'{field} debe ser un entero no negativo.', filename, row)
    result = int(value)
    if result < minimum or choices is not None and result not in choices:
        reject('invalid_enum', f'{field} contiene un valor fuera del contrato admitido.', filename, row)
    return result


def inspect_zip(archive):
    infos = archive.infolist()
    if not infos or len(infos) > MAX_FILES:
        reject('zip_file_count', f'ZIP vacío o con más de {MAX_FILES} miembros.')
    names = set()
    total = 0
    for member in infos:
        name = member.filename
        if (not name or '/' in name or '\\' in name or ':' in name or name in ('.', '..')
                or '\x00' in name or member.is_dir()):
            reject('unsafe_zip_path', 'Los archivos GTFS deben estar en la raíz del ZIP, sin rutas.', name)
        if name.casefold() in names:
            reject('duplicate_zip_member', 'El ZIP contiene nombres de archivo duplicados o ambiguos.', name)
        names.add(name.casefold())
        if stat.S_ISLNK(member.external_attr >> 16) or member.flag_bits & 1:
            reject('unsafe_zip_member', 'No se admiten enlaces simbólicos ni archivos cifrados.', name)
        if member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            reject('unsupported_zip_compression', 'Sólo se admiten miembros ZIP almacenados o DEFLATE.', name)
        total += member.file_size
        if (member.file_size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES
                or member.file_size > max(1, member.compress_size) * MAX_COMPRESSION_RATIO):
            reject('zip_size_limit', 'El ZIP excede el presupuesto de tamaño o expansión.', name)
    for filename in ('agency.txt', 'stops.txt', 'routes.txt', 'trips.txt', 'stop_times.txt'):
        if filename not in archive.namelist():
            reject('missing_file', f'Falta {filename}.', filename)
    if not {'calendar.txt', 'calendar_dates.txt'} & set(archive.namelist()):
        reject('missing_calendar', 'Se requiere calendar.txt o calendar_dates.txt.')
    return total


def rows(archive, filename):
    if filename not in archive.namelist():
        return
    with archive.open(filename) as binary, io.TextIOWrapper(binary, encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream, strict=True)
        header = reader.fieldnames
        if (not header or len(header) != len(set(header)) or any(not col or col != col.strip() for col in header)):
            reject('invalid_header', 'Encabezados CSV vacíos, duplicados o con espacios.', filename)
        if not set(REQUIRED[filename]).issubset(header):
            reject('missing_column', 'Faltan columnas obligatorias de GTFS.', filename)
        for index, row in enumerate(reader, 2):
            if index > MAX_ROWS + 1:
                reject('row_limit', f'{filename} excede {MAX_ROWS} filas.', filename, index)
            if None in row or any(value is None or any(c in value for c in '\t\r\n\x00') for value in row.values()):
                reject('invalid_csv_row', 'Cantidad de columnas o controles dentro del CSV inválidos.', filename, index)
            if any(len(value) > MAX_FIELD_CHARS for value in row.values()):
                reject('field_limit', 'El campo CSV excede el presupuesto de longitud.', filename, index)
            yield index, row


def required_value(row, key, filename, index):
    value = row.get(key, '')
    if not value or not value.strip():
        reject('missing_value', f'{key} no puede estar vacío.', filename, index)
    return value


def insert(db, table, values, filename, row):
    try:
        db.execute(f'INSERT INTO {table} VALUES({",".join("?" for _ in values)})', values)
    except sqlite3.IntegrityError:
        reject('duplicate_key', 'La clave GTFS está duplicada.', filename, row)


def load_tables(archive, db, counts):
    db.executescript('''
      CREATE TABLE agency(id TEXT PRIMARY KEY,tz TEXT NOT NULL);
      CREATE TABLE stops(id TEXT PRIMARY KEY,lat REAL,lon REAL,kind INTEGER,parent TEXT);
      CREATE TABLE routes(id TEXT PRIMARY KEY,agency TEXT,kind INTEGER NOT NULL);
      CREATE TABLE trips(id TEXT PRIMARY KEY,route TEXT NOT NULL,service TEXT NOT NULL);
      CREATE INDEX trips_service ON trips(service);
      CREATE TABLE stop_times(trip TEXT NOT NULL,sequence INTEGER NOT NULL,stop TEXT NOT NULL,arrival INTEGER,departure INTEGER,
                              PRIMARY KEY(trip,sequence));
      CREATE TABLE calendar(service TEXT PRIMARY KEY,start TEXT,end TEXT,week TEXT NOT NULL);
      CREATE TABLE exceptions(service TEXT NOT NULL,day TEXT NOT NULL,kind INTEGER NOT NULL,PRIMARY KEY(service,day));
      CREATE TABLE feed_info(start TEXT,end TEXT,version TEXT);
    ''')
    for filename in REQUIRED:
        if filename not in archive.namelist():
            continue
        count = 0
        for index, row in rows(archive, filename):
            count += 1
            if filename == 'agency.txt':
                required_value(row, 'agency_name', filename, index)
                public_url(required_value(row, 'agency_url', filename, index))
                tz = required_value(row, 'agency_timezone', filename, index)
                try:
                    ZoneInfo(tz)
                except (ZoneInfoNotFoundError, ValueError):
                    reject('invalid_timezone', 'Zona IANA desconocida o base IANA no disponible en el host.', filename, index)
                insert(db, 'agency', (row.get('agency_id') or '__single_agency__', tz), filename, index)
            elif filename == 'stops.txt':
                ident = required_value(row, 'stop_id', filename, index)
                kind = integer(row.get('location_type') or '0', filename, index, 'location_type', range(5))
                if kind in (0, 1, 2):
                    required_value(row, 'stop_name', filename, index)
                lat = number(row['stop_lat'], filename, index, 'stop_lat') if row['stop_lat'] else None
                lon = number(row['stop_lon'], filename, index, 'stop_lon') if row['stop_lon'] else None
                if (lat is None) != (lon is None) or kind in (0, 1, 2) and lat is None:
                    reject('missing_coordinate', 'El tipo de parada requiere ambas coordenadas.', filename, index)
                if lat is not None and not (ARGENTINA_BBOX[1] <= lat <= ARGENTINA_BBOX[3]
                                           and ARGENTINA_BBOX[0] <= lon <= ARGENTINA_BBOX[2]):
                    reject('outside_argentina', 'La parada queda fuera del rectángulo argentino admitido.', filename, index)
                insert(db, 'stops', (ident, lat, lon, kind, row.get('parent_station', '')), filename, index)
            elif filename == 'routes.txt':
                if not row.get('route_short_name') and not row.get('route_long_name'):
                    reject('missing_route_name', 'La ruta requiere nombre corto o largo.', filename, index)
                kind = integer(row['route_type'], filename, index, 'route_type', MODES)
                insert(db, 'routes', (required_value(row, 'route_id', filename, index), row.get('agency_id', ''), kind), filename, index)
            elif filename == 'trips.txt':
                insert(db, 'trips', tuple(required_value(row, field, filename, index)
                                         for field in ('trip_id', 'route_id', 'service_id')), filename, index)
            elif filename == 'stop_times.txt':
                arrival = gtfs_time(row['arrival_time'], filename, index)
                departure = gtfs_time(row['departure_time'], filename, index)
                if (arrival is None) != (departure is None) or arrival is not None and departure < arrival:
                    reject('invalid_stop_times', 'Llegada/salida deben estar juntas y ordenadas.', filename, index)
                timepoint = integer(row.get('timepoint') or '1', filename, index, 'timepoint', (0, 1))
                if timepoint == 1 and arrival is None:
                    reject('missing_stop_time', 'Un timepoint exacto requiere horarios.', filename, index)
                insert(db, 'stop_times', (required_value(row, 'trip_id', filename, index),
                                         integer(row['stop_sequence'], filename, index, 'stop_sequence'),
                                         required_value(row, 'stop_id', filename, index), arrival, departure), filename, index)
            elif filename == 'calendar.txt':
                start, end = gtfs_date(row['start_date'], filename, index), gtfs_date(row['end_date'], filename, index)
                if start > end:
                    reject('calendar_range', 'El inicio de calendario es posterior al fin.', filename, index)
                week = ''.join(str(integer(row[field], filename, index, field, (0, 1)))
                               for field in REQUIRED[filename][1:8])
                insert(db, 'calendar', (required_value(row, 'service_id', filename, index), start.isoformat(), end.isoformat(), week), filename, index)
            elif filename == 'calendar_dates.txt':
                day = gtfs_date(row['date'], filename, index)
                kind = integer(row['exception_type'], filename, index, 'exception_type', (1, 2))
                insert(db, 'exceptions', (required_value(row, 'service_id', filename, index), day.isoformat(), kind), filename, index)
            elif filename == 'feed_info.txt':
                required_value(row, 'feed_publisher_name', filename, index)
                public_url(required_value(row, 'feed_publisher_url', filename, index))
                required_value(row, 'feed_lang', filename, index)
                start = gtfs_date(row['feed_start_date'], filename, index).isoformat() if row.get('feed_start_date') else None
                end = gtfs_date(row['feed_end_date'], filename, index).isoformat() if row.get('feed_end_date') else None
                if bool(start) != bool(end) or start and start > end:
                    reject('feed_range', 'feed_start_date/feed_end_date deben estar juntos y ordenados.', filename, index)
                insert(db, 'feed_info', (start, end, row.get('feed_version')), filename, index)
        counts[filename] = count
        if not count and filename in ('agency.txt', 'stops.txt', 'routes.txt', 'trips.txt', 'stop_times.txt'):
            reject('empty_file', 'La tabla obligatoria está vacía.', filename)
    db.commit()


def relational_checks(db):
    checks = (
        ('route_agency', "SELECT r.id FROM routes r LEFT JOIN agency a ON a.id=r.agency WHERE r.agency<>'' AND a.id IS NULL LIMIT 1"),
        ('trip_route', 'SELECT t.id FROM trips t LEFT JOIN routes r ON r.id=t.route WHERE r.id IS NULL LIMIT 1'),
        ('trip_service', '''SELECT t.id FROM trips t LEFT JOIN calendar c ON c.service=t.service
                            WHERE c.service IS NULL AND NOT EXISTS(SELECT 1 FROM exceptions e WHERE e.service=t.service AND e.kind=1) LIMIT 1'''),
        ('stop_time_trip', 'SELECT s.trip FROM stop_times s LEFT JOIN trips t ON t.id=s.trip WHERE t.id IS NULL LIMIT 1'),
        ('stop_time_stop', 'SELECT s.stop FROM stop_times s LEFT JOIN stops p ON p.id=s.stop WHERE p.id IS NULL OR p.kind<>0 LIMIT 1'),
        ('stop_parent', "SELECT s.id FROM stops s LEFT JOIN stops p ON p.id=s.parent WHERE s.parent<>'' AND (p.id IS NULL OR p.id=s.id OR (s.kind=4 AND p.kind<>0) OR (s.kind<>4 AND p.kind<>1) OR s.kind=1) LIMIT 1"),
        ('stop_parent_required', "SELECT id FROM stops WHERE kind IN(2,3,4) AND parent='' LIMIT 1"),
        ('orphan_removed_service', '''SELECT e.service FROM exceptions e LEFT JOIN calendar c ON c.service=e.service
                                     WHERE e.kind=2 AND c.service IS NULL AND NOT EXISTS
                                     (SELECT 1 FROM exceptions a WHERE a.service=e.service AND a.kind=1) LIMIT 1'''),
    )
    for code, query in checks:
        if db.execute(query).fetchone():
            reject('foreign_key_' + code, 'Referencia GTFS inexistente o incompatible: ' + code + '.')
    if db.execute('SELECT count(*) FROM agency').fetchone()[0] > 1:
        if db.execute("SELECT 1 FROM agency WHERE id='__single_agency__'").fetchone() or db.execute("SELECT 1 FROM routes WHERE agency='' LIMIT 1").fetchone():
            reject('agency_id_required', 'Múltiples agencias requieren agency_id en agencias y rutas.')
    if db.execute('SELECT count(DISTINCT tz) FROM agency').fetchone()[0] != 1:
        reject('mixed_agency_timezones', 'Todas las agencias del feed deben compartir agency_timezone.')
    if db.execute('SELECT count(*) FROM feed_info').fetchone()[0] > 1:
        reject('feed_info_count', 'feed_info.txt permite una única fila.')
    counts = db.execute('''SELECT t.id,count(s.sequence) FROM trips t LEFT JOIN stop_times s ON s.trip=t.id
                           GROUP BY t.id HAVING count(s.sequence)<2 LIMIT 1''').fetchone()
    if counts:
        reject('trip_stop_count', 'Cada viaje requiere al menos dos paradas con secuencia única.')
    previous_trip, previous_departure = None, None
    for trip, sequence, arrival, departure in db.execute('SELECT trip,sequence,arrival,departure FROM stop_times ORDER BY trip,sequence'):
        if trip != previous_trip:
            if previous_trip is not None and previous_departure is None:
                reject('missing_terminal_time', 'La última parada de cada viaje requiere horario.')
            if arrival is None:
                reject('missing_terminal_time', 'La primera parada de cada viaje requiere horario.')
            previous_trip, previous_departure = trip, departure
        elif arrival is not None:
            if previous_departure is not None and arrival < previous_departure:
                reject('non_monotonic_time', 'Los horarios retroceden dentro de la secuencia de un viaje.')
            previous_departure = departure
        # For blank intermediate interpolation keep the last known time.
    # Explicit final-sequence lookup also covers blank intermediate times.
    if db.execute('''SELECT s.trip FROM stop_times s JOIN
                      (SELECT trip,max(sequence) AS final FROM stop_times GROUP BY trip) x
                      ON x.trip=s.trip AND x.final=s.sequence WHERE s.arrival IS NULL LIMIT 1''').fetchone():
        reject('missing_terminal_time', 'La última parada de cada viaje requiere horario.')


def calendar_summary(db, as_of):
    active = []
    first, last = None, None
    for (service,) in db.execute('SELECT DISTINCT service FROM trips ORDER BY service'):
        row = db.execute('SELECT start,end,week FROM calendar WHERE service=?', (service,)).fetchone()
        exceptions = {date.fromisoformat(day): kind for day, kind in db.execute('SELECT day,kind FROM exceptions WHERE service=?', (service,))}
        base_start, base_end, week = (date.fromisoformat(row[0]), date.fromisoformat(row[1]), row[2]) if row else (None, None, '0000000')
        additions = [day for day, kind in exceptions.items() if kind == 1]
        candidates_first, candidates_last = list(additions), list(additions)
        if row and '1' in week:
            for direction, initial, candidates in ((1, base_start, candidates_first), (-1, base_end, candidates_last)):
                day = initial
                while base_start <= day <= base_end:
                    if week[day.weekday()] == '1' and exceptions.get(day) != 2:
                        candidates.append(day)
                        break
                    # At most 7 checks per excluded service date, not a date expansion.
                    if direction == 1 and day == date.max or direction == -1 and day == date.min:
                        break
                    day += timedelta(days=direction)
        if candidates_first:
            beginning, ending = min(candidates_first), max(candidates_last)
            first = beginning if first is None else min(first, beginning)
            last = ending if last is None else max(last, ending)
        runs = bool(row and base_start <= as_of <= base_end and week[as_of.weekday()] == '1')
        if as_of in exceptions:
            runs = exceptions[as_of] == 1
        if runs:
            active.append(service)
    feed = db.execute('SELECT start,end,version FROM feed_info LIMIT 1').fetchone()
    feed_start = date.fromisoformat(feed[0]) if feed and feed[0] else None
    feed_end = date.fromisoformat(feed[1]) if feed and feed[1] else None
    if feed_start and first and feed_start > first or feed_end and last and feed_end < last:
        reject('feed_calendar_mismatch', 'La vigencia feed_info no contiene todos los servicios efectivos.')
    if feed_start and as_of < feed_start or feed_end and as_of > feed_end:
        reject('feed_outside_validity', 'El feed está fuera de la vigencia declarada para la fecha pedida.')
    valid_until = min(last, feed_end) if last and feed_end else last
    active_count = sum(db.execute('SELECT count(*) FROM trips WHERE service=?', (service,)).fetchone()[0] for service in active)
    summary = {'active_service_ids': active, 'active_trip_count': active_count,
               'first_service_date': first.isoformat() if first else None,
               'last_service_date': last.isoformat() if last else None,
               'feed_start_date': feed_start.isoformat() if feed_start else None,
               'feed_end_date': feed_end.isoformat() if feed_end else None,
               'feed_version': feed[2] if feed else None,
               'valid_until': valid_until.isoformat() if valid_until else None}
    return summary


def validate_feed(path, source_url, license_name, consulted_at, as_of=None):
    path = Path(path)
    report = {'schema_version': 1, 'artifact_type': 'gtfs_schedule_validation',
              'status': 'rejected', 'ready_for_graph': False, 'as_of': None,
              'source': {}, 'artifact': {'filename': path.name}, 'coverage': {},
              'calendar': {}, 'counts': {}, 'errors': [], 'warnings': []}
    try:
        day = as_of or datetime.now(timezone.utc).date()
        day = date.fromisoformat(day) if isinstance(day, str) else day
        if not isinstance(day, date) or isinstance(day, datetime):
            reject('invalid_as_of', '--as-of debe ser una fecha YYYY-MM-DD.')
        report['as_of'] = day.isoformat()
        consulted = datetime.fromisoformat(consulted_at.replace('Z', '+00:00'))
        if consulted.tzinfo is None or consulted.utcoffset() is None:
            reject('naive_consulted_at', '--consulted-at requiere fecha/hora con zona UTC u offset explícito.')
        if not license_name or not license_name.strip() or any(c in license_name for c in '\t\r\n\x00'):
            reject('missing_license', 'Declarar la licencia de la fuente es obligatorio.')
        report['source'] = {'url': public_url(source_url), 'license': license_name,
                            'consulted_at': consulted.astimezone(timezone.utc).isoformat(),
                            'license_verification': 'operator_declared'}
        with path.open('rb') as source:
            size = source.seek(0, 2)
            source.seek(0)
            if size > MAX_ARCHIVE_BYTES:
                reject('archive_size_limit', 'El archivo comprimido excede el límite permitido.')
            digest = hashlib.sha256()
            for chunk in iter(lambda: source.read(1024*1024), b''):
                digest.update(chunk)
            report['artifact'].update({'sha256': digest.hexdigest(), 'size_bytes': size})
            source.seek(0)
            with zipfile.ZipFile(source) as archive, tempfile.TemporaryDirectory(prefix='ruta-gtfs-') as staging:
                report['artifact']['uncompressed_bytes'] = inspect_zip(archive)
                with closing(sqlite3.connect(str(Path(staging) / 'validation.sqlite'))) as db:
                    db.execute('PRAGMA cache_size=-8192')
                    db.execute('PRAGMA temp_store=FILE')
                    load_tables(archive, db, report['counts'])
                    relational_checks(db)
                    bbox = db.execute('SELECT min(lon),min(lat),max(lon),max(lat) FROM stops WHERE lat IS NOT NULL').fetchone()
                    scheduled_types = [row[0] for row in db.execute('SELECT DISTINCT r.kind FROM routes r JOIN trips t ON t.route=r.id ORDER BY r.kind')]
                    report['calendar'] = calendar_summary(db, day)
                    db.execute('CREATE TEMP TABLE active_service(id TEXT PRIMARY KEY)')
                    db.executemany('INSERT INTO active_service VALUES(?)',
                                   ((service,) for service in report['calendar']['active_service_ids']))
                    route_types = [row[0] for row in db.execute('''SELECT DISTINCT r.kind FROM routes r JOIN trips t ON t.route=r.id
                                                                 JOIN active_service a ON a.id=t.service ORDER BY r.kind''')]
                    report['coverage'] = {'country': 'AR', 'bbox': list(bbox), 'bounds_basis': 'gtfs_stop_points',
                                          'boundary_verified': False, 'modes': sorted({MODES[kind] for kind in route_types}),
                                          'scheduled_modes': sorted({MODES[kind] for kind in scheduled_types}),
                                          'route_types': route_types, 'timezones': [row[0] for row in db.execute('SELECT DISTINCT tz FROM agency')],
                                          'realtime_available': False}
                    if not report['calendar']['active_trip_count']:
                        reject('no_active_service', 'No hay viajes con servicio efectivo en la fecha pedida; no se admite como feed vigente.')
        report['status'], report['ready_for_graph'] = 'available', True
        report['warnings'] = [{'code': 'schedule_only', 'message': 'Horarios programados; no acredita servicio real, tarifa ni posiciones en vivo.'},
                              {'code': 'partial_validator', 'message': 'Admisión acotada de GTFS de paradas fijas; complementar con validador canónico y carga real de OTP.'}]
    except InvalidFeed as failure:
        report['errors'].append(failure.issue)
    except (OSError, ValueError, TypeError, csv.Error, UnicodeError, zipfile.BadZipFile, RuntimeError, sqlite3.Error) as failure:
        report['errors'].append({'code': 'unreadable_feed', 'message': 'Archivo, CSV o metadata no legibles: ' + type(failure).__name__})
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('zip_path', type=Path)
    parser.add_argument('--source-url', required=True)
    parser.add_argument('--license', dest='license_name', required=True)
    parser.add_argument('--consulted-at', required=True)
    parser.add_argument('--as-of', help='Fecha YYYY-MM-DD; por defecto hoy UTC')
    parser.add_argument('--output', type=Path, required=True, help='Reporte JSON de procedencia y admisión')
    args = parser.parse_args(argv)
    if args.zip_path.resolve() == args.output.resolve():
        parser.error('El reporte no puede reemplazar el ZIP fuente.')
    report = validate_feed(args.zip_path, args.source_url, args.license_name, args.consulted_at, args.as_of)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': report['status'], 'ready_for_graph': report['ready_for_graph'],
                      'report': str(args.output), 'errors': [issue['code'] for issue in report['errors']]}))
    return 0 if report['ready_for_graph'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
