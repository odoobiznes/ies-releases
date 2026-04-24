# ies-releases

Release-index meta-repo for **IT Enterprise** services. Clients poll `index.json` for new versions.

See [IES_RELEASE_PIPELINE design](https://github.com/odoobiznes/.github) for architecture.

## What to put here

Only two files:

- `index.json` — machine-readable service → version mapping
- `README.md` — this file

Per-service code + release tarballs live in each service's own repo under `odoobiznes/<service>`.

## Format

```json
{
  "generated_at": "2026-04-24T14:30:00Z",
  "services": {
    "pohoda-digi":       { "stable": "0.1.0" },
    "pohoda-api":        { "stable": "1.1.0" },
    "pohoda-xml-agent":  { "stable": "0.9.5" },
    "pohoda-kontrola":   { "stable": "1.0.8" },
    "forms-doks":        { "stable": "1.0.0" },
    "iesocr-worker":     { "stable": "0.1.0" },
    "ies-agent-manager": { "stable": "0.5.0" }
  }
}
```

Updated automatically by `S:\Pohoda_APPS\make_release.sh`.
