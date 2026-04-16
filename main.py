import argparse

from database.connection import DatabaseConnection
from repositories.location_repository import LocationRepository
from services.distance_service import DistanceService
from services.export_service import ExportService
from services.parser_service import TimelineParserService


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Parse a Google Maps Timeline JSON export and produce reports.'
    )
    parser.add_argument('json_file', help='Path to the Timeline JSON file')
    parser.add_argument(
        '--db',
        default='timeline.db',
        metavar='PATH',
        help='SQLite database path (default: timeline.db)',
    )
    parser.add_argument('--gpx', action='store_true', help='Export locations to a GPX file')
    parser.add_argument('--distance', action='store_true', help='Print total distance travelled')
    parser.add_argument('--heatmap', action='store_true', help='Generate an interactive heatmap')
    args = parser.parse_args()

    # Infrastructure
    db = DatabaseConnection(args.db)

    # Repository
    location_repo = LocationRepository(db)
    location_repo.create_table()

    # Services
    parser_svc = TimelineParserService(args.json_file)
    export_svc = ExportService()
    distance_svc = DistanceService()

    # Parse the JSON and persist new locations
    locations = parser_svc.parse()
    inserted = location_repo.insert_many(locations)
    print(f'Parsed {len(locations)} location(s) — {inserted} new record(s) saved to {args.db}')

    # Load the canonical ordered set from the database for downstream services
    all_locations = location_repo.get_all_ordered()

    if args.gpx:
        export_svc.export_gpx(all_locations)
        print('GPX exported  →  timeline.gpx')

    if args.distance:
        total = distance_svc.calculate_total(all_locations)
        print(f'Total distance covered: {total:.2f} km')

    if args.heatmap:
        export_svc.export_heatmap(all_locations)
        print('Heatmap exported  →  travel_heatmap.html')

    if not any([args.gpx, args.distance, args.heatmap]):
        print('No export options specified. Use --gpx, --distance, or --heatmap.')


if __name__ == '__main__':
    main()
