"""
Rule-based delay detector: which turnarounds will miss their STD?

Logic (simple on purpose, so it is easy to explain):
  1. A turn cannot be shorter than the minimum ground time (45 min),
     so a late inbound pushes the earliest possible off-block.
  2. Tasks run in parallel but some depend on others (bags can't load
     until offload ends, boarding waits for cleaning and catering). A
     reported overrun pushes every task after it on that chain. The turn
     is ready when "Doors closed & pushback" ends.
  3. projected_delay = max(0, projected_ready - STD)

RAG status: green < 5 min, amber 5-14 min, red >= 15 min.
"""
import pandas as pd

from turnaround import MIN_TURN_MIN

FLAG_THRESHOLD_MIN = 5


def rag(delay):
    if delay >= 15:
        return "red"
    if delay >= FLAG_THRESHOLD_MIN:
        return "amber"
    return "green"


def project_delays(flights: pd.DataFrame, tasks: pd.DataFrame, now: int) -> pd.DataFrame:
    latest_task_end = tasks.groupby("flight_id")["estimated_end"].max()
    issues = (tasks[tasks["reported_issue"].fillna("") != ""]
              .groupby("flight_id")
              .agg(issue_task=("task", "first"), issue=("reported_issue", "first"),
                   issue_code=("delay_code", "first")))

    df = flights.set_index("flight_id").join(latest_task_end).join(issues)
    df["projected_ready"] = df[["estimated_end"]].max(axis=1).clip(lower=df["eta"] + MIN_TURN_MIN)
    df["projected_delay_min"] = (df["projected_ready"] - df["std"]).clip(lower=0).astype(int)
    df["rag"] = df["projected_delay_min"].apply(rag)

    # Main cause: a reported ground issue, otherwise a late inbound
    df["cause_code"] = df["issue_code"].where(df["issue_code"].notna() & (df["issue_code"] != ""),
                                              df["inbound_delay_min"].gt(0).map({True: "93", False: ""}))
    df["cause_code"] = df["cause_code"].fillna("").astype(str).str.replace(r"\.0$", "", regex=True)

    # Stage of the turn at the snapshot time
    df["phase"] = "departed"
    df.loc[df["projected_ready"] > now, "phase"] = "on ground"
    df.loc[df["eta"] > now, "phase"] = "inbound"

    return df.reset_index()


def flagged(board: pd.DataFrame, now: int, horizon_min: int = 120) -> pd.DataFrame:
    """Flights still actionable (not departed, STD within horizon) with delay >= threshold."""
    live = board[(board["phase"] != "departed") & (board["std"] <= now + horizon_min)]
    return live[live["projected_delay_min"] >= FLAG_THRESHOLD_MIN].sort_values("std")
