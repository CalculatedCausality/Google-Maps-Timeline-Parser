import pytest

from services.parsing_utils import e7, e7_pair, iso_utc, parse_latlng, parse_time

MIDNIGHT_UTC = 1_714_521_600_000  # 2024-05-01T00:00:00Z


@pytest.mark.parametrize('text, expected', [
    ('2024-05-01T00:00:00Z', (MIDNIGHT_UTC, 0)),
    ('2024-05-01T00:00:00.000Z', (MIDNIGHT_UTC, 0)),
    ('2024-05-01T10:00:00.000+10:00', (MIDNIGHT_UTC, 600)),
    ('2024-05-01T10:00:00+1000', (MIDNIGHT_UTC, 600)),
    ('2024-04-30T14:30:00-09:30', (MIDNIGHT_UTC, -570)),
    ('2024-05-01T00:00:00.5Z', (MIDNIGHT_UTC + 500, 0)),
    ('2024-05-01T00:00:00.123456789Z', (MIDNIGHT_UTC + 123, 0)),
    ('2024-05-01T00:00:00', (MIDNIGHT_UTC, None)),
    ('1714521600000', (MIDNIGHT_UTC, None)),
    (1714521600000, (MIDNIGHT_UTC, None)),
])
def test_parse_time_reads_every_form(text, expected):
    assert parse_time(text) == expected


@pytest.mark.parametrize('bad', [None, '', 'yesterday', '2024-13-45T00:00:00Z', True, {}])
def test_parse_time_rejects_unreadable_values(bad):
    assert parse_time(bad) is None


def test_iso_utc_round_trip():
    assert iso_utc(MIDNIGHT_UTC) == '2024-05-01T00:00:00Z'


def test_e7_converts_and_unwraps_unsigned_negative_values():
    assert e7(-338568000) == pytest.approx(-33.8568)
    assert e7(-338568000 + 2 ** 32) == pytest.approx(-33.8568)
    assert e7('1512153000') == pytest.approx(151.2153)
    assert e7(None) is None


def test_e7_pair_rejects_impossible_coordinates():
    assert e7_pair(999999999999, 0) is None
    assert e7_pair(None, 1512153000) is None
    assert e7_pair(-338568000, 1512153000) == pytest.approx((-33.8568, 151.2153))


@pytest.mark.parametrize('value', [
    '-33.8568°, 151.2153°',
    '-33.8568Â°, 151.2153Â°',          # degree sign mangled by a wrong encoding
    'geo:-33.8568,151.2153',
    {'latLng': '-33.8568°, 151.2153°'},
    {'LatLng': '-33.8568°, 151.2153°'},
    {'latitudeE7': -338568000, 'longitudeE7': 1512153000},
])
def test_parse_latlng_reads_android_and_ios_forms(value):
    assert parse_latlng(value) == pytest.approx((-33.8568, 151.2153))


@pytest.mark.parametrize('value', [None, '', 'geo:', '123.0, 500.0', {}, 12])
def test_parse_latlng_rejects_unusable_values(value):
    assert parse_latlng(value) is None
