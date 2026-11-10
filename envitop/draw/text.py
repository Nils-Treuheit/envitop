"""Colour palettes and small Rich/text helpers used by the widgets."""

from __future__ import annotations

from typing import Optional, Sequence

from rich.cells import cell_len


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
            max(vals[int(i * count / width) : max(int(i * count / width) + 1, int((i + 1) * count / width))])
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
        sum(vals[max(0, i - radius) : min(len(vals), i + radius + 1)])
        / len(vals[max(0, i - radius) : min(len(vals), i + radius + 1)])
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
