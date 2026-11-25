"""Parser for the files a phone exports ('Export Timeline data').

Android usually writes an object with `semanticSegments`, `rawSignals` and
`userLocationProfile`. iOS usually writes a bare list of segments. Field
spellings and number formats differ between the two, so everything here is
read leniently. These shapes are observed, not published by Google.
"""
from typing import Any, Dict, Iterable, Iterator, Optional

from models.records import Activity, FrequentPlace, PathPoint, Position, Visit

from .parsing_utils import parse_latlng, parse_time, to_float, to_int


class Skipped:
    """Counts records that were present but could not be read or are not kept."""

    def __init__(self) -> None:
        self.counts: Dict[str, int] = {}

    def add(self, reason: str, amount: int = 1) -> None:
        self.counts[reason] = self.counts.get(reason, 0) + amount


def parse_segments(segments: Iterable[Any], skipped: Skipped) -> Iterator[Any]:
    for segment in segments:
        if not isinstance(segment, dict):
            skipped.add('segment that is not an object')
            continue
        start = parse_time(segment.get('startTime'))
        end = parse_time(segment.get('endTime'))
        offset = to_int(segment.get('startTimeTimezoneUtcOffsetMinutes'))
        if offset is None and start is not None:
            offset = start[1]

        recognised = False
        if 'visit' in segment:
            recognised = True
            visit = _visit(segment['visit'], start, end, offset)
            if visit is None:
                skipped.add('visit without usable times')
            else:
                yield visit
        if 'activity' in segment:
            recognised = True
            activity = _activity(segment['activity'], start, end, offset)
            if activity is None:
                skipped.add('activity without usable times')
            else:
                yield activity
        if 'timelinePath' in segment:
            recognised = True
            yield from _path(segment['timelinePath'], start, skipped)
        if not recognised:
            # For example `timelineMemory` trip summaries.
            skipped.add('segment of a kind not kept')


def parse_raw_signals(signals: Iterable[Any], skipped: Skipped) -> Iterator[Position]:
    for signal in signals:
        position = signal.get('position') if isinstance(signal, dict) else None
        if not isinstance(position, dict):
            # Wi-Fi scans and motion classifications are not locations.
            skipped.add('raw signal that is not a position')
            continue
        coordinates = parse_latlng(position)
        moment = parse_time(position.get('timestamp'))
        if coordinates is None or moment is None:
            skipped.add('raw position without usable time or coordinates')
            continue
        yield Position(
            record_type='raw_signal',
            t_ms=moment[0],
            latitude=coordinates[0],
            longitude=coordinates[1],
            accuracy_m=to_float(position.get('accuracyMeters')),
            source=_text(position.get('source')),
            altitude_m=to_float(position.get('altitudeMeters')),
            speed_mps=to_float(position.get('speedMetersPerSecond')),
        )


def parse_profile(profile: Any) -> Iterator[FrequentPlace]:
    if not isinstance(profile, dict):
        return
    for place in profile.get('frequentPlaces') or []:
        if not isinstance(place, dict):
            continue
        coordinates = parse_latlng(place.get('placeLocation'))
        yield FrequentPlace(
            place_id=_text(place.get('placeId', place.get('placeID'))),
            label=_text(place.get('label')),
            latitude=coordinates[0] if coordinates else None,
            longitude=coordinates[1] if coordinates else None,
        )


# ----------------------------------------------------------------------
# Private helpers
# ----------------------------------------------------------------------

def _text(value: Any) -> Optional[str]:
    return None if value is None else str(value)


def _visit(visit: Any, start, end, offset) -> Optional[Visit]:
    if start is None or end is None or not isinstance(visit, dict):
        return None
    candidate = visit.get('topCandidate')
    if not isinstance(candidate, dict):
        candidate = {}
    coordinates = parse_latlng(candidate.get('placeLocation'))
    probability = to_float(candidate.get('probability'))
    if probability is None:
        probability = to_float(visit.get('probability'))
    return Visit(
        start_ms=start[0],
        end_ms=end[0],
        utc_offset_min=offset,
        latitude=coordinates[0] if coordinates else None,
        longitude=coordinates[1] if coordinates else None,
        place_id=_text(candidate.get('placeId', candidate.get('placeID'))),
        semantic_type=_text(candidate.get('semanticType')),
        probability=probability,
    )


def _activity(activity: Any, start, end, offset) -> Optional[Activity]:
    if start is None or end is None or not isinstance(activity, dict):
        return None
    candidate = activity.get('topCandidate')
    if not isinstance(candidate, dict):
        candidate = {}
    begin = parse_latlng(activity.get('start'))
    finish = parse_latlng(activity.get('end'))
    probability = to_float(candidate.get('probability'))
    if probability is None:
        probability = to_float(activity.get('probability'))
    return Activity(
        start_ms=start[0],
        end_ms=end[0],
        utc_offset_min=offset,
        activity_type=str(candidate.get('type') or 'UNKNOWN').upper(),
        distance_m=to_float(activity.get('distanceMeters')),
        probability=probability,
        start_latitude=begin[0] if begin else None,
        start_longitude=begin[1] if begin else None,
        end_latitude=finish[0] if finish else None,
        end_longitude=finish[1] if finish else None,
    )


def _path(path: Any, start, skipped: Skipped) -> Iterator[PathPoint]:
    if not isinstance(path, list):
        skipped.add('path that is not a list')
        return
    for point in path:
        if not isinstance(point, dict):
            skipped.add('path point that is not an object')
            continue
        coordinates = parse_latlng(point.get('point'))
        moment = parse_time(point.get('time'))
        t_ms = moment[0] if moment else None
        if t_ms is None and start is not None:
            # iOS gives minutes from the start of the segment instead of a time.
            minutes = to_float(point.get('durationMinutesOffsetFromStartTime'))
            if minutes is not None:
                t_ms = start[0] + int(minutes * 60_000)
        if coordinates is None or t_ms is None:
            skipped.add('path point without usable time or coordinates')
            continue
        yield PathPoint(t_ms=t_ms, latitude=coordinates[0], longitude=coordinates[1])
