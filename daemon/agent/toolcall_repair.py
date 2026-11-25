"""Malformed tool-call repair (v0.9.0 Phase 3).

The ladder, per turn, before anything executes:

1. **validate** — every call must name a known tool, carry a JSON
   object of arguments, and satisfy the tool's own schema (the
   ``Tool.validate`` check: required parameters present, declared
   types matching). An adapter that could not parse the model's
   arguments JSON marks them ``{"_raw": ...}`` (see
   ``openai_compat._finalise_calls``); that is a validation error,
   not an argument.
2. **repair** — deterministic, mechanical fixes only: parse a JSON
   string argument, salvage a truncated ``_raw`` payload, coerce a
   scalar to the schema's declared type. Repair NEVER renames a
   tool (a near-miss name could execute a different tool than the
   model intended) and NEVER invents a missing required parameter.
   A repaired call is re-validated; if it still fails, it is not
   repaired.
3. **retry** — the runtime sends the validation errors back to the
   model once (config ``tool_call.repair_attempts``, default 1) and
   lets it re-issue corrected calls.
4. **fallback** — still malformed after the retry: the turn ends
   with a text-only plan and zero tool executions. No tool runs on
   a call the runtime could not validate.

All-or-nothing per turn: if any call in a turn is unrepairable,
none of the turn's calls execute — a half-executed malformed turn
is worse than a retried one.
"""

from __future__ import annotations

import json
from typing import Any

from ..providers.types import ToolCall
from ..tools.base import ToolRegistry


def validate_call(call: ToolCall, registry: ToolRegistry) -> list[str]:
    """Validation errors for one call; empty when the call is usable."""
    errors: list[str] = []
    try:
        tool = registry.get(call.name)
    except Exception as exc:  # noqa: BLE001 - unknown tool is an error, not a crash
        return [f"Unknown tool '{call.name}': {exc}"]
    args = call.arguments
    if not isinstance(args, dict):
        return [f"Tool '{call.name}': arguments must be a JSON object, "
                f"got {type(args).__name__}."]
    if "_raw" in args:
        raw = args.get("_raw")
        preview = str(raw)[:200] if raw is not None else ""
        return [f"Tool '{call.name}': arguments arrived unparsed "
                f"(raw JSON: {preview!r})."]
    try:
        tool.validate(args)
    except Exception as exc:  # noqa: BLE001 - ToolError text is the error
        errors.append(f"Tool '{call.name}': {exc}")
    return errors


def _salvage_json(raw: str) -> dict[str, Any] | None:
    """Best-effort parse of a (possibly truncated) JSON object string.

    Tries the string as-is, then progressively trimmed to the last
    closing brace, then with the braces balanced (a truncated object
    missing its closers), then the first balanced ``{...}``
    substring. Returns a dict, or None when nothing parses as an
    object.
    """
    text = raw.strip()
    if not text:
        return None
    candidates = [text]
    last_brace = text.rfind("}")
    if last_brace > 0:
        candidates.append(text[:last_brace + 1])
    depth = text.count("{") - text.count("}")
    if depth > 0:
        candidates.append(text + "}" * depth)
    start = text.find("{")
    if start >= 0:
        depth = 0
        for index in range(start, len(text)):
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start:index + 1])
                    break
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _coerce_args(args: dict[str, Any],
                 properties: dict[str, Any]) -> dict[str, Any]:
    """Coerce scalar arguments to the schema's declared types.

    string <- any scalar (str(value)); number/integer <- a numeric
    string; boolean <- "true"/"false". Anything else is left alone
    and fails validation afterwards.
    """
    fixed = dict(args)
    for key, value in args.items():
        spec = properties.get(key)
        if not isinstance(spec, dict):
            continue
        expected = spec.get("type")
        if expected == "string" and isinstance(value, (int, float, bool)):
            fixed[key] = str(value)
        elif expected in ("number", "integer") and isinstance(value, str):
            try:
                number = json.loads(value)
            except json.JSONDecodeError:
                continue
            if isinstance(number, (int, float)) and not isinstance(number, bool):
                fixed[key] = number
        elif expected == "boolean" and isinstance(value, str):
            if value.lower() == "true":
                fixed[key] = True
            elif value.lower() == "false":
                fixed[key] = False
    return fixed


def repair_call(call: ToolCall, registry: ToolRegistry) -> ToolCall | None:
    """A validated repaired copy of ``call``, or None when no
    mechanical repair makes it valid."""
    try:
        tool = registry.get(call.name)
    except Exception:  # noqa: BLE001 - unknown tool: never rename, no repair
        return None
    args: Any = call.arguments
    if isinstance(args, str):
        salvaged = _salvage_json(args)
        if salvaged is None:
            return None
        args = salvaged
    elif isinstance(args, dict) and "_raw" in args:
        raw = args.get("_raw")
        if not isinstance(raw, str):
            return None
        salvaged = _salvage_json(raw)
        if salvaged is None:
            return None
        args = salvaged
    elif not isinstance(args, dict):
        return None
    schema = tool.parameters or {}
    repaired = _coerce_args(args, schema.get("properties") or {})
    candidate = ToolCall(id=call.id, name=call.name, arguments=repaired)
    if validate_call(candidate, registry):
        return None
    return candidate


def repair_calls(calls: list[ToolCall],
                 registry: ToolRegistry) -> tuple[list[ToolCall], list[str]]:
    """Validate a turn's calls, repairing what can be repaired.

    Returns ``(usable_calls, errors)``. ``errors`` is empty exactly
    when every call validated (directly or after repair); when it is
    non-empty the caller must NOT execute ``usable_calls`` — the turn
    goes to the retry/fallback rungs instead.
    """
    usable: list[ToolCall] = []
    errors: list[str] = []
    for call in calls:
        if not validate_call(call, registry):
            usable.append(call)
            continue
        repaired = repair_call(call, registry)
        if repaired is not None:
            usable.append(repaired)
        else:
            errors.extend(validate_call(call, registry))
    return usable, errors
