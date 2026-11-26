#!/usr/bin/env bash
# Starts the local dev stack: LiveKit (4070, media 4071), the lecturer agent, and
# the web app (4072) if it is installed. Ctrl-C stops all of them.
#
#   dev/stack.sh            everything
#   dev/stack.sh --no-web   LiveKit and the lecturer only
set -euo pipefail
cd "$(dirname "$0")/.."

# Lessons are looked for in lessons/, then in the test lessons the checks use.
export CH_LESSONS_DIR="${CH_LESSONS_DIR:-$PWD/lessons:$PWD/dev/checks/lessons}"

web=1
[ "${1:-}" = "--no-web" ] && web=0
[ -d web/node_modules ] || web=0

ports=(4070 4071)
[ $web = 1 ] && ports+=(4072)
for port in "${ports[@]}"; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "port $port is already in use ($(lsof -nP -iTCP:"$port" -sTCP:LISTEN | awk 'NR==2{print $1, $2}')). Is the stack already running?" >&2
    exit 1
  fi
done

pids=()
# Each server is started with its output sent through sed, not piped into it, so
# that $! is the server's own pid and stopping the stack really stops the servers.
cleanup() { kill "${pids[@]}" 2>/dev/null || true; pkill -TERM -P $$ 2>/dev/null || true; wait 2>/dev/null || true; }
trap cleanup EXIT INT TERM

livekit-server --dev --bind 127.0.0.1 --config dev/livekit.yaml > >(sed -u 's/^/[livekit] /') 2>&1 &
pids+=($!)
for _ in $(seq 50); do
  curl -fsS http://127.0.0.1:4070 >/dev/null 2>&1 && break
  sleep 0.2
done

(cd agent && exec uv run python -m lecturer.agent dev --no-reload) > >(sed -u 's/^/[lecturer] /') 2>&1 &
pids+=($!)

if [ $web = 1 ]; then
  # Open rooms (any room name, the lesson from the link): this is a development stack.
  # Listening on this machine only.
  (cd web && CH_OPEN_ROOMS=1 exec pnpm dev --port 4072 --hostname 127.0.0.1) > >(sed -u 's/^/[web] /') 2>&1 &
  pids+=($!)
fi

echo "[stack] LiveKit ws://127.0.0.1:4070, lecturer agent$([ $web = 1 ] && echo ', web http://localhost:4072') (Ctrl-C to stop)"
wait
