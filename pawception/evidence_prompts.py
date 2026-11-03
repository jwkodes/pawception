"""Small object descriptions that bridge Muse detections to SAM segmentation."""

FIELDS = ("pet_segmentation_prompt", "object_segmentation_prompt")


def schema_fields():
    return {field: {"type": ["string", "null"], "maxLength": 120} for field in FIELDS}


def validate_prompts(answer):
    for field in FIELDS:
        value = answer[field]
        if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 120):
            raise ValueError(f"Invalid {field}")
    if answer.get("detected") is False and any(answer[field] is not None for field in FIELDS):
        raise ValueError("Undetected events need null segmentation prompts")


PROMPT = """
For each event and hazard also return pet_segmentation_prompt and
object_segmentation_prompt. These locate the pet and relevant hazard or event
object in ONE evidence image. Each is a short, concrete noun phrase naming one
concept, with visible color/type attributes to distinguish the intended target.
Do not include actions, commands, relationships, lists or claims of danger.
Use null when a distinct visible target cannot be named; do not invent an
object for an off-screen hazard, illness or smoke. For detected=false, both
prompts must be null.
For a household hazard with no visible pet, pet_segmentation_prompt must be
null. Name a concrete visible source or affected object when available, such as
an outlet or sofa. If no suitable object is visible, both prompts may be null;
the original evidence frame will still be shown. Never invent a pet or object
just to produce a segmentation prompt.
"""
