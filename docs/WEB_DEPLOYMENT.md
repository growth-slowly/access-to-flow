# Running the browser version

The hosted converter is one container with no dependencies: the whole service
is the Python standard library. That is a deliberate choice, not a constraint
worked around — when a customer asks what code touches their database on your
server, the answer should be short enough to read.

## What it does and does not do

| | |
|---|---|
| Accepts | `.accdt` files, up to a configured size |
| Runs | in memory only — the upload is never written to disk |
| Logs | method, path, status, byte count, duration |
| Never logs | the file name, any object name, any content |
| Returns | the conversion result as JSON; the viewer renders it in the browser |
| Never serves | the converter's own source |

Binary `.mdb` / `.accdb` files are refused by the web endpoint. Reading them
needs page-level seeks in a real file, and spooling an upload to disk is
exactly what this service exists to avoid. Those go through the offline CLI.

## Run it locally

```powershell
python -m converter.web --host 127.0.0.1 --port 8080
```

Then open http://127.0.0.1:8080/.

## Configuration

All optional; the defaults are the ones in the table.

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `8080` | Port to listen on |
| `ACCESS_CONVERTER_MAX_UPLOAD_MB` | `48` | Largest accepted upload |
| `ACCESS_CONVERTER_RATE_PER_MINUTE` | `10` | Conversions per client address per minute |
| `ACCESS_CONVERTER_INCLUDE_SOURCE` | `1` | Include the original definition text in the response |
| `ACCESS_CONVERTER_TOKEN` | *(unset)* | If set, every conversion needs this shared secret |

Set `ACCESS_CONVERTER_TOKEN` for anything that is not a public demo. The
browser sends it as `X-Access-Token`; the API also accepts
`Authorization: Bearer <token>`.

## Deploying to Google Cloud Run (recommended)

Cloud Run scales to zero, so an idle service costs nothing, and it terminates
TLS and gives you a certificate without any work.

```bash
gcloud run deploy access-converter \
  --source . \
  --region asia-northeast1 \
  --allow-unauthenticated \
  --memory 1Gi \
  --cpu 1 \
  --concurrency 4 \
  --max-instances 3 \
  --timeout 120 \
  --set-env-vars ACCESS_CONVERTER_TOKEN=<a long random string>
```

Why those numbers:

- **1 GiB** — a 48 MB upload plus its parsed model needs headroom. Halve the
  upload limit if you want to run at 512 MiB.
- **concurrency 4** — conversions are CPU- and memory-bound; letting 80
  requests share one instance is how you get an out-of-memory kill.
- **max-instances 3** — an upper bound on the bill. Raise it deliberately.
- **timeout 120** — a conversion of a large template takes seconds, not
  minutes. A long timeout only helps an attacker.

## What it costs

Cloud Run's monthly free tier is 2 million requests, 180,000 vCPU-seconds and
360,000 GiB-seconds. One conversion of the Northwind Developer Edition
(12 MB, 187 objects) takes roughly 0.9 vCPU-seconds and about 1 GiB-second.

| Monthly conversions | vCPU-seconds | GiB-seconds | Cost |
|---:|---:|---:|---|
| 1,000 | ~900 | ~1,000 | free tier |
| 20,000 | ~18,000 | ~20,000 | free tier |
| 200,000 | ~180,000 | ~200,000 | around the free-tier edge |

In practice a demo or an internal tool sits inside the free tier and the bill
is the domain name. Idle time costs nothing because the service scales to zero;
the price of that is a cold start of one to three seconds on the first request
after an idle period.

### Other hosts

| Host | Cost | Trade-off |
|---|---|---|
| **Cloud Run** | ~free at low volume | Cold start after idle |
| **Fly.io** (shared-cpu-1x, 512 MB) | a few USD per month | Always warm, no cold start |
| **Render free tier** | free | Sleeps after 15 minutes; cold start ~30 s |
| **A small VPS** | ~5 USD per month | You own the TLS certificate and the patching |

Cloudflare Workers and similar edge runtimes are not suitable: the converter
needs `zipfile`, `xml.etree` and the rest of the standard library.

## Hardening checklist before real customer data

The service is written to be defensible, but deployment decides most of it.

- [ ] Set `ACCESS_CONVERTER_TOKEN`, or put the service behind your identity
      provider (Cloud Run supports IAP and `--no-allow-unauthenticated`).
- [ ] Serve over HTTPS only. Cloud Run does this for you; a VPS does not.
- [ ] Run the container read-only: `--read-only` on Docker, or the equivalent
      on your platform. The service never writes a file, so nothing breaks.
- [ ] Turn off request-body logging in any proxy or WAF you put in front. A
      logged body defeats every guarantee this document makes.
- [ ] Decide your retention answer before a customer asks. Today it is "we
      keep nothing"; a caching layer added later would quietly change that.
- [ ] Tell customers the offline CLI exists. For a database that must not
      leave the building, it produces the identical result.

## What this service still cannot promise

The file travels over the network and is decompressed in a process you
operate. That is strictly more exposure than never uploading it at all. The
service minimises the window — memory only, no logs, no retention — but it
cannot eliminate it, and a customer whose policy forbids the upload is right
to use the offline converter instead.
