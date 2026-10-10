"""Builds the service dependency map from the compose file.

The map is derived automatically from `*_ADDR` environment variables and cross-checked against the
upstream architecture diagram (`compose.REFERENCE_EDGES`), so it is both generated and verified.
`graph.DependencyGraph` also scores each service's criticality, which is what the day-2 planner
ranks experiments by.
"""
from .compose import Edge, ParsedCompose, cross_check, parse_compose
from .graph import DependencyGraph, ServiceInfo

__all__ = ["DependencyGraph", "Edge", "ParsedCompose", "ServiceInfo", "cross_check", "parse_compose"]
