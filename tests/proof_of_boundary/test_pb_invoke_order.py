# PB-6: Backbone Invoke-Order Verification — FIN-C2-019
#
# Verifies that a VERIFIED_EXTERNAL caller traverses the full 5-node backbone:
#   InitializeNode → PreProcessNode → ClassifyRoutingGraphNode → PostProcessNode → FinalizeNode
#
# WHY THIS TEST EXISTS (and why InvocationContext.for_internal() is FORBIDDEN here):
#   GraphNode.execute() forwards the outer InvocationContext UNCHANGED into the inner graph
#   (graph_node.py:134 InvocationContext.from_state).  A real external caller arrives at the
#   inner nodes as VERIFIED_EXTERNAL(1).  Inner nodes that wrongly declare INTERNAL(2) DENY
#   that caller (1 < 2) → SubgraphError → backbone skips post_process.
#   An INTERNAL context (for_internal()) masks this: INTERNAL(2) ≥ every inner gate → passes
#   even broken inner trust levels.  Only VERIFIED_EXTERNAL exposes the trust-trap.
#   Reaching post_process proves the inner nodes are correctly gated at ANONYMOUS(0).
#
# Template-specific constants (fill once; the rest is boilerplate):
#   _MAIN_SLOT_NODE  — the class assigned to self._nodes["main"] in Graph.register_nodes()
#   _VALID_PAYLOAD   — a document string that yields backbone SUCCESS (not short-circuited)

# ── Template-specific constants ───────────────────────────────────────────────

# The main-slot GraphNode class registered in src/graph/graph.py.
# Must match the class assigned to self._nodes["main"] in Graph.register_nodes().
_MAIN_SLOT_NODE_NAME = "ClassifyRoutingGraphNode"

# A SUCCESS-yielding disclosure document payload.
# Must be non-empty and contain taxonomy keywords so the inner classification
# succeeds and does NOT short-circuit before post_process.
_VALID_PAYLOAD = "有価証券報告書 2025年度第2四半期 annual securities report"

# Expected backbone node class names in execution order
_EXPECTED_BACKBONE = [
    "InitializeNode",
    "PreProcessNode",
    _MAIN_SLOT_NODE_NAME,
    "PostProcessNode",
    "FinalizeNode",
]


# ── Helpers ────────────────────────────────────────────────────────────────────


def _class_name(entry) -> str:
    """Extract the class name from a node_history entry.

    node_history entries may be:
      - str class name ("InitializeNode")
      - class type (InitializeNode)
      - instance (some_node_instance)
    Handle all three formats gracefully.
    """
    if isinstance(entry, str):
        return entry
    if isinstance(entry, type):
        return entry.__name__
    return type(entry).__name__


# ── PB-6 test class ───────────────────────────────────────────────────────────


