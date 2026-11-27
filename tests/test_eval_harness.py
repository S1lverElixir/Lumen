"""R8-01/R8-06/R8-07: safe harness failure and an honest run verdict.

Why this test: the harness_eval agent runs on the owner's machine, where keys are in the environment, and any leaked variable reaches the model's context; after the agent, the judge runs pytest on the sandbox code with the same host environment.
What breaks it: agent_env copies all of os.environ again, judge calls lose the env argument, or bash edits no longer deny .env reads.
Why existing tests miss it: there were previously no tests for harness_eval.
"""

import argparse
import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# Names carrying secrets (values in the test are fake; production keys are not read).
SECRET_NAMES = [
    "GEMINI_API_KEY",
    "MUNITO_API_KEY",
    "BOT_TOKEN",
    "HF_TOKEN",
    "GITHUB_TOKEN",
    "DENO_DEPLOY_TOKEN",
    "SENTRY_AUTH_TOKEN",
    "UPSTASH_PASSWORD",
    "TELEGRAM_WEBHOOK_URL",
    "SENTRY_DSN",
    "GATEWAY_AUTH",
    "LEGACY_TOKEN2",
]
# Toolchain variables that must survive the cleanup.
KEEP_NAMES = ["PATH", "NO_PROXY", "PYTHONIOENCODING", "LUMEN_EVAL_BENIGN"]


def _load_harness(monkeypatch):
    monkeypatch.syspath_prepend(str(REPO / "eval"))
    for name in SECRET_NAMES + KEEP_NAMES:
        monkeypatch.setenv(name, "fake-value")
    import harness_eval

    return harness_eval


def test_agent_env_scrubs_secret_variables(monkeypatch, tmp_path):
    # R8-01 (review 26.11.2026): the agent received all of os.environ; filtering by names
    # removes key carriers and keeps PATH and harmless variables.
    harness_eval = _load_harness(monkeypatch)
    env = harness_eval.agent_env(tmp_path)
    leaked = sorted(name for name in SECRET_NAMES if name in env)
    assert leaked == []
    for name in KEEP_NAMES:
        assert env.get(name) == "fake-value", name
    assert env["PIP_TARGET"] == str(tmp_path / ".pip-target")
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


def test_judge_pytest_runs_without_secret_env(monkeypatch, tmp_path):
    # The judge runs pytest on the agent-modified code: env must be cleaned,
    # otherwise, silently inherited os.environ would leak into the sandbox conftest.
    harness_eval = _load_harness(monkeypatch)
    captured = {}

    def fake_run(args, **kwargs):
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(harness_eval.subprocess, "run", fake_run)
    harness_eval.pytest_state(tmp_path)
    env = captured["env"]
    assert env is not None, "судья наследует окружение хоста целиком"
    assert sorted(name for name in SECRET_NAMES if name in env) == []


def test_keep_sandbox_git_runs_without_secret_env(monkeypatch, tmp_path):
    # R8-01 (review 26.11.2026): git in the sandbox after the agent could execute
    # an agent filter/hook from .git/config — its env must also be cleaned.
    harness_eval = _load_harness(monkeypatch)
    captured = []
    sandbox = tmp_path / "sb"
    sandbox.mkdir()

    def fake_run(args, **kwargs):
        captured.append(kwargs.get("env"))
        import subprocess

        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(harness_eval.subprocess, "run", fake_run)
    harness_eval.keep_sandbox(sandbox, tmp_path / "kept", [])
    assert captured, "git не вызывался"
    for env in captured:
        assert env is not None, "git на песочнице наследует окружение хоста"
        assert sorted(name for name in SECRET_NAMES if name in env) == []


def test_bash_permission_denies_env_file_reads(monkeypatch):
    # the read tool already blocks *.env, but bash could simply cat the file:
    # deny for explicit calls to both shells (the host is Windows, using PowerShell).
    harness_eval = _load_harness(monkeypatch)
    bash = harness_eval.sandbox_config()["permission"]["bash"]
    env_patterns = [p for p in bash if ".env" in p]
    assert any(p.startswith("cat ") for p in env_patterns), env_patterns
    assert any(p.startswith("Get-Content ") for p in env_patterns), env_patterns
    assert all(bash[p] == "deny" for p in env_patterns)


