#!/usr/bin/env python3
"""wechat-publisher CLI: dispatch to newspic or article subcommand.

Usage:
  publish.py newspic [args...]    Publish an image-text post (article_type=newspic)
  publish.py article  [args...]    Publish a markdown article (article_type=news)
  publish.py --help                Show help
"""

from __future__ import annotations

import argparse
import json
import sys

from lib.errors import PublishError

import mode_article
import mode_newspic


def _newspic_parser(parent: argparse.ArgumentParser) -> None:
    parent.add_argument("source", nargs="?", help="Source file (.md/.yaml/.yml/.json). Defaults to source.md in the current directory.")
    parent.add_argument("--title", help="Override/provide the draft title.")
    parent.add_argument("--content", help="Override/provide the draft content.")
    parent.add_argument("--image", action="append", metavar="PATH", help="Image path; repeat for multiple images. Replaces images from the source.")
    parent.add_argument("--manifest", help="Validated pipeline .pipeline/manifest.json; binds sealed text and ordered images.")
    parent.add_argument("--snapshot", help="Immutable pipeline .pipeline/publish-snapshot.json.")
    parent.add_argument("--author", help="Override/provide the author.")
    parent.add_argument("--digest", help="Override/provide the digest.")
    parent.add_argument("--account", help="Account alias from WECHAT_ACCOUNTS.")
    parent.add_argument("--env-file", help="Path to .env/.env.local.")
    parent.add_argument("--dry-run", action="store_true", help="Validate and print the draft payload without uploading.")
    parent.add_argument("--yes", action="store_true", help="Skip interactive confirmation.")
    parent.add_argument("--result-output", help="Atomically persist the draft receipt for duplicate-safe resume.")
    parent.add_argument("--verify-draft", action="store_true", help="Read back and verify the created draft. Requires --result-output.")
    parent.add_argument("--recover-draft-media-id", help="Bind a manually confirmed draft media_id after an ambiguous draft/add result, then verify it.")


def _article_parser(parent: argparse.ArgumentParser) -> None:
    parent.add_argument("markdown_pos", nargs="?", help="Path to a markdown file (.md). Positional or use --markdown.")
    parent.add_argument("--markdown", dest="markdown", help="Path to a markdown file (.md). Same as the positional argument.")
    parent.add_argument("--html", help="Path to a pre-typeset gzh-design HTML body fragment. Mutually exclusive with Markdown input.")
    parent.add_argument("--layout-manifest", help="Pipeline .pipeline/layout.json to validate with --html.")
    parent.add_argument("--snapshot", help="Immutable pipeline .pipeline/publish-snapshot.json.")
    parent.add_argument("--theme", help="Theme name: default | grace | simple | modern.")
    parent.add_argument("--color", help="Primary color preset (blue/green/...) or #hex.")
    parent.add_argument("--no-cite", action="store_true", help="Disable bottom-citation rewriting of ordinary external links.")
    parent.add_argument("--title", help="Override the rendered title.")
    parent.add_argument("--author", help="Override the rendered author.")
    parent.add_argument("--summary", help="Override the rendered summary/digest.")
    parent.add_argument("--cover", help="Cover image path. Falls back to frontmatter coverImage/featureImage/cover/image, then imgs/cover.png, then first body image.")
    parent.add_argument("--account", help="Account alias from WECHAT_ACCOUNTS.")
    parent.add_argument("--env-file", help="Path to .env/.env.local.")
    parent.add_argument("--dry-run", action="store_true", help="Validate and print the plan without uploading.")
    parent.add_argument("--yes", action="store_true", help="Skip interactive confirmation.")
    parent.add_argument("--result-output", help="Atomically persist the draft receipt for duplicate-safe resume.")
    parent.add_argument("--verify-draft", action="store_true", help="Read back and verify the created draft. Requires --result-output.")
    parent.add_argument("--recover-draft-media-id", help="Bind a manually confirmed draft media_id after an ambiguous draft/add result, then verify it.")


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        _show_help()
        return 0
    if sys.argv[1] not in ("newspic", "article"):
        sys.stderr.write(f"error: unknown subcommand '{sys.argv[1]}'. Use 'newspic' or 'article'. Run with --help for usage.\n")
        return 2

    parser = argparse.ArgumentParser(prog=f"publish.py {sys.argv[1]}")
    if sys.argv[1] == "newspic":
        _newspic_parser(parser)
    else:
        _article_parser(parser)

    args = parser.parse_args(sys.argv[2:])
    args.mode = sys.argv[1]

    try:
        if args.mode == "newspic":
            return mode_newspic.run(args)
        return mode_article.run(args)
    except (PublishError, OSError, json.JSONDecodeError) as err:
        sys.stderr.write(f"error: {err}\n")
        return 1


def _show_help() -> None:
    sys.stdout.write(
        "Usage: publish.py <subcommand> [args]\n"
        "\n"
        "Subcommands:\n"
        "  newspic    Image-text post (article_type=newspic)\n"
        "  article    Markdown or validated gzh-design HTML article (article_type=news)\n"
        "\n"
        "Run `publish.py <subcommand> --help` for subcommand-specific options.\n"
    )


if __name__ == "__main__":
    raise SystemExit(main())
