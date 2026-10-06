# Disclosure & IR Document Routing Agent

AI agent for classifying and routing financial disclosure and investor relations documents, built with Agentic Star.

> **Category**: Cat 2 (domain-specific classification and routing pipeline)
> **Industry**: Finance
> **Template ID**: FIN-C2-019

## Overview

Sorts incoming corporate disclosure and investor-relations documents into the review queue that
should handle them, and says why.

A submission arrives as the document's text or its metadata block. The agent decides which
disclosure type it is — a securities report (有価証券報告書), a timely-disclosure notice
(適時開示), an XBRL filing, or an ESG report — scores how confident that reading is, attaches the
regulatory reference that governs the type, and names the review queue the filing belongs in.
Anything the taxonomy cannot place goes to manual review rather than to a guess.

Callers that already hold filing metadata can send it: the keywords their document-management
system tagged the filing with become a second classification signal, a page count decides between
standard and bulk review, and a confidence floor diverts a weakly-classified filing to manual
review instead of dropping it into a specialised queue on thin evidence. Every one of those fields
is bounds-checked, and a request that fails a check is refused rather than partly honoured.

What comes back is a routing decision and nothing else: a record with a fixed set of fields, each
holding a value drawn from a closed set. The submitted document text is not one of those fields,
and an assembled record that does not satisfy that contract is withheld whole rather than trimmed.

Typical users are corporate disclosure and IR teams routing a filing season's intake, and the
compliance reviewers on the receiving end of the queues.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >= 3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. If the platform is unreachable or the SDK version does not match, the agent fails at graph
compile / start-up preflight rather than starting in a partially working state. This is
intentional — a half-running agent is worse than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Calling it

`POST /invoke` takes the submission as `input` and the optional filing details as
`input_context`:

```json
{
  "input": "有価証券報告書 2025年度第2四半期 annual securities report",
  "session_id": "intake-0001",
  "input_context": {
    "channel": "dms",
    "document_ref": "edinet-1234-5678-90",
    "filing_year": 2026,
    "page_count": 640,
    "confidence_floor": 0.7,
    "metadata_terms": ["securities_report"]
  }
}
```

The response `output` is the routing decision, serialised:

```json
{
  "document_ref": "edinet-1234-5678-90",
  "disclosure_type": "有価証券報告書",
  "classification_confidence": 0.8,
  "routing_target": "securities_report_review_queue",
  "regulatory_reference": "金融商品取引法第24条",
  "review_mode": "bulk",
  "manual_review_required": false,
  "filing_year": 2026,
  "channel": "dms"
}
```

Every `input_context` field is optional; with none of them the agent classifies on the document
text alone. `channel`, `document_ref` and `metadata_terms` are restricted to inert alphabets,
`filing_year`, `page_count` and `confidence_floor` to finite values inside a declared range.

## Project Structure

```
src/nodes/       the domain nodes: intake, classify, route, assemble, and the output gate
src/graph/       the outer graph, the inner workflow, and the request bridge between them
src/services/    the caller-request contract: screens, bounds, and the identifier strip
src/schemas/     the shared state definition
src/api/         the standalone HTTP entry point
tests/           unit tests and end-to-end boundary tests
config/          the manifest and the runtime parameters
docs/            design and test documentation
```

`docs/02_design.md` describes the architecture and the security boundaries;
`docs/03_test_spec.md` maps every shipped test to what it proves.

## Customising

1. Edit the disclosure taxonomy in `src/nodes/classify_document_type_node.py` — each type carries
   its detection keywords and a confidence ceiling.
2. Edit the routing table in `src/nodes/apply_routing_rule_node.py` — each type maps to one review
   queue and one regulatory reference. The output gate derives what it will release from this
   table, so a queue added here is releasable without a second edit.
3. Tune `config/config.yaml`: `intake.bulk_review_page_threshold` is the declared page count at or
   above which a filing is flagged for bulk review.
4. Adjust the personal-data shapes in `src/services/caller_contract.py` for your jurisdiction —
   they drive both the inbound strip and the outbound refusal.
5. Re-run the test suite. The tests are written against behaviour, not wording, so they should
   keep passing across those changes.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
