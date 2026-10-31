# dsh install for the run image

`package.json` pins the top-level package `@deepseek-ai/dsh` at **0.1.5-rc.1**, the version every one of the paper's
59 runs reports (`dsh_version` in the ledger's `episode_start` event, and the install folder its sessions linked to).

The installed tree of the paper's runs was: top level 0.1.5-rc.1, about 230 `@deepseek-ai/*` sub-packages at
0.1.5-rc.2, and `@deepseek-ai/cordis` 4.0.2. The sub-packages of 0.1.5-rc.1 are declared with version ranges, so a
fresh install resolves them again on the day of the build and can pull newer releases (0.1.5-rc.3, for example,
changed only how the third-party dependencies are pinned). `overrides` keeps `cordis` at 4.0.2; the other
sub-packages are fixed only by a lock file.

No lock file is included, so a build installs the pinned top level and lets npm resolve the sub-packages on the
day of the build; their versions may differ from the tree of the paper's runs. The image records what it got:
`/opt/dsh/LOCK_STATUS` and the full tree in `/opt/dsh/npm-ls.txt`. If you place a `package-lock.json` in this
folder, the build uses it (`npm ci`) instead.

Checks done at build time: the installed top level is 0.1.5-rc.1, and `dsh` on PATH resolves to the package's
JavaScript entry point (the orchestrator's prompt-file launcher imports `runCli` from it). Two checks need the built
image: `ra preflight` (Node with built-in zstd, a zstd test frame decoded through the orchestrator) and one smoke run
whose dsh session logs are named `session.v3.jsonl.zstd`.
