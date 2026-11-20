"""
Read-only tools the agent can call to investigate a delayed turnaround.

Every tool only READS data. The agent cannot change anything in the
operation: it can only submit a recovery plan, which a human duty
manager approves or rejects. That is the human-in-the-loop design.
"""
import json
from pathlib import Path

import pandas as pd

from data_generator import hhmm
from detector import project_delays
from turnaround import PLAYBOOK

DATA_DIR = Path(__file__).parent / "data"


class Station:
    """Snapshot of the station: flights, task progress, resources, history."""

    def __init__(self, now, data_dir=DATA_DIR):
        self.now = now
        self.flights = pd.read_csv(data_dir / "flights.csv")
        self.tasks = pd.read_csv(data_dir / "tasks.csv").fillna({"reported_issue": "", "delay_code": ""})
        self.tasks["delay_code"] = self.tasks["delay_code"].astype(str).str.replace(r"\.0$", "", regex=True)
        self.resources = pd.read_csv(data_dir / "resources.csv").fillna({"nearest_idle_stand": ""})
        self.history = pd.read_csv(data_dir / "delay_history.csv", dtype={"delay_code": str})
        self.board = project_delays(self.flights, self.tasks, now).set_index("flight_id")

    # ---- tools -------------------------------------------------------------

    def get_turnaround_status(self, flight_id):
        if flight_id not in self.board.index:
            raise ValueError(f"Unknown flight {flight_id}")
        f = self.board.loc[flight_id]
        tasks = self.tasks[self.tasks["flight_id"] == flight_id]
        return {
            "flight_id": flight_id,
            "stand": f["stand"],
            "snapshot_time": hhmm(self.now),
            "sta": hhmm(f["sta"]), "std": hhmm(f["std"]), "on_block_or_eta": hhmm(f["eta"]),
            "inbound_delay_min": int(f["inbound_delay_min"]),
            "projected_ready": hhmm(f["projected_ready"]),
            "projected_delay_min": int(f["projected_delay_min"]),
            "tasks": [
                {
                    "task": t["task"],
                    "status": t["status"],
                    "planned": f"{hhmm(t['planned_start'])}-{hhmm(t['planned_end'])}",
                    "estimated": f"{hhmm(t['estimated_start'])}-{hhmm(t['estimated_end'])}",
                    "reported_issue": t["reported_issue"],
                    "delay_code": t["delay_code"],
                }
                for _, t in tasks.iterrows()
            ],
        }

    def get_resource_availability(self, resource_type):
        rows = self.resources[self.resources["resource"] == resource_type]
        if rows.empty:
            raise ValueError(f"Unknown resource type {resource_type}. "
                             f"Valid: {', '.join(self.resources['resource'])}")
        r = rows.iloc[0]
        return {k: (v.item() if hasattr(v, "item") else v) for k, v in r.to_dict().items()}

    def get_passenger_impact(self, flight_id):
        f = self.board.loc[flight_id]
        delay = int(f["projected_delay_min"])
        connection_margin = int(f["tightest_connection_min"]) - delay
        return {
            "flight_id": flight_id,
            "total_pax": int(f["total_pax"]),
            "connecting_pax": int(f["connecting_pax"]),
            "tightest_connection_margin_after_delay_min": connection_margin,
            "connections_at_risk": connection_margin < 30,
            "next_sector_slack_min": int(f["next_sector_slack_min"]),
            "reactionary_delay_next_sector_min": max(0, delay - int(f["next_sector_slack_min"])),
        }

    def get_delay_history(self, delay_code):
        h = self.history[self.history["delay_code"] == str(delay_code)]
        total = self.history["delay_min"].sum()
        return {
            "delay_code": str(delay_code),
            "events_last_60_days": int(len(h)),
            "avg_delay_min": round(float(h["delay_min"].mean()), 1) if len(h) else 0,
            "share_of_all_delay_minutes_pct": round(100 * h["delay_min"].sum() / total, 1) if total else 0,
        }

    def get_playbook(self, delay_code):
        options = PLAYBOOK.get(str(delay_code), [])
        return {
            "delay_code": str(delay_code),
            "standard_recovery_options": [
                {"action": a, "typical_minutes_saved": m, "risk": r} for a, m, r in options
            ],
        }

    def run(self, name, args):
        """Dispatch a tool call by name. Returns a JSON string."""
        fn = {
            "get_turnaround_status": lambda: self.get_turnaround_status(args["flight_id"]),
            "get_resource_availability": lambda: self.get_resource_availability(args["resource_type"]),
            "get_passenger_impact": lambda: self.get_passenger_impact(args["flight_id"]),
            "get_delay_history": lambda: self.get_delay_history(args["delay_code"]),
            "get_playbook": lambda: self.get_playbook(args["delay_code"]),
        }[name]
        return json.dumps(fn(), default=str)


