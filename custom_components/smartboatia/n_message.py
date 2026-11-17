"""Decoder for the Smartboatia N-message, version 3 (CBOR).

Adapted from the Smartboatia platform decoder (specification N_MESSAGE_V3.md; the
section numbers below refer to it). Pure Python, no dependencies. Only version 3 is
supported. Payloads are untrusted: the limits of section 3.2 are enforced while
reading and no decoded data is ever formatted into exception messages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Context, Decimal
import math
import struct
from typing import Any

MAX_PAYLOAD_BYTES = 16384
MAX_DEPTH = 8
MAX_TIMESTAMP_MS = 253402300799999
MAX_INSTANCE = 255
MAX_ENTRIES_PER_MEASUREMENT = 32

_FORMAT_VERSION = 3
_ORIGIN_WIFI = 0
_ORIGIN_LTE = 1
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_ROUND_CONTEXT = Context(prec=80)

ENGINE_STATUS_1_BITS: dict[int, str] = {
    0: "Check Engine",
    1: "Over Temperature",
    2: "Low Oil Pressure",
    3: "Low Oil Level",
    4: "Low Fuel Pressure",
    5: "Low System Voltage",
    6: "Low Coolant Level",
    7: "Water Flow",
    8: "Water In Fuel",
    9: "Charge Indicator",
    10: "Preheat Indicator",
    11: "High Boost Pressure",
    12: "Rev Limit Exceeded",
    13: "EGR System",
    14: "Throttle Position Sensor",
    15: "Emergency Stop",
}
ENGINE_STATUS_2_BITS: dict[int, str] = {
    0: "Warning Level 1",
    1: "Warning Level 2",
    2: "Power Reduction",
    3: "Maintenance Needed",
    4: "Engine Comm Error",
    5: "Sub or Secondary Throttle",
    6: "Neutral Start Protect",
    7: "Engine Shutting Down",
}
GEAR_NAMES: dict[int, str] = {0: "forward", 1: "neutral", 2: "reverse"}

# tag keys (section 4.3)
_TAG_VALIDITY = 0
_TAG_REFERENCE = 1
_TAG_ORIENTATION = 2
_TAG_INSTANCE = 3
_TAG_NAMES = {
    _TAG_VALIDITY: "validity",
    _TAG_REFERENCE: "reference",
    _TAG_ORIENTATION: "orientation",
    _TAG_INSTANCE: "instance",
}
_TAG_VALUES: dict[int, dict[int, str]] = {
    _TAG_VALIDITY: {0: "outlier"},
    _TAG_REFERENCE: {0: "apparent", 1: "ground", 2: "true"},
    _TAG_ORIENTATION: {0: "true", 1: "magnetic"},
}


@dataclass(frozen=True)
class FieldDef:
    """One field of a measurement."""

    number: int
    name: str
    decimals: int
    mandatory: bool = False
    # reference tag value -> field name override (wind angle)
    names_by_reference: dict[str, str] = field(default_factory=dict)
    # code fields: unsigned integer up to max_code, anything else is no data
    max_code: int | None = None


@dataclass(frozen=True)
class MeasurementDef:
    """One measurement of the registry (section 6)."""

    key: int
    name: str
    fields: tuple[FieldDef, ...]
    allowed_tags: frozenset[str] = frozenset({"validity"})
    required_tags: frozenset[str] = frozenset()


def _single(key: int, name: str, field_name: str, decimals: int, **kw: Any):
    return MeasurementDef(key, name, (FieldDef(0, field_name, decimals, True),), **kw)


_INST = frozenset({"validity", "instance"})
_INST_REQ = frozenset({"instance"})


def _tank(key: int, name: str) -> MeasurementDef:
    return _single(key, name, "level", 0, allowed_tags=_INST, required_tags=_INST_REQ)


def _opt(number: int, name: str, decimals: int, **kw: Any) -> FieldDef:
    return FieldDef(number, name, decimals, False, **kw)


MEASUREMENTS: tuple[MeasurementDef, ...] = (
    MeasurementDef(
        10, "gps_pos", (FieldDef(0, "lat", 6, True), FieldDef(1, "long", 7, True))
    ),
    _single(11, "voltage", "voltage", 2),
    _single(12, "heading", "true_heading", 0),
    _single(13, "sea_roughness", "roughness", 5),
    MeasurementDef(
        14,
        "wind",
        (
            FieldDef(0, "speed", 2, True),
            FieldDef(1, "direction", 0, True, {"apparent": "rel_angle"}),
        ),
        allowed_tags=frozenset({"validity", "reference"}),
        required_tags=frozenset({"reference"}),
    ),
    _single(15, "water_depth", "depth", 1),
    _single(16, "atmospheric_pressure", "pressure", 1),
    _single(17, "stw", "speed", 2),
    _single(18, "sog", "speed", 2),
    _single(
        19,
        "cog",
        "course",
        0,
        allowed_tags=frozenset({"validity", "orientation"}),
        required_tags=frozenset({"orientation"}),
    ),
    _single(20, "water_temperature", "temperature", 1),
    _single(21, "log", "event_type", 0),
    MeasurementDef(
        22,
        "battery",
        (
            FieldDef(0, "voltage", 1, True),
            _opt(1, "current", 1),
            _opt(2, "temperature", 1),
            _opt(3, "soc", 0),
            _opt(4, "soh", 0),
        ),
        allowed_tags=_INST,
        required_tags=_INST_REQ,
    ),
    _tank(23, "freshwater"),
    _tank(24, "fuel"),
    _tank(25, "blackwater"),
    MeasurementDef(
        26,
        "engine_n2k",
        (
            FieldDef(0, "engine_speed", 0, True),
            _opt(1, "boost_pressure", 2),
            _opt(2, "tilt_trim", 0),
            _opt(3, "coolant_temperature", 1),
            _opt(4, "oil_pressure", 2),
            _opt(5, "oil_temperature", 1),
            _opt(6, "alternator_voltage", 1),
            _opt(7, "fuel_rate", 1),
            _opt(8, "total_engine_hours", 2),
            _opt(9, "fuel_pressure", 2),
            _opt(10, "coolant_pressure", 2),
            _opt(11, "engine_load", 0),
            _opt(12, "engine_torque", 0),
            _opt(13, "discrete_status_1", 0, max_code=0xFFFF),
            _opt(14, "discrete_status_2", 0, max_code=0xFFFF),
        ),
        allowed_tags=_INST,
        required_tags=_INST_REQ,
    ),
    MeasurementDef(
        27,
        "transmission",
        (
            FieldDef(0, "gear", 0, True, max_code=0xFF),
            _opt(1, "oil_pressure", 2),
            _opt(2, "oil_temperature", 1),
            _opt(3, "discrete_status", 0, max_code=0xFF),
        ),
        allowed_tags=_INST,
        required_tags=_INST_REQ,
    ),
)
MEASUREMENTS_BY_KEY = {m.key: m for m in MEASUREMENTS}


@dataclass
class NEntry:
    """One decoded entry: measurement name, fields (cloud names), tags (lowercase)."""

    measurement: str
    fields: dict[str, float]
    tags: dict[str, str]


@dataclass
class NMessage:
    """A decoded N-message."""

    timestamp: datetime
    origin: str | None  # "wifi", "lte" or None if unknown
    rssi: int | None
    entries: list[NEntry] = field(default_factory=list)


class NMessageError(ValueError):
    """The payload is not a usable N-message v3 (message text never has payload data)."""


def round_half_away(x: float, decimals: int) -> float:
    """Round the exact binary value of x, ties away from zero (section 5.1)."""
    x = float(x)
    if not math.isfinite(x):
        raise ValueError("non-finite")
    if x.is_integer():
        return x + 0.0 if x != 0 else 0.0
    rounded = Decimal(x).quantize(
        Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP, context=_ROUND_CONTEXT
    )
    result = float(rounded)
    return 0.0 if result == 0 else result


class _Opaque:
    """Tolerated but never interpreted item (strings, booleans, null)."""


_OPAQUE = _Opaque()
_FLOAT_FORMATS = {25: ">e", 26: ">f", 27: ">d"}


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_uint(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _decode_item(data: bytes, pos: int, depth: int) -> tuple[Any, int]:
    """Strictly decode the item at pos; return (value, next position)."""
    if pos >= len(data):
        raise ValueError("Truncated CBOR data")
    major, info = data[pos] >> 5, data[pos] & 0x1F
    start = pos + 1
    if info < 24:
        arg, pos = info, start
    elif info < 28:
        pos = start + (1 << (info - 24))
        if pos > len(data):
            raise ValueError("Truncated CBOR data")
        arg = int.from_bytes(data[start:pos], "big")
    elif info == 31:
        raise ValueError("Indefinite-length item")
    else:
        raise ValueError("Malformed CBOR head")

    if major == 0:
        return arg, pos
    if major == 1:
        return -1 - arg, pos
    if major in (2, 3):
        if arg > len(data) - pos:
            raise ValueError("Truncated CBOR data")
        if major == 3:
            try:
                data[pos : pos + arg].decode("utf-8")
            except UnicodeDecodeError:
                raise ValueError("Text string is not valid UTF-8") from None
        return _OPAQUE, pos + arg
    if major == 6:
        raise ValueError("CBOR tag")
    if major == 7:
        if info in _FLOAT_FORMATS:
            return struct.unpack(_FLOAT_FORMATS[info], data[start:pos])[0], pos
        if info == 24 and arg < 32:
            raise ValueError("Malformed CBOR simple value")
        return _OPAQUE, pos

    depth += 1
    if depth > MAX_DEPTH:
        raise ValueError("Nesting too deep")
    if major == 4:
        if arg > len(data) - pos:
            raise ValueError("Truncated CBOR data")
        items = []
        for _ in range(arg):
            item, pos = _decode_item(data, pos, depth)
            items.append(item)
        return items, pos

    if 2 * arg > len(data) - pos:
        raise ValueError("Truncated CBOR data")
    result: dict[int, Any] = {}
    for _ in range(arg):
        if pos >= len(data) or data[pos] >> 5 != 0:
            raise ValueError("Map key is not an unsigned integer")
        key, pos = _decode_item(data, pos, depth)
        result[key], pos = _decode_item(data, pos, depth)
    return result, pos


def is_v3(payload: bytes) -> bool:
    """Return whether the first byte marks a CBOR map (section 2)."""
    return len(payload) > 0 and 0xA0 <= payload[0] <= 0xBF


def _decode_tags(definition: MeasurementDef, raw: Any) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    tags: dict[str, str] = {}
    for key, value in raw.items():
        if key not in _TAG_NAMES or not _is_uint(value):
            return None
        name = _TAG_NAMES[key]
        if name not in definition.allowed_tags:
            return None
        if key == _TAG_INSTANCE:
            if value > MAX_INSTANCE:
                return None
            tags[name] = str(value)
        elif value in _TAG_VALUES[key]:
            tags[name] = _TAG_VALUES[key][value]
        else:
            return None
    return tags if definition.required_tags.issubset(tags) else None


def _split_entry(value: Any) -> tuple[dict[Any, Any], Any] | None:
    """Apply the spellings 1 to 4 of section 4.1."""
    if _is_number(value):
        return {0: value}, None
    if not isinstance(value, dict) or 0 not in value:
        return None
    if isinstance(value[0], dict):
        if 1 in value and not isinstance(value[1], dict):
            return None
        return value[0], value.get(1)
    if not _is_number(value[0]):
        return None
    if isinstance(value.get(1), dict):
        return {0: value[0]}, value[1]
    return value, None


def _decode_entry(definition: MeasurementDef, value: Any) -> NEntry | None:
    split = _split_entry(value)
    if split is None:
        return None
    raw_fields, raw_tags = split
    tags = _decode_tags(definition, raw_tags) if raw_tags else {}
    if tags is None or not definition.required_tags.issubset(tags):
        return None
    fields: dict[str, float] = {}
    for fdef in definition.fields:
        if fdef.number not in raw_fields:
            if fdef.mandatory:
                return None
            continue
        raw = raw_fields[fdef.number]
        if not _is_number(raw):
            return None
        try:
            number = float(raw)
        except OverflowError:
            number = math.inf
        bad = not math.isfinite(number)
        if not bad and fdef.max_code is not None:
            bad = not (number.is_integer() and 0 <= number <= fdef.max_code)
        if bad:
            if fdef.mandatory:
                return None
            continue
        name = fdef.names_by_reference.get(tags.get("reference", ""), fdef.name)
        fields[name] = round_half_away(number, fdef.decimals)
    return NEntry(definition.name, fields, tags)


def _decode_measurement(definition: MeasurementDef, value: Any) -> list[NEntry]:
    candidates = value if isinstance(value, list) else [value]
    unique: dict[frozenset, NEntry] = {}
    for candidate in candidates[:MAX_ENTRIES_PER_MEASUREMENT]:
        entry = _decode_entry(definition, candidate)
        if entry is not None:
            unique[frozenset(entry.tags.items())] = entry  # last one wins (4.4)
    return list(unique.values())


def decode_n_message(payload: bytes) -> NMessage:
    """Decode an N-message v3.

    Raises:
        NMessageError: if the message as a whole must be discarded.
    """
    payload = bytes(payload)
    try:
        if len(payload) > MAX_PAYLOAD_BYTES:
            raise ValueError("Payload too large")
        root, end = _decode_item(payload, 0, 0)
        if end != len(payload):
            raise ValueError("Trailing bytes")
    except ValueError as err:
        raise NMessageError(f"Invalid CBOR: {err}") from None
    except (struct.error, OverflowError, RecursionError):
        raise NMessageError("Invalid CBOR") from None

    if not isinstance(root, dict):
        raise NMessageError("Not a CBOR map")
    version, timestamp, origin = root.get(0), root.get(1), root.get(2)
    if not _is_uint(version) or version != _FORMAT_VERSION:
        raise NMessageError("Not a version 3 N-message")
    if not _is_uint(timestamp) or timestamp > MAX_TIMESTAMP_MS:
        raise NMessageError("Invalid timestamp")
    if not isinstance(origin, dict) or not _is_uint(origin.get(0)):
        raise NMessageError("Invalid origin")

    origin_name = None
    rssi = None
    if origin[0] == _ORIGIN_WIFI:
        origin_name = "wifi"
    elif origin[0] == _ORIGIN_LTE:
        origin_name = "lte"
        value = origin.get(1)
        rssi = value if _is_uint(value) and value <= 6 else None

    entries: list[NEntry] = []
    for key, value in root.items():
        definition = MEASUREMENTS_BY_KEY.get(key)
        if definition is not None:
            entries.extend(_decode_measurement(definition, value))
    return NMessage(
        timestamp=_EPOCH + timedelta(milliseconds=timestamp),
        origin=origin_name,
        rssi=rssi,
        entries=entries,
    )
