# Access translation progress

Last updated: 2026-09-03

This document records what the offline converter can actually do. Percentages
always include their numerator and denominator. A successful task or a readable
object name is not counted as a completed semantic translation.

The machine-readable snapshot is `docs/translation_progress.json`, regenerated
by `python tools/verify_corpus.py --json docs/translation_progress.json`.

## Status definitions

| Status | Meaning |
|---|---|
| `complete` | The required source semantics were extracted, represented in IR, and translated. |
| `partial` | Identity or some metadata was recovered, but content or semantics remain missing. |
| `unsupported` | The source feature is known, but no product implementation exists. |
| `failed` | A supported operation was attempted and ended with a concrete error. |
| `not_started` | The operation has not yet been attempted. |
| `not_applicable` | The operation does not apply and is excluded from the denominator. |

`partial` never receives fractional credit toward `complete`. This prevents a
catalog-only result from being reported as a converted query, form, or module.

## Corpus baseline

The 15 collected samples contain 21 Access artifacts across four format
families. Every one of them is now readable by a direct reader.

| Measure | Value | Rate |
|---|---:|---:|
| Artifacts accepted by a direct reader | 21 / 21 | 100.00% |
| Format families with a direct reader | 4 / 4 (Jet3, Jet4, ACE, ACCDT) | 100.00% |
| Objects discovered from the artifacts themselves | 1,522 | — |
| Object definitions extracted | 275 / 1,522 | 18.07% |
| Objects semantically translated (`complete`) | 168 / 1,522 | 11.04% |
| Objects semantically translated (`partial`) | 107 / 1,522 | 7.03% |

Discovery is not translation, and the table above keeps them apart. The 1,247
objects that are discovered but not extracted all live in binary `.mdb`/`.accdb`
files and carry the reason code
`BINARY_OBJECT_DEFINITION_EXTRACTION_NOT_IMPLEMENTED`.

### By format family

| Family | Artifacts | Objects discovered | Definitions extracted |
|---|---:|---:|---:|
| ACCDT (OOXML template) | 2 | 275 | 275 |
| ACE 12/14/16 (`.accdb`) | 15 | 1,172 | 0 |
| Jet 4.0 (`.mdb`) | 3 | 23 | 0 |
| Jet 3.x (`.mdb`) | 1 | 52 | 0 |

The Jet4/ACE catalog reader's object counts were checked against the
independently produced inventories in `samples/*/object_inventory.json`; all 18
Jet4/ACE artifacts agree exactly, kind by kind, including linked tables and
hidden `~sq_` queries.

## Semantic translation of the extracted definitions

For the 275 definitions that were extracted (both Northwind ACCDT artifacts),
each object is scored on the aspects that apply to it. See
`docs/SEMANTIC_TRANSLATION.md` for what each aspect covers.

| Aspect | Complete | Partial | Scored | Rate |
|---|---:|---:|---:|---:|
| Structure — the definition itself was recovered and modelled | 273 | 2 | 275 | 99.27% |
| Data logic — expressible as data processing in the target | 209 | 35 | 244 | 85.66% |
| Application logic — screen behaviour, VBA, macros | 72 | 71 | 143 | 50.35% |

Objects with no blocking issue on any applicable aspect: 168 / 275 (61.09%).
There are zero extraction failures and zero semantic parse failures across both
artifacts.

### What blocks the rest

| Reason code | Objects | Aspect |
|---|---:|---|
| `VBA_INTERACTIVE_DIALOG` | 43 | application logic |
| `VBA_DRIVES_ACCESS_UI` | 33 | application logic |
| `USER_DEFINED_VBA_FUNCTION` | 31 | data logic |
| `VBA_ACCESS_FORM_REFERENCE` | 17 | application logic |
| `VBA_ACCESS_APPLICATION_OBJECT` | 14 | application logic |
| `ACCESS_RUNTIME_FUNCTION` | 13 | data logic |
| `VBA_FILE_SYSTEM_ACCESS` | 10 | application logic |
| `VBA_HOST_ENVIRONMENT` | 7 | application logic |
| `MACRO_ACTION_UI_NOT_TRANSLATABLE` | 5 | application logic |
| `FIELD_TYPE_HAS_NO_PORTABLE_EQUIVALENT` | 3 | data logic |
| `ROW_SOURCE_TYPE_NOT_A_QUERY` | 3 | application logic |
| `MACRO_ACTION_SYSTEM_NOT_TRANSLATABLE` | 3 | application logic |
| `UNKNOWN_FUNCTION` | 3 | data logic |
| `VBA_ACCESS_REPORT_REFERENCE` | 3 | application logic |
| `OBJECT_METHOD_CALL` | 1 | application logic |

Most of the remaining gap is not a converter defect. It is the honest shape of
the problem: roughly half of the application logic in a real Access system is
user-interface behaviour that a database cannot host, and it needs an
application layer in the target rather than a better translator.

## Current unavailable areas and reasons

| Area | Status | Reason code |
|---|---|---|
| Jet3/Jet4/ACE object catalog | `complete` | 1,247 objects discovered from binary files |
| Binary object definition extraction | `not_started` | `BINARY_OBJECT_DEFINITION_EXTRACTION_NOT_IMPLEMENTED` |
| ACCDT definition decoding | `complete` | 275/275 definitions decoded |
| Table schema, keys, indexes, defaults, validation | `complete` | For extracted definitions |
| Relationships and foreign keys | `complete` | 32 relationships across both artifacts |
| Saved-query translation to portable SQL | `partial` | Blocked only by Access/VBA functions inside expressions |
| Union/crosstab/pass-through query text | `partial` | `STORED_SQL_NOT_RE_PARSED` — text preserved exactly |
| Non-select query operation codes | `not_started` | `QUERY_OPERATION_CODE_NOT_MAPPED` — no verified sample yet |
| Form/report structure, bindings and event wiring | `complete` | For extracted definitions |
| Macro and data-macro action semantics | `partial` | UI/system actions need an application layer |
| VBA structure and control-flow graphs | `complete` | 377 procedures in the Developer Edition alone |
| VBA semantics as portable code | `not_started` | Only structure and effects are modelled |
| Table row data | `not_started` | Reported per input by the CLI |
| Target application generation | `not_started` | `TARGET_GENERATOR_NOT_IMPLEMENTED` |
| Batch conversion and resumable output | `not_started` | `PIPELINE_NOT_IMPLEMENTED` |

## Roadmap

1. **TASK-ACCESS-002 — complete:** canonical Access IR and coverage reporting.
2. **TASK-ACCESS-003 — complete:** direct ACCDT input, inert definition
   preservation, single-file CLI, explicit limitation reporting.
3. **TASK-ACCESS-004 — complete:** semantic translation of ACCDT definitions
   (tables, relationships, queries, forms, reports, macros, data macros, VBA),
   the per-aspect capability model, the flow model, the offline HTML viewer, and
   the Jet4/ACE catalog reader.
4. Binary object-definition extraction for `.accdb`/`.mdb`, starting with table
   schemas and saved-query SQL.
5. Table-data extraction with type and value fidelity reporting.
6. Verified operation codes for append/update/delete/make-table queries.
7. Target generation, batch CLI and resumable output.

## Update rule

Every future translation task must add tests for its covered sample objects and
update the generated coverage data. Reports must retain counts and reason codes
for partial, unsupported, failed, and not-applicable objects. A percentage may
increase only when the corresponding executable tests pass.
