"""Five-step script -> Veo prompt pipeline.

Each step is a standalone, individually testable function:

  step1_extract(script)                      -> segments[]      (voiceover + visual)
  step2_concept(script, segments)            -> concept{}       (summary + per-visual intent)
  step3_style(script, concept)               -> style{}         (style prefix + characters)
  step4_detailed(concept, style, segments, *, script)
                                             -> shots[]         (split into single-shot beats)
  step5_veo(style, segments)                 -> updates[]       (veo_prompt per id)

`run_pipeline` chains them with progress callbacks (used by the web app).
`run_step` runs a single step from a payload dict (used by the REST endpoints
and the CLI harness `run_step.py`) so you can test each step in isolation.
"""

import hashlib
import json

from gemini_client import GeminiClient

# Realism / anti-AI-look negative stack, folded into every Veo prompt.
#
# DELIBERATELY SHORT. Veo's prose sweet spot is ~80-160 words and "very long
# prompts dilute" (Veo guidance). A 200-word negative wall buries the actual
# shot and — because we must fold negatives INTO the prompt text on the Gemini
# Developer API (it rejects the separate negative_prompt field) — long lists of
# anatomy nouns ("six fingers, fused fingers...") can paradoxically summon what
# they name. So we keep a tight, deduplicated stack covering only the highest-
# value buckets: AI look, hand/limb/face deformity, broken motion, on-screen
# text, and the few post artefacts that must never be baked into the plate.
REALISM_NEGATIVE = (
    "plastic skin, waxy skin, over-smoothed skin, uncanny valley, doll-like, "
    "3d render, cgi, cartoon, anime, illustration, over-saturated, "
    "perfect symmetry, dead eyes, deformed hands, extra fingers, fused fingers, "
    "malformed limbs, extra limbs, mutated anatomy, morphing face, warped face, "
    "identity drift, jerky motion, unnatural movement, sliding feet, floating, "
    "subtitles, captions, text overlay, on-screen text, gibberish text, "
    "watermark, logo, timecode, film edge code, film edge markings, "
    "sprocket holes, film perforations, film border, frame counter, "
    "film leader, glitch, scan lines, light leaks, "
    "low quality, blurry, deformed"
)

# ONE short positive close, baked at the END of every assembled prompt. This
# replaces the old ~130-word motion+clean paragraphs. It carries the realism
# baseline (photoreal skin, real physics, correct anatomy) and the clean-plate
# instruction in a single ~30-word sentence, so it survives even when the API
# drops the negative_prompt field WITHOUT diluting the shot.
REALISM_CLOSE = (
    "Photoreal with natural skin texture and visible pores; anatomically correct "
    "hands and limbs that hold their shape, and movement that obeys real-world "
    "physics with believable weight and balance; a clean full-frame plate with "
    "clean edges and no text, captions, logos, film edge markings or sprocket "
    "holes anywhere in frame"
)

# Anti-hallucination rule injected into the grounding-sensitive prompts so the
# model sticks to what the script actually says instead of inventing material.
GROUNDING_RULE = (
    "Stay strictly grounded in the script and the locked bible. Only describe "
    "people, objects, actions and places that are actually present in, or "
    "directly implied by, the source material — do NOT invent new characters, "
    "props, settings, events or backstory. If a detail is not supported by the "
    "script, leave it out rather than guessing. Prefer faithful, literal "
    "description over embellishment."
)

# Faces are where invention does the most damage: a made-up scar, mole, tattoo
# or "piercing gaze" both breaks fidelity to the script and amplifies Veo's
# identity-drift. Lock this down hard wherever a face is described.
FACE_RULE = (
    "FACE RULE: Describe a character's face using ONLY traits the script states "
    "or directly implies. Do NOT invent scars, moles, birthmarks, freckles, "
    "tattoos, piercings, dimples, wrinkles, stubble, glasses or any other "
    "distinguishing mark that the script does not mention. If the script gives "
    "no facial detail, use plain, ordinary, neutral features and move on. Even "
    "for traits the script DOES mention, state them once, plainly and "
    "literally — never exaggerate, dramatise or pile on adjectives."
)

# A working director's toolkit, injected into the shot-writing step so camera
# choices are MOTIVATED by emotion instead of defaulting to a flat eye-level
# medium every time. Tuned for short-form / microdrama storytelling (the punchy,
# reaction-driven, mobile-first style of vertical mini-dramas) while staying
# inside Veo's hard limits: ONE continuous shot per clip, ONE slow motivated
# move, never a zoom. The goal is expressive coverage, not coverage variety
# inside a single clip (Veo cannot cut).
DIRECTION_STYLE = (
    "DIRECTION STYLE — direct each shot like a short-form / microdrama "
    "storyteller; every choice of size, angle, lens and move must be MOTIVATED "
    "by that beat's emotion, never a default. "
    "SHOT SIZE: wide/establishing to place the world and isolate a figure in it; "
    "medium for action and relationship; close-up (CU) and extreme close-up "
    "(ECU) for emotion, decision and reaction — microdrama lives on faces, so "
    "land the emotional peak of a beat on a tight face or a telling detail "
    "insert (hands, an object, eyes). "
    "ANGLE: eye-level reads honest and neutral; a LOW angle gives a subject "
    "power, threat or heroism; a HIGH angle makes them small, watched or "
    "defeated; a slight DUTCH/canted tilt signals unease, shock or a world "
    "knocked off-balance; over-the-shoulder frames confrontation and longing; "
    "a POV puts us inside the character. "
    "LENS: a wide lens (≈18-28mm) opens space, isolation and environment but "
    "distorts faces up close; a normal lens (≈35-50mm) feels natural and human; "
    "a long lens (≈85mm+) compresses space and isolates the subject for "
    "intimacy or voyeuristic tension. "
    "CAMERA MOVE: a move must MEAN something — a slow push-in tightens on a "
    "growing realization, dread or intimacy; a pull-out reveals context, "
    "isolation or abandonment; a lateral dolly/track walks WITH a subject for "
    "momentum; a tilt up gives awe or scale, a tilt down gives discovery or "
    "defeat; restrained handheld adds nerves and immediacy; a locked-off static "
    "frame is a strong, confident choice for stillness, control or dread — do "
    "NOT move the camera unless the emotion earns it. "
    "DEPTH & BLOCKING: build depth with a foreground element (a 'dirty' frame) "
    "and place the subject off-centre, in negative space or with leading/"
    "looking room to create tension or longing. "
    "RHYTHM: across the whole sequence, VARY the coverage — avoid the same shot "
    "size or angle on back-to-back shots, and build a progression (e.g. "
    "establish wider, then tighten toward a close-up on the emotional turn) so "
    "the cut sequence has visual momentum."
)

