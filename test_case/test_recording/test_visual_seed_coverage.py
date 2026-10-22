"""Round-22 matcher seed-coverage regression (2026-10-21 live diagnosis).

The coarse-grid score-ordered seed cap let a dense family of near-identical
decoy tiles consume every seed, starving a unique target that ranked lower
on the coarse grid but matches perfectly once refined (live frame: true
target coarse rank 217/770, full score 1.0 never reached; replay refused
with visual_match_ambiguous although the correct position existed).

Fix under test: ``PillowTemplateMatcher`` additionally seeds the strict
local maxima of the coarse score grid (a unique exact match is always one,
a decoy plateau contributes at most one per tile). All safety gates stay:
min_confidence 0.92, min_margin 0.05, context_margin 0.03, redacted/window
checks, duplicate-icon ambiguity refusal, scale policy. No full-frame
brute force, no silent first-pick.
"""

from __future__ import annotations

import io

from PIL import Image, ImageDraw

import pytest

from agent.execution import StepMaterializationError
from agent.recording.pillow_matcher import PillowTemplateMatcher
from agent.recording.models import ReplaySelector
from agent.recording.visual import SafeVisualResolver

pytestmark = pytest.mark.unit


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _observation(frame: bytes, size) -> dict:
    return {
        "screenshot_bytes": frame,
        "screenshot_origin": [0, 0],
        "screenshot_scope": "window",
        "screenshot_redacted": True,
        "active_window": {"rect": [0, 0, *size]},
    }


def _selector(template, name="按钮"):
    return ReplaySelector(
        window={"processName": "EDRClient.exe"},
        control={"name": name},
        visual={"template": template, "elementSha256": "sha256:" + "a" * 64,
                "redacted": True},
    )


def _dense_frame():
    """480x300 window: 20 near-identical decoy tiles top-right + one unique
    target bottom-left (off the coarse grid, text-dense glyph)."""
    image = Image.new("RGB", (480, 300), (250, 251, 253))
    painter = ImageDraw.Draw(image)
    for i in range(4):
        for j in range(5):
            x, y = 192 + i * 48, 16 + j * 40
            painter.rectangle((x, y, x + 43, y + 33), fill=(238, 242, 248),
                               outline=(190, 200, 215))
            painter.rectangle((x + 10, y + 8, x + 33, y + 12), fill=(60, 80, 120))
            painter.rectangle((x + 12, y + 18, x + 30, y + 20), fill=(90, 100, 120))
            painter.rectangle((x + 10, y + 24, x + 33, y + 26), fill=(140, 150, 165))
    tx, ty = 14, 252
    painter.rectangle((tx, ty, tx + 43, ty + 33), fill=(238, 242, 248),
                      outline=(190, 200, 215))
    painter.ellipse((tx + 12, ty + 5, tx + 31, ty + 24), outline=(20, 40, 90), width=3)
    painter.line((tx + 13, ty + 28, tx + 32, ty + 28), fill=(20, 40, 90), width=3)
    painter.line((tx + 9, ty + 14, tx + 21, ty + 14), fill=(20, 40, 90), width=3)
    painter.line((tx + 23, ty + 14, tx + 35, ty + 14), fill=(20, 40, 90), width=3)
    return image, (tx, ty, tx + 44, ty + 34)


def test_unique_target_survives_dense_lookalike_cluster(tmp_path):
    """The round-21 defect: decoys outrank the target on the coarse grid.

    Pre-fix this failed (true position absent from candidates); the fix
    must refine the target, rank it first, and leave a margin above the
    0.05 gate so the resolver can dispatch."""
    image, target_rect = _dense_frame()
    frame_path = tmp_path / "frame.png"
    template_path = tmp_path / "template.png"
    Image.frombytes("RGB", image.size, image.tobytes()).save(frame_path)
    image.crop(target_rect).save(template_path)

    matches = PillowTemplateMatcher(scales=(1.0,))(str(template_path), {
        "screenshot_path": str(frame_path),
    })
    target = [candidate for candidate in matches if candidate.rect == target_rect]
    assert target, "unique target starved by dense decoy cluster"
    assert matches[0].rect == target_rect
    assert matches[0].confidence == 1.0
    if len(matches) > 1:
        assert matches[0].confidence - matches[1].confidence >= 0.05