@pytest.mark.parametrize("entry", ["run_one", "cmd_run", "cmd_batch", "cmd_propose", "run_agent"])
def test_agent_paths_refuse_without_host_ack_before_side_effects(monkeypatch, tmp_path, entry):
    # R8-01: scrub/env does not prevent the agent from reading the host filesystem; the
    # refusal must precede clone, directory/transcript creation, CLI lookup and OpenCode launch.
    harness_eval = _load_harness(monkeypatch)
    monkeypatch.delenv("HARNESS_UNSAFE_HOST_ACK", raising=False)
    side_effects = []
    monkeypatch.setattr(harness_eval, "load_tasks", lambda: side_effects.append("tasks"))
    monkeypatch.setattr(harness_eval, "find_cli", lambda: side_effects.append("cli"))
    if entry == "run_one":
        call = lambda: harness_eval.run_one("t", "base", tmp_path / "out", None, "model")
    elif entry == "cmd_run":
        args = argparse.Namespace(task="t", variant="base", out=str(tmp_path / "out"), agents_md=None, model=None)
        call = lambda: harness_eval.cmd_run(args)
    elif entry == "cmd_batch":
        args = argparse.Namespace(tasks=None, variant="base", out=str(tmp_path / "out"), agents_md=None,
                                  model=None, jobs=1)
        call = lambda: harness_eval.cmd_batch(args)
    elif entry == "cmd_propose":
        args = argparse.Namespace(base_dir=str(tmp_path / "base"), model="model")
        call = lambda: harness_eval.cmd_propose(args)
    else:
        call = lambda: harness_eval.run_agent(tmp_path, "prompt", "model", tmp_path / "trace.jsonl", 1)
    with pytest.raises(RuntimeError, match="HARNESS_UNSAFE_HOST_ACK=1") as exc:
        call()
    assert "абсолютному пути" in str(exc.value)
    assert "VM/контейнере" in str(exc.value)
    assert side_effects == []
    assert not (tmp_path / "out").exists()
    assert not (tmp_path / "base").exists()
    assert not (tmp_path / "trace.jsonl").exists()


def test_explicit_host_ack_allows_run_entry(monkeypatch, tmp_path):
    # Explicit consent accepts the risk but does not claim isolation.
    harness_eval = _load_harness(monkeypatch)
    monkeypatch.setenv("HARNESS_UNSAFE_HOST_ACK", "1")
    calls = []
    monkeypatch.setattr(harness_eval, "run_one", lambda *args: calls.append(args) or {"solved": True})
    args = argparse.Namespace(task="t", variant="base", out=str(tmp_path), agents_md=None, model="model")
    assert harness_eval.cmd_run(args) == 0
    assert calls == [("t", "base", tmp_path, None, "model")]


@pytest.mark.parametrize("solved", [False, True])
def test_run_exit_reports_payload_verdict(monkeypatch, tmp_path, solved):
    # An honest JSON payload must also control the process exit status.
    harness_eval = _load_harness(monkeypatch)
    monkeypatch.setenv("HARNESS_UNSAFE_HOST_ACK", "1")
    monkeypatch.setattr(harness_eval, "run_one", lambda *args: {"solved": solved})
    args = argparse.Namespace(task="t", variant="base", out=str(tmp_path), agents_md=None, model="model")
    assert harness_eval.cmd_run(args) == (0 if solved else 1)


@pytest.mark.parametrize("value", ["", "0", "true", "yes"])
def test_host_ack_requires_exact_value(monkeypatch, value):
    harness_eval = _load_harness(monkeypatch)
    monkeypatch.setenv("HARNESS_UNSAFE_HOST_ACK", value)
    with pytest.raises(RuntimeError, match="HARNESS_UNSAFE_HOST_ACK=1"):
        harness_eval.require_unsafe_host_ack()


