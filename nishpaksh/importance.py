"""Front-page rank of a story: its rating (priority.py: the 1-5 rubric and the filler test, one call per
20 stories from their headlines) plus coverage breadth. Many outlets independently choosing to cover
something is itself a signal of importance. The per-story rating call that lived here was removed
(Oct 8 2026): it repeated priority.py's and was re-asked whenever a draft headline changed."""
from __future__ import annotations


def rank(score: int, independent_sources: int, languages: int) -> float:
    """Model score plus coverage: up to +1.5 for 6+ independent outlets, +0.5 if covered in both
    English and Hindi."""
    return round(score + 0.25 * min(independent_sources, 6) + (0.5 if languages >= 2 else 0), 2)
