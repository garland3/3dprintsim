"""Multi-format mesh loader.

Sniffs the incoming file (by magic bytes first, filename second) and dispatches
to the appropriate parser. Returns the same ``Mesh`` shape everything else in
the pipeline already understands, so adding a new format only requires a
``_parse_<ext>`` helper here.

Currently supported:
  - ``.stl`` — binary or ASCII (numpy-backed bulk parser in ``stl_loader``)
  - ``.3mf`` — ZIP container holding a ``3dmodel.model`` XML payload, parsed
    natively with stdlib (``zipfile`` + ``xml.etree``). Supports
    multi-component builds via ``<build><item>`` transforms.
  - ``.step`` / ``.stp`` — optional, requires either ``cadquery`` or
    ``trimesh`` with STEP-loading extras. Falls back to a clear error when
    no STEP toolchain is installed.
"""

from __future__ import annotations

import io
import xml.etree.ElementTree as ET
import zipfile
from typing import Iterable

import numpy as np

from .stl_loader import Mesh, _compute_aabb, parse_stl


# ZIP local-file-header magic. 3MF and any other ZIP-based format starts here.
_ZIP_MAGIC = b"PK\x03\x04"
# STEP files are ASCII and always begin with the standard ISO header. Some
# tools (especially Windows-side CAD exporters) prepend a UTF-8 BOM
# (0xEF 0xBB 0xBF), so accept that prefix too.
_STEP_HEADER_PREFIXES = (
    b"ISO-10303-21",
    b"\xef\xbb\xbfISO-10303-21",
    b"FILE_DESCRIPTION",
)


def parse_mesh(data: bytes, filename: str = "") -> Mesh:
    """Parse `data` into a Mesh, picking the right format by magic + filename.

    Filename is advisory — content sniffing wins when the two disagree (e.g.
    a renamed `.stl` file uploaded as `.txt` still parses).
    """
    if not data:
        raise ValueError("empty mesh payload")
    fmt = _detect_format(data, filename)
    if fmt == "stl":
        return parse_stl(data)
    if fmt == "3mf":
        return parse_3mf(data)
    if fmt == "step":
        return parse_step(data)
    raise ValueError(
        f"unknown mesh format for {filename!r} (supported: .stl, .3mf, .step/.stp)"
    )


def _detect_format(data: bytes, filename: str) -> str:
    name = filename.lower()
    if data.startswith(_ZIP_MAGIC):
        # Every modern 3MF is a zip; .gltf-binary is also magic-distinct.
        # If a different zip-based format ever shows up here we can sniff
        # the inner manifest to disambiguate.
        return "3mf"
    head = data[:128].lstrip().lower()
    for prefix in _STEP_HEADER_PREFIXES:
        if head.startswith(prefix.lower()):
            return "step"
    if name.endswith(".3mf"):
        return "3mf"
    if name.endswith(".step") or name.endswith(".stp"):
        return "step"
    if name.endswith(".stl"):
        return "stl"
    # No magic and no suffix — try STL last because its detection inside
    # parse_stl handles both ASCII and binary cleanly.
    return "stl"


# --- 3MF -------------------------------------------------------------------


# Default 3MF XML namespace. Spec is permissive about the exact URL but every
# producer we've seen ships this one.
_3MF_NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"