@pytest.mark.parametrize("entry", ["check", "probe", "report"])
def test_offline_commands_do_not_require_host_ack(monkeypatch, tmp_path, entry):
    harness_eval = _load_harness(monkeypatch)
    monkeypatch.delenv("HARNESS_UNSAFE_HOST_ACK", raising=False)
    if entry == "check":
        monkeypatch.setattr(harness_eval, "load_tasks", lambda: [])
        args = argparse.Namespace(out=str(tmp_path))
    elif entry == "probe":
        monkeypatch.setattr(harness_eval, "compute_targets", lambda task: {"fail_to_pass": [], "broken_at_fix": []})
        monkeypatch.setattr(harness_eval, "git", lambda *args: "base")
        args = argparse.Namespace(commits=["abc:tests/test_x.py"])
    else:
        args = argparse.Namespace(out=str(tmp_path))
    assert getattr(harness_eval, f"cmd_{entry}")(args) == 0


@pytest.mark.parametrize("returncode", [0, 1, 2])
def test_pytest_state_reports_process_success_separately(monkeypatch, tmp_path, returncode):
    # R8-06: a collection/usage error may leave no FAILED line in the output.
    harness_eval = _load_harness(monkeypatch)
    monkeypatch.setattr(
        harness_eval, "run_cmd",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, returncode, "pytest usage error\n", ""),
    )
    failed, output, ok = harness_eval.pytest_state(tmp_path)
    assert failed == set()
    assert "usage error" in output
    assert ok is (returncode == 0)


@pytest.mark.parametrize("failure", [None, "targets", "target_failed", "new_failures", "tests_touched", "pyflakes", "pytest", "agent"])
def test_solved_requires_every_gate_dimension(monkeypatch, failure):
    # R8-06: each measurement must independently establish solved.
    harness_eval = _load_harness(monkeypatch)

    result = harness_eval.RunResult(
        task="t", variant="base", model="m", pre_fail=["tests/test_x.py::test_old"],
        pyflakes_ok=True, pytest_ok=True, agent_ok=True,
    )
    targets = ["tests/test_x.py::test_target"]
    if failure == "targets":
        targets = []
    elif failure == "target_failed":
        result.post_fail = targets
    elif failure == "new_failures":
        result.post_fail = ["tests/test_x.py::test_new"]
    elif failure == "tests_touched":
        result.violations["tests_touched"] = ["tests/test_x.py"]
    elif failure == "pyflakes":
        result.pyflakes_ok = False
    elif failure == "pytest":
        result.pytest_ok = False
    elif failure == "agent":
        result.agent_ok = False
    harness_eval.score(harness_eval.Task("t", "fix", [], "subject"), result, targets)
    assert result.solved is (failure is None)


@pytest.mark.parametrize("failure", [None, "target", "new", "tests_touched", "pyflakes", "pytest", "agent"])
def test_run_one_propagates_all_gate_statuses_to_payload(monkeypatch, tmp_path, failure):
    # run_one pair: a crashed process without a FAILED line and a nonzero agent exit
    # must change the payload, not just the internal score.
    harness_eval = _load_harness(monkeypatch)
    monkeypatch.setenv("HARNESS_UNSAFE_HOST_ACK", "1")
    task = harness_eval.Task("t", "fix", ["tests/test_x.py"], "subject", "base")
    target = "tests/test_x.py::test_target"
    sandbox = tmp_path / "sandbox"
    kept = tmp_path / "kept"
    sandbox.mkdir()
    kept.mkdir()
    monkeypatch.setattr(harness_eval, "load_tasks", lambda: [task])
    monkeypatch.setattr(harness_eval, "prepare", lambda *args: sandbox)
    monkeypatch.setattr(harness_eval, "git", lambda *args, **kwargs: "")
    monkeypatch.setattr(harness_eval, "compute_targets", lambda task: {"fail_to_pass": [target]})
    changed_files = ["tests/test_x.py"] if failure == "tests_touched" else []
    monkeypatch.setattr(harness_eval, "keep_sandbox", lambda *args: {"changed_files": changed_files, "diff": ""})
    monkeypatch.setattr(harness_eval, "session_from_db", lambda sandbox: None)
    post_fail = []
    if failure == "target":
        post_fail = [target]
    elif failure == "new":
        post_fail = ["tests/test_x.py::test_new"]
    states = iter([
        ({target}, "base output", False),
        (set(post_fail), "post output", failure != "pytest"),
    ])
    monkeypatch.setattr(harness_eval, "pytest_state", lambda path: next(states))
    monkeypatch.setattr(harness_eval, "pyflakes_state", lambda path: (failure != "pyflakes", "lint"))
    monkeypatch.setattr(harness_eval, "run_agent", lambda *args: ("final", "trace", failure != "agent"))
    out_dir = tmp_path / "out"
    payload = harness_eval.run_one("t", "base", out_dir, None, "model")
    assert payload["solved"] is (failure is None)
    assert payload["agent_ok"] is (failure != "agent")
    assert payload["pytest_ok"] is (failure != "pytest")
    assert payload["pyflakes_ok"] is (failure != "pyflakes")
    assert json.loads((out_dir / "t.base.json").read_text(encoding="utf-8"))["solved"] is (failure is None)


