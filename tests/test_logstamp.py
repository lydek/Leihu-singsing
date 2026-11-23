"""fork：logs/app.log 每一行開頭加時間（leihu/logstamp.py），成功的 POST /api/overlay 不記。"""

from __future__ import annotations

import io
import logging
import re
import sys

STAMP = re.compile(r"^\d\d-\d\d \d\d:\d\d:\d\d ")


def test_stamps_each_line_once():
    from leihu.logstamp import Stamped

    buf = io.StringIO()
    s = Stamped(buf)
    print("[gpu] 開啟", file=s)          # print 分兩次寫：文字、換行
    s.write("a\nb\n")
    s.write("沒有換行")
    s.write("結尾\n")
    lines = buf.getvalue().split("\n")[:-1]
    assert len(lines) == 4
    assert all(STAMP.match(x) for x in lines)
    assert [STAMP.sub("", x) for x in lines] == ["[gpu] 開啟", "a", "b", "沒有換行結尾"]


def test_progress_carriage_return_not_stamped():
    """進度條用 \\r 覆蓋同一行：只有整行開頭有時間。"""
    from leihu.logstamp import Stamped

    buf = io.StringIO()
    s = Stamped(buf)
    s.write("10%\r")
    s.write("50%\r100%\n")
    out = buf.getvalue()
    assert STAMP.match(out) and len(STAMP.findall(out)) == 1
    assert STAMP.sub("", out) == "10%\r50%\r100%\n"


def test_delegates_to_stream(tmp_path):
    """gpu_worker 把 sys.stdout 交給子程式（Popen 用 fileno），其他屬性要轉給原本的串流。"""
    from leihu.logstamp import Stamped

    with open(tmp_path / "app.log", "w", encoding="utf-8") as f:
        s = Stamped(f)
        assert s.fileno() == f.fileno()
        assert s.encoding == f.encoding
        s.write("x\n")
        s.flush()
    assert STAMP.match((tmp_path / "app.log").read_text("utf-8"))


def test_bytes_rejected_like_text_stream():
    """click.echo 用 write(b"") 判斷是不是二進位串流；要和原本的文字串流一樣丟 TypeError，
    不然 click 會改寫 bytes 進來（契約沙盒啟動時 TypeError: endswith first arg must be bytes）。"""
    import click
    import pytest

    from leihu.logstamp import Stamped

    buf = io.StringIO()
    s = Stamped(buf)
    with pytest.raises(TypeError):
        s.write(b"")
    click.echo("hello", file=s)
    assert STAMP.sub("", buf.getvalue()) == "hello\n"


def test_install_wraps_once(monkeypatch):
    from leihu import logstamp

    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    logstamp.install()
    logstamp.install()
    assert isinstance(sys.stdout, logstamp.Stamped) and not isinstance(sys.stdout._stream, logstamp.Stamped)
    assert isinstance(sys.stderr, logstamp.Stamped)


def test_quiet_polls_overlay_post():
    from leihu.web import _QuietPolls

    def rec(msg):
        return logging.LogRecord("werkzeug", logging.INFO, "", 0, msg, None, None)

    f = _QuietPolls()
    assert not f.filter(rec('127.0.0.1 - - [23/Nov/2026 23:10:40] "POST /api/overlay HTTP/1.1" 200 -'))
    assert f.filter(rec('127.0.0.1 - - [23/Nov/2026 23:10:40] "POST /api/overlay HTTP/1.1" 400 -'))   # 失敗的照樣記
    assert f.filter(rec('127.0.0.1 - - [23/Nov/2026 23:10:40] "POST /api/songs HTTP/1.1" 200 -'))
