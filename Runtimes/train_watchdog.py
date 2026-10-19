import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

import numpy as np

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from Modules.Watchdog.model import anomaly_path
from Modules.Watchdog.recorder import load_csv, plant_sampler
from Modules.Watchdog.scenarios import FAILURE_MODES, LAYOUTS, TARGET, build_plant, run_episode
from Modules.Watchdog.dataset import rising_edges
from Modules.Watchdog.training import (_evaluate_recordings, fit, optional_channels, parts_from_recordings,
                                       select_alarm_threshold, train_watchdog)
from Modules.Watchdog.watchdog import Watchdog


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _file_provenance(path):
    return {"name": os.path.basename(path), "sha256": _sha256_file(path)}


# =====================================================================
# Train the stop-prediction watchdog and save it (a .npz file the live watchdog loads).
#
#   python Runtimes\train_watchdog.py                      simulated "bad day" episodes (Modules/Watchdog/scenarios.py)
#   python Runtimes\train_watchdog.py --episodes 400 --corpus Data\corpus --workers 8
#                                                          the same, generated once in parallel and cached (corpus.py)
#   python Runtimes\train_watchdog.py --campaigns 24 --workers 8
#                                                          also train on long runs where incidents arrive at random
#                                                          times under day/night weather (scenarios.run_campaign)
#   python Runtimes\train_watchdog.py --csv a.csv b.csv --target P101.Fault
#                                                          recordings made with run_plant.py --record
# Afterwards it replays one unseen simulated episode and reports how early the alarm came before the stop.
# =====================================================================


def replay(model, detector, seed, alarm_threshold):
    times, matrix, names = run_episode(seed, sample_period=model.period)
    stops = rising_edges(times, matrix[:, names.index(model.target)])
    wd = Watchdog(model, detector, alarm_threshold=alarm_threshold)
    first_alarm = None
    for i, t in enumerate(times):
        report = wd.update(t, dict(zip(names, matrix[i])))
        if report and report.alarm and first_alarm is None and (len(stops) == 0 or t < stops[0]):
            first_alarm = t
    if len(stops):
        lead = f"{stops[0] - first_alarm:.0f} s before the stop" if first_alarm is not None else "no warning"
        print(f"Replay (seed {seed}): stop at {stops[0]:.0f} s, alarm {lead}")
    else:
        print(f"Replay (seed {seed}): no stop; false alarm: {first_alarm is not None}")


def add_plant_residuals(recordings, plant_name):
    """Append the physics residual channels (flow balance, resistance) found in the stored plant layout."""
    from sqlalchemy.orm import Session
    from Data.database import get_engine
    from Modules.Editor.document import PlantDocument
    from Modules.Watchdog.residuals import add_residuals, discover_residuals
    with Session(get_engine()) as session:
        doc = PlantDocument(session, plant_name)
        specs = discover_residuals(doc.nodes(), doc.edges(), doc.probes())
    print(f"Residual channels: {[s.name for s in specs]}")
    return [add_residuals(t, m, n, specs) for t, m, n in recordings]


