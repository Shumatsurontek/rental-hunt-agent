# Chrome side panel

1. Start or deploy the API.
2. Open `chrome://extensions`, enable **Developer mode**, then choose **Load unpacked**.
3. Select this `chrome-extension/` directory.
4. Click the toolbar action, enter the API origin and `ADMIN_API_TOKEN`, and approve access to
   that origin.
5. Create the watcher from a filtered SeLoger results URL. The saved card shows every constraint.
6. Use **Edit watcher** to change URL, constraints, or frequency. **Use open tab** copies a
   filtered SeLoger URL into the form; **Save changes** performs one atomic update.
7. Click **Scan with Chrome**.

The token is stored in `chrome.storage.local`, never synced. The background service worker polls a
durable server outbox once per minute and produces native Chrome notifications. Individual listing
alerts include Interested/Dismiss actions; their event IDs are idempotent. The **Deliver alerts**
button runs the same bounded poll immediately. Chat is read-only and streamed over an authenticated
POST/SSE response. Every traced chat response includes a copyable LangSmith trace ID.

The SeLoger content script has access only to `https://*.seloger.com/*`. A manual scan opens the
exact saved watcher URL in a disposable inactive tab in the same Chrome profile and captures at
most three pages. The backend validates the watcher revision and bounded snapshot, persists it as
an idempotent durable scan job, and never sends raw page content to LangSmith. A homepage URL fails
explicitly and must be replaced through **Edit watcher**.

Changing the URL or preferences creates a clean baseline revision while keeping older listings in
PostgreSQL for traceability. Only current-revision listings appear in the side panel. Changing only
the frequency does not reset the baseline.

With the default `SOURCE_MODE=chrome_extension`, a one-minute extension alarm checks the persisted
watch due-time and starts the same bounded capture automatically. Chrome must remain running. The
server scheduler is disabled in this mode, so it cannot race the extension or repeatedly hit a
container-only anti-bot page.

The UI follows the PostHog-inspired design.md palette and component language: cream canvas, olive
ink, white hairline cards, 6px radii, pastel status callouts, and a yellow-orange primary action.
