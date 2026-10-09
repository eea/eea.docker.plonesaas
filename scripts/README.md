# EPANET migration scripts

Offline alternative to `@@export_epanet`
([eea/collective.exportimport@epanet](https://github.com/eea/collective.exportimport/tree/epanet)):
take a **plain** `collective.exportimport` export of
`epanet.eea.europa.eu` (classic Plone 5.2) and turn it into **one** JSON file
that is imported into the EEA website subsite `/en/epanet` (Volto).

Use these scripts when the EPANET site runs the upstream
`collective.exportimport` (no `@@export_epanet` view). Both routes produce the
same kind of file; use one, not both.

All scripts are Python 3.9+ and use the standard library only (no `pip
install`). Run them **from the repository root**.

## Before you start

1. **Blocks converter** running on `http://localhost:8000/toblocks`, built
   from the `extract-inline-images` branch of
   [volto-blocks-converter](https://github.com/eea/volto-blocks-converter/pull/13)
   (inline images, multi-row layout tables). For example:

   The `volto-blocks-converter` service in `docker-compose.yml` uses the
   untagged image, which may predate that branch; run the branch instead:

   ```bash
   # from a checkout of volto-blocks-converter on extract-inline-images
   .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000

   curl -s -X POST -H 'Content-Type: application/json' \
        -d '{"html": "<p>ping</p>"}' http://localhost:8000/toblocks
   ```

2. **The subsite exists** on the target at `/en/epanet` (type `Subsite`),
   created by hand. The import only updates it.

3. **A raw export** of EPANET, made with the standard export view:
   `https://epanet.eea.europa.eu/@@export_content` as a Manager, with
   - types: Folder, Document, News Item, Collection, File, Image, Link
   - *Include blobs*: **as base-64 encoded strings**
   - *Modify exported data for migrations*: checked
   - *Download to local machine* → save it as e.g. `fresh-test/epanet.json`

## Quick path: one command

`build_epanet_import.py` runs every step below in order and writes a single
import file:

```bash
python3 scripts/build_epanet_import.py --input fresh-test/epanet.json
# -> content-exports/epanet-import/epanet-import.json
```

Common options:

```bash
python3 scripts/build_epanet_import.py --input fresh-test/epanet.json \
    --output-dir fresh-test/run \
    --target-root https://demo-www.eea.europa.eu/en/epanet \
    --parent-uid 4b5a784a7bd543b39d8a4feb2ab8a4d7 \
    --split
```

| Option | Default | Meaning |
|---|---|---|
| `--input` | (required) | Raw export JSON |
| `--output-dir` | `content-exports/epanet-import` | Where every file is written |
| `--target-root` | `http://localhost:8080/Plone/en/epanet` | Target subsite URL (only its path matters on import) |
| `--parent-uid` | looked up from the target | UID of `/en` on the target; `4b5a784a7bd543b39d8a4feb2ab8a4d7` on www and demo-www |
| `--converter-url` | `http://localhost:8000/toblocks` | Blocks converter endpoint |
| `--skip-validate` | off | Skip step 1 |
| `--skip-align` | off | Skip step 3 (it reads the production EPANET site) |
| `--split` | off | Also write `epanet-import-chunks/` for uploads over the request size limit |

The runner only *reads* the target (to look up the parent UID) and the
production site (step 3); it never writes to either. It stops with exit code
1 if the converter is down, a step fails, or a final check fails.

Output directory:

| File | From |
|---|---|
| `VALIDATION_REPORT.md` | step 1 |
| `epanet-transformed.json`, `collections.json`, `TRANSFORM_REPORT.md` | step 2 |
| `epanet-transformed-fixed.json` | step 3 |
| `epanet-transformed-fixed-teasers.json` | step 4 |
| **`epanet-import.json`** | steps 5–7, **the file to import** |
| `epanet-import-chunks/` | `--split` only |

Steps 5–7 exist only inside the runner: the front page's blocks become the
`Subsite` item (first in the file, parent `/en` by UID), `review_state` is
restored from the export (except published items without an effective date,
so no date is changed), listing path criteria become site paths, Slate
elements without text are removed, and the result is checked (counts, parent
order, blobs, Slate validity).

## The scripts one by one

Run them individually only to debug a step. Paths below use `fresh-test/`;
each script's defaults point at `content-exports/`.

### 1. `validate_export.py` — check the raw export

Compares type counts with the known EPANET inventory (2026-09-10) and lists
languages, review states, private and workflow-less items, Collections and the
largest blobs. Count differences are reported, not fatal.

```bash
python3 scripts/validate_export.py --input fresh-test/epanet.json
# -> fresh-test/VALIDATION_REPORT.md   (or --report <path>)
```

### 2. `epanet_migrate.py` — transform to Volto content

Type mapping (Folder/Collection → Document), `en-gb` → `en`, URLs moved below
the target subsite, HTML → blocks via the converter, News Item header blocks
from `content-exports/press-release-template.json`, Collections → listing
blocks, private items skipped.

```bash
python3 scripts/epanet_migrate.py \
    --input fresh-test/epanet.json \
    --output fresh-test/epanet-transformed.json \
    --collections-output fresh-test/collections.json \
    --report-output fresh-test/TRANSFORM_REPORT.md \
    --target-root http://localhost:8080/Plone/en/epanet
# options: --converter-url, --old-root, --template, --include-private,
#          --skip-collections, -v
```

### 3. `fix_news_image_alignment.py` — image float (left/right)

Sets each image block's `align` from the production page
(`https://epanet.eea.europa.eu`). The runner uses JSON mode and also maps
`/resolveuid/<uid>` image URLs to file names first; run on its own, that
mapping is not applied, so prefer the runner for this step.

```bash
# JSON mode: export in, fixed export out
python3 scripts/fix_news_image_alignment.py \
    --input-json fresh-test/epanet-transformed.json \
    --output-json fresh-test/epanet-transformed-fixed.json --dry-run

# API mode: patch already-imported News Items on a running site
python3 scripts/fix_news_image_alignment.py \
    --local-base http://localhost:8080/Plone \
    --local-path /en/epanet/reports-letters/plenary-meetings \
    --user admin --password admin --dry-run
```

Drop `--dry-run` to write.

### 4. `build_teaser_grid.py` — member logos on `/our-group`

Replaces the member logo table with a `group` of 4-column `teaserGrid` blocks
(image, agency name, agency link).

```bash
python3 scripts/build_teaser_grid.py \
    --input fresh-test/epanet-transformed-fixed.json \
    --output fresh-test/epanet-transformed-fixed-teasers.json
# options: --page /en/epanet/our-group, --dry-run, --verbose
```

### 5. `split_transformed_json.py` — split a large file (optional)

`@@import_content` uploads the file in one request; a file of about 180 MB
can hit "413 Request Entity Too Large". This splits it into chunks, keeping
parents before children. Import the chunks in order with the same settings.

```bash
python3 scripts/split_transformed_json.py \
    content-exports/epanet-import/epanet-import.json \
    --output-dir content-exports/epanet-import/epanet-import-chunks \
    --max-size 10485760
```

## Import

1. Open `<site>/en/epanet/@@import_content` as a Manager
   (local: `http://localhost:8080/Plone/en/epanet/@@import_content`).
   Without a parent UID in the file, import at the site root instead
   (`<site>/@@import_content`).
2. Upload `epanet-import.json` (or the chunks, in order).
3. *Handle existing content*: **Update: Reuse and only overwrite imported
   data** (`handle_existing_content=2`). Never *Replace*: it deletes and
   recreates the subsite.
4. Afterwards run `updateRoleMappings` (portal_workflow) and reindex
   `allowedRolesAndUsers`.

Only `/en/epanet` is touched; nothing outside the subsite is created or
changed.
