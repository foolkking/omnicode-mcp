# `_keep_/` — pinned artefacts allow-list

Anything you drop under this directory bypasses `.gitignore`. The rest
of the repo aggressively ignores anything that looks like a
session-local artefact (`_*.py`, `*.log`, `.data/`, etc.); when you
*do* need to share a snippet, log, or reproducer with the team or with
CI, put it here.

## Conventions

* **`_keep_/regressions/<YYYY-MM-DD>-<short-name>.log|.py|.json`** —
  reproducers for fixed bugs. Each file should also be referenced by
  a comment in the code that fixed the bug (so we don't lose the
  paper trail).
* **`_keep_/samples/<topic>.<ext>`** — minimal positive samples used
  by the tests or by the docs.
* **`_keep_/perf/<YYYY-MM>-baseline.json`** — periodic baseline
  measurements; useful for diffing new optimisation runs against the
  shipped baseline.

## Anti-patterns

* Don't dump full server logs here — even after redaction they tend
  to leak workspace paths or model names. Trim to the minimum needed
  to reproduce the issue, *then* commit.
* Don't put real customer code or PII here. The repo is public.

## Rotating

When a regression file becomes irrelevant (the underlying bug has
been re-tested by a proper unit test), delete it. We don't archive
debug artefacts forever.
