# nl2cli

Turns a natural-language request into a **structured, validated Linux administration plan** — then executes it locally under a safety gate.

```bash
nl2cli -- enable bluetooth
nl2cli -- change ssh port to 2222
nl2cli --dry-run -- set up docker
```

The LLM only ever emits JSON. It has no shell, no file handle, no execution path. Every command it proposes is parsed into an argv list, validated by local code, risk-scored, and run with `shell=False` — or rejected before it ever reaches a subprocess.

---

## Install

```bash
pip install -e .
export OPENAI_API_KEY="sk-..."   # see .env.example
```

Requires Python 3.10+. Developed and tested on **Pop!\_OS 22.04 / System76 Gazelle** (systemd, GNOME); should work on any systemd distro.

## Usage

```bash
nl2cli -- <request>                # plan, show, confirm, execute
nl2cli --dry-run -- <request>      # plan only, no execution
nl2cli --apply -- <request>        # skip confirmation (overrides the risk gate)
nl2cli --verbose -- <request>      # show validation + LLM detail
nl2cli --no-adaptive -- <request>  # disable missing-tool substitution
```

## How it works

```
natural language
      ↓
host facts (distro, init system, available binaries, sysfs paths)
      ↓
LLM tool-calling loop  ──►  lookup tools: get_tool_usage, check_path,
      ↓                                    check_sysfs, get_service_info
emit_plan → JSON Plan
      ↓
pydantic parse  →  argv construction, shell-operator rejection
      ↓
local validators  →  safety patterns, host compatibility, critical-file guard
      ↓
risk score  →  confirmation gate
      ↓
sysfs/service snapshot  →  execute (shell=False)  →  snapshot diff
      ↓
audit log (JSONL) + side-effect report
```

## Safety model

| Layer | What it does |
|---|---|
| Schema | `;` `&&` `` ` `` `$(` `>` `<` rejected at parse time; commands become argv via `shlex` |
| sudo | Never present in a model-emitted string — actions set `requires_sudo=True` and `SudoManager` prepends `sudo` locally |
| Patterns | `rm -rf /`, `mkfs`, `dd`, `fdisk`, fork bombs, bootloader writes, critical-file writes |
| Host check | Validates target binaries and paths actually exist on this machine |
| Risk gate | Score-based confirmation; `--apply` is the only bypass |
| Blast radius | Timestamped backups before file edits, per-action timeout (30s default) |
| Forensics | Full JSONL audit log at `~/.local/state/nl2cli/history.jsonl` |

Pipes are the single deliberate exception: allowed only when the action needs no sudo and contains no write-intent pattern (`tee`, redirects, `dd`, `chmod`, `chown`). Only those run through `bash -c`; everything else is `shell=False`.

## Architecture

```
src/
├── schema.py            # Plan, ShellAction, EditFileAction, HostFacts
├── model_client.py      # OpenAI client, tool-calling loop, context pre-fetch
├── planner.py           # Plan generation
├── adaptive_planner.py  # Missing-tool detection, substitution, install actions
├── validators.py        # Multi-layer plan validation
├── safety.py            # Dangerous-pattern matching, critical-file protection
├── risk.py              # Risk scoring and confirmation policy
├── utils.py             # Host fact gathering
├── sudo_manager.py      # Sudo auth + background session keepalive
├── state_diff.py        # Before/after sysfs + service snapshots
├── sysfs_prefs.py       # Persistent desired-value store for hardware controls
├── audit.py             # JSONL audit log
├── config.py            # TOML + env config
├── renderer.py          # Rich plan rendering
└── executor/
    ├── shell.py         # argv execution
    ├── filesystem.py    # File edits, backups, sysfs writes
    └── services.py      # systemd / sysv / upstart
```

## Testing

```bash
pip install -e ".[dev]"
pytest    # 54 tests
```

Coverage is deliberately weighted toward the safety boundary — schema rejection, dangerous patterns, and the confirmation gate — rather than toward LLM behaviour, which is not deterministic enough to assert on.

---

## Roadmap

- [ ] `BaseLLMClient` ABC — plug in Claude, Gemini, local Ollama
- [ ] Split flat `src/` into `core/`, `planning/`, `validation/`, `execution/`, `storage/`
- [ ] Executor integration tests — filesystem edits, sysfs writes, service management
- [ ] Cross-session memory — successful plans as few-shot context

## License

MIT — see [LICENSE](LICENSE).
