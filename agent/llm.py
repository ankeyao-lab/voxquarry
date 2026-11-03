from __future__ import annotations

import asyncio
import errno
import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping, Protocol

import httpx

from .config import LLMSettings
from .contracts import AgentError


class LLMRuntime(Protocol):
    status: str

    def complete(
        self,
        *,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
        response_format: Mapping[str, Any] | None = None,
        seed: int | None = None,
    ) -> dict[str, Any]: ...


class UnavailableLLMRuntime:
    status = "unavailable"

    def complete(
        self,
        *,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
        response_format: Mapping[str, Any] | None = None,
        seed: int | None = None,
    ) -> dict[str, Any]:
        del messages, tools, response_format, seed
        raise AgentError(
            "llm_unavailable",
            "语义模型暂不可用",
            http_status=503,
            retryable=True,
        )


class OpenAICompatibleLLMRuntime:
    """Replaceable semantic runtime; never a fact or permission authority."""

    status = "ready"

    def __init__(
        self,
        settings: LLMSettings,
        *,
        timeout_seconds: float = 60.0,
        max_tokens: int = 4096,
        stream_total_seconds: float = 300.0,
    ) -> None:
        self._settings = settings
        self._timeout = timeout_seconds
        self._max_tokens = max_tokens
        self._stream_total = stream_total_seconds

    def complete_stream(
        self, *, messages: list[dict[str, str]], tools: list[dict[str, Any]],
        response_format: Mapping[str, Any] | None = None, seed: int | None = None,
        check_active: Callable[[], None] | None = None,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Receive bounded SSE in a cancellable worker; never retain reasoning.

        Public progress contains phase and elapsed time only. A completed JSON
        answer is returned atomically for parsing and review, never streamed to
        users as an unchecked finding. Legacy planner calls retain complete().
        """
        payload = self.request_payload(messages=messages, tools=tools,
                                       response_format=response_format, seed=seed)
        payload.update(stream=True, stream_options={"include_usage": True})
        return asyncio.run(self._stream(payload, check_active, on_progress))

    async def _stream(self, payload, check_active, on_progress):
        started = time.monotonic()
        idle = min(self._timeout, 60.0)
        state = {"phase": "waiting", "last_activity": started, "last_notice": -10.0,
                 "last_phase": None, "chunks": 0, "first_chunk_ms": None, "first_content_ms": None}
        content: list[str] = []
        result: dict[str, Any] = {"model": self._settings.model}
        finish = None
        done = False
        content_size = 0

        def packet(raw):
            nonlocal finish, done, content_size
            if raw.strip() == b"[DONE]":
                done = True
                return
            try:
                value = json.loads(raw)
            except (ValueError, UnicodeError):
                raise AgentError("llm_contract_invalid", "模型返回了无法解析的内容", http_status=502) from None
            if not isinstance(value, dict) or value.get("error"):
                raise AgentError("llm_provider_unavailable", "模型生成中断，请重试。", http_status=503, retryable=True)
            if isinstance(value.get("model"), str):
                result["model"] = value["model"]
            if isinstance(value.get("usage"), dict):
                result["usage"] = value["usage"]
            choices = value.get("choices", [])
            if not isinstance(choices, list) or len(choices) > 1:
                raise AgentError("llm_contract_invalid", "模型回答形状无效", http_status=502)
            if not choices:
                return
            choice = choices[0]
            if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                raise AgentError("llm_contract_invalid", "模型回答形状无效", http_status=502)
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                raise AgentError("llm_contract_invalid", "模型回答形状无效", http_status=502)
            text = delta.get("content")
            # The reasoning string is only inspected for presence and then
            # discarded with this packet; it never enters result or callbacks.
            if delta.get("reasoning_content") and state["phase"] != "answering":
                state["phase"] = "thinking"
            if text is not None and not isinstance(text, str):
                raise AgentError("llm_contract_invalid", "模型回答形状无效", http_status=502)
            if text:
                state["phase"] = "answering"
                if state["first_content_ms"] is None:
                    state["first_content_ms"] = round((time.monotonic() - started) * 1000)
                content_size += len(text)
                if content_size > 1_000_000:
                    raise AgentError("llm_contract_invalid", "模型响应超过大小限制", http_status=502)
                content.append(text)
            if choice.get("finish_reason") is not None:
                finish = choice["finish_reason"]

        async def receive():
            buffer = b""
            data = []
            received = 0
            timeout = httpx.Timeout(connect=min(idle, 15.0), read=idle, write=30.0, pool=15.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("POST", self._settings.base_url + "/chat/completions",
                    json=payload, headers={"Authorization": f"Bearer {self._settings.api_key}",
                                          "Accept": "text/event-stream"}) as response:
                    if response.status_code >= 400:
                        # Do not read an upstream error body.
                        raise self._http_error(response.status_code)
                    async for chunk in response.aiter_bytes():
                        now = time.monotonic()
                        state["last_activity"] = now
                        state["chunks"] += 1
                        if state["first_chunk_ms"] is None:
                            state["first_chunk_ms"] = round((now - started) * 1000)
                        received += len(chunk)
                        buffer += chunk
                        if received > 4_000_000 or len(buffer) > 1_000_000:
                            raise AgentError("llm_contract_invalid", "模型响应超过大小限制", http_status=502)
                        while b"\n" in buffer:
                            line, buffer = buffer.split(b"\n", 1)
                            line = line.rstrip(b"\r")
                            if line.startswith(b"data:"):
                                data.append(line[5:].lstrip(b" "))
                            elif not line and data:
                                packet(b"\n".join(data))
                                data = []
                            if done:
                                break
                        if done:
                            break
                    # A dropped connection is not a completed answer, even if
                    # its partial content happens to parse as JSON.
                    if not done or finish not in {"stop", "length", "content_filter"}:
                        raise AgentError("llm_response_incomplete", "模型生成中断，已保留完成的分析。", http_status=502, retryable=True)

        request = asyncio.create_task(receive())
        try:
            while True:
                if check_active:
                    check_active()
                now = time.monotonic()
                if now - started >= self._stream_total:
                    raise AgentError("llm_total_timeout", "这次分析超过等待时间，已保留完成的分析。", http_status=504, retryable=True)
                if now - state["last_activity"] >= idle:
                    raise self._stream_idle_error()
                if on_progress and (state["phase"] != state["last_phase"] or now - state["last_notice"] >= 2):
                    on_progress({"phase": state["phase"], "elapsed_seconds": int(now - started)})
                    state.update(last_phase=state["phase"], last_notice=now)
                completed, _ = await asyncio.wait({request}, timeout=0.25)
                if completed:
                    await request
                    break
            if check_active:
                check_active()
            result["choices"] = [{"message": {"content": "".join(content)}, "finish_reason": finish}]
            final = self._final_response(result)
            final["stream"] = {key: state[key] for key in ("chunks", "first_chunk_ms", "first_content_ms")}
            final["stream"]["duration_ms"] = round((time.monotonic() - started) * 1000)
            return final
        except httpx.TimeoutException:
            raise self._stream_idle_error() from None
        except httpx.HTTPError:
            raise AgentError("llm_unavailable", "模型连接中断，请重试。", http_status=503, retryable=True) from None
        finally:
            if not request.done():
                request.cancel()
            # Await cancellation so sockets and the async client are closed.
            await asyncio.gather(request, return_exceptions=True)

    @staticmethod
    def _stream_idle_error():
        return AgentError("llm_idle_timeout", "模型暂时没有响应，已保留完成的分析。",
                          http_status=504, retryable=True)

    def request_payload(
        self,
        *,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
        response_format: Mapping[str, Any] | None = None,
        seed: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._settings.model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": self._max_tokens,
        }
        from urllib.parse import urlsplit
        host = urlsplit(self._settings.base_url).hostname or ""
        if self._settings.is_glm_53_flash or host == "dashscope.aliyuncs.com" or host.endswith(".dashscope.aliyuncs.com"):
            payload["enable_thinking"] = True if self._settings.is_glm_53_flash else self._settings.enable_thinking
        if tools:
            payload["tools"] = tools
        effort = self._settings.reasoning_effort
        if self._settings.is_glm_53_flash:
            effort = effort or "high"
            if effort not in {"low", "high", "max"}:
                raise AgentError(
                    "dependency_config_invalid", "当前模型的推理强度配置无效", http_status=503
                )
        if effort is not None:
            payload["reasoning_effort"] = effort
        if response_format is not None:
            payload["response_format"] = dict(response_format)
        if seed is not None:
            payload["seed"] = seed
        return payload

    def complete(
        self,
        *,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
        response_format: Mapping[str, Any] | None = None,
        seed: int | None = None,
    ) -> dict[str, Any]:
        payload = self.request_payload(
            messages=messages,
            tools=tools,
            response_format=response_format,
            seed=seed,
        )
        request = urllib.request.Request(
            self._settings.base_url + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._settings.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read(1_000_001)
        except urllib.error.HTTPError as exc:
            # Never consume or propagate provider error bodies: they can include
            # request content, credentials, or internal provider diagnostics.
            exc.close()
            raise self._http_error(exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            if (
                isinstance(reason, (TimeoutError, socket.timeout))
                or getattr(reason, "errno", None) == errno.ETIMEDOUT
                or (isinstance(reason, str) and "timed out" in reason.lower())
            ):
                raise self._timeout_error() from None
            raise AgentError(
                "llm_unavailable",
                "无法连接语义模型服务",
                http_status=503,
                retryable=True,
                safe_next_step="稍后重试；持续失败时检查本地网络与模型服务配置。",
            ) from None
        if len(raw) > 1_000_000:
            raise AgentError("llm_contract_invalid", "模型响应超过大小限制", http_status=502)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            raise AgentError("llm_contract_invalid", "模型响应不是有效 JSON", http_status=502) from None
        if not isinstance(value, dict):
            raise AgentError("llm_contract_invalid", "模型响应形状无效", http_status=502)
        return self._final_response(value)

    @staticmethod
    def _timeout_error() -> AgentError:
        return AgentError(
            "llm_timeout",
            "语义模型响应超时，本轮未生成回答",
            http_status=504,
            retryable=True,
            safe_next_step="可以重试当前问题，或缩短问题后重试。",
        )

    @classmethod
    def _http_error(cls, status: int) -> AgentError:
        if status in {408, 504}:
            return cls._timeout_error()
        if status == 401:
            return AgentError(
                "llm_authentication_failed", "语义模型凭据无效或已失效", http_status=503,
                safe_next_step="请管理员更新本地模型服务的 API Key。",
            )
        if status in {403, 404}:
            return AgentError(
                "llm_model_access_denied", "当前凭据或服务地址无法使用指定模型", http_status=503,
                safe_next_step="请管理员核对模型 ID、百炼服务地域及模型访问权限。",
            )
        if status == 429:
            return AgentError(
                "llm_rate_limited", "语义模型服务额度或速率受限", http_status=503,
                retryable=True, safe_next_step="稍后重试；持续受限时请管理员检查百炼配额。",
            )
        if status >= 500:
            return AgentError(
                "llm_provider_unavailable", "语义模型服务暂时故障", http_status=503,
                retryable=True, safe_next_step="稍后重试当前问题。",
            )
        return AgentError(
            "llm_upstream_error", "语义模型拒绝了请求参数", http_status=502,
            safe_next_step="请管理员检查模型请求参数与服务配置。",
        )

    def _final_response(self, value: Mapping[str, Any]) -> dict[str, Any]:
        """Keep only final text and bounded usage metadata for the orchestrator.

        Provider reasoning and arbitrary extra response fields never enter a
        planner, persisted run, audit record, or downstream answer.
        """
        choices = value.get("choices")
        if not isinstance(choices, list) or not choices or len(choices) > 16:
            raise AgentError("llm_contract_invalid", "模型响应缺少最终回答", http_status=502)
        final_choices: list[dict[str, Any]] = []
        for index, choice in enumerate(choices):
            if not isinstance(choice, Mapping) or not isinstance(choice.get("message"), Mapping):
                raise AgentError("llm_contract_invalid", "模型回答形状无效", http_status=502)
            if choice.get("finish_reason") == "length":
                raise AgentError(
                    "llm_response_incomplete", "语义模型输出被截断，本轮未生成完整回答",
                    http_status=502, retryable=True,
                    safe_next_step="可以缩短问题后重试。",
                )
            content = choice["message"].get("content")
            if not isinstance(content, str) or not content.strip():
                raise AgentError("llm_contract_invalid", "模型响应缺少最终回答", http_status=502)
            final_choice: dict[str, Any] = {
                "index": index,
                "message": {"role": "assistant", "content": content},
            }
            if choice.get("finish_reason") in {"stop", "content_filter", "tool_calls", "function_call"}:
                final_choice["finish_reason"] = choice["finish_reason"]
            final_choices.append(final_choice)
        result: dict[str, Any] = {"choices": final_choices, "model": self._settings.model}
        if isinstance(value.get("model"), str) and len(value["model"]) <= 160:
            result["model"] = value["model"]
        usage = value.get("usage")
        if isinstance(usage, Mapping):
            final_usage: dict[str, Any] = {}
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                count = usage.get(key)
                if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                    final_usage[key] = count
            for key, detail_keys in (
                ("prompt_tokens_details", ("cached_tokens", "audio_tokens")),
                ("completion_tokens_details", ("reasoning_tokens", "audio_tokens")),
            ):
                details = usage.get(key)
                if isinstance(details, Mapping):
                    counts = {
                        name: details[name] for name in detail_keys
                        if isinstance(details.get(name), int)
                        and not isinstance(details[name], bool) and details[name] >= 0
                    }
                    if counts:
                        final_usage[key] = counts
            result["usage"] = final_usage
        return result
