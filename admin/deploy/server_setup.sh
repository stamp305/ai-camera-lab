#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# AI Camera Admin — Server Setup Script
# ==============================================================================
# Deploys the AI Camera Admin backend + frontend on the Linux server, checks
# the nginx wiring, configures the buildtunnel account, applies migrations and
# restarts the services.
#
# THIS SCRIPT NEVER EDITS /etc/nginx/nginx.conf.
# app.advice.th is already serving branches over /ai-camera/socket.io; an
# automated rewrite of the live nginx config is not worth the risk, so nginx is
# only ever *checked* here and any missing piece is printed for a human to add.
#
# Usage:
#   sudo bash deploy/server_setup.sh                 # check + deploy
#   sudo bash deploy/server_setup.sh --check-only    # change nothing, just report
#   sudo bash deploy/server_setup.sh --add-key <pubkey-file>
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
BACKEND_DIR="$PROJECT_DIR/backend"
FRONTEND_DIR="$PROJECT_DIR/frontend"

CHECK_ONLY=false
FAILURES=0

# ==============================================================================
# Logging
# ==============================================================================

log()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
warn() { echo "[WARN] $*" >&2; }
error() { echo "[ERROR] $*" >&2; FAILURES=$((FAILURES + 1)); }

# A step that fails must be visible. The old script chained every step as
# `step || error "..." && true`, which swallowed the failure and carried on to
# the next one regardless — a broken deploy still printed "Setup complete!".
run_step() {
    local name="$1"; shift
    if "$@"; then
        return 0
    fi
    error "$name failed"
    return 1
}

# ==============================================================================
# Configuration loading
# ==============================================================================

load_config() {
    if [[ -f "$PROJECT_DIR/env.server" ]]; then
        # shellcheck disable=SC1091
        source "$PROJECT_DIR/env.server"
    else
        error "env.server not found at $PROJECT_DIR/env.server"
        exit 1
    fi

    if [[ -z "${AI_CAMERA_ADMIN_BUILDER_KEY:-}" && -f "/etc/ai-camera/builder.key" ]]; then
        AI_CAMERA_ADMIN_BUILDER_KEY=$(tr -d '\r\n' < /etc/ai-camera/builder.key)
    fi
    if [[ -z "${AI_CAMERA_ADMIN_BUILDER_KEY:-}" && -f "$BACKEND_DIR/.builder_key" ]]; then
        AI_CAMERA_ADMIN_BUILDER_KEY=$(tr -d '\r\n' < "$BACKEND_DIR/.builder_key")
    fi
    BUILDER_KEY="${AI_CAMERA_ADMIN_BUILDER_KEY:-}"
    if [[ -z "$BUILDER_KEY" ]]; then
        # No "change-me" default: a placeholder key silently accepted here means
        # the gateway comes up with an auth secret nobody intended.
        error "AI_CAMERA_ADMIN_BUILDER_KEY is not set (env.server, /etc/ai-camera/builder.key, or backend/.builder_key)"
        exit 1
    fi

    BUILDER_PUBKEY="${AI_CAMERA_ADMIN_BUILDER_PUBKEY:-$BACKEND_DIR/builder_signing.pub}"

    # One port for the reverse tunnel, everywhere. build_farm_gateway.py
    # defaults to 8765 and the Mac Mini's builder.env sets FARM_ARTIFACT_PORT=8765,
    # so sshd's PermitListen and this URL have to agree with that or the admin
    # dials a port nothing is listening on.
    FARM_TUNNEL_PORT="${AI_CAMERA_ADMIN_FARM_TUNNEL_PORT:-8765}"
    FARM_TUNNEL_URL="${AI_CAMERA_ADMIN_FARM_TUNNEL_URL:-http://127.0.0.1:${FARM_TUNNEL_PORT}}"

    BACKEND_PUBLIC_URL="${AI_CAMERA_ADMIN_BACKEND_PUBLIC_URL:-http://127.0.0.1:8011}"
    FRONTEND_URL="${AI_CAMERA_ADMIN_FRONTEND_URL:-http://127.0.0.1:8002}"

    log "  builder key:  loaded (${#BUILDER_KEY} chars)"
    log "  tunnel URL:   $FARM_TUNNEL_URL"
    log "  backend:      $BACKEND_PUBLIC_URL"
    log "  frontend:     $FRONTEND_URL"
}

