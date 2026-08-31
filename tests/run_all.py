"""Lance les suites Python et navigateur en parallèle."""
from __future__ import annotations

import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SUITES = (
    ("python", [sys.executable, "-m", "unittest", "discover", "-s", "tests"]),
    ("browser", ["npm", "run", "test:e2e:all"]),
)


def run_suite(name: str, command: list[str]) -> tuple[str, int, float]:
    started = time.monotonic()
    print(f"[{name}] démarrage : {' '.join(command)}", flush=True)
    result = subprocess.run(command, cwd=ROOT)
    elapsed = time.monotonic() - started
    print(f"[{name}] terminé avec {result.returncode} en {elapsed:.2f}s", flush=True)
    return name, result.returncode, elapsed


def main() -> int:
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=len(SUITES)) as pool:
        futures = [pool.submit(run_suite, name, command) for name, command in SUITES]
        results = [future.result() for future in futures]

    elapsed = time.monotonic() - started
    failures = [name for name, returncode, _ in results if returncode != 0]
    if failures:
        print(f"Suites en échec : {', '.join(failures)}", flush=True)
        return 1
    print(f"Toutes les suites sont vertes en {elapsed:.2f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
