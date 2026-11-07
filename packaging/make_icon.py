"""Draws the station's icon: a geometric hummingbird with a blue cap on a dark
green rounded square. Original artwork.

    python packaging/make_icon.py /tmp/icon   # needs Pillow

then copy cicg-estacion.png to src/feeder_station/static/icon.png,
favicon.png to src/feeder_station/static/favicon.png and cicg-estacion.ico
to packaging/cicg-estacion.ico."""
import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw

S = 2048  # supersampled canvas, downsampled at the end
BG = (18, 66, 61, 255)
BIRD = (236, 244, 239, 255)
WING = (190, 222, 208, 255)
CAP = (64, 140, 255, 255)
EYE = (18, 66, 61, 255)


def rot(points, angle_deg, cx, cy):
    a = math.radians(angle_deg)
    ca, sa = math.cos(a), math.sin(a)
    return [(cx + (x - cx) * ca - (y - cy) * sa, cy + (x - cx) * sa + (y - cy) * ca) for x, y in points]


def ellipse_poly(cx, cy, rx, ry, angle_deg, n=96):
    pts = [(cx + rx * math.cos(2 * math.pi * i / n), cy + ry * math.sin(2 * math.pi * i / n)) for i in range(n)]
    return rot(pts, angle_deg, cx, cy)


def draw(size_out: int) -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = S * 0.04
    d.rounded_rectangle([pad, pad, S - pad, S - pad], radius=S * 0.22, fill=BG)

    # Tail: a narrow fan pointing down-left.
    d.polygon([(880, 1220), (520, 1600), (680, 1600), (690, 1780), (990, 1330)], fill=BIRD)
    # Body: a tilted teardrop-ish ellipse.
    d.polygon(ellipse_poly(1060, 1120, 330, 190, -38), fill=BIRD)
    # Wing: a long blade raised up and back.
    d.polygon([(990, 1110), (690, 430), (850, 410), (1190, 1060)], fill=WING)
    # Head.
    hx, hy, hr = 1290, 860, 170
    d.ellipse([hx - hr, hy - hr, hx + hr, hy + hr], fill=BIRD)
    # Blue cap: the upper part of the head.
    d.pieslice([hx - hr, hy - hr, hx + hr, hy + hr], start=190, end=350, fill=CAP)
    # Beak: long, thin, slightly down-pointing.
    d.polygon([(1430, 820), (1840, 700), (1450, 890)], fill=BIRD)
    # Eye.
    er = 34
    d.ellipse([hx + 40 - er, hy - 10 - er, hx + 40 + er, hy - 10 + er], fill=EYE)

    return img.resize((size_out, size_out), Image.Resampling.LANCZOS)


out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
big = draw(1024)
big.resize((512, 512), Image.Resampling.LANCZOS).save(out / "cicg-estacion-512.png")
big.resize((256, 256), Image.Resampling.LANCZOS).save(out / "cicg-estacion.png")
big.resize((64, 64), Image.Resampling.LANCZOS).save(out / "favicon.png")
big.save(out / "cicg-estacion.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("ok")
