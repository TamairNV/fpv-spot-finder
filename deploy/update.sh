#!/bin/sh
# Pull the deploy branch and rebuild now.
exec "$(dirname "$0")/autodeploy.sh" --force
