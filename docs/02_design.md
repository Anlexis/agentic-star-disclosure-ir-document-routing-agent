# Design — FIN-C2-019 FinancialDisclosureIRRoutingAgent

## Position in the framework

| Aspect | Value |
|---|---|
| Agent class | `Graph` (`src/graph/graph.py`) |
| L1 Base (framework base class) | `AgentBaseGraph` — direct framework inheritance |
| Inner graph base | `BaseGraph` — `DisclosureClassificationWorkflow` |
| Pattern | two-layer nested: the fixed outer backbone, with a `GraphNode` in the `main` slot wrapping an inner workflow graph |
| Category | Cat 2 — a domain-specific classification and routing pipeline |
| Generation mode | deterministic — the classifier is a keyword-weighted table, not a model call |

Three-layer separation, as the framework requires it:

- **State** — a flat `TypedDict` (`src/schemas/state.py`). No model objects, no credentials.
  Structured fields travel as JSON strings so a checkpointed run round-trips intact.
- **Nodes** — `FunctionNode` subclasses implementing `execute(state) -> dict` and returning only
  the fields they change.
- **Graph** — the outer `Graph(AgentBaseGraph)` with `ClassifyRoutingGraphNode(GraphNode)` in the
  `main` slot; `add_edges()` is not overridden on the outer graph.

---

## Architecture

```
Outer backbone (AgentBaseGraph — fixed):
  START → initialize → pre_process → main → {route} → post_process → finalize → END
                                       │  (RETRY, bounded by max_retry)
                                       └→ pre_process

  main = ClassifyRoutingGraphNode, which invokes:

Inner workflow (BaseGraph — linear):
  START → input_validate → classify → apply_routing → output_format → END
```

### Node map

| Slot | Class | File | Trust | Responsibility |
|---|---|---|---|---|
| `pre_process` | `PreProcessNode` | `src/nodes/pre_process_node.py` | `VERIFIED_EXTERNAL` | Trust gate; validates and screens everything the caller sent |
| `main` | `ClassifyRoutingGraphNode` | `src/graph/graph.py` | `ANONYMOUS` | Wraps the inner workflow; bridges the validated request into it |
| `post_process` | `PostProcessNode` | `src/nodes/post_process_node.py` | `VERIFIED_EXTERNAL` | The output gate — the single enforcement point of the released-output contract |
| inner `input_validate` | `InputValidateNode` | `src/nodes/input_validate_node.py` | `ANONYMOUS` | Settles the document reference and the intake metadata |
| inner `classify` | `ClassifyDocumentTypeNode` | `src/nodes/classify_document_type_node.py` | `ANONYMOUS` | Scores the submission and the caller's metadata keywords against the taxonomy |
| inner `apply_routing` | `ApplyRoutingRuleNode` | `src/nodes/apply_routing_rule_node.py` | `ANONYMOUS` | Maps the classification to a queue, a reference and a review mode |
| inner `output_format` | `OutputFormatNode` | `src/nodes/output_format_node.py` | `ANONYMOUS` | Assembles the routing decision record |

**Why the inner nodes are `ANONYMOUS`.** A nested graph is invoked with the outer caller's
invocation context unchanged, so a real external caller reaches the inner nodes as
`VERIFIED_EXTERNAL`. That clears `ANONYMOUS`. An inner node declaring `INTERNAL` would deny every
external caller once deployed, and a unit suite would not notice — which is why the boundary test
drives the whole backbone with a `VERIFIED_EXTERNAL` context rather than an internal one.

### The request bridge

The framework invokes a nested graph as `subgraph.invoke(user_input, session_id=…, ctx=…)`. Only
the submission string crosses. The caller's structured parameters — the document reference, the
filing details, the confidence floor, the metadata keywords — would therefore never reach the
classifier, and the agent would behave as though the caller had sent nothing but text.

`src/graph/context_bridge.py` carries them across, using the two sanctioned subclass hooks:

```
ClassifyRoutingGraphNode.extract_input(state)          [before subgraph.invoke]
    → set_caller_contract(<validated contract>)
DisclosureClassificationWorkflow._extra_initial_state() [inside subgraph.invoke]
    → seeds the contract into the inner state
```

