"""
openai_responses_util.py — Chat Completions → Responses API 변환 헬퍼.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI


@dataclass(frozen=True)
class ResponsesFunctionCall:
    name: str
    arguments: str


@dataclass(frozen=True)
class ResponsesToolCall:
    id: str
    type: str
    function: ResponsesFunctionCall


@dataclass
class Turn1AssistantMessage:
    """Turn1 responses 출력 — normalize_turn1_tool_calls 호환."""

    content: str | None
    tool_calls: list[Any]


def usage_to_dict(usage: Any) -> dict[str, int]:
    if usage is None:
        return {}
    prompt = getattr(usage, "prompt_tokens", None)
    if prompt is None:
        prompt = getattr(usage, "input_tokens", None)
    completion = getattr(usage, "completion_tokens", None)
    if completion is None:
        completion = getattr(usage, "output_tokens", None)
    total = getattr(usage, "total_tokens", None)
    out: dict[str, int] = {}
    if prompt is not None:
        out["prompt_tokens"] = int(prompt)
    if completion is not None:
        out["completion_tokens"] = int(completion)
    if total is not None:
        out["total_tokens"] = int(total)
    return out


def _add_nullable_type(prop: dict[str, Any]) -> None:
    type_val = prop.get("type")
    if type_val is None:
        return
    if isinstance(type_val, list):
        if "null" not in type_val:
            prop["type"] = [*type_val, "null"]
    elif type_val != "null":
        prop["type"] = [type_val, "null"]
    if "enum" in prop and None not in prop["enum"]:
        prop["enum"] = [*prop["enum"], None]


def _normalize_property_schema(prop: dict[str, Any], *, optional: bool) -> None:
    prop_type = prop.get("type")

    if prop_type == "object" or (
        isinstance(prop_type, list) and "object" in prop_type
    ):
        _normalize_object_schema(prop, required_keys=set(prop.get("required") or []))
        if optional:
            _add_nullable_type(prop)
        return

    if prop_type == "array" or (
        isinstance(prop_type, list) and "array" in prop_type
    ):
        items = prop.get("items")
        if isinstance(items, dict) and items.get("type") == "object":
            _normalize_object_schema(
                items,
                required_keys=set(items.get("required") or []),
            )
        if optional:
            _add_nullable_type(prop)
        return

    if optional:
        _add_nullable_type(prop)


def _normalize_object_schema(
    schema: dict[str, Any],
    *,
    required_keys: set[str],
) -> None:
    properties = schema.get("properties") or {}
    if not isinstance(properties, dict):
        return

    for key, prop in list(properties.items()):
        if not isinstance(prop, dict):
            continue
        _normalize_property_schema(prop, optional=key not in required_keys)
        properties[key] = prop

    schema["properties"] = properties
    schema["required"] = list(properties.keys())
    schema["additionalProperties"] = False
    if schema.get("type") is None:
        schema["type"] = "object"


def normalize_strict_parameters(parameters: dict[str, Any]) -> dict[str, Any]:
    """
    Responses strict function schema — properties의 모든 키를 required에 포함.
    선택 필드는 nullable 타입으로 변환.
    """
    params = copy.deepcopy(parameters)
    if params.get("type") != "object":
        return params
    _normalize_object_schema(params, required_keys=set(params.get("required") or []))
    return params


def chat_tools_to_responses_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Chat Completions tool schema → Responses API function tool (strict)."""
    converted: list[dict[str, Any]] = []
    for tool in tools:
        if tool.get("type") != "function":
            converted.append(tool)
            continue
        if "function" in tool:
            fn = tool["function"]
            raw_params = fn.get(
                "parameters",
                {"type": "object", "properties": {}, "additionalProperties": False},
            )
            converted.append({
                "type": "function",
                "name": fn["name"],
                "description": fn.get("description", ""),
                "parameters": normalize_strict_parameters(raw_params),
                "strict": True,
            })
        else:
            item = dict(tool)
            if "parameters" in item:
                item["parameters"] = normalize_strict_parameters(item["parameters"])
            item.setdefault("strict", True)
            converted.append(item)
    return converted


