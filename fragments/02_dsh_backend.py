
# ---------------------------------------------------------------- dsh (DeepSeek Harness) backend
DSH_HOME_RA3 = "/workspace/.dsh_ra3"        # root: gw_patch.yml + zstd_cat.js, and calls/<NN_tag>/ = one DSH_HOME per call
DSH_PATCH = DSH_HOME_RA3 + "/gw_patch.yml"
def _critic_patch_path(): return re.sub(r"\.yml$", "_critic.yml", DSH_PATCH)   # the critic's patch: one tool call cut at CRITIC_TOOL_S
DSH_ZSTD_JS = DSH_HOME_RA3 + "/zstd_cat.js"
DSH_NODE = os.environ.get("RA_DSH_NODE", "")   # the Node binary for dsh (RA_DSH_NODE); empty = node on PATH
DSH_KILL_GRACE_S = 12     # SIGTERM -> dsh stops its own tool trees -> then SIGKILL + marker sweep
DSH_CTX_CHARS = 3500      # transcript-tail budget of a continuation header
DSH_SESSIONS = {}         # sid -> {home, tag, uid, t0, t1, rc, ended}
DSH_LOCK = threading.RLock()
_dsh_ready = {"ok": False}

def _shim_on():
    """True when dsh talks to the local proxy (RA_DSH_SHIM=1, the route of the paper runs); otherwise dsh uses
    DEEPSEEK_BASE_URL and DEEPSEEK_API_KEY directly."""
    return os.environ.get("RA_DSH_SHIM", "").strip() == "1"


def _dsh_model():
    """The model id dsh sends: the bare name through the local proxy (the proxy sets the upstream id), MODEL as given otherwise."""
    return MODEL.rsplit("/", 1)[-1] if (_shim_on() and "/" in MODEL) else MODEL

# Node's zlib decodes ONE zstd frame per call: frames are split per RFC 8878 and decoded one by one.
ZSTD_CAT_JS = r'''"use strict";
const zlib = require("zlib"), fs = require("fs");
const buf = fs.readFileSync(process.argv[2]);
const MAGIC = 0xFD2FB528;
let i = 0, out = [];
while (i + 4 <= buf.length) {
  const magic = buf.readUInt32LE(i);
  if ((magic & 0xFFFFFFF0) === 0x184D2A50) { const sz = buf.readUInt32LE(i + 4); i += 8 + sz; continue; }
  if (magic !== MAGIC) break;
  const start = i; i += 4;
  const fhd = buf[i]; i += 1;
  const fcsFlag = fhd >> 6, single = (fhd >> 5) & 1, checksum = (fhd >> 2) & 1, didFlag = fhd & 3;
  if (!single) i += 1;
  i += [0, 1, 2, 4][didFlag];
  i += fcsFlag === 0 ? (single ? 1 : 0) : [1, 2, 4, 8][fcsFlag];
  while (true) {
    const bh = buf[i] | (buf[i + 1] << 8) | (buf[i + 2] << 16); i += 3;
    const last = bh & 1, btype = (bh >> 1) & 3, bsize = bh >> 3;
    if (btype === 1) i += 1; else i += bsize;
    if (last) break;
  }
  if (checksum) i += 4;
  try { out.push(zlib.zstdDecompressSync(buf.subarray(start, i))); }
  catch (e) { process.stderr.write("frame at " + start + ": " + e.message + "\n"); }
}
process.stdout.write(Buffer.concat(out));
'''

DSH_PROTOCOL_FROM = (0, 1, 6)   # `protocol: chat-completions` is written into the llm-deepseek block from this dsh version on (0.1.6 and later)


def _dsh_version():
    """Version string of the dsh on PATH, read from the @deepseek-ai/dsh package.json above its resolved binary; None when it cannot be told."""
    try:
        exe = shutil.which("dsh")
        if not exe:
            return None
        d = os.path.dirname(os.path.realpath(exe))
        for _ in range(6):
            pj = os.path.join(d, "package.json")
            if os.path.isfile(pj):
                j = json.loads(read(pj) or "{}")
                if isinstance(j, dict) and j.get("name") == "@deepseek-ai/dsh":
                    return str(j.get("version") or "") or None
            nd = os.path.dirname(d)
            if nd == d:
                break
            d = nd
    except Exception:
        pass
    return None


