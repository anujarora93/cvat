
# Copyright (C) 2019 Intel Corporation
#
# SPDX-License-Identifier: MIT

from itertools import groupby
import numpy as np

from datumaro.util.image import lazy_image, load_image


def generate_colormap(length=256):
    def get_bit(number, index):
        return (number >> index) & 1

    colormap = np.zeros((length, 3), dtype=int)
    indices = np.arange(length, dtype=int)

    for j in range(7, -1, -1):
        for c in range(3):
            colormap[:, c] |= get_bit(indices, c) << j
        indices >>= 3

    return {
        id: tuple(color) for id, color in enumerate(colormap)
    }

def invert_colormap(colormap):
    return {
        tuple(a): index for index, a in colormap.items()
    }

_default_colormap = generate_colormap()
_default_unpaint_colormap = invert_colormap(_default_colormap)

def _default_unpaint_colormap_fn(r, g, b):
    return _default_unpaint_colormap[(r, g, b)]

def unpaint_mask(painted_mask, colormap=None):
    # expect HWC BGR [0; 255] image
    # expect RGB->index colormap
    assert len(painted_mask.shape) == 3
    if colormap is None:
        colormap = _default_unpaint_colormap_fn
    if callable(colormap):
        map_fn = lambda a: colormap(int(a[2]), int(a[1]), int(a[0]))
    else:
        map_fn = lambda a: colormap[(int(a[2]), int(a[1]), int(a[0]))]

    unpainted_mask = np.apply_along_axis(map_fn,
        1, np.reshape(painted_mask, (-1, 3)))
    unpainted_mask = np.reshape(unpainted_mask, (painted_mask.shape[:2]))
    return unpainted_mask.astype(int)


def apply_colormap(mask, colormap=None):
    # expect HW [0; max_index] mask
    # expect index->RGB colormap
    assert len(mask.shape) == 2

    if colormap is None:
        colormap = _default_colormap
    if callable(colormap):
        map_fn = lambda p: colormap(int(p[0]))[::-1]
    else:
        map_fn = lambda p: colormap[int(p[0])][::-1]
    painted_mask = np.apply_along_axis(map_fn, 1, np.reshape(mask, (-1, 1)))

    painted_mask = np.reshape(painted_mask, (*mask.shape, 3))
    return painted_mask.astype(np.float32)


def load_mask(path, colormap=None):
    mask = load_image(path)
    if colormap is not None:
        if len(mask.shape) == 3 and mask.shape[2] != 1:
            mask = unpaint_mask(mask, colormap=colormap)
    return mask

def lazy_mask(path, colormap=None):
    return lazy_image(path, lambda path: load_mask(path, colormap))


def convert_mask_to_rle(binary_mask):
    counts = []
    for i, (value, elements) in enumerate(
            groupby(binary_mask.ravel(order='F'))):
        # decoding starts from 0
        if i == 0 and value == 1:
            counts.append(0)
        counts.append(len(list(elements)))

    return {
        'counts': counts,
        'size': list(binary_mask.shape)
    }

def convert_mask_to_polygons(mask, tolerance=1.0, area_threshold=1):
    """
    Convert an instance mask to polygons

    Args:
        mask: a 2d binary mask
        tolerance: maximum distance from original points of
            a polygon to the approximated ones
        area_threshold: minimal area of generated polygons

    Returns:
        A list of polygons like [[x1,y1, x2,y2 ...], [...]]
    """
    from pycocotools import mask as mask_utils
    from skimage import measure

    polygons = []

    # pad mask with 0 around borders
    padded_mask = np.pad(mask, pad_width=1, mode='constant', constant_values=0)
    contours = measure.find_contours(padded_mask, 0.5)
    # Fix coordinates after padding
    contours = np.subtract(contours, 1)

    for contour in contours:
        if not np.array_equal(contour[0], contour[-1]):
            contour = np.vstack((contour, contour[0])) # make polygon closed

        contour = measure.approximate_polygon(contour, tolerance)
        if len(contour) < 2:
            continue

        contour = np.flip(contour, axis=1).flatten().clip(0) # [x0, y0, ...]

        # Check if the polygon is big enough
        rle = mask_utils.frPyObjects([contour], mask.shape[0], mask.shape[1])
        area = sum(mask_utils.area(rle))
        if area_threshold < area:
            polygons.append(contour)
    return polygons

def crop_covered_polygons(polygons, height, width,
        threshold=0.0, ratio_tolerance=0.001, area_threshold=1):
    """
    Find all contours occluded by others and crop them to the visible part only.
    Input polygons are expected to be sorted from background to foreground.

    Args:
        polygons: 2d array of contours
        height: height of the image
        width: width of the image
        threshold: IoU threshold for two objects to be counted as intersected
            By default is set to 0 and processes any two intersected objects
        ratio_tolerance: an IoU "handicap" value for a situation
            when an object is (almost) fully covered by another one and we
            don't want make a "hole" in the background object
        area_threshold: minimal area of included polygons

    Returns:
        A list of input polygons' segments (in the same order as input):
            [
                [[x1,y1, x2,y2 ...], ...], # input polygon #0 segments
                [[x1,y1, x2,y2 ...], ...], # input polygon #1 segments
                ...
            ]
    """
    from pycocotools import mask as mask_utils

    input_rles = mask_utils.frPyObjects(polygons, height, width)

    polygons = [[p] for p in polygons]

    for i, rle_bottom in enumerate(input_rles):
        area_bottom = sum(mask_utils.area(rle_bottom))
        if area_bottom < area_threshold:
            polygons[i] = []
            continue

        rles_top = []
        for j in range(i + 1, len(input_rles)):
            rle_top = input_rles[j]
            iou = sum(mask_utils.iou(rle_bottom, rle_top, [0, 0]))[0]

            if iou < threshold:
                continue

            area_top = sum(mask_utils.area(rle_top))
            area_ratio = area_top / area_bottom

            # If a segment is fully inside another one, skip this segment
            if abs(area_ratio - iou) < ratio_tolerance:
                continue

            # Check if the bottom segment is fully covered by the top one.
            # There is a mistake in the annotation, keep the background one
            if abs(1 / area_ratio - iou) < ratio_tolerance:
                rles_top = []
                break

            rles_top.append(rle_top)
        if not rles_top:
            continue
        rle_top = mask_utils.merge(rles_top)

        bottom_mask = mask_utils.decode(rle_bottom).astype(np.uint8)
        top_mask = mask_utils.decode(rle_top).astype(np.uint8)

        bottom_mask -= top_mask
        bottom_mask[bottom_mask != 1] = 0
        polygons[i] = convert_mask_to_polygons(bottom_mask,
            area_threshold=area_threshold)

    return polygons