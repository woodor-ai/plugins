"""Public mycodex command: launch one Codex lease."""

from __future__ import annotations

import sys

from mycodex.launcher import codex_tui_session


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] in (["update"], ["--update"]):
        print(
            "mycodex --update has moved to am-update. Run: am-update",
            file=sys.stderr,
        )
        return 2
    return codex_tui_session.main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
