"""Finds Timeline files, works out which format each one is, and parses it.

The format is decided from what is inside the file, never from its name:
Google has renamed these files several times, and Android and iOS differ.
"""
import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any, Callable, BinaryIO, Iterable, Iterator, List, Optional

from .device_parser import Skipped, parse_profile, parse_raw_signals, parse_segments
from .legacy_parser import parse_edits, parse_records, parse_semantic

try:  # Optional: reads very large files one record at a time.
    import ijson
except ImportError:  # pragma: no cover - exercised by running without ijson
    ijson = None

DEVICE_EXPORT = 'device_export'
LEGACY_RECORDS = 'legacy_records'
LEGACY_SEMANTIC = 'legacy_semantic'
TIMELINE_EDITS = 'timeline_edits'
SETTINGS = 'settings'
UNKNOWN = 'unknown'

FORMAT_NAMES = {
    DEVICE_EXPORT: 'phone export (visits, movements, route points)',
    LEGACY_RECORDS: 'old Takeout location readings',
    LEGACY_SEMANTIC: 'old Takeout monthly visits and movements',
    TIMELINE_EDITS: 'Takeout Timeline Edits',
    SETTINGS: 'Takeout settings (no locations)',
    UNKNOWN: 'not a Timeline file',
}

_ROOT_KEYS = {
    'semanticSegments': DEVICE_EXPORT,
    'rawSignals': DEVICE_EXPORT,
    'userLocationProfile': DEVICE_EXPORT,
    'locations': LEGACY_RECORDS,
    'timelineObjects': LEGACY_SEMANTIC,
    'timelineEdits': TIMELINE_EDITS,
    'deviceSettings': SETTINGS,
    'historyEnabled': SETTINGS,
}

LARGE_FILE_BYTES = 200 * 1024 * 1024


class SourceFile:
    """One JSON file, on disk or inside a ZIP archive."""

    def __init__(self, name: str, opener: Callable[[], BinaryIO]) -> None:
        self.name = name
        self._opener = opener
        self._format: Optional[str] = None
        self._data: Any = None
        self._fingerprint: Optional[str] = None
        self._size = 0

    def open(self) -> BinaryIO:
        return self._opener()

    def _measure(self) -> None:
        digest = hashlib.sha256()
        size = 0
        with self.open() as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
                size += len(block)
        self._fingerprint, self._size = digest.hexdigest(), size

    @property
    def sha256(self) -> str:
        """Fingerprint of the exact bytes, to recognise a file already imported."""
        if self._fingerprint is None:
            self._measure()
        return self._fingerprint  # type: ignore[return-value]

    @property
    def size_bytes(self) -> int:
        if self._fingerprint is None:
            self._measure()
        return self._size

    @property
    def format(self) -> str:
        if self._format is None:
            self._detect()
        return self._format  # type: ignore[return-value]

    def _detect(self) -> None:
        if ijson is not None:
            sniffed = self._sniff()
            if sniffed in (LEGACY_RECORDS, UNKNOWN, SETTINGS):
                # Readings are streamed later; the others need no loading.
                self._format = sniffed
                return
        try:
            with self.open() as stream:
                self._data = json.load(stream)
        except (ValueError, UnicodeDecodeError):
            self._format = UNKNOWN
            return
        self._format = detect_format(self._data)
        if self._format in (UNKNOWN, SETTINGS):
            self._data = None

    def _sniff(self) -> str:
        """Read just far enough into the file to recognise it."""
        try:
            with self.open() as stream:
                for prefix, event, value in ijson.parse(stream):
                    if prefix != '':
                        continue
                    if event == 'start_array':
                        return DEVICE_EXPORT
                    if event == 'map_key' and value in _ROOT_KEYS:
                        return _ROOT_KEYS[value]
                    if event not in ('start_map', 'map_key'):
                        return UNKNOWN
        except (ijson.JSONError, UnicodeDecodeError, ValueError):
            return UNKNOWN
        return UNKNOWN

    def records(self, skipped: Skipped) -> Iterator[Any]:
        """Yield every record this file holds, whatever its format."""
        kind = self.format
        if kind == LEGACY_RECORDS and self._data is None:
            with self.open() as stream:
                yield from parse_records(
                    ijson.items(stream, 'locations.item', use_float=True), skipped
                )
            return
        data, self._data = self._data, None  # free the memory once parsed
        if kind == LEGACY_RECORDS:
            yield from parse_records(_list(data.get('locations')), skipped)
        elif kind == LEGACY_SEMANTIC:
            yield from parse_semantic(_list(data.get('timelineObjects')), skipped)
        elif kind == TIMELINE_EDITS:
            yield from parse_edits(_list(data.get('timelineEdits')), skipped)
        elif kind == DEVICE_EXPORT:
            if isinstance(data, list):
                yield from parse_segments(data, skipped)
            else:
                yield from parse_segments(_list(data.get('semanticSegments')), skipped)
                yield from parse_raw_signals(_list(data.get('rawSignals')), skipped)
                yield from parse_profile(data.get('userLocationProfile'))


def detect_format(data: Any) -> str:
    """Decide the format of an already loaded JSON document."""
    if isinstance(data, list):
        looks_like_segments = any(
            isinstance(item, dict)
            and ('startTime' in item or 'visit' in item
                 or 'activity' in item or 'timelinePath' in item)
            for item in data[:50]
        )
        return DEVICE_EXPORT if looks_like_segments else UNKNOWN
    if isinstance(data, dict):
        for key, kind in _ROOT_KEYS.items():
            if key in data:
                return kind
    return UNKNOWN


def _list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


class TimelineParserService:
    """Turns the paths given on the command line into parseable source files."""

    def discover(self, paths: Iterable[str]) -> Iterator[SourceFile]:
        """Accepts JSON files, ZIP archives (a Takeout download) and folders."""
        for raw in paths:
            path = Path(raw)
            if path.is_dir():
                children = sorted(
                    child for child in path.rglob('*')
                    if child.is_file() and child.suffix.lower() in ('.json', '.zip')
                )
                for child in children:
                    yield from self._from_file(child)
            elif path.is_file():
                yield from self._from_file(path)
            else:
                raise FileNotFoundError(f'No such file or folder: {raw}')

    def _from_file(self, path: Path) -> Iterator[SourceFile]:
        if path.suffix.lower() == '.zip':
            if not zipfile.is_zipfile(path):
                return
            with zipfile.ZipFile(path) as archive:
                members = sorted(
                    name for name in archive.namelist()
                    if name.lower().endswith('.json') and '__MACOSX' not in name
                )
            for member in members:
                yield SourceFile(f'{path}!{member}', _zip_opener(path, member))
        else:
            yield SourceFile(str(path), lambda path=path: open(path, 'rb'))


def _zip_opener(path: Path, member: str) -> Callable[[], BinaryIO]:
    def opener() -> BinaryIO:
        return zipfile.ZipFile(path).open(member)  # type: ignore[return-value]
    return opener
