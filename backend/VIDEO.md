# Local robot vision video slice

Devnet/localnet only. No streaming, capture UI, or contributor upload API. Ingest
requires a signed-in wallet in the operator allowlist. A separate reviewer
allowlist controls human approval. Both default to empty. Existing clients get
images unless they explicitly request `modality=video` or `modality=all`.

## Operator setup

Use the normal backend environment plus:

- `SEEKER_MEDIA_ROOT`: a private directory outside any static/document root
  (default `data/media`, directory permissions 0700).
- `SEEKER_MEDIA_OPERATORS` and `SEEKER_MEDIA_REVIEWERS`: JSON arrays of wallet
  public keys. Keep review credentials with the person performing review.
- `SEEKER_MEDIA_SIGNING_SECRET`: persistent random secret of at least 32 characters.
- `SEEKER_SIGHTENGINE_USER` and `SEEKER_SIGHTENGINE_SECRET`: moderation credentials.
  Only normalized, metadata-scrubbed bytes go to the fixed Sightengine HTTPS
  endpoint. Missing credentials/results fail closed. No remote input URLs.

Runtime prerequisites: Linux, `ffmpeg`/`ffprobe` with libx264, `bubblewrap`, and
`prlimit`. User namespaces must be enabled. The decoder runs without networking,
with a private work directory and CPU/memory/file/time limits. Sandbox failure
fails ingest; there is no unsandboxed fallback.

Before a separately authorized local run, apply the schema explicitly with
`.venv/bin/python -m seeker.migrate` (0019). API startup performs no DDL.
Run `.venv/bin/python -m seeker.media_worker --once` to process one queued clip,
or omit `--once` for a supervised worker. Keep the normal settlement worker
running too: it reconciles chain receipts and prepared transactions independently
of client callbacks. These instructions do not deploy or restart any service.

## Ingest, review, commission

1. With an operator bearer session, `POST /media/ingest` with raw MP4 bytes and
   `Content-Type: video/mp4`. Do not send original names, captions, or source
   descriptions. The API returns opaque `asset_id` and `job_id`. Limits are 32 MiB
   by default, one video stream, 0.5–30 seconds, and at most 3840×2160 pixels.
2. The worker decodes and normalizes to 640×360, 24 fps, four seconds, H.264 MP4.
   Short clips are held on their last frame; long clips are trimmed. Audio,
   subtitle/data tracks, chapters, metadata boxes, handler/compressor names,
   timestamps, and H.264 SEI are removed. Unknown MP4 boxes are erased at every
   depth; offsets are preserved. Every decoded output frame contributes a dHash.
3. `GET /media/ingest/{asset_id}` reports status. Automated face/text inspection
   uses `face-analysis,text` and requires complete real/artificial face results at
   half-second intervals (response positions are milliseconds). Any detected face/text,
   missing result, provider failure, or perceptual duplicate blocks review.
4. A human reviewer runs `.venv/bin/python -m seeker.media_worker --review-list`
   on the private workstation. Watch the **entire local derivative**, including
   frame stepping. Confirm it is neutral robot vision content, with no sensitive
   subjects, identifying scene details, people, text, brands, source labels, or
   other revealing visual material. Sampling cannot establish confidentiality;
   the human must inspect the actual frames. No review clip is served over HTTP.
5. Only after that inspection, the reviewer posts to
   `/media/assets/{asset_id}/approve` with the exact displayed SHA-256 and:
   `{"sha256":"<hash>","watched_entire_clip":true,"neutral_content":true,"no_identifying_content":true}`.
   Approval publishes immutable, read-only derivative and neutral JSON manifest
   files. Failed/unreviewed assets cannot be approved or delivered. To reject a
   review or withdraw approval, `POST /media/assets/{asset_id}/revoke`.
6. The operator calls `POST /media/pairs` with
   `{"asset_a_id":"<opaque ID>","asset_b_id":"<opaque ID>"}`. Both must be approved.
   The task and public rubric are fixed; there are no free-text labels or source
   lineage in paths, manifests, or chain memos.

## Delivery and judging

Authenticated `GET /judge/next?modality=all` returns strict video manifests and
relative signed delivery URLs. Each URL binds wallet, pair, variant, version,
hash, revocation revision and expiry (120 seconds by default, maximum 300).
`GET`/`HEAD /media/deliver/{variant_id}?token=…` also requires that wallet's bearer
session. Delivery supports byte ranges. Approval and file hash are rechecked on
every request. Revocation blocks new requests immediately, including HEAD/Range;
bytes already downloaded cannot be recalled. The settlement worker cancels
collecting pairs whose approval is withdrawn and releases their reservations
without scoring; already committed payout obligations remain honored.

Responses are private/no-store. Do not mount the media directory, expose the
quarantine/review folders, or place these endpoints behind a cache that drops
query strings or Authorization. Object storage/CDN delivery is deliberately out
of scope. Android's bounded 64 MiB cache retains the full signed URL and session
identity in its key, validates authorization with HEAD even before cached reads,
and stops playback at expiry. Reload renews delivery URLs. Both players release
on navigation and pause on background, use identical geometry/controls/1× speed,
and require loaded clips and foreground playback opportunity before any answer.
The API additionally requires delivery of both outputs and a four-second window;
this is an opportunity gate, not proof of human attention.

`POST /judge/{pair_id}/media-skip` records a technical skip, excludes that pair
for the wallet and creates **no vote, reservation, abstention or payment**. A
loaded, viewed “can't tell” remains the existing paid abstention workflow.

## Verification

Run the mandatory suites in each repo:

```sh
cd backend
SEEKER_LOCAL_SQLITE=true .venv/bin/python -m pytest -q
cd ../android
ANDROID_HOME=/home/hermes/android-sdk ./gradlew testDebugUnitTest
```

Video tests generate neutral synthetic clips and run real sandboxed transcoding,
metadata stripping, decoded-frame hashing, API approval/delivery and accounting.
Provider responses are fixtures; tests never send private footage to a service
or substitute an automatic approval in application code. Live provider behavior
and device playback still require an operator smoke test with reviewed content.

Provider contracts: [Sightengine face analysis](https://www.sightengine.com/docs/face-analysis),
[Sightengine synchronous video](https://www.sightengine.com/docs/moderate-stored-video),
[Sightengine short-video text/frames](https://www.sightengine.com/docs/text-detection-model),
[Media3 cache setup](https://developer.android.com/media/media3/exoplayer/network-stacks).
