"""Primitives SEI communes au remux et à la normalisation HEVC."""
from __future__ import annotations


def remove_emulation_prevention_bytes(data: bytes) -> bytes:
    out = bytearray()
    zeros = 0
    for byte in data:
        if zeros >= 2 and byte == 3:
            zeros = 0
            continue
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


def add_emulation_prevention_bytes(data: bytes) -> bytes:
    out = bytearray()
    zeros = 0
    for byte in data:
        if zeros >= 2 and byte <= 3:
            out.append(3)
            zeros = 0
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


def iter_sei_messages(rbsp: bytes, *, strict: bool = False) -> list[tuple[int, bytes]]:
    messages: list[tuple[int, bytes]] = []
    pos = 0
    while pos < len(rbsp):
        if rbsp[pos:] == b"\x80":
            return messages
        values = []
        for _ in range(2):
            value = 0
            while pos < len(rbsp) and rbsp[pos] == 255:
                value += 255
                pos += 1
            if pos == len(rbsp):
                if strict:
                    raise ValueError("En-tête SEI tronqué")
                return messages
            values.append(value + rbsp[pos])
            pos += 1
        kind, size = values
        if pos + size > len(rbsp):
            if strict:
                raise ValueError("Message SEI tronqué")
            return messages
        messages.append((kind, rbsp[pos:pos + size]))
        pos += size
    if strict:
        raise ValueError("Fin de RBSP SEI absente")
    return messages


def encode_sei_value(value: int) -> bytes:
    if value < 0:
        raise ValueError("Valeur SEI négative")
    return b"\xff" * (value // 255) + bytes([value % 255])


def build_sei_rbsp(messages: list[tuple[int, bytes]]) -> bytes:
    return add_emulation_prevention_bytes(b"".join(
        encode_sei_value(kind) + encode_sei_value(len(payload)) + payload
        for kind, payload in messages
    ) + b"\x80")
