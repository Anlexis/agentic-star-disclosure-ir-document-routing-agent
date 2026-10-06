"""AgentCore Platform v1.0"""

# Inner domain node: step 4 of the classification workflow.
#
# Responsibility: assemble the routing decision — the fixed-shape record that
# is the agent's whole product — and serialise it.
#
# Every value it writes comes from a vetted source: the routing table, the
# taxonomy, or a caller field that already passed its bounds check at the
# request boundary. The submitted document text is not one of those sources,
# which is how the "no document content is released" guarantee is kept at the
# point the record is built rather than patched afterwards.
#
# This node does NOT re-run the output gate. The gate lives once, at the
# backbone's output boundary (src/nodes/post_process_node.py), because that is
# the last point before the framework builds the response envelope and the only
# point where clearing a field actually withholds it. A second copy here would
# contain most violations on its own and would thereby make the boundary gate
# untestable — more checks, less assurance.

import json
from typing import Any, ClassVar, Dict

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.nodes.apply_routing_rule_node import REVIEW_MODE_STANDARD
from src.schemas.state import from_json

# The exact field set of a routing decision. The output gate checks the
# assembled record against this tuple, so adding a field here without deciding
# how it is validated there fails the gate rather than silently widening what
# the agent releases.
DECISION_FIELDS = (
    "document_ref",
    "disclosure_type",
    "classification_confidence",
    "routing_target",
    "regulatory_reference",
    "review_mode",
    "manual_review_required",
    "filing_year",
    "channel",
)


class OutputFormatNode(FunctionNode):
    """Assemble and serialise the routing decision.

    Declared ANONYMOUS — an inner node; the backbone's trust gate already ran.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: Dict[str, Any]) -> Dict[str, Any]:
        contract = from_json(state.get("caller_contract"), {}) or {}
        confidence = state.get("classification_confidence")

        decision: Dict[str, Any] = {
            "document_ref": state.get("validated_document_ref") or "",
            "disclosure_type": state.get("disclosure_type") or "unknown",
            "classification_confidence": float(confidence) if isinstance(confidence, (int, float)) else 0.0,
            "routing_target": state.get("routing_target") or "",
            "regulatory_reference": state.get("regulatory_reference") or "",
            "review_mode": state.get("review_mode") or REVIEW_MODE_STANDARD,
            "manual_review_required": bool(state.get("routing_overridden")),
            "filing_year": contract.get("filing_year"),
            "channel": contract.get("channel") or "",
        }

        serialised = json.dumps(decision, ensure_ascii=False)

        emit_trace_event(
            "routing_decision_assembled",
            {
                # Decision fields only — never an excerpt of the submission.
                "document_ref": decision["document_ref"],
                "disclosure_type": decision["disclosure_type"],
                "routing_target": decision["routing_target"],
                "confidence": decision["classification_confidence"],
                "review_mode": decision["review_mode"],
            },
            state,
        )

        return {
            "routing_decision": serialised,
            "result": serialised,
            "status": AgentStatus.SUCCESS.value,
        }