def _dsh_version_tuple(v):
    m = re.match(r"(\d+)\.(\d+)\.(\d+)", v or "")
    return tuple(int(x) for x in m.groups()) if m else None

DSH_CONTEXT_WINDOW = int(os.environ.get("RA3SR_DS_CONTEXT", "1000000"))   # contextWindow of the catalog entry (dsh DEFAULT_CONTEXT_WINDOW = 1e6)

DSH_CATALOG_NAME = "DeepSeek-V41-Flash"                                   # the model the vision entry stands for (dsh 0.1.5 catalog id deepseek-flash)


def _dsh_models_yaml():
    """The `models:` catalog for the llm-deepseek block when vision is on - one entry for the id dsh sends to the endpoint (an
    OpenAI-compatible endpoint may expose V4.1 Flash under its own id, which is out of dsh's catalog and therefore text-only). Empty
    when vision is off: no `models:` line, the schema default (DEFAULT_MODELS) stays. imagePixelBudget is not set (dsh default 64e4)."""
    if not VISION_OK:
        return ""
    return ("    models:\n"
            "      - id: %s\n        name: %s\n        contextWindow: %d\n        inputModalities: [text, image]\n        systemPromptUpdate: in-history\n"
            % (_dsh_model(), DSH_CATALOG_NAME, DSH_CONTEXT_WINDOW))


def _dsh_setup():
    with DSH_LOCK:
        if _dsh_ready["ok"]: return
        os.makedirs(os.path.join(DSH_HOME_RA3, "calls"), exist_ok=True)
        ver = _dsh_version()
        vt = _dsh_version_tuple(ver)
        proto = "    protocol: chat-completions\n" if (vt and vt >= DSH_PROTOCOL_FROM) else ""
        models = _dsh_models_yaml()   # vision on -> the catalog entry that lets dsh send images for this model id
        patch_txt = (
            "- id: agent-default-model\n  config:\n    provider: deepseek-official\n    model: %s\n"
            "- id: llm-deepseek\n  config:\n%s    reasoningEffort: max\n    streamIdleTimeoutMs: 1800000\n%s"
            "- id: bash-sandbox\n  config:\n    timeoutMs: 43200000\n    maxTimeoutMs: 43200000\n"
            "- id: session-log-deepseek\n  config:\n    enabled: false\n"                # dsh 0.1.5+ else uploads the session log as a request field (dsh_session_log)
            "- id: plugin-package-inventory-deepseek\n  config:\n    enabled: false\n"   # ... and its plugin inventory (dsh_plugin_packages); neither belongs in a model request
            % (_dsh_model(), proto, models))
        open(DSH_PATCH, "w").write(patch_txt)
        open(_critic_patch_path(), "w").write(patch_txt.replace("timeoutMs: 43200000\n    maxTimeoutMs: 43200000",   # critic probes are formal checks
                                                            "timeoutMs: %d\n    maxTimeoutMs: %d" % (CRITIC_TOOL_S * 1000, CRITIC_TOOL_S * 1000)))
        open(DSH_ZSTD_JS, "w").write(ZSTD_CAT_JS)
        _dsh_ready["ok"], _dsh_ready["version"] = True, ver
        log("dsh setup: version %s; patch %s (session-log / package-inventory upload off%s%s)" % (ver or "unknown", DSH_PATCH, ", protocol chat-completions" if proto else "",
                                                                                              (", vision catalog entry %s [text, image]" % _dsh_model()) if models else ", text only"))


def _dsh_call_home(callno, tag):
    """One DSH_HOME per call (dsh rewrites its profile at every boot: a shared home races under concurrency)."""
    home = os.path.join(DSH_HOME_RA3, "calls", "%02d_%s" % (callno, tag))
    os.makedirs(home, exist_ok=True)
    return home


