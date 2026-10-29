"""Flight logic: drone link, safety, tracking, return home, flight plans and logs. No UI code; see gui.py."""
import os
import sys
import time
import math
import json
import subprocess
import threading
import traceback
import cv2
import numpy as np
from PIL import Image
from utilities.yolo_model import YOLOModel
import utilities.coord_csv_module as ccsv

# GOKBAG_SIM=0 (webcam index) or GOKBAG_SIM=path/to/video runs without a drone
if os.environ.get("GOKBAG_SIM"):
    from utilities.fake_tello import FakeTello
    me = FakeTello(os.environ["GOKBAG_SIM"])
else:
    from djitellopy import Tello
    me = Tello()

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

# key name -> (axis index in [lr, fb, ud, yv], sign)
KEY_BINDINGS = {
    "left": (0, -1), "right": (0, 1),
    "up": (1, 1), "down": (1, -1),
    "w": (2, 1), "s": (2, -1),
    "a": (3, -1), "d": (3, 1),
}
ALERT_LEVELS = ("error", "warn", "info")  # most severe first


def clamp(value, limit):
    return int(max(-limit, min(limit, value)))


def wrap_degrees(angle):
    return (angle + 180) % 360 - 180


def parse_plan(filename):
    with open(filename) as f:
        waypoints = json.load(f)["waypoints"]
    # files can come from anywhere: accept only a list of numeric pairs within 50 m of takeoff
    plan = [(float(dx), float(dy)) for dx, dy in waypoints]
    if not plan or any(math.hypot(dx, dy) > 5000 for dx, dy in plan):
        raise ValueError("expected 1+ waypoints within 50 m of takeoff")
    return plan


