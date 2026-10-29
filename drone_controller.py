import os
import sys
import time
import math
import signal
import subprocess
import threading
import traceback
import cv2
import numpy as np
from PIL import Image, ImageTk
import customtkinter
from tkinter import Label, Canvas
from utilities.yolo_model import YOLOModel
import utilities.coord_csv_module as ccsv

# GOKBAG_SIM=0 (webcam index) or GOKBAG_SIM=path/to/video runs without a drone
if os.environ.get("GOKBAG_SIM"):
    from utilities.fake_tello import FakeTello
    me = FakeTello(os.environ["GOKBAG_SIM"])
else:
    from djitellopy import Tello
    me = Tello()

GREEN, GREEN_HOVER, RED, RED_HOVER, AMBER, MUTED = "#2fa572", "#258a5e", "#d64545", "#b33838", "#e0a526", "gray60"

# Safety limits
MIN_TAKEOFF_BATTERY = 20   # % preflight check
LOW_BATTERY_WARNING = 30   # %
RTH_BATTERY = 20           # % flies back to the takeoff point by itself
AUTO_LAND_BATTERY = 10     # % lands wherever it is (report GCS04)
MAX_TEMPERATURE = 90       # °C preflight check
MAX_HEIGHT_CM = 400        # climbing is blocked above this
LINK_TIMEOUT = 2.0         # s without a telemetry packet = link lost (Tello sends ~10/s)

# Tracking (pixel errors -> rc values, -100..100)
YAW_KP, YAW_KD, YAW_DEADBAND = 0.4, 0.4, 20
UD_KP, UD_DEADBAND = 0.3, 30
FB_KP, TARGET_AREA_MIN, TARGET_AREA_MAX = 0.004, 8000, 25000
SEARCH_YAW = 20            # turn in place when nobody is visible (report MACS03)
SAR_CAPTURE_COOLDOWN = 3   # s between automatic search & rescue photos

# Return to home. Position is estimated in rc-units x seconds and the way back is flown with rc commands too,
# so the unknown rc->speed scale cancels out; accuracy is limited by drift, not calibration.
RTH_SPEED = 40             # rc forward value on the way home
RTH_ARRIVE = 30            # estimated distance at which it lands
RTH_HEADING_KP = 1.5       # degrees of heading error -> rc yaw
RTH_ALIGNED = 15           # degrees; flies forward only when roughly facing home

# Tk keysym -> (axis index in [lr, fb, ud, yv], sign)
KEY_BINDINGS = {
    "left": (0, -1), "right": (0, 1),
    "up": (1, 1), "down": (1, -1),
    "w": (2, 1), "s": (2, -1),
    "a": (3, -1), "d": (3, 1),
}
ALERT_COLORS = {"error": RED, "warn": AMBER, "info": GREEN}


def clamp(value, limit):
    return int(max(-limit, min(limit, value)))


def wrap_degrees(angle):
    return (angle + 180) % 360 - 180


