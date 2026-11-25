"""Small helpers shared by the format parsers."""
import calendar
import re
from datetime import datetime, timezone
from typing import Any, Optional, Tuple

_TIMESTAMP = re.compile(
    r'^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d+))?)?'
    r'\s*(Z|z|[+-]\d{2}(?::?\d{2})?)?$'
)
_NUMBER = re.compile(r'[-+]?\d+(?:\.\d+)?')


def parse_time(value: Any) -> Optional[Tuple[int, Optional[int]]]:
    """Read a timestamp in any form Google has used.

    Returns (milliseconds since 1970 in UTC, UTC offset in minutes or None),
    or None when the value cannot be read. Accepts ISO 8601 text with or
    without an offset, and the older epoch-millisecond numbers or strings.
    A time with no offset is taken to be UTC.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value), None
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r'-?\d+', text):
        return int(text), None

    match = _TIMESTAMP.match(text)
    if not match:
        return None
    year, month, day, hour, minute, second, fraction, zone = match.groups()
    try:
        seconds = calendar.timegm(
            (int(year), int(month), int(day), int(hour), int(minute), int(second or 0))
        )
    except (ValueError, OverflowError):
        return None
    millis = int((fraction or '0')[:3].ljust(3, '0'))

    offset_min: Optional[int] = None
    if zone and zone not in ('Z', 'z'):
        sign = -1 if zone[0] == '-' else 1
        digits = zone[1:].replace(':', '')
        offset_min = sign * (int(digits[:2]) * 60 + int(digits[2:4] or 0))
    elif zone:
        offset_min = 0
    total = seconds * 1000 + millis - (offset_min or 0) * 60_000
    return total, offset_min


def iso_utc(t_ms: int) -> str:
    """Format milliseconds since 1970 as readable UTC text."""
    moment = datetime.fromtimestamp(t_ms / 1000, tz=timezone.utc)
    return moment.strftime('%Y-%m-%dT%H:%M:%SZ')


def valid_coordinates(lat: Optional[float], lng: Optional[float]) -> bool:
    return (
        lat is not None and lng is not None
        and -90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0
    )


def e7(value: Any) -> Optional[float]:
    """Convert Google's 'degrees times ten million' integers to degrees.

    Some old exports wrote negative values as unsigned 32-bit numbers, which
    matters for southern latitudes; those are wrapped back before dividing.
    """
    number = to_float(value)
    if number is None:
        return None
    if number > 1_800_000_000:
        number -= 4_294_967_296
    return number / 1e7


def e7_pair(lat: Any, lng: Any) -> Optional[Tuple[float, float]]:
    latitude, longitude = e7(lat), e7(lng)
    if not valid_coordinates(latitude, longitude):
        return None
    return latitude, longitude


def parse_latlng(value: Any) -> Optional[Tuple[float, float]]:
    """Read 'lat, lng' text from a phone export.

    Handles Android's degree-sign form ('-33.87°, 151.21°'), the iOS form
    ('geo:-33.87,151.21') and objects that wrap the text in `latLng`.
    """
    if isinstance(value, dict):
        for key in ('latLng', 'LatLng', 'point'):
            if key in value:
                return parse_latlng(value[key])
        if 'latitudeE7' in value or 'latE7' in value:
            return e7_pair(
                value.get('latitudeE7', value.get('latE7')),
                value.get('longitudeE7', value.get('lngE7')),
            )
        return None
    if not isinstance(value, str):
        return None
    numbers = _NUMBER.findall(value)
    if len(numbers) < 2:
        return None
    latitude, longitude = float(numbers[0]), float(numbers[1])
    if not valid_coordinates(latitude, longitude):
        return None
    return latitude, longitude


def to_float(value: Any) -> Optional[float]:
    """Numbers arrive as numbers or as text, depending on the phone."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_int(value: Any) -> Optional[int]:
    number = to_float(value)
    return None if number is None else int(number)
