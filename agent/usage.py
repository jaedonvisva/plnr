"""Token usage tracking, shared across both LLM backends.

Both ollama.Client and GeminiClient return per-response token counts under
the SAME field names -- `prompt_eval_count` / `eval_count` -- on the object
`run_turn()` gets back from `.chat()` (Ollama provides these natively;
GeminiClient populates them from Gemini's own `usage_metadata`, see
agent/gemini_client.py). Usage.add() reads exactly those two fields, so
tracking works identically regardless of which backend is selected.

Deliberately reports raw token counts only, not a dollar estimate: pricing
differs by model and backend (free/local for Ollama; billed and
model-specific for Gemini) and changes over time -- hardcoding it here
would silently go stale. See README for current Gemini pricing links.
"""

from dataclasses import dataclass


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def add(self, prompt_tokens: int | None, completion_tokens: int | None) -> None:
        self.prompt_tokens += prompt_tokens or 0
        self.completion_tokens += completion_tokens or 0

    def add_usage(self, other: "Usage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
