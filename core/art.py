"""把卡片画成一张好看的图（Pillow 直接绘制，不依赖文转图服务）。

设计稿的思路：深色底 + 圆角面板 + 一个主色（橙）+ 彩色 emoji 图标。
`render_card(spec)` 只认一种「卡片描述」，各业务卡片在 core/cards.py 里拼描述，
这样改版式只要动这一个文件。
"""
from __future__ import annotations

import math
import os
import random
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ------------------------------------------------------------------ 配色
BG = "#0f1116"
PANEL = "#191c24"
PANEL_ALT = "#1f232d"
PANEL_SOFT = "#232834"
LINE = "#2a303c"
INK = "#e9ecf1"
MUTED = "#8b93a3"
DIM = "#5f6877"

ACCENT = "#ff8b3d"       # 主色：外卖橙
ACCENT_SOFT = "#3a2517"
BLUE = "#5aa9ff"
GREEN = "#3ddc84"
RED = "#ff6b6b"
YELLOW = "#ffcc55"
PURPLE = "#b98cff"

TONE = {
    "accent": ACCENT,
    "good": GREEN,
    "bad": RED,
    "warn": YELLOW,
    "info": BLUE,
    "muted": MUTED,
    "purple": PURPLE,
}

WIDTH = 900
PAD = 34
GAP = 16
RADIUS = 22

# ------------------------------------------------------------------ 字体
# 先找插件自带的（data/fonts 里放字体就优先用），再找系统字体。
# AstrBot 大多数装在 Linux 上，那边没有 msyh.ttc，所以要把常见的
# Noto CJK / 文泉驿 / 思源 / 苹方 都试一遍，找不到再退回默认位图字体。
_HERE = Path(__file__).resolve().parent
_DIRS = [
    _HERE.parent / "data" / "fonts",
    Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts",
    Path("/usr/share/fonts"),
    Path("/usr/local/share/fonts"),
    Path.home() / ".fonts",
    Path.home() / ".local/share/fonts",
    Path("/System/Library/Fonts"),
    Path("/Library/Fonts"),
]
_FONT_CACHE: dict[tuple[int, bool], ImageFont.FreeTypeFont] = {}
_EMOJI_CACHE: dict[int, ImageFont.FreeTypeFont] = {}
_SCANNED: list[Path] = []


def _font_files() -> list[Path]:
    if _SCANNED:
        return _SCANNED
    for d in _DIRS:
        try:
            if d.is_dir():
                _SCANNED.extend(p for p in d.rglob("*") if p.suffix.lower() in (".ttf", ".ttc", ".otf"))
        except OSError:
            continue
    return _SCANNED


def _pick_font(names: list[str], keys: tuple[str, ...] = ()) -> str | None:
    """先按文件名精确找，再按关键字模糊找（跨平台用）。"""
    files = _font_files()
    for name in names:
        for d in _DIRS:
            p = d / name
            try:
                if p.exists():
                    return str(p)
            except OSError:
                continue
    for key in keys:
        for p in files:
            if key.lower() in p.name.lower():
                return str(p)
    return None


_CJK = _pick_font(
    ["msyh.ttc", "Deng.ttf", "simhei.ttf", "simsun.ttc", "NotoSansCJK-Regular.ttc", "wqy-microhei.ttc"],
    ("msyh", "NotoSansCJK", "NotoSansSC", "NotoSansHans", "wqy", "SourceHanSans", "PingFang", "Deng"),
)
_CJK_BOLD = _pick_font(
    ["msyhbd.ttc", "DengXian Bold.ttf", "simhei.ttf", "msyh.ttc", "NotoSansCJK-Bold.ttc"],
    ("msyhbd", "NotoSansCJK-Bold", "NotoSansSC-Bold", "SourceHanSans-Bold", "PingFang"),
) or _CJK
_EMOJI = _pick_font(
    ["seguiemj.ttf", "SEGUIEMJ.TTF", "NotoColorEmoji.ttf", "Apple Color Emoji.ttc"],
    ("seguiemj", "NotoColorEmoji", "Emoji"),
)


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = (size, bold)
    got = _FONT_CACHE.get(key)
    if got is not None:
        return got
    path = _CJK_BOLD if bold else _CJK
    try:
        f = ImageFont.truetype(path, size) if path else ImageFont.load_default(size=size)
    except OSError:
        f = ImageFont.load_default(size=size)
    _FONT_CACHE[key] = f
    return f


