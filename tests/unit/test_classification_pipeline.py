# The three classification-workflow steps that turn a submission into a routing
# decision: establishing the document reference, classifying it, and applying
# the routing rules.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from src.nodes.apply_routing_rule_node import (
    MANUAL_REVIEW_RULE,
    REVIEW_MODE_BULK,
    REVIEW_MODE_STANDARD,
    ROUTING_RULES,
    ApplyRoutingRuleNode,
)
from src.nodes.classify_document_type_node import (
    UNCLASSIFIED_CONFIDENCE,
    UNCLASSIFIED_TYPE,
    ClassifyDocumentTypeNode,
    classify,
)
from src.nodes.input_validate_node import InputValidateNode
from src.schemas.state import from_json, to_json


def _contract(**overrides):
    contract = {
        "submission": "有価証券報告書 2025年度 annual securities report",
        "channel": "",
        "document_ref": "",
        "filing_year": None,
        "page_count": None,
        "confidence_floor": None,
        "metadata_terms": [],
    }
    contract.update(overrides)
    return contract


def _state(contract=None, **extra):
    state = {
        "caller_contract": to_json(contract if contract is not None else _contract()),
        "node_history": [],
        "error_log": [],
        "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
    }
    state.update(extra)
    return state


class TestInnerNodesAcceptTheForwardedCaller:
    """A VERIFIED_EXTERNAL caller's context is forwarded into the inner graph
    unchanged. An inner node requiring INTERNAL would deny every real caller
    once deployed, and only an end-to-end run would show it."""

    @pytest.mark.parametrize("node_class", [InputValidateNode, ClassifyDocumentTypeNode, ApplyRoutingRuleNode])
    def test_inner_nodes_do_not_out_rank_the_forwarded_caller(self, node_class):
        assert node_class.required_trust_level == TrustLevel.ANONYMOUS


class TestDocumentReference:
    def test_a_derived_reference_is_stable_for_the_same_submission(self):
        first = InputValidateNode().execute(_state())
        second = InputValidateNode().execute(_state())
        assert first["validated_document_ref"] == second["validated_document_ref"]
        assert first["validated_document_ref"].startswith("doc-")

    def test_a_different_submission_gets_a_different_reference(self):
        other = InputValidateNode().execute(_state(_contract(submission="ESG sustainability report")))
        base = InputValidateNode().execute(_state())
        assert other["validated_document_ref"] != base["validated_document_ref"]

    def test_the_callers_own_reference_is_preferred(self):
        result = InputValidateNode().execute(_state(_contract(document_ref="edinet-1234-5678-90")))
        assert result["validated_document_ref"] == "edinet-1234-5678-90"
        assert from_json(result["document_metadata"], {})["ref_origin"] == "caller"

    def test_the_intake_metadata_records_length_and_provenance_only(self):
        metadata = from_json(InputValidateNode().execute(_state())["document_metadata"], {})
        assert set(metadata) == {"submission_chars", "ref", "ref_origin"}
        assert metadata["submission_chars"] > 0

    def test_an_empty_submission_fails_closed(self):
        result = InputValidateNode().execute(_state(_contract(submission="")))
        assert result["status"] == AgentStatus.ERROR.value
        assert "validated_document_ref" not in result