# ==============================================================================
# Step 1: Python requirements
# ==============================================================================

install_requirements() {
    log "--- Step 1: Installing Python requirements ---"
    local req="$BACKEND_DIR/client_update_master/requirements.txt"
    if [[ ! -f "$req" ]]; then
        warn "  $req not found — is the client_update_master submodule initialised?"
        warn "  run: git submodule update --init --recursive"
        return 0
    fi

    # Install into the project venv when there is one. A bare `pip install` on
    # the server writes into the system interpreter, which is shared with
    # everything else running on that host.
    local pip_bin="pip"
    for candidate in "$PROJECT_DIR/venv/bin/pip" "$PROJECT_DIR/.venv/bin/pip" "$BACKEND_DIR/venv/bin/pip"; do
        if [[ -x "$candidate" ]]; then
            pip_bin="$candidate"
            break
        fi
    done
    log "  using: $pip_bin"

    if $CHECK_ONLY; then
        log "  (check-only: skipping install)"
        return 0
    fi
    "$pip_bin" install -q -r "$req"
    log "  pip install complete"
}

# ==============================================================================
# Step 2: Check nginx — REPORT ONLY, never rewrite
# ==============================================================================

check_nginx() {
    log "--- Step 2: Checking nginx wiring (read-only) ---"

    if ! command -v nginx >/dev/null 2>&1; then
        warn "  nginx not found on PATH — skipping check"
        return 0
    fi

    # The branch clients (identify, DLC push, main update) all ride ONE socket
    # at /ai-camera/socket.io -> frontend on 8002. If that proxy is missing,
    # nothing about branches works, so it is the only thing that must exist.
    if grep -rqs "ai-camera/socket.io" /etc/nginx/ 2>/dev/null; then
        log "  OK: /ai-camera/socket.io proxy found (branch clients + DLC use this)"
    else
        warn "  /ai-camera/socket.io proxy NOT found under /etc/nginx/."
        warn "  Branches could not connect at all without it, so it is probably"
        warn "  configured elsewhere — verify by hand before assuming it is missing."
    fi

    # The build gateway does NOT need to be exposed publicly. The Mac builder
    # reaches it through the same SSH tunnel it serves artifacts over, so the
    # recommended setup adds no nginx location for it at all.
    if grep -rqs "build-farm" /etc/nginx/ 2>/dev/null; then
        log "  note: a /build-farm/ location exists — only needed if the builder"
        log "        dials in over the public URL instead of the SSH tunnel."
    else
        log "  OK: no public /build-farm/ location (builder should use the SSH tunnel)"
    fi

    if nginx -t >/dev/null 2>&1; then
        log "  nginx -t: OK (config left untouched by this script)"
    else
        error "nginx -t reports the CURRENT config is invalid — fix before deploying"
        nginx -t 2>&1 | sed 's/^/    /' >&2
        return 1
    fi
}

# ==============================================================================
# Step 3: buildtunnel user for the reverse artifact tunnel
# ==============================================================================