class DroneController:
    def __init__(self, root=None):
        me.connect()
        me.streamon()
        self.root = root or customtkinter.CTk()
        self.root.title("GÖKBAĞ Ground Control")
        self.root.resizable(False, False)
        self.build_ui()

        self.yolo_enabled = True
        self.is_flying = False
        self.busy = False  # takeoff/land in progress on a worker thread
        # djitellopy matches replies to commands by order, so commands that wait for a reply must not overlap
        self.cmd_lock = threading.Lock()
        self.running = True
        self.manual_control_enabled = False
        self.current_mode = "operational"
        self.pressed_keys = set()
        self.auto_rc = [0, 0, 0, 0]
        self.searching = False
        self.prev_err_x = 0
        self.latest_frame = None
        self.detected_frame = None
        self.detections = []
        self.last_frame = None
        self.last_sar_capture = 0
        self.writer = None
        self.alerts = {}

        self.link_ok = True
        self.last_state = None
        self.last_state_time = time.monotonic()
        self.signal = None
        self.auto_landed = False
        self.returning = False
        self.rth_triggered = False

        self.width = 640
        self.height = 480
        self.min_area = 3000

        os.makedirs('captured_images', exist_ok=True)

        self.interval = 0.25
        self.speed = 50
        self.x, self.y, self.z = 500, 500, 0
        self.yaw0 = 0

        self.home = (self.x, self.y)
        self.points = [(self.x, self.y, self.z)]
        self.person_points = []
        self.yolo_model = YOLOModel()

        # Keyboard is read from this window, no separate pygame window needed.
        self.root.bind("<KeyPress>", self.on_key_press)
        self.root.bind("<KeyRelease>", self.on_key_release)
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)

    def build_ui(self):
        heading = customtkinter.CTkFont(size=13, weight="bold")
        big = customtkinter.CTkFont(size=24, weight="bold")
        small = customtkinter.CTkFont(size=11)

        # left: flight state bar, alerts, video, mini-map
        left = customtkinter.CTkFrame(self.root, corner_radius=12)
        left.grid(row=0, column=0, padx=(16, 8), pady=16, sticky="n")
        bar = customtkinter.CTkFrame(left, fg_color="transparent")
        bar.pack(fill="x", padx=14, pady=(10, 6))
        self.state_label = customtkinter.CTkLabel(bar, text="● LANDED", font=heading, text_color=MUTED)
        self.state_label.pack(side="left")
        self.info_label = customtkinter.CTkLabel(bar, text="", text_color=MUTED)
        self.info_label.pack(side="right")
        # plain tk Label: a PhotoImage per frame is much cheaper than CTkImage rescaling
        self.cap_lbl = Label(left, text="Waiting for video…", fg="gray50", bg="#1a1a1a", bd=0, highlightthickness=0, width=80, height=30)
        self.cap_lbl.pack(padx=14, pady=(0, 10))
        # overlaid on the video top so alerts appearing/clearing never resize the window
        self.alert_label = customtkinter.CTkLabel(left, text="", corner_radius=0, text_color="white", font=heading, height=32)

        bottom = customtkinter.CTkFrame(left, fg_color="transparent")
        bottom.pack(fill="x", padx=14, pady=(0, 14))
        self.map_canvas = Canvas(bottom, width=330, height=150, bg="#1a1a1a", bd=0, highlightthickness=0)
        self.map_canvas.pack(side="left")
        self.keys_label = customtkinter.CTkLabel(bottom, justify="left", text_color=MUTED, font=customtkinter.CTkFont(family="monospace", size=12),
                                                 text="↑ ↓ ← →  move\nW / S    up / down\nA / D    rotate\nE / Q    take off / land\nH        return home\nSPACE    emergency stop")
        self.keys_label.pack(side="right", anchor="n")

        side = customtkinter.CTkFrame(self.root, fg_color="transparent", width=280)
        side.grid(row=0, column=1, padx=(8, 16), pady=16, sticky="ns")

        def card(title):
            frame = customtkinter.CTkFrame(side, corner_radius=12)
            frame.pack(fill="x", pady=(0, 12))
            customtkinter.CTkLabel(frame, text=title.upper(), font=customtkinter.CTkFont(size=11, weight="bold"), text_color=MUTED).pack(anchor="w", padx=14, pady=(10, 0))
            return frame

        telemetry = card("Telemetry")
        numbers = customtkinter.CTkFrame(telemetry, fg_color="transparent")
        numbers.pack(fill="x", padx=14)
        numbers.columnconfigure((0, 1, 2), weight=1)
        self.battery_label = customtkinter.CTkLabel(numbers, text="--%", font=big)
        self.battery_label.grid(row=0, column=0, sticky="w")
        self.height_label = customtkinter.CTkLabel(numbers, text="-- cm", font=big)
        self.height_label.grid(row=0, column=1, sticky="w")
        self.signal_label = customtkinter.CTkLabel(numbers, text="--", font=big)
        self.signal_label.grid(row=0, column=2, sticky="w")
        for column, caption in enumerate(("Battery", "Height", "Signal")):
            customtkinter.CTkLabel(numbers, text=caption, text_color=MUTED).grid(row=1, column=column, sticky="w")
        self.battery_bar = customtkinter.CTkProgressBar(telemetry, height=8)
        self.battery_bar.pack(fill="x", padx=14, pady=(6, 14))

        flight = card("Flight")
        self.takeoff_land_button = customtkinter.CTkButton(flight, text="Take off", height=44, font=heading, fg_color=GREEN, hover_color=GREEN_HOVER, command=self.toggle_takeoff_land)
        self.takeoff_land_button.pack(fill="x", padx=14, pady=(8, 8))
        customtkinter.CTkButton(flight, text="Emergency stop (Space)", fg_color="transparent", border_width=1, border_color=RED, text_color=RED,
                                hover_color="#3a2222", command=self.emergency_stop).pack(fill="x", padx=14, pady=(0, 8))
        self.home_button = customtkinter.CTkButton(flight, text="Return home (H)", fg_color="transparent", border_width=1, border_color=AMBER,
                                                   text_color=AMBER, hover_color="#3a3222", command=self.toggle_return_home)
        self.home_button.pack(fill="x", padx=14, pady=(0, 8))
        tools = customtkinter.CTkFrame(flight, fg_color="transparent")
        tools.pack(fill="x", padx=14)
        tools.columnconfigure((0, 1, 2), weight=1)
        outline = dict(fg_color="transparent", border_width=1, border_color="gray40", width=70)
        customtkinter.CTkButton(tools, text="Photo", command=self.capture_image, **outline).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.record_button = customtkinter.CTkButton(tools, text="Record", command=self.toggle_recording, **outline)
        self.record_button.grid(row=0, column=1, sticky="ew", padx=4)
        customtkinter.CTkButton(tools, text="3D map", command=self.show_3d_map, **outline).grid(row=0, column=2, sticky="ew", padx=(4, 0))
        self.capture_label = customtkinter.CTkLabel(flight, text="", text_color=MUTED, font=small)
        self.capture_label.pack(pady=(0, 6))

        control = card("Control")
        self.control_switch = customtkinter.CTkSegmentedButton(control, values=["Auto", "Manual"], command=self.set_control)
        self.control_switch.set("Auto")
        self.control_switch.pack(fill="x", padx=14, pady=(8, 8))
        self.mode_switch = customtkinter.CTkSegmentedButton(control, values=["Operational", "Search & Rescue"], command=self.set_mode)
        self.mode_switch.set("Operational")
        self.mode_switch.pack(fill="x", padx=14, pady=(0, 10))
        self.yolo_switch = customtkinter.CTkSwitch(control, text="Person detection", command=self.toggle_yolo)
        self.yolo_switch.select()
        self.yolo_switch.pack(anchor="w", padx=14, pady=(0, 14))

    def run_app(self):
        # Ctrl+C would otherwise be swallowed by Tk callbacks and the app keeps running
        signal.signal(signal.SIGINT, lambda *_: self.root.after(0, self.root.destroy))
        threading.Thread(target=self.detection_worker, daemon=True).start()
        threading.Thread(target=self.signal_worker, daemon=True).start()
        try:
            self.video_stream()
            self.control_loop()
            self.root.mainloop()
        except Exception as e:
            print(f"Error running the application: {e}")
        finally:
            self.cleanup()

    # ---- alerts -------------------------------------------------------------

    def set_alert(self, key, text, level="warn"):
        if self.alerts.get(key) != (level, text):
            self.alerts[key] = (level, text)
            self.refresh_alert()

    def clear_alert(self, key):
        if self.alerts.pop(key, None):
            self.refresh_alert()

    def refresh_alert(self):
        if not self.alerts:
            self.alert_label.place_forget()
            return
        level, text = min(self.alerts.values(), key=lambda alert: list(ALERT_COLORS).index(alert[0]))
        more = f"   (+{len(self.alerts) - 1} more)" if len(self.alerts) > 1 else ""
        self.alert_label.configure(text=f"{text}{more}", fg_color=ALERT_COLORS[level])
        self.alert_label.place(in_=self.cap_lbl, relx=0.5, y=0, anchor="n", relwidth=1.0)

    # ---- flight commands ----------------------------------------------------

    def toggle_yolo(self):
        self.yolo_enabled = not self.yolo_enabled
        self.auto_rc = [0, 0, 0, 0]
        self.detections = []

    def toggle_takeoff_land(self):
        if self.is_flying:
            self.land()
        else:
            self.takeoff()

    def preflight_problems(self):
        problems = []
        if not self.link_ok:
            problems.append("no telemetry from drone")
        if me.get_battery() < MIN_TAKEOFF_BATTERY:
            problems.append(f"battery below {MIN_TAKEOFF_BATTERY}%")
        if me.get_temperature() > MAX_TEMPERATURE:
            problems.append(f"drone hotter than {MAX_TEMPERATURE}°C")
        return problems

    def takeoff(self):
        if self.is_flying or self.busy:
            return
        problems = self.preflight_problems()
        if problems:
            self.set_alert("preflight", "Preflight check failed: " + ", ".join(problems), "error")
            return
        self.clear_alert("preflight")
        self.clear_alert("emergency")
        self.clear_alert("home")
        self.auto_landed = False
        self.rth_triggered = False
        self.yaw0 = me.get_yaw()
        self.home = (self.x, self.y)
        self.points = [(self.x, self.y, self.z)]
        self.person_points = []
        self.run_drone_command(me.takeoff, flying_after=True)

    def land(self):
        if not self.is_flying:
            return
        self.returning = False
        self.run_drone_command(me.land, flying_after=False)
        self.save_flight_log()

    def emergency_stop(self):
        # cuts the motors at once, the drone falls; no reply to wait for, so no lock
        me.emergency()
        self.returning = False
        was_flying, self.is_flying = self.is_flying, False
        self.set_alert("emergency", "EMERGENCY STOP: motors cut", "error")
        if was_flying:
            self.save_flight_log()

    def toggle_return_home(self):
        if self.returning:
            self.cancel_return_home("Return home cancelled")
        else:
            self.start_return_home("Returning home")

    def start_return_home(self, reason):
        if not self.is_flying or self.busy:
            return
        self.returning = True
        self.set_alert("home", f"{reason}: ~{self.home_distance() / 100:.1f} m to takeoff point (estimated)", "warn")

    def cancel_return_home(self, message):
        if self.returning:
            self.returning = False
            self.set_alert("home", message, "info")

    def home_distance(self):
        return math.hypot(self.home[0] - self.x, self.home[1] - self.y)

    def return_home_rc(self):
        if self.home_distance() < RTH_ARRIVE:
            self.returning = False
            self.set_alert("home", "Home reached: landing", "info")
            self.land()
            return [0, 0, 0, 0]
        self.set_alert("home", f"Returning home: ~{self.home_distance() / 100:.1f} m to go (estimated)", "warn")
        bearing = math.degrees(math.atan2(self.home[0] - self.x, self.home[1] - self.y))  # clockwise from takeoff heading
        error = wrap_degrees(bearing - (me.get_yaw() - self.yaw0))
        forward = RTH_SPEED if abs(error) < RTH_ALIGNED else 0
        return [0, forward, 0, clamp(RTH_HEADING_KP * error, 60)]

    def run_drone_command(self, command, flying_after):
        # takeoff/land wait for the drone's reply, so keep them off the GUI thread
        if self.busy:
            return
        self.busy = True

        def worker():
            try:
                with self.cmd_lock:
                    command()
                self.is_flying = flying_after
            except Exception as e:
                print(f"{command.__name__} failed: {e}")
            finally:
                self.busy = False

        threading.Thread(target=worker, daemon=True).start()

    def signal_worker(self):
        while self.running:
            # skip when a takeoff/land holds the lock; never queue behind it
            if self.link_ok and self.cmd_lock.acquire(blocking=False):
                try:
                    self.signal = int(me.query_wifi_signal_noise_ratio())
                except Exception:
                    self.signal = None
                finally:
                    self.cmd_lock.release()
            time.sleep(5)

    # ---- input --------------------------------------------------------------

    def set_control(self, value):
        self.cancel_return_home("Return home cancelled: control changed")
        self.manual_control_enabled = value == "Manual"
        self.pressed_keys.clear()
        self.auto_rc = [0, 0, 0, 0]
        self.keys_label.configure(text_color="gray90" if self.manual_control_enabled else MUTED)
        self.root.focus_set()

    def on_key_press(self, event):
        key = event.keysym.lower()
        if key == "space":
            self.emergency_stop()
            return
        if key == "h":
            self.toggle_return_home()
            return
        self.pressed_keys.add(key)
        if key in KEY_BINDINGS and self.manual_control_enabled:
            self.cancel_return_home("Return home cancelled: operator took control")
        if not self.manual_control_enabled:
            return
        if key == "q":
            self.land()
        elif key == "e":
            self.takeoff()

    def on_key_release(self, event):
        self.pressed_keys.discard(event.keysym.lower())

    def set_mode(self, value):
        self.current_mode = "search_and_rescue" if value == "Search & Rescue" else "operational"

    def get_keyboard_input(self):
        rc = [0, 0, 0, 0]
        for key in self.pressed_keys:
            if key in KEY_BINDINGS:
                axis, sign = KEY_BINDINGS[key]
                rc[axis] = sign * self.speed
        return rc

    # ---- media --------------------------------------------------------------

    def save_frame(self, prefix):
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        filename = f'captured_images/{prefix}_{timestamp}.jpg'
        Image.fromarray(self.last_frame).save(filename)
        self.capture_label.configure(text=f"Saved {os.path.basename(filename)}")
        return filename

    def capture_image(self):
        if self.last_frame is not None:
            print(f"Image saved as {self.save_frame('capture')}")

    def toggle_recording(self):
        if self.writer is None:
            os.makedirs('recordings', exist_ok=True)
            filename = f"recordings/flight_{time.strftime('%Y%m%d-%H%M%S')}.mp4"
            # shortcut: fixed 15 fps tag while real frame rate varies with YOLO load, playback speed is approximate
            self.writer = cv2.VideoWriter(filename, cv2.VideoWriter_fourcc(*"mp4v"), 15, (self.width, self.height))
            self.record_button.configure(text="■ Stop", text_color=RED)
            self.capture_label.configure(text=f"Recording {os.path.basename(filename)}")
        else:
            self.writer.release()
            self.writer = None
            self.record_button.configure(text="Record", text_color=("gray10", "gray90"))
            self.capture_label.configure(text="Recording saved")

    def show_3d_map(self):
        # plotly opens in the browser; a separate process keeps the GUI responsive
        subprocess.Popen([sys.executable, "show_map_3d.py"])

    # ---- loops --------------------------------------------------------------

    def video_stream(self):
        try:
            self.update_status()
            frame = cv2.resize(me.get_frame_read().frame, (self.width, self.height))
            self.latest_frame = frame  # handed to the detection thread untouched
            display = frame.copy()
            if self.yolo_enabled:
                self.draw_detections(display, self.detections)
            self.last_frame = display
            if self.writer is not None:
                self.writer.write(cv2.cvtColor(display, cv2.COLOR_RGB2BGR))
            self.search_and_rescue_capture()

            imgtk = ImageTk.PhotoImage(image=Image.fromarray(display))
            self.cap_lbl.imgtk = imgtk
            # width/height switch from characters (placeholder text) to pixels once an image is set
            self.cap_lbl.configure(image=imgtk, width=self.width, height=self.height)
        except Exception:
            traceback.print_exc()
        finally:
            # always reschedule, otherwise one bad frame freezes the video for good.
            # after_idle first: a frame slower than 5ms would keep a timer always due and Tk never redraws
            self.root.after_idle(self.root.after, 5, self.video_stream)

    def detection_worker(self):
        # YOLO runs here so slow inference never stalls the video or the controls
        while self.running:
            frame = self.latest_frame
            if not self.yolo_enabled or frame is None or frame is self.detected_frame:
                time.sleep(0.01)
                continue
            self.detected_frame = frame
            try:
                detections = self.detect(frame)
                if self.yolo_enabled:
                    self.detections = detections
                    self.auto_rc = self.compute_auto_rc(detections)
            except Exception:
                traceback.print_exc()

    def control_loop(self):
        try:
            if self.returning and self.is_flying and not self.busy:
                rc = self.return_home_rc()
            elif self.manual_control_enabled:
                rc = self.get_keyboard_input()
            elif self.yolo_enabled:
                rc = list(self.auto_rc)
            else:
                rc = [0, 0, 0, 0]

            if self.is_flying and not self.busy:
                if rc[2] > 0 and me.get_height() >= MAX_HEIGHT_CM:
                    rc[2] = 0
                    self.set_alert("ceiling", f"Height limit {MAX_HEIGHT_CM} cm reached", "warn")
                else:
                    self.clear_alert("ceiling")
                me.send_rc_control(*rc)
                self.log_flight_path(*rc)
            self.draw_minimap()
        except Exception:
            traceback.print_exc()
        finally:
            self.root.after(int(self.interval * 1000), self.control_loop)

    def cleanup(self):
        self.running = False
        try:
            print("Cleaning up resources...")
            if self.writer is not None:
                self.writer.release()
            if self.is_flying:
                self.save_flight_log()
            me.end()  # lands first if still flying, then stops the stream
        except Exception as e:
            print(f"Error performing cleanup: {e}")

    # ---- status & safety ----------------------------------------------------

    def check_link(self):
        # djitellopy stores a new state dict for every telemetry packet
        state = me.get_current_state()
        now = time.monotonic()
        if state is not self.last_state:
            self.last_state, self.last_state_time = state, now
        self.link_ok = now - self.last_state_time < LINK_TIMEOUT
        if self.link_ok:
            self.clear_alert("link")
        else:
            self.set_alert("link", "LINK LOST: no telemetry. Tello lands by itself after 15 s without commands", "error")

    def check_battery(self, battery):
        if self.is_flying and not self.busy and battery <= AUTO_LAND_BATTERY and not self.auto_landed:
            self.auto_landed = True
            self.set_alert("battery", f"Battery {battery}%: landing automatically", "error")
            self.land()
        elif self.is_flying and not self.busy and battery <= RTH_BATTERY and not self.rth_triggered:
            self.rth_triggered = True  # once per flight, so the operator can cancel it
            self.set_alert("battery", f"Battery {battery}%: returning home", "warn")
            self.start_return_home("Low battery")
        elif battery <= LOW_BATTERY_WARNING:
            self.set_alert("battery", f"Low battery {battery}%: land soon", "warn")
        else:
            self.clear_alert("battery")

    def update_status(self):
        self.check_link()
        battery = me.get_battery()
        self.check_battery(battery)
        self.battery_label.configure(text=f"{battery}%")
        self.battery_bar.set(battery / 100)
        self.battery_bar.configure(progress_color=GREEN if battery > 50 else AMBER if battery > 20 else RED)
        self.height_label.configure(text=f"{me.get_height()} cm")
        self.signal_label.configure(text="--" if self.signal is None else str(self.signal))

        if not self.link_ok:
            state, color = "LINK LOST", RED
        elif self.busy:
            state, color = ("LANDING", AMBER) if self.is_flying else ("TAKING OFF", AMBER)
        elif self.returning:
            state, color = "RETURNING HOME", AMBER
        else:
            state, color = ("AIRBORNE", GREEN) if self.is_flying else ("LANDED", MUTED)
        self.state_label.configure(text=f"● {state}", text_color=color)
        control = "Manual" if self.manual_control_enabled else "Auto (searching)" if self.searching and self.is_flying else "Auto"
        self.info_label.configure(text=f"{control} · {self.mode_switch.get()}")
        self.home_button.configure(text="Cancel return" if self.returning else "Return home (H)")
        self.takeoff_land_button.configure(text="Land" if self.is_flying else "Take off",
                                           fg_color=RED if self.is_flying else GREEN,
                                           hover_color=RED_HOVER if self.is_flying else GREEN_HOVER,
                                           state="disabled" if self.busy else "normal")

    # ---- detection & tracking ----------------------------------------------

    def detect(self, img):
        detections = []
        for result in self.yolo_model.predict(img):
            for box in result.boxes:
                if result.names[int(box.cls[0])] != "person":
                    continue
                x1, y1, x2, y2 = (int(v) for v in box.xyxy[0])
                detections.append((x1, y1, x2, y2))
        return detections

    def draw_detections(self, img, detections):
        centers = []
        for x1, y1, x2, y2 in detections:
            area = (x2 - x1) * (y2 - y1)
            if area >= self.min_area:
                centers.append([(x1 + x2) // 2, (y1 + y2) // 2])
            if self.current_mode == "search_and_rescue":
                text, color = "Person Detected", (0, 0, 255)
            elif area >= self.min_area:
                text, color = "Ally Detected", (0, 255, 0)
            else:
                text, color = "Hostile Detected", (255, 0, 0)
            cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
            cv2.putText(img, text, (x1, max(15, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)

        if centers:
            gx, gy = (int(v) for v in np.mean(centers, axis=0))
            cv2.circle(img, (gx, gy), 8, (255, 0, 255), -1)
            cv2.putText(img, "Group Center", (gx + 12, gy + 5), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 0, 255), 2)

    def compute_auto_rc(self, detections):
        targets = [(x1, y1, x2, y2) for x1, y1, x2, y2 in detections if (x2 - x1) * (y2 - y1) >= self.min_area]
        self.searching = not targets
        if not targets:
            self.prev_err_x = 0
            return [0, 0, 0, SEARCH_YAW]

        cx = np.mean([(x1 + x2) / 2 for x1, _, x2, _ in targets])
        cy = np.mean([(y1 + y2) / 2 for _, y1, _, y2 in targets])
        area = np.mean([(x2 - x1) * (y2 - y1) for x1, y1, x2, y2 in targets])

        # PD on yaw keeps the group centred without the old fixed-speed wobble
        err_x = cx - self.width / 2
        yaw = 0 if abs(err_x) < YAW_DEADBAND else clamp(YAW_KP * err_x + YAW_KD * (err_x - self.prev_err_x), 60)
        self.prev_err_x = err_x

        err_y = self.height / 2 - cy  # positive: target above centre -> climb
        up_down = 0 if abs(err_y) < UD_DEADBAND else clamp(UD_KP * err_y, 40)

        if TARGET_AREA_MIN <= area <= TARGET_AREA_MAX:
            forward = 0
        else:
            forward = clamp(FB_KP * ((TARGET_AREA_MIN + TARGET_AREA_MAX) / 2 - area), 30)
        return [0, forward, up_down, yaw]

    # ---- search & rescue, map, flight log ------------------------------------

    def search_and_rescue_capture(self):
        if self.current_mode != "search_and_rescue" or not self.is_flying or not self.detections:
            return
        if time.monotonic() - self.last_sar_capture < SAR_CAPTURE_COOLDOWN:
            return
        self.last_sar_capture = time.monotonic()
        self.save_frame("sar")
        self.person_points.append((self.x, self.y, self.z))

    def log_flight_path(self, lr, fb, ud, yv):
        if lr != 0 or fb != 0 or ud != 0 or yv != 0:
            # heading and height from telemetry; horizontal speed estimated from the commands (rc treated as cm/s)
            heading = math.radians(me.get_yaw() - self.yaw0)  # Tello yaw is clockwise-positive
            self.x += (lr * math.cos(heading) + fb * math.sin(heading)) * self.interval
            self.y += (fb * math.cos(heading) - lr * math.sin(heading)) * self.interval
            self.z = me.get_height()

            self.points.append((self.x, self.y, self.z))

    def draw_minimap(self):
        c = self.map_canvas
        c.delete("all")
        w, h = int(c["width"]), int(c["height"])
        path = [(x, y) for x, y, _ in self.points]
        people = [(x, y) for x, y, _ in self.person_points]
        xs, ys = [p[0] for p in path + people + [self.home]], [p[1] for p in path + people + [self.home]]
        span = max(200, max(xs) - min(xs), max(ys) - min(ys))  # at least 2 m across
        scale = min(w, h) * 0.8 / span
        mid_x, mid_y = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2

        def to_canvas(x, y):
            return w / 2 + (x - mid_x) * scale, h / 2 - (y - mid_y) * scale

        if len(path) > 1:
            c.create_line(*[v for p in path for v in to_canvas(*p)], fill=GREEN, width=2)
        hx, hy = to_canvas(*self.home)
        c.create_rectangle(hx - 7, hy - 7, hx + 7, hy + 7, outline=AMBER, width=2)
        c.create_text(hx, hy, text="H", fill=AMBER, font=("TkDefaultFont", 7, "bold"))
        for x, y in people:
            px, py = to_canvas(x, y)
            c.create_polygon(px, py - 6, px + 6, py, px, py + 6, px - 6, py, fill=RED)
        px, py = to_canvas(self.x, self.y)
        heading = math.radians(me.get_yaw() - self.yaw0)
        c.create_line(px, py, px + 14 * math.sin(heading), py - 14 * math.cos(heading), fill="white", width=2)
        c.create_oval(px - 4, py - 4, px + 4, py + 4, fill="white", outline="")
        c.create_text(8, 8, anchor="nw", text="FLIGHT MAP", fill="gray50", font=("TkDefaultFont", 8, "bold"))
        if people:
            c.create_text(w - 8, 8, anchor="ne", text=f"◆ {len(people)} sighting(s)", fill=RED, font=("TkDefaultFont", 8))

    def save_flight_log(self):
        # first point is the pre-takeoff position; the path stays on the mini-map until the next takeoff
        ccsv.make_csv(self.points[1:], detections=self.person_points)
