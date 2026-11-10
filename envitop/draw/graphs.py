"""Colour palettes and small Rich/text helpers used by the widgets."""

from __future__ import annotations

from typing import Iterable, Optional, Sequence

from rich.text import Text

from .palette import Palette


def pct_bar(pct: float, width: int, palette: Palette, *, style: str = "") -> Text:
    """Smooth gradient percentage bar built from eighth blocks."""
    width = max(1, width)
    pct = max(0.0, min(100.0, pct or 0.0))
    filled = pct / 100.0 * width
    full = int(filled)
    frac = filled - full
    text = Text()
    for i in range(full):
        colour = palette.gradient((i + 1) / width * 100.0)
        text.append("█", style=f"{colour} {style}".strip())
    if full < width:
        eighths = " ▏▎▍▌▋▊▉"
        idx = int(frac * 8)
        if idx:
            colour = palette.gradient((full + frac) / width * 100.0)
            text.append(eighths[idx], style=f"{colour} {style}".strip())
            remaining = width - full - 1
        else:
            remaining = width - full
        if remaining > 0:
            text.append(" " * remaining, style=f"on {palette.faint} {style}".strip())
    return text


def tiny_bar(pct: float, width: int, palette: Palette, *, style: str = "") -> Text:
    """Compact bar, one colour for the filled part (good for table cells)."""
    width = max(1, width)
    pct = max(0.0, min(100.0, pct or 0.0))
    full = int(round(pct / 100.0 * width))
    colour = palette.gradient(pct)
    text = Text()
    text.append("█" * full, style=f"{colour} {style}".strip())
    text.append("░" * (width - full), style=f"{palette.faint} {style}".strip())
    return text


def status_glyph(status: str) -> tuple[str, str]:
    return {
        "thinking": ("◐", "yellow"),
        "executing": ("●", "green"),
        "waiting": ("○", "cyan"),
        "idle": ("·", "dim"),
        "done": ("✓", "dim"),
        "rate_limited": ("⏳", "red"),
        "unknown": ("?", "dim"),
    }.get(status, ("?", "dim"))


def braille_graph(
    values: Sequence[float],
    width: int,
    height: int,
    palette: Palette,
    *,
    maximum: Optional[float] = None,
    minimum: float = 0.0,
    empty_bg: bool = True,
    color_by: str = "value",
    solid_color: Optional[str] = None,
    invert: bool = False,
) -> list[Text]:
    """Render a filled btop-like braille area graph.

    Each character is 2 samples wide and 4 dot rows tall.  The result is a
    list of Rich Text lines (top to bottom), ready to hand to a Widget.

    Pass *solid_color* to paint every dot with one colour instead of the
    green-to-red gradient (used by the mirrored network/disk graphs).
    """
    width = max(1, int(width))
    height = max(1, int(height))
    if not values:
        values = [0.0]
    samples_wanted = width * 2
    vals = list(values)[-samples_wanted:]
    if len(vals) < samples_wanted:
        vals = [minimum] * (samples_wanted - len(vals)) + vals
    if maximum is None:
        maximum = max(vals + [minimum + 1e-9]) * 1.12
    maximum = max(maximum, minimum + 1e-9)
    span = maximum - minimum
    dots_h = height * 4

    # levels[x][cell_row] = 8 bit braille mask
    bits = [[0] * height for _ in range(width)]
    for x in range(width):
        for sub in (0, 1):
            v = vals[x * 2 + sub]
            frac = (v - minimum) / span
            level = int(round(max(0.0, min(1.0, frac)) * dots_h))
            if level <= 0:
                continue
            for d in range(level):
                cell_row = d // 4 if invert else height - 1 - (d // 4)
                # Normal graphs fill upward from the bottom baseline. Mirrored
                # graphs fill downward from the top, including Braille dot order.
                dot = d % 4 if invert else 3 - (d % 4)
                # Braille ordering: dots 1,2,3,7 on the left and 4,5,6,8
                # on the right (bits 0x01,0x02,0x04,0x40 / 0x08..0x80).
                if dot == 3:
                    bit = 0x40 if sub == 0 else 0x80
                else:
                    bit = (0x01 if sub == 0 else 0x08) << dot
                bits[x][cell_row] |= bit

    lines: list[Text] = []
    for row in range(height):
        line = Text()
        for x in range(width):
            mask = bits[x][row]
            if not mask:
                line.append(" " if empty_bg else " ", style=f"on {palette.panel}")
                continue
            avg = (vals[x * 2] + vals[x * 2 + 1]) / 2.0
            pct = (avg - minimum) / span * 100.0
            if solid_color:
                colour = solid_color
            elif color_by == "value":
                colour = palette.gradient(pct)
            else:
                colour = palette.accent
            line.append(chr(0x2800 + mask), style=colour)
        lines.append(line)
    return lines


