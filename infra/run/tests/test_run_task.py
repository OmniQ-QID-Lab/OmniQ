"""Offline tests of infra/run/run_task.sh: staging, refusals, the container command, and a full launch with a fake
`docker` that plays the container (it calls the real proxy through DSH_SHIM_PORT, as dsh would) against a mock
upstream. Run: python3 -m unittest discover -s infra/run/tests -v
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
INFRA = os.path.dirname(os.path.dirname(HERE))
RUN_TASK = os.path.join(INFRA, "run", "run_task.sh")
sys.path.insert(0, os.path.join(INFRA, "proxy", "tests"))
from mock_upstream import MockUpstream  # noqa: E402

KEY = "runkey-31"
FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys, urllib.request
args = sys.argv[1:]
if args[:2] == ["image", "inspect"]:
    print(json.dumps([{"Id": "sha256:fake"}])); sys.exit(0)
if args[:1] == ["kill"]:
    sys.exit(1)
assert args[0] == "run", args
vols, envf, i = {}, None, 1
two = {"--name", "--network", "--cpus", "--memory", "--shm-size", "--user", "--security-opt", "-e", "-w",
       "--cpuset-cpus", "--gpus"}
while i < len(args):
    a = args[i]
    if a == "-v":
        src, dst = args[i + 1].split(":", 1); vols[dst] = src; i += 2; continue
    if a == "--env-file":
        envf = args[i + 1]; i += 2; continue
    if a in two:
        i += 2; continue
    if a in ("--rm", "--init"):
        i += 1; continue
    break
image, cmd = args[i], args[i + 1:]
with open(envf) as f:
    env = dict(l.rstrip("\n").split("=", 1) for l in f if "=" in l)
answer = None
if env.get("RA_DSH_SHIM") == "1":
    body = {"model": "deepseek-v4.1-flash", "stream": False, "messages": [{"role": "user", "content": "hi"}]}
    req = urllib.request.Request("http://127.0.0.1:%s/v1/chat/completions" % env["DSH_SHIM_PORT"],
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer shim-local"})
    answer = json.loads(urllib.request.urlopen(req, timeout=20).read())["choices"][0]["message"]["content"]
with open(os.environ["FAKE_DOCKER_LOG"], "w") as f:
    json.dump({"image": image, "cmd": cmd, "env": env, "vols": vols, "argv": args, "answer": answer}, f)
with open(os.path.join(vols["/workspace"], "report.md"), "w") as f:
    f.write("# report\n")
print("fake container done")
sys.exit(int(os.environ.get("FAKE_RC", "0")))
'''


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def write(path, text="x", mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    if mode:
        os.chmod(path, mode)


class RunTaskBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ra_run_task_test_")
        t = self.tmp
        self.task = os.path.join(t, "task")
        write(os.path.join(self.task, "CLAUDE.md"), "# Task\n")
        write(os.path.join(self.task, "skills", "INDEX.md"), "skills")
        self.paper = os.path.join(t, "paper.pdf")
        write(self.paper, "%PDF paper")
        self.sm = os.path.join(t, "sm.pdf")
        write(self.sm, "%PDF sm")
        self.key = os.path.join(t, "key")
        write(self.key, KEY + "\n", 0o600)
        self.out = os.path.join(t, "out")
        self.bin = os.path.join(t, "bin")
        write(os.path.join(self.bin, "docker"), FAKE_DOCKER, 0o755)
        self.log = os.path.join(t, "fake_docker.json")
        self.mock = MockUpstream().start()

    def tearDown(self):
        self.mock.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_task(self, *extra, env_extra=None, upstream=True, key=True, paper=True):
        env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ.get("PATH", ""), FAKE_DOCKER_LOG=self.log)
        env.update(env_extra or {})
        args = ["bash", RUN_TASK, "--task", self.task]
        if paper:
            args += ["--paper", self.paper, "--sm", self.sm]
        args += ["--paper-num", "5", "--label", "demo", "--out", self.out, "--ncpus", "8", "--mem-gb", "32"]
        if key:
            args += ["--key-file", self.key]
        if upstream:
            args += ["--upstream", upstream if isinstance(upstream, str) else "http://127.0.0.1:%d/v1" % self.mock.port]
        return subprocess.run(args + list(extra), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)

    @property
    def rundir(self):
        return os.path.join(self.out, "paper_5", "demo_p5")

    @property
    def aux(self):
        return os.path.join(self.out, "paper_5", "demo_p5.aux")


class TestStagingAndCommands(RunTaskBase):
    def test_dry_run_stages_and_prints(self):
        r = self.run_task("--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        for f in ("CLAUDE.md", "skills/INDEX.md", "paper_5.pdf", "paper_5_SM.pdf", "ENV.md"):
            self.assertTrue(os.path.exists(os.path.join(self.rundir, f)), f)
        with open(os.path.join(self.rundir, "ENV.md")) as f:
            env_md = f.read()
        self.assertIn("- **This job's actual allocation: 8 CPU cores.**", env_md)
        self.assertIn("Hard memory cgroup limit: 32 GB total for ALL processes", env_md)
        self.assertNotIn("CPU and RAM are allocated by the job scheduler", env_md)
        out = r.stdout.decode()
        self.assertIn("timeout -s TERM -k 300 86400 docker run --rm --init", out)
        for part in ("--network host", "--cpus 8 --memory 32g --shm-size 16g", self.rundir + ":/workspace",
                     os.path.join(self.aux, "store") + ":/pece_store", os.path.join(self.aux, "home") + ":/home/ra",
                     "residual-agents:1.0.0 /opt/ra/infra/run/run_in_container.sh --profile paper",
                     "--inject-ids 1", "--model-force deepseek/deepseek-v4.1-flash", "WALLTIME_SEC=85800"):
            self.assertIn(part, out)
        self.assertNotIn(os.path.expanduser("~") + ":", out, "the host home is never mounted")
        with open(os.path.join(self.aux, "run_meta.json")) as f:
            meta = json.load(f)
        self.assertEqual((meta["status"], meta["walltime_sec"], meta["allocation"]["ncpus"], meta["mode"]),
                         ("dry_run", 85800, 8, "proxy"))
        self.assertEqual(meta["proxy"]["upstream"], "http://127.0.0.1:%d/v1" % self.mock.port)
        self.assertFalse(os.path.exists(os.path.join(self.rundir, "run_meta.json")), "not in the run folder before the run")

    def test_task_without_paper(self):
        """A task without a source paper (the toy task) runs without --paper; nothing is staged as paper_N.pdf."""
        r = self.run_task("--dry-run", "--profile", "smoke", paper=False)
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertIn(b"no --paper given", r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.rundir, "CLAUDE.md")))
        self.assertFalse(os.path.exists(os.path.join(self.rundir, "paper_5.pdf")))
        with open(os.path.join(self.aux, "run_meta.json")) as f:
            self.assertIsNone(json.load(f)["inputs"]["paper_sha256"])
        r = self.run_task("--dry-run", "--sm", self.sm, paper=False)
        self.assertEqual(r.returncode, 64)
        self.assertIn(b"--sm needs --paper", r.stderr)

    def test_smoke_profile_and_apptainer(self):
        r = self.run_task("--dry-run", "--profile", "smoke", "--engine", "apptainer", "--image", "/img/ra.sif",
                          "--gpu-index", "1", "--gpu-facts", "NVIDIA A100-SXM4-40GB, 40960 MiB", "--cpuset", "0-7")
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        out = r.stdout.decode()
        self.assertIn("timeout -s TERM -k 300 21600 taskset -c 0-7 apptainer exec --containall --cleanenv --pid", out)
        self.assertIn("--nv /img/ra.sif /opt/ra/infra/run/run_in_container.sh --profile smoke", out)
        self.assertIn("WALLTIME_SEC=21000", out, "the smoke profile's wall: 6 h - 600 s")
        self.assertIn("RA3SR_GPU_FACTS", out)

    def test_refusals(self):
        write(os.path.join(self.task, "skills", "sub", "judge.md"), "criteria")
        r = self.run_task("--dry-run")
        self.assertEqual(r.returncode, 64)
        self.assertIn(b"criteria must never be visible", r.stderr)
        os.remove(os.path.join(self.task, "skills", "sub", "judge.md"))
        for bad in (["--label", "a-b"], ["--paper-num", "x"], ["--env", "OTHER=1"], ["--env", "PECE_NCPUS=4"],
                    ["--profile", "fast"], ["--engine", "podman"], ["--wall-hours", "0.1"], ["--inject-ids", "2"]):
            r = self.run_task("--dry-run", *bad)
            self.assertEqual(r.returncode, 64, bad)
        self.assertEqual(self.run_task("--dry-run", upstream=False).returncode, 64, "proxy mode needs an upstream")
        for bad_url in ("http://user:pw@127.0.0.1:9/v1", "https://example.invalid/v1?key=1", "ftp://x"):
            self.assertEqual(self.run_task("--dry-run", upstream=bad_url).returncode, 64, bad_url)
        self.assertEqual(self.run_task(key=False).returncode, 64, "a real run needs the key file")
        self.assertEqual(self.run_task("--dry-run").returncode, 0)
        write(os.path.join(self.rundir, "_pece", "ledger.jsonl"), "{}")
        r = self.run_task("--dry-run")
        self.assertEqual(r.returncode, 64, "an episode never runs twice in the same folder")


class TestLaunch(RunTaskBase):
    def test_proxy_mode_end_to_end(self):
        r = self.run_task("--env", "RA3_CP_S=1800", "--wall-hours", "1")
        self.assertEqual(r.returncode, 0, r.stderr.decode() + r.stdout.decode())
        with open(self.log) as f:
            rec = json.load(f)
        env = rec["env"]
        self.assertEqual(sorted(env), sorted(["PAPER_NUM", "RUN_ID", "WALLTIME_SEC", "PECE_NCPUS", "PECE_MEM_GB",
                                              "HOST_WORK_DIR", "LANG", "RA3_CP_S", "RA_DSH_SHIM", "DSH_SHIM_PORT"]))
        self.assertEqual((env["RUN_ID"], env["WALLTIME_SEC"], env["RA_DSH_SHIM"], env["RA3_CP_S"]),
                         ("demo_p5", "3000", "1", "1800"))
        self.assertEqual(rec["answer"], "ok", "dsh's request went through the proxy to the endpoint")
        up = self.mock.requests[-1]
        self.assertEqual(up["headers"]["Authorization"], "Bearer " + KEY)
        self.assertEqual(json.loads(up["body"])["model"], "deepseek/deepseek-v4.1-flash")
        self.assertTrue(json.loads(up["body"])["messages"][0]["id"].startswith("msg_"), "ids injected (paper setting)")
        self.assertEqual(rec["vols"], {"/workspace": self.rundir, "/pece_store": os.path.join(self.aux, "store"),
                                       "/home/ra": os.path.join(self.aux, "home")})
        with open(os.path.join(self.rundir, "run_meta.json")) as f:
            meta = json.load(f)
        self.assertEqual((meta["status"], meta["exit_code"], meta["image_id"]), ("finished", 0, "sha256:fake"))
        self.assertFalse(os.path.exists(os.path.join(self.aux, "container.env")), "the env file is removed")
        with open(os.path.join(self.aux, "proxy", "pid")) as f:
            pid = int(f.read())
        time.sleep(0.3)
        with self.assertRaises(OSError, msg="the proxy is stopped after the run"):
            os.kill(pid, 0)
        for root, dirs, files in os.walk(self.out):
            for name in files:
                with open(os.path.join(root, name), "rb") as f:
                    self.assertNotIn(KEY.encode(), f.read(), name)
        self.assertNotIn(KEY.encode(), r.stdout + r.stderr)
        with open(os.path.join(self.aux, "console.log")) as f:
            self.assertIn("fake container done", f.read())
        with open(os.path.join(self.aux, "proxy", "usage.jsonl")) as f:
            self.assertEqual(json.loads(f.readline())["status"], 200)

    def test_exit_status_passed_on(self):
        r = self.run_task(env_extra={"FAKE_RC": "3"})
        self.assertEqual(r.returncode, 3)
        with open(os.path.join(self.rundir, "run_meta.json")) as f:
            self.assertEqual(json.load(f)["status"], "infra_failure")

    def test_direct_mode(self):
        r = self.run_task("--direct", "--base-url", "https://api.example.invalid", upstream=False)
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertIn(b"direct mode", r.stderr)
        with open(self.log) as f:
            env = json.load(f)["env"]
        self.assertEqual((env["RA_DSH_SHIM"], env["DEEPSEEK_BASE_URL"], env["DEEPSEEK_API_KEY"]),
                         ("0", "https://api.example.invalid", KEY))
        self.assertNotIn("DSH_SHIM_PORT", env)
        self.assertFalse(os.path.exists(os.path.join(self.aux, "container.env")))
        with open(os.path.join(self.rundir, "run_meta.json")) as f:
            self.assertNotIn(KEY, f.read())

    def test_proxy_failure_is_infra(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        p = s.getsockname()[1]
        try:
            r = self.run_task(env_extra={"RA_PROXY_PORT_RANGE": "%d-%d" % (p, p)})
        finally:
            s.close()
        self.assertEqual(r.returncode, 3)
        self.assertTrue(os.path.exists(os.path.join(self.rundir, ".infra_failure")))
        self.assertFalse(os.path.exists(self.log), "no container is started without a proxy")
        with open(os.path.join(self.rundir, "run_meta.json")) as f:
            self.assertEqual(json.load(f)["status"], "infra_failure")


if __name__ == "__main__":
    unittest.main()
