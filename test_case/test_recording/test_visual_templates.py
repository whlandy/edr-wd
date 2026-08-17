from __future__ import annotations

from PIL import Image, ImageDraw
import pytest

from agent.execution import StepMaterializationError
from agent.recording.pillow_matcher import PillowTemplateMatcher
from agent.recording.templates import generate_redacted_templates

pytestmark = pytest.mark.unit


def test_template_generation_redacts_before_element_and_context_crop(tmp_path):
    screenshot = tmp_path / "screen.png"
    image = Image.new("RGB", (160, 100), "white")
    painter = ImageDraw.Draw(image)
    painter.rectangle((60, 30, 99, 59), fill="red")
    painter.rectangle((10, 10, 29, 19), fill="blue")
    image.save(screenshot)

    artifacts = generate_redacted_templates(
        screenshot,
        tmp_path / "assets",
        step_id="step-0001",
        element_rect=(60, 30, 100, 60),
        redactions=[(10, 10, 30, 20)],
        context_padding=60,
    )
    assert Image.open(artifacts.element).size == (40, 30)
    context = Image.open(artifacts.context)
    assert context.getpixel((15, 15)) == (0, 0, 0)
    assert artifacts.element_sha256.startswith("sha256:")


def test_template_generation_refuses_redaction_over_target(tmp_path):
    screenshot = tmp_path / "screen.png"
    Image.new("RGB", (100, 100), "white").save(screenshot)
    with pytest.raises(StepMaterializationError) as exc:
        generate_redacted_templates(
            screenshot,
            tmp_path,
            step_id="step-0001",
            element_rect=(20, 20, 50, 50),
            redactions=[(30, 30, 40, 40)],
        )
    assert exc.value.code == "visual_template_target_redacted"


def test_pillow_matcher_finds_template_at_supported_scale(tmp_path):
    screenshot = tmp_path / "screen.png"
    template = tmp_path / "template.png"
    image = Image.new("RGB", (120, 80), "white")
    painter = ImageDraw.Draw(image)
    painter.rectangle((45, 25, 64, 44), fill="black")
    painter.line((45, 25, 64, 44), fill="red", width=3)
    image.save(screenshot)
    image.crop((45, 25, 65, 45)).save(template)

    matches = PillowTemplateMatcher(scales=(1.0,))(str(template), {
        "screenshot_path": str(screenshot),
    })
    assert matches[0].rect == (45, 25, 65, 45)
    assert matches[0].confidence == 1.0


def test_pillow_matcher_projects_window_local_pixels_to_screen_coordinates(tmp_path):
    screenshot = tmp_path / "window.png"
    template = tmp_path / "template.png"
    image = Image.new("RGB", (80, 60), "white")
    ImageDraw.Draw(image).rectangle((10, 15, 29, 34), fill="blue")
    image.save(screenshot)
    image.crop((10, 15, 30, 35)).save(template)

    matches = PillowTemplateMatcher(scales=(1.0,))(str(template), {
        "screenshot_path": str(screenshot),
        "screenshot_origin": [100, 200],
    })

    assert matches[0].rect == (110, 215, 130, 235)
