# LEARN (Brightspace) API + Login Flow — Rebuild Spec

Purpose: everything known about how this project talks to UWaterloo LEARN
(Brightspace/D2L), written so another agent can rebuild the data layer from
scratch and then improve on it. Everything marked **[verified]** was observed
against a real account during development; **[unverified]** means inferred or
never confirmed; **[gap]** is an explicit opening for improvement.

Source of truth in this repo: `learn/` (auth, cdp, session, paste, transport,
client, models, exceptions) and `agent/tools.py`, `agent/aggregation.py`,
`agent/serialize.py`, `agent/course_registry.py`. Narrative history of
live-found bugs: `HANDOFF.md`.

---

## 1. Big picture

```
dedicated Chrome (CDP, profile in .auth/chrome-profile)
   └─ user completes WatIAM (ADFS) + Duo → whoami succeeds → cookies harvested → session store
        └─ requests + stored cookies
             └─ GET https://learn.uwaterloo.ca/d2l/api/{lp|le}/{ver}/...  → JSON
```

- **Auth is cookie-only.** No OAuth token, no `Authorization` header, no
  Valence ID/Key HMAC signing. The Valence "documented" API accepts the same
  session cookies the LEARN web UI uses. Registering a real Valence app needs
  Waterloo IT approval, which a student can't self-serve — hence the cookie
  approach. **[verified]**
- **Everything is GET and returns JSON.** No HTML scraping was ever needed.
  No write endpoint is implemented or should be. **[verified]**
- Host: `https://learn.uwaterloo.ca` (constants `BASE_URL`, `LEARN_URL`).

---

## 2. Login flow (current: agent2learn-style CDP sign-in)

Adapted from https://github.com/ManagementMO/agent2learn (Apache-2.0). Code:
`learn/auth.py` (orchestration + verify), `learn/cdp.py` (browser + CDP),
`learn/session.py` (store), `learn/paste.py` (fallback), `learn/transport.py`.

