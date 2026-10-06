"""AgentCore Platform v1.0"""

# Caller-request contract for the disclosure routing agent.
#
# One place validates everything a caller can send, so there is exactly one
# answer to "what is accepted?" — the pre_process node calls into here and
# nothing downstream re-parses raw request data.
#
# Two request channels reach this module:
#   * the submission itself — the disclosure document text or its metadata
#     block, sent as the request's free-text input;
#   * the structured invocation parameters, which carry the filing details a
#     document-management system already knows: a channel label, the caller's
#     own document reference, the filing year, the page count, a confidence
#     floor below which the submission must go to manual review, and the
#     metadata keywords the source system has already tagged it with.
#
# Rules that hold for every field:
#   * numbers are parsed by a finite + bounded parser. NaN and the infinities
#     survive float() and every comparison against them is False, so an
#     unchecked confidence floor silently routes everything — or nothing — to
#     manual review while no error is ever logged;
#   * every string that reaches the routing decision is restricted to an inert
#     alphabet, so caller text cannot forge decision content;
#   * a value that fails any check REFUSES the request, naming the field but
#     never repeating the value;
#   * absent data is not an error — the agent classifies on the submission text
#     alone, which is the baseline behaviour.

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

# ── Bounds ────────────────────────────────────────────────────────────────────
MAX_SUBMISSION_CHARS = 8000
MAX_METADATA_TERMS = 16
MAX_CONTEXT_DEPTH = 6

FILING_YEAR_MIN = 1900
FILING_YEAR_MAX = 2100
PAGE_COUNT_MIN = 0
PAGE_COUNT_MAX = 100_000
CONFIDENCE_FLOOR_MIN = 0.0
CONFIDENCE_FLOOR_MAX = 1.0

# Page count at or above which a submission is flagged for bulk review rather
# than the standard single-reviewer queue.
BULK_REVIEW_PAGE_THRESHOLD = 500

# ── Inert alphabets ───────────────────────────────────────────────────────────
# Labels (channel, metadata terms) and the document reference are the only
# caller strings that reach the routing decision, so they are restricted rather
# than escaped. The reference alphabet includes "-" because filing systems write
# references that way (edinet-1234-5678-90).
_LABEL_RE = re.compile(r"^[a-z0-9_]{1,32}$")
_DOCUMENT_REF_RE = re.compile(r"^[a-z0-9_-]{1,64}$")

# Field names are caller-controlled too. One is repeated back in a refusal only
# when it is short, inert, and carries no disallowed pattern of its own.
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")

# Zero-width and bidi controls: invisible in rendered text, so they can hide a
# directive from a human reviewer while a model still reads it.
_INVISIBLE_RE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")

# ── Personal-data shapes ──────────────────────────────────────────────────────
# ONE definition, used by both directions of the personal-data guarantee: the
# inbound strip that rewrites these shapes out of the submission before it is
# scored, and the outbound gate that refuses to release anything still matching
# them. Two lists would drift, and the drift would always favour the leak.
#
# The identifier guards on the numeric shape are load-bearing, and they are
# there because of this template's own render alphabet. Document references are
# hyphenated digit groups — edinet-1234-5678-90 — and an unguarded "long id"
# shape reads one as a personal identifier, so the output gate would refuse a
# perfectly ordinary filing. The guards make the shape match only a digit run
# standing on its own in prose. The residual case is a reference written as ten
# or more bare digits with nothing around it: that is refused, deliberately, on
# the fail-closed side.
_ID_GUARD_LEFT = r"(?<![A-Za-z0-9_-])"
_ID_GUARD_RIGHT = r"(?![A-Za-z0-9_-])"