def split_messages_for_responses(
    messages: list[dict[str, Any]],
) -> tuple[str | None, list[dict[str, Any]]]:
    """system → instructions, user/assistant → input."""
    system_parts: list[str] = []
    input_items: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if role == "system":
            if isinstance(content, str) and content.strip():
                system_parts.append(content.strip())
            elif content is not None:
                system_parts.append(str(content))
        elif role in ("user", "assistant"):
            item: dict[str, Any] = {"role": role}
            if content is not None:
                item["content"] = content
            input_items.append(item)
    instructions = "\n\n".join(system_parts) if system_parts else None
    return instructions, input_items


def reasoning_from_kwargs(create_kwargs: dict[str, Any]) -> dict[str, str] | None:
    if "reasoning" in create_kwargs:
        return create_kwargs.pop("reasoning")
    effort = create_kwargs.pop("reasoning_effort", None)
    if effort:
        return {"effort": str(effort)}
    return None


def model_reasoning_efforts(model: str) -> tuple[str, ...]:
    """모델별 Responses API reasoning.effort 지원값 (낮은 순)."""
    m = (model or "").strip().lower()
    if "5.4" in m or "5-4" in m:
        return ("none", "low", "medium", "high", "xhigh")
    if m.startswith("gpt-5"):
        return ("minimal", "low", "medium", "high")
    return ("low", "medium", "high")


def resolve_reasoning_effort(model: str, effort: str) -> str:
    """요청 effort를 모델이 지원하는 값으로 보정."""
    supported = model_reasoning_efforts(model)
    key = (effort or "").strip().lower()
    if key in ("", "lowest", "min"):
        return supported[0]
    if key in supported:
        return key
    aliases = {
        "none": ("minimal", "none"),
        "minimal": ("none", "minimal"),
        "min": supported[:2],
    }
    for candidate in aliases.get(key, ()):
        if candidate in supported:
            return candidate
    return supported[0]


def apply_model_reasoning(params: dict[str, Any]) -> None:
    reasoning = params.get("reasoning")
    if not isinstance(reasoning, dict):
        return
    effort = reasoning.get("effort")
    if not effort:
        return
    model = str(params.get("model") or "")
    reasoning["effort"] = resolve_reasoning_effort(model, str(effort))


def normalize_responses_create_kwargs(create_kwargs: dict[str, Any]) -> None:
    """Chat Completions 키 → Responses API 키 (in-place)."""
    if "max_tokens" in create_kwargs:
        create_kwargs["max_output_tokens"] = create_kwargs.pop("max_tokens")
    text_format = create_kwargs.pop("text_format", None)
    if text_format is not None:
        create_kwargs["text"] = text_format


def response_output_text(response: Any) -> str:
    text = (getattr(response, "output_text", None) or "").strip()
    if text:
        return text
    parts: list[str] = []
    for item in getattr(response, "output", None) or []:
        if getattr(item, "type", None) != "message":
            continue
        for block in getattr(item, "content", None) or []:
            if getattr(block, "type", None) == "output_text":
                block_text = (getattr(block, "text", None) or "").strip()
                if block_text:
                    parts.append(block_text)
    return "\n".join(parts).strip()


