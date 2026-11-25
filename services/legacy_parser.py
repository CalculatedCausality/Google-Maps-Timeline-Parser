"""Parsers for the older Google Takeout files.

- `Records.json` (or `Location History.json`): individual location readings.
- `Semantic Location History/YYYY/YYYY_MONTH.json`: inferred visits and movements.
- `Timeline Edits.json`: may carry some raw position signals.
"""
from typing import Any, Iterable, Iterator, Optional

from models.records import Activity, PathPoint, Position, Visit

from .device_parser import Skipped
from .parsing_utils import e7_pair, parse_time, to_float


def parse_records(locations: Iterable[Any], skipped: Skipped) -> Iterator[Position]:
    for record in locations:
        if not isinstance(record, dict):
            skipped.add('record that is not an object')
            continue
        coordinates = e7_pair(record.get('latitudeE7'), record.get('longitudeE7'))
        moment = parse_time(record.get('timestamp', record.get('timestampMs')))
        if coordinates is None or moment is None:
            skipped.add('record without usable time or coordinates')
            continue
        yield Position(
            record_type='legacy_record',
            t_ms=moment[0],
            latitude=coordinates[0],
            longitude=coordinates[1],
            accuracy_m=to_float(record.get('accuracy')),
            source=_text(record.get('source')),
            device_tag=_text(record.get('deviceTag')),
            altitude_m=to_float(record.get('altitude')),
            speed_mps=to_float(record.get('velocity')),
        )


def parse_semantic(objects: Iterable[Any], skipped: Skipped) -> Iterator[Any]:
    for item in objects:
        if not isinstance(item, dict):
            skipped.add('timeline object that is not an object')
            continue
        if isinstance(item.get('placeVisit'), dict):
            yield from _place_visit(item['placeVisit'], skipped)
        elif isinstance(item.get('activitySegment'), dict):
            yield from _activity_segment(item['activitySegment'], skipped)
        else:
            skipped.add('timeline object of a kind not kept')


def parse_edits(edits: Iterable[Any], skipped: Skipped) -> Iterator[Position]:
    for edit in edits:
        raw = edit.get('rawSignal') if isinstance(edit, dict) else None
        signal = raw.get('signal') if isinstance(raw, dict) else None
        position = signal.get('position') if isinstance(signal, dict) else None
        if not isinstance(position, dict):
            skipped.add('edit entry that is not a position')
            continue
        point = position.get('point')
        coordinates = (
            e7_pair(point.get('latE7'), point.get('lngE7'))
            if isinstance(point, dict) else None
        )
        moment = parse_time(position.get('timestamp'))
        if coordinates is None or moment is None:
            skipped.add('edit position without usable time or coordinates')
            continue
        accuracy_mm = to_float(position.get('accuracyMm'))
        yield Position(
            record_type='edits_raw_signal',
            t_ms=moment[0],
            latitude=coordinates[0],
            longitude=coordinates[1],
            accuracy_m=None if accuracy_mm is None else accuracy_mm / 1000.0,
            source=_text(position.get('source')),
            device_tag=_text(edit.get('deviceId')),
            altitude_m=to_float(position.get('altitudeMeters')),
            speed_mps=to_float(position.get('speedMetersPerSecond')),
        )


# ----------------------------------------------------------------------
# Private helpers
# ----------------------------------------------------------------------

def _text(value: Any) -> Optional[str]:
    return None if value is None else str(value)


def _duration(duration: Any):
    if not isinstance(duration, dict):
        return None, None
    start = parse_time(duration.get('startTimestamp', duration.get('startTimestampMs')))
    end = parse_time(duration.get('endTimestamp', duration.get('endTimestampMs')))
    return start, end


def _place_visit(visit: dict, skipped: Skipped) -> Iterator[Visit]:
    start, end = _duration(visit.get('duration'))
    if start is None or end is None:
        skipped.add('visit without usable times')
        return
    location = visit.get('location')
    if not isinstance(location, dict):
        location = {}
    coordinates = e7_pair(location.get('latitudeE7'), location.get('longitudeE7'))
    if coordinates is None:
        coordinates = e7_pair(visit.get('centerLatE7'), visit.get('centerLngE7'))
    probability = to_float(location.get('locationConfidence'))
    if probability is None:
        probability = to_float(visit.get('visitConfidence'))
    yield Visit(
        start_ms=start[0],
        end_ms=end[0],
        utc_offset_min=None,
        latitude=coordinates[0] if coordinates else None,
        longitude=coordinates[1] if coordinates else None,
        place_id=_text(location.get('placeId')),
        semantic_type=_text(location.get('semanticType')),
        name=_text(location.get('name')),
        address=_text(location.get('address')),
        # Old exports score confidence out of 100; phone exports use 0 to 1.
        probability=None if probability is None else probability / 100.0,
        confidence=_text(visit.get('placeConfidence')),
    )


def _activity_segment(segment: dict, skipped: Skipped) -> Iterator[Any]:
    start, end = _duration(segment.get('duration'))
    if start is None or end is None:
        skipped.add('activity without usable times')
        return
    begin = _endpoint(segment.get('startLocation'))
    finish = _endpoint(segment.get('endLocation'))
    yield Activity(
        start_ms=start[0],
        end_ms=end[0],
        utc_offset_min=None,
        activity_type=str(segment.get('activityType') or 'UNKNOWN').upper(),
        distance_m=to_float(segment.get('distance')),
        confidence=_text(segment.get('confidence')),
        start_latitude=begin[0] if begin else None,
        start_longitude=begin[1] if begin else None,
        end_latitude=finish[0] if finish else None,
        end_longitude=finish[1] if finish else None,
    )
    # Only the timed, measured path is kept. `waypointPath` is a route Google
    # inferred afterwards and carries no times, so it is not treated as a track.
    raw_path = segment.get('simplifiedRawPath')
    points = raw_path.get('points') if isinstance(raw_path, dict) else None
    for point in points or []:
        if not isinstance(point, dict):
            continue
        coordinates = e7_pair(point.get('latE7'), point.get('lngE7'))
        moment = parse_time(point.get('timestamp', point.get('timestampMs')))
        if coordinates is None or moment is None:
            skipped.add('path point without usable time or coordinates')
            continue
        yield PathPoint(t_ms=moment[0], latitude=coordinates[0], longitude=coordinates[1])


def _endpoint(location: Any):
    if not isinstance(location, dict):
        return None
    return e7_pair(location.get('latitudeE7'), location.get('longitudeE7'))
