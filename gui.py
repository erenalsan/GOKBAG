"""GÖKBAĞ ground control window (PySide6). All flight logic lives in drone_controller.py."""
import math
import os
import traceback
from PySide6.QtCore import Qt, QTimer, QPointF, QRectF
from PySide6.QtGui import QImage, QPixmap, QPainter, QPen, QColor, QPolygonF, QFont
from PySide6.QtWidgets import (QWidget, QLabel, QPushButton, QHBoxLayout, QVBoxLayout, QFrame, QCheckBox,
                               QFileDialog, QSizePolicy, QButtonGroup)
import drone_controller as dc

# Neutral greys, one accent; colour only where it carries meaning (state, danger)
BG, SURFACE, RAISED, BORDER = "#111214", "#18191c", "#202226", "#2c2f34"
TEXT, MUTED, FAINT = "#e4e5e7", "#8b8f96", "#5c6067"
ACCENT, RED, AMBER, GREEN = "#4d8dff", "#d94848", "#d99a2b", "#4caf72"
LEVEL_COLORS = {"error": RED, "warn": AMBER, "info": ACCENT}
STATE_COLORS = {"idle": FAINT, "ok": GREEN, "plan": ACCENT, "warn": AMBER, "error": RED}

STYLE = f"""
QWidget {{ background: {BG}; color: {TEXT}; font-family: "Ubuntu", "Cantarell", "Noto Sans", sans-serif; font-size: 14px; }}
QLabel {{ background: transparent; }}
QFrame#Panel {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 10px; }}
QLabel#Title {{ font-size: 15px; font-weight: 700; letter-spacing: 3px; color: {TEXT}; }}
QLabel#State {{ font-size: 15px; font-weight: 600; }}
QLabel#Caption {{ color: {MUTED}; font-size: 11px; font-weight: 600; letter-spacing: 1px; }}
QLabel#Value {{ font-size: 19px; font-weight: 600; }}
QLabel#Muted {{ color: {MUTED}; font-size: 13px; }}
QLabel#Video {{ background: #000; border-radius: 8px; color: {MUTED}; font-size: 15px; }}
QLabel#Banner {{ background: rgba(17, 18, 20, 230); border-radius: 6px; padding: 10px 14px; font-size: 15px; font-weight: 600; }}
QFrame#Divider {{ background: {BORDER}; max-width: 1px; }}
QPushButton {{ background: {RAISED}; border: 1px solid {BORDER}; border-radius: 8px; padding: 8px 14px; font-weight: 500; }}
QPushButton:hover {{ background: #272a2f; border-color: #3a3e45; }}
QPushButton:pressed {{ background: #2d3036; }}
QPushButton:checked {{ border-color: {ACCENT}; color: {ACCENT}; }}
QPushButton:disabled {{ color: {FAINT}; border-color: #24262a; background: #1a1b1e; }}
QPushButton#Big {{ font-size: 17px; font-weight: 600; min-height: 60px; padding: 0 20px; }}
QPushButton#Big[tone="primary"] {{ background: {ACCENT}; border: none; color: white; }}
QPushButton#Big[tone="primary"]:hover {{ background: #3f7ce8; }}
QPushButton#Big[tone="active"] {{ border: 1px solid {ACCENT}; color: {ACCENT}; background: #1b2232; }}
QPushButton#Big[tone="stop"] {{ background: {RED}; border: none; color: white; }}
QPushButton#Big[tone="stop"]:hover {{ background: #c43d3d; }}
QPushButton#Big[tone="stop"]:disabled {{ background: #1a1b1e; border: 1px solid #24262a; color: {FAINT}; }}
QPushButton#Danger {{ background: transparent; border: 1px solid #5a2a2a; color: #e07a7a; }}
QPushButton#Danger:hover {{ background: #241616; border-color: {RED}; }}
QCheckBox {{ spacing: 8px; background: transparent; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid #45494f; background: {RAISED}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QLineEdit {{ background: {RAISED}; border: 1px solid {BORDER}; border-radius: 8px; padding: 10px 12px; font-size: 15px; }}
QLineEdit:focus {{ border-color: {ACCENT}; }}
QComboBox {{ background: {RAISED}; border: 1px solid {BORDER}; border-radius: 8px; padding: 8px 12px; }}
QToolTip {{ background: {RAISED}; color: {TEXT}; border: 1px solid {BORDER}; padding: 6px; }}
"""

