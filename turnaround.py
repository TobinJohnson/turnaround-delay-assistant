"""
Shared domain knowledge for an A320 turnaround.

Everything the generator, detector and agent need to agree on lives here:
the standard turnaround plan, the IATA delay codes we use, and the
recovery playbook a duty manager would reach for.
"""

MIN_TURN_MIN = 45  # minimum ground time for an A320 domestic turn

# Standard turnaround plan: (task, start, end) in minutes after on-block.
# Tasks run in parallel; the turn is ready when the last task ends.
STANDARD_PLAN = [
    ("Deboarding",              2, 12),
    ("Baggage offload",         3, 18),
    ("Catering",               12, 25),
    ("Cleaning",               12, 25),
    ("Fuelling",               12, 28),
    ("Baggage loading",        20, 38),
    ("Boarding",               25, 40),
    ("Load sheet & docs",      38, 41),
    ("Doors closed & pushback", 41, 45),
]

# Which tasks must finish before another can start (the critical path).
DEPENDS_ON = {
    "Cleaning": ["Deboarding"],
    "Catering": ["Deboarding"],
    "Baggage loading": ["Baggage offload"],
    "Boarding": ["Cleaning", "Catering"],
    "Load sheet & docs": ["Fuelling", "Baggage loading"],
    "Doors closed & pushback": ["Boarding", "Load sheet & docs"],
}

# IATA standard delay codes (AHM 730) for the causes we simulate.
# task -> (code, description, resource type that fixes it)
DELAY_CODES = {
    "Deboarding":        ("34", "Servicing equipment (passenger steps / bridge) lacking or broken", "passenger_steps"),
    "Baggage offload":   ("33", "Loading equipment (belt loader) lacking or broken", "belt_loader"),
    "Catering":          ("37", "Catering late delivery or loading", "catering_truck"),
    "Cleaning":          ("35", "Aircraft cleaning late", "cleaning_crew"),
    "Fuelling":          ("36", "Fuelling / defuelling late", "fuel_bowser"),
    "Baggage loading":   ("18", "Baggage processing, sorting or late bags", "ramp_staff"),
    "Boarding":          ("15", "Boarding discrepancies, missing passengers", "gate_staff"),
    "Load sheet & docs": ("31", "Aircraft documentation late or inaccurate", "load_controller"),
}
LATE_INBOUND = ("93", "Aircraft rotation: late arrival of inbound aircraft", None)

# Recovery playbook a duty manager uses today (pre-AI, from experience).
# code -> list of (action, typical minutes saved, risk note)
PLAYBOOK = {
    "93": [("Quick-turn procedure: pre-position all GSE and crews at the stand before on-block", 6, "Needs all resources free at ETA"),
           ("Run cleaning, catering and fuelling in parallel (transit clean only)", 5, "Reduced cabin standard")],
    "33": [("Move the spare belt loader from the nearest idle stand", 8, "Check that stand's next arrival"),
           ("Start manual offload with extra ramp staff while GSE arrives", 4, "Manual handling safety")],
    "34": [("Use rear steps / open second door for deboarding", 5, "Needs second steps unit"),
           ("Call GSE maintenance for on-stand repair", 3, "Fix time uncertain")],
    "37": [("Escalate to catering supervisor for a priority truck", 6, "Other flights may slip"),
           ("Depart with minimum catering, top up at next station", 10, "Service impact, needs airline approval")],
    "35": [("Cut to transit clean scope", 6, "Lower cabin standard"),
           ("Add a second cleaning crew from the pool", 5, "Crew availability")],
    "36": [("Request a second fuel bowser", 7, "Bowser availability"),
           ("Fuel with passengers on board, per SOP with fire cover", 8, "Only if SOP conditions are met")],
    "18": [("Add 2 ramp staff and load holds in parallel", 6, "Staff pulled from other stands"),
           ("Apply bag cut-off: offload bags of no-show passengers", 5, "Security reconciliation needed")],
    "15": [("Final call + gate staff search for missing passengers", 4, "May still need offload"),
           ("Offload no-show passengers and their bags at cut-off", 6, "Bag search adds time if late")],
    "31": [("Escalate to load control for priority load sheet", 5, "Accuracy must not drop"),
           ("Pre-issue provisional load sheet, finalise with last-minute changes (LMC)", 4, "LMC limits apply")],
}

ROLE_FOR_RESOURCE = {
    "passenger_steps": "GSE controller",
    "belt_loader": "GSE controller",
    "catering_truck": "Catering supervisor",
    "cleaning_crew": "Cabin services lead",
    "fuel_bowser": "Fuel supplier dispatcher",
    "ramp_staff": "Ramp supervisor",
    "gate_staff": "Passenger services lead",
    "load_controller": "Load control",
    None: "Duty manager",
}
