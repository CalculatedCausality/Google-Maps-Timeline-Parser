import argparse
import sys
from datetime import timezone
from typing import List, Optional

from database.connection import DatabaseConnection
from repositories.timeline_repository import TimelineRepository
from services.coverage_service import CoverageService
from services.device_parser import Skipped
from services.distance_service import DistanceService
from services.export_service import ExportService
from services.parser_service import (
    FORMAT_NAMES,
    LARGE_FILE_BYTES,
    LEGACY_RECORDS,
    SETTINGS,
    UNKNOWN,
    TimelineParserService,
    ijson,
)
from services.parsing_utils import parse_time


def build_arguments() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Import Google Maps Timeline exports (phone exports and older '
                    'Takeout files) into SQLite and produce reports.'
    )
    parser.add_argument(
        'paths', nargs='*', metavar='PATH',
        help='Timeline JSON files, Takeout ZIP archives, or folders holding them. '
             'Leave out to report on what the database already holds.',
    )
    parser.add_argument('--db', default='timeline.db', metavar='PATH',
                        help='SQLite database path (default: timeline.db)')
    parser.add_argument('--coverage', action='store_true',
                        help='Print a month-by-month report of what the data covers')
    parser.add_argument('--coverage-csv', metavar='PATH',
                        help='Also write the month-by-month table to a CSV file')
    parser.add_argument('--tz', default='UTC', metavar='ZONE',
                        help='Time zone for counting days and months in the coverage '
                             'report, for example Australia/Brisbane (default: UTC)')
    parser.add_argument('--gpx', action='store_true', help='Export the track to a GPX file')
    parser.add_argument('--gpx-out', default='timeline.gpx', metavar='PATH')
    parser.add_argument('--distance', action='store_true',
                        help='Print the distance along the track')
    parser.add_argument('--heatmap', action='store_true',
                        help='Generate an interactive heatmap')
    parser.add_argument('--heatmap-out', default='travel_heatmap.html', metavar='PATH')
    parser.add_argument('--from', dest='date_from', metavar='YYYY-MM-DD',
                        help='Only use records on or after this UTC date (GPX, distance, heatmap)')
    parser.add_argument('--to', dest='date_to', metavar='YYYY-MM-DD',
                        help='Only use records before this UTC date (GPX, distance, heatmap)')
    parser.add_argument('--max-gap-minutes', type=float, default=30.0,
                        help='Break the track where readings are further apart than this '
                             '(default: 30)')
    parser.add_argument('--max-accuracy', type=float, default=200.0, metavar='METRES',
                        help='Leave out location readings less accurate than this '
                             '(default: 200)')
    return parser


def _date_ms(text: Optional[str], option: str) -> Optional[int]:
    if text is None:
        return None
    parsed = parse_time(f'{text}T00:00:00Z')
    if parsed is None:
        raise SystemExit(f'{option} must be a date like 2019-03-31, not {text!r}')
    return parsed[0]


def _zone(name: str):
    if name.upper() == 'UTC':
        return timezone.utc
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise SystemExit(f'Unknown time zone {name!r}. Use a name like Australia/Brisbane.')


def import_paths(paths: List[str], repo: TimelineRepository) -> None:
    for source in TimelineParserService().discover(paths):
        kind = source.format
        if kind in (UNKNOWN, SETTINGS):
            print(f'Skipped   {source.name}  ({FORMAT_NAMES[kind]})')
            continue
        if repo.has_source(source.sha256):
            print(f'Already imported   {source.name}')
            continue
        if kind == LEGACY_RECORDS and ijson is None and source.size_bytes > LARGE_FILE_BYTES:
            print('  Note: this file is large and is being read into memory in one go. '
                  'Run "pip install ijson" to read it piece by piece instead.')
        skipped = Skipped()
        totals = repo.import_source(
            source.name, source.sha256, kind, source.size_bytes,
            source.records(skipped), skipped.counts,
        )
        print(f'Imported  {source.name}  ({FORMAT_NAMES[kind]})')
        for table, (read, new) in sorted(totals.items()):
            print(f'    {table}: {read:,} read, {new:,} new')
        for reason, count in sorted(skipped.counts.items()):
            print(f'    not kept: {count:,} x {reason}')
        if not totals:
            print('    no records found in this file')


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arguments().parse_args(argv)
    zone = _zone(args.tz)
    from_ms = _date_ms(args.date_from, '--from')
    to_ms = _date_ms(args.date_to, '--to')

    repo = TimelineRepository(DatabaseConnection(args.db))
    if repo.has_old_table():
        print(f'Note: {args.db} holds a "timeline" table written by an earlier version. '
              'It is left as it is and no longer used; the new tables sit beside it.')
    repo.create_tables()

    try:
        import_paths(args.paths, repo)
    except FileNotFoundError as error:
        print(error, file=sys.stderr)
        return 2

    if args.coverage or args.coverage_csv:
        coverage = CoverageService(repo, zone, args.tz)
        report = coverage.build()
        if args.coverage:
            print()
            print(coverage.format_text(report))
        if args.coverage_csv:
            coverage.write_csv(report, args.coverage_csv)
            print(f'Coverage table written  →  {args.coverage_csv}')

    distance_svc = DistanceService(max_gap_minutes=args.max_gap_minutes)
    export_svc = ExportService()

    def track():
        return repo.iter_track(from_ms, to_ms, args.max_accuracy)

    if args.gpx:
        segments, points = export_svc.export_gpx(
            distance_svc.split_segments(track()), args.gpx_out)
        print(f'GPX exported  →  {args.gpx_out}  ({segments:,} segments, {points:,} points)')

    if args.distance:
        total = distance_svc.calculate_total(track())
        print(f'Distance along the track: {total:,.2f} km '
              f'(straight lines between readings; gaps over '
              f'{args.max_gap_minutes:g} minutes and impossible jumps left out)')

    if args.heatmap:
        try:
            points = [(loc.latitude, loc.longitude) for loc in track()]
            points.extend(repo.visit_points(from_ms, to_ms))
            cells = export_svc.export_heatmap(points, args.heatmap_out)
        except ImportError:
            print('The heatmap needs folium. Run "pip install folium" and try again.',
                  file=sys.stderr)
            return 1
        if cells:
            print(f'Heatmap exported  →  {args.heatmap_out}  ({cells:,} squares)')
        else:
            print('No points to draw, so no heatmap was written.')

    if not any([args.paths, args.coverage, args.coverage_csv,
                args.gpx, args.distance, args.heatmap]):
        print('Nothing to do. Give one or more files to import, or use '
              '--coverage, --gpx, --distance or --heatmap.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
