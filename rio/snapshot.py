# rio/snapshot.py — `python -m rio.snapshot [--out DIR] [--only SECTIONS]`: export the
# instance-config snapshot that now_field_exists / now_choice_values read, from the instance in
# .env, as the identity in .env.
#
# now-data's own CLI (`now-mcp-export`) reads SN_USER / SN_PASSWORD; rio reads SN_IDENTITY and its
# pair. This hands rio's credentials to now-data, then runs that CLI: same files, same messages,
# same exit code (1 when nothing was written, now-mcp#16).

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rio import config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m rio.snapshot",
                                     description="Export the instance-config snapshot of the instance in .env.")
    parser.add_argument("--out", default="snapshot", help="output directory (default: ./snapshot)")
    parser.add_argument("--only", default="", help="comma-separated sections: schema,platform,security,catalog")
    args = parser.parse_args(argv)
    try:
        from now_data import client, export
    except ImportError as e:
        print(f"now-data is not installed ({e.name}); see requirements.txt", file=sys.stderr)
        return 1
    from rio.tools.now import missing_schema

    problem = config.sn_config_problem()
    if not problem:
        try:
            client.configure(config.SN_INSTANCE, *config.sn_credentials())
        except client.ConfigError as e:  # e.g. a password of spaces, which sn_config_problem() lets by
            problem = str(e)
    if problem:
        print(f"Can't export: {problem}", file=sys.stderr)
        return 1
    # Resolved as the exporter resolves it (PowerShell hands `~` through unexpanded).
    root = Path(args.out).expanduser().resolve()
    code = export.main(["--out", str(root)] + (["--only", args.only] if args.only else []))
    if code != 0:
        return code
    # The same question rio's startup asks, so this never prints a line that rio then refuses.
    why_not = missing_schema(root)
    if why_not:
        print(f"\nNot usable by rio yet: {why_not}.")
    else:
        print(f"\nTo use it, set this in .env and restart rio:\nINSTANCE_CONFIG_ROOT={root}")
    return code


if __name__ == "__main__":
    sys.exit(main())
