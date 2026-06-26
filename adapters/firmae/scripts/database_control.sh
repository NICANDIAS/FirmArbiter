#!/usr/bin/env bash

set -euo pipefail

ACTION="${1:-}"

STATE_ROOT="${VERITAS_FIRMAE_STATE_ROOT:-/var/lib/veritas/firmae}"
PG_ROOT="$STATE_ROOT/postgres"
PGDATA="$PG_ROOT/data"
PGSOCKET="$PG_ROOT/socket"
PGLOG="$PG_ROOT/postgres.log"
PGPORT="${VERITAS_FIRMAE_PGPORT:-55432}"

DATABASE_NAME="firmware"
DATABASE_USER="firmadyne"
DATABASE_PASSWORD="firmadyne"
SCHEMA_FILE="/opt/firmae/database/schema"

PROVENANCE_DIRECTORY="$STATE_ROOT/provenance"
EXPECTED_TABLES="brand,image,object,object_to_image,product"

POSTGRESQL_VERSION="$(
    find /usr/lib/postgresql \
        -mindepth 1 \
        -maxdepth 1 \
        -type d \
        -printf '%f\n' |
    sort -V |
    tail -1
)"

if [ -z "$POSTGRESQL_VERSION" ]
then
    echo "No PostgreSQL server installation found." >&2
    exit 1
fi

PG_BINDIR="/usr/lib/postgresql/${POSTGRESQL_VERSION}/bin"
INITDB="$PG_BINDIR/initdb"
PG_CTL="$PG_BINDIR/pg_ctl"
POSTGRES="$PG_BINDIR/postgres"
PSQL="$PG_BINDIR/psql"
CREATEDB="$PG_BINDIR/createdb"
PG_ISREADY="$PG_BINDIR/pg_isready"

for executable in \
    "$INITDB" \
    "$PG_CTL" \
    "$POSTGRES" \
    "$PSQL" \
    "$CREATEDB" \
    "$PG_ISREADY"
do
    if [ ! -x "$executable" ]
    then
        echo "Required executable not found: $executable" >&2
        exit 1
    fi
done

if [ ! -s "$SCHEMA_FILE" ]
then
    echo "FirmAE database schema is missing: $SCHEMA_FILE" >&2
    exit 1
fi

as_postgres() {
    runuser -u postgres -- "$@"
}

prepare_directories() {
    install \
        -d \
        -o postgres \
        -g postgres \
        -m 0700 \
        "$PG_ROOT" \
        "$PGDATA"

    install \
        -d \
        -o postgres \
        -g postgres \
        -m 0770 \
        "$PGSOCKET"

    install \
        -d \
        -m 0755 \
        "$PROVENANCE_DIRECTORY"

    touch "$PGLOG"
    chown postgres:postgres "$PGLOG"
    chmod 0600 "$PGLOG"
}

server_running() {
    [ -s "$PGDATA/PG_VERSION" ] &&
    as_postgres \
        "$PG_CTL" \
        -D "$PGDATA" \
        status \
        >/dev/null 2>&1
}

initialize_cluster() {
    prepare_directories

    if [ ! -s "$PGDATA/PG_VERSION" ]
    then
        echo "Initialising private PostgreSQL cluster..."

        as_postgres \
            "$INITDB" \
            -D "$PGDATA" \
            --encoding=UTF8 \
            --locale=C \
            --auth-local=trust \
            --auth-host=scram-sha-256

        cat >> "$PGDATA/postgresql.conf" <<CONFIG

# VERITAS FIRMAE DATABASE SETTINGS
listen_addresses = '127.0.0.1'
port = ${PGPORT}
unix_socket_directories = '${PGSOCKET}'
password_encryption = 'scram-sha-256'
CONFIG

        cat > "$PGDATA/pg_hba.conf" <<HBA
local   all   all                         trust
host    all   all   127.0.0.1/32          scram-sha-256
host    all   all   ::1/128               scram-sha-256
HBA

        chown postgres:postgres \
            "$PGDATA/postgresql.conf" \
            "$PGDATA/pg_hba.conf"

        chmod 0600 \
            "$PGDATA/postgresql.conf" \
            "$PGDATA/pg_hba.conf"
    fi
}

wait_until_ready() {
    for attempt in $(seq 1 30)
    do
        if as_postgres \
            "$PG_ISREADY" \
            -h "$PGSOCKET" \
            -p "$PGPORT" \
            -d postgres \
            >/dev/null 2>&1
        then
            return 0
        fi

        sleep 1
    done

    echo "PostgreSQL did not become ready." >&2
    tail -100 "$PGLOG" >&2 || true
    return 1
}

start_server() {
    initialize_cluster

    if server_running
    then
        echo "PostgreSQL is already running."
        return 0
    fi

    echo "Starting private PostgreSQL cluster..."

    as_postgres \
        "$PG_CTL" \
        -D "$PGDATA" \
        -l "$PGLOG" \
        -w \
        start

    wait_until_ready
}

stop_server() {
    if ! server_running
    then
        echo "PostgreSQL is already stopped."
        return 0
    fi

    echo "Stopping private PostgreSQL cluster..."

    as_postgres \
        "$PG_CTL" \
        -D "$PGDATA" \
        -m fast \
        -w \
        stop
}

admin_query() {
    as_postgres \
        "$PSQL" \
        -h "$PGSOCKET" \
        -p "$PGPORT" \
        -d postgres \
        -v ON_ERROR_STOP=1 \
        "$@"
}

database_query() {
    as_postgres \
        "$PSQL" \
        -h "$PGSOCKET" \
        -p "$PGPORT" \
        -d "$DATABASE_NAME" \
        -v ON_ERROR_STOP=1 \
        "$@"
}

