"""Measures what the imported history actually covers.

A long history is rarely even: early years can be sparse, phones change and
recording gets switched off. This report shows, month by month, how much of
each kind of record exists, so a gap in the data is not mistaken for a
change in behaviour.
"""
import calendar
import csv
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

if TYPE_CHECKING:  # avoids importing the repository package in a circle
    from repositories.timeline_repository import TimelineRepository

from .parser_service import FORMAT_NAMES
from .parsing_utils import iso_utc

# Clock changes fall on half-hour boundaries of UTC at the finest
# (Lord Howe Island), so half-hour buckets convert to local dates exactly.
BUCKET_MS = 1_800_000
# A single "visit" lasting longer than this is treated as doubtful and only
# its first and last days are counted as covered.
MAX_INTERVAL_DAYS = 62


@dataclass
class MonthCoverage:
    month: str                      # 'YYYY-MM'
    days_in_month: int
    days_with_data: int = 0
    positions: int = 0
    path_points: int = 0
    visits: int = 0
    activities: int = 0
    reported_km: float = 0.0


@dataclass
class Gap:
    first_day: date
    last_day: date

    @property
    def days(self) -> int:
        return (self.last_day - self.first_day).days + 1


@dataclass
class CoverageReport:
    timezone_name: str
    sources: List[dict] = field(default_factory=list)
    tables: Dict[str, Tuple[int, Optional[int], Optional[int]]] = field(default_factory=dict)
    position_types: List[Tuple[str, int, int]] = field(default_factory=list)
    months: List[MonthCoverage] = field(default_factory=list)
    gaps: List[Gap] = field(default_factory=list)
    km_by_year_and_mode: Dict[Tuple[int, str], float] = field(default_factory=dict)

    @property
    def first_day(self) -> Optional[date]:
        return self._days[0] if self._days else None

    @property
    def last_day(self) -> Optional[date]:
        return self._days[-1] if self._days else None

    @property
    def days_with_data(self) -> int:
        return len(self._days)

    _days: List[date] = field(default_factory=list, repr=False)


