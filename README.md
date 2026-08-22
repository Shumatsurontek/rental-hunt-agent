# Rental Hunt Agent

A single-user, Dockerized SeLoger rental watcher. It polls one saved search, applies hard
constraints in deterministic Python, asks a bounded Deep Agent to rank eligible discoveries, and
stores durable alerts for a Manifest V3 Chrome extension. The extension delivers native Chrome
notifications with Interested/Dismiss actions and provides deployment status, manual scans,
recent matches, and streamed read-only operator chat in a side panel. Manual scans capture SeLoger
through the user's real Chrome session, so a server-side DataDome block does not masquerade as an
empty or malformed result page.

V0 intentionally has no general-purpose web UI, multi-search/site support, automated contact,
price-change notifications, subagents, or horizontal scaling.

## Runtime shape

`docker compose up` runs two services:

- `app`: FastAPI, one durable-job worker, Playwright Chromium, Deep Agents, read-only operator
  chat, optional LangSmith tracing, and a durable Chrome-notification outbox.
- `postgres`: the relational source of truth, durable job queue, leases, events, notification
  idempotency, agent skill, and rendered preference memory.

`SOURCE_MODE=chrome_extension` is the V0 default. It disables the server-side watch scheduler; a
bounded extension alarm checks once per minute and starts a capture only when the watch is due.
Chrome must therefore be running for scheduled captures. `SOURCE_MODE=playwright` re-enables the
process-local Docker scheduler for environments where container navigation is not blocked. The two
modes never run automatic scans concurrently.

The first complete scan establishes a baseline. It analyzes at most the 25 newest hard matches and
sends one ranked digest showing at most 10. Later scans alert once for every newly discovered hard
match. A model score never suppresses an alert. Price/content changes are versioned but do not
notify in V0.

## Prerequisites

- Docker Desktop or Docker Engine with Compose v2.
- Chrome 116 or newer for the unpacked extension and native notifications.
- Exactly one tool-capable model provider:
  - an Ollama server reachable from the container; or
  - an OpenAI API key.
- A sufficiently narrow HTTPS SeLoger search URL with at most 150 results across at most three
  pages.

## Configure

```bash
cp .env.example .env
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Put the generated value in `ADMIN_API_TOKEN`, then configure exactly one provider.

For OpenAI (the default):

```dotenv
MODEL_PROVIDER=openai
MODEL_NAME=gpt-5.6-luna
MODEL_BASE_URL=
OPENAI_API_KEY=sk-...
```

The GPT-5.6 Chat Completions tool path explicitly uses reasoning effort `none`; the model's default
reasoning mode is not compatible with the function tools used by the Deep Agent.

For Ollama on the Docker host:

```dotenv
MODEL_PROVIDER=ollama
MODEL_NAME=gpt-oss:20b
MODEL_BASE_URL=http://host.docker.internal:11434
OPENAI_API_KEY=
```

Pull and start that exact model before starting the application. The process validates that Ollama
knows the configured model; it does not fall back to OpenAI. Ollama reasoning is explicitly
disabled so the bounded output is reserved for required tool calls and the final schema; its HTTP
timeout is the same 200-second bound as one complete agent assessment.

Blank optional values are treated as unset. An unknown provider, missing model, missing provider
credential/base URL, non-PostgreSQL database URL, or short admin token fails startup.

### LangSmith tracing

Tracing is opt-in and has no hidden fallback. Add a LangSmith API key to `.env`:

```dotenv
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=lsv2_...
LANGSMITH_PROJECT=rental-hunt-agent
LANGSMITH_WORKSPACE_ID=
```

`LANGSMITH_WORKSPACE_ID` is only required for an organization-scoped key. Each durable job and
operator-chat request starts in a function decorated with `@traceable`. LangChain model/agent calls
made inside that function inherit the trace context and appear as child spans. Chat responses and
claimed jobs expose their current root trace ID so the Chrome panel can copy it. Inputs include the
listing or bounded service snapshot, so treat the LangSmith project as sensitive operational data.

## Start and verify

```bash
docker compose up --build -d
docker compose ps
curl --fail http://127.0.0.1:8000/healthz
curl --fail http://127.0.0.1:8000/readyz
docker compose logs -f app
```

`serve` applies Alembic migrations before accepting traffic. `/healthz` is liveness-only;
`/readyz` verifies PostgreSQL, queue capacity, and the background loops. Compose uses `/readyz` for
its application health check.

Run the live model tool-calling and structured-output probe before enabling a watch:

```bash
docker compose run --rm app agent-doctor
```

The doctor requires the model to call `read_file` and return the exact assessment schema. It fails
if the selected model cannot use tools or structured output.

## Configure the watch

Set shell variables locally; these are examples, not additional service configuration:

```bash
export RENTAL_HUNT_TOKEN='the ADMIN_API_TOKEN value from .env'
export RENTAL_HUNT_URL='https://www.seloger.com/list.htm?...'
```

Store preferences first. This endpoint is initialization-only once an active watch exists; later
edits use the atomic watcher configuration endpoint below.

```bash
curl --fail-with-body \
  -X PUT http://127.0.0.1:8000/v1/preferences \
  -H "Authorization: Bearer $RENTAL_HUNT_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "rent_eur_monthly_max": 2000,
    "surface_m2_min": "40.00",
    "rooms_min": 2,
    "furnished": "any",
    "postal_codes_allowed": ["75011", "75012"],
    "soft_preferences": ["quiet", "close to public transport"]
  }'