def response_to_turn1_message(response: Any) -> Turn1AssistantMessage:
    content_parts: list[str] = []
    tool_calls: list[ResponsesToolCall] = []
    for item in getattr(response, "output", None) or []:
        item_type = getattr(item, "type", None)
        if item_type == "message":
            for block in getattr(item, "content", None) or []:
                if getattr(block, "type", None) == "output_text":
                    block_text = (getattr(block, "text", None) or "").strip()
                    if block_text:
                        content_parts.append(block_text)
        elif item_type == "function_call":
            call_id = getattr(item, "call_id", None) or getattr(item, "id", "")
            tool_calls.append(
                ResponsesToolCall(
                    id=str(call_id),
                    type="function",
                    function=ResponsesFunctionCall(
                        name=str(getattr(item, "name", "") or ""),
                        arguments=str(getattr(item, "arguments", "") or "{}"),
                    ),
                )
            )
    content = "\n".join(content_parts).strip() or None
    if not content:
        fallback = response_output_text(response)
        content = fallback or None
    return Turn1AssistantMessage(content=content, tool_calls=tool_calls)


async def responses_create_text(
    *,
    client: AsyncOpenAI,
    model: str,
    messages: list[dict[str, Any]],
    usage_out: list[dict[str, int]] | None = None,
    tools: list[dict[str, Any]] | None = None,
    **create_kwargs: Any,
) -> str:
    """Responses API 비스트리밍 — 텍스트만 반환."""
    instructions, input_items = split_messages_for_responses(messages)
    reasoning = reasoning_from_kwargs(create_kwargs)
    normalize_responses_create_kwargs(create_kwargs)
    params: dict[str, Any] = {
        "model": model,
        "input": input_items,
        **create_kwargs,
    }
    if instructions:
        params["instructions"] = instructions
    if reasoning:
        params["reasoning"] = reasoning
    if tools:
        params["tools"] = chat_tools_to_responses_tools(tools)
    apply_model_reasoning(params)
    response = await client.responses.create(**params)
    if usage_out is not None:
        usage_out.append(usage_to_dict(response.usage))
    return response_output_text(response)


async def responses_create_turn1(
    *,
    client: AsyncOpenAI,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    usage_out: list[dict[str, int]] | None = None,
    **create_kwargs: Any,
) -> Turn1AssistantMessage:
    instructions, input_items = split_messages_for_responses(messages)
    reasoning = reasoning_from_kwargs(create_kwargs)
    normalize_responses_create_kwargs(create_kwargs)
    params: dict[str, Any] = {
        "model": model,
        "input": input_items,
        "tools": chat_tools_to_responses_tools(tools),
        **create_kwargs,
    }
    if instructions:
        params["instructions"] = instructions
    if reasoning:
        params["reasoning"] = reasoning
    apply_model_reasoning(params)
    response = await client.responses.create(**params)
    if usage_out is not None:
        usage_out.append(usage_to_dict(response.usage))
    return response_to_turn1_message(response)


async def responses_stream_text(
    *,
    client: AsyncOpenAI,
    model: str,
    messages: list[dict[str, Any]],
    on_delta=None,
    usage_out: list[dict[str, int]] | None = None,
    **create_kwargs: Any,
) -> str:
    """Responses API 스트리밍 — 전체 텍스트 조립."""
    instructions, input_items = split_messages_for_responses(messages)
    reasoning = reasoning_from_kwargs(create_kwargs)
    normalize_responses_create_kwargs(create_kwargs)
    params: dict[str, Any] = {
        "model": model,
        "input": input_items,
        "stream": True,
        **create_kwargs,
    }
    if instructions:
        params["instructions"] = instructions
    if reasoning:
        params["reasoning"] = reasoning
    apply_model_reasoning(params)

    api_stream = await client.responses.create(**params)
    raw_text = ""
    completed_response = None
    async for event in api_stream:
        event_type = getattr(event, "type", None)
        if event_type == "response.output_text.delta":
            chunk_text = getattr(event, "delta", None) or ""
            if chunk_text:
                raw_text += chunk_text
                if on_delta:
                    await on_delta(chunk_text)
        elif event_type == "response.completed":
            completed_response = getattr(event, "response", None)
            if usage_out is not None and completed_response is not None:
                usage_out.append(usage_to_dict(completed_response.usage))
    if not raw_text.strip() and completed_response is not None:
        raw_text = response_output_text(completed_response)
    return raw_text
