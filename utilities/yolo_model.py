import importlib.util
import os
# Idle inference threads sleep instead of spinning between detections; must be set before torch loads
os.environ.setdefault("OMP_WAIT_POLICY", "PASSIVE")
import numpy as np
import torch
from ultralytics import YOLO

# Measured on a 12-thread Ryzen 5 5500U: 4 threads infer as fast as 12 at half the CPU
torch.set_num_threads(4)


def pick_device():
    """GOKBAG_DEVICE overrides (e.g. "cpu", "cuda:1", "mps"); otherwise the best available accelerator."""
    forced = os.environ.get("GOKBAG_DEVICE")
    if forced:
        return forced
    if torch.cuda.is_available():  # NVIDIA, or AMD with a ROCm build of PyTorch
        return "cuda:0"
    if torch.backends.mps.is_available():  # Apple silicon
        return "mps"
    return "cpu"


def device_name(device):
    if device.startswith("cuda"):
        return torch.cuda.get_device_name(torch.device(device))
    return {"mps": "Apple GPU", "cpu": "CPU"}.get(device, device)


class YOLOModel:
    def __init__(self, weights="yolo26n.pt"):
        self.device = pick_device()
        self.on_gpu = self.device != "cpu"
        # FP16 roughly doubles throughput on CUDA/ROCm GPUs; MPS and CPU stay in FP32
        self.half = self.device.startswith("cuda")
        self.engine = "PyTorch"
        if self.half:
            torch.backends.cudnn.benchmark = True  # picks the fastest kernels for our fixed frame size
            self.model = self.load_tensorrt(weights) or YOLO(weights)
        else:
            self.model = YOLO(weights)
        self.name = f"{device_name(self.device)} ({self.engine}{', FP16' if self.half else ''})"
        self.predict(np.zeros((480, 640, 3), dtype=np.uint8))  # warm-up so the first real frame isn't slow
        print(f"Person detection running on {self.name}")

    def load_tensorrt(self, weights):
        """TensorRT is the fastest way to run YOLO on NVIDIA. Used only when installed (pip install tensorrt);
        the engine is built once per machine and cached next to the weights."""
        if torch.version.hip or importlib.util.find_spec("tensorrt") is None:
            return None
        engine = os.path.splitext(weights)[0] + ".engine"
        try:
            if not os.path.exists(engine):
                print("Building TensorRT engine for this GPU (one time, can take a few minutes)...")
                YOLO(weights).export(format="engine", half=True, device=self.device, imgsz=640, verbose=False)
            model = YOLO(engine, task="detect")
            model.predict(np.zeros((480, 640, 3), dtype=np.uint8), device=self.device, half=True, verbose=False)
        except Exception as e:
            # e.g. an engine built for another GPU or TensorRT version
            print(f"TensorRT unavailable ({e}); using PyTorch FP16")
            return None
        self.engine = "TensorRT"
        return model

    def predict(self, img):
        # classes=[0]: only people are used downstream; verbose=False stops per-frame log spam
        return self.model.predict(img, classes=[0], device=self.device, half=self.half, verbose=False)
