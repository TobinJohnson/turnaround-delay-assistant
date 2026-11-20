"""
The Turnaround Delay Assistant.

Two planners produce the SAME plan format:
  1. ClaudeAgent  - an LLM agent (Claude) that investigates with read-only
                    tools and submits a recovery plan.
  2. rule_based_plan - the deterministic playbook, used when no API key is
                    set. It is also the "before AI" baseline to compare with.

Either way the plan goes to a human duty manager. Nothing is executed
automatically.

Run from the command line:
    python agent.py XA123            # Claude if credentials are set, else rules
    python agent.py XA123 --rules    # force the rule-based planner
"""
import json
import os
import sys

from turnaround import DELAY_CODES, ROLE_FOR_RESOURCE
from tools import TOOLS, Station

MODEL = os.environ.get("TURNAROUND_MODEL", "claude-opus-5-5")
MAX_STEPS = 10

SYSTEM_PROMPT = """You are a turnaround recovery assistant in the ground-handling control \
room of an Indian airport station. A turnaround has been flagged because it is projected \
to miss its scheduled departure (STD).

Your job: find the root cause, check what resources are actually free, weigh the \
passenger and network impact, and propose the smallest set of recovery actions that \
recovers the most minutes. Then call submit_recovery_plan once.

How to work:
- Start with get_turnaround_status. Use the other tools only as needed.
- Only recommend an action if the resource it needs is available, or say clearly when \
it will free up.
- Be realistic about minutes saved; don't double-count actions that overlap in time.
- Safety and regulation come first. Never recommend skipping a safety check, security \
reconciliation of baggage, or an SOP condition. Fuelling with passengers on board is \
only acceptable "per SOP with fire cover" and you must say so.
- If the delay cannot be recovered, say so and focus on protecting connections and the \
next sector.
- The duty manager is busy: write message_to_duty_manager like a short radio brief.

A human approves or rejects your plan. You cannot execute anything yourself."""


class ClaudeAgent:
    def __init__(self, station: Station, client=None, model=MODEL, on_step=None):
        import anthropic

        self.station = station
        self.client = client or anthropic.Anthropic()
        self.model = model
        self.on_step = on_step or (lambda text: None)  # callback for live progress in the UI
        self.trace = []                                 # tool calls made, for the audit log

    def plan(self, flight_id):
        messages = [{"role": "user", "content": f"Flight {flight_id} is flagged. Investigate and submit a recovery plan."}]

        for _ in range(MAX_STEPS):
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
                output_config={"effort": "medium"},
                # If a safety classifier declines, the API retries on a fallback model.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )

            if response.stop_reason == "refusal":
                raise RuntimeError("Model declined the request.")
            if response.stop_reason == "max_tokens":
                raise RuntimeError("Response was cut off (max_tokens).")

            messages.append({"role": "assistant", "content": response.content})
            tool_calls = [b for b in response.content if b.type == "tool_use"]
            if not tool_calls:
                # Claude answered in text without submitting: ask it to use the tool.
                messages.append({"role": "user", "content": "Please submit your plan with submit_recovery_plan."})
                continue

            results = []
            for call in tool_calls:
                if call.name == "submit_recovery_plan":
                    self.trace.append({"tool": call.name, "input": call.input})
                    self.on_step("Plan submitted for approval")
                    plan = dict(call.input)
                    plan["planner"] = f"Claude agent ({self.model})"
                    plan["trace"] = self.trace
                    return plan

                self.on_step(f"Calling {call.name}({json.dumps(call.input)})")
                try:
                    output = self.station.run(call.name, call.input)
                    is_error = False
                except Exception as exc:  # unknown flight, bad resource type...
                    output, is_error = f"Error: {exc}", True
                self.trace.append({"tool": call.name, "input": call.input, "output": output})
                results.append({"type": "tool_result", "tool_use_id": call.id,
                                "content": output, "is_error": is_error})

            # All tool results go back together in one user message
            messages.append({"role": "user", "content": results})

        raise RuntimeError(f"No plan after {MAX_STEPS} steps.")


def rule_based_plan(station: Station, flight_id):
    """Deterministic playbook: what a duty manager does today, without AI."""
    status = station.get_turnaround_status(flight_id)
    impact = station.get_passenger_impact(flight_id)
    f = station.board.loc[flight_id]
    code = f["cause_code"] or "93"
    delay = int(f["projected_delay_min"])

    resource = next((r for c, _, r in DELAY_CODES.values() if c == code), None)
    available = station.get_resource_availability(resource)["available"] if resource else 1

    actions, saved = [], 0
    for option in station.get_playbook(code)["standard_recovery_options"]:
        if saved >= delay:
            break
        needs_spare = "spare" in option["action"] or "second" in option["action"]
        if needs_spare and available == 0:
            continue  # can't borrow equipment that isn't there
        actions.append({"action": option["action"], "owner": ROLE_FOR_RESOURCE.get(resource, "Duty manager"),
                        "minutes_saved": option["typical_minutes_saved"], "risk": option["risk"]})
        saved += option["typical_minutes_saved"]

    issue = f["issue"] if isinstance(f["issue"], str) and f["issue"] else "Late inbound aircraft"
    after = max(0, delay - saved)
    return {
        "flight_id": flight_id,
        "root_cause": issue,
        "delay_code": code,
        "projected_delay_min": delay,
        "actions": actions,
        "expected_delay_after_min": after,
        "passenger_impact": f"{impact['connecting_pax']} connecting pax; "
                            f"connections at risk: {'yes' if impact['connections_at_risk'] else 'no'}.",
        "message_to_duty_manager": f"{flight_id} at {status['stand']}: {issue}, projected +{delay} min. "
                                   f"Playbook actions could bring it to +{after}.",
        "confidence": "medium" if actions else "low",
        "planner": "Rule-based playbook (no AI)",
        "trace": [],
    }


def has_claude_credentials():
    """True if an API key or an `ant auth login` profile is configured."""
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    return os.path.isdir(os.path.expanduser("~/.config/anthropic"))


if __name__ == "__main__":
    from data_generator import NOW

    if len(sys.argv) < 2:
        sys.exit("usage: python agent.py FLIGHT_ID [--rules]")
    flight = sys.argv[1]
    station = Station(NOW)
    if "--rules" in sys.argv or not has_claude_credentials():
        result = rule_based_plan(station, flight)
    else:
        result = ClaudeAgent(station, on_step=print).plan(flight)
    print(json.dumps({k: v for k, v in result.items() if k != "trace"}, indent=2))
