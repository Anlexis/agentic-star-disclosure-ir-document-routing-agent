# Test Specification — FIN-C2-019 FinancialDisclosureIRRoutingAgent

## Strategy

- `python -m pytest tests/ -v` runs everything; there are two directories and they prove different
  things.
- `tests/unit/` proves each part in isolation, calling `execute()` **directly** where the guarantee
  belongs to the template. A test that went through the framework's own input gate would pass on a
  deployment where that gate is absent or configured off, and the payload would reach the
  classifier anyway.
- `tests/proof_of_boundary/` proves the assembled agent through the real ASGI `/invoke` entry
  point, with Bearer authentication, the compiled graph, the request bridge and the output gate all
  in the path. Everything that can only be observed end to end lives there.
- Assertions are behavioural. Nothing asserts a framework message's wording, so a framework
  release that rephrases a refusal does not turn a security guarantee red.

Shipped test files, and the count each contributes:

| File | Tests | Proves |
|---|---|---|
| `tests/unit/test_caller_contract.py` | 86 | The request contract: screens, bounds, inert alphabets, personal-data shapes |
| `tests/unit/test_pre_process_node.py` | 25 | The request boundary node: trust declaration, acceptance, refusal |
| `tests/unit/test_classification_pipeline.py` | 27 | Document reference, classification, routing rules |
| `tests/unit/test_output_gate.py` | 54 | The released-output contract and containment on violation |
| `tests/unit/test_graph_composition.py` | 20 | Backbone slots, the nested workflow, the request bridge |
| `tests/unit/test_config_manifest.py` | 8 | Manifest shape and the live runtime configuration |
| `tests/unit/test_framework_compliance_tc06_tc07.py` | 2 | The framework's final security gates are not overridden |
| `tests/proof_of_boundary/test_pb_invoke_endpoint.py` | 52 | The whole stack through `/invoke` |
| `tests/proof_of_boundary/test_pb_invoke_order.py` | 4 | Backbone execution order under a real external caller |
| `tests/proof_of_boundary/test_server_boot.py` | 5 | The standalone entry point imports, compiles, and carries the live config |
| `tests/proof_of_boundary/test_import_isolation.py` | 1 | No platform-SDK imports under `src/` |
| `tests/proof_of_boundary/test_state_safety.py` | 1 | State carries no model objects and no credential-shaped field names |
| `tests/proof_of_boundary/test_pb7_hitl_interrupt_propagation.py` | 2 | Human-review interrupt propagation — skipped: this template does not enable it |

Total: **285 passing, 2 conditionally skipped.**

---

## What the boundary tests prove

### The public path does real work

A submission returns a routing decision computed from the document — a taxonomy label, a queue, a
regulatory reference and a confidence — not a fixed baseline. A different document routes to a
different queue, and a document the taxonomy cannot place goes to manual review rather than to a
guess.

### Caller data reaches the inner workflow

This is the regression that matters, because the framework forwards only a string into a nested
graph. Proven end to end, not at node level:

- metadata keywords change the classification: the same submission routes to manual review without
  them and to the XBRL queue with them;
- a confidence floor visibly diverts a filing to manual review;
- the caller's own document reference and channel appear in the decision;
- absent structured parameters degrade to text-only classification rather than failing;
- a **declared runtime value** reaches the inner graph: a request at the declared page threshold is
  flagged for bulk review and one below it is not. A reader pointed at the manifest instead of the
  runtime file would degrade to a built-in default, and nothing would fail.

### Authentication

A missing token and a wrong token are both refused with 401 and the same generic body.

### Refusals, fail closed and without echo

Every caller-controlled number is driven through a non-finite matrix — `NaN`, `Infinity`,
`-Infinity`, out-of-range values, non-numeric text and booleans — per field, through the endpoint.
Labels outside the inert alphabet, malformed references and oversized keyword lists are refused.
The rejected value never appears in the response.

Instruction content is refused from every channel it can arrive on: the submission, a structured
value, a `\u`-escaped structured value, and a hostile field name. The opposite direction is tested
too — a real timely-disclosure notice containing "instructions for exercising voting rights" and
"the board disregarded the earlier draft" is classified and routed normally, because a screen that
fires on genuine filings is the failure that actually stops work.

