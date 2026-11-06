"""Content policy for presets (ADR-121).

A preset marked ``content_policy.adult`` (adult-only image or text model):

* is never chosen by Autonomous Mode (its worker only offers ``direct.<preset>``; ``autonomous_default`` is
  forced off) — it runs when the user picks it, or when a workflow the user wrote names it explicitly;
* needs the opt-in ``general.allow_adult_content: true`` in settings **and** a per-run confirmation that
  the user is an adult and the content is about consenting adults;
* refuses prompts about minors, non-consent or abuse, and sexual imagery of real, identifiable people;
* always adds a negative prompt against minors.

The filter lowers risk; it is not a guarantee, so every decision is written to the event log.
"""
from __future__ import annotations

import re

MINORS = re.compile(
    r"(?i)\b(child|children|kid|kids|toddler|infant|baby|minor|minors|underage|under[- ]age|preteen|pre-teen|"
    r"teen|teens|teenage|teenager|adolescent|juvenile|schoolgirl|schoolboy|school ?uniform|loli|lolita|shota|"
    r"jailbait|young[- ]looking|childlike|petite girl|little girl|little boy|elementary|middle school|"
    r"high ?school(er)?|1[0-7] ?(yo|y/o|years? old)|[1-9] ?(yo|y/o|years? old)|"
    r"trẻ em|trẻ con|bé gái|bé trai|em bé|vị thành niên|học sinh|thiếu niên|thiếu nữ|nữ sinh|nam sinh|"
    r"[1-9] tuổi|1[0-7] tuổi)\b")
NON_CONSENT = re.compile(
    r"(?i)\b(rape|raped|raping|non[- ]?consensual|without consent|forced|forcing|coerced|drugged|unconscious|"
    r"asleep|sleeping|passed out|incest|bestiality|zoophilia|abuse|abused|molest\w*|assault\w*|trafficking|"
    r"hidden camera|spy ?cam|upskirt|voyeur\w*|revenge porn|snuff|"
    r"cưỡng hiếp|hiếp dâm|ép buộc|không đồng thuận|bị chuốc thuốc|bất tỉnh|loạn luân|xâm hại|lạm dụng|quay lén)\b")
REAL_PEOPLE = re.compile(r"(?i)\b(deepfake|deep fake|face ?swap|real person|celebrity|celeb|lookalike|"
                         r"my (ex|wife|girlfriend|boyfriend|husband|coworker|classmate|neighbou?r)|người thật|"
                         r"người nổi tiếng|ghép mặt)\b")
ADULT_NEGATIVE = "child, minor, teen, underage, young-looking, childlike, school uniform"

CONFIRM_TEXT = ("I am 18 or older, and the content involves only fictional, consenting adults "
                "(no minors, no non-consent, no real identifiable people).")


class PolicyError(PermissionError):
    pass


def check(preset: dict, params: dict, *, allowed: bool, confirmed: bool) -> dict:
    """Raise PolicyError when the run is not allowed; return the (possibly amended) params."""
    pol = preset.get("content_policy") or {}
    if not pol.get("adult"):
        return params
    if not allowed:
        raise PolicyError(f"preset {preset.get('id')} is adult-only and disabled; "
                          f"set general.allow_adult_content: true in your settings to enable it")
    if not confirmed:
        raise PolicyError("adult-only preset: confirm the age/consent statement to run it")
    text = " ".join(str(params.get(k, "")) for k in ("prompt", "system"))
    for rx, why in ((MINORS, "minors"), (NON_CONSENT, "non-consent or abuse"),
                    (REAL_PEOPLE, "real, identifiable people")):
        m = rx.search(text)
        if m:
            raise PolicyError(f"prompt refused by the content policy ({why}: {m.group(0)!r})")
    out = dict(params)
    if "negative" in (preset.get("input_schema") or {}).get("properties", {}) or preset.get("kind") == "image":
        neg = str(out.get("negative") or "")
        out["negative"] = (neg + ", " if neg else "") + ADULT_NEGATIVE
    return out
