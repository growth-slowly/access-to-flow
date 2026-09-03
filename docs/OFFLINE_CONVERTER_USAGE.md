# Offline Access converter usage

The converter reads an Access file and writes a JSON intermediate document plus,
optionally, a self-contained HTML viewer. It never starts Microsoft Access, COM,
PowerShell, an AI service, or a network client, and it never executes a query, a
macro, a data macro, VBA, or a startup form.

## Convert one file

```powershell
python -m converter translate "input.accdt" --output result.json
```

Add `--html` to also produce the offline viewer:

```powershell
python -m converter translate "input.accdt" --output result.json --html result.html
```

The HTML file is fully self-contained: no CDN, no web font, no image request.
Double-clicking it opens the whole report in a browser on a machine with no
network connection at all.

## What the result contains

| Key | Meaning |
|---|---|
| `ir` | The canonical `access-ir/1` object model, with each object's `semantics` |
| `semantics` | Per-aspect verdicts, per-kind rollups, and the reason-code index |
| `coverage` | Exact status counts and percentages per pipeline stage |
| `completion_summary` | The short numeric headline |
| `unprocessed_features` | Parts of the input this converter has not touched |
| `diagnostics` | Concrete errors, if any |

## Options

| Option | Effect |
|---|---|
| `--omit-source-text` | Keep hashes and metadata in the JSON, drop raw definition text |
| `--html PATH` | Also write the offline viewer |
| `--ui-omit-source` | Leave raw definition text out of the viewer (much smaller file) |

The viewer is trilingual - Japanese, English and Chinese - and remembers the
reader's choice. Reason codes stay in English in every language, because they
are identifiers shared with the JSON and this documentation.

## Render a viewer from an existing result

```powershell
python -m converter ui result.json --output result.html
```

## What each input format yields today

| Input | What is read | What you get |
|---|---|---|
| `.accdt` | Every object definition in the package | Full semantic translation: table DDL, query SQL, screen and event model, macro and data-macro statement trees, VBA control-flow graphs |
| `.accdb` (ACE 12/14/16/17) | `MSysObjects` only | A complete object inventory - names and kinds. **Not** a translation |
| `.mdb` (Jet 4.0) | `MSysObjects` only | The same object inventory |
| `.mdb` (Jet 3.x, Access 95/97) | `MSysObjects` only | The same object inventory |

For a binary database every object leaves the converter with
`extraction: not_started` and the reason code
`BINARY_OBJECT_DEFINITION_EXTRACTION_NOT_IMPLEMENTED`. The viewer says so in a
red banner on its front page, because an object list is a stock-take, not a
migration.

## Meaning of a partial result

`status: partial` is the expected successful result today. Read the per-aspect
numbers rather than a single percentage:

- **structure** - was the definition itself recovered and modelled?
- **data logic** - can it be expressed as data processing in the target?
- **application logic** - screen behaviour, VBA and macros, which need an
  application layer in the target system rather than a database feature.

A form whose layout and data binding translate perfectly but whose button calls
`DoCmd.OpenForm` is complete on the first two aspects and partial on the third.
Collapsing that into one number would hide where the work actually is.

## The browser version

The same viewer is also served as a web application, where a `.accdt` file is
uploaded and converted in the server's memory:

```powershell
python -m converter.web --host 127.0.0.1 --port 8080
```

Its guarantees, configuration and deployment notes are in
[WEB_DEPLOYMENT.md](WEB_DEPLOYMENT.md). Binary `.mdb`/`.accdb` files are
refused there on purpose: reading them needs a real file on disk, and spooling
an upload is exactly what that service avoids. They go through this CLI.

## Verify the whole sample corpus

```powershell
python tools/verify_corpus.py --json docs/translation_progress.json
python tools/verify_corpus.py --html-dir build/ui --ui-omit-source
```

## Run the tests

```powershell
python -m pytest tests
python tools/run_unittests.py          # standard library only, no pytest needed
```

Keep original Access files read-only and write output to a separate directory.
Nothing under `samples/` is ever written by the converter or by these tools.