create_buildtunnel_user() {
    log "--- Step 3: buildtunnel account ---"

    if id -u buildtunnel >/dev/null 2>&1; then
        log "  user buildtunnel already exists"
    elif $CHECK_ONLY; then
        log "  (check-only: would create user buildtunnel)"
        return 0
    else
        log "  creating user buildtunnel"
        useradd -r -m -d /home/buildtunnel -s /usr/sbin/nologin buildtunnel
        passwd -l buildtunnel >/dev/null 2>&1 || true
        log "  user buildtunnel created"
    fi

    local sshd_conf="/etc/ssh/sshd_config"
    if grep -q "Match User buildtunnel" "$sshd_conf" 2>/dev/null; then
        log "  sshd Match User buildtunnel already present"
        if ! grep -q "PermitListen 127.0.0.1:${FARM_TUNNEL_PORT}" "$sshd_conf" 2>/dev/null; then
            warn "  but PermitListen does not mention port ${FARM_TUNNEL_PORT} —"
            warn "  the admin fetches artifacts from ${FARM_TUNNEL_URL}, so these must match."
        fi
        return 0
    fi

    if $CHECK_ONLY; then
        log "  (check-only: would append Match User buildtunnel to $sshd_conf)"
        return 0
    fi

    # A Match block must be the LAST thing in sshd_config: every keyword after
    # it belongs to that block. Appending is correct — but back up first and
    # validate before reloading, because a bad sshd_config can lock the box out.
    cp -a "$sshd_conf" "${sshd_conf}.bak-$(date +%Y%m%d_%H%M%S)"
    cat >> "$sshd_conf" <<EOF

# Added by ai-camera deploy/server_setup.sh — reverse tunnel for build artifacts
Match User buildtunnel
    PermitListen 127.0.0.1:${FARM_TUNNEL_PORT}
    PermitOpen none
    AllowTcpForwarding remote
    PermitTTY no
    X11Forwarding no
    ForceCommand /usr/sbin/nologin
EOF

    if sshd -t >/dev/null 2>&1; then
        log "  sshd -t: OK, reloading sshd"
        systemctl reload sshd 2>/dev/null || service sshd reload 2>/dev/null || warn "Could not reload sshd"
    else
        # Do not leave a config that refuses to load: restore and report.
        error "sshd -t failed — restoring the previous sshd_config"
        sshd -t 2>&1 | sed 's/^/    /' >&2
        cp -a "${sshd_conf}.bak-"* "$sshd_conf" 2>/dev/null || true
        return 1
    fi
}

# ==============================================================================
# Step 4: Migrations
# ==============================================================================

