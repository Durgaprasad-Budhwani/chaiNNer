# From https://github.com/victorca25/iNNfer/blob/main/utils/utils.py
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple, Union
import re
import os

import numpy as np
from sanic.log import logger

NUMBERS = re.compile(r"(\d+)")

ALPHABET = [*"ABCDEFGHIJKLMNOPQRSTUVWXYZ"]


def round_half_up(number: Union[float, int]) -> int:
    """
    Python's `round` method implements round-half-to-even rounding which is very unintuitive.
    This function implements round-half-up rounding.

    Round half up is consistent with JavaScript's `Math.round`.

    https://en.wikipedia.org/wiki/Rounding#Rounding_to_the_nearest_integer
    """
    return int(number + 0.5)


def get_h_w_c(image: np.ndarray) -> Tuple[int, int, int]:
    """Returns the height, width, and number of channels."""
    h, w = image.shape[:2]
    c = 1 if image.ndim == 2 else image.shape[2]
    return h, w, c


def resize_to_side_conditional(
    w: int, h: int, target: int, side: str, condition: str
) -> Tuple[int, int]:
    def compare_conditions(b: int) -> bool:
        if condition == "both":
            return False
        if condition == "downscale":
            return target > b
        elif condition == "upscale":
            return target < b
        else:
            raise RuntimeError(f"Unknown condition {condition}")

    if side == "width":
        if compare_conditions(w):
            w_new = w
            h_new = h
        else:
            w_new = target
            h_new = max(round_half_up((target / w) * h), 1)

    elif side == "height":
        if compare_conditions(h):
            w_new = w
            h_new = h
        else:
            w_new = max(round_half_up((target / h) * w), 1)
            h_new = target

    elif side == "shorter side":
        if compare_conditions(min(h, w)):
            w_new = w
            h_new = h
        else:
            w_new = max(round_half_up((target / min(h, w)) * w), 1)
            h_new = max(round_half_up((target / min(h, w)) * h), 1)

    elif side == "longer side":
        if compare_conditions(max(h, w)):
            w_new = w
            h_new = h
        else:
            w_new = max(round_half_up((target / max(h, w)) * w), 1)
            h_new = max(round_half_up((target / max(h, w)) * h), 1)

    else:
        raise RuntimeError(f"Unknown side selection {side}")

    return w_new, h_new


def alphanumeric_sort(value: str) -> List[Union[str, int]]:
    """Key function to sort strings containing numbers by proper
    numerical order."""

    lcase_value = value.upper()
    parts = NUMBERS.split(lcase_value)
    parts[1::2] = map(int, parts[1::2])
    return parts  # type: ignore


def walk_error_handler(exception_instance):
    logger.warning(
        f"Exception occurred during walk: {exception_instance} Continuing..."
    )


def list_all_files_sorted(
    directory: str, ext_filter: Union[List[str], None] = None
) -> List[str]:
    just_files: List[str] = []
    for root, dirs, files in os.walk(
        directory, topdown=True, onerror=walk_error_handler
    ):
        dirs.sort(key=alphanumeric_sort)
        for name in sorted(files, key=alphanumeric_sort):
            filepath = os.path.join(root, name)
            _base, ext = os.path.splitext(filepath)
            if ext_filter is None or ext.lower() in ext_filter:
                just_files.append(filepath)
    return just_files


@dataclass(frozen=True)
class Padding:
    top: int
    right: int
    bottom: int
    left: int

    @staticmethod
    def all(value: int) -> "Padding":
        return Padding(value, value, value, value)

    @staticmethod
    def to(value: Padding | int) -> Padding:
        if isinstance(value, int):
            return Padding.all(value)
        return value

    @property
    def horizontal(self) -> int:
        return self.left + self.right

    @property
    def vertical(self) -> int:
        return self.top + self.bottom

    @property
    def empty(self) -> bool:
        return self.top == 0 and self.right == 0 and self.bottom == 0 and self.left == 0

    def scale(self, factor: int) -> Padding:
        return Padding(
            self.top * factor,
            self.right * factor,
            self.bottom * factor,
            self.left * factor,
        )

    def min(self, other: Padding | int) -> Padding:
        other = Padding.to(other)
        return Padding(
            min(self.top, other.top),
            min(self.right, other.right),
            min(self.bottom, other.bottom),
            min(self.left, other.left),
        )

    def remove_from(self, image: np.ndarray) -> np.ndarray:
        h, w, _ = get_h_w_c(image)

        return image[
            self.top : (h - self.bottom),
            self.left : (w - self.right),
            ...,
        ]


@dataclass(frozen=True)
class Region:
    x: int
    y: int
    width: int
    height: int

    @property
    def size(self) -> Tuple[int, int]:
        return self.width, self.height

    def scale(self, factor: int) -> Region:
        return Region(
            self.x * factor,
            self.y * factor,
            self.width * factor,
            self.height * factor,
        )

    def intersect(self, other: Region) -> Region:
        x = max(self.x, other.x)
        y = max(self.y, other.y)
        width = min(self.x + self.width, other.x + other.width) - x
        height = min(self.y + self.height, other.y + other.height) - y
        return Region(x, y, width, height)

    def add_padding(self, pad: Padding) -> Region:
        return Region(
            x=self.x - pad.left,
            y=self.y - pad.top,
            width=self.width + pad.horizontal,
            height=self.height + pad.vertical,
        )

    def remove_padding(self, pad: Padding) -> Region:
        return self.add_padding(pad.scale(-1))

    def child_padding(self, child: Region) -> Padding:
        """
        Returns the padding `p` such that `child.add_padding(p) == self`.
        """
        left = child.x - self.x
        top = child.y - self.y
        right = self.width - child.width - left
        bottom = self.height - child.height - top
        return Padding(top, right, bottom, left)

    def read_from(self, image: np.ndarray) -> np.ndarray:
        h, w, _ = get_h_w_c(image)
        if (w, h) == self.size:
            return image

        return image[
            self.y : (self.y + self.height),
            self.x : (self.x + self.width),
            ...,
        ]

    def write_into(self, lhs: np.ndarray, rhs: np.ndarray):
        h, w, _ = get_h_w_c(rhs)
        assert (w, h) == self.size

        lhs[
            self.y : (self.y + self.height),
            self.x : (self.x + self.width),
            ...,
        ] = rhs