def parse_3mf(data: bytes) -> Mesh:
    """Parse a 3MF file (ZIP + XML) into a Mesh.

    A 3MF can carry many ``<object>`` resources and a ``<build>`` block that
    references which of them to render — usually with per-instance transforms.
    We honor the build list so multi-component models (a base + lid in the
    same file) come in pre-arranged. Objects referenced multiple times yield
    one instance per ``<item>``.
    """
    # `with` guarantees the ZipFile (and its in-memory BytesIO) gets closed
    # even on parse failure — the BytesIO release is cheap, but tying
    # lifetime to the function scope keeps ownership unambiguous.
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            # The spec requires the model at `3D/3dmodel.model`, but some
            # authoring tools tuck it elsewhere. Search for the first
            # `.model` so we don't silently 400 on legitimate-but-quirky
            # files.
            model_name = None
            for name in zf.namelist():
                lname = name.lower()
                if lname.endswith("3dmodel.model"):
                    model_name = name
                    break
                if model_name is None and lname.endswith(".model"):
                    model_name = name
            if model_name is None:
                raise ValueError("3MF archive missing a `.model` payload")
            xml_bytes = zf.read(model_name)
    except zipfile.BadZipFile as exc:
        raise ValueError(f"not a valid 3MF/ZIP: {exc}") from exc
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ValueError(f"3MF model XML invalid: {exc}") from exc

    # Resource table: object id -> (vertex array, triangle index array).
    # Triangle index → real vertex index is per-object.
    objects: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    resources_node = _find(root, f"{_3MF_NS}resources") or _find(root, "resources")
    if resources_node is None:
        raise ValueError("3MF model missing <resources>")
    for obj in _findall(resources_node, "object"):
        oid = obj.attrib.get("id")
        if oid is None:
            continue
        mesh_node = _find_named(obj, "mesh")
        if mesh_node is None:
            continue
        vert_node = _find_named(mesh_node, "vertices")
        tri_node = _find_named(mesh_node, "triangles")
        if vert_node is None or tri_node is None:
            continue
        verts = _collect_3mf_vertices(vert_node)
        tris = _collect_3mf_triangles(tri_node)
        if verts.size == 0 or tris.size == 0:
            continue
        objects[oid] = (verts, tris)

    if not objects:
        raise ValueError("3MF contains no usable mesh objects")

    # Build pass: each `<item>` references an object id and optionally a
    # `transform` (12 floats interpreted here in column-major order and
    # applied as a 4x4 affine — see _parse_3mf_transform for the layout).
    chunks: list[np.ndarray] = []
    build_node = _find(root, f"{_3MF_NS}build") or _find(root, "build")
    items: list[tuple[str, np.ndarray]] = []
    if build_node is not None:
        for item in _findall(build_node, "item"):
            oid = item.attrib.get("objectid") or item.attrib.get("objectId")
            if oid is None or oid not in objects:
                continue
            tx = _parse_3mf_transform(item.attrib.get("transform"))
            items.append((oid, tx))
    if not items:
        # No build section (rare) — fall back to rendering every defined
        # object at its native pose. Matches what slicers like PrusaSlicer do.
        items = [(oid, _identity_4x4()) for oid in objects.keys()]

    for oid, transform in items:
        verts, tris = objects[oid]
        # Vertices are stored per-object as (V, 3); triangles index into them.
        # Validate the indices BEFORE the numpy gather — a malformed 3MF that
        # references vertex 9999 in an 8-vertex object would otherwise raise
        # IndexError, which the upload route doesn't translate to 400.
        if tris.size and (tris.min() < 0 or tris.max() >= verts.shape[0]):
            raise ValueError(
                f"3MF object {oid!r} references out-of-range vertex indices "
                f"(have {verts.shape[0]} vertices)"
            )
        # Resolve indices, optionally apply the build transform, then keep
        # only triangle vertex coords (N, 3, 3).
        coords = verts[tris]  # (N, 3, 3)
        if not _is_identity_4x4(transform):
            coords = _apply_4x4(transform, coords)
        chunks.append(coords)

    if not chunks:
        raise ValueError("3MF build list referenced no valid objects")
    all_verts = np.concatenate(chunks, axis=0)
    mn, mx = _compute_aabb(all_verts)
    return Mesh(vertices=all_verts, min_xyz=mn, max_xyz=mx)


def _find(root: ET.Element, qname: str) -> ET.Element | None:
    """ElementTree.find that tolerates both namespaced and unnamespaced trees.

    Some 3MF authoring tools omit the default namespace on the inner mesh
    nodes; we want to find them regardless.
    """
    found = root.find(qname)
    if found is not None:
        return found
    # Strip the namespace and retry as a local-name match.
    if qname.startswith("{") and "}" in qname:
        local = qname.split("}", 1)[1]
        for child in root.iter():
            if child.tag.endswith("}" + local) or child.tag == local:
                return child
    return None


def _find_named(parent: ET.Element, local_name: str) -> ET.Element | None:
    """Locate the first child whose tag ends with `local_name`, namespace-agnostic."""
    for child in parent:
        if child.tag == local_name or child.tag.endswith("}" + local_name):
            return child
    return None


def _findall(parent: ET.Element, local_name: str) -> Iterable[ET.Element]:
    for child in parent:
        if child.tag == local_name or child.tag.endswith("}" + local_name):
            yield child


