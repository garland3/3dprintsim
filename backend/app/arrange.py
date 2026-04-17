"""Shelf packer for part footprints on the print bed.

Each part's XY footprint is treated as its AABB width/depth plus a configurable
margin. We place parts on rows (shelves): widest-first, left-to-right, wrapping
to a new row when the current row overflows. This is not optimal but is robust
and deterministic, which matters for tests.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Placement:
    part_id: str
    x: float  # translation applied to bring part's min-x to this value
    y: float
    rotation_deg: float = 0.0


@dataclass
class ArrangeInput:
    part_id: str
    width: float
    depth: float


class ArrangeError(Exception):
    pass


def arrange(
    parts: list[ArrangeInput],
    bed_x: float,
    bed_y: float,
    margin: float = 5.0,
) -> list[Placement]:
    if not parts:
        return []

    # widest first for a cleaner shelf packing
    ordered = sorted(parts, key=lambda p: p.width, reverse=True)

    placements: list[Placement] = []
    cursor_x = margin
    cursor_y = margin
    row_depth = 0.0

    for part in ordered:
        if part.width + 2 * margin > bed_x or part.depth + 2 * margin > bed_y:
            raise ArrangeError(
                f"part {part.part_id} ({part.width:.1f}x{part.depth:.1f}) "
                f"does not fit on {bed_x:.0f}x{bed_y:.0f} bed"
            )

        if cursor_x + part.width + margin > bed_x:
            # wrap to next row
            cursor_x = margin
            cursor_y += row_depth + margin
            row_depth = 0.0

        if cursor_y + part.depth + margin > bed_y:
            raise ArrangeError(
                f"ran out of bed space placing {part.part_id}; "
                f"reduce parts or enlarge bed"
            )

        placements.append(Placement(part_id=part.part_id, x=cursor_x, y=cursor_y))
        cursor_x += part.width + margin
        row_depth = max(row_depth, part.depth)

    return placements