@pytest.mark.parametrize("returncode", [0, 1])
def test_run_agent_returns_exit_success(monkeypatch, tmp_path, returncode):
    harness_eval = _load_harness(monkeypatch)
    monkeypatch.setenv("HARNESS_UNSAFE_HOST_ACK", "1")
    monkeypatch.setattr(harness_eval, "find_cli", lambda: "opencode")
    monkeypatch.setattr(harness_eval, "session_from_db", lambda sandbox: None)

    class Proc:
        def wait(self, timeout):
            self.returncode = returncode

    monkeypatch.setattr(harness_eval.subprocess, "Popen", lambda *args, **kwargs: Proc())
    _, _, ok = harness_eval.run_agent(tmp_path, "prompt", "model", tmp_path / "trace.jsonl", 1)
    assert ok is (returncode == 0)


def test_targets_cache_key_includes_every_behavior_input(monkeypatch):
    # R8-07: the old task@fix reused goals after any input changed.
    harness_eval = _load_harness(monkeypatch)
    task = harness_eval.Task("t", "fix", ["tests/test_x.py"], "subject", "base")
    baseline = harness_eval.targets_cache_key(task)
    variants = [
        replace(task, id="other"), replace(task, fix="other-fix"),
        replace(task, tests=["tests/test_y.py"]), replace(task, base="other-base"),
    ]
    for changed in variants:
        assert harness_eval.targets_cache_key(changed) != baseline
    monkeypatch.setattr(harness_eval, "AGENT_MODEL", "other-model")
    assert harness_eval.targets_cache_key(task) != baseline
    monkeypatch.setattr(harness_eval, "HARNESS_VERSION", "next")
    assert harness_eval.targets_cache_key(task) != baseline


def test_targets_cache_uses_unique_atomic_temp_files(monkeypatch, tmp_path):
    # Two parallel writers must have different tmp files before atomic replace.
    harness_eval = _load_harness(monkeypatch)
    cache_file = tmp_path / "targets.json"
    barrier = threading.Barrier(2)
    replace_lock = threading.Lock()
    original_replace = Path.replace
    temp_names = []

    def paused_replace(path, target):
        temp_names.append(path.name)
        barrier.wait(timeout=5)
        # Windows rejects simultaneous replaces of one target; serialize only the final rename.
        with replace_lock:
            return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", paused_replace)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(
            lambda value: harness_eval.write_targets_cache(cache_file, {"key": {"value": value}}),
            [1, 2],
        ))
    assert len(set(temp_names)) == 2
    assert all(name.startswith(".targets.json.") and name.endswith(".tmp") for name in temp_names)
    assert not list(tmp_path.glob("*.tmp"))
    assert not list(tmp_path.glob(".*.tmp"))
    assert json.loads(cache_file.read_text(encoding="utf-8"))["key"]["value"] in (1, 2)


