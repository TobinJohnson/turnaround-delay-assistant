"""
Turnaround Delay Assistant - control-room dashboard.

Run:  streamlit run app.py
"""
from datetime import datetime
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

import data_generator
from agent import ClaudeAgent, has_claude_credentials, rule_based_plan
from data_generator import NOW, hhmm
from detector import flagged
from tools import Station

DATA_DIR = Path(__file__).parent / "data"
DECISIONS = DATA_DIR / "decisions.csv"
RAG_COLORS = {"red": "#d64545", "amber": "#e0a100", "green": "#2e9e5b"}

st.set_page_config(page_title="Turnaround Delay Assistant", page_icon="✈️", layout="wide")

# ---------------------------------------------------------------- data -----
if not (DATA_DIR / "flights.csv").exists():
    data_generator.write_all()


@st.cache_data
def load_station(seed):
    data_generator.write_all(seed)
    return Station(NOW)


with st.sidebar:
    st.header("Control room")
    st.caption(f"Station BLR · snapshot {hhmm(NOW)} · fictional airline XA")
    seed = st.number_input("Scenario seed", value=7, step=1,
                           help="Change to generate a different (random) operating day.")
    creds = has_claude_credentials()
    use_ai = st.toggle("Use Claude agent", value=creds, disabled=not creds,
                       help="Needs ANTHROPIC_API_KEY. Without it the rule-based playbook is used.")
    if not creds:
        st.info("No API key found: running the rule-based playbook. "
                "Set ANTHROPIC_API_KEY to enable the AI agent.")

station = load_station(int(seed))
board = station.board.reset_index()
live = flagged(board, NOW)

st.title("✈️ Turnaround Delay Assistant")
st.caption("Flags turnarounds that will miss STD and drafts recovery actions for the duty manager to approve.")

tab_board, tab_desk, tab_rca, tab_process, tab_case = st.tabs(
    ["Live board", "Recovery desk", "Root cause (Pareto)", "Process map", "Business case"])

# ---------------------------------------------------------- live board -----
with tab_board:
    window = board[(board["std"] >= NOW - 30) & (board["std"] <= NOW + 180)].copy()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Flights in next 3 h", int((window["phase"] != "departed").sum()))
    c2.metric("Flagged (≥5 min)", len(live))
    c3.metric("Red (≥15 min)", int((live["rag"] == "red").sum()))
    c4.metric("Delay minutes at risk", int(live["projected_delay_min"].sum()))

    view = pd.DataFrame({
        "Flight": window["flight_id"], "Stand": window["stand"],
        "STA": window["sta"].map(hhmm), "STD": window["std"].map(hhmm),
        "On-block / ETA": window["eta"].map(hhmm),
        "Phase": window["phase"],
        "Projected delay (min)": window["projected_delay_min"],
        "Status": window["rag"].str.upper(),
        "Cause code": window["cause_code"],
        "Reported issue": window["issue"].fillna("").where(window["cause_code"] != "93", "Late inbound aircraft"),
    })

    def color_status(val):
        return f"background-color: {RAG_COLORS.get(val.lower(), '')}; color: white" if val else ""

    st.dataframe(view.style.map(color_status, subset=["Status"]), hide_index=True, width="stretch")

