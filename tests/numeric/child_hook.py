"""顯示卡子程式裡的紀錄：每一步實際用的裝置與模型，各印一行 [device]，報告直接引用，不從顯示卡探測結果推定。

只包一層印訊息，不改行為。sandbox.py 把 `import child_hook` 插在子程式啟動命令前面（在 gpu_worker.serve 之前）。
- separate：audio-separator 載入模型後的 torch_device、onnx_execution_provider（實際要求的執行裝置）；
- pitch：torchcrepe.predict 收到的 model、device；
- whisper：pipeline._whisper_model 回傳的 CTranslate2 模型的 device、compute_type
  （t_whisper 遇到 RuntimeError 時會自己改用 CPU 重建模型，那條路徑沒經過這裡，由 log 的 Traceback 列為異常）。
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
        if not getattr(P, "_leihu_hooked", False):
            load = P._whisper_model

            def whisper_model(cfg):
                m = load(cfg)
                inner = getattr(m, "model", None)
                _say(f"whisper：device={getattr(inner, 'device', '?')}、compute_type={getattr(inner, 'compute_type', '?')}")
                return m

            P._whisper_model = whisper_model
            P._leihu_hooked = True
        return orig(P, progress, *a, **k)

    return run


for _name, _wrap in (("separate", _separate), ("pitch", _pitch), ("whisper", _whisper)):
    if _name in G.TASKS:
        G.TASKS[_name] = _wrap(G.TASKS[_name])
    else:
        _say(f"{_name}：gpu_worker.TASKS 裡沒有這一步，沒有記錄裝置")
