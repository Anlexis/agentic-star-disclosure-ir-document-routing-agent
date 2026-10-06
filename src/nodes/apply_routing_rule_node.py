"""AgentCore Platform v1.0"""

# Inner domain node: step 3 of the classification workflow.
#
# Responsibility: turn the classification into a routing decision — which
# review queue the filing goes to, under which regulatory reference, and
# whether it needs a bulk-review team rather than a single reviewer.
#
# Two caller-supplied controls change the answer here, which is the point of
# accepting them at all:
#
#   * confidence_floor — a submission the classifier is less sure about than
#     the caller's floor is diverted to manual review rather than dropped into
#     a specialised queue on a weak signal. Absent floor means no diversion.
#   * page_count — a filing at or above the bulk threshold is flagged for the
#     bulk review mode. The threshold itself is a deployment setting, read from
#     the runtime config the inner graph was seeded with rather than from a
#     constant, so changing config/config.yaml changes the behaviour.
#
# The routing table is the single source of both the queue and the regulatory
# reference, so a queue can never be paired with a reference from another type.

from typing import Any, ClassVar, Dict

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.schemas.state import from_json
from src.services.caller_contract import BULK_REVIEW_PAGE_THRESHOLD

# ── Routing rules ─────────────────────────────────────────────────────────────
# Each canonical disclosure type maps to its approved review queue and the
# regulatory reference that governs it.
ROUTING_RULES: Dict[str, Dict[str, str]] = {
    "有価証券報告書": {
        "routing_target": "securities_report_review_queue",
        "regulatory_reference": "金融商品取引法第24条",
    },
    "適時開示": {
        "routing_target": "timely_disclosure_review_queue",
        "regulatory_reference": "東京証券取引所規則第401条",
    },
    "XBRL": {
        "routing_target": "xbrl_filing_review_queue",
        "regulatory_reference": "EDINET提出規則第4条",
    },
    "ESG": {
        "routing_target": "esg_report_review_queue",
        "regulatory_reference": "東京証券取引所コーポレートガバナンスコード",
    },
}

# Where anything the taxonomy could not place, or the classifier was not
# confident enough about, is sent instead.
MANUAL_REVIEW_RULE: Dict[str, str] = {
    "routing_target": "unclassified_review_queue",
    "regulatory_reference": "指定なし — 手動レビューが必要",
}

REVIEW_MODE_STANDARD = "standard"
REVIEW_MODE_BULK = "bulk"


def _bulk_threshold(state: Dict[str, Any]) -> int:
    """Read the bulk-review page threshold from the seeded runtime config.

    Falls back to the contract's documented default only when the deployment
    declared nothing, so a live setting is used where one exists instead of the
    config being declared and ignored.
    """
    intake = from_json(state.get("intake_config"), {}) or {}
    declared = intake.get("bulk_review_page_threshold")
    if isinstance(declared, bool) or not isinstance(declared, int):
        return BULK_REVIEW_PAGE_THRESHOLD
    return declared


class ApplyRoutingRuleNode(FunctionNode):
    """Map the classification to a review queue, a reference and a review mode.

    Declared ANONYMOUS — an inner node; the backbone's trust gate already ran.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: Dict[str, Any]) -> Dict[str, Any]:
        contract = from_json(state.get("caller_contract"), {}) or {}
        disclosure_type = str(state.get("disclosure_type") or "unknown")
        confidence = state.get("classification_confidence")
        floor = contract.get("confidence_floor")
        page_count = contract.get("page_count")

        rule = ROUTING_RULES.get(disclosure_type, MANUAL_REVIEW_RULE)

        # The floor arrived through the finite + bounded parser, so it is a real
        # number in [0, 1] here; the comparison cannot be silently defeated by a
        # NaN that makes every comparison False.
        overridden = False
        if floor is not None and isinstance(confidence, (int, float)) and float(confidence) < float(floor):
            rule = MANUAL_REVIEW_RULE
            overridden = True

        threshold = _bulk_threshold(state)
        review_mode = (
            REVIEW_MODE_BULK if isinstance(page_count, int) and page_count >= threshold else REVIEW_MODE_STANDARD
        )

        emit_trace_event(
            "routing_rule_applied",
            {
                "disclosure_type": disclosure_type,
                "routing_target": rule["routing_target"],
                "regulatory_reference": rule["regulatory_reference"],
                "confidence_override": overridden,
                "review_mode": review_mode,
                "bulk_threshold": threshold,
                "document_ref": state.get("validated_document_ref", ""),
            },
            state,
        )

        return {
            "routing_target": rule["routing_target"],
            "regulatory_reference": rule["regulatory_reference"],
            "routing_overridden": overridden,
            "review_mode": review_mode,
            "status": AgentStatus.SUCCESS.value,
        }
