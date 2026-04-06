# Self Improving AI Bot (Supervisor + Ollama)

This project runs an autonomous software-improvement loop against itself.

It connects to Ollama (`qwen3.5:9b`) at `100.94.152.3`, then repeatedly:

1. Baselines the codebase with validation commands.
2. Plans one small improvement using the model.
3. Generates a git diff patch.
4. Runs supervisor checks (allowlist, patch size, hunks, reviewer gate).
5. Applies the patch, re-validates, and auto-commits only if quality does not regress.
6. Stores iteration memory and adapts generation policy for the next cycle.

Failed patches are automatically rolled back.

## Quick Start

```powershell
python -m venv .venv
. .venv\Scripts\Activate.ps1
python -m pip install -U pip pytest
python -m self_improver status
python -m self_improver cycle
```

Run perpetual mode:

```powershell
python -m self_improver run
```

Run N cycles:

```powershell
python -m self_improver run --cycles 20
```

## Configuration

Copy and edit config:

```powershell
Copy-Item config.example.json config.json
python -m self_improver run --config config.json
```

Environment overrides:

- `SELF_IMPROVER_CONFIG` path to config JSON
- `OLLAMA_BASE_URL` override Ollama URL
- `OLLAMA_MODEL` override model name

## Supervisor Safety Model

- Patch scope restricted by `allowed_paths`.
- Patch byte/file/hunk limits.
- Validation gate blocks regressions.
- Rollback on failed validation.
- Mandatory git commits on accepted changes.
- Persistent memory in `.self_improver/memory.db`.
- Adaptive policy in `.self_improver/policy.json`.

## Perpetual Runtime

Use a process manager so the loop is self-sufficient across restarts. Example with PowerShell scheduled task is recommended for Windows.

Manual background loop:

```powershell
Start-Process -FilePath "python" -ArgumentList "-m self_improver run --config config.json" -NoNewWindow
```

## Notes

- First run auto-initializes git if needed.
- By default the bot refuses to run on a dirty working tree.
- Logs are written to `.self_improver/logs/bot.log`.
