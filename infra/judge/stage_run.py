#!/usr/bin/env python3
"""Copy a finished RA run into a fresh folder for judging, leaving out what the paper's judge did not see.

Usage: stage_run.py SRC_RUN_DIR DEST_DIR [--excluded FILE.tsv] [--max-mb 200]

Left out (each listed in the --excluded TSV: path, reason, bytes):
  - earlier verdicts and criteria at the top level: judge_result.md, judge_result.md.* and JUDGE.md (judge.sh adds
    the task's JUDGE.md afterwards);
  - the agent-session state .dsh_ra3/ (prompts and transcripts) and .dsh_gw/, and .claude* folders and files;
  - the orchestrator's snapshot folder _pece/snap/;
  - Python virtual environments (a folder holding pyvenv.cfg, or named venv or .venv), site-packages folders, and
    pip --target trees (a folder whose name starts with "lib" and that holds a *.dist-info folder);
  - files larger than --max-mb megabytes (default 200).
These follow how the paper's runs reached the judge: they were copied to the judged tree without these items.
Symbolic links are copied as links, never followed. DEST_DIR must not exist yet. Python 3.6 or newer.
"""
import argparse
import os
import shutil
import stat
import sys

TOP_EXCLUDE_PREFIX = ("judge_result.md",)
TOP_EXCLUDE = ("JUDGE.md",)
DIR_EXCLUDE = (".dsh_ra3", ".dsh_gw", "site-packages", "venv", ".venv")


def reason_for_dir(rel, full):
    name = os.path.basename(rel)
    if name in DIR_EXCLUDE:
        return "agent-session state" if name.startswith(".dsh") else "Python environment or packages"
    if name.startswith(".claude"):
        return "agent tool state"
    if rel.replace(os.sep, "/") == "_pece/snap":
        return "orchestrator snapshot"
    try:
        entries = os.listdir(full)
    except OSError:
        entries = []
    if "pyvenv.cfg" in entries:
        return "Python environment or packages"
    if name.startswith("lib") and any(e.endswith(".dist-info") for e in entries):
        return "pip --target package tree"
    return None


def reason_for_file(rel, size, max_bytes):
    name = os.path.basename(rel)
    top = os.sep not in rel
    if top and (name in TOP_EXCLUDE or any(name.startswith(p) for p in TOP_EXCLUDE_PREFIX)):
        return "earlier verdict or criteria"
    if name.startswith(".claude"):
        return "agent tool state"
    if size > max_bytes:
        return "larger than the size limit"
    return None


def tree_bytes(path):
    n = 0
    for dp, dn, fn in os.walk(path):
        for f in fn:
            try:
                n += os.lstat(os.path.join(dp, f)).st_size
            except OSError:
                pass
    return n


def stage(src, dest, max_mb=200):
    """Copy src to dest with the exclusions; returns [(relative path, reason, bytes)] of what was left out."""
    if os.path.exists(dest):
        raise ValueError("%s exists already" % dest)
    max_bytes = int(max_mb * 1024 * 1024)
    excluded = []
    os.makedirs(dest)
    for dp, dn, fn in os.walk(src):
        rel_dir = os.path.relpath(dp, src)
        keep = []
        for d in sorted(dn):
            full = os.path.join(dp, d)
            rel = os.path.normpath(os.path.join(rel_dir, d))
            if os.path.islink(full):
                os.symlink(os.readlink(full), os.path.join(dest, rel))
                continue
            why = reason_for_dir(rel, full)
            if why:
                excluded.append((rel + "/", why, tree_bytes(full)))
                continue
            os.makedirs(os.path.join(dest, rel))
            keep.append(d)
        dn[:] = keep
        for f in sorted(fn):
            full = os.path.join(dp, f)
            rel = os.path.normpath(os.path.join(rel_dir, f))
            st = os.lstat(full)
            if stat.S_ISLNK(st.st_mode):
                os.symlink(os.readlink(full), os.path.join(dest, rel))
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            why = reason_for_file(rel, st.st_size, max_bytes)
            if why:
                excluded.append((rel, why, st.st_size))
                continue
            shutil.copy2(full, os.path.join(dest, rel))
            os.chmod(os.path.join(dest, rel), (st.st_mode & 0o777) | stat.S_IWUSR)
    return excluded


def main(argv=None):
    ap = argparse.ArgumentParser(description="Copy a finished RA run for judging, without the items the judge did not see.")
    ap.add_argument("src")
    ap.add_argument("dest")
    ap.add_argument("--excluded", help="write the list of left-out items here (TSV)")
    ap.add_argument("--max-mb", type=float, default=200)
    a = ap.parse_args(argv)
    if not os.path.isdir(a.src):
        sys.stderr.write("stage_run: %s is not a folder\n" % a.src)
        return 2
    try:
        excluded = stage(a.src, a.dest, a.max_mb)
    except (ValueError, OSError) as e:
        sys.stderr.write("stage_run: %s\n" % e)
        return 2
    if a.excluded:
        with open(a.excluded, "w") as f:
            f.write("path\treason\tbytes\n")
            for rel, why, n in excluded:
                f.write("%s\t%s\t%d\n" % (rel, why, n))
    sys.stderr.write("stage_run: copied %s -> %s; left out %d items (%.1f MB)\n"
                     % (a.src, a.dest, len(excluded), sum(n for _, _, n in excluded) / 1e6))
    return 0


if __name__ == "__main__":
    sys.exit(main())
