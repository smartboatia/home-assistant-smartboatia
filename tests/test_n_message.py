"""Decoder tests (sample payloads from the platform reference tests)."""

from __future__ import annotations

import cbor2
import pytest

from custom_components.smartboatia.n_message import (
    NMessageError,
    decode_n_message,
    is_v3,
    round_half_away,
)

from .conftest import EXAMPLE_HEX, EXAMPLE_TS, make_message


def by_name(message):
    return {(e.measurement, tuple(sorted(e.tags.items()))): e.fields for e in message.entries}


def test_spec_example():
    payload = bytes.fromhex(EXAMPLE_HEX)
    assert is_v3(payload)
    msg = decode_n_message(payload)
    assert int(msg.timestamp.timestamp() * 1000) == EXAMPLE_TS
    assert msg.origin == "lte"
    assert msg.rssi == 4
    got = by_name(msg)
    assert got[("gps_pos", ())] == {"lat": 53.550123, "long": 9.993456}
    assert got[("voltage", ())] == {"voltage": 12.6}
    assert got[("sea_roughness", ())] == {"roughness": 0.4}
    assert got[("wind", (("reference", "ground"),))] == {"speed": 12.3, "direction": 240}
    assert got[("water_depth", ())] == {"depth": 8.2}
    assert got[("atmospheric_pressure", ())] == {"pressure": 1013.2}
    assert got[("sog", ())] == {"speed": 6.4}
    assert got[("cog", (("orientation", "true"),))] == {"course": 245}
    assert got[("water_temperature", ())] == {"temperature": 17.5}
    assert got[("log", ())] == {"event_type": 6}


@pytest.mark.parametrize(
    ("x", "d", "expected"),
    [
        (12.6, 2, 12.6),
        (1013.25, 1, 1013.3),
        (1.005, 2, 1.0),
        (244.5, 0, 245.0),
        (-2.5, 0, -3.0),
        (0.125, 2, 0.13),
        (0.39990234375, 5, 0.3999),
    ],
)
def test_rounding(x, d, expected):
    assert round_half_away(x, d) == expected


def test_half_precision_noise_is_rounded():
    # half precision 12.6 is 12.6015625
    payload = make_message((11, bytes.fromhex("f94a4d")))
    assert decode_n_message(payload).entries[0].fields == {"voltage": 12.6}


def test_spellings_are_equivalent():
    for value in (6.4, {0: 6.4}, {0: {0: 6.4}}):
        msg = decode_n_message(make_message((18, value)))
        assert msg.entries[0].fields == {"speed": 6.4}


def test_wind_references_and_angle_name():
    payload = make_message(
        (
            14,
            [
                {0: {0: 12.3, 1: 240}, 1: {1: 1}},
                {0: {0: 14.1, 1: 35}, 1: {1: 0}},
            ],
        )
    )
    got = by_name(decode_n_message(payload))
    assert got[("wind", (("reference", "ground"),))] == {"speed": 12.3, "direction": 240}
    assert got[("wind", (("reference", "apparent"),))] == {"speed": 14.1, "rel_angle": 35}


def test_instances():
    payload = make_message(
        (22, [{0: {0: 12.6, 1: -3.2, 3: 88}, 1: {3: 0}}, {0: {0: 13.2}, 1: {3: 1}}]),
        (24, {0: 41, 1: {3: 0}}),
        (26, {0: {0: 1850.5, 13: 5}, 1: {3: 0}}),
        (27, {0: {0: 2, 3: 300}, 1: {3: 0}}),
    )
    got = by_name(decode_n_message(payload))
    assert got[("battery", (("instance", "0"),))] == {"voltage": 12.6, "current": -3.2, "soc": 88}
    assert got[("battery", (("instance", "1"),))] == {"voltage": 13.2}
    assert got[("fuel", (("instance", "0"),))] == {"level": 41}
    # status 5 = bits 0 and 2, round-trips as the code
    assert got[("engine_n2k", (("instance", "0"),))] == {"engine_speed": 1851.0, "discrete_status_1": 5}
    # status 300 is above 255: no data for that optional field, entry stays
    assert got[("transmission", (("instance", "0"),))] == {"gear": 2}


def test_missing_mandatory_field_skips_only_the_entry():
    payload = make_message((10, {0: 53.5}), (18, 6.4))
    msg = decode_n_message(payload)
    assert [e.measurement for e in msg.entries] == ["sog"]


def test_instance_above_255_and_unknown_tag_skip_entry():
    payload = make_message(
        (24, {0: 41, 1: {3: 256}}), (23, {0: 41, 1: {9: 1}}), (18, 1.5)
    )
    assert [e.measurement for e in decode_n_message(payload).entries] == ["sog"]


def test_unknown_keys_and_fields_ignored():
    payload = make_message((99, {0: 1}), (18, {0: 6.4, 7: "text"}))
    msg = decode_n_message(payload)
    assert msg.entries[0].fields == {"speed": 6.4}


def test_nan_field_skipped():
    payload = make_message((18, bytes.fromhex("f97e00")))
    assert decode_n_message(payload).entries == []


def test_wifi_origin():
    msg = decode_n_message(make_message((18, 1.5)))
    assert msg.origin == "wifi"
    assert msg.rssi is None


def test_duplicate_tag_sets_last_wins():
    payload = make_message((24, [{0: 10, 1: {3: 0}}, {0: 20, 1: {3: 0}}]))
    assert decode_n_message(payload).entries[0].fields == {"level": 20}


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"1761578411000A53.5,9.9",  # v2 text
        b"\xa0",  # empty map: no version
        bytes.fromhex(EXAMPLE_HEX)[:-1],  # truncated
        bytes.fromhex(EXAMPLE_HEX) + b"\x00",  # trailing bytes
        cbor2.dumps({0: 2, 1: EXAMPLE_TS, 2: {0: 0}}),
        cbor2.dumps({0: 3, 2: {0: 0}}),  # no timestamp
        cbor2.dumps({0: 3, 1: EXAMPLE_TS}),  # no origin
        cbor2.dumps({0: 3, 1: 253402300800000, 2: {0: 0}}),  # timestamp too large
        cbor2.dumps({"a": 1}),  # text key
        cbor2.dumps({0: 3, 1: EXAMPLE_TS, 2: {0: 0}, 18: cbor2.CBORTag(28, 1)}),
        bytes.fromhex("bf00030102") + b"\xff",  # indefinite length
        b"\xa1" * 20 + b"\x00",  # too deep / truncated
        b"\xa0" + b"\x00" * 20000,  # too large
    ],
)
def test_discarded(payload):
    with pytest.raises(NMessageError):
        decode_n_message(payload)


def test_error_message_has_no_payload_data():
    with pytest.raises(NMessageError) as err:
        decode_n_message(cbor2.dumps({0: 3, 1: EXAMPLE_TS, 2: {0: 0}, 18: cbor2.CBORTag(28, "secret-value")}))
    assert "secret-value" not in str(err.value)
