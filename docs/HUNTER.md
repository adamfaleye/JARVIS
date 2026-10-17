# The Hunter (acquisitions intelligence)

The Hunter finds motivated-seller records, scores them, and runs your AvaData skip-trace round trip. It lives inside JARVIS Engine, which means your PC. You work it from the **The Hunter** screen in JARVIS: open JARVIS in the Claude desktop app with the PC link on. Voice works too ("Hey Jarvis, what are my top Hillsborough leads?").

## How records flow
fetch → validate → normalize → match (parcel, then address) → de-duplicate → score → save → report

- Each source record is kept untouched in `raw_records`. That's the evidence trail.
- Properties, owners, distress events, contacts and scores are stored separately. Two owners are never merged just because their names look alike.
- Unknown values stay unknown. Bad dates or amounts are cleared and noted, never guessed.
- Re-running a sync or re-importing a file never creates duplicates.

## What is actually supported today
| Source | Category | Status |
|---|---|---|
| Pinellas foreclosure auction calendar (RealAuction) | Foreclosure sale scheduled | Operational |
| Hillsborough foreclosure auction calendar (RealAuction) | Foreclosure sale scheduled | Operational |
| Pinellas tax deed auction calendar (RealAuction) | Tax deed sale scheduled | Operational |
| Every other county/category | — | Not supported yet; use **Manual upload** |

The auction calendars give the case number, parcel, address, judgment or opening bid, and assessed value. They don't give owner names or mailing addresses, which is why skip tracing matters. **Sources → Coverage** shows the live, honest status of each county and category.

The other categories need a source that publishes them: probate, pre-foreclosure (lis pendens), tax-delinquent rolls, code violations, evictions, bankruptcy and divorce. Many counties publish these as downloadable files. Upload those under **Sources → Manual upload**: pick the county and category, then map the columns. Each county that offers an automated portal or API can get its own adapter later (see "Adding a county").

## Skip tracing with AvaData
1. **Skip trace → Export CSV for AvaData** (or select leads in **Leads** and click **Skip-trace**). This exports leads that aren't skip-traced yet and have a street address, with duplicates removed. Exporting does **not** mark anything as skip-traced.
2. Upload the CSV to AvaData. Click **Mark sent** on the batch.
3. When results come back, open **Import AvaData results**, choose the batch and the file, check the column mapping, then import.
4. Rows are matched first by the **Reference** column and then by address. If a row is ambiguous, or the returned name doesn't match the owner on record, it goes to **Review** instead of being attached to the wrong person.

AvaData's exact upload columns couldn't be confirmed from its public documentation. Use **Skip trace → Column template** to rename the headers to whatever AvaData's upload screen asks for. Every export also writes a manifest (`out/skiptrace_<batch>_manifest.csv`) that maps each row to its lead.

## Contact rules
A skip-traced number is **data, not consent**. Each number has separate flags:
- identity verified
- DNC screened
- OK to call manually
- consent reviewed
- SMS eligibility reviewed
- automated outreach approved
- suppressed

Automated outreach can't be approved until consent is reviewed and DNC screening is done. An opt-out anywhere (a text reply, the Do Not Contact list) suppresses that number in The Hunter and in the pipeline. Promoting a lead only copies a phone number that you've marked "OK to call manually".

## Scoring
Each lead gets an explainable score out of 100. The components are distress, equity, ownership length, condition, absentee owner, multiple independent indicators, data completeness, contact match and freshness. Every component shows its reason. The default weights are starting estimates; change them under **Scoring**, and every lead is rescored.

- **Equity:** uses the county's assessed value against the published amount owed. It's an estimate, not market value, and other mortgages or liens are unknown.
- **Repeat filings:** the same filing appearing in several feeds counts once.
- **Sensitive records:** probate, bankruptcy, eviction and divorce records alone send a lead to **Review**. They are never proof that the owner wants to sell, and they're never referenced in outreach.
- **Score never acts alone:** a score never triggers contact, an offer or a contract.

## Controls
- **Sources:** Sync now, Pause/Resume, last success, last error, rate limit.
- **Jobs:** fetched, accepted, new, duplicates, rejected and errors for each job; Cancel.
- **Pausing:** a paused source is skipped by the daily 6 AM scrape too.
- **Failures:** one failing source never stops the others.

## Adding a county
1. Run `python run.py add-county "Pasco" --state FL`. It auto-detects RealAuction foreclosure and tax-deed sites, or you can give it a URL.
2. The source appears in **Sources** as "Not run yet". Click **Sync now**. It only shows as Operational after it returns real records.
3. For a county with its own records portal, copy `engine/adapters/_template.py`, build the adapter, and add `adapter: custom:<name>` in `counties.yaml`.

Never bypass CAPTCHAs, logins or rate limits. Adapters stop and report when a site blocks automated access.

## Tests
`python -m tests.test_hunter` runs the engine checks against synthetic records in a throwaway database.
