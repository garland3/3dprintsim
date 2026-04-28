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


def make_binary_t_overhang_stl(
    stem_w: float = 4.0,
    stem_h: float = 5.0,
    cap_w: float = 10.0,
    cap_h: float = 4.0,
) -> bytes:
    """T-shape STL: a narrow stem from z=0..stem_h and a wider cap on top
    (z=stem_h..stem_h+cap_h). Useful for testing overhang/support logic —
    the cap's underside that sticks out past the stem needs support.

    Both halves are axis-aligned cuboids centered on (0,0) in XY. Writing
    this out as 24 triangles (12 per cuboid) keeps the fixture minimal
    while still exercising intersecting-mesh → overhang detection.
    """
    def cuboid_triangles(x0, y0, z0, x1, y1, z1):
        # 6 faces × 2 tris
        return [
            [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0)],  # -z
            [(x0, y0, z0), (x1, y1, z0), (x0, y1, z0)],
            [(x0, y0, z1), (x1, y1, z1), (x1, y0, z1)],  # +z
            [(x0, y0, z1), (x0, y1, z1), (x1, y1, z1)],
            [(x0, y0, z0), (x0, y1, z0), (x0, y1, z1)],  # -x
            [(x0, y0, z0), (x0, y1, z1), (x0, y0, z1)],
            [(x1, y0, z0), (x1, y0, z1), (x1, y1, z1)],  # +x
            [(x1, y0, z0), (x1, y1, z1), (x1, y1, z0)],
            [(x0, y0, z0), (x0, y0, z1), (x1, y0, z1)],  # -y
            [(x0, y0, z0), (x1, y0, z1), (x1, y0, z0)],
            [(x0, y1, z0), (x1, y1, z0), (x1, y1, z1)],  # +y
            [(x0, y1, z0), (x1, y1, z1), (x0, y1, z1)],
        ]

    sw, sh = stem_w / 2.0, stem_h
    cw, ch = cap_w / 2.0, cap_h
    tris = cuboid_triangles(-sw, -sw, 0.0, sw, sw, sh)
    tris += cuboid_triangles(-cw, -cw, sh, cw, cw, sh + ch)

    header = b"binary t-overhang".ljust(80, b"\x00")
    out = [header, struct.pack("<I", len(tris))]
    for tri in tris:
        out.append(struct.pack("<fff", 0.0, 0.0, 0.0))
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


def make_3mf_cube(size: float = 10.0, *, transform: str | None = None) -> bytes:
    """Return a tiny 3MF (ZIP) carrying a single-cube model.

    Mirrors `make_binary_cube_stl` so the same upload-pipeline tests can
    exercise both formats. `transform` lets callers pass a build-item
    transformation matrix (12-float, column-major) to verify the loader
    applies build transforms correctly.
    """
    import io
    import zipfile

    s = size
    verts = [
        (0, 0, 0), (s, 0, 0), (s, s, 0), (0, s, 0),
        (0, 0, s), (s, 0, s), (s, s, s), (0, s, s),
    ]
    tris = [
        (0, 2, 1), (0, 3, 2),
        (4, 5, 6), (4, 6, 7),
        (0, 1, 5), (0, 5, 4),
        (3, 7, 6), (3, 6, 2),
        (1, 2, 6), (1, 6, 5),
        (0, 4, 7), (0, 7, 3),
    ]

    vert_xml = "".join(
        f'<vertex x="{x}" y="{y}" z="{z}"/>' for (x, y, z) in verts
    )
    tri_xml = "".join(
        f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for (a, b, c) in tris
    )
    item_attr = f' transform="{transform}"' if transform else ""
    model_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<model unit="millimeter" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        "<resources>"
        '<object id="1" type="model"><mesh>'
        f"<vertices>{vert_xml}</vertices>"
        f"<triangles>{tri_xml}</triangles>"
        "</mesh></object>"
        "</resources>"
        f'<build><item objectid="1"{item_attr}/></build>'
        "</model>"
    )

    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            (
                '<?xml version="1.0"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="model" '
                'ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
                "</Types>"
            ),
        )
        zf.writestr("3D/3dmodel.model", model_xml)
    return bio.getvalue()
