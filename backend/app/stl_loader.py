"""STL parsing producing a numpy-backed mesh and a typed triangle facade.

Supports both ASCII and binary STL. Binary is parsed in one numpy
``frombuffer`` call so a 1M-triangle file lands in milliseconds instead of
the seconds a per-triangle struct loop would cost.

The on-the-wire structure exposed to the slicer + frontend stays unchanged:
``mesh.triangles`` returns a list of ``Triangle`` objects on demand. Bulk
operations (parse, AABB, scale, rotate, translate, validate) work on the
underlying ``(N, 3, 3)`` float64 array so they stay O(N) in numpy rather
than O(N) in Python overhead.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import Sequence

import numpy as np


# 3x3 identity — exposed so callers can compare against "no rotation applied".
IDENTITY_ROTATION: tuple[tuple[float, float, float], ...] = (
    (1.0, 0.0, 0.0),
    (0.0, 1.0, 0.0),
    (0.0, 0.0, 1.0),
)


@dataclass
class Triangle:
    v0: tuple[float, float, float]
    v1: tuple[float, float, float]
    v2: tuple[float, float, float]

    def as_list(self) -> list[list[float]]:
        return [list(self.v0), list(self.v1), list(self.v2)]


class Mesh:
    """Triangle mesh stored as a numpy ``(N, 3, 3)`` float64 array.

    Backwards-compatible: callers can still construct
    ``Mesh(triangles=[Triangle(...), ...], min_xyz=..., max_xyz=...)`` and
    iterate ``mesh.triangles``. Bulk consumers (parse, transforms, AABB,
    validation, the slicer's z-bucket) work on ``mesh.vertices`` directly so
    they stay O(N) without paying Python-object overhead per facet.

    The triangle list is built lazily and cached the first time
    ``mesh.triangles`` is read; transforms invalidate the cache by returning
    a new ``Mesh`` instance built from the transformed vertex array.
    """

    __slots__ = ("_vertices", "_triangles", "min_xyz", "max_xyz")

    def __init__(
        self,
        triangles: list[Triangle] | None = None,
        min_xyz: tuple[float, float, float] | None = None,
        max_xyz: tuple[float, float, float] | None = None,
        *,
        vertices: np.ndarray | None = None,
    ) -> None:
        if vertices is not None:
            v = np.ascontiguousarray(vertices, dtype=np.float64)
            if v.ndim != 3 or v.shape[1] != 3 or v.shape[2] != 3:
                raise ValueError(
                    f"vertices must have shape (N, 3, 3); got {v.shape}"
                )
            self._vertices = v
            self._triangles: list[Triangle] | None = None
        else:
            tris = list(triangles or ())
            self._triangles = tris
            if tris:
                # One bulk allocation beats 3*N Python tuple unpacks.
                self._vertices = np.array(
                    [(t.v0, t.v1, t.v2) for t in tris],
                    dtype=np.float64,
                )
            else:
                self._vertices = np.zeros((0, 3, 3), dtype=np.float64)
        if min_xyz is None or max_xyz is None:
            mn, mx = _compute_aabb(self._vertices)
            min_xyz = mn if min_xyz is None else min_xyz
            max_xyz = mx if max_xyz is None else max_xyz
        self.min_xyz = tuple(float(v) for v in min_xyz)
        self.max_xyz = tuple(float(v) for v in max_xyz)

    @property
    def vertices(self) -> np.ndarray:
        """Underlying ``(N, 3, 3)`` float64 array; vertex i is ``vertices[t, i, :]``."""
        return self._vertices

    @property
    def triangles(self) -> list[Triangle]:
        """Lazy list of Triangle objects.

        Built on first access and cached. The slicer + tests rely on this
        being a real Python list so iteration / indexing match the original
        dataclass-list contract.
        """
        if self._triangles is None:
            v = self._vertices
            n = v.shape[0]
            # Lift to Python lists once, then construct Triangle tuples in a
            # single comprehension. ``tolist()`` is implemented in C and
            # noticeably faster than per-element float() casting.
            data = v.tolist()
            self._triangles = [
                Triangle(
                    (row[0][0], row[0][1], row[0][2]),
                    (row[1][0], row[1][1], row[1][2]),
                    (row[2][0], row[2][1], row[2][2]),
                )
                for row in data
            ]
        return self._triangles

    @property
    def size(self) -> tuple[float, float, float]:
        return (
            self.max_xyz[0] - self.min_xyz[0],
            self.max_xyz[1] - self.min_xyz[1],
            self.max_xyz[2] - self.min_xyz[2],
        )

    def triangle_count(self) -> int:
        """Cheap O(1) count without forcing the triangle-list cache."""
        return int(self._vertices.shape[0])


def _compute_aabb(vertices: np.ndarray) -> tuple[
    tuple[float, float, float], tuple[float, float, float]
]:
    if vertices.size == 0:
        return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
    flat = vertices.reshape(-1, 3)
    mn = flat.min(axis=0)
    mx = flat.max(axis=0)
    return (
        (float(mn[0]), float(mn[1]), float(mn[2])),
        (float(mx[0]), float(mx[1]), float(mx[2])),
    )


def _looks_ascii(data: bytes) -> bool:
    # ASCII STL starts with the literal "solid" and contains "facet normal"
    # somewhere in the first KB. Binary STLs may also start with "solid" in
    # their 80-byte header, hence the second check.
    head = data[:1024].lower()
    return head.startswith(b"solid") and b"facet normal" in head


# Binary STL record layout: 12B normal + 36B vertices + 2B attribute = 50B.
_STL_TRI_DTYPE = np.dtype(
    [
        ("normal", "<f4", 3),
        ("v0", "<f4", 3),
        ("v1", "<f4", 3),
        ("v2", "<f4", 3),
        ("attr", "<u2"),
    ]
)


def _parse_ascii(data: bytes) -> np.ndarray:
    """Parse ASCII STL into the (N, 3, 3) float64 vertex array.

    ASCII STLs are rare for big files (they triple the byte count vs. binary)
    so the per-line Python parser is acceptable here; the bulk-numpy path is
    reserved for binary parses, which dominate real-world uploads.
    """
    verts: list[list[tuple[float, float, float]]] = []
    current: list[tuple[float, float, float]] = []
    for raw in data.decode("utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line.startswith("vertex"):
            parts = line.split()
            if len(parts) >= 4:
                current.append((float(parts[1]), float(parts[2]), float(parts[3])))
                if len(current) == 3:
                    verts.append(current)
                    current = []
        elif line.startswith("endfacet"):
            current = []
    if not verts:
        return np.zeros((0, 3, 3), dtype=np.float64)
    return np.array(verts, dtype=np.float64)


def _parse_binary(data: bytes) -> np.ndarray:
    """Parse a binary STL into a contiguous ``(N, 3, 3)`` float64 array.

    Reads every triangle in a single ``np.frombuffer`` call against a
    structured dtype that mirrors the on-disk layout — the per-triangle
    Python loop the old ``struct.unpack`` parser used was the dominant cost
    on multi-MB files (a 1M-tri STL took multiple seconds where the numpy
    path takes tens of milliseconds).
    """
    if len(data) < 84:
        raise ValueError("binary STL truncated (header)")
    count = struct.unpack("<I", data[80:84])[0]
    if count == 0:
        return np.zeros((0, 3, 3), dtype=np.float64)
    expected = 84 + count * 50
    if len(data) < expected:
        raise ValueError(
            f"binary STL truncated: header claims {count} triangles "
            f"(need {expected} bytes, got {len(data)})"
        )
    # ``frombuffer`` keeps a read-only view into ``data`` — copy the slices
    # into a fresh contiguous array so callers don't have to worry about
    # aliasing the request body.
    arr = np.frombuffer(data, dtype=_STL_TRI_DTYPE, count=count, offset=84)
    out = np.empty((count, 3, 3), dtype=np.float64)
    out[:, 0, :] = arr["v0"]
    out[:, 1, :] = arr["v1"]
    out[:, 2, :] = arr["v2"]
    return out


def parse_stl(data: bytes) -> Mesh:
    if not data:
        raise ValueError("empty STL payload")
    if _looks_ascii(data):
        verts = _parse_ascii(data)
    else:
        verts = _parse_binary(data)
    if verts.shape[0] == 0:
        raise ValueError("no triangles parsed from STL")
    mn, mx = _compute_aabb(verts)
    return Mesh(vertices=verts, min_xyz=mn, max_xyz=mx)


def validate_mesh(mesh: Mesh, *, area_eps: float = 1e-9) -> list[str]:
    """Sanity-check a parsed mesh and return human-readable warnings.

    Checks performed:
      - Degenerate triangles (area ≤ ``area_eps``) — produce garbage slice
        contours because the plane-intersection step can't pick a consistent
        segment.
      - Non-manifold edges — an edge shared by !=2 triangles means the mesh
        is either open (holes that rasterization will mis-classify as both
        interior and exterior) or has T-junctions (stacked slicing picks up
        bogus contours).

    Both checks are fully vectorized with numpy (sort + ``np.unique``), so
    a million-triangle mesh finishes in well under a second instead of the
    multi-second per-edge dict scan a Python loop would cost.
    """
    warnings: list[str] = []
    v = mesh.vertices
    n = int(v.shape[0])
    if n == 0:
        return warnings

    a = v[:, 0, :]
    b = v[:, 1, :]
    c = v[:, 2, :]
    u = b - a
    w = c - a
    cross = np.cross(u, w)
    area2 = np.einsum("ij,ij->i", cross, cross)
    threshold = area_eps * area_eps * 4.0
    degenerate = int(np.count_nonzero(area2 < threshold))
    if degenerate:
        warnings.append(
            f"{degenerate} degenerate triangles (area ~ 0) — slicer output "
            "near those faces may be unreliable"
        )

    # Build a (3N, 6) array where each row is one undirected edge — the two
    # endpoints lex-sorted so adjacent facets sharing the edge collide. We
    # flatten the 6 float64s into a single 48-byte structured dtype and let
    # ``np.unique`` sort/count in a 1D pass; that is ~10x faster than
    # ``np.unique(..., axis=0)`` on a 2D float array because it skips the
    # axis-tuple comparison machinery and walks contiguous memory.
    edges_a = v[:, [0, 1, 2], :]  # (N, 3, 3) — start of each edge
    edges_b = v[:, [1, 2, 0], :]  # (N, 3, 3) — end   of each edge
    pa = edges_a.reshape(-1, 3)
    pb = edges_b.reshape(-1, 3)
    swap = _lex_less(pb, pa)
    lo = np.where(swap[:, None], pb, pa)
    hi = np.where(swap[:, None], pa, pb)
    edge_rows = np.ascontiguousarray(np.concatenate([lo, hi], axis=1))
    edge_bytes = edge_rows.view(np.dtype((np.void, edge_rows.dtype.itemsize * 6)))
    _, counts = np.unique(edge_bytes, return_counts=True)
    non_manifold = int(np.count_nonzero(counts != 2))
    if non_manifold:
        warnings.append(
            f"{non_manifold} non-manifold edges — mesh is not closed or has "
            "T-junctions; support/overhang detection may miss features"
        )
    return warnings


def _lex_less(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Row-wise ``a < b`` lexicographic comparison for (M, K) arrays."""
    eq01 = a[:, 0] == b[:, 0]
    eq012 = eq01 & (a[:, 1] == b[:, 1])
    return (
        (a[:, 0] < b[:, 0])
        | (eq01 & (a[:, 1] < b[:, 1]))
        | (eq012 & (a[:, 2] < b[:, 2]))
    )


def scale_mesh(mesh: Mesh, factor: float) -> Mesh:
    """Return a copy of `mesh` with every vertex coordinate multiplied by `factor`."""
    if factor <= 0:
        raise ValueError(f"scale factor must be positive, got {factor}")
    if factor == 1.0:
        return mesh
    verts = mesh.vertices * factor
    mn = (
        mesh.min_xyz[0] * factor,
        mesh.min_xyz[1] * factor,
        mesh.min_xyz[2] * factor,
    )
    mx = (
        mesh.max_xyz[0] * factor,
        mesh.max_xyz[1] * factor,
        mesh.max_xyz[2] * factor,
    )
    return Mesh(vertices=verts, min_xyz=mn, max_xyz=mx)


Matrix3 = Sequence[Sequence[float]]


def axis_rotation_matrix(axis: str, degrees: float) -> tuple[tuple[float, float, float], ...]:
    """Right-handed rotation matrix around world X/Y/Z by `degrees`."""
    theta = math.radians(degrees)
    c = math.cos(theta)
    s = math.sin(theta)
    axis = axis.lower()
    if axis == "x":
        return (
            (1.0, 0.0, 0.0),
            (0.0, c, -s),
            (0.0, s, c),
        )
    if axis == "y":
        return (
            (c, 0.0, s),
            (0.0, 1.0, 0.0),
            (-s, 0.0, c),
        )
    if axis == "z":
        return (
            (c, -s, 0.0),
            (s, c, 0.0),
            (0.0, 0.0, 1.0),
        )
    raise ValueError(f"axis must be one of x/y/z, got {axis!r}")


def multiply_matrix(a: Matrix3, b: Matrix3) -> tuple[tuple[float, float, float], ...]:
    """Standard 3x3 matrix product (row-major)."""
    return tuple(
        tuple(
            a[i][0] * b[0][j] + a[i][1] * b[1][j] + a[i][2] * b[2][j]
            for j in range(3)
        )
        for i in range(3)
    )


def rotate_mesh(mesh: Mesh, matrix: Matrix3) -> Mesh:
    """Apply a 3x3 rotation (or any linear map) to every vertex of `mesh`.

    Recomputes the AABB after the transform because a rotated object's
    bounds are tighter or wider than the original.
    """
    m = np.asarray(matrix, dtype=np.float64)
    if m.shape != (3, 3):
        raise ValueError(f"matrix must be 3x3, got {m.shape}")
    # Vertices: (N, 3, 3); apply m to each row's last axis: out = v @ m.T
    verts = mesh.vertices @ m.T
    mn, mx = _compute_aabb(verts)
    return Mesh(vertices=verts, min_xyz=mn, max_xyz=mx)


def translate(mesh: Mesh, dx: float, dy: float, dz: float) -> Mesh:
    if dx == 0.0 and dy == 0.0 and dz == 0.0:
        return mesh
    delta = np.array([dx, dy, dz], dtype=np.float64)
    verts = mesh.vertices + delta
    mn = (mesh.min_xyz[0] + dx, mesh.min_xyz[1] + dy, mesh.min_xyz[2] + dz)
    mx = (mesh.max_xyz[0] + dx, mesh.max_xyz[1] + dy, mesh.max_xyz[2] + dz)
    return Mesh(vertices=verts, min_xyz=mn, max_xyz=mx)