# Aspect-specific framing craft, appended to DIRECTION_STYLE per run.
DIRECTION_BY_ASPECT = {
    "9:16": (
        "VERTICAL FRAMING (9:16, mobile-first microdrama): compose tall — stack "
        "foreground, subject and background top-to-bottom; favour tighter, "
        "face-forward framing (MCU/CU) that reads instantly on a small phone "
        "screen; keep the eyes in the upper third with deliberate headroom; use "
        "vertical negative space (above or below the subject) for tension."
    ),
    "16:9": (
        "WIDESCREEN FRAMING (16:9): use the horizontal width — place the subject "
        "on a third with lateral negative space and looking room; let the "
        "environment breathe across the frame in wides; reserve the full width "
        "for scale and relationship between subject and place."
    ),
}


def _direction_for(aspect: str) -> str:
    """DIRECTION_STYLE plus the framing craft for this aspect ratio."""
    extra = DIRECTION_BY_ASPECT.get(aspect, DIRECTION_BY_ASPECT["9:16"])
    return f"{DIRECTION_STYLE} {extra}"


STEP_TITLES = {
    1: "Extract voiceover & visuals",
    2: "Understand the concept",
    3: "Cinematic style & characters",
    4: "Split into single-shot beats",
    5: "Veo 3.1 prompts",
    6: "Check visuals against the script",
}

# What each step needs in its payload when run individually.
STEP_INPUTS = {
    1: ["script"],
    2: ["script", "segments"],
    3: ["script", "concept"],
    4: ["concept", "style", "segments"],
    5: ["style", "segments"],
    6: ["script", "style", "segments"],
}


# --------------------------------------------------------------------------- #
# Full pipeline
# --------------------------------------------------------------------------- #

def run_pipeline(
    script: str,
    emit,
    gemini: GeminiClient | None = None,
    confirm=None,
    aspect: str = "9:16",
) -> dict:
    """Run all five steps with `emit(step, status, message, data=None)` callbacks.

    `confirm`, if given, is a callable invoked AFTER the shot split (step 4) and
    BEFORE the Veo prompts are written (step 5): `confirm(segments) -> bool`.
    Returning False stops the pipeline at the shot breakdown — the result holds
    the split segments but no `veo_prompt`. This lets the web app ask the user to
    review the shots before spending model calls on prompt generation. When
    `confirm` is None (CLI / tests) the pipeline runs straight through.
    """
    gemini = gemini or GeminiClient()
    result: dict = {"script": script}

    emit(1, "start", "Extracting voiceover and visual details")
    segments = step1_extract(script, gemini)
    result["segments"] = segments
    emit(1, "done", f"Extracted {len(segments)} segments", {"segments": segments})

    emit(2, "start", "Understanding the overall concept")
    concept = step2_concept(script, segments, gemini)
    result["concept"] = concept
    _merge_by_id(segments, concept.get("segments", []), "intent")
    emit(2, "done", "Concept and per-visual intent ready", {"concept": concept})

    emit(3, "start", "Designing cinematic style and characters")
    style = step3_style(script, concept, gemini)
    result["style"] = style
    emit(3, "done", "Style prefix and character bible ready", {"style": style})

    emit(4, "start", "Splitting visuals into single-shot beats with full story context")
    detailed = step4_detailed(concept, style, segments, gemini, script=script)
    segments = _rebuild_shots(segments, detailed)
    result["segments"] = segments
    emit(4, "done", f"Split into {len(segments)} single-shot beats", {"segments": segments})

    # Gate: let the caller approve the shot breakdown before we write prompts.
    if confirm is not None and not confirm(segments):
        result["segments"] = segments
        return result

    emit(5, "start", "Writing Veo 3.1 text-to-video prompts")
    veo = step5_veo(style, segments, gemini, aspect=aspect)
    _merge_by_id(segments, veo, ["veo_prompt", "veo_shot_body", "veo_seed", "veo_negative", "veo_duration"])
    emit(5, "done", "Veo prompts ready", {"segments": segments})

    # Step 6: with the whole story in view, confirm each shot's visual actually
    # makes sense against the script and fix any prompt that drifted.
    emit(6, "start", "Checking visuals against the script")
    review = step5b_review(script, style, segments, gemini)
    _merge_by_id(
        segments, review,
        ["veo_prompt", "veo_shot_body", "veo_seed", "visual_ok", "visual_note"],
    )
    fixed = sum(1 for r in review if r.get("visual_ok") is False)
    emit(
        6, "done",
        (f"Visuals checked — corrected {fixed} shot(s)" if fixed
         else "Visuals checked — all consistent with the script"),
        {"segments": segments},
    )

    result["segments"] = segments
    return result


