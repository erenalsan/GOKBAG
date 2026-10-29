"""Logic tests that need no drone, camera or window: python -m unittest discover tests"""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
os.environ["GOKBAG_SIM"] = "0"  # FakeTello; the camera only opens on streamon(), which tests never call
os.environ["GOKBAG_LOG_KEY_FILE"] = os.path.join(tempfile.mkdtemp(), "test.key")

import drone_controller as dc
import utilities.coord_csv_module as ccsv
from utilities import user_management as um
from show_map_3d import collapse_points


def make_controller():
    # skip __init__ (connects, builds the window, loads YOLO); set only what the logic uses
    c = dc.DroneController.__new__(dc.DroneController)
    c.width, c.height, c.min_area = 640, 480, 3000
    c.interval, c.speed = 0.25, 50
    c.x, c.y, c.z, c.yaw0 = 0.0, 0.0, 0, 0
    c.points, c.person_points = [], []
    c.prev_err_x, c.searching = 0, False
    c.pressed_keys = set()
    c.is_flying, c.busy, c.auto_landed, c.link_ok = False, False, False, True
    c.manual_control_enabled, c.yolo_enabled = False, True
    c.auto_rc = [0, 0, 0, 0]
    c.alerts = {}
    c.refresh_alert = lambda: None
    c.root = mock.MagicMock()
    c.last_state, c.last_state_time = None, time.monotonic()
    return c


