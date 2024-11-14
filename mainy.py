import os
import sqlite3
import json
from math import radians, cos, sin, sqrt, atan2
import folium
from folium.plugins import HeatMap

class TimelineProcessor:
    def __init__(self, json_file, db_file):
        self.json_file = json_file
        self.db_file = db_file
        self.locations = []

    def load_timeline(self):
        with open(self.json_file, 'r') as file:
            self.timeline = json.load(file)

    def create_database(self):
        with sqlite3.connect(self.db_file) as db:
            db.execute('''
            CREATE TABLE IF NOT EXISTS timeline (
                timestamp TEXT PRIMARY KEY,
                latitude REAL,
                longitude REAL,
                semanticLocation TEXT
            )
            ''')

    def insert_locations(self):
        with sqlite3.connect(self.db_file) as db:
            for location in self.locations:
                try:
                    db.execute('''
                    INSERT INTO timeline (timestamp, latitude, longitude, semanticLocation)
                    VALUES (?, ?, ?, ?)
                    ''', location)
                except sqlite3.IntegrityError:
                    # Skip duplicate timestamps
                    continue
            db.commit()

    def prepare_data(self):
        for location in self.timeline['semanticSegments']:
            if 'visit' in location:
                semantic_location = location['visit']['topCandidate']['semanticType']
                timestamp = location['startTime']
                lat_long = location['visit']['topCandidate']['placeLocation']['latLng'].replace('Â°', '').split(',')
                latitude = float(lat_long[0])
                longitude = float(lat_long[1])
                self.locations.append((timestamp, latitude, longitude, semantic_location))

            if 'timelinePath' in location:
                for sub_location in location['timelinePath']:
                    timestamp = sub_location['time']
                    lat_long = sub_location['point'].replace('Â°', '').split(', ')
                    latitude = float(lat_long[0])
                    longitude = float(lat_long[1])
                    self.locations.append((timestamp, latitude, longitude, ''))

    def generate_gpx(self):
        with sqlite3.connect(self.db_file) as db:
            with open('timeline.gpx', 'w') as gpx_file:
                gpx_file.write('<?xml version="1.0" encoding="UTF-8"?>\n')
                gpx_file.write('<gpx version="1.1" creator="TimelineProcessor">\n')
                for row in db.execute('SELECT timestamp, latitude, longitude FROM timeline ORDER BY timestamp'):
                    gpx_file.write(f'  <wpt lat="{row[1]}" lon="{row[2]}">\n')
                    gpx_file.write(f'    <time>{row[0]}</time>\n')
                    gpx_file.write('  </wpt>\n')
                gpx_file.write('</gpx>\n')

    def calculate_total_distance(self):
        def haversine(lat1, lon1, lat2, lon2):
            R = 6371.0  # Earth radius in kilometres

            dlat = radians(lat2 - lat1)
            dlon = radians(lon2 - lon1)

            a = sin(dlat / 2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2)**2
            c = 2 * atan2(sqrt(a), sqrt(1 - a))

            distance = R * c
            return distance

        total_distance = 0.0
        previous_point = None

        with sqlite3.connect(self.db_file) as db:
            for row in db.execute('SELECT latitude, longitude FROM timeline ORDER BY timestamp'):
                if previous_point:
                    total_distance += haversine(previous_point[0], previous_point[1], row[0], row[1])
                previous_point = (row[0], row[1])

        print(f'Total distance covered: {total_distance:.2f} km')

    def generate_heatmap(self):
        map_center = [0, 0]
        if self.locations:
            map_center = [self.locations[0][1], self.locations[0][2]]

        travel_map = folium.Map(location=map_center, zoom_start=12)
        heat_data = [(loc[1], loc[2]) for loc in self.locations]
        HeatMap(heat_data).add_to(travel_map)
        travel_map.save('travel_heatmap.html')

    def run(self, export_gpx=False, calculate_distance=False, generate_heatmap=False):
        self.load_timeline()
        self.create_database()
        self.prepare_data()
        self.insert_locations()
        if export_gpx:
            self.generate_gpx()
        if calculate_distance:
            self.calculate_total_distance()
        if generate_heatmap:
            self.generate_heatmap()

# Example usage:
processor = TimelineProcessor('Timeline.json', 'timeline.db')
processor.run(export_gpx=True, calculate_distance=True, generate_heatmap=True)