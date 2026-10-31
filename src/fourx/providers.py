"""One adapter per provider, each converting our saved messages to and from its SDK.

History is saved in our own format (models.Message), so a conversation doesn't belong
to either provider. Gemini's own raw parts are kept in provider_metadata as well,
because its thought signatures must be sent back exactly as they came.
"""
import json
import os
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any

import groq
import openai
from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from .models import PROVIDER_NAMES, Message, ToolCall, ToolDefinition, ToolResult

PROVIDERS = PROVIDER_NAMES
DEFAULT_PROVIDER = "openai"
TIMEOUT_SECONDS = 60
# Google's documented value for function calls that Gemini didn't produce itself.
FOREIGN_THOUGHT_SIGNATURE = b"skip_thought_signature_validator"

load_dotenv(Path(__file__).resolve().parents[2] / ".env")


class ProviderError(Exception):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def _new_call_id() -> str:
    return f"call_{uuid.uuid4().hex[:12]}"


def _result_payload(result: ToolResult) -> dict[str, Any]:
    return {"status": result["status"], "output": result["output"]}


def _reply(text: str, calls: list[ToolCall], metadata: dict[str, Any]) -> Message:
    message: Message = {"role": "assistant", "content": text, "provider_metadata": metadata}
    if calls:
        message["tool_calls"] = calls
    return message


class ChatAdapter:
    """Groq and OpenAI share one chat-completions format and the same exception names."""

    def __init__(self, name: str, sdk: Any, client_class: Any, api_key: str, model: str):
        self.name, self.sdk, self.model = name, sdk, model
        self.client = client_class(api_key=api_key, timeout=TIMEOUT_SECONDS, max_retries=0)  # runner retries

    @staticmethod
    def to_messages(system: str, messages: list[Message]) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for message in messages:
            if message["role"] == "tool":
                result = message["tool_result"]
                converted.append({"role": "tool", "tool_call_id": result["call_id"],
                                  "content": json.dumps(_result_payload(result), ensure_ascii=False)})
            elif message.get("tool_calls"):
                converted.append({"role": "assistant", "content": message["content"], "tool_calls": [
                    {"id": call["call_id"], "type": "function", "function": {
                        "name": call["name"], "arguments": json.dumps(call["arguments"], ensure_ascii=False)}}
                    for call in message["tool_calls"]]})
            else:
                converted.append({"role": message["role"], "content": message["content"]})
        return converted

    @staticmethod
    def _arguments(raw: str) -> dict[str, Any]:
        try:
            arguments = json.loads(raw or "{}")
        except ValueError:
            arguments = None
        # A malformed call stays visible: tool validation rejects it and tells the model.
        return arguments if isinstance(arguments, dict) else {"_malformed": raw}

    def complete(self, system: str, messages: list[Message], tools: list[ToolDefinition]) -> Message:
        try:
            response = self.client.chat.completions.create(
                model=self.model, messages=self.to_messages(system, messages), tool_choice="auto",
                tools=[{"type": "function", "function": tool} for tool in tools])
        except (self.sdk.RateLimitError, self.sdk.APIConnectionError, self.sdk.InternalServerError) as exc:
            raise ProviderError(str(exc), retryable=True) from exc
        except self.sdk.APIError as exc:
            # tool_use_failed: Groq rejected the model's own badly formed call.
            raise ProviderError(str(exc), retryable="tool_use_failed" in str(exc)) from exc
        reply = response.choices[0].message
        calls = [{"call_id": call.id or _new_call_id(), "name": call.function.name,
                  "arguments": self._arguments(call.function.arguments)} for call in reply.tool_calls or []]
        return _reply(reply.content or "", calls, {"provider": self.name, "model": self.model})


class GeminiAdapter:
    name = "gemini"

    def __init__(self, api_key: str, model: str):
        self.model = model
        self.client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=TIMEOUT_SECONDS * 1000))

    @staticmethod
    def _model_parts(message: Message) -> list[types.Part]:
        metadata = message.get("provider_metadata", {})
        if metadata.get("provider") == "gemini":
            return [types.Part.model_validate(part) for part in metadata["parts"]]
        parts = [types.Part(text=message["content"])] if message["content"] else []
        for index, call in enumerate(message.get("tool_calls", [])):
            parts.append(types.Part(
                function_call=types.FunctionCall(name=call["name"], args=call["arguments"]),
                thought_signature=FOREIGN_THOUGHT_SIGNATURE if index == 0 else None))
        return parts

    @classmethod
    def to_contents(cls, messages: list[Message]) -> list[types.Content]:
        contents: list[types.Content] = []
        previous_role = None
        for message in messages:
            if message["role"] == "user":
                contents.append(types.Content(role="user", parts=[types.Part(text=message["content"])]))
            elif message["role"] == "assistant":
                contents.append(types.Content(role="model", parts=cls._model_parts(message)))
            else:
                result = message["tool_result"]
                part = types.Part(function_response=types.FunctionResponse(
                    name=result["name"], response=_result_payload(result)))
                # Gemini wants all results for one batch in a single turn, in call order.
                if previous_role == "tool":
                    contents[-1].parts.append(part)
                else:
                    contents.append(types.Content(role="user", parts=[part]))
            previous_role = message["role"]
        return contents

    def complete(self, system: str, messages: list[Message], tools: list[ToolDefinition]) -> Message:
        config = types.GenerateContentConfig(system_instruction=system, tools=[types.Tool(function_declarations=[
            types.FunctionDeclaration(name=tool["name"], description=tool["description"],
                                      parameters_json_schema=tool["parameters"]) for tool in tools])])
        try:
            response = self.client.models.generate_content(model=self.model, contents=self.to_contents(messages), config=config)
        except genai_errors.APIError as exc:
            # A per-day quota won't clear by waiting a few seconds, so don't retry it.
            daily = exc.code == 429 and "PerDay" in str(exc)
            raise ProviderError(str(exc), retryable=(exc.code == 429 and not daily) or (exc.code or 0) >= 500) from exc
        except OSError as exc:
            raise ProviderError(str(exc), retryable=True) from exc
        candidate = response.candidates[0] if response.candidates else None
        parts = (candidate.content.parts if candidate and candidate.content else None) or []
        if not parts:
            reason = candidate.finish_reason if candidate else "no candidates"
            raise ProviderError(f"Gemini returned nothing ({reason}).", retryable=True)
        text = "".join(part.text for part in parts if part.text and not part.thought)
        calls = [{"call_id": _new_call_id(), "name": part.function_call.name, "arguments": dict(part.function_call.args or {})}
                 for part in parts if part.function_call]
        saved = [part.model_dump(mode="json", exclude_none=True) for part in parts]
        for part in saved:
            part.get("function_call", {}).pop("id", None)  # results are matched by order
        return _reply(text, calls, {"provider": self.name, "model": self.model, "parts": saved})


@lru_cache
def make_adapter(name: str) -> GeminiAdapter | ChatAdapter:
    key_name, model_name = f"{name.upper()}_API_KEY", f"{name.upper()}_MODEL"
    key, model = os.environ.get(key_name), os.environ.get(model_name)
    if not key or not model:
        raise ProviderError(f"Set {key_name} and {model_name} in .env to use {name}.")
    if name == "gemini":
        return GeminiAdapter(key, model)
    sdk, client_class = (groq, groq.Groq) if name == "groq" else (openai, openai.OpenAI)
    return ChatAdapter(name, sdk, client_class, key, model)
