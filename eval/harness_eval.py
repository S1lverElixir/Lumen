"""Experiment: whether automatic AGENTS.md edits help an agent solve tasks.

The idea comes from google-research/harness2, but without the framework: we take real tasks from
repository history, run an agent (OpenCode CLI) in a sandbox with the current
AGENTS.md, then a model proposes an AGENTS.md edit, and the same tasks run
with the new file. The judge is the repository gate: pytest and pyflakes.

Subcommands: run (one task and variant), propose (a candidate AGENTS.md edit),
report (summary), check (validate tasks without an agent).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TASKS_FILE = Path(__file__).resolve().parent / "tasks.json"
# Keep artifacts outside %TEMP%\opencode: OpenCode cleans that folder itself, and
# runs and their sandboxes would disappear without a trace.
OUT_ROOT = Path(
    os.environ.get(
        "EVAL_OUT",
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "Lumen" / "harness-eval"),
    )
)
OPENCODE_DB = Path(os.environ.get("USERPROFILE", ".")) / ".local" / "share" / "opencode" / "opencode.db"

AGENT_MODEL = os.environ.get("EVAL_AGENT_MODEL", "opencode/glm-5.3-flash-free")
# Google models are unavailable on this account ("User location is not supported"),
# so both the agent and the editor come from the free opencode lineup.
PROPOSER_MODEL = os.environ.get("EVAL_PROPOSER_MODEL", "opencode/nemotron-3-ultra-free")
AGENT_TIMEOUT = int(os.environ.get("EVAL_AGENT_TIMEOUT", "1200"))
# Change together with the sandbox rules, gate composition, or result schema.
HARNESS_VERSION = "2"
UNSAFE_HOST_ACK_MESSAGE = (
    "Отказ: HARNESS_UNSAFE_HOST_ACK=1 не задан. Имена-носители ключей и deny-паттерны "
    "не являются ОС-изоляцией: агент может читать файлы хоста по абсолютному пути, "
    "включая секреты. Запускайте агентский код только в одноразовой ОС-изолированной "
    "VM/контейнере без учётных данных хоста; эта переменная лишь явно принимает риск."
)

# R8-01 (review 26.11.2026): key-carrier names. The agent and judge no longer
# receive the entire os.environ: the filter removes obvious secret carriers,
# but leaves toolchain variables (PATH, HOME, PYTHONPATH) alone.
SECRET_ENV_RE = re.compile(
    r"(^|_)(API_KEY|APIKEY|KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|CREDENTIALS"
    r"|AUTH|AUTHORIZATION|WEBHOOK|DSN)(_|$|\d)"
)

# Commands the agent must not run in the sandbox: some break
# the sandbox, others the host (disk formatting, file deletion, push to origin).
DENY_PATTERNS = {
    "git push*": "deny",
    "git remote*": "deny",
    "git gc*": "deny",
    "rm -rf *": "deny",
    "rmdir*": "deny",
    "del *": "deny",
    "Remove-Item*": "deny",
    "Move-Item*": "deny",
    "format *": "deny",
    "shutdown*": "deny",
    "reg delete*": "deny",
    # R8-01: the read tool already denies *.env, but bash could simply cat the
    # file; the patterns are crude (argument reordering bypasses them), but they block
    # explicit calls in both shells: the Windows host uses PowerShell.
    "cat *.env*": "deny",
    "type *.env*": "deny",
    "Get-Content *.env*": "deny",
}

# Signs of Unix commands in the sandbox: the host is Windows, and the shell is PowerShell.
UNIX_SHELL_PATTERNS = [
    r"\bls -[a-zA-Z]",
    r"\bcat \S",
    r"\bgrep -[a-zA-Z]",
    r"\bhead -",
    r"\btail -",
    r"\brm -rf",
    r"\bchmod \+",
    r"\bexport [A-Z_]+=",
]


@dataclass
class Task:
    id: str
    fix: str
    tests: list[str]
    subject: str
    base: str = ""

    def prompt(self) -> str:
        return (
            f"Задача: {self.subject}\n\n"
            "В репозитории уже есть падающие тесты под это поведение. "
            "Почини код так, чтобы все тесты проходили.\n"
            "Тесты не менять. Работай по AGENTS.md."
        )


@dataclass
class RunResult:
    task: str
    variant: str
    model: str
    solved: bool = False
    pre_fail: list[str] = field(default_factory=list)
    post_fail: list[str] = field(default_factory=list)
    target_tests: list[str] = field(default_factory=list)
    target_failed: list[str] = field(default_factory=list)
    new_failures: list[str] = field(default_factory=list)
    pyflakes_ok: bool = True
    pyflakes_out: str = ""
    pytest_ok: bool = False
    agent_ok: bool = False
    violations: dict = field(default_factory=dict)
    final_message: str = ""
    duration_s: float = 0.0
    error: str = ""
    transcript: str = ""
    sandbox: str = ""


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def require_unsafe_host_ack() -> None:
    # R8-01: scrub/env and command bans do not restrict an agent from reading the host FS.
    if os.environ.get("HARNESS_UNSAFE_HOST_ACK") != "1":
        raise RuntimeError(UNSAFE_HOST_ACK_MESSAGE)


def run_cmd(
    args: list[str],
    cwd: Path | None = None,
    timeout: int = 600,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
    )


def git(repo: Path, *args: str, env: dict | None = None) -> str:
    proc = run_cmd(["git", "-C", str(repo), *args], env=env)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def find_cli() -> str:
    env = os.environ.get("OPENCODE_CLI")
    if env and Path(env).exists():
        return env
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "@opencodedesktop" / "resources" / "opencode-cli.exe"
    if local.exists():
        return str(local)
    found = shutil.which("opencode")
    if found:
        return found
    raise RuntimeError("opencode CLI не найден: задай OPENCODE_CLI или проверь %LOCALAPPDATA%\\Programs\\@opencodedesktop")


def load_tasks() -> list[Task]:
    data = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
    tasks = [Task(**item) for item in data["tasks"]]
    for task in tasks:
        task.base = git(REPO, "rev-parse", f"{task.fix}^").strip()
    return tasks


def sandbox_config() -> dict:
    return {
        "$schema": "https://opencode.ai/config.json",
        "share": "disabled",
        "permission": {
            "read": {"*.env": "deny", "*.env.*": "deny"},
            "edit": {"*": "allow"},
            "bash": {"*": "allow", **DENY_PATTERNS},
        },
    }


def prepare(task: Task, sandbox: Path, agents_md: Path | None, apply_fix: bool = False) -> Path:
    """Clones the repository at its pre-fix state and installs the tests from the fix commit."""
    if sandbox.exists():
        for _ in range(5):
            shutil.rmtree(sandbox, ignore_errors=True)
            if not sandbox.exists():
                break
            time.sleep(2)
        if sandbox.exists():
            # the directory holds a live OpenCode process: use another name to avoid interfering
            sandbox = sandbox.with_name(f"{sandbox.name}-{int(time.time())}")
    sandbox.parent.mkdir(parents=True, exist_ok=True)
    clone = run_cmd(["git", "clone", "--no-hardlinks", "--quiet", str(REPO), str(sandbox)])
    if clone.returncode != 0:
        raise RuntimeError(f"git clone не удался: {clone.stderr.strip()}")
    git(sandbox, "checkout", "--quiet", "--detach", task.base)
    git(sandbox, "remote", "remove", "origin")
    git(sandbox, "config", "user.email", "eval@localhost")
    git(sandbox, "config", "user.name", "harness-eval")
    git(sandbox, "config", "commit.gpgsign", "false")
    git(sandbox, "checkout", task.fix, "--", *task.tests)
    if apply_fix:
        source_files = source_files_of(task)
        if source_files:
            git(sandbox, "checkout", task.fix, "--", *source_files)
    (sandbox / "opencode.json").write_text(
        json.dumps(sandbox_config(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    target = sandbox / "AGENTS.md"
    if agents_md and agents_md.exists():
        shutil.copyfile(agents_md, target)
    else:
        shutil.copyfile(REPO / "AGENTS.md", target)
    return sandbox


def source_files_of(task: Task) -> list[str]:
    """Files changed by the fix commit, excluding tests: without them the task cannot be built."""
    changed = git(REPO, "diff", "--name-only", task.base, task.fix).splitlines()
    return [f for f in changed if f and not f.startswith("tests/")]


def targets_cache_path() -> Path:
    return OUT_ROOT / "targets.json"


def targets_cache_key(task: Task) -> str:
    # R8-07: task@fix produced stale targets after the base, tests, or harness changed.
    inputs = {
        "task": task.id,
        "fix": task.fix,
        "tests": sorted(task.tests),
        "base": task.base,
        "model": AGENT_MODEL,
        "harness": HARNESS_VERSION,
    }
    raw = json.dumps(inputs, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


_TARGETS_CACHE_LOCK = threading.Lock()


def update_targets_cache(cache_file: Path, key: str, entry: dict) -> None:
    """Serialize the complete read-merge-write cycle for parallel batches."""
    with _TARGETS_CACHE_LOCK:
        cache = {}
        if cache_file.exists():
            cache = json.loads(cache_file.read_text(encoding="utf-8"))
        cache[key] = entry
        write_targets_cache(cache_file, cache)


def write_targets_cache(cache_file: Path, cache: dict) -> None:
    # R8-07: a shared targets.json.tmp overwrote another write in parallel batches.
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=cache_file.parent,
            prefix=f".{cache_file.name}.", suffix=".tmp", delete=False,
        ) as tmp:
            tmp_name = tmp.name
            json.dump(cache, tmp, ensure_ascii=False, indent=2)
        Path(tmp_name).replace(cache_file)
    finally:
        if tmp_name:
            Path(tmp_name).unlink(missing_ok=True)


def compute_targets(task: Task) -> dict:
    """FAIL_TO_PASS: tests that fail on the base and pass after the fix.

    Calculated empirically, not from the diff: the fix commit could change expectations in existing
    tests, not only add new ones.
    """
    cache = {}
    cache_file = targets_cache_path()
    if cache_file.exists():
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
    key = targets_cache_key(task)
    if key in cache:
        return cache[key]

    work = OUT_ROOT / "targets"
    sandbox = work / task.id
    prepare(task, sandbox, None)
    failed_base, _, _ = pytest_state(sandbox)
    git(sandbox, "checkout", task.fix, "--", *source_files_of(task))
    failed_fix, _, _ = pytest_state(sandbox)
    shutil.rmtree(sandbox, ignore_errors=True)

    entry = {
        "fail_to_pass": sorted(failed_base - failed_fix),
        "broken_at_fix": sorted(failed_base & failed_fix),
        "failed_base": sorted(failed_base),
    }
    update_targets_cache(cache_file, key, entry)
    return entry


def target_tests(task: Task) -> list[str]:
    """Tests added by the fix commit (from the diff). The exact failing list comes from compute_targets."""
    diff = git(REPO, "diff", task.base, task.fix, "--", *task.tests)
    ids: list[str] = []
    current_file = ""
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:].strip()
        elif line.startswith("+") and not line.startswith("+++"):
            match = re.match(r"\+\s*def (test_\w+)", line)
            if match and current_file:
                ids.append(f"{current_file}::{match.group(1)}")
    return ids


def pytest_state(sandbox: Path) -> tuple[set[str], str, bool]:
    # R8-01: the judge runs sandbox code the agent could modify — the env uses the same
    # cleaned template as the agent.
    proc = run_cmd(
        [sys.executable, "-m", "pytest", "-q", "--tb=no", "-rf"],
        cwd=sandbox,
        timeout=900,
        env=agent_env(sandbox),
    )
    failed = set()
    for line in proc.stdout.splitlines():
        match = re.match(r"^(FAILED|ERROR)\s+(\S+)", line.strip())
        if match:
            failed.add(match.group(2))
    return failed, proc.stdout[-4000:], proc.returncode == 0


def pyflakes_state(sandbox: Path) -> tuple[bool, str]:
    # The gate, as in CI: bot.py, system_prompt.py, all lumen_*.py, and all tests/*.py.
    modules = sorted(p.name for p in sandbox.glob("lumen_*.py"))
    tests = sorted(f"tests/{p.name}" for p in (sandbox / "tests").glob("*.py"))
    proc = run_cmd(
        [sys.executable, "-m", "pyflakes", "bot.py", "system_prompt.py", *modules, *tests],
        cwd=sandbox,
        timeout=300,
        env=agent_env(sandbox),
    )
    return proc.returncode == 0, (proc.stdout + proc.stderr)[-2000:]


def agent_env(sandbox: Path) -> dict:
    # R8-01 (review 26.11.2026): this used to receive the entire os.environ, and a leaked
    # key variable became a key in the model context; this is not OS
    # isolation, only removal of obvious secret carriers.
    env = {k: v for k, v in os.environ.items() if not SECRET_ENV_RE.search(k.upper())}
    env["PIP_TARGET"] = str(sandbox / ".pip-target")
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def session_from_db(sandbox: Path, retries: int = 10) -> dict | None:
    """Retrieves the agent session from the OpenCode database: the stdout log may disappear, but the database does not."""
    import sqlite3

    if not OPENCODE_DB.exists():
        return None
    target = str(sandbox).replace("\\", "/")
    for attempt in range(retries):
        try:
            con = sqlite3.connect(f"file:{OPENCODE_DB}?mode=ro", uri=True)
            cur = con.cursor()
            row = cur.execute(
                "select id from session_v2 where directory = ? order by time_created desc limit 1",
                (target,),
            ).fetchone()
            if row is None:
                row = cur.execute(
                    "select id from session where directory = ? order by time_created desc limit 1",
                    (target,),
                ).fetchone()
            if row is None:
                con.close()
                time.sleep(2)
                continue
            session_id = row[0]
            messages = list(
                cur.execute(
                    "select data from session_message where session_id=? order by seq", (session_id,)
                )
            )
            con.close()
        except sqlite3.Error:
            time.sleep(2)
            continue
        if not messages:
            time.sleep(2)
            continue

        tool_calls: list[dict] = []
        final_text = ""
        for (data,) in messages:
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            for item in obj.get("content") or []:
                kind = item.get("type")
                if kind == "text" and item.get("text"):
                    final_text = item["text"]
                elif kind == "tool":
                    state = item.get("state") or {}
                    output = ""
                    for chunk in state.get("content") or []:
                        if isinstance(chunk, dict) and chunk.get("text"):
                            output += chunk["text"]
                    tool_calls.append(
                        {
                            "name": item.get("name"),
                            "input": state.get("input") or {},
                            "output": output[-4000:],
                        }
                    )
        return {
            "session_id": session_id,
            "final_text": final_text,
            "tool_calls": tool_calls,
            "shell_commands": [
                (call.get("input") or {}).get("command", "")
                for call in tool_calls
                if call.get("name") in ("shell", "bash")
            ],
        }
    return None


def keep_sandbox(sandbox: Path, kept: Path, base_status: list[str]) -> dict:
    """Takes a snapshot of the sandbox: its contents may be deleted after the run.

    base_status is the git state immediately after task setup: staged tests and
    the AGENTS.md/opencode.json files we wrote are not counted as agent edits.
    """
    info = {"changed_files": [], "diff": ""}
    if not sandbox.exists():
        return info
    # R8-01 (review 26.11.2026): after the agent runs, the sandbox belongs to it — the agent could
    # set a filter or hooksPath in .git/config, and the git call below would execute that code.
    # The env is cleaned, as for the agent and judge: untrusted code does not carry host keys.
    try:
        status = [line for line in git(sandbox, "status", "--porcelain", env=agent_env(sandbox)).splitlines() if line]
    except RuntimeError:
        status = []
    base_keys = {line[3:] for line in base_status}
    info["changed_files"] = [line for line in status if line[3:] not in base_keys]
    if kept.exists():
        shutil.rmtree(kept, ignore_errors=True)
    shutil.copytree(sandbox, kept, ignore=shutil.ignore_patterns(".pip-target"), dirs_exist_ok=True)
    try:
        info["diff"] = git(kept, "diff", "HEAD", env=agent_env(kept))
    except RuntimeError:
        info["diff"] = ""
    return info


def run_agent(sandbox: Path, prompt: str, model: str, transcript: Path, timeout: int) -> tuple[str, str, bool]:
    """Runs the agent. Returns (last message, log path, exit code 0)."""
    require_unsafe_host_ack()
    cli = find_cli()
    # --standalone was in argv before 10.11.2026: CLI 1.19.5 printed usage for the unknown flag and
    # exited with code 0, so the run silently became solved=False.
    args = [cli, "run", "--auto", "--model", model, "--format", "json", prompt]
    with transcript.open("w", encoding="utf-8", errors="replace") as out:
        proc = subprocess.Popen(
            args,
            cwd=str(sandbox),
            stdout=out,
            stderr=subprocess.STDOUT,
            env=agent_env(sandbox),
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        agent_ok = False
        try:
            proc.wait(timeout=timeout)
            agent_ok = proc.returncode == 0
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
            proc.wait(timeout=60)
    session = session_from_db(sandbox)
    if session:
        final = session["final_text"]
    else:
        texts = []
        if transcript.exists():
            for line in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                part = event.get("part") or {}
                if part.get("type") == "text" and part.get("text"):
                    texts.append(part["text"])
        final = texts[-1] if texts else ""
    return final, str(transcript), agent_ok


def check_violations(session: dict | None, changed_files: list[str]) -> dict:
    """Coarse project-rule violations, based on the agent's commands and changed files."""
    commands = (session or {}).get("shell_commands") or []
    joined = "\n".join(commands)
    unix_hits = sorted({p for p in UNIX_SHELL_PATTERNS if re.search(p, joined)})
    touched_tests = [f for f in changed_files if "tests/" in f]
    return {
        "tests_touched": touched_tests,
        "unix_shell_commands": unix_hits,
        "ran_pytest": "pytest" in joined,
        "ran_pyflakes": "pyflakes" in joined,
        "shell_calls": len(commands),
        "changed_files": changed_files,
    }


