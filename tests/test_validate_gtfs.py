import csv
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import warnings
import zipfile


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'validate_gtfs.py'
spec = importlib.util.spec_from_file_location('validate_gtfs', SCRIPT)
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)
SOURCE = 'https://operator.example/public/synthetic.gtfs.zip'
LICENSE = 'CC-BY-4.0'
CONSULTED = '2026-10-07T12:00:00-03:00'
TODAY = '2026-10-07'


def fixture():
    return {
        'agency.txt': [['agency_id', 'agency_name', 'agency_url', 'agency_timezone'],
                       ['a', 'Operador sintético público', 'https://operator.example', 'America/Argentina/Buenos_Aires']],
        'stops.txt': [['stop_id', 'stop_name', 'stop_lat', 'stop_lon'],
                      ['s1', 'Parada sintética uno', '-34.6', '-58.4'],
                      ['s2', 'Parada sintética dos', '-34.61', '-58.41']],
        'routes.txt': [['route_id', 'agency_id', 'route_short_name', 'route_type'], ['r1', 'a', '1', '3']],
        'trips.txt': [['route_id', 'service_id', 'trip_id'], ['r1', 'week', 't1']],
        'stop_times.txt': [['trip_id', 'arrival_time', 'departure_time', 'stop_id', 'stop_sequence'],
                           ['t1', '24:30:00', '24:31:00', 's1', '1'],
                           ['t1', '25:00:00', '25:01:00', 's2', '2']],
        'calendar.txt': [['service_id', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday', 'start_date', 'end_date'],
                         ['week', '1', '1', '1', '1', '1', '1', '1', '20261001', '20261031']],
        'feed_info.txt': [['feed_publisher_name', 'feed_publisher_url', 'feed_lang', 'feed_start_date', 'feed_end_date', 'feed_version'],
                          ['Operador sintético', 'https://operator.example', 'es', '20261001', '20261031', 'fixture-only']],
    }


def make_zip(folder, data, extra=None):
    path = Path(folder) / 'synthetic.gtfs.zip'
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, table in data.items():
            buffer = io.StringIO(newline='')
            csv.writer(buffer).writerows(table)
            archive.writestr(name, buffer.getvalue().encode('utf-8-sig'))
        for name, content in extra or []:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', UserWarning)
                archive.writestr(name, content)
    return path


class GtfsValidationTests(unittest.TestCase):
    def check(self, data, extra=None, **kwargs):
        with TemporaryDirectory() as folder:
            path = make_zip(folder, data, extra)
            return validator.validate_feed(path, SOURCE, LICENSE, CONSULTED, kwargs.pop('as_of', TODAY), **kwargs)

    def assert_rejected(self, data, code, extra=None):
        result = self.check(data, extra)
        self.assertEqual(result['status'], 'rejected', result)
        self.assertFalse(result['ready_for_graph'])
        self.assertEqual(result['errors'][0]['code'], code)
        return result

    def test_active_feed_reports_hash_provenance_coverage_and_after_midnight_times(self):
        with TemporaryDirectory() as folder:
            path = make_zip(folder, fixture())
            result = validator.validate_feed(path, SOURCE, LICENSE, CONSULTED, TODAY)
            self.assertEqual(result['status'], 'available', result)
            self.assertTrue(result['ready_for_graph'])
            self.assertEqual(result['artifact']['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(result['source']['consulted_at'], '2026-10-07T15:00:00+00:00')
            self.assertEqual(result['coverage']['bbox'], [-58.41, -34.61, -58.4, -34.6])
            self.assertEqual(result['coverage']['modes'], ['BUS'])
            self.assertFalse(result['coverage']['realtime_available'])
            self.assertEqual(result['calendar']['active_service_ids'], ['week'])
            self.assertEqual(result['calendar']['valid_until'], '2026-10-31')
            self.assertEqual(result['counts']['stop_times.txt'], 2)
            self.assertEqual(list(Path(folder).iterdir()), [path])

    def test_expired_and_future_calendars_are_not_admitted_as_current(self):
        for start, end in (('20250901', '20250930'), ('20261101', '20261130')):
            data = fixture()
            del data['feed_info.txt']
            data['calendar.txt'][1][-2:] = [start, end]
            result = self.assert_rejected(data, 'no_active_service')
            self.assertEqual(result['calendar']['active_trip_count'], 0)

    def test_feed_info_expiration_and_disagreement_fail_closed(self):
        data = fixture()
        data['calendar.txt'][1][-2:] = ['20260901', '20260930']
        data['feed_info.txt'][1][3:5] = ['20260901', '20260930']
        self.assert_rejected(data, 'feed_outside_validity')
        data = fixture()
        data['feed_info.txt'][1][4] = '20261020'
        self.assert_rejected(data, 'feed_calendar_mismatch')

    def test_calendar_removal_today_and_last_day_are_not_service(self):
        data = fixture()
        data['calendar_dates.txt'] = [['service_id', 'date', 'exception_type'], ['week', '20261007', '2']]
        result = self.assert_rejected(data, 'no_active_service')
        self.assertEqual(result['calendar']['active_service_ids'], [])
        data['calendar_dates.txt'][1] = ['week', '20261031', '2']
        result = self.check(data)
        self.assertEqual(result['status'], 'available', result)
        self.assertEqual(result['calendar']['valid_until'], '2026-10-30')

    def test_all_calendar_days_removed_cannot_fake_horizon(self):
        data = fixture()
        data['calendar.txt'][1][-2:] = ['20261007', '20261007']
        data['calendar_dates.txt'] = [['service_id', 'date', 'exception_type'], ['week', '20261007', '2']]
        result = self.assert_rejected(data, 'no_active_service')
        self.assertIsNone(result['calendar']['valid_until'])

    def test_exception_only_addition_is_valid_but_removal_is_not(self):
        data = fixture()
        del data['calendar.txt']
        data['calendar_dates.txt'] = [['service_id', 'date', 'exception_type'], ['week', '20261007', '1']]
        result = self.check(data)
        self.assertEqual(result['status'], 'available', result)
        self.assertEqual(result['calendar']['valid_until'], TODAY)
        data['calendar_dates.txt'][1][2] = '2'
        self.assert_rejected(data, 'foreign_key_trip_service')

    def test_weekday_schedule_without_service_today_is_unavailable(self):
        data = fixture()
        data['calendar.txt'][1][3] = '0'  # Wednesday.
        self.assert_rejected(data, 'no_active_service')

    def test_future_train_does_not_advertise_current_rail_coverage(self):
        data = fixture()
        del data['feed_info.txt']
        data['routes.txt'].append(['rail', 'a', 'R', '2'])
        data['trips.txt'].append(['rail', 'future', 't2'])
        data['calendar.txt'].append(['future', '1', '1', '1', '1', '1', '1', '1', '20261101', '20261130'])
        data['stop_times.txt'].extend([['t2', '10:00:00', '10:01:00', 's1', '1'],
                                       ['t2', '11:00:00', '11:01:00', 's2', '2']])
        result = self.check(data)
        self.assertEqual(result['status'], 'available', result)
        self.assertEqual(result['coverage']['modes'], ['BUS'])
        self.assertEqual(result['coverage']['scheduled_modes'], ['BUS', 'RAIL'])
        self.assertEqual(result['calendar']['active_trip_count'], 1)

    def test_foreign_keys_between_trips_routes_stops_and_calendar(self):
        edits = [('trips.txt', 1, 0, 'missing', 'foreign_key_trip_route'),
                 ('trips.txt', 1, 1, 'missing', 'foreign_key_trip_service'),
                 ('stop_times.txt', 1, 0, 'missing', 'foreign_key_stop_time_trip'),
                 ('stop_times.txt', 1, 3, 'missing', 'foreign_key_stop_time_stop'),
                 ('routes.txt', 1, 1, 'missing', 'foreign_key_route_agency')]
        for filename, row, column, value, code in edits:
            with self.subTest(file=filename, code=code):
                data = fixture()
                data[filename][row][column] = value
                self.assert_rejected(data, code)

    def test_duplicate_object_and_compound_stop_time_keys_are_rejected(self):
        for filename in ('stops.txt', 'routes.txt', 'trips.txt', 'stop_times.txt', 'calendar.txt'):
            with self.subTest(file=filename):
                data = fixture()
                data[filename].append(list(data[filename][1]))
                self.assert_rejected(data, 'duplicate_key')
        data = fixture()
        data['calendar_dates.txt'] = [['service_id', 'date', 'exception_type'],
                                      ['week', '20261007', '1'], ['week', '20261007', '2']]
        self.assert_rejected(data, 'duplicate_key')

    def test_unknown_timezone_invalid_coordinate_and_unknown_mode_are_rejected(self):
        data = fixture()
        data['agency.txt'][1][3] = 'Invalid/Timezone'
        self.assert_rejected(data, 'invalid_timezone')
        for value, code in [('NaN', 'invalid_coordinate'), ('inf', 'invalid_coordinate'), ('30', 'outside_argentina')]:
            data = fixture()
            data['stops.txt'][1][2] = value
            self.assert_rejected(data, code)
        data = fixture()
        data['routes.txt'][1][3] = '999'
        self.assert_rejected(data, 'invalid_enum')
        for value, mode in [('1', 'SUBWAY'), ('2', 'RAIL')]:
            data = fixture()
            data['routes.txt'][1][3] = value
            self.assertEqual(self.check(data)['coverage']['modes'], [mode])

    def test_invalid_times_reversals_and_single_stop_trip_are_rejected(self):
        data = fixture()
        data['stop_times.txt'][1][1] = '25:61:00'
        self.assert_rejected(data, 'invalid_time')
        data = fixture()
        data['stop_times.txt'][2][1:3] = ['24:00:00', '24:01:00']
        self.assert_rejected(data, 'non_monotonic_time')
        data = fixture()
        data['stop_times.txt'].pop()
        self.assert_rejected(data, 'trip_stop_count')

    def test_blank_intermediate_times_only_allowed_as_explicit_interpolation(self):
        data = fixture()
        data['stop_times.txt'][0].append('timepoint')
        data['stop_times.txt'][1].append('1')
        data['stop_times.txt'][2][-1] = '3'
        data['stop_times.txt'][2].append('1')
        data['stop_times.txt'].insert(2, ['t1', '', '', 's1', '2', '0'])
        result = self.check(data)
        self.assertEqual(result['status'], 'available', result)
        data['stop_times.txt'][2][-1] = '1'
        self.assert_rejected(data, 'missing_stop_time')

    def test_zip_paths_duplicates_and_expansion_bomb_are_rejected_without_extraction(self):
        for name in ('../stops.txt', '/stops.txt', 'folder/stops.txt', 'C:\\stops.txt', '..\\stops.txt'):
            with self.subTest(name=name):
                self.assert_rejected(fixture(), 'unsafe_zip_path', [(name, 'x')])
        self.assert_rejected(fixture(), 'duplicate_zip_member', [('stops.txt', 'x')])
        self.assert_rejected(fixture(), 'duplicate_zip_member', [('STOPS.TXT', 'x')])
        self.assert_rejected(fixture(), 'zip_size_limit', [('padding.txt', 'A'*100000)])
        with patch.object(validator, 'MAX_FILES', 2):
            self.assert_rejected(fixture(), 'zip_file_count')
        with patch.object(validator, 'MAX_TOTAL_BYTES', 500):
            self.assert_rejected(fixture(), 'zip_size_limit')

    def test_missing_file_calendar_column_duplicate_header_and_bad_date(self):
        data = fixture()
        del data['stops.txt']
        self.assert_rejected(data, 'missing_file')
        data = fixture()
        del data['calendar.txt']
        self.assert_rejected(data, 'missing_calendar')
        data = fixture()
        data['calendar.txt'][1][-1] = '20260230'
        self.assert_rejected(data, 'invalid_date')
        data = fixture()
        data['routes.txt'][0][-1] = 'unknown'
        self.assert_rejected(data, 'missing_column')
        data = fixture()
        data['stops.txt'][0][-1] = 'stop_lat'
        self.assert_rejected(data, 'invalid_header')

    def test_consultation_requires_offset_and_provenance_requires_license_and_public_url(self):
        with TemporaryDirectory() as folder:
            path = make_zip(folder, fixture())
            for source, license_name, consulted, code in (
                (SOURCE, LICENSE, '2026-10-07T12:00:00', 'naive_consulted_at'),
                (SOURCE, '', CONSULTED, 'missing_license'),
                ('https://user:password@operator.example/feed', LICENSE, CONSULTED, 'invalid_source_url')):
                report = validator.validate_feed(path, source, license_name, consulted, TODAY)
                self.assertFalse(report['ready_for_graph'])
                self.assertEqual(report['errors'][0]['code'], code)
                self.assertNotIn('password', json.dumps(report))

    def test_cli_writes_reviewable_report_and_rejected_exit_code(self):
        with TemporaryDirectory() as folder:
            source = make_zip(folder, fixture())
            output = Path(folder) / 'reports' / 'validation.json'
            args = [sys.executable, str(SCRIPT), str(source), '--source-url', SOURCE,
                    '--license', LICENSE, '--consulted-at', CONSULTED,
                    '--as-of', TODAY, '--output', str(output)]
            valid = subprocess.run(args, capture_output=True, text=True, timeout=10)
            self.assertEqual(valid.returncode, 0, valid.stderr + valid.stdout)
            self.assertTrue(json.loads(output.read_text(encoding='utf-8'))['ready_for_graph'])
            invalid = subprocess.run([*args[:-2], '--output', str(output), '--as-of', '2027-01-01'],
                                     capture_output=True, text=True, timeout=10)
            self.assertEqual(invalid.returncode, 2, invalid.stderr + invalid.stdout)
            self.assertFalse(json.loads(output.read_text(encoding='utf-8'))['ready_for_graph'])


if __name__ == '__main__':
    unittest.main()
