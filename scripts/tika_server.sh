#!/usr/bin/env bash
# Start Apache Tika in server mode on the port rescan expects.
set -euo pipefail
JAR="${TIKA_JAR:-$(dirname "$0")/../vendor/tika-server.jar}"
PORT="${TIKA_PORT:-9998}"
if [[ ! -f "$JAR" ]]; then
  echo "Tika jar not found at $JAR" >&2
  echo "Fetch it with: curl -L -o vendor/tika-server.jar \\" >&2
  echo "  https://repo1.maven.org/maven2/org/apache/tika/tika-server-standard/2.9.2/tika-server-standard-2.9.2.jar" >&2
  exit 1
fi
exec java -jar "$JAR" --host 0.0.0.0 --port "$PORT"
