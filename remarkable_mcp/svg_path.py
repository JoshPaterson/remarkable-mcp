"""Parse SVG path data and scale it, for marks drawn from paths.

Only path data is handled (``M L H V C S Q T A Z``, absolute and relative);
the result is path data again, with every coordinate mapped by an axis-aligned
scale and offset. Scaling the coordinates instead of the drawing keeps line
widths exact when a path is stretched to fill a region.
"""

import re
from typing import List, Tuple

# Number of arguments each command takes per repetition.
ARGS = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2, "A": 7, "Z": 0}
_NUMBER = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_FLAG = re.compile(r"[01]")
_SEPARATOR = re.compile(r"[\s,]*")


class PathError(ValueError):
    """Path data that cannot be parsed."""


def parse(data: str) -> List[Tuple[str, List[float]]]:
    """``[(command, args), ...]`` with implicit repetitions made explicit.

    Arc flags may be written without separators (``a5 5 0 1010 10``), as SVG
    allows.
    """
    commands: List[Tuple[str, List[float]]] = []
    pos, n = 0, len(data)
    command = None

    def skip(p: int) -> int:
        return _SEPARATOR.match(data, p).end()

    pos = skip(pos)
    while pos < n:
        char = data[pos]
        if char.upper() in ARGS:
            command = char
            pos = skip(pos + 1)
        elif command is None:
            raise PathError("Path data must start with a command such as M.")
        count = ARGS[command.upper()]
        if count == 0:
            commands.append((command, []))
            command = None if pos >= n or data[pos].upper() in ARGS else command
            if command is not None:
                raise PathError("Z takes no numbers.")
            continue
        args: List[float] = []
        for index in range(count):
            pattern = _FLAG if command.upper() == "A" and index in (3, 4) else _NUMBER
            match = pattern.match(data, pos)
            if not match:
                raise PathError(f"Expected a number after {command!r} in the path data.")
            args.append(float(match.group()))
            pos = skip(match.end())
        commands.append((command, args))
        # Further numbers repeat the command; after M they mean L.
        if command == "M":
            command = "L"
        elif command == "m":
            command = "l"
    return commands


def transform(
    commands: List[Tuple[str, List[float]]],
    scale_x: float,
    scale_y: float,
    offset_x: float,
    offset_y: float,
) -> str:
    """Path data with absolute points mapped to ``(x * sx + ox, y * sy + oy)``.

    Relative coordinates are scaled only. Arc radii scale with their axes;
    an arc rotated by other than a multiple of 90 degrees is only exact when
    both scales are equal.
    """
    parts: List[str] = []
    for command, args in commands:
        upper, relative = command.upper(), command.islower()
        ox, oy = (0.0, 0.0) if relative else (offset_x, offset_y)
        if upper == "Z":
            out: List[float] = []
        elif upper == "H":
            out = [args[0] * scale_x + ox]
        elif upper == "V":
            out = [args[0] * scale_y + oy]
        elif upper == "A":
            rx, ry, rotation, large, sweep, ax, ay = args
            if rotation % 180 == 90:
                rx, ry = rx * scale_y, ry * scale_x
            else:
                rx, ry = rx * scale_x, ry * scale_y
            if scale_x * scale_y < 0:
                sweep = 1 - sweep
            out = [abs(rx), abs(ry), rotation, large, sweep, ax * scale_x + ox, ay * scale_y + oy]
        else:
            out = [
                value * (scale_x if i % 2 == 0 else scale_y) + (ox if i % 2 == 0 else oy)
                for i, value in enumerate(args)
            ]
        parts.append(command + " ".join(_number(v) for v in out))
    return " ".join(parts)


def _number(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text
