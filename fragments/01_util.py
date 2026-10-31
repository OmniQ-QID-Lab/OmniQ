
# ---------------------------------------------------------------- basic utilities (defined first: the node facts below read files)
def read(p, default=""):
    try:
        return open(p, encoding="utf-8", errors="replace").read()
    except Exception:
        return default

def now(): return time.strftime("%H:%M:%S")
def log(msg): print("[%s +%dm] %s" % (now(), (time.time() - T0) // 60, msg), flush=True)

def ledger(event):
    event.update({"ts": int(time.time()), "paper": PAPER, "episode": RUN_ID})
    with LEDGER_LOCK:
        for p in (os.path.join(STORE, "ledger.jsonl"), os.path.join(PD, "ledger.jsonl")):
            try:
                with open(p, "a") as f:
                    f.write(json.dumps(event) + "\n")
            except Exception as e:
                print("ledger write failed:", p, e)

def _mtime(p):
    try:
        return os.stat(p).st_mtime
    except OSError:
        return None

def _atomic_json_dump(obj, path):
    """JSON via a temp file + rename (agents and threads read these files while they are rewritten)."""
    tmp = "%s.tmp.%d.%d" % (path, os.getpid(), threading.get_ident())
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)

def _affinity():
    try:
        return sorted(os.sched_getaffinity(0))
    except Exception:
        return list(range(os.cpu_count() or 1))

CGROUP_ROOT = "/sys/fs/cgroup"      # where the cgroup file system is mounted, and this process's cgroup in each hierarchy
CGROUP_SELF = "/proc/self/cgroup"   # (module constants: the tests point them at a fake tree)
NODE_SRC = {}                       # "ncpus" / "mem_gb" -> where the value came from (logged at the episode start)


def _cgroup_chain(controller=None):
    """The folders of this process's own cgroup, leaf first, up to the mount: cgroup v2 (the "0::" line of CGROUP_SELF) or, with
    `controller`, that controller's cgroup v1 hierarchy (mounted at CGROUP_ROOT/<controller>). [] when CGROUP_SELF cannot be read."""
    out = []
    for line in read(CGROUP_SELF).splitlines():
        parts = line.strip().split(":", 2)
        if len(parts) != 3:
            continue
        if controller is None:
            if parts[0] != "0" or parts[1]:
                continue
            base = CGROUP_ROOT
        elif controller in parts[1].split(","):
            base = os.path.join(CGROUP_ROOT, controller)
        else:
            continue
        rel = parts[2].strip().strip("/")
        while rel:
            out.append(os.path.join(base, rel))
            rel = os.path.dirname(rel)
        out.append(base)
    return out


def _ncpus():
    """Cores the plan is validated/scheduled against: PECE_NCPUS (the batch scheduler's allocation) > the affinity mask (it can show the
    whole node), capped by ceil(quota / period) of the tightest cgroup v2 CPU quota (cpu.max) in this process's own cgroup chain.
    NODE_SRC["ncpus"] names the source."""
    v = os.environ.get("PECE_NCPUS")
    if v:
        try:
            n = max(1, int(float(v)))
            NODE_SRC["ncpus"] = "PECE_NCPUS"
            return n
        except ValueError:
            pass
    n = len(_affinity())
    NODE_SRC["ncpus"] = "affinity mask"
    for d in _cgroup_chain() + [CGROUP_ROOT]:
        p = os.path.join(d, "cpu.max")
        try:
            quota, period = read(p).split()[:2]
            if quota == "max":
                continue
            c = max(1, -(-int(quota) // int(period)))
        except (ValueError, ZeroDivisionError):
            continue
        if c < n:
            n, NODE_SRC["ncpus"] = c, "affinity mask capped by the CPU quota in %s" % p
    return n


def _mem_gb():
    """PECE_MEM_GB (the batch scheduler's allocation) > the smallest memory limit in this process's own cgroup chain (v2 memory.max,
    v1 memory.limit_in_bytes, and the two root files) > physical RAM; 0 = unknown. NODE_SRC["mem_gb"] names the source."""
    v = os.environ.get("PECE_MEM_GB")
    if v:
        try:
            n = max(1, int(float(v)))
            NODE_SRC["mem_gb"] = "PECE_MEM_GB"
            return n
        except ValueError:
            pass
    paths = ([os.path.join(d, "memory.max") for d in _cgroup_chain()]
             + [os.path.join(d, "memory.limit_in_bytes") for d in _cgroup_chain("memory")]
             + [os.path.join(CGROUP_ROOT, "memory.max"), os.path.join(CGROUP_ROOT, "memory", "memory.limit_in_bytes")])
    limits = []
    for p in paths:
        s = read(p).strip()
        if s.isdigit() and int(s) < (1 << 50):   # "max" and the v1 "unlimited" value are no limit
            limits.append((int(s), p))
    if limits:
        b, p = min(limits)
        NODE_SRC["mem_gb"] = "cgroup limit in %s" % p
        return max(1, b // (1 << 30))
    try:
        n = max(1, int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // (1 << 30)))
        NODE_SRC["mem_gb"] = "physical RAM"
        return n
    except Exception:
        NODE_SRC["mem_gb"] = "unknown"
        return 0

NCPUS = _ncpus()
MEM_GB = _mem_gb()
