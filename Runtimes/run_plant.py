import argparse
import math
import os
import sys
import time

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sqlalchemy.orm import Session

from Data.build_test_plant import PLANT_NAME
from Data.database import get_engine
from Data.loader import load_plant
from Data.snapshots import list_snapshots, load_snapshot, save_snapshot
from Data.tags import load_tags
from Modules.PLC.bridge import AdsLink, LinkError, LoopbackLink, PlcBridge

# =====================================================================
# Runtime: loads a plant from the database and runs it in step with the PLC.
#   read commands from the PLC -> advance the plant by dt -> write feedback
# If the PLC connection is lost the plant goes to its fail-safe commands and
# the runtime keeps trying to reconnect.
# =====================================================================


def main():
    ap = argparse.ArgumentParser(description="Run a plant model against a TwinCAT PLC")
    ap.add_argument("--plant", default=PLANT_NAME)
    ap.add_argument("--dt", type=float, default=0.1, help="simulation step in seconds")
    ap.add_argument("--speed", type=float, default=1.0, help="2 = simulate twice as fast as real time")
    ap.add_argument("--duration", type=float, default=None, help="stop after this many simulated seconds")
    ap.add_argument("--ambient-mean", type=float, help="mean outdoor temperature in C")
    ap.add_argument("--daily-temperature-amplitude", type=float, help="half the day/night temperature swing in C")
    ap.add_argument("--seasonal-temperature-amplitude", type=float, help="half the summer/winter temperature swing in C")
    ap.add_argument("--slow-time-scale", type=float, help="speed up weather and gradual degradation relative to the plant")
    ap.add_argument("--ams-net-id", default="127.0.0.1.1.1")
    ap.add_argument("--port", type=int, default=851)
    ap.add_argument("--offline", action="store_true", help="no PLC: use an in-memory loopback")
    ap.add_argument("--load-snapshot", help="restore this saved snapshot before starting")
    ap.add_argument("--save-snapshot", help="save a snapshot with this name when the run ends")
    ap.add_argument("--list-snapshots", action="store_true")
    args = ap.parse_args()
    if not math.isfinite(args.dt) or args.dt <= 0:
        ap.error("--dt must be a finite positive number")
    if not math.isfinite(args.speed) or args.speed <= 0:
        ap.error("--speed must be a finite positive number")
    if args.duration is not None and (not math.isfinite(args.duration) or args.duration < 0):
        ap.error("--duration must be a finite non-negative number")
    if args.slow_time_scale is not None and (
            not math.isfinite(args.slow_time_scale) or args.slow_time_scale < 0):
        ap.error("--slow-time-scale must be a finite non-negative number")

    with Session(get_engine()) as session:
        if args.list_snapshots:
            for name, sim_time, created in list_snapshots(session, args.plant):
                print(f"{name}: sim time {sim_time:.1f} s, saved {created:%Y-%m-%d %H:%M:%S}")
            return
        plant = load_plant(session, args.plant)
        tags = load_tags(session, args.plant)
        if args.load_snapshot:
            load_snapshot(session, args.plant, plant, args.load_snapshot)
            print(f"Restored snapshot '{args.load_snapshot}' (sim time {plant.time:.1f} s)")
        if args.ambient_mean is not None:
            plant.environment.mean_c = args.ambient_mean
        if args.daily_temperature_amplitude is not None:
            plant.environment.daily_amp = args.daily_temperature_amplitude
        if args.seasonal_temperature_amplitude is not None:
            plant.environment.annual_amp = args.seasonal_temperature_amplitude
        if args.slow_time_scale is not None:
            plant.slow_time_scale = args.slow_time_scale

        link = LoopbackLink() if args.offline else AdsLink(args.ams_net_id, args.port)
        bridge = PlcBridge(plant, tags, link)
        connected = False
        start_time = plant.time
        next_tick = time.monotonic()
        try:
            while args.duration is None or plant.time - start_time < args.duration:
                if not connected:
                    try:
                        link.connect()
                        connected = True
                        print("Offline loopback active" if args.offline else "PLC connected")
                    except LinkError as err:
                        print(f"Waiting for PLC: {err}")
                        plant.apply_failsafe()
                        plant.step(args.dt)     # the plant keeps running on its fail-safe commands
                        time.sleep(1.0)
                        continue
                try:
                    bridge.read_commands()
                    plant.step(args.dt)
                    bridge.write_feedback()
                except LinkError as err:
                    print(f"PLC link lost ({err}): fail-safe")
                    connected = False
                    plant.apply_failsafe()
                    continue

                next_tick += args.dt / args.speed
                delay = next_tick - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                else:
                    next_tick = time.monotonic()
        except KeyboardInterrupt:
            print("Stopped")
        finally:
            link.close()
            if args.save_snapshot:
                save_snapshot(session, args.plant, plant, args.save_snapshot, overwrite=True)
                print(f"Saved snapshot '{args.save_snapshot}' at sim time {plant.time:.1f} s")


if __name__ == "__main__":
    main()
