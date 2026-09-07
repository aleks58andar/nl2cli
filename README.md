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

Five layers, with a dependency direction that is enforced by a test
(`tests/test_public_api.py::TestLayering`): `core` is the bottom and imports
nothing else; `planning`, `validation`, `execution` and `storage` may use
`core` but never each other; the CLI sits on top.

```
src/
├── cli.py                   # argv parsing, orchestration, exit codes
├── renderer.py              # Rich plan preview and script export
│
├── core/                    # domain types + host facts (imports no other layer)
│   ├── schema.py            # Plan, ShellAction, EditFileAction, HostFacts
│   ├── config.py            # TOML + env config
│   ├── utils.py             # host fact gathering
│   └── state_diff.py        # before/after sysfs + service snapshots
│
├── planning/                # the only layer that talks to the LLM
│   ├── planner.py           # plan generation
│   ├── adaptive_planner.py  # missing-tool detection, substitution, installs
│   └── model_client.py      # OpenAI client, tool-calling loop, pre-fetch
│
├── validation/              # may this plan run? does it need confirmation?
│   ├── validators.py        # multi-layer plan validation
│   ├── host_compat.py       # init system, binaries, paths; plan enrichment
│   ├── safety.py            # dangerous patterns, critical-file protection
│   └── risk.py              # risk scoring and confirmation policy
│
├── execution/               # runs a validated plan
│   ├── runner.py            # per-action orchestration, timeouts, cancellation
│   ├── base.py              # ActionRunner ABC, Result
│   ├── shell.py             # argv execution
│   ├── filesystem.py        # file edits, backups, sysfs writes
│   ├── services.py          # systemd / sysv / upstart
│   └── sudo_manager.py      # sudo auth + background session keepalive
│
└── storage/                 # what persists between runs
    ├── audit.py             # JSONL audit log
    └── sysfs_prefs.py       # desired hardware values
```

## Testing

```bash
pip install -e ".[dev]"
pytest              # 584 tests
pytest --cov=src    # 83% coverage
```

Coverage is weighted toward the safety boundary — schema rejection, dangerous
patterns, the confirmation gate — rather than toward LLM behaviour, which is
not deterministic enough to assert on. The OpenAI transport is mocked
throughout; no test performs network I/O, invokes sudo, or writes outside a
temporary directory.

---

## Roadmap

- [ ] `BaseLLMClient` ABC — plug in Claude, Gemini, local Ollama
- [ ] Split flat `src/` into `core/`, `planning/`, `validation/`, `execution/`, `storage/`
- [ ] Executor integration tests — filesystem edits, sysfs writes, service management
- [ ] Cross-session memory — successful plans as few-shot context

## License

MIT — see [LICENSE](LICENSE).
