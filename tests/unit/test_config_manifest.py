# The manifest and the runtime configuration: two files with two jobs.
#
# config/agent.yaml is the static manifest the registry discovers the agent
# with — identity and compile-time requirements, read at ROOT level.
# config/config.yaml carries the runtime parameters and is passed to the graph
# constructor. A reader pointed at the wrong one gets an empty mapping and
# degrades to defaults without failing, which is why both are pinned here.

from pathlib import Path

import yaml

from src.graph.graph import runtime_config

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load(relative):
    return yaml.safe_load((_REPO_ROOT / relative).read_text(encoding="utf-8"))


class TestManifest:
    def test_every_key_is_at_the_root(self):
        manifest = _load("config/agent.yaml")
        assert "agent" not in manifest
        for key in ("id", "name", "namespace", "version", "category", "industry", "class"):
            assert key in manifest

    def test_the_entry_point_names_the_shipped_class(self):
        assert _load("config/agent.yaml")["class"] == "src.graph.graph.Graph"

    def test_the_declared_name_matches_the_graph(self):
        from src.graph.graph import Graph

        assert _load("config/agent.yaml")["name"] == Graph().name

    def test_compile_time_requirements_are_declared_and_empty(self):
        """This agent constructs no client and requires no secret. Declaring one
        that is not provisioned makes the agent fail at compile time on a real
        deployment, so the empty declaration is the correct one."""
        requires = _load("config/agent.yaml")["requires"]
        assert requires["secrets"] == []
        assert requires["extras"] == []

    def test_the_generation_mode_matches_the_implementation(self):
        assert _load("config/agent.yaml")["generation_mode"] == "deterministic"


class TestRuntimeConfig:
    def test_the_loader_reads_the_runtime_file_not_the_manifest(self):
        loaded = runtime_config()
        assert loaded == _load("config/config.yaml")
        assert "id" not in loaded

    def test_the_declared_parameters_are_present(self):
        loaded = runtime_config()
        assert loaded["max_retry"] == 3
        assert loaded["timeout_s"] == 30
        assert isinstance(loaded["intake"]["bulk_review_page_threshold"], int)

    def test_the_declared_retry_budget_reaches_the_compiled_agent(self):
        from src.graph.graph import Graph

        agent = Graph(config=runtime_config())
        agent.compile()
        assert agent.config["max_retry"] == runtime_config()["max_retry"]