current_tables() {
    database_query \
        -Atqc \
        "SELECT COALESCE(
            string_agg(tablename, ',' ORDER BY tablename),
            ''
         )
         FROM pg_tables
         WHERE schemaname = 'public';"
}

write_provenance() {
    local table_list
    local table_owners

    table_list="$(current_tables)"

    table_owners="$(
        database_query \
            -Atqc \
            "SELECT COALESCE(
                string_agg(
                    tablename || ':' || tableowner,
                    ',' ORDER BY tablename
                ),
                ''
             )
             FROM pg_tables
             WHERE schemaname = 'public';"
    )"

    sha256sum "$SCHEMA_FILE" \
        > "$PROVENANCE_DIRECTORY/database-schema.sha256"

    "$POSTGRES" --version \
        > "$PROVENANCE_DIRECTORY/postgresql-version.txt"

    printf '%s\n' "$table_list" \
        > "$PROVENANCE_DIRECTORY/database-tables.txt"

    printf '%s\n' "$table_owners" \
        > "$PROVENANCE_DIRECTORY/database-table-owners.txt"

    cat > "$PROVENANCE_DIRECTORY/database-contract.txt" <<CONTRACT
PostgreSQL major version: ${POSTGRESQL_VERSION}
Database: ${DATABASE_NAME}
Role: ${DATABASE_USER}
Host: 127.0.0.1
Port: ${PGPORT}
Schema: ${SCHEMA_FILE}
Expected tables: ${EXPECTED_TABLES}
Authentication: SCRAM-SHA-256 over TCP
Cluster ownership: postgres:postgres
State root: ${STATE_ROOT}
CONTRACT

    sha256sum \
        "$PROVENANCE_DIRECTORY/database-schema.sha256" \
        "$PROVENANCE_DIRECTORY/postgresql-version.txt" \
        "$PROVENANCE_DIRECTORY/database-tables.txt" \
        "$PROVENANCE_DIRECTORY/database-table-owners.txt" \
        "$PROVENANCE_DIRECTORY/database-contract.txt" \
        > "$PROVENANCE_DIRECTORY/database-provenance.sha256"
}

verify_database() {
    local table_list
    local owner_list

    start_server

    PGPASSWORD="$DATABASE_PASSWORD" \
        "$PSQL" \
        -h 127.0.0.1 \
        -p "$PGPORT" \
        -U "$DATABASE_USER" \
        -d "$DATABASE_NAME" \
        -v ON_ERROR_STOP=1 \
        -Atqc 'SELECT 1;' \
        | grep -Fx '1' >/dev/null

    table_list="$(current_tables)"

    if [ "$table_list" != "$EXPECTED_TABLES" ]
    then
        echo "FirmAE database table verification failed." >&2
        echo "Expected: $EXPECTED_TABLES" >&2
        echo "Actual:   $table_list" >&2
        exit 1
    fi

    owner_list="$(
        database_query \
            -Atqc \
            "SELECT COALESCE(
                string_agg(tableowner, ',' ORDER BY tablename),
                ''
             )
             FROM pg_tables
             WHERE schemaname = 'public';"
    )"

    if [ "$owner_list" != \
         "firmadyne,firmadyne,firmadyne,firmadyne,firmadyne" ]
    then
        echo "FirmAE database ownership verification failed." >&2
        echo "Actual owners: $owner_list" >&2
        exit 1
    fi

    write_provenance

    echo "FirmAE database verification passed."
}

initialize_database() {
    local role_exists
    local database_exists
    local table_list

    start_server

    role_exists="$(
        admin_query \
            -Atqc \
            "SELECT 1
             FROM pg_roles
             WHERE rolname = '${DATABASE_USER}';"
    )"

    if [ "$role_exists" != "1" ]
    then
        admin_query \
            -c \
            "CREATE ROLE ${DATABASE_USER}
             LOGIN
             PASSWORD '${DATABASE_PASSWORD}';"
    else
        admin_query \
            -c \
            "ALTER ROLE ${DATABASE_USER}
             LOGIN
             PASSWORD '${DATABASE_PASSWORD}';"
    fi

    database_exists="$(
        admin_query \
            -Atqc \
            "SELECT 1
             FROM pg_database
             WHERE datname = '${DATABASE_NAME}';"
    )"

    if [ "$database_exists" != "1" ]
    then
        as_postgres \
            "$CREATEDB" \
            -h "$PGSOCKET" \
            -p "$PGPORT" \
            -O "$DATABASE_USER" \
            "$DATABASE_NAME"
    fi

    table_list="$(current_tables)"

    if [ -z "$table_list" ]
    then
        echo "Importing pinned FirmAE database schema..."

        database_query \
            -f "$SCHEMA_FILE"

        table_list="$(current_tables)"
    fi

    if [ "$table_list" != "$EXPECTED_TABLES" ]
    then
        echo "Unexpected or partially initialised schema." >&2
        echo "Expected: $EXPECTED_TABLES" >&2
        echo "Actual:   $table_list" >&2
        exit 1
    fi

    verify_database

    echo "FirmAE database initialisation completed."
}

show_status() {
    if server_running
    then
        echo "running"
        return 0
    fi

    echo "stopped"
    return 1
}

case "$ACTION" in
    initialize)
        initialize_database
        ;;
    start)
        start_server
        ;;
    stop)
        stop_server
        ;;
    verify)
        verify_database
        ;;
    status)
        show_status
        ;;
    *)
        echo \
            "Usage: $0 {initialize|start|stop|verify|status}" \
            >&2
        exit 64
        ;;
esac