```

Create the only active watch:

```bash
curl --fail-with-body \
  -X POST http://127.0.0.1:8000/v1/watches \
  -H "Authorization: Bearer $RENTAL_HUNT_TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"url\":\"$RENTAL_HUNT_URL\",\"poll_interval_s\":600}"
```

A second active watch returns `409`. Main-document navigation accepts only HTTPS on `seloger.com`
or its subdomains, with no credentials or non-default port.

To edit the saved URL, frequency, and constraints together:

```bash
curl --fail-with-body \
  -X PUT http://127.0.0.1:8000/v1/watches/REPLACE_WATCH_ID/configuration \
  -H "Authorization: Bearer $RENTAL_HUNT_TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{
    \"watch\": {\"url\": \"$RENTAL_HUNT_URL\", \"poll_interval_s\": 600},
    \"preferences\": {
      \"rent_eur_monthly_max\": 2000,
      \"surface_m2_min\": \"40.00\",
      \"rooms_min\": 2,
      \"furnished\": \"any\",
      \"postal_codes_allowed\": [\"75011\", \"75012\"],
      \"soft_preferences\": [\"quiet\"]
    }
  }"
```

URL or preference changes increment the bounded configuration revision, reset the baseline, and
keep prior listings as historical rows. `/v1/listings` shows only the active revision. A
frequency-only edit keeps the current revision and baseline. Editing waits for durable jobs to
finish, and a browser capture from an older revision is rejected instead of being misapplied.

## API operations

All `/v1/*` routes require the bearer token.

```bash
# Current active watch
curl --fail-with-body \
  -H "Authorization: Bearer $RENTAL_HUNT_TOKEN" \
  http://127.0.0.1:8000/v1/watches/current

# Current constraints
curl --fail-with-body \
  -H "Authorization: Bearer $RENTAL_HUNT_TOKEN" \
  http://127.0.0.1:8000/v1/preferences

# Trigger one scan; the response is 202 with a job_id
curl --fail-with-body \
  -X POST \
  -H "Authorization: Bearer $RENTAL_HUNT_TOKEN" \
  http://127.0.0.1:8000/v1/watches/REPLACE_WATCH_ID/scan

# Inspect a durable job, including terminal last_error
curl --fail-with-body \
  -H "Authorization: Bearer $RENTAL_HUNT_TOKEN" \
  http://127.0.0.1:8000/v1/jobs/REPLACE_JOB_ID

# Cursor-paginated listings; limit is 1–100
curl --fail-with-body \
  -H "Authorization: Bearer $RENTAL_HUNT_TOKEN" \
  'http://127.0.0.1:8000/v1/listings?limit=50'

# Current-revision hard matches only
curl --fail-with-body \
  -H "Authorization: Bearer $RENTAL_HUNT_TOKEN" \
  'http://127.0.0.1:8000/v1/listings?eligible_only=true&limit=100'

# Model, worker, queue, and tracing status
curl --fail-with-body \
  -H "Authorization: Bearer $RENTAL_HUNT_TOKEN" \
  http://127.0.0.1:8000/v1/agent/status

# Pull at most 20 pending Chrome alerts (normally called by the extension)
curl --fail-with-body \
  -H "Authorization: Bearer $RENTAL_HUNT_TOKEN" \
  'http://127.0.0.1:8000/v1/notifications?limit=20'

# Acknowledge one alert only after Chrome accepted it for display
curl --fail-with-body \
  -X POST \
  -H "Authorization: Bearer $RENTAL_HUNT_TOKEN" \
  http://127.0.0.1:8000/v1/notifications/REPLACE_NOTIFICATION_ID/ack

# Replace the current listing feedback with an idempotent client event ID
curl --fail-with-body \
  -X PUT \
  -H "Authorization: Bearer $RENTAL_HUNT_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"value":"interested","event_id":"chrome:manual-1:interested"}' \
  http://127.0.0.1:8000/v1/listings/REPLACE_LISTING_ID/feedback

# Read-only operator chat over a bounded service snapshot
curl --fail-with-body \
  -X POST \
  -H "Authorization: Bearer $RENTAL_HUNT_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"Summarize the queue and recent matches."}]}' \
  http://127.0.0.1:8000/v1/chat

# The same chat as an authenticated SSE token stream
curl --no-buffer --fail-with-body \
  -X POST \
  -H "Authorization: Bearer $RENTAL_HUNT_TOKEN" \
  -H 'Accept: text/event-stream' \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"Summarize the queue and recent matches."}]}' \
  http://127.0.0.1:8000/v1/chat/stream

# Disable or re-enable a known watch
curl --fail-with-body \
  -X PATCH \
  -H "Authorization: Bearer $RENTAL_HUNT_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"enabled":false}' \
  http://127.0.0.1:8000/v1/watches/REPLACE_WATCH_ID
```

Overlapping scans return `409`. Disabling a watch prevents a queued scan from starting; an already
running browser navigation completes within its hard timeout.

Operator chat accepts at most 20 messages of 2,000 characters each, receives at most 10 recent
listings in a 30,000-character snapshot, and times out after 60 seconds. It uses the configured
provider/model directly and has no write tools. The SSE endpoint emits at most 2,000 deltas, then
one `done` event with the complete message, model identity, and LangSmith trace ID. A failure after
headers are sent becomes an explicit `error` event.

Chrome feedback event IDs are idempotent; a later distinct Interested/Dismiss action replaces the
current state and appends a new event.

## Doctor and migration commands

```bash
# Explicit migration command (serve also does this)
docker compose run --rm app db-upgrade

# Model/tool/schema probe
docker compose run --rm app agent-doctor

# Opt-in live source probe; does not persist listings or create browser alerts
docker compose run --rm app source-doctor --url "$RENTAL_HUNT_URL"
```

Only run the source doctor or create a watch when live SeLoger access is intended. CAPTCHA,
persistent anti-bot responses, redirects to login, selector drift, suspicious empty pages, and
incomplete/over-broad results fail explicitly and do not mutate listing presence.

## Chrome side panel

The extension is unpacked in V0:

1. Open `chrome://extensions` and enable **Developer mode**.
2. Choose **Load unpacked** and select the repository's `chrome-extension/` directory.
3. Click the Rental Hunt toolbar action.
4. Enter the API origin (for local Compose, `http://127.0.0.1:8000`) and `ADMIN_API_TOKEN`.
5. Approve Chrome access to that one configured origin.
6. If no watch exists, enter the filtered SeLoger results URL and constraints in the **Watch**
   card; the default frequency is 10 minutes.
7. The saved watcher is rendered as a readable constraint summary. Choose **Edit watcher** to
   prefill and modify URL, budget, surface, rooms, furnished state, postal codes, soft preferences,
   or frequency. **Use open tab** explicitly copies the most recently accessed filtered SeLoger
   URL into the form; nothing is saved until **Save changes** succeeds. The extension mirrors
   budget, minimum surface, minimum rooms, and newest-first ordering into the URL. Location and
   furnished filters must still be selected on SeLoger because their URL encoding is not stable.
8. Keep Chrome notifications enabled for Rental Hunt Agent in macOS System Settings.
9. **Explore with agent** always reopens the exact saved URL in a disposable background tab,
   captures up to three pages, waits for the durable Deep Agent pipeline, delivers ready alerts,
   and then refreshes the ranked hard matches. It never silently replaces the saved search with
   another tab.

After changing unpacked-extension files, use **Reload** on `chrome://extensions`. The panel checks
its version against the running Manifest V3 service worker and disables exploration with an
explicit reload message when Chrome is still executing an older worker.

The token stays in extension-local storage and is not synced. The manifest declares HTTP/HTTPS as
optional host permissions, so no remote origin is granted until the save action requests it. The
required SeLoger-only host permission lets the packaged content script read result cards from the
user's browser session; it cannot run on unrelated sites. Browser captures are bounded to three
pages, 150 cards, 100 structured-data scripts, 5 MB of JSON, and 10 MB total. They enter the same
durable scan job, parser, hard-policy, version/event, assessment, and notification pipeline as
server-side scans. The durable job payload is compacted after success or terminal failure, and
LangSmith receives capture metadata rather than raw HTML. The **Chrome Eyes** block streams the
same safe per-page snapshot counts (listing links, captured cards, structured blocks, and URL)
while the extension navigates; raw page bodies remain outside extension-local status and traces.

The panel shows readiness, model, source mode, queue depth, LangSmith state, bounded watcher
creation/editing with its current revision, recent hard matches, scan job state, pending alerts,
streamed agent responses, and copyable job/chat trace IDs. A bounded service worker checks scan
due-times and polls at most 20 outbox items once per minute. It displays each alert through the
native Notifications API, then acknowledges it.
Listing notifications expose Interested and Dismiss buttons; **Deliver alerts** triggers the same
bounded notification poll immediately for debugging. Chat cannot mutate service state.

The visual system is adapted from the
[PostHog design.md reference](https://github.com/VoltAgent/awesome-design-md/tree/main/design-md/posthog):
warm cream canvas, flat white cards with olive hairlines, compact 6px radii, semantic pastel
callouts, and one yellow-orange primary CTA. It intentionally avoids gradients and decorative
drop shadows.

For a remote deployment, terminate TLS in front of the app and configure the HTTPS origin in the
panel. Compose intentionally publishes port 8000 to loopback only; do not expose the bearer-token
API directly over plaintext internet traffic.

## Failure and recovery behavior

- A source attempt is retried at most twice inside a 90-second overall scan window.
- DataDome CAPTCHA markup and “temporarily restricted” pages are classified as `source_blocked`.
  Use **Explore with agent** with a normal filtered SeLoger tab when container Playwright is
  blocked.
- Assessment jobs make two attempts with at most two concurrent model calls. Exhaustion creates a
  clearly marked analysis-unavailable notification.
- Notification preparation makes at most five attempts. The database retains at most 100 pending
  alerts and `/readyz` fails when that outbox is full, so alerts are never silently dropped.
- The extension uses the durable database notification ID as the Chrome notification ID, persists
  at most 100 local action mappings, and acknowledges only after Chrome accepts display. A crash
  between those operations recovers the same ID instead of creating a second logical alert.
- Jobs and scans are persisted transactionally. Expired leases are reclaimable after restart;
  outbox and event keys prevent logical replay.
- When LangSmith is enabled, every claimed job attempt gets a fresh persisted root trace ID. A
  retry therefore remains distinct and `/v1/jobs/{id}` exposes the latest attempt's trace ID.
- At 500 active jobs, new scans stop and `/readyz` returns `503`; queued jobs are never dropped.
- Listings deactivate only after absence from three complete scans. Failed/partial scans never
  advance absence.
- Twenty listing versions are retained per listing. A version referenced by active delivery work
  is protected during pruning.
- Debug captures retain at most 20 failed-page HTML/screenshot pairs for seven days in the
  `debug-data` volume. Scan/job history cleanup is bounded to 1,000 rows per hourly cycle and 30
  days.

Inspect dead jobs and notification state directly when operating V0:

```bash
docker compose exec postgres psql -U rental_hunt -d rental_hunt -c \
  "SELECT id, kind, attempts, last_error, updated_at FROM jobs WHERE status = 'dead' ORDER BY updated_at DESC LIMIT 20;"

docker compose exec postgres psql -U rental_hunt -d rental_hunt -c \
  "SELECT id, kind, status, last_error, created_at FROM notifications WHERE status <> 'sent' ORDER BY created_at DESC LIMIT 20;"
```

Back up the `postgres-data` volume/database before upgrades. `docker compose down` keeps volumes;
`docker compose down -v` permanently removes database, browser-profile, and debug data.

## Local development

Python is fixed to 3.12 and all resolved dependencies are recorded in `uv.lock`.

```bash
uv sync --frozen
uv run playwright install chromium
make check
```

The verification gate is:

```bash
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
node --check chrome-extension/background.js
node --check chrome-extension/content-capture.js
node --check chrome-extension/sidepanel.js
docker build -t rental-hunt-agent:v0 .
docker compose up -d
docker compose ps
docker compose run --rm app agent-doctor
docker compose run --rm app source-doctor --url "$RENTAL_HUNT_URL"  # explicit live opt-in
```

Unit/integration tests use fake models and a relational SQLite harness for speed. Alembic revision
bounds are tested locally; migrations and the production runtime target PostgreSQL. Playwright
tests serve recorded SeLoger markup through Chromium and exercise watcher creation plus atomic
editing in the real side panel. Live model, LangSmith, Chrome notification, and SeLoger gates
require operator credentials and are never run implicitly.
