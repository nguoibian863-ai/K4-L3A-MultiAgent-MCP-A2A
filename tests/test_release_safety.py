import subprocess
from pathlib import Path


def test_repository_contains_no_competition_payload() -> None:
    root = Path(__file__).resolve().parents[1]
    # Check the release inventory; local runtime data is intentionally gitignored.
    if (root / ".git").exists():
        files = subprocess.check_output(["git", "ls-files", "-z"], cwd=root)
        paths = [Path(name) for name in files.decode("utf-8").split("\0") if name]
    else:
        paths = [p.relative_to(root) for p in root.rglob("*") if p.is_file()]
    assert Path("case-set.json") not in paths
    assert not any(p.parts[0] in {"inputs", "outputs"} and p.suffix == ".json" for p in paths)
    forbidden = {"oracles", "reference-outputs", "private-partitions.json", "mcp-access.json"}
    assert not any(path.name in forbidden for path in paths)


def test_example_environment_has_no_real_key() -> None:
    root = Path(__file__).resolve().parents[1]
    content = (root / ".env.example").read_text(encoding="utf-8")
    assert "sk-team-replace_me" in content
    assert content.count("sk-team-") == 1