resolve_branch_db_path() {
    # AI_CAMERA_ADMIN_BRANCH_DB_URL looks like sqlite:///./branches.db or
    # sqlite:////abs/path.db. The old code ran `sed 's|.*/||'` on it, which threw
    # away the directory and left just "branches.db" — so the file was looked up
    # in the current directory and essentially never found.
    local url="${AI_CAMERA_ADMIN_BRANCH_DB_URL:-}"
    [[ -z "$url" ]] && return 1
    case "$url" in
        sqlite:////*) echo "/${url#sqlite:////}" ;;
        sqlite:///*)  local rel="${url#sqlite:///}"
                      case "$rel" in
                          /*) echo "$rel" ;;
                          *)  echo "$BACKEND_DIR/${rel#./}" ;;
                      esac ;;
        *) return 1 ;;   # not sqlite (MySQL etc.) — handled by the app itself
    esac
}

run_migrations() {
    log "--- Step 4: Migrations ---"

    # branch_db.init_branches_db() calls _add_column_if_missing() for role on
    # every start, so the backend migrates itself. The .sql file is only for
    # databases touched out-of-band.
    log "  note: the backend adds the 'role' column itself on start;"
    log "        this step is a best-effort extra, not a requirement."

    local migrations_dir="$PROJECT_DIR/deploy/migrations"
    if [[ ! -d "$migrations_dir" ]] || [[ -z "$(ls -A "$migrations_dir" 2>/dev/null)" ]]; then
        log "  no migrations to apply"
        return 0
    fi

    if [[ -z "${AI_CAMERA_ADMIN_BRANCH_DB_URL:-}" ]]; then
        log "  AI_CAMERA_ADMIN_BRANCH_DB_URL not set — backend will use its default and self-migrate"
        return 0
    fi

    local db_path
    if ! db_path=$(resolve_branch_db_path); then
        log "  branch DB is not sqlite — the backend handles its own migration, skipping"
        return 0
    fi
    if [[ ! -f "$db_path" ]]; then
        log "  branch DB not created yet at $db_path — the backend will create and migrate it"
        return 0
    fi
    if ! command -v sqlite3 >/dev/null 2>&1; then
        log "  sqlite3 not installed — skipping (the backend self-migrates anyway)"
        return 0
    fi
    if $CHECK_ONLY; then
        log "  (check-only: would apply migrations to $db_path)"
        return 0
    fi

    cp -a "$db_path" "${db_path}.bak-$(date +%Y%m%d_%H%M%S)"
    local f
    for f in "$migrations_dir"/*.sql; do
        [[ -f "$f" ]] || continue
        log "  applying $(basename "$f") to $db_path"
        # "duplicate column name" just means it already ran; anything else is
        # worth showing rather than sending to /dev/null like the old version.
        local out
        if out=$(sqlite3 "$db_path" < "$f" 2>&1); then
            log "    OK"
        elif echo "$out" | grep -qi "duplicate column name"; then
            log "    already applied"
        else
            warn "    sqlite3: $out"
        fi
    done
}

# ==============================================================================
# Step 5: Restart services
# ==============================================================================

restart_one() {
    local unit="$1" name="$2" workdir="$3"; shift 3
    if systemctl list-unit-files 2>/dev/null | grep -q "^${unit}"; then
        log "  restarting $name via systemctl"
        if systemctl restart "$unit"; then
            log "  $name restarted"
            return 0
        fi
        error "systemctl restart $unit failed"
        return 1
    fi

    # The old script wrote `systemctl restart X || log "..."` followed by
    # indented lines that were NOT part of the || branch — so the manual pkill
    # and nohup ran every single time, killing a service systemd had just
    # started correctly and launching a second copy beside it.
    warn "  no systemd unit '$unit' — starting $name manually (not supervised)"
    if $CHECK_ONLY; then
        log "  (check-only: would start $name manually)"
        return 0
    fi
    ( cd "$workdir" && nohup "$@" > "/var/log/ai-camera-${name}.log" 2>&1 & )
    log "  $name started manually, log: /var/log/ai-camera-${name}.log"
}

restart_services() {
    log "--- Step 5: Restarting services ---"
    restart_one "ai-camera-backend" "backend" "$BACKEND_DIR" \
        uvicorn data_service.api:app --host 127.0.0.1 --port 8011
    restart_one "ai-camera-frontend" "frontend" "$FRONTEND_DIR" \
        python3 app_ai_camera_frontend.py
}

# ==============================================================================
# Step 6: Self-test
# ==============================================================================

self_test() {
    log "--- Step 6: Self-test ---"
    local pass=0 fail=0 code

    # Give a manually started backend a moment to bind.
    local i
    for i in 1 2 3 4 5 6 7 8 9 10; do
        curl -s -o /dev/null "${BACKEND_PUBLIC_URL}/docs" 2>/dev/null && break
        sleep 1
    done

    log "  Test 1: GET /build-farm/jobs with no key -> expect 401/403"
    code=$(curl -s -o /dev/null -w "%{http_code}" "${BACKEND_PUBLIC_URL}/build-farm/jobs" 2>/dev/null || echo "000")
    # 000 means curl could not connect at all. The old script counted that as a
    # PASS, so a backend that was completely down reported a clean self-test.
    if [[ "$code" == "401" || "$code" == "403" ]]; then
        log "    PASS: HTTP $code"; pass=$((pass + 1))
    elif [[ "$code" == "000" ]]; then
        log "    FAIL: backend unreachable at ${BACKEND_PUBLIC_URL}"; fail=$((fail + 1))
    else
        log "    FAIL: HTTP $code (endpoint is not requiring a key)"; fail=$((fail + 1))
    fi

    log "  Test 2: GET /build-farm/jobs with a wrong key -> expect 401/403"
    code=$(curl -s -o /dev/null -w "%{http_code}" -H "x-builder-key: definitely-not-the-key" \
           "${BACKEND_PUBLIC_URL}/build-farm/jobs" 2>/dev/null || echo "000")
    if [[ "$code" == "401" || "$code" == "403" ]]; then
        log "    PASS: HTTP $code"; pass=$((pass + 1))
    else
        log "    FAIL: HTTP $code"; fail=$((fail + 1))
    fi

    log "  Test 3: frontend answers on ${FRONTEND_URL}"
    code=$(curl -s -o /dev/null -w "%{http_code}" "${FRONTEND_URL}/ai-camera/" 2>/dev/null || echo "000")
    if [[ "$code" != "000" ]]; then
        log "    PASS: HTTP $code"; pass=$((pass + 1))
    else
        log "    FAIL: frontend unreachable"; fail=$((fail + 1))
    fi

    log "  self-test: $pass passed, $fail failed"
    [[ "$fail" -eq 0 ]]
}

# ==============================================================================
# --add-key
# ==============================================================================

add_builder_key() {
    local key_file="${1:-}"
    if [[ -z "$key_file" || ! -f "$key_file" ]]; then
        error "Usage: $0 --add-key <pubkey-file>"
        exit 1
    fi

    log "--- Adding builder SSH public key to buildtunnel ---"
    id -u buildtunnel >/dev/null 2>&1 || { error "user buildtunnel does not exist — run without --add-key first"; exit 1; }

    local home ak
    home=$(getent passwd buildtunnel | cut -d: -f6)
    home="${home:-/home/buildtunnel}"
    ak="$home/.ssh/authorized_keys"

    install -d -m 700 -o buildtunnel -g buildtunnel "$home/.ssh"
    touch "$ak"

    local key_content
    key_content=$(tr -d '\r' < "$key_file")
    if grep -qF "$key_content" "$ak" 2>/dev/null; then
        log "  key already present"
    else
        printf '%s\n' "$key_content" >> "$ak"
        log "  key appended"
    fi
    chown buildtunnel:buildtunnel "$ak"
    chmod 600 "$ak"
    log "  done"
}

# ==============================================================================
# Main
# ==============================================================================

main() {
    local do_add_key="" 

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --check-only) CHECK_ONLY=true; shift ;;
            --add-key)    do_add_key="${2:-}"; shift 2 ;;
            *)            error "unknown argument: $1"; exit 1 ;;
        esac
    done

    log "========================================="
    log "AI Camera Admin Server Setup"
    $CHECK_ONLY && log "MODE: check-only (nothing will be changed)"
    log "========================================="
    log "Project dir: $PROJECT_DIR"

    if [[ $EUID -ne 0 ]] && ! $CHECK_ONLY; then
        error "must run as root (sudo bash deploy/server_setup.sh)"
        exit 1
    fi

    load_config

    if [[ -n "$do_add_key" ]]; then
        add_builder_key "$do_add_key"
        exit $(( FAILURES > 0 ? 1 : 0 ))
    fi

    run_step "Step 1 (requirements)" install_requirements       || true
    run_step "Step 2 (nginx check)"  check_nginx                || true
    run_step "Step 3 (buildtunnel)"  create_buildtunnel_user    || true
    run_step "Step 4 (migrations)"   run_migrations             || true
    run_step "Step 5 (restart)"      restart_services           || true

    if $CHECK_ONLY; then
        log "check-only run finished"
    else
        self_test || warn "self-test had failures (see above)"
    fi

    log "========================================="
    if [[ "$FAILURES" -gt 0 ]]; then
        log "Finished with $FAILURES failed step(s) — DO NOT assume the deploy is good"
        exit 1
    fi
    log "Setup complete"
    log "========================================="
}

main "$@"
