#!/usr/bin/env python3
"""Driver for the scislop skill: SciSlop's six slop measures on one or more LaTeX papers.

The four deterministic measures run the upstream code unchanged. The two model-based measures
(argument graph, figure exposition) are split into a prep step that writes a task file, a judgement
step that Claude does, and a score step that applies the upstream scoring rules to those judgements.

  scislop.py setup
  scislop.py sections      --tex NAME=path/main.tex [--role 'PATTERN=Role' ...]
  scislop.py deterministic --tex NAME=path/main.tex [...] --work DIR [--role ...]
  scislop.py intro         --tex NAME=path/main.tex --work DIR [--role ...]
  scislop.py argument-prep  --work DIR --name NAME
  scislop.py argument-score --work DIR --name NAME
  scislop.py figure        --work DIR --name NAME
  scislop.py report        --work DIR

Upstream: https://github.com/yerimoh/ScientificSlop (the repo carries no license, so it is
cloned at run time and never copied into this repository).
"""
from __future__ import annotations
import argparse, contextlib, io, json, os, random, re, subprocess, sys

UPSTREAM = "https://github.com/yerimoh/scientificslop"
REPO = os.environ.get("SCISLOP_REPO", os.path.expanduser("~/.cache/scislop/scientificslop"))
S = os.path.join(REPO, "scislop")

DETERMINISTIC = {
    "xsec_ref": "Structure/xsec_ref",
    "macro_redund": "Structure/macro_redund",
    "citation": "Argument/citation",
    "evidence_gap": "Artifacts/evidence_gap",
}
PLANES = {"Structure": ["xsec_ref", "macro_redund"],
          "Argument": ["argument_graph", "citation"],
          "Artifacts": ["fig_exposition", "evidence_gap"]}
# Means of the released SciSlopBench scores (scislopbench/data/scores.parquet, 390 pairs).
REFERENCE = {
    "macro_redund":   (0.004, 0.036),
    "xsec_ref":       (0.792, 0.960),
    "argument_graph": (0.364, 0.422),
    "citation":       (0.409, 0.644),
    "fig_exposition": (0.029, 0.200),
    "evidence_gap":   (0.372, 0.902),
    "aggregate":      (0.347, 0.538),
}
INFERENCE = {"argument_graph", "fig_exposition"}
KEY_KINDS = ("superiority", "prior_limitation", "design_choice")


# --------------------------------------------------------------------------- upstream access

def setup():
    if os.path.isdir(os.path.join(S, "_common")):
        return
    os.makedirs(os.path.dirname(REPO), exist_ok=True)
    env = dict(os.environ, GIT_LFS_SKIP_SMUDGE="1")
    subprocess.run(["git", "clone", "--depth", "1", UPSTREAM, REPO], check=True, env=env)


def common(roles):
    """Import the upstream loader, with extra heading->role patterns tried before the default map."""
    setup()
    sys.path.insert(0, os.path.join(S, "_common"))
    import views
    extra = []
    for spec in roles or []:
        pat, _, role = spec.rpartition("=")
        extra.append((role, re.compile(pat, re.I)))
    views.ROLE_MAP[:0] = extra
    patch_views(views)
    return views


def fix_section_sign(module):
    """Every module-level pattern that guards a "§" alternative with \\b gets the letter guard."""
    for name, val in list(vars(module).items()):
        if isinstance(val, re.Pattern) and "§" in val.pattern and val.pattern.startswith(r"\b(?:"):
            setattr(module, name, re.compile(val.pattern.replace(r"\b(?:", r"(?<![A-Za-z])(?:", 1), val.flags))


