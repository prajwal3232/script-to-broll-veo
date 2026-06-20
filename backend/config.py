import os

from dotenv import load_dotenv

# Load .env from the project root, regardless of the current working directory.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_ROOT, ".env"))

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
# Treat the .env template placeholder as "not set".
if GEMINI_API_KEY == "your_gemini_api_key_here":
    GEMINI_API_KEY = ""
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-pro-preview")
# Upper bound on Gemini's response tokens. gemini-3.1-pro-preview is a *thinking*
# model: its internal reasoning is billed against the same output budget as the
# visible answer, so a low cap truncates large structured outputs (e.g. the
# step-3 style bible) mid-JSON. Keep this generous so the model can think AND
# still emit the full JSON.
GEMINI_MAX_OUTPUT_TOKENS = int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "32768"))

# Veo 3.1 text-to-video model (the cost-effective "lite" tier by default).
VEO_MODEL = os.getenv("VEO_MODEL", "veo-3.1-lite-generate-preview")
VEO_ASPECT_RATIO = os.getenv("VEO_ASPECT_RATIO", "9:16")
GENERATED_DIR = os.path.join(_ROOT, "backend", "generated")

# Selectable Veo models, aspect ratios, and output counts surfaced in the UI.
# Model IDs verified against https://ai.google.dev/gemini-api/docs/video
VEO_MODELS = [
    {"id": "veo-3.1-lite-generate-preview", "label": "Veo 3.1 · Lite (Lower priority)"},
    {"id": "veo-3.1-fast-generate-preview", "label": "Veo 3.1 · Fast"},
    {"id": "veo-3.1-generate-preview", "label": "Veo 3.1 · Quality"},
    {"id": "veo-3.0-fast-generate-001", "label": "Veo 3.0 · Fast"},
    {"id": "veo-3.0-generate-001", "label": "Veo 3.0 · Quality"},
]
VEO_ALLOWED_MODELS = {m["id"] for m in VEO_MODELS}
VEO_ASPECT_RATIOS = ["9:16", "16:9"]
VEO_MAX_OUTPUTS = 4
# The `seed` parameter is ONLY accepted on Vertex AI / Gemini Enterprise.
# The Gemini Developer API (plain API key) rejects it. Off by default; flip to
# "1"/"true" only when running against Vertex. Text consistency does not depend
# on it — the verbatim Look Line + identity/place blocks carry the look.
VEO_USE_SEED = os.getenv("VEO_USE_SEED", "").strip().lower() in {"1", "true", "yes"}
# Video generation throughput: dispatch shots in waves of VEO_BATCH_SIZE at once,
# then wait VEO_BATCH_DELAY seconds before starting the next wave.
VEO_BATCH_SIZE = int(os.getenv("VEO_BATCH_SIZE", "10"))
VEO_BATCH_DELAY = int(os.getenv("VEO_BATCH_DELAY", "10"))
# Hard ceiling on how many clips a single run may generate, to guard against an
# accidental huge document turning into a massive, expensive Veo job.
VEO_MAX_VIDEOS_PER_RUN = int(os.getenv("VEO_MAX_VIDEOS_PER_RUN", "50"))

# Upload guardrails. A ~60-page document would explode token cost and produce
# hundreds of shots, so reject anything over this size or that isn't plain text.
MAX_SCRIPT_CHARS = int(os.getenv("MAX_SCRIPT_CHARS", "20000"))

# Character reference photos (for ingredient-to-video). Cap the count and per-file
# size so a careless upload can't balloon memory or the Veo request payload.
MAX_CHAR_IMAGES = int(os.getenv("MAX_CHAR_IMAGES", "6"))
MAX_CHAR_IMAGE_BYTES = int(os.getenv("MAX_CHAR_IMAGE_BYTES", str(8 * 1024 * 1024)))
CHAR_IMAGE_MIMES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
}

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "5000"))

AZURE_STORAGE_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "")
AZURE_STORAGE_CONTAINER = os.getenv("AZURE_STORAGE_CONTAINER", "generated-videos")
# Container-level SAS URL (preferred). Example:
#   https://<account>.blob.core.windows.net/<container>?sp=racwl&st=...&sig=...
AZURE_SAS_URL = os.getenv("AZURE_BLOB_SAS_URL", "").strip()
# Folder prefix inside the container, e.g. "hogel-brolls/testing".
AZURE_BLOB_PREFIX = os.getenv("AZURE_BLOB_PREFIX", "").strip().strip("/")
