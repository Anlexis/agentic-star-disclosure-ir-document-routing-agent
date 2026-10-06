"""AgentCore Platform v1.0"""

# PostProcessNode — the output boundary of the agent.
#
# The stated output invariant of this template is:
#
#   what leaves the agent is a routing decision and nothing else — a record
#   with exactly the declared fields, each holding a value drawn from a closed
#   set (an approved review queue, a taxonomy label, a regulatory reference
#   from the routing table, a confidence in [0, 1], an inert document
#   reference) — and it carries no submitted document text, no credential-shaped
#   string and no personal-data shape.
#
# This node is the single place that invariant is enforced, and it is enforced
# on the record that is actually about to be released.
#
# Why one place and not several: the framework's response envelope falls back
# to state["result"] whatever the status, so a gate that merely raised — or set
# an error status without clearing the fields — would still ship the un-gated
# record inside the error envelope, together with a traceback. Clearing is the
# containment; and a duplicate of this check earlier in the pipeline would
# contain most violations before they ever reach here, which would leave this
# gate green whether or not it worked.
#
# The credential scan calls the framework's own detector rather than a local
# pattern list. A local list narrower than the framework's would let through a
# value the framework then catches inside the same node, which makes the
# framework raise, discards this node's clearing, and turns a contained refusal
# back into an uncontained one.
#
# No _extra_security_gate_input/_output instance methods are defined here: the
# framework auto-wraps such hooks, and defining them would change the node's
# call pipeline.

import logging
import math
import re
from typing import Any, ClassVar, Dict, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.security.credential_detector import detect_credentials_in_value
from shared.utils.audit_logger import emit_trace_event
from src.nodes.apply_routing_rule_node import (
    MANUAL_REVIEW_RULE,
    REVIEW_MODE_BULK,
    REVIEW_MODE_STANDARD,
    ROUTING_RULES,
)
from src.nodes.classify_document_type_node import DISCLOSURE_TAXONOMY, UNCLASSIFIED_TYPE
from src.nodes.output_format_node import DECISION_FIELDS
from src.schemas.state import from_json
from src.services.caller_contract import (
    FILING_YEAR_MAX,
    FILING_YEAR_MIN,
    PERSONAL_DATA_PATTERNS,
    find_personal_data,
)

logger = logging.getLogger(__name__)

# The closed sets a released decision may draw from. All three are DERIVED from
# the tables the pipeline routes with, so a queue or a reference can never be
# approved here and absent there — the hand-maintained copy this replaced
# carried a "keep in sync" comment, which is a drift waiting to happen.
ROUTING_ALLOWLIST = frozenset(
    [rule["routing_target"] for rule in ROUTING_RULES.values()] + [MANUAL_REVIEW_RULE["routing_target"]]
)
REFERENCE_ALLOWLIST = frozenset(
    [rule["regulatory_reference"] for rule in ROUTING_RULES.values()] + [MANUAL_REVIEW_RULE["regulatory_reference"]]
)
DISCLOSURE_TYPES = frozenset(list(DISCLOSURE_TAXONOMY.keys()) + [UNCLASSIFIED_TYPE])
REVIEW_MODES = frozenset({REVIEW_MODE_STANDARD, REVIEW_MODE_BULK})

_DOCUMENT_REF_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
_CHANNEL_RE = re.compile(r"^[a-z0-9_]{1,32}$")

_BLOCKED_NOTICE = (
    "[OUTPUT WITHHELD — the assembled routing decision did not satisfy the released-output "
    "contract. Resubmit the document without credential-like or personal-data-like strings.]"
)

# Every state field that can carry released content. On a violation each one is
# overwritten, so no path out of the graph — including the framework's own
# fallback to state["result"] — can reach the un-gated decision.
_OUTPUT_BEARING_FIELDS = (
    "result",
    "formatted_output",
    "routing_decision",
    "routing_target",
    "regulatory_reference",
    "disclosure_type",
    "classification_confidence",
    "review_mode",
    "routing_overridden",
    "document_metadata",
)


def _scan_for_disallowed_content(value: Any) -> Optional[str]:
    """Name the first credential or personal-data shape in a value, or None.

    Walks nested mappings and sequences. Caller material can ride one level
    down inside the record, and a scan that only looked at top-level strings
    would report zero findings on a payload whose leak sits in a nested value.
    Returns the pattern NAME — never the matched text, which would put the leak
    into the log that reports it.
    """
    if isinstance(value, dict):
        for item in value.values():
            hit = _scan_for_disallowed_content(item)
            if hit:
                return hit
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            hit = _scan_for_disallowed_content(item)
            if hit:
                return hit
        return None
    if value is None or isinstance(value, bool):
        return None
    text = str(value)
    findings = detect_credentials_in_value(text)
    if findings:
        return f"credential:{findings[0]['type']}"
    personal = find_personal_data(text)
    if personal:
        return f"personal_data:{personal}"
    return None


