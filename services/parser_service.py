import json
from typing import List, Tuple

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

    @staticmethod
    def _split_latlng(raw: str, sep: str = ',') -> Tuple[float, float]:
        """Strip degree signs and split a 'lat<sep>lng' string into floats."""
        cleaned = raw.replace('\u00b0', '').replace('\xc2\xb0', '').replace('Â°', '')
        parts = cleaned.split(sep)
        return float(parts[0].strip()), float(parts[1].strip())

    def _parse_visit(self, segment: dict) -> Location:
        candidate = segment['visit']['topCandidate']
        lat, lng = self._split_latlng(candidate['placeLocation']['latLng'])
        return Location(
            timestamp=segment['startTime'],
            latitude=lat,
            longitude=lng,
            semantic_type=candidate['semanticType'],
        )

    def _parse_path(self, segment: dict) -> List[Location]:
        locations: List[Location] = []
        for point in segment['timelinePath']:
            lat, lng = self._split_latlng(point['point'], sep=', ')
            locations.append(Location(
                timestamp=point['time'],
                latitude=lat,
                longitude=lng,
            ))
        return locations
