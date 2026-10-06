# The output boundary: what the agent is allowed to release, and what happens
# to a decision that does not satisfy that contract.
#
# The stated invariant is a shape as much as a content rule — the released
# record has exactly the declared fields, each drawn from a closed set — and
# both halves are tested here, because the shape half is what makes "no
# submitted document text is released" enforceable rather than merely intended.

import json

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from src.nodes.output_format_node import DECISION_FIELDS, OutputFormatNode
from src.nodes.post_process_node import (
    REFERENCE_ALLOWLIST,
    ROUTING_ALLOWLIST,
    PostProcessNode,
    _scan_for_disallowed_content,
    security_gate_output,
)
from src.schemas.state import to_json


def _decision(**overrides):
    decision = {
        "document_ref": "edinet-1234-5678-90",
        "disclosure_type": "ESG",
        "classification_confidence": 0.8,
        "routing_target": "esg_report_review_queue",
        "regulatory_reference": "東京証券取引所コーポレートガバナンスコード",
        "review_mode": "standard",
        "manual_review_required": False,
        "filing_year": 2026,
        "channel": "dms",
    }
    decision.update(overrides)
    return decision


def _state(decision=None, **extra):
    serialised = json.dumps(decision if decision is not None else _decision(), ensure_ascii=False)
    state = {
        "result": serialised,
        "routing_decision": serialised,
        "validated_document_ref": "edinet-1234-5678-90",
        "node_history": [],
        "error_log": [],
        "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
    }
    state.update(extra)
    return state


class TestTheAllowlistsAreDerivedNotCopied:
    def test_every_routing_rule_target_is_releasable(self):
        from src.nodes.apply_routing_rule_node import MANUAL_REVIEW_RULE, ROUTING_RULES

        for rule in list(ROUTING_RULES.values()) + [MANUAL_REVIEW_RULE]:
            assert rule["routing_target"] in ROUTING_ALLOWLIST
            assert rule["regulatory_reference"] in REFERENCE_ALLOWLIST

    def test_nothing_else_is_releasable(self):
        from src.nodes.apply_routing_rule_node import MANUAL_REVIEW_RULE, ROUTING_RULES

        expected = {rule["routing_target"] for rule in ROUTING_RULES.values()}
        expected.add(MANUAL_REVIEW_RULE["routing_target"])
        assert set(ROUTING_ALLOWLIST) == expected


