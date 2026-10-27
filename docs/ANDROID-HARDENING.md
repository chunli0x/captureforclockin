# Android release hardening

## Signing and local configuration

This app has its own release identity. Never reuse the other Capture app's key.
`android/signing.properties` supplies `storeFile`, `storePassword`, `keyAlias`, and
`keyPassword`; relative paths resolve against `android/`. Copy the committed
`.example` on a new machine. The generated local key is
`android/keystore/release.jks` (RSA 3072, JKS), with owner-only permissions; both
it and the real properties file are ignored by Git.

Local signing identity: `app.capture`. Certificate SHA-256:
`a6b6d4c6cb9395e2b8af6f6efffd7ed45999e7c3b290d7ea42a4b11e33e97ff4`.

Securely back up the key and
passwords separately before distributing an APK. Losing them prevents updates
signed with this identity. Existing `SEEKER_KEYSTORE_*`/`SEEKER_KEY_*` environment
overrides remain available for CI.

Release uses R8, resource shrinking, `proguard-android-optimize.txt` and the app's
`proguard-rules.pro`. Debug remains unminified, debug-signed and telemetry-free.
Release still requires a reachable HTTPS `SEEKER_API_BASE` (or `-PseekerApiBase`)
and successful `/health` response. Normal distribution commands retain this gate.

## Crash reporting and residency decision

Sentry Android core **8.60.0** is pinned; replay, NDK, HTTP instrumentation,
logging integrations and the upload Gradle plugin are not included. Manifest
auto-init is disabled. `CaptureApplication` initializes only a release with a
nonempty DSN after installing the privacy configuration.

Copy `android/crash-reporting.properties.example` to the ignored
`android/crash-reporting.properties`. Use a separate Sentry project/DSN for each
app. An empty DSN disables reporting. A nonempty DSN requires
`residencyApproved=true`; the default is false. Do not put an auth token in this
file. A DSN is compiled into an enabled APK, so it is not a secret from installed
clients, even though the real configuration is never committed.

**Review status:** the repositories specify confidentiality and a processor/data
residency review, but do not name an approved region, Sentry organization or
processor agreement. No conflict was identified in the written technical
requirements, but organizational approval cannot be inferred. Reporting remains
disabled pending the privacy owner's confirmation of region, processor terms,
retention/deletion, access controls and disclosures. No data was sent to Sentry
as part of implementation or tests.

Sentry offers US and EU hosting. Region is selected when creating the
organization; EU event data is stored in Germany, but some account/integration
metadata remains in the US. EU hosting alone is therefore not a claim of zero
US processing. Use an approved self-hosted endpoint if the organization's policy
requires it. Confirm the actual project's organization region before setting
`residencyApproved=true`.

Sources reviewed 2026-10-27:

