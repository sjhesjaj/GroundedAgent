"""Local policy operations; never exposed as Agent tools.

python -m aftersales.policy_cli --root <wiki-root> compile [--sources policy_sources]
python -m aftersales.policy_cli --root <wiki-root> diff build-0001 build-0002
python -m aftersales.policy_cli --root <wiki-root> publish build-0002
python -m aftersales.policy_cli --root <wiki-root> rollback build-0001
python -m aftersales.policy_cli --root <wiki-root> freeze
python -m aftersales.policy_cli --root <wiki-root> verify <manifest.json>
"""
import argparse
import json
from pathlib import Path

from wiki_maintenance.repository import WikiRepository
from .policy_lifecycle import (CORPUS_PATH, compile_policy_draft, diff_policy_builds,
                               frozen_manifest, load_policy_sources, read_corpus, verify_frozen_manifest)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    compile_command = commands.add_parser("compile")
    compile_command.add_argument("--sources", type=Path, default=CORPUS_PATH)
    diff = commands.add_parser("diff")
    diff.add_argument("old")
    diff.add_argument("new")
    for command in ("publish", "rollback"):
        commands.add_parser(command).add_argument("build_id")
    commands.add_parser("freeze")
    commands.add_parser("verify").add_argument("manifest", type=Path)
    args = parser.parse_args(argv)
    repository = WikiRepository(args.root)
    if args.command == "compile":
        build = compile_policy_draft(repository, read_corpus(args.sources))
        result = {"build_id": build.build_id, "status": "draft"}
    elif args.command == "diff":
        result = diff_policy_builds(repository, args.old, args.new)
    elif args.command == "freeze":
        result = frozen_manifest(repository)
    elif args.command == "verify":
        verify_frozen_manifest(repository, json.loads(args.manifest.read_text(encoding="utf-8")))
        result = {"verified": True, "build_id": repository.get_current_build_id()}
    else:
        load_policy_sources(repository, repository.load_build(args.build_id))
        result = getattr(repository, args.command)(args.build_id).to_dict()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