def _collect_3mf_vertices(vert_node: ET.Element) -> np.ndarray:
    """Pull `<vertex x= y= z= />` children into a numpy (V, 3) float64 array."""
    rows: list[tuple[float, float, float]] = []
    for v in vert_node:
        if not (v.tag == "vertex" or v.tag.endswith("}vertex")):
            continue
        attr = v.attrib
        try:
            rows.append(
                (float(attr["x"]), float(attr["y"]), float(attr["z"]))
            )
        except (KeyError, ValueError):
            # 3MF spec requires all three; skip malformed entries rather
            # than abort the whole parse so a slightly bad file still loads.
            continue
    if not rows:
        return np.zeros((0, 3), dtype=np.float64)
    return np.array(rows, dtype=np.float64)


def _collect_3mf_triangles(tri_node: ET.Element) -> np.ndarray:
    """Pull `<triangle v1= v2= v3= />` children into a numpy (T, 3) int array."""
    rows: list[tuple[int, int, int]] = []
    for t in tri_node:
        if not (t.tag == "triangle" or t.tag.endswith("}triangle")):
            continue
        attr = t.attrib
        try:
            rows.append((int(attr["v1"]), int(attr["v2"]), int(attr["v3"])))
        except (KeyError, ValueError):
            continue
    if not rows:
        return np.zeros((0, 3), dtype=np.int64)
    return np.array(rows, dtype=np.int64)


def _identity_4x4() -> np.ndarray:
    return np.eye(4, dtype=np.float64)


def _is_identity_4x4(m: np.ndarray) -> bool:
    return bool(np.allclose(m, np.eye(4, dtype=np.float64), atol=1e-12))


def _parse_3mf_transform(raw: str | None) -> np.ndarray:
    """Parse a 3MF `transform` string ("a b c d e f g h i j k l") into 4x4.

    The spec stores 12 floats representing a 3x4 affine in column-major order
    (per the schema): the first 9 are the rotation/scale matrix columns, the
    last 3 are the translation. Build the equivalent 4x4 with [0 0 0 1] in
    the bottom row.
    """
    if not raw:
        return _identity_4x4()
    nums = []
    for tok in raw.replace(",", " ").split():
        try:
            nums.append(float(tok))
        except ValueError:
            return _identity_4x4()
    if len(nums) != 12:
        return _identity_4x4()
    m = np.eye(4, dtype=np.float64)
    # Column-major: cols 0..3 are 3-vectors; col 3 is translation.
    m[0, 0], m[1, 0], m[2, 0] = nums[0], nums[1], nums[2]
    m[0, 1], m[1, 1], m[2, 1] = nums[3], nums[4], nums[5]
    m[0, 2], m[1, 2], m[2, 2] = nums[6], nums[7], nums[8]
    m[0, 3], m[1, 3], m[2, 3] = nums[9], nums[10], nums[11]
    return m


def _apply_4x4(m: np.ndarray, coords: np.ndarray) -> np.ndarray:
    """Apply a 4x4 affine to a (..., 3) point array."""
    flat = coords.reshape(-1, 3)
    homog = np.concatenate([flat, np.ones((flat.shape[0], 1), dtype=flat.dtype)], axis=1)
    out = (homog @ m.T)[:, :3]
    return out.reshape(coords.shape)


# --- STEP ------------------------------------------------------------------


def parse_step(data: bytes) -> Mesh:
    """Parse an ISO-10303 STEP file into a Mesh.

    STEP describes B-Rep CAD geometry (NURBS surfaces, edges, faces) — not
    triangles — so loading it requires a tessellator. We support two
    optional toolchains, in priority order:

      1. ``cadquery`` (built on OpenCASCADE) — full surface tessellation.
      2. ``trimesh`` with STEP support — convenience wrapper around
         OpenCASCADE bindings.

    Neither is shipped by default because both pull in OpenCASCADE
    (~150 MB). Operators who upload STEP regularly should
    ``pip install cadquery`` (or equivalent) into the backend env; this
    function raises a clear error pointing at that fix when neither is
    available.
    """
    try:
        return _parse_step_cadquery(data)
    except _StepBackendUnavailable:
        pass
    try:
        return _parse_step_trimesh(data)
    except _StepBackendUnavailable:
        pass
    raise ValueError(
        "STEP file uploaded but no STEP-capable backend is installed. "
        "Run `pip install cadquery` or `pip install trimesh[recommend] "
        "cascadio` in the backend environment to enable STEP support."
    )


class _StepBackendUnavailable(RuntimeError):
    """Raised by an individual STEP loader when the backing library is missing."""


