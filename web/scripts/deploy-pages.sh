#!/bin/sh
# Builds the app and publishes web/dist to the gh-pages branch, which GitHub Pages serves at
# https://<owner>.github.io/<repo>/. The branch holds only the latest build (one commit, force-pushed),
# so deploys don't pile up copies of the data in the repo's history.
set -e
cd "$(dirname "$0")/.."
npx vite build
src=$(git rev-parse --short HEAD)
url=$(git remote get-url origin)
tmp=$(mktemp -d)
cp -r dist/. "$tmp"/
touch "$tmp/.nojekyll"
cd "$tmp"
git init -q -b gh-pages
git add -A
git -c user.name="${GIT_AUTHOR_NAME:-deploy}" -c user.email="${GIT_AUTHOR_EMAIL:-deploy@localhost}" commit -q -m "Deploy $src"
git push -f "$url" gh-pages
cd - >/dev/null
rm -rf "$tmp"
echo "Pushed $src to gh-pages; GitHub Pages takes a minute or two to publish it."
