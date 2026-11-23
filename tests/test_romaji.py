"""static/app.js 的 roma2hira（2.3.5：點詞輸入羅馬拼音改讀音）。用 node 執行從 app.js 取出的那一段；沒有 node 就略過。"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
node = shutil.which("node")


def _roma2hira(words: list[str]) -> list[str]:
    js = (ROOT / "static" / "app.js").read_text("utf-8")
    start = js.index("const ROMA = ")
    end = js.index("\n}\n", js.index("function roma2hira(")) + 3
    code = js[start:end] + f"\nconsole.log(JSON.stringify({json.dumps(words)}.map(roma2hira)));\n"
    r = subprocess.run([node, "-e", code], capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(r.stdout)


@pytest.mark.skipif(not node, reason="沒有 node")
def test_nn_before_vowel_keeps_next_syllable():
    """Codex r5-7：nn 一律吃掉兩個字元，onna 變成「おんあ」、konnichiha 變成「こんいちは」，還會存成錯的讀音。"""
    words = ["onna", "minna", "konnichiha", "shinnen", "kanna", "hon", "honn", "kan'i", "kani", "nnaka"]
    assert _roma2hira(words) == ["おんな", "みんな", "こんにちは", "しんねん", "かんな", "ほん", "ほん", "かんい", "かに",
                                 "んなか"]