def score(task: Task, result: RunResult, targets: list[str]) -> None:
    result.target_tests = targets
    result.target_failed = [
        t for t in targets if any(f == t or f.startswith(t + "[") for f in result.post_fail)
    ]
    result.new_failures = sorted(set(result.post_fail) - set(result.pre_fail))
    # Empty targets do not mean “solved”: without a single check, do not award success
    # (review 17.11.2026).
    result.solved = (
        bool(targets) and not result.target_failed and not result.new_failures
        and not result.violations.get("tests_touched")
        and result.pyflakes_ok and result.pytest_ok and result.agent_ok
    )


def run_one(task_id: str, variant: str, out_dir: Path, agents_md: Path | None, model: str) -> dict:
    """One run: sandbox, agent, snapshot, judge. Returns the result payload."""
    require_unsafe_host_ack()
    tasks = {t.id: t for t in load_tasks()}
    task = tasks[task_id]
    out_dir.mkdir(parents=True, exist_ok=True)
    sandbox = out_dir / f"sandbox-{task.id}-{variant}"
    kept = out_dir / f"kept-{task.id}-{variant}"

    log(f"песочница {task.id} ({variant}) на {task.base[:8]}")
    sandbox = prepare(task, sandbox, agents_md)
    repo_status_before = [line for line in git(REPO, "status", "--porcelain").splitlines() if line]

    pre_fail, _, _ = pytest_state(sandbox)
    targets = compute_targets(task)["fail_to_pass"]
    base_status = [line for line in git(sandbox, "status", "--porcelain").splitlines() if line]
    log(f"до агента падают {len(pre_fail)} тестов, целевых (FAIL_TO_PASS): {len(targets)}")

    transcript = out_dir / f"{task.id}.{variant}.jsonl"
    log(f"агент: {model}, таймаут {AGENT_TIMEOUT}с")
    started = time.time()
    final, _, agent_ok = run_agent(sandbox, task.prompt(), model, transcript, AGENT_TIMEOUT)
    duration = round(time.time() - started, 1)

    # Take the snapshot immediately: sandbox contents may be deleted (temp cleanup, OpenCode).
    snapshot = keep_sandbox(sandbox, kept, base_status)
    session = session_from_db(sandbox)
    if session:
        (out_dir / f"{task.id}.{variant}.session.json").write_text(
            json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    result = RunResult(
        task=task.id,
        variant=variant,
        model=model,
        duration_s=duration,
        final_message=final,
        agent_ok=agent_ok,
        transcript=str(transcript),
        sandbox=str(sandbox),
    )
    result.post_fail, post_out, result.pytest_ok = pytest_state(kept)
    result.pyflakes_ok, result.pyflakes_out = pyflakes_state(kept)
    result.violations = check_violations(session, snapshot["changed_files"])
    result.pre_fail = sorted(pre_fail)
    score(task, result, targets)

    payload = {
        "task": task.id,
        "variant": variant,
        "model": model,
        "solved": result.solved,
        "pre_fail": result.pre_fail,
        "post_fail": sorted(result.post_fail),
        "target_tests": result.target_tests,
        "target_failed": result.target_failed,
        "new_failures": result.new_failures,
        "pyflakes_ok": result.pyflakes_ok,
        "pytest_ok": result.pytest_ok,
        "agent_ok": result.agent_ok,
        "violations": result.violations,
        "final_message": result.final_message,
        "session_id": (session or {}).get("session_id", ""),
        "duration_s": duration,
        "diff": snapshot["diff"][-8000:],
        "post_pytest_tail": post_out[-1500:],
    }
    result_file = out_dir / f"{task.id}.{variant}.json"
    result_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"итог: {'решено' if result.solved else 'не решено'} ({duration}с) -> {result_file}")
    check_repo_untouched(repo_status_before)
    return payload


