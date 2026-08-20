"""Shared JSON-serialization helpers for the LEARN tool layer.

Split out of agent/tools.py so agent/aggregation.py can reuse the exact same
HTML-stripping/truncation logic (e.g. for search results) without a circular
import between the two modules.

Security note: every dataclass serialized here originates from LEARN --
i.e. from instructor/administrator-controlled content (course names,
announcement titles/bodies, content descriptions, ...), which this project
treats as untrusted input, not as instructions. serialize() marks every
object it produces with UNTRUSTED_MARKER_KEY so that property is visible
in the data itself, not just asserted in the system prompt (which is
guidance, not a security boundary -- see agent/system_prompt.py's
"UNTRUSTED CONTENT" section). Nothing here filters or blocks that content;
the model can read and quote it freely -- the marker exists so injected
text in it can never be structurally confused with a trusted instruction
or tool-provided fact.
"""

import dataclasses
import html
import re
from datetime import datetime
from typing import Any

RICH_TEXT_FIELDS = {"body", "description", "comments"}
TRUNCATE_LENGTH = 500

EXCLUDED_FIELDS = {"raw", "grade_item_id"}
"""Fields dropped from what the LLM sees, kept on the dataclass for internal
use. `raw` is D2L's full unfiltered JSON (see the project's core privacy
guarantee). `grade_item_id` is an internal D2L correlation id LearnClient
uses to join Assignment<->Grade records -- not human-meaningful, never used
as an argument to any tool, and not needed to answer any question the model
could be asked. Classified "unnecessary" (not sensitive) in the field
audit; removed to keep tool results minimal, not because it's a secret."""

UNTRUSTED_MARKER_KEY = "_content_source"
UNTRUSTED_MARKER_VALUE = "learn_data_untrusted"

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def strip_html(value: str) -> str:
    """HTML -> plain text: drop tags, unescape entities, collapse whitespace."""
    text = _TAG_RE.sub(" ", value)
    text = html.unescape(text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def truncate(value: str, max_len: int = TRUNCATE_LENGTH) -> str:
    plain = strip_html(value)
    if len(plain) <= max_len:
        return plain
    return plain[:max_len].rstrip() + "...[truncated -- use get_announcement_detail for the full text]"


def serialize(value: Any, truncate_text: bool = True) -> Any:
    """Convert a normalized model (or list of them) into plain JSON-safe
    data: dataclasses -> dict (excluding EXCLUDED_FIELDS), datetimes -> ISO
    8601 strings, rich-text fields stripped of HTML and truncated (unless
    truncate_text=False), everything else passed through."""
    if isinstance(value, datetime):
        return value.isoformat()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        result = {UNTRUSTED_MARKER_KEY: UNTRUSTED_MARKER_VALUE}
        for f in dataclasses.fields(value):
            if f.name in EXCLUDED_FIELDS:
                continue
            field_value = getattr(value, f.name)
            if f.name in RICH_TEXT_FIELDS and isinstance(field_value, str):
                result[f.name] = truncate(field_value) if truncate_text else strip_html(field_value)
            else:
                result[f.name] = serialize(field_value, truncate_text=truncate_text)
        return result
    if isinstance(value, list):
        return [serialize(v, truncate_text=truncate_text) for v in value]
    return value
