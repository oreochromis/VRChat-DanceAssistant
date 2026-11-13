"""Run the complete unittest suite in bounded fresh processes.

GUI integration batches stay small because closed native Qt windows and delayed
callbacks can otherwise accumulate across hundreds of independent test cases.
Crashes/timeouts are failures, and checkpoints retain the number actually run.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
FIELDS = ("failures", "errors", "skipped", "expected_failures", "unexpected_successes")


def _report():
    return {"status": "failed", "platform": sys.platform, "python": sys.version,
            "qt_platform": "offscreen", "tests_run": 0, "passed": 0,
            **{key: [] for key in FIELDS}}


def _write(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


def _child(names, path):
    class CheckpointResult(unittest.TextTestResult):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.passed = 0
            self.current = None

        def checkpoint(self):
            data = _report()
            data.update(tests_run=self.testsRun, passed=self.passed, in_progress=self.current,
                        failures=[{"test": str(test), "traceback": detail} for test, detail in self.failures],
                        errors=[{"test": str(test), "traceback": detail} for test, detail in self.errors],
                        skipped=[{"test": str(test), "reason": reason} for test, reason in self.skipped],
                        expected_failures=[str(test) for test, _ in self.expectedFailures],
                        unexpected_successes=[str(test) for test in self.unexpectedSuccesses])
            data["status"] = "passed" if (self.testsRun and self.current is None
                                          and not any(data[key] for key in FIELDS)) else "failed"
            _write(path, data)

        def startTest(self, test):
            super().startTest(test)
            self.current = test.id()
            self.checkpoint()

        def addSuccess(self, test):
            super().addSuccess(test)
            self.passed += 1

        def stopTest(self, test):
            super().stopTest(test)
            self.current = None
            self.checkpoint()

    _write(path, _report())
    suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2, resultclass=CheckpointResult).run(suite)
    result.checkpoint()
    return 0 if (result.wasSuccessful() and result.testsRun == len(names)
                 and not result.skipped and not result.expectedFailures
                 and not result.unexpectedSuccesses) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports", type=Path, default=ROOT / "build-reports")
    parser.add_argument("--ui-batch-size", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--child", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--report", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.ui_batch_size < 1 or args.timeout <= 0:
        parser.error("Batch size and timeout must be positive.")
    os.chdir(ROOT)
    sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    if args.child:
        return _child(json.loads(args.child.read_text(encoding="utf-8")), args.report)

    args.reports.mkdir(parents=True, exist_ok=True)
    combined = _report()
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    names = [case.id() for case in _flatten(suite)]
    combined["tests_discovered"] = len(names)
    normal = [name for name in names if not name.startswith("test_ui.")]
    gui = [name for name in names if name.startswith("test_ui.")]
    groups = ([normal] if normal else []) + [gui[i:i + args.ui_batch_size] for i in range(0, len(gui), args.ui_batch_size)]
    with (args.reports / "unit-tests.log").open("w", encoding="utf-8") as log, tempfile.TemporaryDirectory(prefix="dance-tests-") as temporary:
        def emit(line):
            log.write(line)
            log.flush()
            sys.stdout.write(line)
            sys.stdout.flush()

        for index, group in enumerate(groups):
            names_path = Path(temporary) / f"group-{index}.json"
            report_path = Path(temporary) / f"result-{index}.json"
            names_path.write_text(json.dumps(group), encoding="utf-8")
            emit(f"\n=== Isolated test group {index + 1}/{len(groups)} ({len(group)} cases) ===\n")
            process = subprocess.Popen([sys.executable, "-u", str(Path(__file__).resolve()),
                                        "--child", str(names_path), "--report", str(report_path)],
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, encoding="utf-8", errors="replace")
            def forward():
                for line in process.stdout:
                    emit(line)
            reader = threading.Thread(target=forward, daemon=True)
            reader.start()
            timed_out = False
            try:
                process.wait(timeout=args.timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                process.kill()
                process.wait()
            reader.join(timeout=5)
            process.stdout.close()
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                report = _report()
            combined["tests_run"] += report["tests_run"]
            combined["passed"] += report["passed"]
            for key in FIELDS:
                combined[key].extend(report[key])
            if timed_out or process.returncode != 0 or report["tests_run"] != len(group):
                message = (f"Group {index + 1} timed out after {args.timeout:g}s" if timed_out
                           else f"Group {index + 1} exited with code {process.returncode}")
                message += f"; {report['tests_run']}/{len(group)} cases started, {report['passed']} passed."
                if report.get("in_progress"):
                    message += " In progress: " + report["in_progress"]
                emit(message + "\n")
                if timed_out or not any(report[key] for key in FIELDS) or report["tests_run"] != len(group):
                    combined["errors"].append({"test": f"isolated-group-{index + 1}",
                                               "traceback": message + "\nRequested tests:\n" + "\n".join(group)})
            _write(args.reports / "unit-tests.json", combined)
        combined["status"] = "passed" if (combined["tests_run"] == len(names) > 0
                                          and combined["passed"] == len(names)
                                          and not any(combined[key] for key in FIELDS)) else "failed"
        _write(args.reports / "unit-tests.json", combined)
        emit(f"\nComplete suite: {combined['tests_run']}/{len(names)} run; {combined['passed']} passed; "
             + ", ".join(f"{len(combined[key])} {key}" for key in FIELDS) + ".\n")
    return 0 if combined["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
