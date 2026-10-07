#!/usr/bin/env sh
# Create the managed Cloud Spanner database the worker uses with SPANNER=cloud
# (WORKER_CLOUD_SPANNER_DATABASE in .env.compose), with the codegraph schema
# for VECTOR_LENGTH-dimensional embeddings. `reset` drops it first, and every
# graph, run and embedding in it. `update` adds the tables and indexes a
# newer schema has that the database lacks (e.g. CGCrossLinks), keeping
# everything in it; it never alters or drops. The instance must already
# exist: creating one is a billing decision, so this never does. Runs as
# your gcloud user, since changing a database needs the Spanner database
# admin role.
#
#   apps/forge-codegraph-worker/scripts/spanner-cloud.sh create             # fails if it exists
#   apps/forge-codegraph-worker/scripts/spanner-cloud.sh update             # shows what it adds, asks first
#   apps/forge-codegraph-worker/scripts/spanner-cloud.sh reset              # asks first
#   FORCE=1 apps/forge-codegraph-worker/scripts/spanner-cloud.sh reset      # no prompt
#   DATABASE=projects/p/instances/i/databases/d ...         # another target
set -eu
cd "$(dirname "$0")/../../.."
ACTION=${1:-create}
case "$ACTION" in create | update | reset) ;; *)
  echo "usage: $0 [create|update|reset]" >&2
  exit 2
  ;;
esac
GO=${GO:-go}
DATABASE=${DATABASE:-$(sed -n 's/^WORKER_CLOUD_SPANNER_DATABASE=//p' .env.compose 2>/dev/null | tr -d "\"'" | head -1)}
if [ -z "$DATABASE" ]; then
  echo "set WORKER_CLOUD_SPANNER_DATABASE in .env.compose (or DATABASE=...)" >&2
  exit 1
fi
PROJECT=$(echo "$DATABASE" | cut -d/ -f2)
INSTANCE=$(echo "$DATABASE" | cut -d/ -f4)
DB=$(echo "$DATABASE" | cut -d/ -f6)
case "$DATABASE" in projects/*/instances/*/databases/*) ;; *) PROJECT= ;; esac
if [ -z "$PROJECT" ] || [ -z "$INSTANCE" ] || [ -z "$DB" ]; then
  echo "the database must look like projects/P/instances/I/databases/D: $DATABASE" >&2
  exit 1
fi
# The worker's CODEGRAPH_EMBEDDING_DIMENSIONS, through its ${NAME}
# reference to .env.common when it is one.
dimensions() {
  value=$(sed -n 's/^CODEGRAPH_EMBEDDING_DIMENSIONS=//p' apps/forge-codegraph-worker/.env 2>/dev/null | head -1 | tr -d "\"'")
  case "$value" in '${'*'}')
    name=${value#'${'}
    value=$(sed -n "s/^${name%'}'}=//p" .env.common 2>/dev/null | head -1 | tr -d "\"'")
    ;;
  esac
  echo "$value"
}
VECTOR_LENGTH=${VECTOR_LENGTH:-$(dimensions)}
case "${VECTOR_LENGTH:-0}" in '' | 0) VECTOR_LENGTH=3072 ;; esac
echo "target:        $DATABASE"
echo "vector length: $VECTOR_LENGTH (the worker's CODEGRAPH_EMBEDDING_DIMENSIONS must match)"

exists=0
if gcloud spanner databases describe "$DB" --instance="$INSTANCE" --project="$PROJECT" >/dev/null 2>&1; then
  exists=1
fi
if [ "$ACTION" = update ]; then
  if [ "$exists" = 0 ]; then
    echo "$DATABASE doesn't exist; create it with: $0 create" >&2
    exit 1
  fi
  EXISTING=$(mktemp -t codegraph-ddl.XXXXXX)
  PENDING=$(mktemp -t codegraph-pending.XXXXXX)
  trap 'rm -f "$EXISTING" "$PENDING"' EXIT
  gcloud spanner databases ddl describe "$DB" --instance="$INSTANCE" --project="$PROJECT" --format=json >"$EXISTING"
  env $GO -C apps/forge-codegraph-worker run ./cmd/codegraph-schema -vector-length "$VECTOR_LENGTH" -existing "$EXISTING" >"$PENDING"
  if [ ! -s "$PENDING" ]; then
    echo "$DATABASE is up to date"
    exit 0
  fi
  echo "adding what $DB lacks (nothing in it changes):"
  cat "$PENDING"
  if [ "${FORCE:-0}" != 1 ]; then
    printf 'Apply these statements? [y/N] '
    read -r answer
    case "$answer" in y | Y | yes) ;; *)
      echo "aborted"
      exit 1
      ;;
    esac
  fi
  gcloud spanner databases ddl update "$DB" --instance="$INSTANCE" --project="$PROJECT" --ddl-file="$PENDING"
  echo "done: $DATABASE has the current codegraph schema"
  exit 0
fi
if [ "$exists" = 1 ] && [ "$ACTION" = create ]; then
  echo "$DATABASE already exists; the worker can use it as is. To start over: $0 reset" >&2
  exit 1
fi
if [ "$exists" = 1 ] && [ "${FORCE:-0}" != 1 ]; then
  printf 'This DROPS the database and every graph, run and embedding in it. Type its name (%s) to continue: ' "$DB"
  read -r answer
  [ "$answer" = "$DB" ] || {
    echo "aborted"
    exit 1
  }
fi

DDL=$(mktemp -t codegraph-schema.XXXXXX)
trap 'rm -f "$DDL"' EXIT
env $GO -C apps/forge-codegraph-worker run ./cmd/codegraph-schema -vector-length "$VECTOR_LENGTH" >"$DDL"
head -1 "$DDL"
if [ "$exists" = 1 ]; then
  echo "dropping $DB ..."
  gcloud spanner databases delete "$DB" --instance="$INSTANCE" --project="$PROJECT" --quiet
fi
echo "creating $DB ..."
gcloud spanner databases create "$DB" --instance="$INSTANCE" --project="$PROJECT" --ddl-file="$DDL"
echo "done: $DATABASE has the codegraph schema at vector length $VECTOR_LENGTH"
