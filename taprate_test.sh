#!/usr/bin/env bash
# =============================================================================
# TapRate API Test Playbook
# =============================================================================
# Usage:
#   chmod +x taprate_test.sh
#   ./taprate_test.sh
#
# Prerequisites: curl, jq
# Set the CONFIG section before running.
# =============================================================================
 
# NOTE: no set -e — arithmetic counters and intentional non-zero exits
#       are handled explicitly
set -uo pipefail

# ── CONFIG ────────────────────────────────────────────────────────────────────
# Secrets come from env vars, or from a gitignored taprate_test.env next to
# this script (STAFF_PASSWORD, OTHER_PASSWORD, REGISTRATION_CODE).
[ -f "$(dirname "$0")/taprate_test.env" ] && source "$(dirname "$0")/taprate_test.env"

BASE="http://localhost:8000/api"

STAFF_EMAIL="scottbegin@outlook.com"
STAFF_PASSWORD="${STAFF_PASSWORD:?Set STAFF_PASSWORD (see taprate_test.env)}"

# A second non-staff account to test org isolation (can be a different org)
OTHER_EMAIL="test@test.com"
OTHER_PASSWORD="${OTHER_PASSWORD:-}"

# A known NFC tag UUID that is claimed and assigned to a location in your DB.
# If blank, the tag session tests are skipped.
TEST_TAG_ID="55e084d7-3445-4c7a-bf76-52fa9a99078e"

# Demo location ID (from seed_demo output)
DEMO_LOCATION_ID="fbf5457b-bddb-5cfd-9849-b5abab1dabf2"

# Registration invite code (REGISTRATION_CODE env var on backend)
REGISTRATION_CODE="${REGISTRATION_CODE:?Set REGISTRATION_CODE (see taprate_test.env)}"

# ── HELPERS ───────────────────────────────────────────────────────────────────
 
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'
 
PASS=0
FAIL=0
SKIP=0
 
pass() { echo -e "${GREEN}  ✓ $1${NC}"; PASS=$((PASS + 1)); }
fail() { echo -e "${RED}  ✗ $1${NC}"; FAIL=$((FAIL + 1)); }
skip() { echo -e "${YELLOW}  ~ $1${NC}"; SKIP=$((SKIP + 1)); }
section() { echo -e "\n${CYAN}── $1 ──${NC}"; }
 
expect_status() {
  local label="$1" expected="$2" actual="$3"
  if [ "$actual" -eq "$expected" ]; then
    pass "$label (HTTP $actual)"
  else
    fail "$label — expected HTTP $expected, got HTTP $actual"
  fi
}
 
# All request helpers store status code in REQ_CODE and body in REQ_BODY
do_request() {
  local method="$1" url="$2" data="${3:-}" token="${4:-}"
  local full_url="$BASE$url"
  local args=(-s -w "\n%{http_code}" -X "$method" "$full_url" -H "Content-Type: application/json")
  [ -n "$token" ] && args+=(-H "Authorization: Bearer $token")
  [ -n "$data" ]  && args+=(-d "$data")
  local raw
  raw=$(curl "${args[@]}")
  REQ_CODE=$(echo "$raw" | tail -1)
  REQ_BODY=$(echo "$raw" | sed '$d')
}
 
jv() { echo "$1" | jq -r "$2" 2>/dev/null; }   # jq shorthand
 
 
# =============================================================================
# 1. AUTH
# =============================================================================
section "1. Auth — login and token setup"
 
do_request POST "/auth/login/" "{\"email\":\"$STAFF_EMAIL\",\"password\":\"$STAFF_PASSWORD\"}"
STAFF_TOKEN=$(jv "$REQ_BODY" ".tokens.access")
 
if [ -z "$STAFF_TOKEN" ] || [ "$STAFF_TOKEN" = "null" ]; then
  fail "Staff login — cannot continue without token"
  echo "  HTTP $REQ_CODE — Body: $REQ_BODY"
  exit 1
fi
pass "Staff login"
 
do_request GET "/auth/me/" "" "$STAFF_TOKEN"
expect_status "GET /auth/me/" 200 "$REQ_CODE"
IS_STAFF=$(jv "$REQ_BODY" ".is_staff")
 