PERSONAL_DATA_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    ("long_id_number", re.compile(_ID_GUARD_LEFT + r"\d{4}[-\s]?\d{4}[-\s]?\d{2,11}" + _ID_GUARD_RIGHT)),
    (
        "international_phone_number",
        re.compile(_ID_GUARD_LEFT + r"\+\d{1,3}[-\s]\d{1,4}[-\s]\d{2,4}[-\s]\d{3,4}" + _ID_GUARD_RIGHT),
    ),
    ("phone_number", re.compile(_ID_GUARD_LEFT + r"0\d{1,4}[-\s]\d{2,4}[-\s]\d{4}" + _ID_GUARD_RIGHT)),
)

REDACTION_STUB = "[REDACTED]"


def strip_direct_identifiers(text: str) -> str:
    """Replace direct-identifier shapes in free text with a fixed stub.

    Applied to the submission before it is stored or scored. Investor-relations
    material routinely carries a press-desk address or a filing agent's phone
    number, and none of it is needed to decide which review queue the document
    belongs in.
    """
    for _name, pattern in PERSONAL_DATA_PATTERNS:
        text = pattern.sub(REDACTION_STUB, text)
    return text


def find_personal_data(text: str) -> Optional[str]:
    """Name the first personal-data shape present in a string, or None."""
    for name, pattern in PERSONAL_DATA_PATTERNS:
        if pattern.search(text):
            return name
    return None


# ── Disallowed-instruction screen ─────────────────────────────────────────────
# Chat-template control tokens are screened as a class. They are how a payload
# forges a turn boundary, and they carry no meaning in a disclosure filing, so
# matching them cannot block real work.
_CONTROL_TOKEN_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("chat_template_token", re.compile(r"<\|[^<>|]{0,64}\|>")),
    ("instruction_token", re.compile(r"\[/?INST\]", re.IGNORECASE)),
    ("system_token", re.compile(r"<</?SYS>>", re.IGNORECASE)),
    ("script_tag", re.compile(r"<\s*/?\s*script\b", re.IGNORECASE)),
)

# Instruction-shaped phrases. Every pattern requires a verb AND its object, so
# the surrounding prose has to actually be an instruction. That matters here:
# disclosure documents describe rules, instructions to shareholders and system
# behaviour in ordinary business language, and a screen that fires on
# "instructions for exercising voting rights" refuses a genuine filing — the
# more damaging of the two failure directions.
_DIRECTIVE_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    (
        "override_directive",
        re.compile(
            r"\b(?:ignore|disregard|forget|override|bypass)\s+"
            r"(?:all\s+|any\s+|the\s+|your\s+|these\s+|those\s+)*"
            # One or more qualifiers: "the above safety rules" stacks two of
            # them, and a screen that allowed only one read that as ordinary
            # prose.
            r"(?:(?:previous|prior|above|earlier|preceding|system|initial|safety)\s+)+"
            r"(?:instruction|rule|prompt|direction|guardrail|guideline)s?",
            re.IGNORECASE,
        ),
    ),
    (
        "role_reassignment",
        re.compile(
            r"\b(?:you\s+are\s+now|pretend\s+to\s+be|act\s+as|behave\s+as|roleplay\s+as)\s+"
            r"(?:a|an|the)\s+"
            r"(?:system|assistant|language\s+model|ai\s+model|unrestricted|jailbroken|"
            r"admin(?:istrator)?|developer\s+mode)",
            re.IGNORECASE,
        ),
    ),
    (
        "prompt_disclosure",
        re.compile(
            r"\b(?:reveal|print|repeat|show|output|display|disclose)\s+(?:me\s+)?"
            r"(?:your|the)\s+(?:system|initial|original|hidden|full|exact)\s+"
            r"(?:prompt|instruction|rule)s?",
            re.IGNORECASE,
        ),
    ),
    (
        "code_execution",
        re.compile(r"\b(?:exec|eval|system|popen)\s*\(\s*['\"]", re.IGNORECASE),
    ),
)

_ALL_SCREEN_PATTERNS = _CONTROL_TOKEN_PATTERNS + _DIRECTIVE_PATTERNS


class CallerDataError(ValueError):
    """A caller field failed its contract. Carries a field reference, never a value."""