### 2.1 Steps
1. **Reuse first.** `session.load()` → `auth.verify()`; if whoami succeeds, no browser opens.
2. **Dedicated browser.** Locate Chrome/Edge/Chromium; launch with
   `--remote-debugging-address=127.0.0.1 --remote-debugging-port=0 --user-data-dir=.auth/chrome-profile --no-first-run --no-default-browser-check`
   (own profile, never the user's everyday one; persists so Duo "remember this device" works).
   Read `DevToolsActivePort` (refuse symlinks), validate `/json/version` is a Chrome/Edge product,
   the websocket is loopback on that port, and the path matches the marker. If a browser already
   owns the profile, attach only after confirming via `ps` that it owns that profile.
3. **Gate every request** with CDP `Fetch.requestPaused` (`urlPattern: *`): allow only
   `https` on 443 to `learn.uwaterloo.ca`, `adfs.uwaterloo.ca`, and `*.duosecurity.com`
   (DNS-label boundary match). Others get `BlockedByClient`; a blocked Document/iframe aborts
   with the host name (fallback hint: `auth --paste`).
4. `Page.navigate` → `https://learn.uwaterloo.ca/d2l/home`. User completes WatIAM + Duo in that window.
   **Nothing here reads or fills the form.**
5. **Detect login by API, not URL.** Every 0.5 s (max 300 s) `Runtime.evaluate` runs in-page JS:
   `GET /d2l/api/versions/` → for each lp version (latest first) `GET /d2l/api/lp/{v}/users/whoami`;
   success = JSON with non-empty `Identifier`. Retry only the transient "target/context" CDP
   error (-32000) for 10 s (navigation race).
6. **Harvest.** `Storage.getCookies` → keep only `d2lSessionVal`, `d2lSecureSessionVal`,
   `XSRF-TOKEN`/`XSRF_TOKEN`/`d2lXSRF-Token`, scoped exactly to the LEARN host. Require the two
   session cookies. `xsrf` = the XSRF cookie value.
7. **Verify + store.** Re-verify over plain `requests`, store `Session{base_url, cookies, xsrf,
   harvested_at, user_id}` in the OS keyring (if `keyring` installed; macOS calls run in a
   timeout-bounded child process) else `.auth/session.json` (0600, atomic write).
8. **Close** the browser we launched (`Browser.close` → terminate → kill, bounded), remove our
   `DevToolsActivePort`.

### 2.2 Using the session
`SessionTransport` replays the cookies with `requests` (`trust_env=False`, no redirects,
same-origin only, `X-Csrf-Token` header, 429/5xx retry with backoff/Retry-After). An HTML
body or redirect where JSON is expected = **expired** → `LearnAuthenticationError`
("run: python agent.py auth"). `python agent.py auth --check` exits **75** if expired.

### 2.3 Fallbacks / commands
- `auth --paste`: hidden-TTY paste (refuses piped input) of a `Cookie:` header, JSON export
  or DevTools cookie table; verified via whoami before storing.
- `auth --clear-profile`: removes session + browser profile after typed confirmation.
- `logout`: removes the session only (Duo trust kept).

### 2.4 Session lifetime **[partially verified]**
- Duo browser-trust persists in the dedicated profile across runs; real duration unknown.
- WatIAM/ADFS session expires sooner than Duo trust; when `d2lSessionVal` lapses the stored
  cookies stop working and a fresh interactive sign-in is needed (usually password only).

### 2.5 Improvement room (login)
- **[done]** Mid-run re-auth: `SessionTransport(reauth=auth.authenticate)` re-runs sign-in once on
  expiry (401 / redirect / HTML), swaps cookies, retries the request once; a failed sign-in is not
  retried for 120 s. In Discord this blocks that turn (up to 300 s) while the window is open.
- **[gap]** No human ping when Duo approval is needed for unattended (Discord) use.
- **[gap]** Cookie expiry is not recorded; log `expires` to learn the true re-login cadence.
- **[gap]** CSRF header is sent on GETs only because agent2learn does; unverified that it's needed.
- **[gap]** Windows/Linux paths ported but untested here (macOS Chrome verified).

---

## 3. API conventions

### 3.1 Version discovery
`GET /d2l/api/versions/` — called once, cached. **[verified]**
```json
[
  {"ProductCode": "le", "LatestVersion": "1.96", "SupportedVersions": ["1.75","1.96"]},
  {"ProductCode": "lp", "LatestVersion": "1.62", "SupportedVersions": ["1.47","1.62"]}
]
```
Build `{lp}` / `{le}` from `ProductCode` → `LatestVersion`. Don't hardcode
(Brightspace upgrades bump these). `lp` = platform (enrollments/users),
`le` = learning environment (per-course tools). Note: image URLs in the
enrollments payload show older `lp` versions (e.g. `1.9`, `1.47`) — those are
just what LEARN emitted, not what to call.

### 3.2 Course id
`{orgUnitId}` below is the numeric `OrgUnit.Id` from enrollments (as a string).
It is interpolated into the URL path → **validate it against the known set of
ids returned by enrollments before use** (path-injection surface when an LLM
supplies it; see `agent/course_registry.py`: lazy cache, one refresh-and-recheck
on a miss, inactive courses count as known).

### 3.3 Status / error handling contract
| Response | Meaning | Handling in this project |
|---|---|---|
| 200 + JSON | ok | parse |
| **401** | session missing/expired | raise `LearnAuthenticationError`; **never** convert to empty data |
| **403** + `{"Errors":[{"Message":"Not Authorized"}]}` | tool disabled for that course *or* no permission (indistinguishable) | raise `LearnToolUnavailableError`; per-course getters map it to `[]` |
| 404 / other non-2xx | unexpected | `LearnApiError(status, url)` |
| **200 with `{"Errors":[...]}` body** | defensive case, not actually observed | treat as 403 |

Typed exceptions exist because "expired session → `[]`" once made the agent
tell the user they had no assignments. Keep the 401 ≠ empty distinction.
Observed: non-credit orientation modules commonly 403 on Dropbox/Grades/etc.

### 3.4 Shared shapes
- Timestamps: ISO-8601 UTC with `Z` (`2026-05-16T03:59:59.000Z`). Parse to tz-aware UTC; display in `America/Toronto`.
- Rich text: `{"Text": "...", "Html": "..."}`; empty string when absent → normalize to `None`. **Exception:** quizzes nest it one level deeper (`Description.Text.Html`).
- Paged list responses: `LearnClient._get_collection()` follows a bare array, `{"Objects":[…],"Next":url}`, and `{"Items":[…],"PagingInfo":{"HasMoreItems","Bookmark"}}` (re-sending `?bookmark=`). It raises `LearnApiError` — never a partial list — on a repeated page, an unexpected shape, an invalid `PagingInfo`, a `Next` link to another host, or more than 50 pages. Only the `Items`/`PagingInfo` shape (enrollments) is confirmed live here; `Objects`/`Next` is per agent2learn.

---

## 4. Endpoints (all `GET`, base `https://learn.uwaterloo.ca`)

### 4.1 `GET /d2l/api/versions/`
See §3.1. Auth required? Used post-login. Returns array above.

### 4.2 Courses — `GET /d2l/api/lp/{lp}/enrollments/myenrollments/[?bookmark=…]`
Paged. Each item (fixture-accurate):
```json
{
  "OrgUnit": {
    "Id": 1000001,
    "Type": {"Id": 3, "Code": "Course Offering", "Name": "Course Offering"},
    "Name": "Intro to Widgets - Spring 2026",
    "Code": "WIDG101_smith_1265",
    "HomeUrl": "https://learn.uwaterloo.ca/d2l/home/1000001",
    "ImageUrl": "https://learn.uwaterloo.ca/d2l/api/lp/1.47/courses/1000001/image"
  },
  "Access": {
    "IsActive": true, "StartDate": "…Z", "EndDate": "…Z", "CanAccess": true,
    "ClasslistRoleName": "Student", "LISRoles": ["urn:lti:instrole:ims/lis/Student"],
    "LastAccessed": "2026-08-17T13:22:10.000Z"
  },
  "PinDate": null
}
```
- **Filter to `OrgUnit.Type.Id == 3`** (Course Offering). Type 1 = the whole "University of Waterloo" Organization row (Id 6606, `Code: null`) is also returned and must be skipped.
- `Code` is the raw section code (`MSE232_prof_1265`: course, instructor username, term code), not a pretty "MSE 232". Prettifying was deliberately not inferred. `Name` is free text. **[gap: derive display code + term from `Code` (`1265` = Waterloo term code: 1 + yy + month-digit) if wanted — term-code format is inferred, unverified.]**
- → `Course{id:str, name, code|None, is_active, url=HomeUrl, last_accessed_at, raw}`.
- "Active" = `Access.IsActive`. Cross-course tools only touch active courses; id validation accepts inactive too.
- An internal undocumented endpoint `/d2l/le/manageCourses/api/mycourses` (powers the My Courses widget) was seen and deliberately not used — enrollments is a superset.

### 4.3 Assignments — `GET /d2l/api/le/{le}/{orgUnitId}/dropbox/folders/`
Plain array of dropbox folders. Used fields:
| JSON | → model | Notes |
|---|---|---|
| `Id` | `id` | |
| `Name` | `name` | |
| `CustomInstructions.Html` | `description` | |
| `DueDate` | `due_at` | **nullable** (many have none) |
| `Assessment.ScoreDenominator` | `max_points` | |
| `GradeItemId` | `grade_item_id` | join key to grades |
| `IsHidden` | `is_hidden` | |

Other keys seen but unused: `CategoryId, Attachments, Availability, DropboxType, GroupTypeId, ActivityId (opaque LTI-style URN, not a page link), IsAnonymous, SubmissionType, CompletionType, AllowableFileType, DisplayInCalendar, NotificationEmail, LinkAttachments, …`.
**Unreliable for Student role:** `TotalFiles, UnreadFiles, FlaggedFiles, TotalUsers, TotalUsersWithSubmissions, TotalUsersWithFeedback` are all `-1`. So **submission status is not obtainable here** — do not imply it. **[gap: try `/dropbox/folders/{id}/submissions/` for own submissions — never tried.]**
Grade joining: fetch grades (4.4), index by `GradeObjectIdentifier` (string), match on `str(GradeItemId)`; fill `points_earned/possible`. Dropbox alone has no grade.
No per-assignment browsable URL exists in this payload. **[gap: could construct `/d2l/lms/dropbox/user/folder_submit_files.d2l?db={Id}&ou={orgUnitId}` — a known D2L UI route, unverified here.]**

### 4.4 Grades — `GET /d2l/api/le/{le}/{orgUnitId}/grades/values/myGradeValues/`
Plain array, own grades only:
```json
{
  "PointsNumerator": 8.5, "PointsDenominator": 10.0,
  "WeightedNumerator": 8.5, "WeightedDenominator": 10.0,
  "GradeObjectIdentifier": "700001", "GradeObjectName": "Assignment 1 - Basics",
  "GradeObjectType": 1, "GradeObjectTypeName": "Numeric",
  "DisplayedGrade": "85 %",
  "Comments": {"Text": "Good work.", "Html": "<p>Good work.</p>"},
  "PrivateComments": {"Text": "", "Html": ""},
  "LastModified": "2026-05-20T14:00:00.000Z", "LastModifiedBy": null, "ReleasedDate": null
}
```
- `percentage` = parse `DisplayedGrade` only when it ends in `%` (`"51.18 %"`); else `None`. `displayed_grade` verbatim is authoritative. Don't recompute from points.
- Only `GradeObjectTypeName == "Numeric"` ever observed → `letter_grade` always `None`. **[gap: SelectBox/PassFail/Calculated types unseen.]**
- `Weighted*` fields exist but are unused; course weights/categories unknown.
- **No overall/final course grade.** `/grades/final/values/myGradeValue` returned 404. Naively summing points was caught as a hallucination source. **[gap: `GET /d2l/api/le/{le}/{orgUnitId}/grades/` (grade item definitions: weights, categories, MaxPoints) is listed in HANDOFF as discovered but isn't called by `LearnClient` — a correct weighted calc would need it, plus handling of drop-lowest/bonus/ungraded; or find a working final-grade endpoint.]**

### 4.5 Quizzes — `GET /d2l/api/le/{le}/{orgUnitId}/quizzes/`
Returns **either** a bare array (fixtures) **or** `{"Objects":[…], …}` (a paged envelope) — the client handles both. Item:
```json
{"QuizId":800001,"Name":"Quiz 1 - Fundamentals","AutoExportToGrades":true,"IsActive":true,
 "GradeItemId":700010,"IsAutoSetGraded":true,
 "Instructions":{"Text":{"Text":"","Html":""},"IsDisplayed":false},
 "Description":{"Text":{"Text":"15 minutes, one attempt.","Html":"<p>…</p>"}}}
```
→ `Quiz{id,name,is_active,grade_item_id|None,description=Description.Text.Html,due_at=DueDate,start_at=StartDate,end_at=EndDate}`.
**[unverified live]** The date fields are read as `DueDate`/`StartDate`/`EndDate` (as agent2learn does); they parse to `None` when absent. Quizzes appear in "upcoming" only when `DueDate` is present. No attempts/scores (`/quizzes/{id}/attempts/`) **[gap]**.

### 4.6 Announcements — `GET /d2l/api/le/{le}/{orgUnitId}/news/`
Plain array:
```json
{"PinnedDate":null,"Id":900001,"IsHidden":false,"Attachments":[],"CreatedBy":123456,
 "CreatedDate":"…Z","LastModifiedBy":123456,"LastModifiedDate":"…Z",
 "Title":"Final Exam Updates","Body":{"Text":"…","Html":"<p>…</p>"}}
```
→ `Announcement{id,title,body=Body.Html,is_hidden,created_at,updated_at}`. No "read" flag. `Attachments` and `PinnedDate` unused **[gap]**. Reality check: courses often only publish due dates here as free text (e.g. MSE 263 had no Dropbox items at all), and one course had 52 announcements; the agent falls back to announcement text and must label it as non-structured.

### 4.7 Content tree — `GET /d2l/api/le/{le}/{orgUnitId}/content/toc` (no trailing slash)
```json
{"Modules":[{"ModuleId":600001,"Title":"Week 1","IsHidden":false,"SortOrder":1,
  "StartDateTime":null,"EndDateTime":null,"IsLocked":false,
  "Modules":[ /* nested, same shape */ ],
  "Topics":[{"TopicId":610001,"Identifier":"610001","TypeIdentifier":"Link","Title":"Course Outline",
    "Url":"https://outline.uwaterloo.ca/viewer/view/example","GradeItemId":null,
    "LastModifiedDate":"…Z","Description":{"Text":"","Html":""},
    "IsHidden":false,"IsLocked":false,"IsBroken":false,"Unread":false,"Bookmarked":false,
    "ActivityId":"https://ids.brightspace.com/activities/contenttopic/…","CompletionType":2,
    "ToolId":null,"ToolItemId":null,"ActivityType":2,"SortOrder":1,"IsExempt":false}]}]}
```
Walk modules recursively; flatten `Topics` into `ContentItem{id=TopicId,module_title=<immediate parent title>,title,type=TypeIdentifier,url|None,description,grade_item_id,updated_at}`. **Optional keys** (`Url`, `Description`, `GradeItemId`, `ActivityId`) are absent on some topics (e.g. `File`) — use `.get`. `TypeIdentifier` values seen: `Link`, `File`; full enum unknown **[gap]**. Nested module path is lost in flattening **[gap: keep breadcrumb]**. Actual file download (`/d2l/api/le/{le}/{org}/content/topics/{topicId}/file`) not implemented **[gap/unverified]**. Hidden/locked flags ignored **[gap]**.

### 4.8 Discussion forums — `GET /d2l/api/le/{le}/{orgUnitId}/discussions/forums/`
Plain array. Client returns raw-ish dicts `{id=ForumId, name=Name, description_html=Description.Html, is_locked=IsLocked, is_hidden=IsHidden}`. Implemented in `LearnClient` but **not exposed as an agent tool** and no model exists. Topics/posts endpoints (`…/forums/{id}/topics/`, `…/topics/{id}/posts/`) not explored **[gap]**.

### 4.9 Grade item definitions — `GET /d2l/api/le/{le}/{orgUnitId}/grades/`
Noted as discovered/working in HANDOFF but **not implemented in code** and shape not captured here **[gap]**.

### 4.10 Not explored (candidates)
Calendar (`/d2l/api/le/{le}/{org}/calendar/events/…` and `/d2l/api/le/{le}/calendar/events/myEvents/` — a single call could replace per-course due-date scraping if enabled; **unverified**), `/d2l/api/lp/{lp}/users/whoami`, classlist, rubrics, own dropbox submissions, quiz attempts, checklist, "my notifications"/activity feed. All speculative until tried with a live session.

---

## 5. Domain models (`learn/models.py`, frozen dataclasses, each with `raw`)

| Model | Fields |
|---|---|
| `Course` | id, name, code, is_active, url, last_accessed_at |
| `Assignment` | id, course_id, name, description, due_at, max_points, grade_item_id, points_earned, points_possible, is_hidden |
| `Grade` | id, course_id, name, points_earned, points_possible, percentage, displayed_grade, letter_grade(always None), comments, graded_at |
| `Quiz` | id, course_id, name, is_active, grade_item_id, description |
| `Announcement` | id, course_id, title, body, is_hidden, created_at, updated_at |
| `ContentItem` | id, course_id, module_title, title, type, url, description, grade_item_id, updated_at |

All ids are `str`. `Course` also carries inferred `display_code`/`term`/`term_label` (see `learn/terms.py`) and `Assignment.attachments` (`Attachment{id,name,size_bytes}`, metadata only). Deliberately absent (unreliable or unverified): submission status, assignment URL, overall grade.

---

## 6. LLM-facing tool layer (what a consumer of the API exposes)

`build_tools(client, decider=None)` returns a whitelist `{name: callable}`;
nothing is exposed just because the client has it. All outputs are plain
JSON, `raw` and `grade_item_id` stripped.

| Tool | Args | Returns |
|---|---|---|
| `get_courses` | none | `[Course…]` (always refreshes registry) |
| `get_assignments` | `course_id` | `[Assignment…]` with grades joined |
| `get_grades` | `course_id` | `[Grade…]` |
| `get_quizzes` | `course_id` | `[Quiz…]` |
| `get_announcements` | `course_id` | `[Announcement…]`, bodies stripped + truncated to 500 chars + marker |
| `get_announcement_detail` | `course_id, announcement_id` | one announcement, full stripped text, or `not_found` error |
| `get_course_content` | `course_id` | `[ContentItem…]` |
| `get_upcoming_items` | `days_ahead=14` (1–365) | cross-course result (below), assignments only |
| `search_announcements` | `query (≤200 chars), course_id?` | cross-course result, searches full untruncated text |
| `search_course_content` | `query, course_id?` | cross-course result |
| `get_recent_changes` *(only if a snapshot store is passed)* | none | `{first_run, since, checked_at, courses_checked, changes[], errors[]}` — diff against the saved snapshot, then advances it (see `agent/changes.py`) |
| `get_important_announcements` *(only if `laya` installed)* | `course_id?, min_score=0.5, limit=10 (≤50)` | cross-course result with `score`, `signals`, `source:"local_classifier_estimate"` |

Cross-course result shape (identical across tools so a course can't silently vanish):
```json
{"courses_checked":[{"course_id","course_name"}], "items|results":[…],
 "courses_with_no_results":[…], "errors":[{"course_id","course_name","message"}], "note":"…"}
```
`get_upcoming_items` adds `window:{from,to,days_ahead}`, sorts by `due_at`, tags each item `source:"structured_assignment_data"`. "Now" comes from `America/Toronto` wall clock, never from the model.

Serialization rules: dataclass→dict, datetime→ISO string, fields named `body|description|comments` HTML-stripped (+ truncated to 500 chars unless detail tool). Every object gets `_content_source: "learn_data_untrusted"` — LEARN text is instructor-controlled and must be treated as data, never instructions (prompt-injection defense).

Error dicts (returned, never raised, to the model): `{"error":True,"type":…,"message":…}` with types `authentication_expired`, `api_error`, `tool_error`, `unknown_tool`, `invalid_course_id`, `not_found`, `triage_unavailable`, `tool_call_limit_exceeded`, `duplicate_tool_call`, `result_budget_exceeded`.

Guardrails (`agent/limits.py`, `agent/loop.py`): `MAX_TOOL_ROUNDS=20`, ≤10 calls/round, ≤30 calls/turn, ≤500 KB cumulative results/turn, duplicate identical calls refused.

---

## 7. Rebuild checklist (minimal viable path)

1. `pip install requests websocket-client python-dotenv` (optional: `keyring`).
2. Implement §2: dedicated Chrome over CDP → gate requests → poll `/users/whoami` → harvest the whitelisted cookies → store; plus the `requests` transport that replays them.
3. Implement `_get(url)` per §3.3 and `_versions()` per §3.1 on top of that transport's `.get`.
4. Implement the seven getters in §4.2–4.8 with the models in §5; mirror the null-handling called out per endpoint.
5. Add the course-id registry (§3.2) before exposing anything to an LLM.
6. Test offline: a fake transport whose `.get(url)` returns `(status, body)` from a routes dict + the JSON fixtures in `tests/fixtures/` (fixtures are synthetic but shape-faithful).
7. Smoke-test live: courses → one course's assignments/grades/announcements; deliberately hit a 403 course and a logged-out session (HTML login page → expired).

## 8. Biggest opportunities, ranked

1. Re-auth/refresh without restarting + human ping for Duo (unlocks unattended use).
2. Calendar and own-submission endpoints for real due-date + submitted/not-submitted status (currently impossible → announcement-text fallback).
3. Grade item definitions → correct weighted course grade, or find a final-grade endpoint.
4. Quiz attempts and live confirmation of quiz date fields; discussion topics/posts; content file download.
5. Concurrency (calls are sequential per course now) and short-TTL caching. (Pagination is handled generically, but only the `Items`/`PagingInfo` shape is confirmed live — enrollments; `Objects`/`Next` is per agent2learn.)
6. Distinguish 403 "tool disabled" vs "no permission" (e.g. probe `/d2l/api/lp/{lp}/courses/{id}` or tool-enabled flags) so errors are meaningful.
7. Structural provenance (`source` field on announcement-derived dates) rather than relying on the model's caveat.