def box(cx, cy, size):
    return (cx - size // 2, cy - size // 2, cx + size // 2, cy + size // 2)


class TrackingTest(unittest.TestCase):
    def test_target_right_and_above_turns_right_and_climbs(self):
        rc = make_controller().compute_auto_rc([box(560, 60, 100)])  # area 10000: inside target range
        self.assertGreater(rc[3], 0)
        self.assertGreater(rc[2], 0)
        self.assertEqual(rc[1], 0)

    def test_centred_target_holds_still(self):
        self.assertEqual(make_controller().compute_auto_rc([box(320, 240, 120)]), [0, 0, 0, 0])

    def test_far_target_approaches_and_close_target_backs_off(self):
        c = make_controller()
        self.assertGreater(c.compute_auto_rc([box(320, 240, 60)])[1], 0)   # 3600 px²: far
        self.assertLess(c.compute_auto_rc([box(320, 240, 300)])[1], 0)     # 90000 px²: too close

    def test_outputs_are_clamped(self):
        rc = make_controller().compute_auto_rc([box(639, 0, 60)])
        self.assertLessEqual(abs(rc[3]), 60)
        self.assertLessEqual(abs(rc[2]), 40)
        self.assertLessEqual(abs(rc[1]), 30)

    def test_nobody_visible_searches_by_turning(self):
        c = make_controller()
        self.assertEqual(c.compute_auto_rc([]), [0, 0, 0, dc.SEARCH_YAW])
        self.assertTrue(c.searching)
        # tiny boxes are ignored for tracking, so still searching
        self.assertEqual(c.compute_auto_rc([box(320, 240, 20)]), [0, 0, 0, dc.SEARCH_YAW])


class ControlTest(unittest.TestCase):
    def test_keyboard_mapping(self):
        c = make_controller()
        c.pressed_keys = {"up", "a", "x"}
        self.assertEqual(c.get_keyboard_input(), [0, 50, 0, -50])

    def test_flight_path_uses_drone_heading(self):
        c = make_controller()
        with mock.patch.object(dc.me, "get_yaw", return_value=90), mock.patch.object(dc.me, "get_height", return_value=120):
            c.log_flight_path(0, 40, 0, 0)  # forward while facing 90° clockwise -> +x
        self.assertAlmostEqual(c.x, 10)
        self.assertAlmostEqual(c.y, 0, places=6)
        self.assertEqual(c.z, 120)

    def test_climb_blocked_above_height_limit(self):
        c = make_controller()
        c.is_flying, c.manual_control_enabled, c.pressed_keys = True, True, {"w"}
        c.draw_minimap = lambda: None
        with mock.patch.object(dc.me, "get_height", return_value=dc.MAX_HEIGHT_CM), \
                mock.patch.object(dc.me, "send_rc_control") as send:
            c.control_loop()
        send.assert_called_once_with(0, 0, 0, 0)
        self.assertIn("ceiling", c.alerts)

    def test_no_commands_while_landed_or_busy(self):
        c = make_controller()
        c.draw_minimap = lambda: None
        with mock.patch.object(dc.me, "send_rc_control") as send:
            c.control_loop()
            c.is_flying, c.busy = True, True
            c.control_loop()
        send.assert_not_called()


class SafetyTest(unittest.TestCase):
    def test_link_lost_after_timeout_and_recovers(self):
        c = make_controller()
        dc.me.link_lost = False
        c.check_link()
        self.assertTrue(c.link_ok)
        dc.me.link_lost = True
        c.last_state_time -= dc.LINK_TIMEOUT + 0.1
        c.check_link()
        self.assertFalse(c.link_ok)
        self.assertIn("link", c.alerts)
        dc.me.link_lost = False
        c.check_link()
        self.assertTrue(c.link_ok)
        self.assertNotIn("link", c.alerts)

    def test_low_battery_lands_once(self):
        c = make_controller()
        c.is_flying = True
        c.land = mock.MagicMock()
        c.check_battery(dc.AUTO_LAND_BATTERY)
        c.check_battery(dc.AUTO_LAND_BATTERY - 1)
        c.land.assert_called_once()

    def test_battery_warning(self):
        c = make_controller()
        c.check_battery(dc.LOW_BATTERY_WARNING)
        self.assertEqual(c.alerts["battery"][0], "warn")
        c.check_battery(80)
        self.assertNotIn("battery", c.alerts)

    def test_preflight_blocks_takeoff_on_low_battery(self):
        c = make_controller()
        c.run_drone_command = mock.MagicMock()
        with mock.patch.object(dc.me, "get_battery", return_value=dc.MIN_TAKEOFF_BATTERY - 1):
            c.takeoff()
        c.run_drone_command.assert_not_called()
        self.assertIn("preflight", c.alerts)


class FlightLogTest(unittest.TestCase):
    def test_round_trip_with_sightings_never_writes_plaintext(self):
        f = os.path.join(tempfile.mkdtemp(), "log.csv")
        ccsv.make_csv([(1.0, 2.0, 3.0), (4.5, 5.0, 6.0)], f, detections=[(4.5, 5.0, 6.0)])
        self.assertEqual(ccsv.read_flight_log(f), ([(1.0, 2.0, 3.0), (4.5, 5.0, 6.0)], [(4.5, 5.0, 6.0)]))
        with open(f, "rb") as raw:
            self.assertNotIn(b"4.5", raw.read())

    def test_legacy_2024_log_still_readable(self):
        path, sightings = ccsv.read_flight_log("flight_logs/2024-06-07_01-06-23.csv")
        self.assertEqual(path[0], (500.0, 500.0, 0.0))
        self.assertEqual(sightings, [])

    def test_collapse_points_counts_seconds(self):
        self.assertEqual(collapse_points([(0, 0, 0), (0, 0, 0), (1, 0, 0)]), [(0, 0, 0, 0.5), (1, 0, 0, 0.25)])


class PasswordTest(unittest.TestCase):
    def test_bcrypt_verify(self):
        record = um.make_record("secret")
        self.assertTrue(record["password"].startswith("$2"))
        self.assertTrue(um.verify_password("secret", record))
        self.assertFalse(um.verify_password("wrong", record))
        self.assertFalse(um.needs_rehash(record))

    def test_legacy_sha256_record_verifies_and_needs_rehash(self):
        salt = os.urandom(16)
        import hashlib
        record = {"password": hashlib.sha256(salt + b"old").hexdigest(), "salt": salt.hex()}
        self.assertTrue(um.verify_password("old", record))
        self.assertFalse(um.verify_password("nope", record))
        self.assertTrue(um.needs_rehash(record))


if __name__ == "__main__":
    unittest.main()