# -------------------------------------------------------- recovery desk ----
with tab_desk:
    if live.empty:
        st.success("No flights flagged right now.")
    else:
        options = {f"{r.flight_id} · STD {hhmm(r.std)} · +{r.projected_delay_min} min ({r.rag})": r.flight_id
                   for r in live.itertuples()}
        flight_id = options[st.selectbox("Flagged flight", list(options))]
        status = station.get_turnaround_status(flight_id)

        left, right = st.columns([3, 2])
        with left:
            st.subheader(f"{flight_id} at stand {status['stand']}")
            st.write(f"STD **{status['std']}** · projected ready **{status['projected_ready']}** · "
                     f"delay **+{status['projected_delay_min']} min**")
            t = station.tasks[station.tasks["flight_id"] == flight_id]
            gantt = pd.concat([
                pd.DataFrame({"task": t["task"], "start": t["planned_start"], "end": t["planned_end"], "kind": "Planned"}),
                pd.DataFrame({"task": t["task"], "start": t["estimated_start"], "end": t["estimated_end"], "kind": "Estimated now"}),
            ])
            base_day = pd.Timestamp("2026-01-01")
            for col in ("start", "end"):
                gantt[col] = base_day + pd.to_timedelta(gantt[col], unit="m")
            order = list(t["task"])
            chart = alt.Chart(gantt).mark_bar(height=9).encode(
                x=alt.X("start:T", title="Time", axis=alt.Axis(format="%H:%M")),
                x2="end:T",
                y=alt.Y("task:N", sort=order, title=None),
                yOffset="kind:N",
                color=alt.Color("kind:N", scale=alt.Scale(domain=["Planned", "Estimated now"], range=["#9aa5b1", "#d64545"]), title=None),
                tooltip=["task", "kind", alt.Tooltip("start:T", format="%H:%M"), alt.Tooltip("end:T", format="%H:%M")],
            )
            now_rule = alt.Chart(pd.DataFrame({"x": [base_day + pd.Timedelta(minutes=NOW)]})).mark_rule(strokeDash=[4, 4]).encode(x="x:T")
            std_time = base_day + pd.Timedelta(minutes=int(station.board.loc[flight_id, "std"]))
            std_rule = alt.Chart(pd.DataFrame({"x": [std_time]})).mark_rule(color="#d64545").encode(x="x:T")
            st.altair_chart(chart + now_rule + std_rule, width="stretch")
            st.caption("Dashed line = now. Red line = STD.")
            issues = t[t["reported_issue"] != ""]
            for _, row in issues.iterrows():
                st.warning(f"Reported: **{row['task']}**: {row['reported_issue']} (IATA {row['delay_code']})")
            if status["inbound_delay_min"]:
                st.warning(f"Inbound aircraft late by {status['inbound_delay_min']} min (IATA 93)")

        with right:
            st.subheader("Recovery plan")
            plans = st.session_state.setdefault("plans", {})
            if st.button("Draft recovery plan", type="primary"):
                if use_ai:
                    with st.status("Agent investigating…", expanded=True) as box:
                        try:
                            plans[flight_id] = ClaudeAgent(station, on_step=box.write).plan(flight_id)
                            box.update(label="Plan ready", state="complete")
                        except Exception as exc:
                            box.update(label="Agent failed, using playbook", state="error")
                            st.error(f"{type(exc).__name__}: {exc}")
                            plans[flight_id] = rule_based_plan(station, flight_id)
                else:
                    plans[flight_id] = rule_based_plan(station, flight_id)

            plan = plans.get(flight_id)
            if plan:
                st.caption(plan["planner"])
                st.info(plan["message_to_duty_manager"])
                st.write(f"**Root cause:** {plan['root_cause']} (IATA {plan['delay_code']})")
                st.write(f"**Delay:** +{plan['projected_delay_min']} → **+{plan['expected_delay_after_min']} min** "
                         f"· confidence {plan['confidence']}")
                st.dataframe(pd.DataFrame(plan["actions"]), hide_index=True, width="stretch")
                st.write(f"**Passengers:** {plan['passenger_impact']}")
                if plan["trace"]:
                    with st.expander("Agent reasoning trail (tool calls)"):
                        for step in plan["trace"]:
                            st.code(f"{step['tool']}({step['input']})", language="text")

                note = st.text_input("Duty manager note (optional)")
                a, r = st.columns(2)
                decision = "approved" if a.button("✅ Approve") else "rejected" if r.button("❌ Reject") else None
                if decision:
                    row = pd.DataFrame([{
                        "logged_at": datetime.now().isoformat(timespec="seconds"),
                        "flight_id": flight_id, "decision": decision, "planner": plan["planner"],
                        "delay_code": plan["delay_code"], "delay_before": plan["projected_delay_min"],
                        "delay_after": plan["expected_delay_after_min"],
                        "actions": " | ".join(x["action"] for x in plan["actions"]), "note": note,
                    }])
                    row.to_csv(DECISIONS, mode="a", header=not DECISIONS.exists(), index=False)
                    st.success(f"Plan {decision} and logged.")

    if DECISIONS.exists():
        st.subheader("Decision log")
        st.dataframe(pd.read_csv(DECISIONS).iloc[::-1], hide_index=True, width="stretch")

