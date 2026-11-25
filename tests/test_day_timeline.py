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


# ----------------------------------------------------------------------
# Changing a day's shape: times, splitting, merging
# ----------------------------------------------------------------------

from services.day_timeline import ShapeError  # noqa: E402

DAY = date(2020, 3, 1)


@pytest.fixture
def shaped(write_json, repo, timeline):
    import_paths([write_json('Records.json', readings_day())], repo)
    return timeline


def local(timeline, text):
    return timeline.parse_local(f'2020-03-01T{text}')


def shape(day):
    return [(i['kind'], i['start_text'], i['end_text']) for i in day['items']]


def test_editing_a_place_s_times_moves_its_neighbours(shaped, edits):
    first, walk, second = shaped.build(DAY)['items']
    assert shape(shaped.build(DAY)) == [('stop', '12:00', '12:31'), ('move', '12:31', '12:40'),
                                        ('stop', '12:40', '13:22')]
    shaped.set_times(DAY, first['id'], local(shaped, '11:50'), local(shaped, '12:20'))
    day = shaped.build(DAY)
    assert shape(day) == [('stop', '11:50', '12:20'), ('move', '12:20', '12:40'),
                          ('stop', '12:40', '13:22')]
    assert day['summary']['source'] == 'plan' and day['summary']['edited']
    # The journey's line is redrawn from readings for its new, longer window.
    assert day['items'][1]['line'][0] == pytest.approx([first['lat'], first['lng']], abs=1e-5)
    assert edits.plan('2020-03-01') is not None


@pytest.mark.parametrize('start, end, message', [
    ('12:40', '12:35', 'at least a minute after'),
    ('12:32', '13:30', 'after the one below'),
])
def test_impossible_times_are_refused(shaped, start, end, message):
    walk = shaped.build(DAY)['items'][1]
    with pytest.raises(ShapeError, match=message):
        shaped.set_times(DAY, walk['id'], local(shaped, start), local(shaped, end))


def test_changing_times_forgets_a_snapped_line(shaped, edits):
    walk = shaped.build(DAY)['items'][1]
    edits.set_geometry('2020-03-01', walk['id'], [(walk['times'][0], *OPERA_HOUSE),
                                                  (walk['times'][-1], *HARBOUR_BRIDGE)])
    assert shaped.build(DAY)['items'][1]['snapped']
    shaped.set_times(DAY, walk['id'], local(shaped, '12:32'), local(shaped, '12:40'))
    assert not shaped.build(DAY)['items'][1]['snapped']


def test_split_a_journey_keeps_its_mode_on_both_parts(shaped, edits):
    walk = shaped.build(DAY)['items'][1]
    edits.set_mode('2020-03-01', walk['id'], 'running')
    shaped.split_move(DAY, walk['id'], local(shaped, '12:36'))
    day = shaped.build(DAY)
    assert shape(day) == [('stop', '12:00', '12:31'), ('move', '12:31', '12:36'),
                          ('move', '12:36', '12:40'), ('stop', '12:40', '13:22')]
    assert [i['mode'] for i in day['items'] if i['kind'] == 'move'] == ['running', 'running']
    first, second = [i for i in day['items'] if i['kind'] == 'move']
    assert first['id'] == walk['id'] and second['id'] != walk['id']
    # The parts meet without doubling back: together they are no longer than the whole.
    assert first['km'] + second['km'] <= walk['km'] + 0.05
    assert first['line'][-1] == second['line'][0]
    with pytest.raises(ShapeError, match='inside the journey'):
        shaped.split_move(DAY, first['id'], local(shaped, '12:31'))


def test_add_a_stop_during_a_journey(shaped, edits):
    walk = shaped.build(DAY)['items'][1]
    stop_id = shaped.add_stop(DAY, walk['id'], local(shaped, '12:35'), local(shaped, '12:37'),
                              'Coffee cart')
    day = shaped.build(DAY)
    assert shape(day) == [('stop', '12:00', '12:31'), ('move', '12:31', '12:35'),
                          ('stop', '12:35', '12:37'), ('move', '12:37', '12:40'),
                          ('stop', '12:40', '13:22')]
    added = day['items'][2]
    assert added['id'] == stop_id and (added['name'], added['name_source']) == ('Coffee cart', 'you')
    # Placed where the readings were at that time: part-way between the two ends.
    assert min(OPERA_HOUSE[0], HARBOUR_BRIDGE[0]) < added['lat'] < max(OPERA_HOUSE[0], HARBOUR_BRIDGE[0])
    with pytest.raises(ShapeError):
        shaped.add_stop(DAY, added['id'], local(shaped, '12:35'), local(shaped, '12:36'), None)


def test_merge_two_places_and_two_journeys(shaped, edits):
    walk = shaped.build(DAY)['items'][1]
    shaped.add_stop(DAY, walk['id'], local(shaped, '12:35'), local(shaped, '12:37'), 'Cart')
    first_walk = shaped.build(DAY)['items'][1]
    shaped.merge_next(DAY, first_walk['id'])
    assert shape(shaped.build(DAY)) == [('stop', '12:00', '12:31'), ('move', '12:31', '12:40'),
                                        ('stop', '12:40', '13:22')]
    assert 'Cart' not in [i['name'] for i in shaped.build(DAY)['items'] if i['kind'] == 'stop']

    first = shaped.build(DAY)['items'][0]
    shaped.merge_next(DAY, first['id'])
    assert shape(shaped.build(DAY)) == [('stop', '12:00', '13:22')]
    with pytest.raises(ShapeError, match='no later place'):
        shaped.merge_next(DAY, first['id'])


def test_remove_a_place_and_join_the_journeys(shaped):
    walk = shaped.build(DAY)['items'][1]
    stop_id = shaped.add_stop(DAY, walk['id'], local(shaped, '12:35'), local(shaped, '12:37'), None)
    shaped.remove_stop(DAY, stop_id)
    assert shape(shaped.build(DAY)) == [('stop', '12:00', '12:31'), ('move', '12:31', '12:40'),
                                        ('stop', '12:40', '13:22')]
    # A place at the end of the day: the journey before it takes its time.
    last = shaped.build(DAY)['items'][-1]
    shaped.remove_stop(DAY, last['id'])
    assert shape(shaped.build(DAY)) == [('stop', '12:00', '12:31'), ('move', '12:31', '13:22')]


def test_reset_brings_back_the_recorded_day(shaped, edits):
    first = shaped.build(DAY)['items'][0]
    shaped.merge_next(DAY, first['id'])
    assert len(shaped.build(DAY)['items']) == 1
    assert edits.days_with_edits() == ['2020-03-01']
    edits.reset_day('2020-03-01')
    assert len(shaped.build(DAY)['items']) == 3
    assert edits.plan('2020-03-01') is None and edits.days_with_edits() == []


def test_local_times_are_read_in_the_chosen_zone(shaped):
    assert shaped.parse_local('2020-03-01T12:00') == 1_583_024_400_000   # 01:00 UTC
    with pytest.raises(ValueError):
        shaped.parse_local('noon')
