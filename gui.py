"""GÖKBAĞ ground control window (PySide6). All flight logic lives in drone_controller.py."""
import math
import os
import traceback
from PySide6.QtCore import Qt, QTimer, QPointF, QRectF
from PySide6.QtGui import QImage, QPixmap, QPainter, QPen, QColor, QPolygonF, QFont
from PySide6.QtWidgets import (QWidget, QLabel, QPushButton, QHBoxLayout, QVBoxLayout, QFrame, QCheckBox,
                               QFileDialog, QSizePolicy, QButtonGroup)
import drone_controller as dc

BG, CARD, CARD2, BORDER = "#0f1216", "#171b21", "#1f252d", "#2a323d"
TEXT, MUTED = "#e8edf2", "#8b96a3"
GREEN, RED, AMBER, BLUE, INDIGO = "#22c55e", "#ef4444", "#f59e0b", "#3b82f6", "#6366f1"
LEVEL_COLORS = {"error": RED, "warn": AMBER, "info": GREEN}

STYLE = f"""
QWidget {{ background: {BG}; color: {TEXT}; font-family: "Ubuntu", "Cantarell", "Noto Sans", sans-serif; font-size: 15px; }}
QFrame#Card {{ background: {CARD}; border-radius: 18px; }}
QLabel {{ background: transparent; }}
QLabel#Title {{ font-size: 22px; font-weight: 800; letter-spacing: 2px; }}
QLabel#Muted {{ color: {MUTED}; font-size: 13px; }}
QLabel#Section {{ color: {MUTED}; font-size: 12px; font-weight: 700; letter-spacing: 1px; }}
QLabel#Chip {{ background: {CARD2}; border-radius: 14px; padding: 8px 16px; font-size: 18px; font-weight: 700; }}
QLabel#Pill {{ border-radius: 16px; padding: 8px 18px; font-size: 16px; font-weight: 700; }}
QLabel#Pill[level="idle"]  {{ background: #1f252d; color: {MUTED}; }}
QLabel#Pill[level="ok"]    {{ background: #123d26; color: #86efac; }}
QLabel#Pill[level="plan"]  {{ background: #172a4d; color: #93c5fd; }}
QLabel#Pill[level="warn"]  {{ background: #3d2c0c; color: #fcd34d; }}
QLabel#Pill[level="error"] {{ background: #4a1515; color: #fca5a5; }}
QLabel#Video {{ background: #000; border-radius: 14px; color: {MUTED}; font-size: 18px; }}
QLabel#Banner {{ border-radius: 12px; padding: 10px 16px; font-size: 17px; font-weight: 700; color: white; }}
QPushButton {{ background: {CARD2}; border: 1px solid {BORDER}; border-radius: 12px; padding: 10px 14px; font-weight: 600; }}
QPushButton:hover {{ background: #27303a; }}
QPushButton:pressed {{ background: #2f3946; }}
QPushButton:checked {{ background: {BLUE}; border-color: {BLUE}; color: white; }}
QPushButton:disabled {{ color: #556070; }}
QPushButton#Big {{ font-size: 21px; font-weight: 800; border-radius: 20px; min-height: 84px; padding: 0 22px; border: none; color: white; }}
QPushButton#Big[tone="green"]  {{ background: #16a34a; }}  QPushButton#Big[tone="green"]:hover  {{ background: #15803d; }}
QPushButton#Big[tone="indigo"] {{ background: {INDIGO}; }} QPushButton#Big[tone="indigo"]:hover {{ background: #4f46e5; }}
QPushButton#Big[tone="amber"]  {{ background: #d97706; }}  QPushButton#Big[tone="amber"]:hover  {{ background: #b45309; }}
QPushButton#Big[tone="grey"]   {{ background: {CARD2}; border: 2px solid {BORDER}; }}
QPushButton#Big[tone="grey"]:hover {{ background: #27303a; }}
QPushButton#Big[tone="blue"]   {{ background: {BLUE}; }}   QPushButton#Big[tone="blue"]:hover   {{ background: #2563eb; }}
QPushButton#Big[tone="red"]    {{ background: #dc2626; }}  QPushButton#Big[tone="red"]:hover    {{ background: #b91c1c; }}
QPushButton#Big:disabled {{ background: #2a2f36; color: #6b7480; }}
QPushButton#Danger {{ background: transparent; border: 2px solid {RED}; color: #fca5a5; }}
QPushButton#Danger:hover {{ background: #3a1414; }}
QCheckBox {{ spacing: 10px; font-size: 15px; background: transparent; }}
QCheckBox::indicator {{ width: 22px; height: 22px; border-radius: 6px; border: 2px solid {BORDER}; background: {CARD2}; }}
QCheckBox::indicator:checked {{ background: {BLUE}; border-color: {BLUE}; }}
QLineEdit {{ background: {CARD2}; border: 1px solid {BORDER}; border-radius: 12px; padding: 12px 14px; font-size: 16px; }}
QLineEdit:focus {{ border-color: {BLUE}; }}
QComboBox {{ background: {CARD2}; border: 1px solid {BORDER}; border-radius: 12px; padding: 10px 14px; font-size: 16px; }}
QToolTip {{ background: {CARD2}; color: {TEXT}; border: 1px solid {BORDER}; padding: 6px; }}
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


def card():
    frame = QFrame()
    frame.setObjectName("Card")
    return frame


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
        self.banner.setAlignment(Qt.AlignCenter)
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
        self.banner.setStyleSheet(f"background: {LEVEL_COLORS[level]};")
        self.place_banner()
        self.banner.show()

    def place_banner(self):
        self.banner.setFixedWidth(self.width() - 32)
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
        self.setMinimumHeight(250)
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
        p.setBrush(QColor("#0b0e12"))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(QRectF(0, 0, w, h), 14, 14)

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

        p.setPen(QPen(QColor("#1c232c"), 1))  # 1 m grid
        for gx in range(int((mid_x - w / 2 / scale) // 100) * 100, int(mid_x + w / 2 / scale) + 100, 100):
            p.drawLine(QPointF(pt(gx, 0).x(), 0), QPointF(pt(gx, 0).x(), h))
        for gy in range(int((mid_y - h / 2 / scale) // 100) * 100, int(mid_y + h / 2 / scale) + 100, 100):
            p.drawLine(QPointF(0, pt(0, gy).y()), QPointF(w, pt(0, gy).y()))

        small = QFont(self.font())
        small.setPointSize(8)
        small.setBold(True)
        p.setFont(small)
        if targets:
            p.setPen(QPen(QColor(BLUE), 2, Qt.DashLine))
            route = [pt(*q) for q in [origin] + targets]
            p.drawPolyline(route)
            for i, target in enumerate(targets):
                q = pt(*target)
                current = c.plan_active and i == c.plan_index
                p.setPen(QPen(QColor(BLUE), 2))
                p.setBrush(QColor(BLUE) if current else QColor("#0b0e12"))
                p.drawEllipse(q, 11, 11)
                p.setPen(QColor("white"))
                p.drawText(QRectF(q.x() - 11, q.y() - 11, 22, 22), Qt.AlignCenter, str(i + 1))

        if len(path) > 1:
            p.setPen(QPen(QColor(GREEN), 3, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.drawPolyline([pt(*q) for q in path])

        hq = pt(*(c.home if c.is_flying else origin))
        p.setPen(QPen(QColor(AMBER), 2))
        p.setBrush(QColor("#0b0e12"))
        p.drawRoundedRect(QRectF(hq.x() - 11, hq.y() - 11, 22, 22), 5, 5)
        p.setPen(QColor(AMBER))
        p.drawText(QRectF(hq.x() - 11, hq.y() - 11, 22, 22), Qt.AlignCenter, "H")

        p.setPen(Qt.NoPen)
        p.setBrush(QColor(RED))
        for x, y in people:
            q = pt(x, y)
            p.drawPolygon(QPolygonF([q + QPointF(0, -8), q + QPointF(8, 0), q + QPointF(0, 8), q + QPointF(-8, 0)]))

        dq = pt(c.x, c.y)
        heading = math.radians(dc.me.get_yaw() - c.yaw0)
        p.setPen(QPen(QColor("white"), 3, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(dq, dq + QPointF(18 * math.sin(heading), -18 * math.cos(heading)))
        p.setBrush(QColor("white"))
        p.setPen(Qt.NoPen)
        p.drawEllipse(dq, 6, 6)

        p.setPen(QColor(MUTED))
        p.drawText(QRectF(12, 8, w - 24, 20), Qt.AlignLeft, "MAP · squares are 1 m")
        if people:
            p.setPen(QColor(RED))
            p.drawText(QRectF(12, 8, w - 24, 20), Qt.AlignRight, f"◆ {len(people)} found")
        p.setPen(QColor("#5b6675"))
        p.drawText(QRectF(12, h - 26, w - 24, 20), Qt.AlignLeft, "Click: add point   Right-click: undo")


class MainWindow(QWidget):
    def __init__(self, controller):
        super().__init__()
        self.c = controller
        self.setWindowTitle("GÖKBAĞ")
        self.resize(1280, 800)
        self.notice_shown = ""

        # top bar
        top = QHBoxLayout()
        title = QLabel("GÖKBAĞ")
        title.setObjectName("Title")
        self.pill = QLabel()
        self.pill.setObjectName("Pill")
        self.battery = QLabel()
        self.height_chip = QLabel()
        self.signal_chip = QLabel()
        for chip in (self.battery, self.height_chip, self.signal_chip):
            chip.setObjectName("Chip")
        self.notice = QLabel()
        self.notice.setObjectName("Muted")
        self.more_button = button("☰  More", self.toggle_more, checkable=True)
        top.addWidget(title)
        top.addSpacing(12)
        top.addWidget(self.pill)
        top.addStretch()
        top.addWidget(self.notice)
        top.addSpacing(8)
        for widget in (self.battery, self.height_chip, self.signal_chip, self.more_button):
            top.addWidget(widget)

        # video + big buttons
        self.video = VideoView()
        self.takeoff_button = button("", self.c.toggle_takeoff_land, "Big", "green")
        self.home_button = button("", self.c.toggle_return_home, "Big", "amber")
        self.follow_button = button("", self.c.toggle_follow, "Big", "grey")
        self.stop_button = button("✋  STOP", self.c.hover, "Big", "red")
        self.stop_button.setToolTip("Stop moving and hover in place (motors keep running)")
        big_row = QHBoxLayout()
        big_row.setSpacing(14)
        for b in (self.takeoff_button, self.home_button, self.follow_button, self.stop_button):
            big_row.addWidget(b, 1)
        hint = QLabel("Fly with the arrow keys   ·   W / S  up and down   ·   A / D  turn")
        hint.setObjectName("Muted")
        hint.setAlignment(Qt.AlignCenter)

        main = QVBoxLayout()
        main.setSpacing(14)
        main.addLayout(top)
        main.addWidget(self.video, 1)
        main.addLayout(big_row)
        main.addWidget(hint)

        # "More" side panel
        self.more = card()
        self.more.setFixedWidth(380)
        more = QVBoxLayout(self.more)
        more.setContentsMargins(18, 18, 18, 18)
        more.setSpacing(10)

        def section(text):
            label = QLabel(text)
            label.setObjectName("Section")
            more.addSpacing(6)
            more.addWidget(label)

        section("FLIGHT PLAN")
        self.map = FlightMap(self.c)
        more.addWidget(self.map, 1)
        self.plan_label = QLabel()
        self.plan_label.setObjectName("Muted")
        more.addWidget(self.plan_label)
        plan_row = QHBoxLayout()
        self.plan_button = button("▶  Fly plan", self.c.toggle_plan)
        plan_row.addWidget(self.plan_button, 2)
        for text, slot in (("Clear", self.c.clear_plan), ("Save", self.save_plan), ("Load", self.load_plan)):
            plan_row.addWidget(button(text, slot), 1)
        more.addLayout(plan_row)

        section("CAMERA")
        camera_row = QHBoxLayout()
        self.record_button = button("⏺  Record", self.c.toggle_recording)
        for b in (button("📷  Photo", self.c.capture_image), self.record_button, button("🗺  3D map", self.c.show_3d_map)):
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
        self.detect_box = QCheckBox("Show people on the video")
        self.detect_box.setFocusPolicy(Qt.NoFocus)
        self.detect_box.setChecked(True)
        self.detect_box.toggled.connect(lambda _: self.c.toggle_yolo())
        more.addWidget(self.detect_box)

        section("KEYS")
        keys = QLabel("↑ ↓ ← →  move      W / S  up / down\nA / D  turn      E  take off      Q  land\nH  come home      SPACE  emergency motor cut")
        keys.setObjectName("Muted")
        more.addWidget(keys)
        more.addSpacing(6)
        more.addWidget(button("⚠  Emergency motor cut (Space) – drone falls!", self.c.emergency_stop, "Danger"))
        self.more.hide()

        root = QHBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(16)
        root.addLayout(main, 1)
        root.addWidget(self.more)

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
            if self.more.isVisible():
                self.map.update()
        except Exception:
            traceback.print_exc()

    def refresh(self):
        c = self.c
        text, level = c.flight_state()
        self.pill.setText(f"●  {text}")
        set_prop(self.pill, "level", level)

        battery = dc.me.get_battery()
        self.battery.setText(f"🔋 {battery}%")
        self.battery.setStyleSheet(f"color: {GREEN if battery > 50 else AMBER if battery > 20 else RED};")
        self.height_chip.setText(f"↕ {dc.me.get_height() / 100:.1f} m")
        signal = c.signal
        quality = "—" if signal is None else "Good" if signal >= 60 else "OK" if signal >= 30 else "Weak"
        self.signal_chip.setText(f"📶 {quality}")
        self.signal_chip.setToolTip(f"Wi-Fi signal-to-noise: {signal}")
        self.video.show_alert(c.top_alert())
        if c.notice != self.notice_shown:
            self.notice_shown = c.notice
            self.notice.setText(c.notice)
            QTimer.singleShot(4000, lambda shown=c.notice: self.notice.setText("") if self.notice.text() == shown else None)

        flying = c.is_flying
        self.takeoff_button.setText("🛬  Land" if flying else "🛫  Take off")
        set_prop(self.takeoff_button, "tone", "indigo" if flying else "green")
        self.takeoff_button.setEnabled(not c.busy)
        self.home_button.setText("✖  Cancel" if c.returning else "🏠  Come home")
        self.home_button.setEnabled(flying and not c.busy)
        self.follow_button.setText("👤  Following" if c.follow else "👤  Follow people")
        set_prop(self.follow_button, "tone", "blue" if c.follow else "grey")
        self.stop_button.setEnabled(flying)

        self.plan_button.setText("■  Stop plan" if c.plan_active else "▶  Fly plan")
        self.record_button.setText("■  Stop recording" if c.writer is not None else "⏺  Record")
        if c.waypoints:
            self.plan_label.setText(f"{len(c.waypoints)} point(s) · about {c.plan_length() / 100:.1f} m there and back")
        else:
            self.plan_label.setText("Click the map to add points for the drone to visit")

    # ---- actions ------------------------------------------------------------

    def toggle_more(self):
        self.more.setVisible(self.more_button.isChecked())

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
