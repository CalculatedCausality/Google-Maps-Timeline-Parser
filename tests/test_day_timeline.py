import xml.etree.ElementTree as ET
from datetime import date
from zoneinfo import ZoneInfo

import pytest

from conftest import ANDROID_EXPORT, HARBOUR_BRIDGE, OPERA_HOUSE, e7
from main import import_paths, main
from repositories.edits_repository import EditsRepository, nearest_named
from services.day_timeline import DayTimelineService, google_mode, guess_mode
from services.export_service import ExportService

SYDNEY = ZoneInfo('Australia/Sydney')
GPX = '{http://www.topografix.com/GPX/1/1}'


def reading(text, place, accuracy=10):
    return {'latitudeE7': e7(place[0]), 'longitudeE7': e7(place[1]),
            'accuracy': accuracy, 'timestamp': text}


def toward(a, b, fraction):
    return (a[0] + (b[0] - a[0]) * fraction, a[1] + (b[1] - a[1]) * fraction)


def readings_day():
    """30 min at the Opera House, a 12-minute walk, 40 min at the Harbour Bridge (UTC)."""
    records = [reading(f'2020-03-01T01:{m:02d}:00Z', OPERA_HOUSE) for m in range(0, 31, 2)]
    records += [reading(f'2020-03-01T01:{30 + m:02d}:00Z',
                        toward(OPERA_HOUSE, HARBOUR_BRIDGE, m / 12)) for m in range(1, 12)]
    records += [reading(f'2020-03-01T{1 + (42 + m) // 60:02d}:{(42 + m) % 60:02d}:00Z',
                        HARBOUR_BRIDGE) for m in range(0, 41, 2)]
    return {'locations': records}


@pytest.fixture
def edits(tmp_path):
    return EditsRepository(str(tmp_path / 'edits.sqlite'))


@pytest.fixture
def timeline(repo, edits):
    return DayTimelineService(repo, edits, SYDNEY)


def test_stops_are_found_in_plain_readings(write_json, repo, timeline):
    import_paths([write_json('Records.json', readings_day())], repo)
    day = timeline.build(date(2020, 3, 1))

    assert [i['kind'] for i in day['items']] == ['stop', 'move', 'stop']
    first, walk, second = day['items']
    assert first['name'] == 'Unnamed place' and first['name_source'] == 'unknown'
    # Sydney is 11 hours ahead; the walk's first step is still within 100 m of the stop.
    assert first['start_text'] == '12:00' and first['end_text'] == '12:31'
    assert walk['mode'] == 'walking' and walk['mode_source'] == 'guess'
    assert 0.4 < walk['km'] < 0.8      # the stops take in the first and last steps
    assert walk['line'][0] == pytest.approx([first['lat'], first['lng']], abs=1e-5)
    assert walk['line'][-1] == pytest.approx([second['lat'], second['lng']], abs=1e-5)
    assert len(walk['times']) == len(walk['line'])
    assert day['summary'] == {'stops': 2, 'moves': 1, 'km': walk['km'], 'edited': False,
                              'source': 'readings'}


def test_google_visits_and_movements_are_used_when_present(write_json, repo, timeline):
    import_paths([write_json('Timeline.json', ANDROID_EXPORT)], repo)
    day = timeline.build(date(2024, 5, 1))

    stop, walk = day['items']
    assert (stop['name'], stop['name_source']) == ('Home', 'google')
    assert stop['start_text'] == '08:00' and stop['end_text'] == '09:30'
    assert (walk['mode'], walk['mode_source']) == ('walking', 'google')
    assert walk['google_km'] == pytest.approx(0.6505)
    assert len(walk['line']) >= 2 and not walk['estimated']
    assert day['summary']['source'] == 'google'


def test_a_journey_is_added_between_two_stops_far_apart(write_json, repo, timeline):
    visits = {'timelineObjects': [
        {'placeVisit': {'location': {'latitudeE7': e7(OPERA_HOUSE[0]),
                                     'longitudeE7': e7(OPERA_HOUSE[1]), 'name': 'Opera House'},
                        'duration': {'startTimestamp': '2021-06-01T00:00:00Z',
                                     'endTimestamp': '2021-06-01T01:00:00Z'}}},
        {'placeVisit': {'location': {'latitudeE7': e7(HARBOUR_BRIDGE[0]),
                                     'longitudeE7': e7(HARBOUR_BRIDGE[1]), 'name': 'Bridge'},
                        'duration': {'startTimestamp': '2021-06-01T01:20:00Z',
                                     'endTimestamp': '2021-06-01T02:00:00Z'}}},
    ]}
    import_paths([write_json('2021_JUNE.json', visits)], repo)
    items = timeline.build(date(2021, 6, 1))['items']
    assert [i['kind'] for i in items] == ['stop', 'move', 'stop']
    assert items[1]['estimated']                    # no readings: a straight line
    assert items[1]['mode_source'] == 'guess'


