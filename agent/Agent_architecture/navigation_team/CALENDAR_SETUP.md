# Owner's calendar: one-time setup

The navigation team reads the owner's notes from a Google Calendar through the
calendar MCP server (`@cocal/google-calendar-mcp`). Setting it up takes about
ten minutes and is done once per Google account.

## 1. Google Cloud: an OAuth client for the Calendar API

1. Go to https://console.cloud.google.com and pick or create a project (any name, e.g. `vibechecker`).
2. **APIs & Services > Library**: search "Google Calendar API", open it, click **Enable**.
3. **APIs & Services > OAuth consent screen** (Google may call this "Google Auth Platform > Branding / Audience"):
   user type **External**, app name `Vibechecker`, your email as support and developer contact, save.
   Under **Audience / Test users** add the Gmail address that owns the calendar. Leave the app in **Testing**.
4. **APIs & Services > Credentials > Create credentials > OAuth client ID**: application type **Desktop app**,
   name `vibechecker-desktop`, create, then **Download JSON**.
5. Move the file somewhere outside the repo, for example
   `~/.config/vibechecker/gcp-oauth-client.json`, and never commit it.

While the app stays in Testing, Google expires the login every 7 days; rerun step 3 below when that happens.

## 2. The calendar itself

In Google Calendar create a calendar named **Vibechecker owner** (Settings > Add calendar > Create new calendar).
Put the demo notes on the simulated dates, e.g. Fri 5 Apr 2024:

- a timed event `big group of 12`, 3:00pm to 5:00pm
- an all-day event `school holiday crowd`
- a timed event `override light=300 volume=45: quieter evenings`, 6:00pm to 10:00pm

Title = the note, start and end = the window, description = extra detail for the planner.

## 3. Install and log in (once)

```bash
cd "Draft 3/vibechecker"
uv sync --extra calendar --extra dev
# agent/.env: uncomment and fill in
#   GOOGLE_OAUTH_CREDENTIALS=/Users/ashley/.config/vibechecker/gcp-oauth-client.json
#   VIBE_CALENDAR=Vibechecker owner
GOOGLE_OAUTH_CREDENTIALS="$HOME/.config/vibechecker/gcp-oauth-client.json" npx -y @cocal/google-calendar-mcp auth
```

The last command opens a browser; sign in with the test-user account and allow calendar access. The
token is stored by the server (default under `~/.config/google-calendar-mcp/`).

## 4. Run

```bash
uv run python run_navigation.py --days 1 --seed 3 --start 2024-04-05 --mode heuristic --preset demo \
    --calendar "Vibechecker owner" --state state_nav/calendar-demo
```

The morning log shows `calendar 'Vibechecker owner': N event(s) on 2024-04-05, N new note(s)`, and
`state_nav/calendar-demo/notes/2024-04-05.json` holds the notes with `"source": "calendar"`.
If the calendar cannot be reached the log says so and the day runs on the notes already on file.