# --------------------------------------------------------------------------- #
# Single-step dispatcher (used by REST endpoints + CLI)
# --------------------------------------------------------------------------- #

def run_step(n: int, payload: dict, gemini: GeminiClient | None = None) -> dict:
    """Run one step in isolation from a payload dict.

    Returns a dict containing only the keys that step produces, so the output
    can be merged into a running state object and fed to the next step.
    """
    if n not in STEP_INPUTS:
        raise ValueError(f"Unknown step {n}. Valid steps are 1-6.")
    _require(payload, STEP_INPUTS[n], n)
    gemini = gemini or GeminiClient()

    if n == 1:
        return {"segments": step1_extract(payload["script"], gemini)}

    if n == 2:
        concept = step2_concept(payload["script"], payload["segments"], gemini)
        segments = [dict(s) for s in payload["segments"]]
        _merge_by_id(segments, concept.get("segments", []), "intent")
        return {"concept": concept, "segments": segments}

    if n == 3:
        return {"style": step3_style(payload["script"], payload["concept"], gemini)}

    if n == 4:
        segments = [dict(s) for s in payload["segments"]]
        detailed = step4_detailed(
            payload["concept"], payload["style"], segments, gemini,
            script=payload.get("script", ""),
        )
        segments = _rebuild_shots(segments, detailed)
        return {"segments": segments}

    if n == 5:
        segments = [dict(s) for s in payload["segments"]]
        veo = step5_veo(
            payload["style"], segments, gemini,
            aspect=payload.get("aspect", "9:16"),
        )
        _merge_by_id(segments, veo, ["veo_prompt", "veo_shot_body", "veo_seed", "veo_negative", "veo_duration"])
        return {"segments": segments}

    if n == 6:
        segments = [dict(s) for s in payload["segments"]]
        review = step5b_review(
            payload.get("script", ""), payload["style"], segments, gemini
        )
        _merge_by_id(
            segments, review,
            ["veo_prompt", "veo_shot_body", "veo_seed", "visual_ok", "visual_note"],
        )
        return {"segments": segments}

    raise ValueError(f"Unknown step {n}. Valid steps are 1-6.")


# --------------------------------------------------------------------------- #
# Steps
# --------------------------------------------------------------------------- #

def step1_extract(script: str, gemini: GeminiClient | None = None) -> list[dict]:
    gemini = gemini or GeminiClient()
    system = (
        "You are a video producer's assistant. You split a narration script "
        "into sequential beats. For each beat capture the spoken voiceover and "
        "the visual that should appear on screen for it."
    )
    prompt = f"""Read the script below and break it into ordered beats.

For every beat return:
- id: a 1-based integer
- voiceover: the exact spoken/narration text for this beat (empty string if none)
- visual: a short description of what is shown on screen for this beat

If a beat has visuals but no spoken words, still include it. If the script is
pure narration with no explicit visuals, infer a sensible visual for each beat.

Return JSON: {{"segments": [{{"id": 1, "voiceover": "...", "visual": "..."}}]}}

SCRIPT:
\"\"\"
{script}
\"\"\""""
    data = gemini.generate_json(prompt, system, temperature=0.3)
    segments = data["segments"] if isinstance(data, dict) else data
    cleaned = []
    for i, seg in enumerate(segments, start=1):
        cleaned.append(
            {
                "id": seg.get("id", i),
                "voiceover": (seg.get("voiceover") or "").strip(),
                "visual": (seg.get("visual") or "").strip(),
            }
        )
    return cleaned


def step2_concept(
    script: str, segments: list[dict], gemini: GeminiClient | None = None
) -> dict:
    gemini = gemini or GeminiClient()
    system = (
        "You are a creative director. You distil a script into its core idea "
        "and explain the narrative purpose of each visual."
    )
    visuals = [{"id": s["id"], "visual": s["visual"]} for s in segments]
    prompt = f"""Analyse the script and its visuals.

Return JSON:
{{
  "logline": "one-sentence summary of the whole piece",
  "summary": "2-4 sentence summary of the concept and arc",
  "theme": "central theme",
  "tone": "emotional tone (e.g. hopeful, ominous, nostalgic)",
  "genre": "closest genre / mood",
  "segments": [
    {{"id": 1, "intent": "what this visual is trying to convey and why it matters to the story"}}
  ]
}}

SCRIPT:
\"\"\"
{script}
\"\"\"

VISUALS (by id):
{json.dumps(visuals, ensure_ascii=False, indent=2)}"""
    data = gemini.generate_json(prompt, system, temperature=0.3)
    # The model occasionally returns a bare array (just the per-visual intents)
    # instead of the full object. Normalise so callers can rely on a dict with a
    # "segments" key and never hit `'list' object has no attribute 'get'`.
    if isinstance(data, list):
        return {"segments": data}
    if not isinstance(data, dict):
        return {"segments": []}
    data.setdefault("segments", [])
    return data


