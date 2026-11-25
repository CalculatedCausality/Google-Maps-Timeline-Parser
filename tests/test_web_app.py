import http.client
import json
import threading
from zoneinfo import ZoneInfo

import pytest

from conftest import ANDROID_EXPORT, CITY_HALL, LEGACY_SEMANTIC, OPERA_HOUSE, e7
from database.connection import DatabaseConnection
from main import import_paths
from repositories.edits_repository import EditsRepository
from repositories.timeline_repository import TimelineRepository
from services.road_snapper import RoadCache, RoadSnapper
from services.web_app import build_server, simplify_tolerance_m
from test_road_snapper import FakeFetcher, grid_ways, on_a_street


def reading(text, place, accuracy=10):
    return {'latitudeE7': e7(place[0]), 'longitudeE7': e7(place[1]),
            'accuracy': accuracy, 'timestamp': text}


@pytest.fixture
def fetcher():
    return FakeFetcher(grid_ways())


@pytest.fixture
def server(tmp_path, write_json, fetcher):
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
    edits = EditsRepository(str(tmp_path / 'edits.sqlite'))
    snapper = RoadSnapper(RoadCache(str(tmp_path / 'roads.sqlite')), fetcher)
    instance = build_server(reader, 0, ZoneInfo('Australia/Sydney'), 'Australia/Sydney',
                            edits=edits, snapper=snapper)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield instance
    instance.shutdown()
    instance.server_close()


def get(server, path, host=None, method='GET', body=None, headers=None):
    port = server.server_address[1]
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
    sent = {'Host': host or f'127.0.0.1:{port}'}
    sent.update(headers or {})
    conn.request(method, path, body=body, headers=sent)
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


@pytest.mark.parametrize('page, script', [('/', b'/day.js'), ('/explore', b'/explore.js')])
def test_pages_and_scripts_are_served_with_a_strict_policy(server, page, script):
    status, headers, body = get(server, page)
    assert status == 200
    assert headers['Content-Type'].startswith('text/html')
    assert "connect-src 'self'" in headers['Content-Security-Policy']
    assert script in body and b'integrity="sha384-' in body
    status, headers, _ = get(server, script.decode())
    assert status == 200 and headers['Content-Type'].startswith('text/javascript')
    status, headers, _ = get(server, '/material.css')
    assert status == 200 and headers['Content-Type'].startswith('text/css')


@pytest.mark.parametrize('path', ['/../main.py', '/web/day.js', '/main.py', '/api/nothing',
                                  '/%2e%2e/main.py', '/day.html'])
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


# ----------------------------------------------------------------------
# Day timeline and corrections
# ----------------------------------------------------------------------

def post(server, body, headers=None, raw=None):
    port = server.server_address[1]
    sent = {'Content-Type': 'application/json', 'X-Timeline-Edit': '1',
            'Origin': f'http://127.0.0.1:{port}'}
    sent.update(headers or {})
    sent = {k: v for k, v in sent.items() if v is not None}
    payload = raw if raw is not None else json.dumps(body).encode()
    status, _, answer = get(server, '/api/edit', method='POST', body=payload, headers=sent)
    return status, json.loads(answer)


def test_day_lists_stops_and_journeys(server):
    day = get_json(server, '/api/day?date=2019-07-03')
    assert day['date'] == '2019-07-03'
    assert day['previous'] == '2019-07-02' and day['next'] == '2024-05-01'
    kinds = [(i['kind'], i.get('name') or i.get('mode')) for i in day['items']]
    assert kinds == [('stop', 'Brisbane City Hall'), ('move', 'flying')]
    stop, flight = day['items']
    assert stop['start_text'] == 'Mon 01 Jul 10:00' and stop['end_text'] == '12:00'
    assert flight['mode_source'] == 'google' and flight['km'] > 700
    assert 'walking' in day['modes']


def test_rename_mode_hide_and_reset(server):
    stop_id = get_json(server, '/api/day?date=2019-07-03')['items'][0]['id']
    move_id = get_json(server, '/api/day?date=2019-07-03')['items'][1]['id']

    status, answer = post(server, {'date': '2019-07-03', 'item_id': stop_id,
                                   'action': 'rename', 'name': '  Town hall  '})
    assert status == 200
    stop = answer['day']['items'][0]
    assert (stop['name'], stop['name_source']) == ('Town hall', 'you')

    status, answer = post(server, {'date': '2019-07-03', 'item_id': move_id,
                                   'action': 'mode', 'mode': 'driving'})
    assert answer['day']['items'][1]['mode'] == 'driving'

    status, answer = post(server, {'date': '2019-07-03', 'item_id': move_id, 'action': 'hide'})
    assert answer['day']['items'][1]['hidden'] and answer['day']['summary']['moves'] == 0

    status, answer = post(server, {'date': '2019-07-03', 'action': 'reset'})
    stop, flight = answer['day']['items']
    assert stop['name'] == 'Brisbane City Hall' and flight['mode'] == 'flying'
    assert not flight['hidden'] and not answer['day']['summary']['edited']


