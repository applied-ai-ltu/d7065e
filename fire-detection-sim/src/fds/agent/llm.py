"""LiteLLM-compatible client for the fire detection agent.

Routes through LiteLLM which handles model selection.
Instrumented with OpenTelemetry spans for Phoenix tracing.
"""

import json
import logging
import time

import httpx

logger = logging.getLogger(__name__)


def _get_tracer():
    """Get tracer lazily so it picks up the provider set by setup_phoenix."""
    try:
        from opentelemetry import trace
        return trace.get_tracer("fds.agent.llm")
    except ImportError:
        return None


class LLMClient:

    def __init__(self, base_url="https://localhost:30400", api_key="", model="qwen2.5-14b"):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=120.0,
            verify=False,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    async def close(self):
        await self._client.aclose()

    async def chat(self, messages, temperature=0.3):
        """OpenAI-compatible chat completion with tracing."""
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }

        tracer = _get_tracer()
        if tracer:
            with tracer.start_as_current_span(
                "llm.chat",
                attributes={
                    "llm.model": self.model,
                    "llm.temperature": temperature,
                    "llm.message_count": len(messages),
                    "llm.system_prompt": messages[0]["content"][:200] if messages and messages[0]["role"] == "system" else "",
                },
            ) as span:
                start = time.time()
                r = await self._client.post("/v1/chat/completions", json=body)
                r.raise_for_status()
                data = r.json()
                response_text = data["choices"][0]["message"]["content"]
                elapsed = time.time() - start

                span.set_attribute("llm.latency_ms", int(elapsed * 1000))
                span.set_attribute("llm.response_length", len(response_text))
                span.set_attribute("llm.status", "success")

                usage = data.get("usage", {})
                if usage:
                    span.set_attribute("llm.prompt_tokens", usage.get("prompt_tokens", 0))
                    span.set_attribute("llm.completion_tokens", usage.get("completion_tokens", 0))
                    span.set_attribute("llm.total_tokens", usage.get("total_tokens", 0))

                return response_text
        else:
            r = await self._client.post("/v1/chat/completions", json=body)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]

    async def generate(self, prompt, system=None, temperature=0.3):
        """Convenience wrapper using chat format."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return await self.chat(messages, temperature=temperature)

    async def generate_json(self, prompt, system=None):
        """Generate and parse JSON response."""
        if system:
            system += "\nRespond ONLY with valid JSON. No markdown, no explanation."
        else:
            system = "Respond ONLY with valid JSON. No markdown, no explanation."

        response = await self.generate(prompt, system=system)

        # Strip markdown fences if present
        response = response.strip()
        if response.startswith("```"):
            lines = response.split("\n")
            response = "\n".join(lines[1:])
        if response.endswith("```"):
            response = response[:-3]
        response = response.strip()
        if response.startswith("json"):
            response = response[4:].strip()

        return json.loads(response)
