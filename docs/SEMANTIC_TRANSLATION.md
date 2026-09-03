# What is translated, and what is not

This document is the contract behind the numbers. It states, per Access
construct, what the converter turns into a target-neutral model and what it
refuses to guess at.

Nothing here involves running Access. Definitions are read as text or as
inert XML; expressions are parsed, never evaluated.

## The three aspects

A single yes/no verdict per object is useless to a migration team, because the
halves of an Access object go to different people. Every object is therefore
scored on the aspects that apply to it:

| Aspect | Question | Applies to |
|---|---|---|
| `structure` | Was the definition recovered and modelled at all? | every object |
| `data_logic` | Can it be expressed as data processing in the target? | tables, queries, forms, reports, data macros |
| `application_logic` | Does it depend on the Access UI, VBA or the host? | forms, reports, macros, modules |

`not_applicable` is a real verdict: a table has no application logic of its own,
so scoring it against that aspect would invent a denominator.

## Per construct

### Tables (`*.xsd`)

Translated: columns with Jet/ACE types mapped to portable SQL types, text
lengths, nullability, auto-numbers, primary keys, indexes, default-value
expressions, field validation rules (including the bare-comparison form, where
Access leaves the field name implicit), descriptions and captions. A
`CREATE TABLE` statement is generated.

Not translated: attachment, multi-valued and calculated fields, which ACE
stores as `complex` and keeps in hidden child tables. Reported as
`FIELD_TYPE_HAS_NO_PORTABLE_EQUIVALENT`. Datasheet appearance is read and
deliberately discarded.

### Relationships (`relationships.xml`)

Translated: every relationship becomes a foreign key with its columns,
referential-integrity flag and cascade options, rendered as `ALTER TABLE`.

### Queries

Translated: design-grid queries become SQL - input tables with aliases,
output columns with aliases, the join graph (inner, left and right outer),
`WHERE`, `GROUP BY`, `HAVING`, `ORDER BY`, `DISTINCT` and `TOP`.

Preserved but not re-derived: union, crosstab, data-definition and pass-through
queries, which Access already stores as SQL text
(`STORED_SQL_NOT_RE_PARSED`). Their text is kept exactly.

Refused: any `Operation` code this project has not verified against a real
definition (`QUERY_OPERATION_CODE_NOT_MAPPED`). Mislabelling an append query as
a select query would silently drop a write, so the code is reported rather than
guessed.

Also reported: a query whose tables are not fully connected by joins
(`IMPLICIT_CROSS_JOIN`), because the result is a Cartesian product and that is
almost always a defect worth seeing.

### Expressions

Access expressions are parsed into an AST and rendered as target-neutral SQL.
`&` becomes `||`, `IIf` becomes `CASE`, `Nz` becomes `COALESCE`, `IsNull(x)`
becomes `x IS NULL`, `Year(x)` becomes `EXTRACT`, VBA conversions become
`CAST`, and `Like` patterns have their `*`/`?` wildcards rewritten.

Reported rather than faked:

- `ACCESS_RUNTIME_FUNCTION` - `Format`, `DLookUp`, `DateDiff` and friends have
  no portable SQL equivalent.
- `USER_DEFINED_VBA_FUNCTION` - the expression calls this database's own VBA;
  it cannot run in the target until that function is ported.
- `ACCESS_OBJECT_MODEL_REFERENCE` - `CurrentProject`, `TempVars`, `DoCmd`.
- `UI_REFERENCE_BECOMES_PARAMETER` (advisory) - `[Forms]![f]![c]` is not a
  column; it is emitted as a bind parameter the application must supply.

### Forms and reports

Translated: record source, sections, the full control tree with geometry,
control sources (including calculated ones), row sources, subform links, and
every event property with the VBA procedure or macro it is bound to. The
code-behind module is split off and analysed like any other VBA module.

Not translated: ActiveX/OLE/browser controls
(`CONTROL_TYPE_HAS_NO_PORTABLE_EQUIVALENT`), value-list and callback row
sources (`ROW_SOURCE_TYPE_NOT_A_QUERY`), and embedded macros whose body lives
in the object's binary properties rather than in the text definition.

### Macros and data macros

The modern AXL representation is used whenever it exists, because a flat
action list cannot express an `Else` branch. Data macros become trigger-shaped
IR: event, timing, operation, and a nested statement tree.

Actions are classified, not merely listed:

| Category | Meaning |
|---|---|
| `data` | expressible as data manipulation in the target |
| `control` | control flow this converter models directly |
| `ui` | drives the Access user interface; needs an application layer |
| `system` | runs code, moves files, or changes Access itself |

A macro that survives only as the legacy row-per-action list is reported as
`MACRO_STORED_AS_FLAT_ACTION_LIST` and never claimed as complete.

### VBA

Translated: procedures with their signatures, scope and parameters; the nested
statement structure (`If`/`ElseIf`/`Else`, `Select Case`, `For`, `For Each`,
`Do`/`Loop`, `While`, `With`, `On Error`, labels, `GoTo`, `Resume`, `Exit`); a
control-flow graph per procedure including the edge from `On Error GoTo` to its
handler; call relationships; `DoCmd` actions with the objects they open; and
SQL string literals with the tables they touch.

Line numbers (`10`, `20`, ... as used throughout Northwind) are recognised as
line numbers, not as statements.

Reported as application logic that a database cannot host: `DoCmd`, `MsgBox`,
`Shell`, `CreateObject`, file I/O, `Environ`, and direct references to loaded
forms and reports. A module that only computes is complete; a module that
drives the UI is partial, and the reason says which line of thinking has to
move to an application layer.

### Binary databases (`.mdb`, `.accdb`)

The `MSysObjects` catalog is read directly from the pages: every object's name
and kind, including linked tables and the hidden `~sq_` queries Access
materialises from form and report record sources.

Nothing else. Object definitions live in binary structures this converter does
not decode, so every object carries
`BINARY_OBJECT_DEFINITION_EXTRACTION_NOT_IMPLEMENTED` and no semantic verdict
at all. The catalog reader's counts are verified against the independently
produced inventories under `samples/*/object_inventory.json`.

## Advisory codes

These do not block a translation, but a migration team has to decide about
them:

`LIKE_WILDCARD_DIALECT`, `UI_REFERENCE_BECOMES_PARAMETER`,
`TOP_CLAUSE_IS_DIALECT_SPECIFIC`, `INT_FIX_ROUNDING`,
`NZ_DEFAULT_DEPENDS_ON_TYPE`, `INTEGER_DIVISION_SEMANTICS`.

## The rule this file exists to enforce

Every reason code is assigned to exactly one aspect in
`converter/semantics/_capability.py`. A code with no assignment is reported as
`unclassified` rather than being folded into a bucket, and a test over the whole
sample corpus fails if any emitted code is unclassified. That is what stops a
new construct from quietly inflating a percentage.
