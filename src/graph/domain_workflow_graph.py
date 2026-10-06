"""AgentCore Platform v1.0"""

# Inner domain graph for the two-layer nested disclosure routing agent.
# Instantiated by ClassifyRoutingGraphNode.get_subgraph() in graph.py.
#
# Pipeline (linear):
#   START -> input_validate -> classify -> apply_routing -> output_format -> END
#
# Node -> file mapping:
#   input_validate  src/nodes/input_validate_node.py
#   classify        src/nodes/classify_document_type_node.py
#   apply_routing   src/nodes/apply_routing_rule_node.py
#   output_format   src/nodes/output_format_node.py

from typing import Any, Dict

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.graph.context_bridge import get_caller_contract
from src.nodes.apply_routing_rule_node import ApplyRoutingRuleNode
from src.nodes.classify_document_type_node import ClassifyDocumentTypeNode
from src.nodes.input_validate_node import InputValidateNode
from src.nodes.output_format_node import OutputFormatNode
from src.schemas.state import State, to_json

# Everything get_output() can hand back that carries the assembled decision, or
# a piece of it. The outer merge_output() maps exactly these into outer state,
# so this tuple and that mapping are the same contract read from two sides.
_ANSWER_FIELDS = (
    "output",
    "disclosure_type",
    "classification_confidence",
    "regulatory_reference",
    "routing_target",
    "routing_overridden",
    "review_mode",
    "validated_document_ref",
    "routing_decision",
)


class DisclosureClassificationWorkflow(BaseGraph):
    """The four-step classification and routing workflow.

    Inherits BaseGraph for a fully custom linear topology.
    """

    # -- Identity --------------------------------------------------------------

    @property
    def name(self) -> str:
        return "disclosure_classification_workflow"

    @property
    def state_schema(self) -> type:
        return State

    # -- Config validation -----------------------------------------------------

    def _validate_config(self) -> None:
        """Check the forwarded intake tuning before the graph is built.

        The threshold is a number the routing step compares against, so a
        non-numeric or non-finite value here would make every comparison False
        and quietly retire the bulk-review path. Failing at construction is the
        only way that becomes visible.
        """
        intake = (self.config or {}).get("configurable", {}).get("intake") or {}
        threshold = intake.get("bulk_review_page_threshold")
        if threshold is not None and (isinstance(threshold, bool) or not isinstance(threshold, int)):
            raise ValueError(
                f"[{self.__class__.__name__}] 'intake.bulk_review_page_threshold' must be an integer, "
                f"got: {type(threshold).__name__}"
            )

    # -- Initial state ---------------------------------------------------------

    def _extra_initial_state(self) -> Dict[str, Any]:
        """Seed the inner state with everything that cannot travel as a string.

        The framework passes only the submission string into a nested graph, so
        two things are seeded here instead:

        `caller_contract` — the validated contract stashed on the bridge by
        ClassifyRoutingGraphNode.extract_input() one step earlier. It carries
        the caller's document reference, filing details, confidence floor and
        metadata keywords, all already bounds-checked at the request boundary.

        `intake_config` — the live tuning block forwarded by
        ClassifyRoutingGraphNode._parent_config(). Domain nodes take no config
        parameter, so state seeding is the only route runtime config can reach
        ApplyRoutingRuleNode.

        Both are stored as JSON strings rather than mappings, matching the
        serialization rule the shared state schema documents.
        """
        intake = (self.config or {}).get("configurable", {}).get("intake") or {}
        return {
            "caller_contract": to_json(get_caller_contract()),
            "intake_config": to_json(intake),
        }

    # -- Node registration -----------------------------------------------------

    def register_nodes(self) -> None:
        """Register the four domain nodes.

        No super() call — BaseGraph.register_nodes() is abstract.
        Node constructors take no arguments; per-call configuration arrives
        through seeded state.
        """
        self._nodes["input_validate"] = InputValidateNode()
        self._nodes["classify"] = ClassifyDocumentTypeNode()
        self._nodes["apply_routing"] = ApplyRoutingRuleNode()
        self._nodes["output_format"] = OutputFormatNode()

    # -- Edge wiring -----------------------------------------------------------

    def add_edges(self) -> None:
        """Wire the linear domain topology.

        Deliberately linear: there is no conditional branching between domain
        nodes, so add_conditional_edges() is not used and no path callable
        projects the state away. route() below is implemented because the base
        class requires it.
        """
        self._sg.add_edge(START, "input_validate")
        self._sg.add_edge("input_validate", "classify")
        self._sg.add_edge("classify", "apply_routing")
        self._sg.add_edge("apply_routing", "output_format")
        self._sg.add_edge("output_format", END)

    # -- Routing ---------------------------------------------------------------

    def route(self, state: AgentState) -> str:
        """Required by the base class; unused in this linear topology."""
        return END if state.get("status") == AgentStatus.ERROR.value else "output_format"

    # -- Output shape ----------------------------------------------------------

    def get_output(self, state: AgentState) -> Dict[str, Any]:
        """Shape the sub_result handed back to ClassifyRoutingGraphNode.merge_output().

        The keys here and the keys merge_output() reads are one contract; they
        are changed together or not at all.

        The answer is conditioned on this graph's own status. A run that did not
        finish successfully has no decision to hand out — only a draft the last
        completed step left behind — and handing that draft back is what puts it
        on the caller's road: the outer graph writes `output` into
        state["result"], any status other than SUCCESS routes the backbone
        straight to finalize (the output gate in post_process is skipped
        entirely), and the framework's envelope then resolves
        `formatted_output or result`. So the un-gated draft reaches the caller
        through `result` with no gate anywhere on that path.

        The check lives here rather than in the outer merge_output() because
        this method is the inner graph's own output boundary: every caller of
        this graph gets the same guarantee, not only the one GraphNode that
        happens to wrap it today. merge_output() copies what this returns and
        never re-reads inner state, so it cannot put back what is withheld here
        — and a second copy of this status test there would leave neither copy
        falsifiable.
        """
        status = state.get("status")
        envelope: Dict[str, Any] = {
            "status": status,
            "trace_id": state.get("trace_id"),
            "correlation_id": state.get("correlation_id"),
            "node_history": state.get("node_history", []),
        }

        if status != AgentStatus.SUCCESS.value:
            # Withheld, not trimmed: every answer-bearing key is returned as
            # None so the outer delta CLEARS the corresponding outer fields
            # rather than leaving a value from an earlier pass of the backbone's
            # retry loop standing.
            return {**envelope, **{field: None for field in _ANSWER_FIELDS}}

        return {
            **envelope,
            "output": state.get("result"),
            "disclosure_type": state.get("disclosure_type"),
            "classification_confidence": state.get("classification_confidence"),
            "regulatory_reference": state.get("regulatory_reference"),
            "routing_target": state.get("routing_target"),
            "routing_overridden": state.get("routing_overridden"),
            "review_mode": state.get("review_mode"),
            "validated_document_ref": state.get("validated_document_ref"),
            "routing_decision": state.get("routing_decision"),
        }
