"""Shared phrase matching: technical practice is not a sports outing."""
import re


def phrase(text, words):
    return any(re.search(r"(?<!\w)" + re.escape(w) + r"(?!\w)", text, re.I) for w in words)


def technical_activity(text):
    return bool(re.search(r"\b(?:code|coding|python|solver|algorithm|debug|dataset|software|machine learning|neural|model training|training (?:a |the )?model|run (?:a |the )?(?:tests?|code|script|example)|memory pool|thread pool|connection pool)\b", text, re.I))


def activity_family(text, families):
    low = str(text or "").lower()
    if technical_activity(low): return None
    for family, words in families:
        if family == "training":
            # Generic practice/training is ambiguous, even without a technical word.
            words = ("sports training", "athletic training", "team training", "training venue", "training session at the gym")
        if phrase(low, words): return family
    return None
