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


# Shared with state.py's single-part centering path so both code paths agree
# on what "fits on this bed" means.
DEFAULT_MARGIN = 5.0


def arrange(
    parts: list[ArrangeInput],
    bed_x: float,
    bed_y: float,
    margin: float = DEFAULT_MARGIN,
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

    # Shift the packed block so its bbox is centered on the bed, instead of
    # hugging the origin corner. Users expect "auto-arrange" to put their
    # parts in the middle of the plate, not tucked into (margin, margin).
    dims = {p.part_id: (p.width, p.depth) for p in parts}
    min_x = min(pl.x for pl in placements)
    min_y = min(pl.y for pl in placements)
    max_x = max(pl.x + dims[pl.part_id][0] for pl in placements)
    max_y = max(pl.y + dims[pl.part_id][1] for pl in placements)
    offset_x = (bed_x - (max_x - min_x)) / 2 - min_x
    offset_y = (bed_y - (max_y - min_y)) / 2 - min_y
    for pl in placements:
        pl.x = max(0.0, pl.x + offset_x)
        pl.y = max(0.0, pl.y + offset_y)

    return placements
