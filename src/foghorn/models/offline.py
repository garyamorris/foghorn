"""Deterministic, STATELESS, scriptable offline chat model.

Design rules (each one was learned from a probe, see docs/design/04-production-quality.md):

* Behaviour is a pure function of (messages, bound tools, response_format, run config). No call
  counters decide the answer, so replay, time-travel, retries and `interrupt()` re-execution
  (a resumed node re-runs from its first line) give the same answers. A long-lived `langgraph dev`
  server can serve a thousand runs without a script being "used up".
* Scripts are ordered `Rule`s: first match wins; no match -> `default_reply` (schema-synthesis, so
  graphs with no script still run end to end, offline, with no API key).
* Mirrors the provider surface that `create_agent` / `with_structured_output` / `stream_mode=messages`
  rely on: `bind_tools(**kw)`, `response_format` kwarg (ProviderStrategy), `tool_choice="any"`
  (ToolStrategy), `_stream/_astream` with token + tool-call-argument chunks, `usage_metadata`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from dataclasses import dataclass, field
from operator import itemgetter
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.language_models.model_profile import ModelProfile
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.output_parsers import JsonOutputParser, PydanticOutputParser
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable, RunnableMap, RunnablePassthrough
from langchain_core.runnables.config import var_child_runnable_config
from langchain_core.utils.function_calling import convert_to_json_schema, convert_to_openai_tool
from pydantic import BaseModel, ConfigDict, Field

try:  # typed runtime context (Studio "Context" pane) - optional
    from langgraph.runtime import get_runtime
except Exception:  # pragma: no cover
    get_runtime = None  # type: ignore[assignment]


class OfflineScriptError(RuntimeError):
    """A script produced something the bound provider surface could not have accepted."""


# --------------------------------------------------------------------------- stable helpers
def stable_int(*parts: Any) -> int:
    digest = hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()
    return int(digest[:12], 16)


def _text(m: BaseMessage) -> str:
    return str(m.text)


_FALSE_NAME = re.compile(r"(need|revis|retry|reject|fail|error|escalat|missing|unsafe|ambig)", re.I)


def synthesize(schema: dict[str, Any], seed: str, *, _root: dict[str, Any] | None = None, _name: str = "root") -> Any:
    """Deterministic value for a JSON schema (handles $ref, enum, anyOf, bounds, nesting)."""
    root = _root or schema
    if "$ref" in schema:
        ref = schema["$ref"].split("/")[-1]
        return synthesize(root.get("$defs", {}).get(ref, {}), seed, _root=root, _name=_name)
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        opts = schema["enum"]
        return opts[stable_int(seed, _name) % len(opts)]
    for key in ("anyOf", "oneOf"):
        if key in schema:
            opts = [o for o in schema[key] if o.get("type") != "null"] or schema[key]
            return synthesize(opts[0], seed, _root=root, _name=_name)
    if "allOf" in schema:
        merged: dict[str, Any] = {}
        for part in schema["allOf"]:
            part = root.get("$defs", {}).get(part["$ref"].split("/")[-1], {}) if "$ref" in part else part
            merged = {**merged, **part, "properties": {**merged.get("properties", {}), **part.get("properties", {})}}
        return synthesize(merged, seed, _root=root, _name=_name)
    typ = schema.get("type")
    if isinstance(typ, list):
        typ = next((t for t in typ if t != "null"), "null")
    if typ == "object" or "properties" in schema:
        props = schema.get("properties", {})
        required = set(schema.get("required", []))
        return {
            k: synthesize(v, seed, _root=root, _name=k) for k, v in props.items() if k in required or "default" not in v
        }
    if "default" in schema and typ not in ("array",):
        return schema["default"]
    if typ == "array":
        n = max(int(schema.get("minItems", 1)), 1)
        return [synthesize(schema.get("items", {}), seed, _root=root, _name=f"{_name}_{i}") for i in range(n)]
    if typ == "boolean":
        return not _FALSE_NAME.search(_name)
    if typ == "integer":
        lo, hi = (
            schema.get("minimum", schema.get("exclusiveMinimum")),
            schema.get("maximum", schema.get("exclusiveMaximum")),
        )
        v = 3
        if lo is not None:
            v = max(v, int(lo))
        if hi is not None:
            v = min(v, int(hi))
        return v
    if typ == "number":
        lo, hi = schema.get("minimum"), schema.get("maximum")
        if lo is not None and hi is not None:
            return round((lo + hi) / 2, 3)
        return 0.8 if re.search(r"(conf|prob|score|rating)", _name, re.I) else 0.5
    if typ == "string":
        fmt = schema.get("format")
        if fmt == "date-time":
            return "2026-01-01T00:00:00Z"
        if fmt == "date":
            return "2026-01-01"
        if fmt == "email":
            return "user@example.com"
        if fmt == "uuid":
            return f"00000000-0000-4000-8000-{stable_int(seed, _name) % 10**12:012d}"
        return f"{_name.replace('_', ' ')} (offline)"
    if typ == "null":
        return None
    return f"{_name} (offline)"


# --------------------------------------------------------------------------- script vocabulary
@dataclass(frozen=True)
class ToolCallSpec:
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    id: str | None = None


@dataclass(frozen=True)
class Reply:
    """What a rule decides. At most one of text / tool_calls / structured / error / message."""

    text: str = ""
    tool_calls: tuple[ToolCallSpec, ...] = ()
    structured: Any = None
    error: BaseException | None = None
    message: AIMessage | None = None  # full control escape hatch


def say(text: str) -> Reply:
    return Reply(text=text)


def call_tool(name: str, /, **args: Any) -> Reply:
    return Reply(tool_calls=(ToolCallSpec(name, args),))


def call_tools(*specs: tuple[str, dict[str, Any]]) -> Reply:
    return Reply(tool_calls=tuple(ToolCallSpec(n, a) for n, a in specs))


def struct(value: BaseModel | dict[str, Any]) -> Reply:
    return Reply(structured=value)


def fail(exc: BaseException) -> Reply:
    return Reply(error=exc)


@dataclass(frozen=True)
class Call:
    """Everything a rule may key on. Derived views are computed from the message list only."""

    role: str
    messages: tuple[BaseMessage, ...]
    tools: tuple[str, ...]  # names of bound tools (incl. ToolStrategy output tools)
    tool_params: dict[str, dict[str, Any]]  # name -> JSON-schema parameters
    tool_choice: str | None
    schema_name: str | None  # response_format / with_structured_output target
    schema: dict[str, Any] | None
    node: str | None  # langgraph_node
    scenario: str | None  # configurable["offline_scenario"] / context.offline_scenario / context.scenario_id
    thread_id: str | None
    profile: str = "competent"  # run context `model_profile`: competent | naive | cautious | gullible
    context: dict[str, Any] = field(default_factory=dict)  # run context + configurable, flattened (read-only view)

    @property
    def human(self) -> str:
        return next((_text(m) for m in reversed(self.messages) if isinstance(m, HumanMessage)), "")

    @property
    def system(self) -> str:
        return "\n".join(_text(m) for m in self.messages if isinstance(m, SystemMessage))

    @property
    def turn(self) -> tuple[BaseMessage, ...]:
        """Messages after the last human message (the current agent turn)."""
        idx = max((i for i, m in enumerate(self.messages) if isinstance(m, HumanMessage)), default=-1)
        return self.messages[idx + 1 :]

    @property
    def tool_results(self) -> list[ToolMessage]:
        return [m for m in self.turn if isinstance(m, ToolMessage)]

    @property
    def step(self) -> int:
        """0 for the first model call of a turn, 1 after one AI message, ... (stateless counter)."""
        return sum(isinstance(m, AIMessage) for m in self.turn)

    @property
    def text(self) -> str:
        return "\n".join(_text(m) for m in self.messages)

    @property
    def fingerprint(self) -> str:
        blob = json.dumps([[m.type, _text(m), getattr(m, "tool_call_id", None)] for m in self.messages], default=str)
        return hashlib.sha1(f"{self.role}|{self.schema_name}|{blob}".encode()).hexdigest()


Matcher = Callable[[Call], bool]
Responder = Callable[[Call], "Reply | str | BaseModel | AIMessage"]


def _has(hay: str, needles: Sequence[str | re.Pattern[str]], *, any_: bool) -> bool:
    checks = [(n.search(hay) is not None) if isinstance(n, re.Pattern) else (n.lower() in hay.lower()) for n in needles]
    return any(checks) if any_ else all(checks)


def human(*needles: str | re.Pattern[str], any_: bool = False) -> Matcher:
    return lambda c: _has(c.human, needles, any_=any_)


def system(*needles: str | re.Pattern[str], any_: bool = False) -> Matcher:
    return lambda c: _has(c.system, needles, any_=any_)


def anywhere(*needles: str | re.Pattern[str], any_: bool = False) -> Matcher:
    return lambda c: _has(c.text, needles, any_=any_)


def node(name: str) -> Matcher:
    return lambda c: c.node == name


def scenario(name: str) -> Matcher:
    return lambda c: c.scenario == name


def schema(name: str) -> Matcher:
    return lambda c: c.schema_name == name


def role(name: str) -> Matcher:
    return lambda c: c.role == name


def profile(*names: str) -> Matcher:
    """Match the run context `model_profile` (competent | naive | cautious | gullible)."""
    return lambda c: c.profile in names


def step(n: int) -> Matcher:
    return lambda c: c.step == n


def tool_result(name: str | None = None, contains: str | None = None) -> Matcher:
    def _m(c: Call) -> bool:
        return any(
            (name is None or r.name == name) and (contains is None or contains.lower() in _text(r).lower())
            for r in c.tool_results
        )

    return _m


def no_tool_result() -> Matcher:
    return lambda c: not c.tool_results


def all_(*ms: Matcher) -> Matcher:
    return lambda c: all(m(c) for m in ms)


def any_of(*ms: Matcher) -> Matcher:
    return lambda c: any(m(c) for m in ms)


def not_(m: Matcher) -> Matcher:
    return lambda c: not m(c)


@dataclass(frozen=True)
class Rule:
    name: str
    when: Matcher
    then: Responder


def default_reply(call: Call) -> Reply:
    """Schema/tool-aware fallback so any graph runs offline with zero configuration."""
    if call.tool_choice not in (None, "auto", "none") and call.tools:
        forced = call.tool_choice if call.tool_choice in call.tools else None
        if forced is None:  # "any": ToolStrategy appends output tools LAST (langchain factory.py)
            forced = _best_tool(call) if not call.tool_results else call.tools[-1]
        return call_tool(forced, **synthesize(call.tool_params.get(forced, {}), call.fingerprint, _name=forced))
    if call.tools and not call.tool_results and call.tool_choice != "none":
        name = _best_tool(call)
        return call_tool(name, **synthesize(call.tool_params.get(name, {}), call.fingerprint, _name=name))
    if call.schema is not None:
        return struct(synthesize(call.schema, call.fingerprint))
    if call.tool_results:
        digest = " | ".join(_text(r)[:120] for r in call.tool_results)
        return say(f"[offline:{call.role}] Based on the tool results: {digest}")
    echo = re.sub(r"\s+", " ", call.human).strip()[:160]  # (no backslash inside f-string: py3.11 compat)
    return say(f"[offline:{call.role}] {echo}")


def _best_tool(call: Call) -> str:
    words = set(re.findall(r"[a-z0-9]+", call.human.lower()))
    best, score = call.tools[0], -1
    for t in call.tools:
        s = len(words & set(re.findall(r"[a-z0-9]+", t.lower().replace("_", " "))))
        if s > score:
            best, score = t, s
    return best


class Script:
    """Ordered rules + fallback. `calls` records the last 500 Calls for assertions."""

    def __init__(self, rules: Sequence[Rule] = (), fallback: Callable[[Call], Reply] = default_reply) -> None:
        self.rules: list[Rule] = list(rules)
        self.fallback = fallback
        self.calls: deque[Call] = deque(maxlen=500)
        self.matched: deque[str] = deque(maxlen=500)

    def when(self, name: str, matcher: Matcher, then: Responder | Reply | str) -> Script:
        responder: Responder = then if callable(then) else (lambda _c, _t=then: _t)  # type: ignore[misc]
        self.rules.append(Rule(name, matcher, responder))
        return self

    def decide(self, call: Call) -> tuple[str, Reply]:
        self.calls.append(call)
        for r in self.rules:
            if r.when(call):
                self.matched.append(r.name)
                return r.name, _as_reply(r.then(call))
        self.matched.append("<fallback>")
        return "<fallback>", self.fallback(call)


def _as_reply(x: Reply | str | BaseModel | AIMessage) -> Reply:
    if isinstance(x, Reply):
        return x
    if isinstance(x, str):
        return say(x)
    if isinstance(x, AIMessage):
        return Reply(message=x)
    if isinstance(x, BaseModel):
        return struct(x)
    raise OfflineScriptError(f"Rule returned unsupported type {type(x).__name__}")


# test-time override: role or graph key -> Script (global on purpose: pytest-xdist = separate processes)
SCRIPT_OVERRIDES: dict[str, Script] = {}


# --------------------------------------------------------------------------- the model
def _jsonable(v: Any) -> Any:
    return v.model_dump(mode="json") if isinstance(v, BaseModel) else v


class OfflineChatModel(BaseChatModel):
    """Drop-in `BaseChatModel` for tests, evals and key-less Studio demos."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    role: str = "default"
    script: Script = Field(default_factory=Script, exclude=True)
    token_delay_s: float = 0.0  # asyncio.sleep between streamed chunks (pace Studio demos; never time.sleep)
    words_per_chunk: int = 1
    args_chunk_chars: int = 12
    native_structured_output: bool = True  # profile["structured_output"]: True -> create_agent ProviderStrategy
    strict: bool = True  # validate scripted tool calls against bound tool schemas

    @property
    def _llm_type(self) -> str:
        return "offline-fake"

    def _resolve_model_profile(self) -> ModelProfile | None:
        return {
            "tool_calling": True,
            "structured_output": self.native_structured_output,
            "max_input_tokens": 200_000,
            "max_output_tokens": 8_192,
        }

    # -- binding (mirrors real providers: kwargs travel through .bind) ---------------------------
    def bind_tools(
        self, tools: Sequence[Any], *, tool_choice: str | None = None, **kwargs: Any
    ) -> Runnable[Any, AIMessage]:
        formatted = [convert_to_openai_tool(t) for t in tools]
        return self.bind(tools=formatted, tool_choice=tool_choice, **kwargs)

    def with_structured_output(self, schema: Any, *, include_raw: bool = False, **kwargs: Any) -> Runnable[Any, Any]:
        js = convert_to_json_schema(schema)
        name = getattr(schema, "__name__", None) or js.get("title") or "output"
        llm = self.bind(
            response_format={"type": "json_schema", "json_schema": {"name": name, "schema": js}},
            ls_structured_output_format={"kwargs": {"method": "json_schema"}, "schema": convert_to_openai_tool(schema)},
        )
        parser: Any = (
            PydanticOutputParser(pydantic_object=schema)
            if isinstance(schema, type) and issubclass(schema, BaseModel)
            else JsonOutputParser()
        )
        if include_raw:
            assign = RunnablePassthrough.assign(parsed=itemgetter("raw") | parser, parsing_error=lambda _: None)
            none = RunnablePassthrough.assign(parsed=lambda _: None)
            chain: Runnable[Any, Any] = RunnableMap(raw=llm) | assign.with_fallbacks(
                [none], exception_key="parsing_error"
            )
            return chain
        plain: Runnable[Any, Any] = llm | parser
        return plain

    # -- one decision per provider call -----------------------------------------------------------
    def _make_call(self, messages: list[BaseMessage], run_manager: Any, kwargs: dict[str, Any]) -> Call:
        tools = kwargs.get("tools") or []
        names, params = [], {}
        for t in tools:
            fn = t.get("function", t)
            names.append(fn["name"])
            params[fn["name"]] = fn.get("parameters", fn.get("input_schema", {}))
        rf = kwargs.get("response_format")
        schema_name = schema_json = None
        if isinstance(rf, dict) and rf.get("type") == "json_schema":
            schema_name = rf["json_schema"].get("name")
            schema_json = rf["json_schema"].get("schema")
        cfg = var_child_runnable_config.get() or {}
        configurable = cfg.get("configurable", {})
        meta = getattr(run_manager, "metadata", None) or cfg.get("metadata", {}) or {}
        flat: dict[str, Any] = {k: v for k, v in configurable.items() if not k.startswith("__")}
        if get_runtime is not None:
            try:
                ctx = get_runtime().context
            except Exception:
                ctx = None
            if ctx is not None:
                if isinstance(ctx, dict):
                    flat.update(ctx)
                elif hasattr(ctx, "model_dump"):
                    flat.update(ctx.model_dump())
                else:
                    flat.update({k: v for k, v in vars(ctx).items() if not k.startswith("_")})
        scenario_ = flat.get("offline_scenario") or meta.get("offline_scenario") or flat.get("scenario_id")
        return Call(
            role=self.role,
            messages=tuple(messages),
            tools=tuple(names),
            tool_params=params,
            tool_choice=kwargs.get("tool_choice"),
            schema_name=schema_name,
            schema=schema_json,
            node=meta.get("langgraph_node"),
            scenario=scenario_,
            thread_id=configurable.get("thread_id") or meta.get("thread_id"),
            profile=str(flat.get("model_profile") or "competent"),
            context=flat,
        )

    def _decide(self, messages: list[BaseMessage], run_manager: Any, kwargs: dict[str, Any]) -> AIMessage:
        script = SCRIPT_OVERRIDES.get(self.role, self.script)
        call = self._make_call(messages, run_manager, kwargs)
        rule, reply = script.decide(call)
        if reply.error is not None:
            raise reply.error
        if reply.message is not None:
            return reply.message
        tool_calls = []
        for i, spec in enumerate(reply.tool_calls):
            if self.strict:
                if spec.name not in call.tools:
                    raise OfflineScriptError(f"rule {rule!r} calls tool {spec.name!r}; bound tools: {list(call.tools)}")
                missing = set(call.tool_params.get(spec.name, {}).get("required", [])) - set(spec.args)
                if missing:
                    raise OfflineScriptError(
                        f"rule {rule!r}: tool {spec.name!r} missing required args {sorted(missing)}"
                    )
            tool_calls.append(
                {
                    "name": spec.name,
                    "args": spec.args,
                    "id": spec.id or f"call_{call.fingerprint[:10]}_{i}",
                    "type": "tool_call",
                }
            )
        content = json.dumps(_jsonable(reply.structured)) if reply.structured is not None else reply.text
        n_in = sum(len(_text(m).split()) for m in messages)
        n_out = len(content.split()) + 4 * len(tool_calls)
        return AIMessage(
            content=content,
            tool_calls=tool_calls,
            usage_metadata={"input_tokens": n_in, "output_tokens": n_out, "total_tokens": n_in + n_out},
            response_metadata={
                "model_name": f"offline-{self.role}",
                "finish_reason": "tool_calls" if tool_calls else "stop",
                "offline_rule": rule,
            },
        )

    # -- sync / async whole-message ---------------------------------------------------------------
    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._decide(messages, run_manager, kwargs))])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        # pure CPU, no thread hop; never blocks the server loop (delay is only applied when streaming)
        return ChatResult(generations=[ChatGeneration(message=self._decide(messages, run_manager, kwargs))])

    # -- streaming: word-level text chunks, name-then-fragments tool-call chunks -------------------
    def _chunks(self, msg: AIMessage) -> list[AIMessageChunk]:
        out: list[AIMessageChunk] = []
        words = re.findall(r"\S+\s*|\s+", msg.content if isinstance(msg.content, str) else "")
        step_ = max(self.words_per_chunk, 1)
        for i in range(0, len(words), step_):
            out.append(AIMessageChunk(content="".join(words[i : i + step_])))
        for idx, tc in enumerate(msg.tool_calls):
            args = json.dumps(tc["args"])
            out.append(
                AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {"name": tc["name"], "args": "", "id": tc["id"], "index": idx, "type": "tool_call_chunk"}
                    ],
                )
            )
            for j in range(0, len(args), self.args_chunk_chars):
                out.append(
                    AIMessageChunk(
                        content="",
                        tool_call_chunks=[
                            {
                                "name": None,
                                "args": args[j : j + self.args_chunk_chars],
                                "id": None,
                                "index": idx,
                                "type": "tool_call_chunk",
                            }
                        ],
                    )
                )
        out.append(
            AIMessageChunk(
                content="",
                usage_metadata=msg.usage_metadata,
                response_metadata=msg.response_metadata,
                chunk_position="last",
            )
        )
        return out

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        msg = self._decide(messages, run_manager, kwargs)
        for c in self._chunks(msg):
            gen = ChatGenerationChunk(message=c)
            if run_manager:
                run_manager.on_llm_new_token(str(c.content), chunk=gen)
            yield gen

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        msg = self._decide(messages, run_manager, kwargs)
        for c in self._chunks(msg):
            if self.token_delay_s and c.content:  # pace TEXT tokens only; tool-call fragments/final chunk are instant
                await asyncio.sleep(self.token_delay_s)
            gen = ChatGenerationChunk(message=c)
            if run_manager:
                await run_manager.on_llm_new_token(str(c.content), chunk=gen)
            yield gen


def flaky(times: int, exc: BaseException, then: Responder | Reply | str = "recovered") -> Responder:
    """Failure injection: raise `exc` for the first `times` calls, then answer. The ONE legitimately stateful
    pattern (it models time passing). Build a fresh Script per test; never share across threads/runs."""
    left = {"n": times}
    ok: Responder = then if callable(then) else (lambda _c, _t=then: _t)  # type: ignore[misc]

    def _r(call: Call) -> Reply | str | BaseModel | AIMessage:
        if left["n"] > 0:
            left["n"] -= 1
            return fail(exc)
        return ok(call)

    return _r
