from __future__ import annotations

from PIL import Image, ImageDraw
import pytest

from agent.execution import StepMaterializationError
from agent.recording.pillow_matcher import PillowTemplateMatcher
from agent.recording.templates import generate_redacted_templates
from agent.recording.models import RecordingModelError, ReplaySelector
from agent.recording.visual import SafeVisualResolver, VisualCandidate

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
    assert artifacts.context_offset == (60, 30)


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


@pytest.mark.parametrize("bad_origin", [[100], [100, "200"], [True, 200], None])
def test_pillow_matcher_rejects_malformed_explicit_screenshot_origin(tmp_path, bad_origin):
    screenshot = tmp_path / "window.png"
    template = tmp_path / "template.png"
    image = Image.new("RGB", (40, 40), "white")
    ImageDraw.Draw(image).rectangle((10, 10, 19, 19), fill="blue")
    image.save(screenshot)
    image.crop((10, 10, 20, 20)).save(template)

    matches = PillowTemplateMatcher(scales=(1.0,))(str(template), {
        "screenshot_path": str(screenshot), "screenshot_origin": bad_origin,
    })

    assert matches == []


def test_visual_replay_does_not_click_with_malformed_capture_origin(tmp_path):
    screenshot = tmp_path / "window.png"
    template = tmp_path / "template.png"
    image = Image.new("RGB", (40, 40), "white")
    ImageDraw.Draw(image).rectangle((10, 10, 19, 19), fill="blue")
    image.save(screenshot)
    image.crop((10, 10, 20, 20)).save(template)
    selector = ReplaySelector(
        window={"processName": "EDRClient.exe"}, control={},
        visual={"template": str(template), "elementSha256": "sha256:" + "a" * 64,
                "redacted": True},
    )
    resolver = SafeVisualResolver(
        PillowTemplateMatcher(scales=(1.0,)),
        template_verifier=lambda _path, _visual: True,
    )

    with pytest.raises(StepMaterializationError) as exc:
        resolver.resolve("gui.click", selector, {
            "screenshot_path": str(screenshot), "screenshot_origin": [100],
            "screenshot_scope": "window", "screenshot_redacted": True,
            "active_window": {"rect": [0, 0, 40, 40]},
        })

    assert exc.value.code == "visual_match_low_confidence"


def test_pillow_matcher_search_rect_respects_screenshot_origin(tmp_path):
    screenshot = tmp_path / "window.png"
    template = tmp_path / "target.png"
    image = Image.new("RGB", (80, 60), "white")
    painter = ImageDraw.Draw(image)
    painter.rectangle((10, 15, 19, 24), fill="red")
    painter.rectangle((50, 15, 59, 24), fill="red")
    image.save(screenshot)
    image.crop((10, 15, 20, 25)).save(template)
    matches = PillowTemplateMatcher(scales=(1.0,))(str(template), {
        "screenshot_path": str(screenshot), "screenshot_origin": [100, 200],
        "search_rect": [105, 210, 130, 235],
    })
    assert matches[0].rect == (110, 215, 120, 225)
    assert all(candidate.rect[0] < 130 for candidate in matches)


def test_pillow_matcher_distinguishes_icons_with_similar_luminance(tmp_path):
    screenshot = tmp_path / "window.png"
    template = tmp_path / "red-icon.png"
    image = Image.new("RGB", (120, 50), "white")
    # Both colours round to roughly the same grayscale luminance. A gray-only
    # matcher can treat the wrong icon as equally convincing.
    ImageDraw.Draw(image).rectangle((10, 10, 21, 21), fill=(255, 0, 0))
    ImageDraw.Draw(image).rectangle((70, 10, 81, 21), fill=(0, 130, 0))
    image.save(screenshot)
    image.crop((10, 10, 22, 22)).save(template)

    matches = PillowTemplateMatcher(scales=(1.0,))(str(template), {
        "screenshot_path": str(screenshot),
    })

    assert matches[0].rect == (10, 10, 22, 22)
    assert matches[0].confidence == 1.0
    assert all(candidate.confidence < 0.92 for candidate in matches[1:])


