"""
Generate one synthetic operating day of A320 turnarounds at one station,
plus 60 days of delay history and a live GSE / staff resource snapshot.

The data is fictional (airline "XA", fictional tails) but the structure
mirrors what a ground-handling control room sees: schedule, ETA, task
progress, delay codes and resource availability.

Times are stored as minutes after midnight (e.g. 14:05 -> 845).

Run:  python data_generator.py
"""
import random
from pathlib import Path

import pandas as pd

from turnaround import DELAY_CODES, DEPENDS_ON, LATE_INBOUND, STANDARD_PLAN

DATA_DIR = Path(__file__).parent / "data"
STATION = "BLR"
NOW = 14 * 60  # snapshot time: 14:00


def hhmm(minutes):
    minutes = int(round(minutes))
    return f"{(minutes // 60) % 24:02d}:{minutes % 60:02d}"


def schedule(on_block, overruns):
    """Start/end of every task, respecting dependencies. overruns: {task: extra minutes}."""
    times = {}
    for task, start_off, end_off in STANDARD_PLAN:
        earliest = on_block + start_off
        for dep in DEPENDS_ON.get(task, []):
            earliest = max(earliest, times[dep][1])
        times[task] = (earliest, earliest + (end_off - start_off) + overruns.get(task, 0))
    return times


def generate_day(seed=7, n_flights=48):
    rng = random.Random(seed)
    flights, tasks = [], []

    # Hand-made scenarios around the snapshot time, so the demo always has
    # a mix of live problems: (minutes from NOW to STA, ground time,
    # inbound delay, disrupted task, overrun minutes)
    scenarios = [
        (-30, 45, 0, "Baggage offload", 16),   # belt loader broke mid-offload
        (-25, 50, 0, "Catering", 22),          # catering truck late
        (-20, 45, 6, "Fuelling", 12),          # late inbound + slow fuelling
        (-30, 50, 0, "Boarding", 14),          # missing passengers
        (10, 45, 25, None, 0),                 # inbound 25 min late
        (40, 50, 18, None, 0),                 # inbound 18 min late, some slack
    ]

    for i in range(n_flights):
        flight_id = f"XA{101 + i}"
        sta = 6 * 60 + i * 20 + rng.randint(-5, 5)          # spread 06:00 -> ~22:00
        ground_time = rng.choice([45, 45, 50, 55, 60])        # scheduled ground time

        # Late inbound aircraft (code 93) for ~20% of flights
        inbound_delay = rng.choice([0] * 8 + [rng.randint(5, 30) for _ in range(2)])

        # One ground-handling disruption for ~35% of flights
        disrupted_task = None
        overrun = 0
        if rng.random() < 0.35:
            disrupted_task = rng.choice(list(DELAY_CODES))
            overrun = rng.randint(6, 25)

        # Replace the flights closest to NOW with the demo scenarios
        first_scenario = (NOW - 6 * 60) // 20 - 2
        if first_scenario <= i < first_scenario + len(scenarios):
            offset, ground_time, inbound_delay, disrupted_task, overrun = scenarios[i - first_scenario]
            sta = NOW + offset

        std = sta + ground_time
        on_block = sta + inbound_delay

        total_pax = rng.randint(120, 186)
        flights.append({
            "flight_id": flight_id,
            "tail": f"VT-X{chr(65 + i % 12)}{chr(65 + (i * 7) % 26)}",
            "station": STATION,
            "stand": f"S{rng.randint(1, 18)}",
            "sta": sta,
            "std": std,
            "eta": on_block,
            "inbound_delay_min": inbound_delay,
            "total_pax": total_pax,
            "connecting_pax": rng.randint(0, 25),
            "tightest_connection_min": rng.choice([35, 45, 60, 75, 90, 120]),
            "next_sector_slack_min": rng.choice([0, 5, 10, 15, 20]),
            "disrupted_task": disrupted_task or "",
            "true_overrun_min": overrun,
        })

        # Real times: a task starts at its standard offset after on-block,
        # but never before the tasks it depends on have finished.
        true_times = schedule(on_block, {disrupted_task: overrun} if disrupted_task else {})
        disruption_started = bool(disrupted_task) and true_times[disrupted_task][0] <= NOW
        # Control room only knows about a problem once that task has started
        visible = {disrupted_task: overrun} if disruption_started else {}
        est_times = schedule(on_block, visible)

        for task, start_off, end_off in STANDARD_PLAN:
            actual_start, actual_end = true_times[task]
            if NOW >= actual_end:
                status = "done"
            elif NOW >= actual_start:
                status = "in_progress"
            else:
                status = "not_started"
            has_issue = task in visible

            tasks.append({
                "flight_id": flight_id,
                "task": task,
                "planned_start": sta + start_off,
                "planned_end": sta + end_off,
                "actual_start": actual_start if status != "not_started" else None,
                "estimated_start": est_times[task][0],
                "estimated_end": est_times[task][1],
                "status": status,
                "reported_issue": DELAY_CODES[task][1] if has_issue else "",
                "delay_code": DELAY_CODES[task][0] if has_issue else "",
            })

    return pd.DataFrame(flights), pd.DataFrame(tasks)


