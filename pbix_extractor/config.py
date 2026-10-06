"""
Configuration, read from a `.env` file beside the project.

The LLM endpoint is an operator decision, not an end-user one: whoever
deploys this picks the provider, model and key once, and the people
using the UI just get explanations with their results. So there is no
provider UI — everything comes from here.

Precedence, highest first:
  1. real environment variables
  2. a `.env` file at the project root
  3. the defaults below

Parsed with the standard library rather than python-dotenv, to keep the
package installable with nothing but the exports' dependencies.

Recognised keys:

    GEMINI_API_KEY      turns on explanations, via Google's API
    GEMINI_MODEL        default: gemini-flash-lite-latest
    GEMINI_RPM          requests per minute to stay under (default 15)

    # or any OpenAI-compatible endpoint instead
    PBIX_LLM_API_KEY
    PBIX_LLM_BASE_URL   e.g. http://localhost:11434/v1 for local Ollama
    PBIX_LLM_MODEL

    PBIX_LLM_BATCH_SIZE measures per request (default 8)
    PBIX_EXPLAIN        set to 0/false to disable explanations entirely

    # optional: recovering a .pbix's model via a local pbi-tools install
    # (see pbix_converter.py). Off by default only in the sense that it
    # requires pbi-tools to actually be present on this machine.
    PBIX_PBITOOLS_PATH     full path to pbi-tools(.exe), if not on PATH
    PBIX_PBITOOLS_TIMEOUT  seconds to allow the decompile (default 300)
    PBIX_PBITOOLS_ENABLED  set to 0/false to disable even if installed
"""
from __future__ import annotations

import os
from functools import lru_cache

DEFAULT_GEMINI_MODEL = "gemini-flash-lite-latest"
DEFAULT_GEMINI_RPM = 15
DEFAULT_BATCH_SIZE = 8

FALSEY = {"0", "false", "no", "off", ""}


def _project_root() -> str:
    # pbix_extractor/config.py -> pbix_extractor -> project root
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def parse_env_file(path: str) -> dict[str, str]:
    """Minimal `.env` parser: KEY=value, `#` comments, optional quotes."""
    values: dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            lines = f.readlines()
    except OSError:
        return values

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Strip matching quotes; only then drop a trailing comment, so a
        # `#` inside a quoted value survives.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif "#" in value:
            value = value.split("#", 1)[0].strip()
        if key:
            values[key] = value
    return values


@lru_cache(maxsize=1)
def _dotenv() -> dict[str, str]:
    """Load `.env` once. Real environment variables still win."""
    for candidate in (
        os.environ.get("PBIX_ENV_FILE"),
        os.path.join(os.getcwd(), ".env"),
        os.path.join(_project_root(), ".env"),
    ):
        if candidate and os.path.isfile(candidate):
            return parse_env_file(candidate)
    return {}


def get(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is not None and value != "":
        return value
    value = _dotenv().get(name)
    if value is not None and value != "":
        return value
    return default


def get_int(name: str, default: int) -> int:
    try:
        return int(str(get(name, str(default))).strip())
    except (TypeError, ValueError):
        return default


def reload() -> None:
    """Re-read `.env` — useful after editing it without restarting."""
    _dotenv.cache_clear()


def explanations_enabled() -> bool:
    """True when a key is configured and explanations are not switched off."""
    if str(get("PBIX_EXPLAIN", "1")).strip().lower() in FALSEY:
        return False
    return bool(get("GEMINI_API_KEY") or get("PBIX_LLM_API_KEY")
                or get("PBIX_LLM_BASE_URL"))


def llm_settings() -> dict:
    """Resolve which provider to use and how to talk to it.

    Gemini wins when its key is present, because that is the explicit
    choice; otherwise anything OpenAI-compatible, including a local
    runtime that needs no key at all.
    """
    gemini_key = get("GEMINI_API_KEY")
    if gemini_key:
        return {
            "provider": "gemini",
            "api_key": gemini_key,
            "model": get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL),
            "base_url": get("GEMINI_BASE_URL",
                            "https://generativelanguage.googleapis.com/v1beta"),
            "rpm": get_int("GEMINI_RPM", DEFAULT_GEMINI_RPM),
            "batch_size": get_int("PBIX_LLM_BATCH_SIZE", DEFAULT_BATCH_SIZE),
        }

    return {
        "provider": "openai",
        "api_key": get("PBIX_LLM_API_KEY") or get("OPENAI_API_KEY") or "",
        "model": get("PBIX_LLM_MODEL", "gpt-4o-mini"),
        "base_url": get("PBIX_LLM_BASE_URL", "https://api.openai.com/v1"),
        "rpm": get_int("PBIX_LLM_RPM", 0),  # 0 = no client-side limit
        "batch_size": get_int("PBIX_LLM_BATCH_SIZE", DEFAULT_BATCH_SIZE),
    }


def describe() -> str:
    """One line for a status area — never includes the key."""
    if not explanations_enabled():
        return "explanations off (no API key configured)"
    settings = llm_settings()
    limit = f", {settings['rpm']}/min" if settings["rpm"] else ""
    return f"{settings['model']} via {settings['provider']}{limit}"