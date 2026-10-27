package app.capture.seeker

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The branch [SeekerViewModel.recover] takes: replay an exact confirm with a
 * persisted wallet signature, or fall back to a chain-history reconcile when no
 * signature was stored. The decision itself is pure, so it is checked without a
 * device or the wallet adapter.
 */
class SeekerRecoveryTest {

    @Test
    fun `recover supplies a persisted signature to confirm when one is stored`() {
        val action = RecoveryAction.forTransaction("tx-123", "5Kqzq1Base58Signature")

        assertTrue(action is RecoveryAction.Confirm)
        action as RecoveryAction.Confirm
        assertEquals("tx-123", action.id)
        assertEquals("5Kqzq1Base58Signature", action.signature)
    }

    @Test
    fun `recover falls back to reconcile when no signature is stored`() {
        val action = RecoveryAction.forTransaction("tx-123", null)

        assertTrue(action is RecoveryAction.Reconcile)
        action as RecoveryAction.Reconcile
        assertEquals("tx-123", action.id)
    }
}
