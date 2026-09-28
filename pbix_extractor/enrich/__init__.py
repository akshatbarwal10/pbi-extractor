"""Optional enrichment layers over the extracted catalogue.

Everything under `enrich/` is additive: the core extraction never
depends on it, so the package keeps working with no network, no API key
and no third-party libraries.
"""
from .llm_explainer import (  # noqa: F401
    ExplainerError,
    ExplanationCache,
    LLMConfig,
    explain_measures,
    explanation_coverage,
)