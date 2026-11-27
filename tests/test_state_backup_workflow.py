"""Execute backup contracts and trusted-ref checks from parsed workflows."""

import ast
import copy
import json
from pathlib import Path
import shlex
import shutil
import subprocess

import pytest
import yaml

from lumen_state_storage import _serialize_chat_state


_WORKFLOW_DIR = Path(__file__).parents[1] / ".github" / "workflows"
_SECRET_WORKFLOWS = ("state-backup.yml", "relay-deno.yml")


def _load_workflow(name: str) -> dict:
    return yaml.safe_load((_WORKFLOW_DIR / name).read_text(encoding="utf-8"))


def _workflow_filter() -> str:
    workflow = _load_workflow("state-backup.yml")
    step = next(
        step
        for step in workflow["jobs"]["backup"]["steps"]
        if step.get("name") == "Push to private backup repo"
    )
    for token in shlex.split(step["run"], comments=True, posix=True):
        if token.startswith("backup_filter="):
            return token.split("=", 1)[1]
    raise AssertionError("backup workflow must expose its jq contract to this executable test")


def _uses_secrets(value) -> bool:
    if isinstance(value, dict):
        return any(_uses_secrets(key) or _uses_secrets(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_uses_secrets(item) for item in value)
    return isinstance(value, str) and ("secrets." in value or "secrets[" in value)


def _secret_bearing_jobs(workflow: dict) -> dict:
    return {name: job for name, job in workflow["jobs"].items() if _uses_secrets(job)}


def _workflow_triggers(workflow: dict) -> dict:
    # PyYAML follows YAML 1.1, where the unquoted key "on" is parsed as True.
    return workflow.get("on", workflow.get(True, {}))


def _guard_allows_ref(condition, ref: str) -> bool:
    assert isinstance(condition, str), "secret-bearing job must have a job-level ref guard"
    expression = ast.parse(condition, mode="eval").body
    assert isinstance(expression, ast.Compare)
    assert len(expression.ops) == 1 and isinstance(expression.ops[0], ast.Eq)
    assert len(expression.comparators) == 1
    assert isinstance(expression.left, ast.Attribute) and expression.left.attr == "ref"
    assert isinstance(expression.left.value, ast.Name) and expression.left.value.id == "github"
    expected = expression.comparators[0]
    assert isinstance(expected, ast.Constant) and expected.value == "refs/heads/main"
    return ref == expected.value


@pytest.mark.parametrize("workflow_name", _SECRET_WORKFLOWS)
@pytest.mark.parametrize(
    ("event_name", "ref", "allowed"),
    [
        pytest.param("workflow_dispatch", "refs/heads/main", True, id="main-dispatch"),
        pytest.param("workflow_dispatch", "refs/heads/untrusted", False, id="non-main-dispatch"),
    ],
)
def test_secret_bearing_jobs_require_main_ref(workflow_name, event_name, ref, allowed):
    workflow = _load_workflow(workflow_name)
    assert event_name in _workflow_triggers(workflow)
    jobs = _secret_bearing_jobs(workflow)
    assert jobs, f"{workflow_name} must expose the secret-bearing jobs under test"
    for job_name, job in jobs.items():
        assert _guard_allows_ref(job.get("if"), ref) is allowed, f"{workflow_name}:{job_name}"


def test_state_backup_schedule_remains_allowed():
    workflow = _load_workflow("state-backup.yml")
    assert "schedule" in _workflow_triggers(workflow)
    jobs = _secret_bearing_jobs(workflow)
    assert jobs
    for job_name, job in jobs.items():
        assert _guard_allows_ref(job.get("if"), "refs/heads/main"), job_name


def _valid_payload() -> dict:
    snapshot = _serialize_chat_state({
        "history": [{"role": "user", "content": "hello"}],
        "memory_summary": "summary",
        "memory_pending": [{"role": "user", "content": "pending"}],
        "recent_media_ids": {"42": [["photo", "file-id"]]},
        "user_names": {"42": {"name": "Alice"}},
        "last_activity": 1.0,
        "lang": "en",
        "last_drawn_message_id": 7,
        "last_drawn_at": 2.0,
        "last_drawn_user": 42,
        "last_draw_prompt": "cat",
        "draw_versions": [{"file_id": "version-1"}],
        "draw_redo": [{"file_id": "redo-1"}],
    })
    return {
        "export_schema_version": 1,
        "complete": True,
        "exported_at": "2026-11-25T00:00:00+00:00",
        "chat_count": 1,
        "chats": {"1": snapshot},
        "global_quota": {"munito": {}},
    }


def _run_raw(raw: str) -> subprocess.CompletedProcess:
    if shutil.which("jq") is None:
        pytest.skip("jq is required by the backup workflow")
    expression = f"length == 1 and (.[0] | {_workflow_filter()})"
    return subprocess.run(
        ["jq", "-e", "-s", expression],
        input=raw,
        text=True,
        capture_output=True,
        check=False,
    )


def _run_filter(payload: dict) -> subprocess.CompletedProcess:
    return _run_raw(json.dumps(payload))


def test_backup_workflow_accepts_complete_snapshot():
    result = _run_filter(_valid_payload())
    assert result.returncode == 0, result.stderr


def test_backup_workflow_accepts_replacement_generation():
    payload = _valid_payload()
    payload["chats"]["1"]["replacement_generation"] = "a" * 32
    result = _run_filter(payload)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("generation", ["", "not-a-generation", "A" * 32, 7])
def test_backup_workflow_rejects_malformed_replacement_generation(generation):
    payload = _valid_payload()
    payload["chats"]["1"]["replacement_generation"] = generation
    result = _run_filter(payload)
    assert result.returncode != 0


def test_backup_workflow_accepts_integral_json_numbers():
    payload = _valid_payload()
    payload["export_schema_version"] = 1.0
    payload["chat_count"] = 1.0
    payload["chats"]["1"]["schema_version"] = 2.0
    payload["global_quota"] = {"munito": {"model": {"used": 1.0}}}
    result = _run_filter(payload)
    assert result.returncode == 0, result.stderr


def test_backup_workflow_rejects_overflow_quota_counter():
    payload = _valid_payload()
    payload["global_quota"] = {"munito": {"model": {"used": 1}}}
    raw = json.dumps(payload).replace('"used": 1', '"used": 1e400')
    result = _run_raw(raw)
    assert result.returncode != 0


@pytest.mark.parametrize("field", ["last_drawn_message_id", "last_drawn_user"])
def test_backup_workflow_rejects_overflow_draw_identifiers(field):
    payload = _valid_payload()
    raw = json.dumps(payload).replace(f'"{field}": {payload["chats"]["1"][field]}', f'"{field}": 1e400')
    result = _run_raw(raw)
    assert result.returncode != 0


@pytest.mark.parametrize("field", ["used", "last_drawn_message_id", "last_drawn_user"])
def test_backup_workflow_rejects_huge_integer_literals(field):
    payload = _valid_payload()
    if field == "used":
        payload["global_quota"] = {"munito": {"model": {"used": 1}}}
        raw = json.dumps(payload).replace('"used": 1', '"used": ' + "1" + "0" * 400)
    else:
        raw = json.dumps(payload).replace(
            f'"{field}": {payload["chats"]["1"][field]}',
            f'"{field}": ' + "1" + "0" * 400,
        )
    result = _run_raw(raw)
    assert result.returncode != 0


def test_backup_workflow_rejects_multiple_json_documents():
    valid = json.dumps(_valid_payload())
    result = _run_raw('{"complete":false}\n' + valid)
    assert result.returncode != 0


def test_backup_workflow_rejects_multiple_valid_documents():
    valid = json.dumps(_valid_payload())
    result = _run_raw(valid + "\n" + valid)
    assert result.returncode != 0


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda p: p.update(complete=False), id="incomplete"),
        pytest.param(lambda p: p.update(chat_count=2), id="count-mismatch"),
        pytest.param(lambda p: p.update(export_schema_version=2), id="export-schema"),
        pytest.param(lambda p: p.update(chats={}, chat_count=0), id="empty"),
        pytest.param(lambda p: p.update(global_quota=[]), id="quota-shape"),
        pytest.param(lambda p: p.update(global_quota={"munito": []}), id="quota-provider-shape"),
        pytest.param(lambda p: p.update(global_quota={"munito": {"model": []}}), id="quota-entry-shape"),
        pytest.param(lambda p: p.update(global_quota={"munito": None}), id="quota-null-bucket"),
        pytest.param(lambda p: p.update(global_quota={"munito": {"model": {"used": "bad"}}}), id="quota-used-type"),
        pytest.param(lambda p: p.update(global_quota={"munito": {"model": {"future": float("inf")}}}), id="quota-unknown-infinity"),
        pytest.param(lambda p: p["chats"]["1"]["history"][0].update(score=10 ** 400), id="chat-nested-huge-int"),
        pytest.param(lambda p: p.update(global_quota={"munito": {"model": {"used": -1}}}), id="quota-used-negative"),
        pytest.param(lambda p: p.update(global_quota={"munito": {"model": {"cooldown_until": "bad"}}}), id="quota-cooldown-type"),
        pytest.param(lambda p: p.update(global_quota={"user_daily": {"1": {"total": "bad"}}}), id="quota-daily-type"),
        pytest.param(
            lambda p: p.update(global_quota={"user_daily": {
                str(uid): {"total": 0} for uid in range(1, 20002)
            }}),
            id="quota-daily-over-capacity",
        ),
        pytest.param(lambda p: p.update(global_quota={"stats": None}), id="quota-stats-null"),
        pytest.param(lambda p: p.update(global_quota={"banned_users": {"1": "bad"}}), id="quota-banned-type"),
        pytest.param(lambda p: p.update(global_quota={"last_storage_write_ts": None}), id="quota-timestamp-null"),
        pytest.param(lambda p: p["chats"]["1"].pop("history"), id="missing-history"),
        pytest.param(lambda p: p["chats"]["1"].update(history="bad"), id="history-shape"),
        pytest.param(lambda p: p["chats"]["1"].update(schema_version=999), id="chat-schema"),
        pytest.param(lambda p: p["chats"]["1"].update(recent_media_ids=[]), id="media-shape"),
        pytest.param(lambda p: p["chats"]["1"].update(draw_versions=[{"file_id": 7}]), id="draw-version"),
        pytest.param(
            lambda p: p["chats"].update({"01": p["chats"].pop("1")}),
            id="noncanonical-chat-id",
        ),
        pytest.param(
            lambda p: p["chats"].update({"-0": p["chats"].pop("1")}),
            id="negative-zero-chat-id",
        ),
    ],
)
def test_backup_workflow_rejects_partial_or_malformed_snapshot(mutate):
    payload = copy.deepcopy(_valid_payload())
    mutate(payload)
    result = _run_filter(payload)
    assert result.returncode != 0
