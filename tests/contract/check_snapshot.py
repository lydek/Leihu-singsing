"""用目前工作樹的程式（serve.py，要 S28）跑契約案例兩次，都要和 snapshots/contract.json 相同。

快照是用裝了推論套件的內嵌 Python 錄的（/api/status 的 modules 會列出 crepe、uvr、whisper），
uv 的測試環境沒有這些套件，所以 pytest 不收集 test_contract_snapshot（pyproject.toml 的 addopts），改用這支：

  & "$env:LOCALAPPDATA\\LeihuLiantai\\python\\python.exe" tests\\contract\\check_snapshot.py

不會覆寫快照；要更新快照照 docs/refactor-port-plan.md 3.7（先登記 RULES，再用 compare_upstream.py --record）。
"""

import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parents[1])]  # 內嵌 Python（._pth）不會自動加入腳本所在資料夾
import test_contract as T  # noqa: E402

for i in range(2):
    with tempfile.TemporaryDirectory(prefix="leihu-snap-") as d:
        result = T.run_contract(Path(d))
    blocked = {k: v for rec in result.values() for k, v in rec["files"].items() if "net_blocked" in k}
    if blocked:
        sys.exit(f"有案例對外連線：{blocked}")
    T.compare_snapshot(result, T.SNAPSHOT)  # 不同就 AssertionError，結束碼不是 0
    print(f"第 {i + 1} 次：{len(result)} 個案例和快照相同", flush=True)