# Token refresh
do_request POST "/auth/login/" "{\"email\":\"$STAFF_EMAIL\",\"password\":\"$STAFF_PASSWORD\"}"
REFRESH_TOKEN=$(jv "$REQ_BODY" ".tokens.refresh")
do_request POST "/auth/token/refresh/" "{\"refresh\":\"$REFRESH_TOKEN\"}"
expect_status "POST /auth/token/refresh/" 200 "$REQ_CODE"
 
# Unauthenticated /auth/me/ → 401
do_request GET "/auth/me/"
expect_status "GET /auth/me/ — unauthenticated → 401" 401 "$REQ_CODE"
 
# Bad credentials → 401
do_request POST "/auth/login/" '{"email":"nobody@example.com","password":"wrong"}'
expect_status "POST /auth/login/ — bad credentials → 401" 401 "$REQ_CODE"
 
# Registration without code → 400
do_request POST "/auth/register/" '{"email":"test_reg@example.com","password":"testpass123","first_name":"Test","last_name":"User"}'
expect_status "POST /auth/register/ — missing code → 400" 400 "$REQ_CODE"
 
# Other user login
OTHER_TOKEN=""
if [ -n "$OTHER_EMAIL" ]; then
  do_request POST "/auth/login/" "{\"email\":\"$OTHER_EMAIL\",\"password\":\"$OTHER_PASSWORD\"}"
  OTHER_TOKEN=$(jv "$REQ_BODY" ".tokens.access")
  if [ -n "$OTHER_TOKEN" ] && [ "$OTHER_TOKEN" != "null" ]; then
    pass "Other user login"
  else
    fail "Other user login"
    OTHER_TOKEN=""
  fi
else
  skip "Other user login — OTHER_EMAIL not configured"
fi
 
 
# =============================================================================
# 2. SETUP — fetch IDs from API
# =============================================================================
section "2. Setup — fetching test IDs from API"
 
do_request GET "/dashboard/locations/" "" "$STAFF_TOKEN"
expect_status "GET /dashboard/locations/" 200 "$REQ_CODE"
LOCATION_ID=$(jv "$REQ_BODY" ".items[0].id // .results[0].id // .[0].id")
 
if [ -z "$LOCATION_ID" ] || [ "$LOCATION_ID" = "null" ]; then
  fail "No locations found — create one and re-run"
  exit 1
fi
echo "  Using location: $LOCATION_ID"
 
do_request GET "/dashboard/surveys/" "" "$STAFF_TOKEN"
expect_status "GET /dashboard/surveys/" 200 "$REQ_CODE"
 
# Use the survey actually assigned to the test location so question IDs match
do_request GET "/dashboard/locations/$LOCATION_ID/" "" "$STAFF_TOKEN"
LOCATION_SURVEY_ID=$(jv "$REQ_BODY" ".survey")
if [ -n "$LOCATION_SURVEY_ID" ] && [ "$LOCATION_SURVEY_ID" != "null" ]; then
  SURVEY_ID="$LOCATION_SURVEY_ID"
  echo "  Using location's assigned survey: $SURVEY_ID"
else
  SURVEY_ID=$(jv "$REQ_BODY" ".items[0].id // .results[0].id // .[0].id")
  echo "  Warning: location has no survey — question IDs may not match"
fi
 
ALERT_ID=""
 
QUESTION_ID=""
if [ -n "$SURVEY_ID" ] && [ "$SURVEY_ID" != "null" ]; then
  do_request GET "/dashboard/surveys/$SURVEY_ID/questions/" "" "$STAFF_TOKEN"
  QUESTION_ID=$(jv "$REQ_BODY" ".items[0].id // .results[0].id // .[0].id")
  echo "  Using question: $QUESTION_ID"
fi
 
 
# =============================================================================
# 3. DASHBOARD — locations
# =============================================================================
section "3. Dashboard — locations"
 
do_request GET "/dashboard/locations/"
expect_status "GET /dashboard/locations/ — unauthenticated → 401" 401 "$REQ_CODE"
 
do_request GET "/dashboard/locations/$LOCATION_ID/" "" "$STAFF_TOKEN"
expect_status "GET /dashboard/locations/<id>/" 200 "$REQ_CODE"
 
