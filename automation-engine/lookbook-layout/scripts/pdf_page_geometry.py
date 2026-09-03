"""Small PDF page-geometry helpers shared by the reference registry builder."""

from __future__ import annotations


OCCLUSION_TOLERANCE_PT = 1.0
OCCLUSION_COVERAGE_THRESHOLD = 0.98


def page_bounds(page) -> tuple[float, float, float, float]:
    """Return the page rectangle in PDF points as left, bottom, right, top."""
    box = getattr(page, "cropbox", None) or getattr(page, "mediabox")
    return float(box.left), float(box.bottom), float(box.right), float(box.top)


def transformed_bounds(matrix: tuple[float, float, float, float, float, float]) -> tuple[float, float, float, float]:
    """Return the axis-aligned bounds of an image's transformed unit square."""
    a, b, c, d, e, f = matrix
    points = (
        (e, f),
        (a + e, b + f),
        (c + e, d + f),
        (a + c + e, b + d + f),
    )
    return (
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    )


def _intersection(
    left: float,
    bottom: float,
    right: float,
    top: float,
    page: tuple[float, float, float, float],
) -> tuple[float, float, float, float] | None:
    page_left, page_bottom, page_right, page_top = page
    result = (
        max(left, page_left),
        max(bottom, page_bottom),
        min(right, page_right),
        min(top, page_top),
    )
    return result if result[2] - result[0] > 1e-6 and result[3] - result[1] > 1e-6 else None


def _rectangle_coverage(
    target: tuple[float, float, float, float],
    occluders: list[tuple[float, float, float, float]],
) -> float:
    """Return the target-area coverage ratio for axis-aligned rectangles."""
    left, bottom, right, top = target
    target_area = (right - left) * (top - bottom)
    if target_area <= 1e-6:
        return 0.0
    x_edges = {left, right}
    for o_left, _o_bottom, o_right, _o_top in occluders:
        if o_right > left + 1e-6 and o_left < right - 1e-6:
            x_edges.add(max(left, o_left))
            x_edges.add(min(right, o_right))
    ordered_edges = sorted(x_edges)
    covered_area = 0.0
    for x_left, x_right in zip(ordered_edges, ordered_edges[1:]):
        if x_right - x_left <= 1e-6:
            continue
        x_mid = (x_left + x_right) / 2.0
        y_intervals = sorted(
            (o_bottom, o_top)
            for o_left, o_bottom, o_right, o_top in occluders
            if o_left < x_mid < o_right
        )
        merged: list[list[float]] = []
        for o_bottom, o_top in y_intervals:
            clipped_bottom = max(bottom, o_bottom)
            clipped_top = min(top, o_top)
            if clipped_top <= clipped_bottom + 1e-6:
                continue
            if not merged or clipped_bottom > merged[-1][1] + 1e-6:
                merged.append([clipped_bottom, clipped_top])
            else:
                merged[-1][1] = max(merged[-1][1], clipped_top)
        covered_area += (x_right - x_left) * sum(item[1] - item[0] for item in merged)
    return min(1.0, covered_area / target_area)


def draw_fully_covered_by_later(
    page,
    matrix: tuple[float, float, float, float, float, float],
    later_matrices: list[tuple[float, float, float, float, float, float]],
) -> bool:
    """Whether a page-visible image is fully painted over by later image draws.

    InDesign can retain an older image in the PDF content stream and paint the
    replacement on top of it.  Such an image is inside the page but is not a
    visible photograph.  The exact occlusion check is used for the
    axis-aligned transforms produced by the lookbook template.  For rotated or
    skewed transforms it stays conservative and keeps the candidate rather than
    risking the loss of a genuinely visible image.
    """
    a, b, c, d, _e, _f = matrix
    if abs(b) > 1e-6 or abs(c) > 1e-6:
        return False
    page_rect = page_bounds(page)
    target = _intersection(*transformed_bounds(matrix), page_rect)
    if target is None:
        return False
    occluders: list[tuple[float, float, float, float]] = []
    for later in later_matrices:
        later_a, later_b, later_c, later_d, _later_e, _later_f = later
        if abs(later_b) > 1e-6 or abs(later_c) > 1e-6:
            return False
        later_left, later_bottom, later_right, later_top = transformed_bounds(later)
        # A PDF export can round an otherwise identical replacement by a few
        # hundredths of a point, leaving an invisible hairline at its edge.
        # Absorb that export noise, but keep genuinely visible partial strips.
        tolerance = OCCLUSION_TOLERANCE_PT
        box = _intersection(
            later_left - tolerance,
            later_bottom - tolerance,
            later_right + tolerance,
            later_top + tolerance,
            page_rect,
        )
        if box is not None:
            occluders.append(box)
    return bool(occluders) and _rectangle_coverage(target, occluders) >= OCCLUSION_COVERAGE_THRESHOLD


def draw_intersects_page(page, matrix: tuple[float, float, float, float, float, float]) -> bool:
    """Whether an image transformed by a PDF CTM has positive overlap with the page.

    PDF generators can leave old or duplicate image placements in the content
    stream even after those placements were moved completely off the page.  The
    placement is a candidate only when its transformed unit-image quadrilateral
    intersects the page rectangle.  Checking all four corners also handles
    negative scales and rotated/skewed placements; partially clipped images are
    intentionally retained.
    """
    min_x, min_y, max_x, max_y = transformed_bounds(matrix)
    page_left, page_bottom, page_right, page_top = page_bounds(page)
    overlap_x = min(max_x, page_right) - max(min_x, page_left)
    overlap_y = min(max_y, page_top) - max(min_y, page_bottom)
    return overlap_x > 1e-6 and overlap_y > 1e-6
