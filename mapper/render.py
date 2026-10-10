"""Render the dependency graph to a PNG for the architecture deliverable.

matplotlib is used with the non-interactive Agg backend so this works in a plain terminal with no
display. Nodes are laid out in tiers (user -> frontend -> services -> data) rather than by a spring
layout, because a readable picture matters more than a mathematically pleasing one on a slide.
Node colour shows criticality, so the chosen experiment targets are visually obvious.
"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402  (must follow the backend selection)

from .graph import INFRASTRUCTURE, DependencyGraph  # noqa: E402

#: Colour-blind-safe sequential ramp (low -> high criticality).
_RAMP = ["#dbeafe", "#93c5fd", "#60a5fa", "#2563eb", "#1e3a8a"]
_EDGE_STYLE = {"env": "-", "depends_on": ":"}


def _tier(graph: DependencyGraph, name: str) -> int:
    info = graph.services[name]
    if info.infrastructure:
        return 0
    if info.user_facing:
        return 1
    if info.stateful:
        return 4
    if not info.direct_dependencies:
        return 3
    return 2


def _colour(score: float) -> str:
    return _RAMP[min(len(_RAMP) - 1, int(score / (100 / len(_RAMP))))]


def render_png(
    graph: DependencyGraph,
    out_path: Path,
    title: str = "Online Boutique - service dependencies",
) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    tiers: dict[int, list[str]] = {}
    for name in sorted(graph.graph.nodes):
        tiers.setdefault(_tier(graph, name), []).append(name)

    positions: dict[str, tuple[float, float]] = {}
    for tier, names in tiers.items():
        for index, name in enumerate(names):
            offset = (len(names) - 1) / 2
            positions[name] = (index - offset, -float(tier))

    fig, ax = plt.subplots(figsize=(13, 8))
    for src, dst, data in graph.graph.edges(data=True):
        x1, y1 = positions[src]
        x2, y2 = positions[dst]
        ax.annotate(
            "", xy=(x2, y2), xytext=(x1, y1),
            arrowprops={
                "arrowstyle": "-|>", "color": "#64748b", "alpha": 0.55, "linewidth": 1.1,
                "linestyle": _EDGE_STYLE.get(data.get("evidence", "env"), "-"),
                "shrinkA": 26, "shrinkB": 26, "connectionstyle": "arc3,rad=0.06",
            },
        )
    for name, (x, y) in positions.items():
        info = graph.services[name]
        is_infra = name in INFRASTRUCTURE
        ax.scatter([x], [y], s=2100, zorder=3,
                   c="#f1f5f9" if is_infra else _colour(info.score),
                   edgecolors="#94a3b8" if is_infra else "#1e293b",
                   linewidths=1.2, marker="s")
        label = name if is_infra else f"{name}\n{info.score:.0f}"
        ax.text(x, y, label, ha="center", va="center", zorder=4, fontsize=7.5,
                color="#334155" if (is_infra or info.score < 55) else "white",
                fontweight="normal" if is_infra else "bold")

    ax.set_title(title, fontsize=13, fontweight="bold", color="#0f172a", pad=16)
    ax.text(0.5, -0.06,
            "number = criticality score   solid = call edge (*_ADDR)   dotted = depends_on only   "
            "grey = infrastructure, never a target",
            transform=ax.transAxes, ha="center", fontsize=8, color="#64748b")
    ax.set_axis_off()
    ax.margins(0.12)
    fig.tight_layout()
    fig.savefig(out_path, dpi=170, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out_path


__all__ = ["render_png"]
