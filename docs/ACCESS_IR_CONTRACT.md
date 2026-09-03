# Access IR source-identity contract

This document defines how `access-ir/1` derives stable artifact identity from
the local sample corpus. It is a project corpus convention, not a general
Microsoft Access rule.

## Binary Jet/ACE inventories

For an entry in the inventory's `databases` list, the required `file` field is
the artifact name. The source identity combines the sample-relative inventory
path and that exact field value. Missing `counts` or `objects` is invalid;
explicit `counts: {}` and `objects: []` are valid when they are consistent.

## ACCDT inventories

The current ACCDT inventory layout has no authoritative artifact filename
field. The loader therefore enumerates `.accdt` files beneath that sample
directory using metadata only. It never opens an ACCDT file.

Selection is deterministic and fail-closed:

1. A candidate whose first sample-relative path component is `original`
   (case-insensitive) is authoritative over archival or working copies.
2. Exactly one candidate under `original/` is required to apply that
   precedence. Two or more candidates there are ambiguous and invalid.
3. If there is no candidate under `original/`, exactly one candidate may exist
   anywhere in the sample directory.
4. No candidate, or multiple non-authoritative candidates, is invalid.

The selected path is converted only to POSIX separators and is otherwise
preserved exactly in the artifact's `source_identity`. Candidate paths are
never collapsed by basename. `artifact_name` may contain the basename for
display, but it is not the uniqueness key.

Changing this selection rule or supplying an authoritative filename in future
inventory metadata requires an explicit contract and test update. Ambiguous
metadata must raise `AccessIRValidationError`; the loader must never guess.

## Direct ACCDT input

`translate_access_file()` uses a different identity boundary because the user
provides one concrete file rather than a sample directory. Its stable source
identity is the exact input filename plus file size and SHA-256 content digest;
it never contains a machine-specific absolute path or a timestamp.

Object identity is derived from the exact, preserved package part path. The
known ACCDT package prefix (`table`, `query`, `form`, `report`, `macro`,
`module`, or `datamacros`) is removed only for the display name, and the result
records `name_provenance: accdt_package_part_prefix`. The part path remains the
authoritative identity evidence in `content.source_part`.
