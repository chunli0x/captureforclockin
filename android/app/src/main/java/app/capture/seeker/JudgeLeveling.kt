package app.capture.seeker

import kotlin.math.roundToInt

/**
 * The judge accuracy ladder (parallel to [Leveling.kt], which stays capture-only).
 *
 * Keyed on *agreement rate only*, the fraction of a judge's resolved judgments
 * they aligned with the crowd. Payout is `judge_base_skr × multiplier` per aligned
 * judgment. Kept free of Android so every decision is JVM-testable.
 */

const val MAX_JUDGE_LEVEL = 4

/** One rung of the judge ladder. [rateThreshold] is null at level 0 (no gate). */
data class JudgeTier(
    val level: Int,
    val name: String,
    val rateThreshold: Double?,
    val rateMultiplier: Double,
)

/** Contract table, ascending level order. Do not reorder. */
val JUDGE_TIERS: List<JudgeTier> =
    listOf(
        JudgeTier(0, "Scout", null, 1.00),
        JudgeTier(1, "Regular", 0.80, 1.10),
        JudgeTier(2, "Veteran", 0.88, 1.25),
        JudgeTier(3, "Expert", 0.93, 1.50),
        JudgeTier(4, "Pathfinder", 0.97, 2.00),
    )

/** The tier for a level, clamped into the valid range. */
fun judgeTier(level: Int): JudgeTier = JUDGE_TIERS[level.coerceIn(0, MAX_JUDGE_LEVEL)]

/** The highest level whose agreement-rate gate is met (level 0 has no gate). */
fun judgeLevelFrom(agreementRate: Double): Int =
    JUDGE_TIERS.indexOfLast { it.rateThreshold == null || agreementRate >= it.rateThreshold }
        .coerceAtLeast(0)

fun judgeLevelName(level: Int): String = judgeTier(level).name

fun judgeRateMultiplier(level: Int): Double = judgeTier(level).rateMultiplier

/**
 * Renders an agreement rate as a whole percent, accepting both the fraction
 * convention (0.92 -> 92) and an already-percent value (92 -> 92).
 */
fun agreementPercent(rate: Double): Int =
    if (rate.isFinite() && rate in 0.0..1.0) {
        (rate * 100).roundToInt().coerceIn(0, 100)
    } else {
        rate.toInt().coerceIn(0, 100)
    }