# ----------------------------------------------------------- root cause ----
with tab_rca:
    st.subheader("Where do our delay minutes come from? (last 60 days)")
    h = station.history.groupby(["delay_code", "description"], as_index=False)["delay_min"].sum()
    h = h.sort_values("delay_min", ascending=False)
    h["cumulative_pct"] = 100 * h["delay_min"].cumsum() / h["delay_min"].sum()
    h["label"] = h["delay_code"] + " " + h["description"].str.slice(0, 32)
    order = list(h["label"])
    bars = alt.Chart(h).mark_bar(color="#4c78a8").encode(
        x=alt.X("label:N", sort=order, title=None, axis=alt.Axis(labelAngle=-30)),
        y=alt.Y("delay_min:Q", title="Delay minutes"), tooltip=["label", "delay_min"])
    line = alt.Chart(h).mark_line(point=True, color="#d64545").encode(
        x=alt.X("label:N", sort=order), y=alt.Y("cumulative_pct:Q", title="Cumulative %", scale=alt.Scale(domain=[0, 100])))
    st.altair_chart(alt.layer(bars, line).resolve_scale(y="independent"), width="stretch")
    top = h[h["cumulative_pct"] <= 80]
    st.write(f"**80/20:** {len(top) + 1} of {len(h)} causes drive ~80% of delay minutes. "
             "Late inbound (93) is the biggest, so pre-positioning for quick turns has the most leverage; "
             "ground causes 18, 33 and 37 are the next targets.")

# ---------------------------------------------------------- process map ----
with tab_process:
    st.subheader("Delay recovery process: before vs after")
    as_is = """digraph { rankdir=LR; node [shape=box, style=rounded, fontsize=11];
      a [label="Ramp agent notices\\nproblem on stand"]; b [label="Radio / phone call\\nto control room"];
      c [label="Duty manager checks\\nscreens, calls GSE,\\ncatering, load control"]; d [label="Decides from\\nexperience"];
      e [label="Calls owners to act"]; f [label="Delay code entered\\nafter departure"];
      a->b->c->d->e->f; }"""
    to_be = """digraph { rankdir=LR; node [shape=box, style=rounded, fontsize=11];
      a [label="Task overrun\\nreported / detected"]; b [label="Detector projects\\ndelay vs STD", style="rounded,filled", fillcolor="#e8f0fe"];
      c [label="Agent checks resources,\\npax impact, history", style="rounded,filled", fillcolor="#e8f0fe"];
      d [label="Drafts plan +\\nradio brief", style="rounded,filled", fillcolor="#e8f0fe"]; e [label="Duty manager\\napproves / edits", shape=diamond];
      f [label="Owners act"]; g [label="Decision + cause\\nlogged automatically", style="rounded,filled", fillcolor="#e8f0fe"];
      a->b->c->d->e->f->g; }"""
    st.markdown("**As-is (manual)**: 4-6 phone calls, decision depends on who is on shift, root cause logged late.")
    st.graphviz_chart(as_is)
    st.markdown("**To-be (AI-assisted, human-in-the-loop)**: blue steps are automated. The human still decides.")
    st.graphviz_chart(to_be)

# -------------------------------------------------------- business case ----
with tab_case:
    st.subheader("Business case (edit the assumptions)")
    c1, c2, c3 = st.columns(3)
    flights_day = c1.number_input("Departures per day at station", value=120, step=10)
    flag_rate = c1.slider("% turns flagged", 5, 40, 15) / 100
    accept_rate = c2.slider("% plans approved & executed", 10, 100, 70) / 100
    minutes_saved = c2.slider("Minutes saved per executed plan", 1, 15, 6)
    cost_per_min = c3.number_input("Cost per delay minute (€)", value=100, step=10,
                                   help="Assumption: European studies put the average cost of a tactical delay "
                                        "minute around €100. Replace with your airline's own figure.")
    reactionary = c3.slider("Reactionary multiplier", 1.0, 2.0, 1.3, 0.1,
                            help="Each primary minute saved also avoids knock-on minutes on later sectors.")

    def annual(minutes, cost):
        return flights_day * 365 * flag_rate * accept_rate * minutes * reactionary * cost

    base = annual(minutes_saved, cost_per_min)
    st.metric("Estimated annual value (base case)", f"€{base:,.0f}")
    st.write(f"= {flights_day} flights × 365 × {flag_rate:.0%} flagged × {accept_rate:.0%} executed × "
             f"{minutes_saved} min × {reactionary} reactionary × €{cost_per_min}/min")

    sens = pd.DataFrame(
        [[f"€{annual(m, c):,.0f}" for c in (cost_per_min * 0.6, cost_per_min, cost_per_min * 1.4)] for m in (3, minutes_saved, 10)],
        index=["3 min saved (low)", f"{minutes_saved} min saved (base)", "10 min saved (high)"],
        columns=[f"€{cost_per_min * 0.6:.0f}/min", f"€{cost_per_min}/min", f"€{cost_per_min * 1.4:.0f}/min"])
    st.write("**Sensitivity**")
    st.dataframe(sens, width="stretch")
    st.caption("Costs not included: API usage (cents per plan), integration with AODB/GSE systems, training.")
