"""Step 6 — generate video from a Veo prompt using the Gemini API.

Veo 3.1 (fast/lite) text-to-video. Video generation is a long-running
operation: we start it, poll until done, then download the MP4 to disk.
Uses the same GEMINI_API_KEY as the LLM steps (Veo is a paid feature).
"""

import os
import time

from google import genai
from google.genai import types

import config


def generate_video(
    prompt: str,
    out_path: str,
    *,
    aspect_ratio: str | None = None,
    model: str | None = None,
    number_of_videos: int = 1,
    negative_prompt: str | None = None,
    seed: int | None = None,
    duration_seconds: int | None = None,
    on_status=None,
    poll_interval: int = 10,
    timeout: int = 600,
) -> list[str]:
    """Generate video(s) for `prompt` and save them to disk. Returns the saved paths.

    With `number_of_videos > 1` the first clip is saved at `out_path` and the
    rest get a `_2`, `_3`, … suffix before the extension.
    `on_status(message)` is an optional callback for progress text.
    """
    if not config.GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set. Add it to your .env file.")

    model = model or config.VEO_MODEL

    def status(msg: str) -> None:
        if on_status:
            on_status(msg)

    client = genai.Client(api_key=config.GEMINI_API_KEY)

    cfg_kwargs = {
        "aspect_ratio": aspect_ratio or config.VEO_ASPECT_RATIO,
        "number_of_videos": max(1, number_of_videos),
    }
    if negative_prompt:
        cfg_kwargs["negative_prompt"] = negative_prompt
    # Per-shot clip length (4/6/8s on Veo 3.1). Older models (e.g. Veo 3.0) only
    # do 8s and will reject this — _submit_with_fallback drops it transparently.
    if duration_seconds:
        cfg_kwargs["duration_seconds"] = int(duration_seconds)
    # `seed` is only honored on Vertex AI / Gemini Enterprise. The Developer API
    # (plain API key) rejects it outright, so gate it behind config.VEO_USE_SEED.
    if seed is not None and config.VEO_USE_SEED:
        cfg_kwargs["seed"] = seed

    status(f"Submitting to {model}…")
    operation = _submit_with_fallback(client, model, prompt, cfg_kwargs, status)

    waited = 0
    while not operation.done:
        if waited >= timeout:
            raise TimeoutError(f"Veo generation timed out after {timeout}s")
        time.sleep(poll_interval)
        waited += poll_interval
        status(f"Rendering… ({waited}s)")
        operation = client.operations.get(operation)

    if getattr(operation, "error", None):
        raise RuntimeError(f"Veo error: {operation.error}")

    videos = operation.response.generated_videos
    if not videos:
        raise RuntimeError("Veo returned no video (possibly blocked by safety filters).")

    base, ext = os.path.splitext(out_path)
    paths = []
    for idx, gv in enumerate(videos):
        status(f"Downloading video {idx + 1}/{len(videos)}…")
        video = gv.video
        client.files.download(file=video)
        path = out_path if idx == 0 else f"{base}_{idx + 1}{ext}"
        video.save(path)
        paths.append(path)
    return paths


# Optional config fields that some Veo models / API tiers reject. Maps the name
# Veo uses in its error text (camelCase or plain word) to the cfg_kwargs key.
_OPTIONAL_FIELDS = {
    "negativeprompt": "negative_prompt",
    "negative_prompt": "negative_prompt",
    "seed": "seed",
    "durationseconds": "duration_seconds",
    "duration_seconds": "duration_seconds",
    "aspectratio": "aspect_ratio",
    "aspect_ratio": "aspect_ratio",
    "numberofvideos": "number_of_videos",
}
# Required fields we must never strip even if they appear in an error message.
_PROTECTED_FIELDS = {"aspect_ratio", "number_of_videos"}


def _submit_with_fallback(client, model, prompt, cfg_kwargs, status):
    """Submit to Veo, transparently dropping any optional field the model rejects.

    The Gemini Developer API rejects `seed` and (on some models) `negativePrompt`
    with a 400 INVALID_ARGUMENT naming the field. Rather than hard-code which
    model supports what, we retry without the offending field until it submits.
    """
    field_attempts = 0
    transient_attempts = 0
    while True:
        try:
            return client.models.generate_videos(
                model=model,
                prompt=prompt,
                config=types.GenerateVideosConfig(**cfg_kwargs),
            )
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            dropped = _drop_unsupported_field(cfg_kwargs, msg)
            if dropped and field_attempts < len(_OPTIONAL_FIELDS):
                field_attempts += 1
                status(f"'{dropped}' not supported by {model}; retrying without it…")
                continue
            if _is_transient(msg) and transient_attempts < 3:
                transient_attempts += 1
                wait = 5 * transient_attempts
                status(f"Veo temporarily unavailable; retrying in {wait}s…")
                time.sleep(wait)
                continue
            raise


def _is_transient(message: str) -> bool:
    """True for retryable server-side hiccups (503, rate limits, timeouts)."""
    low = message.lower()
    return any(
        s in low
        for s in ("503", "unavailable", "resource_exhausted", "rate limit",
                  "rate_limit", "429", "deadline", "internal error", "500")
    )


# Substrings that mark a failure as a content-safety block (as opposed to a
# transient outage or a bad-argument error). These are the cases a prompt
# rewrite can plausibly fix; everything else is left alone.
_SAFETY_SIGNALS = (
    "safety", "blocked by safety", "possibly blocked", "responsible ai",
    "prohibited", "content policy", "policy violation", "violate",
    "sensitive", "sexual", "explicit", "filtered for", "rai_", "media_filtered",
)


def is_safety_block(message: str) -> bool:
    """True if a Veo failure looks like a content-safety rejection.

    Used by the orchestrator to decide which failed shots are worth handing to
    the prompt-sanitizer for a rewrite-and-retry pass.
    """
    low = (message or "").lower()
    return any(s in low for s in _SAFETY_SIGNALS)


def _drop_unsupported_field(cfg_kwargs: dict, message: str) -> str | None:
    """If `message` complains about an optional field that's set, remove it.

    Returns the cfg key removed, or None if nothing matched.
    """
    low = message.lower()
    signals = ("invalid_argument", "not supported", "isn't supported",
               "only supported", "unsupported", "is not supported")
    if not any(s in low for s in signals):
        return None
    for token, key in _OPTIONAL_FIELDS.items():
        if token in low and key in cfg_kwargs and key not in _PROTECTED_FIELDS:
            cfg_kwargs.pop(key, None)
            return key
    return None
