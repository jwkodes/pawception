"""Compact binary footprints that can be projected onto any heatmap grid."""

import base64
from functools import lru_cache
from io import BytesIO
import math

from PIL import Image


def footprint(mask):
    box = mask.getbbox()
    if box is None:
        return None
    binary = mask.crop(box).point(lambda value: 255 if value else 0).convert('1')
    buffer = BytesIO()
    binary.save(buffer, format='PNG')
    return {'size':list(mask.size), 'box':list(box),
            'png':base64.b64encode(buffer.getvalue()).decode('ascii')}


def occupied_cells(region, columns, rows):
    return _occupied_cells(region['png'], tuple(region['size']), tuple(region['box']), columns, rows)


@lru_cache(maxsize=5000)
def _occupied_cells(png, size, box, columns, rows):
    """A square is occupied if it overlaps at least one foreground pixel."""
    width, height = size
    left, top, right, bottom = box
    if not 0 <= left < right <= width or not 0 <= top < bottom <= height:
        raise ValueError('Invalid footprint bounds')
    with Image.open(BytesIO(base64.b64decode(png, validate=True))) as saved:
        if saved.size != (right-left, bottom-top):
            raise ValueError('Footprint mask does not match its bounds')
        mask = saved.convert('1')
    cells = []
    for row in range(top*rows//height, math.ceil(bottom*rows/height)):
        for column in range(left*columns//width, math.ceil(right*columns/width)):
            crop = (max(left, column*width//columns)-left,
                    max(top, row*height//rows)-top,
                    min(right, math.ceil((column+1)*width/columns))-left,
                    min(bottom, math.ceil((row+1)*height/rows))-top)
            if mask.crop(crop).getbbox():
                cells.append(row*columns+column)
    return tuple(cells)