do_request GET "/dashboard/locations/00000000-0000-0000-0000-000000000000/" "" "$STAFF_TOKEN"
expect_status "GET /dashboard/locations/ — unknown ID → 404" 404 "$REQ_CODE"
 
do_request POST "/dashboard/locations/$LOCATION_ID/preview/" "" "$STAFF_TOKEN"
expect_status "POST /dashboard/locations/<id>/preview/" 200 "$REQ_CODE"
PREVIEW_TOKEN=$(jv "$REQ_BODY" ".token // .session_token")
 
if [ -n "$OTHER_TOKEN" ]; then
  do_request GET "/dashboard/locations/$LOCATION_ID/" "" "$OTHER_TOKEN"
  expect_status "GET location — cross-org isolation → 404" 404 "$REQ_CODE"
else
  skip "Cross-org isolation — OTHER_EMAIL not configured"
fi
 
 
# =============================================================================
# 4. DASHBOARD — surveys and questions
# =============================================================================
section "4. Dashboard — surveys and questions"
 
do_request GET "/dashboard/surveys/" "" "$STAFF_TOKEN"
expect_status "GET /dashboard/surveys/" 200 "$REQ_CODE"
 
if [ -n "$SURVEY_ID" ] && [ "$SURVEY_ID" != "null" ]; then
  do_request GET "/dashboard/surveys/$SURVEY_ID/" "" "$STAFF_TOKEN"
  expect_status "GET /dashboard/surveys/<id>/" 200 "$REQ_CODE"
 
  do_request GET "/dashboard/surveys/$SURVEY_ID/questions/" "" "$STAFF_TOKEN"
  expect_status "GET /dashboard/surveys/<id>/questions/" 200 "$REQ_CODE"
else
  skip "Survey detail and questions — no survey ID available"
fi
 
 
# =============================================================================
# 5. PUBLIC SURVEY — session mint, load, submit
# =============================================================================
section "5. Public survey — QR session mint"
 
do_request POST "/survey/location/$LOCATION_ID/session/" ""
expect_status "POST /survey/location/<id>/session/" 201 "$REQ_CODE"
QR_TOKEN=$(jv "$REQ_BODY" ".token")
 
if [ -n "$QR_TOKEN" ] && [ "$QR_TOKEN" != "null" ]; then
  do_request GET "/survey/$QR_TOKEN/"
  expect_status "GET /survey/<token>/ — valid token" 200 "$REQ_CODE"
 
  # Token should still be valid before submit
  do_request GET "/survey/$QR_TOKEN/"
  expect_status "GET /survey/<token>/ — reusable until submit" 200 "$REQ_CODE"
else
  fail "Could not mint QR session token"
  QR_TOKEN=""
fi
 
do_request GET "/survey/00000000-0000-0000-0000-000000000000/"
expect_status "GET /survey/<token>/ — invalid token → 404" 404 "$REQ_CODE"
 
section "5b. Public survey — submit response"
 
if [ -n "$QR_TOKEN" ] && [ -n "$QUESTION_ID" ] && [ "$QUESTION_ID" != "null" ]; then
  do_request POST "/survey/$QR_TOKEN/response/" \
    "{\"responses\":[{\"question_id\":\"$QUESTION_ID\",\"rating\":5}],\"comment\":\"Great!\",\"email\":\"\",\"marketing_opt_in\":false}"
  expect_status "POST /survey/<token>/response/ — valid submit" 201 "$REQ_CODE"
  IS_TEST_RESP=$(jv "$REQ_BODY" ".is_test")
  if [ "$IS_TEST_RESP" = "false" ]; then
    pass "  QR response correctly marked is_test=false"
  else
    fail "  QR response should be is_test=false, got: $IS_TEST_RESP"
  fi
 
  # Consumed token → 404
  do_request POST "/survey/$QR_TOKEN/response/" \
    "{\"responses\":[{\"question_id\":\"$QUESTION_ID\",\"rating\":5}],\"comment\":\"\",\"email\":\"\",\"marketing_opt_in\":false}"
  expect_status "POST /survey/<token>/response/ — consumed token → 404" 404 "$REQ_CODE"
