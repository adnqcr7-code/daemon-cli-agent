"""Scripted mock provider servers speaking the real wire protocols.

These run the actual HTTP + SSE code paths (httpx streaming, chunk
reassembly) so passing tests mean the adapters genuinely work - no
unittest.mock patching of the network layer anywhere in this suite.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _sse(chunks: list[str]) -> bytes:
    # Each SSE event must be terminated by a blank line.
    return "".join(f"data: {chunk}\n\n" for chunk in chunks).encode()


class _BaseHandler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # silence
        return

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json")

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}


class _MockServer:
    def __init__(self, handler_cls) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


# ---------------------------------------------------------------------------
# OpenAI-compatible mock
# ---------------------------------------------------------------------------


class OpenAICompatMock(_MockServer):
    """Scripted /v1/models + /v1/chat/completions (stream and non-stream).

    ``stages``: list of dicts consumed one per tool-result round:
      {"text": "...", "tool_calls": [{"name": ..., "arguments": {...}}],
       "usage": {...}, "fail_status": 500 (first attempt only)}
    """

    def __init__(self, *, models=("mock-coder", "mock-fast"), stages=(),
                 api_key="test-key-1234567890", require_auth=True) -> None:
        self.models = list(models)
        self.stages = list(stages)
        self.api_key = api_key
        self.require_auth = require_auth
        self.requests: list[dict] = []
        self.failed_once = False
        outer = self

        class Handler(_BaseHandler):
            def do_GET(self):
                if self.path.rstrip("/").endswith("/models"):
                    if outer.require_auth and \
                            self.headers.get("Authorization") != f"Bearer {outer.api_key}":
                        self._json(401, {"error": {"message": "bad key"}})
                        return
                    self._json(200, {"object": "list", "data": [
                        {"id": model, "object": "model"}
                        for model in outer.models]})
                    return
                self._json(404, {"error": {"message": "not found"}})

            def do_POST(self):
                body = self._read_body()
                outer.requests.append(body)
                if not self.path.endswith("/chat/completions"):
                    self._json(404, {"error": {"message": "not found"}})
                    return
                if outer.require_auth and \
                        self.headers.get("Authorization") != f"Bearer {outer.api_key}":
                    self._json(401, {"error": {"message": "bad key"}})
                    return
                stage_index = sum(1 for message in body.get("messages", [])
                                  if message.get("role") == "tool")
                stage = outer.stages[min(stage_index, len(outer.stages) - 1)] \
                    if outer.stages else {"text": "hello"}
                if stage.get("fail_status"):
                    if not outer.failed_once:
                        outer.failed_once = True
                        self._json(stage["fail_status"],
                                   {"error": {"message": "transient"}})
                        return
                    # Retry after a transient failure: use the next stage.
                    stage = outer.stages[min(stage_index + 1,
                                             len(outer.stages) - 1)]
                if body.get("stream"):
                    self._stream(stage, body)
                else:
                    self._plain(stage)

            def _plain(self, stage):
                message: dict = {"role": "assistant", "content": stage.get("text")}
                if stage.get("tool_calls"):
                    message["tool_calls"] = [
                        {"id": f"call_{index}", "type": "function",
                         "function": {"name": call["name"],
                                      "arguments": json.dumps(call["arguments"])}}
                        for index, call in enumerate(stage["tool_calls"])]
                    message["content"] = stage.get("text") or None
                self._json(200, {
                    "id": "chatcmpl-mock", "object": "chat.completion",
                    "choices": [{"index": 0, "message": message,
                                 "finish_reason": "tool_calls"
                                 if stage.get("tool_calls") else "stop"}],
                    "usage": stage.get("usage",
                                       {"prompt_tokens": 10,
                                        "completion_tokens": 5})})

            def _stream(self, stage, body):
                chunks = []
                if stage.get("fail_status"):  # already handled above
                    pass
                text = stage.get("text") or ""
                for index in range(0, len(text), 7):
                    chunks.append(json.dumps(
                        {"id": "chatcmpl-mock",
                         "choices": [{"index": 0,
                                      "delta": {"content": text[index:index + 7]}}]}))
                for index, call in enumerate(stage.get("tool_calls") or []):
                    args = json.dumps(call["arguments"])
                    chunks.append(json.dumps({
                        "choices": [{"index": 0, "delta": {"tool_calls": [
                            {"index": index, "id": f"call_{index}", "type": "function",
                             "function": {"name": call["name"],
                                          "arguments": args[:len(args) // 2]}}]}}]}))
                    chunks.append(json.dumps({
                        "choices": [{"index": 0, "delta": {"tool_calls": [
                            {"index": index,
                             "function": {"arguments": args[len(args) // 2:]}}]}}]}))
                finish = "tool_calls" if stage.get("tool_calls") else "stop"
                chunks.append(json.dumps(
                    {"choices": [{"index": 0, "delta": {},
                                  "finish_reason": finish}]}))
                if body.get("stream_options", {}).get("include_usage"):
                    chunks.append(json.dumps(
                        {"choices": [],
                         "usage": stage.get("usage", {"prompt_tokens": 10,
                                                      "completion_tokens": 5})}))
                chunks.append("[DONE]")
                self._send(200, _sse(chunks), "text/event-stream")

        super().__init__(Handler)


# ---------------------------------------------------------------------------
# Anthropic mock
# ---------------------------------------------------------------------------


class AnthropicMock(_MockServer):
    def __init__(self, *, models=("mock-claude",), stages=(),
                 api_key="sk-ant-api03-mock-key-000000000000") -> None:
        self.models = list(models)
        self.stages = list(stages)
        self.api_key = api_key
        self.requests: list[dict] = []
        outer = self

        class Handler(_BaseHandler):
            def _authed(self) -> bool:
                return (self.headers.get("x-api-key") == outer.api_key and
                        self.headers.get("anthropic-version") == "2023-06-01")

            def do_GET(self):
                if not self._authed():
                    self._json(401, {"type": "error",
                                     "error": {"message": "bad key"}})
                    return
                if self.path.endswith("/models"):
                    self._json(200, {"data": [
                        {"id": model, "display_name": model.title(),
                         "type": "model"} for model in outer.models]})
                    return
                self._json(404, {"error": {"message": "not found"}})

            def do_POST(self):
                body = self._read_body()
                outer.requests.append(body)
                if not self.path.endswith("/v1/messages"):
                    self._json(404, {"error": {"message": "not found"}})
                    return
                if not self._authed():
                    self._json(401, {"type": "error",
                                     "error": {"message": "bad key"}})
                    return
                stage_index = sum(
                    1 for message in body.get("messages", [])
                    for block in message.get("content", [])
                    if isinstance(block, dict) and block.get("type") == "tool_result")
                stage = outer.stages[min(stage_index, len(outer.stages) - 1)] \
                    if outer.stages else {"text": "hi"}
                if body.get("stream"):
                    self._stream(stage)
                else:
                    self._plain(stage)

            def _plain(self, stage):
                content = []
                if stage.get("text"):
                    content.append({"type": "text", "text": stage["text"]})
                for index, call in enumerate(stage.get("tool_calls") or []):
                    content.append({"type": "tool_use", "id": f"call_{index}",
                                    "name": call["name"],
                                    "input": call["arguments"]})
                self._json(200, {
                    "id": "msg_mock", "role": "assistant", "content": content,
                    "stop_reason": "tool_use" if stage.get("tool_calls")
                    else "end_turn",
                    "usage": stage.get("usage", {"input_tokens": 12,
                                                 "output_tokens": 7})})

            def _stream(self, stage):
                events = [
                    "event: message_start\ndata: " + json.dumps({
                        "type": "message_start",
                        "message": {"usage": {"input_tokens": 12}}}),
                ]
                if stage.get("text"):
                    events.append("event: content_block_start\ndata: " +
                                  json.dumps({"type": "content_block_start",
                                              "index": 0,
                                              "content_block": {"type": "text",
                                                                "text": ""}}))
                    text = stage["text"]
                    for index in range(0, len(text), 5):
                        events.append("event: content_block_delta\ndata: " +
                                      json.dumps({"type": "content_block_delta",
                                                  "index": 0,
                                                  "delta": {"type": "text_delta",
                                                            "text": text[index:index + 5]}}))
                    events.append("event: content_block_stop\ndata: " +
                                  json.dumps({"type": "content_block_stop",
                                              "index": 0}))
                for index, call in enumerate(stage.get("tool_calls") or []):
                    block_index = 1 if stage.get("text") else 0
                    events.append("event: content_block_start\ndata: " +
                                  json.dumps({"type": "content_block_start",
                                              "index": block_index,
                                              "content_block": {
                                                  "type": "tool_use",
                                                  "id": f"call_{index}",
                                                  "name": call["name"],
                                                  "input": {}}}))
                    args = json.dumps(call["arguments"])
                    events.append("event: content_block_delta\ndata: " +
                                  json.dumps({"type": "content_block_delta",
                                              "index": block_index,
                                              "delta": {"type": "input_json_delta",
                                                        "partial_json": args}}))
                    events.append("event: content_block_stop\ndata: " +
                                  json.dumps({"type": "content_block_stop",
                                              "index": block_index}))
                events.append("event: message_delta\ndata: " + json.dumps({
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use" if stage.get("tool_calls")
                              else "end_turn"},
                    "usage": {"output_tokens": 7}}))
                events.append("event: message_stop\ndata: " +
                              json.dumps({"type": "message_stop"}))
                payload = ("\n\n".join(events) + "\n\n").encode()
                self._send(200, payload, "text/event-stream")

        super().__init__(Handler)


# ---------------------------------------------------------------------------
# Gemini mock
# ---------------------------------------------------------------------------


class GeminiMock(_MockServer):
    def __init__(self, *, models=("mock-gemini",), stages=(),
                 api_key="AIza" + "a" * 35) -> None:
        self.models = list(models)
        self.stages = list(stages)
        self.api_key = api_key
        self.requests: list[dict] = []
        outer = self

        class Handler(_BaseHandler):
            def _authed(self) -> bool:
                return self.headers.get("x-goog-api-key") == outer.api_key

            def do_GET(self):
                if not self._authed():
                    self._json(403, {"error": {"code": 403,
                                               "message": "bad key"}})
                    return
                if self.path.split("?")[0].endswith("/models"):
                    self._json(200, {"models": [
                        {"name": f"models/{model}",
                         "displayName": model.title(),
                         "supportedGenerationMethods": ["generateContent",
                                                        "countTokens"]}
                        for model in outer.models]})
                    return
                self._json(404, {"error": {"code": 404, "message": "nf"}})

            def do_POST(self):
                body = self._read_body()
                outer.requests.append(body)
                path = self.path.split("?")[0]
                if ":streamGenerateContent" in path:
                    self._generate(body, stream=True)
                elif path.endswith(":generateContent"):
                    self._generate(body, stream=False)
                else:
                    self._json(404, {"error": {"code": 404, "message": "nf"}})

            def _generate(self, body, stream):
                if not self._authed():
                    self._json(403, {"error": {"code": 403,
                                               "message": "bad key"}})
                    return
                stage_index = sum(
                    1 for content in body.get("contents", [])
                    for part in content.get("parts", [])
                    if "functionResponse" in part)
                stage = outer.stages[min(stage_index, len(outer.stages) - 1)] \
                    if outer.stages else {"text": "hi gemini"}
                parts = []
                if stage.get("text"):
                    parts.append({"text": stage["text"]})
                for call in stage.get("tool_calls") or []:
                    parts.append({"functionCall": {"name": call["name"],
                                                   "args": call["arguments"]}})
                candidate = {"content": {"parts": parts},
                             "finishReason": "STOP"}
                usage = {"promptTokenCount": 9, "candidatesTokenCount": 4}
                if stream:
                    chunks = [json.dumps({"candidates": [candidate],
                                          "usageMetadata": usage})]
                    self._send(200, _sse(chunks), "text/event-stream")
                else:
                    self._json(200, {"candidates": [candidate],
                                     "usageMetadata": usage})

        super().__init__(Handler)