def test_resolver_dispatches_unique_target_despite_cluster(tmp_path):
    """End-to-end: the resolver picks the unique target centre, not a decoy."""
    image, target_rect = _dense_frame()
    frame_path = tmp_path / "frame.png"
    template_path = tmp_path / "template.png"
    Image.frombytes("RGB", image.size, image.tobytes()).save(frame_path)
    image.crop(target_rect).save(template_path)

    resolver = SafeVisualResolver(
        PillowTemplateMatcher(scales=(1.0,)),
        template_verifier=lambda _path, _visual: True,
    )
    resolved = resolver.resolve("gui.click", _selector(str(template_path)), {
        "screenshot_path": str(frame_path),
        "screenshot_scope": "window",
        "screenshot_redacted": True,
        "active_window": {"rect": [0, 0, 480, 300]},
    })
    assert resolved.args["x"] == (target_rect[0] + target_rect[2]) // 2
    assert resolved.args["y"] == (target_rect[1] + target_rect[3]) // 2


def test_truly_repeated_target_refuses_click(tmp_path):
    """Counterexample: two pixel-identical targets (both score 1.0) must
    stay ambiguous after the fix — coverage seeds find both, the margin
    gate (0.05) still blocks, no click dispatched."""
    image = Image.new("RGB", (320, 60), (250, 251, 253))
    painter = ImageDraw.Draw(image)
    for x in (20, 200):
        painter.rectangle((x, 20, x + 19, 39), fill=(60, 80, 120))
        painter.rectangle((x + 4, 24, x + 14, 26), fill=(230, 235, 240))
    template_path = tmp_path / "template.png"
    image.crop((20, 20, 40, 40)).save(template_path)

    resolver = SafeVisualResolver(
        PillowTemplateMatcher(scales=(1.0,)),
        template_verifier=lambda _path, _visual: True,
    )
    with pytest.raises(StepMaterializationError) as exc:
        resolver.resolve("gui.click", _selector(str(template_path)),
                         _observation(_png(image), (320, 60)))
    assert exc.value.code == "visual_match_ambiguous"


def test_dense_family_template_refuses_click(tmp_path):
    """Counterexample: template is a member of a dense identical family —
    every instance scores 1.0, the resolver must refuse (no silent
    first-pick among many equal candidates)."""
    image = Image.new("RGB", (400, 80), (250, 251, 253))
    painter = ImageDraw.Draw(image)
    for x in (10, 60, 110, 160, 210, 260, 310, 360):
        painter.rectangle((x, 20, x + 19, 39), fill=(20, 40, 90))
        painter.ellipse((x + 4, 24, x + 15, 35), outline=(230, 235, 240), width=2)
    template_path = tmp_path / "template.png"
    image.crop((10, 20, 30, 40)).save(template_path)

    resolver = SafeVisualResolver(
        PillowTemplateMatcher(scales=(1.0,)),
        template_verifier=lambda _path, _visual: True,
    )
    with pytest.raises(StepMaterializationError) as exc:
        resolver.resolve("gui.click", _selector(str(template_path)),
                         _observation(_png(image), (400, 80)))
    assert exc.value.code == "visual_match_ambiguous"


def test_context_tie_drops_context_and_element_path_decides():
    """Counterexample: two context matches within the 0.03 gate drop the
    context support (existing behaviour) and the element path alone must
    still decide — no crash, no context-forced click."""
    selector = ReplaySelector(
        window={"processName": "EDRClient.exe"}, control={"name": "按钮"},
        visual={"template": "element.png", "elementSha256": "sha256:" + "a" * 64,
                "contextTemplate": "context.png", "contextSha256": "sha256:" + "b" * 64,
                "contextOffset": [10, 10], "redacted": True},
    )

    def matcher(template, _observation):
        if template == "element.png":
            from agent.recording.visual import VisualCandidate
            return [VisualCandidate((20, 30, 40, 50), 0.99)]
        from agent.recording.visual import VisualCandidate
        return [VisualCandidate((10, 20, 60, 70), 0.98),
                VisualCandidate((110, 20, 160, 70), 0.979)]

    resolver = SafeVisualResolver(matcher, template_verifier=lambda _path, _visual: True)
    observation = {"screenshot_scope": "window", "screenshot_redacted": True,
                   "active_window": {"rect": [0, 0, 200, 100]}}
    resolved = resolver.resolve("gui.click", selector, observation)
    assert resolved.args["x"] == 30 and resolved.args["y"] == 40