else
  skip "Survey submit — missing token or question ID"
fi
 
section "5c. Low rating — alert triggered"
 
do_request POST "/survey/location/$LOCATION_ID/session/" ""
ALERT_TOKEN=$(jv "$REQ_BODY" ".token")
 
if [ -n "$ALERT_TOKEN" ] && [ "$ALERT_TOKEN" != "null" ] && [ -n "$QUESTION_ID" ] && [ "$QUESTION_ID" != "null" ]; then
  do_request POST "/survey/$ALERT_TOKEN/response/" \
    "{\"responses\":[{\"question_id\":\"$QUESTION_ID\",\"rating\":1}],\"comment\":\"Terrible!\",\"email\":\"\",\"marketing_opt_in\":false}"
  expect_status "POST /survey/<token>/response/ — rating 1" 201 "$REQ_CODE"
  echo "  ⚠ Check inbox — alert email should arrive within 60 seconds"
 
  do_request GET "/dashboard/alerts/" "" "$STAFF_TOKEN"
  expect_status "GET /dashboard/alerts/ — alert exists after low rating" 200 "$REQ_CODE"
  ALERT_ID=$(jv "$REQ_BODY" ".items[0].id")
  if [ -n "$ALERT_ID" ] && [ "$ALERT_ID" != "null" ]; then
    pass "  Alert ID captured: $ALERT_ID"
  else
    skip "  Alert ID not found in response — resolve test will be skipped"
  fi
else
  skip "Alert trigger test — missing token or question ID"
fi
 
section "5d. Preview session — is_test=true"
 
if [ -n "$PREVIEW_TOKEN" ] && [ "$PREVIEW_TOKEN" != "null" ] && [ -n "$QUESTION_ID" ] && [ "$QUESTION_ID" != "null" ]; then
  do_request POST "/survey/$PREVIEW_TOKEN/response/" \
    "{\"responses\":[{\"question_id\":\"$QUESTION_ID\",\"rating\":1}],\"comment\":\"preview\",\"email\":\"\",\"marketing_opt_in\":false}"
  expect_status "POST /survey/<token>/response/ — preview submit" 201 "$REQ_CODE"
  IS_TEST_RESP=$(jv "$REQ_BODY" ".is_test")
  if [ "$IS_TEST_RESP" = "true" ]; then
    pass "  Preview response correctly marked is_test=true"
  else
    fail "  Preview response should be is_test=true, got: $IS_TEST_RESP"
  fi
else
  skip "Preview is_test check — missing preview token or question ID"
fi
 
 
# =============================================================================
# 6. NFC TAG SESSION
# =============================================================================
section "6. NFC tag session"
 
if [ -z "$TEST_TAG_ID" ]; then
  skip "Tag session tests — TEST_TAG_ID not configured"
else
  do_request GET "/tags/$TEST_TAG_ID/"
  expect_status "GET /tags/<id>/ — public tag check" 200 "$REQ_CODE"
 
  do_request POST "/tags/$TEST_TAG_ID/session/" ""
  expect_status "POST /tags/<id>/session/ — first mint" 201 "$REQ_CODE"
  TAG_TOKEN=$(jv "$REQ_BODY" ".token")
 
  do_request POST "/tags/$TEST_TAG_ID/session/" ""
  expect_status "POST /tags/<id>/session/ — rate limited → 429" 429 "$REQ_CODE"
 
  if [ -n "$TAG_TOKEN" ] && [ "$TAG_TOKEN" != "null" ]; then
    do_request GET "/survey/$TAG_TOKEN/"
    expect_status "GET /survey/<token>/ — tag-minted token valid" 200 "$REQ_CODE"
  fi
fi
 
 
# =============================================================================
# 7. DEMO SESSION
# =============================================================================
section "7. Demo session"
 
do_request POST "/demo/session/" ""
expect_status "POST /demo/session/" 201 "$REQ_CODE"
DEMO_TOKEN=$(jv "$REQ_BODY" ".token")
 
