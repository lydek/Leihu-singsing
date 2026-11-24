"""從 pipeline.py 分出來的程式（docs/pipeline-split-plan.md）。

這裡有兩種檔案：
- 方式一：pipeline._load_part 載入 pipeline 的命名空間，等於那段程式寫在 pipeline.py 裡，不能單獨 import。
  哪些是方式一，看 pipeline.py 裡的 _load_part("名稱") 呼叫（執行時是 pipeline._PARTS）。
- 方式二：一般的模組，pipeline 轉出同樣的名稱（和 leihu/pitch.py 相同）。
"""
