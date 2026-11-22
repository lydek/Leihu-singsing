"""確認 test 依賴群組裝得起 app，路由數量也沒有被意外改動（HTTP API 是契約）。"""

from pathlib import Path


def test_app_imports_with_95_routes(app_module):
    # 含 /api/debug/stacks；契約快照的路由涵蓋（94 條）不含它
    rules = [r for r in app_module.app.url_map.iter_rules() if r.endpoint != "static"]
    assert len(rules) == 95


def test_installer_check_still_works(app_module):
    # installer.py 的「程式本體」檢查是 `import app; print(app.VERSION)`
    assert (Path(__file__).resolve().parents[1] / "VERSION").read_text("utf-8").strip() == app_module.VERSION
