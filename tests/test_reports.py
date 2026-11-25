import csv
import xml.etree.ElementTree as ET
from datetime import date
from zoneinfo import ZoneInfo

import pytest

from conftest import (ANDROID_EXPORT, CITY_HALL, HARBOUR_BRIDGE, LEGACY_SEMANTIC, OPERA_HOUSE,
                      e7, rows)
from main import import_paths, main
from models.location import Location
from services.coverage_service import CoverageService
from services.distance_service import DistanceService
from services.export_service import ExportService
from services.parsing_utils import parse_time

GPX = '{http://www.topografix.com/GPX/1/1}'


def at(text: str) -> int:
    return parse_time(text)[0]


def reading(text, place, accuracy=10):
    return {'latitudeE7': e7(place[0]), 'longitudeE7': e7(place[1]),
            'accuracy': accuracy, 'timestamp': text}


# ----------------------------------------------------------------------
# Track: segments, distance, GPX
# ----------------------------------------------------------------------

def test_track_is_cut_at_long_gaps_and_impossible_jumps():
    points = [
        Location(at('2024-01-01T00:00:00Z'), *OPERA_HOUSE),
        Location(at('2024-01-01T00:10:00Z'), *HARBOUR_BRIDGE),
        # 50 minutes of silence: starts a new segment
        Location(at('2024-01-01T01:00:00Z'), *HARBOUR_BRIDGE),
        Location(at('2024-01-01T01:05:00Z'), *OPERA_HOUSE),
        # Sydney to Brisbane in one minute: a bad fix, starts a new segment
        Location(at('2024-01-01T01:06:00Z'), *CITY_HALL),
        # repeated time: ignored
        Location(at('2024-01-01T01:06:00Z'), *OPERA_HOUSE),
    ]
    service = DistanceService(max_gap_minutes=30)
    segments = list(service.split_segments(points))

    assert [len(segment) for segment in segments] == [2, 2]
    one_leg = service._haversine(*OPERA_HOUSE, *HARBOUR_BRIDGE)
    assert 0.5 < one_leg < 0.8
    assert service.calculate_total(points) == pytest.approx(2 * one_leg)


def test_track_uses_one_source_per_day_and_respects_filters(write_json, repo):
    write = write_json
    import_paths([
        # 1 May: three route points (from the phone export) against one reading.
        write('Timeline.json', ANDROID_EXPORT),
        # 3 March 2014: readings only, one of them too inaccurate to use.
        write('Records.json', {'locations': [
            reading('2014-03-03T00:00:00Z', OPERA_HOUSE),
            reading('2014-03-03T00:05:00Z', HARBOUR_BRIDGE),
            reading('2014-03-03T00:06:00Z', CITY_HALL, accuracy=5000),
        ]}),
    ], repo)

    track = list(repo.iter_track(max_accuracy_m=200))
    assert [point.timestamp for point in track] == [
        at('2014-03-03T00:00:00Z'), at('2014-03-03T00:05:00Z'),
        at('2024-04-30T23:30:00Z'), at('2024-04-30T23:40:00Z'), at('2024-04-30T23:50:00Z'),
    ]
    assert len(list(repo.iter_track())) == 6           # no accuracy limit
    only_2014 = list(repo.iter_track(to_ms=at('2015-01-01T00:00:00Z'), max_accuracy_m=200))
    assert len(only_2014) == 2
    only_2024 = list(repo.iter_track(from_ms=at('2024-01-01T00:00:00Z')))
    assert len(only_2024) == 3


def test_gpx_has_one_segment_per_run(tmp_path):
    points = [
        Location(at('2024-01-01T00:00:00Z'), *OPERA_HOUSE),
        Location(at('2024-01-01T00:10:00Z'), *HARBOUR_BRIDGE),
        Location(at('2024-01-01T05:00:00Z'), *CITY_HALL),
        Location(at('2024-01-01T05:10:00Z'), CITY_HALL[0] + 0.001, CITY_HALL[1]),
    ]
    output = tmp_path / 'out.gpx'
    counts = ExportService().export_gpx(DistanceService().split_segments(points), str(output))

    assert counts == (2, 4)
    root = ET.parse(output).getroot()
    segments = root.findall(f'{GPX}trk/{GPX}trkseg')
    assert [len(segment.findall(f'{GPX}trkpt')) for segment in segments] == [2, 2]
    first = segments[0].find(f'{GPX}trkpt')
    assert float(first.get('lat')) == pytest.approx(OPERA_HOUSE[0])
    assert first.find(f'{GPX}time').text == '2024-01-01T00:00:00Z'


def test_heatmap_groups_points_into_squares():
    service = ExportService()
    cells = service.heatmap_cells([OPERA_HOUSE] * 10 + [HARBOUR_BRIDGE])
    weights = {(lat, lng): weight for lat, lng, weight in cells}
    assert set(weights) == {OPERA_HOUSE, HARBOUR_BRIDGE}
    assert weights[OPERA_HOUSE] > weights[HARBOUR_BRIDGE] == 1.0

    # Too many squares: fall back to coarser ones.
    spread = [(-33.0 + i * 0.0001, 151.0) for i in range(30)]
    assert len(service.heatmap_cells(spread, max_cells=10)) < 10


# ----------------------------------------------------------------------
# Coverage report
# ----------------------------------------------------------------------

