import json
import zipfile

import pytest

from conftest import (ANDROID_EXPORT, CITY_HALL, HARBOUR_BRIDGE, IOS_EXPORT, LEGACY_RECORDS,
                      LEGACY_SEMANTIC, OPERA_HOUSE, SETTINGS, TIMELINE_EDITS, rows)
from main import import_paths
from services import parser_service
from services.parser_service import TimelineParserService


def only_source(path):
    sources = list(TimelineParserService().discover([path]))
    assert len(sources) == 1
    return sources[0]


@pytest.mark.parametrize('data, expected', [
    (ANDROID_EXPORT, parser_service.DEVICE_EXPORT),
    (IOS_EXPORT, parser_service.DEVICE_EXPORT),
    (LEGACY_RECORDS, parser_service.LEGACY_RECORDS),
    (LEGACY_SEMANTIC, parser_service.LEGACY_SEMANTIC),
    (TIMELINE_EDITS, parser_service.TIMELINE_EDITS),
    (SETTINGS, parser_service.SETTINGS),
    ({'unrelated': 1}, parser_service.UNKNOWN),
    ([1, 2, 3], parser_service.UNKNOWN),
    ([], parser_service.UNKNOWN),
])
def test_format_is_recognised_from_content_not_file_name(reader_mode, write_json, data, expected):
    assert only_source(write_json('anything.json', data)).format == expected


def test_file_that_is_not_json_is_skipped(reader_mode, tmp_path, repo, capsys):
    broken = tmp_path / 'broken.json'
    broken.write_text('{"locations": [', encoding='utf-8')
    not_json = tmp_path / 'notes.json'
    not_json.write_text('hello', encoding='utf-8')
    assert only_source(str(not_json)).format == parser_service.UNKNOWN
    if reader_mode == 'whole-file':
        assert only_source(str(broken)).format == parser_service.UNKNOWN
    else:
        # Recognised from its opening, then fails cleanly and stores nothing.
        with pytest.raises(Exception):
            import_paths([str(broken)], repo)
        assert rows(repo, 'SELECT COUNT(*) FROM source_files') == [(0,)]
        assert rows(repo, 'SELECT COUNT(*) FROM positions') == [(0,)]


def test_android_export(reader_mode, write_json, repo):
    import_paths([write_json('Timeline.json', ANDROID_EXPORT)], repo)

    visits = rows(repo, 'SELECT start_utc, end_utc, utc_offset_min, latitude, longitude, '
                        'place_id, semantic_type, probability FROM visits')
    assert visits == [('2024-04-30T22:00:00Z', '2024-04-30T23:30:00Z', 600,
                       OPERA_HOUSE[0], OPERA_HOUSE[1], 'place-opera', 'HOME', 0.8)]

    activities = rows(repo, 'SELECT start_utc, activity_type, distance_m, probability, '
                            'start_latitude, end_latitude FROM activities')
    assert activities == [('2024-04-30T23:30:00Z', 'WALKING', 650.5, 0.7,
                           OPERA_HOUSE[0], HARBOUR_BRIDGE[0])]

    path = rows(repo, 'SELECT time_utc, latitude FROM path_points ORDER BY t_ms')
    assert path == [('2024-04-30T23:30:00Z', -33.8568), ('2024-04-30T23:40:00Z', -33.8545),
                    ('2024-04-30T23:50:00Z', -33.8523)]

    positions = rows(repo, 'SELECT record_type, time_utc, latitude, longitude, accuracy_m, '
                           'source, altitude_m, speed_mps FROM positions')
    assert positions == [('raw_signal', '2024-04-30T22:15:00Z', OPERA_HOUSE[0], OPERA_HOUSE[1],
                          12.0, 'WIFI', 5.5, 0.0)]

    assert rows(repo, 'SELECT place_id, label, latitude FROM frequent_places') == [
        ('place-opera', 'HOME', OPERA_HOUSE[0])]

    skipped = json.loads(rows(repo, 'SELECT skipped FROM source_files')[0][0])
    assert skipped == {'raw signal that is not a position': 2,
                       'segment of a kind not kept': 1}


