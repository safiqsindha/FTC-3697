---
name: scislop
description: Score a LaTeX paper for "scientific slop" with the six SciSlop measures (cross-section references, macro redundancy, argument graph, citation isolation, figure exposition, evidence gap) and say what to fix. Use when asked to check a paper for slop, AI-paper tells, structural padding, or to compare a draft against the SciSlopBench human/AI baselines. No GPU: the two model-based measures are judged by Claude instead of local Qwen models.
---

# SciSlop: measure scientific slop in a paper

SciSlop (*Science or Slop?*, under review at ICLR 2027, code at
https://github.com/yerimoh/ScientificSlop) defines six measures over three planes. Each one is
**failed units / checked units, higher = more slop**, and the aggregate is the mean of each plane's
measures, then the mean of the three planes. On SciSlopBench (390 AI papers paired with human
papers) the aggregate tells the AI paper of a pair from the human one 85.9% of the time.

`scripts/scislop.py` runs everything. On first use it clones the upstream repo to
`~/.cache/scislop/scientificslop` (override with `SCISLOP_REPO`). The upstream repo has no
license, so **never copy its code into this repository**. The skill fetches and runs it.

## The six measures

| Plane | Measure | Unit, and what counts as a failure | Human / AI mean | Fix when high |
|---|---|---|---|---|
| Structure | `xsec_ref` | Each body section and each labelled figure, table or equation. **Fails** when no *other* section points to it. Pointers inside the object's own section don't count. | 0.79 / 0.96 | Have later sections cite earlier tables and sections ("the 2×2 of §3"), and have Discussion and Limitations point back at specific results |
| Structure | `macro_redund` | Each sentence of 8 or more tokens. **Fails** when at least half its tokens sit in 8-grams already used in an earlier, different section | 0.004 / 0.036 | Cut sentences recycled across sections; rephrase restatements in the abstract and conclusion |
| Argument | `argument_graph` | Each key claim of the Introduction (superiority, prior limitation or design choice). **Fails** (*declared*) when its strongest support sentence comes *after* it | 0.36 / 0.42 | Put the evidence or premise before the claim it supports |
| Argument | `citation` | Each citing sentence in the Introduction and Related Work. **Fails** when the citation is isolated: no comparison, contrast or relation to this paper is woven in | 0.41 / 0.64 | Say how each cited work relates: what it lacks, what you take from it, where you differ |
| Artifacts | `fig_exposition` | The method figure. Score = how many of six kinds of exposition it takes over from the text: a notation legend, Ours/Baseline arms, experimental settings or results, ✓/✗ marks, a "Key insight" box, "Stage N" sequences | 0.03 / 0.20 | Move legends into the caption, results into the results section, and the thesis into the prose |
| Artifacts | `evidence_gap` | Applies only when the body has a result table. **Fails** when there's no displayed example anywhere: verbatim block, case or failure caption, or a long quote | 0.37 / 0.90 | Show at least one concrete instance (an input/output, a failure case, a transcript) |

`weak` means fewer than 3 units were checked. Report the score but don't interpret it. `n/a`
means the measure doesn't apply. It is not a clean score, so never present it as one.

## Workflow

Set `P=.claude/skills/scislop/scripts/scislop.py` and pick a work directory (use the
scratchpad).

1. **Get LaTeX.** The measures read `.tex` with `\input` expanded. For this repo, run
   `python3 build/md2tex.py` (long paper → `build/main.tex`) and
   `python3 build/md2tex.py workshop build/workshop-sections` (→ `build/workshop.tex`). No PDF
   is needed.
2. **Check section roles:** `python3 $P sections --tex long=build/main.tex`. Roles come from the
   headings: Intro, Related, Method, Experiments, Conclusion. The argument graph reads Intro
   only, and citation reads Intro and Related. A paper with no heading like "Introduction" makes
   both n/a. Map headings with `--role 'HEADING_REGEX=Role'`, e.g.
   `--role '^1\. summary$=Intro'`, and pass the same flags to every later command. Tell the user
   which overrides you used.
3. **Deterministic measures** (exactly the upstream code):
   `python3 $P deterministic --tex long=build/main.tex --work $W [--role ...]`
4. **Argument graph** (inference):
   - `python3 $P intro --tex ... --work $W` writes `$W/<name>/argument/intro.json`.
   - **Labels.** Read the upstream prompt at the path in `label_prompt`, apply its rules to
     *every* sentence, and write `labels.json` as `{"<i>": "superiority|prior_limitation|design_choice|none"}`.
     The prompt says "when in doubt, answer none". Follow it, since over-labelling inflates the
     denominator.
   - `python3 $P argument-prep --work $W --name <name>` writes `support_tasks.json`. Each claim
     comes with every other sentence, shuffled, under a random id, with no positions.
   - **Support picks, done blind.** Spawn one subagent and give it *only* the contents of
     `support_tasks.json`, never the paper. It answers `{"claim<N>": "c<KK>"}` into `picks.json`.
     You've read the paper in order, so picking yourself brings back the position bias this step
     exists to remove.
   - `python3 $P argument-score --work $W --name <name>`. Compare the score against
     `positional_null_declared`, the declared share you'd expect from claim positions alone.
5. **Figure exposition** (inference). Find the method figure: the overview or pipeline diagram,
   not a result plot.
   - If there is none, write `{"method_figure": false}` to `$W/<name>/figure/transcript.json`.
   - Otherwise, render it (`pdftoppm -png -r 150` for a PDF figure) and Read the image.
     Transcribe every piece of text in it, verbatim, one entry per line, without describing or
     adding anything, as `{"lines": [...]}`.
   - **Experimental content** is the one kind a regex can't decide. Set
     `"experimental_content": "<the line>"` only when the figure shows a run setting the
     experimenter chose (learning rate, epochs, batch size, seeds, dataset size, context length)
     or a value the run produced (a rate, score or speed-up), or has an evaluation panel. A
     constant of the method itself doesn't count (threshold, number of candidates, ensemble
     size).
   - Run `python3 $P figure --work $W --name <name>`. It applies the upstream patterns to your
     transcript.
