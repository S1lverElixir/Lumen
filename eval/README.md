# Harness eval: does automatic AGENTS.md correction help?

This experiment follows the method of [google-research/harness2](https://github.com/google-research/harness2)
without adopting its framework. Tasks from repository history run through an agent
(OpenCode CLI) with the current `AGENTS.md`; a model editor proposes an `AGENTS.md`
change; the same tasks run again. The repository gate is the judge.

## What is measured

- Primary: the share of tasks solved by the agent (target tests pass, no new failures,
  tests are not modified, `pytest` and `pyflakes` exit successfully, and the agent
  exits with code 0).
- Secondary: project-rule violations (test rules, skipped gate, Unix commands in
  PowerShell).

## How a run works

1. **Task** — a fix commit from history that changes code and tests. The sandbox is
   built at `fix^` (code before the fix), while tests come from `fix`: target tests
   fail on the base and the remaining suite passes. `compute_targets` derives the
   target list (`FAIL_TO_PASS`) empirically.
2. **Sandbox** — a repository clone without a remote, an `opencode.json` that denies
   dangerous commands and `.env` reads, and the selected `AGENTS.md` variant. The
   agent and judge receive a scrubbed environment: variables whose names contain
   KEY/TOKEN/SECRET/PASSWORD are removed, while the toolchain environment (PATH,
   HOME) remains (R8-01, 26.11.2026).
3. **Run** — `opencode run --auto --model <model> --format json <task>`. A snapshot is taken
   immediately after the agent finishes because OpenCode may clean `%TEMP%\opencode`,
   which is not a safe location for retained artifacts. The obsolete `--standalone`
   flag is unknown to CLI 1.19.5 and prints usage instead of running (verified
   10.11.2026). On the owner's machine, another failure came from the CLI database
   under `%TEMP%\ocdata\opencode` having no `session_v2` table while
   `session_from_db` read `~/.local/share/opencode/opencode.db`; deleting
   `%TEMP%\ocdata` fixed it and is unrelated to the repository.
4. **Judge** — `pytest -q` and `pyflakes` on the snapshot.

## Commands

```bash
python eval/harness_eval.py check                    # validate tasks; targets fail on the base
python eval/harness_eval.py probe <fix-sha>:<tests>  # investigate candidates from history
HARNESS_UNSAFE_HOST_ACK=1 python eval/harness_eval.py run --task <id> --variant base
HARNESS_UNSAFE_HOST_ACK=1 python eval/harness_eval.py run --task <id> --variant candidate --agents-md <file>
HARNESS_UNSAFE_HOST_ACK=1 python eval/harness_eval.py batch --tasks <id,id>  # sequential runs
HARNESS_UNSAFE_HOST_ACK=1 python eval/harness_eval.py propose --base-dir <base-runs-dir>
python eval/harness_eval.py report --out <directory>
```

Artifacts are written to `%LOCALAPPDATA%\Lumen\harness-eval`, not to
`%TEMP%\opencode`. `check` evaluates every task and exits 1 if any task has no failing target or
still has failures after the fix. `run` exits 1 when its result is unsolved. `batch` runs every
selected task and exits 1 if any worker raises or any result is unsolved; a partial batch is
never reported as a clean run.

Run results from 21.10.2026 are in [RESULTS.md](RESULTS.md).

## Limitations

### P1: host access is not isolated

This harness is **not a security boundary**. Environment-name scrubbing and command deny
patterns do not provide OS isolation: agent-controlled code can read host files by absolute
path, including `.env`, credentials and chat history. `HARNESS_UNSAFE_HOST_ACK=1` is only an
explicit accepted-risk acknowledgement; it does not make a run safe.

Use agent execution only inside a disposable OS-isolated VM or container with no host
credentials, no host secret mounts and no access to host files. Do not expose production
secrets or private runtime state to that environment. By default, `run`, `batch`, `propose`
and the central agent runner refuse to create a sandbox or start OpenCode without the
acknowledgement. Offline `check`, `probe` and `report` remain available without it.

- The sample is small (7 tasks, one run per variant): it is a signal of presence or
  absence of an effect, not a statistical estimate.
- Each task is solved once per agent, so run-to-run variance is not measured.
- The gate judge does not detect every project-rule violation when tests still pass.
- MCP servers are disabled in the sandbox to reduce tool surface; this is not the
  same as the owner's normal environment. Network access is not isolated.
