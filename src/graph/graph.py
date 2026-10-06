"""AgentCore Platform v1.0"""

# FinancialDisclosureIRRoutingAgent — outer graph (two-layer nested pattern).
#
# Architecture:
#
#   Outer backbone (fixed — do NOT override add_edges()):
#     START -> initialize -> pre_process -> main -> {route} -> post_process
#              -> finalize -> END
#                              |  (RETRY, bounded by max_retry)
#                              -> pre_process
#
#   The `main` slot is a GraphNode subclass (ClassifyRoutingGraphNode) that
#   delegates the whole domain workflow to DisclosureClassificationWorkflow
#   (inner graph: input_validate -> classify -> apply_routing -> output_format).
#
# Directory layout:
#   src/graph/graph.py                 <- outer graph (this file)
#   src/graph/domain_workflow_graph.py <- inner classification workflow
#   src/graph/context_bridge.py        <- validated caller contract across the boundary
#
# Class-name contract:
#   graph.py class:           Graph (this file)
#   config/agent.yaml class:  "src.graph.graph.Graph"
#   src/api/server.py import: from src.graph.graph import Graph
#
# Rules enforced here:
#   - Graph inherits AgentBaseGraph (direct framework inheritance)
#   - super().register_nodes() called first (fills initialize + finalize)
#   - ClassifyRoutingGraphNode assigned to self._nodes["main"]
#   - _parent_config() forwards the LIVE runtime config, never {}
#   - merge_output() returns only changed keys
#   - add_edges() NOT overridden

from pathlib import Path
from typing import Any, ClassVar, Dict

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.trust_level import TrustLevel
from framework.utils.config_loader import load_agent_config
from src.graph.context_bridge import set_caller_contract
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State, from_json

# Repo root: src/graph/graph.py -> parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]

# Mirrors config/config.yaml so the intake bounds are never empty even where
# the config file is unreadable in an exotic deployment layout.
_FALLBACK_INTAKE: Dict[str, Any] = {
    "bulk_review_page_threshold": 500,
}


def runtime_config() -> Dict[str, Any]:
    """Load config/config.yaml — the live runtime parameters.

    The agent registry loads this file and passes it to the graph constructor;
    the standalone HTTP entry point does the same, so max_retry and the intake
    tuning are live in both deployments rather than declared and ignored.

    Reading the static manifest (config/agent.yaml) here instead would return
    nothing: the manifest carries identity and compile-time requirements only,
    and a reader pointed at it degrades silently to defaults.
    """
    loaded = load_agent_config(_REPO_ROOT)
    return dict(loaded) if isinstance(loaded, dict) else {}


class ClassifyRoutingGraphNode(GraphNode):
    """The `main` slot: wraps the inner classification and routing workflow.

    Contracts:
      get_subgraph()   - instantiate DisclosureClassificationWorkflow with the
                         forwarded runtime config (_parent_config())
      extract_input()  - hand the validated submission to the inner graph and
                         stash the validated caller contract on the bridge
      merge_output()   - map sub_result fields into the outer state delta
      error_strategy   - "propagate": re-raise inner errors (fail fast)

    The trust gate on a GraphNode is a deliberate no-op: the backbone's
    pre_process node already enforced the external caller gate, and every inner
    node declares ANONYMOUS so a VERIFIED_EXTERNAL caller's context passes
    through unchanged.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS
    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = False

    def _parent_config(self) -> Dict[str, Any]:
        """Forward the live intake tuning to the inner graph.

        Returns the tuning block under config["configurable"] — never an empty
        dict. The inner graph republishes it into inner state
        (DisclosureClassificationWorkflow._extra_initial_state()) because node
        execute() methods take no config parameter, so state seeding is the only
        route runtime config can travel.
        """
        intake = runtime_config().get("intake")
        if not isinstance(intake, dict) or not intake:
            intake = dict(_FALLBACK_INTAKE)
        return {"configurable": {"intake": intake}}

    def get_subgraph(self) -> Any:
        """Instantiate and return the inner classification workflow.

        Imported inside the method to avoid circular-import risk at module load
        time. The inner graph receives the runtime-derived config through its
        constructor; its domain nodes still take no constructor arguments and
        read what they need from seeded state.
        """
        from src.graph.domain_workflow_graph import DisclosureClassificationWorkflow

        return DisclosureClassificationWorkflow(config=self._parent_config())

    def extract_input(self, state: AgentState) -> str:
        """Return the submission string, and bridge the validated contract.

        The framework hands only a string to the inner graph, so the structured
        part of the request travels on the bridge instead — set here, one step
        before the inner invoke, and read by the inner graph's initial-state
        hook. Only the contract the pre_process node already validated crosses.
        """
        set_caller_contract(from_json(state.get("caller_contract"), {}) or {})
        return str(state.get("validated_input") or state.get("user_input", ""))

    def merge_output(self, state: AgentState, sub_result: Dict[str, Any]) -> Dict[str, Any]:
        """Map the inner result into the outer state delta (changed keys only).

        `result` is set as well as `routing_decision`: the post_process slot and
        the framework's response envelope both read state["result"], so without
        that mapping the gated output would always be empty.

        Every value is copied from `sub_result` and nowhere else. The inner
        graph withholds the assembled decision on any non-success run
        (DisclosureClassificationWorkflow.get_output()), and a withheld field
        arrives here as None; reading `state` for a fallback, or substituting a
        default, would hand the caller back exactly what the inner graph
        declined to release — on a non-success status the backbone skips
        post_process, so nothing downstream would gate it.
        """
        return {
            "disclosure_type": sub_result.get("disclosure_type"),
            "classification_confidence": sub_result.get("classification_confidence"),
            "regulatory_reference": sub_result.get("regulatory_reference"),
            "routing_target": sub_result.get("routing_target"),
            "routing_overridden": sub_result.get("routing_overridden"),
            "review_mode": sub_result.get("review_mode"),
            "validated_document_ref": sub_result.get("validated_document_ref"),
            "routing_decision": sub_result.get("routing_decision"),
            "result": sub_result.get("output"),
            "status": sub_result.get("status"),
        }


class Graph(AgentBaseGraph):
    """Financial disclosure and investor-relations document routing agent.

    Backbone (fixed):
        START -> initialize -> pre_process -> main -> post_process -> finalize -> END

    register_nodes() is the ONLY topology override:
      - super().register_nodes() fills: initialize, finalize (framework defaults)
      - pre_process:  PreProcessNode (trust gate + caller-contract validation)
      - main:         ClassifyRoutingGraphNode (delegates to the inner workflow)
      - post_process: PostProcessNode (output gate)
    """

    @property
    def name(self) -> str:
        """Agent identifier registered with the agent registry."""
        return "FinancialDisclosureIRRoutingAgent"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        """Fill all 5 backbone slots.

        super().register_nodes() MUST be called first — it injects the
        framework's default initialize node (schema version, session id, trust
        level) and finalize node (response metadata, elapsed time).
        """
        super().register_nodes()  # fills: initialize, finalize
        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = ClassifyRoutingGraphNode()
        self._nodes["post_process"] = PostProcessNode()

    # add_edges() is NOT overridden — backbone wiring belongs to the framework.
    # get_output() is NOT overridden either: the framework's envelope returns
    # state["formatted_output"] or state["result"], which is the serialised
    # routing decision the output gate cleared. Adding a second, structured copy
    # of the same record to the envelope would give the containment guarantee a
    # second place to be enforced, and a guarantee enforced twice is a guarantee
    # that cannot be shown to work in either place.
