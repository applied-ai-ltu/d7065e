"""LiteLLM-compatible client for the fire detection agent.

Routes through LiteLLM which handles model selection via Semantic Router.
"""

import json
import logging
import ssl

import httpx

logger = logging.getLogger(__name__)


class LLMClient:

    def __init__(self, base_url="https://192.168.68.67:30400", api_key="", model="gemma4:12b"):
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
        """OpenAI-compatible chat completion."""
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
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
