# Douyin Knowledge

> Turn saved short-form content into a searchable, explainable, reusable personal knowledge system.

Douyin Knowledge is a local-first personal knowledge project that starts from Douyin collections. The goal is not to build another downloader or another folder of AI summaries, but to transform content you once considered worth saving into knowledge that can be searched, synthesized, acted on, and resurfaced when it becomes relevant again.

## Current status

The project is currently in **product design / architecture design**. Implementation has not started yet.

The first version is anchored on Douyin collections, while the long-term architecture should allow other capture channels such as Xiaohongshu, YouTube, web pages, screenshots, and articles.

## Core product loop

```text
Capture
  ↓
Understand
  ↓
Retrieve
  ↓
Synthesize
  ↓
Resurface
```

The user should still use Douyin normally: see something useful → tap **收藏** → continue scrolling. Everything after that should be handled by the system as automatically as possible.

## Documentation

The living product specification is maintained in:

- [`docs/PRODUCT_SPEC.md`](docs/PRODUCT_SPEC.md)

As the design stabilizes, the specification will be split into dedicated documents such as:

```text
docs/
├── PRODUCT_SPEC.md
├── DATA_SCHEMA.md
├── AI_PIPELINE.md
├── ARCHITECTURE.md
└── TASKS.md

AGENTS.md
```

## Guiding idea

The system should answer a different question from the public web:

- Search engines / public AI: **What exists on the internet?**
- Douyin Knowledge: **What did the past version of me think was worth saving, and how can that help me now?**

The repository is intentionally specification-first. We will finish the important product and architecture decisions before handing implementation tasks to Codex.