def cmd_run(args: argparse.Namespace) -> int:
    out_dir = Path(args.out) if args.out else OUT_ROOT / time.strftime("%Y%m%d-%H%M%S")
    agents_md = Path(args.agents_md) if args.agents_md else None
    payload = run_one(args.task, args.variant, out_dir, agents_md, args.model or AGENT_MODEL)
    return 0 if payload.get("solved") else 1


def cmd_batch(args: argparse.Namespace) -> int:
    """Runs a set of tasks with one AGENTS.md variant."""
    require_unsafe_host_ack()
    from concurrent.futures import ThreadPoolExecutor

    tasks = [t.id for t in load_tasks()]
    if args.tasks:
        wanted = set(args.tasks.split(","))
        tasks = [t for t in tasks if t in wanted]
    out_dir = Path(args.out) if args.out else OUT_ROOT / time.strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    agents_md = Path(args.agents_md) if args.agents_md else None
    model = args.model or AGENT_MODEL
    log(f"batch: {len(tasks)} задач, вариант {args.variant}, потоков {args.jobs}")

    failures: list[str] = []
    failures_lock = threading.Lock()

    def work(task_id: str) -> None:
        try:
            payload = run_one(task_id, args.variant, out_dir, agents_md, model)
            if not payload.get("solved"):
                with failures_lock:
                    failures.append(task_id)
        except Exception as exc:  # noqa: BLE001 — one run must not crash the entire batch
            log(f"ошибка на {task_id}: {exc}")
            with failures_lock:
                failures.append(task_id)

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        list(pool.map(work, tasks))
    return 1 if failures else 0


