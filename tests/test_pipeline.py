"""
Tests for the whole pipeline. No API key needed: the Claude agent is tested
with a fake client that replays a scripted conversation.

Run:  pytest -q
"""
import sys
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).parent.parent))

import data_generator
from agent import ClaudeAgent, rule_based_plan
from detector import flagged
from tools import Station

data_generator.write_all()
STATION = Station(data_generator.NOW)


def test_standard_plan_fits_minimum_turn():
    times = data_generator.schedule(0, {})
    assert times["Doors closed & pushback"][1] == 45


def test_overrun_on_critical_path_delays_departure():
    # offload ends 18 -> 28; loading had 2 min buffer, so departure moves +8
    times = data_generator.schedule(0, {"Baggage offload": 10})
    assert times["Doors closed & pushback"][1] == 53


def test_detector_flags_demo_flights():
    live = flagged(STATION.board.reset_index(), data_generator.NOW)
    assert len(live) >= 4
    assert {"33", "37", "93"} <= set(live["cause_code"])


def test_rule_based_plan_is_complete():
    plan = rule_based_plan(STATION, "XA123")
    assert plan["delay_code"] == "33"
    assert plan["actions"]
    assert plan["expected_delay_after_min"] <= plan["projected_delay_min"]


def test_tool_errors_are_reported_not_raised_to_user():
    try:
        STATION.run("get_turnaround_status", {"flight_id": "NOPE"})
    except ValueError as exc:
        assert "Unknown flight" in str(exc)


class FakeClient:
    """Mimics client.beta.messages.create with a scripted list of responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)


def tool_use(id_, name, input_):
    return NS(type="tool_use", id=id_, name=name, input=input_)


def test_claude_agent_loop_with_fake_client():
    plan_input = {
        "flight_id": "XA123", "root_cause": "Belt loader failed during offload", "delay_code": "33",
        "projected_delay_min": 14,
        "actions": [{"action": "Move spare belt loader from S13", "owner": "GSE controller",
                     "minutes_saved": 8, "risk": "S13 next arrival in 40 min"}],
        "expected_delay_after_min": 6, "passenger_impact": "No connections at risk",
        "message_to_duty_manager": "XA123 S-stand, belt loader down. Spare from S13, +6.",
        "confidence": "high",
    }
    client = FakeClient([
        NS(stop_reason="tool_use", content=[
            tool_use("t1", "get_turnaround_status", {"flight_id": "XA123"}),
            tool_use("t2", "get_resource_availability", {"resource_type": "belt_loader"}),
        ]),
        NS(stop_reason="tool_use", content=[tool_use("t3", "submit_recovery_plan", plan_input)]),
    ])
    plan = ClaudeAgent(STATION, client=client).plan("XA123")

    assert plan["expected_delay_after_min"] == 6
    assert [t["tool"] for t in plan["trace"]] == [
        "get_turnaround_status", "get_resource_availability", "submit_recovery_plan"]
    # both tool results were sent back in ONE user message
    second_request = client.requests[1]["messages"]
    assert len(second_request[-1]["content"]) == 2
    assert all(r["type"] == "tool_result" for r in second_request[-1]["content"])