def main():
    ap = argparse.ArgumentParser(description="Train the stop-prediction watchdog")
    ap.add_argument("--out", default=os.path.join(PROJECT_ROOT, "Data", "watchdog_model.npz"))
    ap.add_argument("--episodes", type=int, default=40, help="simulated training episodes")
    ap.add_argument("--validation-episodes", type=int, default=20,
                    help="synthetic episodes used to select an alert threshold")
    ap.add_argument("--test-episodes", type=int, default=20, help="simulated episodes held out for evaluation")
    ap.add_argument("--corpus", help="folder caching the simulated episodes (built once, reused, e.g. Data\\corpus)")
    ap.add_argument("--workers", type=int, default=1, help="parallel processes for generating episodes")
    ap.add_argument("--campaigns", type=int, default=0,
                    help="long multi-incident 'training plant' runs added to training (a third as many to validation/test)")
    ap.add_argument("--campaign-hours", type=float, default=6.0, help="simulated hours per campaign")
    ap.add_argument("--csv", nargs="*", help="train from recordings instead of simulating")
    ap.add_argument("--validation-csv", nargs="*", help="separate historian recordings for threshold selection")
    ap.add_argument("--test-csv", nargs="*", help="independent historian recordings for held-out evaluation")
    ap.add_argument("--target", default=TARGET, help="channel that is 1 while the stop is active")
    ap.add_argument("--horizon", type=float, default=600.0, help="seconds ahead to predict")
    ap.add_argument("--window", type=int, default=60, help="samples per window")
    ap.add_argument("--period", type=float, default=5.0, help="seconds between samples")
    ap.add_argument("--alarm-threshold", type=float, default=0.8)
    ap.add_argument("--max-false-alarms-per-hour", type=float, default=0.1,
                    help="validation-set false-alarm budget used to choose the operating threshold")
    ap.add_argument("--model", choices=("boosted", "logistic"), default="boosted", help="learner (default: boosted trees)")
    ap.add_argument("--plant", help="with --csv: add leak/blockage residual channels discovered from this stored plant")
    args = ap.parse_args()

    if (args.validation_csv or args.test_csv) and not args.csv:
        ap.error("--validation-csv and --test-csv require --csv")

    training_sources = {}
    selected_threshold = args.alarm_threshold
    if args.csv:
        recordings = [load_csv(p) for p in args.csv]
        if args.plant:
            recordings = add_plant_residuals(recordings, args.plant)
        parts, channels = parts_from_recordings(recordings, args.target, args.horizon, args.window)
        model, detector, metrics = fit(parts, channels, args.window, args.period, args.horizon, args.target, args.model,
                                       optional=optional_channels(recordings, args.target))
        training_sources["training_csv"] = [
            _file_provenance(p) for p in args.csv]
        if args.validation_csv:
            validation_recordings = [load_csv(p) for p in args.validation_csv]
            if args.plant:
                validation_recordings = add_plant_residuals(validation_recordings, args.plant)
            validation_parts, _ = parts_from_recordings(validation_recordings, args.target, args.horizon, args.window)
            if not validation_parts:
                ap.error("The validation recordings did not produce any threshold-selection windows")
            selected_threshold, validation_metrics = select_alarm_threshold(
                model, [(None, r) for r in validation_recordings], args.max_false_alarms_per_hour)
            metrics["validation_selected_threshold"] = selected_threshold
            metrics["validation_false_alarms_per_hour"] = validation_metrics["false_alarms_per_hour"]
            metrics["validation_event_recall"] = validation_metrics["event_recall"]
            metrics["validation_alert_precision"] = validation_metrics["alert_precision"]
            metrics["validation_budget_met"] = (
                validation_metrics["false_alarms_per_hour"] is not None
                and validation_metrics["alarm_episodes"] > 0
                and validation_metrics["false_alarms_per_hour"] <= args.max_false_alarms_per_hour)
        metrics["selected_alarm_threshold"] = selected_threshold
        if args.test_csv:
            test_recordings = [load_csv(p) for p in args.test_csv]
            if args.plant:
                test_recordings = add_plant_residuals(test_recordings, args.plant)
            test_parts, _ = parts_from_recordings(test_recordings, args.target, args.horizon, args.window)
            if not test_parts:
                ap.error("The held-out recordings did not produce any evaluation windows")
            Xt, yt = np.vstack([part[0] for part in test_parts]), np.concatenate([part[1] for part in test_parts])
            metrics.update(test_windows=len(yt), test_positive=int(yt.sum()))
            evaluated = _evaluate_recordings(model, [(None, record) for record in test_recordings],
                                             selected_threshold)
            metrics.update({f"test_{k}": v for k, v in evaluated.items() if k != "by_failure_mode"})
            metrics["test_by_failure_mode"] = evaluated["by_failure_mode"]
            training_sources["test_csv"] = [
                _file_provenance(p) for p in args.test_csv]
        if args.validation_csv:
            training_sources["validation_csv"] = [
                _file_provenance(p) for p in args.validation_csv]
    else:
        train_seeds = list(range(args.episodes))
        validation_seeds = list(range(1000, 1000 + args.validation_episodes))
        test_seeds = list(range(2000, 2000 + args.test_episodes))
        model, detector, metrics = train_watchdog(train_seeds, test_seeds, args.target, args.horizon, args.window, 3,
                                                  args.period, kind=args.model, alarm_threshold=args.alarm_threshold,
                                                  validation_seeds=validation_seeds,
                                                  max_false_alarms_per_hour=args.max_false_alarms_per_hour,
                                                  corpus=args.corpus, workers=args.workers,
                                                  campaigns=args.campaigns, campaign_hours=args.campaign_hours)
        selected_threshold = metrics["selected_alarm_threshold"]
        training_sources = {"kind": "synthetic", "train_seeds": train_seeds,
                            "validation_seeds": validation_seeds, "test_seeds": test_seeds,
                            "campaigns": args.campaigns, "campaign_hours": args.campaign_hours,
                            "failure_modes": list(FAILURE_MODES), "layouts": list(LAYOUTS)}
    for key, value in metrics.items():
        if isinstance(value, float):
            print(f"{key}: {value:.6g}" if "threshold" in key else f"{key}: {value:.3f}")
        elif isinstance(value, (dict, list)):
            print(f"{key}: {json.dumps(value, sort_keys=True)}")
        else:
            print(f"{key}: {value}")
    training_config = {"target": args.target, "horizon": args.horizon, "window": args.window,
                       "period": args.period, "learner": args.model}
    model.metadata = {
        "trained_at_utc": datetime.now(timezone.utc).isoformat(),
        "dataset_sha256": hashlib.sha256(json.dumps(
            {"sources": training_sources, "config": training_config}, sort_keys=True).encode("utf-8")).hexdigest(),
        "scenario_generator_sha256": _sha256_file(os.path.join(PROJECT_ROOT, "Modules", "Watchdog", "scenarios.py")),
        "training_sources": training_sources,
        "training_config": training_config,
        "input_channels_sha256": hashlib.sha256("\n".join(model.channels).encode("utf-8")).hexdigest(),
        "model_metrics": metrics,
        "evaluation_note": "Risk score is not a calibrated probability; validation metrics require representative, held-out data.",
        "selected_alarm_threshold": selected_threshold,
        "selected_anomaly_threshold": metrics.get("selected_anomaly_threshold", 0.75),
        "false_alarm_budget_per_hour": args.max_false_alarms_per_hour,
    }
    model.save(args.out)
    print(f"Saved model to {args.out}")
    if detector is not None:
        detector.save(anomaly_path(args.out))
        print(f"Saved normal-only detector to {anomaly_path(args.out)}")
    elif os.path.exists(anomaly_path(args.out)):
        os.remove(anomaly_path(args.out))     # never leave a detector that belongs to an older model
    if not args.csv:
        for seed in range(2000, 2006):
            replay(model, detector, seed, selected_threshold)


if __name__ == "__main__":
    main()