def check_repo_untouched(before: list[str]) -> None:
    """Guard: the real repository must not change during a run."""
    try:
        status = [line for line in git(REPO, "status", "--porcelain").splitlines() if line]
    except RuntimeError as exc:
        log(f"ВНИМАНИЕ: не смог проверить состояние репозитория: {exc}")
        return
    before_keys = {line[3:] for line in before}
    unexpected = [line for line in status if line[3:] not in before_keys]
    if unexpected:
        log(f"ВНИМАНИЕ: репозиторий изменён во время прогона: {unexpected[:5]}")


def cmd_check(args: argparse.Namespace) -> int:
    """Validates the tasks: only the target tests fail on the base."""
    out_dir = Path(args.out) if args.out else OUT_ROOT / "check"
    out_dir.mkdir(parents=True, exist_ok=True)
    valid = True
    for task in load_tasks():
        entry = compute_targets(task)
        broken = entry["broken_at_fix"]
        targets = entry["fail_to_pass"]
        task_valid = bool(targets) and not broken
        valid = valid and task_valid
        status = "ok" if task_valid else "проблема"
        log(f"{task.id}: {status}; целевых {len(targets)}, ломаных даже после правки {len(broken)}")
        for item in broken[:5]:
            log(f"    ломается и после правки: {item}")
        if not targets:
            log("    на базе нет падающих целевых тестов — задача негодная")
    return 0 if valid else 1