6. **Report:** `python3 $P report --work $W` writes `report.md`. Raw per-unit evidence is under
   `$W/raw/<measure>/`: `objects.jsonl` (xsec_ref), `sentences.jsonl` (macro_redund),
   `paragraphs.jsonl` and `claims.jsonl` (citation), `instances.jsonl` (evidence_gap). Use it to
   name the *specific* sections, objects and sentences to fix. The scores alone don't say what to
   change.

## How to read the result

- Compare each measure against its two benchmark means, not against zero. A human-written paper
  typically scores about 0.8 on `xsec_ref`.
- The aggregate is comparable to the benchmark only when all six measures scored. Otherwise the
  report labels it partial. Say so, and don't quote it as "the SciSlop score".
- The two inference measures are **not calibrated** to the benchmark numbers. Upstream labels
  with Qwen2.5-32B (majority of 3 greedy runs), picks support by PMI under Qwen2.5-7B, and
  transcribes figures with Qwen2.5-VL. Claude's judgements will differ. Mark these two as
  "inference" whenever you quote them.
- These are structural signals, not authorship detectors. A human paper written around one
  result will legitimately score high on `xsec_ref`. Recommend changes that improve the paper,
  not changes that game the metric.

## Why there's no GPU

Upstream needs GPUs for three reasons, and none of them is essential to the concepts:

1. **Reproducibility.** Fixed open weights, greedy decoding and a cache make the published
   numbers bit-for-bit repeatable. An API model changes under you.
2. **PMI.** The argument graph scores support as `logP(s_i | s_j) − logP(s_i)` over every pair
   of sentences. That needs log-probabilities of arbitrary text, which a hosted chat API doesn't
   expose. The skill replaces it with a blind "which sentence most supports this claim?" pick.
   Hiding positions is what keeps this from being the v2 design the authors dropped. In v2, a
   model was asked which sentence grounds a claim, and the models answered by position: moving
   the grounding sentence to the end lost it in 87–100% of cases. Shuffling the candidates and
   giving the step to a subagent that hasn't read the paper removes that cue, but it is still a
   judge, not a likelihood.
3. **Benchmark scale.** Thousands of labelling calls over 780 papers. For one paper, Claude in
   the loop is cheaper than spinning up vLLM.

The four deterministic measures never needed a GPU.