def test_ios_export(reader_mode, write_json, repo):
    import_paths([write_json('location-history.json', IOS_EXPORT)], repo)

    assert rows(repo, 'SELECT start_utc, utc_offset_min, latitude, longitude, place_id, '
                      'probability FROM visits') == [
        ('2024-06-09T22:00:00Z', 600, HARBOUR_BRIDGE[0], HARBOUR_BRIDGE[1], 'place-bridge', 0.6)]
    assert rows(repo, 'SELECT activity_type, distance_m, start_latitude, end_longitude '
                      'FROM activities') == [
        ('CYCLING', 700.25, HARBOUR_BRIDGE[0], OPERA_HOUSE[1])]
    # iOS path points carry minutes from the start of the segment, not a time.
    assert rows(repo, 'SELECT time_utc, latitude FROM path_points ORDER BY t_ms') == [
        ('2024-06-09T23:00:00Z', HARBOUR_BRIDGE[0]), ('2024-06-09T23:20:00Z', OPERA_HOUSE[0])]
    assert rows(repo, 'SELECT COUNT(*) FROM positions') == [(0,)]


def test_legacy_records(reader_mode, write_json, repo):
    import_paths([write_json('Records.json', LEGACY_RECORDS)], repo)

    positions = rows(repo, 'SELECT record_type, time_utc, t_ms, latitude, longitude, accuracy_m, '
                           'source, device_tag FROM positions ORDER BY t_ms')
    assert [p[0] for p in positions] == ['legacy_record'] * 3
    by_time = {p[1]: p for p in positions}
    assert by_time['2014-03-05T01:00:00Z'][2:] == (
        1_393_981_200_123, pytest.approx(OPERA_HOUSE[0]), pytest.approx(OPERA_HOUSE[1]),
        20.0, 'WIFI', '1234567')
    # Epoch-millisecond text from older exports.
    assert by_time['2014-03-05T06:13:20Z'][3] == pytest.approx(HARBOUR_BRIDGE[0])
    # The unsigned latitude comes back as a southern latitude.
    assert by_time['2014-03-06T01:00:00Z'][3] == pytest.approx(CITY_HALL[0])

    skipped = json.loads(rows(repo, 'SELECT skipped FROM source_files')[0][0])
    assert skipped == {'record without usable time or coordinates': 2}


def test_legacy_semantic(reader_mode, write_json, repo):
    import_paths([write_json('Semantic Location History/2019/2019_JULY.json',
                             LEGACY_SEMANTIC)], repo)

    assert rows(repo, 'SELECT start_utc, end_utc, latitude, name, address, place_id, '
                      'semantic_type, probability, confidence FROM visits') == [
        ('2019-07-01T00:00:00Z', '2019-07-03T02:00:00Z', pytest.approx(CITY_HALL[0]),
         'Brisbane City Hall', '64 Adelaide St, Brisbane City QLD 4000', 'place-city-hall',
         'TYPE_SEARCHED_ADDRESS', pytest.approx(0.875), 'HIGH_CONFIDENCE')]
    assert rows(repo, 'SELECT start_utc, end_utc, activity_type, distance_m, confidence '
                      'FROM activities') == [
        ('2019-07-03T02:00:00Z', '2019-07-03T03:30:00Z', 'FLYING', 733000.0, 'HIGH')]
    # Only the timed path is kept; the untimed inferred waypoints are not.
    assert rows(repo, 'SELECT time_utc FROM path_points ORDER BY t_ms') == [
        ('2019-07-03T02:05:00Z',), ('2019-07-03T03:20:00Z',)]
    skipped = json.loads(rows(repo, 'SELECT skipped FROM source_files')[0][0])
    assert skipped == {'timeline object of a kind not kept': 1}


