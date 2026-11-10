#!/usr/bin/env python3
"""Capture every EnviTop tab as a continuous, 24 fps simulated demo."""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FPS = 24
FRAMES_PER_TAB = 48
SIZE = (160, 50)
PNG_SIZE = (960, 600)
TABS = (
    ("overview", "Overview"),
    ("cpu", "CPU"),
    ("gpu", "GPU"),
    ("net", "Net"),
    ("memory", "Memory"),
    ("procs", "Procs"),
    ("agents", "Agents"),
)


async def capture_frames(directory: Path) -> None:
    import cairosvg

    from envitop.app import EnviTop
    from envitop.demo import DemoSource

    app = EnviTop(scope="all", demo=True)
    frame_number = 0
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.2)
        source = DemoSource(scope="all", step_seconds=1 / FPS, smooth=True)
        source.paused = True
        app.source = source
        app.timeline.clear()

        for view, _label in TABS:
            app._set_view(view)
            await pilot.pause(0.03)
            for _ in range(FRAMES_PER_TAB):
                source.paused = False
                app.refresh_ui()
                source.paused = True
                await pilot.pause(0.001)
                svg = app.export_screenshot(simplify=True)
                cairosvg.svg2png(
                    bytestring=svg.encode("utf-8"),
                    write_to=str(directory / f"frame_{frame_number:04d}.png"),
                    output_width=PNG_SIZE[0],
                    output_height=PNG_SIZE[1],
                )
                frame_number += 1


def build_gif(output: Path) -> None:
    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg is required to assemble the GIF")
    os.environ.pop("NO_COLOR", None)
    os.environ["TERM"] = "xterm-256color"
    os.environ["COLORTERM"] = "truecolor"
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="envitop-demo-") as tmp:
        frames = Path(tmp)
        asyncio.run(capture_frames(frames))
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-framerate",
                str(FPS),
                "-i",
                str(frames / "frame_%04d.png"),
                "-filter_complex",
                "split[s0][s1];[s0]palettegen=max_colors=128:stats_mode=full[p];"
                "[s1][p]paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle",
                "-loop",
                "0",
                str(output),
            ],
            check=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "docs" / "envitop-tabs.gif",
        help="output GIF path (default: docs/envitop-tabs.gif)",
    )
    args = parser.parse_args()
    build_gif(args.output.resolve())
    print(f"Wrote {len(TABS) * FRAMES_PER_TAB} frames at {FPS} fps to {args.output}")


if __name__ == "__main__":
    main()