def emoji_font(size: int) -> ImageFont.FreeTypeFont | None:
    if not _EMOJI:
        return None
    got = _EMOJI_CACHE.get(size)
    if got is not None:
        return got
    try:
        f = ImageFont.truetype(_EMOJI, size)
    except OSError:
        return None
    _EMOJI_CACHE[size] = f
    return f


def is_emoji(ch: str) -> bool:
    o = ord(ch)
    return (
        0x1F000 <= o <= 0x1FAFF
        or 0x2600 <= o <= 0x27BF
        or 0x2B00 <= o <= 0x2BFF
        or 0x1F1E6 <= o <= 0x1F1FF
        or o in (0x2705, 0x274C, 0x2757, 0x2B50, 0x2764)
    )


def _visible(ch: str) -> bool:
    o = ord(ch)
    return o not in (0xFE0F, 0xFE0E, 0x200D, 0x20E3)  # 变体选择符 / 零宽连接


def width_of(text: str, size: int, bold: bool = False) -> float:
    f = font(size, bold)
    ef = emoji_font(size)
    total = 0.0
    for ch in text:
        if not _visible(ch):
            continue
        try:
            total += (ef if (ef and is_emoji(ch)) else f).getlength(ch)
        except Exception:
            total += size * 0.6
    return total


def wrap(text: str, size: int, max_w: float, bold: bool = False) -> list[str]:
    """按像素宽度折行；中文逐字断，英文尽量在空格处断。"""
    text = str(text)
    if not text:
        return [""]
    lines: list[str] = []
    for raw in text.split("\n"):
        if width_of(raw, size, bold) <= max_w:
            lines.append(raw)
            continue
        cur = ""
        for ch in raw:
            if not _visible(ch):
                cur += ch
                continue
            if width_of(cur + ch, size, bold) <= max_w:
                cur += ch
                continue
            cut = cur
            if ch.isascii() and ch.isalnum():
                idx = max(cur.rfind(" "), cur.rfind("　"))
                if idx > 0:
                    cut, rest = cur[:idx], cur[idx + 1:]
                else:
                    rest = ""
            else:
                rest = ""
            lines.append(cut.rstrip())
            cur = rest + ch
        if cur:
            lines.append(cur.rstrip())
    return lines or [""]


def draw_rich(
    draw: ImageDraw.ImageDraw,
    x: float,
    y: float,
    text: str,
    size: int,
    fill: str = INK,
    bold: bool = False,
) -> float:
    """逐字选字体画出混排文本（中英文用雅黑，彩色 emoji 用 seguiemj）。"""
    f = font(size, bold)
    ef = emoji_font(size)
    cx = float(x)
    for ch in text:
        if not _visible(ch):
            continue
        if ef and is_emoji(ch):
            try:
                draw.text((cx, y), ch, font=ef, embedded_color=True, anchor="la")
            except Exception:
                draw.text((cx, y), ch, font=f, fill=fill, anchor="la")
            cx += ef.getlength(ch)
        else:
            draw.text((cx, y), ch, font=f, fill=fill, anchor="la")
            cx += f.getlength(ch)
    return cx - x


def color(value: str | None, default: str = ACCENT) -> str:
    """'accent' / 'good' 这种语气名统一转成色值；已经是 #rrggbb 就原样用。"""
    if not value:
        return default
    v = str(value)
    if v.startswith("#"):
        return v
    return TONE.get(v, default)


def _mix(hex_a: str, hex_b: str, t: float) -> str:
    a = hex_a.lstrip("#")
    b = hex_b.lstrip("#")
    ra, ga, ba = int(a[0:2], 16), int(a[2:4], 16), int(a[4:6], 16)
    rb, gb, bb = int(b[0:2], 16), int(b[2:4], 16), int(b[4:6], 16)
    return "#%02x%02x%02x" % (
        round(ra + (rb - ra) * t),
        round(ga + (gb - ga) * t),
        round(ba + (bb - ba) * t),
    )


# ------------------------------------------------------------------ 画布
class Canvas:
    def __init__(self) -> None:
        self.blocks: list[dict] = []
        self.height = PAD

    def add(self, height: float) -> None:
        self.height += height


def _panel(draw: ImageDraw.ImageDraw, box, fill=PANEL, outline=LINE, radius=RADIUS) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=2)


