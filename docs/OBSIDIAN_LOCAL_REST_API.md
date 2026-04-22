# Obsidian Local REST API (PSB + Hermes)

Used when Obsidian has the **Local REST API** plugin (e.g. coddingtonbear) and you want to **push notes from a script** instead of only writing markdown into the vault folder.

## Defaults

- **Base URL:** `https://127.0.0.1:27124` (HTTPS; plugin may use a self-signed cert).
- **Auth:** `Authorization: Bearer <API_KEY>` (API key from plugin settings).
- **TLS:** clients must allow insecure / self-signed localhost (e.g. `curl -k`, or Python `ssl` context).

## Environment (PSB `.env` — optional)

```bash
OBSIDIAN_REST_API_URL=https://127.0.0.1:27124
OBSIDIAN_API_KEY=<from Obsidian plugin>
```

## Common operations (plugin API)

Exact paths match your installed plugin version; confirm in the plugin’s **OpenAPI** or **Active endpoints** UI. Typical patterns:

- **Put a note:** `PUT {base}/vault/{path/to/Note.md}` with raw markdown body and Bearer header.
- **Append / patch:** use plugin-specific routes if provided.

If the API is **down** or keys are missing, **write the same markdown directly** into the Hermes vault path, e.g.:

`Hermes Second Brain/projects/psb/notes/...`

That is still “second brain” persistence when the vault folder is in the workspace.

## Hermes Second Brain layout (this operator)

Vault root: `Hermes Second Brain/`. PSB notes: `projects/psb/notes/`.

See repo note: `Hermes Second Brain/projects/psb/notes/2026-04-21-psb-agent-memory-correctness-bundle.md`.
