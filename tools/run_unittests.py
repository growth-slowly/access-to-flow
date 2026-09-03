"""Run the project's test modules with the standard library alone.

The repository's test directories are named ``TASK-ACCESS-00X``, which pytest
handles but ``unittest discover`` cannot import.  This runner loads each
``test_*.py`` by path instead, so the suite can be executed on a machine with
no third-party packages installed - the same constraint the converter itself
has to meet.

Usage::

    python tools/run_unittests.py [path-under-tests ...]
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(path: Path) -> unittest.TestSuite:
    # Test modules may import sibling helpers (``_support``), so their own
    # directory has to be importable while they load.
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    name = "acctest_" + path.parent.name.replace("-", "_") + "_" + path.stem
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return unittest.defaultTestLoader.loadTestsFromModule(module)


def main(argv: list[str]) -> int:
    sys.path.insert(0, str(ROOT))
    roots = [ROOT / "tests" / arg for arg in argv] or [ROOT / "tests"]
    suite = unittest.TestSuite()
    files: list[Path] = []
    for root in roots:
        files.extend(sorted(root.rglob("test_*.py")) if root.is_dir() else [root])
    loaded = 0
    for path in files:
        if "__pycache__" in path.parts:
            continue
        try:
            suite.addTest(load(path))
            loaded += 1
        except ImportError as error:
            print(f"SKIP {path.relative_to(ROOT)}: {error}")
    print(f"loaded {loaded} test module(s)")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
