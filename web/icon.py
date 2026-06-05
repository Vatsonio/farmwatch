"""farmwatch app icon. Pillow only (no GUI deps) so it can also build the .ico
used as the exe icon at build time."""

from PIL import Image, ImageDraw


def make_tray_image(size: int = 64) -> Image.Image:
    """A brass filament spool seen head-on that doubles as a watching eye/target.
    A solid brass flange ring surrounds a dark moat and a floating brass hub.
    Drawn at 4x and LANCZOS-downsampled so the silhouette stays crisp at 16x16."""
    ss = 4
    S = size * ss
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    brass = (217, 154, 78, 255)
    brass_lt = (232, 173, 99, 255)
    field = (12, 14, 18, 255)
    c = S / 2.0

    def circle(cx, cy, r, fill=None, outline=None, width=1):
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fill,
                  outline=outline, width=int(round(width)))

    R = S * 0.46
    ring_w = S * 0.135
    hub_r = S * 0.135

    circle(c, c, R, fill=brass)
    circle(c, c, R - ring_w, fill=field)
    circle(c, c, hub_r, fill=brass)

    hw = ring_w * 0.5
    arc_r = R - ring_w * 0.5
    d.arc([c - arc_r, c - arc_r, c + arc_r, c + arc_r],
          start=200, end=315, fill=brass_lt, width=int(round(hw)))

    cl_r = hub_r * 0.42
    cl_off = hub_r * 0.30
    circle(c - cl_off, c - cl_off, cl_r, fill=brass_lt)

    return img.resize((size, size), Image.LANCZOS)


def make_ico(path: str = "farmwatch.ico"):
    """Render a multi-size Windows .ico for the exe icon."""
    base = make_tray_image(256)
    base.save(path, format="ICO",
              sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return path


if __name__ == "__main__":
    print("wrote", make_ico())
