#!/usr/bin/env bash
# Vérifie qu'un tag de release peut être publié sur le commit attendu.
#
# Usage : scripts/check_release_tag.sh <tag> <sha>
# Environnement : GH_TOKEN, GITHUB_REPOSITORY.
# Affiche « absent » (tag à créer) ou « same » (tag déjà posé sur <sha> : relance).
# Échoue si le tag existe sur un autre commit : `gh release create --target` réutiliserait
# silencieusement ce tag, et la release serait rattachée au mauvais commit.
set -euo pipefail

tag="${1:?tag requis}"
expected="${2:?sha requis}"
repo="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY requis}"

if [[ ! "$tag" =~ ^[0-9A-Za-z][0-9A-Za-z._-]*$ ]]; then
  echo "::error::Nom de tag invalide : ${tag}" >&2
  exit 1
fi

# matching-refs répond 200 (liste vide si absent) : une erreur réseau ou d'authentification
# fait échouer le script au lieu de passer pour un tag absent.
object="$(gh api --paginate "repos/${repo}/git/matching-refs/tags/${tag}" \
  --jq ".[] | select(.ref == \"refs/tags/${tag}\") | .object.type + \" \" + .object.sha")"

if [ -z "$object" ]; then
  echo "absent"
  exit 0
fi

read -r object_type sha <<<"$object"
# Tag annoté : commit pointé par l'objet tag.
while [ "$object_type" = "tag" ]; do
  read -r object_type sha <<<"$(gh api "repos/${repo}/git/tags/${sha}" --jq '.object.type + " " + .object.sha')"
done

if [ "$sha" != "$expected" ]; then
  echo "::error::Le tag ${tag} existe déjà sur ${sha} (attendu : ${expected}). Incrémenter la version, ou supprimer ce tag s'il est erroné." >&2
  exit 1
fi
echo "same"