def test_corrections_apply_on_top_and_reset(write_json, repo, timeline, edits):
    import_paths([write_json('Records.json', readings_day())], repo)
    day = date(2020, 3, 1)
    first, walk, second = timeline.build(day)['items']

    edits.set_name('2020-03-01', first['id'], 'Concert')
    edits.set_mode('2020-03-01', walk['id'], 'running')
    edits.set_hidden('2020-03-01', second['id'], True)
    edits.set_geometry('2020-03-01', walk['id'],
                       [(walk['times'][0], *OPERA_HOUSE), (walk['times'][-1], *HARBOUR_BRIDGE)])
    built = timeline.build(day)
    first, walk, second = built['items']
    assert (first['name'], first['name_source']) == ('Concert', 'you')
    assert (walk['mode'], walk['mode_source'], walk['snapped']) == ('running', 'you', True)
    assert len(walk['line']) == 2
    assert second['hidden'] and built['summary']['stops'] == 1 and built['summary']['edited']

    assert edits.days_with_edits() == ['2020-03-01']
    assert edits.reset_day('2020-03-01') == 3
    assert not timeline.build(day)['summary']['edited']


def test_clearing_the_last_correction_removes_its_row(edits):
    edits.set_name('2020-01-01', 'stop-1', 'A')
    edits.set_name('2020-01-01', 'stop-1', None)
    assert edits.for_day('2020-01-01') == {}


def test_named_places_pick_the_nearest_circle():
    places = [(OPERA_HOUSE[0], OPERA_HOUSE[1], 75.0, 'Old name'),
              (OPERA_HOUSE[0], OPERA_HOUSE[1], 75.0, 'New name'),
              (HARBOUR_BRIDGE[0], HARBOUR_BRIDGE[1], 75.0, 'Bridge')]
    assert nearest_named(places, *OPERA_HOUSE) == 'New name'
    assert nearest_named(places, *HARBOUR_BRIDGE) == 'Bridge'
    assert nearest_named(places, -27.0, 153.0) is None


@pytest.mark.parametrize('google, ours', [
    ('WALKING', 'walking'), ('in passenger vehicle', 'driving'), ('IN_BUS', 'bus'),
    ('IN_SUBWAY', 'metro'), ('FLYING', 'flying'), ('cycling', 'cycling'),
    ('SOMETHING_NEW', 'unknown'), (None, 'unknown'),
])
def test_google_modes(google, ours):
    assert google_mode(google) == ours


def test_mode_guess_from_speed():
    assert guess_mode(1, 0.5) == 'walking'
    assert guess_mode(10, 0.5) == 'cycling'
    assert guess_mode(50, 0.5) == 'driving'
    assert guess_mode(800, 1) == 'flying'
    assert guess_mode(1, 0) == 'unknown'


def test_timeline_gpx(write_json, repo, timeline, edits, tmp_path):
    import_paths([write_json('Records.json', readings_day())], repo)
    day = date(2020, 3, 1)
    first, _, second = timeline.build(day)['items']
    edits.set_name('2020-03-01', first['id'], 'Fish & <chips>')
    edits.set_hidden('2020-03-01', second['id'], True)

    output = tmp_path / 'timeline.gpx'
    counts = ExportService().export_timeline_gpx([timeline.build(day)], str(output))
    assert counts == (1, 1, 1)
    root = ET.parse(output).getroot()
    waypoint = root.find(f'{GPX}wpt')
    assert waypoint.find(f'{GPX}name').text == 'Fish & <chips>'
    track = root.find(f'{GPX}trk')
    assert track.find(f'{GPX}type').text == 'walking'
    points = track.findall(f'{GPX}trkseg/{GPX}trkpt')
    assert len(points) >= 2 and points[0].find(f'{GPX}time').text.startswith('2020-03-01T01:')
    # Waypoints come before tracks, as GPX requires.
    assert [child.tag for child in root][:1] == [f'{GPX}wpt']


def test_command_line_timeline_gpx_uses_the_edits_file(write_json, tmp_path, capsys):
    db = str(tmp_path / 'cli.db')
    output = str(tmp_path / 'out.gpx')
    assert main([write_json('Records.json', readings_day()), '--db', db,
                 '--tz', 'Australia/Sydney', '--timeline-gpx', output]) == 0
    assert '(1 days, 1 journeys, 2 stops)' in capsys.readouterr().out
    assert (tmp_path / 'cli-edits.sqlite').exists()
    assert main(['--db', db, '--timeline-gpx', output, '--from', '2030-01-01']) == 0
    assert '(0 days, 0 journeys, 0 stops)' in capsys.readouterr().out
