from collections import Counter
from math import log
from typing import Iterable, List, Tuple
from xml.sax.saxutils import escape

from models.location import Location

from .parsing_utils import iso_utc


class ExportService:
    """Exports location data to GPX and interactive heatmap formats."""

    def export_gpx(
        self,
        segments: Iterable[List[Location]],
        output_path: str = 'timeline.gpx',
    ) -> Tuple[int, int]:
        """Write one GPX track with a segment per run of points.

        Returns (segments written, points written). Each segment is drawn as
        its own line, so apps that colour in where you have been (such as Fog
        of World) do not colour a straight line across a gap.
        """
        segment_count = point_count = 0
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
            f.write('<gpx version="1.1" creator="TimelineProcessor" '
                    'xmlns="http://www.topografix.com/GPX/1/1">\n')
            f.write('  <trk>\n    <name>Google Timeline</name>\n')
            for segment in segments:
                segment_count += 1
                f.write('    <trkseg>\n')
                for loc in segment:
                    point_count += 1
                    f.write(
                        f'      <trkpt lat="{loc.latitude:.7f}" lon="{loc.longitude:.7f}">'
                        f'<time>{iso_utc(loc.timestamp)}</time></trkpt>\n'
                    )
                f.write('    </trkseg>\n')
            f.write('  </trk>\n</gpx>\n')
        return segment_count, point_count

    def export_timeline_gpx(self, days: Iterable[dict], output_path: str) -> Tuple[int, int, int]:
        """Write corrected day timelines: a track per journey, a waypoint per stop.

        Hidden stops and journeys are left out. Returns (days, journeys, stops).
        """
        day_count = journeys = stops = 0
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
            f.write('<gpx version="1.1" creator="TimelineProcessor" '
                    'xmlns="http://www.topografix.com/GPX/1/1">\n')
            tracks: List[str] = []
            for day in days:
                day_count += 1
                for item in day['items']:
                    if item['hidden']:
                        continue
                    if item['kind'] == 'stop' and item['lat'] is not None:
                        stops += 1
                        f.write(f'  <wpt lat="{item["lat"]:.7f}" lon="{item["lng"]:.7f}">'
                                f'<time>{iso_utc(item["start"])}</time>'
                                f'<name>{escape(item["name"])}</name>'
                                f'<desc>{escape(day["date"] + " " + item["start_text"] + " to " + item["end_text"])}</desc>'
                                '</wpt>\n')
                    elif item['kind'] == 'move' and len(item['line']) > 1:
                        journeys += 1
                        points = ''.join(
                            f'<trkpt lat="{lat:.7f}" lon="{lng:.7f}"><time>{iso_utc(t)}</time></trkpt>'
                            for (lat, lng), t in zip(item['line'], item['times']))
                        name = f'{day["date"]} {item["start_text"]} {item["mode_label"]}'
                        tracks.append(f'  <trk><name>{escape(name)}</name>'
                                      f'<type>{escape(item["mode"])}</type>'
                                      f'<trkseg>{points}</trkseg></trk>\n')
            f.writelines(tracks)          # GPX puts waypoints before tracks
            f.write('</gpx>\n')
        return day_count, journeys, stops

    def heatmap_cells(
        self,
        points: Iterable[Tuple[float, float]],
        max_cells: int = 200_000,
    ) -> List[Tuple[float, float, float]]:
        """Group points into small squares so years of data stay drawable.

        Returns (latitude, longitude, weight). Squares are about 11 metres
        across, widening to about 110 metres if there would be too many.
        """
        fine: Counter = Counter()
        for latitude, longitude in points:
            fine[(round(latitude, 4), round(longitude, 4))] += 1
        cells = fine
        if len(fine) > max_cells:
            cells = Counter()
            for (latitude, longitude), count in fine.items():
                cells[(round(latitude, 3), round(longitude, 3))] += count
        # Damp the weight so home and work do not drown everything else out.
        return [(lat, lng, 1.0 + log(count)) for (lat, lng), count in cells.items()]

    def export_heatmap(
        self,
        points: Iterable[Tuple[float, float]],
        output_path: str = 'travel_heatmap.html',
    ) -> int:
        """Write an interactive heatmap. Returns the number of squares drawn."""
        import folium  # imported here so the other features work without it
        from folium.plugins import HeatMap

        cells = self.heatmap_cells(points)
        if not cells:
            return 0
        busiest = max(cells, key=lambda cell: cell[2])
        travel_map = folium.Map(location=[busiest[0], busiest[1]], zoom_start=11)
        HeatMap([list(cell) for cell in cells], radius=8, blur=10).add_to(travel_map)
        travel_map.save(output_path)
        return len(cells)