def patch_views(views):
    """Two corrections to the upstream loader, both of which only ever add what it misses.

    1. TEXT_SECREF_RE opens with \\b before its alternatives, and \\b cannot sit before "§" when a
       space or "(" precedes it, so "(§4)" was never read as a pointer. The guard becomes "not
       after a letter", which is what \\b meant for the word alternatives.
    2. pandoc wraps every heading in \\hypertarget{k}{...} and gives it \\label{k}. Those are
       generated, not declared by the author, and they enter xsec_ref's denominator as objects no
       one could be expected to point at; the hypertarget key also leaks into prose. Both are
       removed for keys that no reference macro uses.
    """
    if getattr(views, "_scislop_patched", False):
        return
    fix_section_sign(views)
    orig = views.expand_tex

    def expand_tex(main_path, depth=4):
        tex, report = orig(main_path, depth)
        keys = set(re.findall(r"\\hypertarget\{([^{}]+)\}\{", tex))
        used = {k for m in views.REF_MACRO_RE.finditer(tex) if not views.REF_BLACKLIST.search(m.group(1))
                for k in re.findall(r"\{([^{}]*)\}", m.group(2))}
        auto = keys - used
        if auto:
            tex = re.sub(r"\\hypertarget\{([^{}]+)\}\{", lambda m: "{" if m.group(1) in auto else m.group(0), tex)
            tex = re.sub(r"\\label\{([^{}]+)\}", lambda m: "" if m.group(1) in auto else m.group(0), tex)
            report = dict(report, pandoc_auto_labels_dropped=len(auto))
        return tex, report

    views.expand_tex = expand_tex
    views._scislop_patched = True


def records(texs):
    out = []
    for spec in texs:
        name, _, path = spec.partition("=")
        path = os.path.abspath(path)
        assert os.path.isfile(path), f"no such file: {path}"
        d = os.path.dirname(path)
        out.append({"corpus": "AI", "id": name, "root": d, "main_tex": path,
                    "paper_dir": d, "exp_dir": None, "diagram": None})
    return out


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)


def dump_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)


def scores_path(work):
    return os.path.join(work, "scores.json")


def put_score(work, name, measure, row):
    allsc = load_json(scores_path(work), {})
    allsc.setdefault(name, {})[measure] = row
    dump_json(scores_path(work), allsc)


# --------------------------------------------------------------------------- commands

def cmd_sections(a):
    views = common(a.role)
    for p in records(a.tex):
        doc = views.load_doc(p)
        print(f"== {p['id']}  (missing inputs: {len(doc.report['missing'])}, body words: {doc.n_words_body})")
        for s in doc.top_sections:
            where = "appendix" if s.in_appendix else "body"
            print(f"  {s.role:<12} {where:<8} {s.title}")


def cmd_deterministic(a):
    views = common(a.role)
    import corpus
    recs = records(a.tex)
    corpus.ai_papers = lambda: recs
    corpus.hu_papers = lambda: []
    here = os.getcwd()
    for measure_name, rel in DETERMINISTIC.items():
        cdir = os.path.join(S, rel, "code")
        out = os.path.join(os.path.abspath(a.work), "raw", measure_name)
        os.makedirs(out, exist_ok=True)
        sys.modules.pop("measure", None)
        sys.path.insert(0, cdir)
        os.chdir(cdir)
        try:
            import measure
            fix_section_sign(measure)
            argv = ["measure.py"]
            if "--out" in open("measure.py").read():
                argv += ["--out", out]
            else:
                measure.RESULTS = out
            sys.argv = argv
            # The upstream summaries compare AI against human papers and fail on an empty human
            # side; papers.jsonl is written before that, so the failure is expected and silenced.
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                try:
                    measure.main()
                except Exception:
                    pass
        finally:
            os.chdir(here)
            sys.path.remove(cdir)
        path = os.path.join(out, "papers.jsonl")
        rows = [json.loads(l) for l in open(path)] if os.path.exists(path) else []
        for r in rows:
            keep = {k: v for k, v in r.items() if not isinstance(v, (dict, list))}
            put_score(a.work, r["id"], measure_name, keep)
        print(f"{measure_name:<13} " + "  ".join(f"{r['id']}={r.get('slop_score')}" for r in rows))
    print(f"raw evidence records under {os.path.join(a.work, 'raw')}")


def cmd_intro(a):
    views = common(a.role)
    sys.path.insert(0, os.path.join(S, "Argument", "Argument_Graph", "code"))
    import llm  # noqa: F401  (imported by measure; needs no server at import time)
    import measure as AG
    for p in records(a.tex):
        doc = views.load_doc(p)
        text = AG.intro_text(doc)
        sents = AG.intro_sentences(text) if text else []
        task = {"paper": p["id"], "n": len(sents),
                "sentences": [{"i": i + 1, "text": s["text"]} for i, s in enumerate(sents)],
                "label_prompt": os.path.join(S, "Argument", "Argument_Graph", "code", "PROMPT_label.txt"),
                "instructions": "Fill labels.json: {\"<i>\": label} for EVERY sentence, label one of "
                                "superiority | prior_limitation | design_choice | none."}
        dump_json(os.path.join(a.work, p["id"], "argument", "intro.json"), task)
        print(f"{p['id']}: {len(sents)} introduction sentences"
              + ("" if sents else "  (no Intro-role section; see `sections` and --role)"))


