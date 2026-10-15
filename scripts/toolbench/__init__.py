"""Toolbench: a hermetic, repeatable tool-calling scorecard for Wisp models.

Every case runs through the production router and agent loop, but every
registered tool is replaced by a fake backed by one synthetic world
(`world.py`), so nothing real is read, written or sent and two runs see the
same data. See `scripts/run_toolbench.py --help` for the command line.

    world.py     synthetic user data + the fake tool implementations
    cases.py     the case schema and the YAML loader (cases/*.yaml)
    matchers.py  argument matchers used by expectations
    scorer.py    pure scoring: one trajectory in, one verdict + labels out
    runner.py    isolation guards, the bench client, route + run_agent
    report.py    scorecard, per-category table, run-vs-run comparison
"""
