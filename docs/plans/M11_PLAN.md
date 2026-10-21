# M11 — Media v2 (approved 2026-10-21)

> Tickets: **CUT-77** (epic) · **CUT-78** M11.1 files, albums, provenance · **CUT-79** M11.2 stock
> browser · **CUT-80** M11.3 Google Drive sync and write-back. Branch `m11-media`.
> Kickoff: `docs/plans/M11_KICKOFF.md`. Decisions under "Risks" confirmed 2026-10-21.
> Phone copy of this plan: https://claude.ai/artifact/WXaUp9e8KQ3K96VL2UMJxC

## Goal

M11 makes getting photos into CuteBot and managing them something a non-technical brand owner
can do from where they already keep photos. Three paths: send files or albums to the bot at
full quality, share a Google Drive folder once and let the bot pull from it nightly and write
back what was posted or banned, and browse the library from the chat with thumbnails and
one-tap ban or delete.

Why now: M10 keyed stock by brand, so each of these lands per brand without rework. Stock is
the input the whole pipeline starves without, and today the only way in is a compressed
Telegram photo or a shell upload to the Railway volume.

Size: about one week, two sessions, one migration.

## Scope

**In.** Telegram `document` uploads (originals, not the compressed `photo`) and media groups
up to 10 files, with HEIC converted to JPEG on import and duplicates skipped by hash.
`/stock` with pages of ten thumbnails, usage per image, inline 🚫 Ban and 🗑 Delete, reachable
from the console card. Google Drive folder sync per brand: the owner shares a folder with the
bot's service-account address, `/drive <link>` registers it, a nightly job and a `/sync`
button pull new images, and published or banned images are moved into `posted/` and
`banned/` sub-folders in the owner's Drive. A `stock_images` table that records where each
file came from.

**Out.** Google Photos (its Library API no longer exposes user albums; the Picker API
`/import` flow is a separate optional ticket, created only if a brand asks). Object storage
(S3 or R2): the Railway volume stays, `image_ref` stays an opaque key so the store can move
later inside `app/stock.py`. A Mini App gallery. Dropbox.

## Today and after

Today the library is a folder scan: `app/stock.py` lists `stock/<brand prefix>/` on the
Railway volume, subtracts images held by committed posts and the `banned_images` rows, and
picks at random. A Telegram `photo` is downloaded at Telegram's compressed size (1280 px,
re-encoded) and dropped in the folder. `GET /media/<ref>` serves any file in the folder,
fitted to Instagram's spec at serve time. There is no record of where a file came from or how
often it was used.

| | Today | After M11 |
|---|---|---|
| Quick add | one compressed `photo` per message | `document` originals, albums of up to 10, HEIC converted, duplicates skipped, one "Saved 7 photos" reply per album |
| Bulk add | shell upload to the volume | shared Google Drive folder, pulled nightly and on `/sync` |
| Status back to the owner | none | Drive files moved to `posted/` or `banned/` as it happens |
| Browse | counts and a banned list, text only | pages of 10 thumbnails with used-count and last-used date, 🚫 Ban and 🗑 Delete per image |
| Record | folder scan plus `banned_images` | `stock_images` row per file: source, Drive file id, hash, imported date; the folder stays the store |

The pipeline's pick logic, `image_ref` and `/media` do not change. Delete moves the file to
`stock/.trash/`, never removes it.

## Storage decision

Keep the Railway volume. One service, one disk, already mounted at `/data`, and nobody but the
app ever sees the path. Object storage buys nothing until a second Railway service needs the
same files or the volume fills, and `image_ref` is already an opaque key, so that move is
contained in `app/stock.py` when it comes. The Drive folder is the owner-facing store; the
volume is the working copy.

Two housekeeping rules come with that: deleted and replaced files go to `stock/.trash/` and
the nightly job empties entries older than 30 days, and the home card warns when the volume is
above 80 percent full.

## Features

