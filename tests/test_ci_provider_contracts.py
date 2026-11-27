"""Contract tests: CI provider env and deploy gates match documented behavior."""

import fnmatch
import subprocess
from pathlib import Path

import pytest
import yaml

_WORKFLOW_DIR = Path(__file__).parents[1] / ".github" / "workflows"

# R9-02 (audit 26.11.2026): persona-eval used default munito targets without passing
# MUNITO_API_KEY, and eval failed on the very first target. Every provider from default
# must have its own key in the step env.
_PROVIDER_KEY_ENV = {"gemini": "GEMINI_API_KEY", "munito": "MUNITO_API_KEY"}


def _load_workflow(name: str) -> dict:
    return yaml.safe_load((_WORKFLOW_DIR / name).read_text(encoding="utf-8"))


def _dispatch_inputs(workflow: dict) -> dict:
    # PyYAML parses an unquoted key "on" as True (YAML 1.1).
    triggers = workflow.get("on", workflow.get(True, {}))
    return triggers["workflow_dispatch"]["inputs"]


def _step(workflow: dict, job: str, name: str) -> dict:
    return next(s for s in workflow["jobs"][job]["steps"] if s.get("name") == name)


def test_persona_eval_passes_keys_for_its_default_providers():
    workflow = _load_workflow("persona-eval.yml")
    inputs = _dispatch_inputs(workflow)
    targets = inputs["models"]["default"] + "," + inputs["judge"]["default"]
    providers = {part.split(":", 1)[0] for part in targets.split(",") if ":" in part}
    assert providers <= set(_PROVIDER_KEY_ENV), f"неизвестный провайдер в дефолте: {providers}"
    env = _step(workflow, "eval", "Persona eval")["env"]
    for provider in sorted(providers):
        required = _PROVIDER_KEY_ENV[provider]
        assert required in env, f"шаг Persona eval не передаёт {required} для {provider}-таргетов"
        assert f"secrets.{required}" in str(env[required])


def test_deploy_proxy_steps_are_gated_off_by_default():
    # README (R9-01, audit 26.11.2026) describes the job as green, while the deploy steps are
    # disabled until relay migration; the test guards this equality between the description and config.
    workflow = _load_workflow("deploy-proxy.yml")
    steps = [
        s for s in workflow["jobs"]["deploy"]["steps"]
        if str(s.get("name", "")).startswith("Deploy to")
    ]
    assert len(steps) == 2
    for step in steps:
        assert step["if"] == "vars.DENO_PROXY_DEPLOY == '1'"


def _docker_ignored(path: str) -> bool:
    # R8-04: check the simple Docker include rules used here without
    # an external matcher; nested devtrace is covered by a separate ** pattern.
    patterns = [
        line.strip() for line in
        (Path(__file__).parents[1] / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    parts = Path(path).parts
    candidates = [path, *(str(Path(*parts[:i])) for i in range(1, len(parts)))]
    return any(
        fnmatch.fnmatchcase(candidate, pattern.rstrip("/"))
        for pattern in patterns if not pattern.startswith("!")
        for candidate in candidates
    )


@pytest.mark.parametrize("path", [
    "devtrace.jsonl",
    "devtrace.jsonl.1",
    "devtrace.private.jsonl",
    "logs/devtrace.jsonl.2",
    "chats/123.json",
    "chat_state.json",
    "chat_state.tmp",
    "chat_index.json",
    "chat_index.tmp",
    "chat_replacements.json",
    "chat_replacements.tmp",
    "global_quota.json",
    "global_quota.tmp",
    "export_state.json",
    "audit/engineering-audit.jsonl",
])
def test_docker_context_excludes_private_runtime_state(path):
    # COPY . /app must not bake in conversation texts, history, or service indexes.
    assert _docker_ignored(path), path


@pytest.mark.parametrize("path", [
    "bot.py", "lumen_chat_state.py", "lumen_devtrace.py", "system_prompt.py",
    "Dockerfile", "requirements.txt", "LICENSE",
])
def test_docker_context_keeps_runtime_source(path):
    # An overly broad state/*.json must not cut out source code or configuration.
    assert not _docker_ignored(path), path


@pytest.mark.parametrize("path", [
    "chats/123.json",
    "chat_index.json",
    "chat_index.json.1",
    "chat_replacements.json",
    "chat_replacements.tmp",
    "export_state.json.1",
    "devtrace.private.jsonl",
    "logs/devtrace.jsonl.2",
])
def test_git_ignores_private_runtime_state(path):
    # Local state must not become visible to git status or accidental commits.
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", path],
        cwd=Path(__file__).parents[1],
        check=False,
    )
    assert result.returncode == 0, path
