"""Agent 1: the Attack Ranker.

Holds the whole attack catalogue and ranks every entry from most to least harmful for the app's
current state. It proposes; it never executes - a human approves one ranked entry (`python -m
planner approve <rank>`) and only that entry reaches `ExperimentRunner.run`, which re-checks
pre-flight against fresh telemetry.
"""
from .catalogue import AttackSpec, build_catalogue, service_from_container
from .features import AttackFeatures, compute_features
from .ranker import Ranker, write_ranking

__all__ = [
    "AttackFeatures",
    "AttackSpec",
    "Ranker",
    "build_catalogue",
    "compute_features",
    "service_from_container",
    "write_ranking",
]
