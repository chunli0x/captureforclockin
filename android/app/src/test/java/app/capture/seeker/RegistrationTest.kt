package app.capture.seeker

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The over-18 self-declaration threads from the sign-in checkbox into the
 * `/auth/verify` request verbatim: `false` is never coerced to `true`, and the
 * challenge/signature are carried through untouched.
 */
class RegistrationTest {

    @Test
    fun affirmativeDeclarationIsCarriedThrough() {
        val request = buildVerifyRequest("challenge-abc", "sig-123", over18 = true)

        assertTrue(request.over18)
        assertEquals("challenge-abc", request.challengeId)
        assertEquals("sig-123", request.signature)
    }

    @Test
    fun uncheckedDeclarationIsSentAsFalseRatherThanDefaultingTrue() {
        val request = buildVerifyRequest("challenge-abc", "sig-123", over18 = false)

        assertFalse(request.over18)
        assertEquals("challenge-abc", request.challengeId)
        assertEquals("sig-123", request.signature)
    }
}
