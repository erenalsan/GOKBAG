import time
import cv2
import numpy as np


class FakeTello:
    """Stand-in for djitellopy.Tello so the app runs without a drone.

    Video comes from a webcam index ("0") or a video/image file path; it loops at the end.
    """

    def __init__(self, source="0"):
        self.source = int(source) if source.isdigit() else source
        self.cap = None
        self.frame = np.zeros((480, 640, 3), dtype=np.uint8)
        self.is_flying = False
        self.height = 0
        self.battery = 100.0
        self.yaw = 0.0
        self.temperature = 45.0
        self.link_lost = False  # set True to simulate losing the Wi-Fi link
        self.state = {}
        self.last_rc_time = time.monotonic()

    def connect(self):
        print("[SIM] connected")

    def streamon(self):
        self.cap = cv2.VideoCapture(self.source)
        if not self.cap.isOpened():
            print(f"[SIM] could not open video source {self.source!r}, showing a black frame")

    def streamoff(self):
        if self.cap is not None:
            self.cap.release()

    def get_frame_read(self):
        # shortcut: reads synchronously on each call, so video files play at GUI speed, not real time
        ok, frame = self.cap.read() if self.cap is not None else (False, None)
        if not ok and self.cap is not None:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = self.cap.read()
        if ok:
            self.frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)  # djitellopy>=2.5 gives RGB
        return self

    def takeoff(self):
        print("[SIM] takeoff...")
        time.sleep(2)  # real takeoff blocks for a few seconds too
        self.is_flying = True
        self.height = 80
        print("[SIM] airborne")

    def land(self):
        print("[SIM] landing...")
        time.sleep(2)
        self.is_flying = False
        self.height = 0
        print("[SIM] landed")

    def send_rc_control(self, lr, fb, ud, yv):
        now = time.monotonic()
        if self.is_flying:
            dt = now - self.last_rc_time
            self.height = max(20, self.height + ud * dt)
            self.yaw = (self.yaw + yv * dt + 180) % 360 - 180
        self.last_rc_time = now
        if lr or fb or ud or yv:
            print(f"[SIM] rc lr={lr} fb={fb} ud={ud} yaw={yv}")

    def get_battery(self):
        self.battery = max(0.0, self.battery - 0.001)
        return int(self.battery)

    def get_height(self):
        return int(self.height)

    def get_yaw(self):
        return int(self.yaw)

    def get_temperature(self):
        return self.temperature

    def get_current_state(self):
        # like djitellopy: a new dict per telemetry packet, none while the link is down
        if not self.link_lost:
            self.state = {"bat": int(self.battery), "h": int(self.height), "yaw": int(self.yaw)}
        return self.state

    def query_wifi_signal_noise_ratio(self):
        return "90"

    def emergency(self):
        print("[SIM] EMERGENCY: motors cut")
        self.is_flying = False
        self.height = 0

    def end(self):
        if self.is_flying:
            self.land()
        self.streamoff()
