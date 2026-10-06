# Graph composition: the backbone slots, the nested workflow, and the bridge
# that carries the validated request across the boundary between them.

import pytest

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.graph.base_graph import BaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.trust_level import TrustLevel
from src.graph.context_bridge import get_caller_contract, set_caller_contract
from src.graph.domain_workflow_graph import DisclosureClassificationWorkflow
from src.graph.graph import ClassifyRoutingGraphNode, Graph, runtime_config
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State, from_json, to_json


class TestOuterGraph:
    def test_it_inherits_the_framework_base_directly(self):
        assert issubclass(Graph, AgentBaseGraph)

    def test_it_compiles_with_the_five_backbone_slots(self):
        agent = Graph()
        agent.compile()
        assert set(agent._nodes) == {"initialize", "pre_process", "main", "post_process", "finalize"}

    def test_the_slots_hold_this_templates_nodes(self):
        agent = Graph()
        agent.compile()
        assert isinstance(agent._nodes["pre_process"], PreProcessNode)
        assert isinstance(agent._nodes["main"], ClassifyRoutingGraphNode)
        assert isinstance(agent._nodes["post_process"], PostProcessNode)

    def test_the_main_slot_is_a_nested_graph_node(self):
        assert issubclass(ClassifyRoutingGraphNode, GraphNode)

    def test_the_state_schema_is_this_templates_state(self):
        assert Graph().state_schema is State

    def test_the_agent_name_matches_the_manifest(self):
        assert Graph().name == "FinancialDisclosureIRRoutingAgent"

    def test_the_backbone_wiring_is_not_overridden(self):
        """Backbone topology belongs to the framework; overriding add_edges()
        here is how a template quietly loses the retry and routing behaviour."""
        assert "add_edges" not in Graph.__dict__

    def test_the_response_envelope_is_not_overridden(self):
        """The envelope returns the gated decision. A second, structured copy of
        the same record would give containment a second place to be enforced,
        and a guarantee enforced twice cannot be shown to work in either."""
        assert "get_output" not in Graph.__dict__


class TestInnerWorkflow:
    def test_it_inherits_the_framework_base_graph(self):
        assert issubclass(DisclosureClassificationWorkflow, BaseGraph)

    def test_it_compiles_with_the_four_domain_nodes(self):
        workflow = DisclosureClassificationWorkflow()
        workflow.compile()
        assert list(workflow._nodes) == ["input_validate", "classify", "apply_routing", "output_format"]

    def test_domain_node_constructors_take_no_arguments(self):
        """Per-call configuration arrives through seeded state, so a node that
        needed constructor arguments could not be registered at all."""
        import inspect

        workflow = DisclosureClassificationWorkflow()
        workflow.register_nodes()
        for node in workflow._nodes.values():
            parameters = [p for p in inspect.signature(type(node).__init__).parameters if p != "self"]
            assert parameters in ([], ["args", "kwargs"])

    def test_the_topology_is_linear_with_no_conditional_path_callable(self):
        """A conditional path callable is read as the input schema of the step
        it routes, and the fields outside that schema are projected away — so a
        callable annotated with the framework's own state type silently stops
        seeing the flag it was routing on. This workflow registers no such
        callable, and this pins that rather than trusting the comment."""
        assert "add_conditional_edges" not in DisclosureClassificationWorkflow.add_edges.__code__.co_names
        assert "add_edge" in DisclosureClassificationWorkflow.add_edges.__code__.co_names

    def test_a_non_integer_bulk_threshold_fails_at_construction(self):
        """A non-numeric threshold would make every comparison False and retire
        the bulk-review path in silence."""
        with pytest.raises(ValueError):
            DisclosureClassificationWorkflow(
                config={"configurable": {"intake": {"bulk_review_page_threshold": "many"}}}
            ).compile()


class TestTheRequestBridge:
    """The framework hands only a string to a nested graph, so the structured
    part of the request has to travel another way or it does not travel."""

    def test_what_is_stashed_is_what_the_inner_graph_is_seeded_with(self):
        contract = {"submission": "esg report", "metadata_terms": ["esg"], "confidence_floor": 0.5}
        set_caller_contract(contract)
        seeded = DisclosureClassificationWorkflow()._extra_initial_state()
        assert from_json(seeded["caller_contract"], {}) == contract

    def test_the_bridge_is_empty_when_nothing_was_stashed(self):
        set_caller_contract(None)
        assert get_caller_contract() == {}

    def test_extract_input_stashes_the_contract_and_returns_the_submission(self):
        set_caller_contract(None)
        contract = {"submission": "esg report", "metadata_terms": ["esg"]}
        state = {"validated_input": "esg report", "caller_contract": to_json(contract)}
        returned = ClassifyRoutingGraphNode().extract_input(state)
        assert returned == "esg report"
        assert get_caller_contract() == contract

    def test_the_seeded_state_also_carries_the_live_runtime_config(self):
        node = ClassifyRoutingGraphNode()
        workflow = DisclosureClassificationWorkflow(config=node._parent_config())
        seeded = workflow._extra_initial_state()
        assert from_json(seeded["intake_config"], {}) == runtime_config()["intake"]

    def test_the_forwarded_config_is_never_empty(self):
        forwarded = ClassifyRoutingGraphNode()._parent_config()
        assert forwarded["configurable"]["intake"]

    def test_the_nested_node_does_not_out_rank_the_caller(self):
        assert ClassifyRoutingGraphNode.required_trust_level == TrustLevel.ANONYMOUS

    def test_inner_errors_are_propagated_rather_than_absorbed(self):
        assert ClassifyRoutingGraphNode.error_strategy == "propagate"
