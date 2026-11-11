"""Bağımsız QR kod üretici (bayt modu, hata düzeltme M, sürüm 1-13). Harici kütüphane gerektirmez.

Sunucu tarafında SVG üretir; böylece QR afiş/broşür internet bağlantısı ya da CDN olmadan da basılır.
"""

# Sürüm -> (blok başına EC kod sözcüğü, [(blok sayısı, blok başına veri kod sözcüğü), ...])  — seviye M
_BLOCKS_M = {
    1: (10, [(1, 16)]), 2: (16, [(1, 28)]), 3: (26, [(1, 44)]), 4: (18, [(2, 32)]),
    5: (24, [(2, 43)]), 6: (16, [(4, 27)]), 7: (18, [(4, 31)]), 8: (22, [(2, 38), (2, 39)]),
    9: (22, [(3, 36), (2, 37)]), 10: (26, [(4, 43), (1, 44)]), 11: (30, [(1, 50), (4, 51)]),
    12: (22, [(6, 36), (2, 37)]), 13: (22, [(8, 37), (1, 38)]),
}
_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42],
          9: [6, 26, 46], 10: [6, 28, 50], 11: [6, 30, 54], 12: [6, 32, 58], 13: [6, 34, 62]}
_REMAINDER = {1: 0, 2: 7, 3: 7, 4: 7, 5: 7, 6: 7, 7: 0, 8: 0, 9: 0, 10: 0, 11: 0, 12: 0, 13: 0}


def _gf_mul(x, y):
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z & 0xFF


def _rs_divisor(degree):
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data, divisor):
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _data_capacity(v):
    ec, groups = _BLOCKS_M[v]
    return sum(n * d for n, d in groups)


def _encode_codewords(data: bytes):
    for v in range(1, 14):
        count_bits = 8 if v < 10 else 16
        need_bits = 4 + count_bits + 8 * len(data)
        cap = _data_capacity(v) * 8
        if need_bits <= cap:
            break
    else:
        raise ValueError("Metin QR için çok uzun")
    bits = []

    def put(val, n):
        bits.extend((val >> i) & 1 for i in range(n - 1, -1, -1))

    put(0b0100, 4)
    put(len(data), count_bits)
    for b in data:
        put(b, 8)
    put(0, min(4, cap - len(bits)))
    put(0, (-len(bits)) % 8)
    pad = [0xEC, 0x11]
    i = 0
    while len(bits) < cap:
        put(pad[i % 2], 8)
        i += 1
    codewords = [int("".join(map(str, bits[k:k + 8])), 2) for k in range(0, len(bits), 8)]

    ec_len, groups = _BLOCKS_M[v]
    divisor = _rs_divisor(ec_len)
    blocks, pos = [], 0
    for n, d in groups:
        for _ in range(n):
            blk = codewords[pos:pos + d]
            pos += d
            blocks.append((blk, _rs_remainder(blk, divisor)))
    out = []
    for i in range(max(len(b[0]) for b in blocks)):
        for blk, _ in blocks:
            if i < len(blk):
                out.append(blk[i])
    for i in range(ec_len):
        for _, ecw in blocks:
            out.append(ecw[i])
    return v, out


def _mask_bit(mask, x, y):
    return [
        (x + y) % 2 == 0, y % 2 == 0, x % 3 == 0, (x + y) % 3 == 0,
        (x // 3 + y // 2) % 2 == 0, x * y % 2 + x * y % 3 == 0,
        (x * y % 2 + x * y % 3) % 2 == 0, ((x + y) % 2 + x * y % 3) % 2 == 0,
    ][mask]


def _build(v, codewords, mask):
    size = 17 + 4 * v
    m = [[False] * size for _ in range(size)]
    fn = [[False] * size for _ in range(size)]

    def setf(x, y, dark):
        m[y][x] = dark
        fn[y][x] = True

    for i in range(size):
        setf(6, i, i % 2 == 0)
        setf(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if 0 <= x < size and 0 <= y < size:
                    setf(x, y, max(abs(dx), abs(dy)) not in (2, 4))
    al = _ALIGN[v]
    n = len(al)
    for i in range(n):
        for j in range(n):
            if (i == 0 and j == 0) or (i == 0 and j == n - 1) or (i == n - 1 and j == 0):
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    setf(al[i] + dx, al[j] + dy, max(abs(dx), abs(dy)) != 1)
    _format(setf, size, mask)          # yer tutucu (alanı işaretler)
    if v >= 7:
        rem = v
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        bits = v << 12 | rem
        for i in range(18):
            bit = (bits >> i) & 1 == 1
            a, b = size - 11 + i % 3, i // 3
            setf(a, b, bit)
            setf(b, a, bit)

    i, total = 0, len(codewords) * 8
    right = size - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(size):
            for j in range(2):
                x = right - j
                upward = ((right + 1) & 2) == 0
                y = size - 1 - vert if upward else vert
                if not fn[y][x] and i < total:
                    m[y][x] = (codewords[i >> 3] >> (7 - (i & 7))) & 1 == 1
                    i += 1
        right -= 2
    for y in range(size):
        for x in range(size):
            if not fn[y][x] and _mask_bit(mask, x, y):
                m[y][x] = not m[y][x]
    _format(setf, size, mask)
    return m


def _format(setf, size, mask):
    data = (0 << 3) | mask            # M seviyesi = 00
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    bits = ((data << 10) | rem) ^ 0x5412
    b = lambda i: (bits >> i) & 1 == 1
    for i in range(6):
        setf(8, i, b(i))
    setf(8, 7, b(6))
    setf(8, 8, b(7))
    setf(7, 8, b(8))
    for i in range(9, 15):
        setf(14 - i, 8, b(i))
    for i in range(8):
        setf(size - 1 - i, 8, b(i))
    for i in range(8, 15):
        setf(8, size - 15 + i, b(i))
    setf(8, size - 8, True)


def _penalty(m):
    """ISO 18004 maske cezası: 1) 5+ aynı renk dizisi 2) 2x2 bloklar 3) bulucuya benzeyen desen 4) renk dengesi."""
    size = len(m)
    score = 0
    pat_a = [True, False, True, True, True, False, True, False, False, False, False]
    pat_b = pat_a[::-1]
    for lines in (m, [list(col) for col in zip(*m)]):
        for row in lines:
            run, prev = 0, None
            for c in row:
                if c == prev:
                    run += 1
                else:
                    if run >= 5:
                        score += run - 2
                    run, prev = 1, c
            if run >= 5:
                score += run - 2
            for i in range(size - 10):
                seg = row[i:i + 11]
                if seg == pat_a or seg == pat_b:
                    score += 40
    for y in range(size - 1):
        for x in range(size - 1):
            c = m[y][x]
            if c == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                score += 3
    dark = sum(sum(r) for r in m)
    total = size * size
    k = (abs(dark * 20 - total * 10) + total - 1) // total - 1
    score += max(k, 0) * 10
    return score


def matrix(text: str):
    v, cw = _encode_codewords(text.encode("utf-8"))
    best = min((_build(v, cw, k) for k in range(8)), key=_penalty)
    return best


def svg(text: str, border=2, color="#000") -> str:
    m = matrix(text)
    size = len(m) + border * 2
    parts = []
    for y, row in enumerate(m):
        for x, dark in enumerate(row):
            if dark:
                parts.append(f"M{x + border},{y + border}h1v1h-1z")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" shape-rendering="crispEdges" '
            f'role="img" aria-label="QR kod"><rect width="100%" height="100%" fill="#fff"/>'
            f'<path d="{"".join(parts)}" fill="{color}"/></svg>')