def _pill(draw: ImageDraw.ImageDraw, x: float, y: float, text: str, size: int,
          color: str, bg: str, pad_x: int = 14, height: int = 40) -> float:
    w = width_of(text, size) + pad_x * 2
    draw.rounded_rectangle((x, y, x + w, y + height), radius=height // 2, fill=bg)
    draw_rich(draw, x + pad_x, y + (height - size) // 2 - 2, text, size, color)
    return w


# ------------------------------------------------------------------ 卡片描述渲染
def render_card(spec: dict) -> Image.Image:
    """把 spec 画成整张图。

    spec = {
        "emoji": "🛵", "title": "...", "subtitle": "...", "eyebrow": "...",
        "accent": "#ff8b3d",
        "chips": [(icon_text, tone), ...],
        "hero": {"emoji": "✅", "text": "...", "sub": "..."},
        "blocks": [...],
        "footer": "...",
    }
    """
    accent = color(spec.get("accent"))
    content_w = WIDTH - PAD * 2

    # 先按「大概多高」铺一张足够长的画布，画完再按实际底部裁掉多余空白。
    # 用估算高度去排位置很容易估少（emoji、换行都会多占几行），一估少就压字。
    est = _measure_header(spec, content_w) + 30
    for blk in spec.get("blocks") or []:
        est += _measure_block(blk, content_w) + GAP
    est = int(max(200, est * 1.35)) + (140 if spec.get("footer") else 0)
    # 画一遍；要是发现画布不够高（底部被切了），就加高重画一遍。
    # 千万别用 min(est, 实际底部) 直接裁——est 估少的那次会把卡片底边切掉。
    for _ in range(3):
        img = Image.new("RGB", (WIDTH, est), BG)
        draw = ImageDraw.Draw(img)

        # 顶部一条主色渐层，给整张图一个「头」
        for i in range(6):
            c = _mix(accent, BG, 1 - (i + 1) / 7)
            draw.rectangle((0, i * 2, WIDTH, i * 2 + 2), fill=c)

        y = PAD
        y += _draw_header(draw, spec, PAD, y, content_w, accent) + GAP
        for blk in spec.get("blocks") or []:
            y += _draw_block(draw, blk.get("kind", "note"), blk, PAD, y, content_w, accent) + GAP
        if spec.get("footer"):
            y += _draw_footer(draw, spec["footer"], PAD, y + 6, content_w)
        need = int(y + PAD - GAP / 2)
        if need <= est:
            break
        est = need + 140
    return img.crop((0, 0, WIDTH, max(132, need)))


def _measure_header(spec: dict, content_w: float) -> float:
    h = 0.0
    if spec.get("eyebrow"):
        h += 30
    title = spec.get("title") or ""
    size = 40
    if title:
        h += 22 * len(wrap(title, size, content_w - 80, bold=True)) + 10
    if spec.get("subtitle"):
        h += 14 * len(wrap(spec["subtitle"], 24, content_w - 80))
    if spec.get("hero"):
        hero = spec["hero"]
        h += 24 + 44 + (18 * len(wrap(hero.get("text") or "", 30, content_w - 90, bold=True)))
        if hero.get("sub"):
            h += 12 + 20 * len(wrap(hero["sub"], 22, content_w - 90))
    return h or 60


def _draw_header(draw, spec, x, y, content_w, accent) -> float:
    start = y
    if spec.get("eyebrow"):
        draw_rich(draw, x, y, spec["eyebrow"], 20, accent, bold=True)
        y += 30
    title = spec.get("title") or ""
    if title:
        for line in wrap(title, 40, content_w - 80, bold=True):
            draw_rich(draw, x + 74, y, line, 40, INK, bold=True)
            y += 52
        y += 6
    if spec.get("subtitle"):
        for line in wrap(spec["subtitle"], 24, content_w - 80):
            draw_rich(draw, x + 74, y, line, 24, MUTED)
            y += 34
    if spec.get("emoji"):
        ef = emoji_font(54)
        if ef:
            try:
                draw.text((x, start + 2), spec["emoji"], font=ef, embedded_color=True, anchor="la")
            except Exception:
                pass
    chips = spec.get("chips") or []
    if chips:
        y += 12
        cx = float(x)
        for text, tone in chips:
            w = width_of(text, 21) + 28
            if cx + w > x + content_w:
                cx = float(x)
                y += 50
            _pill(draw, cx, y, text, 21, TONE.get(tone, INK), _mix(TONE.get(tone, INK), PANEL, 0.84))
            cx += w + 10
        y += 56
    hero = spec.get("hero")
    if hero:
        y += 6
        _panel(draw, (x, y, x + content_w, y + 24 + 44 + 18 * len(wrap(hero.get("text") or "", 30, content_w - 90, bold=True))
                      + (12 + 20 * len(wrap(hero["sub"], 22, content_w - 90)) if hero.get("sub") else 0)),
               fill=_mix(accent, PANEL, 0.88), outline=_mix(accent, LINE, 0.55))
        inner = y + 22
        if hero.get("emoji"):
            ef = emoji_font(40)
            text_x = x + 26
            if ef:
                try:
                    draw.text((x + 20, inner - 4), hero["emoji"], font=ef, embedded_color=True, anchor="la")
                except Exception:
                    pass
                text_x = x + 74
        else:
            text_x = x + 26
        for line in wrap(hero.get("text") or "", 30, content_w - 90, bold=True):
            draw_rich(draw, text_x, inner, line, 30, INK, bold=True)
            inner += 44
        if hero.get("sub"):
            inner += 4
            for line in wrap(hero["sub"], 22, content_w - 90):
                draw_rich(draw, text_x, inner, line, 22, MUTED)
                inner += 30
        y = inner + 16
    return y - start


def _measure_block(blk: dict, content_w: float) -> float:
    kind = blk.get("kind", "note")
    x_pad = 28
    inner_w = content_w - x_pad * 2
    h = 0.0
    if kind == "section":
        h += 46
    elif kind == "rows":
        h += 18
        for it in blk.get("items") or []:
            h += 44
        h += 16
    elif kind == "bars":
        h += 18
        for _ in blk.get("items") or []:
            h += 72
        h += 16
    elif kind == "orders":
        h += 6
        for it in blk.get("items") or []:
            h += _measure_order(it, inner_w) + 14
        h += 6
    elif kind == "list":
        h += 18
        for it in blk.get("items") or []:
            h += 34 * max(1, len(wrap(it.get("text") or "", 25, inner_w - 60))) + 26
            if it.get("bar"):
                h += 16
        h += 14
    elif kind == "rank":
        h += 14
        for _ in blk.get("items") or []:
            h += 66
        h += 12
    elif kind == "table":
        h += 16 + 40 + 34 * len(blk.get("rows") or []) + 14
    elif kind == "note":
        tone = blk.get("tone") or "info"
        h += 20 + 30 * len(wrap(blk.get("text") or "", 23, content_w - 40)) + 20
        del tone
    elif kind == "text":
        h += 12 * len(wrap(blk.get("text") or "", 25, content_w - 8)) + 6
    return max(h, 40)


def _order_layout(order: dict, inner_w: float) -> dict:
    """订单卡的内容排布：先算好每行放哪，再决定卡片高度 —— 先画框后填字
    很容易高度估少，字就压到下一张卡上了。"""
    badge = str(order.get("badge") or "")
    badge_w = width_of(badge, 24, True) + 26 + 14 if badge else 0
    text_x = 24 + badge_w
    route = wrap(order.get("route") or "", 27, inner_w - text_x - 8, bold=True)
    who = wrap(order.get("who") or "", 22, inner_w - 30) if order.get("who") else []
    item = wrap(order.get("item") or "", 24, inner_w - 30) if order.get("item") else []
    note = wrap(order.get("note") or "", 21, inner_w - 30) if order.get("note") else []

    rows: list[list[tuple[str, str]]] = []
    px = 0.0
    cur: list[tuple[str, str]] = []
    for text, tone in order.get("pills") or []:
        pw = width_of(text, 20) + 26 + 8
        if px + pw > inner_w - 24 and cur:
            rows.append(cur)
            cur, px = [], 0.0
        cur.append((text, tone))
        px += pw
    if cur:
        rows.append(cur)

    h = 18 + 36 * len(route) + 30 * len(who) + 32 * len(item) + 27 * len(note)
    if rows:
        h += 6 + 46 * len(rows)
    h += 14
    return {"h": h, "badge": badge, "text_x": text_x, "route": route, "who": who,
            "item": item, "note": note, "pills": rows}


def _measure_order(order: dict, inner_w: float) -> float:
    return _order_layout(order, inner_w)["h"]


def _draw_block(draw, kind, blk, x, y, content_w, accent) -> float:
    start = y
    x_pad = 28
    inner_w = content_w - x_pad * 2
    if kind == "section":
        icon = blk.get("icon") or ""
        tx = x
        if icon:
            ef = emoji_font(26)
            if ef:
                try:
                    draw.text((x, y + 2), icon, font=ef, embedded_color=True, anchor="la")
                except Exception:
                    pass
            tx = x + 38
        draw_rich(draw, tx, y + 4, blk.get("title") or "", 26, INK, bold=True)
        return 46

    if kind == "rows":
        items = blk.get("items") or []
        h = 18 + 44 * len(items) + 16
        _panel(draw, (x, y, x + content_w, y + h))
        cy = y + 18
        for it in items:
            k = str(it.get("k") or "")
            v = str(it.get("v") or "")
            draw_rich(draw, x + x_pad, cy + 8, k, 24, MUTED)
            vw = width_of(v, 26, True)
            draw_rich(draw, x + content_w - x_pad - vw, cy + 6, v, 26, TONE.get(it.get("tone") or "accent", INK), bold=True)
            cy += 44
        return y + h - start

    if kind == "bars":
        items = blk.get("items") or []
        h = 18 + 72 * len(items) + 16
        _panel(draw, (x, y, x + content_w, y + h))
        cy = y + 18
        for it in items:
            label = str(it.get("label") or "")
            cur = float(it.get("cur") or 0)
            mx = float(it.get("max") or 100) or 100
            bar_color = color(it.get("color"), accent)
            draw_rich(draw, x + x_pad, cy + 2, label, 23, MUTED)
            note = str(it.get("note") or f"{cur:g}/{mx:g}")
            nw = width_of(note, 23, True)
            draw_rich(draw, x + content_w - x_pad - nw, cy + 2, note, 23, INK, bold=True)
            bx0, by0 = x + x_pad, cy + 34
            bx1 = x + content_w - x_pad
            draw.rounded_rectangle((bx0, by0, bx1, by0 + 16), radius=8, fill=PANEL_SOFT)
            ratio = max(0.0, min(1.0, cur / mx))
            if ratio > 0:
                draw.rounded_rectangle((bx0, by0, bx0 + max(16, (bx1 - bx0) * ratio), by0 + 16), radius=8, fill=bar_color)
            cy += 72
        return y + h - start

    if kind == "orders":
        cy = y + 6
        for order in blk.get("items") or []:
            lay = _order_layout(order, inner_w)
            h = lay["h"]
            accent_o = color(order.get("accent"), accent)
            _panel(draw, (x, cy, x + content_w, cy + h), fill=PANEL, outline=_mix(accent_o, LINE, 0.7))
            badge = lay["badge"]
            if badge:
                bw = width_of(badge, 24, True) + 26
                draw.rounded_rectangle((x + 22, cy + 20, x + 22 + bw, cy + 20 + 40), radius=12,
                                       fill=_mix(accent_o, PANEL, 0.75))
                draw_rich(draw, x + 22 + 13, cy + 26, badge, 24, accent_o, bold=True)
            ty = cy + 18
            for line in lay["route"]:
                draw_rich(draw, x + lay["text_x"], ty, line, 27, INK, bold=True)
                ty += 36
            for line in lay["who"]:
                draw_rich(draw, x + 24, ty, line, 22, MUTED)
                ty += 30
            for line in lay["item"]:
                draw_rich(draw, x + 24, ty, line, 24, INK)
                ty += 32
            for line in lay["note"]:
                draw_rich(draw, x + 24, ty, line, 21, DIM)
                ty += 27
            py = ty + 6
            for row in lay["pills"]:
                px = float(x + 24)
                for text, tone in row:
                    t = TONE.get(tone, accent)
                    pw = width_of(text, 20) + 26 + 8
                    _pill(draw, px, py, text, 20, t, _mix(t, PANEL_SOFT, 0.86), pad_x=13, height=36)
                    px += pw
                py += 46
            cy += h + 14
        return cy - start

    if kind == "list":
        cy = y + 10
        for it in blk.get("items") or []:
            icon = it.get("icon") or ""
            tx = x + 6
            if icon:
                ef = emoji_font(26)
                if ef:
                    try:
                        draw.text((x + 2, cy), icon, font=ef, embedded_color=True, anchor="la")
                    except Exception:
                        pass
                tx = x + 44
            lines = wrap(it.get("text") or "", 25, inner_w - 60)
            for li, line in enumerate(lines):
                draw_rich(draw, tx, cy + 2 + li * 34, line, 25, INK)
            meta = it.get("meta") or ""
            if meta:
                mw = width_of(meta, 23, True)
                draw_rich(draw, x + content_w - 6 - mw, cy + 2, meta, 23, TONE.get(it.get("tone") or "accent", accent), bold=True)
            cy += 34 * max(1, len(lines)) + 26
            bar = it.get("bar")
            if bar:
                bx0 = x + 44
                bx1 = x + content_w - 6
                draw.rounded_rectangle((bx0, cy - 8, bx1, cy + 6), radius=7, fill=PANEL_SOFT)
                ratio = max(0.0, min(1.0, float(bar.get("ratio") or 0)))
                if ratio > 0:
                    draw.rounded_rectangle((bx0, cy - 8, bx0 + max(14, (bx1 - bx0) * ratio), cy + 6),
                                           radius=7, fill=TONE.get(bar.get("tone") or "accent", accent))
                cy += 16
        return cy - 6 - start

    if kind == "rank":
        items = blk.get("items") or []
        h = 14 + 66 * len(items) + 12
        _panel(draw, (x, y, x + content_w, y + h))
        cy = y + 14
        for it in items:
            pos = it.get("pos")
            medal = it.get("medal") or f"{pos}"
            tone = it.get("tone") or "accent"
            draw.rounded_rectangle((x + 20, cy + 10, x + 20 + 46, cy + 10 + 46), radius=14,
                                   fill=_mix(TONE.get(tone, accent), PANEL, 0.8))
            pw = width_of(str(medal), 24, True)
            draw_rich(draw, x + 20 + (46 - pw) / 2, cy + 20, str(medal), 24, TONE.get(tone, accent), bold=True)
            draw_rich(draw, x + 84, cy + 12, it.get("name") or "", 26, INK, bold=True)
            sub = it.get("sub") or ""
            if sub:
                draw_rich(draw, x + 84, cy + 44, sub, 21, MUTED)
            val = it.get("value") or ""
            vw = width_of(val, 26, True)
            draw_rich(draw, x + content_w - 24 - vw, cy + 22, val, 26, TONE.get(tone, accent), bold=True)
            cy += 66
        return y + h - start

    if kind == "table":
        head = blk.get("head") or []
        rows = blk.get("rows") or []
        h = 16 + 40 + 34 * len(rows) + 14
        _panel(draw, (x, y, x + content_w, y + h))
        cols = len(head) or 1
        col_w = inner_w / cols
        cx = x + x_pad
        for i, cell in enumerate(head):
            draw_rich(draw, cx + 2, y + 16, str(cell), 20, MUTED, bold=True)
            cx += col_w
        yy = y + 16 + 40
        for row in rows:
            cx = x + x_pad
            for i, cell in enumerate(row[:cols]):
                text = str(cell)
                while text and width_of(text, 21) > col_w - 10:
                    text = text[:-1]
                if text != str(cell):
                    text = text[:-1] + "…" if len(text) > 1 else text
                draw_rich(draw, cx + 2, yy, text, 21, INK)
                cx += col_w
            yy += 34
        return y + h - start

    if kind == "note":
        tone = blk.get("tone") or "info"
        tone_color = TONE.get(tone, BLUE)
        lines = wrap(blk.get("text") or "", 23, content_w - 40)
        h = 20 + 30 * len(lines) + 20
        _panel(draw, (x, y, x + content_w, y + h), fill=_mix(tone_color, PANEL, 0.88), outline=_mix(tone_color, LINE, 0.6))
        cy = y + 20
        for line in lines:
            draw_rich(draw, x + 20, cy, line, 23, _mix(tone_color, INK, 0.35))
            cy += 30
        return y + h - start

    if kind == "text":
        cy = y
        for line in wrap(blk.get("text") or "", 25, content_w - 8):
            draw_rich(draw, x + 4, cy, line, 25, MUTED)
            cy += 34
        return cy - start

    return 40


def _draw_footer(draw, text: str, x, y, content_w) -> float:
    lines = wrap(text, 22, content_w - 8)
    cy = y
    for line in lines:
        draw_rich(draw, x + 4, cy, line, 22, DIM)
        cy += 30
    return cy - y


# ------------------------------------------------------------------ 落盘
def _temp_dir() -> Path:
    d = Path(__file__).resolve().parent.parent / "data" / "temp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_card(img: Image.Image, prefix: str = "card") -> str:
    d = _temp_dir()
    now = time.time()
    for old in d.glob("*.png"):
        try:
            if now - old.stat().st_mtime > 3600:
                old.unlink()
        except OSError:
            pass
    path = d / f"{prefix}-{int(now)}-{random.randrange(1000, 9999)}.png"
    img.save(path, "PNG", optimize=True)
    return str(path)