"""One day as a list of stops and journeys, with your corrections applied.

Where Google recorded visits and movements for the day, those are used.
Where it only kept location readings (common in older years), stops are
found by looking for places you stayed at least ten minutes, and the
journeys between them are what is left.
"""
from bisect import bisect_left, bisect_right
from datetime import date, datetime, timedelta, tzinfo
from typing import Any, Dict, List, Optional, Tuple

from models.location import Location
from repositories.edits_repository import EditsRepository, ItemEdit, nearest_named
from repositories.timeline_repository import TimelineRepository

from .track_cleaner import CleanerSettings, TrackCleaner, distance_m

# code -> label shown to you
MODES: Dict[str, str] = {
    'walking': 'Walking',
    'running': 'Running',
    'cycling': 'Cycling',
    'driving': 'Driving',
    'motorcycling': 'Motorcycling',
    'bus': 'Bus',
    'train': 'Train',
    'tram': 'Tram',
    'metro': 'Metro',
    'ferry': 'Ferry',
    'flying': 'Flying',
    'unknown': 'Moving',
}

_GOOGLE_MODES = {
    'WALKING': 'walking', 'ON_FOOT': 'walking', 'HIKING': 'walking',
    'RUNNING': 'running',
    'CYCLING': 'cycling', 'ON_BICYCLE': 'cycling',
    'IN_PASSENGER_VEHICLE': 'driving', 'IN_VEHICLE': 'driving', 'DRIVING': 'driving',
    'IN_CAR': 'driving', 'IN_TAXI': 'driving',
    'MOTORCYCLING': 'motorcycling',
    'IN_BUS': 'bus',
    'IN_TRAIN': 'train', 'IN_SUBWAY': 'metro', 'IN_TRAM': 'tram',
    'IN_CABLECAR': 'tram', 'IN_FUNICULAR': 'tram',
    'FLYING': 'flying',
    'IN_FERRY': 'ferry', 'SAILING': 'ferry', 'BOATING': 'ferry',
}

_SEMANTIC_NAMES = {
    'HOME': 'Home', 'TYPE_HOME': 'Home', 'INFERRED_HOME': 'Home',
    'WORK': 'Work', 'TYPE_WORK': 'Work', 'INFERRED_WORK': 'Work',
}

STAY_RADIUS_M = 100.0
STAY_MINUTES = 10.0
MIN_MOVE_M = 150.0


def google_mode(activity_type: Optional[str]) -> str:
    key = (activity_type or '').upper().replace(' ', '_')
    return _GOOGLE_MODES.get(key, 'unknown')


def guess_mode(km: float, hours: float) -> str:
    """A rough guess from average speed, used only when nothing better exists."""
    if hours <= 0:
        return 'unknown'
    speed = km / hours
    if speed < 8:
        return 'walking'
    if speed < 25:
        return 'cycling'
    if speed < 250:
        return 'driving'
    return 'flying'


def line_km(points: List[Tuple[float, float]]) -> float:
    return sum(distance_m(Location(0, *a), Location(0, *b))
               for a, b in zip(points, points[1:])) / 1000.0


