"""Endian-aware primitives."""

from __future__ import annotations


def u16be(data: bytes) -> int:
    return int.from_bytes(data[:2], "big", signed=False)


def u32be(data: bytes) -> int:
    return int.from_bytes(data[:4], "big", signed=False)


def u64be(data: bytes) -> int:
    return int.from_bytes(data[:8], "big", signed=False)


def u16le(data: bytes) -> int:
    return int.from_bytes(data[:2], "little", signed=False)


def u32le(data: bytes) -> int:
    return int.from_bytes(data[:4], "little", signed=False)


def u64le(data: bytes) -> int:
    return int.from_bytes(data[:8], "little", signed=False)
