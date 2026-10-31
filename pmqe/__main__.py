"""Entry point for ``python -m pmqe`` and the ``pmqe`` command.

Nothing heavy is imported at module level: OCR worker processes re-run the
main module when they start (and the frozen app re-runs this file), so the
Qt GUI must only be imported after ``freeze_support()`` has diverted them.

``pmqe --self-test [REPORT.json]`` checks an installation end to end.
"""

import sys


def main() -> None:
    import multiprocessing

    multiprocessing.freeze_support()

    if "--self-test" in sys.argv:
        from pmqe.selftest import run

        idx = sys.argv.index("--self-test")
        report = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else None
        sys.exit(run(report))

    from pmqe.gui import main as run_gui

    run_gui()


if __name__ == "__main__":
    main()