def test_name_everywhere_reaches_other_days(server):
    stop_id = get_json(server, '/api/day?date=2019-07-02')['items'][0]['id']
    status, answer = post(server, {'date': '2019-07-02', 'item_id': stop_id, 'action': 'rename',
                                   'name': 'The big clock', 'everywhere': True})
    assert status == 200 and 'Every visit within 75 m' in answer['note']
    other = get_json(server, '/api/day?date=2019-07-03')['items'][0]
    assert (other['name'], other['name_source']) == ('The big clock', 'your place')


def test_snap_a_journey_to_roads_and_undo(server, fetcher):
    day = get_json(server, '/api/day?date=2019-02-10')
    moves = [i for i in day['items'] if i['kind'] == 'move']
    assert len(moves) == 1 and not moves[0]['snapped']

    status, answer = post(server, {'date': '2019-02-10', 'item_id': moves[0]['id'],
                                   'action': 'snap'})
    assert status == 200, answer
    assert 'follows roads' in answer['note']
    snapped = [i for i in answer['day']['items'] if i['kind'] == 'move'][0]
    assert snapped['snapped']
    from models.location import Location
    assert all(on_a_street(Location(0, lat, lng)) for lat, lng in snapped['line'])
    assert len(fetcher.calls) >= 1

    # Saved: the day comes back snapped without downloading again.
    calls = len(fetcher.calls)
    again = get_json(server, '/api/day?date=2019-02-10')
    assert [i for i in again['items'] if i['kind'] == 'move'][0]['snapped']
    assert len(fetcher.calls) == calls

    status, answer = post(server, {'date': '2019-02-10', 'item_id': moves[0]['id'],
                                   'action': 'unsnap'})
    assert not [i for i in answer['day']['items'] if i['kind'] == 'move'][0]['snapped']


def test_snap_failure_is_reported_and_nothing_saved(server, fetcher):
    fetcher.fail = True
    move = [i for i in get_json(server, '/api/day?date=2019-02-10')['items']
            if i['kind'] == 'move'][0]
    status, answer = post(server, {'date': '2019-02-10', 'item_id': move['id'], 'action': 'snap'})
    assert status == 409 and 'could not be downloaded' in answer['error']
    assert not get_json(server, '/api/day?date=2019-02-10')['summary']['edited']


@pytest.mark.parametrize('body, message', [
    ({'date': '2019-07-03', 'item_id': 'stop-1', 'action': 'hide'}, 'not on this day'),
    ({'date': 'soon', 'action': 'reset'}, 'must be a date'),
    ({'date': '2019-07-03', 'item_id': 'x', 'action': 'explode'}, 'not on this day'),
    (['not', 'an', 'object'], 'JSON object'),
])
def test_bad_edits_are_rejected(server, body, message):
    status, answer = post(server, body)
    assert status == 400 and message in answer['error']


def test_flights_are_not_snapped(server, fetcher):
    flight = get_json(server, '/api/day?date=2019-07-03')['items'][1]
    status, answer = post(server, {'date': '2019-07-03', 'item_id': flight['id'], 'action': 'snap'})
    assert status == 409 and 'no roads to follow' in answer['error']
    assert fetcher.calls == []


def test_walks_are_matched_against_paths(server, fetcher):
    move = [i for i in get_json(server, '/api/day?date=2019-02-10')['items']
            if i['kind'] == 'move'][0]
    assert move['mode'] == 'walking'
    post(server, {'date': '2019-02-10', 'item_id': move['id'], 'action': 'snap'})
    assert {profile for _, profile in fetcher.calls} == {'foot'}


def test_wrong_mode_or_action_is_rejected(server):
    items = get_json(server, '/api/day?date=2019-07-03')['items']
    status, answer = post(server, {'date': '2019-07-03', 'item_id': items[1]['id'],
                                   'action': 'mode', 'mode': 'teleport'})
    assert status == 400
    status, answer = post(server, {'date': '2019-07-03', 'item_id': items[0]['id'],
                                   'action': 'mode', 'mode': 'walking'})
    assert status == 400


@pytest.mark.parametrize('headers', [
    {'X-Timeline-Edit': None},
    {'Content-Type': 'text/plain'},
    {'Content-Type': 'application/x-www-form-urlencoded'},
    {'Origin': 'https://evil.example'},
])
def test_edits_from_anywhere_but_the_page_are_refused(server, headers):
    """A web page elsewhere cannot send corrections through your browser."""
    status, _ = post(server, {'date': '2019-07-03', 'action': 'reset'}, headers=headers)
    assert status == 403


def test_oversized_and_unreadable_edits(server):
    status, _ = post(server, None, raw=b'x' * (70 * 1024))
    assert status == 413
    status, answer = post(server, None, raw=b'{not json')
    assert status == 400


def test_edits_are_kept_out_of_the_imported_history(server, tmp_path):
    import sqlite3
    stop_id = get_json(server, '/api/day?date=2019-07-03')['items'][0]['id']
    post(server, {'date': '2019-07-03', 'item_id': stop_id, 'action': 'rename', 'name': 'X'})
    with sqlite3.connect(str(tmp_path / 'map.db')) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    assert 'item_edits' not in tables
    with sqlite3.connect(str(tmp_path / 'edits.sqlite')) as conn:
        assert conn.execute('SELECT name FROM item_edits').fetchall() == [('X',)]