if [ -n "$DEMO_TOKEN" ] && [ "$DEMO_TOKEN" != "null" ]; then
  do_request GET "/survey/$DEMO_TOKEN/"
  expect_status "GET /survey/<demo_token>/ — survey loads" 200 "$REQ_CODE"
  DEMO_LOC_RETURNED=$(jv "$REQ_BODY" ".location_id")
  if [ "$DEMO_LOC_RETURNED" = "$DEMO_LOCATION_ID" ]; then
    pass "  Demo token resolves to correct demo location"
  else
    fail "  Demo location mismatch: expected $DEMO_LOCATION_ID, got $DEMO_LOC_RETURNED"
  fi
else
  fail "Demo session mint returned no token — check DEMO_LOCATION_ID env var"
fi
 
 
# =============================================================================
# 8. CONTACT FORM
# =============================================================================
section "8. Contact form"
 
do_request POST "/contact/" \
  '{"name":"Test User","business_name":"Test Co","email":"test@example.com","location_count":"1","message":"Test message","website":""}'
expect_status "POST /contact/ — valid submission" 201 "$REQ_CODE"
 
# Honeypot filled → 200 but not persisted
do_request POST "/contact/" \
  '{"name":"Bot","business_name":"Spam Co","email":"bot@spam.com","location_count":"1","message":"","website":"http://spam.com"}'
expect_status "POST /contact/ — honeypot → silent 200" 200 "$REQ_CODE"
 
# Missing required fields → 400
do_request POST "/contact/" \
  '{"name":"","business_name":"","email":"","location_count":""}'
expect_status "POST /contact/ — missing fields → 400" 400 "$REQ_CODE"
 
# Invalid email → 400
do_request POST "/contact/" \
  '{"name":"Test","business_name":"Test Co","email":"notanemail","location_count":"1"}'
expect_status "POST /contact/ — invalid email → 400" 400 "$REQ_CODE"
 
 
# =============================================================================
# 9. DASHBOARD — insights, comments, alerts, organization
# =============================================================================
section "9. Dashboard — insights, comments, alerts, organization"
 
do_request GET "/dashboard/insights/" "" "$STAFF_TOKEN"
expect_status "GET /dashboard/insights/" 200 "$REQ_CODE"
 
do_request GET "/dashboard/comments/" "" "$STAFF_TOKEN"
expect_status "GET /dashboard/comments/" 200 "$REQ_CODE"
 
do_request GET "/dashboard/alerts/" "" "$STAFF_TOKEN"
expect_status "GET /dashboard/alerts/" 200 "$REQ_CODE"
 
if [ -n "$ALERT_ID" ] && [ "$ALERT_ID" != "null" ]; then
  do_request PATCH "/dashboard/alerts/$ALERT_ID/" '{"status":"resolved"}' "$STAFF_TOKEN"
  expect_status "PATCH /dashboard/alerts/<id>/ — resolve" 200 "$REQ_CODE"
else
  skip "Alert resolve — no alert ID from section 5c (low rating may not have triggered one)"
fi
 
do_request GET "/dashboard/organization/" "" "$STAFF_TOKEN"
expect_status "GET /dashboard/organization/" 200 "$REQ_CODE"
 
 
# =============================================================================
# 10. ADMIN ENDPOINTS
# =============================================================================
section "10. Admin endpoints"
 
do_request GET "/admin/overview/" "" "$STAFF_TOKEN"
if [ "$IS_STAFF" = "true" ]; then
  expect_status "GET /admin/overview/ — staff → 200" 200 "$REQ_CODE"
else
  expect_status "GET /admin/overview/ — non-staff → 403" 403 "$REQ_CODE"
  skip "Remaining admin tests — need staff account"
fi
 
