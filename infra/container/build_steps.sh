#!/bin/sh
# Build steps of the Residual Agents images, shared by Dockerfile and apptainer.def so that the two cannot drift.
# Run as root inside the image being built, from a copy of the repository at /opt/ra:
#   build_steps.sh system | python | dsh | ra | judge
#   system  links npm/npx for the Node 24 copied in from the node image; installs zstd, poppler-utils, procps
#   python  the scientific stack of requirements-paper.txt (see VERSIONS.md for the evidence behind each pin)
#   dsh     the dsh agent CLI, pinned at 0.1.5-rc.1 in dsh/package.json. With a dsh/package-lock.json the tree is
#           installed with npm ci; without one, npm resolves the sub-packages at build time (/opt/dsh/LOCK_STATUS
#           says which case applies, /opt/dsh/npm-ls.txt records the tree)
#   ra      the repository as an editable install (the orchestrator's path, /opt/ra/residual_agents/orchestrator.py,
#           holds no "python", so an agent's `pkill -f python3` cannot match it); user "ra" (uid 1000)
#   judge   Claude Code for the judge image (CLAUDE_CODE_VERSION, default 2.1.281, the version of the paper's judge)
set -eu
RA=/opt/ra
C="$RA/infra/container"

phase_system() {
  [ -e /usr/local/bin/npm ] || ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm
  [ -e /usr/local/bin/npx ] || ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
  node -e 'const [a,b]=process.versions.node.split(".").map(Number); if (a<23||(a===23&&b<8)) { console.error("dsh needs Node >= 23.8 (built-in zstd), found "+process.versions.node); process.exit(1) }'
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates zstd poppler-utils procps
  rm -rf /var/lib/apt/lists/*
}

phase_python() {
  pip install --no-cache-dir -r "$C/requirements-paper.txt"
  pip freeze > /opt/pip-freeze.txt
}

phase_dsh() {
  mkdir -p /opt/dsh
  cp "$C/dsh/package.json" /opt/dsh/
  if [ -f "$C/dsh/package-lock.json" ]; then
    cp "$C/dsh/package-lock.json" /opt/dsh/
    (cd /opt/dsh && npm ci --omit=dev --no-audit --no-fund)
    echo "locked: installed with npm ci from package-lock.json" > /opt/dsh/LOCK_STATUS
  else
    (cd /opt/dsh && npm install --omit=dev --no-audit --no-fund)
    echo "unlocked: top level pinned at 0.1.5-rc.1; sub-package versions resolved at build time" > /opt/dsh/LOCK_STATUS
  fi
  (cd /opt/dsh && npm ls --all > /opt/dsh/npm-ls.txt 2>&1) || true
  ln -sf /opt/dsh/node_modules/.bin/dsh /usr/local/bin/dsh
  v="$(node -p 'require("/opt/dsh/node_modules/@deepseek-ai/dsh/package.json").version')"
  [ "$v" = "0.1.5-rc.1" ] || { echo "build_steps: dsh is $v, expected 0.1.5-rc.1" >&2; exit 1; }
  case "$(readlink -f /usr/local/bin/dsh)" in
    *.js) ;;
    *) echo "build_steps: dsh does not resolve to its JavaScript entry point; the prompt-file launcher needs it" >&2; exit 1 ;;
  esac
}

phase_ra() {
  pip install --no-cache-dir -e "$RA"
  mkdir -p /workspace /pece_store
  id ra >/dev/null 2>&1 || useradd --create-home --uid 1000 --shell /bin/bash ra
  chown ra:ra /workspace /pece_store
  chmod 0755 "$RA/infra/run/run_in_container.sh"
}

phase_judge() {
  npm install -g --no-audit --no-fund "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION:-2.1.281}"
  claude --version
}

case "${1:-}" in
  system) phase_system ;;
  python) phase_python ;;
  dsh) phase_dsh ;;
  ra) phase_ra ;;
  judge) phase_judge ;;
  *) echo "usage: build_steps.sh system|python|dsh|ra|judge" >&2; exit 2 ;;
esac
