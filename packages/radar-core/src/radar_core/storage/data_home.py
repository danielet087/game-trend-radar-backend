"""Whether a source's documents reach the site from Git or from the database.

The flag ``data/publication/data_home.json`` is an ordinary document of the
source it governs, so every job reads the same database copy::

    {"schema_version": 1, "site": "git" | "database", "git_projection": true | false}

* No flag: the site is built from Git (the deploy only compares the database
  copy) and every publication pushes its snapshot to Git, as before.
* ``"site": "database"``: the deploy builds the site's data from the newest
  database snapshot instead of the Git tree.
* ``"git_projection": false``: publications commit to the database only. They
  never push, acknowledge no projection and report the database manifest
  digest as their published revision. It requires ``"site": "database"``,
  because nothing would update the Git copy the site would be built from.

Turning ``git_projection`` back on is the rollback: the next publication
projects the newest database snapshot and pushes it to Git in one commit.
Publications cannot change the flag; only this command writes it::

    python -m radar_core.storage.data_home show
    python -m radar_core.storage.data_home set --site database --git-projection true

Database credentials come from the environment, as for every primary read.
Reports contain only the source, generation and mode; never credentials.
"""
from __future__ import annotations

import argparse
import json

from .sql import StorageError

FLAG = "data/publication/data_home.json"
REPOSITORY = "danielet087/game-trend-radar"
BRANCH = "main"
SITES = ("git", "database")
FIELDS = {"schema_version", "site", "git_projection"}


def parse(raw):
    """The mode a flag document selects; ``None`` (no flag) keeps Git."""
    if raw is None:
        return {"site": "git", "git_projection": True}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        value = None
    if not (isinstance(value, dict) and set(value) == FIELDS
            and type(value["schema_version"]) is int and value["schema_version"] == 1
            and value["site"] in SITES and type(value["git_projection"]) is bool
            and (value["git_projection"] or value["site"] == "database")):
        raise StorageError("INVALID_DATA_HOME", "The data home flag is not a valid document.")
    return {"site": value["site"], "git_projection": value["git_projection"]}


def document(*, site, git_projection):
    raw = (json.dumps({"schema_version": 1, "site": site, "git_projection": git_projection},
                      indent=2) + "\n").encode("utf-8")
    parse(raw)
    return raw


def mode(database, source):
    """The mode recorded in a source's newest snapshot, read with ``database``."""
    from . import primary_store
    _, files = primary_store.read_files(database, source, [FLAG])
    return parse(files.get(FLAG))


def current(repository=REPOSITORY, *, branch=BRANCH):
    """``(head, mode, present)`` from the newest snapshot of an active source."""
    from . import primary
    head, files = primary.read_files(repository, [FLAG], branch=branch)
    return head, parse(files.get(FLAG)), FLAG in files


def change(repository=REPOSITORY, *, branch=BRANCH, site, git_projection):
    """Write the flag with a compare-and-swap on the generation that was read.

    The write itself follows the mode it selects: switching the projection off
    is never pushed, and switching it back on is pushed with the next snapshot.
    """
    from . import primary
    from ..publication import snapshot_revision
    raw = document(site=site, git_projection=git_projection)
    head, selected, present = current(repository, branch=branch)
    wanted = {"site": site, "git_projection": git_projection}
    if selected == wanted:
        return {"status": "unchanged", "generation": head["generation"], **wanted,
                "flag": "present" if present else "absent"}
    # The generation keeps a later switch back distinct from an earlier one.
    operation = snapshot_revision({"document": FLAG, "base_generation": head["generation"], **wanted})
    receipt = primary.write_files(repository, {FLAG: raw}, base_generation=head["generation"],
        operation_id=operation, payload_revision=operation, branch=branch,
        projection_required=git_projection)
    return {"status": "written", "generation": receipt["generation"], **wanted, "flag": "present",
            "previous": selected, "projection_pending": receipt["projection_pending"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Show or change where a source's documents live.")
    parser.add_argument("--repository", default=REPOSITORY)
    parser.add_argument("--branch", default=BRANCH)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("show")
    command = commands.add_parser("set")
    command.add_argument("--site", required=True, choices=SITES)
    command.add_argument("--git-projection", required=True, choices=("true", "false"))
    args = parser.parse_args(argv)
    source = {"source_id": args.repository + "@" + args.branch}
    try:
        if args.command == "show":
            head, selected, present = current(args.repository, branch=args.branch)
            report = {"status": "ok", "generation": head["generation"], **selected,
                      "flag": "present" if present else "absent"}
        else:
            report = change(args.repository, branch=args.branch, site=args.site,
                            git_projection=args.git_projection == "true")
    except StorageError as error:
        print(json.dumps({"status": "error", **source, "code": error.code}))
        return 1
    print(json.dumps({**report, **source}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
