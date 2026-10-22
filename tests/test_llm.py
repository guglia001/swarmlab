"""Tests del backend LLM. Ninguno invoca el CLI real: se inyecta un runner falso."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field

from swarmlab.llm import (
    AuthError,
    CallBudget,
    CallBudgetExceeded,
    CallLog,
    ClaudeCLI,
    Conversation,
    FakeClaudeCLI,
    JSONExtractionError,
    LLMCallFailed,
    ProcessOutput,
    StructuredOutputError,
    UsageLimitError,
    classify_failure,
    count_logged_calls,
    extract_json,
    parse_structured,
    reset_hint,
    run_subprocess,
    sanitized_env,
)


class Item(BaseModel):
    name: str
    score: float = Field(ge=-1, le=1)


class Items(BaseModel):
    items: list[Item]


def payload(result: str = "", structured: Any = None, **extra: Any) -> str:
    body: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": result,
        "session_id": extra.pop("session_id", "sess-1"),
        "duration_ms": 1000,
        "duration_api_ms": 900,
        "num_turns": 1,
        "total_cost_usd": 0.001,
        "usage": {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 100},
        "modelUsage": {"claude-haiku-5-5": {}},
    }
    if structured is not None:
        body["structured_output"] = structured
    body.update(extra)
    return json.dumps(body)


class ScriptedRunner:
    """Devuelve salidas predefinidas y registra cada invocación."""

    def __init__(self, outputs: list[ProcessOutput | BaseException]) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, args: list[str], stdin: str, cwd: Path, env: dict[str, str], timeout: float):
        self.calls.append({"args": args, "stdin": stdin, "cwd": cwd, "env": env, "timeout": timeout})
        out = self.outputs.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out


def ok(stdout: str) -> ProcessOutput:
    return ProcessOutput(0, stdout, "", 2500)


def make_cli(tmp_path: Path, runner: ScriptedRunner, **kw: Any) -> ClaudeCLI:
    async def no_sleep(_: float) -> None:
        return None

    return ClaudeCLI(
        sandbox_dir=tmp_path / ".sandbox",
        log=CallLog(tmp_path / "llm_calls.jsonl"),
        runner=runner,
        sleep=no_sleep,
        **kw,
    )


# ----------------------------------------------------------------------------- extracción JSON


@pytest.mark.parametrize(
    "text",
    [
        '{"items": [{"name": "a", "score": 0.5}]}',
        'Claro, aquí tienes:\n```json\n{"items": [{"name": "a", "score": 0.5}]}\n```\nEspero que sirva.',
        'Resultado: {"items": [{"name": "a", "score": 0.5},]} -- fin',
        '```\n{"items": [{"name": "a", "score": 0.5}]}\n```',
        '\ufeff  {"items": [{"name": "a", "score": 0.5}]}  ',
    ],
)
def test_extract_json_tolerates_noise(text: str) -> None:
    assert extract_json(text) == {"items": [{"name": "a", "score": 0.5}]}


def test_extract_json_respects_braces_inside_strings() -> None:
    text = 'Nota {no es json}. Datos: {"items": [{"name": "llaves } y { dentro", "score": 0}]}'
    assert extract_json(text)["items"][0]["name"] == "llaves } y { dentro"


def test_extract_json_accepts_raw_newlines_in_strings() -> None:
    assert extract_json('{"name": "línea 1\nlínea 2", "score": 0}')["name"] == "línea 1\nlínea 2"


@pytest.mark.parametrize("text", ["", "no hay nada aquí", '{"items": [1, 2', "```json\n{roto: }\n```"])
def test_extract_json_rejects_garbage(text: str) -> None:
    with pytest.raises(JSONExtractionError):
        extract_json(text)


def test_parse_structured_prefers_structured_output_and_coerces_bare_list() -> None:
    data = parse_structured({"structured_output": [{"name": "x", "score": 1}], "result": "ignored"}, Items)
    assert data.items[0].name == "x"
    data = parse_structured({"result": 'texto ```json\n{"items": []}\n```'}, Items)
    assert data.items == []


# ----------------------------------------------------------------------------- clasificación


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("You've hit your session limit · resets 3:45pm", "usage_limit"),
        ("You've hit your weekly limit · resets Mon 12:00am", "usage_limit"),
        ("Claude AI usage limit reached|1767225600", "usage_limit"),
        ("API Error: Server is temporarily limiting requests (not your usage limit)", "transient"),
        ("API Error: Repeated 529 Overloaded errors", "transient"),
        ("Not logged in · Please run /login", "auth"),
        ("Error: --json-schema is not a valid JSON Schema", "fatal"),
        ("No conversation found with session ID: abc", "fatal"),
        ("API Error: Request rejected (429) · this may be a temporary capacity issue", "rate_limit"),
        ("socket hang up", "transient"),
    ],
)
def test_classify_failure(text: str, kind: str) -> None:
    assert classify_failure(text) == kind


def test_reset_hint() -> None:
    assert reset_hint("You've hit your session limit · resets 3:45pm") == "3:45pm"
    assert reset_hint("sin pista") is None


def test_sanitized_env_strips_api_credentials() -> None:
    env = sanitized_env(
        {"PATH": "/bin", "ANTHROPIC_API_KEY": "x", "ANTHROPIC_BASE_URL": "y", "CLAUDECODE": "1", "HOME": "/h"}
    )
    assert env == {"PATH": "/bin", "HOME": "/h"}


# ----------------------------------------------------------------------------- ClaudeCLI


def test_complete_uses_pure_llm_flags_and_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    runner = ScriptedRunner([ok(payload("pong"))])
    cli = make_cli(tmp_path, runner)
    res = asyncio.run(cli.complete("di pong", system="sys", model="sonnet", purpose="t"))
    assert res.text == "pong"
    call = runner.calls[0]
    args = call["args"]
    assert call["stdin"] == "di pong" and "di pong" not in args
    for flag in (
        "-p",
        "--safe-mode",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
    ):
        assert flag in args
    assert args[args.index("--tools") + 1] == ""
    assert args[args.index("--setting-sources") + 1] == ""
    assert args[args.index("--max-turns") + 1] == "1"
    assert args[args.index("--model") + 1] == "sonnet"
    assert args[args.index("--output-format") + 1] == "json"
    assert "ANTHROPIC_API_KEY" not in call["env"]
    assert call["cwd"] == tmp_path / ".sandbox"
    assert res.usage.startup_ms == 1500 and res.usage.total_input_tokens == 110
    assert count_logged_calls(tmp_path / "llm_calls.jsonl") == 1


def test_native_schema_is_passed_and_validated(tmp_path: Path) -> None:
    runner = ScriptedRunner([ok(payload("", structured={"items": [{"name": "a", "score": 0.1}]}))])
    cli = make_cli(tmp_path, runner)
    res = asyncio.run(cli.complete("x", schema=Items))
    assert res.parsed(Items).items[0].name == "a"
    schema_arg = json.loads(runner.calls[0]["args"][runner.calls[0]["args"].index("--json-schema") + 1])
    assert schema_arg["properties"]["items"]["type"] == "array"
    assert "title" not in schema_arg


def test_prompt_schema_fallback_and_repair_retry(tmp_path: Path) -> None:
    runner = ScriptedRunner(
        [
            ok(payload("Lo siento, aquí va: {items: rotos}")),
            ok(payload('Perdón:\n```json\n{"items": [{"name": "b", "score": 2}]}\n```')),
            ok(payload('{"items": [{"name": "b", "score": 0.9}]}')),
        ]
    )
    cli = make_cli(tmp_path, runner, native_schema=False)
    res = asyncio.run(cli.complete("dame items", schema=Items))
    assert res.parsed(Items).items[0].score == 0.9
    assert res.attempts == 3
    assert "--json-schema" not in runner.calls[0]["args"]
    assert "JSON Schema" in runner.calls[0]["stdin"]
    assert "CORRECCIÓN NECESARIA" in runner.calls[1]["stdin"]
    assert "less than or equal to 1" in runner.calls[2]["stdin"]
    assert cli.stats.invalid_outputs == 2


def test_schema_retries_exhausted(tmp_path: Path) -> None:
    runner = ScriptedRunner([ok(payload("nada")), ok(payload("nada")), ok(payload("nada"))])
    cli = make_cli(tmp_path, runner, native_schema=False, schema_retries=2)
    with pytest.raises(StructuredOutputError):
        asyncio.run(cli.complete("x", schema=Items))
    assert len(runner.calls) == 3


def test_transient_errors_are_retried(tmp_path: Path) -> None:
    runner = ScriptedRunner(
        [
            TimeoutError(),
            ProcessOutput(1, payload("API Error: Repeated 529 Overloaded errors", is_error=True), "", 10),
            ok(payload("bien")),
        ]
    )
    cli = make_cli(tmp_path, runner)
    res = asyncio.run(cli.complete("x"))
    assert res.text == "bien" and res.attempts == 3
    assert cli.stats.retries == 2 and cli.stats.calls == 3


def test_retries_exhausted_raise(tmp_path: Path) -> None:
    runner = ScriptedRunner([ProcessOutput(1, "", "boom", 10)] * 3)
    cli = make_cli(tmp_path, runner, max_retries=2)
    with pytest.raises(LLMCallFailed):
        asyncio.run(cli.complete("x"))


def test_usage_limit_is_not_retried(tmp_path: Path) -> None:
    msg = payload("You've hit your session limit · resets 3:45pm", is_error=True)
    runner = ScriptedRunner([ProcessOutput(1, msg, "", 10), ok(payload("nunca"))])
    cli = make_cli(tmp_path, runner)
    with pytest.raises(UsageLimitError) as info:
        asyncio.run(cli.complete("x"))
    assert info.value.reset_hint == "3:45pm"
    assert len(runner.calls) == 1


def test_persistent_rate_limit_becomes_usage_limit(tmp_path: Path) -> None:
    msg = ProcessOutput(1, payload("API Error: Request rejected (429)", is_error=True), "", 10)
    runner = ScriptedRunner([msg, msg])
    cli = make_cli(tmp_path, runner, max_retries=1)
    with pytest.raises(UsageLimitError):
        asyncio.run(cli.complete("x"))


def test_auth_error(tmp_path: Path) -> None:
    runner = ScriptedRunner(
        [ProcessOutput(1, payload("Not logged in · Please run /login", is_error=True), "", 5)]
    )
    with pytest.raises(AuthError):
        asyncio.run(make_cli(tmp_path, runner).complete("x"))


def test_budget_cap_and_reserve(tmp_path: Path) -> None:
    budget = CallBudget(3, reserve=1)
    runner = ScriptedRunner([ok(payload("a")), ok(payload("b")), ok(payload("c"))])
    cli = make_cli(tmp_path, runner, budget=budget)
    asyncio.run(cli.complete("1"))
    asyncio.run(cli.complete("2"))
    with pytest.raises(CallBudgetExceeded):
        asyncio.run(cli.complete("3"))
    with budget.reserve_unlocked():
        assert asyncio.run(cli.complete("3")).text == "c"
    assert budget.remaining == 0


def test_session_flags_and_conversation(tmp_path: Path) -> None:
    runner = ScriptedRunner([ok(payload("hola", session_id="s-42")), ok(payload("sigo", session_id="s-42"))])
    cli = make_cli(tmp_path, runner)
    conv = Conversation(
        cli, system="sys", model="sonnet", purpose="chat", preamble="DOSSIER", session_id="s-42"
    )
    asyncio.run(conv.send("primera"))
    asyncio.run(conv.send("segunda"))
    first, second = (c["args"] for c in runner.calls)
    assert first[first.index("--session-id") + 1] == "s-42" and "--no-session-persistence" not in first
    assert second[second.index("--resume") + 1] == "s-42"
    assert runner.calls[0]["stdin"].startswith("DOSSIER") and runner.calls[1]["stdin"] == "segunda"


def test_new_session_retry_uses_fresh_id(tmp_path: Path) -> None:
    runner = ScriptedRunner([TimeoutError(), ok(payload("ok", session_id="nuevo"))])
    cli = make_cli(tmp_path, runner)
    res = asyncio.run(cli.complete("x", session_id="orig"))
    a1, a2 = (c["args"] for c in runner.calls)
    assert a1[a1.index("--session-id") + 1] == "orig"
    assert a2[a2.index("--session-id") + 1] != "orig"
    assert res.session_id == "nuevo"


def test_run_subprocess_kills_on_timeout(tmp_path: Path) -> None:
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        asyncio.run(run_subprocess(["sleep", "30"], "", tmp_path, {"PATH": "/usr/bin:/bin"}, 0.3))
    assert time.monotonic() - t0 < 5


# ----------------------------------------------------------------------------- Fake


def test_fake_is_deterministic_and_simulates_limits(tmp_path: Path) -> None:
    def responder(req: Any) -> dict[str, Any]:
        return {"items": [{"name": f"n{req.rng.randint(0, 999)}", "score": 0}]}

    a = FakeClaudeCLI(responders={"p": responder}, seed=1)
    b = FakeClaudeCLI(responders={"p": responder}, seed=1, usage_limit_at=2)
    ra = asyncio.run(a.complete("mismo prompt", schema=Items, purpose="p"))
    rb = asyncio.run(b.complete("mismo prompt", schema=Items, purpose="p"))
    assert ra.parsed(Items) == rb.parsed(Items)
    with pytest.raises(UsageLimitError):
        asyncio.run(b.complete("otro", schema=Items, purpose="p"))
    with pytest.raises(LLMCallFailed):
        asyncio.run(a.complete("x", purpose="sin-responder"))