def cmd_propose(args: argparse.Namespace) -> int:
    """Builds a candidate AGENTS.md edit from failures in baseline runs."""
    require_unsafe_host_ack()
    base_dir = Path(args.base_dir)
    runs = sorted(base_dir.glob("*.base.json"))
    if not runs:
        log(f"нет базовых прогонов в {base_dir}")
        return 1
    work = base_dir / "propose"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    shutil.copyfile(REPO / "AGENTS.md", work / "AGENTS.md")
    reports = work / "failures"
    reports.mkdir()
    for run_file in runs:
        data = json.loads(run_file.read_text(encoding="utf-8"))
        violations = data.get("violations") or {}
        body = [
            f"Задача: {data['task']}",
            f"Итог: {'решено' if data.get('solved') else 'не решено'}, {data.get('duration_s')}с",
            f"Целевые тесты: {', '.join(data.get('target_tests') or [])}",
            f"Целевые тесты после прогона упали: {', '.join(data.get('target_failed') or [])}",
            f"Новые падения: {', '.join(data.get('new_failures') or [])}",
            f"Нарушения правил: {json.dumps(violations, ensure_ascii=False)}",
            f"pyflakes: {'ok' if data.get('pyflakes_ok') else 'ошибка'}",
            "",
            "Последнее сообщение агента:",
            (data.get("final_message") or "")[:3000],
            "",
            "Падения тестов после прогона:",
            "\n".join(data.get("post_fail") or [])[:2000],
        ]
        (reports / f"{data['task']}.md").write_text("\n".join(body), encoding="utf-8")
    if not any(reports.iterdir()):
        log("базовых прогонов нет — править нечего")
        return 0

    prompt = (
        "Ты редактор правил проекта. Посмотри AGENTS.md и отчёты о прогонах в папке failures.\n"
        "Составь новый AGENTS.md: добавь правила, которые предотвратили бы провалы "
        "и убрали бы замеченную в отчётах неэффективность (лишние команды, unix-команды "
        "в PowerShell, гейт не запускался). Правила конкретные, в стиле существующих разделов, "
        "только по фактам из отчётов.\n"
        "Существующие правила не удаляй и не переписывай, только дополняй.\n"
        "Сохрани результат в файл AGENTS.candidate.md в этой же папке. Больше ничего не меняй."
    )
    transcript = base_dir / "propose.jsonl"
    log(f"модель-редактор: {args.model}")
    run_agent(work, prompt, args.model, transcript, AGENT_TIMEOUT)
    candidate = work / "AGENTS.candidate.md"
    if not candidate.exists():
        log("модель не создала AGENTS.candidate.md")
        return 1

    original = (REPO / "AGENTS.md").read_text(encoding="utf-8")
    new = candidate.read_text(encoding="utf-8")
    missing = [line for line in original.splitlines() if line.strip() and line not in new]
    diff = run_cmd(
        ["git", "diff", "--no-index", "--unified=3", str(REPO / "AGENTS.md"), str(candidate)], cwd=REPO
    )
    diff_text = diff.stdout or diff.stderr
    (base_dir / "agents_candidate.diff").write_text(diff_text, encoding="utf-8")
    (base_dir / "AGENTS.candidate.md").write_text(new, encoding="utf-8")
    log(f"кандидат записан: {base_dir / 'AGENTS.candidate.md'}")
    log(f"удалённых строк из оригинала: {len(missing)}")
    for line in missing[:10]:
        log(f"    пропала строка: {line[:90]}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    out_dir = Path(args.out) if args.out else OUT_ROOT
    rows = []
    for file in sorted(out_dir.glob("*.json")):
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if "task" not in data or "variant" not in data:
            continue
        rows.append(data)
    by_task: dict[str, dict[str, dict]] = {}
    for row in rows:
        by_task.setdefault(row["task"], {})[row["variant"]] = row
    variants = sorted({v for task in by_task.values() for v in task})

    header = "| Задача | " + " | ".join(variants) + " |"
    sep = "|---|" + "---|" * len(variants)
    lines = [
        "# Harness-eval: варианты AGENTS.md на задачах из истории",
        "",
        f"Модель агента: {AGENT_MODEL} · Модель-редактор: {PROPOSER_MODEL}",
        "",
        header,
        sep,
    ]
    totals = {v: 0 for v in variants}
    for task_id, task_variants in sorted(by_task.items()):
        cells = []
        for variant in variants:
            data = task_variants.get(variant)
            if not data:
                cells.append("-")
                continue
            ok = bool(data.get("solved"))
            totals[variant] += ok
            viol = data.get("violations") or {}
            mark = "решено" if ok else "нет"
            if viol.get("tests_touched"):
                mark += " (тесты правил)"
            if viol.get("unix_shell_commands"):
                mark += " (unix)"
            cells.append(mark)
        lines.append(f"| {task_id} | " + " | ".join(cells) + " |")
    lines += ["", "Итог: " + " · ".join(f"{v}: {totals[v]}/{len(by_task)}" for v in variants), ""]
    text = "\n".join(lines)
    print(text)
    if args.out:
        (Path(args.out) / "report.md").write_text(text, encoding="utf-8")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    """Reconnaissance: which candidates in history yield usable tasks."""
    for spec in args.commits:
        fix, _, files = spec.partition(":")
        tests = files.split(";") if files else []
        task = Task(id=f"probe-{fix}", fix=fix.strip(), tests=[t for t in tests if t], subject="probe")
        task.base = git(REPO, "rev-parse", f"{task.fix}^").strip()
        entry = compute_targets(task)
        log(
            f"{fix}: целевых {len(entry['fail_to_pass'])}, "
            f"ломаных после правки {len(entry['broken_at_fix'])} -> {entry['fail_to_pass']}"
        )
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="прогнать одну задачу одним вариантом AGENTS.md")
    run.add_argument("--task", required=True)
    run.add_argument("--variant", default="base", choices=["base", "candidate"])
    run.add_argument("--agents-md", default=None, help="файл AGENTS.md (кандидат)")
    run.add_argument("--model", default=None)
    run.add_argument("--out", default=None)
    run.set_defaults(func=cmd_run)

    batch = sub.add_parser("batch", help="прогнать несколько задач одним вариантом")
    batch.add_argument("--variant", default="base", choices=["base", "candidate", "weak", "fixed"])
    batch.add_argument("--tasks", default=None, help="список id через запятую")
    batch.add_argument("--agents-md", default=None)
    batch.add_argument("--model", default=None)
    batch.add_argument("--jobs", type=int, default=1)
    batch.add_argument("--out", default=None)
    batch.set_defaults(func=cmd_batch)

    check = sub.add_parser("check", help="проверить, что задачи валидны на базе")
    check.add_argument("--out", default=None)
    check.set_defaults(func=cmd_check)

    probe = sub.add_parser("probe", help="разведка кандидатов из истории")
    probe.add_argument("commits", nargs="+", help="<fix-sha>[:<tests>;...>]")
    probe.set_defaults(func=cmd_probe)

    propose = sub.add_parser("propose", help="собрать кандидатную правку AGENTS.md")
    propose.add_argument("--base-dir", required=True)
    propose.add_argument("--model", default=PROPOSER_MODEL)
    propose.set_defaults(func=cmd_propose)

    report = sub.add_parser("report", help="сводка по прогонам")
    report.add_argument("--out", default=None)
    report.set_defaults(func=cmd_report)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
