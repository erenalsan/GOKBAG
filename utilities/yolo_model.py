import os
# Idle inference threads sleep instead of spinning between detections; must be set before torch loads
os.environ.setdefault("OMP_WAIT_POLICY", "PASSIVE")
import torch
from ultralytics import YOLO

# Measured on a 12-thread Ryzen 5 5500U: 4 threads infer as fast as 12 at half the CPU
torch.set_num_threads(4)

class YOLOModel:
    def __init__(self):
        self.model = YOLO("yolo26n.pt")

    def predict(self, img):
        # classes=[0]: only people are used downstream; verbose=False stops per-frame log spam
        results = self.model.predict(img, classes=[0], verbose=False)
        return results