def test_pillow_matcher_keeps_spatially_distinct_repeated_icons(tmp_path):
    screenshot = tmp_path / "repeated.png"
    template = tmp_path / "icon.png"
    image = Image.new("RGB", (330, 34), "white")
    painter = ImageDraw.Draw(image)
    locations = [10 + index * 26 for index in range(12)]
    for x in locations:
        painter.rectangle((x, 10, x + 9, 19), fill="red")
    image.save(screenshot)
    image.crop((locations[0], 10, locations[0] + 10, 20)).save(template)

    matches = PillowTemplateMatcher(scales=(1.0,))(str(template), {
        "screenshot_path": str(screenshot),
    })

    exact = {candidate.rect for candidate in matches if candidate.confidence == 1.0}
    assert {(x, 10, x + 10, 20) for x in locations} <= exact


def test_real_matcher_and_context_choose_first_of_many_repeated_icons(tmp_path):
    screenshot = tmp_path / "repeated.png"
    element = tmp_path / "element.png"
    context = tmp_path / "context.png"
    image = Image.new("RGB", (1600, 60), "white")
    painter = ImageDraw.Draw(image)
    for index in range(60):
        x = 10 + index * 26
        painter.rectangle((x, 10, x + 9, 19), fill="red")
    painter.rectangle((2, 32, 12, 42), fill="blue")
    image.save(screenshot)
    image.crop((10, 10, 20, 20)).save(element)
    image.crop((0, 0, 40, 50)).save(context)
    selector = ReplaySelector(
        window={"processName": "EDRClient.exe"}, control={"name": "重复图标"},
        visual={"template": str(element), "elementSha256": "sha256:" + "a" * 64,
                "contextTemplate": str(context), "contextSha256": "sha256:" + "b" * 64,
                "contextOffset": [10, 10], "redacted": True},
    )
    matcher = PillowTemplateMatcher(scales=(1.0,))
    assert (10, 10, 20, 20) not in {
        candidate.rect for candidate in matcher(str(element), {"screenshot_path": str(screenshot)})
    }
    resolver = SafeVisualResolver(matcher,
                                  template_verifier=lambda _path, _visual: True)
    observation = {"screenshot_path": str(screenshot), "screenshot_scope": "window",
                   "screenshot_redacted": True,
                   "active_window": {"rect": [0, 0, 1600, 60]}}

    result = resolver.resolve("gui.click", selector, observation)

    assert result.args["x"] == 15 and result.args["y"] == 15


def test_context_template_disambiguates_repeated_element_hits():
    selector = ReplaySelector(
        window={"processName": "EDRClient.exe"}, control={"name": "重复图标"},
        visual={"template": "element.png", "elementSha256": "sha256:" + "a" * 64,
                "contextTemplate": "context.png", "contextSha256": "sha256:" + "b" * 64,
                "contextOffset": [10, 10], "redacted": True},
    )
    observation = {
        "snapshot_id": "OBS-1", "screenshot_scope": "window", "screenshot_redacted": True,
        "active_window": {"rect": [0, 0, 200, 100]},
        "window_identity": {"native_id": "7"},
    }
    def matcher(template, _observation):
        if template == "element.png":
            return [VisualCandidate((20, 30, 40, 50), 0.99),
                    VisualCandidate((120, 30, 140, 50), 0.99)]
        return [VisualCandidate((110, 20, 160, 70), 0.98)]
    resolver = SafeVisualResolver(matcher, template_verifier=lambda _path, _visual: True)
    resolved = resolver.resolve("gui.click", selector, observation)
    assert resolved.args["x"] == 130 and resolved.args["y"] == 40
    evidence = resolver.collect_evidence(selector, observation)
    assert len(evidence) == 1 and evidence[0]["rect"] == (120, 30, 140, 50)


