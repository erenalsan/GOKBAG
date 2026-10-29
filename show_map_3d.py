import glob
import os
import sys
from itertools import groupby
import plotly.graph_objects as go
import utilities.coord_csv_module as ccsv

INTERVAL = 0.25  # seconds per logged point, same as DroneController.interval


def collapse_points(coordinates):
    # ardisik ayni noktalari tek noktaya indirip, her noktada kac saniye kalindigini buluyoruz
    return [point + (len(list(group)) * INTERVAL,) for point, group in groupby(coordinates)]


if __name__ == "__main__":
    # usage: python show_map_3d.py [log.csv]   (defaults to the newest log)
    logs = glob.glob('flight_logs/*.csv')
    if len(sys.argv) < 2 and not logs:
        sys.exit("No flight logs yet: fly and land once first.")
    log_file = sys.argv[1] if len(sys.argv) > 1 else max(logs, key=os.path.getmtime)
    path, sightings = ccsv.read_flight_log(log_file)
    if not path:
        sys.exit(f"{log_file} has no movement recorded.")
    coordinates = collapse_points(path)

    x, y, z, time = zip(*coordinates)

    traces = [go.Scatter3d(x=x, y=y, z=z, mode='markers', name='Flight path', marker=dict(
            size=8,
            color=time,
            colorscale='Bluered',
            opacity=0.7,
        ), hovertemplate='(%{x}, %{y}, %{z})<br>Time: %{marker.color} seconds<br>')]
    if sightings:
        sx, sy, sz = zip(*sightings)
        traces.append(go.Scatter3d(x=sx, y=sy, z=sz, mode='markers', name='Person spotted',
                                   marker=dict(size=10, color='red', symbol='diamond'),
                                   hovertemplate='Person spotted near (%{x}, %{y}, %{z})<extra></extra>'))

    fig = go.Figure(data=traces)
    fig.update_layout(title=log_file, scene=dict(xaxis_title='X Axis', yaxis_title='Y Axis', zaxis_title='Z Axis'))

    fig.show()