Only the contract the request boundary already validated crosses; the raw request body does not.
A `ContextVar` keeps the hand-off correct per thread and per task, so concurrent invocations
inside one process cannot see each other's request.

The same hook seeds `intake_config`, the live runtime tuning forwarded by
`ClassifyRoutingGraphNode._parent_config()`. Node `execute()` methods take no config parameter, so
state seeding is the only route runtime configuration can reach a domain node.

### Data flow

```
input (document text or metadata block) + input_context (filing details)
  ↓ PreProcessNode — trust gate, disallowed-instruction screen, per-field bounds
validated_input, caller_contract
  ↓ ClassifyRoutingGraphNode.extract_input → the bridge
  ↓ InputValidateNode — document reference (the caller's, or a digest) + intake metadata
validated_document_ref, document_metadata
  ↓ ClassifyDocumentTypeNode — taxonomy scoring over text + metadata keywords
disclosure_type, classification_confidence
  ↓ ApplyRoutingRuleNode — routing table, confidence floor, review mode
routing_target, regulatory_reference, routing_overridden, review_mode
  ↓ OutputFormatNode — assembles the routing decision record
routing_decision, result
  ← merge_output into the outer state
  ↓ PostProcessNode — the output gate
formatted_output (the released decision) or a withheld-output notice
```

---

## Configuration

Two files with two jobs, and a reader pointed at the wrong one gets an empty mapping and degrades
to defaults without failing — so they are worth keeping straight:

| File | Job | Read by |
|---|---|---|
| `config/agent.yaml` | the static manifest: identity and compile-time requirements, all keys at root level | the agent registry, at discovery |
| `config/config.yaml` | the runtime parameters, passed to the graph constructor | `runtime_config()` in `src/graph/graph.py`, used by both the registry and the standalone entry point |

`config/config.yaml` declares `max_retry`, `timeout_s` and `intake.bulk_review_page_threshold`.
The threshold is forwarded into the inner workflow and read by `ApplyRoutingRuleNode`, so changing
it changes behaviour; a boundary test drives a request at the declared value and one below it to
prove that end to end rather than asserting the file's contents.

`requires.secrets` and `requires.extras` are both empty, and that is the code-derived answer: the
agent constructs no client and requires no secret. Declaring a secret that is not provisioned
makes the agent fail at compile time on a real deployment.

---

## State

`src/schemas/state.py` extends the framework's flat state `TypedDict`.

| Field | Type | Purpose |
|---|---|---|
| `caller_contract` | `str` (JSON) | The validated caller contract, written once at the request boundary |
| `intake_config` | `str` (JSON) | The forwarded runtime tuning, seeded into the inner graph |
| `document_ref` / `validated_document_ref` | `str` | The reference for the submission — the caller's own, or a digest of it |
| `document_metadata` | `str` (JSON) | Length and provenance of the intake; no excerpt of the submission |
| `disclosure_type` | `str` | A taxonomy label, or `unknown` |
| `classification_confidence` | `float` | In [0.0, 1.0] |
| `regulatory_reference` | `str` | From the routing table |
| `routing_target` | `str` | An approved review queue |
| `routing_overridden` | `bool` | True when the confidence floor diverted the filing to manual review |
| `review_mode` | `str` | `standard` or `bulk` |
| `routing_decision` | `str` (JSON) | The assembled decision record |

**The submitted document text is never written into a state field that leaves the graph.** The
request boundary derives a reference for it, and only the reference travels.

---

## The caller-request contract

One module validates everything a caller can send (`src/services/caller_contract.py`), so there is
exactly one answer to "what is accepted?", and nothing downstream re-parses raw request data.

| Field | Rule | Effect on the decision |
|---|---|---|
| `input` | 1–8000 characters, screened, personal-data shapes stripped | the classification signal |
| `channel` | `[a-z0-9_]{1,32}` | recorded in the decision |
| `document_ref` | `[a-z0-9_-]{1,64}` | replaces the derived reference |
| `filing_year` | finite integer in [1900, 2100] | recorded in the decision |
| `page_count` | finite integer in [0, 100000] | at or above the declared threshold → bulk review |
| `confidence_floor` | finite number in [0.0, 1.0] | below it → diverted to manual review |
| `metadata_terms` | at most 16 labels, each `[a-z0-9_]{1,32}` | scored against the same taxonomy as the text |

