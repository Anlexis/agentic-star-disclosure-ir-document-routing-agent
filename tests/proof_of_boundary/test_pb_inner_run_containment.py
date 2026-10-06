# Containment for a non-success inner run — the road that skips the output gate.
#
# The classification workflow is a nested graph. Its get_output() hands a
# sub_result to ClassifyRoutingGraphNode.merge_output(), which writes it into
# the outer state — including state["result"]. The backbone then routes on the
# status: anything other than SUCCESS goes straight to finalize, so
# PostProcessNode (the output gate) never runs, and the framework's response
# envelope resolves `formatted_output or result`. An inner run that assembled a
# decision and then ended on a non-success status therefore has a road to the
# caller with no gate anywhere on it — the decision travels as `result`, not as
# the gated `formatted_output`.
#
# These tests drive that road end to end. The fault is injected at the last
# inner node, so the draft answer is assembled by the real node from the real
# submission and the run really does terminate non-success; nothing here builds
# a state dict by hand, because it is the live routing decision — which node
# runs next — that opens or closes this channel.
#
# The assertion walks the whole response, keys and values, at every depth: the
# point is that the answer is nowhere in what the caller receives, not that one
# expected field happens to be empty.

import json
import os
import warnings

import pytest

from framework.schemas.agent_status import AgentStatus

_TOKEN = "pb-inner-containment-token"
_SUBMISSION = "有価証券報告書 2025年度第2四半期 annual securities report"

# Unmistakable, and placed inside the assembled decision rather than alongside
# it: a marker that only ever existed in inner state, so finding it anywhere in
# the response means the inner run's own draft answer travelled out.
_DRAFT_MARKER = "zz-inner-draft-answer-marker-zz"

# Every terminal status that is not SUCCESS. `error` is on the list because it
# is the one molt's finding named, and because it takes a different route out
# of GraphNode (error_strategy="propagate" raises before merge_output) — so it
# proves containment on both roads rather than only the one that leaks.
_NON_SUCCESS_STATUSES = (
    AgentStatus.TIMEOUT.value,
    AgentStatus.CANCELLED.value,
    AgentStatus.ERROR.value,
)


def _strings(value):
    """Yield every key and every scalar in a nested structure, as text."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)
    else:
        yield str(value)


def _contains(value, needle: str) -> bool:
    return any(needle in text for text in _strings(value))


def _inject_draft_then_fail(monkeypatch, status_value: str) -> None:
    """Let the real inner pipeline assemble its decision, then end non-success.

    This is the shape of the failure that matters: the draft answer is already
    in inner state when the run stops. A node that fails before assembling one
    has nothing to leak and would prove nothing.
    """
    import src.nodes.output_format_node as output_format

    original = output_format.OutputFormatNode.execute

    def assembled_then_failed(self, state):
        result = original(self, state)
        decision = json.loads(result["routing_decision"])
        decision["routing_target"] = _DRAFT_MARKER
        drafted = json.dumps(decision, ensure_ascii=False)
        return {**result, "routing_decision": drafted, "result": drafted, "status": status_value}

    monkeypatch.setattr(output_format.OutputFormatNode, "execute", assembled_then_failed)


@pytest.fixture(scope="module")
def client():
    previous = os.environ.get("INVOKE_AUTH_TOKEN")
    os.environ["INVOKE_AUTH_TOKEN"] = _TOKEN
    with warnings.catch_warnings():
        # Import-time deprecation notice from the sync test client shim; not
        # application behaviour.
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient

        import src.api.server as server

        with TestClient(server.app) as test_client:
            yield test_client
    if previous is None:
        os.environ.pop("INVOKE_AUTH_TOKEN", None)
    else:
        os.environ["INVOKE_AUTH_TOKEN"] = previous


def _invoke(client, payload):
    return client.post("/invoke", json=payload, headers={"Authorization": f"Bearer {_TOKEN}"})


class TestTheScanItself:
    """A containment assertion is only worth the scan behind it."""

    def test_the_scan_finds_the_marker_in_a_nested_value(self):
        assert _contains({"envelope": {"items": [{"decision": _DRAFT_MARKER}]}}, _DRAFT_MARKER)

    def test_the_scan_finds_the_marker_used_as_a_key(self):
        assert _contains({"envelope": {_DRAFT_MARKER: "x"}}, _DRAFT_MARKER)

    def test_the_scan_is_silent_on_a_clean_body(self):
        assert not _contains({"output": None, "status": "timeout", "node_history": ["FinalizeNode"]}, _DRAFT_MARKER)


class TestCompiledGraphContainment:
    """Through the compiled graph — the layer that decides which node runs next."""

    @pytest.mark.parametrize("status_value", _NON_SUCCESS_STATUSES)
    def test_a_non_success_inner_run_releases_no_draft_answer(self, monkeypatch, status_value):
        from framework.schemas.invocation_context import InvocationContext
        from framework.schemas.trust_level import TrustLevel
        from src.graph.graph import Graph, runtime_config

        _inject_draft_then_fail(monkeypatch, status_value)

        agent = Graph(config=runtime_config())
        agent.compile()
        ctx = InvocationContext(
            session_id=f"pb-inner-{status_value}",
            caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        )
        envelope = agent.invoke(_SUBMISSION, ctx=ctx)

        assert envelope["status"] == status_value
        assert not envelope.get("output")
        assert not _contains(envelope, _DRAFT_MARKER), envelope

        # The containment is NOT the output gate doing its job: on a non-success
        # status the backbone skips post_process entirely. Asserting its absence
        # keeps this test honest about which layer is being proven.
        assert "PostProcessNode" not in [str(entry) for entry in envelope.get("node_history", [])]


class TestInvokeEndpointContainment:
    """Through the real ASGI entry point, the way an external caller reaches it."""

    @pytest.mark.parametrize("status_value", _NON_SUCCESS_STATUSES)
    def test_a_non_success_inner_run_releases_no_draft_answer(self, client, monkeypatch, status_value):
        _inject_draft_then_fail(monkeypatch, status_value)

        response = _invoke(client, {"input": _SUBMISSION, "session_id": f"pb-inner-http-{status_value}"})

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == status_value
        assert not body.get("output")
        assert not _contains(body, _DRAFT_MARKER), json.dumps(body, ensure_ascii=False)
        assert _DRAFT_MARKER not in response.text

    def test_the_same_request_without_the_fault_still_releases_its_decision(self, client):
        """The control: a red above is containment acting, not a broken request."""
        response = _invoke(client, {"input": _SUBMISSION, "session_id": "pb-inner-http-control"})

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == AgentStatus.SUCCESS.value
        decision = json.loads(body["output"])
        assert decision["routing_target"] == "securities_report_review_queue"
        assert _DRAFT_MARKER not in response.text
