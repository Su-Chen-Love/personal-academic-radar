"""Copy or verify the canonical local UI in the published Site checkout."""
import argparse
from pathlib import Path
import shutil


UI_FOLDERS = ("templates", "static")


def differences(source: Path, target: Path) -> list[str]:
    """Compare the complete file inventory and bytes; checking never writes."""
    problems = []
    for folder in UI_FOLDERS:
        canonical = source / folder
        shared = target / folder
        if not canonical.is_dir():
            problems.append(f"Missing canonical directory: {folder}")
            continue
        if not shared.is_dir():
            problems.append(f"Missing shared directory: {folder}")
            continue
        expected = {path.relative_to(canonical) for path in canonical.rglob("*") if path.is_file()}
        actual = {path.relative_to(shared) for path in shared.rglob("*") if path.is_file()}
        for path in sorted(expected - actual):
            problems.append(f"Missing shared file: {folder}/{path}")
        for path in sorted(actual - expected):
            problems.append(f"Unexpected shared file: {folder}/{path}")
        for path in sorted(expected & actual):
            if (canonical / path).read_bytes() != (shared / path).read_bytes():
                problems.append(f"Different shared file: {folder}/{path}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Reject missing, extra, or different shared UI files without changing them")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    source = root / "src/academic_radar"
    target = root / "sites/shared"
    if not args.check:
        for folder in UI_FOLDERS:
            destination = target / folder
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(source / folder, destination)
    problems = differences(source, target)
    if problems:
        print("Shared Site UI differs from the canonical local UI:")
        for problem in problems:
            print(problem)
        print("Run python scripts/sync-site-ui.py before building the Site.")
        return 1
    print("Shared Site UI matches every local template and static asset byte for byte.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
