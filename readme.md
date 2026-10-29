# Introduction

The GÖKBAĞ Drone Project aims to enhance drone functionalities in military applications by incorporating advanced object detection, autonomous and manual control systems, and robust communication frameworks. This README file provides essential information about the project structure, system requirements, installation procedures, and basic usage.
## System Requirements

Python 3.10 or higher (tested on 3.14)\
Libraries: djitellopy, cv2, numpy, PIL, PySide6 (Qt), ultralytics, plotly, cryptography, bcrypt (see requirements.txt)\
Compatible hardware: DJI Tello drone



## Installation
Clone the repository
```bash
git clone https://github.com/ahmetselimkaraca/drone
```
Navigate to the project directory
```bash
cd GokbagDrone
```

Install required Python libraries
```bash
pip install -r requirements.txt
```

__Using a GPU (optional):__
Person detection picks the fastest hardware automatically and shows it in the bottom-right corner ("Detection: ..."):
- NVIDIA: install a CUDA build of PyTorch (pick the command for your CUDA version at pytorch.org), e.g. `pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128`. Detection then runs in FP16 on every camera frame.
- Fastest on NVIDIA: also `pip install tensorrt`. On first start a TensorRT engine is built for your GPU (a few minutes, once) and cached as `yolo26n.engine`; delete it after changing GPU or TensorRT version.
- AMD graphics cards: install the ROCm build of PyTorch (Linux, pytorch.org); it is used like CUDA. Integrated Radeon graphics in laptops is not supported by ROCm and uses the CPU.
- Apple silicon: used automatically (MPS).
- Force a device with `GOKBAG_DEVICE=cpu`, `cuda:1`, `mps`, ...

# Project Structure
main.py / login.py: Login window; regular users open the drone controller, `admin` opens user management.\
gui.py: The ground control window (PySide6/Qt).\
drone_controller.py: All flight logic without UI: drone link, safety, tracking, return home, flight plans, logs.\
utilities/coord_csv_module.py: Writes and reads the encrypted flight logs.\
utilities/user_management.py: Password hashing and user data management.\
utilities/yolo_model.py: YOLO26 person detection (yolo26n.pt, nano size for real-time on CPU).\
utilities/fake_tello.py: Simulated Tello for testing without a drone.\
show_map_3d.py: Plots a flight log in 3D, with search & rescue sightings.\
tests/: Logic tests (`python -m unittest discover tests`).

# Usage
__Starting the Drone Controller:__
```bash
python main.py
```
__Testing without a drone:__
Set `GOKBAG_SIM` to a webcam index or a video file and the app uses a simulated Tello (fake battery/height, logged rc commands):
```bash
GOKBAG_SIM=0 python main.py                  # webcam
GOKBAG_SIM=path/to/video.mp4 python main.py  # video file, loops
```

__The main screen:__
A big video with four buttons underneath: **Take off / Land**, **Come home**, **Follow people** and a red **Stop**. Stop makes the drone stop moving and hover in place; it never turns the motors off. Battery, height and signal are shown at the top, and alerts appear over the video. The side panel holds the map, flight plans, camera, mission mode and the emergency motor cut.

__Flying by hand:__
Hold the arrow keys to move, W/S to go up/down, A/D to turn; let go and the drone hovers. Pressing a movement key always takes over from Follow, Come home and flight plans. E takes off, Q lands, H comes home.

__Follow people:__
The drone keeps the group of detected people centred (smooth proportional control) and holds a set distance. With nobody in view it turns in place to search. Detection runs in a background thread so video and controls stay smooth.

__Flight Plans (waypoints):__
Click the map in the side panel to add points (right-click removes the last one); positions are relative to the takeoff point, with "up" on the map being the direction the drone faces at takeoff. The grid is 1 m. "Fly plan" takes off if needed, flies to each waypoint in order, then returns home and lands. Combined with Search & Rescue mode this scans an area and marks sightings on the map. Movement keys, STOP, low battery or "Stop plan" end the plan. Plans can be saved to and loaded from `plans/*.json`. Like return home, positions are estimated (no GPS), so expect drift of a meter or two.

__Mission Modes:__
Operational labels people as Ally (large/close) or Hostile (small/far); this is a size heuristic, not identification. Search & Rescue labels everyone "Person Detected", saves a photo every 3 s while people are in view in flight, and marks each sighting on the flight map.

__Safety:__
- Preflight check before takeoff: telemetry link, battery at least 20%, temperature below 90°C.
- Low battery: warning at 30%, automatic return home at 20%, landing wherever it is at 10%.
- Come home (H key or button): turns toward the takeoff point, flies back and lands. There is no GPS: the position is estimated from the commands flown, so expect to land within roughly a meter or two of the takeoff point. Any manual movement key cancels it. It cannot work after a link loss, since commands use the same Wi-Fi.
- Link watchdog: shows LINK LOST when telemetry stops for 2 s (the Tello itself lands after 15 s without commands).
- Height limit of 400 cm.
- Emergency motor cut with SPACE or the red-outlined button in the side panel: the motors stop at once and the drone falls. Use it only when hovering is not safe.
- Alerts appear as a banner over the video.

__Login:__
Passwords are hashed with bcrypt; older SHA-256 records upgrade automatically on the next login. Five failed attempts lock that username for 30 s.

__Flight Data Logging:__
Flight paths are saved as encrypted CSV files in the flight_logs directory on every landing. The key is kept outside the repository at `~/.config/gokbag/flight_log.key` (override with `GOKBAG_LOG_KEY_FILE`); back it up, logs cannot be read without it. The map in the side panel shows the path during flight; Record saves the video to `recordings/`. View a log in 3D with the "3D map" button or:
```bash
python show_map_3d.py [flight_logs/<file>.csv]   # newest log if omitted
```

# Additional Information
Limits: the Tello has no obstacle sensors, so there is no obstacle avoidance. Horizontal position on the map is estimated from the commands sent (heading and height come from telemetry). Commands between the ground station and the Tello use the Tello SDK's plain UDP protocol and cannot be encrypted.\
The project's progress and updates are regularly pushed to the repository, so keep an eye on the latest commits for new features and improvements.


## Contact

For more information or to report issues, please contact the project team at\
 aselim.karaca@tedu.edu.tr\
 berkay.babayigit@tedu.edu.tr\
 burak.bilgi@tedu.edu.tr\
 eren.alsan@tedu.edu.tr