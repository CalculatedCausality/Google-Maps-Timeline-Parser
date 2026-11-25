"""Shared test set-up. Every coordinate here is a public landmark, not real data."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.connection import DatabaseConnection  # noqa: E402
from repositories.timeline_repository import TimelineRepository  # noqa: E402
from services import parser_service  # noqa: E402

OPERA_HOUSE = (-33.8568, 151.2153)
HARBOUR_BRIDGE = (-33.8523, 151.2108)
CITY_HALL = (-27.4689, 153.0235)   # Brisbane


def e7(value: float) -> int:
    return round(value * 1e7)


ANDROID_EXPORT = {
    'semanticSegments': [
        {
            'startTime': '2024-05-01T08:00:00.000+10:00',
            'endTime': '2024-05-01T09:30:00.000+10:00',
            'startTimeTimezoneUtcOffsetMinutes': 600,
            'endTimeTimezoneUtcOffsetMinutes': 600,
            'visit': {
                'hierarchyLevel': 0,
                'probability': 0.9,
                'topCandidate': {
                    'placeId': 'place-opera',
                    'semanticType': 'HOME',
                    'probability': 0.8,
                    'placeLocation': {'latLng': '-33.8568°, 151.2153°'},
                },
            },
        },
        {
            'startTime': '2024-05-01T09:30:00.000+10:00',
            'endTime': '2024-05-01T09:50:00.000+10:00',
            'activity': {
                'start': {'latLng': '-33.8568°, 151.2153°'},
                'end': {'latLng': '-33.8523°, 151.2108°'},
                'distanceMeters': 650.5,
                'topCandidate': {'type': 'WALKING', 'probability': 0.7},
            },
        },
        {
            'startTime': '2024-05-01T09:30:00.000+10:00',
            'endTime': '2024-05-01T09:50:00.000+10:00',
            'timelinePath': [
                {'point': '-33.8568°, 151.2153°', 'time': '2024-05-01T09:30:00.000+10:00'},
                {'point': '-33.8545°, 151.2130°', 'time': '2024-05-01T09:40:00.000+10:00'},
                {'point': '-33.8523°, 151.2108°', 'time': '2024-05-01T09:50:00.000+10:00'},
            ],
        },
        {
            'startTime': '2024-05-01T00:00:00.000+10:00',
            'endTime': '2024-05-02T00:00:00.000+10:00',
            'timelineMemory': {'trip': {'distanceFromOriginKms': 10}},
        },
    ],
    'rawSignals': [
        {'position': {
            'LatLng': '-33.8568°, 151.2153°',
            'accuracyMeters': 12,
            'altitudeMeters': 5.5,
            'source': 'WIFI',
            'timestamp': '2024-05-01T08:15:00.000+10:00',
            'speedMetersPerSecond': 0.0,
        }},
        {'wifiScan': {'deliveryTime': '2024-05-01T08:15:00.000+10:00', 'devicesRecords': []}},
        {'activityRecord': {'timestamp': '2024-05-01T08:15:00.000+10:00',
                            'probableActivities': [{'type': 'STILL', 'confidence': 0.9}]}},
    ],
    'userLocationProfile': {
        'frequentPlaces': [
            {'placeId': 'place-opera', 'placeLocation': '-33.8568°, 151.2153°', 'label': 'HOME'},
        ],
    },
}

IOS_EXPORT = [
    {
        'startTime': '2024-06-10T08:00:00.000+10:00',
        'endTime': '2024-06-10T09:00:00.000+10:00',
        'visit': {
            'hierarchyLevel': '0',
            'probability': '0.75',
            'topCandidate': {
                'placeID': 'place-bridge',
                'semanticType': 'Unknown',
                'probability': '0.6',
                'placeLocation': 'geo:-33.8523,151.2108',
            },
        },
    },
    {
        'startTime': '2024-06-10T09:00:00.000+10:00',
        'endTime': '2024-06-10T09:20:00.000+10:00',
        'activity': {
            'start': 'geo:-33.8523,151.2108',
            'end': 'geo:-33.8568,151.2153',
            'distanceMeters': '700.25',
            'topCandidate': {'type': 'cycling', 'probability': '0.5'},
        },
    },
    {
        'startTime': '2024-06-10T09:00:00.000+10:00',
        'endTime': '2024-06-10T09:20:00.000+10:00',
        'timelinePath': [
            {'point': 'geo:-33.8523,151.2108', 'durationMinutesOffsetFromStartTime': '0'},
            {'point': 'geo:-33.8568,151.2153', 'durationMinutesOffsetFromStartTime': '20'},
        ],
    },
]

LEGACY_RECORDS = {
    'locations': [
        {   # current shape
            'latitudeE7': e7(OPERA_HOUSE[0]), 'longitudeE7': e7(OPERA_HOUSE[1]),
            'accuracy': 20, 'source': 'WIFI', 'deviceTag': 1234567,
            'timestamp': '2014-03-05T01:00:00.123Z', 'altitude': 10, 'velocity': 1,
        },
        {   # pre-2022 shape: epoch milliseconds as text
            'latitudeE7': e7(HARBOUR_BRIDGE[0]), 'longitudeE7': e7(HARBOUR_BRIDGE[1]),
            'accuracy': 900, 'timestampMs': '1394000000000',
        },
        {   # negative latitude written as an unsigned 32-bit number
            'latitudeE7': e7(CITY_HALL[0]) + 2 ** 32, 'longitudeE7': e7(CITY_HALL[1]),
            'timestamp': '2014-03-06T01:00:00Z',
        },
        {'latitudeE7': 999999999999, 'longitudeE7': 0, 'timestamp': '2014-03-07T01:00:00Z'},
        {'accuracy': 5, 'timestamp': '2014-03-08T01:00:00Z'},
    ],
}

LEGACY_SEMANTIC = {
    'timelineObjects': [
        {'placeVisit': {
            'location': {
                'latitudeE7': e7(CITY_HALL[0]), 'longitudeE7': e7(CITY_HALL[1]),
                'placeId': 'place-city-hall', 'name': 'Brisbane City Hall',
                'address': '64 Adelaide St, Brisbane City QLD 4000',
                'semanticType': 'TYPE_SEARCHED_ADDRESS', 'locationConfidence': 87.5,
            },
            'duration': {'startTimestamp': '2019-07-01T00:00:00Z',
                         'endTimestamp': '2019-07-03T02:00:00Z'},
            'placeConfidence': 'HIGH_CONFIDENCE',
        }},
        {'activitySegment': {
            'startLocation': {'latitudeE7': e7(CITY_HALL[0]), 'longitudeE7': e7(CITY_HALL[1])},
            'endLocation': {'latitudeE7': e7(OPERA_HOUSE[0]), 'longitudeE7': e7(OPERA_HOUSE[1])},
            'duration': {'startTimestampMs': '1562119200000', 'endTimestampMs': '1562124600000'},
            'distance': 733000, 'activityType': 'FLYING', 'confidence': 'HIGH',
            'waypointPath': {'waypoints': [{'latE7': e7(CITY_HALL[0]), 'lngE7': e7(CITY_HALL[1])}]},
            'simplifiedRawPath': {'points': [
                {'latE7': e7(CITY_HALL[0]), 'lngE7': e7(CITY_HALL[1]),
                 'timestamp': '2019-07-03T02:05:00Z', 'accuracyMeters': 30},
                {'latE7': e7(OPERA_HOUSE[0]), 'lngE7': e7(OPERA_HOUSE[1]),
                 'timestampMs': '1562124000000', 'accuracyMeters': 30},
            ]},
        }},
        {'somethingElse': {}},
    ],
}

TIMELINE_EDITS = {
    'timelineEdits': [
        {'deviceId': '42', 'rawSignal': {'signal': {'position': {
            'point': {'latE7': e7(OPERA_HOUSE[0]), 'lngE7': e7(OPERA_HOUSE[1])},
            'accuracyMm': 15000, 'altitudeMeters': 4.0, 'source': 'GPS',
            'timestamp': '2024-02-02T03:00:00Z', 'speedMetersPerSecond': 1.5,
        }}}},
        {'deviceId': '42', 'placeAggregates': {'placeAggregateInfo': []}},
    ],
}

SETTINGS = {'createdTime': '2013-01-01T00:00:00Z', 'historyEnabled': True, 'deviceSettings': []}


@pytest.fixture(params=['streaming', 'whole-file'])
def reader_mode(request, monkeypatch):
    """Run format tests both with and without the optional ijson reader."""
    if request.param == 'whole-file':
        monkeypatch.setattr(parser_service, 'ijson', None)
    elif parser_service.ijson is None:
        pytest.skip('ijson is not installed')
    return request.param


@pytest.fixture
def repo(tmp_path):
    repository = TimelineRepository(DatabaseConnection(str(tmp_path / 'test.db')))
    repository.create_tables()
    return repository


@pytest.fixture
def write_json(tmp_path):
    def write(name: str, data) -> str:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding='utf-8')
        return str(path)
    return write


def rows(repo, sql, params=()):
    with repo._db.get_connection() as conn:
        return conn.execute(sql, params).fetchall()
