# Animated tab demo

Generate the README animation from the actual Textual app and its synthetic
data source:

```bash
python3 -m pip install -r demo/requirements.txt
python3 demo/generate_demo.py
```

The generator captures 48 frames for each of the seven tabs at 24 fps, then
assembles a 14-second looping GIF at `docs/envitop-tabs.gif`. One smooth
simulation continues across tab changes so the graphs advance from frame to
frame.

The generator needs `ffmpeg` on `PATH`. Use `--output PATH` to write the GIF
elsewhere.
