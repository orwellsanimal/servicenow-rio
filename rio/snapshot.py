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
    problem = config.sn_config_problem()
    if problem:
        print(f"Can't export: {problem}", file=sys.stderr)
        return 1
    client.configure(config.SN_INSTANCE, *config.sn_credentials())
    code = export.main(["--out", args.out] + (["--only", args.only] if args.only else []))
    root = Path(args.out).resolve()
    if code == 0 and (root / "schema" / "tables.json").is_file():
        print(f"\nTo use it, set this in .env and restart rio:\nINSTANCE_CONFIG_ROOT={root}")
    elif code == 0:  # e.g. --only security into an empty folder: now-config can't load it yet
        print(f"\n{root} has no schema yet, which now-config needs: run it once without --only.")
    return code


if __name__ == "__main__":
    sys.exit(main())