class TestClassification:
    @pytest.mark.parametrize(
        "submission,expected",
        [
            ("有価証券報告書 2025年度 annual securities report", "有価証券報告書"),
            ("適時開示 重要事実 timely disclosure via TDnet", "適時開示"),
            ("XBRL structured data for the EDINET filing", "XBRL"),
            ("ESG sustainability report on climate disclosure", "ESG"),
        ],
    )
    def test_each_taxonomy_type_is_recognised_from_the_document_text(self, submission, expected):
        disclosure_type, confidence = classify(submission, [])
        assert disclosure_type == expected
        assert 0.6 <= confidence <= 0.9

    def test_text_with_no_taxonomy_evidence_is_left_unclassified(self):
        disclosure_type, confidence = classify("minutes of the quarterly logistics meeting", [])
        assert disclosure_type == UNCLASSIFIED_TYPE
        assert confidence == UNCLASSIFIED_CONFIDENCE

    def test_metadata_keywords_classify_a_document_the_text_alone_cannot(self):
        """The caller's filing system already tagged the document; that tag is
        the signal, and it has to survive the trip into the inner graph."""
        without = classify("quarterly filing package for the group", [])
        with_terms = classify("quarterly filing package for the group", ["xbrl"])
        assert without[0] == UNCLASSIFIED_TYPE
        assert with_terms[0] == "XBRL"

    def test_metadata_keywords_are_matched_against_the_same_taxonomy(self):
        assert classify("quarterly package", ["securities_report"])[0] == "有価証券報告書"

    def test_more_evidence_raises_confidence_up_to_the_type_ceiling(self):
        weak = classify("annual securities filing", [])[1]
        strong = classify("有価証券報告書 有報 securities report annual securities", [])[1]
        assert strong > weak
        assert strong <= 0.90

    def test_the_node_writes_the_classification_into_state(self):
        result = ClassifyDocumentTypeNode().execute(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["disclosure_type"] == "有価証券報告書"
        assert 0.0 <= result["classification_confidence"] <= 1.0


class TestRoutingRules:
    @pytest.mark.parametrize("disclosure_type,rule", sorted(ROUTING_RULES.items()))
    def test_each_type_routes_to_its_own_queue_and_reference(self, disclosure_type, rule):
        result = ApplyRoutingRuleNode().execute(_state(disclosure_type=disclosure_type, classification_confidence=0.8))
        assert result["routing_target"] == rule["routing_target"]
        assert result["regulatory_reference"] == rule["regulatory_reference"]
        assert result["routing_overridden"] is False

    def test_an_unrecognised_type_goes_to_manual_review(self):
        result = ApplyRoutingRuleNode().execute(
            _state(disclosure_type=UNCLASSIFIED_TYPE, classification_confidence=0.4)
        )
        assert result["routing_target"] == MANUAL_REVIEW_RULE["routing_target"]

    def test_a_confidence_below_the_callers_floor_diverts_to_manual_review(self):
        result = ApplyRoutingRuleNode().execute(
            _state(
                _contract(confidence_floor=0.9),
                disclosure_type="ESG",
                classification_confidence=0.7,
            )
        )
        assert result["routing_target"] == MANUAL_REVIEW_RULE["routing_target"]
        assert result["routing_overridden"] is True

    def test_a_confidence_at_or_above_the_floor_keeps_its_queue(self):
        result = ApplyRoutingRuleNode().execute(
            _state(
                _contract(confidence_floor=0.7),
                disclosure_type="ESG",
                classification_confidence=0.7,
            )
        )
        assert result["routing_target"] == ROUTING_RULES["ESG"]["routing_target"]
        assert result["routing_overridden"] is False

    def test_no_floor_means_no_diversion(self):
        result = ApplyRoutingRuleNode().execute(_state(disclosure_type="ESG", classification_confidence=0.61))
        assert result["routing_target"] == ROUTING_RULES["ESG"]["routing_target"]

    def test_the_review_mode_follows_the_declared_page_count(self):
        bulk = ApplyRoutingRuleNode().execute(
            _state(_contract(page_count=500), disclosure_type="ESG", classification_confidence=0.8)
        )
        standard = ApplyRoutingRuleNode().execute(
            _state(_contract(page_count=499), disclosure_type="ESG", classification_confidence=0.8)
        )
        assert bulk["review_mode"] == REVIEW_MODE_BULK
        assert standard["review_mode"] == REVIEW_MODE_STANDARD

    def test_the_bulk_threshold_comes_from_the_seeded_runtime_config(self):
        """A node that ignored the seeded config would keep using the built-in
        default, and changing config/config.yaml would change nothing."""
        state = _state(
            _contract(page_count=10),
            disclosure_type="ESG",
            classification_confidence=0.8,
            intake_config=to_json({"bulk_review_page_threshold": 5}),
        )
        assert ApplyRoutingRuleNode().execute(state)["review_mode"] == REVIEW_MODE_BULK
