from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from time import monotonic

from .parallel import default_jobs
from .pipeline import obfuscate_modules
from .renamers.allowlist import read_allowlist

__all__: list[str] = ["main"]


def main() -> None:
    started = monotonic()
    parser = argparse.ArgumentParser(description="Flatten src/ into a release-only app.min.py.")
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Overwrite app.min.py in an existing output directory",
    )
    parser.add_argument("--report", action="store_true", help="Show allowlisted identifier renames")
    parser.add_argument(
        "-d",
        "--debug",
        action="store_true",
        help="Add original class, function, argument, and attribute names as comments",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=default_jobs(),
        metavar="N",
        help="Worker processes (default: half the available CPUs; 1 disables multiprocessing)",
    )
    parser.add_argument(
        "--allowlist",
        default=str(Path(__file__).resolve().parents[2] / "minifier_allowlist.json"),
        help="Path to a JSON list, or a literal JSON list, of identifiers allowed to be renamed",
    )
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")
    print("Renaming classes enabled. Beware of mangled log output.", file=sys.stderr, flush=True)
    source = args.source.resolve()
    output = args.output.resolve()
    if output == source or output in source.parents or source in output.parents:
        parser.error("source and output must be separate, non-overlapping directories")
    if output.exists():
        if not output.is_dir():
            parser.error("output must be a directory")
        if not args.force:
            parser.error("output already exists; use --force to overwrite it")
    paths: list[Path] = []
    for directory, directories, files in os.walk(source):
        # Build tooling and tests must never become part of the application.
        directories[:] = [
            name for name in directories if name not in {"tests", "obfuscator", "__pycache__"}
        ]
        paths.extend(Path(directory) / name for name in files if name.endswith(".py"))
    originals = {
        path.relative_to(source): path.read_text(encoding="utf-8") for path in sorted(paths)
    }
    count = len(originals)
    if not originals:
        raise SystemExit("No application Python modules found")

    def report_progress(stage: str) -> None:
        print(f"[{monotonic() - started:.1f}s] {stage}...", file=sys.stderr, flush=True)

    try:
        result = obfuscate_modules(
            originals,
            read_allowlist(args.allowlist),
            jobs=args.jobs,
            progress=report_progress,
            debug=args.debug,
        )
    except ValueError as error:
        parser.error(str(error))
    output.mkdir(parents=True, exist_ok=args.force)
    (output / "app.min.py").write_text(result.source, encoding="utf-8")
    before = sum(len(original.encode("utf-8")) for original in originals.values())
    after = len(result.source.encode("utf-8"))
    print(
        f"Flattened {count} application modules into app.min.py: {before:,} -> {after:,} source bytes"
    )
    print(f"Renamed {len(result.renames):,} allowlisted identifiers")
    print(
        f"Renamed {result.renamed_parameters:,}/"
        f"{result.renamed_parameters + result.skipped_parameters.total():,} parameters automatically"
    )
    print(f"Renamed {result.renamed_methods:,} private methods automatically")
    if args.report:
        for reason, preserved in result.skipped_parameters.most_common():
            print(f"  Preserved {preserved:,}: {reason}")
        for entry, name in result.renames.items():
            print(f"  {entry} -> {name}")
    sys.stdout.flush()
    print(f"Completed in {monotonic() - started:.2f}s", file=sys.stderr, flush=True)
