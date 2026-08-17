"""Шаг 7 (опция). Живая обложка: фон оживляем через kie.ai, текст клеим статикой поверх.

Правило из доков: двигается только фон (свет/glow/parallax), текст - НЕ двигается,
поэтому оверлеим прозрачный PNG с текстом через ffmpeg."""
from __future__ import annotations

import subprocess
from pathlib import Path

from . import config
from .kie_client import KieClient

_LOOP_PROMPT = (
    "Subtle cinemagraph loop of this exact photo: gentle light shimmer, soft glow drift, "
    "tiny parallax on background details, delicate film grain movement. Camera locked. "
    "No new objects, no text, faces and bodies stay almost still (micro movement only). "
    "First frame equals last frame for a seamless 5 second loop."
)


def _sound_scene(design: dict) -> str:
    """Звук подбираем под предмет первой панели - чтобы он был про кадр, а не «шум»."""
    scene = ""
    for panel in design.get("panels", []):
        if panel.get("n") == 1:
            scene = panel.get("scene", "").lower()
            break
    table = [
        (("ножниц", "стриж"), "crisp scissors snips with metallic ring"),
        (("фен", "сушк"), "soft hairdryer hum with airflow"),
        (("кист", "краск", "фольг"), "brush strokes in a bowl, foil crinkle"),
        (("кофе", "чашк", "латте"), "espresso machine hiss, cup set on a saucer"),
        (("телефон", "директ", "экран"), "short phone notification chimes, finger taps on glass"),
        (("лак", "флакон", "пилк", "ногт"), "glass bottle click, nail file rasp"),
        (("зеркал", "салон", "кресл"), "quiet salon room tone, chair leather creak"),
    ]
    for keys, sound in table:
        if any(k in scene for k in keys):
            return sound + ", plus warm room ambience"
    return "warm room ambience, quiet material sounds matching the object"