def test_targets_cache_merges_concurrent_updates_without_losing_keys(monkeypatch, tmp_path):
    # Unique temporary files prevent collisions; the read-merge-write must also be serialized.
    harness_eval = _load_harness(monkeypatch)
    cache_file = tmp_path / "targets.json"
    original_read = Path.read_text
    original_write = harness_eval.write_targets_cache

    def guarded_read(path, *args, **kwargs):
        if path == cache_file:
            assert harness_eval._TARGETS_CACHE_LOCK.locked()
        return original_read(path, *args, **kwargs)

    def guarded_write(path, cache):
        assert harness_eval._TARGETS_CACHE_LOCK.locked()
        original_write(path, cache)

    monkeypatch.setattr(Path, "read_text", guarded_read)
    monkeypatch.setattr(harness_eval, "write_targets_cache", guarded_write)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(
            lambda key: harness_eval.update_targets_cache(cache_file, key, {"task": key}),
            ("a", "b"),
        ))
    assert json.loads(original_read(cache_file, encoding="utf-8")) == {
        "a": {"task": "a"},
        "b": {"task": "b"},
    }


def test_targets_cache_cleans_temp_and_preserves_old_file_on_replace_error(monkeypatch, tmp_path):
    harness_eval = _load_harness(monkeypatch)
    cache_file = tmp_path / "targets.json"
    cache_file.write_text('{"old": true}', encoding="utf-8")

    def fail_replace(path, target):
        raise OSError("replace failed")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        harness_eval.write_targets_cache(cache_file, {"new": True})
    assert json.loads(cache_file.read_text(encoding="utf-8")) == {"old": True}
    assert list(tmp_path.iterdir()) == [cache_file]


def _load_persona(monkeypatch):
    monkeypatch.syspath_prepend(str(REPO / "eval"))
    import persona_models
    return persona_models


def test_persona_env_paths_are_worktree_or_explicit(monkeypatch, tmp_path):
    # R8-16: a personal absolute path is no longer a hidden fallback.
    persona_models = _load_persona(monkeypatch)
    repo = tmp_path / "worktree"
    monkeypatch.setattr(persona_models, "REPO", repo)
    monkeypatch.delenv("LUMEN_ENV_DIR", raising=False)
    assert persona_models.env_files() == [repo / ".env"]
    external = tmp_path / "external"
    monkeypatch.setenv("LUMEN_ENV_DIR", str(external))
    assert persona_models.env_files() == [repo / ".env", external / ".env"]
    monkeypatch.setenv("LUMEN_ENV_DIR", str(repo))
    assert persona_models.env_files() == [repo / ".env"]


def test_persona_missing_explicit_env_dir_is_ignored_without_reading_values(monkeypatch, tmp_path):
    persona_models = _load_persona(monkeypatch)
    monkeypatch.setattr(persona_models, "REPO", tmp_path / "missing-worktree")
    monkeypatch.setenv("LUMEN_ENV_DIR", str(tmp_path / "missing-external"))

    def unexpected_read(*args, **kwargs):
        raise AssertionError("несуществующий .env не должен читаться")

    monkeypatch.setattr(Path, "read_text", unexpected_read)
    persona_models.load_env()


def test_casual_questions_have_russian_language_metadata(monkeypatch):
    # R8-13: answer_script_ok(None, ...) was always False and marked a Russian answer as an error.
    persona_models = _load_persona(monkeypatch)
    data = persona_models.load_questions(REPO / "eval" / "persona_questions_casual.json")
    assert data["questions"]
    for question in data["questions"]:
        assert question["lang"] == "ru"
        assert persona_models.answer_script_ok(question["lang"], "Привет! Я Lumen.") is True
        assert persona_models.answer_script_ok(question["lang"], "Hello, I am Lumen.") is False


