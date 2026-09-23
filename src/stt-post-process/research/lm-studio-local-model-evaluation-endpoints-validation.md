# LM Studio local model evaluation endpoint validation

Date: 2026-05-31

## Question

Validate whether `eval_lmstudio_postprocess.py` should use LM Studio's local OpenAI-compatible chat completions endpoint and native model-load endpoint for sequential transcript post-processing benchmarks.

## Sources checked

- LM Studio REST API docs: https://lmstudio.ai/docs/developer/rest
- LM Studio list models docs: https://lmstudio.ai/docs/developer/rest/list
- LM Studio load model docs: https://lmstudio.ai/docs/developer/rest/load
- LM Studio unload model docs: https://lmstudio.ai/docs/developer/rest/unload
- LM Studio OpenAI compatibility docs: https://lmstudio.ai/docs/developer/openai-compat
- LM Studio chat completions docs: https://lmstudio.ai/docs/developer/openai-compat/chat-completions

## Findings

LM Studio's current native REST API is v1 under `/api/v1/*`, and its supported endpoints include `GET /api/v1/models`, `POST /api/v1/models/load`, and `POST /api/v1/models/unload`.

`POST /api/v1/models/load` accepts a model identifier plus optional load configuration such as `context_length` and `flash_attention`. This is the right endpoint for loading each candidate model before running the benchmark.

`POST /api/v1/models/unload` unloads by `instance_id`, not by model key. The safest implementation is to use `instance_id` returned by the load response, and to fall back to `GET /api/v1/models` `loaded_instances[].id` when the script needs to unload a model that was already loaded or when the load response is unavailable.

LM Studio also exposes OpenAI-compatible endpoints under `/v1`, including `POST /v1/chat/completions`. The chat completions endpoint accepts normal chat payload fields such as `model`, `messages`, `temperature`, `max_tokens`, and `stream`.

For this benchmark, `/v1/chat/completions` is appropriate because transcript cleanup is stateless and fits a system/user chat prompt. The native `/api/v1/chat` endpoint has LM Studio-specific features such as stateful chat, MCP access, model-load streaming events, and request-level context length, but those are not required for a fair stateless model comparison.

## Decision

Keep the script on:

- `POST /api/v1/models/load` for sequential model loading.
- `POST /api/v1/models/unload` after each model run, using loaded instance IDs.
- `POST /v1/chat/completions` for transcript post-processing requests.
- `GET /api/v1/models` as a preflight check so local model identifier mismatches are visible before a long benchmark run.
- A generated HTML report as the primary review artifact for comparing performance, speed, quality, granularity, and mixed-task behavior per model.

## Consequences

The evaluator remains simple and portable. Users can still override model IDs and endpoints, while the built-in benchmark list now matches the requested models. Exact local model IDs may differ from the short names in LM Studio, so the preflight warning is useful and should not abort the run.

The final evaluator uses Python's standard library HTTP client instead of `requests`, so it can run in a plain Python environment without installing extra packages.
