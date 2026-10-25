#!/bin/sh
# Rebuild and restart if the deploy branch has moved on GitHub. Meant for cron every few minutes;
# `deploy/update.sh` runs it straight away. DEPLOY_BRANCH (default main) picks the branch.
set -e
cd "$(dirname "$0")/.."
branch=${DEPLOY_BRANCH:-main}
exec 9>/tmp/fpv-spotter-deploy.lock
flock -n 9 || exit 0
git fetch -q origin "$branch"
new=$(git rev-parse "origin/$branch")
[ "$new" = "$(git rev-parse HEAD)" ] && [ "${1:-}" != "--force" ] && exit 0
echo "$(date -Is) deploying $branch $(git rev-parse --short "$new")"
git reset -q --hard "$new"
# made here so they belong to tnv: docker would create a missing bind-mount folder as root
mkdir -p "${SPOTTER_DATA_DIR:-../fpv-spot-finder-data}/mine" "${SPOTTER_PIPELINE_DIR:-../fpv-spot-finder-data/pipeline}"
docker compose -f deploy/docker-compose.yml up -d --build --remove-orphans
docker image prune -f --filter label=app=fpv-spotter >/dev/null
echo "$(date -Is) done"
