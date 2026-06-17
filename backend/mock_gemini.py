"""A fake Gemini client returning canned data per step.

Lets you exercise the full pipeline, REST endpoints and frontend with no
GEMINI_API_KEY. Used by the CLI harness (`run_step.py --mock`) and the web
app's demo mode (`?demo=1`).
"""

import time


class MockGemini:
    def __init__(self, delay: float = 0.0):
        self.delay = delay

    def generate_json(self, prompt: str, system: str | None = None, temperature=None):
        if self.delay:
            time.sleep(self.delay)
        p = prompt.lower()

        if "break it into ordered beats" in p:
            return {
                "segments": [
                    {"id": 1, "voiceover": "It began with silence.", "visual": "empty harbor at dawn"},
                    {"id": 2, "voiceover": "Then, a light.", "visual": "lighthouse beam sweeping the fog"},
                    {"id": 3, "voiceover": "And the keeper climbed, as he always had.", "visual": "old man ascending spiral stairs with a lantern"},
                ]
            }
        if "analyse the script" in p:
            return {
                "logline": "A keeper guards the last light on a dying coast.",
                "summary": "Across one foggy dawn, a solitary lighthouse keeper keeps watch, embodying quiet persistence against an indifferent sea.",
                "theme": "Persistence and solitude",
                "tone": "Contemplative, austere",
                "genre": "Atmospheric drama",
                "segments": [
                    {"id": 1, "intent": "Establish stillness and isolation"},
                    {"id": 2, "intent": "Introduce hope cutting through the gloom"},
                    {"id": 3, "intent": "Embody duty and quiet endurance"},
                ],
            }
        if "consistency bible" in p or "design the visual identity" in p:
            return {
                "style_prefix": "Shot on ARRI Alexa with vintage anamorphic lenses, naturalistic overcast light, a cool desaturated slate-and-teal digital grade with clean realistic skin, a fine subtle grain, shallow depth of field, photoreal.",
                "palette": "Slate grey, pale blue, weak amber",
                "lighting": "Overcast diffusion with the lighthouse beam as key",
                "camera": "Slow dolly and locked-off wides, 50mm",
                "characters": [
                    {
                        "name": "The Keeper",
                        "description": "Aging lighthouse keeper",
                        "appearance": "60s, weathered face, white stubble, sea-grey eyes",
                        "wardrobe": "Oilskin coat, wool cap",
                        "identity_block": "a man in his late sixties with weathered fair skin, a long lined face with deep crow's feet and pale sea-grey eyes, short white stubble and thinning grey hair under a navy wool cap, stocky and slightly stooped, wearing a worn mustard-yellow oilskin coat over a grey roll-neck sweater",
                    }
                ],
                "locations": [
                    {
                        "name": "Harbor",
                        "place_block": "a deserted stone harbor at first light, glassy still water, coiled ropes and a single moored dinghy, fog erasing the horizon, slate-grey palette, cold flat dawn light",
                    },
                    {
                        "name": "Lighthouse Gallery",
                        "place_block": "the open railed gallery atop a stone lighthouse, a great glass lens housing behind, dense fog all around, the revolving beam cutting the murk, weak amber-on-grey light at dawn",
                    },
                    {
                        "name": "Lighthouse Stairwell",
                        "place_block": "a narrow tower interior with a worn iron spiral staircase, damp whitewashed stone walls, a small grimy window, deep shadow warmed only by a hand lantern",
                    },
                ],
            }
        if "split each visual" in p or "single-shot beats" in p or "expand each visual" in p:
            # Beat 2 ("a light") is split into two single-shot beats: the beam,
            # then the Keeper reacting — demonstrating the multi-shot split.
            return {
                "segments": [
                    {"source_id": 1, "detailed_visual": "A deserted stone harbor at first light, glassy water, fog erasing the horizon; coiled ropes and a single moored dinghy in the foreground.", "characters": [], "location": "Harbor"},
                    {"source_id": 2, "detailed_visual": "The lighthouse beam carves a single slow arc through the dense fog above the water.", "characters": [], "location": "Lighthouse Gallery"},
                    {"source_id": 2, "detailed_visual": "The Keeper stands small at the gallery rail, turning his head to watch the beam pass.", "characters": ["The Keeper"], "location": "Lighthouse Gallery"},
                    {"source_id": 3, "detailed_visual": "Inside the tower, the Keeper ascends a worn iron spiral staircase, lantern in hand, warm light raking his weathered face against cold stone.", "characters": ["The Keeper"], "location": "Lighthouse Stairwell"},
                ]
            }
        if "makes_sense" in p or "check each shot's visual against the whole story" in p:
            # Visual-coherence review: most shots pass unchanged; shot 3 is
            # "corrected" to demonstrate the prompt-fix path. The unchanged ones
            # return their exact step-5 bodies so no re-assembly is triggered.
            return {
                "segments": [
                    {"id": 1, "makes_sense": True, "issue": "", "shot_body": "Wide establishing shot on a 35mm lens, slow dolly forward across the water. The fog thins for a moment to reveal the empty quay, the moored dinghy rocking once on a low swell. Audio: gentle lapping water, a distant buoy bell, faint wind; no music."},
                    {"id": 2, "makes_sense": True, "issue": "", "shot_body": "Wide on a 50mm lens, locked off. The lighthouse beam sweeps one slow arc across the fog. Audio: low wind, the deep hum of the rotating lamp; no music."},
                    {"id": 3, "makes_sense": False, "issue": "Keeper should look toward the beam he tends, matching the 'a light' beat.", "shot_body": "Medium wide on a 50mm lens, locked off. The Keeper grips the gallery rail and lifts his gaze to follow the sweeping beam, breath clouding in the cold. Audio: low wind, a creak of metal; no music."},
                    {"id": 4, "makes_sense": True, "issue": "", "shot_body": "Medium shot on a 35mm lens, slow tracking push up the stairwell. The Keeper climbs one heavy step at a time, raising the lantern so its glow slides up the curved stone. Audio: echoing footsteps on iron, the hiss of the lantern, faint dripping; no music."},
                ]
            }
        if "shot body" in p or "veo" in p:
            return {
                "segments": [
                    {"id": 1, "shot_body": "Wide establishing shot on a 35mm lens, slow dolly forward across the water. The fog thins for a moment to reveal the empty quay, the moored dinghy rocking once on a low swell. Audio: gentle lapping water, a distant buoy bell, faint wind; no music.", "ideal_duration": 8},
                    {"id": 2, "shot_body": "Wide on a 50mm lens, locked off. The lighthouse beam sweeps one slow arc across the fog. Audio: low wind, the deep hum of the rotating lamp; no music.", "ideal_duration": 4},
                    {"id": 3, "shot_body": "Medium wide on a 50mm lens, locked off. The Keeper grips the gallery rail and turns his head to follow the sweeping beam, breath clouding in the cold. Audio: low wind, a creak of metal; no music.", "ideal_duration": 4},
                    {"id": 4, "shot_body": "Medium shot on a 35mm lens, slow tracking push up the stairwell. The Keeper climbs one heavy step at a time, raising the lantern so its glow slides up the curved stone. Audio: echoing footsteps on iron, the hiss of the lantern, faint dripping; no music.", "ideal_duration": 6},
                ]
            }
        return {}


def make_mock(delay: float = 0.0) -> MockGemini:
    return MockGemini(delay=delay)


SAMPLE_SCRIPT = """EXT. COASTAL CLIFF — DAWN

The sea is grey and endless. Waves break against black rock far below.

VO: Every morning, the old keeper climbed the same hundred steps.

INT. LIGHTHOUSE STAIRWELL — CONTINUOUS

A weathered man ascends a spiral staircase, lantern in hand.

VO: Not because anyone told him to. But because the light had to burn.

EXT. LIGHTHOUSE GALLERY — DAWN

He polishes the great lens as the first sun cuts through the fog.

VO: Some duties outlast the people who remember why they began."""