def cmd_argument_prep(a):
    base = os.path.join(a.work, a.name, "argument")
    intro = load_json(os.path.join(base, "intro.json"))
    labels = load_json(os.path.join(base, "labels.json"))
    assert intro and labels is not None, "run intro, then write labels.json"
    n = intro["n"]
    missing = [i for i in range(1, n + 1) if str(i) not in labels]
    assert not missing, f"labels.json is missing sentences {missing}: every sentence gets a label"
    rng = random.Random(0)
    tasks, key = [], {}
    for c in [i for i in range(1, n + 1) if labels[str(i)] in KEY_KINDS]:
        others = [j for j in range(1, n + 1) if j != c]
        rng.shuffle(others)
        ids = {f"c{k:02d}": j for k, j in enumerate(others)}
        key[str(c)] = ids
        tasks.append({"claim_id": f"claim{c}", "claim": intro["sentences"][c - 1]["text"],
                      "candidates": {cid: intro["sentences"][j - 1]["text"] for cid, j in ids.items()}})
    dump_json(os.path.join(base, "support_tasks.json"),
              {"question": "For each claim, which ONE candidate sentence, if you had read it first, would most "
                           "raise how likely the claim is to be written and believed? Candidates are in random "
                           "order and carry no positions. Answer {\"claim<N>\": {\"id\": \"c<KK>\", \"quote\": "
                           "\"<the first eight or so words of that candidate, copied exactly>\"}}.",
               "tasks": tasks})
    dump_json(os.path.join(base, "_support_key.json"), key)
    print(f"{len(tasks)} key claims -> {os.path.join(base, 'support_tasks.json')}; answers go in picks.json")


def cmd_argument_score(a):
    base = os.path.join(a.work, a.name, "argument")
    intro = load_json(os.path.join(base, "intro.json"))
    labels = load_json(os.path.join(base, "labels.json"))
    key = load_json(os.path.join(base, "_support_key.json"))
    picks = load_json(os.path.join(base, "picks.json"))
    assert key is not None and picks is not None, "run argument-prep, then write picks.json"
    n, claims, expected, bad = intro["n"], [], [], []
    norm = lambda s: re.sub(r"\W+", " ", s).strip().lower()
    for c, ids in key.items():
        c = int(c)
        pick = picks.get(f"claim{c}") or {}
        if isinstance(pick, str):
            pick = {"id": pick}
        pid, quote = pick.get("id"), pick.get("quote", "")
        if pid not in ids:
            bad.append(f"claim{c}: id {pid!r} is not one of its candidate ids")
            continue
        j = ids[pid]
        # A judge facing forty shuffled ids slips on the id while naming the right sentence; the
        # quote catches that, and a pick whose quote is not in its own candidate is refused.
        if not quote or norm(quote) not in norm(intro["sentences"][j - 1]["text"]):
            bad.append(f"claim{c}: quote {quote!r} is not in candidate {pid}")
            continue
        claims.append({"i": c, "label": labels[str(c)], "support": j, "argued": j < c,
                       "claim": intro["sentences"][c - 1]["text"]})
        expected.append((c - 1) / (n - 1))
    if bad:
        raise SystemExit("picks.json refused; re-ask the judge for these claims:\n  " + "\n  ".join(bad))
    declared = sum(not x["argued"] for x in claims)
    k = len(claims)
    row = {"slop_score": round(declared / k, 4) if k else None, "slop_numerator": declared,
           "slop_denominator": k, "weak": k < 3, "coverage": round(k / n, 4) if n else None,
           "positional_null_declared": round(1 - sum(expected) / k, 4) if k else None,
           "method": "inference (Claude labels + blind support pick); upstream uses Qwen labels + PMI"}
    dump_json(os.path.join(base, "claims.json"), claims)
    put_score(a.work, a.name, "argument_graph", row)
    print(json.dumps(row, indent=1))


