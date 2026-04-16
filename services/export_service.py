from typing import List

import folium
from folium.plugins import HeatMap

from models.location import Location


class ExportService:
    """Exports location data to GPX and interactive heatmap formats."""

    def export_gpx(self, locations: List[Location], output_path: str = 'timeline.gpx') -> None:
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
            f.write('<gpx version="1.1" creator="TimelineProcessor">\n')
            for loc in locations:
                f.write(f'  <wpt lat="{loc.latitude}" lon="{loc.longitude}">\n')
                f.write(f'    <time>{loc.timestamp}</time>\n')
                f.write('  </wpt>\n')
            f.write('</gpx>\n')

    def export_heatmap(
        self,
        locations: List[Location],
        output_path: str = 'travel_heatmap.html',
    ) -> None:
        if not locations:
            return
        center = [locations[0].latitude, locations[0].longitude]
        travel_map = folium.Map(location=center, zoom_start=12)
        HeatMap([(loc.latitude, loc.longitude) for loc in locations]).add_to(travel_map)
        travel_map.save(output_path)