def _dsh_env(home=None):
    e = dict(os.environ)
    if DSH_NODE:   # RA_DSH_NODE set: its folder goes first on the agents' PATH
        e["PATH"] = os.path.dirname(DSH_NODE) + ":" + e.get("PATH", "")
    e["DSH_HOME"] = home or DSH_HOME_RA3
    if _shim_on():   # the local proxy holds the key; dsh gets a dummy one
        e["DEEPSEEK_API_KEY"] = "shim-local"
        e["DEEPSEEK_BASE_URL"] = "http://127.0.0.1:%s/v1" % e.get("DSH_SHIM_PORT", "8500")
    else:   # direct mode: the caller's endpoint and key, passed on unchanged (agents can read the key)
        e["DEEPSEEK_BASE_URL"] = e.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"
        e["DEEPSEEK_API_KEY"] = e.get("DEEPSEEK_API_KEY", "")
    e["DSH_PERMISSION_MODE"] = "danger-full-access"
    e["DSH_TELEMETRY_DISABLED"] = "1"   # dsh 0.1.5+ telemetry default FEEDBACK_ONLY -> off
    return e


# ---- the endpoint probe and the prompt-file launcher
def _gw_probe():
    """(url, bearer key, model) of the path the backend really talks through."""
    e = _dsh_env()
    return e["DEEPSEEK_BASE_URL"].rstrip("/") + "/chat/completions", e["DEEPSEEK_API_KEY"], _dsh_model()

def gw_ok():
    try:
        import urllib.request
        url, key, model = _gw_probe()
        req = urllib.request.Request(
            url,
            data=json.dumps({"model": model, "max_tokens": 1,
                             "messages": [{"role": "user", "content": "ping"}]}).encode(),
            headers={"Authorization": "Bearer " + key,
                     "Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=30).status == 200
    except Exception:
        return False

DSH_ARGV = {"off": os.environ.get("RA3SR_DSH_ARGV_FILE", "1") == "0"}   # prompts reach dsh through a file (off: on the command line)

DSH_ARGV_JS = r"""// Written by the RA orchestrator. dsh takes its task only from argv, and argv is what `pkill -f` matches
// (/proc/<pid>/cmdline). This launcher reads the prompt from a file and hands it to dsh inside the process: the task text dsh
// receives is unchanged, but no command line carries it. Usage: node dsh_argv.mjs <dsh bin.js> <prompt file> <dsh options...>
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
const [bin, promptFile, ...opts] = process.argv.slice(2);
let mod;
try {
  const prompt = readFileSync(promptFile, "utf8");
  process.argv = [process.argv[0], bin, ...opts, prompt];
  mod = await import(pathToFileURL(bin).href);
  if (typeof mod.runCli !== "function") throw new Error("no runCli export in " + bin);
} catch (e) {
  console.error("DSH_ARGV_WRAPPER_FAILED: " + (e && e.message ? e.message : String(e)));
  process.exit(97);
}
await mod.runCli();
"""


def _dsh_argv_cmd(cmd, home, prompt):
    """dsh takes its task only from argv, and argv is what `pkill -f` matches - an agent's `pkill -f name.py` would kill every
    session whose prompt names name.py. When the dsh on PATH is the real Node CLI, the prompt goes to <call home>/prompt.txt
    and dsh_argv.mjs hands it to dsh inside the process (the text dsh receives is unchanged). Otherwise (a test fake, no
    node) the command comes back unchanged."""
    if DSH_ARGV["off"]:
        return cmd
    try:
        exe = shutil.which("dsh")
        node = DSH_NODE if os.path.isfile(DSH_NODE) else shutil.which("node")   # RA_DSH_NODE when it is a file, else node on PATH (dsh 0.1.5 needs Node >= 23.8; Node 20 cannot load it)
        real = os.path.realpath(exe) if exe else ""
        if not (node and real.endswith(".js") and os.path.isfile(real)):
            return cmd
        js = os.path.join(PD, "dsh_argv.mjs")
        if read(js) != DSH_ARGV_JS:
            with open(js + ".tmp", "w") as f:
                f.write(DSH_ARGV_JS)
            os.replace(js + ".tmp", js)
        pf = os.path.join(home if home and os.path.isdir(home) else PD, "prompt.txt")
        with open(pf, "w") as f:
            f.write(prompt)
        return [node, js, real, pf] + list(cmd[1:-1])
    except Exception as e:
        log("dsh prompt-file launcher not used: %s" % e)
        return cmd


def _argv_safe(text):
    """An argv string must not contain NUL (Popen raises "embedded null byte" and the call dies before its first byte, which
    the dead-call check then reports as an infra failure). A transcript tail or a file excerpt can carry NUL; it is shown
    to the model as U+FFFD instead."""
    return text.replace("\x00", "\ufffd") if isinstance(text, str) and "\x00" in text else text