@pytest.fixture
def covered_repo(write_json, repo):
    import_paths([
        write_json('Records.json', {'locations': [
            # 8 pm UTC on 31 January is already 1 February in Brisbane.
            reading('2019-01-31T20:00:00Z', CITY_HALL),
            reading('2019-02-10T02:00:00Z', CITY_HALL),
            reading('2019-02-10T02:01:00Z', CITY_HALL, accuracy=None),
        ]}),
        # A visit from 1 to 3 July and a flight on 3 July.
        write_json('2019_JULY.json', LEGACY_SEMANTIC),
    ], repo)
    return repo


def test_coverage_counts_days_in_the_chosen_time_zone(covered_repo):
    brisbane = CoverageService(covered_repo, ZoneInfo('Australia/Brisbane'), 'Australia/Brisbane')
    report = brisbane.build()
    months = {month.month: month for month in report.months}

    assert '2019-01' not in months
    assert months['2019-02'].positions == 3
    assert months['2019-02'].days_with_data == 2
    assert months['2019-02'].days_in_month == 28
    assert report.first_day == date(2019, 2, 1)

    utc = CoverageService(covered_repo).build()
    utc_months = {month.month: month for month in utc.months}
    assert utc_months['2019-01'].positions == 1
    assert utc_months['2019-02'].positions == 2


def test_coverage_lists_empty_months_gaps_and_distance(covered_repo):
    service = CoverageService(covered_repo)
    report = service.build()
    months = {month.month: month for month in report.months}

    # Months with nothing in them still appear, so a hole is visible.
    assert [month.month for month in report.months] == [
        '2019-01', '2019-02', '2019-03', '2019-04', '2019-05', '2019-06', '2019-07']
    assert months['2019-04'].days_with_data == 0
    # The visit covers 1, 2 and 3 July even though it has no readings.
    assert months['2019-07'].days_with_data == 3
    assert months['2019-07'].visits == 1
    assert months['2019-07'].activities == 1
    assert months['2019-07'].path_points == 2
    assert months['2019-07'].reported_km == pytest.approx(733.0)
    assert report.km_by_year_and_mode == {(2019, 'FLYING'): pytest.approx(733.0)}

    longest = report.gaps[0]
    assert (longest.first_day, longest.last_day, longest.days) == (
        date(2019, 2, 11), date(2019, 6, 30), 140)
    assert report.days_with_data == 5
    assert report.position_types == [('legacy_record', 3, 1)]

    text = service.format_text(report)
    assert 'Days with any record: 5 of 154 between 2019-01-31 and 2019-07-03' in text
    assert '2019-02-11 to 2019-06-30   140 days' in text
    assert 'FLYING' in text and '733.0 km' in text
    assert 'old Takeout location readings' in text


def test_coverage_of_an_empty_database(repo):
    service = CoverageService(repo)
    text = service.format_text(service.build())
    assert 'none' in text and 'no month-by-month table' in text


# ----------------------------------------------------------------------
# Command line
# ----------------------------------------------------------------------

def test_command_line_end_to_end(write_json, tmp_path, capsys):
    db = str(tmp_path / 'cli.db')
    gpx = str(tmp_path / 'cli.gpx')
    table = str(tmp_path / 'coverage.csv')
    code = main([write_json('Timeline.json', ANDROID_EXPORT), '--db', db, '--coverage',
                 '--coverage-csv', table, '--tz', 'Australia/Sydney',
                 '--gpx', '--gpx-out', gpx, '--distance'])
    output = capsys.readouterr().out

    assert code == 0
    assert 'phone export' in output
    assert 'path_points: 3 read, 3 new' in output
    assert 'COVERAGE REPORT' in output
    # The three route points lie on a straight line, so the cleaned track keeps its ends.
    assert '(1 segments, 2 points; jumps removed' in output
    assert 'Distance along the track: 0.' in output
    with open(table, encoding='utf-8') as f:
        table_rows = list(csv.DictReader(f))
    assert table_rows == [{
        'month': '2024-05', 'days_in_month': '31', 'days_with_data': '1',
        'location_readings': '1', 'route_points': '3', 'visits': '1', 'movements': '1',
        'reported_km': '0.7'}]

    # Reporting again needs no files; --raw keeps every reading.
    assert main(['--db', db, '--gpx', '--gpx-out', gpx, '--raw']) == 0
    assert '(1 segments, 3 points; readings as recorded)' in capsys.readouterr().out
    assert main(['--db', db, '--distance', '--from', '2030-01-01']) == 0
    assert 'Distance along the track: 0.00 km' in capsys.readouterr().out


def test_command_line_rejects_bad_options(tmp_path, capsys):
    db = str(tmp_path / 'cli.db')
    with pytest.raises(SystemExit):
        main(['--db', db, '--tz', 'Mars/Olympus'])
    with pytest.raises(SystemExit):
        main(['--db', db, '--from', 'last week'])
    assert main(['/no/such/file.json', '--db', db]) == 2
    assert main(['--db', db]) == 0
    assert 'Nothing to do' in capsys.readouterr().out


def test_old_single_table_database_is_left_alone(tmp_path, capsys):
    import sqlite3
    db = str(tmp_path / 'old.db')
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE timeline (timestamp TEXT PRIMARY KEY, latitude REAL, '
                     'longitude REAL, semantic_type TEXT)')
        conn.execute("INSERT INTO timeline VALUES ('2024-01-01T00:00:00Z', 1, 2, '')")
    assert main(['--db', db, '--coverage']) == 0
    assert 'earlier version' in capsys.readouterr().out
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT COUNT(*) FROM timeline').fetchone() == (1,)
