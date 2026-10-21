"""Эксперимент: помогает ли автоматическая правка AGENTS.md агенту решать задачи.

Идея из google-research/harness2, но без фреймворка: берём реальные задачи из
истории репозитория, гоняем агента (OpenCode CLI) в песочнице с текущим
AGENTS.md, затем модель предлагает правку AGENTS.md, и те же задачи гоняются
с новым файлом. Судья — гейт репозитория: pytest и pyflakes.

Подкоманды: run (одна задача и вариант), propose (кандидатная правка AGENTS.md),
report (сводка), check (проверка задач без агента).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TASKS_FILE = Path(__file__).resolve().parent / "tasks.json"
# Артефакты держим вне %TEMP%\opencode: эту папку OpenCode чистит сам, и
# прогоны вместе с песочницей исчезали бы, не оставив следа.
OUT_ROOT = Path(
    os.environ.get(
        "EVAL_OUT",
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "Lumen" / "harness-eval"),
    )
)
OPENCODE_DB = Path(os.environ.get("USERPROFILE", ".")) / ".local" / "share" / "opencode" / "opencode.db"

AGENT_MODEL = os.environ.get("EVAL_AGENT_MODEL", "opencode/glm-5.3-flash-free")
PROPOSER_MODEL = os.environ.get("EVAL_PROPOSER_MODEL", "google/gemini-3.5-flash")
AGENT_TIMEOUT = int(os.environ.get("EVAL_AGENT_TIMEOUT", "1200"))

# Команды, которые агент в песочнице запускать не должен: часть из них ломает
# песочницу, часть — хост (формат диска, удаление файлов, push в origin).
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
}

# Признаки unix-команд в песочнице: хост под Windows, шелл — PowerShell.
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
    violations: dict = field(default_factory=dict)
    final_message: str = ""
    duration_s: float = 0.0
    error: str = ""
    transcript: str = ""
    sandbox: str = ""


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def run_cmd(args: list[str], cwd: Path | None = None, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def git(repo: Path, *args: str) -> str:
    proc = run_cmd(["git", "-C", str(repo), *args])
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


def prepare(task: Task, sandbox: Path, agents_md: Path | None, apply_fix: bool = False) -> None:
    """Клонирует репозиторий на состояние до правки и ставит тесты из fix-коммита."""
    if sandbox.exists():
        shutil.rmtree(sandbox, ignore_errors=True)
    sandbox.mkdir(parents=True)
    run_cmd(["git", "clone", "--no-hardlinks", "--quiet", str(REPO), str(sandbox)])
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


def source_files_of(task: Task) -> list[str]:
    """Файлы, которые менял fix-коммит, кроме тестов: без них задача не собирается."""
    changed = git(REPO, "diff", "--name-only", task.base, task.fix).splitlines()
    return [f for f in changed if f and not f.startswith("tests/")]


def targets_cache_path() -> Path:
    return OUT_ROOT / "targets.json"


def compute_targets(task: Task) -> dict:
    """FAIL_TO_PASS: тесты, что падают на базе и проходят после правки.

    Считаем эмпирически, а не по diff: fix-коммит мог менять ожидания существующих
    тестов, а не только добавлять новые.
    """
    cache = {}
    cache_file = targets_cache_path()
    if cache_file.exists():
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
    key = f"{task.id}@{task.fix}"
    if key in cache:
        return cache[key]

    work = OUT_ROOT / "targets"
    sandbox = work / task.id
    prepare(task, sandbox, None)
    failed_base, _ = pytest_state(sandbox)
    git(sandbox, "checkout", task.fix, "--", *source_files_of(task))
    failed_fix, _ = pytest_state(sandbox)
    shutil.rmtree(sandbox, ignore_errors=True)

    entry = {
        "fail_to_pass": sorted(failed_base - failed_fix),
        "broken_at_fix": sorted(failed_base & failed_fix),
        "failed_base": sorted(failed_base),
    }
    cache[key] = entry
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    return entry


def target_tests(task: Task) -> list[str]:
    """Тесты, добавленные fix-коммитом (по diff). Точный список падающих — compute_targets."""
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


def pytest_state(sandbox: Path) -> tuple[set[str], str]:
    proc = run_cmd([sys.executable, "-m", "pytest", "-q", "--tb=no", "-rf"], cwd=sandbox, timeout=900)
    failed = set()
    for line in proc.stdout.splitlines():
        match = re.match(r"^(FAILED|ERROR)\s+(\S+)", line.strip())
        if match:
            failed.add(match.group(2))
    return failed, proc.stdout[-4000:]


def pyflakes_state(sandbox: Path) -> tuple[bool, str]:
    # список модулей lumen_*.py собираем отдельно: гейт в CI так же собирает список
    modules = sorted(p.name for p in sandbox.glob("lumen_*.py"))
    proc = run_cmd(
        [sys.executable, "-m", "pyflakes", "bot.py", "system_prompt.py", *modules], cwd=sandbox, timeout=300
    )
    return proc.returncode == 0, (proc.stdout + proc.stderr)[-2000:]


def agent_env(sandbox: Path) -> dict:
    env = dict(os.environ)
    env["PIP_TARGET"] = str(sandbox / ".pip-target")
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def session_from_db(sandbox: Path, retries: int = 10) -> dict | None:
    """Достаёт сессию агента из базы OpenCode: лог в stdout может пропасть, база нет."""
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


def keep_sandbox(sandbox: Path, kept: Path) -> dict:
    """Снимает слепок песочницы: после прогона содержимое могут снести."""
    info = {"changed_files": [], "diff": ""}
    if not sandbox.exists():
        return info
    try:
        info["changed_files"] = [line for line in git(sandbox, "status", "--porcelain").splitlines() if line]
    except RuntimeError:
        info["changed_files"] = []
    if kept.exists():
        shutil.rmtree(kept, ignore_errors=True)
    shutil.copytree(sandbox, kept, ignore=shutil.ignore_patterns(".pip-target"), dirs_exist_ok=True)
    try:
        info["diff"] = git(kept, "diff", "HEAD")
    except RuntimeError:
        info["diff"] = ""
    return info


def run_agent(sandbox: Path, prompt: str, model: str, transcript: Path, timeout: int) -> tuple[str, str]:
    """Гоняет агента в песочнице. Возвращает (последнее сообщение, путь к логу)."""
    cli = find_cli()
    args = [cli, "run", "--standalone", "--auto", "--model", model, "--format", "json", prompt]
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
        try:
            proc.wait(timeout=timeout)
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
    return final, str(transcript)


def check_violations(session: dict | None, changed_files: list[str]) -> dict:
    """Грубые нарушения правил проекта: по командам агента и по изменённым файлам."""
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
    result.solved = not result.target_failed and not result.new_failures


def cmd_run(args: argparse.Namespace) -> int:
    tasks = {t.id: t for t in load_tasks()}
    task = tasks[args.task]
    out_dir = Path(args.out) if args.out else OUT_ROOT / time.strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    sandbox = out_dir / f"sandbox-{task.id}-{args.variant}"
    kept = out_dir / f"kept-{task.id}-{args.variant}"
    agents_md = Path(args.agents_md) if args.agents_md else None
    model = args.model or AGENT_MODEL

    log(f"песочница {task.id} ({args.variant}) на {task.base[:8]}")
    prepare(task, sandbox, agents_md)

    pre_fail, _ = pytest_state(sandbox)
    targets = compute_targets(task)["fail_to_pass"]
    log(f"до агента падают {len(pre_fail)} тестов, целевых (FAIL_TO_PASS): {len(targets)}")

    transcript = out_dir / f"{task.id}.{args.variant}.jsonl"
    log(f"агент: {model}, таймаут {AGENT_TIMEOUT}с")
    started = time.time()
    final, _ = run_agent(sandbox, task.prompt(), model, transcript, AGENT_TIMEOUT)
    duration = round(time.time() - started, 1)

    # Слепок снимаем сразу: содержимое песочницы могут снести (чистка temp, OpenCode).
    snapshot = keep_sandbox(sandbox, kept)
    session = session_from_db(sandbox)
    if session:
        (out_dir / f"{task.id}.{args.variant}.session.json").write_text(
            json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    result = RunResult(
        task=task.id,
        variant=args.variant,
        model=model,
        duration_s=duration,
        final_message=final,
        transcript=str(transcript),
        sandbox=str(sandbox),
    )
    result.post_fail, post_out = pytest_state(kept)
    result.pyflakes_ok, result.pyflakes_out = pyflakes_state(kept)
    result.violations = check_violations(session, snapshot["changed_files"])
    result.pre_fail = sorted(pre_fail)
    score(task, result, targets)

    payload = {
        "task": task.id,
        "variant": args.variant,
        "model": model,
        "solved": result.solved,
        "pre_fail": result.pre_fail,
        "post_fail": sorted(result.post_fail),
        "target_tests": result.target_tests,
        "target_failed": result.target_failed,
        "new_failures": result.new_failures,
        "pyflakes_ok": result.pyflakes_ok,
        "violations": result.violations,
        "final_message": result.final_message,
        "session_id": (session or {}).get("session_id", ""),
        "duration_s": duration,
        "diff": snapshot["diff"][-8000:],
        "post_pytest_tail": post_out[-1500:],
    }
    result_file = out_dir / f"{task.id}.{args.variant}.json"
    result_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"итог: {'решено' if result.solved else 'не решено'} ({duration}с) -> {result_file}")
    check_repo_untouched()
    return 0


def check_repo_untouched() -> None:
    """Сторож: настоящий репозиторий не должен меняться во время прогона."""
    try:
        status = git(REPO, "status", "--porcelain")
    except RuntimeError as exc:
        log(f"ВНИМАНИЕ: не смог проверить состояние репозитория: {exc}")
        return
    unexpected = [line for line in status.splitlines() if line and not line.startswith("?? eval/")]
    if unexpected:
        log(f"ВНИМАНИЕ: репозиторий изменён во время прогона: {unexpected[:5]}")


def cmd_check(args: argparse.Namespace) -> int:
    """Проверяет, что задачи валидны: на базе падают только целевые тесты."""
    out_dir = Path(args.out) if args.out else OUT_ROOT / "check"
    out_dir.mkdir(parents=True, exist_ok=True)
    for task in load_tasks():
        entry = compute_targets(task)
        broken = entry["broken_at_fix"]
        targets = entry["fail_to_pass"]
        status = "ok" if targets and not broken else "проблема"
        log(f"{task.id}: {status}; целевых {len(targets)}, ломаных даже после правки {len(broken)}")
        for item in broken[:5]:
            log(f"    ломается и после правки: {item}")
        if not targets:
            log("    на базе нет падающих целевых тестов — задача негодная")
    return 0


def cmd_propose(args: argparse.Namespace) -> int:
    """Собирает кандидатную правку AGENTS.md по провалам базовых прогонов."""
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
        if data.get("solved") and not data.get("violations", {}).get("tests_touched"):
            continue
        body = [
            f"Задача: {data['task']}",
            f"Целевые тесты: {', '.join(data.get('target_tests') or [])}",
            f"Целевые тесты после прогона упали: {', '.join(data.get('target_failed') or [])}",
            f"Новые падения: {', '.join(data.get('new_failures') or [])}",
            f"Нарушения правил: {json.dumps(data.get('violations') or {}, ensure_ascii=False)}",
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
        log("базовые прогоны без провалов — править нечего")
        return 0

    prompt = (
        "Ты редактор правил проекта. Посмотри AGENTS.md и отчёты о провалах в папке failures.\n"
        "Составь новый AGENTS.md: добавь правила, которые предотвратили бы эти провалы "
        "(конкретные, в стиле существующих разделов).\n"
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
    files = sorted(out_dir.glob("*.json"))
    files = [f for f in files if f.name not in {"propose.json"}]
    rows = []
    for file in files:
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

    lines = [
        "# Harness-eval: базовый AGENTS.md против кандидатного",
        "",
        f"Модель агента: {AGENT_MODEL} · Модель-редактор: {PROPOSER_MODEL}",
        "",
        "| Задача | База | Кандидат | Тесты (база) | Тесты (кандидат) | Нарушения (база) | Нарушения (кандидат) |",
        "|---|---|---|---|---|---|---|",
    ]
    base_score = cand_score = 0
    for task_id, variants in sorted(by_task.items()):
        base = variants.get("base", {})
        cand = variants.get("candidate", {})
        base_ok = bool(base.get("solved"))
        cand_ok = bool(cand.get("solved"))
        base_score += base_ok
        cand_score += cand_ok
        lines.append(
            f"| {task_id} | {'решено' if base_ok else 'нет'} | "
            f"{'решено' if cand_ok else 'нет'} | {len(base.get('target_failed') or [])} | "
            f"{len(cand.get('target_failed') or [])} | "
            f"{len(base.get('violations', {}).get('tests_touched') or [])} | "
            f"{len(cand.get('violations', {}).get('tests_touched') or [])} |"
        )
    lines += ["", f"Решено базой: {base_score}/{len(by_task)} · Кандидатом: {cand_score}/{len(by_task)}", ""]
    text = "\n".join(lines)
    print(text)
    if args.out:
        (Path(args.out) / "report.md").write_text(text, encoding="utf-8")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    """Разведка: какие кандидаты в истории дают годные задачи."""
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