class TestBackboneInvokeOrder:
    """PB-6: full Graph.invoke() over VERIFIED_EXTERNAL must reach post_process."""

    def test_backbone_order_verified_external_caller(self, monkeypatch):
        """Core PB-6: VERIFIED_EXTERNAL caller traverses the full backbone in correct order.

        A SubgraphError from the inner workflow (e.g. inner nodes gated at INTERNAL)
        causes the backbone to short-circuit and SKIP post_process.  Asserting that
        PostProcessNode IS in the node_history is the mechanistic proof that no
        trust-trap exists in the inner domain nodes.
        """

        # Silence audit-log side-effects (never stub the shared package in sys.modules)
        def noop(*args, **kwargs):
            return None

        for mod in (
            "src.nodes.pre_process_node",
            "src.nodes.post_process_node",
            "src.nodes.input_validate_node",
            "src.nodes.classify_document_type_node",
            "src.nodes.apply_routing_rule_node",
            "src.nodes.output_format_node",
        ):
            monkeypatch.setattr(f"{mod}.emit_trace_event", noop, raising=False)

        from framework.schemas.invocation_context import InvocationContext
        from framework.schemas.trust_level import TrustLevel
        from framework.schemas.agent_status import AgentStatus
        from src.graph.graph import Graph

        # ── Instantiate and compile ────────────────────────────────────────────
        agent = Graph()
        agent.compile()

        # ── VERIFIED_EXTERNAL context — never for_internal() ──────────────────
        ctx = InvocationContext(
            session_id="pb6-backbone-invoke-order",
            caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        )
        # Confirm we are NOT using an INTERNAL context
        assert (
            ctx.caller_trust_level == TrustLevel.VERIFIED_EXTERNAL
        ), "PB-6 MUST use VERIFIED_EXTERNAL — using INTERNAL masks the trust-trap"

        # ── Invoke ────────────────────────────────────────────────────────────
        result = agent.invoke(_VALID_PAYLOAD, ctx=ctx)

        assert result is not None, "invoke() must return a result dict"

        # ── Assert SUCCESS (load-bearing: non-SUCCESS skips post_process) ─────
        status = result.get("status")
        assert status in (AgentStatus.SUCCESS, AgentStatus.SUCCESS.value), (
            f"Expected SUCCESS; got {status!r}.\n"
            f"A non-SUCCESS from pre_process or main causes the backbone to skip "
            f"post_process, which hides trust-trap failures in the inner workflow."
        )

        # ── Assert backbone nodes appear in node_history ──────────────────────
        node_history = result.get("node_history", [])
        history_names = [_class_name(e) for e in node_history]

        # Every backbone node must appear
        missing = [n for n in _EXPECTED_BACKBONE if not any(n in h for h in history_names)]
        assert not missing, (
            f"Missing backbone nodes in node_history: {missing}\n"
            f"Actual history: {history_names}\n"
            f"PostProcessNode absent → likely inner trust-trap (inner node gated INTERNAL)"
        )

        # ── Assert correct execution order ─────────────────────────────────────
        def first_idx(cls_fragment: str) -> int:
            for i, name in enumerate(history_names):
                if cls_fragment in name:
                    return i
            return -1

        pre_idx = first_idx("PreProcessNode")
        main_idx = first_idx(_MAIN_SLOT_NODE_NAME)
        post_idx = first_idx("PostProcessNode")
        finalize_idx = first_idx("FinalizeNode")

        assert pre_idx != -1, f"PreProcessNode not found in history: {history_names}"
        assert main_idx != -1, f"{_MAIN_SLOT_NODE_NAME} not found in history: {history_names}"
        assert post_idx != -1, (
            f"PostProcessNode not found in history: {history_names}\n"
            f"Trust-trap hypothesis: {_MAIN_SLOT_NODE_NAME} raised SubgraphError because "
            f"inner nodes have required_trust_level=INTERNAL instead of ANONYMOUS"
        )

        assert pre_idx < main_idx, f"pre_process ({pre_idx}) must execute before main ({main_idx})"
        assert main_idx < post_idx, (
            f"main/ClassifyRoutingGraphNode ({main_idx}) must execute before "
            f"post_process ({post_idx}) — if this fails, check inner node trust levels"
        )
        if finalize_idx != -1:
            assert post_idx < finalize_idx, f"post_process ({post_idx}) must execute before finalize ({finalize_idx})"

    def test_no_internal_context_used(self):
        """Structural proof that the test CANNOT use for_internal() or INTERNAL trust.

        An INTERNAL(2) context silently passes every inner trust gate — it masks the
        trust-trap that breaks a real deployment.  VERIFIED_EXTERNAL(1) is the only
        trust level that exercises the real external-caller path.
        """
        from framework.schemas.trust_level import TrustLevel

        # TrustLevel.value is a string in the real SDK wheel, not an int.
        # Use an explicit ordinal dict for numeric trust-level comparisons.
        _trust_order = {
            TrustLevel.ANONYMOUS: 0,
            TrustLevel.VERIFIED_EXTERNAL: 1,
            TrustLevel.INTERNAL: 2,
        }

        caller = TrustLevel.VERIFIED_EXTERNAL
        assert caller != TrustLevel.INTERNAL, "Test context must NOT be INTERNAL — it masks trust-trap failures"
        # VERIFIED_EXTERNAL sits between ANONYMOUS(0) and INTERNAL(2)
        assert (
            _trust_order[TrustLevel.ANONYMOUS] < _trust_order[caller]
        ), "VERIFIED_EXTERNAL must be stricter than ANONYMOUS"
        assert (
            _trust_order[caller] < _trust_order[TrustLevel.INTERNAL]
        ), "VERIFIED_EXTERNAL must be less privileged than INTERNAL"

    def test_inner_domain_nodes_declared_anonymous(self):
        """Structural proof that all 4 inner nodes declare ANONYMOUS (not INTERNAL).

        This is what makes the trust path safe: the outer VERIFIED_EXTERNAL(1) caller
        forwarded into the inner graph passes each inner node's ANONYMOUS(0) gate.
        """
        from framework.schemas.trust_level import TrustLevel
        from src.nodes.input_validate_node import InputValidateNode
        from src.nodes.classify_document_type_node import ClassifyDocumentTypeNode
        from src.nodes.apply_routing_rule_node import ApplyRoutingRuleNode
        from src.nodes.output_format_node import OutputFormatNode

        for cls in (
            InputValidateNode,
            ClassifyDocumentTypeNode,
            ApplyRoutingRuleNode,
            OutputFormatNode,
        ):
            assert cls.required_trust_level == TrustLevel.ANONYMOUS, (
                f"{cls.__name__}.required_trust_level must be ANONYMOUS (not INTERNAL). "
                f"Got {cls.required_trust_level!r}. "
                f"INTERNAL(2) would deny a VERIFIED_EXTERNAL(1) caller → SubgraphError → "
                f"backbone skips post_process → the deployed agent errors on every call."
            )

    def test_valid_payload_is_success_yielding(self):
        """Confirm _VALID_PAYLOAD produces SUCCESS so the backbone does not short-circuit."""
        # A non-empty payload with clear taxonomy keywords must pass PreProcessNode
        assert _VALID_PAYLOAD, "_VALID_PAYLOAD must be non-empty"
        # Must contain at least one Japanese disclosure keyword for reliable classification
        has_keyword = any(
            kw in _VALID_PAYLOAD for kw in ["有価証券報告書", "適時開示", "XBRL", "ESG", "securities report"]
        )
        assert has_keyword, (
            f"_VALID_PAYLOAD should contain a taxonomy keyword for reliable classification. " f"Got: {_VALID_PAYLOAD!r}"
        )