Rules that hold across all of them:

- **Numbers go through a finite and bounded parser.** `NaN` and the infinities survive `float()`
  and every comparison against them is False, so an unchecked confidence floor would silently
  route everything — or nothing — to manual review with no error in the log. Booleans are rejected
  too, because `True` is an integer in Python.
- **Strings that reach the decision are restricted to inert alphabets**, not escaped.
- **A failed check refuses the request**, naming the field and never repeating the value.
- **Absent data is not an error.** With no structured parameters the agent classifies on the
  document text alone.
- **Unknown structured keys are screened and then ignored**, not refused: a hosting platform puts
  its own material on that channel, and refusing unknown keys would break every hosted deployment.

### Screens

**Disallowed instructions.** Chat-template control tokens (`<|…|>`, `[INST]`, `<<SYS>>`) are
screened as a class: they forge a turn boundary and carry no meaning in a filing, so matching them
cannot block real work. Instruction-shaped phrases require a verb *and* its object, because
disclosure documents describe rules, instructions to shareholders and system behaviour in ordinary
business language — a screen that fires on "instructions for exercising voting rights" refuses a
genuine filing, which is the more damaging of the two failure directions.

Each string is screened four ways: as received, after the identifier strip, with invisible
controls removed, and with invisible controls replaced by a space. None of the passes subsumes
another. A rewrite that silently removed a token and forwarded the rest would turn a detectable
attack into undetectable prose, which is worse than not rewriting at all.

The structured channel is screened depth-first **including mapping keys**, because a JSON payload
can write any pattern into a key and `\u` escapes make a scan of the raw request text unreliable —
only a scan after parsing sees what the reader will see. Nesting is bounded.

**Personal data.** One pattern definition drives both directions: the inbound strip that rewrites
these shapes out of the submission, and the outbound refusal. Two lists would drift, and the drift
would always favour the leak.

The identifier guards on the numeric shape are load-bearing and they are there because of this
template's own render alphabet. Filing references are hyphenated digit groups —
`edinet-1234-5678-90` — and an unguarded "long id" shape reads one as a personal identifier, so
the output gate would refuse a perfectly ordinary filing. The guards make the shape match only a
digit run standing on its own in prose. **Known limitation:** a reference written as ten or more
bare digits with nothing around it is refused. That is deliberate, on the fail-closed side.

### The credential screen at the adapter

`src/api/server.py` screens the assembled `input_context` for credential shapes **before**
`invoke()`, and refuses with a 400 that names the field.

This is not extra strictness; it converts an opaque failure into an actionable one. The framework's
mandatory output gate scans every value of every node result for credential patterns, and the
backbone's first node copies the structured parameters verbatim into its own result — so a
credential-shaped string anywhere in them fails the *first* node of the graph, before any template
code runs, and what the caller receives is an error with no indication of which parameter caused
it. On a hosted conversation the same context is replayed every turn, so the session never
recovers on its own. The request cannot succeed either way.

The screen calls the same detector the framework's gate calls, on the same assembled object, so
what the adapter refuses and what the gate blocks are one set by construction. Scanning field by
field composes exactly to scanning the whole mapping, which is what lets the refusal name the
offending field without widening or narrowing the match. Field names are caller data too: a name
is repeated back only when it is short and inert, otherwise it is reported by position.

---

## Caller authentication in the standalone deployment

The `pre_process` slot requires `VERIFIED_EXTERNAL`, and nothing sets a trust level in a
standalone deployment — so without an entry-point boundary every request would arrive
`ANONYMOUS`, the trust gate would deny it, and the agent would return an error for every call.

`src/api/server.py` is that boundary: when `INVOKE_AUTH_TOKEN` is set on the server environment, a
caller no upstream middleware vouched for must present it as a Bearer token and then runs at
`VERIFIED_EXTERNAL`. Trust established by middleware is never demoted. The refusal body is generic
on purpose — it does not say whether the token was absent, malformed or wrong. The comparison is
constant-time over bytes, because a non-ASCII header would otherwise raise and return a 500 in
place of the 401.