def generate_resources(seed=7):
    rng = random.Random(seed + 1)
    rows = []
    totals = {
        "belt_loader": 8, "passenger_steps": 10, "catering_truck": 5,
        "cleaning_crew": 6, "fuel_bowser": 4, "ramp_staff": 30,
        "gate_staff": 12, "load_controller": 3,
    }
    for resource, total in totals.items():
        unserviceable = rng.randint(0, max(1, total // 4)) if resource in ("belt_loader", "passenger_steps", "catering_truck", "fuel_bowser") else 0
        in_use = rng.randint(total // 2, total - unserviceable)
        available = total - unserviceable - in_use
        rows.append({
            "resource": resource,
            "total": total,
            "in_use": in_use,
            "unserviceable": unserviceable,
            "available": available,
            "next_free_in_min": 0 if available > 0 else rng.randint(5, 25),
            "nearest_idle_stand": f"S{rng.randint(1, 18)}" if available > 0 else "",
        })
    return pd.DataFrame(rows)


def generate_history(seed=7, days=60):
    """60 days of delay events by IATA code, for root-cause (Pareto) analysis."""
    rng = random.Random(seed + 2)
    # relative frequency and typical minutes per event, roughly what a busy station sees
    profile = {
        "93": (0.30, 18), "18": (0.14, 9), "33": (0.12, 12), "37": (0.10, 10),
        "15": (0.10, 7), "36": (0.08, 11), "35": (0.07, 8), "31": (0.05, 6), "34": (0.04, 9),
    }
    codes = list(profile)
    weights = [profile[c][0] for c in codes]
    descriptions = {v[0]: v[1] for v in DELAY_CODES.values()}
    descriptions[LATE_INBOUND[0]] = LATE_INBOUND[1]

    rows = []
    for day in range(days):
        for _ in range(rng.randint(10, 22)):
            code = rng.choices(codes, weights)[0]
            minutes = max(3, int(rng.gauss(profile[code][1], profile[code][1] * 0.4)))
            rows.append({"day": day + 1, "delay_code": code,
                         "description": descriptions[code], "delay_min": minutes})
    return pd.DataFrame(rows)


def write_all(seed=7):
    DATA_DIR.mkdir(exist_ok=True)
    flights, tasks = generate_day(seed)
    flights.to_csv(DATA_DIR / "flights.csv", index=False)
    tasks.to_csv(DATA_DIR / "tasks.csv", index=False)
    generate_resources(seed).to_csv(DATA_DIR / "resources.csv", index=False)
    generate_history(seed).to_csv(DATA_DIR / "delay_history.csv", index=False)
    return flights, tasks


if __name__ == "__main__":
    f, t = write_all()
    print(f"Wrote {len(f)} flights, {len(t)} task rows to {DATA_DIR}/ (snapshot {hhmm(NOW)})")