class TestAConformingDecisionIsReleased:
    def test_the_gate_passes_a_well_formed_decision(self):
        assert security_gate_output(_decision()) is None

    def test_the_node_releases_it_unchanged(self):
        result = PostProcessNode().execute(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert json.loads(result["formatted_output"]) == _decision()

    @pytest.mark.parametrize(
        "reference",
        ["edinet-1234-5678-90", "s100abcd", "7203_2026q1", "doc-a1b2c3d4e5f6a7b8", "tdnet-2026-0001"],
    )
    def test_ordinary_filing_references_are_not_mistaken_for_personal_data(self, reference):
        """The other direction of the personal-data guarantee: a gate that
        refused these would refuse every real filing."""
        assert security_gate_output(_decision(document_ref=reference)) is None

    def test_an_absent_optional_field_is_still_conforming(self):
        assert security_gate_output(_decision(filing_year=None, channel="")) is None


class TestTheGateNamesEveryViolationClass:
    @pytest.mark.parametrize(
        "decision,expected",
        [
            ("not a record", "decision_not_a_record"),
            (None, "decision_not_a_record"),
        ],
    )
    def test_a_non_record_is_refused(self, decision, expected):
        assert security_gate_output(decision) == expected

    def test_an_extra_field_is_refused(self):
        assert security_gate_output(_decision(submitted_text="…the whole document…")) == ("unexpected_decision_shape")

    def test_a_missing_field_is_refused(self):
        decision = _decision()
        del decision["routing_target"]
        assert security_gate_output(decision) == "unexpected_decision_shape"

    @pytest.mark.parametrize("value", ["Doc Ref", "REF/2026", "", "a" * 65, 12345])
    def test_a_reference_outside_the_inert_alphabet_is_refused(self, value):
        assert security_gate_output(_decision(document_ref=value)) == "malformed_document_ref"

    def test_a_disclosure_type_outside_the_taxonomy_is_refused(self):
        assert security_gate_output(_decision(disclosure_type="内部メモ")) == "unknown_disclosure_type"

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.1, 1.1, "0.8", True, None])
    def test_a_confidence_that_is_not_a_finite_fraction_is_refused(self, value):
        assert security_gate_output(_decision(classification_confidence=value)) == "malformed_confidence"

    def test_a_queue_outside_the_routing_table_is_refused(self):
        assert security_gate_output(_decision(routing_target="exfiltration_queue")) == (
            "routing_target_not_allowlisted"
        )

    def test_a_reference_outside_the_routing_table_is_refused(self):
        assert security_gate_output(_decision(regulatory_reference="社内規定")) == ("unknown_regulatory_reference")

    def test_an_unknown_review_mode_is_refused(self):
        assert security_gate_output(_decision(review_mode="express")) == "unknown_review_mode"

    def test_a_non_boolean_review_flag_is_refused(self):
        assert security_gate_output(_decision(manual_review_required="yes")) == ("malformed_manual_review_flag")

    @pytest.mark.parametrize("value", [1899, 2101, 2026.5, "2026", True])
    def test_a_filing_year_outside_its_range_is_refused(self, value):
        assert security_gate_output(_decision(filing_year=value)) == "malformed_filing_year"

    def test_a_channel_outside_the_inert_alphabet_is_refused(self):
        assert security_gate_output(_decision(channel="Marketing Copy")) == "malformed_channel"

    def test_a_free_text_field_would_be_caught_by_the_shape_check_first(self):
        """Worth stating plainly: on the shape this template ships, every
        released field is drawn from a closed set, so nothing CAN carry free
        text past the shape check. That is the guarantee — not an accident —
        and the content scan below is what covers a field added later with a
        more permissive rule."""
        assert security_gate_output(_decision(channel="ir.desk@example.co.jp")) == "malformed_channel"


class TestTheContentScanBehindTheShapeCheck:
    """The second layer, tested where it can be reached.

    The shape check makes this scan unnecessary for today's decision record —
    which is why it is exercised directly rather than through a contrived
    record. It earns its place as the check that still applies when
    DECISION_FIELDS grows a field whose own rule admits free text.
    """

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("questions to ir.desk@example.co.jp", "personal_data:email"),
            ("investor line 03-1234-5678", "personal_data:phone_number"),
            ("call +81-3-1234-5678", "personal_data:international_phone_number"),
        ],
    )
    def test_a_personal_data_shape_is_named(self, value, expected):
        assert _scan_for_disallowed_content({"note": value}) == expected

    def test_a_credential_shape_is_named_by_the_frameworks_own_detector(self):
        """A local pattern list narrower than the framework's would let a value
        through that the framework then catches inside this node — which makes
        the framework raise and discards the clearing below. Calling the
        framework's detector keeps the two sets identical by construction."""
        fake_jwt = "eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12
        assert _scan_for_disallowed_content({"note": fake_jwt}) == "credential:jwt"

    def test_a_violation_nested_inside_the_record_is_found(self):
        nested = {"outer": {"inner": ["fine", "ir.desk@example.co.jp"]}}
        assert _scan_for_disallowed_content(nested) == "personal_data:email"

    def test_a_clean_record_at_the_same_nesting_depth_is_passed(self):
        """The control that proves the scan is looking, not merely returning a
        name: same shape, nothing to find."""
        assert _scan_for_disallowed_content({"outer": {"inner": ["fine", "esg_report_review_queue"]}}) is None

    def test_the_violation_name_never_carries_the_matched_text(self):
        violation = _scan_for_disallowed_content({"note": "ir.desk@example.co.jp"})
        assert "ir.desk@example.co.jp" not in str(violation)


