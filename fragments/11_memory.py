
# ---------------------------------------------------------------- notes (data, not code) and the residual bank
NOTES_DIR = os.environ.get("RA3SR_NOTES_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "memories")
NOTES = {}
NOTE_NAMES = ("planner_notes", "critic_notes", "residual_bank", "libraries")


def load_notes():
    """The code carries the mechanics; the lessons live in NOTES_DIR/*.md. Each file present is loaded, copied to
    /workspace/_pece/notes/ for the record and hashed into the ledger; libraries.md is published as /workspace/LIBRARIES.md.
    A file that is absent or empty is left out (the ablation switch), with a warning and a notes_missing ledger event."""
    NOTES.clear()
    rec = {}
    for name in NOTE_NAMES:
        txt = (read(os.path.join(NOTES_DIR, name + ".md"), "") or "").strip()
        NOTES[name] = txt
        rec[name] = ({"bytes": len(txt), "sha": hashlib.sha256(txt.encode("utf-8")).hexdigest()[:8]} if txt else None)
        if txt:
            try:
                os.makedirs(os.path.join(PD, "notes"), exist_ok=True)
                with open(os.path.join(PD, "notes", name + ".md"), "w") as f:
                    f.write(txt + "\n")
            except Exception as e:
                log("notes copy failed (%s): %s" % (name, e))
    if NOTES.get("libraries"):
        try:
            with open(os.path.join(WS, "LIBRARIES.md"), "w") as f:
                f.write(NOTES["libraries"] + "\n")
        except Exception as e:
            log("LIBRARIES.md write failed: %s" % e)
    ledger({"kind": "notes_loaded", "dir": NOTES_DIR, "notes": rec})
    log("notes: %s" % ", ".join("%s %d B" % (k, v["bytes"]) if v else "%s -" % k for k, v in rec.items()))
    missing = [name for name in NOTE_NAMES if not NOTES.get(name)]
    if missing:   # the run goes on without these memories, on the record
        ledger({"kind": "notes_missing", "dir": NOTES_DIR, "missing": missing})
        log("WARNING: memory file(s) absent or empty in %s: %s - the run goes on without them"
            % (NOTES_DIR, ", ".join(name + ".md" for name in missing)))

def notes_block(name, title, index=False):
    """The prompt tail carrying a note ('' when absent); index=True appends the RESIDUAL INDEX (full audits and checkpoints only)."""
    txt = NOTES.get(name) or ""
    out = ("\n\n%s\n%s" % (title, txt)) if txt else ""
    if index:
        out += bank_index_block()
    return out

BANK_K = 3                 # keyword matches per launch
BANK_MAX_CHARS = 4000      # cap of one residual block

_BANK_CACHE = {}

def bank_entries():
    """The residual bank as entries {id, title, stage, event, keys, text}; the older untagged format parses too (keys = title words)."""
    txt = NOTES.get("residual_bank") or ""
    if _BANK_CACHE.get("src") is txt:
        return _BANK_CACHE["entries"]
    clean = re.sub(r"<!--.*?-->", "", txt, flags=re.S)
    out = []
    for m in re.finditer(r"^R(\d+) ([^\n]+)\n(.*?)(?=^R\d+ |\Z)", clean, re.S | re.M):
        rid, title, body = int(m.group(1)), m.group(2).strip(), m.group(3).strip()
        stage, event, keys = set(), set(), []
        t = re.search(r"^TAGS:\s*(.+)$", body, re.M)
        if t:
            for part in t.group(1).split(";"):
                k, _, v = part.partition("=")
                vals = [x.strip().lower() for x in v.split(",") if x.strip()]
                k = k.strip().lower()
                if k == "stage":
                    stage = set(vals)
                elif k == "event":
                    event = set(x for x in vals if x != "none")
                elif k == "keys":
                    keys = vals
        else:
            keys = sorted(set(re.findall(r"[a-z]{5,}", title.lower())))
        body_txt = re.sub(r"^TAGS:.*\n?", "", body, flags=re.M).strip()
        out.append({"id": rid, "title": title, "stage": stage, "event": event, "keys": keys,
                    "text": ("R%d %s\n%s" % (rid, title, body_txt)).strip()})
    _BANK_CACHE.update(src=txt, entries=out)
    return out


def _bank_norm(text):
    return " " + re.sub(r"[^a-z0-9]+", " ", (text or "").lower()) + " "


def bank_pick(text="", stage=None, events=(), ids=(), always=False, k=None, stage_only=None, ids_cap=4, ev_cap=3):
    """Cited IDs first, then entries whose event tag matches, then (launches) the 'always' entries and the best keyword matches."""
    k = BANK_K if k is None else k
    ents = bank_entries()
    by_id = {e["id"]: e for e in ents}
    picked, seen = [], set()
    def add(e):
        if e is not None and e["id"] not in seen:
            picked.append(e)
            seen.add(e["id"])
    for i in list(ids or ())[:ids_cap]:
        add(by_id.get(int(i)))
    if always and not stage_only:   # the 'always' entries right after the cited ones, before events and keywords
        for e in ents:
            if "always" in e["keys"]:
                add(e)
    ev = set(events or ())
    if ev:
        for e in [e for e in ents if e["event"] & ev and e["id"] not in seen][:ev_cap]:
            add(e)
    if stage_only:
        for e in [e for e in ents if stage_only in e["stage"]][:k]:
            add(e)
        return picked
    if text and k > 0:
        norm = _bank_norm(text)
        scored = []
        for e in ents:
            if e["id"] in seen:
                continue
            hits = sum(1 for kw in e["keys"] if kw != "always" and _bank_norm(kw).strip() and _bank_norm(kw) in norm)
            if hits:
                scored.append((hits + (0.5 if (stage and (stage in e["stage"] or "any" in e["stage"])) else 0.0), -e["id"], e))
        for _, _, e in sorted(scored, key=lambda x: (x[0], x[1]), reverse=True)[:k]:
            add(e)
    return picked

def bank_render(entries, title, where, uid=None, extra=None):
    """The residual block (full texts, cited first, capped at BANK_MAX_CHARS) + ledger 'bank_selected' with the IDs actually rendered."""
    out, n, shown = [], 0, []
    for e in entries or []:
        if out and n + len(e["text"]) > BANK_MAX_CHARS:
            continue
        out.append(e["text"])
        shown.append(e["id"])
        n += len(e["text"])
    ev = {"kind": "bank_selected", "unit": uid, "where": where, "ids": shown, "dropped": [e["id"] for e in (entries or []) if e["id"] not in shown]}
    ev.update(extra or {})
    ledger(ev)
    return ("\n\n%s\n%s" % (title, "\n\n".join(out))) if out else ""

def _fix_plan_of(txt):
    """The last FIX_PLAN: {...} JSON of a report text -> dict or None."""
    best = None
    for m in re.finditer(r"FIX_PLAN\s*:\s*(\{.*?\})\s*$", txt or "", re.M):
        try:
            d = json.loads(m.group(1))
            if isinstance(d, dict):
                best = d
        except Exception:
            continue
    return best

def bank_cited_ids(path):
    """The residual-bank IDs a report names in its FIX_PLAN "residual_ids", existing entries only, at most 4. Nothing else in the report is
    read - lattice labels, probe rows and 'checked clear' mentions look like IDs too."""
    txt = read(path, "") or ""
    fp = _fix_plan_of(txt)
    raw = [str(x) for x in fp["residual_ids"]] if (fp and isinstance(fp.get("residual_ids"), list)) else []
    have = {e["id"] for e in bank_entries()}
    ids = []
    for x in re.findall(r"\bR(\d{1,3})\b", " ".join(raw)):
        i = int(x)
        if i in have and i not in ids:
            ids.append(i)
    return ids[:4]

def bank_index_block():
    ents = bank_entries()
    if not ents:
        return ""
    return "\n\n%s\n%s" % (BANK_INDEX_TITLE, "\n".join("R%d %s" % (e["id"], e["title"][:90]) for e in ents))

def bank_start_block(u, restart=None):
    """Residuals for a launch: the 'always' entries + keyword matches; a restart: the IDs the critic cited first, then the restart entries."""
    uid = u["id"]
    try:
        text = json.dumps(u, ensure_ascii=False) + " " + read(os.path.join(unit_dir(uid), "spec.md"), "")[:20000]
    except Exception:
        text = str(u)
    cited = []
    if restart and restart.get("archive"):
        cited = bank_cited_ids(os.path.join(_ws_path(restart["archive"]), "critic_report.md"))
    ents = (bank_pick(text=text, stage="validate", ids=cited, always=True, events=("restart",), k=2, ids_cap=3, ev_cap=1) if restart
            else bank_pick(text=text, stage="validate", always=True))
    return bank_render(ents, BANK_START_TITLE, "restart" if restart else "start", uid, {"cited": cited})

def bank_round_block(uid, events=(), cite=True):
    """Residuals for a fix / continuation / resume round: the critic's RESIDUAL_IDS + the entries tagged with what just happened."""
    cited = bank_cited_ids(os.path.join(unit_dir(uid), "critic_report.md")) if cite else []
    ents = bank_pick(events=set(events or ()), ids=cited, k=0)
    return bank_render(ents, BANK_ROUND_TITLE, "round", uid, {"events": sorted(events or ()), "cited": cited})

def bank_report_block():
    ents = bank_pick(stage_only="report", k=BANK_K)
    return bank_render(ents, BANK_REPORT_TITLE, "report", "report")

def bank_scout_block(uid, archive):
    """The entries the failed round's critic cited + the 'always' entries, for the scouts."""
    cited = bank_cited_ids(os.path.join(_ws_path(archive), "critic_report.md")) if archive else []
    ents = bank_pick(ids=cited, always=True, k=0, ids_cap=3)
    return bank_render(ents, BANK_SCOUT_TITLE, "scout", uid, {"cited": cited})

def mech_events(mech):
    """Bank events of a mechanical failure list."""
    ev = set()
    fails = " ".join(str(x) for x in ((mech or {}).get("fails") or [])).lower()
    if "missing/unparseable deliverable" in fails or "does not exist" in fails:
        ev.add("deliverable_missing")
    if "handoff.json" in fails:
        ev.add("handoff_invalid")
    if "\"waiting\"" in fails or "waiting must" in fails or "waiting\" is a list" in fails:
        ev.add("reg_invalid")
    if (mech or {}).get("outside"):
        ev.add("writes_outside")
    if fails and not ev:
        ev.add("mech")
    return ev
