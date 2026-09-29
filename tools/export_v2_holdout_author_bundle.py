"""Export the files an isolated V2 holdout author may read (design §7.4).

    python tools/export_v2_holdout_author_bundle.py <output-dir>

Copies exactly the files listed in eval/v2/holdout-input.manifest.json into a
directory outside the repository, after verifying every sha256 and the
content digest. Nothing is written unless every check passes. The bundle gets
its own bundle-manifest.json.

Maintainers refresh the in-repo manifest after changing an allowed input:

    python tools/export_v2_holdout_author_bundle.py --write-manifest --base-commit <sha>

Hashes are over LF-normalized bytes, i.e. the content git stores, so they do
not depend on a checkout's line-ending conversion.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_RELATIVE = "eval/v2/holdout-input.manifest.json"
MANIFEST_SCHEMA = "v2-holdout-input-manifest/1"
BUNDLE_SCHEMA = "v2-holdout-author-bundle/1"
BUNDLE_MANIFEST_NAME = "bundle-manifest.json"

ALLOWED_INPUTS = (
    "docs/v2/holdout-domain-spec.md",
    "eval/v2/case_contract.py",
    "eval/v2/spec/case.schema.json",
    "eval/v2/spec/slots.json",
    "eval/v2/spec/archetypes.json",
    "eval/v2/spec/holdout-plan.json",
    "eval/v2/spec/personas.json",
    "eval/v2/spec/final-outcomes.json",
    "aftersales/schema.sql",
    "system_fixtures/aftersales_demo_seed.sql",
    "docs/v2/stage4.3-frozen-manifest.json",
)
ALLOWED_GLOBS = ("policy_sources/*.md",)

# Whole path tokens (split on / . _ -) an author input may never contain.
DENIED_TOKENS = frozenset({
    "planner", "router", "routing", "dev", "validation", "handoff", "test", "tests",
    "diagnostic", "diagnostics", "trace", "runs", "artifacts", "holdout", "baseline", "git",
})
# "handoff" is also the name of a policy rule type; a policy source is domain
# input, never a stage handoff document.
_TOKEN_EXEMPTIONS = {"policy_sources": frozenset({"handoff"})}
# The allowed domain spec and holdout plan are about the holdout, not holdout data.
_PATH_EXEMPTIONS = {
    "docs/v2/holdout-domain-spec.md": frozenset({"holdout"}),
    "eval/v2/spec/holdout-plan.json": frozenset({"holdout"}),
}


class BundleRefused(RuntimeError):
    """The export was refused; nothing was written."""


def normalized_bytes(path: Path) -> bytes:
    return path.read_bytes().replace(b"\r\n", b"\n")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def path_tokens(relative: str) -> set[str]:
    return {token for token in re.split(r"[/._\-]+", relative.lower()) if token}


def denied_tokens(relative: str) -> set[str]:
    exempt = set(_PATH_EXEMPTIONS.get(relative, ()))
    exempt |= _TOKEN_EXEMPTIONS.get(relative.split("/", 1)[0], frozenset())
    return (path_tokens(relative) & DENIED_TOKENS) - exempt


def check_relative(relative: str) -> None:
    pure = PurePosixPath(relative)
    if (not relative or pure.is_absolute() or "\\" in relative or ":" in relative
            or any(part in ("", ".", "..") or part.startswith(".") for part in pure.parts)):
        raise BundleRefused("not a plain repo-relative path: " + relative)
    denied = denied_tokens(relative)
    if denied:
        raise BundleRefused(relative + " contains denied token(s): " + ", ".join(sorted(denied)))


def expected_paths(root: Path = REPO_ROOT) -> list[str]:
    paths = set(ALLOWED_INPUTS)
    for pattern in ALLOWED_GLOBS:
        paths |= {p.relative_to(root).as_posix() for p in root.glob(pattern) if p.is_file()}
    return sorted(paths)


def content_digest(files: list[dict]) -> str:
    """Over the schema and (path, sha256) pairs only; never commits or times."""
    payload = {"schema": MANIFEST_SCHEMA,
               "files": [{"path": f["path"], "sha256": f["sha256"]} for f in files]}
    return sha256_hex(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                 separators=(",", ":")).encode("utf-8"))


def build_manifest(base_commit: str, root: Path = REPO_ROOT) -> dict:
    files = []
    for relative in expected_paths(root):
        check_relative(relative)
        data = normalized_bytes(root / relative)
        files.append({"path": relative, "sha256": sha256_hex(data), "bytes": len(data)})
    return {
        "schema": MANIFEST_SCHEMA,
        "description": "The only files an isolated holdout author may read (design §7.4). "
                       "sha256 is over LF-normalized content. content_digest covers schema and "
                       "(path, sha256) pairs only.",
        "base_commit": base_commit,
        "hash_normalization": "crlf-to-lf",
        "content_digest": content_digest(files),
        "files": files,
    }


def load_manifest(root: Path = REPO_ROOT) -> dict:
    return json.loads((root / MANIFEST_RELATIVE).read_text(encoding="utf-8"))


def verify_manifest(manifest: dict, root: Path = REPO_ROOT) -> list[tuple[str, bytes]]:
    """Every check the export relies on. Returns (path, content) pairs."""
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise BundleRefused("unknown manifest schema")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise BundleRefused("manifest lists no files")
    listed = [f.get("path") for f in files]
    if listed != sorted(set(listed)):
        raise BundleRefused("manifest paths must be unique and sorted")
    if listed != expected_paths(root):
        raise BundleRefused("manifest paths differ from the allowed input list")
    if manifest.get("content_digest") != content_digest(files):
        raise BundleRefused("content digest mismatch")
    contents = []
    for entry in files:
        relative = entry["path"]
        check_relative(relative)
        source = (root / relative).resolve()
        if not source.is_relative_to(root.resolve()) or not source.is_file():
            raise BundleRefused("missing or outside the repository: " + relative)
        data = normalized_bytes(source)
        if sha256_hex(data) != entry.get("sha256"):
            raise BundleRefused("sha256 mismatch: " + relative)
        contents.append((relative, data))
    return contents


def check_destination(output_dir: Path, root: Path = REPO_ROOT) -> Path:
    target = output_dir.resolve()
    repo = root.resolve()
    if target == repo or target.is_relative_to(repo) or repo.is_relative_to(target):
        raise BundleRefused("output directory must be outside the repository")
    if target.exists():
        if not target.is_dir():
            raise BundleRefused("output path exists and is not a directory")
        if any(target.iterdir()):
            raise BundleRefused("output directory is not empty")
    return target


def export_bundle(output_dir: Path, root: Path = REPO_ROOT) -> dict:
    target = check_destination(Path(output_dir), root)
    manifest = load_manifest(root)
    contents = verify_manifest(manifest, root)
    target.mkdir(parents=True, exist_ok=True)
    for relative, data in contents:
        destination = target / PurePosixPath(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    bundle = {
        "schema": BUNDLE_SCHEMA,
        "input_manifest_schema": manifest["schema"],
        "base_commit": manifest["base_commit"],
        "hash_normalization": manifest["hash_normalization"],
        "content_digest": manifest["content_digest"],
        "files": [{"path": f["path"], "sha256": f["sha256"], "bytes": f["bytes"]}
                  for f in manifest["files"]],
    }
    (target / BUNDLE_MANIFEST_NAME).write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return bundle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("output_dir", nargs="?", type=Path)
    parser.add_argument("--write-manifest", action="store_true")
    parser.add_argument("--base-commit")
    args = parser.parse_args(argv)
    try:
        if args.write_manifest:
            if args.output_dir is not None or not args.base_commit:
                parser.error("--write-manifest takes --base-commit and no output directory")
            manifest = build_manifest(args.base_commit)
            (REPO_ROOT / MANIFEST_RELATIVE).write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8", newline="\n")
            print(json.dumps({"files": len(manifest["files"]),
                              "content_digest": manifest["content_digest"]}))
            return 0
        if args.output_dir is None:
            parser.error("output directory is required")
        bundle = export_bundle(args.output_dir)
    except BundleRefused as exc:
        print("refused: " + str(exc), file=sys.stderr)
        return 1
    print(json.dumps({"files": len(bundle["files"]), "content_digest": bundle["content_digest"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
