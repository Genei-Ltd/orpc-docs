# oRPC v2 documentation snapshot

A documentation mirror of [oRPC v2](https://orpc.dev/).

Start with [llms.txt](llms.txt), the canonical index. Every link points directly to a local Markdown page.

Includes the v2 guides, RPC and OpenAPI documentation, contracts, clients, adapters, plugins, integrations, recipes, and migration guides under `docs/`. The separate v1 documentation site and blog are excluded. The v2 migration guide contains v1 examples for comparison.

Pages retain their upstream paths and content. Links, embedded components, and external API references may still require the upstream website. [snapshot.json](snapshot.json) records the sync time, source commit used for the inventory, source URLs, and file checksums. The live website exports are not claimed to match that source commit byte for byte.

## Use with skills

Skills use `~/.coloop-skills/docs/orpc.dev`. Clone this repository there when the directory is missing:

```sh
orpc_docs_root="${HOME}/.coloop-skills/docs/orpc.dev"
if [ ! -e "$orpc_docs_root" ]; then
  mkdir -p "${orpc_docs_root%/*}"
  git clone --depth 1 https://github.com/Genei-Ltd/orpc-docs.git "$orpc_docs_root"
fi
```

## Sync with the upstream documentation

Run from the repository root with Python 3.10 or newer on macOS or Linux. No additional packages are required.

```sh
python3 scripts/sync.py
```

The script combines the upstream `llms.txt`, sitemap, and Markdown/MDX inventory under `apps/content/docs/` in [middleapi/orpc](https://github.com/middleapi/orpc). It downloads each page's native Markdown export. Source Markdown/MDX is used for an unindexed page if its public export returns 404, and saved with a `.md` extension.

Downloads are validated before the snapshot is updated. The script removes obsolete managed files, preserves unmanaged files, and stops if managed files have local changes or a download fails. It generates the local `llms.txt`; the upstream index is kept under `.snapshot/upstream/`, outside Git.

The script updates this checkout without committing or pushing. Maintainers can review the diff, commit, and push the refreshed snapshot. Existing skill caches can receive published updates with `git pull --ff-only` from a clean checkout, or run the sync script directly.

Run the sync integration checks with:

```sh
python3 -m unittest discover -s scripts -p 'test_*.py'
```

Original documentation is by the oRPC project and its contributors. Upstream licensing information is preserved in [LICENSE](LICENSE).