- [Sentry data residency](https://github.com/getsentry/sentry/discussions/49564)
- [Android data collected](https://docs.sentry.io/platforms/android/data-management/data-collected/)
- [SDK source at the pinned version](https://github.com/getsentry/sentry-java/tree/8.60.0)

## Privacy boundary

Automatic user information, headers, bodies, cookies, URL query parameters,
file paths and database/GraphQL content collection are disabled. Breadcrumbs,
HTTP tracing, transactions, profiles, logs, metrics, sessions, screenshots,
view hierarchies, replay, native tombstones, raw ANR dumps, scope persistence and
SDK diagnostic logging are disabled too. Java/Kotlin uncaught crashes and ANRs
can be reported when enabled; native crash dumps are deliberately unavailable.

`CrashReporting` builds a fresh event containing only event ID/time, level,
constant application release/environment/platform, exception type/module,
handled flag and JVM stack symbols/line numbers. Exception messages and values,
wallets, bearer tokens, signed URLs, signatures, HTTP bodies, camera frames,
confidential strings, user/request/extra/context/tag data, thread names, source
paths/text, local variables and unknown fields are not copied. Message-only
events are dropped. The remaining symbols are compiled code identifiers: keep
source identifiers neutral and do not add business data to stack symbols.

`CrashTransport` repeats the allowlist before the SDK's disk-cache/network
transport, drops every non-event envelope item, and rebuilds envelope headers
without scope baggage or transaction/user metadata. It also filters cached
retries. This uses pinned Sentry envelope APIs; rerun the serialized-envelope
privacy tests and review upstream collection/transport changes on SDK upgrades.
Tests run entirely against an in-memory transport with synthetic data.
Sentry's receiving server necessarily sees the connection IP even though event
user/IP fields are removed; account for that in the processor review.

## Consumer rule review

Reviewed the actual resolved AAR `proguard.txt` files and embedded JAR metadata,
not generic internet keep-rule snippets. AGP merges these automatically.

| Dependency | Shipped coverage and app action |
| --- | --- |
| MWA clientlib, clientlib-ktx, common 2.2.0 | Empty consumer files. Source uses explicit JSON and direct callbacks, with no reflection requiring app rules. No package-wide keep. |
| ML Kit common 18.11.0 | Keeps runtime annotations and `@UsedBy*` classes/members for native/reflection entry points. |
| ML Kit face-detection 16.1.7 | Keeps JNI names and fields of generated protobuf base `zzuw`; suppresses optional `dalvik.system.VMStack`. |
| LiteRT 2.1.5 | Keeps `@UsedByReflection` classes/members. App rule scopes native-name preservation to `org.tensorflow.lite`, also covered by AGP defaults. |
| CameraX 1.6.2 | Keeps `Camera2Config$DefaultProvider`, Quirk subclasses/names and `onReadoutStarted` framework callback. Core/lifecycle/view/video/pipe need no additional app keeps. |
| Coil 3.6.2 | Core protects GenericViewTarget subclasses from R8 merging. Network JAR declares `OkHttpNetworkFetcherServiceLoaderTarget` in `META-INF/services`; app keeps only that class/constructor for discovery. |
| App and MWA serialization | `org.json` uses explicit keys; no reflective Gson/kotlinx DTO serialization. No app model keep required. |
| Sentry core/Android core 8.60.0 | Android AAR preserves options, integration names, enums, Throwable identities and line/source metadata; protocol JSON serializers use direct calls. No whole-SDK keep. |

Retain `android/app/build/outputs/mapping/release/mapping.txt` together with the
exact APK/version and certificate digest in restricted release storage. No
mapping upload credentials or automatic external uploads are configured. The
reporter sends a stable app/version release identifier for manual mapping lookup;
use R8 Retrace against that release's mapping to decode JVM symbols. Do not
publish mappings or overwrite them with another build.

## Verification

From `android/`, with a valid per-app HTTPS backend configured:

```sh
export ANDROID_HOME=/home/hermes/android-sdk
export SEEKER_API_BASE=https://your-app-backend
./gradlew assembleRelease
./gradlew testDebugUnitTest
./gradlew lintRelease
./gradlew assembleDebug
```

Inspect signing with Android SDK `apksigner verify --print-certs` and retain
separate certificate fingerprints for the two apps. Physical Seeker QA remains
necessary for a minified build's wallet authorization/signing, camera inference,
Coil loading and recovery. Live crash/ANR ingestion and mapping verification
require the approved per-app Sentry DSNs; no live reporting was exercised here.

### Verification result — 2026-10-27

- `assembleRelease`: **passed**, with
  `SEEKER_API_BASE=https://capture.magnumopus.ai` and the existing live HTTPS
  `/health`/signing gate enabled. R8 and resource shrinking completed and produced
  the mapping and shrinking reports.
- `testDebugUnitTest`: **67 passed**, no failures/errors/skips, including the
  three crash privacy tests.
- `lintRelease`: **passed**, **0 errors, 30 warnings, 2 hints**.
- `assembleDebug`: **passed**; debug remains unminified and telemetry-free.
- `apksigner verify --print-certs`: **passed**; APK package `app.capture`,
  certificate matching the fingerprint above. Both Sentry startup providers are
  absent in the final merged release manifest.

The final APKs are under `android/app/build/outputs/apk/`; signing files and all
build outputs remain ignored. No APK was deployed and no commit was pushed.
Reporting remains disabled; residency approval and a real per-app DSN are still
pending. Physical-device and live-ingestion checks described above were not run.
