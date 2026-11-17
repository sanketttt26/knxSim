"""Datapoint type codecs for the DPTs the simulated devices use.

Values of 6 bits or less (DPT 1.x, 3.x) travel inside the APCI byte and are
represented as int; larger values are bytes.
"""
from __future__ import annotations

import struct


def _main(dpt: str) -> str:
    return dpt.split(".")[0]


def encode(dpt: str, value) -> int | bytes:
    main = _main(dpt)
    if main == "1":
        return 1 if value else 0
    if main == "3":  # signed step: +n increase, -n decrease, 0 stop
        v = int(value)
        return (0x08 if v > 0 else 0) | min(abs(v), 7)
    if dpt == "5.001":
        return bytes([round(max(0.0, min(100.0, float(value))) * 255 / 100)])
    if main == "5":
        return bytes([int(value) & 0xFF])
    if main == "7":
        return struct.pack(">H", int(value) & 0xFFFF)
    if main == "9":
        return struct.pack(">H", _float16(float(value)))
    if main == "14":
        return struct.pack(">f", float(value))
    raise ValueError(f"unsupported DPT {dpt}")


def decode(dpt: str, payload: int | bytes):
    main = _main(dpt)
    if main == "1":
        return bool((payload if isinstance(payload, int) else payload[0]) & 1)
    if main == "3":
        raw = payload if isinstance(payload, int) else payload[0]
        step = raw & 0x07
        return step if raw & 0x08 else -step
    if isinstance(payload, int) or not payload:
        raise ValueError(f"DPT {dpt} needs a data payload")
    if dpt == "5.001":
        return round(payload[0] * 100 / 255, 1)
    if main == "5":
        return payload[0]
    if main == "7":
        return struct.unpack(">H", payload[:2])[0]
    if main == "9":
        return _float16_decode(struct.unpack(">H", payload[:2])[0])
    if main == "14":
        return round(struct.unpack(">f", payload[:4])[0], 4)
    raise ValueError(f"unsupported DPT {dpt}")


def _float16(value: float) -> int:
    exp = 0
    mant = round(value * 100)
    while not -2048 <= mant <= 2047:
        exp += 1
        if exp > 15:
            raise ValueError(f"{value} out of range for DPT 9")
        mant = round(value * 100 / (1 << exp))
    m12 = mant & 0xFFF
    return (m12 & 0x800) << 4 | exp << 11 | (m12 & 0x7FF)


def _float16_decode(raw: int) -> float:
    mant = raw & 0x7FF
    if raw & 0x8000:
        mant -= 2048
    return round(0.01 * mant * (1 << (raw >> 11 & 0xF)), 2)
