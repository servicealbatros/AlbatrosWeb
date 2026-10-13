# Albatros on Render

Render website and authenticated reception are deployed. The current operational connection uses one authoritative database on the owned Windows worker; this is not a completed independent cloud migration.

## Linux migration test (13 October 2026)

The isolated free service `albatros-robot-linux-test` tracks the private
`codex/render-robot-linux` branch with manual deploys and no real messaging keys.
`Dockerfile.render-robot` installs Chromium and a private X display. Browser CDP
and the worker API bind to loopback only; the authenticated gateway is public.
The gateway does not run a second copy of background automations.

Verified in Render: Chromium/CDP, the existing Inter Cars extension, and HTTP 200
from public DSM, AutoTotal and Inter Cars pages. This does NOT establish supplier
authentication, VIN matching, prices, OE equivalences or labour times.

A private operational snapshot with SHA256 verification is stored in the current
paid service's recovery folder. Local test restoration verified 20 requests,
213 scheduler rows, 17,403 DSM transactions, 4,584 VIN vehicles and 116,494 part
rows across 122 files. SQLite integrity passed and attachment paths were mapped
without unresolved references. This snapshot is NOT the active cloud database.

The owner chose to retain the free test only and did NOT approve the proposed
25 USD/month compute upgrade plus 2.50 USD/month disk. Do not upgrade plans,
change production DNS/Meta callback, or activate the snapshot under that scope.
The free service has ephemeral test data and ROBOT_ENABLED=false.
The free integration test closes provider tabs after the browser probe to reduce
memory use (`ALBATROS_KEEP_PROVIDER_TABS=false`). A production worker keeps those
tabs open; the free test does not establish capacity for concurrent supplier work.

Before any later cutover: obtain approval for persistent compute, authenticate
the supplier accounts in the Linux browser, verify complete parts/labour and DSM
imports, take a fresh quiescent snapshot, restore to a new directory, retain the
cloud device approval database, and validate the single-receiver WhatsApp route.
The supervisor production mode is a compatibility candidate, not a completed
production migration. Keep the previous service available for rollback.

## Public website

Render Static Site serves `website/dist` at `servicealbatros.ro` and `www.servicealbatros.ro`.
It contains workshop presentation only, with approved photos and contact details.
DNS and HTTPS are verified for servicealbatros.ro, www.servicealbatros.ro and
app.servicealbatros.ro. The public login link points to the application domain.

## Backend

Start `uvicorn cloud_app:app --host 0.0.0.0 --port $PORT --workers 1`.
Use Python 3.13 and the variables in `render.yaml`. Set `TZ=Europe/Bucharest`.
The cloud gateway requires authentication for every application route, including documentation,
chat endpoints, static files and admin screens. Local-address bypasses are disabled in cloud mode.
The only unauthenticated backend endpoints are the login form, minimal health check, robots.txt,
and the Meta webhook (which validates its verification token/signature).
Dashboard sessions require an administrator to activate the device. Activate the first device
through `/api/dashboard/devices` using the server's ADMIN_API_TOKEN, not through a public UI.

The paid backend and a persistent disk preserve SQLite, attachments and session/device data.
Use one process and one instance until storage and background work are redesigned for replication.
Copy live data through an authenticated private migration while the source is quiescent; never commit it.
Browser automation is not migrated: the cloud entry point rejects laptop-only worker start endpoints.
Connecting an authenticated worker is a separate step before supplier/DSM searches are enabled online.

## WhatsApp cutover

Provide server-side OPENAI_API_KEY, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_ACCESS_TOKEN,
WHATSAPP_VERIFY_TOKEN, WHATSAPP_APP_SECRET and the approved template settings.
Do not publish these in Git. Keep ROBOT_ENABLED=false during initial deployment.
After private functional tests, change the Meta callback to
`https://app.servicealbatros.ro/webhook/whatsapp`, verify the token and subscribe to messages.
Use one live receiver; do not keep the laptop and server processing the same workload.
Enable the robot only after identities/data and the worker routing have been verified.
Scheduled processing uses the Romania timezone; preserve existing client-notification preferences.

## Git and deployment

Render must be connected to the GitHub provider, not just a public repository URL.
Both services track `main` with `autoDeployTrigger: commit`.
The website builds only for website/** changes; the backend ignores website/**.
These filters are configured on the live services and in render.yaml.
Audit and add only source files; exclude .env, browser profiles, database dumps, recordings and exports.
Confirm the selected paid backend/disk costs before provisioning them.
Apply `render.yaml`, supply secrets through Render, verify deploys, then configure DNS using
the exact A/CNAME values shown in Render. RoTLD registration alone is not necessarily DNS hosting.
Verify the public site, HTTPS, unauthenticated route denial and a signed webhook before cutover.

## Current worker connection

RECEPTION_BRIDGE_URL is the owned worker HTTPS origin. RECEPTION_BRIDGE_SECRET is
a separate random secret, stored only in environment settings on both machines.
The cloud authenticates and checks device revocation before forwarding dashboard
routes. Signed requests bind identity, method, path, query and body and reject
replay/expiry. Browser cookies and administrator tokens are never forwarded.
Cloud device management and robot administrator endpoints are never delegated.
Mutations require same-origin requests and are never automatically retried.

All operational records remain in the worker's existing database to avoid diverging
requests or schedules. If the laptop or tunnel is offline, operations return 503;
there is no writable stale fallback. ROBOT_ENABLED stays false on Render, so its
background automations cannot duplicate those on the worker. The Meta webhook can
be relayed after its signature is verified, preserving the original signature.

POST /api/admin/worker-backup requires the cloud administrator bearer token and a
SHA256 checksum. It stores a private validated recovery archive on the persistent
disk, without extracting it or replacing live databases. SQLite files are copied
using the SQLite backup API; snapshots are not a second active robot.
