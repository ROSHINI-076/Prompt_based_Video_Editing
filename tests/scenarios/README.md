# Engine scenarios

Each `*.yaml` file is one deterministic, structured-intent conversation. The
pytest runner loads all scenario files, executes each conversation three times,
checks the expected result after every step, and compares the per-step timeline
hashes across runs.

Required top-level keys:

- `name`: descriptive scenario name.
- `transcript`: `synthetic_5min` (the shared five-minute fixture) or
  `synthetic_two_speaker`.
- `steps`: ordered operations. Each step has `ops` (structured engine
  intents), optional `answer` (automatically supplied only after a
  clarification), and `expect`.

Scenario-specific `words`, `sentences`, and `duration_s` may replace or adjust
the selected synthetic transcript to keep hard cases compact and explicit.
Word entries use `text`, `start_s`, `end_s`, and optional `speaker`.
`time_expression` is a test-harness convenience that exercises the existing
pure time parser and converts its result to the engine's ordinary numeric
time-target intent; it does not add an instruction parser.

Expectations support status/error, duration with optional tolerance,
warning/summary/question/option substrings, caption display times, operation
source ranges, active word IDs, protected-pause IDs, history sizes, hashes
matching prior one-based steps, and selected fields from final timeline output.
`{"op": "render"}` is a marker only: it inspects the timeline projection and
does not start a renderer or create media.
