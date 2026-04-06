from __future__ import annotations

import json
import time
from dataclasses import dataclass
from urllib import error, request


@dataclass
class OllamaOptions:
    temperature: float = 0.2
    num_predict: int = 2048


class OllamaClient:
    def __init__(self, base_url: str, model: str, timeout_seconds: int = 120) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds

    def generate(
        self,
        prompt: str,
        *,
        system: str = "",
        options: OllamaOptions | None = None,
        json_mode: bool = False,
        retries: int = 3,
    ) -> str:
        opts = options or OllamaOptions()
        payload = {
            "model": self.model,
            "prompt": prompt,
            "system": system,
            "stream": False,
            "options": {
                "temperature": opts.temperature,
                "num_predict": opts.num_predict,
            },
        }
        if json_mode:
            payload["format"] = "json"
        data = json.dumps(payload).encode("utf-8")
        endpoint = f"{self.base_url}/api/generate"

        attempt = 0
        while True:
            attempt += 1
            try:
                req = request.Request(
                    endpoint,
                    data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with request.urlopen(req, timeout=self.timeout_seconds) as response:
                    raw = response.read().decode("utf-8")
                parsed = json.loads(raw)
                response_text = parsed.get("response", "")
                thinking_text = parsed.get("thinking", "")

                if isinstance(thinking_text, str) and thinking_text.strip():
                    print(
                        f"\n[OLLAMA:{self.model}:thinking]\n",
                        flush=True,
                    )
                    for line in thinking_text.strip().split("\n"):
                        print(f"  {line}", flush=True)
                if isinstance(response_text, str) and response_text.strip():
                    print(
                        f"\n[OLLAMA:{self.model}:response]\n",
                        flush=True,
                    )
                    for line in response_text.strip().split("\n"):
                        print(f"  {line}", flush=True)

                text = response_text
                if (not isinstance(text, str)) or (not text.strip()):
                    text = thinking_text
                if not isinstance(text, str):
                    raise RuntimeError("Ollama response did not include text.")
                return text.strip()
            except (error.URLError, error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
                if attempt >= retries:
                    raise RuntimeError(f"Ollama request failed after {retries} attempts: {exc}") from exc
                time.sleep(min(2 ** attempt, 8))
