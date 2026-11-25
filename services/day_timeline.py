"""One day as a list of stops and journeys, with your corrections applied.

Where Google recorded visits and movements for the day, those are used.
Where it only kept location readings (common in older years), stops are
found by looking for places you stayed at least ten minutes, and the
journeys between them are what is left.
"""
import uuid
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

    def _local_input(self, t_ms: int) -> str:
        """The form a browser's date-and-time box uses: 'YYYY-MM-DDTHH:MM'."""
        return datetime.fromtimestamp(t_ms / 1000, tz=self._zone).strftime('%Y-%m-%dT%H:%M')

    def parse_local(self, text: Any) -> int:
        """Read a local 'YYYY-MM-DDTHH:MM' time; raise ValueError if it is not one."""
        moment = datetime.strptime(str(text)[:16], '%Y-%m-%dT%H:%M')
        return int(moment.replace(tzinfo=self._zone).timestamp() * 1000)

    def _points(self, start_ms: int, end_ms: int) -> List[Location]:
        return list(self._repo.iter_track(start_ms, end_ms, self._max_accuracy_m))

    def _computed_items(self, start_ms: int, end_ms: int,
                        points: List[Location]) -> Tuple[List[dict], str]:
        """The day's places and journeys as worked out from the history."""
        visits = self._repo.visits_between(start_ms, end_ms, 2000)
        activities = self._repo.activities_between(start_ms, end_ms)
        if visits or activities:
            raw_items, source = self._from_google(visits, activities), 'google'
        else:
            raw_items, source = self._from_readings(points), 'readings'
        raw_items = self._fill_missing_moves(raw_items)
        for raw in raw_items:
            raw['id'] = f"{raw['kind']}-{raw['start']}"
        return raw_items, source

    def _day_items(self, day: date) -> Tuple[List[dict], List[Location], str]:
        """Your saved plan for the day if there is one, else the computed list."""
        start_ms, end_ms = self.day_bounds(day)
        plan = self._edits.plan(day.isoformat())
        if plan is not None:
            first = min([start_ms] + [r['start'] for r in plan])
            last = max([end_ms] + [r['end'] for r in plan])
            return plan, self._points(first, last), 'plan'
        points = self._points(start_ms, end_ms)
        raw_items, source = self._computed_items(start_ms, end_ms, points)
        return raw_items, points, source

    def build(self, day: date) -> Dict[str, Any]:
        day_key = day.isoformat()
        raw_items, points, source = self._day_items(day)
        times = [p.timestamp for p in points]

        edits = self._edits.for_day(day_key)
        named = self._edits.named_places()
        frequent = self._repo.frequent_places()
        items = []
        for index, raw in enumerate(raw_items):
            if raw['kind'] == 'stop':
                item = self._finish_stop(raw, edits, named, frequent)
            else:
                before, after = self._neighbours(raw_items, index)
                item = self._finish_move(raw, points, times, before, after, edits)
            item['start_text'] = self._clock(item['start'], day)
            item['end_text'] = self._clock(item['end'], day)
            item['start_local'] = self._local_input(item['start'])
            item['end_local'] = self._local_input(item['end'])
            items.append(item)

        visible = [i for i in items if not i['hidden']]
        return {
            'date': day_key,
            'items': items,
            'summary': {
                'stops': sum(1 for i in visible if i['kind'] == 'stop'),
                'moves': sum(1 for i in visible if i['kind'] == 'move'),
                'km': round(sum(i['km'] for i in visible if i['kind'] == 'move'), 2),
                'edited': bool(edits) or source == 'plan',
                'source': source,
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
        item_id = raw.get('id') or f"stop-{raw['start']}"
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
        item_id = raw.get('id') or f"move-{raw['start']}"
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

    @staticmethod
    def _neighbours(items: List[dict], index: int) -> Tuple[Optional[dict], Optional[dict]]:
        """The items directly before and after, or None at either end of the day."""
        before = items[index - 1] if index > 0 else None
        after = items[index + 1] if index + 1 < len(items) else None
        return before, after

    def move_line(self, raw: dict, points: List[Location], times: List[int],
                  before: Optional[dict], after: Optional[dict]) -> List[Tuple[int, float, float]]:
        """The cleaned line for a journey, joined to what comes directly before and after.

        A place next to it is joined at its pin. Another journey next to it (after
        a split) is joined at the first reading after the split, so the two lines
        meet without either doubling back.
        """
        # Readings strictly inside the journey; its ends are joined to its neighbours.
        first, last = bisect_right(times, raw['start']), bisect_left(times, raw['end'])
        cleaner = TrackCleaner(CleanerSettings(max_gap_minutes=24 * 60))
        line: List[Tuple[int, float, float]] = []
        for segment in cleaner.clean(points[first:last]):
            line.extend((p.timestamp, p.latitude, p.longitude) for p in segment)
        if not line and first < last:
            line = [(p.timestamp, p.latitude, p.longitude) for p in points[first:last]]

        start_point = end_point = None
        if before is None:
            start_point = raw.get('from')
        elif before['kind'] == 'stop' and before['lat'] is not None:
            start_point = (before['lat'], before['lng'])
        elif before['kind'] == 'move':
            # The same reading the journey before ends on, so the two lines meet.
            meeting = bisect_left(times, raw['start'])
            if meeting < len(points) and points[meeting].timestamp <= raw['end']:
                start_point = (points[meeting].latitude, points[meeting].longitude)
        if after is None:
            end_point = raw.get('to')
        elif after['kind'] == 'stop' and after['lat'] is not None:
            end_point = (after['lat'], after['lng'])
        elif after['kind'] == 'move':
            following = bisect_left(times, raw['end'])
            if following < len(points) and points[following].timestamp <= after['end']:
                end_point = (points[following].latitude, points[following].longitude)
        if start_point:
            line.insert(0, (raw['start'], start_point[0], start_point[1]))
        if end_point:
            line.append((raw['end'], end_point[0], end_point[1]))
        return line

    def move_locations(self, day: date, item_id: str) -> Optional[List[Location]]:
        """The journey's cleaned line with times, ignoring any saved snapping."""
        raw_items, points, _ = self._day_items(day)
        times = [p.timestamp for p in points]
        for index, raw in enumerate(raw_items):
            if raw['kind'] != 'move' or raw['id'] != item_id:
                continue
            before, after = self._neighbours(raw_items, index)
            line = self.move_line(raw, points, times, before, after)
            return [Location(t, lat, lng) for t, lat, lng in line]
        return None

    # ------------------------------------------------------------------
    # Changing the shape of a day: times, splitting, merging
    # ------------------------------------------------------------------

    def _editable_plan(self, day: date) -> List[dict]:
        """The day's plan, saved from the computed list the first time it is needed."""
        raw_items, _, source = self._day_items(day)
        if source != 'plan':
            raw_items = [dict(raw) for raw in raw_items]
        return raw_items

    @staticmethod
    def _find(plan: List[dict], item_id: str) -> int:
        for index, raw in enumerate(plan):
            if raw['id'] == item_id:
                return index
        raise ShapeError('That place or journey is not on this day.')

    @staticmethod
    def _new_id(kind: str) -> str:
        return f'{kind}-n{uuid.uuid4().hex[:10]}'

    def _save(self, day: date, plan: List[dict], forget_lines: List[str],
              removed: List[str]) -> None:
        key = day.isoformat()
        self._edits.save_plan(key, plan)
        for item_id in forget_lines:
            # A journey whose times changed needs snapping again.
            self._edits.set_geometry(key, item_id, None)
        if removed:
            self._edits.delete_items(key, removed)

    def set_times(self, day: date, item_id: str, start_ms: int, end_ms: int) -> None:
        plan = self._editable_plan(day)
        index = self._find(plan, item_id)
        item = plan[index]
        if end_ms - start_ms < 60_000:
            raise ShapeError('The end must be at least a minute after the start.')
        before = plan[index - 1] if index > 0 else None
        after = plan[index + 1] if index + 1 < len(plan) else None
        if before and start_ms - before['start'] < 60_000:
            raise ShapeError('That would start before the one above it has begun. '
                             'Merge them instead.')
        if after and after['end'] - end_ms < 60_000:
            raise ShapeError('That would end after the one below it has finished. '
                             'Merge them instead.')
        changed = [item]
        # Neighbours that touched this one keep touching it; overlapped ones give way.
        if before and (before['end'] >= item['start'] - 60_000 or start_ms < before['end']):
            before['end'] = start_ms
            changed.append(before)
        if after and (after['start'] <= item['end'] + 60_000 or end_ms > after['start']):
            after['start'] = end_ms
            changed.append(after)
        item['start'], item['end'] = start_ms, end_ms
        self._save(day, plan, [r['id'] for r in changed if r['kind'] == 'move'], [])

    def split_move(self, day: date, item_id: str, at_ms: int) -> None:
        plan = self._editable_plan(day)
        index = self._find(plan, item_id)
        move = plan[index]
        if move['kind'] != 'move':
            raise ShapeError('Only journeys can be split.')
        if not move['start'] + 60_000 <= at_ms <= move['end'] - 60_000:
            raise ShapeError('Choose a time at least a minute inside the journey.')
        second = dict(move, id=self._new_id('move'), start=at_ms)
        move['end'] = at_ms
        plan.insert(index + 1, second)
        self._save(day, plan, [move['id']], [])
        self._edits.copy_mode(day.isoformat(), move['id'], second['id'])

    def add_stop(self, day: date, item_id: str, start_ms: int, end_ms: int,
                 name: Optional[str]) -> str:
        plan = self._editable_plan(day)
        index = self._find(plan, item_id)
        move = plan[index]
        if move['kind'] != 'move':
            raise ShapeError('A stop can only be added during a journey.')
        if not move['start'] + 60_000 <= start_ms < end_ms <= move['end'] - 60_000:
            raise ShapeError('The stop must lie inside the journey, at least a minute from '
                             'each end, and finish after it starts.')
        lat, lng = self._position_between(day, plan, index, start_ms, end_ms)
        stop = {'kind': 'stop', 'id': self._new_id('stop'), 'start': start_ms, 'end': end_ms,
                'lat': lat, 'lng': lng, 'google_name': None, 'address': None,
                'semantic_type': None, 'probability': None}
        second = dict(move, id=self._new_id('move'), start=end_ms)
        move['end'] = start_ms
        plan[index + 1:index + 1] = [stop, second]
        self._save(day, plan, [move['id']], [])
        self._edits.copy_mode(day.isoformat(), move['id'], second['id'])
        if name:
            self._edits.set_name(day.isoformat(), stop['id'], name)
        return stop['id']

    def _position_between(self, day: date, plan: List[dict], index: int,
                          start_ms: int, end_ms: int) -> Tuple[float, float]:
        """Where you were between two times: the readings' centre, or the line's point."""
        inside = self._points(start_ms, end_ms + 1)
        if inside:
            return (sum(p.latitude for p in inside) / len(inside),
                    sum(p.longitude for p in inside) / len(inside))
        move = plan[index]
        points = self._points(move['start'], move['end'] + 1)
        times = [p.timestamp for p in points]
        before, after = self._neighbours(plan, index)
        line = self.move_line(move, points, times, before, after)
        if not line:
            raise ShapeError('There is no line to place the stop on.')
        middle = (start_ms + end_ms) / 2
        nearest = min(line, key=lambda p: abs(p[0] - middle))
        return nearest[1], nearest[2]

    def merge_next(self, day: date, item_id: str) -> None:
        """Join a place with the next place, or a journey with the next journey."""
        plan = self._editable_plan(day)
        index = self._find(plan, item_id)
        item = plan[index]
        later = next((j for j in range(index + 1, len(plan)) if plan[j]['kind'] == item['kind']),
                     None)
        if later is None:
            raise ShapeError(f"There is no later {'place' if item['kind'] == 'stop' else 'journey'}"
                             ' on this day to merge with.')
        removed = [r['id'] for r in plan[index + 1:later + 1]]
        item['end'] = plan[later]['end']
        del plan[index + 1:later + 1]
        self._save(day, plan, [item['id']] if item['kind'] == 'move' else [], removed)

    def remove_stop(self, day: date, item_id: str) -> None:
        """Take a place out and join the journeys either side of it."""
        plan = self._editable_plan(day)
        index = self._find(plan, item_id)
        stop = plan[index]
        if stop['kind'] != 'stop':
            raise ShapeError('Only places can be removed this way.')
        before = plan[index - 1] if index > 0 and plan[index - 1]['kind'] == 'move' else None
        after = plan[index + 1] if index + 1 < len(plan) and plan[index + 1]['kind'] == 'move' \
            else None
        removed = [stop['id']]
        if before and after:
            before['end'] = after['end']
            removed.append(after['id'])
            del plan[index:index + 2]
            changed = [before['id']]
        elif before:
            before['end'] = stop['end']
            del plan[index]
            changed = [before['id']]
        elif after:
            after['start'] = stop['start']
            del plan[index]
            changed = [after['id']]
        else:
            del plan[index]
            changed = []
        self._save(day, plan, changed, removed)


class ShapeError(Exception):
    """A change to the day's shape that cannot be made, explained in plain words."""
