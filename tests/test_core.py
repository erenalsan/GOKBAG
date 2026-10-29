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
    # skip __init__ (connects to the drone, loads YOLO); set only what the logic uses
    c = dc.DroneController.__new__(dc.DroneController)
    c.width, c.height, c.min_area = 640, 480, 3000
    c.interval, c.speed = 0.25, 50
    c.x, c.y, c.z, c.yaw0 = 0.0, 0.0, 0, 0
    c.home, c.returning, c.rth_triggered = (0.0, 0.0), False, False
    c.waypoints, c.plan_active, c.plan_index = [], False, 0
    c.points, c.person_points = [], []
    c.prev_err_x, c.searching = 0, False
    c.pressed_keys = set()
    c.is_flying, c.busy, c.auto_landed, c.link_ok = False, False, False, True
    c.follow, c.yolo_enabled = False, True
    c.auto_rc = [0, 0, 0, 0]
    c.alerts = {}
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
        c.is_flying, c.pressed_keys = True, {"w"}
        with mock.patch.object(dc.me, "get_height", return_value=dc.MAX_HEIGHT_CM), \
                mock.patch.object(dc.me, "send_rc_control") as send:
            c.tick_control()
        send.assert_called_once_with(0, 0, 0, 0)
        self.assertIn("ceiling", c.alerts)

    def test_no_commands_while_landed_or_busy(self):
        c = make_controller()
        with mock.patch.object(dc.me, "send_rc_control") as send:
            c.tick_control()
            c.is_flying, c.busy = True, True
            c.tick_control()
        send.assert_not_called()


class SimpleControlsTest(unittest.TestCase):
    def test_follow_flies_auto_rc_only_when_enabled(self):
        c = make_controller()
        c.is_flying, c.auto_rc = True, [0, 10, 0, 20]
        with mock.patch.object(dc.me, "send_rc_control") as send:
            c.tick_control()                      # not following: hover
            c.toggle_follow()
            c.tick_control()                      # following: tracking commands
        self.assertEqual(send.call_args_list, [mock.call(0, 0, 0, 0), mock.call(0, 10, 0, 20)])

    def test_arrow_key_takes_over_and_release_hovers(self):
        c = make_controller()
        c.is_flying, c.follow, c.auto_rc = True, True, [0, 10, 0, 20]
        c.key_down("up")
        self.assertFalse(c.follow)
        self.assertEqual(c.flight_state()[0], "Flying by hand")
        with mock.patch.object(dc.me, "send_rc_control") as send:
            c.tick_control()
            c.key_up("up")
            c.tick_control()
        self.assertEqual(send.call_args_list, [mock.call(0, 50, 0, 0), mock.call(0, 0, 0, 0)])

    def test_stop_hovers_and_never_cuts_motors(self):
        c = make_controller()
        c.is_flying, c.follow, c.returning, c.plan_active = True, True, True, True
        with mock.patch.object(dc.me, "emergency") as emergency, mock.patch.object(dc.me, "send_rc_control") as send:
            c.hover()
        emergency.assert_not_called()
        send.assert_called_once_with(0, 0, 0, 0)
        self.assertFalse(c.follow or c.returning or c.plan_active)
        self.assertTrue(c.is_flying)

    def test_space_is_the_emergency_motor_cut(self):
        c = make_controller()
        c.is_flying = True
        c.save_flight_log = mock.MagicMock()
        with mock.patch.object(dc.me, "emergency") as emergency:
            c.key_down("space")
        emergency.assert_called_once()
        self.assertFalse(c.is_flying)

    def test_flight_state_texts(self):
        c = make_controller()
        self.assertEqual(c.flight_state(), ("On the ground", "idle"))
        c.is_flying = True
        self.assertEqual(c.flight_state(), ("Hovering", "ok"))
        c.link_ok = False
        self.assertEqual(c.flight_state()[1], "error")


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


