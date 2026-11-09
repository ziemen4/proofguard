"""PGV1 canonical values for owner enrollment identities.

Tags: null=0, false=1, true=2, signed i64=3, bytes=4, UTF-8=5,
list=6, map=7. Lengths/counts are u64 big-endian. Maps sort UTF-8 keys.
Floating point and non-string map keys are deliberately unsupported.
"""

import hashlib
import struct


def commitment(domain: str, *parts: bytes) -> str:
    """PGW1 framing: domain and ordered fields, each prefixed by u64 BE length."""
    framed = b"PGW1"
    for part in (domain.encode("ascii"), *parts):
        framed += struct.pack(">Q", len(part)) + part
    return hashlib.sha256(framed).hexdigest()


def encode(value: object) -> bytes:
    """Encode supported owner values using versioned PGV1 tags and sorted map keys."""
    def length(n):
        return struct.pack(">Q", n)

    def term(item):
        """Encode one value recursively, rejecting types outside the PGV1 schema."""
        if item is None:
            return b"\x00"
        if type(item) is bool:
            return b"\x02" if item else b"\x01"
        if type(item) is int:
            return b"\x03" + struct.pack(">q", item)
        if isinstance(item, bytes):
            return b"\x04" + length(len(item)) + item
        if isinstance(item, str):
            data = item.encode("utf-8")
            return b"\x05" + length(len(data)) + data
        if isinstance(item, (tuple, list)):
            return b"\x06" + length(len(item)) + b"".join(term(x) for x in item)
        if isinstance(item, dict) and all(isinstance(key, str) for key in item):
            keys = sorted(item, key=lambda key: key.encode("utf-8"))
            return b"\x07" + length(len(keys)) + b"".join(term(key) + term(item[key]) for key in keys)
        raise ValueError("not a PGV1 canonical value")

    return b"PGV1" + term(value)
