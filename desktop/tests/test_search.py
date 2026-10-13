"""Offline tests for Traditional/Simplified-insensitive local searches."""

from __future__ import annotations

from desktop.search import normalize_search_text, search_matches


def test_traditional_and_simplified_names_share_a_search_key() -> None:
    for traditional, simplified in (
        ("台灣頻道", "台湾频道"),
        ("器材交流群組", "器材交流群组"),
        ("學習影片", "学习影片"),
    ):
        assert normalize_search_text(traditional) == normalize_search_text(simplified)
        assert search_matches(traditional, simplified)
        assert search_matches(simplified, traditional)


def test_search_is_unicode_casefolded_and_keeps_original_values_unchanged() -> None:
    original = "Channel:ABC-123"
    assert search_matches("channel:abc-123", original)
    assert search_matches("CHANNEL:ABC", original)
    assert original == "Channel:ABC-123"


def test_empty_query_matches_and_multiple_fields_are_supported() -> None:
    assert search_matches("", "任何內容")
    assert search_matches("群组", "台灣頻道", "器材交流群組")
    assert not search_matches("不存在", "台灣頻道", "器材交流群組")


def test_search_preserves_non_bmp_characters_and_embedded_nuls() -> None:
    expected = {
        "😀台灣": "😀台湾",
        "台😀灣": "台😀湾",
        "台灣😀": "台湾😀",
        "𠀀台灣": "𠀀台湾",
        "台灣𠀀": "台湾𠀀",
        "台\x00灣😀": "台\x00湾😀",
    }
    for value, wanted in expected.items():
        assert normalize_search_text(value) == wanted
        assert search_matches(value, value)


def test_windows_nls_covers_tai_variant_and_nfkc() -> None:
    assert normalize_search_text("台灣") == normalize_search_text("臺灣")
    assert normalize_search_text("ＡＢＣ１２３") == "abc123"