def cmd_figure(a):
    common(None)
    sys.path.insert(0, os.path.join(S, "Artifacts", "fig_exposition", "code"))
    with contextlib.redirect_stdout(io.StringIO()):
        import measure as FE
    base = os.path.join(a.work, a.name, "figure")
    tr = load_json(os.path.join(base, "transcript.json"))
    if tr is None or tr.get("method_figure") is False:
        put_score(a.work, a.name, "fig_exposition",
                  {"slop_score": None, "applicable": False, "method": "no method figure"})
        print("no method figure: not applicable (never scored as clean)")
        return
    found, words = FE.read_kinds([str(x) for x in tr["lines"]], pid="__none__")
    if tr.get("experimental_content"):
        found["experimental_content"] = [tr["experimental_content"]]
    row = {"slop_score": round(len(found) / FE.N_KINDS, 4), "slop_numerator": len(found),
           "slop_denominator": FE.N_KINDS, "applicable": True, "text_words": words,
           "kinds": {k: v[:3] for k, v in found.items()},
           "method": "inference (Claude transcription + upstream patterns); upstream uses Qwen2.5-VL"}
    put_score(a.work, a.name, "fig_exposition", row)
    print(json.dumps(row, indent=1, ensure_ascii=False))


def cmd_report(a):
    allsc = load_json(scores_path(a.work), {})
    lines = []
    for name, sc in allsc.items():
        lines += [f"## {name}", "",
                  "| Plane | Measure | Score | Units | Human mean | AI mean | How |",
                  "|---|---|---:|---|---:|---:|---|"]
        planes = {}
        for plane, ms in PLANES.items():
            vals = []
            for m in ms:
                r = sc.get(m, {})
                v = r.get("slop_score")
                vals.append(v)
                units = f"{r.get('slop_numerator')}/{r.get('slop_denominator')}" if v is not None else "n/a"
                if r.get("weak") and v is not None:
                    units += " (weak)"
                how = "inference" if m in INFERENCE else "deterministic"
                hu, ai = REFERENCE[m]
                shown = "n/a" if v is None else f"{v:.3f}"
                lines.append(f"| {plane} | {m} | {shown} | {units} | {hu:.3f} | {ai:.3f} | {how} |")
            got = [v for v in vals if v is not None]
            planes[plane] = sum(got) / len(got) if got else None
        got = [v for v in planes.values() if v is not None]
        agg = sum(got) / len(got) if got else None
        complete = all(sc.get(m, {}).get("slop_score") is not None for ms in PLANES.values() for m in ms)
        hu, ai = REFERENCE["aggregate"]
        tag = "" if complete else " (partial: n/a measures skipped, not comparable to the benchmark aggregate)"
        lines += ["", f"Aggregate (plane means, then mean of planes): "
                      f"{'n/a' if agg is None else f'{agg:.3f}'}{tag}. Benchmark: human {hu}, AI {ai}.", ""]
    out = os.path.join(a.work, "report.md")
    with open(out, "w") as f:
        f.write("\n".join(lines))
    print("\n".join(lines))
    print(f"-> {out}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("setup", "sections", "deterministic", "intro", "argument-prep", "argument-score", "figure", "report"):
        p = sub.add_parser(c)
        if c in ("sections", "deterministic", "intro"):
            p.add_argument("--tex", action="append", required=True, help="NAME=path/to/main.tex")
            p.add_argument("--role", action="append", help="'HEADING_REGEX=Role', tried before the default map")
        if c != "setup" and c != "sections":
            p.add_argument("--work", required=True)
        if c in ("argument-prep", "argument-score", "figure"):
            p.add_argument("--name", required=True)
    a = ap.parse_args()
    if getattr(a, "work", None):
        a.work = os.path.abspath(a.work)
    {"setup": lambda a: setup(), "sections": cmd_sections, "deterministic": cmd_deterministic,
     "intro": cmd_intro, "argument-prep": cmd_argument_prep, "argument-score": cmd_argument_score,
     "figure": cmd_figure, "report": cmd_report}[a.cmd](a)


if __name__ == "__main__":
    main()