def _parse_step_cadquery(data: bytes) -> Mesh:
    try:
        import cadquery as cq  # type: ignore
        from cadquery import exporters  # type: ignore  # noqa: F401
    except ImportError as exc:
        raise _StepBackendUnavailable("cadquery not installed") from exc

    import tempfile
    import os

    # cadquery's STEP loader expects a file path (because it delegates to
    # OpenCASCADE's STEPControl_Reader, which reads from disk). Stash the
    # bytes in a tempfile rather than monkey-patching cadquery.
    with tempfile.NamedTemporaryFile(suffix=".step", delete=False) as fh:
        fh.write(data)
        path = fh.name
    try:
        shape = cq.importers.importStep(path)
        # `shape` is a Workplane; `val()` gives the underlying compound. We
        # tessellate that to triangles. Tolerance picked by eye — 0.1 mm
        # gives tight curves on hobbyist parts without exploding triangle
        # counts on large CAD files.
        compound = shape.val().wrapped
        from OCP.BRepMesh import BRepMesh_IncrementalMesh  # type: ignore
        from OCP.TopExp import TopExp_Explorer  # type: ignore
        from OCP.TopAbs import TopAbs_FACE  # type: ignore
        from OCP.BRep import BRep_Tool  # type: ignore
        from OCP.TopLoc import TopLoc_Location  # type: ignore

        mesher = BRepMesh_IncrementalMesh(compound, 0.1, False, 0.5, True)
        mesher.Perform()

        chunks: list[np.ndarray] = []
        explorer = TopExp_Explorer(compound, TopAbs_FACE)
        while explorer.More():
            face = explorer.Current()
            loc = TopLoc_Location()
            triangulation = BRep_Tool.Triangulation_s(face, loc)
            if triangulation is not None:
                trsf = loc.Transformation()
                n_nodes = triangulation.NbNodes()
                nodes = np.empty((n_nodes, 3), dtype=np.float64)
                for i in range(1, n_nodes + 1):
                    p = triangulation.Node(i).Transformed(trsf)
                    nodes[i - 1, 0] = p.X()
                    nodes[i - 1, 1] = p.Y()
                    nodes[i - 1, 2] = p.Z()
                n_tris = triangulation.NbTriangles()
                idx = np.empty((n_tris, 3), dtype=np.int64)
                for i in range(1, n_tris + 1):
                    t = triangulation.Triangle(i)
                    idx[i - 1, 0] = t.Value(1) - 1
                    idx[i - 1, 1] = t.Value(2) - 1
                    idx[i - 1, 2] = t.Value(3) - 1
                chunks.append(nodes[idx])
            explorer.Next()
        if not chunks:
            raise ValueError("STEP file produced no tessellated faces")
        verts = np.concatenate(chunks, axis=0)
    finally:
        os.unlink(path)
    mn, mx = _compute_aabb(verts)
    return Mesh(vertices=verts, min_xyz=mn, max_xyz=mx)


def _parse_step_trimesh(data: bytes) -> Mesh:
    try:
        import trimesh  # type: ignore
    except ImportError as exc:
        raise _StepBackendUnavailable("trimesh not installed") from exc

    try:
        loaded = trimesh.load(io.BytesIO(data), file_type="step")
    except Exception as exc:  # broad: trimesh raises various errors
        # If trimesh's STEP loader couldn't engage (no OCP/cascadio), surface
        # as an unavailable backend so the dispatcher tries the next option
        # or returns the install hint.
        msg = str(exc).lower()
        if "no loader" in msg or "step" in msg or "cascadio" in msg:
            raise _StepBackendUnavailable(str(exc)) from exc
        raise ValueError(f"trimesh failed to parse STEP: {exc}") from exc

    if isinstance(loaded, trimesh.Scene):  # type: ignore
        # Concatenate all meshes in the scene.
        if not loaded.geometry:
            raise ValueError("STEP scene contained no geometry")
        merged = trimesh.util.concatenate(
            [g for g in loaded.geometry.values() if hasattr(g, "vertices")]
        )
    else:
        merged = loaded
    if not hasattr(merged, "vertices") or not hasattr(merged, "faces"):
        raise ValueError("STEP file did not yield a triangulated mesh")
    nodes = np.asarray(merged.vertices, dtype=np.float64)
    faces = np.asarray(merged.faces, dtype=np.int64)
    verts = nodes[faces]
    mn, mx = _compute_aabb(verts)
    return Mesh(vertices=verts, min_xyz=mn, max_xyz=mx)


__all__ = ["parse_mesh", "parse_3mf", "parse_step"]
