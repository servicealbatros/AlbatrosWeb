# Albatros on Render

Prepared configuration, not a completed migration.

## Public website

Render Static Site serves `website/dist` at `servicealbatros.ro` and `www.servicealbatros.ro`.
It contains workshop presentation only. The login link leads to `app.servicealbatros.ro/receptie/login`.
Add the approved public address, telephone, hours and logo before launch.

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
Audit and add only source files; exclude .env, browser profiles, database dumps, recordings and exports.
Confirm the selected paid backend/disk costs before provisioning them.
Apply `render.yaml`, supply secrets through Render, verify deploys, then configure DNS using
the exact A/CNAME values shown in Render. RoTLD registration alone is not necessarily DNS hosting.
Verify the public site, HTTPS, unauthenticated route denial and a signed webhook before cutover.
