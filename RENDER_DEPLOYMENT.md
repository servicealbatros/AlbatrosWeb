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
requests or schedules. If the worker or tunnel is offline, operations return 503;
there is no writable stale fallback. Gateway-only mode suppresses operational
background automations; ROBOT_ENABLED alone does not select a runtime role. The Meta webhook can
be relayed after its signature is verified, preserving the original signature.

POST /api/admin/worker-backup requires the cloud administrator bearer token and a
SHA256 checksum. It stores a private validated recovery archive on the persistent
disk, without extracting it or replacing live databases. SQLite files are copied
using the SQLite backup API; snapshots are not a second active robot.

## Resource lifecycle and runtime roles

The source changes below have been tested locally; they do not change a live
Render service, plan, customer record or messaging configuration.

| Entry point / role | Processes and intended work |
| --- | --- |
| `render.yaml`: `cloud_app:app`, role `gateway` | One gateway application; no Chromium/Xvfb/session keeper. `ALBATROS_GATEWAY_ONLY=true` suppresses operational background tasks. |
| Docker supervisor, role `robot` | Existing single application with two listeners, plus managed Chromium/Xvfb and one session-maintenance helper. The helper keeps sessions and performs idle housekeeping even without a vault key. |
| Docker supervisor, role `probe` | Synthetic compatibility browser probe. It is not a lightweight gateway or an operational messaging robot. |

Set `ALBATROS_RUNTIME_ROLE` explicitly for a supervisor deployment. If omitted,
the previous `ALBATROS_RENDER_MODE=production` selects `robot`; other modes select
`probe`. `ROBOT_ENABLED=false` keeps its existing business-processing meaning:
the robot role still pays the browser's resource cost and maintains supplier
sessions. Choosing the gateway role avoids starting that browser entirely.
The existing single-application-process deployment has been preserved.

Keepalive requests now copy only response metadata and dispose the Playwright
response in `finally`, before any login navigation. Application-owned CDP drivers
serialize initialization, stop failed/stale drivers and stop at application
shutdown. They never close the Chromium instance to which they attach.

Request limits apply to actual ASGI bytes before multipart parsing, including
chunked requests and false/missing Content-Length. The default limit is 20 MiB;
the authenticated backup route retains its 256 MiB limit. Talon uploads read
at most 12 MiB + 1 byte. HMAC checks still use the original request bytes.
Bridge replies stream in bounded chunks; HTML style injection buffers at most
64 KiB of the head and closes the upstream connection on completion, error or
disconnect. Expiring rate-limit, confirmation and console-access maps are
cleaned periodically. Capacity limits reject new keys instead of evicting live
rate-limit buckets or authorizations.

Conversation reads use `conversation_log_index.sqlite3` beside the original
JSONL recovery log. Import checkpoints commit every 500 records and resume
without duplicating those records. Every legacy line is retained verbatim,
including malformed lines; incomplete trailing records wait for completion.
Truncation/rotation retains previously imported history. Imports stream one
record at a time, so an unusually large individual legacy record can still
allocate that record's size. Pages use indexed phone/case predicates and keyset
pagination, default 100 and maximum 200 records, in original chronology.
The 24-hour WhatsApp window uses indexed valid inbound timestamps only.
The first index build needs additional disk space and startup time; keep the
JSONL and index together in backups if logs are rotated.

Housekeeping retains activity leases, idle/safe-page checks and login/CAPTCHA
protection. `browser_memory_health.json` and the existing master status expose
`enabled`, actual fresh `running`, `heartbeat_at`, `last_checked_at`, `pid` and
`stale`. An absent or invalid vault key does not disable housekeeping; invalid
keys appear as `vault_error` in DSM session health. A stale heartbeat expires
after 120 seconds, so configuration alone never proves the helper is running.

### Reproducing local resource checks

Use the pinned `requirements.txt` dependencies. The Dockerfile runs the existing
resource/runtime/provider/bridge/security suites plus the new regressions.
The opt-in synthetic probe owns only fresh local browser fixtures:

```powershell
python -m unittest test_resource_lifecycle test_resource_bounds test_conversation_log_index test_browser_resources test_render_runtime test_provider_sessions test_provider_console test_reception_bridge test_cloud_access
python scripts/resource_regression_probe.py --requests 100 --failures 15 --records 30000 --chromium
```

The probe compares undisposed/disposed 1 MiB responses, checks owned processes
after repeated real CDP failures and reconnects, verifies cookies survive driver
shutdown/reconnect and response disposal, and imports a synthetic conversation
history. Its JSON report includes every process in the owned descendant tree,
summed RSS, Windows private commit, and cgroup memory when available. Summed RSS
can count shared pages more than once; it is not unique physical memory.
Windows runs cannot validate Linux cgroup limits, Xvfb or real `flock` behavior.
A fresh browser fixture also excludes authenticated supplier pages, live traffic
and concurrent jobs. It cannot establish that a 512 MiB deployment fits.

Measured on Windows with Python 3.14.7 and all 12 direct pinned package versions
matching `requirements.txt` (2026-10-28):

| Synthetic workload | Complete owned-tree measurement |
| --- | --- |
| 100 undisposed 1 MiB responses | RSS 158.54 → 258.96 MiB; private commit 132.42 → 230.90 MiB. |
| 100 disposed 1 MiB responses | RSS samples 160.30, 168.34, 174.01, 168.67, 174.73 MiB; private commit 134.79 → 147.16 MiB. Non-monotonic allocations remain; response bodies no longer accumulate one per request. |
| 15 real CDP connection failures | One Python process after every failure; no remaining driver processes. |
| 10 real CDP driver reconnects | Exactly one owned Node driver after each reconnect; external Chromium and both fixture cookies survived. Chromium's own process count varied normally. |
| Fresh Chromium fixture | 733.89 MiB summed tree RSS / 434.66 MiB private commit at the baseline sample. This is not a full robot or Linux capacity measurement. |
| Import 30,000 JSONL records (18,203,400 bytes) | 4.91 seconds; Python RSS increased about 1.50 MiB. |

Raw measurements are produced under `test_runtime_data/resource-probe.json` and
include per-process samples. Docker/Python 3.13 Linux container execution and
cgroup measurements still need to be run in that environment; neither Docker
nor Python 3.13 is installed on this workstation.

Final verification ran all 26 modules named by `Dockerfile.render-robot`: 264
tests completed, 263 passed and the real Linux-only flock test was skipped.
A focused run covering the affected provider/bridge/security behavior plus
WhatsApp intake and crawler regressions passed 94 tests. All 38 new regression
tests also passed with ResourceWarnings enabled and emitted none.
The broader suite exposes existing unclosed-SQLite-connection ResourceWarnings
in older database/scheduler code and fixtures; these remain a follow-up beyond
the six resource paths addressed here. The final full-run log is
`test_runtime_data/final-full-resource-suite.log`.
