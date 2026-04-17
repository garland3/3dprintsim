"""STL parsing producing a typed triangle list and AABB.

Supports both ASCII and binary STL. We roll our own parser instead of using
numpy-stl's mesh object directly so the output structure matches exactly what
the slicer and the frontend need (plain lists of floats, JSON-serializable).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Iterable


@dataclass
class Triangle:
    v0: tuple[float, float, float]
    v1: tuple[float, float, float]
    v2: tuple[float, float, float]

    def as_list(self) -> list[list[float]]:
        return [list(self.v0), list(self.v1), list(self.v2)]


@dataclass
class Mesh:
    triangles: list[Triangle]
    min_xyz: tuple[float, float, float]
    max_xyz: tuple[float, float, float]

    @property
    def size(self) -> tuple[float, float, float]:
        return (
            self.max_xyz[0] - self.min_xyz[0],
            self.max_xyz[1] - self.min_xyz[1],
            self.max_xyz[2] - self.min_xyz[2],
        )


def _aabb(triangles: Iterable[Triangle]) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    xs, ys, zs = [], [], []
    for t in triangles:
        for v in (t.v0, t.v1, t.v2):
            xs.append(v[0])
            ys.append(v[1])
            zs.append(v[2])
    if not xs:
        return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def _looks_ascii(data: bytes) -> bool:
    # ASCII STL starts with the literal "solid" and contains "facet normal"
    # somewhere in the first KB. Binary STLs may also start with "solid" in
    # their 80-byte header, hence the second check.
    head = data[:1024].lower()
    return head.startswith(b"solid") and b"facet normal" in head


def _parse_ascii(data: bytes) -> list[Triangle]:
    tris: list[Triangle] = []
    current: list[tuple[float, float, float]] = []
    for raw in data.decode("utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line.startswith("vertex"):
            parts = line.split()
            if len(parts) >= 4:
                current.append((float(parts[1]), float(parts[2]), float(parts[3])))
                if len(current) == 3:
                    tris.append(Triangle(current[0], current[1], current[2]))
                    current = []
        elif line.startswith("endfacet"):
            current = []
    return tris


def _parse_binary(data: bytes) -> list[Triangle]:
    if len(data) < 84:
        raise ValueError("binary STL truncated (header)")
    count = struct.unpack("<I", data[80:84])[0]
    expected = 84 + count * 50
    if len(data) < expected:
        raise ValueError(
            f"binary STL truncated: header claims {count} triangles "
            f"(need {expected} bytes, got {len(data)})"
        )
    tris: list[Triangle] = []
    offset = 84
    for _ in range(count):
        # 12 bytes normal + 3*12 bytes vertices + 2 bytes attribute
        vx1, vy1, vz1, vx2, vy2, vz2, vx3, vy3, vz3 = struct.unpack(
            "<12x9f", data[offset : offset + 48]
        )
        tris.append(
            Triangle(
                (vx1, vy1, vz1),
                (vx2, vy2, vz2),
                (vx3, vy3, vz3),
            )
        )
        offset += 50
    return tris


def parse_stl(data: bytes) -> Mesh:
    if not data:
        raise ValueError("empty STL payload")
    if _looks_ascii(data):
        tris = _parse_ascii(data)
    else:
        tris = _parse_binary(data)
    if not tris:
        raise ValueError("no triangles parsed from STL")
    mn, mx = _aabb(tris)
    return Mesh(triangles=tris, min_xyz=mn, max_xyz=mx)


def scale_mesh(mesh: Mesh, factor: float) -> Mesh:
    """Return a copy of `mesh` with every vertex coordinate multiplied by `factor`.

    Useful for unit conversion (inches → mm is `factor=25.4`) and for letting
    the user resize a part after it's been uploaded.
    """
    if factor <= 0:
        raise ValueError(f"scale factor must be positive, got {factor}")
    tris = [
        Triangle(
            (t.v0[0] * factor, t.v0[1] * factor, t.v0[2] * factor),
            (t.v1[0] * factor, t.v1[1] * factor, t.v1[2] * factor),
            (t.v2[0] * factor, t.v2[1] * factor, t.v2[2] * factor),
        )
        for t in mesh.triangles
    ]
    return Mesh(
        triangles=tris,
        min_xyz=(mesh.min_xyz[0] * factor, mesh.min_xyz[1] * factor, mesh.min_xyz[2] * factor),
        max_xyz=(mesh.max_xyz[0] * factor, mesh.max_xyz[1] * factor, mesh.max_xyz[2] * factor),
    )


def translate(mesh: Mesh, dx: float, dy: float, dz: float) -> Mesh:
    tris = [
        Triangle(
            (t.v0[0] + dx, t.v0[1] + dy, t.v0[2] + dz),
            (t.v1[0] + dx, t.v1[1] + dy, t.v1[2] + dz),
            (t.v2[0] + dx, t.v2[1] + dy, t.v2[2] + dz),
        )
        for t in mesh.triangles
    ]
    return Mesh(
        triangles=tris,
        min_xyz=(mesh.min_xyz[0] + dx, mesh.min_xyz[1] + dy, mesh.min_xyz[2] + dz),
        max_xyz=(mesh.max_xyz[0] + dx, mesh.max_xyz[1] + dy, mesh.max_xyz[2] + dz),
    )
