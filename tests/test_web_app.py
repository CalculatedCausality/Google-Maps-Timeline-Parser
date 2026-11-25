import http.client
import json
import threading
from zoneinfo import ZoneInfo

import pytest

from conftest import ANDROID_EXPORT, CITY_HALL, LEGACY_SEMANTIC, OPERA_HOUSE, e7
from database.connection import DatabaseConnection
from main import import_paths
from repositories.timeline_repository import TimelineRepository
from services.web_app import build_server, simplify_tolerance_m


def reading(text, place, accuracy=10):
    return {'latitudeE7': e7(place[0]), 'longitudeE7': e7(place[1]),
            'accuracy': accuracy, 'timestamp': text}


@pytest.fixture
def server(tmp_path, write_json):
    db = str(tmp_path / 'map.db')
    writer = TimelineRepository(DatabaseConnection(db))
    writer.create_tables()
    walk = [reading(f'2019-02-10T02:{minute:02d}:00Z',
                    (OPERA_HOUSE[0] + minute * 0.0005, OPERA_HOUSE[1]))
            for minute in range(20)]
    import_paths([
        write_json('Records.json', {'locations': walk + [
            reading('2019-02-10T02:30:00Z', CITY_HALL, accuracy=5000)]}),
        write_json('2019_JULY.json', LEGACY_SEMANTIC),
        write_json('Timeline.json', ANDROID_EXPORT),
    ], writer)

    reader = TimelineRepository(DatabaseConnection(db, read_only=True))
    instance = build_server(reader, 0, ZoneInfo('Australia/Sydney'), 'Australia/Sydney')
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield instance
    instance.shutdown()
    instance.server_close()


def get(server, path, host=None):
    port = server.server_address[1]
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
    conn.request('GET', path, headers={'Host': host or f'127.0.0.1:{port}'})
    response = conn.getresponse()
    body = response.read()
    headers = dict(response.getheaders())
    conn.close()
    return response.status, headers, body


def get_json(server, path):
    status, _, body = get(server, path)
    assert status == 200, body
    return json.loads(body)


def test_summary_lists_local_days(server):
    summary = get_json(server, '/api/summary')
    assert summary['timezone'] == 'Australia/Sydney'
    # 2 am UTC on 10 Feb is 1 pm in Sydney, same day; the July visit spans 1 to 3 July.
    assert summary['days'][:2] == ['2019-02-10', '2019-07-01']
    assert summary['days'][-1] == '2024-05-01'


def test_track_is_cleaned_lines_for_local_dates(server):
    track = get_json(server, '/api/track?from=2019-02-10&to=2019-02-10')
    assert track['type'] == 'FeatureCollection'
    assert track['stats']['segments'] == 1
    assert track['stats']['readings'] == 20          # the 5 km-inaccurate one is left out
    line = track['features'][0]
    assert line['geometry']['type'] == 'LineString'
    # A straight walk simplifies down to its two ends.
    assert len(line['geometry']['coordinates']) == 2
    assert line['properties']['day'] == '2019-02-10'
    assert line['properties']['start'] == '2019-02-10 13:00'
    assert 1.0 < track['stats']['km'] < 1.1

    raw = get_json(server, '/api/track?from=2019-02-10&to=2019-02-10&clean=0')
    assert len(raw['features'][0]['geometry']['coordinates']) == 20

    empty = get_json(server, '/api/track?from=2020-01-01&to=2020-01-31')
    assert empty['features'] == [] and empty['stats']['segments'] == 0


def test_readings_visits_and_heat(server):
    readings = get_json(server, '/api/readings?from=2019-02-10&to=2019-02-10')
    assert len(readings['points']) == 20 and not readings['truncated']
    assert readings['points'][0][3] == '2019-02-10 13:00'

    visits = get_json(server, '/api/visits?from=2019-07-01&to=2019-07-31')['visits']
    assert len(visits) == 1
    assert visits[0]['name'] == 'Brisbane City Hall'
    assert visits[0]['start'] == '2019-07-01 10:00'      # midnight UTC, in Sydney time

    heat = get_json(server, '/api/heat?from=2019-01-01&to=2019-12-31')['cells']
    assert heat and all(len(cell) == 3 for cell in heat)


def test_page_and_scripts_are_served_with_a_strict_policy(server):
    status, headers, body = get(server, '/')
    assert status == 200
    assert headers['Content-Type'].startswith('text/html')
    assert "connect-src 'self'" in headers['Content-Security-Policy']
    assert b'/app.js' in body and b'integrity="sha384-' in body
    status, headers, _ = get(server, '/app.js')
    assert status == 200 and headers['Content-Type'].startswith('text/javascript')


@pytest.mark.parametrize('path', ['/../main.py', '/web/app.js', '/main.py', '/api/nothing',
                                  '/%2e%2e/main.py'])
def test_only_known_paths_are_served(server, path):
    status, _, _ = get(server, path)
    assert status == 404


def test_other_host_names_are_refused(server):
    """Stops a web page elsewhere from reaching the map through a look-alike address."""
    status, _, _ = get(server, '/api/summary', host='evil.example:8765')
    assert status == 403
    port = server.server_address[1]
    status, _, _ = get(server, '/api/summary', host=f'localhost:{port}')
    assert status == 200


@pytest.mark.parametrize('query', ['', '?from=2019-01-01', '?from=yesterday&to=2019-01-01',
                                   '?from=2019-02-01&to=2019-01-01'])
def test_bad_dates_are_rejected(server, query):
    status, _, body = get(server, f'/api/track{query}')
    assert status == 400
    assert 'error' in json.loads(body)


def test_server_listens_on_this_computer_only(server):
    assert server.server_address[0] == '127.0.0.1'


def test_database_is_opened_read_only(tmp_path):
    import sqlite3
    db = str(tmp_path / 'ro.db')
    TimelineRepository(DatabaseConnection(db)).create_tables()
    reader = DatabaseConnection(db, read_only=True)
    with pytest.raises(sqlite3.OperationalError):
        with reader.get_connection() as conn:
            conn.execute('DELETE FROM positions')


def test_long_periods_are_drawn_with_fewer_points():
    assert simplify_tolerance_m(1) < simplify_tolerance_m(30) < simplify_tolerance_m(3650)
