from ultralytics import YOLO

class YOLOModel:
    def __init__(self):
        self.model = YOLO("yolo26n.pt")

    def predict(self, img):
        # classes=[0]: only people are used downstream; verbose=False stops per-frame log spam
        results = self.model.predict(img, classes=[0], verbose=False)
        return results
