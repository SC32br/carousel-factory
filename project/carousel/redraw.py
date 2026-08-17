"""Шаг 3. Редроу: пин-референс → наш легальный кадр (kie.ai, image-to-image).

Железное правило из GUARDRAILS: всегда пин → редроу, никогда не с нуля,
и НЕ копия 1:1 - та же энергия, но сменить ракурс/деталь."""
from __future__ import annotations

from pathlib import Path

from . import config, llm
from .kie_client import KieClient

_VARIATION = (
    "Recreate this Pinterest reference as a NEW original photograph: keep the same subject, "
    "energy, styling and composition idea, but change the camera angle by 15-30 degrees or "
    "the crop, and change ONE prop or gesture, so it is clearly not a 1:1 copy. "
    "Remove every logo, brand name and any readable text. "
    "If a model is visible: dark brown brunette hair (NOT blonde), faceless framing only. "
)


def build_redraw_prompt(pin_path: Path, theme: dict, extra_note: str = "") -> str:
    """Claude смотрит пин и пишет короткое предметное описание сцены для редроу."""
    schema = {
        "type": "object",
        "properties": {"scene": {"type": "string"}},
        "required": ["scene"],
        "additionalProperties": False,
    }
    result = llm.structured(
        [
            {
                "type": "text",
                "text": (
                    "Опиши сцену этого фото одним абзацем по-английски для image-to-image "
                    "генерации: что в кадре, ракурс, свет, фактуры, настроение. Без брендов. "
                    f"Контекст: контент-семья «{theme['family']}», сегмент «{theme['segment']}»."
                ),
            },
            llm.image_block(pin_path),
        ],
        schema,
        max_tokens=2000,
    )
    prompt = f"{result['scene']} {_VARIATION}{config.STYLE_TAIL}"
    learned = config.extra_rules("Визуал")
    if learned:
        prompt += " LEARNED RULES (mandatory): " + learned.replace("\n", " ")
    if extra_note:
        prompt += " CLIENT FEEDBACK ON PREVIOUS VERSION (mandatory fix): " + extra_note
    return prompt


def redraw(pin_path: Path, theme: dict, out_path: Path) -> Path:
    kie = KieClient(config.require("KIE_API_KEY", config.KIE_API_KEY))
    prompt = build_redraw_prompt(pin_path, theme)
    (out_path.with_suffix(".prompt.txt")).write_text(prompt, encoding="utf-8")

    source_url = kie.upload_file(pin_path, upload_path="carousel/pins")
    urls = kie.redraw_image(config.KIE_IMAGE_MODEL, prompt, source_url, aspect_ratio="4:5")
    kie.download(urls[0], out_path)
    return out_path