def step3_style(
    script: str, concept: dict, gemini: GeminiClient | None = None
) -> dict:
    gemini = gemini or GeminiClient()
    system = (
        "You are a cinematographer and character designer building a TEXT-ONLY "
        "consistency bible for Google Veo. Each clip is generated independently "
        "with no memory, so the same look, faces and places only recur if they "
        "are described in IDENTICAL words every time. You write tight, verbatim "
        "blocks that will be pasted unchanged into every prompt."
    )
    prompt = f"""Based on the script and its concept, build the consistency bible.

Match the tone "{concept.get('tone', '')}" and genre "{concept.get('genre', '')}".
Everything must read photoreal and real-life — never animated, stylised or the
plastic "AI" look. Name real capture media and motivated light. Do NOT use the
words beautiful, stunning, flawless, perfect, cinematic lighting, 8k, hyper-detailed.

{GROUNDING_RULE}

{FACE_RULE}

CONSISTENCY RULE: Each clip is generated with no memory, so a character only
looks the same across clips if their identity_block fully pins them in IDENTICAL
words every time. So make every identity_block self-sufficient and concrete: a
clear age, skin tone, face shape, hair, build, and ONE specific locked outfit
(named garments, colors, footwear). Wardrobe is the most common thing that drifts
— lock a single default outfit per character and never vary it. If the script
does not describe a character's clothing, choose one plausible, ordinary outfit
and lock it; this is allowed for wardrobe even though invented facial marks are
not. Only give a character a second outfit if the script explicitly has them
change clothes.

Return JSON:
{{
  "style_prefix": "ONE concrete sentence — the Look Line — naming a real digital cinema camera, lens family, color grade, lighting philosophy and a fine subtle grain, ending with the word photoreal. Describe the grade and grain as a digital look ONLY — do NOT mention physical film stock, celluloid, Kodak/Fuji stock names, film edges, sprocket holes, keykode or edge printing (Veo renders those as gibberish text along the frame border). This exact sentence is pasted at the front of EVERY shot.",
  "palette": "key colors",
  "lighting": "lighting approach",
  "camera": "lens & movement language",
  "characters": [
    {{
      "name": "short name or role label, used to tag which shots they appear in",
      "description": "who they are (one line, for the UI)",
      "appearance": "age, build, face, hair (for the UI) — only what the script supports, no invented marks",
      "wardrobe": "default clothing (for the UI)",
      "identity_block": "30-45 words, a single verbatim sentence in this order: apparent age, skin tone, face shape, hair (color/length/style), build, then ONE locked default outfit named concretely — specific garments, colors and footwear (e.g. 'faded navy denim jacket over a grey crew-neck tee, black jeans, scuffed brown leather boots'). Plain, ordinary and literal — NOT flattering, NOT exaggerated. Add a facial mark (scar/mole/tattoo/etc.) ONLY if the script explicitly mentions it. This exact text is the ONLY description Veo ever sees, so it must fully pin the same person AND the same clothes in every clip."
    }}
  ],
  "locations": [
    {{
      "name": "short location label, used to tag which shots happen here",
      "place_block": "20-35 words, a single verbatim sentence: architecture/space, 1-3 fixed landmarks/fixtures, palette, light character, time of day. Pasted whenever a shot is set here."
    }}
  ]
}}

List every recurring on-screen character and every distinct location in the
script. If the piece is faceless/voiceover-driven with no recurring person,
return an empty characters list and lock the recurring objects/motifs as
locations instead.

CONCEPT:
{json.dumps(concept, ensure_ascii=False, indent=2)}

SCRIPT:
\"\"\"
{script}
\"\"\""""
    style = gemini.generate_json(prompt, system)
    # Guard against the model returning a non-object so downstream `.get` calls
    # (and _assign_seeds) never crash on a list.
    if not isinstance(style, dict):
        style = {}
    style.setdefault("characters", [])
    style.setdefault("locations", [])
    _assign_seeds(style)
    return style


def step4_detailed(
    concept: dict,
    style: dict,
    segments: list[dict],
    gemini: GeminiClient | None = None,
    script: str = "",
) -> list[dict]:
    """Split each beat's visual into the individual single-shot beats it actually
    contains, expand each into a shootable description, and tag it with the bible
    characters / location.

    Veo (and every current Veo model) can only render ONE continuous camera shot
    per clip — it cannot cut between angles, locations or moments within a single
    generation. So a beat whose visual implies a scene change, a cut to a
    different subject, or several actions in sequence must become MULTIPLE shots.
    Each output shot carries a `source_id` back to its parent beat so the
    voiceover/narration context is preserved. The full script is supplied for
    grounding, and the call runs at a low temperature to curb hallucination.
    """
    gemini = gemini or GeminiClient()
    system = (
        "You are a storyboard artist and editor preparing a shot list for "
        "Google Veo, which can only render ONE continuous camera shot per clip — "
        "it cannot cut between angles, locations or moments inside a single "
        "generation. You split each beat's visual into the individual single-shot "
        "beats it actually contains (one continuous shot, one primary action, one "
        "location each), expand each into a shootable description, and tag it with "
        "its bible characters and location. You stay strictly faithful to the "
        "script and never invent material that is not in it."
    )
    items = [
        {
            "id": s["id"],
            "visual": s["visual"],
            "voiceover": s.get("voiceover", ""),
            "intent": s.get("intent", ""),
        }
        for s in segments
    ]
    char_names = [c.get("name", "") for c in style.get("characters", [])]
    loc_names = [l.get("name", "") for l in style.get("locations", [])]
    prompt = f"""Split each visual into one or more single-shot beats, then expand
each beat into a detailed, shootable description and tag who and where it is.

{GROUNDING_RULE}

Veo renders only ONE continuous shot per clip — it cannot cut inside a clip.
Split one beat's visual into SEVERAL shots when it implies any of:
- a change of location or setting;
- a cut to a different subject or framing (e.g. a wide of a place AND a
  close-up of a person are two shots);
- more than one distinct action or moment happening in sequence;
- any "then / and then / as / while / before / after" that chains separate
  actions or moments.
Keep it as a SINGLE shot when it is already one continuous action in one place.
Order the shots in the sequence they should play on screen.

Each shot must describe ONE primary action in ONE location. Stay consistent
with the bible — do not re-invent a character's appearance or a location's
fixtures.

For every output shot return:
- source_id: the id of the parent beat it came from (so its narration is kept).
- detailed_visual: a shootable description of ONE primary action in ONE place.
- characters: the subset of bible character names that actually appear ON SCREEN
  in this shot (exact names from the list below; empty list if none).
- location: the single bible location name where this shot takes place (exact
  name from the list below; empty string if none clearly applies).

Return JSON in play order:
{{"segments": [{{"source_id": 1, "detailed_visual": "...", "characters": ["..."], "location": "..."}}]}}

BIBLE CHARACTER NAMES (choose only from these):
{json.dumps(char_names, ensure_ascii=False)}

BIBLE LOCATION NAMES (choose only from these):
{json.dumps(loc_names, ensure_ascii=False)}

FULL SCRIPT (for context — keep every shot consistent with this story, and do
not add anything that is not grounded in it):
\"\"\"
{script}
\"\"\"

CONCEPT:
{json.dumps(concept, ensure_ascii=False, indent=2)}

STYLE & CHARACTERS:
{json.dumps(style, ensure_ascii=False, indent=2)}

BEATS TO SPLIT AND EXPAND:
{json.dumps(items, ensure_ascii=False, indent=2)}"""
    data = gemini.generate_json(prompt, system, temperature=0.25)
    return data["segments"] if isinstance(data, dict) else data


