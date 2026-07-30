"""Deprecated compatibility entrypoint for the renamed ``amcodex`` command."""

from __future__ import annotations

from mycodex.commands import amcodex_cli


def main(argv=None):
    return amcodex_cli.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
