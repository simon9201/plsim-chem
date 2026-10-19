import argparse
import os
import signal
import sys
import time

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sqlalchemy.orm import Session

from Data.build_test_plant import PLANT_NAME
from Data.database import get_engine
from Data.tags import load_tags
from Modules.Entity.Entity import TO_PLC
from Modules.PLC.bridge import AdsLink, LinkError
from Modules.Watchdog.model import RiskModel
from Modules.Watchdog.recorder import SignalRecorder, link_sampler
from Modules.Watchdog.watchdog import Watchdog

# =====================================================================
# Live watchdog: a separate process that only READS the PLC tags over ADS (the same variables the
# simulation writes), feeds them to a trained model, and prints an alarm when a stop looks likely.
# It never writes to the PLC. Run it next to run_plant.py:
#
#   python Runtimes\run_watchdog.py --model Data\watchdog_model.npz
#   python Runtimes\run_watchdog.py --model ... --record live.csv     (also keep the tags for later training)
#
# The model's channels ("P101.Current", ...) must exist in the plant's tag table.
# =====================================================================


def main():
    ap = argparse.ArgumentParser(description="Watch PLC tags and warn before stoppages")
    ap.add_argument("--plant", default=PLANT_NAME)
    ap.add_argument("--model", default=os.path.join(PROJECT_ROOT, "Data", "watchdog_model.npz"))
    ap.add_argument("--ams-net-id", default="127.0.0.1.1.1")
    ap.add_argument("--port", type=int, default=851)
    ap.add_argument("--alarm-threshold", type=float, help="override the threshold selected during validation")
    ap.add_argument("--record", help="also save every sampled tag to this CSV when stopped")
    args = ap.parse_args()
    if hasattr(signal, "SIGBREAK"):     # Windows: lets launcher.py stop this process cleanly (CTRL_BREAK -> Stopped)
        signal.signal(signal.SIGBREAK, signal.default_int_handler)

    model = RiskModel.load(args.model)
    with Session(get_engine()) as session:
        tags = load_tags(session, args.plant)
    known = {f"{t.entity_tag}.{t.signal}" for t in tags if t.direction == TO_PLC}
    missing = [c for c in model.channels if c not in known]
    if missing:
        sys.exit(f"The model needs channels that are not PLC feedback tags in plant '{args.plant}': {missing}")
    if model.target not in known:
        sys.exit(f"The model target '{model.target}' is not a PLC feedback tag in plant '{args.plant}'")

    link = AdsLink(args.ams_net_id, args.port)
    sample = link_sampler(link, tags, TO_PLC)
    threshold = args.alarm_threshold
    if threshold is None:
        threshold = model.metadata.get("selected_alarm_threshold", 0.8)
    watchdog = Watchdog(model, alarm_threshold=threshold)
    recorder = SignalRecorder(model.period) if args.record else None
    start = time.monotonic()
    alarm = False
    metadata = model.metadata
    try:
        link.connect()
        trained = metadata.get("trained_at_utc", "unknown")
        print(f"Model {model.model_version} trained {trained}; alert threshold {threshold:.6g}; "
              f"watching {len(model.channels)} feedback channels, predicting '{model.target}' "
              f"{model.horizon:.0f} s ahead")
        if metadata.get("model_metrics", {}).get("validation_budget_met") is False:
            print("WARNING: validation found no alerting threshold within the configured false-alarm budget; "
                  "this artifact is configured not to raise predictive alarms.")
        elif "validation_selected_threshold" not in metadata.get("model_metrics", {}):
            print("WARNING: this artifact has no validation-selected threshold; the alert threshold may be manual.")
        while True:
            t = time.monotonic() - start
            try:
                values = sample()
            except LinkError as err:
                print(f"PLC link lost ({err}); retrying")
                time.sleep(2.0)
                try:
                    link.connect()
                except LinkError:
                    pass
                continue
            if recorder is not None:
                recorder.record(t, values)
            report = watchdog.update(t, values)
            if report and report.alarm != alarm:
                alarm = report.alarm
                if alarm:
                    why = ", ".join(f"{name} ({c:+.1f})" for name, c in report.reasons)
                    print(f"[{t:8.0f} s] WARNING: stop of {model.target} likely within {model.horizon:.0f} s "
                          f"(risk {report.risk:.2f}). Main drivers: {why}")
                else:
                    print(f"[{t:8.0f} s] risk back to normal ({report.risk:.2f})")
            time.sleep(model.period)
    except (KeyboardInterrupt, LinkError) as err:
        print("Stopped" if isinstance(err, KeyboardInterrupt) else f"Stopped: {err}")
    finally:
        link.close()
        if recorder is not None and recorder.rows:
            recorder.save_csv(args.record)
            print(f"Saved {len(recorder.rows)} samples to {args.record}")


if __name__ == "__main__":
    main()