class ReturnHomeTest(unittest.TestCase):
    def test_turns_toward_home_before_flying(self):
        c = make_controller()
        c.x = 100.0  # home is to the left (west) of the drone, which faces north
        with mock.patch.object(dc.me, "get_yaw", return_value=0):
            lr, fb, ud, yaw = c.return_home_rc()
        self.assertLess(yaw, 0)
        self.assertEqual(fb, 0)
        with mock.patch.object(dc.me, "get_yaw", return_value=-90):
            self.assertEqual(c.return_home_rc(), [0, dc.RTH_SPEED, 0, 0])

    def test_round_trip_lands_near_takeoff_point(self):
        c = make_controller()
        c.is_flying = True
        c.land = mock.MagicMock()
        sim = {"yaw": 0.0}

        def step(rc):  # the drone turns rc-yaw degrees per second
            sim["yaw"] = dc.wrap_degrees(sim["yaw"] + rc[3] * c.interval)
            c.log_flight_path(*rc)

        with mock.patch.object(dc.me, "get_yaw", side_effect=lambda: int(sim["yaw"])), \
                mock.patch.object(dc.me, "get_height", return_value=100):
            for rc in [[0, 50, 0, 0]] * 12 + [[0, 0, 0, 50]] * 8 + [[0, 50, 0, 0]] * 8:  # out, turn right, out
                step(rc)
            self.assertGreater(c.home_distance(), 150)
            c.start_return_home("test")
            for _ in range(400):
                if not c.returning:
                    break
                step(c.return_home_rc())
        c.land.assert_called_once()
        self.assertLess(c.home_distance(), dc.RTH_ARRIVE)

    def test_low_battery_returns_home_once_then_lands(self):
        c = make_controller()
        c.is_flying, c.x = True, 500.0
        c.land = mock.MagicMock()
        c.check_battery(dc.RTH_BATTERY)
        self.assertTrue(c.returning)
        c.cancel_return_home("operator")
        c.check_battery(dc.RTH_BATTERY - 1)
        self.assertFalse(c.returning)  # not re-triggered after the operator cancelled
        c.check_battery(dc.AUTO_LAND_BATTERY)
        c.land.assert_called_once()

    def test_manual_key_cancels_return(self):
        c = make_controller()
        c.is_flying, c.returning = True, True
        c.key_down("up")
        self.assertFalse(c.returning)


class FlightPlanTest(unittest.TestCase):
    def fly(self, c, steps):
        sim = {"yaw": 0.0}
        visited = []
        with mock.patch.object(dc.me, "get_yaw", side_effect=lambda: int(sim["yaw"])), \
                mock.patch.object(dc.me, "get_height", return_value=100):
            for _ in range(steps):
                if c.returning:
                    rc = c.return_home_rc()
                elif c.plan_active:
                    index = c.plan_index
                    rc = c.plan_rc()
                    if c.plan_index != index:
                        visited.append(index)
                else:
                    break
                sim["yaw"] = dc.wrap_degrees(sim["yaw"] + rc[3] * c.interval)
                c.log_flight_path(*rc)
        return visited

    def test_visits_waypoints_in_order_then_returns_home_and_lands(self):
        c = make_controller()
        c.is_flying = True
        c.land = mock.MagicMock()
        c.waypoints = [(0, 200), (200, 200), (200, 0)]  # a square: forward, right, back
        c.toggle_plan()
        visited = self.fly(c, 800)
        self.assertEqual(visited, [0, 1, 2])
        c.land.assert_called_once()
        self.assertLess(c.home_distance(), dc.RTH_ARRIVE)

    def test_waypoints_are_relative_to_takeoff_point(self):
        c = make_controller()
        c.x, c.y = 300.0, 400.0  # landed here; this becomes home at takeoff
        c.waypoints = [(100, 0)]
        self.assertEqual(c.plan_targets(), [(400.0, 400.0)])

    def test_run_while_landed_takes_off_and_cancelled_by_failed_preflight(self):
        c = make_controller()
        c.waypoints = [(0, 100)]
        c.takeoff = mock.MagicMock(side_effect=lambda: setattr(c, "busy", True))
        c.toggle_plan()
        c.takeoff.assert_called_once()
        self.assertTrue(c.plan_active)
        c2 = make_controller()
        c2.waypoints = [(0, 100)]
        c2.takeoff = mock.MagicMock()  # preflight refused: never busy
        c2.toggle_plan()
        self.assertFalse(c2.plan_active)

    def test_manual_key_and_low_battery_stop_the_plan(self):
        c = make_controller()
        c.is_flying, c.plan_active, c.waypoints = True, True, [(0, 500)]
        c.key_down("left")
        self.assertFalse(c.plan_active)
        c.plan_active, c.x = True, 300.0
        c.check_battery(dc.RTH_BATTERY)
        self.assertFalse(c.plan_active)
        self.assertTrue(c.returning)

    def test_plan_files_are_validated(self):
        d = tempfile.mkdtemp()
        good, bad, far = (os.path.join(d, n) for n in ("good.json", "bad.json", "far.json"))
        with open(good, "w") as f:
            f.write('{"waypoints": [[0, 100], [50.5, 0]]}')
        with open(bad, "w") as f:
            f.write('{"waypoints": [["rm -rf", 1]]}')
        with open(far, "w") as f:
            f.write('{"waypoints": [[0, 99999]]}')
        self.assertEqual(dc.parse_plan(good), [(0.0, 100.0), (50.5, 0.0)])
        self.assertRaises(ValueError, dc.parse_plan, bad)
        self.assertRaises(ValueError, dc.parse_plan, far)


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
