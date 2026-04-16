import json
from typing import List

from models.location import Location


class TimelineParserService:
    """Parses a Google Maps Timeline JSON export into Location objects."""

    def __init__(self, json_path: str) -> None:
        self._json_path = json_path

    def parse(self) -> List[Location]:
        with open(self._json_path, 'r', encoding='utf-8') as f:
            timeline = json.load(f)

        locations: List[Location] = []
        for segment in timeline.get('semanticSegments', []):
            if 'visit' in segment:
                locations.append(self._parse_visit(segment))
            if 'timelinePath' in segment:
                locations.extend(self._parse_path(segment))
        return locations

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _parse_visit(self, segment: dict) -> Location:
        candidate = segment['visit']['topCandidate']
        lat_lng = (
            candidate['placeLocation']['latLng']
            .replace('\u00b0', '')   # strip degree sign (handles both encodings)
            .replace('Â°', '')
            .split(',')
        )
        return Location(
            timestamp=segment['startTime'],
            latitude=float(lat_lng[0]),
            longitude=float(lat_lng[1]),
            semantic_type=candidate['semanticType'],
        )

    def _parse_path(self, segment: dict) -> List[Location]:
        locations: List[Location] = []
        for point in segment['timelinePath']:
            lat_lng = (
                point['point']
                .replace('\u00b0', '')
                .replace('Â°', '')
                .split(', ')
            )
            locations.append(Location(
                timestamp=point['time'],
                latitude=float(lat_lng[0]),
                longitude=float(lat_lng[1]),
            ))
        return locations