def test_target_at_supported_scale_still_resolves(tmp_path):
    """Counterexample: target rendered at 1.25x the template (no 1.0
    instance on screen). The matcher must find the scaled instance and the
    resolver accept the in-policy scale — no misaligned 1.0 first-pick."""
    # Template 30x30: dark 20x20 icon at (4,4) + light 12x3 bar at (8,8).
    # Frame renders the same icon at exactly 1.25x: 25x25 at (5,5)+origin,
    # bar 15x4 at (10,10)+origin; origin = (60,30).
    image = Image.new("RGB", (200, 120), (250, 251, 253))
    painter = ImageDraw.Draw(image)
    painter.rectangle((65, 35, 90, 60), fill=(60, 80, 120))
    painter.rectangle((70, 40, 85, 44), fill=(230, 235, 240))
    template = Image.new("RGB", (30, 30), (238, 242, 248))
    template_painter = ImageDraw.Draw(template)
    template_painter.rectangle((4, 4, 23, 23), fill=(60, 80, 120))
    template_painter.rectangle((8, 8, 19, 10), fill=(230, 235, 240))
    frame_path = tmp_path / "frame.png"
    template_path = tmp_path / "template.png"
    image.save(frame_path)
    template.save(template_path)

    matches = PillowTemplateMatcher()(str(template_path), {
        "screenshot_path": str(frame_path),
    })
    assert matches, "scaled target not found"
    best = matches[0]
    assert best.scale == 1.25
    assert best.confidence >= 0.92
    # resolver accepts the in-policy scale
    resolver = SafeVisualResolver(PillowTemplateMatcher(),
                                   template_verifier=lambda _p, _v: True)
    resolved = resolver.resolve("gui.click", _selector(str(template_path)), {
        "screenshot_path": str(frame_path),
        "screenshot_scope": "window",
        "screenshot_redacted": True,
        "active_window": {"rect": [0, 0, 200, 120]},
    })
    assert 60 <= resolved.args["x"] <= 95
    assert 30 <= resolved.args["y"] <= 65


def test_local_maxima_cap_starves_target_is_safe_failure(tmp_path):
    """Guarantee bound for the local-maxima coverage pass (round-23).

    34 decoy tiles that each form a strict coarse-grid local maximum and
    all outscore the unique target's coarse neighbourhood: the local-max
    cap (max_candidates=32) and the score-ordered cap are both consumed by
    decoys, so the target is starved again (its exact position is absent
    from the candidates). The failure must stay SAFE: no decoy candidate
    may ever be dispatched — with 32 tied ~0.99 decoys the margin is 0 and
    the resolver refuses with visual_match_ambiguous.
    """
    width, height = 10, 20  # template size -> stride max(2, 20//8=2) = 2
    image = Image.new("RGB", (900, 60), (250, 251, 253))
    painter = ImageDraw.Draw(image)

    def tile(x, y, mark=False):
        painter.rectangle((x, y, x + 9, y + 19), fill=(238, 242, 248))
        painter.rectangle((x + 2, y + 4, x + 7, y + 9), fill=(20, 40, 90))
        painter.rectangle((x + 2, y + 12, x + 7, y + 13), fill=(230, 235, 240))
        if mark:  # 2x2 decoy mark -> decoys score just below the exact tile
            painter.rectangle((x + 7, y + 17, x + 8, y + 18), fill=(60, 60, 60))

    for i in range(34):
        tile(10 + i * 26, 10, mark=True)
    tile(881, 11)  # unique target, off the coarse grid (stride 2)
    target_rect = (881, 11, 891, 31)
    frame_path = tmp_path / "frame.png"
    template_path = tmp_path / "template.png"
    image.save(frame_path)
    image.crop(target_rect).save(template_path)

    matches = PillowTemplateMatcher(scales=(1.0,))(str(template_path), {
        "screenshot_path": str(frame_path),
    })
    assert all(candidate.rect != target_rect for candidate in matches), \
        "target expected to be starved beyond the local-maxima cap"
    assert matches, "decoys should dominate the candidates"

    resolver = SafeVisualResolver(
        PillowTemplateMatcher(scales=(1.0,)),
        template_verifier=lambda _path, _visual: True,
    )
    selector = ReplaySelector(
        window={"processName": "EDRClient.exe"}, control={"name": "目标"},
        visual={"template": str(template_path), "elementSha256": "sha256:" + "a" * 64,
                "redacted": True},
    )
    # no control tree: no text source can rescue a starved target
    with pytest.raises(StepMaterializationError) as exc:
        resolver.resolve("gui.click", selector, {
            "screenshot_path": str(frame_path), "screenshot_scope": "window",
            "screenshot_redacted": True, "active_window": {"rect": [0, 0, 900, 60]},
        })
    assert exc.value.code in {"visual_match_ambiguous", "visual_match_low_confidence"}


