"""The dependency map is a day-1 deliverable, so the parsing and scoring are pinned down."""
from __future__ import annotations

from pathlib import Path

import pytest

from mapper.compose import REFERENCE_EDGES, cross_check, parse_compose
from mapper.graph import DependencyGraph

COMPOSE = Path(__file__).resolve().parent.parent / "app" / "docker-compose.yml"

MINIMAL = """
name: demo
services:
  frontend:
    environment:
      CART_SERVICE_ADDR: cartservice:7070
      PRODUCT_CATALOG_SERVICE_ADDR: productcatalogservice:3550
      MISSING_SERVICE_ADDR: ghostservice:9999
    ports: ["8080:8080"]
    restart: unless-stopped
  cartservice:
    environment:
      REDIS_ADDR: redis-cart:6379
    depends_on: [redis-cart]
  productcatalogservice: {}
  redis-cart:
    image: redis:alpine
"""


@pytest.fixture
def minimal(tmp_path: Path) -> Path:
    path = tmp_path / "docker-compose.yml"
    path.write_text(MINIMAL, encoding="utf-8")
    return path


# ---------------------------------------------------------------- parsing
def test_addr_variables_become_call_edges(minimal):
    parsed = parse_compose(minimal)
    env_edges = {(e.src, e.dst) for e in parsed.edges if e.evidence == "env"}
    assert ("frontend", "cartservice") in env_edges
    assert ("cartservice", "redis-cart") in env_edges


def test_a_dependency_on_a_missing_service_is_warned_about_not_graphed(minimal):
    """The real compose file points frontend at a shoppingassistantservice that does not exist."""
    parsed = parse_compose(minimal)
    assert all(e.dst != "ghostservice" for e in parsed.edges)
    assert any("ghostservice" in w for w in parsed.warnings)


def test_the_redis_edge_is_labelled_tcp_not_grpc(minimal):
    parsed = parse_compose(minimal)
    edge = next(e for e in parsed.edges if e.dst == "redis-cart")
    assert edge.type == "tcp"


def test_env_evidence_wins_over_a_depends_on_hint(minimal):
    """cartservice declares redis-cart both ways; the real call edge must be kept."""
    parsed = parse_compose(minimal)
    edges = [e for e in parsed.edges if (e.src, e.dst) == ("cartservice", "redis-cart")]
    assert len(edges) == 1 and edges[0].evidence == "env"


def test_ports_are_parsed_from_the_addr_value(minimal):
    parsed = parse_compose(minimal)
    assert next(e for e in parsed.edges if e.dst == "cartservice").port == 7070


def test_a_missing_compose_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        parse_compose(tmp_path / "nope.yml")


def test_a_compose_file_with_no_services_raises(tmp_path):
    path = tmp_path / "docker-compose.yml"
    path.write_text("name: empty\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no `services:` section"):
        parse_compose(path)


def test_an_environment_list_is_parsed_like_a_mapping(tmp_path):
    path = tmp_path / "docker-compose.yml"
    path.write_text(
        "services:\n  a:\n    environment:\n      - B_SERVICE_ADDR=b:50051\n  b: {}\n",
        encoding="utf-8",
    )
    parsed = parse_compose(path)
    assert ("a", "b") in {(e.src, e.dst) for e in parsed.edges}


# ---------------------------------------------------------------- scoring
def test_the_data_store_outranks_a_leaf_service(minimal):
    """redis-cart is stateful and has dependents; productcatalogservice has fewer."""
    graph = DependencyGraph(parse_compose(minimal))
    assert graph.services["redis-cart"].score > graph.services["productcatalogservice"].score


def test_infrastructure_is_excluded_from_the_ranking(tmp_path):
    path = tmp_path / "docker-compose.yml"
    path.write_text(
        "services:\n  frontend: {}\n  loadgenerator:\n    environment:\n"
        "      FRONTEND_ADDR: frontend:8080\n",
        encoding="utf-8",
    )
    graph = DependencyGraph(parse_compose(path))
    assert "loadgenerator" not in [i.name for i in graph.ranked()]
    assert graph.services["loadgenerator"].infrastructure is True


def test_transitive_dependents_are_counted(minimal):
    graph = DependencyGraph(parse_compose(minimal))
    # frontend -> cartservice -> redis-cart, so frontend depends on redis-cart transitively
    assert "frontend" in graph.services["redis-cart"].dependents


def test_every_service_gets_a_rationale(minimal):
    graph = DependencyGraph(parse_compose(minimal))
    assert all(info.rationale for info in graph.ranked())


def test_the_user_entry_point_counts_as_a_single_point_of_failure(minimal):
    graph = DependencyGraph(parse_compose(minimal))
    assert graph.services["frontend"].single_point is True


# ------------------------------------------------- the project's real compose
@pytest.mark.skipif(not COMPOSE.exists(), reason="app/docker-compose.yml not present")
def test_the_real_compose_file_matches_the_documented_architecture():
    """Proves the generated map is right, not just self-consistent."""
    parsed = parse_compose(COMPOSE)
    notes = cross_check(parsed)
    assert any("match the documented architecture" in note for note in notes), notes


@pytest.mark.skipif(not COMPOSE.exists(), reason="app/docker-compose.yml not present")
def test_the_real_graph_json_has_the_documented_shape():
    payload = DependencyGraph(parse_compose(COMPOSE)).to_json()
    assert {"services", "edges", "analysis", "ranked_targets"} <= set(payload)
    first_edge = payload["edges"][0]
    assert {"from", "to", "type"} <= set(first_edge)  # same keys as docs/graph.example.json
    assert payload["ranked_targets"][0] == "redis-cart"


@pytest.mark.skipif(not COMPOSE.exists(), reason="app/docker-compose.yml not present")
def test_the_reference_edges_are_all_present_in_the_real_file():
    parsed = parse_compose(COMPOSE)
    found = {(e.src, e.dst) for e in parsed.edges if e.evidence == "env"}
    assert found >= REFERENCE_EDGES
