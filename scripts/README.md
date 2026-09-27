# scripts

One click entry points for local runs and one time setup. Every script only
uses tools that are already installed: nothing here downloads or installs
binaries for you.

## install

```
scripts/install.sh      # bash
scripts/install.ps1     # powershell
```

Steps, in order:

1. locate `uv`, stop with a clear message and exit 1 when it is missing
2. locate `python` (or `py -3`) and require 3.12 or newer, exit 1 otherwise
3. `uv sync`
4. `uv run nodebench doctor`

The exit code of the last step is returned, so a failing doctor exits non zero.

## run

```
scripts/run.sh              # bash
scripts/run.ps1             # powershell
scripts/run.bat             # cmd
```

Behaviour:

- profile comes from `NODEBENCH_PROFILE` and defaults to `local`
- extra arguments are passed through, for example
  `scripts/run.bat --dry-run` or `scripts/run.sh --strict`
- stdout and stderr are captured to `output/logs/run-<yyyyMMdd-HHmmss>.log`
  and echoed to the console
- the exit code of `nodebench run` is returned unchanged
- a missing `uv` is reported explicitly and exits non zero

Manual run without scripts:

```
uv run nodebench run --profile local
```

## scheduler

```
uv run nodebench scheduler install --profile local --time 04:37 --yes
uv run nodebench scheduler status
uv run nodebench scheduler uninstall --name nodebench-local-0437 --yes
```

`install` and `uninstall` print the plan first and only touch the system
scheduler after `--yes` (or an interactive confirmation). `--dry-run` prints
the plan without executing anything.

## notes for unix

Make the shell scripts executable once after checkout:

```
chmod +x scripts/run.sh scripts/install.sh
```

The `.sh` files use LF line endings, the `.ps1` and `.bat` files use CRLF.
