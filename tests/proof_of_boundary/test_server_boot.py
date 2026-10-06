# Server boot boundary: importing the standalone entry point must not raise.
#
# src/api/server.py constructs the agent, calls compile() and provisions
# secrets AT MODULE LEVEL — so an import failure is a deploy-time outage that
# node-level unit tests would never catch. The boundary proven here:
#
#   1. `import src.api.server` completes (the ASGI app and the compiled agent
#      are built; the secrets provider tolerates absent environment files).
#   2. The module-level agent is this template's agent class, compiled, and
#      constructed with the LIVE runtime config rather than an empty one.
#   3. A fresh agent constructs and compiles via the supported path.
#
# Deterministic — no model call, no network, no socket bind (the ASGI app is
# built but never served).

import importlib


class TestServerBoot:
    def test_server_module_imports_without_raising(self):
        server = importlib.import_module("src.api.server")
        assert server.app is not None, "the ASGI app must be constructed at import"

    def test_module_level_agent_is_compiled(self):
        server = importlib.import_module("src.api.server")
        from src.graph.graph import Graph

        assert isinstance(server.agent, Graph)
        assert server.agent._compiled is not None, "server.py must compile() the agent at import time"

    def test_the_module_level_agent_carries_the_live_runtime_config(self):
        """The registry passes config/config.yaml to the constructor; a
        standalone server that did not would run the same code with different
        parameters, and only one of the two deployments would be tested."""
        server = importlib.import_module("src.api.server")
        from src.graph.graph import runtime_config

        assert server.agent.config == runtime_config()

    def test_health_endpoint_reports_this_agent(self):
        server = importlib.import_module("src.api.server")
        assert server.health() == {"status": "ok", "agent": "FinancialDisclosureIRRoutingAgent"}

    def test_fresh_agent_constructs_and_compiles(self):
        from src.graph.graph import Graph

        agent = Graph()
        agent.compile()
        assert agent._compiled is not None
        assert set(agent._nodes) == {"initialize", "pre_process", "main", "post_process", "finalize"}