# Qt key -> drone_controller key name
KEYS = {Qt.Key_Left: "left", Qt.Key_Right: "right", Qt.Key_Up: "up", Qt.Key_Down: "down",
        Qt.Key_W: "w", Qt.Key_S: "s", Qt.Key_A: "a", Qt.Key_D: "d",
        Qt.Key_E: "e", Qt.Key_Q: "q", Qt.Key_H: "h", Qt.Key_Space: "space"}


def set_prop(widget, name, value):
    if widget.property(name) != value:
        widget.setProperty(name, value)
        widget.style().unpolish(widget)
        widget.style().polish(widget)


def label(text="", name=None):
    lbl = QLabel(text)
    if name:
        lbl.setObjectName(name)
    return lbl


def button(text, slot, name=None, tone=None, checkable=False):
    b = QPushButton(text)
    b.setFocusPolicy(Qt.NoFocus)  # keys must reach the window: Space on a focused button would press it
    b.setCursor(Qt.PointingHandCursor)
    b.setCheckable(checkable)
    if name:
        b.setObjectName(name)
    if tone:
        b.setProperty("tone", tone)
    b.clicked.connect(slot)
    return b


def readout(caption):
    """Caption above a value, e.g. BATTERY / 87 %. Returns (layout, value label)."""
    box = QVBoxLayout()
    box.setSpacing(0)
    value = label("", "Value")
    box.addWidget(label(caption, "Caption"))
    box.addWidget(value)
    return box, value


def divider():
    line = QFrame()
    line.setObjectName("Divider")
    line.setFixedWidth(1)
    return line


