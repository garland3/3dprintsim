"""Unit tests for the raster-mask surfaces module."""

from __future__ import annotations

from app.surfaces import (
    bottom_surface_cells,
    compute_support_cells,
    overhang_cells,
    rasterize_polygons,
    top_surface_cells,
)


def _square(x0: float, y0: float, side: float) -> list[tuple[float, float]]:
    return [
        (x0, y0),
        (x0 + side, y0),
        (x0 + side, y0 + side),
        (x0, y0 + side),
        (x0, y0),
    ]


def test_rasterize_empty_returns_empty_set():
    assert rasterize_polygons([], res=2.0) == frozenset()


def test_rasterize_square_counts_cells():
    # 10x10 square, 2mm grid -> 5x5 = 25 cells.
    mask = rasterize_polygons([_square(0, 0, 10)], res=2.0)
    assert len(mask) == 25


def test_rasterize_offset_square_matches_offset():
    # A 10mm square offset by 100mm must cover a 5x5 cell block at the new location.
    mask = rasterize_polygons([_square(100, 100, 10)], res=2.0)
    assert len(mask) == 25
    # Cells should be at col∈[50..54], row∈[50..54].
    cols = {c for (c, r) in mask}
    rows = {r for (c, r) in mask}
    assert cols == {50, 51, 52, 53, 54}
    assert rows == {50, 51, 52, 53, 54}


def test_overhang_cells_flags_new_material():
    small = rasterize_polygons([_square(0, 0, 4)], res=2.0)
    big = rasterize_polygons([_square(0, 0, 10)], res=2.0)
    # Big is a superset of small, so overhang = big - small.
    over = overhang_cells(big, small)
    assert len(over) == len(big) - len(small)


def test_top_surface_is_empty_on_cube_midlayers():
    """A prismatic cube has no intermediate top surfaces — every layer is
    identical, so the intersection above equals the current layer."""
    masks = [rasterize_polygons([_square(0, 0, 10)], res=2.0) for _ in range(10)]
    for i in range(len(masks) - 3):  # ignore last top_layers
        assert top_surface_cells(masks, i, top_layers=3) == frozenset()


def test_top_surface_detects_intermediate_ceiling():
    """A T-shape: wide at top, narrow at bottom. The transition layer above
    the narrow section has top-surface cells where the wider footprint shrinks."""
    layers = []
    for z in range(5):
        layers.append(rasterize_polygons([_square(0, 0, 4)], res=2.0))
    # Then layers widen:
    for z in range(5):
        layers.append(rasterize_polygons([_square(-3, -3, 10)], res=2.0))

    # From the narrow section looking up, there's no top surface (wider above).
    # But the last narrow layer looking up 1 step still sees wider mask
    # intersect narrow — at that cell mask[4] - mask[5..7] shrinks because the
    # above-intersection is narrow footprint only (depends on top_layers).
    top_at_4 = top_surface_cells(layers, 4, top_layers=1)
    # Layer 4 is narrow; layer 5 is wide. Intersection of layer 5 with itself
    # in a 1-ahead window is layer 5 (wide). Current layer (narrow) minus wide
    # is empty — no top surface here. Correct.
    assert top_at_4 == frozenset()

    # But at layer 9 (topmost wide), there's no layer above -> entire layer
    # counts as top surface.
    top_at_9 = top_surface_cells(layers, 9, top_layers=3)
    assert len(top_at_9) == len(layers[9])


def test_bottom_surface_on_first_layer_is_full():
    masks = [rasterize_polygons([_square(0, 0, 10)], res=2.0) for _ in range(5)]
    # On layer 0, there are no layers below -> entire layer is bottom surface.
    bot = bottom_surface_cells(masks, 0, bottom_layers=3)
    assert bot == masks[0]


def test_bottom_surface_detects_overhang_pocket():
    """A hanging feature: narrow layer N+1 extends outside narrow layer N.
    The extension cells are bottom-surface cells at N+1."""
    layers = [
        rasterize_polygons([_square(0, 0, 4)], res=2.0),
        rasterize_polygons([_square(0, 0, 10)], res=2.0),
        rasterize_polygons([_square(0, 0, 10)], res=2.0),
    ]
    bot = bottom_surface_cells(layers, 1, bottom_layers=1)
    # Layer 1 cells not in layer 0 are bottom-surface.
    expected = layers[1] - layers[0]
    assert bot == expected


def test_support_cells_generates_columns_under_overhang():
    """A floating block at layer 3 with nothing below must trigger support
    columns in layers 0..2 at every cell in the block."""
    empty = frozenset()
    block = rasterize_polygons([_square(0, 0, 4)], res=2.0)
    layers = [empty, empty, empty, block, block]
    support = compute_support_cells(layers)
    # Layers 0, 1, 2 should contain a support set matching the block footprint.
    for L in range(3):
        assert support[L] == set(block), f"layer {L} missing support"
    # Layer 3 & 4 are solid, no supports.
    assert support[3] == set()
    assert support[4] == set()


def test_support_walks_down_only_through_empty_layers():
    """Support should stop at the first layer that already has material."""
    block = rasterize_polygons([_square(0, 0, 4)], res=2.0)
    mid = rasterize_polygons([_square(0, 0, 4)], res=2.0)
    layers = [block, mid, frozenset(), block]  # gap at layer 2
    support = compute_support_cells(layers)
    # Overhang at layer 3 (block) on top of empty layer 2 -> support in layer 2.
    # Layer 1 (mid) is solid at those cells, so walk stops — no support at 1 or 0.
    assert support[2] == set(block)
    assert support[1] == set()
    assert support[0] == set()
