"""Ideate: a small coding agent that builds a project in its own folder (see README.md).

The notch's Ideate tab (app/Sources/WispApp/IdeateModel.swift) talks to the routes in
api.py. A run is one coding-harness child process (harness.py, Pi today), confined by
a generated sandbox profile (sandbox.py), with every shell command and every write
outside the project judged by Wisp's own policy first (gate.py).
"""