def screen_text(text: str) -> Optional[str]:
    """Name the first disallowed pattern in one string, or None.

    Screens the string as received, after the identifier strip, and twice more
    with invisible controls resolved — once removed and once replaced by a
    space. None of the passes subsumes another: control tokens have to be seen
    before any rewrite could consume them; a directive split by an
    identifier-shaped run only reads as a directive once that run is collapsed;
    and a zero-width character is a word separator in one attack and a
    letter-level splitter in the next, so both readings are screened. A rewrite
    that silently removed a token and forwarded the rest would turn a detectable
    attack into undetectable prose, which is worse than not rewriting at all.
    """
    candidates = (
        text,
        strip_direct_identifiers(text),
        _INVISIBLE_RE.sub("", text),
        _INVISIBLE_RE.sub(" ", text),
    )
    for candidate in candidates:
        for name, pattern in _ALL_SCREEN_PATTERNS:
            if pattern.search(candidate):
                return name
    return None


def _reference(parent: str, name: object, index: int) -> str:
    """Render a caller-supplied field name safe to repeat in a refusal."""
    if isinstance(name, str) and _SAFE_NAME_RE.match(name) and screen_text(name) is None:
        return f"{parent}.{name}"
    return f"{parent}[field #{index}]"


def screen_payload(value: object, reference: str = "input_context", depth: int = 0) -> Optional[Tuple[str, str]]:
    """Depth-first screen of a parsed payload; returns (pattern, field) or None.

    Mapping KEYS are screened as well as values: a payload delivered as JSON can
    write any pattern into a key, and \\u escapes make a scan of the raw request
    text unreliable — only a scan after parsing sees what the reader will see.
    Nesting is bounded so a pathologically nested payload cannot exhaust the
    stack before the per-field checks run.
    """
    if depth > MAX_CONTEXT_DEPTH:
        return ("nesting_depth", reference)
    if isinstance(value, str):
        hit = screen_text(value)
        return (hit, reference) if hit else None
    if isinstance(value, Mapping):
        for index, (key, item) in enumerate(value.items(), start=1):
            child = _reference(reference, key, index)
            if isinstance(key, str):
                hit = screen_text(key)
                if hit:
                    return (hit, child)
            found = screen_payload(item, child, depth + 1)
            if found:
                return found
        return None
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value, start=1):
            found = screen_payload(item, f"{reference}[{index}]", depth + 1)
            if found:
                return found
        return None
    return None


# ── Field parsers (every failure refuses the request) ─────────────────────────
def parse_number(
    value: object,
    *,
    field: str,
    minimum: float,
    maximum: float,
    integer: bool = False,
) -> float:
    """Parse a caller number, or refuse.

    Rejects booleans (True is an int in Python), non-numeric text, NaN and the
    infinities, and anything outside the stated range. A non-finite value that
    reaches a comparison never raises — it simply makes every comparison False.
    A NaN confidence floor would therefore send every submission straight past
    the manual-review check, silently, on the exact decision this agent exists
    to make.
    """
    if isinstance(value, bool) or value is None:
        raise CallerDataError(f"{field} must be a number")
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except (TypeError, ValueError):
            raise CallerDataError(f"{field} must be a number") from None
    elif isinstance(value, (int, float)):
        number = float(value)
    else:
        raise CallerDataError(f"{field} must be a number")
    if not math.isfinite(number):
        raise CallerDataError(f"{field} must be a finite number")
    if integer and number != int(number):
        raise CallerDataError(f"{field} must be a whole number")
    if not minimum <= number <= maximum:
        raise CallerDataError(f"{field} must be between {minimum} and {maximum}")
    return float(int(number)) if integer else number


def parse_label(value: object, *, field: str) -> str:
    """Parse an inert label (channel, metadata term), or refuse."""
    if not isinstance(value, str):
        raise CallerDataError(f"{field} must be text")
    label = value.strip().lower()
    if not _LABEL_RE.match(label):
        raise CallerDataError(f"{field} must be 1-32 characters of lowercase letters, digits or underscores")
    return label


