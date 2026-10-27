package app.capture.seeker

/**
 * The wallet sign-in verify payload. [over18] is a self-declared age
 * attestation captured in the UI; it threads through to the registration
 * request unchanged and is never coerced.
 */
data class VerifyRequest(val challengeId: String, val signature: String, val over18: Boolean)

/**
 * Builds the `/auth/verify` request that registers the wallet. The over-18
 * declaration is carried verbatim: a `false` value stays `false` so the
 * backend records what the user actually declared, not an optimistic default.
 */
fun buildVerifyRequest(challengeId: String, signature: String, over18: Boolean): VerifyRequest =
    VerifyRequest(challengeId = challengeId, signature = signature, over18 = over18)
