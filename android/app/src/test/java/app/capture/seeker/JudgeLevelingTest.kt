package app.capture.seeker

import org.junit.Assert.assertEquals
import org.junit.Test

/** The judge ladder (0..4) is pure and pinned to the judgment contract table. */
class JudgeLevelingTest {

    @Test
    fun thresholdsStepThroughTheLadder() {
        assertEquals("Scout", judgeLevelName(0))
        assertEquals(0, judgeLevelFrom(0.0))
        assertEquals(0, judgeLevelFrom(0.79))
        assertEquals(1, judgeLevelFrom(0.80))
        assertEquals(1, judgeLevelFrom(0.87))
        assertEquals(2, judgeLevelFrom(0.88))
        assertEquals(3, judgeLevelFrom(0.93))
        assertEquals(4, judgeLevelFrom(0.97))
        assertEquals(4, judgeLevelFrom(1.0))
    }

    @Test
    fun tierTableMatchesTheContract() {
        assertEquals(
            listOf("Scout", "Regular", "Veteran", "Expert", "Pathfinder"),
            JUDGE_TIERS.map { it.name },
        )
        assertEquals(listOf(1.00, 1.10, 1.25, 1.50, 2.00), JUDGE_TIERS.map { it.rateMultiplier })
        assertEquals(listOf(null, 0.80, 0.88, 0.93, 0.97), JUDGE_TIERS.map { it.rateThreshold })
    }

    @Test
    fun agreementPercentAcceptsFractionAndPercent() {
        assertEquals(92, agreementPercent(0.92))
        assertEquals(90, agreementPercent(90.0))
        assertEquals(0, agreementPercent(0.0))
        assertEquals(100, agreementPercent(1.0))
    }

    @Test
    fun judgeTierClampsOutOfRange() {
        assertEquals("Scout", judgeTier(-1).name)
        assertEquals("Pathfinder", judgeTier(9).name)
    }

    @Test
    fun judgeRateMultiplierFollowsTheTable() {
        assertEquals(1.0, judgeRateMultiplier(0), 0.0)
        assertEquals(1.25, judgeRateMultiplier(2), 0.0)
        assertEquals(2.0, judgeRateMultiplier(4), 0.0)
    }
}
