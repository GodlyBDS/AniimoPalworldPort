"""Reference semantics for rare-variant selection.

This is the *specification and test oracle* for the in-game spawn logic, which
must ultimately run inside Palworld (Blueprint/UE4SS); Python never runs there.

Rules (from the project requirements):
* One spawn event produces exactly one individual and exactly one variant.
* Shiny and Prismana have independently configurable probabilities.
* A rare outcome *replaces* the normal outcome for that individual; it is never
  an extra spawn alongside a normal copy.

Implementation: a single uniform draw ``u`` in [0, 1) is partitioned into
consecutive intervals, so outcomes are mutually exclusive by construction:
``[0, p_prismana)`` -> prismana, ``[p_prismana, p_prismana + p_shiny)`` -> shiny,
otherwise normal.
"""

from __future__ import annotations

import random
from typing import Any

RARE_ORDER = ("prismana", "shiny")  # rarest first; order only affects which slice of u maps where


def variant_probabilities(variants: dict[str, Any]) -> dict[str, float]:
    """Effective per-spawn probabilities for normal/shiny/prismana (sums to 1)."""
    probs: dict[str, float] = {}
    for name in RARE_ORDER:
        v = variants.get(name) or {}
        probs[name] = float(v.get("spawn_probability", 0.0)) if v.get("enabled") else 0.0
        if not 0.0 <= probs[name] <= 1.0:
            raise ValueError(f"{name}.spawn_probability must be within [0, 1]")
    rare_total = sum(probs.values())
    if rare_total > 1.0:
        raise ValueError("combined rare-variant probability exceeds 1")
    probs["normal"] = 1.0 - rare_total
    return probs


def roll_variant(variants: dict[str, Any], u: float | None = None, rng: random.Random | None = None) -> str:
    """Pick the variant for ONE spawn event using a single uniform draw."""
    probs = variant_probabilities(variants)
    if u is None:
        u = (rng or random).random()
    if not 0.0 <= u < 1.0:
        raise ValueError("u must be in [0, 1)")
    edge = 0.0
    for name in RARE_ORDER:
        edge += probs[name]
        if u < edge:
            return name
    return "normal"
