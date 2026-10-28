"""LLM access for the AI steps. Three providers, chosen by LLM_PROVIDER:

* anthropic - Claude through the official SDK (paid per use).
* ollama    - a local model through Ollama's HTTP API (free to run).
* fake      - deterministic offline stand-in for development and tests. It never
              produces anything worth sending; production refuses to start with it.

Every call asks for JSON matching a schema, so callers get a dict back.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Protocol

import httpx

from growthstack.config import Settings, get_settings

log = logging.getLogger(__name__)


class LLMError(Exception):
    pass


class LLMRefusal(LLMError):
    pass


class LLM(Protocol):
    name: str

    def json(self, *, system: str, prompt: str, schema: dict) -> dict: ...


class AnthropicLLM:
    name = "anthropic"

    def __init__(self, model: str, effort: str = "medium", api_key: str | None = None, client=None):
        import anthropic

        self._anthropic = anthropic
        self.client = client or (anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic())
        self.model = model
        self.effort = effort

    def json(self, *, system: str, prompt: str, schema: dict) -> dict:
        a = self._anthropic
        try:
            # Server-side fallback: if the model declines on a safety classifier,
            # the API retries on a fallback model inside the same call.
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except a.RateLimitError as exc:
            raise LLMError(f"Claude rate limited: {exc.message}") from exc
        except a.APIStatusError as exc:
            raise LLMError(f"Claude API error {exc.status_code}: {exc.message}") from exc
        except a.APIConnectionError as exc:
            raise LLMError(f"could not reach Claude API: {exc}") from exc

        if response.stop_reason == "refusal":
            category = getattr(response.stop_details, "category", None) if response.stop_details else None
            raise LLMRefusal(f"model declined (category={category})")
        if response.stop_reason == "max_tokens":
            raise LLMError("response hit max_tokens before finishing")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise LLMError("no text block in response")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError("model returned invalid JSON") from exc


class OllamaLLM:
    name = "ollama"

    def __init__(self, base_url: str, model: str, http: httpx.Client | None = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.http = http or httpx.Client(timeout=300)

    def json(self, *, system: str, prompt: str, schema: dict) -> dict:
        try:
            resp = self.http.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": self.model,
                    "stream": False,
                    "format": schema,  # Ollama structured outputs
                    "options": {"temperature": 0.3},
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
            return json.loads(resp.json()["message"]["content"])
        except (httpx.HTTPError, KeyError, json.JSONDecodeError) as exc:
            raise LLMError(f"Ollama call failed: {exc}") from exc


class FakeLLM:
    """Offline, deterministic stand-in. Recognises which task it is from the schema."""

    name = "fake"

    def json(self, *, system: str, prompt: str, schema: dict) -> dict:
        props = schema.get("properties", {})
        website = _between(prompt, "<website_content>", "</website_content>")
        if "fit_score" in props:
            words = re.findall(r"[A-Za-z][A-Za-z\-]{3,}", website)
            summary = " ".join(website.split()[:40]) or "No website content was available."
            icp = _between(prompt, "<ideal_customer_profile>", "</ideal_customer_profile>").lower()
            overlap = len({w.lower() for w in words} & set(re.findall(r"[a-z\-]{4,}", icp)))
            return {
                "summary": summary,
                "fit_score": min(100, 30 + 15 * overlap) if website else 20,
                "fit_reasons": [f"{overlap} profile keywords found on the website"],
                "personal_hook": (summary[:120] + "...") if website else "",
            }
        if "subject" in props and "body" in props:
            first = _between(prompt, "<first_name>", "</first_name>") or "there"
            company = _between(prompt, "<company>", "</company>") or "your team"
            return {
                "subject": f"Quick question for {company}",
                "body": (
                    f"Hi {first},\n\n[DEV DRAFT - fake LLM] I had a look at {company} and thought our "
                    "product might help. Would a 15-minute call next week be useful?\n\nThanks"
                ),
            }
        raise LLMError("FakeLLM does not know this schema")


def _between(text: str, start: str, end: str) -> str:
    i = text.find(start)
    j = text.find(end, i + len(start)) if i >= 0 else -1
    return text[i + len(start) : j].strip() if i >= 0 and j >= 0 else ""


def get_llm(settings: Settings | None = None) -> LLM:
    settings = settings or get_settings()
    provider = settings.llm_provider.lower()
    if provider == "anthropic":
        key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
        return AnthropicLLM(settings.llm_model, settings.llm_effort, api_key=key)
    if provider == "ollama":
        return OllamaLLM(settings.ollama_url, settings.ollama_model)
    if provider == "fake":
        return FakeLLM()
    raise LLMError(f"unknown LLM_PROVIDER {settings.llm_provider!r}")
