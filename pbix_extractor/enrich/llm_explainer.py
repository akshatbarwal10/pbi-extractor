"""
Plain-language explanations for DAX measures.

What makes these explanations worth having is not the model doing the
writing — it is the context this tool can hand it, which a
model-only catalogue cannot:

  - the DAX of every measure in the dependency chain, so `[Margin %]`
    is not explained blind against an unknown `[Total Sales]`
  - **where the measure is actually used** — "Executive Summary /
    Margin % card". A page name is a business-context signal, and it is
    the difference between "divides one measure by another" and "the
    exec-level profitability KPI on the summary page"
  - the format string, which silently confirms intent (`0.0%` means the
    author meant a ratio), plus display folder and any author
    description

Explanations are produced in **dependency order** — base measures
first, their explanations then passed as context for the measures built
on top of them. Quality improves noticeably, and the ordering is free
because the dependency graph already exists.

Transport is an OpenAI-compatible `/chat/completions` endpoint, spoken
over stdlib `urllib` so the package keeps its no-dependency property.
That one shape covers OpenAI, Azure OpenAI, Groq, OpenRouter, Together,
and a local Ollama or LM Studio — the only difference is `base_url`.

On sending model metadata to a third party: this is off unless asked
for, and when it runs it sends measure names, table and column names,
and DAX expressions. For client work where that is not acceptable,
point `base_url` at a local runtime; nothing else changes. There is
deliberately no "redaction mode" here, because stripping table names
while still shipping the DAX would offer reassurance rather than
protection.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request

# Bump when the prompt changes materially — it is part of the cache key,
# so a reworded prompt re-explains rather than serving stale text.
PROMPT_VERSION = "v1"

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_CACHE = os.path.join(
    os.path.expanduser("~"), ".cache", "pbix-extractor", "explanations.json"
)

SYSTEM_PROMPT = (
    "You are a Power BI semantic model analyst. You explain DAX measures to a "
    "mixed audience of business stakeholders and BI developers.\n\n"
    "Rules:\n"
    "- Describe only what the DAX actually does. Never invent behaviour.\n"
    "- The summary must avoid DAX function names entirely and read as business "
    "language.\n"
    "- Do not restate the formula in words ('sums the Amount column' is "
    "useless). Say what the number means and what question it answers.\n"
    "- Flag genuine risks in caveats: unguarded division, reliance on an "
    "inactive relationship, filter-context assumptions, hardcoded values, "
    "circular references.\n"
    "- If a measure looks like leftover or duplicated logic, say so plainly.\n"
    "- Return valid JSON only, with no markdown fencing."
)


class ExplainerError(Exception):
    pass


# ---------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------

class RateLimiter:
    """Client-side requests-per-minute cap.

    Gemini's free tier allows a small number of requests per minute, and
    exceeding it returns 429s that cost more time than waiting would
    have. A sliding window over recent request times keeps the batch
    loop just under the limit instead of retrying into it.
    """

    def __init__(self, per_minute: int = 0):
        self.per_minute = max(0, int(per_minute or 0))
        self._times: list[float] = []

    def wait(self) -> float:
        """Block until another request is allowed. Returns seconds waited."""
        if not self.per_minute:
            return 0.0

        now = time.monotonic()
        window_start = now - 60.0
        self._times = [t for t in self._times if t > window_start]

        waited = 0.0
        if len(self._times) >= self.per_minute:
            # Wait until the oldest call in the window ages out.
            sleep_for = 60.0 - (now - self._times[0]) + 0.25
            if sleep_for > 0:
                time.sleep(sleep_for)
                waited = sleep_for
                now = time.monotonic()
                self._times = [t for t in self._times if t > now - 60.0]

        self._times.append(now)
        return waited


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

class LLMConfig:
    """Connection settings for an OpenAI-compatible or Gemini endpoint.

    Two providers, because Google's native API is worth the extra branch:
    it accepts `responseMimeType: application/json`, which makes the
    model return parseable JSON rather than JSON wrapped in prose that
    then has to be salvaged.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: int = 90,
        max_retries: int = 2,
        batch_size: int = 8,
        temperature: float = 0.2,
        provider: str = "openai",
        rpm: int = 0,
    ):
        self.provider = (provider or "openai").lower()
        self.api_key = api_key or os.environ.get("PBIX_LLM_API_KEY") \
            or os.environ.get("OPENAI_API_KEY") or ""
        self.base_url = (base_url or os.environ.get("PBIX_LLM_BASE_URL")
                         or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or os.environ.get("PBIX_LLM_MODEL") or DEFAULT_MODEL
        self.timeout = timeout
        self.max_retries = max_retries
        self.batch_size = max(1, batch_size)
        self.temperature = temperature
        self.rpm = max(0, int(rpm or 0))
        self.limiter = RateLimiter(self.rpm)

    @classmethod
    def from_env(cls, **overrides) -> "LLMConfig":
        """Build from `.env` / environment — the normal path.

        Keeps provider choice out of the UI: whoever deploys this sets
        it once, and everyone else just gets explanations.
        """
        from ..config import llm_settings
        settings = llm_settings()
        settings.update(overrides)
        return cls(
            api_key=settings.get("api_key"),
            base_url=settings.get("base_url"),
            model=settings.get("model"),
            provider=settings.get("provider", "openai"),
            rpm=settings.get("rpm", 0),
            batch_size=settings.get("batch_size", 8),
            **{k: v for k, v in overrides.items()
               if k in ("timeout", "max_retries", "temperature")},
        )

    @property
    def endpoint(self) -> str:
        if self.provider == "gemini":
            return f"{self.base_url}/models/{self.model}:generateContent"
        return f"{self.base_url}/chat/completions"

    def describe(self) -> str:
        limit = f", {self.rpm}/min" if self.rpm else ""
        return f"{self.model} via {self.provider}{limit}"


# ---------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------

class ExplanationCache:
    """Disk cache keyed on the DAX itself, so re-runs cost nothing.

    Keyed on expression + model + prompt version rather than measure
    name: renaming a measure should not invalidate, and editing its DAX
    must.
    """

    def __init__(self, path: str | None = DEFAULT_CACHE):
        self.path = path
        self._data: dict = {}
        self._dirty = False
        self._load()

    def _load(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        except (OSError, json.JSONDecodeError):
            self._data = {}

    @staticmethod
    def key(expression: str, model: str) -> str:
        raw = f"{PROMPT_VERSION}|{model}|{expression or ''}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def get(self, expression: str, model: str):
        return self._data.get(self.key(expression, model))

    def put(self, expression: str, model: str, value: dict):
        self._data[self.key(expression, model)] = value
        self._dirty = True

    def save(self):
        if not self.path or not self._dirty:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._data, f)
            self._dirty = False
        except OSError:
            pass  # cache is an optimisation, never load-bearing


# ---------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------

def _build_request(config: LLMConfig, system: str, user: str) -> tuple[dict, dict]:
    """Return (body, headers) in whichever dialect the provider speaks."""
    if config.provider == "gemini":
        headers = {"Content-Type": "application/json"}
        if config.api_key:
            headers["x-goog-api-key"] = config.api_key
        body = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": config.temperature,
                # Ask the API itself to guarantee JSON rather than hoping
                # the model does not wrap it in prose.
                "responseMimeType": "application/json",
            },
        }
        return body, headers

    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    body = {
        "model": config.model,
        "temperature": config.temperature,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    return body, headers


def _post(config: LLMConfig, payload: dict, headers: dict | None = None) -> dict:
    body = json.dumps(payload).encode("utf-8")
    if headers is None:
        headers = {"Content-Type": "application/json"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"

    last_error = None
    for attempt in range(config.max_retries + 1):
        # Stay under the provider's per-minute allowance rather than
        # discovering it through 429s.
        config.limiter.wait()

        request = urllib.request.Request(
            config.endpoint, data=body, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=config.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")[:400]
            except Exception:  # noqa: BLE001
                pass
            last_error = ExplainerError(f"HTTP {e.code} from {config.base_url}: {detail}")
            # Rate limit and server errors are worth retrying; 4xx is not.
            if e.code not in (408, 409, 429) and e.code < 500:
                raise last_error
            if e.code == 429:
                # Back off harder than a transient server error: the
                # window has to actually clear before retrying helps.
                time.sleep(min(20.0, 5.0 * (attempt + 1)))
                continue
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last_error = ExplainerError(f"Could not reach {config.base_url}: {e}")

        if attempt < config.max_retries:
            time.sleep(1.5 * (attempt + 1))

    raise last_error or ExplainerError("LLM request failed")


def _content(response: dict, provider: str = "openai") -> str:
    if provider == "gemini":
        candidates = (response or {}).get("candidates") or []
        if candidates:
            candidate = candidates[0] or {}
            parts = (candidate.get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
            if text.strip():
                return text
            # No text: the response was blocked, truncated, or empty.
            # Say which, rather than letting an empty string fall through
            # to a misleading "did not return parseable JSON".
            reason = candidate.get("finishReason") or "unknown"
            raise ExplainerError(
                f"Gemini returned no text (finishReason={reason}). "
                + ("The response hit the output token limit; try a smaller "
                   "PBIX_LLM_BATCH_SIZE." if reason == "MAX_TOKENS" else
                   "The prompt or response may have been filtered.")
            )

        feedback = (response or {}).get("promptFeedback") or {}
        blocked = feedback.get("blockReason")
        raise ExplainerError(
            f"Gemini returned no candidates"
            + (f" (blockReason={blocked})" if blocked else "")
        )

    try:
        return response["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as e:
        raise ExplainerError(f"Unexpected response shape: {e}") from e


def _parse_json(text: str) -> dict:
    """Parse model output, tolerating markdown fencing and stray prose."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text.strip("`")
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
    raise ExplainerError("Model did not return parseable JSON")


# ---------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------

def _model_summary(catalogue: dict, max_tables: int = 40) -> str:
    """A compact schema sketch so the model knows the vocabulary."""
    lines = []
    for table in catalogue["model"]["tables"][:max_tables]:
        columns = [c["name"] for c in table["columns"] if not c.get("is_hidden")][:14]
        suffix = ", ..." if len(table["columns"]) > 14 else ""
        lines.append(f"- {table['name']}: {', '.join(columns)}{suffix}")

    relationships = [
        f"- {r['from_table']}[{r['from_column']}] -> {r['to_table']}[{r['to_column']}]"
        f"{'' if r['is_active'] else '  (INACTIVE)'}"
        for r in catalogue["model"]["relationships"][:40]
    ]

    out = ["Tables and columns:", *lines]
    if relationships:
        out += ["", "Relationships:", *relationships]
    return "\n".join(out)


def _usage_context(measure: dict) -> str:
    places = measure.get("used_in") or []
    if not places:
        return "Not used by any visual in this report."
    shown = places[:6]
    rendered = "; ".join(
        f"{p.get('page')} / {p.get('visual')} ({p.get('visual_type')})" for p in shown
    )
    more = f" (+{len(places) - len(shown)} more)" if len(places) > len(shown) else ""
    return f"Used on: {rendered}{more}"


def _measure_block(measure: dict, by_name: dict, explained: dict) -> str:
    """One measure's full context, including its dependency chain."""
    analysis = measure.get("analysis", {})
    parts = [
        f"### Measure: {measure['name']}",
        f"Home table: {measure.get('table')}",
        f"DAX:\n{measure.get('expression', '')}",
    ]
    if measure.get("format_string"):
        parts.append(f"Format string: {measure['format_string']}")
    if measure.get("display_folder"):
        parts.append(f"Display folder: {measure['display_folder']}")
    if measure.get("description"):
        parts.append(f"Author description: {measure['description']}")
    parts.append(_usage_context(measure))

    deps = analysis.get("references", {}).get("measures", []) or []
    dep_lines = []
    for dep in deps:
        source = by_name.get(dep)
        if not source:
            continue
        # Prefer an explanation already produced this run — cheaper and
        # more informative than re-sending raw DAX.
        prior = explained.get(dep, {}).get("summary")
        if prior:
            dep_lines.append(f"- [{dep}] = {source.get('expression','')}  ({prior})")
        else:
            dep_lines.append(f"- [{dep}] = {source.get('expression','')}")
    if dep_lines:
        parts.append("Depends on these measures:\n" + "\n".join(dep_lines))

    if analysis.get("confidence") in ("medium", "low"):
        parts.append(
            f"Note: static analysis confidence is {analysis['confidence']} "
            f"({'; '.join(analysis.get('notes', []))}). Resolve the real "
            "dependencies yourself from the DAX."
        )
    return "\n".join(parts)


def _dependency_levels(measures: list[dict], graph: dict) -> list[list[dict]]:
    """Group measures into dependency levels.

    Level 0 is measures that depend on no other measure; level N depends
    only on levels below it. Batching *within* a level rather than
    across the flat order guarantees a measure's dependencies were
    explained in an **earlier request**, so their summaries are
    available as context by the time the dependant is explained. A flat
    ordering does not guarantee that — a measure and its dependency can
    land in the same batch, where neither has been explained yet.

    Measures in a dependency cycle are placed at level 0: there is no
    correct order for them, and the circular reference is already
    reported as an error finding.
    """
    by_name = {m["name"]: m for m in measures}
    edges = graph.get("edges", {})
    depth: dict[str, int] = {}
    visiting: set[str] = set()

    def resolve(name: str) -> int:
        if name in depth:
            return depth[name]
        if name in visiting:
            return 0  # cycle
        visiting.add(name)
        deps = [d for d in edges.get(name, []) if d in by_name and d != name]
        depth[name] = 1 + max((resolve(d) for d in deps), default=-1)
        visiting.discard(name)
        return depth[name]

    for measure in measures:
        resolve(measure["name"])

    levels: dict[int, list[dict]] = {}
    for measure in measures:
        levels.setdefault(depth.get(measure["name"], 0), []).append(measure)
    return [levels[k] for k in sorted(levels)]


# ---------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------

def explain_measures(
    catalogue: dict,
    config: LLMConfig | None = None,
    cache: ExplanationCache | None = None,
    only_used: bool = False,
    progress=None,
) -> dict:  # noqa: D401
    """Attach plain-language explanations to every measure in-place.

    Returns a small report: how many were explained, served from cache,
    or failed. Failure is never fatal — a measure simply keeps its
    static analysis and gains no explanation, because a catalogue that
    refuses to build because an API was down would be worse than one
    with a few blanks.
    """
    config = config or LLMConfig.from_env()
    cache = cache if cache is not None else ExplanationCache()

    model = catalogue.get("model", {})
    if not model.get("available"):
        return {"explained": 0, "cached": 0, "failed": 0,
                "skipped": 0, "errors": ["Model layer unavailable."]}

    measures = list(model.get("measures", []))
    if only_used:
        measures = [m for m in measures if m.get("usage_count", 0) > 0]
    if not measures:
        return {"explained": 0, "cached": 0, "failed": 0, "skipped": 0, "errors": []}

    by_name = {m["name"]: m for m in model.get("measures", [])}
    levels = _dependency_levels(measures, catalogue.get("dax_dependency_graph", {}))

    explained: dict[str, dict] = {}
    stats = {"explained": 0, "cached": 0, "failed": 0,
             "skipped": 0, "errors": [], "model": config.describe()}

    # Serve whatever the cache already holds before spending any calls,
    # keeping level structure so ordering still holds for the remainder.
    pending_levels: list[list[dict]] = []
    for level in levels:
        remaining = []
        for measure in level:
            hit = cache.get(measure.get("expression", ""), config.model)
            if hit:
                measure["explanation"] = dict(hit, cached=True)
                explained[measure["name"]] = hit
                stats["cached"] += 1
            else:
                remaining.append(measure)
        if remaining:
            pending_levels.append(remaining)

    if not pending_levels:
        cache.save()
        return stats

    # Batches never span a level, so every dependency is explained by an
    # earlier request than the measures that build on it.
    batches: list[list[dict]] = []
    for level in pending_levels:
        for index in range(0, len(level), config.batch_size):
            batches.append(level[index:index + config.batch_size])

    schema = _model_summary(catalogue)
    total_batches = len(batches)

    for batch_index, batch in enumerate(batches, start=1):
        if progress:
            progress(batch_index, total_batches, [m["name"] for m in batch])

        blocks = [_measure_block(m, by_name, explained) for m in batch]
        names = [m["name"] for m in batch]

        user_prompt = (
            f"Semantic model context:\n{schema}\n\n"
            f"Explain each of the following {len(batch)} measure(s).\n\n"
            + "\n\n".join(blocks)
            + "\n\nReturn a JSON object whose keys are exactly these measure "
              f"names: {json.dumps(names)}\n"
              "Each value must be an object with:\n"
              '  "summary": one sentence in business language, no DAX function names\n'
              '  "detail": 2-3 sentences on what it computes and what filter '
              'context it assumes\n'
              '  "caveats": array of strings for real risks; [] if none\n'
              '  "dependencies": array of measure names it actually references, '
              "including any resolved through VAR\n"
        )

        try:
            body, headers = _build_request(config, SYSTEM_PROMPT, user_prompt)
            response = _post(config, body, headers)
            parsed = _parse_json(_content(response, config.provider))
        except ExplainerError as e:
            stats["failed"] += len(batch)
            message = str(e)
            if message not in stats["errors"]:
                stats["errors"].append(message)
            continue

        for measure in batch:
            entry = parsed.get(measure["name"])
            if not isinstance(entry, dict):
                stats["failed"] += 1
                continue
            explanation = {
                "summary": str(entry.get("summary", "")).strip(),
                "detail": str(entry.get("detail", "")).strip(),
                "caveats": [str(c) for c in (entry.get("caveats") or []) if str(c).strip()],
                "dependencies": [str(d) for d in (entry.get("dependencies") or [])],
                "generated_by": config.model,
            }
            # Where static analysis was unsure, record what the model
            # resolved that the lexer could not — a review queue rather
            # than a silent overwrite of the regex result.
            static_deps = set(
                measure.get("analysis", {}).get("references", {}).get("measures", []) or []
            )
            llm_deps = {d for d in explanation["dependencies"] if d in by_name}
            missed = sorted(llm_deps - static_deps)
            if missed and measure.get("analysis", {}).get("confidence") != "high":
                explanation["dependencies_missed_by_static_analysis"] = missed

            measure["explanation"] = explanation
            explained[measure["name"]] = explanation
            cache.put(measure.get("expression", ""), config.model, explanation)
            stats["explained"] += 1

    cache.save()
    return stats


def explanation_coverage(catalogue: dict) -> dict:
    """How much of the model carries an explanation."""
    measures = catalogue.get("model", {}).get("measures", []) or []
    with_explanation = [m for m in measures if m.get("explanation", {}).get("summary")]
    return {
        "total_measures": len(measures),
        "explained": len(with_explanation),
        "percent": round(100 * len(with_explanation) / len(measures), 1) if measures else 0.0,
    }