def step5_veo(
    style: dict,
    segments: list[dict],
    gemini: GeminiClient | None = None,
    aspect: str = "9:16",
) -> list[dict]:
    """Write the per-shot 'shot body' with the LLM, then ASSEMBLE each prompt
    deterministically: Look Line + location block + character block(s) verbatim
    + the shot body. Consistency lives in the verbatim blocks, not in wording the
    model re-invents per shot. Also attaches the per-shot seed and negatives.

    `aspect` selects the framing craft (vertical microdrama vs widescreen) folded
    into the direction guidance.
    """
    gemini = gemini or GeminiClient()
    system = (
        "You are a director and prompt engineer for Google's Veo 3.1 "
        "text-to-video model. "
        "The film look, the characters' appearance and the locations are ALREADY "
        "locked and will be prepended verbatim — you must NOT re-describe them. "
        "You write ONLY the shot body: shot grammar (size + angle + lens + camera "
        "move), ONE primary action, and the audio line. You DIRECT each shot — "
        "choosing the size, angle, lens and move that serve the beat's emotion — "
        "not just transcribe it. "
        "You know Veo's real failure modes and you write to dodge them: it "
        "collapses on more than one action per clip; it morphs hands and fingers "
        "during close fine-finger manipulation; it breaks anatomy and physics on "
        "fast, acrobatic or busy motion; it clones faces in crowds; and longer "
        "holds drift more than short ones. So every shot body describes a SINGLE, "
        "simple, physically plausible human movement at an unhurried, natural "
        "speed, frames fine hand work wide or just off-frame, keeps crowds small "
        "and vague, and uses only camera moves Veo honors (static, slow push-in, "
        "pull-out, dolly, tracking, pan, tilt, handheld — never a zoom). You first "
        "read each shot's intent and voiceover to understand what it must convey, "
        "then pick the shortest duration that lets that one action breathe."
    )
    items = [
        {
            "id": s["id"],
            "visual": s.get("visual", ""),
            "detailed_visual": s.get("detailed_visual", s.get("visual", "")),
            "intent": s.get("intent", ""),
            "voiceover": s.get("voiceover", ""),
            "characters": s.get("characters", []),
            "location": s.get("location", ""),
        }
        for s in segments
    ]
    prompt = f"""For each shot, read its visual, detailed_visual, intent and
voiceover to understand what it must convey. Then DIRECT and write ONLY the
"shot body" — do NOT describe the characters' looks, the film grade, or the
location's fixtures (those are prepended verbatim for you, so repeating them
only dilutes the prompt).

{GROUNDING_RULE}

{_direction_for(aspect)}

A shot body is ONE tight paragraph, about 35-55 words, in this exact order:
1. Shot grammar: shot size + camera angle + lens + camera move, chosen per the
   DIRECTION STYLE above to serve THIS beat's emotion — e.g. "Low-angle medium
   close-up on an 85mm lens, slow push-in." Always state the angle (eye-level,
   low, high, or a slight Dutch tilt) deliberately; a locked-off static frame is
   a valid, strong choice. Use only moves Veo honors (static, slow push-in,
   pull-out, dolly, tracking, pan, tilt, handheld) — never "zoom", and only ONE
   slow move per shot.
2. ONE primary action, present tense, serving the intent. It must be a SINGLE
   simple, physically plausible human movement (a head turn, one step, a reach,
   a slow exhale) at a natural, unhurried speed. No "then/and then", no second
   action, no crowds (if people are needed, keep them few and vague), no fast or
   acrobatic motion. Refer to a character by name only ("the Keeper grips the
   rail") — never re-describe their appearance.
3. Audio line: "Audio: <ambient bed>, <1-2 SFX anchored to the visible action>;
   no music."

Direct the WHOLE list as one sequence: vary the shot size and angle from one
shot to the next (never repeat the same framing back-to-back) and build a
deliberate progression toward each beat's emotional peak, while keeping every
single move slow and Veo-safe.

Failure-dodging rules:
- If the action involves close finger work, frame it WIDE or keep the hands
  partly out of frame — Veo morphs fingers in tight hand close-ups.
- No on-screen text, signs, screens or readable writing in frame (Veo renders
  text as gibberish); frame any such element out.
- No spoken dialogue lines. No reference to any other clip. Each shot stands alone.

Also choose ideal_duration (one of 4, 6, 8) — the SHORTEST that lets the single
action play out unhurried. Longer clips DRIFT MORE, so bias short:
- 4: a brief beat (a glance, a small gesture, a near-static moment).
- 6: a moderate action with people on screen (a step, a turn, a reach) — this is
  the safe default whenever a person is the subject.
- 8: RESERVE for establishing / atmosphere / landscape shots with NO people (or
  people tiny in frame), where there is little anatomy to drift.
Do not pad; if a person is the subject, prefer 4 or 6 over 8.

Return JSON: {{"segments": [{{"id": 1, "shot_body": "...", "ideal_duration": 6}}]}}

SHOTS (with intent / who / where):
{json.dumps(items, ensure_ascii=False, indent=2)}"""
    data = gemini.generate_json(prompt, system, temperature=0.3)
    bodies = data["segments"] if isinstance(data, dict) else data
    body_by_id = {b.get("id"): b for b in bodies}

    updates = []
    for i, s in enumerate(segments, start=1):
        b = body_by_id.get(s["id"]) or body_by_id.get(i) or {}
        body = (b.get("shot_body") or b.get("veo_prompt") or "").strip()
        if not body:
            body = (s.get("detailed_visual") or s.get("visual") or "").strip()

        veo_prompt, seed = _assemble_veo_prompt(style, s, body)
        has_people = bool(s.get("characters"))
        duration = _resolve_duration(b.get("ideal_duration"), has_people)
        updates.append(
            {
                "id": s["id"],
                "veo_prompt": veo_prompt,
                "veo_shot_body": body,  # kept so the review step can revise it
                "veo_seed": seed,
                "veo_negative": REALISM_NEGATIVE,
                "veo_duration": duration,
            }
        )
    return updates