class TestContainmentOnViolation:
    """Returning an error is not containment on its own: the framework's output
    envelope falls back to state["result"] whatever the status, so a gate that
    raised — or set an error status without clearing — would ship the un-gated
    record inside the error envelope."""

    def _blocked(self, **overrides):
        return PostProcessNode().execute(_state(_decision(**overrides)))

    def test_the_status_is_an_error(self):
        assert self._blocked(routing_target="exfiltration_queue")["status"] == AgentStatus.ERROR.value

    def test_every_output_bearing_field_is_overwritten(self):
        result = self._blocked(routing_target="exfiltration_queue")
        rendered = json.dumps(result, default=str, ensure_ascii=False)
        assert "exfiltration_queue" not in rendered
        assert result["routing_decision"] is None
        assert result["routing_target"] is None
        assert result["disclosure_type"] is None

    def test_the_envelope_fallback_cannot_reach_the_record(self):
        """state["result"] is what the framework falls back to, so it is the
        field that has to stop carrying the record."""
        result = self._blocked(routing_target="exfiltration_queue")
        assert "exfiltration_queue" not in result["result"]
        assert "exfiltration_queue" not in result["formatted_output"]

    def test_the_caller_is_told_which_contract_failed_but_not_what_matched(self):
        secret = "ir.desk@example.co.jp"
        result = PostProcessNode().execute(_state(_decision(document_ref="x", channel=secret)))
        rendered = json.dumps(result, default=str, ensure_ascii=False)
        assert result["error_log"]
        assert secret not in rendered
        assert "Traceback" not in rendered

    def test_a_result_that_is_not_a_decision_at_all_is_withheld(self):
        """Fail closed: anything that does not parse back into a routing
        decision is withheld rather than passed through untouched."""
        result = PostProcessNode().execute(_state(**{"result": "…the raw submitted document text…"}))
        assert result["status"] == AgentStatus.ERROR.value
        assert "raw submitted document" not in json.dumps(result, ensure_ascii=False)


class TestTheAssembledRecordMatchesTheDeclaredShape:
    def test_the_node_assembles_exactly_the_declared_fields(self):
        state = {
            "caller_contract": to_json({"filing_year": 2026, "channel": "dms"}),
            "validated_document_ref": "edinet-1234-5678-90",
            "disclosure_type": "ESG",
            "classification_confidence": 0.8,
            "routing_target": "esg_report_review_queue",
            "regulatory_reference": "東京証券取引所コーポレートガバナンスコード",
            "review_mode": "standard",
            "routing_overridden": False,
            "node_history": [],
        }
        result = OutputFormatNode().execute(state)
        decision = json.loads(result["routing_decision"])
        assert tuple(sorted(decision)) == tuple(sorted(DECISION_FIELDS))
        assert security_gate_output(decision) is None

    def test_the_submission_text_is_not_one_of_the_assembled_fields(self):
        state = {
            "caller_contract": to_json({"submission": "confidential draft body text"}),
            "validated_document_ref": "doc-a1b2c3d4e5f6a7b8",
            "disclosure_type": "unknown",
            "classification_confidence": 0.4,
            "routing_target": "unclassified_review_queue",
            "regulatory_reference": "指定なし — 手動レビューが必要",
            "review_mode": "standard",
            "routing_overridden": False,
            "node_history": [],
        }
        result = OutputFormatNode().execute(state)
        assert "confidential draft body text" not in result["routing_decision"]


class TestOutputBoundaryTrust:
    def test_the_output_boundary_requires_a_verified_external_caller(self):
        assert PostProcessNode.required_trust_level == TrustLevel.VERIFIED_EXTERNAL

    def test_the_final_gates_are_not_overridden(self):
        """Overriding either would silently bypass the framework's own scans;
        the framework rejects it at class-definition time, and this pins it."""
        for node_class in (PostProcessNode, OutputFormatNode):
            assert "_security_gate_input" not in node_class.__dict__
            assert "_security_gate_output" not in node_class.__dict__
