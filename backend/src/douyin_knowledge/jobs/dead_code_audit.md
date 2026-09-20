# Dead Code Audit (P1-5)

This document records abstractions that exist in the codebase but are not on any live execution path.

## Principle

An abstraction that implies a working feature but has no path to it is worse than no abstraction: it misleads readers about what the system can do, and creates maintenance burden for code that never runs.

## Findings

### 1. QueryPlanner (retrieval/query_planner.py)

**Status**: Dead - imported but never called

**Evidence**:
- Imported in `retrieval/__init__.py` 
- Config has `query_planner_model` setting
- No call site exists anywhere in the codebase
- `enable_query_enrichment` flag exists but is never read

**Decision**: Remove the module and config. Query planning may be valuable later, but this implementation was never wired and pretending it works is misleading.

### 2. KnowledgeItem write path

**Status**: Dead - table exists, no writer

**Evidence**:
- `knowledge_items` table in schema
- `KnowledgeItem` model defined
- `KnowledgeItemUserState` exists to track state on these items
- No code path creates rows
- Wiki builder and retriever never read from it

**Decision**: Keep the table (it's in migration 0001) but document honestly that the writer is not implemented. Remove it from `__all__` exports to signal it's not ready.

### 3. Unregistered JobTypes

**Status**: Dead - enum values with no handlers

**Evidence**:
- `ENRICH_SOURCE` defined in JobType enum, no handler registered
- `CLEANUP_CACHE` defined, no handler
- `EXPORT_MARKDOWN` defined, no handler

**Decision**: Remove from the enum. If these are needed later, they can be added with their handlers.

### 4. Wiki content indexed but not retrieved

**Status**: Partially dead - wiki pages are indexed into search_documents but never retrieved

**Evidence**:
- `collect_wiki_candidates` in indexer creates search documents for wiki pages
- No retrieval path selects `doc_type = 'wiki'`
- HybridRetriever only retrieves 'chunk' documents

**Decision**: Document this honestly. The indexer does real work, so removing it would be wrong, but the retrieval path is incomplete. Add a comment in the retriever explaining that wiki document retrieval is not yet implemented.

### 5. UserState tables (EntityUserState, KnowledgeItemUserState, UserAnnotation)

**Status**: Scaffolding - tables exist, no read/write paths

**Evidence**:
- Three tables in migration 0001
- Models defined
- No API endpoints
- No service layer
- P1-4 (Resurface) was meant to use these but is not implemented

**Decision**: Keep the tables (they're in the initial migration) but document that the feature is not implemented. This is honest scaffolding, not misleading dead code - the tables don't pretend to work.

## Removed

- `retrieval/query_planner.py` and imports
- `config.query_planner_model` 
- `JobType.ENRICH_SOURCE`
- `JobType.CLEANUP_CACHE`
- `JobType.EXPORT_MARKDOWN`

## Documented but kept

- `KnowledgeItem` - table exists, writer not implemented (add docstring)
- UserState tables - schema exists, feature not implemented (already documented in models)
- Wiki document retrieval - indexer works, retriever doesn't use it yet (add comment)