def braille_stacked_graph(
    series: Sequence[Sequence[float]],
    width: int,
    height: int,
    palette: Palette,
    *,
    colors: Optional[Sequence[str]] = None,
    maximum: float = 100.0,
) -> list[Text]:
    """Render additive time-series layers in device-specific colors.

    Each input series contains its contribution to the shared 0–maximum scale.
    The layers stack from the baseline, while each Braille cell uses the color
    of the device contributing most of its dots.
    """
    width = max(1, int(width))
    height = max(1, int(height))
    point_count = width * 2
    layer_count = len(series)
    if not layer_count:
        return [Text(" " * width) for _ in range(height)]
    palette_colors = colors or (
        palette.green,
        palette.cyan,
        palette.yellow,
        palette.magenta,
        palette.blue,
        palette.orange,
        palette.red,
    )
    layer_colors = [palette_colors[i % len(palette_colors)] for i in range(layer_count)]
    normalized: list[list[float]] = []
    for source in series:
        values = [max(0.0, float(value)) for value in source]
        if len(values) > point_count:
            count = len(values)
            values = [
                max(
                    values[
                        int(i * count / point_count) : max(
                            int(i * count / point_count) + 1,
                            int((i + 1) * count / point_count),
                        )
                    ]
                )
                for i in range(point_count)
            ]
        elif len(values) < point_count:
            values = [0.0] * (point_count - len(values)) + values
        normalized.append(values)

    scale = max(float(maximum), 1e-9)
    dots_h = height * 4
    masks = [[0] * height for _ in range(width)]
    color_counts = [[[0] * layer_count for _ in range(height)] for _ in range(width)]
    for x in range(width):
        for sub_x in (0, 1):
            point = x * 2 + sub_x
            cumulative = 0.0
            for layer, values in enumerate(normalized):
                start = int(round(cumulative / scale * dots_h))
                cumulative += values[point]
                end = int(round(cumulative / scale * dots_h))
                for d in range(max(0, start), min(dots_h, end)):
                    cell_row = height - 1 - d // 4
                    dot_row = 3 - d % 4
                    if dot_row == 3:
                        bit = 0x40 if sub_x == 0 else 0x80
                    else:
                        bit = (0x01 if sub_x == 0 else 0x08) << dot_row
                    masks[x][cell_row] |= bit
                    color_counts[x][cell_row][layer] += 1

    lines: list[Text] = []
    for row in range(height):
        line = Text()
        for x in range(width):
            mask = masks[x][row]
            if not mask:
                line.append(" ", style=f"on {palette.panel}")
                continue
            # Prefer the uppermost occupied layer at a boundary so a small
            # contribution remains visible as its own color in the stack.
            layer = max(index for index, dots in enumerate(color_counts[x][row]) if dots)
            line.append(chr(0x2800 + mask), style=layer_colors[layer])
        lines.append(line)
    return lines


def braille_line_graph(
    values: Sequence[float],
    width: int,
    height: int,
    palette: Palette,
    *,
    maximum: Optional[float] = None,
    color_by: str = "value",
    solid_color: Optional[str] = None,
    invert: bool = False,
) -> list[Text]:
    """Render a connected Braille trace instead of a filled dot field."""
    width = max(1, int(width))
    height = max(1, int(height))
    point_count = width * 2
    vals = [float(v) for v in values]
    if not vals:
        vals = [0.0]
    if len(vals) < point_count:
        vals = [0.0] * (point_count - len(vals)) + vals
    elif len(vals) > point_count:
        count = len(vals)
        vals = [
            max(
                vals[
                    int(i * count / point_count) : max(
                        int(i * count / point_count) + 1, int((i + 1) * count / point_count)
                    )
                ]
            )
            for i in range(point_count)
        ]
    maximum = max(maximum if maximum is not None else max(vals), 1e-9)
    dot_height = height * 4
    points = []
    for x, value in enumerate(vals):
        level = int(round(max(0.0, min(1.0, value / maximum)) * (dot_height - 1)))
        points.append((x, level if invert else dot_height - 1 - level))
    cells = [[0] * height for _ in range(width)]

    def plot(x: int, y: int) -> None:
        col, row = x // 2, y // 4
        sub_x, sub_y = x % 2, y % 4
        bit = ((0x01 if sub_x == 0 else 0x08) << sub_y) if sub_y < 3 else (0x40 if sub_x == 0 else 0x80)
        cells[col][row] |= bit

    for start, end in zip(points, points[1:]):
        x0, y0 = start
        x1, y1 = end
        dx, dy = abs(x1 - x0), abs(y1 - y0)
        sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
        error = dx - dy
        while True:
            plot(x0, y0)
            if (x0, y0) == (x1, y1):
                break
            twice = 2 * error
            if twice > -dy:
                error -= dy
                x0 += sx
            if twice < dx:
                error += dx
                y0 += sy

    lines: list[Text] = []
    for row in range(height):
        line = Text()
        for col in range(width):
            mask = cells[col][row]
            if not mask:
                line.append(" ")
                continue
            if solid_color:
                color = solid_color
            elif color_by == "value":
                value = max(vals[col * 2], vals[col * 2 + 1])
                color = palette.gradient(value / maximum * 100)
            else:
                color = palette.accent
            line.append(chr(0x2800 + mask), style=color)
        lines.append(line)
    return lines


def smooth_history(values: Iterable[float], length: int) -> list[float]:
    vals = list(values)
    if len(vals) > length:
        vals = vals[-length:]
    if len(vals) < length:
        vals = [0.0] * (length - len(vals)) + vals
    return vals
