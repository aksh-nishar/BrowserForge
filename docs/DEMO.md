# 2-minute demo script

Goal: show that a non-technical person can run a browser job from a button and get evidence of
what happened. Keep to what the tool actually does.

**Before the demo (once):**

```
uv sync
uv run playwright install chromium
uv run browserforge
```

Open <http://127.0.0.1:8000> and do one dry run so the browser is warm. The example recipes use
public practice websites, so you need an internet connection.

| Time | Do | Say |
|---|---|---|
| 0:00 | Show the dashboard. Open `recipes/example_login_scrape.yaml` in an editor for about 10 seconds. | "This is the whole automation: a short text file. Your portal's steps go here, and changing a value is editing text, not code." |
| 0:20 | Select **example_login_scrape**, click **Run**. | "Your team picks the recipe and clicks Run." |
| 0:30 | Watch the progress bar and the step table fill in. | "Each step is timed and logged as it happens." |
| 0:50 | When it says **Complete**, scroll to the screenshots. | "Here is the record of what the page looked like after login, and the table it read." |
| 1:05 | Click **Download Excel report** and open it. | "Three pages of that table, merged into one sheet with a header." |
| 1:25 | Point at the amber notice about the page limit. | "If a scrape is cut short by its safety limit, it says so, so nobody mistakes partial data for the whole thing." |
| 1:40 | Run a recipe that fails (for example the same recipe with a wrong password). | "When something breaks you get a plain-language reason and a screenshot of the page at that moment, not a stack trace." |
| 1:55 | Close. | "Adding your portal means writing a recipe for it. Scheduling and hosting are not in this version; they are possible next steps." |

**Do not claim** anything the tool does not do: there is no scheduling, email delivery, user login
or hosting in this version (see the README's "Scope and limitations").