class CoverageService:
    def __init__(self, repository: 'TimelineRepository', zone: tzinfo = timezone.utc,
                 zone_name: str = 'UTC') -> None:
        self._repo = repository
        self._zone = zone
        self._zone_name = zone_name

    def _local_day(self, t_ms: int) -> date:
        return datetime.fromtimestamp(t_ms / 1000, tz=self._zone).date()

    def build(self) -> CoverageReport:
        report = CoverageReport(timezone_name=self._zone_name)
        report.sources = self._repo.sources()
        report.tables = self._repo.table_summary()
        report.position_types = self._repo.position_types()

        months: Dict[str, MonthCoverage] = {}
        covered: set = set()

        def month_of(day: date) -> MonthCoverage:
            key = f'{day.year:04d}-{day.month:02d}'
            if key not in months:
                months[key] = MonthCoverage(
                    key, calendar.monthrange(day.year, day.month)[1])
            return months[key]

        for table, attribute in (('positions', 'positions'), ('path_points', 'path_points')):
            for bucket, count in self._repo.point_buckets(table, BUCKET_MS):
                day = self._local_day(bucket * BUCKET_MS)
                covered.add(day)
                entry = month_of(day)
                setattr(entry, attribute, getattr(entry, attribute) + count)

        for start_ms, end_ms in self._repo.visit_intervals():
            first = self._local_day(start_ms)
            month_of(first).visits += 1
            covered.update(self._interval_days(first, self._local_day(end_ms)))

        for start_ms, end_ms, mode, distance_m in self._repo.activity_intervals():
            first = self._local_day(start_ms)
            entry = month_of(first)
            entry.activities += 1
            km = (distance_m or 0.0) / 1000.0
            entry.reported_km += km
            key = (first.year, mode)
            report.km_by_year_and_mode[key] = report.km_by_year_and_mode.get(key, 0.0) + km
            covered.update(self._interval_days(first, self._local_day(end_ms)))

        days = sorted(covered)
        report._days = days
        if days:
            # Show every month between the first and last, including empty ones.
            cursor = date(days[0].year, days[0].month, 1)
            while cursor <= days[-1]:
                month_of(cursor)
                cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
            for day in days:
                month_of(day).days_with_data += 1
            for previous, current in zip(days, days[1:]):
                if (current - previous).days > 1:
                    report.gaps.append(
                        Gap(previous + timedelta(days=1), current - timedelta(days=1)))
            report.gaps.sort(key=lambda gap: (-gap.days, gap.first_day))
        report.months = [months[key] for key in sorted(months)]
        return report

    @staticmethod
    def _interval_days(first: date, last: date) -> List[date]:
        if last < first:
            return [first]
        span = (last - first).days
        if span > MAX_INTERVAL_DAYS:
            return [first, last]
        return [first + timedelta(days=offset) for offset in range(span + 1)]

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def format_text(self, report: CoverageReport, max_gaps: int = 15) -> str:
        lines: List[str] = []
        add = lines.append
        add('COVERAGE REPORT')
        add(f'Days and months are counted in this time zone: {report.timezone_name}')
        add('')

        add('Files imported')
        if not report.sources:
            add('  none')
        for source in report.sources:
            add(f"  {source['name']}")
            add(f"    {FORMAT_NAMES.get(source['format'], source['format'])}; "
                f"{source['records']:,} records read, {source['new_records']:,} new")
            for reason, count in source['skipped'].items():
                add(f'    not kept: {count:,} x {reason}')
        add('')

        labels = (
            ('positions', 'Location readings'),
            ('path_points', 'Route points'),
            ('visits', 'Visits'),
            ('activities', 'Movements'),
        )
        add('What the history holds (each kind has its own date range)')
        for table, label in labels:
            count, first, last = report.tables.get(table, (0, None, None))
            if count:
                add(f'  {label:<18}{count:>12,}   {iso_utc(first)[:10]} to {iso_utc(last)[:10]}')
            else:
                add(f'  {label:<18}{0:>12,}   none')
        for record_type, count, without_accuracy in report.position_types:
            add(f'    readings of type {record_type}: {count:,} '
                f'({without_accuracy or 0:,} with no accuracy figure)')
        add('')

        if report.first_day is None:
            add('No dated records, so there is no month-by-month table.')
            return '\n'.join(lines)

        total_days = (report.last_day - report.first_day).days + 1
        add(f'Days with any record: {report.days_with_data:,} of {total_days:,} '
            f'between {report.first_day} and {report.last_day}')
        add('A day with no record means no evidence, not that nothing happened.')
        add('')

        add('Month     Days with data   Readings  Route pts   Visits  Movements  Reported km')
        for month in report.months:
            add(f'{month.month}   {month.days_with_data:>5} of {month.days_in_month:<5}'
                f'{month.positions:>12,}{month.path_points:>11,}{month.visits:>9,}'
                f'{month.activities:>11,}{month.reported_km:>13,.1f}')
        add('')

        add(f'Longest runs of days with no record (top {max_gaps})')
        if not report.gaps:
            add('  none')
        for gap in report.gaps[:max_gaps]:
            add(f'  {gap.first_day} to {gap.last_day}   {gap.days:,} days')
        add('')

        if report.km_by_year_and_mode:
            add("Distance by year and travel mode (Google's own estimate for each movement)")
            for (year, mode), km in sorted(report.km_by_year_and_mode.items()):
                add(f'  {year}  {mode:<28}{km:>12,.1f} km')
        return '\n'.join(lines)

    def write_csv(self, report: CoverageReport, output_path: str) -> None:
        with open(output_path, 'w', encoding='utf-8', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['month', 'days_in_month', 'days_with_data', 'location_readings',
                             'route_points', 'visits', 'movements', 'reported_km'])
            for month in report.months:
                writer.writerow([month.month, month.days_in_month, month.days_with_data,
                                 month.positions, month.path_points, month.visits,
                                 month.activities, f'{month.reported_km:.1f}'])
