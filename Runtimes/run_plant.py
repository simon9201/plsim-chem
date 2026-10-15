# Runtimes/run_plant.py
import argparse
import os
import sys
import time

# Setup for a dynamic search path.
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sqlalchemy.orm import Session

from Data.database import get_engine
from Data.loader import load_plant
from Data.tags import load_tags
from Modules.PLC.bridge import AdsLink, LinkError, LoopbackLink, PlcBridge

DEFAULT_PLANT = "test_plant_3tank_reactor"
RECONNECT_INTERVAL = 2.0    # s between reconnect attempts


def parse_args():
    p = argparse.ArgumentParser(description="Run a stored plant against a TwinCAT PLC.")
    p.add_argument("--plant", default=DEFAULT_PLANT, help="name of the plant in the database")
    p.add_argument("--dt", type=float, default=0.1, help="simulation step in seconds (default 0.1)")
    p.add_argument("--speed", type=float, default=1.0, help="1 = real time, 2 = twice as fast")
    p.add_argument("--duration", type=float, default=0.0, help="stop after this many simulated seconds (0 = run until Ctrl+C)")
    p.add_argument("--print-every", type=float, default=5.0, help="status line interval in simulated seconds")
    p.add_argument("--ams-net-id", default="127.0.0.1.1.1", help="AMS Net ID of the TwinCAT runtime")
    p.add_argument("--port", type=int, default=851, help="ADS port of the PLC runtime (851 = first PLC)")
    p.add_argument("--offline", action="store_true", help="no PLC: all commands stay at 0 (closed/stopped)")
    return p.parse_args()


def status_line(plant):
    m = plant.read_instruments()
    r = plant.equipment["R201"]
    return (f"t={plant.time:7.1f}s  TK101={m['LT101']:5.1f}%  TK102={m['LT102']:5.1f}%  "
            f"TK103={m['LT103']:5.1f}%  R201: level={m['LT201']:5.1f}%  T={m['TT201']:5.1f}C  "
            f"[C]={r.concentration('C'):5.2f} mol/L  out={m['FT201']:4.2f} L/s")


def main():
    args = parse_args()
    with Session(get_engine()) as session:
        plant = load_plant(session, args.plant)
        tags = load_tags(session, args.plant)
    if not tags:
        sys.exit("The plant has no PLC tags. Run Data/build_test_plant.py first.")

    link = LoopbackLink() if args.offline else AdsLink(args.ams_net_id, args.port)
    bridge = PlcBridge(plant, tags, link)

    connected = False
    try:
        link.connect()
        connected = True
        print(f"Connected to the PLC ({'offline' if args.offline else f'{args.ams_net_id}:{args.port}'})")
    except LinkError as err:
        print(f"{err}\nStarting anyway with fail-safe commands; will keep trying to connect.")
        plant.apply_failsafe()

    print(f"Runtime started: plant '{plant.name}', {len(plant.equipment)} equipment, "
          f"{len(plant.instruments)} instruments, {len(tags)} PLC tags. Ctrl+C stops.")

    tick = args.dt / args.speed
    next_tick = time.perf_counter()
    next_print = 0.0
    next_reconnect = 0.0
    try:
        while args.duration <= 0 or plant.time < args.duration:
            if connected:
                try:
                    bridge.read_commands()
                except LinkError as err:
                    print(f"t={plant.time:.1f}s  PLC connection lost ({err}). Actuators go to fail-safe.")
                    plant.apply_failsafe()
                    connected = False
                    next_reconnect = plant.time + RECONNECT_INTERVAL
            elif plant.time >= next_reconnect:
                try:
                    link.connect()
                    connected = True
                    print(f"t={plant.time:.1f}s  PLC connection restored.")
                except LinkError:
                    next_reconnect = plant.time + RECONNECT_INTERVAL

            plant.step(args.dt)

            if connected:
                try:
                    bridge.write_feedback()
                except LinkError as err:
                    print(f"t={plant.time:.1f}s  PLC connection lost ({err}). Actuators go to fail-safe.")
                    plant.apply_failsafe()
                    connected = False
                    next_reconnect = plant.time + RECONNECT_INTERVAL

            if plant.time >= next_print:
                print(status_line(plant))
                next_print += args.print_every

            next_tick += tick
            delay = next_tick - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            elif delay < -1.0:
                print(f"t={plant.time:.1f}s  Running behind real time by {-delay:.1f}s (lower --speed or raise --dt)")
                next_tick = time.perf_counter()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        link.close()


if __name__ == "__main__":
    main()