def parse_document_ref(value: object, *, field: str) -> str:
    """Parse the caller's own document reference, or refuse.

    The reference is the one caller string that is echoed back in the routing
    decision, so it is restricted to an inert alphabet rather than escaped.
    """
    if not isinstance(value, str):
        raise CallerDataError(f"{field} must be text")
    reference = value.strip().lower()
    if not _DOCUMENT_REF_RE.match(reference):
        raise CallerDataError(f"{field} must be 1-64 characters of lowercase letters, digits, underscores or hyphens")
    return reference


def parse_metadata_terms(value: object, *, field: str) -> List[str]:
    """Parse the source system's metadata keywords, or refuse."""
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise CallerDataError(f"{field} must be a list of keywords")
    if len(value) > MAX_METADATA_TERMS:
        raise CallerDataError(f"{field} accepts at most {MAX_METADATA_TERMS} keywords")
    return [parse_label(item, field=f"{field}[{index}]") for index, item in enumerate(value, start=1)]


# ── The whole contract ────────────────────────────────────────────────────────
# Keys the caller may set. Anything else on the structured channel is screened
# and then ignored rather than refused: a hosting platform puts its own material
# there (conversation history, routing metadata), and refusing unknown keys
# would break every hosted deployment.
CONTEXT_KEYS: Sequence[str] = (
    "channel",
    "document_ref",
    "filing_year",
    "page_count",
    "confidence_floor",
    "metadata_terms",
)


def build_caller_contract(user_input: object, input_context: object) -> Dict[str, Any]:
    """Screen, validate and normalise everything the caller sent.

    Returns the validated contract. Raises CallerDataError naming the offending
    field — and only the field — when anything fails.
    """
    context: Mapping[str, Any] = input_context if isinstance(input_context, Mapping) else {}

    found = screen_payload(context, "input_context")
    if found:
        raise CallerDataError(f"{found[1]} contains a disallowed instruction pattern")

    text = user_input.strip() if isinstance(user_input, str) else ""
    if not text:
        raise CallerDataError("input must not be empty")
    if len(text) > MAX_SUBMISSION_CHARS:
        raise CallerDataError(f"input must be at most {MAX_SUBMISSION_CHARS} characters")

    hit = screen_text(text)
    if hit:
        raise CallerDataError("input contains a disallowed instruction pattern")

    contract: Dict[str, Any] = {
        "submission": strip_direct_identifiers(_INVISIBLE_RE.sub("", text)),
        "channel": "",
        "document_ref": "",
        "filing_year": None,
        "page_count": None,
        "confidence_floor": None,
        "metadata_terms": [],
    }

    channel = context.get("channel")
    if channel is not None:
        contract["channel"] = parse_label(channel, field="input_context.channel")

    document_ref = context.get("document_ref")
    if document_ref is not None:
        contract["document_ref"] = parse_document_ref(document_ref, field="input_context.document_ref")

    filing_year = context.get("filing_year")
    if filing_year is not None:
        contract["filing_year"] = int(
            parse_number(
                filing_year,
                field="input_context.filing_year",
                minimum=FILING_YEAR_MIN,
                maximum=FILING_YEAR_MAX,
                integer=True,
            )
        )

    page_count = context.get("page_count")
    if page_count is not None:
        contract["page_count"] = int(
            parse_number(
                page_count,
                field="input_context.page_count",
                minimum=PAGE_COUNT_MIN,
                maximum=PAGE_COUNT_MAX,
                integer=True,
            )
        )

    confidence_floor = context.get("confidence_floor")
    if confidence_floor is not None:
        contract["confidence_floor"] = parse_number(
            confidence_floor,
            field="input_context.confidence_floor",
            minimum=CONFIDENCE_FLOOR_MIN,
            maximum=CONFIDENCE_FLOOR_MAX,
        )

    contract["metadata_terms"] = parse_metadata_terms(
        context.get("metadata_terms"), field="input_context.metadata_terms"
    )
    return contract
