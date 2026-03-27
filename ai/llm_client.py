from __future__ import annotations

import json
import logging
import re
import time
from threading import Lock
from typing import Any

from openai import OpenAI

from config import AppConfig

logger = logging.getLogger(__name__)


class LLMClient:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._clients = [
            OpenAI(api_key=api_key, base_url=config.llm_base_url, timeout=config.llm_timeout_seconds, max_retries=0)
            for api_key in config.llm_api_keys
        ]
        self._next_client_index = 0
        self._client_lock = Lock()
        self._disabled_reason: str | None = None

    @property
    def available(self) -> bool:
        return bool(self._clients) and self._disabled_reason is None

    def reserve_client(self) -> int:
        client_index, _ = self._next_client()
        return client_index

    def generate_json(
        self,
        system_prompt: str,
        user_prompt: str,
        client_index: int | None = None,
    ) -> dict[str, Any]:
        return self._run_with_retries(system_prompt, user_prompt, self._extract_json, client_index=client_index)

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any]:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, flags=re.DOTALL)
            if not match:
                raise
            return json.loads(match.group(0))

    def generate_text(self, system_prompt: str, user_prompt: str, client_index: int | None = None) -> str:
        return self._run_with_retries(system_prompt, user_prompt, lambda text: text, client_index=client_index)

    def _run_with_retries(
        self,
        system_prompt: str,
        user_prompt: str,
        response_parser,
        client_index: int | None = None,
    ) -> Any:
        if not self._clients:
            raise RuntimeError("No LLM API key is configured. Set LLM_API_KEYS, OPENAI_API_KEYS, GROQ_API_KEYS, or a single-key equivalent.")
        if self._disabled_reason is not None:
            raise RuntimeError(f"LLM client disabled for this run: {self._disabled_reason}")

        delay_seconds = 1.0
        last_error: Exception | None = None
        for attempt in range(1, self.config.llm_retries + 1):
            active_client_index, client = self._get_client(client_index)
            try:
                response = client.responses.create(
                    model=self.config.llm_model,
                    input=[
                        {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
                        {"role": "user", "content": [{"type": "input_text", "text": user_prompt}]},
                    ],
                )
                return response_parser(response.output_text)
            except Exception as exc:  # pragma: no cover
                last_error = exc
                logger.warning(
                    "LLM request failed on attempt %d/%d using key %d/%d: %s",
                    attempt,
                    self.config.llm_retries,
                    active_client_index + 1,
                    len(self._clients),
                    exc,
                )
                if attempt < self.config.llm_retries:
                    time.sleep(delay_seconds)
                    delay_seconds *= 2

        self._disabled_reason = str(last_error) if last_error else "unknown LLM error"
        logger.warning("Disabling LLM client for the rest of this run after repeated failures: %s", self._disabled_reason)
        raise RuntimeError("LLM request failed after retries.") from last_error

    def _next_client(self) -> tuple[int, OpenAI]:
        with self._client_lock:
            client_index = self._next_client_index
            client = self._clients[client_index]
            self._next_client_index = (self._next_client_index + 1) % len(self._clients)
        return client_index, client

    def _get_client(self, client_index: int | None) -> tuple[int, OpenAI]:
        if client_index is None:
            return self._next_client()
        if client_index < 0 or client_index >= len(self._clients):
            raise IndexError(f"LLM client index out of range: {client_index}")
        return client_index, self._clients[client_index]
