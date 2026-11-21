"""顯示卡子程式裡的紀錄：每一步實際用的裝置與模型，各印一行 [device]，報告直接引用，不從顯示卡探測結果推定。

只包一層印訊息，不改行為。sandbox.py 把 `import child_hook` 插在子程式啟動命令前面（在 gpu_worker.serve 之前）。
每一行的格式是「[device] <項目>：<說明> 實際=<cuda|cpu|...>」；baseline.gpu_used() 只看「實際=」，
同一個項目以最後一行為準（例如 Whisper 先建 CUDA 模型、失敗後重建 CPU 模型，最後是 CPU）。
- <工作> onnx <模型檔>：ONNX session 建好後實際啟用的 provider（第一個）；執行中 onnxruntime 自己改用別的
  provider 時（run() 失敗後 set_providers 重試，log 是「Falling back to ...」）再記一行「執行中改用」。
  工作名稱跟著目前的工作走，所以 Whisper 的 VAD session 會記成 whisper，不會誤標成 separate；
- separate 模型 <檔名>：audio-separator 載入模型時的 torch_device 與要求的 provider（Roformer 等 PyTorch 模型
  的「實際」看 torch_device，MDX 等 ONNX 模型看上面的 onnx 那行）；
- pitch torchcrepe：torchcrepe.predict 收到的 model、device；
- whisper 模型：每次建立 faster_whisper.WhisperModel 都記 device、compute_type。
"""

import os

import gpu_worker as G

TASK = {"name": "?"}  # 目前子程式在做的工作


def _say(msg: str) -> None:
    print(f"[device] {msg}", flush=True)


def _kind(dev) -> str:
    """裝置的種類：cuda、cpu、dml……（cuda:0 → cuda）。"""
    s = str(dev).lower()
    for k, v in (("cudaexecutionprovider", "cuda"), ("dmlexecutionprovider", "dml"), ("cpuexecutionprovider", "cpu")):
        if k in s:
            return v
    return s.split(":")[0] or "?"


def _source(P) -> None:
    """第一個工作時印出子程式載入的 pipeline 路徑（報告檢查它在沙盒裡）。"""
    if not getattr(P, "_leihu_source", False):
        print(f"[source] pipeline {P.__file__}", flush=True)
        P._leihu_source = True


def _hook_onnx() -> None:
    import onnxruntime as ort

    if getattr(ort, "_leihu_hooked", False):
        return
    base = ort.InferenceSession

    class Session(base):
        def __init__(self, *x, **y):
            super().__init__(*x, **y)
            src = x[0] if x else y.get("path_or_bytes")
            self._leihu_key = f"{TASK['name']} onnx {os.path.basename(src) if isinstance(src, str) else '（記憶體裡的模型）'}"
            self._leihu_prov = self.get_providers()
            _say(f"{self._leihu_key}：要求 {y.get('providers')}、實際 {self._leihu_prov} 實際={_kind(self._leihu_prov[0])}")

        def run(self, *x, **y):
            out = super().run(*x, **y)
            now = self.get_providers()
            if now != self._leihu_prov:  # onnxruntime 執行失敗後自己 set_providers 改用別的
                self._leihu_prov = now
                _say(f"{self._leihu_key}：執行中改用 {now} 實際={_kind(now[0])}")
            return out

    ort.InferenceSession = Session
    ort._leihu_hooked = True


def _separate(orig):
    def run(P, progress, model, *a, **k):
        TASK["name"] = "separate"
        _source(P)
        _hook_onnx()
        from audio_separator.separator import Separator

        if not getattr(Separator, "_leihu_hooked", False):
            load = Separator.load_model

            def load_model(self, *x, **y):
                r = load(self, *x, **y)
                name = str(y.get("model_filename") or (x[0] if x else ""))
                actual = "" if name.endswith(".onnx") else f" 實際={_kind(self.torch_device)}"  # ONNX 模型看 onnx 那行
                _say(f"separate 模型 {name}：torch={self.torch_device}、要求 onnx={self.onnx_execution_provider}{actual}")
                return r

            Separator.load_model = load_model
            Separator._leihu_hooked = True
        return orig(P, progress, model, *a, **k)

    return run


def _pitch(orig):
    def run(P, progress, *a, **k):
        TASK["name"] = "pitch"
        _source(P)
        import torchcrepe

        if not getattr(torchcrepe, "_leihu_hooked", False):
            predict = torchcrepe.predict

            def hooked(*x, **y):
                model = x[5] if len(x) > 5 else y.get("model")
                _say(f"pitch torchcrepe：model={model}、device={y.get('device')} 實際={_kind(y.get('device'))}")
                return predict(*x, **y)

            torchcrepe.predict = hooked
            torchcrepe._leihu_hooked = True
        return orig(P, progress, *a, **k)

    return run


def _whisper(orig):
    def run(P, progress, *a, **k):
        TASK["name"] = "whisper"
        _source(P)
        _hook_onnx()  # Whisper 的 VAD 也用 ONNX：記成 whisper onnx
        import faster_whisper

        if not getattr(faster_whisper.WhisperModel, "_leihu_hooked", False):
            init = faster_whisper.WhisperModel.__init__

            def hooked(self, *x, **y):
                init(self, *x, **y)
                inner = getattr(self, "model", None)
                dev = getattr(inner, "device", "?")
                _say(f"whisper 模型：device={dev}、compute_type={getattr(inner, 'compute_type', '?')}"
                     f"（要求 {y.get('device')}） 實際={_kind(dev)}")

            faster_whisper.WhisperModel.__init__ = hooked
            faster_whisper.WhisperModel._leihu_hooked = True
        return orig(P, progress, *a, **k)

    return run


for _name, _wrap in (("separate", _separate), ("pitch", _pitch), ("whisper", _whisper)):
    if _name in G.TASKS:
        G.TASKS[_name] = _wrap(G.TASKS[_name])
    else:
        _say(f"{_name}：gpu_worker.TASKS 裡沒有這一步，沒有記錄裝置")