class VideoView(QLabel):
    """Video that scales with the window, with the current alert overlaid on top."""

    def __init__(self):
        super().__init__("Connecting to the camera…")
        self.setObjectName("Video")
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(480, 360)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.image = None
        self.banner = QLabel(self)
        self.banner.setObjectName("Banner")
        self.banner.setWordWrap(True)
        self.banner.hide()

    def show_frame(self, rgb):
        h, w, _ = rgb.shape
        self.image = QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy()
        self.refresh()

    def refresh(self):
        if self.image is not None:
            self.setPixmap(QPixmap.fromImage(self.image).scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def show_alert(self, alert):
        if alert is None:
            self.banner.hide()
            return
        level, text = alert
        self.banner.setText(text)
        # a coloured edge carries the severity; the bar itself stays quiet over the video
        self.banner.setStyleSheet(f"border-left: 4px solid {LEVEL_COLORS[level]};")
        self.place_banner()
        self.banner.show()

    def place_banner(self):
        self.banner.setFixedWidth(min(560, self.width() - 32))
        self.banner.adjustSize()
        self.banner.move(16, 16)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.refresh()
        self.place_banner()


class FlightMap(QWidget):
    """Top-down map: path, home, sightings and flight-plan waypoints. Click adds a waypoint, right-click undoes."""

    MIN_SPAN = 600  # always shows at least ~6 m so waypoints can be placed

    def __init__(self, controller):
        super().__init__()
        self.c = controller
        self.setMinimumHeight(240)
        self.setCursor(Qt.CrossCursor)
        self.transform = None

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            self.c.remove_last_waypoint()
        elif self.transform is not None:
            mid_x, mid_y, scale = self.transform
            p = event.position()
            self.c.add_waypoint_at(mid_x + (p.x() - self.width() / 2) / scale, mid_y - (p.y() - self.height() / 2) / scale)
        self.update()

    def paintEvent(self, event):
        c, w, h = self.c, self.width(), self.height()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor("#0c0d0f"))
        p.setPen(QPen(QColor(BORDER), 1))
        p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 8, 8)

        path = [(x, y) for x, y, _ in c.points]
        people = [(x, y) for x, y, _ in c.person_points]
        origin = c.plan_origin()
        targets = c.plan_targets()
        everything = path + people + targets + [c.home, origin]
        xs, ys = [q[0] for q in everything], [q[1] for q in everything]
        span = max(self.MIN_SPAN, max(xs) - min(xs), max(ys) - min(ys))
        scale = min(w, h) * 0.8 / span
        mid_x, mid_y = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
        self.transform = (mid_x, mid_y, scale)

        def pt(x, y):
            return QPointF(w / 2 + (x - mid_x) * scale, h / 2 - (y - mid_y) * scale)

        p.setPen(QPen(QColor("#1b1d20"), 1))  # 1 m grid
        for gx in range(int((mid_x - w / 2 / scale) // 100) * 100, int(mid_x + w / 2 / scale) + 100, 100):
            p.drawLine(QPointF(pt(gx, 0).x(), 1), QPointF(pt(gx, 0).x(), h - 1))
        for gy in range(int((mid_y - h / 2 / scale) // 100) * 100, int(mid_y + h / 2 / scale) + 100, 100):
            p.drawLine(QPointF(1, pt(0, gy).y()), QPointF(w - 1, pt(0, gy).y()))

        small = QFont(self.font())
        small.setPixelSize(11)
        p.setFont(small)
        if targets:
            p.setPen(QPen(QColor(ACCENT), 1.5, Qt.DashLine))
            p.drawPolyline([pt(*q) for q in [origin] + targets])
            for i, target in enumerate(targets):
                q = pt(*target)
                current = c.plan_active and i == c.plan_index
                p.setPen(QPen(QColor(ACCENT), 1.5))
                p.setBrush(QColor(ACCENT) if current else QColor("#0c0d0f"))
                p.drawEllipse(q, 9, 9)
                p.setPen(QColor("white") if current else QColor(ACCENT))
                p.drawText(QRectF(q.x() - 9, q.y() - 9, 18, 18), Qt.AlignCenter, str(i + 1))

        if len(path) > 1:
            p.setPen(QPen(QColor("#9aa0a8"), 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.drawPolyline([pt(*q) for q in path])

        hq = pt(*(c.home if c.is_flying else origin))
        p.setPen(QPen(QColor(TEXT), 1.5))
        p.setBrush(QColor("#0c0d0f"))
        p.drawRect(QRectF(hq.x() - 8, hq.y() - 8, 16, 16))
        p.drawText(QRectF(hq.x() - 8, hq.y() - 8, 16, 16), Qt.AlignCenter, "H")

        p.setPen(Qt.NoPen)
        p.setBrush(QColor(RED))
        for x, y in people:
            q = pt(x, y)
            p.drawPolygon(QPolygonF([q + QPointF(0, -6), q + QPointF(6, 0), q + QPointF(0, 6), q + QPointF(-6, 0)]))

        dq = pt(c.x, c.y)
        heading = math.radians(dc.me.get_yaw() - c.yaw0)
        p.setPen(QPen(QColor("white"), 2, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(dq, dq + QPointF(14 * math.sin(heading), -14 * math.cos(heading)))
        p.setBrush(QColor("white"))
        p.setPen(Qt.NoPen)
        p.drawEllipse(dq, 4.5, 4.5)

        p.setPen(QColor(FAINT))
        p.drawText(QRectF(10, 8, w - 20, 16), Qt.AlignLeft, "Grid: 1 m")
        if people:
            p.setPen(QColor(MUTED))
            p.drawText(QRectF(10, 8, w - 20, 16), Qt.AlignRight, f"{len(people)} sighting{'s' if len(people) > 1 else ''}")
        p.setPen(QColor(FAINT))
        p.drawText(QRectF(10, h - 24, w - 20, 16), Qt.AlignLeft, "Click to add a point, right-click to undo")


class MainWindow(QWidget):
    def __init__(self, controller):
        super().__init__()
        self.c = controller
        self.setWindowTitle("GÖKBAĞ")
        self.resize(1280, 800)
        self.notice_shown = ""

        # top bar: name and state on the left, telemetry on the right
        top = QHBoxLayout()
        top.setSpacing(18)
        self.state_dot = label("●")
        self.state = label("", "State")
        state = QHBoxLayout()
        state.setSpacing(8)
        state.addWidget(self.state_dot)
        state.addWidget(self.state)
        self.notice = label("", "Muted")
        battery_box, self.battery = readout("BATTERY")
        height_box, self.height_value = readout("HEIGHT")
        signal_box, self.signal_value = readout("SIGNAL")
        top.addWidget(label("GÖKBAĞ", "Title"))
        top.addWidget(divider())
        top.addLayout(state)
        top.addStretch()
        top.addWidget(self.notice)
        for box in (battery_box, height_box, signal_box):
            top.addLayout(box)

        # video + the four controls a beginner needs
        self.video = VideoView()
        self.takeoff_button = button("", self.c.toggle_takeoff_land, "Big", "primary")
        self.home_button = button("", self.c.toggle_return_home, "Big")
        self.follow_button = button("", self.c.toggle_follow, "Big")
        self.stop_button = button("Stop", self.c.hover, "Big", "stop")
        self.stop_button.setToolTip("Stop moving and hover in place. The motors keep running.")
        controls = QHBoxLayout()
        controls.setSpacing(10)
        for b in (self.takeoff_button, self.home_button, self.follow_button, self.stop_button):
            controls.addWidget(b, 1)
        hint = label("Arrow keys move the drone, W and S go up and down, A and D turn. Let go to hover.", "Muted")

        main = QVBoxLayout()
        main.setSpacing(12)
        main.addLayout(top)
        main.addWidget(self.video, 1)
        main.addLayout(controls)
        main.addWidget(hint)

        # side panel: map, flight plan, camera, mission
        self.side = QFrame()
        self.side.setObjectName("Panel")
        self.side.setFixedWidth(360)
        more = QVBoxLayout(self.side)
        more.setContentsMargins(16, 16, 16, 16)
        more.setSpacing(8)

        def section(text):
            more.addSpacing(10)
            more.addWidget(label(text, "Caption"))

        more.addWidget(label("FLIGHT PLAN", "Caption"))
        self.map = FlightMap(self.c)
        more.addWidget(self.map, 1)
        self.plan_label = label("", "Muted")
        more.addWidget(self.plan_label)
        plan_row = QHBoxLayout()
        self.plan_button = button("Fly plan", self.c.toggle_plan)
        plan_row.addWidget(self.plan_button, 2)
        for text, slot in (("Clear", self.c.clear_plan), ("Save", self.save_plan), ("Load", self.load_plan)):
            plan_row.addWidget(button(text, slot), 1)
        more.addLayout(plan_row)

        section("CAMERA")
        camera_row = QHBoxLayout()
        self.record_button = button("Record", self.c.toggle_recording)
        for b in (button("Photo", self.c.capture_image), self.record_button, button("3D map", self.c.show_3d_map)):
            camera_row.addWidget(b)
        more.addLayout(camera_row)

        section("MISSION")
        mission_row = QHBoxLayout()
        self.mission = QButtonGroup(self)
        for i, (text, mode) in enumerate((("Operational", "operational"), ("Search && Rescue", "search_and_rescue"))):
            b = button(text, lambda _=False, m=mode: self.c.set_mode(m), checkable=True)
            b.setChecked(i == 0)
            self.mission.addButton(b)
            mission_row.addWidget(b)
        more.addLayout(mission_row)
        self.detect_box = QCheckBox("Show detected people on the video")
        self.detect_box.setFocusPolicy(Qt.NoFocus)
        self.detect_box.setChecked(True)
        self.detect_box.toggled.connect(lambda _: self.c.toggle_yolo())
        more.addWidget(self.detect_box)

        section("KEYS")
        more.addWidget(label("E take off   ·   Q land   ·   H come home\nSpace emergency motor cut", "Muted"))
        more.addSpacing(10)
        more.addWidget(button("Emergency motor cut (Space)", self.c.emergency_stop, "Danger"))
        more.addWidget(label("Motors stop immediately and the drone falls.", "Muted"))

        root = QHBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(16)
        root.addLayout(main, 1)
        root.addWidget(self.side)

        self.video_timer = QTimer(self, timeout=self.tick_video, interval=15)
        self.control_timer = QTimer(self, timeout=self.tick_control, interval=int(self.c.interval * 1000))
        self.refresh()

    def start(self):
        self.c.start()
        self.video_timer.start()
        self.control_timer.start()

    # ---- timers -------------------------------------------------------------

    def tick_video(self):
        try:
            self.video.show_frame(self.c.tick_video())
            self.refresh()
        except Exception:
            traceback.print_exc()

    def tick_control(self):
        try:
            self.c.tick_control()
            self.map.update()
        except Exception:
            traceback.print_exc()

    def refresh(self):
        c = self.c
        text, level = c.flight_state()
        self.state.setText(text)
        self.state_dot.setStyleSheet(f"color: {STATE_COLORS[level]}; font-size: 12px;")

        battery = dc.me.get_battery()
        self.battery.setText(f"{battery} %")
        self.battery.setStyleSheet("" if battery > 30 else f"color: {AMBER if battery > 20 else RED};")
        self.height_value.setText(f"{dc.me.get_height() / 100:.1f} m")
        signal = c.signal
        self.signal_value.setText("–" if signal is None else "Good" if signal >= 60 else "Fair" if signal >= 30 else "Weak")
        self.signal_value.setToolTip(f"Wi-Fi signal-to-noise: {signal}")
        self.video.show_alert(c.top_alert())
        if c.notice != self.notice_shown:
            self.notice_shown = c.notice
            self.notice.setText(c.notice)
            QTimer.singleShot(4000, lambda shown=c.notice: self.notice.setText("") if self.notice.text() == shown else None)

        flying = c.is_flying
        self.takeoff_button.setText("Land" if flying else "Take off")
        self.takeoff_button.setEnabled(not c.busy)
        self.home_button.setText("Cancel return" if c.returning else "Come home")
        set_prop(self.home_button, "tone", "active" if c.returning else None)
        self.home_button.setEnabled(flying and not c.busy)
        self.follow_button.setText("Following" if c.follow else "Follow people")
        set_prop(self.follow_button, "tone", "active" if c.follow else None)
        self.stop_button.setEnabled(flying)

        self.plan_button.setText("Stop plan" if c.plan_active else "Fly plan")
        self.record_button.setText("Stop recording" if c.writer is not None else "Record")
        if c.waypoints:
            self.plan_label.setText(f"{len(c.waypoints)} point{'s' if len(c.waypoints) > 1 else ''}, "
                                    f"about {c.plan_length() / 100:.1f} m there and back")
        else:
            self.plan_label.setText("Click the map to add points for the drone to visit.")

    # ---- actions ------------------------------------------------------------

    def save_plan(self):
        if not self.c.waypoints:
            return
        os.makedirs("plans", exist_ok=True)
        filename, _ = QFileDialog.getSaveFileName(self, "Save flight plan", "plans/plan.json", "Flight plan (*.json)")
        if filename:
            self.c.save_plan(filename)

    def load_plan(self):
        filename, _ = QFileDialog.getOpenFileName(self, "Load flight plan", "plans", "Flight plan (*.json)")
        if filename:
            self.c.load_plan(filename)
            self.map.update()

    def keyPressEvent(self, event):
        key = KEYS.get(event.key())
        if key and not event.isAutoRepeat():
            self.c.key_down(key)
        elif not key:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        key = KEYS.get(event.key())
        if key and not event.isAutoRepeat():
            self.c.key_up(key)

    def closeEvent(self, event):
        self.video_timer.stop()
        self.control_timer.stop()
        self.c.cleanup()
        super().closeEvent(event)
