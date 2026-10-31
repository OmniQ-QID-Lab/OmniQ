# Judge prompt: an equivalent, not the original

**This is not the prompt the paper's judge received.** The original prompt was part of the authors' judge job and
is not included in this release. The text below was written for this release from what the published criteria files
(`JUDGE.md`) and verdicts (`judge_result.md`) show. Verdicts made with it can differ from the paper's verdicts, as
verdicts can also differ between judge models and Claude Code versions.

Why a short prompt is enough: every task's `JUDGE.md` already holds the judge's role, the rules and the procedure.

- Role: a strict, adversarial evaluation that audits the saved artifacts (figure, report, source code, saved
  numerical results) and the paper, and does not rerun code.
- Two severity classes only, CRITICAL and NON-CRITICAL. Strict pass (score 1) means zero CRITICAL issues; one or more
  CRITICAL issues give score 0.
- Audit steps: check the deliverables, read the paper and locate the target figure, apply every paper-specific
  criterion with its stated severity, and read the report and code for admissions that violate a criterion.
- Output format: `judge_result.md` with the sections `## Score` (0 or 1 on the next line), `## Figure Analysis`,
  `## Issues Found` (one `[CRITICAL]` or `[NON-CRITICAL]` item per line) and `## Reason`.

`judge.sh` sends the text between the two marker lines below, unchanged, as the prompt of a headless Claude Code
session whose working folder is `/workspace`. `/workspace` then holds a copy of the finished run (without earlier
verdicts and without `.dsh_ra3/`), the task's `JUDGE.md` and the paper PDF(s) the user supplied.
`infra/judge/parse_score.py` reads the score.

<!-- BEGIN PROMPT -->
You are working in /workspace. Read ENV.md first for the environment, then read JUDGE.md for your evaluation instructions: it defines your role, the rules, the audit procedure and the output format. The original paper is the PDF paper_*.pdf in /workspace (its supplementary material, when present, is paper_*_SM.pdf). Evaluate the reproduction in /workspace exactly as JUDGE.md says: the reproduced figure files (reproduced_figure*.png and .pdf), report.md, the source code and the saved numerical results. Do not rerun the reproduction, and do not change any file in /workspace other than judge_result.md. Write your verdict to /workspace/judge_result.md following the Output Format in JUDGE.md; the line after "## Score" must hold only the number 0 or 1. Work fully autonomously. Do not ask any questions. Start now.
<!-- END PROMPT -->
