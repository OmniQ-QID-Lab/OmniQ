"""Offline tests of infra/hpc: ra_pack.sh (members in dry-run mode, a fake nvidia-smi) and the job templates
(syntax, generic headers, explicit variables). Run: python3 -m unittest discover -s infra/hpc/tests -v
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
HPC = os.path.dirname(HERE)
PACK = os.path.join(HPC, "ra_pack.sh")

FAKE_NVIDIA_SMI = """#!/bin/sh
case "$*" in
  -L) echo "GPU 0: NVIDIA A100-SXM4-40GB (UUID: GPU-0)"; echo "GPU 1: NVIDIA A100-SXM4-40GB (UUID: GPU-1)" ;;
  *"-i 0"*|*"-i 1"*) echo "NVIDIA A100-SXM4-40GB, 40960 MiB" ;;
esac
"""


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    if mode:
        os.chmod(path, mode)


@unittest.skipUnless(hasattr(os, "sched_getaffinity"), "Linux only")
class TestPack(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ra_pack_test_")
        t = self.tmp
        for p in ("5", "6", "7"):
            write(os.path.join(t, "task%s" % p, "CLAUDE.md"), "# Task %s\n" % p)
            write(os.path.join(t, "paper%s.pdf" % p), "%PDF")
        write(os.path.join(t, "key"), "k\n", 0o600)
        write(os.path.join(t, "bin", "nvidia-smi"), FAKE_NVIDIA_SMI, 0o755)
        self.members = os.path.join(t, "members.tsv")
        rows = ["# paper label task pdf sm ncpus mem gpu",
                "5\tpk\t%s/task5\t%s/paper5.pdf\t-\t2\t16\t0" % (t, t),
                "6\tpk\t%s/task6\t%s/paper6.pdf\t-\t3\t24\t1" % (t, t),
                "",
                "7\tpk\t%s/task7\t%s/paper7.pdf\t-\t1\t8\t1" % (t, t)]
        write(self.members, "\n".join(rows) + "\n")
        self.out = os.path.join(t, "out")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def pack(self, *common):
        env = dict(os.environ, PATH=os.path.join(self.tmp, "bin") + os.pathsep + os.environ.get("PATH", ""))
        args = ["bash", PACK, self.members, "--stagger", "0", "--", "--out", self.out, "--dry-run",
                "--upstream", "http://127.0.0.1:9/v1", "--key-file", os.path.join(self.tmp, "key")] + list(common)
        return subprocess.run(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)

    def test_blocks_and_gpus(self):
        r = self.pack()
        self.assertEqual(r.returncode, 0, r.stderr.decode() + r.stdout.decode())
        cpus = sorted(os.sched_getaffinity(0))
        if len(cpus) < 6:
            self.skipTest("needs 6 cores in the affinity mask")
        logs = {}
        for p in ("5", "6", "7"):
            with open(os.path.join(self.out, "pack_logs", "pk_p%s.log" % p)) as f:
                logs[p] = f.read()
        blocks = {"5": cpus[0:2], "6": cpus[2:5], "7": cpus[5:6]}
        for p, b in blocks.items():
            self.assertIn("--cpuset-cpus " + "\\,".join(map(str, b)), logs[p], p)   # printed with %q quoting
        self.assertNotIn("--gpus", logs["5"])
        self.assertIn("--gpus device=0", logs["6"])
        self.assertIn("--gpus device=1", logs["7"])
        self.assertIn("RA3SR_GPU_FACTS", logs["6"])
        self.assertIn("--cpus 3 --memory 24g", logs["6"])

    def test_refusals(self):
        r = self.pack("--cpuset", "0")
        self.assertEqual(r.returncode, 64)
        r = subprocess.run(["bash", PACK, self.members, "--", "--dry-run"], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(r.returncode, 64, "--out is required")
        with open(self.members, "a") as f:
            f.write("8\tpk\tx\ty\t-\t2\n")
        self.assertEqual(self.pack().returncode, 64, "a short member line is refused")

    def test_member_failure_counted(self):
        os.remove(os.path.join(self.tmp, "task7", "CLAUDE.md"))
        r = self.pack()
        self.assertEqual(r.returncode, 1, "one member failed (its task has no CLAUDE.md)")


class TestTemplates(unittest.TestCase):
    def test_syntax_and_generic_headers(self):
        for name in ("ra_episode.pbs", "ra_episode.slurm", "ra_pack.sh"):
            path = os.path.join(HPC, name)
            r = subprocess.run(["bash", "-n", path], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(r.returncode, 0, name + r.stderr.decode())
        with open(os.path.join(HPC, "ra_episode.pbs")) as f:
            pbs = f.read()
        self.assertIn("#PBS -q <queue>", pbs)
        self.assertIn("#PBS -A <account>", pbs)
        self.assertIn("#PBS -r n", pbs)
        self.assertNotRegex(pbs, r"(?m)^#PBS -V")
        self.assertNotRegex(pbs, r"(?m)^#PBS -P")
        with open(os.path.join(HPC, "ra_episode.slurm")) as f:
            slurm = f.read()
        self.assertIn("#SBATCH --partition=<partition>", slurm)
        self.assertIn("#SBATCH --no-requeue", slurm)

    def test_template_runs_run_task(self):
        """The PBS template, run as a plain script with a dry-run run_task.sh, stages the task and builds the
        apptainer command."""
        tmp = tempfile.mkdtemp(prefix="ra_pbs_test_")
        try:
            write(os.path.join(tmp, "task", "CLAUDE.md"), "# T\n")
            write(os.path.join(tmp, "paper.pdf"), "%PDF")
            write(os.path.join(tmp, "key"), "k\n", 0o600)
            repo = os.path.dirname(os.path.dirname(HPC))
            # the template execs $RA_REPO/infra/run/run_task.sh: point RA_REPO at a copy whose run_task.sh adds --dry-run
            fake_repo = os.path.join(tmp, "repo")
            shutil.copytree(os.path.join(repo, "infra"), os.path.join(fake_repo, "infra"))
            rt = os.path.join(fake_repo, "infra", "run", "run_task.sh")
            os.rename(rt, rt + ".real")
            write(rt, '#!/bin/bash\nexec bash "$(dirname "$0")/run_task.sh.real" "$@" --dry-run\n', 0o755)
            env = dict(os.environ, RA_REPO=fake_repo, RA_SIF="/img/ra.sif", RA_TASK=os.path.join(tmp, "task"),
                       RA_PAPER_PDF=os.path.join(tmp, "paper.pdf"), RA_PAPER_NUM="3", RA_LABEL="hpc",
                       RA_OUT=os.path.join(tmp, "out"), RA_UPSTREAM="http://127.0.0.1:9/v1",
                       RA_KEY_FILE=os.path.join(tmp, "key"), RA_MEM_GB="32", RA_WALL_HOURS="12", NCPUS="8", RA_GPU="1")
            for name in ("ra_episode.pbs", "ra_episode.slurm"):
                shutil.rmtree(os.path.join(tmp, "out"), ignore_errors=True)
                r = subprocess.run(["bash", os.path.join(HPC, name)], env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, timeout=120)
                self.assertEqual(r.returncode, 0, name + r.stderr.decode())
                out = r.stdout.decode()
                self.assertIn("apptainer exec --containall --cleanenv --pid", out, name)
                self.assertIn("--nv /img/ra.sif", out, name)
                self.assertIn("WALLTIME_SEC=42600", out, name)
                self.assertTrue(os.path.exists(os.path.join(tmp, "out", "paper_3", "hpc_p3", "CLAUDE.md")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