def _assemble_veo_prompt(style: dict, seg: dict, body: str) -> tuple[str, int | None]:
    """Build the final 'positive/negative' Veo prompt for one shot.

    Consistency lives in the locked bible blocks, NOT in per-shot wording, so the
    prompt is always assembled in this fixed order: Look Line -> location block ->
    character identity block(s) -> the shot body -> the realism close, with the
    negative stack folded in as a labelled block (the Developer API rejects the
    separate negative_prompt field). `body` is the only free part; everything
    else is pasted verbatim from the bible. Returns (veo_prompt, seed).
    """
    look_line = (style.get("style_prefix") or "").strip()
    char_by_name = {
        (c.get("name") or "").strip().lower(): c for c in style.get("characters", [])
    }
    loc_by_name = {
        (l.get("name") or "").strip().lower(): l for l in style.get("locations", [])
    }

    parts = []
    if look_line:
        parts.append(_ensure_period(look_line))

    loc = _lookup_block(seg.get("location") or "", loc_by_name)
    if loc and loc.get("place_block"):
        parts.append(_ensure_period(loc["place_block"]))

    seeds = []
    for name in seg.get("characters", []) or []:
        c = _lookup_block(name or "", char_by_name)
        if c and c.get("identity_block"):
            parts.append(_ensure_period(c["identity_block"]))
            if c.get("seed") is not None:
                seeds.append(c["seed"])

    if body:
        parts.append(_ensure_period(body))

    # ONE short positive close pins the realism baseline + clean plate without
    # bloating the prompt (Veo dilutes on very long prompts).
    parts.append(_ensure_period(REALISM_CLOSE))

    positive_text = " ".join(parts).strip()
    veo_prompt = (
        f"positive prompt:- {positive_text}\n"
        f"negative prompt:- {REALISM_NEGATIVE}"
    )
    seed = seeds[0] if seeds else (loc.get("seed") if loc else None)
    return veo_prompt, seed


