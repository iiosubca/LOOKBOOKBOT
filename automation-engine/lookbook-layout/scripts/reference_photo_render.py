"""Crop visible photographs from a rendered PDF, preserving PDF colour rules."""
from __future__ import annotations

from pathlib import Path

from PIL import Image

from pdf_page_geometry import page_bounds, transformed_bounds


def crop_rendered_photo(page, matrix: tuple[float, ...], rendered: Image.Image) -> Image.Image:
    page_left, page_bottom, page_right, page_top = page_bounds(page)
    left, bottom, right, top = transformed_bounds(matrix)
    left, right = max(left, page_left), min(right, page_right)
    bottom, top = max(bottom, page_bottom), min(top, page_top)
    if left >= right or bottom >= top:
        raise ValueError("Reference photograph is completely outside the visible page.")
    width, height = page_right - page_left, page_top - page_bottom
    points = [((x - page_left) / width, (page_top - y) / height)
              for x in (left, right) for y in (bottom, top)]
    rotation = int(getattr(page, "rotation", 0) or 0) % 360
    if rotation == 90:
        points = [(1 - y, x) for x, y in points]
    elif rotation == 180:
        points = [(1 - x, 1 - y) for x, y in points]
    elif rotation == 270:
        points = [(y, 1 - x) for x, y in points]
    box = (round(min(x for x, _ in points) * rendered.width),
           round(min(y for _, y in points) * rendered.height),
           round(max(x for x, _ in points) * rendered.width),
           round(max(y for _, y in points) * rendered.height))
    return rendered.crop(box).convert("RGB")


def rendered_photo_pair(page, rendered_path: Path, matrices: list[tuple[float, ...]]) -> tuple[Image.Image, Image.Image]:
    if len(matrices) != 2:
        raise ValueError(f"Reference page needs two visible photographs; found {len(matrices)}.")
    with Image.open(rendered_path) as raw:
        return tuple(crop_rendered_photo(page, matrix, raw) for matrix in matrices)
