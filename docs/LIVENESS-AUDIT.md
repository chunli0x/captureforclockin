Audit of the Capture liveness flow, 2026-10-26. Finding locations below refer to
the pre-fix source at commit `73dd351`; Android paths use the current package root,
`android/app/src/main/java/app/capture/`.

The supplied log evidence locates the reported incident before session creation:
neither liveness endpoint was called. The checked-in backend returns
`liveness_completed=false` and `kind=none` for an unverified actor when liveness
is required (`backend/seeker/api.py:265`, `:644`). In that state the old UI does
render the gate, but only after the guidance, explanation, and stats. It does not
automatically open liveness after sign-in. The code therefore establishes a
buried entry point, not an unconditional failure to render it. Without a device
trace/screenshot, the precise interaction behind the user's report cannot be
proved. Camera or digest failures cannot explain the absence of a session call.

| Severity | Location at `73dd351` | Verified defect and fix |
| --- | --- | --- |
| HIGH | `seeker/JudgmentScreen.kt:45`, `:55`, `:115`; `LivenessScreen.kt:156`, `:187` | The only human-check entry point follows all guidance/stats and exists only inside `EmptyCard` when `judgePair == null`. A populated pair suppresses the gate even when `livenessCompleted == false`; shorter viewports require scrolling past informational content. The liveness intro similarly places both actions below its hero and three information rows. Move the account gate immediately below the judge heading, independent of pair state, and move Begin/demo above the intro artwork. |
| HIGH | `seeker/SeekerApp.kt:98`, `:100`; `seeker/SeekerViewModel.kt:121` | Session creation runs once on entry. With a null challenge, the UI always says “Starting…” and offers neither the request error nor retry/cancel. A rejected/failed request (or a call ignored by the busy guard) leaves that screen stuck. Show errors, retry, and cancel/back even before a challenge exists. Clear the previous challenge/session before a new request. |
| HIGH | `seeker/SeekerApp.kt:108`; `seeker/SeekerViewModel.kt:323`, `:332`, `:350` | `onPassed` starts an asynchronous request and immediately closes liveness, before acceptance. Failures lose their liveness UI; the judgment screen does not render `s.error`. Even after acceptance, the local completion flag is updated only after all three `/me`, `/judge/next`, and `/judge/stats` requests succeed. Keep liveness visible on failure, offer a fresh-session retry, disable duplicate submissions, and set the local flag immediately after `/liveness/complete` succeeds. Navigation observes that accepted flag. |
| MED | `LivenessScreen.kt:400`, `:409`, `:428` | ML Kit receives `rotationDegrees`, but the spoof crop uses its rotated face bounds on an unrotated `toBitmap()` image. For nonzero sensor rotation these coordinates/orientations disagree; a failed/non-live crop resets motion progress. Rotate the bitmap into ML Kit's coordinate system before cropping. This is a separate motion-path defect, not an explanation for zero session requests. |
| MED | `LivenessScreen.kt:270`, `:276`, `:290`, `:303`, `:348` | Camera use cases bind to the activity lifecycle, but leaving the composable closes detectors/executor without unbinding preview/analysis. The pending provider listener also has no disposal guard, and binding exceptions escape. Unbind this screen's use cases on exit, ignore callbacks after disposal, and display camera-start errors while leaving demo available. |

No CRITICAL finding was established. The demo bypass is intentional and retained.
No token, bond, judgment submission, or backend production logic was changed.

Backend and wire checks:

- `LivenessScreen.kt:192` sends exactly 64 hex characters / 32 nonzero bytes
  (`deadbeef` repeated eight times). It already satisfies the schema and
  degenerate-digest check; changing backend validation would not fix this incident.
- `backend/seeker/schemas.py:42` accepts the completion fields sent by Android.
  `backend/seeker/api.py:295` authenticates the contributor, decodes hex, maps
  unknown/expired/used sessions to 404/410/409, and persists
  `worker.liveness_completed=True` inside a transaction at `:316`.
- `backend/seeker/liveness.py:194` checks ownership, single use and expiry;
  `:210` rejects empty/all-zero digests. Nonzero client digests, including demo,
  remain accepted by design. The four server challenge names match Android's
  `LivenessChallenge` values. The motion detector produces a SHA-256 hex digest.
- Camera permission is declared and requested by Begin check; demo does not
  request it. The preview already uses `COMPATIBLE` mode to permit Compose
  overlays, so no SurfaceView-mode change was needed.
- The rotation correction follows [CameraX's rotation contract](https://developer.android.com/media/camera/camerax/orientation-rotation)
  and [ML Kit's rotated input contract](https://developers.google.com/ml-kit/vision/face-detection/android).
  The resolved CameraX 1.6.2 bytecode was also inspected: `toBitmap()` routes YUV
  conversion through `ImageProcessingUtil`, rather than applying the image-info
  rotation used by ML Kit.

Validation:

- Backend: `SEEKER_LOCAL_SQLITE=true /home/hermes/tap-ci/backend/.venv/bin/python -m pytest -q backend/tests`
  — 61 passed, 8 skipped. Skips require the existing, absent
  `target/deploy/seeker_bounty.so` artifact.
- Added API regressions exercise the exact demo digest and a motion-style SHA-256
  digest from session issuance through persistent completion, `/me`, `/judge/next`
  and successful judgment submission. They also verify replay rejection,
  invalid-digest rejection/retry, expiry/replacement, and wallet isolation.
- Android: `ANDROID_HOME=/home/hermes/android-sdk ./gradlew testDebugUnitTest lintDebug --console=plain`
  (from `android/`). Unit tests: 64 passed, zero failures/errors/skips.
  Lint also completed successfully.
- No ADB device is attached. Real Seeker camera inference, permission UI and
  visual placement have not been exercised on hardware. Device smoke checks:
  sign in with an unverified wallet and see the gate immediately; complete demo
  with camera permission denied; separately run Begin check; cancel/reenter;
  expire a session and retry; disconnect networking and verify error/retry UI.

The fix is local only; no deployment or push is part of this audit.
