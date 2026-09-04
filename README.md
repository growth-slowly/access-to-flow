# access2flow

**access-to-flow** reads Microsoft Access applications completely offline,
translates them into an intermediate representation (IR) that can be carried
forward to a target database, and clearly distinguishes what can and cannot be
translated.

It never launches Access, COM, or PowerShell, and it never executes SQL, VBA,
or macros. It also makes no network connections. This is a deliberate design
choice because Access files often contain sensitive information.

---

## What it does

| Input | Scope read | Output |
|---|---|---|
| `.accdt` | All object definitions in the package | Semantic translations: table DDL, query SQL, screen and event structure, macro/data-macro steps, and VBA control flow |
| `.accdb` (ACE 12/14/16/17) | `MSysObjects` only | Object inventory (name and type). **This is not a full translation.** |
| `.mdb` (Jet 4.0 / Jet 3.x) | `MSysObjects` only | Object inventory (name and type). **This is not a full translation.** |

All 21 collected sample files can be read by a direct reader. Full definitions
are currently extracted for 275 `.accdt` objects. See
[docs/TRANSLATION_PROGRESS.md](docs/TRANSLATION_PROGRESS.md) for the detailed
breakdown.

## Two ways to run it

|  | Offline (CLI) | Browser (hosted) |
|---|---|---|
| Where the file stays | never leaves the machine | uploaded to the server |
| Formats | `.accdt`, `.accdb`, `.mdb` | `.accdt` only |
| Confidential data | no constraint | needs a decision — see below |
| Distribution | anywhere Python runs | open a URL |

Use the offline CLI for a database that must not leave the building. The
hosted version analyses an upload **in the server's memory only** — it is never
written to disk, and neither the file name nor any of its contents reaches a
log — but sending the file over a network is still more exposure than not
sending it, and that fact does not go away because the handling is careful.

## Usage (offline)

```powershell
# Translate an Access template and write JSON plus an offline HTML viewer.
python -m converter translate "input.accdt" --output result.json --html result.html

# Regenerate only the HTML viewer from existing JSON.
python -m converter ui result.json --output result.html

# Verify the sample corpus and update the progress snapshot.
python tools/verify_corpus.py --json docs/translation_progress.json
```

The generated HTML is a self-contained single file. It uses no CDN, web font,
or external image, so it can be opened by double-clicking it on an offline
machine.

For detailed instructions, see
[docs/OFFLINE_CONVERTER_USAGE.md](docs/OFFLINE_CONVERTER_USAGE.md).

## Usage (browser)

```powershell
python -m converter.web --host 127.0.0.1 --port 8080
```

Open http://127.0.0.1:8080/ and drop a `.accdt` file onto the page. The
language selector in the top right switches between **日本語 / English / 中文**.

Publishing it is one container with no dependencies to install:

```bash
gcloud run deploy access-to-flow --source . --region asia-northeast1 \
  --memory 1Gi --concurrency 4 --max-instances 3
```

Cloud Run scales to zero, so a low-traffic deployment stays inside the free
tier. Deployment steps, cost estimates, and a hardening checklist to work
through before handling real customer data are in
[docs/WEB_DEPLOYMENT.md](docs/WEB_DEPLOYMENT.md).

## Viewer features

- **Overall summary** — Translation status by structural, data-processing,
  and application-processing concern; counts by object type; and reason codes
  with concrete examples for unsupported parts.
- **Objects** — A per-object overview, generated SQL/DDL, a layout summary,
  event-to-action mappings, unsupported sections, and original definition text.
- **Flowcharts** — Visual control-flow diagrams for VBA procedures, macros,
  and data macros. Branches, loops, error handling, and UI operations are
  color-coded; diagrams support zooming and panning.
- **System relationship graph** — Links such as form to query to table, and
  button to procedure to screen.
- **Three languages** — Japanese, English and Chinese, switchable at any time
  and remembered. The intermediate representation carries no prose in any one
  language: flow-chart wording travels as keys and reasons travel as codes, so
  a new language is a new catalogue and nothing else. Reason codes themselves
  stay in English in every locale, because they are identifiers shared by the
  JSON, the documentation and bug reports.

## How translation status is reported

One pass/fail value is not useful. A form layout and data binding may translate
completely, while a button calling `DoCmd.OpenForm` still requires application
logic in the target system. Those concerns may also be handled by different
people.

Each object is therefore assessed from three perspectives.

| Perspective | Question |
|---|---|
| Structure | Can the definition itself be read and modelled? |
| Data processing | Can it be represented as target-side data logic, such as SQL? |
| Application processing | Does it require another layer, such as UI operations, VBA, or macros? |

Every untranslated section has a reason code and an assigned perspective.
Unclassified reason codes are reported as `unclassified` and cause the
corpus-wide tests to fail. New syntax cannot silently inflate coverage.

See [docs/SEMANTIC_TRANSLATION.md](docs/SEMANTIC_TRANSLATION.md) for the full
mapping of supported syntax and known non-translations.

## Project layout

```
converter/
├─ __main__.py        Command entry point (translate / ui)
├─ access/
│  ├─ translation.py  Entry point for ACCDT packages and binary inputs
│  ├─ jet_catalog.py  Jet 3 (Access 97) catalog reader
│  └─ ace_catalog.py  Jet 4 / ACE catalog reader
├─ semantics/         Semantic translation: expressions, queries, tables, UI, macros, and VBA
├─ ir/                Intermediate-representation vocabulary, coverage, and errors
├─ flow/              Flow model for relationship graphs and process flows
├─ ui/                The viewer: single-file HTML output and the ja/en/zh catalogues
├─ web/               The hosted service: no web framework, no dependencies
└─ utils/             Shared utilities, including identifier normalization

samples/open_access_systems/   Open Access systems used for validation
docs/                          Progress and contract documentation
tools/                         Corpus validation and test tools
```

## Tests

```powershell
python -m pytest tests
python tools/run_unittests.py     # Standard library only; pytest is not required.
```

`tools/run_unittests.py` works in an environment with no third-party packages,
matching the constraint imposed on the product itself.

## Requirements

Python 3.10 or later. Standard library only; no installation is required. The
hosted service is the same — no web framework — so the answer to "what code
runs on the machine that touches our database" stays short enough to read.
