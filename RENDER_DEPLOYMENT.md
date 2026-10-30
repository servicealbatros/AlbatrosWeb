# Albatros on Render

Render website and authenticated reception are deployed. The current operational connection uses one authoritative database on the owned Windows worker; this is not a completed independent cloud migration.

## Linux migration test (13 October 2026)

### Supplier authentication test in progress

The owner approved entering supplier credentials personally in the protected
test console. `ALBATROS_PROVIDER_TEST_CONSOLE=true` is set on the isolated test
service only. `/api/admin/provider-test-grant` requires the test administrator
token; a single-use two-minute grant unlocks a secure, HttpOnly, same-site cookie
for 30 minutes. The cookie only unlocks login/verification forms and known
supplier destinations, not dashboard access or robot administration. Passwords
and cookies are not exported and browser submission errors are suppressed.

The dual-process deployment was killed by Render for exceeding its 512 MiB
limit. The current candidate uses one application process with two listeners:
private loopback worker API on 8010 and the authenticated public gateway on PORT.
Application startup/background tasks run only once. Tests explicitly verify that
the private-listener trust cannot open the public gateway or forwarded requests.
The private X display now verifies an active socket instead of a stale filename.
30 tests passed, including real Chromium fixtures for email-first login and
new-device confirmation. Human-operated reCAPTCHA clicks are restricted to the
currently visible trusted CAPTCHA frame; automatic challenge solving is absent.
AutoTotal login and its new-device confirmation reached the authenticated
eOriginal homepage. DSM login reached QService and a read-only lookup in Render
identified B103TST / WBA00000000000001 / BMW 5 (F10). These sessions need renewal
after the earlier deployments because the browser profile was ephemeral. Inter
Cars subsequently reached the authenticated catalogue and Haynes reached its
vehicle-make selection after intermittent provider HTTP 503 responses. Complete
parts/labour offering remains unverified. Initial disk attachment requires one
fresh login to populate the persistent profile; old temporary sessions were not
exported or migrated.

The isolated case is based on request 37 (B103TST, BMW 5 F10): engine oil,
oil/air/fuel/cabin filters, mandatory drain plug and labour times. Its original
request is unchanged. No real messaging keys were enabled.

The isolated service `albatros-robot-linux-test` tracks the private
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

After confirmed 512 MiB OOM failures, the owner approved the 25 USD/month 2 GB
compute plan for this test and activated it in Render. Plan `1c-2g` is verified.
The owner subsequently approved the 10 GB persistent disk (2.50 USD/month),
`dsk-db74fvmi0phs738htmeg`, mounted at `/var/data/albatros`. Both DATA_DIR and
OFFERING_DIR now use the mount. Browser profile identity survived a controlled
Render restart; the post-restart runtime report confirms the actual mount.
Validation is saved privately in data/render_browser_persistence_validation.json.
Base test compute plus disk: 27.50 USD/month; the old backend is still separate.
Do not change production
DNS/Meta callback or activate the operational snapshot under the testing scope.
The service now persists test data and the browser profile; ROBOT_ENABLED=false.
The free integration test closes provider tabs after the browser probe to reduce
memory use (`ALBATROS_KEEP_PROVIDER_TABS=false`). A production worker keeps those
tabs open; the free test does not establish capacity for concurrent supplier work.

The free full-runtime integration test passed: authenticated requests and
scheduler reads are relayed to the loopback core in Render, anonymous reads
return 401, an unsigned webhook returns 401, and a signed empty webhook returns
200 when the isolated test robot is temporarily enabled. It was disabled again
immediately afterward. No real messages, customers, Meta account or AI key were
used. The private runtime diagnostic reports the browser and extension ready.
19 tests passed in the Linux build. The larger open-tab test timed out and
restarted near the free memory limit; production capacity remains unverified.

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

## Registration-certificate refresh during pre-arrival

Production pre-reception requests the registration certificate when the vehicle
is absent from the native DSM appointment, or one calendar year has elapsed
since its latest verified completed/closed/invoiced work order. On February 29,
the following anniversary falls on February 28. Estimates, cancelled documents,
future dates and new open pre-orders never reset this clock. Historical work
orders must match the verified VIN; a former vehicle with the same plate does
not qualify. History is read again when the customer confirms attendance and
before creating an order using stored data.

For a recent visit the customer confirms attendance, contact details and any
additional requirements, then gives final confirmation. The flow uses existing
vehicle data without OCR, without claiming a document was received and without
updating saved ITP/year values. Missing saved values remain missing; client
details are requested separately as before. Concurrent DSM changes cause review
instead of overwriting a newer record. The existing duplicate-order checks and
exact approved-order bindings still apply to both paths.

An unknown/unreadable last-visit date requires adviser review, not an automatic
photo request. A first-time customer with a native appointment but no vehicle
can send the certificate; its VIN must match the appointment's plate. Before a
real order can be opened, that vehicle must be registered and linked in DSM.
This change deliberately does not guess a vehicle-creation endpoint or attach
the order to a generic vehicle. Explicit manual document/OCR tests retain their
document requirement.

The policy changes no enrolment, recipient list, 17:00 schedule, messaging
enablement or live record. Existing pilot restrictions remain in place.
Regression tests: `python -m unittest test_pre_arrival_policy test_pre_reception
test_pre_arrival_reminders test_dsm_pre_arrival test_manual_pre_reception
test_pre_arrival_worker` (run the command on one line).
Local verification passed 27 policy tests and 88 focused pre-reception tests.
The broader deployment regression run completed 246 tests with one Linux-only
lease test skipped on Windows. No live DSM/WhatsApp writes or deployment were
performed during this verification.