def test_timeline_edits(reader_mode, write_json, repo):
    import_paths([write_json('Timeline Edits.json', TIMELINE_EDITS)], repo)
    assert rows(repo, 'SELECT record_type, time_utc, latitude, accuracy_m, source, device_tag, '
                      'speed_mps FROM positions') == [
        ('edits_raw_signal', '2024-02-02T03:00:00Z', pytest.approx(OPERA_HOUSE[0]), 15.0,
         'GPS', '42', 1.5)]


def test_settings_and_unrelated_files_store_nothing(reader_mode, write_json, repo, capsys):
    import_paths([write_json('Settings.json', SETTINGS),
                  write_json('package.json', {'name': 'x'})], repo)
    assert rows(repo, 'SELECT COUNT(*) FROM source_files') == [(0,)]
    output = capsys.readouterr().out
    assert output.count('Skipped') == 2


def test_folder_and_zip_are_searched(reader_mode, write_json, tmp_path, repo):
    write_json('Takeout/Location History/Records.json', LEGACY_RECORDS)
    write_json('Takeout/Location History/Settings.json', SETTINGS)
    archive = tmp_path / 'Takeout' / 'takeout-part-2.zip'
    with zipfile.ZipFile(archive, 'w') as bundle:
        bundle.writestr('Takeout/Location History/Semantic Location History/2019/2019_JULY.json',
                        json.dumps(LEGACY_SEMANTIC))
        bundle.writestr('Takeout/archive_browser.html', '<html></html>')
        bundle.writestr('__MACOSX/._junk.json', 'junk')

    import_paths([str(tmp_path / 'Takeout')], repo)

    sources = rows(repo, 'SELECT name, format FROM source_files ORDER BY id')
    assert [fmt for _, fmt in sources] == ['legacy_records', 'legacy_semantic']
    assert sources[1][0].endswith('takeout-part-2.zip!Takeout/Location History/'
                                  'Semantic Location History/2019/2019_JULY.json')
    assert rows(repo, 'SELECT COUNT(*) FROM positions') == [(3,)]
    assert rows(repo, 'SELECT COUNT(*) FROM visits') == [(1,)]


def test_same_file_is_imported_once(reader_mode, write_json, repo, capsys):
    path = write_json('Timeline.json', ANDROID_EXPORT)
    import_paths([path], repo)
    import_paths([path], repo)
    assert 'Already imported' in capsys.readouterr().out
    assert rows(repo, 'SELECT COUNT(*) FROM source_files') == [(1,)]
    assert rows(repo, 'SELECT COUNT(*) FROM path_points') == [(3,)]


def test_newer_export_repeating_old_history_adds_only_the_new_part(reader_mode, write_json, repo):
    import_paths([write_json('may.json', ANDROID_EXPORT)], repo)
    later = json.loads(json.dumps(ANDROID_EXPORT))
    later['semanticSegments'].append({
        'startTime': '2024-05-02T08:00:00.000+10:00',
        'endTime': '2024-05-02T09:00:00.000+10:00',
        'visit': {'topCandidate': {'placeId': 'place-bridge',
                                   'placeLocation': {'latLng': '-33.8523°, 151.2108°'}}},
    })
    import_paths([write_json('june.json', later)], repo)

    assert rows(repo, 'SELECT COUNT(*) FROM visits') == [(2,)]
    assert rows(repo, 'SELECT COUNT(*) FROM activities') == [(1,)]
    assert rows(repo, 'SELECT COUNT(*) FROM path_points') == [(3,)]
    assert rows(repo, 'SELECT COUNT(*) FROM positions') == [(1,)]
    assert rows(repo, 'SELECT records, new_records FROM source_files ORDER BY id')[1] == (8, 1)


def test_missing_path_is_reported(repo):
    with pytest.raises(FileNotFoundError):
        import_paths(['/no/such/file.json'], repo)
