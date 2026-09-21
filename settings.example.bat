@echo off
rem Copy this file to settings.bat before a server deployment.
rem Never commit or share the real secret and administrator password.

set "PTS_ENV=production"
set "PTS_SECRET_KEY=replace-with-a-random-string-of-at-least-32-characters"
set "PTS_BOOTSTRAP_ADMIN_USERNAME=admin"
set "PTS_BOOTSTRAP_ADMIN_PASSWORD=replace-with-a-strong-initial-password"
set "PTS_BOOTSTRAP_ADMIN_DISPLAY_NAME=系统管理员"

set "TRACE_HOST=0.0.0.0"
set "TRACE_PORT=5080"

rem Set to 1 only when the site is accessed through HTTPS.
set "PTS_HTTPS_ONLY=0"

rem Lingxing OpenAPI credentials. Keep the write endpoints empty until Lingxing
rem has enabled the matching APIs for this account and supplied their exact paths.
set "PTS_LINGXING_APP_ID=replace-with-lingxing-app-id"
set "PTS_LINGXING_APP_SECRET=replace-with-lingxing-app-secret"
set "PTS_LINGXING_API_BASE_URL=https://openapi.lingxing.com"
set "PTS_LINGXING_PURCHASE_ORDER_URL="
set "PTS_LINGXING_INBOUND_URL="
set "PTS_LINGXING_INVENTORY_URL="

rem Outbound endpoint policy (SSRF defence). By default only
rem openapi.lingxing.com is allowed, over HTTPS only. Prefer relative paths
rem (e.g. /erp/sc/routing/...) for the *_URL settings above.
set "PTS_LINGXING_ALLOWED_HOSTS="
set "PTS_LINGXING_ALLOW_HTTP=0"
set "PTS_LINGXING_ALLOW_PRIVATE_HOSTS=0"