def animate_slide(slide_png: Path, design: dict, out_mp4: Path) -> Path:
    """Первый слайд → 6-сек живая обложка со звуком (grok, без речи).

    Grok сам наследует пропорции входной картинки, поэтому подаём готовый слайд
    1080×1350 и приводим результат к тому же размеру. Текст на слайде уже вшит,
    поэтому просим двигать только фон и свет - буквы должны остаться неподвижными.
    """
    kie = KieClient(config.require("KIE_API_KEY", config.KIE_API_KEY))
    url = kie.upload_file(slide_png, upload_path="carousel/slides")
    prompt = (
        "Cinemagraph loop of this exact card: gentle light shimmer, soft glow drift, "
        "subtle parallax on the background object, delicate film grain movement. "
        "Camera locked. The TEXT stays perfectly still, sharp and unchanged. "
        "No new objects, no new letters, no people. "
        f"Mood: {design.get('series_look', 'clean film editorial')}. "
        # Звук просим предметный и слышимый: «тихий эмбиент» модель понимает как тишину
        # (замер 17.08: −56 дБ, фактически ничего). Никакой речи и песен.
        f"AUDIO (clearly audible, foreground): {_sound_scene(design)} "
        "Rich detailed sound design, close-miked, no speech, no voices, no singing, no lyrics."
    )
    task_id = kie.create_task(
        config.KIE_VIDEO_MODEL,
        {"prompt": prompt, "image_urls": [url], "duration": "6",
         "resolution": "720p", "mode": "normal"},
    )
    urls = kie.wait_for_result(task_id, timeout_sec=900)

    raw = out_mp4.with_name("cover-raw.mp4")
    kie.download(urls[0], raw)
    _check_frame0(raw, slide_png)

    # 1080×1350, H.264 + AAC. Звук нормализуем к -14 LUFS
    # (стандарт соцсетей): grok отдаёт дорожку на -56 дБ, без этого её не слышно.
    cmd = [
        "ffmpeg", "-y", "-i", str(raw),
        "-vf", "scale=1080:1350:force_original_aspect_ratio=increase,crop=1080:1350",
        "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30",
        "-c:a", "aac", "-b:a", "128k", "-ac", "2",
        "-movflags", "+faststart", str(out_mp4),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if not out_mp4.is_file():
        raise RuntimeError(f"ffmpeg не собрал живую обложку: {result.stderr[-500:]}")
    _check_audio(out_mp4)
    return out_mp4


def _check_audio(video: Path, *, min_peak_db: float = -20.0) -> None:
    """Проверка, что звук реально слышен, а не формальная дорожка тишины."""
    out = subprocess.run(
        ["ffmpeg", "-i", str(video), "-af", "volumedetect", "-f", "null", "/dev/null"],
        capture_output=True, text=True, timeout=180,
    ).stderr
    peak = next((float(l.split(":")[1].strip().split()[0])
                 for l in out.splitlines() if "max_volume" in l), None)
    if peak is None:
        print("[видео] звуковой дорожки нет", flush=True)
    elif peak < min_peak_db:
        print(f"[видео] звук почти беззвучный (пик {peak} дБ) - модель дала тишину", flush=True)
    else:
        print(f"[видео] звук в порядке (пик {peak} дБ)", flush=True)


def make_live_cover(cover_bg: Path, overlay_png: Path, out_mp4: Path) -> Path:
    kie = KieClient(config.require("KIE_API_KEY", config.KIE_API_KEY))
    bg_url = kie.upload_file(cover_bg, upload_path="carousel/covers")
    urls = kie.animate_image(config.KIE_VIDEO_MODEL, _LOOP_PROMPT, bg_url)

    raw = out_mp4.with_name("cover-raw.mp4")
    kie.download(urls[0], raw)
    _check_frame0(raw, cover_bg)
    return assemble(raw, overlay_png, out_mp4)


def assemble(raw: Path, overlay_png: Path, out_mp4: Path) -> Path:
    """Сборка без нового вызова veo (проверено 15.08: veo отдаёт 8с, хвост дрейфует):
    первые 4 сек → бумеранг (вперёд+реверс = бесшовный луп по построению)
    → кроп 1080×1350 → статичный текст оверлеем. Без аудио."""
    cmd = [
        "ffmpeg", "-y", "-t", "4", "-i", str(raw), "-i", str(overlay_png),
        "-filter_complex",
        "[0:v]split[f][r];[r]reverse[rev];[f][rev]concat=n=2:v=1[loop];"
        "[loop]scale=1080:1350:force_original_aspect_ratio=increase,"
        "crop=1080:1350[bg];[bg][1:v]overlay=0:0",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30", "-an",
        "-movflags", "+faststart", str(out_mp4),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if not out_mp4.is_file():
        raise RuntimeError(f"ffmpeg не собрал живую обложку: {result.stderr[-500:]}")
    return out_mp4


def _check_frame0(video: Path, source_png: Path, *, threshold: float = 35.0) -> None:
    """QA из плейбука Каруселек: кадр-0 видео обязан совпадать с фоном обложки."""
    import io

    from PIL import Image, ImageChops, ImageStat

    frame = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(video), "-vframes", "1", "-f", "image2pipe",
         "-vcodec", "png", "-"],
        capture_output=True, timeout=120,
    )
    img_a = Image.open(io.BytesIO(frame.stdout)).convert("RGB")
    img_b = Image.open(source_png).convert("RGB")

    # veo может отдать другую пропорцию (9:16 против 4:5) - сравниваем честно:
    # центральный кроп обоих кадров до общей пропорции, потом один размер.
    ratio = min(img_a.width / img_a.height, img_b.width / img_b.height)

    def center_crop(img: Image.Image) -> Image.Image:
        w = min(img.width, int(img.height * ratio))
        h = int(w / ratio)
        left, top = (img.width - w) // 2, (img.height - h) // 2
        return img.crop((left, top, left + w, top + h))

    img_a, img_b = center_crop(img_a), center_crop(img_b)
    img_b = img_b.resize(img_a.size)
    mae = sum(ImageStat.Stat(ImageChops.difference(img_a, img_b)).mean) / 3
    if mae > threshold:
        raise RuntimeError(
            f"Кадр-0 живой обложки не совпадает с фоном (MAE {mae:.0f} > {threshold}) - "
            "veo уехал от исходника, нужен перегон"
        )