def security_gate_output(decision: Any) -> Optional[str]:
    """Check a routing decision against the released-output contract.

    Returns the name of the first violation, or None when the record may be
    released. The shape check comes first: it is what makes "no document text
    is released" enforceable rather than merely intended, because a record
    whose fields are all drawn from closed sets has nowhere to put free text.
    """
    if not isinstance(decision, dict):
        return "decision_not_a_record"
    if tuple(sorted(decision)) != tuple(sorted(DECISION_FIELDS)):
        return "unexpected_decision_shape"

    document_ref = decision["document_ref"]
    if not isinstance(document_ref, str) or not _DOCUMENT_REF_RE.match(document_ref):
        return "malformed_document_ref"

    if decision["disclosure_type"] not in DISCLOSURE_TYPES:
        return "unknown_disclosure_type"

    confidence = decision["classification_confidence"]
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(float(confidence))
        or not 0.0 <= float(confidence) <= 1.0
    ):
        return "malformed_confidence"

    if decision["routing_target"] not in ROUTING_ALLOWLIST:
        return "routing_target_not_allowlisted"

    if decision["regulatory_reference"] not in REFERENCE_ALLOWLIST:
        return "unknown_regulatory_reference"

    if decision["review_mode"] not in REVIEW_MODES:
        return "unknown_review_mode"

    if not isinstance(decision["manual_review_required"], bool):
        return "malformed_manual_review_flag"

    filing_year = decision["filing_year"]
    if filing_year is not None and (
        isinstance(filing_year, bool)
        or not isinstance(filing_year, int)
        or not FILING_YEAR_MIN <= filing_year <= FILING_YEAR_MAX
    ):
        return "malformed_filing_year"

    channel = decision["channel"]
    if not isinstance(channel, str) or (channel and not _CHANNEL_RE.match(channel)):
        return "malformed_channel"

    return _scan_for_disallowed_content(decision)


class PostProcessNode(FunctionNode):
    """Output gate: release the routing decision only when it satisfies its contract.

    Declared VERIFIED_EXTERNAL: a caller who could not clear the entry gate must
    not receive gated output either.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: Dict[str, Any]) -> Dict[str, Any]:
        result = state.get("result", "")

        if not result or not str(result).strip():
            # Nothing was assembled — there is nothing to release and nothing
            # to gate.
            emit_trace_event(
                "post_process_empty",
                {"document_ref": state.get("validated_document_ref", "")},
                state,
            )
            return {
                "formatted_output": result,
                "status": AgentStatus.SUCCESS.value,
            }

        # The gate reads the field that is actually released. The framework's
        # envelope returns state["formatted_output"] or state["result"], so
        # checking `result` checks the released bytes rather than a sibling
        # field that is merely expected to match them. Anything that does not
        # parse back into a routing decision is withheld — including a value
        # some other node substituted.
        decision = from_json(str(result), None)
        violation = security_gate_output(decision)
        if violation:
            logger.error("PostProcessNode: output withheld — violation type: %s", violation)
            emit_trace_event(
                "post_process_blocked",
                {"violation": violation},
                state,
            )
            blocked: Dict[str, Any] = {field: None for field in _OUTPUT_BEARING_FIELDS}
            blocked.update(
                {
                    "formatted_output": _BLOCKED_NOTICE,
                    "result": _BLOCKED_NOTICE,
                    "status": AgentStatus.ERROR.value,
                    "error_log": [f"PostProcessNode: output withheld — released-output contract ({violation})"],
                }
            )
            return blocked

        emit_trace_event(
            "post_process_complete",
            {
                "document_ref": decision["document_ref"],
                "routing_target": decision["routing_target"],
                "review_mode": decision["review_mode"],
            },
            state,
        )

        return {
            "formatted_output": result,
            "status": AgentStatus.SUCCESS.value,
        }


# Re-exported so a reader of the personal-data guarantee finds both directions
# from one place: the inbound strip lives in the caller contract, the outbound
# refusal here, and both read the same pattern definition.
__all__ = [
    "DISCLOSURE_TYPES",
    "PERSONAL_DATA_PATTERNS",
    "PostProcessNode",
    "REFERENCE_ALLOWLIST",
    "REVIEW_MODES",
    "ROUTING_ALLOWLIST",
    "security_gate_output",
]
