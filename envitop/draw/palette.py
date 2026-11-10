"""Colour palettes and small Rich/text helpers used by the widgets."""

from __future__ import annotations

from dataclasses import dataclass


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
