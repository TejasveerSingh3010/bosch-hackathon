"""Which per-service latency graphs are open on the dashboard, and the data they plot.

Kept free of Streamlit so the rules can be tested:

  * clicking a row in the Service health table opens that service's graph (it stays until closed);
  * a service that is *attacked* - targeted by the running experiment, or down for any reason -
    opens its graph automatically;
  * an automatically opened graph closes AUTO_CLOSE_AFTER_S seconds after the service recovers;
  * closing an auto-opened graph while the attack is still going keeps it closed until the
    service recovers, so it doesn't pop straight back open.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

AUTO_CLOSE_AFTER_S = 10.0


def new_state() -> dict[str, Any]:
    return {"open": {}, "dismissed": set(), "last_selection": set()}


def update(state: dict[str, Any], attacked: set[str], selection: set[str], now: float,
           auto_close_s: float = AUTO_CLOSE_AFTER_S) -> dict[str, Any]:
    """Apply one dashboard refresh. `selection` is the set of rows currently selected in the table.

    Only rows that became selected since the last refresh open a graph, so a row that stays
    selected does not reopen a graph the user has closed."""
    opened = state["open"]
    for svc in selection - state["last_selection"]:
        opened[svc] = {"source": "manual", "last_bad": opened.get(svc, {}).get("last_bad")}
        state["dismissed"].discard(svc)
    state["last_selection"] = set(selection)

    state["dismissed"] &= attacked  # a dismissal lasts only until the service recovers
    for svc in attacked:
        if svc in opened:
            opened[svc]["last_bad"] = now
        elif svc not in state["dismissed"]:
            opened[svc] = {"source": "auto", "last_bad": now}

    for svc, info in list(opened.items()):
        recovered_for = now - (info["last_bad"] or now)
        if info["source"] == "auto" and svc not in attacked and recovered_for >= auto_close_s:
            del opened[svc]
    return state


def close(state: dict[str, Any], svc: str, attacked: set[str]) -> None:
    """The user closed a graph. If the service is still under attack, don't auto-reopen it."""
    state["open"].pop(svc, None)
    if svc in attacked:
        state["dismissed"].add(svc)


def ordered(state: dict[str, Any], attacked: set[str], order: list[str]) -> list[str]:
    """Open graphs, attacked services first, then the table's order."""
    rank = {name: i for i, name in enumerate(order)}
    return sorted(state["open"], key=lambda s: (s not in attacked, rank.get(s, len(rank)), s))


def attacked_services(sample: dict[str, Any], experiment: dict[str, Any] | None,
                      known: list[str] | tuple[str, ...] = ()) -> set[str]:
    """Services that are down (container or health check), plus the running experiment's target.

    The experiment journal names a container (online-boutique-redis-cart-1); it is matched to a
    service through the sample, or by name when there is no container data."""
    attacked = set(sample.get("summary", {}).get("services_down") or [])
    container = (experiment or {}).get("container")
    if container:
        target = next((r["service"] for r in sample.get("services") or []
                       if r.get("container") == container), None)
        if target is None:
            stem = re.sub(r"-\d+$", "", container)
            matches = [k for k in known if stem == k or stem.endswith("-" + k)]
            target = max(matches, key=len) if matches else None
        if target:
            attacked.add(target)
    return attacked


def latency_rows(history, svc: str) -> list[dict[str, Any]]:
    """One point per sample: health-check latency and whether the service was down then.

    A down service keeps its point (a refused connection answers in ~1 ms, a hung one at the
    timeout), so the chart shows *when* it was down, not just a gap."""
    rows = []
    for sample in history:
        row = next((r for r in sample.get("services") or [] if r.get("service") == svc), None)
        if row is None:
            continue
        try:
            when = datetime.fromisoformat(sample["timestamp"])
        except (KeyError, ValueError):
            continue
        rows.append({
            "time": when,
            "latency_ms": row.get("health_latency_ms"),
            "status": "Down" if row.get("down") else "Up",
            "detail": row.get("reason") or row.get("health_check") or "",
        })
    return rows
