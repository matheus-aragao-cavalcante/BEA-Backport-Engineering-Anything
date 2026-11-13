"""Tests for the name-based heuristics of bea.analyze (settings and engine guesses)."""

from __future__ import annotations

import pytest

from bea.analyze import classify_setting, guess_engine, scan_config_text


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("r.ScreenPercentage", "resolution"),
        ("ResolutionScale", "resolution"),
        ("render_scale", "resolution"),
        ("ScreenWidth", "resolution"),
        ("DynamicResolutionEnabled", "resolution"),
        ("MaxFPS", "frame_rate"),
        ("t.MaxFPS", "frame_rate"),
        ("fps_limit", "frame_rate"),
        ("VSyncInterval", "frame_rate"),
        ("vsync", "frame_rate"),
        ("TargetFrameRate", "frame_rate"),
        ("ShadowQuality", "shadows"),
        ("shadow_map_size", "shadows"),
        ("LODBias", "level_of_detail"),
        ("r.ViewDistanceScale", "level_of_detail"),
        ("draw-distance", "level_of_detail"),
        ("TextureQuality", "textures"),
        ("r.Streaming.PoolSize", "textures"),
        ("AnisotropicFiltering", "textures"),
        ("MSAA", "post_processing"),
        ("motionBlur", "post_processing"),
        ("AmbientOcclusionQuality", "post_processing"),
        ("HeapSize", "memory"),
        # Words that merely contain a keyword must not match.
        ("ExplodeRadius", None),
        ("Melody", None),
        ("Upload", None),
        ("Presets", None),
        ("PlayerName", None),
        ("Volume", None),
        ("language", None),
    ],
)
def test_classify_setting_by_key(key: str, expected: str | None) -> None:
    assert classify_setting(key) == expected


def test_classify_setting_by_dimensions_value() -> None:
    assert classify_setting("Mode", "1920x1080") == "resolution"
    assert classify_setting("Mode", "1280 X 720") == "resolution"
    assert classify_setting("Mode", "16x16") is None  # too small to be a screen size
    assert classify_setting("Mode", "version 12x") is None


def test_scan_ini_style_text() -> None:
    text = "\n".join(
        [
            "; graphics settings",
            "[Rendering]",
            "ResolutionScale=100",
            "ShadowQuality = High",
            "# MaxFPS=30 (commented out)",
            "MaxFPS=60",
            "PlayerName=Hero",
            "WindowMode: 1920x1080",
        ]
    )
    hits = scan_config_text(text)
    assert [(hit["line"], hit["key"], hit["value"], hit["category"]) for hit in hits] == [
        (3, "ResolutionScale", "100", "resolution"),
        (4, "ShadowQuality", "High", "shadows"),
        (6, "MaxFPS", "60", "frame_rate"),
        (8, "WindowMode", "1920x1080", "resolution"),
    ]


def test_scan_json_and_xml_text() -> None:
    json_text = '{\n  "video": {"maxFps": 60, "vsync": true},\n  "audio": {"volume": 0.8},\n  "shadowQuality": "high"\n}'
    hits = scan_config_text(json_text)
    assert [(hit["line"], hit["key"], hit["value"]) for hit in hits] == [
        (2, "maxFps", "60"),
        (2, "vsync", "true"),
        (4, "shadowQuality", "high"),
    ]
    xml_text = '<settings>\n  <RenderScale>0.75</RenderScale>\n  <option name="fps" value="30"/>\n</settings>'
    xml_hits = scan_config_text(xml_text)
    assert [(hit["line"], hit["key"], hit["value"], hit["category"]) for hit in xml_hits] == [
        (2, "RenderScale", "0.75", "resolution"),
    ]


def test_scan_skips_overlong_lines_and_truncates_values() -> None:
    long_value = "x" * 500
    hits = scan_config_text(f"ShadowQuality={long_value}\nMaxFPS=" + "9" * 5000)
    assert len(hits) == 1
    assert hits[0]["key"] == "ShadowQuality" and len(hits[0]["value"]) == 120


def test_guess_engine() -> None:
    unity = guess_engine(
        ["Data/globalgamemanagers", "Data/sharedassets0.assets", "Data/level0", "x.bin"]
    )
    assert unity["guess"] == "unity" and unity["confidence"] == "inferred"
    assert unity["candidates"][0]["files"] == 2
    assert unity["candidates"][0]["evidence"] == [
        "Data/globalgamemanagers",
        "Data/sharedassets0.assets",
    ]

    unreal = guess_engine(
        ["Game/Content/Paks/Game-Switch.pak", "Game/Content/Paks/Game-Switch.ucas"]
    )
    assert unreal["guess"] == "unreal"

    mixed = guess_engine(["a.bntx", "b.bfres", "c.szs", "Content/x.xnb"])
    assert mixed["guess"] == "nintendoware"
    assert [candidate["engine"] for candidate in mixed["candidates"]] == [
        "nintendoware",
        "monogame",
    ]

    assert guess_engine(["readme.txt", "data/file.bin"])["guess"] is None
    assert guess_engine([])["candidates"] == []
