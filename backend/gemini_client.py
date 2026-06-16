import json
import time

from google import genai
from google.genai import types

import config

# Transient, server-side errors worth retrying (Gemini occasionally returns 503
# UNAVAILABLE / 500 INTERNAL / deadline exceeded under load). Non-retryable
# errors like a 400 bad request or a 429 monthly-spend cap are NOT in this list.
_TRANSIENT_SIGNALS = (
    "503", "unavailable", "500", "internal error", "internal server",
    "deadline", "timed out", "timeout", "temporarily",
)
_MAX_RETRIES = 4


class GeminiClient:
    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or config.GEMINI_API_KEY
        self.model = model or config.GEMINI_MODEL
        if not self.api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is not set. Add it to your .env file."
            )
        self._client = genai.Client(api_key=self.api_key)

    def generate_json(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float | None = None,
    ) -> dict | list:
        """Call Gemini and parse the response as JSON.

        `temperature` lets grounding-sensitive steps run cooler (lower = more
        faithful to the source, fewer invented details / less hallucination).
        """
        config_kwargs = {
            "response_mime_type": "application/json",
            "max_output_tokens": config.GEMINI_MAX_OUTPUT_TOKENS,
        }
        if system:
            config_kwargs["system_instruction"] = system
        if temperature is not None:
            config_kwargs["temperature"] = temperature

        cfg = types.GenerateContentConfig(**config_kwargs)
        response = self._generate_with_retry(prompt, cfg)
        _check_truncated(response)
        text = (response.text or "").strip()
        return _parse_json(text)

    def _generate_with_retry(self, prompt, cfg):
        """Call Gemini, retrying transient server-side errors with backoff.

        Gemini intermittently returns 503 UNAVAILABLE / 500 INTERNAL under load;
        a single hiccup should not sink the whole pipeline. We retry those a few
        times with exponential backoff and re-raise anything non-transient (bad
        request, auth, spend cap) immediately.
        """
        last_exc = None
        for attempt in range(_MAX_RETRIES):
            try:
                return self._client.models.generate_content(
                    model=self.model,
                    contents=prompt,
                    config=cfg,
                )
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if not _is_transient(str(exc)) or attempt == _MAX_RETRIES - 1:
                    raise
                wait = 2 ** attempt  # 1s, 2s, 4s, …
                time.sleep(wait)
        raise last_exc  # pragma: no cover — loop always returns or raises above


def _is_transient(message: str) -> bool:
    """True for retryable server-side hiccups (503, 500, deadline, timeout)."""
    low = message.lower()
    return any(s in low for s in _TRANSIENT_SIGNALS)


def _check_truncated(response) -> None:
    """Raise a clear error if Gemini cut the response off at the token ceiling.

    A thinking model can burn its whole output budget on reasoning and return a
    half-finished JSON body with finish_reason == MAX_TOKENS. That used to surface
    downstream as an opaque "Could not parse JSON" error; catch it here so the
    real cause (and the fix — raise GEMINI_MAX_OUTPUT_TOKENS) is obvious.
    """
    try:
        finish_reason = response.candidates[0].finish_reason
    except (AttributeError, IndexError, TypeError):
        return
    if finish_reason is None:
        return
    name = getattr(finish_reason, "name", str(finish_reason)).upper()
    if "MAX_TOKEN" in name:
        raise ValueError(
            "Gemini response was truncated at the output-token limit "
            f"({config.GEMINI_MAX_OUTPUT_TOKENS} tokens). The model likely spent "
            "its budget on internal reasoning before finishing the JSON. Raise "
            "GEMINI_MAX_OUTPUT_TOKENS (env or config.py) and retry."
        )


def _parse_json(text: str):
    """Best-effort JSON parsing that tolerates code fences and stray text."""
    if not text:
        raise ValueError("Empty response from Gemini")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Strip markdown code fences if present.
    if "```" in text:
        inner = text.split("```", 2)
        if len(inner) >= 2:
            candidate = inner[1]
            candidate = candidate.removeprefix("json").strip()
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass

    # Fall back to slicing from the first bracket to the last.
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start = text.find(open_c)
        end = text.rfind(close_c)
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue

    raise ValueError(f"Could not parse JSON from Gemini response: {text[:300]}")
