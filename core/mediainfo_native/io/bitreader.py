"""Bit-level reading utilities."""

from __future__ import annotations


class BitReader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.bitpos = 0

    def read_bits(self, count: int) -> int:
        out = 0
        for _ in range(count):
            byte_index = self.bitpos // 8
            if byte_index >= len(self.data):
                return out
            bit_index = 7 - (self.bitpos % 8)
            out = (out << 1) | ((self.data[byte_index] >> bit_index) & 1)
            self.bitpos += 1
        return out

    def read_bit(self) -> int:
        return self.read_bits(1)

    def read_ue(self) -> int:
        zeros = 0
        while self.read_bit() == 0:
            zeros += 1
            if zeros > 31:
                return 0
        return (1 << zeros) - 1 + self.read_bits(zeros)

    def read_se(self) -> int:
        ue = self.read_ue()
        return -(ue // 2) if ue % 2 == 0 else (ue + 1) // 2
