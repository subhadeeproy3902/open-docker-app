"""A small QR code generator (byte mode, error correction level M, versions 1–10: up to 213 bytes, plenty for links).
Pure Python so Berth needs no extra package. Follows ISO/IEC 18004; output is checked with a real decoder in the tests.

    matrix = encode("http://192.168.1.20:8787/s/ab12")   # list of rows of bools (True = dark)
    png_bytes(matrix, scale=8)                             # a PNG via cairo (for Gtk.Picture / Gdk.Texture)"""

# version -> (EC codewords per block, [(number of blocks, data codewords per block), …]) for level M
_BLOCKS = {1: (10, [(1, 16)]), 2: (16, [(1, 28)]), 3: (26, [(1, 44)]), 4: (18, [(2, 32)]), 5: (24, [(2, 43)]),
           6: (16, [(4, 27)]), 7: (18, [(4, 31)]), 8: (22, [(2, 38), (2, 39)]), 9: (22, [(3, 36), (2, 37)]),
           10: (26, [(4, 43), (1, 44)])}
_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42],
          9: [6, 26, 46], 10: [6, 28, 50]}

# GF(256) with the QR polynomial x^8 + x^4 + x^3 + x^2 + 1
_EXP, _LOG = [0] * 512, [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _mul(a, b):
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _rs_ecc(data, n):
    gen = [1]
    for i in range(n):                       # generator polynomial (x - a^0)…(x - a^(n-1))
        gen = [c ^ _mul(g, _EXP[i]) for c, g in zip(gen + [0], [0] + gen)]
    rem = [0] * n
    for d in data:
        f = d ^ rem[0]
        rem = rem[1:] + [0]
        for j in range(n):
            rem[j] ^= _mul(gen[j + 1], f)
    return rem


def _codewords(data, version):
    ec, groups = _BLOCKS[version]
    capacity = sum(n * k for n, k in groups)
    bits = []

    def put(val, length):
        bits.extend((val >> (length - 1 - i)) & 1 for i in range(length))
    put(0b0100, 4)                                       # byte mode
    put(len(data), 8 if version < 10 else 16)
    for b in data:
        put(b, 8)
    put(0, min(4, capacity * 8 - len(bits)))             # terminator
    bits += [0] * (-len(bits) % 8)
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    n_data = len(words)
    while len(words) < capacity:                          # pad bytes alternate 0xEC, 0x11
        words.append(0xEC if (len(words) - n_data) % 2 == 0 else 0x11)
    blocks, i = [], 0
    for n, k in groups:
        for _ in range(n):
            blocks.append(words[i:i + k])
            i += k
    eccs = [_rs_ecc(b, ec) for b in blocks]
    out = []
    for j in range(max(len(b) for b in blocks)):         # interleave data, then EC
        out += [b[j] for b in blocks if j < len(b)]
    for j in range(ec):
        out += [e[j] for e in eccs]
    return out


def _bch(value, poly, total):
    deg = poly.bit_length() - 1
    v = value << deg
    while v.bit_length() > deg:
        v ^= poly << (v.bit_length() - poly.bit_length())
    return (value << deg) | v


_MASKS = [lambda r, c: (r + c) % 2 == 0, lambda r, c: r % 2 == 0, lambda r, c: c % 3 == 0,
          lambda r, c: (r + c) % 3 == 0, lambda r, c: (r // 2 + c // 3) % 2 == 0,
          lambda r, c: (r * c) % 2 + (r * c) % 3 == 0, lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
          lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0]


def _build(version, words, mask):
    size = 17 + 4 * version
    m = [[None] * size for _ in range(size)]

    def finder(r0, c0):
        for r in range(-1, 8):
            for c in range(-1, 8):
                rr, cc = r0 + r, c0 + c
                if 0 <= rr < size and 0 <= cc < size:
                    inside = 0 <= r <= 6 and 0 <= c <= 6
                    m[rr][cc] = inside and (r in (0, 6) or c in (0, 6) or (2 <= r <= 4 and 2 <= c <= 4))
    finder(0, 0)
    finder(0, size - 7)
    finder(size - 7, 0)
    for i in range(8, size - 8):                          # timing patterns
        m[6][i] = m[i][6] = i % 2 == 0
    pos = _ALIGN[version]
    corners = {(pos[0], pos[0]), (pos[0], pos[-1]), (pos[-1], pos[0])} if pos else set()
    for r in pos:
        for c in pos:
            if (r, c) in corners:                         # those would overlap the finder patterns
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    m[r + dr][c + dc] = max(abs(dr), abs(dc)) != 1
    m[size - 8][8] = True                                 # dark module
    reserved = [(8, i) for i in range(9)] + [(i, 8) for i in range(8)] + \
               [(8, size - 1 - i) for i in range(8)] + [(size - 1 - i, 8) for i in range(7)]
    for r, c in reserved:
        if m[r][c] is None:
            m[r][c] = False
    if version >= 7:
        for i in range(18):
            m[size - 11 + i % 3][i // 3] = m[i // 3][size - 11 + i % 3] = False
    func = [[v is not None for v in row] for row in m]
    bits = [(w >> (7 - i)) & 1 for w in words for i in range(8)]
    i, up = 0, True
    c = size - 1
    while c > 0:
        if c == 6:
            c -= 1
        for k in range(size):
            r = size - 1 - k if up else k
            for cc in (c, c - 1):
                if not func[r][cc]:
                    bit = bits[i] if i < len(bits) else 0
                    i += 1
                    m[r][cc] = bool(bit) ^ _MASKS[mask](r, cc)
        up = not up
        c -= 2
    fmt = _bch((0b00 << 3) | mask, 0b10100110111, 15) ^ 0b101010000010010   # level M = 00
    fbits = [(fmt >> i) & 1 for i in range(15)]
    for i in range(6):
        m[i][8] = bool(fbits[i])
    m[7][8], m[8][8], m[8][7] = bool(fbits[6]), bool(fbits[7]), bool(fbits[8])
    for i in range(9, 15):
        m[8][14 - i] = bool(fbits[i])
    for i in range(8):
        m[8][size - 1 - i] = bool(fbits[i])
    for i in range(8, 15):
        m[size - 15 + i][8] = bool(fbits[i])
    if version >= 7:
        v = _bch(version, 0b1111100100101, 18)
        for i in range(18):
            b = bool((v >> i) & 1)
            m[size - 11 + i % 3][i // 3] = m[i // 3][size - 11 + i % 3] = b
    return m


def _penalty(m):
    size, p = len(m), 0
    for grid in (m, [list(col) for col in zip(*m)]):
        for row in grid:
            run, prev = 0, None
            for v in row:
                if v == prev:
                    run += 1
                else:
                    if run >= 5:
                        p += run - 2
                    run, prev = 1, v
            if run >= 5:
                p += run - 2
            s = "".join("1" if v else "0" for v in row)
            p += 40 * (s.count("10111010000") + s.count("00001011101"))
    for r in range(size - 1):
        for c in range(size - 1):
            if m[r][c] == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                p += 3
    dark = sum(v for row in m for v in row)
    p += abs(dark * 20 - size * size * 10) // (size * size) * 10
    return p


def encode(text):
    data = text.encode("utf-8")
    for version in range(1, 11):
        cap = sum(n * k for n, k in _BLOCKS[version][1])
        if len(data) + (2 if version < 10 else 3) <= cap:
            break
    else:
        raise ValueError("Too long for a QR code here (max ~210 characters)")
    words = _codewords(data, version)
    return min((_build(version, words, mk) for mk in range(8)), key=_penalty)


def png_bytes(matrix, scale=8, border=4, dark=(0, 0, 0), light=(1, 1, 1)):
    import io
    import cairo
    n = len(matrix) + 2 * border
    surf = cairo.ImageSurface(cairo.FORMAT_RGB24, n * scale, n * scale)
    cr = cairo.Context(surf)
    cr.set_source_rgb(*light)
    cr.paint()
    cr.set_source_rgb(*dark)
    for r, row in enumerate(matrix):
        for c, v in enumerate(row):
            if v:
                cr.rectangle((c + border) * scale, (r + border) * scale, scale, scale)
    cr.fill()
    buf = io.BytesIO()
    surf.write_to_png(buf)
    return buf.getvalue()
