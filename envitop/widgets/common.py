"""Custom btop-flavoured widgets (all render with Rich/Textual)."""

from __future__ import annotations

import shlex
from typing import Optional, Sequence

from rich.cells import cell_len
from rich.text import Text

from ..draw import (
    blend,
    pct_bar,
)


def _line(parts: Sequence[tuple[str, str]], width: int, base: str = "") -> Text:
    text = Text(style=base)
    for value, style in parts:
        if value:
            text.append(value, style=style or None)
    plain_width = cell_len(text.plain)
    if plain_width < width:
        text.append(" " * (width - plain_width))
    text.truncate(width, overflow="crop")
    return text


def _join(lines: Sequence[Text], width: int, height: int) -> Text:
    out = Text()
    for i in range(height):
        if i:
            out.append("\n")
        if i < len(lines):
            out.append_text(lines[i])
        else:
            out.append(" " * width)
    return out


def _pad_text(text: Text, width: int) -> Text:
    if text.cell_len < width:
        text.append(" " * (width - text.cell_len))
    text.truncate(width, overflow="crop")
    return text


def _bar_text(pct: Optional[float], width: int, palette, selected: bool = False) -> Text:
    style = "on " + palette.selected_bg if selected else ""
    if pct is None:
        return Text("—" * width, style=palette.faint)
    return pct_bar(pct, width, palette, style=style)


def _context_bar(
    pct: Optional[float],
    width: int,
    palette,
    *,
    selected: bool = False,
    fill_pct: Optional[float] = None,
) -> Text:
    """Render a full-width meter with a green-to-red usage color."""
    width = max(0, width)
    text = Text()
    background = f"on {palette.selected_bg}" if selected else ""
    if not width:
        return text
    if pct is None:
        text.append("·" * width, style=f"{background} {palette.faint}".strip())
        return text
    pct = max(0.0, min(100.0, pct))
    stops = ("#5ee08a", "#a3e635", "#f5d76e", "#ffa657", "#ff6b6b")
    position = pct / 100 * (len(stops) - 1)
    index = min(len(stops) - 2, int(position))
    color = blend(stops[index], stops[index + 1], position - index)
    filled = max(0.0, min(100.0, pct if fill_pct is None else fill_pct)) / 100 * width
    full = min(width, int(filled))
    text.append("█" * full, style=f"{background} {color}".strip())
    remainder = width - full
    fraction = int((filled - full) * 8)
    partial = " ▏▎▍▌▋▊▉"
    if remainder and fraction:
        text.append(partial[fraction], style=f"{background} {color}".strip())
        remainder -= 1
    if remainder:
        text.append("░" * remainder, style=f"{background} {palette.faint}".strip())
    return text


def _process_usage_color(pct: Optional[float], palette) -> str:
    """Use green below 30%, warning colors from 30–49%, and red at 50%."""
    if pct is None:
        return palette.faint
    pct = max(0.0, min(100.0, pct))
    if pct >= 50:
        return palette.red
    if pct >= 30:
        return blend(palette.yellow, palette.orange, (pct - 30) / 20)
    return palette.green


def _gpu_memory(value: int) -> str:
    """Format framebuffer memory with nvitop-like precision."""
    gib = 1024**3
    mib = 1024**2
    if value >= gib:
        return f"{value / gib:.2f}GiB"
    if value >= mib:
        return f"{value / mib:.1f}MiB"
    return f"{value / 1024:.1f}KiB" if value >= 1024 else f"{value}B"


def _llama_model(command: str) -> str:
    try:
        args = shlex.split(command)
    except ValueError:
        args = command.split()
    model = ""
    for index, arg in enumerate(args):
        if arg == "--model" and index + 1 < len(args):
            model = args[index + 1]
            break
        if arg.startswith("--model="):
            model = arg.partition("=")[2]
            break
    if not model:
        return "?"
    model = model.removeprefix("hf://")
    marker = "huggingface.co/"
    if marker in model:
        model = model.split(marker, 1)[1]
        components = model.split("/")
        if len(components) >= 2:
            model = "/".join(components[:2])
    for component in model.replace("\\", "/").split("/"):
        if component.startswith("models--"):
            repo = component.removeprefix("models--")
            organization, separator, name = repo.partition("--")
            if separator and organization and name:
                return f"{organization}/{name}"
    model = model.rstrip("/")
    if "/" in model and (model.startswith("/") or model.startswith(".")):
        model = model.rsplit("/", 1)[-1]
    for extension in (".gguf", ".safetensors", ".bin"):
        if model.endswith(extension):
            model = model[: -len(extension)]
            break
    return model or "?"


def _gpu_process_details(proc, active_projects: Sequence[str]) -> str:
    command = proc.cmd or proc.name
    lower = f"{proc.name} {command}".lower()
    if "llama-server" in lower or "llama_server" in lower:
        return f"M:{_llama_model(command)}"
    ai_app = any(
        name in lower
        for name in ("chatgpt", "claude", "codex", "opencode", "aider", "cursor", "copilot", "gemini")
    )
    if not ai_app:
        return "-"
    projects = list(dict.fromkeys(project.strip() for project in active_projects if project.strip()))
    return "P:[" + ",".join(projects) + "]" if projects else "-"