# ---- tool definitions sent to Claude --------------------------------------

RESOURCE_TYPES = ["belt_loader", "passenger_steps", "catering_truck", "cleaning_crew",
                  "fuel_bowser", "ramp_staff", "gate_staff", "load_controller"]


def _schema(props, required):
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


TOOLS = [
    {
        "name": "get_turnaround_status",
        "description": "Live status of one turnaround: STD, on-block/ETA, projected delay, and every "
                       "ground task with planned vs estimated times, status and any reported issue "
                       "with its IATA delay code. Call this first.",
        "input_schema": _schema({"flight_id": {"type": "string"}}, ["flight_id"]),
        "strict": True,
    },
    {
        "name": "get_resource_availability",
        "description": "How many units of a ground resource are free, in use or unserviceable right "
                       "now, when the next one frees up, and the nearest idle stand to borrow from.",
        "input_schema": _schema({"resource_type": {"type": "string", "enum": RESOURCE_TYPES}},
                                ["resource_type"]),
        "strict": True,
    },
    {
        "name": "get_passenger_impact",
        "description": "Passenger and network impact of the projected delay: connecting passengers, "
                       "connection margin after the delay, and knock-on (reactionary) delay to the "
                       "aircraft's next sector.",
        "input_schema": _schema({"flight_id": {"type": "string"}}, ["flight_id"]),
        "strict": True,
    },
    {
        "name": "get_delay_history",
        "description": "60-day history for an IATA delay code at this station: number of events, "
                       "average minutes, and share of all delay minutes. Use it to judge whether "
                       "this is a recurring root cause.",
        "input_schema": _schema({"delay_code": {"type": "string"}}, ["delay_code"]),
        "strict": True,
    },
    {
        "name": "get_playbook",
        "description": "The station's standard recovery options for an IATA delay code, with typical "
                       "minutes saved and the risk of each. Options are suggestions, not orders: "
                       "check resources before recommending one.",
        "input_schema": _schema({"delay_code": {"type": "string"}}, ["delay_code"]),
        "strict": True,
    },
    {
        "name": "submit_recovery_plan",
        "description": "Submit the final recovery plan to the duty manager for approval. Call this "
                       "exactly once, at the end. Nothing is executed until a human approves it.",
        "input_schema": _schema({
            "flight_id": {"type": "string"},
            "root_cause": {"type": "string", "description": "One sentence: what is causing the delay."},
            "delay_code": {"type": "string", "description": "Main IATA delay code."},
            "projected_delay_min": {"type": "integer"},
            "actions": {
                "type": "array",
                "items": _schema({
                    "action": {"type": "string"},
                    "owner": {"type": "string", "description": "Role that carries it out."},
                    "minutes_saved": {"type": "integer"},
                    "risk": {"type": "string"},
                }, ["action", "owner", "minutes_saved", "risk"]),
            },
            "expected_delay_after_min": {"type": "integer"},
            "passenger_impact": {"type": "string"},
            "message_to_duty_manager": {"type": "string",
                                        "description": "Short radio-style brief, max 3 sentences."},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        }, ["flight_id", "root_cause", "delay_code", "projected_delay_min", "actions",
            "expected_delay_after_min", "passenger_impact", "message_to_duty_manager", "confidence"]),
        "strict": True,
    },
]
