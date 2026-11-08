"""Build every Berth icon from the one logo artwork (data/icons/berth-logo.png, 2000x2000).

    python3 tools/make_icons.py

Writes, next to the artwork:
  berth-512.png, berth-1024.png          app tile (README, About, tray, Linux launchers)
  io.github.subhadeeproy3902.Berth.svg   the same tile for the hicolor theme (embeds the 512 px PNG)
  hicolor/<size>/apps/<app id>.png      32-512 px PNGs for the icon theme
  berth.ico                              Windows: 16-256 px, PNG-compressed entries
  berth-macos-1024.png                   macOS: Apple's 824 px tile inside a 1024 canvas (CI turns it into .icns)
  io.github.subhadeeproy3902.Berth-symbolic.svg   one-colour outline for symbolic contexts (from berth-logo-trace.svg)
Needs only pycairo (already a Berth dependency)."""
import base64, io, math, os, re, struct
import cairo

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "icons")
SRC = os.path.join(HERE, "berth-logo.png")
APPID = "io.github.subhadeeproy3902.Berth"


def content_box(img):
    """Bounding box of everything that is not the flat background colour."""
    w, h, st, d = img.get_width(), img.get_height(), img.get_stride(), img.get_data()
    bg = d[0:3]
    x0, y0, x1, y1 = w, h, 0, 0
    for y in range(0, h, 4):
        row = y * st
        for x in range(0, w, 4):
            i = row + x * 4
            if abs(d[i] - bg[0]) + abs(d[i + 1] - bg[1]) + abs(d[i + 2] - bg[2]) > 60:
                x0, y0, x1, y1 = min(x0, x), min(y0, y), max(x1, x), max(y1, y)
    return x0, y0, x1, y1, (bg[2] / 255, bg[1] / 255, bg[0] / 255)


def squircle(cr, x, y, size, n=5.0, steps=256):
    """Superellipse |x|^n + |y|^n = 1 — the soft 'app tile' outline used by macOS and GNOME."""
    r = size / 2
    cx, cy = x + r, y + r
    for k in range(steps + 1):
        t = 2 * math.pi * k / steps
        c, s = math.cos(t), math.sin(t)
        px = cx + r * math.copysign(abs(c) ** (2 / n), c)
        py = cy + r * math.copysign(abs(s) ** (2 / n), s)
        cr.line_to(px, py) if k else cr.move_to(px, py)
    cr.close_path()


def tile(src, box, canvas, tile_size, art=0.80):
    """The artwork centred on a squircle tile of its own background colour."""
    x0, y0, x1, y1, bg = box
    out = cairo.ImageSurface(cairo.FORMAT_ARGB32, canvas, canvas)
    cr = cairo.Context(out)
    off = (canvas - tile_size) / 2
    squircle(cr, off, off, tile_size)
    cr.set_source_rgb(*bg)
    cr.fill_preserve()
    cr.clip()
    scale = art * tile_size / max(x1 - x0, y1 - y0)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    cr.translate(canvas / 2, canvas / 2)
    cr.scale(scale, scale)
    cr.translate(-cx, -cy)
    cr.set_source_surface(src, 0, 0)
    cr.get_source().set_filter(cairo.FILTER_BEST)
    cr.paint()
    cr.identity_matrix()                             # faint rim so the dark tile still reads on dark backgrounds
    squircle(cr, off, off, tile_size)
    cr.set_source_rgba(1, 1, 1, 0.16)
    cr.set_line_width(max(2.0, tile_size * 0.014))   # half of it falls outside the clip, so it stays inside the tile
    cr.stroke()
    return out


def scaled(surf, size):
    out = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(out)
    cr.scale(size / surf.get_width(), size / surf.get_height())
    cr.set_source_surface(surf, 0, 0)
    cr.get_source().set_filter(cairo.FILTER_BEST)
    cr.paint()
    return out


def png_bytes(surf):
    b = io.BytesIO()
    surf.write_to_png(b)
    return b.getvalue()


def write_ico(path, master, sizes=(16, 24, 32, 48, 64, 128, 256)):
    blobs = [png_bytes(scaled(master, s)) for s in sizes]
    head = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries = b""
    for s, blob in zip(sizes, blobs):
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
    with open(path, "wb") as f:
        f.write(head + entries + b"".join(blobs))


def symbolic(trace_path, out_path):
    """One-colour version: the traced shapes (containers, hull, wave) filled with the symbolic foreground."""
    svg = open(trace_path).read()
    paths = re.findall(r'<path[^>]*\bd="([^"]+)"', svg)
    pts = [tuple(map(float, p)) for d in paths for p in re.findall(r"(-?\d+(?:\.\d+)?)[ ,](-?\d+(?:\.\d+)?)", d)]
    x0, x1 = min(p[0] for p in pts), max(p[0] for p in pts)
    y0, y1 = min(p[1] for p in pts), max(p[1] for p in pts)
    side = max(x1 - x0, y1 - y0) * 1.04
    vx, vy = (x0 + x1 - side) / 2, (y0 + y1 - side) / 2
    body = "\n".join(f'  <path fill="#2e3436" fill-rule="evenodd" d="{d}"/>' for d in paths)
    with open(out_path, "w") as f:
        f.write(f'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" '
                f'viewBox="{vx:.0f} {vy:.0f} {side:.0f} {side:.0f}">\n{body}\n</svg>\n')


def main():
    src = cairo.ImageSurface.create_from_png(SRC)
    box = content_box(src)
    master = tile(src, box, 1024, 928)               # hicolor guideline: a little air around the tile
    master.write_to_png(os.path.join(HERE, "berth-1024.png"))
    scaled(master, 512).write_to_png(os.path.join(HERE, "berth-512.png"))
    b64 = base64.b64encode(png_bytes(scaled(master, 512))).decode()
    with open(os.path.join(HERE, f"{APPID}.svg"), "w") as f:
        f.write('<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
                'width="128" height="128" viewBox="0 0 512 512">\n  <title>Berth</title>\n'
                f'  <image width="512" height="512" xlink:href="data:image/png;base64,{b64}"/>\n</svg>\n')
    for s in (32, 48, 64, 128, 256, 512):            # plain PNGs: every launcher (GTK, Qt, rofi…) reads these
        d = os.path.join(HERE, "hicolor", f"{s}x{s}", "apps")
        os.makedirs(d, exist_ok=True)
        scaled(master, s).write_to_png(os.path.join(d, f"{APPID}.png"))
    write_ico(os.path.join(HERE, "berth.ico"), tile(src, box, 1024, 1000))   # Windows icons fill their square
    tile(src, box, 1024, 824).write_to_png(os.path.join(HERE, "berth-macos-1024.png"))  # Apple's icon grid
    symbolic(os.path.join(HERE, "berth-logo-trace.svg"), os.path.join(HERE, f"{APPID}-symbolic.svg"))
    print("icons written to", HERE)


main()