### Adapter guards

Oversized structured parameters are refused with 413. A credential-shaped structured value is
refused with 400 naming the field — and ordinary domain text on the same field still passes.

### Output containment

Two tests inject a defect and drive the whole endpoint: a routing step that emits a queue outside
the routing table, and an assembly step that puts a direct identifier into the record. In both the
response carries an error status, no released record, no traceback and no source path. A third test
is the control — without the injected defect the identical request succeeds, so a red result in
the first two is the gate acting and not the request being broken.

Those tests were verified against three variants of the output boundary rather than assumed to be
load-bearing:

| Variant | Result |
|---|---|
| As shipped | all pass |
| Clearing removed, error status kept | 4 fail, including both containment tests |
| The pre-migration boundary restored (raise on a non-allowlisted queue, no personal-data check, no clearing) | 41 fail, including both containment tests — the response carried the routing decision inside an error envelope |

The third variant is the one that matters: layered guards mask each other, so removing them one at
a time can leave a full-invoke test green while it is in fact decorative.

---

## What the unit tests pin

### The request contract

- Attack forms named: chat-template control tokens (`<|…|>`, `[INST]`, `<<SYS>>`), override
  directives, role reassignment, prompt disclosure, script tags, code execution.
- Genuine disclosure language not named: nine sentences drawn from the vocabulary a securities
  report, a timely-disclosure notice and an ESG report actually use.
- Payload screening is depth-first and includes mapping keys; `\u`-escaped payloads are caught
  after parsing; nesting depth is bounded; a hostile key is reported by position, never repeated.
- Directives hidden behind zero-width characters are caught in both readings — the character used
  as a word separator, and used to split a word.
- Personal-data shapes are named; the strip removes them from the submission; and five realistic
  filing references (`edinet-1234-5678-90`, `s100abcd`, `7203_2026q1`, `doc-…`, `tdnet-2026-0001`)
  are **not** read as personal identifiers. That last set is the reason the numeric shape carries
  identifier guards.
- Every numeric field: non-finite matrix, out-of-range matrix, and the accepted range.
- Refusals name the field and never the value.

### Classification and routing

- Each taxonomy type is recognised from document text; text with no evidence is left unclassified.
- Metadata keywords classify a document the text alone cannot, and are matched against the same
  taxonomy rather than a second lookup table.
- More evidence raises confidence, up to the type's ceiling.
- Each type routes to its own queue and reference; an unrecognised type goes to manual review.
- The confidence floor diverts below it and does not divert at or above it.
- The review mode follows the declared page count, and the threshold comes from the seeded runtime
  config — a node that ignored it would keep using its built-in default and the config file would
  be decorative.
- The derived document reference is stable for the same submission and differs for a different
  one; the caller's own reference wins when supplied; the intake metadata records length and
  provenance only.
- An empty submission fails closed rather than being classified as `unknown` and routed as if it
  were real.

### The output boundary

- The allowlists are derived from the routing table, not copied beside it: every rule's queue and
  reference is releasable, and nothing else is.
- Each violation class is named individually — shape, reference alphabet, taxonomy membership,
  confidence range, queue, regulatory reference, review mode, flag type, filing year, channel.
- The content scan behind the shape check is exercised directly, with a clean control at the same
  nesting depth so a passing scan is evidence the scan looked rather than evidence it returns
  `None`.
- On violation every output-bearing field is overwritten, including the one the response envelope
  falls back to.
- A `result` that is not a routing decision at all is withheld.

### Composition

- The outer graph inherits the framework base directly, fills the five backbone slots, and
  overrides neither `add_edges()` nor `get_output()`.
- The inner workflow registers four nodes whose constructors take no arguments, and its
  `add_edges()` registers no conditional path callable — pinned against the code object, not
  against a comment.
- A non-integer bulk threshold fails at construction rather than silently retiring the bulk path.
- What `extract_input()` stashes on the bridge is what `_extra_initial_state()` seeds.
- The forwarded runtime config is never empty.
- The inner nodes declare `ANONYMOUS`, so a forwarded `VERIFIED_EXTERNAL` caller passes; the
  backbone order test drives the whole graph with such a caller and asserts `post_process` is
  reached, which is the mechanistic proof that no inner node out-ranks a real caller.