@pytest.mark.regression
def test_full_size_dense_frame_performance_bound(tmp_path):
    """Performance bound for the seed-coverage fix on a 922x602 window frame
    (the 11.26 live-desktop size) with 40 decoy tiles and the default 4
    scales. Pre-fix baseline on the real frame: element 46s / context 158s.
    Post-fix measured (2026-10-22): element 26.4s, context 116.7s,
    this synthetic 21.9s. The bound is 4x the measured synthetic run so CI
    jitter stays green while a true cost regression (e.g. brute force) is
    caught."""
    image, target_rect = _dense_frame()
    image = image.resize((922, 602), Image.LANCZOS)
    painter = ImageDraw.Draw(image)
    for i in range(8):
        for j in range(5):
            x, y = 260 + i * 112, 30 + j * 112
            painter.rectangle((x, y, x + 104, y + 97), fill=(238, 242, 248),
                               outline=(190, 200, 215))
            painter.rectangle((x + 20, y + 16, x + 86, y + 24), fill=(60, 80, 120))
            painter.rectangle((x + 24, y + 40, x + 80, y + 45), fill=(90, 100, 120))
            painter.rectangle((x + 20, y + 60, x + 86, y + 66), fill=(140, 150, 165))
            painter.ellipse((x + 38, y + 74, x + 66, y + 94), outline=(20, 40, 90), width=2)
    tx, ty = 30, 470
    painter.rectangle((tx, ty, tx + 104, ty + 97), fill=(238, 242, 248),
                      outline=(190, 200, 215))
    painter.ellipse((tx + 28, ty + 12, tx + 76, ty + 60), outline=(20, 40, 90), width=3)
    painter.line((tx + 22, ty + 72, tx + 82, ty + 72), fill=(20, 40, 90), width=3)
    painter.line((tx + 14, ty + 36, tx + 40, ty + 36), fill=(20, 40, 90), width=3)
    painter.line((tx + 64, ty + 36, tx + 90, ty + 36), fill=(20, 40, 90), width=3)
    target_rect = (tx, ty, tx + 105, ty + 98)
    frame_path = tmp_path / "frame.png"
    template_path = tmp_path / "template.png"
    image.save(frame_path)
    image.crop(target_rect).save(template_path)

    import time as _time
    matcher = PillowTemplateMatcher()
    t0 = _time.perf_counter()
    matches = matcher(str(template_path), {
        "screenshot_path": str(frame_path),
    })
    elapsed = _time.perf_counter() - t0
    assert matches[0].rect == target_rect and matches[0].confidence == 1.0
    assert elapsed < 90.0, f"matcher cost regression: {elapsed:.1f}s > 90s bound"


def test_out_of_policy_scale_candidate_is_rejected():
    """Counterexample: a candidate at scale 0.6 (outside 0.75..1.5) must be
    refused even at high confidence — no silent first-pick."""
    selector = _selector("template.png")
    from agent.recording.visual import VisualCandidate
    resolver = SafeVisualResolver(
        lambda _path, _obs: [VisualCandidate((10, 10, 20, 20), 0.99, scale=0.6)],
        template_verifier=lambda _path, _visual: True,
    )
    with pytest.raises(StepMaterializationError) as exc:
        resolver.resolve("gui.click", selector, _observation(b"", (40, 40)))
    assert exc.value.code == "visual_scale_unsupported"
