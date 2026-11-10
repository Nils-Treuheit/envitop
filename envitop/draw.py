"""Colour palettes and small Rich/text helpers used by the widgets."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from rich.cells import cell_len
from rich.text import Text


@dataclass(frozen=True)
class Palette:
    name: str
    bg: str
    panel: str
    border: str
    border_focus: str
    text: str
    dim: str
    faint: str
    accent: str
    green: str
    cyan: str
    blue: str
    yellow: str
    orange: str
    red: str
    magenta: str
    selected_bg: str
    selected_fg: str = "#e6edf3"
    graph: tuple[str, str, str, str] = ("#5ee08a", "#3fd0c9", "#f5d76e", "#ff6b6b")

    def gradient(self, pct: float) -> str:
        """Return a colour for a 0..100 percentage (green -> red)."""
        stops = self.graph
        p = max(0.0, min(100.0, pct)) / 100.0
        if p <= 0.5:
            a, b, t = stops[0], stops[1], p / 0.5
        elif p <= 0.8:
            a, b, t = stops[1], stops[2], (p - 0.5) / 0.3
        else:
            a, b, t = stops[2], stops[3], (p - 0.8) / 0.2
        return blend(a, b, t)


PALETTES: dict[str, Palette] = {
    "btop": Palette(
        name="btop",
        bg="#070b10",
        panel="#0b1219",
        border="#26414f",
        border_focus="#3fd0c9",
        text="#d6e2ea",
        dim="#7d93a3",
        faint="#41525f",
        accent="#3fd0c9",
        green="#5ee08a",
        cyan="#3fd0c9",
        blue="#58a6ff",
        yellow="#f5d76e",
        orange="#ffa657",
        red="#ff6b6b",
        magenta="#c792ea",
        selected_bg="#15303b",
        graph=("#5ee08a", "#3fd0c9", "#f5d76e", "#ff6b6b"),
    ),
    "dracula": Palette(
        name="dracula",
        bg="#0d0b18",
        panel="#141124",
        border="#3b3360",
        border_focus="#bd93f9",
        text="#f8f8f2",
        dim="#8f8aa8",
        faint="#4d4766",
        accent="#bd93f9",
        green="#50fa7b",
        cyan="#8be9fd",
        blue="#8be9fd",
        yellow="#f1fa8c",
        orange="#ffb86c",
        red="#ff5555",
        magenta="#ff79c6",
        selected_bg="#2c2348",
        graph=("#50fa7b", "#8be9fd", "#f1fa8c", "#ff5555"),
    ),
    "gruvbox": Palette(
        name="gruvbox",
        bg="#0f0e0b",
        panel="#17150f",
        border="#4d4435",
        border_focus="#fabd2f",
        text="#ebdbb2",
        dim="#a89984",
        faint="#5b5244",
        accent="#fabd2f",
        green="#b8bb26",
        cyan="#8ec07c",
        blue="#83a598",
        yellow="#fabd2f",
        orange="#fe8019",
        red="#fb4934",
        magenta="#d3869b",
        selected_bg="#322f22",
        graph=("#b8bb26", "#8ec07c", "#fabd2f", "#fb4934"),
    ),
    "nord": Palette(
        name="nord",
        bg="#0b0f14",
        panel="#111820",
        border="#33445a",
        border_focus="#88c0d0",
        text="#eceff4",
        dim="#8f9aa8",
        faint="#46556a",
        accent="#88c0d0",
        green="#a3be8c",
        cyan="#88c0d0",
        blue="#81a1c1",
        yellow="#ebcb8b",
        orange="#d08770",
        red="#bf616a",
        magenta="#b48ead",
        selected_bg="#22303f",
        graph=("#a3be8c", "#88c0d0", "#ebcb8b", "#bf616a"),
    ),
    # btop's Adapta palette (theme file: adapta.theme). Its main background
    # is intentionally terminal-default; use the app's dark panel surface.
    "adapta": Palette(
        name="adapta",
        bg="#070b10",
        panel="#0b1219",
        border="#00bcd4",
        border_focus="#00bcd4",
        text="#cfd8dc",
        dim="#809098",
        faint="#505050",
        accent="#90a4ae",
        green="#00bcd4",
        cyan="#00bcd4",
        blue="#00b0ff",
        yellow="#d4d400",
        orange="#ff8040",
        red="#ff0040",
        magenta="#991199",
        selected_bg="#bb0040",
        selected_fg="#ffffff",
        graph=("#00bcd4", "#d4d400", "#ff0040", "#ff0040"),
    ),
}

PALETTE_ORDER = list(PALETTES)
_current = PALETTES["btop"]


def get_palette() -> Palette:
    return _current


def set_palette(name: str) -> Palette:
    global _current
    _current = PALETTES.get(name, PALETTES["btop"])
    return _current


def cycle_palette(delta: int = 1) -> Palette:
    idx = PALETTE_ORDER.index(_current.name) if _current.name in PALETTE_ORDER else 0
    return set_palette(PALETTE_ORDER[(idx + delta) % len(PALETTE_ORDER)])


# ---------------------------------------------------------------------------
# Colour + text helpers
# ---------------------------------------------------------------------------


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def blend(a: str, b: str, t: float) -> str:
    t = max(0.0, min(1.0, t))
    ar, ag, ab = _hex_to_rgb(a)
    br, bg, bb = _hex_to_rgb(b)
    return "#{:02x}{:02x}{:02x}".format(
        round(ar + (br - ar) * t),
        round(ag + (bg - ag) * t),
        round(ab + (bb - ab) * t),
    )


def fit(text: str, width: int, pad: bool = True) -> str:
    """Crop/pad plain text to exactly *width* terminal cells."""
    if width <= 0:
        return ""
    out = text
    if cell_len(out) > width:
        out = out[:width]
        while out and cell_len(out) > width:
            out = out[:-1]
    if pad and cell_len(out) < width:
        out += " " * (width - cell_len(out))
    return out


def clip(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if cell_len(text) <= width:
        return text
    if width <= 1:
        return "…"
    out = text
    while out and cell_len(out) + 1 > width:
        out = out[:-1]
    return out + "…"


def truncate_middle(text: str, width: int) -> str:
    if cell_len(text) <= width or width < 5:
        return clip(text, width)
    left = (width - 1) // 2
    right = width - 1 - left
    return text[:left] + "…" + text[-right:] if right else clip(text, width)


def human_bytes(n: float, precise: bool = False) -> str:
    n = float(n or 0)
    neg = n < 0
    n = abs(n)
    units = ["B", "K", "M", "G", "T", "P"]
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024.0
        i += 1
    if i == 0:
        s = f"{int(n)}{units[i]}"
    elif precise and i <= 2:
        s = f"{n:.2f}{units[i]}"
    elif n >= 100:
        s = f"{n:.0f}{units[i]}"
    elif n >= 10:
        s = f"{n:.1f}{units[i]}"
    else:
        s = f"{n:.2f}{units[i]}"
    return ("-" if neg else "") + s


def human_rate(n: float) -> str:
    return human_bytes(n, precise=False) + "/s"


def sparkline(values: Sequence[float], width: int = 4) -> str:
    """Return a compact, connected Unicode trace for a short history."""
    width = max(1, int(width))
    vals = [max(0.0, float(v)) for v in values]
    if not vals:
        vals = [0.0]
    if len(vals) < width:
        vals = [0.0] * (width - len(vals)) + vals
    elif len(vals) > width:
        count = len(vals)
        vals = [
            max(vals[int(i * count / width): max(int(i * count / width) + 1,
                                                  int((i + 1) * count / width))])
            for i in range(width)
        ]
    peak = max(vals) or 1.0
    glyphs = "▁▂▃▄▅▆▇█"
    return "".join(glyphs[min(7, int(value / peak * 7))] for value in vals)


def smooth_series(values: Sequence[float], window: int = 3) -> list[float]:
    """Average adjacent samples for display so isolated peaks do not float."""
    vals = [float(value) for value in values]
    if len(vals) < 3 or window <= 1:
        return vals
    radius = max(1, window // 2)
    return [
        sum(vals[max(0, i - radius): min(len(vals), i + radius + 1)])
        / len(vals[max(0, i - radius): min(len(vals), i + radius + 1)])
        for i in range(len(vals))
    ]


def human_tokens(n: float) -> str:
    n = float(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return f"{int(n)}"


def human_duration(seconds: float, short: bool = False) -> str:
    seconds = max(0, int(seconds or 0))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h" if short else f"{days}d {hours:02d}h {mins:02d}m"
    if hours:
        return f"{hours}h {mins:02d}m" if short else f"{hours:02d}h {mins:02d}m {secs:02d}s"
    if mins:
        return f"{mins}m {secs:02d}s" if short else f"{mins:02d}m {secs:02d}s"
    return f"{secs}s"


def human_ago(ts: float, now: Optional[float] = None) -> str:
    import time

    if not ts:
        return "-"
    delta = max(0, int((now or time.time()) - ts))
    if delta < 5:
        return "now"
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


# ---------------------------------------------------------------------------
# Bars and braille graphs
# ---------------------------------------------------------------------------


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
    palette_colors = colors or (palette.green, palette.cyan, palette.yellow, palette.magenta,
                                palette.blue, palette.orange, palette.red)
    layer_colors = [palette_colors[i % len(palette_colors)] for i in range(layer_count)]
    normalized: list[list[float]] = []
    for source in series:
        values = [max(0.0, float(value)) for value in source]
        if len(values) > point_count:
            count = len(values)
            values = [
                max(values[int(i * count / point_count):max(
                    int(i * count / point_count) + 1,
                    int((i + 1) * count / point_count),
                )])
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
            max(vals[int(i * count / point_count): max(int(i * count / point_count) + 1,
                                                         int((i + 1) * count / point_count))])
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