def step5b_review(
    script: str, style: dict, segments: list[dict], gemini: GeminiClient | None = None
) -> list[dict]:
    """Coherence gate between prompt writing (step 5) and video generation.

    With the FULL script in view, judge each shot's visual (its shot body) against
    the story: does it actually make sense for that moment, faithfully convey the
    intent, and avoid contradicting or inventing events? If a visual is wrong,
    misleading, off-story or nonsensical, rewrite ONLY the shot body to fix it,
    then re-assemble the Veo prompt around the unchanged locked bible blocks. The
    Look Line, identity blocks and location block are never touched here — only
    the per-shot action — so consistency is preserved while accuracy improves.

    Returns updates [{id, veo_prompt, veo_shot_body, veo_seed, visual_ok,
    visual_note}] only for shots that have a prompt; merge them into the segments.
    """
    gemini = gemini or GeminiClient()
    prompted = [s for s in segments if s.get("veo_prompt")]
    if not prompted:
        return []

    system = (
        "You are a continuity and grounding checker for a text-to-video shotlist. "
        "You have the FULL script, so you understand the whole story. For each "
        "shot you get its narration/voiceover, its intent, the source visual, and "
        "the current SHOT BODY — the camera + single action description that will "
        "actually be filmed. Your job: decide whether that visual genuinely makes "
        "sense for this moment in the story and is faithful to the script — it "
        "must not contradict the script, invent events or props that aren't there, "
        "depict the wrong subject or action, or be physically implausible, and it "
        "must clearly convey the intended beat. If the shot body is fine, return "
        "it UNCHANGED and makes_sense=true. If it is wrong, misleading, off-story "
        "or nonsensical, set makes_sense=false and rewrite ONLY the shot body to "
        "fix it. Keep the same format: shot grammar (size + lens + camera move Veo "
        "honors — static, push-in, pull-out, dolly, tracking, pan, tilt, handheld; "
        "never zoom), ONE simple physically-plausible action in present tense, and "
        "an 'Audio: ...' line. Refer to characters by name only — do NOT describe "
        "their looks, the film grade, or location fixtures (those are locked and "
        "prepended separately, so repeating them only dilutes the prompt). Stay "
        "strictly grounded in the script; never add anything it does not support."
    )
    items = [
        {
            "id": s["id"],
            "voiceover": s.get("voiceover", ""),
            "intent": s.get("intent", ""),
            "visual": s.get("visual", ""),
            "detailed_visual": s.get("detailed_visual", ""),
            "shot_body": s.get("veo_shot_body", ""),
        }
        for s in prompted
    ]
    prompt = f"""Check each shot's visual against the whole story and fix it if it
does not make sense or is not faithful to the script.

For every shot return:
- id: the shot id, unchanged.
- makes_sense: true if the current shot_body is a sensible, faithful visual for
  this moment; false if it is wrong, misleading, off-story or nonsensical.
- issue: a SHORT note on what was wrong (empty string if makes_sense is true).
- shot_body: the shot body to use — unchanged when makes_sense is true, or the
  corrected version when it is false (same format and grounding rules).

Return JSON: {{"segments": [{{"id": 1, "makes_sense": true, "issue": "", "shot_body": "..."}}]}}

FULL SCRIPT (the complete story — judge every shot against this):
\"\"\"
{script}
\"\"\"

SHOTS TO CHECK (in play order):
{json.dumps(items, ensure_ascii=False, indent=2)}"""
    data = gemini.generate_json(prompt, system, temperature=0.25)
    reviews = data["segments"] if isinstance(data, dict) else data
    review_by_id = {r.get("id"): r for r in (reviews or [])}

    updates = []
    for i, s in enumerate(prompted, start=1):
        r = review_by_id.get(s["id"]) or review_by_id.get(i) or {}
        ok = bool(r.get("makes_sense", True))
        note = (r.get("issue") or "").strip()
        new_body = (r.get("shot_body") or "").strip()
        old_body = (s.get("veo_shot_body") or "").strip()

        update = {"id": s["id"], "visual_ok": ok, "visual_note": note}
        # Only re-assemble when the reviewer actually changed the body.
        if new_body and new_body != old_body:
            veo_prompt, seed = _assemble_veo_prompt(style, s, new_body)
            update["veo_prompt"] = veo_prompt
            update["veo_shot_body"] = new_body
            update["veo_seed"] = seed
            update["visual_ok"] = False  # it was corrected
        updates.append(update)
    return updates


def sanitize_prompt(
    veo_prompt: str, error: str = "", gemini: GeminiClient | None = None
) -> str:
    """Rewrite a Veo prompt that the safety filter rejected so it can pass,
    while preserving the shot's story intent, locked look, characters and place.

    Veo refuses prompts it reads as unsafe — graphic violence, gore, blood,
    wounds, weapons harming people, sexual or suggestive content, hate, real
    named public figures/brands, or minors in unsafe contexts. Many legitimate
    B-roll beats trip this as false positives. We ask Gemini to keep the same
    scene but reframe whatever tripped the filter into policy-safe wording
    (imply harm via aftermath/reaction instead of depicting it, drop gore,
    replace a weapon with a non-violent action, make ambiguous figures clearly
    adult, remove named real people). The verbatim look line, identity blocks
    and location block are kept intact; only the offending action is softened.
    Returns the reassembled 'positive/negative' prompt.
    """
    gemini = gemini or GeminiClient()
    positive, negative = _split_veo_prompt(veo_prompt)
    system = (
        "You are a prompt-safety editor for a text-to-video model. A prompt was "
        "rejected by the model's content-safety filter. Rewrite it so it COMPLIES "
        "with content policy while keeping the shot's story intent, the locked "
        "film look, the characters and the location. Make it policy-safe: no "
        "graphic violence, gore, blood or wounds; no weapons aimed at or harming "
        "people; no sexual, suggestive or nude content; no hate or harassment; no "
        "real named public figures or trademarks; no minors in unsafe or adult "
        "contexts. Where the original implied something unsafe, REFRAME it to "
        "imply rather than depict — show the aftermath or a character's reaction "
        "instead of the act, replace a violent action with a restrained one, make "
        "any ambiguous person clearly an adult. Keep the verbatim look line, the "
        "character identity blocks and the location block word-for-word unchanged; "
        "only adjust the single action/description that tripped the filter. Stay "
        "photoreal and keep roughly the same length."
    )
    err_line = f" The filter said: {error}." if error else ""
    user = f"""The following text-to-video prompt was blocked by the safety filter.{err_line}
Rewrite ONLY the positive prompt so it passes, keeping the same scene, look,
characters and location but removing or softening whatever violates content
policy. Do not add disclaimers or meta commentary.

Return JSON: {{"positive_prompt": "..."}}

POSITIVE PROMPT:
\"\"\"
{positive}
\"\"\""""
    data = gemini.generate_json(user, system, temperature=0.4)
    new_pos = ""
    if isinstance(data, dict):
        new_pos = (data.get("positive_prompt") or data.get("prompt") or "").strip()
    if not new_pos:
        new_pos = positive
    out = f"positive prompt:- {new_pos}"
    if negative:
        out += f"\nnegative prompt:- {negative}"
    return out


