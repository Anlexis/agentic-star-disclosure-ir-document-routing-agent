"""AgentCore Platform v1.0"""

# State is a flat TypedDict, never a Pydantic model: checkpoints are
# msgpack-serialized and model objects corrupt silently on the round trip.
# Extend AgentState with agent-specific fields only. Never add credentials or
# secrets.
#
# For the same reason, structured fields (dict / list[dict]) are stored as JSON
# STRINGS. Producers serialize with to_json() on write; consumers deserialize
# with from_json() on read.
#
# FinancialDisclosureIRRoutingAgent is a two-layer nested graph — an outer
# backbone plus an inner classification workflow — and the fields below cover
# both layers.
#
# Document-content note: the submitted disclosure text is never persisted into
# state. The request boundary derives a reference for it (the caller's own
# identifier when supplied, otherwise a digest of the submission) and only that
# reference travels onward. The routing decision released to the caller is a
# fixed-shape record of scalars; the output gate refuses anything else.

import json
from typing import Any, NotRequired, Optional

from framework.schemas.agent_state import AgentState


def to_json(value: Any) -> Optional[str]:
    """Serialize a dict/list state field to a JSON string.

    None passes through unchanged so an 'unset' field stays distinguishable
    from an empty container.
    """
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def from_json(value: Optional[str], default: Any = None) -> Any:
    """Deserialize a JSON-string state field back to its dict/list.

    None / empty / malformed input returns the supplied ``default``, so a
    missing or corrupt field is non-fatal for the consuming node.
    """
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


class State(AgentState):
    """Flat TypedDict for the disclosure routing agent.

    All shared fields (user_input, status, session_id, node_history,
    error_log, hitl_*, validated_input, result, formatted_output, ...) are
    inherited from AgentState. Domain fields are NotRequired so the TypedDict
    is valid at graph initialisation, before any node has written a value.
    """

    # ------------------------------------------------------------------
    # Outer layer — written by PreProcessNode / ClassifyRoutingGraphNode
    # ------------------------------------------------------------------

    # JSON STRING (to_json) of the validated caller contract, written by
    # PreProcessNode and carried into the inner graph by the submission bridge.
    # Deserialised shape: {"submission": str, "channel": str,
    # "document_ref": str, "filing_year": int | None, "page_count": int | None,
    # "confidence_floor": float | None, "metadata_terms": list[str]}.
    # Every value passed its bounds check before this field was written;
    # consumers read it back with from_json() and do not re-validate.
    caller_contract: NotRequired[Optional[str]]

    # ------------------------------------------------------------------
    # Inner layer — the classification workflow
    # ------------------------------------------------------------------

    # Runtime `intake` block forwarded by ClassifyRoutingGraphNode.
    # _parent_config() -> DisclosureClassificationWorkflow._extra_initial_state().
    # JSON STRING (to_json) of {"bulk_review_page_threshold": int}. Read back by
    # ApplyRoutingRuleNode via from_json().
    intake_config: NotRequired[Optional[str]]

    # Reference for the submitted document: the caller's own identifier when
    # supplied, otherwise a digest of the submission. Never the content itself.
    document_ref: NotRequired[Optional[str]]
    validated_document_ref: NotRequired[Optional[str]]

    # JSON STRING (to_json) of intake metadata — {"submission_chars": int,
    # "ref": str, "ref_origin": "caller" | "derived"}. Length and provenance
    # only; no excerpt of the submission.
    document_metadata: NotRequired[Optional[str]]

    # Classification outputs: one of the taxonomy labels (or "unknown") and a
    # confidence in [0.0, 1.0].
    disclosure_type: NotRequired[Optional[str]]
    classification_confidence: NotRequired[Optional[float]]

    # Routing outputs.
    regulatory_reference: NotRequired[Optional[str]]
    routing_target: NotRequired[Optional[str]]

    # True when the classifier's confidence fell below the caller's floor and
    # the submission was diverted to manual review instead of its type queue.
    routing_overridden: NotRequired[Optional[bool]]

    # "standard" or "bulk" — derived from the declared page count.
    review_mode: NotRequired[Optional[str]]

    # JSON STRING (to_json) of the assembled routing decision. Deserialised
    # shape is fixed and enforced by the output gate; see
    # src/nodes/post_process_node.py.
    routing_decision: NotRequired[Optional[str]]

    # ------------------------------------------------------------------
    # Tracing / audit — framework-managed; do NOT write from node code
    # ------------------------------------------------------------------

    trace_id: Optional[str]
    correlation_id: Optional[str]
