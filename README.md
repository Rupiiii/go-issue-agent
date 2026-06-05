# go-issue-agent

A six-stage pipeline agent that takes a GitHub issue from
[go-playground/validator](https://github.com/go-playground/validator), understands the
repository, plans and implements a fix with an LLM (tool-calling), validates it with the
Go toolchain, and emits a git diff, a local branch, and a PR summary.

The pipeline is built directly — so every
stage is readable and auditable.

## Pipeline stages

| Stage | Module | Responsibility |
|-------|--------|----------------|
| 1. Ingest | `agent/ingest.py` | Fetch issue title/body/comments/labels via PyGithub |
| 2. Understand | `agent/understand.py` | File tree, per-file skeletons, relevant-file scoring (+ files named in the issue) |
| 3. Plan | `agent/plan.py` | LLM produces a strict-JSON, minimal `FixPlan` |
| 4. Implement | `agent/implement.py` | Tool-calling loop: search / read / edit / write / run Go |
| 5. Validate | `agent/validate.py` | `go fmt`/`build`/`vet`/`test`; retry on *mechanical* failures |
| 6. Output | `agent/output.py` | Branch + commit (agent's files only) + diff + PR summary + run metadata |

`agent/pipeline.py` wires them together; `main.py` is the CLI.

## LLM provider

The LLM backend is swappable behind `agent/llm.py` and selected by `provider` in
`config/settings.yaml`:

- **`google`** (default) — Google AI Studio (Gemini), via the `google-genai` SDK. Needs
  `GEMINI_API_KEY` (or `GOOGLE_API_KEY`). Default model `gemini-2.5-flash`; `thinking_budget`
  caps thinking tokens per call.
- **`anthropic`** — Claude via the Anthropic SDK. Needs `ANTHROPIC_API_KEY`.

The canonical message format is provider-neutral; each backend translates to/from its own
wire format, so Stages 3/4/6 are identical regardless of provider. Transient API errors
(503/429/5xx) are retried with exponential backoff.

## Setup

1. Clone this repo.
2. `pip install -r requirements.txt`
3. Install Go 1.21+ (required for Stage 5 validation).
4. *(Optional)* Install ripgrep for faster code search: `brew install ripgrep` (macOS) or
   `apt install ripgrep` (Linux). If absent, `search_code` falls back to a built-in
   Python search — no functionality is lost.
5. Copy `.env.example` to `.env` and fill in the LLM key for your provider plus
   `GITHUB_TOKEN`.
6. Clone the target repo: `git clone https://github.com/go-playground/validator /tmp/validator`

## Running

```bash
python main.py \
  --issue https://github.com/go-playground/validator/issues/XXX \
  --repo /tmp/validator
```

Options:
- `--output DIR` — output directory (default: `./outputs/<issue_number>/`)
- `--dry-run` — run Stages 1–3 only and print the plan (no code changes)

### Per-stage runners (debugging)

Each stage can be exercised in isolation:

```bash
python scripts/run_phase1.py <issue_url>                # Ingest
python scripts/run_phase2.py <issue_url> <repo_path>    # + Understand
python scripts/run_phase3.py <issue_url> <repo_path>    # + Plan (first LLM call)
python scripts/run_phase4.py <issue_url> <repo_path>    # + Implement (tool loop)
python scripts/run_phase5.py <issue_url> <repo_path>    # + Validate
```

## Tools (Stage 4)

The model drives the fix through five tools (`agent/tools.py`), all confined to the repo
root:

- `search_code` — ripgrep across the repo (Python fallback if `rg` is absent); returns line numbers
- `read_file` — full file, or a line range (`start_line`/`end_line`) for large files
- `edit_file` — unique-match string replace; preferred for edits (escape-safe, works on large files)
- `write_file` — whole-file write; for new or small files only
- `run_go_command` — one of `go fmt|build|vet|test ./...` (whitelisted)

## Configuration (`config/settings.yaml`)

| Key | Meaning |
|-----|---------|
| `provider` | `google` (Gemini) or `anthropic` |
| `model` | e.g. `gemini-2.5-flash` |
| `thinking_budget` | Gemini thinking tokens per call (0=off, -1=dynamic) |
| `max_tokens_per_call` | output token cap per LLM call |
| `context_budget_tokens` | Stage-4 input budget; oldest tool results are truncated past this |
| `max_implement_iterations` | Stage-4 tool-loop cap |
| `max_validate_retries` | Stage-5 retries for mechanical failures |
| `excluded_dirs` | dirs skipped during repo mapping (`vendor`, `translations`, `_examples`, …) |

## Output

```
outputs/{issue_number}/
  changes.diff       ← unified diff of the agent's files ONLY (no go-fmt noise)
  pr_summary.md      ← PR title and body, reporting the real validation result
  run_summary.json   ← branch, files, validation, iterations, tokens, est. cost

logs/
  run_{issue}_{timestamp}.json  ← full structured log (per-stage, per-iteration, model)
```

## Behavior notes

- **Validation retries are scoped.** Stage 5 retries Stage 4 only on *mechanical* failures
  — a `go build` error, a `go vet` error, or a test binary that fails to compile. A test
  that runs and fails an **assertion** is reported as honest signal and is NOT retried —
  "retry until green" would only pressure the model to weaken or delete tests. A partial
  success (build passes, tests fail) is a valid, clearly-reported outcome.
- **Diffs are clean.** `go fmt ./...` reformats the whole module on disk, so Stage 6
  commits only the files the agent actually wrote and discards unrelated reformatting.
- **Self-contained fixes only.** The agent cannot add third-party dependencies
  (`go get`/`go mod tidy` are not available); it fixes within the standard library and the
  repo's existing dependencies.
- The repo is never left broken: on an unrecoverable error the working tree is reset.

## Tests

Offline smoke tests (no network or API key needed):

```bash
python tests/test_pipeline.py        # or: python -m pytest tests/ -q
```

## Sample outputs

See `outputs/1543/` for a clean end-to-end success (add `dns_label` validator: build,
tests, and vet all pass). 
