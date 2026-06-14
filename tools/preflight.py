"""Run the local release gate before committing or deploying.

The checks here mirror the validations that repeatedly caught real regressions
in this repo: contract tests, Python syntax, embedded Kaggle runner syntax,
frontend JS syntax, and whitespace/conflict markers.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Check:
    name: str
    command: list[str]
    required: bool = True


def _python() -> str:
    return sys.executable


def planned_checks() -> list[Check]:
    checks = [
        Check("unit contracts", [_python(), "-m", "unittest", "discover", "-v"]),
        Check("python compileall", [_python(), "-m", "compileall", "app.py", "database.py", "montador.py", "services", "tests", "tools"]),
        Check(
            "embedded kaggle runner",
            [
                _python(),
                "-c",
                "from services import kaggle_service; compile(kaggle_service._RUNNER, 'runner.py', 'exec'); print('runner ok')",
            ],
        ),
        Check("smoke review flow", [_python(), "tools/smoke_review_flow.py"]),
        Check("smoke long mode", [_python(), "tools/smoke_long_mode.py"]),
        Check("review csrf repro", [_python(), "tools/repro_review_prod.py"]),
    ]
    node = shutil.which("node")
    if node:
        checks.append(Check("frontend js syntax", [node, "--check", "static/gallery.js"]))
    else:
        checks.append(Check("frontend js syntax", ["node", "--check", "static/gallery.js"], required=False))
    git = shutil.which("git")
    if git:
        checks.append(Check("git whitespace", [git, "diff", "--check"]))
    else:
        checks.append(Check("git whitespace", ["git", "diff", "--check"], required=False))
    return checks


def run_check(check: Check) -> tuple[bool, str]:
    if not check.required and not shutil.which(check.command[0]):
        return True, f"SKIP {check.name}: {check.command[0]} unavailable"
    print(f"\n==> {check.name}")
    print(" ".join(check.command))
    proc = subprocess.run(
        check.command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=900,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0:
        tail = "\n".join(output.strip().splitlines()[-8:])
        return True, tail or "OK"
    return False, output.strip() or f"exit {proc.returncode}"


def main() -> int:
    failures: list[tuple[str, str]] = []
    for check in planned_checks():
        ok, detail = run_check(check)
        print(detail)
        if not ok:
            failures.append((check.name, detail))
            break
    if failures:
        print("\nPREFLIGHT FAILED")
        for name, detail in failures:
            print(f"\n-- {name}\n{detail}")
        return 1
    print("\nPREFLIGHT OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