def test_equal_context_matches_do_not_choose_a_repeated_icon():
    selector = ReplaySelector(
        window={"processName": "EDRClient.exe"}, control={"name": "重复图标"},
        visual={"template": "element.png", "elementSha256": "sha256:" + "a" * 64,
                "contextTemplate": "context.png", "contextSha256": "sha256:" + "b" * 64,
                "contextOffset": [10, 10], "redacted": True},
    )

    def matcher(template, _observation):
        if template == "element.png":
            return [VisualCandidate((20, 30, 40, 50), 0.99),
                    VisualCandidate((120, 30, 140, 50), 0.99)]
        return [VisualCandidate((10, 20, 60, 70), 0.98),
                VisualCandidate((110, 20, 160, 70), 0.98)]

    resolver = SafeVisualResolver(matcher, template_verifier=lambda _path, _visual: True)
    observation = {"screenshot_scope": "window", "screenshot_redacted": True,
                   "active_window": {"rect": [0, 0, 200, 100]}}
    with pytest.raises(StepMaterializationError) as exc:
        resolver.resolve("gui.click", selector, observation)
    assert exc.value.code == "visual_match_ambiguous"


def test_context_digest_failure_blocks_even_if_element_match_is_unique():
    selector = ReplaySelector(
        window={"processName": "EDRClient.exe"}, control={"name": "按钮"},
        visual={"template": "element.png", "elementSha256": "sha256:" + "a" * 64,
                "contextTemplate": "context.png", "contextSha256": "sha256:" + "b" * 64,
                "contextOffset": [10, 10], "redacted": True},
    )
    resolver = SafeVisualResolver(
        lambda _path, _obs: [VisualCandidate((20, 30, 40, 50), 0.99)],
        template_verifier=lambda path, _visual: path != "context.png",
    )
    observation = {"screenshot_scope": "window", "screenshot_redacted": True,
                   "active_window": {"rect": [0, 0, 200, 100]}}
    with pytest.raises(StepMaterializationError, match="context template"):
        resolver.resolve("gui.click", selector, observation)


def test_visual_click_anchor_stays_inside_exclusive_template_edges():
    selector = ReplaySelector(
        window={"processName": "EDRClient.exe"}, control={"name": "按钮"},
        visual={"template": "element.png", "elementSha256": "sha256:" + "a" * 64,
                "redacted": True,
                "relativePoint": [1.0, 1.0]},
    )
    observation = {"screenshot_scope": "window", "screenshot_redacted": True,
                   "active_window": {"rect": [100, 200, 140, 240]}}
    resolver = SafeVisualResolver(
        lambda _path, _obs: [VisualCandidate((110, 210, 120, 220), 0.99)],
        template_verifier=lambda _path, _visual: True,
    )
    resolved = resolver.resolve("gui.click", selector, observation)
    assert resolved.args["x"] == 19
    assert resolved.args["y"] == 19


def test_visual_click_anchor_rejects_boolean_coordinates():
    with pytest.raises(RecordingModelError) as exc:
        ReplaySelector(
            window={}, control={"name": "按钮"},
            visual={"template": "element.png", "elementSha256": "sha256:" + "a" * 64,
                    "redacted": True, "relativePoint": [True, 0.5]},
        )
    assert exc.value.code == "visual_relative_point_invalid"


@pytest.mark.parametrize("confidence", [float("nan"), float("inf"), -0.1, 1.1, True])
def test_visual_only_replay_rejects_invalid_match_confidence(confidence):
    selector = ReplaySelector(
        window={"processName": "EDRClient.exe"}, control={"name": "按钮"},
        visual={"template": "element.png", "elementSha256": "sha256:" + "a" * 64,
                "redacted": True},
    )
    observation = {"screenshot_scope": "window", "screenshot_redacted": True,
                   "active_window": {"rect": [0, 0, 40, 40]}}
    resolver = SafeVisualResolver(
        lambda _path, _obs: [VisualCandidate((10, 10, 20, 20), confidence)],
        template_verifier=lambda _path, _visual: True,
    )
    with pytest.raises(StepMaterializationError) as exc:
        resolver.resolve("gui.click", selector, observation)
    assert exc.value.code == "visual_match_low_confidence"
    assert resolver.collect_evidence(selector, observation) == ()
