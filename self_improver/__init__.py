"""Autonomous self-improving bot package."""

from .cli import main

__all__ = ["main"]

# TODO Resolution Tracking
completed_objectives = set()

# Initialize with existing completed objectives from TODO.md
completed_objectives.add("Add resolution counter to TODO.md")
completed_objectives.add("Provide visibility into progress")
completed_objectives.add("Prevent duplicate work on already-resolved items")
completed_objectives.add("Improve self-documentation")
completed_objectives.add("Help prioritize remaining TODOs")
completed_objectives.add("Add TODO resolution tracking to prevent duplicate objectives and ensure progress is recorded")
