package app.capture

/**
 * Minimal Base58 encoder for displaying transaction signatures.
 *
 * Hand-rolled rather than pulled from web3-solana: the app needs exactly this
 * one function, and a 30-line encoder is cheaper than another dependency whose
 * version has to be kept in step with MWA's.
 */
object Base58 {
    private const val ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

    fun encode(input: ByteArray): String {
        if (input.isEmpty()) return ""

        // Leading zero bytes are not representable in the arithmetic below, so
        // they are counted here and re-added as '1' characters at the end.
        var leadingZeros = 0
        while (leadingZeros < input.size && input[leadingZeros] == 0.toByte()) {
            leadingZeros++
        }

        val digits = ByteArray(input.size * 2)
        var digitsLength = 1
        for (i in leadingZeros until input.size) {
            var carry = input[i].toInt() and 0xFF
            for (j in 0 until digitsLength) {
                carry += (digits[j].toInt() and 0xFF) shl 8
                digits[j] = (carry % 58).toByte()
                carry /= 58
            }
            while (carry > 0) {
                digits[digitsLength++] = (carry % 58).toByte()
                carry /= 58
            }
        }

        return buildString(leadingZeros + digitsLength) {
            repeat(leadingZeros) { append(ALPHABET[0]) }
            for (i in digitsLength - 1 downTo 0) {
                append(ALPHABET[digits[i].toInt()])
            }
        }
    }
}