class DayTimelineService:
    def __init__(self, repository: TimelineRepository, edits: EditsRepository,
                 zone: tzinfo, max_accuracy_m: Optional[float] = 200.0) -> None:
        self._repo = repository
        self._edits = edits
        self._zone = zone
        self._max_accuracy_m = max_accuracy_m

    # ------------------------------------------------------------------
    # Times
    # ------------------------------------------------------------------

    def day_bounds(self, day: date) -> Tuple[int, int]:
        start = datetime(day.year, day.month, day.day, tzinfo=self._zone)
        end_day = day + timedelta(days=1)
        end = datetime(end_day.year, end_day.month, end_day.day, tzinfo=self._zone)
        return int(start.timestamp() * 1000), int(end.timestamp() * 1000)

    def _clock(self, t_ms: int, day: date) -> str:
        moment = datetime.fromtimestamp(t_ms / 1000, tz=self._zone)
        if moment.date() == day:
            return moment.strftime('%H:%M')
        return moment.strftime('%a %d %b %H:%M')

    # ------------------------------------------------------------------
    # Building the day
    # ------------------------------------------------------------------

    def build(self, day: date) -> Dict[str, Any]:
        start_ms, end_ms = self.day_bounds(day)
        day_key = day.isoformat()
        points = list(self._repo.iter_track(start_ms, end_ms, self._max_accuracy_m))
        times = [p.timestamp for p in points]

        visits = self._repo.visits_between(start_ms, end_ms, 2000)
        activities = self._repo.activities_between(start_ms, end_ms)
        if visits or activities:
            raw_items = self._from_google(visits, activities)
        else:
            raw_items = self._from_readings(points)
        raw_items = self._fill_missing_moves(raw_items)

        edits = self._edits.for_day(day_key)
        named = self._edits.named_places()
        frequent = self._repo.frequent_places()
        items = []
        previous_stop: Optional[dict] = None
        for index, raw in enumerate(raw_items):
            if raw['kind'] == 'stop':
                item = self._finish_stop(raw, edits, named, frequent)
                previous_stop = item
            else:
                following = next((r for r in raw_items[index + 1:] if r['kind'] == 'stop'), None)
                item = self._finish_move(raw, points, times, previous_stop, following, edits)
            item['start_text'] = self._clock(item['start'], day)
            item['end_text'] = self._clock(item['end'], day)
            items.append(item)

        visible = [i for i in items if not i['hidden']]
        return {
            'date': day_key,
            'items': items,
            'summary': {
                'stops': sum(1 for i in visible if i['kind'] == 'stop'),
                'moves': sum(1 for i in visible if i['kind'] == 'move'),
                'km': round(sum(i['km'] for i in visible if i['kind'] == 'move'), 2),
                'edited': bool(edits),
                'source': 'google' if (visits or activities) else 'readings',
            },
        }

    def _from_google(self, visits: List[tuple], activities: List[tuple]) -> List[dict]:
        items = []
        for start_ms, end_ms, lat, lng, name, address, semantic_type, probability in visits:
            items.append({'kind': 'stop', 'start': start_ms, 'end': end_ms,
                          'lat': lat, 'lng': lng, 'google_name': name, 'address': address,
                          'semantic_type': semantic_type, 'probability': probability})
        for (start_ms, end_ms, activity_type, distance, s_lat, s_lng,
             e_lat, e_lng) in activities:
            items.append({'kind': 'move', 'start': start_ms, 'end': end_ms,
                          'google_mode': google_mode(activity_type),
                          'google_km': None if distance is None else distance / 1000.0,
                          'from': (s_lat, s_lng) if s_lat is not None else None,
                          'to': (e_lat, e_lng) if e_lat is not None else None})
        items.sort(key=lambda item: (item['start'], item['kind'] == 'move'))
        return items

    def _from_readings(self, points: List[Location]) -> List[dict]:
        cleaner = TrackCleaner()
        clean = list(cleaner.drop_spikes(points))
        items: List[dict] = []
        i = 0
        while i < len(clean):
            sum_lat, sum_lng = clean[i].latitude, clean[i].longitude
            j = i + 1
            while j < len(clean):
                count = j - i
                centre = Location(0, sum_lat / count, sum_lng / count)
                if distance_m(centre, clean[j]) > STAY_RADIUS_M:
                    break
                sum_lat += clean[j].latitude
                sum_lng += clean[j].longitude
                j += 1
            count = j - i
            if clean[j - 1].timestamp - clean[i].timestamp >= STAY_MINUTES * 60_000 and count >= 2:
                items.append({'kind': 'stop', 'start': clean[i].timestamp,
                              'end': clean[j - 1].timestamp,
                              'lat': sum_lat / count, 'lng': sum_lng / count,
                              'google_name': None, 'address': None,
                              'semantic_type': None, 'probability': None})
                i = j
            else:
                i += 1
        # Journeys are the time between stops (and before the first, after the last).
        result: List[dict] = []
        cursor = clean[0].timestamp if clean else None
        for stop in items:
            if cursor is not None and stop['start'] > cursor:
                result.append({'kind': 'move', 'start': cursor, 'end': stop['start']})
            result.append(stop)
            cursor = stop['end']
        if clean and cursor is not None and clean[-1].timestamp > cursor:
            result.append({'kind': 'move', 'start': cursor, 'end': clean[-1].timestamp})
        return result

    @staticmethod
    def _fill_missing_moves(items: List[dict]) -> List[dict]:
        """Add a journey between two stops that are apart but have none recorded."""
        result: List[dict] = []
        for item in items:
            if (item['kind'] == 'stop' and result and result[-1]['kind'] == 'stop'):
                prev = result[-1]
                apart = distance_m(Location(0, prev['lat'], prev['lng']),
                                   Location(0, item['lat'], item['lng']))
                if apart >= MIN_MOVE_M and item['start'] >= prev['end']:
                    result.append({'kind': 'move', 'start': prev['end'], 'end': item['start']})
            result.append(item)
        return result

    # ------------------------------------------------------------------
    # Finishing items: names, modes, lines and your corrections
    # ------------------------------------------------------------------

    def _finish_stop(self, raw: dict, edits: Dict[str, ItemEdit], named, frequent) -> dict:
        item_id = f"stop-{raw['start']}"
        edit = edits.get(item_id)
        name, source = None, 'unknown'
        if edit and edit.name:
            name, source = edit.name, 'you'
        elif raw['lat'] is not None and (place := nearest_named(named, raw['lat'], raw['lng'])):
            name, source = place, 'your place'
        elif raw.get('google_name'):
            name, source = raw['google_name'], 'google'
        elif raw.get('semantic_type') in _SEMANTIC_NAMES:
            name, source = _SEMANTIC_NAMES[raw['semantic_type']], 'google'
        elif raw['lat'] is not None:
            for label, lat, lng in frequent:
                if distance_m(Location(0, lat, lng), Location(0, raw['lat'], raw['lng'])) <= 100:
                    name, source = _SEMANTIC_NAMES.get(label.upper(), label.title()), 'google'
                    break
        return {
            'id': item_id, 'kind': 'stop', 'start': raw['start'], 'end': raw['end'],
            'lat': raw['lat'], 'lng': raw['lng'],
            'name': name or 'Unnamed place', 'name_source': source,
            'address': raw.get('address'), 'probability': raw.get('probability'),
            'hidden': bool(edit and edit.hidden), 'km': 0.0,
        }

    def _finish_move(self, raw: dict, points: List[Location], times: List[int],
                     before: Optional[dict], after: Optional[dict],
                     edits: Dict[str, ItemEdit]) -> dict:
        item_id = f"move-{raw['start']}"
        edit = edits.get(item_id)
        base = self.move_line(raw, points, times, before, after)
        estimated = len(base) <= 2 and not self._has_readings(raw, times)
        snapped = bool(edit and edit.geometry)
        geometry = [(t, lat, lng) for t, lat, lng in edit.geometry] if snapped else base
        line = [(lat, lng) for _, lat, lng in geometry]
        km = line_km(line)

        if edit and edit.mode:
            mode, mode_source = edit.mode, 'you'
        elif raw.get('google_mode') and raw['google_mode'] != 'unknown':
            mode, mode_source = raw['google_mode'], 'google'
        else:
            # Guessed from the original line, so snapping never changes the guess.
            hours = (raw['end'] - raw['start']) / 3_600_000
            mode = guess_mode(line_km([(lat, lng) for _, lat, lng in base]), hours)
            mode_source = 'guess'
        return {
            'id': item_id, 'kind': 'move', 'start': raw['start'], 'end': raw['end'],
            'mode': mode, 'mode_label': MODES.get(mode, mode), 'mode_source': mode_source,
            'km': round(km, 2), 'google_km': raw.get('google_km'),
            'line': [[round(lat, 6), round(lng, 6)] for lat, lng in line],
            'times': [t for t, _, _ in geometry],
            'snapped': snapped, 'estimated': estimated,
            'hidden': bool(edit and edit.hidden),
        }

    @staticmethod
    def _has_readings(raw: dict, times: List[int]) -> bool:
        return bisect_left(times, raw['end']) - bisect_right(times, raw['start']) > 0

    def move_line(self, raw: dict, points: List[Location], times: List[int],
                  before: Optional[dict], after: Optional[dict]) -> List[Tuple[int, float, float]]:
        """The cleaned line for a journey, joined to the stops either side."""
        # Readings strictly inside the journey; its ends are the stops either side.
        first, last = bisect_right(times, raw['start']), bisect_left(times, raw['end'])
        cleaner = TrackCleaner(CleanerSettings(max_gap_minutes=24 * 60))
        line: List[Tuple[int, float, float]] = []
        for segment in cleaner.clean(points[first:last]):
            line.extend((p.timestamp, p.latitude, p.longitude) for p in segment)
        if not line and first < last:
            line = [(p.timestamp, p.latitude, p.longitude) for p in points[first:last]]

        start_point = (before['lat'], before['lng']) if before and before['lat'] is not None \
            else raw.get('from')
        end_point = (after['lat'], after['lng']) if after and after['lat'] is not None \
            else raw.get('to')
        if start_point:
            line.insert(0, (raw['start'], start_point[0], start_point[1]))
        if end_point:
            line.append((raw['end'], end_point[0], end_point[1]))
        return line

    def move_locations(self, day: date, item_id: str) -> Optional[List[Location]]:
        """The journey's cleaned line with times, ignoring any saved snapping."""
        start_ms, end_ms = self.day_bounds(day)
        points = list(self._repo.iter_track(start_ms, end_ms, self._max_accuracy_m))
        times = [p.timestamp for p in points]
        visits = self._repo.visits_between(start_ms, end_ms, 2000)
        activities = self._repo.activities_between(start_ms, end_ms)
        raw_items = self._from_google(visits, activities) if (visits or activities) \
            else self._from_readings(points)
        raw_items = self._fill_missing_moves(raw_items)
        for index, raw in enumerate(raw_items):
            if raw['kind'] != 'move' or f"move-{raw['start']}" != item_id:
                continue
            before = next((r for r in reversed(raw_items[:index]) if r['kind'] == 'stop'), None)
            after = next((r for r in raw_items[index + 1:] if r['kind'] == 'stop'), None)
            line = self.move_line(raw, points, times, before, after)
            return [Location(t, lat, lng) for t, lat, lng in line]
        return None
