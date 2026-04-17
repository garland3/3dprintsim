"""Test fixture helpers — synthetic STL binaries."""

from __future__ import annotations

import struct


def make_binary_cube_stl(size: float = 10.0) -> bytes:
    """Return a binary STL for an axis-aligned cube [0,size]^3."""
    verts = [
        # each face as two triangles
        # bottom (z=0)
        [(0, 0, 0), (size, 0, 0), (size, size, 0)],
        [(0, 0, 0), (size, size, 0), (0, size, 0)],
        # top (z=size)
        [(0, 0, size), (size, size, size), (size, 0, size)],
        [(0, 0, size), (0, size, size), (size, size, size)],
        # -x
        [(0, 0, 0), (0, size, 0), (0, size, size)],
        [(0, 0, 0), (0, size, size), (0, 0, size)],
        # +x
        [(size, 0, 0), (size, 0, size), (size, size, size)],
        [(size, 0, 0), (size, size, size), (size, size, 0)],
        # -y
        [(0, 0, 0), (0, 0, size), (size, 0, size)],
        [(0, 0, 0), (size, 0, size), (size, 0, 0)],
        # +y
        [(0, size, 0), (size, size, 0), (size, size, size)],
        [(0, size, 0), (size, size, size), (0, size, size)],
    ]
    header = b"binary cube".ljust(80, b"\x00")
    out = [header, struct.pack("<I", len(verts))]
    for tri in verts:
        out.append(struct.pack("<fff", 0.0, 0.0, 0.0))  # normal
        for v in tri:
            out.append(struct.pack("<fff", *v))
        out.append(struct.pack("<H", 0))
    return b"".join(out)


def make_ascii_triangle_stl() -> bytes:
    """Minimal ASCII STL with a single triangle (degenerate vertically)."""
    return (
        b"solid tri\n"
        b"  facet normal 0 0 1\n"
        b"    outer loop\n"
        b"      vertex 0 0 0\n"
        b"      vertex 10 0 0\n"
        b"      vertex 0 10 0\n"
        b"    endloop\n"
        b"  endfacet\n"
        b"endsolid tri\n"
    )
