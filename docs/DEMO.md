# Demo scripts and how to pitch it

Goal: show that a non-technical person can run a browser job from a button and get evidence of
what happened. Keep to what the tool actually does.

**Before any demo (once):**

```
uv sync
uv run playwright install chromium
uv run browserforge
```

Open <http://127.0.0.1:8000> and do one dry run so the browser is warm. The example recipes use
public practice websites, so you need an internet connection.

---

## Demo 1: one run, two minutes

| Time | Do | Say |
|---|---|---|
| 0:00 | Show the dashboard. Open `recipes/example_login_scrape.yaml` in an editor for about 10 seconds. | "This is the whole automation: a short text file. Your portal's steps go here, and changing a value is editing text, not code." |
| 0:20 | Select **example_login_scrape**, click **Run**. | "Your team picks the recipe and clicks Run." |
| 0:30 | Watch the progress bar and the step table fill in. | "Each step is timed and logged as it happens." |
| 0:50 | When it says **Complete**, scroll to the screenshots. | "Here is the record of what the page looked like after login, and the table it read." |
| 1:05 | Click **Download Excel report** and open it. | "Three pages of that table, merged into one sheet with a header." |
| 1:25 | Point at the amber notice about the page limit. | "If a scrape is cut short by its safety limit, it says so, so nobody mistakes partial data for the whole thing." |
| 1:40 | Run a recipe that fails (for example the same recipe with a wrong password). | "When something breaks you get a plain-language reason and a screenshot of the page at that moment, not a stack trace." |
| 1:55 | Close. | "Adding your portal means writing a recipe for it, or generating one from the form's address." |

---

## Demo 2: a whole spreadsheet, about three minutes (the one that sells)

This shows the case where the time saving is obvious: many records, one form. Use the prospect's
own form and 5 of their own rows if you can; otherwise the practice form works. Prepare a small
`.csv` first (`First Name`, `E-mail`, ... one row per person) and put one deliberately wrong value in
one row.

| Time | Do | Say |
|---|---|---|
| 0:00 | In **Create a recipe from a web form**, paste the form's address and press **Scan the page**. | "Instead of writing selectors, I paste the page address. It finds the fields for me." |
| 0:30 | Untick anything unwanted, pick the submit button, save the recipe. | "I confirm what it found. The recipe uses placeholders instead of fixed values." |
| 0:50 | Choose **Upload a sheet**, pick the `.csv`. Point at the column-mapping table. | "Your sheet's columns are matched to the form's fields. If it guessed wrong I change it here." |
| 1:10 | Tick **Test run**, press **Run**. Open the screenshots. | "A test run fills the form in but stops before the submit button, so you see exactly what it will do first." |
| 1:40 | Untick **Test run**, press **Run**. Watch the row counter. | "Now it does it for real, one row at a time. This many rows by hand is hours of typing." |
| 2:15 | Point at the failed row and its reason, then at the Excel `Run_Report` sheet. | "One row failed, and it tells you which one and why. The others went through. This report is your proof of what was submitted." |
| 2:45 | Show the **Scheduled runs** card. | "If it repeats every week, it can run itself, as long as this program is running on a computer that is on." |

---

## How to pitch it

**In one line:** "Stop retyping spreadsheets into websites. Upload the sheet, point it at the form, and it
fills in every one for you, with a report of what happened."

**The order that works with a business owner:**

1. **Their pain.** "How many hours a week does your team spend copying data from Excel into a
   portal?" Let them answer with their own number.
2. **The arithmetic, with their numbers.** For example: 150 forms at about 5 minutes is 12+ hours.
   Say plainly these are estimates, and offer to time one form together.
3. **Trust.** Show the failure screenshot and the per-row report: nothing is silent, and every
   submission has evidence.
4. **A live demo on their own form**, with five of their own rows and a test run first. This does
   more than any slide.
5. **A small offer.** "Give me one repetitive task. I'll set it up and show you a real run before
   you decide."

**Sell the outcome, not the technology:** "12 hours back" and "fewer typos" land better than
"browser automation".

**Be honest about the limits** (it builds trust, and it is true):

- It suits a form or portal a person could fill in by hand. Sites with CAPTCHAs or two-factor login
  are out.
- When a site redesigns, the recipe needs updating. That is ordinary maintenance.
- A one-off form is faster by hand. The tool pays off on repetition or volume.
- The generator writes recipes for simple, single-page forms; harder sites still need a recipe
  written and tested by hand, which is the part you are offering as a service.

**Do not claim** anything the tool does not do: there is no email delivery, hosting, user login on
the dashboard or catch-up for missed scheduled runs (schedules run only while the program is
running). See the README's "Scope and limitations".
