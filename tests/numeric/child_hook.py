"""顯示卡子程式裡的紀錄：每一步實際用的裝置與模型，各印一行 [device]，報告直接引用，不從顯示卡探測結果推定。

只包一層印訊息，不改行為。sandbox.py 把 `import child_hook` 插在子程式啟動命令前面（在 gpu_worker.serve 之前）。
- separate：audio-separator 載入模型後的 torch_device、要求的 onnx provider，以及 ONNX session 建好後
  實際啟用的 provider（get_providers()；要求的沒啟用時 audio-separator 會印 warning，報告列為異常）；
- pitch：torchcrepe.predict 收到的 model、device；
- whisper：每次建立 faster_whisper.WhisperModel 都記 device、compute_type（含 pipeline 改用 CPU、
  t_whisper 遇到 RuntimeError 自己重建 CPU 模型的情況，所以退回 CPU 時會看到兩行）。
"""

import gpu_worker as G


def _say(msg: str) -> None:
    print(f"[device] {msg}", flush=True)


def _source(P) -> None:
    """第一個工作時印出子程式載入的 pipeline 路徑（報告檢查它在沙盒裡）。"""
    if not getattr(P, "_leihu_source", False):
        print(f"[source] pipeline {P.__file__}", flush=True)
        P._leihu_source = True


def _separate(orig):
    def run(P, progress, model, *a, **k):
        _source(P)
        from audio_separator.separator import Separator

        import onnxruntime as ort

        if not getattr(ort, "_leihu_hooked", False):
            base = ort.InferenceSession

            class Session(base):
                def __init__(self, *x, **y):
                    super().__init__(*x, **y)
                    _say(f"separate onnx session：要求 {y.get('providers')}、實際 {self.get_providers()}")

            ort.InferenceSession = Session
            ort._leihu_hooked = True
        if not getattr(Separator, "_leihu_hooked", False):
            load = Separator.load_model

            def load_model(self, *x, **y):
                r = load(self, *x, **y)
                name = y.get("model_filename") or (x[0] if x else "")
                _say(f"separate {name}：torch={self.torch_device}、onnx={self.onnx_execution_provider}")
                return r

            Separator.load_model = load_model
            Separator._leihu_hooked = True
        return orig(P, progress, model, *a, **k)

    return run


def _pitch(orig):
    def run(P, progress, *a, **k):
        _source(P)
        import torchcrepe

        if not getattr(torchcrepe, "_leihu_hooked", False):
            predict = torchcrepe.predict

            def hooked(*x, **y):
                model = x[5] if len(x) > 5 else y.get("model")
                _say(f"pitch：torchcrepe model={model}、device={y.get('device')}")
                return predict(*x, **y)

            torchcrepe.predict = hooked
            torchcrepe._leihu_hooked = True
        return orig(P, progress, *a, **k)

    return run


def _whisper(orig):
    def run(P, progress, *a, **k):
        _source(P)
        import faster_whisper

        if not getattr(faster_whisper.WhisperModel, "_leihu_hooked", False):
            init = faster_whisper.WhisperModel.__init__

            def hooked(self, *x, **y):
                init(self, *x, **y)
                inner = getattr(self, "model", None)
                _say(f"whisper 建立模型：device={getattr(inner, 'device', '?')}、"
                     f"compute_type={getattr(inner, 'compute_type', '?')}（要求 device={y.get('device')}）")

            faster_whisper.WhisperModel.__init__ = hooked
            faster_whisper.WhisperModel._leihu_hooked = True
        return orig(P, progress, *a, **k)

    return run


for _name, _wrap in (("separate", _separate), ("pitch", _pitch), ("whisper", _whisper)):
    if _name in G.TASKS:
        G.TASKS[_name] = _wrap(G.TASKS[_name])
    else:
        _say(f"{_name}：gpu_worker.TASKS 裡沒有這一步，沒有記錄裝置")
