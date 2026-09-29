import re
from pathlib import Path

ROOT = Path(__file__).parents[3]


def luminance(hex_color: str) -> float:
    channels = [int(hex_color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(a: str, b: str) -> float:
    lighter, darker = sorted((luminance(a), luminance(b)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def test_shell_has_accessible_structural_floor() -> None:
    source = (ROOT / "frontend" / "src" / "main.tsx").read_text()
    css = (ROOT / "frontend" / "src" / "index.css").read_text()
    assert 'href="#content"' in source and 'id="content"' in source
    assert "Experiência demonstrativa. Nenhuma compra real." in source
    assert "aria-label" in source and "<main" in source and "<h1" in source
    assert ":focus-visible" in css
    assert "prefers-reduced-motion: reduce" in css
    assert "forced-colors: active" in css
    assert "min-width: 320px" in css
    assert not re.search(r"https?://", css)
    assert contrast("#f4f2ff", "#080a12") >= 4.5
    assert contrast("#b8bed2", "#080a12") >= 4.5
    assert contrast("#090311", "#ffd44a") >= 4.5