if [ "$IS_STAFF" = "true" ]; then
  do_request GET "/admin/organizations/" "" "$STAFF_TOKEN"
  expect_status "GET /admin/organizations/" 200 "$REQ_CODE"
  ADMIN_ORG_ID=$(jv "$REQ_BODY" ".items[0].id // .results[0].id // .[0].id")
 
  do_request GET "/admin/tags/" "" "$STAFF_TOKEN"
  expect_status "GET /admin/tags/" 200 "$REQ_CODE"
 
  do_request GET "/admin/signups/" "" "$STAFF_TOKEN"
  expect_status "GET /admin/signups/" 200 "$REQ_CODE"
 
  do_request GET "/admin/contacts/" "" "$STAFF_TOKEN"
  expect_status "GET /admin/contacts/" 200 "$REQ_CODE"
 
  do_request GET "/admin/contacts/?status=new" "" "$STAFF_TOKEN"
  expect_status "GET /admin/contacts/?status=new" 200 "$REQ_CODE"
  CONTACT_ID=$(jv "$REQ_BODY" ".results[0].id")
 
  if [ -n "$CONTACT_ID" ] && [ "$CONTACT_ID" != "null" ]; then
    do_request PATCH "/admin/contacts/$CONTACT_ID/" \
      '{"status":"contacted","notes":"Called, left voicemail"}' "$STAFF_TOKEN"
    expect_status "PATCH /admin/contacts/<id>/ — status update" 200 "$REQ_CODE"
    FIRST_CONTACTED_AT=$(jv "$REQ_BODY" ".contacted_at")
    if [ -n "$FIRST_CONTACTED_AT" ] && [ "$FIRST_CONTACTED_AT" != "null" ]; then
      pass "  contacted_at stamped on transition to 'contacted'"
    else
      fail "  contacted_at not stamped"
    fi
 
    # Re-save — contacted_at should not change
    do_request PATCH "/admin/contacts/$CONTACT_ID/" \
      '{"status":"contacted","notes":"Updated notes"}' "$STAFF_TOKEN"
    SECOND_CONTACTED_AT=$(jv "$REQ_BODY" ".contacted_at")
    if [ "$FIRST_CONTACTED_AT" = "$SECOND_CONTACTED_AT" ]; then
      pass "  contacted_at not re-stamped on repeat save"
    else
      fail "  contacted_at changed on repeat save"
    fi
 
    do_request PATCH "/admin/contacts/$CONTACT_ID/" '{"status":"invalid_status"}' "$STAFF_TOKEN"
    expect_status "PATCH /admin/contacts/<id>/ — invalid status → 400" 400 "$REQ_CODE"
  else
    skip "Contact detail tests — submit the contact form first"
  fi
 
  if [ -n "$ADMIN_ORG_ID" ] && [ "$ADMIN_ORG_ID" != "null" ]; then
    do_request GET "/admin/orgs/$ADMIN_ORG_ID/" "" "$STAFF_TOKEN"
    expect_status "GET /admin/orgs/<id>/" 200 "$REQ_CODE"
 
    do_request GET "/admin/orgs/$ADMIN_ORG_ID/locations/" "" "$STAFF_TOKEN"
    expect_status "GET /admin/orgs/<id>/locations/" 200 "$REQ_CODE"
 
    do_request GET "/admin/orgs/$ADMIN_ORG_ID/tags/" "" "$STAFF_TOKEN"
    expect_status "GET /admin/orgs/<id>/tags/" 200 "$REQ_CODE"
  fi
 
  do_request GET "/admin/overview/"
  expect_status "GET /admin/overview/ — unauthenticated → 401" 401 "$REQ_CODE"
fi
 
 
# =============================================================================
# 11. AUTH BOUNDARY — all protected endpoints reject unauthenticated requests
# =============================================================================
section "11. Auth boundary — unauthenticated access"
 
PROTECTED=(
  "/dashboard/locations/"
  "/dashboard/surveys/"
  "/dashboard/alerts/"
  "/dashboard/insights/"
  "/dashboard/comments/"
  "/dashboard/organization/"
  "/dashboard/incentives/"
  "/admin/overview/"
  "/admin/organizations/"
  "/admin/contacts/"
)
 
for endpoint in "${PROTECTED[@]}"; do
  do_request GET "$endpoint"
  if [ "$REQ_CODE" -eq 401 ] || [ "$REQ_CODE" -eq 403 ]; then
    pass "GET $endpoint — unauthenticated → $REQ_CODE"
  else
    fail "GET $endpoint — expected 401/403, got $REQ_CODE"
  fi
done
 
 
# =============================================================================
# SUMMARY
# =============================================================================
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e " ${GREEN}Passed: $PASS${NC}   ${RED}Failed: $FAIL${NC}   ${YELLOW}Skipped: $SKIP${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
 
if [ "$FAIL" -gt 0 ]; then
  exit 1
fi