@pytest.mark.parametrize("failed_tasks", [set(), {"bad"}])
@pytest.mark.parametrize("unsolved_tasks", [set(), {"bad"}])
def test_batch_exit_reports_worker_failures(monkeypatch, tmp_path, failed_tasks, unsolved_tasks):
    # CI must not report a green batch when an individual run crashed or stayed unsolved.
    harness_eval = _load_harness(monkeypatch)
    monkeypatch.setenv("HARNESS_UNSAFE_HOST_ACK", "1")
    monkeypatch.setattr(harness_eval, "load_tasks", lambda: [
        harness_eval.Task("good", "fix", [], "subject"),
        harness_eval.Task("bad", "fix", [], "subject"),
    ])
    attempts = []

    def run_one(task_id, *args):
        attempts.append(task_id)
        if task_id in failed_tasks:
            raise RuntimeError("worker failed")
        return {"solved": task_id not in unsolved_tasks}

    monkeypatch.setattr(harness_eval, "run_one", run_one)
    args = argparse.Namespace(tasks=None, variant="base", out=str(tmp_path / "out"), agents_md=None,
                              model="model", jobs=2)
    failed = failed_tasks or unsolved_tasks
    assert harness_eval.cmd_batch(args) == (1 if failed else 0)
    assert sorted(attempts) == ["bad", "good"]


@pytest.mark.parametrize("entry", ["valid", "no_target", "broken"])
def test_check_exit_reports_any_invalid_task(monkeypatch, tmp_path, entry):
    # R3-06: check all tasks and make malformed eval fixtures visible to the caller.
    harness_eval = _load_harness(monkeypatch)
    monkeypatch.setattr(harness_eval, "load_tasks", lambda: [
        harness_eval.Task("good", "fix", [], "subject"),
        harness_eval.Task("bad", "fix", [], "subject"),
    ])

    def targets(task):
        if task.id == "good":
            return {"fail_to_pass": ["tests/test_good.py::test_target"], "broken_at_fix": []}
        return {
            "fail_to_pass": [] if entry == "no_target" else ["tests/test_bad.py::test_target"],
            "broken_at_fix": ["tests/test_bad.py::test_old"] if entry == "broken" else [],
        }

    monkeypatch.setattr(harness_eval, "compute_targets", targets)
    assert harness_eval.cmd_check(argparse.Namespace(out=str(tmp_path))) == (0 if entry == "valid" else 1)


@pytest.mark.parametrize("failure", [None, "target", "question", "judge", "malformed_judge"])
def test_persona_run_returns_nonzero_for_any_evaluation_error(monkeypatch, tmp_path, failure):
    # R8-12: error entries must be visible to CI/the owner through the process exit code.
    persona_models = _load_persona(monkeypatch)
    data = {"scale": {"2": "ok", "1": "partly", "0": "fail"},
            "questions": [{"id": 1, "lang": "ru", "turns": ["привет"], "expect": "ответ"}]}
    monkeypatch.setattr(persona_models, "load_env", lambda: None)
    monkeypatch.setattr(persona_models, "load_questions", lambda path=None: data)
    monkeypatch.setattr(persona_models, "check_quota_budget", lambda *args: None)
    monkeypatch.setattr(persona_models, "print_report", lambda *args, **kwargs: None)

    def answers(target, questions):
        if failure == "target":
            raise RuntimeError("target failed")
        if failure == "question":
            return {"1": {"turns": ["привет"], "error": "question failed"}}
        return {"1": {"turns": ["привет"], "answer": "Привет!", "script_ok": True}}

    real_judge = persona_models.run_judge

    def judge(*args):
        if failure == "question":
            return real_judge(*args)
        if failure == "judge":
            return {"error": "judge failed", "score": None}
        if failure == "malformed_judge":
            monkeypatch.setattr(persona_models, "call_with_retry", lambda *a, **kw: ("not JSON, criterion 2 mentioned", 1, 1))
            monkeypatch.setattr(persona_models.time, "sleep", lambda seconds: None)
            return real_judge(*args)
        return {"score": 2, "reason": "ok"}

    monkeypatch.setattr(persona_models, "collect_answers", answers)
    monkeypatch.setattr(persona_models, "run_judge", judge)
    args = argparse.Namespace(
        questions=None, only=None, models="gemini:gemini-2.5-flash",
        judge="gemini:gemini-2.5-flash", out=str(tmp_path), dry_run=False,
    )
    assert persona_models.cmd_run(args) == (0 if failure is None else 1)
    scores = json.loads((tmp_path / "scores.json").read_text(encoding="utf-8"))
    assert bool(scores["gemini:gemini-2.5-flash"]["errors"]) is (failure is not None)
