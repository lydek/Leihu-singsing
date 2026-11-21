"""pyproject.toml 的 test 依賴群組和 requirements-win.txt（使用者端安裝用）版本不能漂移。"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()   # PEP 503


def _pins(lines) -> dict[str, str]:
    """只收 `名稱==版本`；沒有釘版本的（例如 yt-dlp）略過。"""
    out = {}
    for line in lines:
        line = line.split("#", 1)[0].strip()
        m = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([^\s;]+)", line)
        if m:
            out[_norm(m[1])] = m[2]
    return out


def test_pins_match_requirements_win():
    req = _pins((ROOT / "requirements-win.txt").read_text("utf-8").splitlines())
    group = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["dependency-groups"]["test"]
    pins = _pins(group)
    # 名稱打錯會讓比對悄悄略過，所以 test 群組釘了版本的套件都必須出現在 requirements-win.txt
    assert not pins.keys() - req.keys(), f"requirements-win.txt 沒有這些套件：{sorted(pins.keys() - req.keys())}"
    diff = {n: (pins[n], req[n]) for n in pins if pins[n] != req[n]}
    assert not diff, f"版本不一致（pyproject、requirements-win）：{diff}"


def test_pins_parser():
    assert _pins(["Flask==3.1.3  # x", "yt-dlp", "sudachidict_small==1", "# c"]) == {
        "flask": "3.1.3", "sudachidict-small": "1"}
