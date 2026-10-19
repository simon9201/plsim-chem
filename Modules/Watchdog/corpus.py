import json
import os
from concurrent.futures import ProcessPoolExecutor

import numpy as np

from Modules.Watchdog.scenarios import GENERATOR_VERSION, episode_plan, run_episode

# =====================================================================
# A corpus is a folder of simulated episodes, one .npz per seed, so a big training set is generated once (in
# parallel, resumable) and reused for every training and tuning run:
#
#   build_corpus("Data/corpus", range(0, 2000), workers=8)     generate whatever is missing
#   load_corpus("Data/corpus", range(0, 2000))                 [(label, (times, matrix, names)), ...]
#
# `label` is (failure mode, plant family) so results can be broken down by both. An episode is regenerated if
# it was made by a different generator version, sample period or length, so stale files are never mixed in.
# =====================================================================


def _label(seed):
    plan = episode_plan(seed)
    return plan["mode"], plan["family"]


def _job(args):
    seed, sample_period, max_seconds = args
    return seed, run_episode(seed, max_seconds=max_seconds, sample_period=sample_period)


def _path(directory, seed):
    return os.path.join(directory, f"episode_{seed:07d}.npz")


def _meta(seed, sample_period, max_seconds):
    mode, family = _label(seed)
    return {"seed": int(seed), "mode": mode, "family": family, "generator_version": GENERATOR_VERSION,
            "sample_period": float(sample_period), "max_seconds": float(max_seconds)}


def _is_current(path, seed, sample_period, max_seconds):
    try:
        with np.load(path, allow_pickle=False) as data:
            meta = json.loads(str(data["meta"]))
    except (OSError, ValueError, KeyError):
        return False
    return meta == _meta(seed, sample_period, max_seconds)


def generate_recordings(seeds, sample_period=5.0, max_seconds=7200.0, workers=1):
    """Simulate episodes in memory. Returns [(label, (times, matrix, names))] in seed order."""
    seeds = list(seeds)
    jobs = [(seed, sample_period, max_seconds) for seed in seeds]
    if workers > 1 and len(jobs) > 1:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(_job, jobs, chunksize=max(1, len(jobs) // (workers * 4))))
    else:
        results = [_job(job) for job in jobs]
    return [(_label(seed), arrays) for seed, arrays in results]


def build_corpus(directory, seeds, sample_period=5.0, max_seconds=7200.0, workers=1, batch=64, progress=None):
    """Generate every missing or outdated episode into `directory`. Returns the number generated."""
    os.makedirs(directory, exist_ok=True)
    todo = [s for s in seeds if not _is_current(_path(directory, s), s, sample_period, max_seconds)]
    done = 0
    for start in range(0, len(todo), batch):
        chunk = todo[start:start + batch]
        jobs = [(seed, sample_period, max_seconds) for seed in chunk]
        if workers > 1 and len(jobs) > 1:
            with ProcessPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(_job, jobs))
        else:
            results = [_job(job) for job in jobs]
        for seed, (times, matrix, names) in results:
            np.savez_compressed(_path(directory, seed), times=times, matrix=matrix.astype(np.float32),
                                names=json.dumps(names), meta=json.dumps(_meta(seed, sample_period, max_seconds)))
        done += len(chunk)
        if progress:
            progress(done, len(todo))
    return len(todo)


def load_corpus(directory, seeds):
    """Load episodes written by build_corpus. Raises if one is missing: build the corpus first."""
    records = []
    for seed in seeds:
        path = _path(directory, seed)
        if not os.path.exists(path):
            raise FileNotFoundError(f"Episode {seed} is not in the corpus {directory}; run build_corpus first")
        with np.load(path, allow_pickle=False) as data:
            meta = json.loads(str(data["meta"]))
            records.append(((meta["mode"], meta["family"]),
                            (data["times"], data["matrix"].astype(float), json.loads(str(data["names"])))))
    return records
