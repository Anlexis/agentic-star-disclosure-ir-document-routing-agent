"""AgentCore Platform v1.0"""

# Inner domain node: step 2 of the classification workflow.
#
# Responsibility: decide which disclosure type the submission is, and how
# confident that decision is.
#
# Two signals feed the decision, and they are scored against the SAME taxonomy:
#
#   * the submission text — keyword evidence found in the document itself;
#   * the metadata keywords the caller's document-management system already
#     tagged the filing with. Those are worth more per hit than a word found in
#     running text, because a filing system tags a document deliberately while
#     a document may mention a disclosure type only in passing.
#
# The taxonomy is a table, not a model. Swapping the table for a model call
# changes nothing outside this file: the node's execute() contract and the
# state fields it writes stay as they are.

from typing import Any, ClassVar, Dict, List, Set, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.schemas.state import from_json

# ── Disclosure taxonomy ───────────────────────────────────────────────────────
# Each entry maps a canonical disclosure type to its detection keywords
# (Japanese and English) and the confidence ceiling for that type. The ceilings
# differ because the types are not equally distinguishable: a securities report
# announces itself in its own title, while an ESG report shares much of its
# vocabulary with ordinary corporate communications.
DISCLOSURE_TAXONOMY: Dict[str, Dict[str, Any]] = {
    "有価証券報告書": {
        "keywords": [
            "有価証券報告書",
            "securities report",
            "annual securities",
            "yuukashouken",
            "有報",
        ],
        "confidence_ceiling": 0.90,
    },
    "適時開示": {
        "keywords": [
            "適時開示",
            "timely disclosure",
            "material fact",
            "重要事実",
            "tdnet",
        ],
        "confidence_ceiling": 0.85,
    },
    "XBRL": {
        "keywords": [
            "xbrl",
            "structured data",
            "financial data",
            "edinet xbrl",
        ],
        "confidence_ceiling": 0.88,
    },
    "ESG": {
        "keywords": [
            "esg",
            "sustainability",
            "サステナビリティ",
            "環境報告",
            "climate disclosure",
            "non-financial",
            "csr",
        ],
        "confidence_ceiling": 0.82,
    },
}

UNCLASSIFIED_TYPE = "unknown"
UNCLASSIFIED_CONFIDENCE = 0.40

# Confidence starts here once there is any evidence at all, and each further
# unit of evidence adds one step, up to the type's own ceiling.
_CONFIDENCE_BASE = 0.60
_CONFIDENCE_STEP = 0.05

# A metadata keyword from the source system counts for this many text hits.
_METADATA_TERM_WEIGHT = 2


def _score(keywords: List[str], haystack: str, terms: Set[str]) -> int:
    """Count taxonomy evidence for one type across both signals."""
    text_hits = sum(1 for keyword in keywords if keyword in haystack)
    term_hits = sum(1 for keyword in keywords if keyword in terms)
    return text_hits + _METADATA_TERM_WEIGHT * term_hits


def classify(text: str, metadata_terms: List[str]) -> Tuple[str, float]:
    """Return (disclosure_type, confidence) for a submission.

    Ties go to the type that appears first in the taxonomy, which keeps the
    result stable across runs: a caller resubmitting the same document gets the
    same routing decision.
    """
    haystack = text.lower()
    # Metadata keywords arrive as inert labels (securities_report); the taxonomy
    # is written in prose (securities report). Normalising here means one
    # vocabulary serves both signals instead of a second lookup table that would
    # have to be kept in step with this one.
    terms = {term.replace("_", " ").strip().lower() for term in metadata_terms}

    best_type = UNCLASSIFIED_TYPE
    best_score = 0
    best_confidence = UNCLASSIFIED_CONFIDENCE

    for disclosure_type, spec in DISCLOSURE_TAXONOMY.items():
        score = _score(spec["keywords"], haystack, terms)
        if score > best_score:
            best_score = score
            best_type = disclosure_type
            best_confidence = min(
                _CONFIDENCE_BASE + _CONFIDENCE_STEP * score,
                float(spec["confidence_ceiling"]),
            )

    return best_type, round(best_confidence, 3)


class ClassifyDocumentTypeNode(FunctionNode):
    """Classify the submission against the disclosure taxonomy.

    Declared ANONYMOUS — an inner node; the backbone's trust gate already ran.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: Dict[str, Any]) -> Dict[str, Any]:
        contract = from_json(state.get("caller_contract"), {}) or {}
        text = str(contract.get("submission") or state.get("validated_input") or "")
        metadata_terms = contract.get("metadata_terms") or []
        if not isinstance(metadata_terms, list):
            metadata_terms = []

        disclosure_type, confidence = classify(text, metadata_terms)

        emit_trace_event(
            "classify_complete",
            {
                "disclosure_type": disclosure_type,
                "confidence": confidence,
                "metadata_term_count": len(metadata_terms),
                "document_ref": state.get("validated_document_ref", ""),
            },
            state,
        )

        return {
            "disclosure_type": disclosure_type,
            "classification_confidence": confidence,
            "status": AgentStatus.SUCCESS.value,
        }
