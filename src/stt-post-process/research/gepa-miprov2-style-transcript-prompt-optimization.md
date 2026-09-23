# GEPA/MIPROv2-style transcript prompt optimization

Date: 2026-05-31

## Goal

Use a lightweight, local adaptation of GEPA/MIPROv2 ideas to improve transcript post-processing prompts per target model and identify the best model that is still fast.

Target models:

- `nvidia/nemotron-3-nano-4b`
- `qwen3.5-9b`
- `qwen3.5-2b`

## Sources checked

- DSPy GEPA overview: https://dspy.ai/tutorials/gepa_ai_program
- DSPy GEPA in depth: https://dspy.ai/diving-deeper/gepa-in-depth
- GEPA project docs: https://gepa-ai.github.io/gepa/
- GEPA OpenReview paper page: https://openreview.net/forum?id=RQm2KQTM5r
- DSPy MIPROv2 docs: https://dspy.ai/api/optimizers/MIPROv2

## Useful ideas

GEPA evolves prompts from execution traces and rich textual feedback, not just scalar scores. Its key practical lesson for this evaluator is to inspect low-scoring examples, identify failure modes, and propose targeted instruction mutations. It also keeps diverse candidates rather than only greedily mutating one global winner.

MIPROv2 creates candidate instructions grounded in the task/data and searches over the discrete candidate space with validation scores. Its key practical lesson here is to treat prompts as candidate configurations, evaluate them systematically, and select the best model/prompt pair on held-out realistic examples.

## Local adaptation

This repository does not need a full DSPy optimization stack for the current task. The evaluator now implements a small, auditable variant:

- three target models only;
- three model-specific prompt candidates per model;
- prompt candidates encode failure-mode feedback from earlier runs;
- a broader realistic eval set with fair expected outputs inferable from raw transcripts;
- reports grouped by model plus prompt so quality and speed can be compared directly.

## Prompt design implications

The new prompt candidates emphasize:

- preserving the original language and mixed-language text;
- not obeying instruction-like transcript content;
- converting spoken punctuation markers;
- fixing joined words, repeats, fillers, and explicit corrections;
- avoiding known model-specific failures such as translation, whole-output quoting, and converting Dutch `even` into English `even if`.