**A. Telegram files and albums (quick add).** `process_message` accepts `document` messages
whose MIME type is an image, downloads the original (Telegram's bot download limit is 20 MB;
larger files get a reply saying so), converts HEIC and HEIF to JPEG with `pillow-heif`, skips
a file whose SHA-256 already exists for the brand, and writes a `stock_images` row with source
`telegram`. Albums arrive as separate updates sharing a `media_group_id`; each file is saved
on arrival and the reply is sent once per group after a two-second quiet period ("Saved 7
photos, 1 duplicate skipped"). The compressed `photo` path keeps working. `/help` and the
low-stock message say "send as file for full quality".

**B. `/stock` browser.** The text summary stays for the typed command and the console tap. A
new "Browse" button opens page 1: one media group of up to 10 thumbnails (each resized to
320 px, captioned `1` to `10` with used-count and last-used date) followed by one control
message with a row of `🚫 1` to `🚫 10`, a row of `🗑 1` to `🗑 10`, and `‹ Prev` / `Next ›` /
`‹ Menu`. Ban writes `banned_images`; Delete moves the file to `stock/.trash/` and marks the
row deleted. Both answer with a toast and redraw the control message. Media groups cannot
carry buttons, which is why the page is two messages; the console card itself stays text.

**C. Google Drive folder sync (bulk).** A service account (JSON key in
`GOOGLE_SERVICE_ACCOUNT_JSON`) whose address `/drive` prints. The owner shares a folder with
that address and sends `/drive <folder link>`; the bot verifies it can list the folder and
stores `drive_folder_id` on the brand. Sync: list image files in the folder (not the
sub-folders), download those with no `stock_images` row for their Drive id or hash, convert
HEIC, write rows with source `drive`. Runs nightly after generation and on `/sync` (typed or
the Stock sub-menu). Write-back: when a post publishes, its image's Drive file is moved to
`posted/`; when an image is banned, to `banned/`; the sub-folders are created on first use.
Moves are Drive `update` calls on the file's parents, so the owner can drag a file back.
Drive REST through `httpx` with a JWT from `google-auth`; no Google client library.

**D. Google Photos Picker (optional, not planned).** A `/import` flow where the bot opens a
Picker session and the owner taps photos in Google Photos. Written up here so it is not
forgotten; a ticket is created only if a brand lives in Google Photos and will not use Drive.

## Data model

One migration, 0011, Postgres-only like the others.

| Table | Change | Notes |
|---|---|---|
| `stock_images` (new) | `brand_id`, `image_ref` (composite key), `source` (`telegram`, `drive`, `manual`), `source_id` (Telegram file id or Drive file id, null), `sha256`, `imported_at`, `deleted_at` (null) | Index on (`brand_id`, `sha256`) for duplicate checks. A file on the volume with no row is `manual` and gets a row the first time the browser or sync sees it |
| `brands` | `drive_folder_id` (null), `drive_synced_at` (null) | Set by `/drive`, read by the nightly job |

`banned_images` stays as is: a ban is a decision, a stock row is provenance. Used-count and
last-used date are computed from `posts` when a page is drawn, not stored.

## Tickets

| Ticket | What | Done when | Size |
|---|---|---|---|
| CUT-77 M11 epic | Close-out checklist: migration ran on Railway, Drive sync verified on the owner's real folder, docs, plan archived | All three below Done, owner confirms from the phone | — |
| CUT-78 M11.1 Files, albums, provenance | Migration 0011 and `StockImage`; `document` uploads with the 20 MB limit; HEIC to JPEG via `pillow-heif` (new dependency); SHA-256 duplicate skip; media-group reply after a quiet period; `stock.register(brand, path, source, source_id)` as the one way a file enters the library; `/help` text | Gate green; a document, a photo, an album of 3 and a duplicate each do the right thing in offline tests; HEIC fixture converts | 1 d |
| CUT-79 M11.2 Stock browser | `/stock` sub-menu with Browse; pages of 10 thumbnails as a media group plus a control message; `🚫` and `🗑` per image; `‹ Prev` / `Next ›` / `‹ Menu`; delete to `stock/.trash/`; used-count and last-used from `posts`; nightly trash expiry; volume-usage warning on the home card | Gate green; paging, ban, delete and the empty-library case covered offline; a 25-image fixture pages into 3 | 1 d |
| CUT-80 M11.3 Google Drive sync and write-back | `app/drive.py` (service-account JWT, list folder, download, move file, create sub-folder) over `httpx` + `google-auth` (new dependency); `/drive` prints the address or registers a folder link; `/sync` typed and as a Stock sub-menu button; nightly job after generation per brand; write-back on publish and on ban; `GOOGLE_SERVICE_ACCOUNT_JSON` in `env.example`; Drive API mocked in tests | Gate green; a fake Drive with 3 files syncs 3, a re-run syncs 0, publish moves the file to `posted/`, ban to `banned/`; `/drive` with a folder the account cannot see fails with a clear message | 2 d |

## Order and sessions

Two sessions on one branch `m11-media`, one checkout, no worktrees; split by model, run one
after the other, orchestrated as subagents like M10.

1. **Session 1, Claude Sonnet 5.5: M11.1 then M11.2.** Both live in `app/stock.py` and
   `app/notifier/telegram.py`, with offline tests against the fakes that already exist.
   Commits on the branch and stops.
2. **Session 2, Claude Opus 5.5: M11.3 then the close-out.** A new external integration with
   auth, idempotent sync and write-back into the owner's own files; the part where a wrong
   assumption costs real data. Pushes, opens the PR, never merges.
3. **Planner: review, merge, deploy.** After the deploy: the owner shares a test folder,
   `/drive <link>`, `/sync`, one publish and one ban, and checks the sub-folders on the phone.

M10 is merged (`bb6d0d1`); M11 builds on `brand_id` and `stock_prefix`.

## Risks and decisions (confirmed 2026-10-21)

- **Write-back moves files in the owner's Drive.** A move, not a delete, and only into
  sub-folders of the shared folder; the owner can drag a file back. Decision: **on by
  default**, it is the feature.
- **Service account versus OAuth.** One shared service account means every brand shares its
  folder with the same address: no consent screen, no token refresh, nothing stored per brand
  but a folder id. Decision: **service account.** OAuth would let the bot read a brand's whole
  Drive, which nobody asked for.
- **Phone photos are HEIC.** iPhones and recent Androids default to HEIC; Drive keeps them as
  is. `pillow-heif` converts on import; it ships wheels for Linux x86-64, so the Railway build
  is unaffected. Decision: **add the dependency.**
- **Thumbnail pages cost sends.** Ten photos per page is one `sendMediaGroup` call; browsing
  100 images is ten calls. Fine for one owner, but the browser is for occasional pruning, not
  daily use.
- **Volume capacity.** Originals are 3 to 8 MB each; 500 photos is a few GB. The home card
  warns above 80 percent; moving to object storage stays a later, contained change.
- **Telegram 20 MB bot limit.** Larger documents cannot be fetched by a bot at all; the reply
  says to use Drive for those.
- **Drive API quotas** are generous (per-user per-minute limits in the thousands); nightly
  sync of a few hundred files is nowhere near them.

## Done when

- [ ] Gate green: `ruff check . && mypy app && pytest`
- [ ] Migration 0011 dry run on a copy of the prod database; Railway deploy SUCCESS with
      `0010 -> 0011` in the log
- [ ] From the phone: send one photo as a file and one album of three; the bot replies once
      with the count and the files appear under Stock
- [ ] A HEIC file sent from the phone arrives as a JPEG in the library
- [ ] Stock › Browse pages ten thumbnails; Ban and Delete each update the page; deleted file
      is in `stock/.trash/`
- [ ] `/drive` prints the service-account address; after sharing a test folder,
      `/drive <link>` registers it and `/sync` imports its images
- [ ] One publish moves that image to `posted/` in Drive; one ban moves one to `banned/`
- [ ] Nightly job logged once on Railway with the sync count
- [ ] Docs: PRODUCT_SPEC §3 stock, ROADMAP M11 row, SCRIPTS_REFERENCE (`/drive`, `/sync`,
      browser callbacks), INSTAGRAM_RUNBOOK untouched, `env.example`, CLAUDE.md; plan and
      kickoff archived
- [ ] Linear tickets Done with ship comments, epic closed