def _split_veo_prompt(text: str) -> tuple[str, str]:
    """Split a stored Veo prompt into (positive, negative).

    Prompts are stored as 'positive prompt:- ...\\nnegative prompt:- ...'. We
    pull the two halves apart so the sanitizer only rewrites the positive text
    and re-attaches the unchanged negative block.
    """
    text = text or ""
    pos, neg = text, ""
    neg_marker = "negative prompt:-"
    idx = text.lower().find(neg_marker)
    if idx != -1:
        pos = text[:idx]
        neg = text[idx + len(neg_marker):].strip()
    pos_marker = "positive prompt:-"
    pidx = pos.lower().find(pos_marker)
    if pidx != -1:
        pos = pos[pidx + len(pos_marker):]
    return pos.strip(), neg.strip()


def _resolve_duration(ideal, has_people: bool) -> int:
    """Pick the Veo clip length (4/6/8s) for a shot, research-aligned.

    Veo guidance: longer clips DRIFT MORE (limbs/face morph, physics break) the
    longer a person is held on screen, so 8s is reserved for establishing /
    atmosphere shots with no people. Character / motion shots are capped at 6s,
    which is the single biggest lever against the "fake limbs, weird movement"
    artefacts. We trust the model's `ideal` (the shortest unhurried length) and
    only clamp it:
      - people on screen   -> 4 or 6 (cap at 6)
      - no people present   -> 4, 6 or 8 allowed
    Missing / out-of-range values fall back to a safe 6s.
    """
    try:
        ideal = int(ideal)
    except (TypeError, ValueError):
        ideal = 6
    # Snap to a supported tier.
    if ideal <= 4:
        ideal = 4
    elif ideal <= 6:
        ideal = 6
    else:
        ideal = 8
    if has_people:
        return min(ideal, 6)
    return ideal


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _lookup_block(name: str, table: dict) -> dict | None:
    """Resolve a shot's character/location label to its bible entry, tolerating
    the small wording drift LLMs introduce between steps (case, a leading
    "the", or a short form like "Keeper" vs "The Keeper").

    This matters for consistency: if the label fails to resolve, the verbatim
    identity/place block never gets injected, and Veo then invents a fresh look
    (different face, different clothes) for that clip. A tolerant match keeps the
    same locked block on every appearance.
    """
    if not name:
        return None
    key = name.strip().lower()
    if key in table:
        return table[key]
    stripped = key.removeprefix("the ").strip()
    if stripped and stripped in table:
        return table[stripped]
    # Fall back to a containment match in either direction.
    for k, v in table.items():
        if not k:
            continue
        a, b = stripped or key, k.removeprefix("the ").strip()
        if a and b and (a in b or b in a):
            return v
    return None


def _rebuild_shots(parents: list[dict], shots: list[dict]) -> list[dict]:
    """Replace the parent beats with the (usually more numerous) single-shot
    beats produced by the step-4 split.

    Each split shot carries a `source_id` pointing back to the parent beat it
    came from. We re-id the result sequentially (1..M) so every downstream step
    and the UI see a flat, ordered shot list, while inheriting the parent's
    voiceover / original visual / intent so narration context is preserved.
    Falls back to the parents unchanged if the split returned nothing usable.
    """
    by_id = {p.get("id"): p for p in parents}
    rebuilt = []
    for i, shot in enumerate(shots or [], start=1):
        src = shot.get("source_id", shot.get("id"))
        parent = by_id.get(src, {})
        rebuilt.append(
            {
                "id": i,
                "source_id": src,
                "voiceover": parent.get("voiceover", ""),
                "visual": parent.get("visual", ""),
                "intent": parent.get("intent", ""),
                "detailed_visual": (shot.get("detailed_visual") or "").strip(),
                "characters": shot.get("characters", []) or [],
                "location": (shot.get("location") or "").strip(),
            }
        )
    if not rebuilt:
        return [dict(p) for p in parents]
    return rebuilt


def _merge_by_id(segments: list[dict], updates: list[dict], field) -> None:
    fields = [field] if isinstance(field, str) else list(field)
    by_id = {u.get("id"): u for u in updates}
    for i, seg in enumerate(segments, start=1):
        upd = by_id.get(seg["id"]) or by_id.get(i)
        for f in fields:
            if upd and f in upd:
                seg[f] = upd[f]
            seg.setdefault(f, "")


def _seed_for(name: str) -> int:
    """Stable per-name uint32 seed so the same subject anchors every clip."""
    digest = hashlib.sha256((name or "").strip().lower().encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def _assign_seeds(style: dict) -> None:
    """Give each character and location a fixed seed for text-only identity lock."""
    for c in style.get("characters", []) or []:
        c.setdefault("seed", _seed_for(c.get("name", "")))
    for l in style.get("locations", []) or []:
        l.setdefault("seed", _seed_for("loc:" + l.get("name", "")))


def _ensure_period(text: str) -> str:
    text = (text or "").strip()
    if text and text[-1] not in ".!?":
        text += "."
    return text


def _require(payload: dict, keys: list[str], step: int) -> None:
    missing = [k for k in keys if k not in payload or payload[k] in (None, "")]
    if missing:
        raise KeyError(
            f"Step {step} ({STEP_TITLES.get(step, '?')}) requires: {', '.join(keys)}. "
            f"Missing: {', '.join(missing)}"
        )
