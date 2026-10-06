"""AgentCore Platform v1.0"""

# Inner domain node: step 1 of the classification workflow.
#
# Responsibility: settle the identity of the submission before anything is
# classified. The caller's own document reference is preferred when one was
# supplied; otherwise a digest of the submission is derived so the same
# document always gets the same reference. Only that reference travels onward —
# the document text itself is never written into a field that leaves the graph.
#
# The request boundary (PreProcessNode) already validated and screened
# everything the caller sent, so this node re-parses nothing. It re-checks the
# one invariant it depends on — that a submission is actually present — and
# refuses if the bridge handed it nothing, which fails closed rather than
# classifying an empty document as "unknown" and routing it as if it were real.

import hashlib
from typing import Any, ClassVar, Dict

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.schemas.state import from_json, to_json

# Length of the derived reference digest. Sixteen hex characters is enough for
# de-duplication inside one filing season without carrying the whole digest
# through every audit line.
_DERIVED_REF_CHARS = 16


class InputValidateNode(FunctionNode):
    """Establish the document reference and the intake metadata.

    Declared ANONYMOUS because the backbone's PreProcessNode (VERIFIED_EXTERNAL)
    already enforced the external caller trust gate. The inner graph receives
    the outer InvocationContext unchanged, so a real external caller arrives at
    VERIFIED_EXTERNAL and passes. Requiring INTERNAL here would deny every
    external caller once deployed.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: Dict[str, Any]) -> Dict[str, Any]:
        contract = from_json(state.get("caller_contract"), {}) or {}
        text = str(contract.get("submission") or state.get("validated_input") or "").strip()

        if not text:
            emit_trace_event(
                "input_validate_rejected",
                {"reason": "empty_submission"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["InputValidateNode: the document submission is empty"],
                # The runner surfaces `formatted_output or result` as `output`. A reason left only in
                # error_log reaches no one: the terminal result carries just `status`, and get_output()
                # does not copy error_log out of the graph -- the caller sees a blank spinner.
                "formatted_output": "Request could not be completed. "
                + ("InputValidateNode: the document submission is empty"),
            }

        caller_reference = str(contract.get("document_ref") or "")
        if caller_reference:
            reference = caller_reference
            origin = "caller"
        else:
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:_DERIVED_REF_CHARS]
            reference = f"doc-{digest}"
            origin = "derived"

        metadata: Dict[str, Any] = {
            "submission_chars": len(text),
            "ref": reference,
            "ref_origin": origin,
        }

        emit_trace_event(
            "input_validate_complete",
            {"document_ref": reference, "ref_origin": origin, "submission_chars": len(text)},
            state,
        )

        return {
            "validated_document_ref": reference,
            "document_metadata": to_json(metadata),
            "status": AgentStatus.SUCCESS.value,
        }