This is a deployment-level caller credential, not an agent secret: no invocation context exists
before authentication, so the secrets provider does not apply.

---

## The released-output contract

> What leaves the agent is a routing decision and nothing else — a record with exactly the
> declared fields, each holding a value drawn from a closed set (an approved review queue, a
> taxonomy label, a regulatory reference from the routing table, a confidence in [0, 1], an inert
> document reference) — and it carries no submitted document text, no credential-shaped string and
> no personal-data shape.

`PostProcessNode` is the single place that contract is enforced, and it enforces it on the field
that is actually released.

| Check | Refuses |
|---|---|
| shape | a record whose field set is not exactly `DECISION_FIELDS` |
| document reference | anything outside `[a-z0-9_-]{1,64}` |
| disclosure type | anything outside the taxonomy |
| confidence | a non-finite value, or one outside [0, 1] |
| routing target | a queue not in the routing table |
| regulatory reference | a reference not in the routing table |
| review mode | anything other than `standard` or `bulk` |
| filing year / channel | outside their declared ranges and alphabets |
| content | a credential shape or a personal-data shape, anywhere in the record including nested values |

The shape check is what makes "no document text is released" enforceable rather than merely
intended: a record whose fields are all drawn from closed sets has nowhere to put free text. The
content scan behind it is the check that still applies when `DECISION_FIELDS` grows a field whose
own rule admits free text; on today's shape it is unreachable, and the tests exercise it directly
rather than through a contrived record that would misrepresent what it does.

The allowlists are **derived** from the routing table rather than copied beside it. The
hand-maintained copy this replaced carried a "keep in sync" comment, which is a drift waiting to
happen.

The credential scan calls the framework's own detector rather than a local pattern list. A local
list narrower than the framework's would let through a value the framework then catches inside the
same node — which makes the framework raise, discards this node's clearing, and turns a contained
refusal back into an uncontained one.

### Containment, and why there is only one gate

On a violation the node returns an error status **and clears every output-bearing field**. That
clearing is the containment, not the error status: the framework's response envelope falls back to
`state["result"]` whatever the status, so a gate that merely raised — or set an error status
without clearing — would still ship the un-gated record inside the error envelope, together with a
traceback.

There is deliberately no second copy of this check earlier in the pipeline, and the outer graph
deliberately does not override `get_output()` to surface a structured copy of the same record. A
duplicate would contain most violations before they ever reached the boundary, which would leave
the boundary gate green whether or not it worked. More checks would buy less assurance.

The two boundary tests that prove containment inject a defect and drive the whole endpoint. They
were verified against three variants of this node: with the clearing removed they fail, and with
the pre-migration boundary restored — a helper that raised on a non-allowlisted queue, with no
personal-data check and no clearing — they fail as well, releasing the routing decision inside an
error envelope. That is the failure the current design exists to prevent.

---

## Import isolation

- No platform-SDK imports; every framework import uses the `framework.*` and `shared.*` prefixes.
- No imports from other agents.

## Design decisions

| Decision | Chosen | Rationale |
|---|---|---|
| Base class | `AgentBaseGraph` | the nested Cat-2 pattern needs the outer backbone |
| Inner graph | `BaseGraph` | a custom linear topology; no backbone needed inside |
| Composition | `GraphNode` in the `main` slot | the canonical nested pattern |
| Inner topology | linear, no conditional edges | a conditional path callable is read as the input schema of the step it routes, and fields outside that schema are projected away — a routing flag can silently vanish while the unit suite stays green. There is no such callable here, and a test pins that |
| Error strategy | `propagate` | a classification failure should surface, not be absorbed into a plausible-looking answer |
| Classifier | keyword-weighted table | swapping it for a model call changes nothing outside `classify_document_type_node.py` |
| Output gate location | `post_process` only | the last point before the response envelope, and the only point where clearing a field actually withholds it |
| Monetary precision grid | not applicable | the released record carries no monetary aggregate — the only number in it is a confidence in [0, 1] and a filing year. The invariant this template enforces instead is the released-output contract above |
