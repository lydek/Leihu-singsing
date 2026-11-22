"""用目前工作樹的程式（serve.py，要 S28）跑契約案例兩次，都要和 snapshots/contract.json 相同。

快照是用裝了推論套件的內嵌 Python 錄的（/api/status 的 modules 會列出 crepe、uvr、whisper），
uv 的測試環境沒有這些套件，所以 pytest 不收集 test_contract_snapshot（pyproject.toml 的 addopts），改用這支：

  & "$env:LOCALAPPDATA\\LeihuLiantai\\python\\python.exe" tests\\contract\\check_snapshot.py

不會覆寫快照；要更新快照照 docs/refactor-port-plan.md 3.7（先登記 RULES，再用 compare_upstream.py --record）。
"""

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parents[1])]  # 內嵌 Python（._pth）不會自動加入腳本所在資料夾
import test_contract as T  # noqa: E402

for k in ("LIANTAI_SNAPSHOT_UPDATE", "LIANTAI_SNAPSHOT_REBASE"):
    if os.environ.get(k):  # compare_snapshot 看到這兩個變數會直接覆寫快照，這支只比對
        sys.exit(f"{k} 有設：這支只比對、不更新快照。先清掉這個環境變數再執行")

for i in range(2):
    with tempfile.TemporaryDirectory(prefix="leihu-snap-") as d:
        result = T.run_contract(Path(d))
    blocked = {k: v for rec in result.values() for k, v in rec["files"].items() if "net_blocked" in k}
    if blocked:
        sys.exit(f"有案例對外連線：{blocked}")
    T.compare_snapshot(result, T.SNAPSHOT)  # 不同就 AssertionError，結束碼不是 0
    print(f"第 {i + 1} 次：{len(result)} 個案例和快照相同", flush=True)

# 路由涵蓋（同 test_contract_snapshot）：app 在這個行程裡 import，資料放暫存資料夾、不跑背景工作
with tempfile.TemporaryDirectory(prefix="leihu-routes-") as d:
    os.environ.update(LIANTAI_DATA=d, LIANTAI_NO_BACKGROUND="1", LIANTAI_NO_JOBS="1", LIANTAI_NO_SPAWN="1")
    import app

    missing, n = T.uncovered_routes(app)
if missing or n != 94:
    sys.exit(f"路由涵蓋不完整：共 {n} 條（應為 94），沒有案例的 {sorted(missing)}")
print(f"路由涵蓋：{n} 條都有案例", flush=True)
