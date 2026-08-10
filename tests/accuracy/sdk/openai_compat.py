"""Small compatibility shims for differences between OpenAI model families."""

from __future__ import annotations

# GPT-5* and the listed o-series models are reasoning models: they reject any non-default
# ``temperature`` with a 400 (only the default of 1 is accepted). Classic chat
# models (gpt-4o, gpt-4.1, ...) accept a custom value.
_FIXED_TEMPERATURE_PREFIXES = ("gpt-5", "o1", "o3", "o4")


def supports_custom_temperature(model: str) -> bool:
    """Whether ``model`` accepts a caller-supplied ``temperature``.

    Reasoning models only allow the default; callers should omit ``temperature``
    for them rather than send ``0.0`` and get a 400. A leading gateway prefix
    (e.g. ``openai/gpt-5.5``) is ignored.
    """
    name = model.lower().rsplit("/", 1)[-1]
    return not name.startswith(_FIXED_TEMPERATURE_PREFIXES)
