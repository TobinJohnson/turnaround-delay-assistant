"""
Build the sample airline's data from a simple, hand-editable schedule.

You edit:   sample_data/schedule.csv   (one row per flight, times as HH:MM)
            sample_data/resources.csv  (equipment and staff on shift)
This script writes the files the dashboard reads:
            sample_data/flights.csv, sample_data/tasks.csv
            sample_data/delay_history.csv (only if it doesn't exist yet)

The airline ("XK", Demo Regional Air) and its data are fictional.
Snapshot time is 14:00, the same as the simulated day.

schedule.csv columns:
  flight_id, route, tail, stand      free text
  sta, std                           scheduled arrival / departure, HH:MM
  inbound_delay_min                  how late the aircraft arrives (0 = on time)
  problem_task, problem_extra_min    one ground task that overruns, and by how much
                                     (leave problem_task empty for a clean turn).
                                     Task names: see STANDARD_PLAN in turnaround.py
  total_pax, connecting_pax          passengers on the departing flight
  tightest_connection_min            shortest onward connection, minutes
  next_sector_slack_min              buffer in the aircraft's next sector, minutes

Run:  python build_sample.py
"""
import random
from pathlib import Path

import pandas as pd

from data_generator import NOW, schedule
from turnaround import DELAY_CODES, LATE_INBOUND, STANDARD_PLAN

SAMPLE_DIR = Path(__file__).parent / "sample_data"
STATION = "TRV"


def to_min(hhmm_text):
    h, m = str(hhmm_text).strip().split(":")
    return int(h) * 60 + int(m)


def build_flights_and_tasks(sched: pd.DataFrame, now=NOW):
    flights, tasks = [], []
    valid_tasks = {t for t, _, _ in STANDARD_PLAN}
    for row in sched.itertuples(index=False):
        sta, std = to_min(row.sta), to_min(row.std)
        inbound_delay = int(row.inbound_delay_min or 0)
        on_block = sta + inbound_delay
        problem = row.problem_task if isinstance(row.problem_task, str) and row.problem_task.strip() else ""
        if problem and problem not in valid_tasks:
            raise ValueError(f"{row.flight_id}: unknown problem_task '{problem}'. Use one of: {sorted(valid_tasks)}")
        extra = int(row.problem_extra_min or 0) if problem else 0

        flights.append({
            "flight_id": row.flight_id, "tail": row.tail, "station": STATION, "stand": row.stand,
            "sta": sta, "std": std, "eta": on_block, "inbound_delay_min": inbound_delay,
            "total_pax": int(row.total_pax), "connecting_pax": int(row.connecting_pax),
            "tightest_connection_min": int(row.tightest_connection_min),
            "next_sector_slack_min": int(row.next_sector_slack_min),
            "disrupted_task": problem, "true_overrun_min": extra,
        })

        # Same logic as the simulated day: the control room only sees a
        # problem once that task has started.
        true_times = schedule(on_block, {problem: extra} if problem else {})
        visible = {problem: extra} if problem and true_times[problem][0] <= now else {}
        est_times = schedule(on_block, visible)

        for task, start_off, end_off in STANDARD_PLAN:
            actual_start, actual_end = true_times[task]
            status = "done" if now >= actual_end else "in_progress" if now >= actual_start else "not_started"
            has_issue = task in visible
            tasks.append({
                "flight_id": row.flight_id, "task": task,
                "planned_start": sta + start_off, "planned_end": sta + end_off,
                "actual_start": actual_start if status != "not_started" else None,
                "estimated_start": est_times[task][0], "estimated_end": est_times[task][1],
                "status": status,
                "reported_issue": DELAY_CODES[task][1] if has_issue else "",
                "delay_code": DELAY_CODES[task][0] if has_issue else "",
            })
    return pd.DataFrame(flights), pd.DataFrame(tasks)


def build_history(days=30, seed=11):
    """30 days of delay events for a small regional station (fixed seed, so it never changes)."""
    rng = random.Random(seed)
    profile = {"93": (0.32, 16), "33": (0.15, 11), "18": (0.12, 8), "37": (0.11, 9),
               "15": (0.10, 6), "36": (0.08, 10), "35": (0.06, 7), "34": (0.04, 8), "31": (0.02, 5)}
    descriptions = {v[0]: v[1] for v in DELAY_CODES.values()}
    descriptions[LATE_INBOUND[0]] = LATE_INBOUND[1]
    codes, weights = list(profile), [p[0] for p in profile.values()]
    rows = []
    for day in range(1, days + 1):
        for _ in range(rng.randint(3, 8)):
            code = rng.choices(codes, weights)[0]
            minutes = max(3, int(rng.gauss(profile[code][1], profile[code][1] * 0.4)))
            rows.append({"day": day, "delay_code": code, "description": descriptions[code], "delay_min": minutes})
    return pd.DataFrame(rows)


def build_all(sample_dir=SAMPLE_DIR):
    sched = pd.read_csv(sample_dir / "schedule.csv").fillna({"problem_task": "", "problem_extra_min": 0})
    flights, tasks = build_flights_and_tasks(sched)
    flights.to_csv(sample_dir / "flights.csv", index=False)
    tasks.to_csv(sample_dir / "tasks.csv", index=False)
    if not (sample_dir / "delay_history.csv").exists():
        build_history().to_csv(sample_dir / "delay_history.csv", index=False)
    return flights, tasks


if __name__ == "__main__":
    f, t = build_all()
    print(f"Wrote {len(f)} flights and {len(t)} task rows to {SAMPLE_DIR}/")
