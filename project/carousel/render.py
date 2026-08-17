"""Шаг 5. Вёрстка: HTML → Chrome headless → PNG 1080×1350.

Шаблон тёмных карточек 1080×1350, Chrome headless."""
from __future__ import annotations

import base64
import subprocess
from pathlib import Path

from . import config

W, H = 1080, 1350

_BASE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<style>
*{{margin:0;padding:0;box-sizing:border-box;-webkit-font-smoothing:antialiased}}
html,body{{width:1080px;height:1350px}}
body{{position:relative;overflow:hidden;background:{bg};
 font-family:"Avenir Next Condensed","Arial Narrow","Helvetica Neue",Arial,sans-serif;color:#f2ece3}}
.grain{{position:absolute;inset:0;opacity:.06;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='140' height='140'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='2'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E")}}
.glow{{position:absolute;top:-25%;right:-20%;width:80%;height:80%;
 background:radial-gradient(closest-side,rgba(120,90,60,.28),rgba(0,0,0,0));filter:blur(10px)}}
.wrap{{position:absolute;inset:0;padding:92px 84px;display:flex;flex-direction:column}}
.kick{{font-family:"Helvetica Neue",Arial,sans-serif;font-size:24px;letter-spacing:.32em;
 text-transform:uppercase;color:#c9a86a;font-weight:600}}
.kick .r{{color:#b5473f}}
.mid{{flex:1;display:flex;flex-direction:column;justify-content:center}}
.h{{font-weight:700;text-transform:uppercase;font-size:88px;line-height:.96;letter-spacing:.004em}}
.h .ac{{color:#e7c979}}
.b{{margin-top:30px;font-family:"Helvetica Neue",Arial,sans-serif;font-weight:300;
 font-size:34px;line-height:1.42;color:#cabfb2;max-width:88%}}
.big{{font-weight:700;font-size:230px;line-height:.9;color:#e7c979;letter-spacing:-.01em}}
.foot{{display:flex;align-items:center;gap:18px}}
.line{{width:64px;height:2px;background:rgba(255,255,255,.5)}}
.sw{{font-family:"Helvetica Neue",Arial,sans-serif;font-size:21px;letter-spacing:.24em;
 text-transform:uppercase;color:#efe7da;opacity:.85}}
.pill{{display:inline-block;margin-top:28px;background:#b5473f;color:#fff;font-weight:700;
 font-size:40px;letter-spacing:.06em;padding:18px 40px;border-radius:60px;text-transform:uppercase}}
.photo{{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}}
.scrim{{position:absolute;inset:0;background:linear-gradient(180deg,rgba(10,8,7,0) 42%,rgba(10,8,7,.82) 78%)}}
.cover-wrap{{position:absolute;left:0;right:0;bottom:0;padding:0 84px 88px;display:flex;flex-direction:column}}
.cover-h{{font-weight:700;text-transform:uppercase;font-size:92px;line-height:.98;color:#fff}}
.cover-h .ac{{color:#e7c979}}
.cover-sub{{margin-top:18px;font-family:"Helvetica Neue",Arial,sans-serif;font-weight:300;
 font-size:34px;color:#e6ddd0}}
.cover-foot{{margin-top:34px;display:flex;align-items:center;gap:18px}}
</style></head><body>{body}</body></html>"""


def _accent(head: str, accent_word: str, css_class: str = "ac") -> str:
    if accent_word and accent_word in head:
        return head.replace(accent_word, f'<span class="{css_class}">{accent_word}</span>', 1)
    return head


def _screenshot(html: str, out_png: Path, *, transparent: bool = False) -> Path:
    out_png.parent.mkdir(parents=True, exist_ok=True)
    html_file = out_png.with_suffix(".html")
    html_file.write_text(html, encoding="utf-8")
    cmd = [
        config.CHROME_BIN, "--headless=new", "--no-sandbox", "--disable-gpu",
        "--hide-scrollbars", f"--window-size={W},{H}",
    ]
    if transparent:
        cmd.append("--default-background-color=00000000")
    cmd += [f"--screenshot={out_png.resolve()}", f"file://{html_file.resolve()}"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if not out_png.is_file():
        raise RuntimeError(f"Chrome не отрисовал {out_png.name}: {result.stderr[-500:]}")
    return out_png


def render_card(slide: dict, total: int, out_png: Path) -> Path:
    n = slide["n"]
    head = _accent(slide["head"], slide["accent_word"])
    body = slide.get("body", "")
    kick_extra = ' &nbsp;·&nbsp; <span style="color:#e7c979">сохрани</span>' if n == 7 else ""
    kick = (
        f'<div class="kick">{config.SERIES_NAME} &nbsp;·&nbsp; '
        f'<span class="r">{n:02d}</span> / {total:02d}{kick_extra}</div>'
    )

    if n == 5 and any(c.isdigit() for c in slide["accent_word"]):
        # слайд-пруф: гигантская цифра, заголовок мельче
        head_wo = slide["head"].replace(slide["accent_word"], "").replace("<br>", " ").strip(" --:")
        mid = (
            f'<div class="mid"><div class="big">{slide["accent_word"]}</div>'
            f'<div class="h" style="font-size:58px;margin-top:20px">{head_wo}</div>'
            f'<div class="b">{body}</div></div>'
        )
    elif n == 9:
        mid = (
            f'<div class="mid"><div class="h" style="font-size:82px;line-height:.98">{head}</div>'
            f'<div class="b">{body}</div>'
            f'<div><span class="pill">напиши «{config.CTA_WORD.lower()}» в директ</span></div></div>'
        )
    else:
        mid = f'<div class="mid"><div class="h">{head}</div>' + (
            f'<div class="b">{body}</div>' if body else ""
        ) + "</div>"

    foot = (
        '<div class="foot"><span class="line"></span><span class="sw">листай →</span></div>'
        if n < total else ""
    )
    html = _BASE.format(bg="#161211", body=f'<div class="grain"></div><div class="glow"></div><div class="wrap">{kick}{mid}{foot}</div>')
    return _screenshot(html, out_png)


def _photo_data_uri(photo: Path) -> str:
    data = base64.standard_b64encode(photo.read_bytes()).decode("ascii")
    suffix = photo.suffix.lstrip(".").lower().replace("jpg", "jpeg")
    return f"data:image/{suffix};base64,{data}"


def render_cover(photo: Path, head: str, accent_word: str, sub: str, out_png: Path) -> Path:
    """Обложка: редроу-фото + жирный КАПС + акцент золотом + «листай →» (формат-победитель)."""
    body = (
        f'<img class="photo" src="{_photo_data_uri(photo)}"><div class="scrim"></div>'
        f'<div class="grain"></div>'
        f'<div class="cover-wrap"><div class="cover-h">{_accent(head, accent_word)}</div>'
        f'<div class="cover-sub">{sub}</div>'
        f'<div class="cover-foot"><span class="line"></span><span class="sw">листай →</span></div></div>'
    )
    html = _BASE.format(bg="#161211", body=body)
    return _screenshot(html, out_png)


def render_cover_overlay(head: str, accent_word: str, sub: str, out_png: Path) -> Path:
    """Тот же текст обложки, но на прозрачном фоне - для ffmpeg-оверлея поверх видео."""
    body = (
        f'<div class="scrim"></div>'
        f'<div class="cover-wrap"><div class="cover-h">{_accent(head, accent_word)}</div>'
        f'<div class="cover-sub">{sub}</div>'
        f'<div class="cover-foot"><span class="line"></span><span class="sw">листай →</span></div></div>'
    )
    html = _BASE.format(bg="transparent", body=body)
    return _screenshot(html, out_png, transparent=True)