class DroneController:
    def __init__(self):
        me.connect()
        me.streamon()

        self.yolo_enabled = True
        self.follow = False        # FOLLOW button: track people automatically
        self.is_flying = False
        self.busy = False  # takeoff/land in progress on a worker thread
        # djitellopy matches replies to commands by order, so commands that wait for a reply must not overlap
        self.cmd_lock = threading.Lock()
        self.running = True
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
        self.notice = ""  # short confirmation for the UI, e.g. "Photo saved"

        self.link_ok = True
        self.last_state = None
        self.last_state_time = time.monotonic()
        self.signal = None
        self.auto_landed = False
        self.returning = False
        self.rth_triggered = False
        self.waypoints = []        # (dx, dy) offsets from the takeoff point, +y = takeoff heading
        self.plan_active = False
        self.plan_index = 0

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

    def start(self):
        threading.Thread(target=self.detection_worker, daemon=True).start()
        threading.Thread(target=self.signal_worker, daemon=True).start()

    # ---- alerts -------------------------------------------------------------

    def set_alert(self, key, text, level="warn"):
        self.alerts[key] = (level, text)

    def clear_alert(self, key):
        self.alerts.pop(key, None)

    def top_alert(self):
        """(level, text) of the most severe alert, or None."""
        if not self.alerts:
            return None
        return min(self.alerts.values(), key=lambda alert: ALERT_LEVELS.index(alert[0]))

    # ---- flight commands ----------------------------------------------------

    def toggle_yolo(self):
        self.yolo_enabled = not self.yolo_enabled
        self.auto_rc = [0, 0, 0, 0]
        self.detections = []
        if not self.yolo_enabled:
            self.follow = False

    def toggle_follow(self):
        self.follow = not self.follow
        if self.follow:
            self.yolo_enabled = True
            self.cancel_return_home("Return home cancelled: following people")
            self.stop_plan("Flight plan stopped: following people")

    def hover(self):
        """The big STOP button: stop every automatic movement and hold position. Motors keep running."""
        self.follow = False
        self.pressed_keys.clear()
        self.cancel_return_home("Stopped: hovering")
        self.stop_plan("Stopped: hovering")
        if self.is_flying:
            me.send_rc_control(0, 0, 0, 0)
            self.set_alert("stop", "Stopped: hovering in place", "info")

    def toggle_takeoff_land(self):
        if self.is_flying:
            self.land()
        else:
            self.takeoff()

    def preflight_problems(self):
        problems = []
        if not self.link_ok:
            problems.append("no connection to the drone")
        if me.get_battery() < MIN_TAKEOFF_BATTERY:
            problems.append(f"battery below {MIN_TAKEOFF_BATTERY}%")
        if me.get_temperature() > MAX_TEMPERATURE:
            problems.append(f"drone too hot (over {MAX_TEMPERATURE}°C)")
        return problems

    def takeoff(self):
        if self.is_flying or self.busy:
            return
        problems = self.preflight_problems()
        if problems:
            self.set_alert("preflight", "Can't take off: " + ", ".join(problems), "error")
            return
        for key in ("preflight", "emergency", "home", "stop"):
            self.clear_alert(key)
        if not self.plan_active:
            self.clear_alert("plan")
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
        self.plan_active = False
        self.follow = False
        self.run_drone_command(me.land, flying_after=False)
        self.save_flight_log()

    def emergency_stop(self):
        # cuts the motors at once, the drone falls; no reply to wait for, so no lock
        me.emergency()
        self.returning = False
        self.plan_active = False
        self.follow = False
        was_flying, self.is_flying = self.is_flying, False
        self.set_alert("emergency", "EMERGENCY: motors cut", "error")
        if was_flying:
            self.save_flight_log()

    def toggle_return_home(self):
        if self.returning:
            self.cancel_return_home("Return home cancelled")
        else:
            self.start_return_home("Coming home")

    def start_return_home(self, reason):
        if not self.is_flying or self.busy:
            return
        self.plan_active = False
        self.follow = False
        self.returning = True
        self.set_alert("home", f"{reason}: ~{self.home_distance() / 100:.1f} m to go", "warn")

    def cancel_return_home(self, message):
        if self.returning:
            self.returning = False
            self.set_alert("home", message, "info")

    def home_distance(self):
        return math.hypot(self.home[0] - self.x, self.home[1] - self.y)

    def fly_to_rc(self, target):
        """rc that turns toward target and flies to it; None once within RTH_ARRIVE."""
        if math.hypot(target[0] - self.x, target[1] - self.y) < RTH_ARRIVE:
            return None
        bearing = math.degrees(math.atan2(target[0] - self.x, target[1] - self.y))  # clockwise from takeoff heading
        error = wrap_degrees(bearing - (me.get_yaw() - self.yaw0))
        forward = RTH_SPEED if abs(error) < RTH_ALIGNED else 0
        return [0, forward, 0, clamp(RTH_HEADING_KP * error, 60)]

    def return_home_rc(self):
        rc = self.fly_to_rc(self.home)
        if rc is None:
            self.returning = False
            self.set_alert("home", "Home reached: landing", "info")
            self.land()
            return [0, 0, 0, 0]
        self.set_alert("home", f"Coming home: ~{self.home_distance() / 100:.1f} m to go", "warn")
        return rc

    # ---- flight plan --------------------------------------------------------

    def plan_origin(self):
        # waypoints are relative to the takeoff point: home in flight, the current spot while landed
        return self.home if self.is_flying else (self.x, self.y)

    def plan_targets(self):
        ox, oy = self.plan_origin()
        return [(ox + dx, oy + dy) for dx, dy in self.waypoints]

    def plan_length(self):
        stops = [(0, 0)] + self.waypoints + [(0, 0)]
        return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(stops, stops[1:]))

    def add_waypoint_at(self, x, y):
        ox, oy = self.plan_origin()
        self.waypoints.append((round(x - ox), round(y - oy)))

    def remove_last_waypoint(self):
        if self.waypoints and not (self.plan_active and self.plan_index >= len(self.waypoints) - 1):
            self.waypoints.pop()

    def clear_plan(self):
        self.stop_plan("Flight plan cleared")
        self.waypoints = []

    def toggle_plan(self):
        if self.plan_active:
            self.stop_plan("Flight plan stopped: hovering")
            return
        if not self.waypoints:
            self.set_alert("plan", "Add waypoints by clicking the map first", "warn")
            return
        self.returning = False
        self.follow = False
        self.plan_index = 0
        self.plan_active = True
        if not self.is_flying:
            self.takeoff()  # the control loop starts the plan once airborne
            if not self.busy:  # preflight refused the takeoff
                self.plan_active = False

    def stop_plan(self, message):
        if self.plan_active:
            self.plan_active = False
            self.set_alert("plan", message, "info")

    def plan_rc(self):
        targets = self.plan_targets()
        while self.plan_index < len(targets):
            target = targets[self.plan_index]
            rc = self.fly_to_rc(target)
            if rc is not None:
                distance = math.hypot(target[0] - self.x, target[1] - self.y)
                self.set_alert("plan", f"Flight plan: point {self.plan_index + 1} of {len(targets)}, ~{distance / 100:.1f} m to go", "info")
                return rc
            self.plan_index += 1
        self.plan_active = False
        self.set_alert("plan", "Flight plan complete", "info")
        self.start_return_home("Flight plan complete")
        return [0, 0, 0, 0]

    def save_plan(self, filename):
        with open(filename, "w") as f:
            json.dump({"waypoints": self.waypoints}, f)

    def load_plan(self, filename):
        try:
            waypoints = parse_plan(filename)
        except (OSError, ValueError, KeyError, TypeError) as e:
            self.set_alert("plan", f"Could not load plan: {e}", "error")
            return
        self.stop_plan("Flight plan loaded")
        self.waypoints = waypoints

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

    def key_down(self, key):
        """key: lower-case name ("up", "w", "space", ...)."""
        if key == "space":
            self.emergency_stop()
            return
        if key == "h":
            self.toggle_return_home()
            return
        if key == "q":
            self.land()
            return
        if key == "e":
            self.takeoff()
            return
        if key in KEY_BINDINGS:
            # flying by hand takes over from every automatic mode
            self.follow = False
            self.cancel_return_home("Return home cancelled: you took control")
            self.stop_plan("Flight plan stopped: you took control")
            self.clear_alert("stop")
            self.pressed_keys.add(key)

    def key_up(self, key):
        self.pressed_keys.discard(key)

    def set_mode(self, mode):
        self.current_mode = mode  # "operational" or "search_and_rescue"

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
        self.notice = f"Photo saved: {os.path.basename(filename)}"
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
            self.notice = f"Recording {os.path.basename(filename)}"
        else:
            self.writer.release()
            self.writer = None
            self.notice = "Video saved in recordings/"

    def show_3d_map(self):
        # plotly opens in the browser; a separate process keeps the GUI responsive
        subprocess.Popen([sys.executable, "show_map_3d.py"])

    # ---- loops (called by the UI's timers) -----------------------------------

    def tick_video(self):
        """Runs safety checks and returns the next frame (RGB, with detections drawn)."""
        self.check_link()
        self.check_battery(me.get_battery())
        frame = cv2.resize(me.get_frame_read().frame, (self.width, self.height))
        self.latest_frame = frame  # handed to the detection thread untouched
        display = frame.copy()
        if self.yolo_enabled:
            self.draw_detections(display, self.detections)
        self.last_frame = display
        if self.writer is not None:
            self.writer.write(cv2.cvtColor(display, cv2.COLOR_RGB2BGR))
        self.search_and_rescue_capture()
        return display

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

    def tick_control(self):
        """Sends one rc command; call every self.interval seconds."""
        if self.returning and self.is_flying and not self.busy:
            rc = self.return_home_rc()
        elif self.plan_active and self.is_flying and not self.busy:
            rc = self.plan_rc()
        elif self.pressed_keys:
            rc = self.get_keyboard_input()
        elif self.follow and self.yolo_enabled:
            rc = list(self.auto_rc)
        else:
            rc = [0, 0, 0, 0]

        if self.is_flying and not self.busy:
            if rc[2] > 0 and me.get_height() >= MAX_HEIGHT_CM:
                rc[2] = 0
                self.set_alert("ceiling", f"Height limit reached ({MAX_HEIGHT_CM / 100:.0f} m)", "warn")
            else:
                self.clear_alert("ceiling")
            me.send_rc_control(*rc)
            self.log_flight_path(*rc)

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
            self.set_alert("link", "Lost connection! The drone will land by itself in 15 s", "error")

    def check_battery(self, battery):
        if self.is_flying and not self.busy and battery <= AUTO_LAND_BATTERY and not self.auto_landed:
            self.auto_landed = True
            self.set_alert("battery", f"Battery {battery}%: landing now", "error")
            self.land()
        elif self.is_flying and not self.busy and battery <= RTH_BATTERY and not self.rth_triggered:
            self.rth_triggered = True  # once per flight, so the operator can cancel it
            self.set_alert("battery", f"Battery {battery}%: coming home", "warn")
            self.start_return_home("Low battery")
        elif battery <= LOW_BATTERY_WARNING:
            self.set_alert("battery", f"Battery low ({battery}%): land soon", "warn")
        else:
            self.clear_alert("battery")

    def flight_state(self):
        """(text, level) describing what the drone is doing, for the status pill."""
        if not self.link_ok:
            return "No connection", "error"
        if self.busy:
            return ("Landing…", "warn") if self.is_flying else ("Taking off…", "warn")
        if not self.is_flying:
            return "On the ground", "idle"
        if self.returning:
            return "Coming home", "warn"
        if self.plan_active:
            return f"Flight plan {min(self.plan_index + 1, len(self.waypoints))}/{len(self.waypoints)}", "plan"
        if self.pressed_keys:
            return "Flying by hand", "ok"
        if self.follow:
            return ("Looking for people", "plan") if self.searching else ("Following", "plan")
        return "Hovering", "ok"

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

    # ---- search & rescue, flight log ---------------------------------------

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

    def save_flight_log(self):
        # first point is the pre-takeoff position; the path stays on the map until the next takeoff
        ccsv.make_csv(self.points[1:], detections=self.person_points)
