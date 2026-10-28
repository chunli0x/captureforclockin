"""Public Solana RPC, unsigned user transactions and exact-message confirmation.

This API client never loads private keys. Signing lives in signing_chain.py.
"""

import base64
import hashlib
import struct
import time
from urllib.parse import urlparse

import httpx
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.message import Message
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import Transaction, TransactionError
from spl.token.constants import ASSOCIATED_TOKEN_PROGRAM_ID, TOKEN_PROGRAM_ID
from spl.token.instructions import get_associated_token_address

SYSTEM = Pubkey.default()
RENT = Pubkey.from_string("SysvarRent111111111111111111111111111111111")
DEVNET_GENESIS = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG"
MEMO_PROGRAM = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")


class ChainUnavailable(ValueError):
    pass


def discriminator(name, kind="global"):
    return hashlib.sha256(f"{kind}:{name}".encode()).digest()[:8]


def meta(key, signer=False, writable=False):
    return AccountMeta(key, signer, writable)


# The wallet may inject compute-budget instructions (setComputeUnitLimit /
# setComputeUnitPrice) before signing a client-signed transaction. Those are
# benign and not part of the operation, so they are excluded from the
# fingerprint together with their program account.
COMPUTE_BUDGET = Pubkey.from_string("ComputeBudget111111111111111111111111111111")


def message_fingerprint(msg):
    """Identity of the operation a message encodes, excluding everything the
    signing wallet may legally vary before it signs a client-signed transaction:

    * the recent_blockhash (refreshed right before signing), and
    * compute-budget instructions (setComputeUnitLimit / setComputeUnitPrice).

    Only the meaningful instructions — their program id, resolved account
    pubkeys, and data — are hashed, so a blockhash refresh or an injected
    compute-budget instruction does not change the fingerprint."""
    h = hashlib.sha256()
    for i in msg.instructions:
        program = msg.account_keys[i.program_id_index]
        if program == COMPUTE_BUDGET:
            continue
        h.update(bytes(program))
        for idx in i.accounts:
            h.update(bytes(msg.account_keys[idx]))
        h.update(bytes(i.data))
    return h.hexdigest()


def ata_instruction(payer, owner, mint):
    ata = get_associated_token_address(owner, mint)
    return Instruction(
        ASSOCIATED_TOKEN_PROGRAM_ID,
        bytes([1]),
        [
            meta(payer, True, True),
            meta(ata, writable=True),
            meta(owner),
            meta(mint),
            meta(SYSTEM),
            meta(TOKEN_PROGRAM_ID),
        ],
    )


class Chain:
    def __init__(self, settings):
        self.settings = settings
        self.program = Pubkey.from_string(settings.program_id)
        self.skr = Pubkey.from_string(settings.skr_mint)
        # Public configuration only. Never inspect key-file settings here.
        verifier = getattr(settings, "verifier_pubkey", None)
        if not verifier:
            raise ChainUnavailable("SEEKER_VERIFIER_PUBKEY is required for read-only chain access")
        self.verifier_pubkey = Pubkey.from_string(verifier)
        treasury = getattr(settings, "treasury_pubkey", None)
        self.treasury_pubkey = Pubkey.from_string(treasury) if treasury else None
        self.config = self.pda(b"config")
        self.penalty_vault = self.pda(b"penalty-vault", bytes(self.skr))

    def pda(self, *seeds):
        return Pubkey.find_program_address(list(seeds), self.program)[0]

    def rpc(self, method, params):
        try:
            response = httpx.post(
                self.settings.rpc_url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()
            if "error" in data or "result" not in data:
                raise ChainUnavailable("RPC rejected request")
            return data["result"]
        except (httpx.HTTPError, ValueError) as exc:
            raise ChainUnavailable("RPC unavailable; no settlement recorded") from exc

    def check_environment(self):
        genesis = self.rpc("getGenesisHash", [])
        if self.settings.cluster == "devnet":
            if genesis != DEVNET_GENESIS:
                raise ChainUnavailable("RPC is not Solana devnet")
        elif urlparse(self.settings.rpc_url).hostname not in {
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            raise ChainUnavailable("Localnet RPC must be loopback")
        account = self.rpc(
            "getAccountInfo",
            [str(self.config), {"encoding": "base64", "commitment": "finalized"}],
        )["value"]
        if not account or account["owner"] != str(self.program):
            raise ChainUnavailable("Escrow program has not been initialized")
        raw = base64.b64decode(account["data"][0], validate=True)
        # Config layout (single $SKR mint): discriminator(8) + admin(32) +
        # verifier(32) + skr_mint(32) + min_bond(8) + slash_authority(32)
        # == 144 bytes. slash_authority is the verifier key (the backend reuses
        # it; no separate authority keypair).
        expected = (
            bytes(self.verifier_pubkey)
            + bytes(self.skr)
            + struct.pack("<Q", self.settings.min_bond)
            + bytes(self.verifier_pubkey)
        )
        if (
            len(raw) != 144
            or raw[:8] != discriminator("Config", "account")
            or raw[40:] != expected
        ):
            raise ChainUnavailable(
                "Configured verifier, mints or bond differ from program"
            )

        if getattr(self.settings, "payout_mode", "mint") == "transfer":
            self.check_payout_environment()

    def build(self, action, wallet, amount=None):
        """Build the instructions for a bond or slash action. ``amount`` is
        unused for warn/slash (kept for the ``issue`` call signature)."""
        who = Pubkey.from_string(wallet)
        worker = who
        bond = self.pda(b"bond", bytes(worker))
        bond_vault = self.pda(b"bond-vault", bytes(worker), bytes(self.skr))
        prefix = []
        if action in {"deposit", "withdraw"}:
            prefix = [ata_instruction(who, who, self.skr)]
            accounts = [
                meta(who, True, action == "deposit"),
                meta(self.config),
                meta(self.skr),
                meta(bond, writable=action == "deposit"),
                meta(bond_vault, writable=True),
                meta(get_associated_token_address(who, self.skr), writable=True),
                meta(TOKEN_PROGRAM_ID),
            ]
            if action == "deposit":
                accounts += [meta(SYSTEM), meta(RENT)]
            data = discriminator(
                "deposit_bond" if action == "deposit" else "withdraw_bond"
            )
            if action == "deposit":
                data += struct.pack("<Q", self.settings.min_bond)
        elif action == "warn":
            # slash_authority (verifier) signs. Warn context (Anchor):
            # slash_authority, config, bond (seeds=['bond', worker]). The worker
            # pubkey travels in the instruction data (no separate account).
            accounts = [
                meta(self.verifier_pubkey, True),
                meta(self.config),
                meta(bond, writable=True),
            ]
            data = discriminator("warn") + bytes(worker)
        elif action == "slash":
            # slash_authority (verifier) signs. Slash context (Anchor):
            # slash_authority, config, bond (writable, seeds=['bond', worker]),
            # bond_vault (writable, seeds=['bond-vault', worker]),
            # penalty_vault (writable, seeds=['penalty-vault', config]),
            # token_program. Full slash: data == discriminator + worker (no amount).
            accounts = [
                meta(self.verifier_pubkey, True),
                meta(self.config),
                meta(bond, writable=True),
                meta(bond_vault, writable=True),
                meta(self.penalty_vault, writable=True),
                meta(TOKEN_PROGRAM_ID),
            ]
            data = discriminator("slash") + bytes(worker)
        else:
            raise ChainUnavailable("Unknown transaction action")
        return prefix + [Instruction(self.program, data, accounts)]

    def prepare(self, action, wallet):
        if action not in {"deposit", "withdraw"}:
            raise ChainUnavailable("Only user-signed bond actions may be prepared")
        self.check_environment()
        result = self.rpc("getLatestBlockhash", [{"commitment": "finalized"}])["value"]
        blockhash = Hash.from_string(result["blockhash"])
        msg = Message.new_with_blockhash(
            self.build(action, wallet),
            Pubkey.from_string(wallet),
            blockhash,
        )
        tx = Transaction.new_unsigned(msg)
        return {
            "transaction": base64.b64encode(bytes(tx)).decode(),
            "message_hash": message_fingerprint(msg),
            "last_valid_height": result["lastValidBlockHeight"],
        }

    def bond_warnings(self, wallet):
        """Read the worker's on-chain bond ``warnings`` counter (0 = clean,
        1 = warned once, >= 2 = slashed). Raises ChainUnavailable if the bond
        cannot be read — fail closed, never fabricate bond state."""
        bond = self.pda(b"bond", bytes(Pubkey.from_string(wallet)))
        account = self.rpc(
            "getAccountInfo",
            [str(bond), {"encoding": "base64", "commitment": "finalized"}],
        )["value"]
        if not account or account["owner"] != str(self.program):
            raise ChainUnavailable("Worker bond account is not initialized")
        raw = base64.b64decode(account["data"][0], validate=True)
        # Bond layout: discriminator(8) + worker(32) + pending(4) + unlock_at(8)
        # + warnings(1). warnings is the trailing byte. Validate the discriminator
        # and exact length so a version-skewed binary cannot silently misread
        # raw[-1] (finding G, blockchain-dev review).
        if len(raw) != 8 + 32 + 4 + 8 + 1 or raw[:8] != discriminator("Bond", "account"):
            raise ChainUnavailable("Worker bond account has unexpected layout")
        return raw[-1]

    def bond_vault_balance(self, wallet):
        """Read the worker's on-chain bond-vault $SKR balance (base units).
        Raises ChainUnavailable if the vault cannot be read — fail closed."""
        vault = self.pda(
            b"bond-vault",
            bytes(Pubkey.from_string(wallet)),
            bytes(self.skr),
        )
        result = self.rpc(
            "getTokenAccountBalance",
            [str(vault), {"commitment": "finalized"}],
        )
        value = result.get("value")
        if not value or value.get("amount") is None:
            raise ChainUnavailable("Worker bond vault is not initialized")
        return int(value["amount"])

    def _account_data(self, address):
        account = self.rpc("getAccountInfo", [str(address), {
            "encoding": "base64", "commitment": "finalized",
        }])["value"]
        if account is None:
            return None
        if account["owner"] != str(self.program):
            raise ChainUnavailable("Receipt has the wrong owner")
        try:
            return base64.b64decode(account["data"][0], validate=True)
        except (ValueError, KeyError, TypeError) as exc:
            raise ChainUnavailable("Invalid receipt data") from exc

    def transfer_payment(self, wallet, amount, memo, obligation_version, valid_until_slot, signer=None):
        """Public instruction/receipt builder; no key access and no network writes."""
        worker = Pubkey.from_string(wallet)
        authority = signer or Pubkey.from_string(self.settings.payout_signer)
        vault = Pubkey.from_string(self.settings.payout_vault)
        policy = self.pda(b"payout", bytes(self.config))
        if vault != self.pda(b"payout-vault", bytes(policy)):
            raise ChainUnavailable("Configured payout vault is not the program vault")
        if not (0 < amount < 2**64 and 0 < obligation_version < 2**64):
            raise ChainUnavailable("Invalid transfer obligation")
        payment_id = hashlib.sha256(memo.encode()).digest()[:16]
        receipt = self.pda(b"payment-v3", payment_id)
        expected = (discriminator("PaymentV3", "account") + bytes(worker) + bytes(self.skr)
                    + struct.pack("<Q", amount) + payment_id + bytes(vault)
                    + struct.pack("<Q", obligation_version))
        instructions = [ata_instruction(authority, worker, self.skr), Instruction(
            self.program, discriminator("record_payment_v3") + payment_id + bytes(worker)
            + struct.pack("<QQQ", amount, obligation_version, valid_until_slot),
            [meta(authority, True, True), meta(self.config), meta(policy, writable=True),
             meta(self.skr), meta(vault, writable=True), meta(receipt, writable=True),
             meta(get_associated_token_address(worker, self.skr), writable=True),
             meta(SYSTEM), meta(TOKEN_PROGRAM_ID)],
        )]
        return receipt, expected, instructions

    def check_payout_environment(self):
        """Read-only startup check for the independently configured payout authority."""
        policy = self.pda(b"payout", bytes(self.config))
        vault = Pubkey.from_string(self.settings.payout_vault)
        if vault != self.pda(b"payout-vault", bytes(policy)):
            raise ChainUnavailable("Configured payout vault is not the program vault")
        raw = self._account_data(policy)
        expected = (discriminator("PayoutVault", "account") + bytes(self.skr)
                    + bytes(vault) + bytes(Pubkey.from_string(self.settings.payout_signer)))
        if raw is None or len(raw) != 137 or raw[:104] != expected:
            raise ChainUnavailable("Payout policy does not match configured mint, vault or signer")

    def height(self):
        return self.rpc("getBlockHeight", [{"commitment": "finalized"}])

    def confirm(self, prepared, signature):
        try:
            Signature.from_string(signature)
        except ValueError as exc:
            raise ChainUnavailable("Invalid transaction signature") from exc
        result = self.rpc(
            "getTransaction",
            [
                signature,
                {
                    "encoding": "base64",
                    "commitment": "finalized",
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        )
        if not result:
            raise ChainUnavailable(
                "Transaction is not finalized yet; retry confirmation"
            )
        if result["meta"]["err"] is not None:
            raise ChainUnavailable("Transaction failed; no settlement recorded")
        try:
            tx = Transaction.from_bytes(
                base64.b64decode(result["transaction"][0], validate=True)
            )
            tx.verify()
        except (ValueError, TypeError, TransactionError) as exc:
            raise ChainUnavailable("Invalid transaction confirmation") from exc
        if hasattr(prepared, "transfer_obligation"):
            # A receipt can outlive an attempt and a signer rotation. Match the
            # complete transfer operation while allowing its attempt deadline
            # and historically authorized payer to differ.
            if not tx.message.instructions:
                raise ChainUnavailable("Transaction is not a v3 transfer")
            ix = tx.message.instructions[-1]
            if len(ix.data) != 80 or ix.data[:8] != discriminator("record_payment_v3"):
                raise ChainUnavailable("Transaction is not a v3 transfer")
            deadline = struct.unpack("<Q", ix.data[-8:])[0]
            payer = tx.message.account_keys[0]
            _, _, instructions = self.transfer_payment(*prepared.transfer_obligation, deadline, payer)
            expected_hash = message_fingerprint(Message.new_with_blockhash(instructions, payer, Hash.default()))
        else:
            expected_hash = prepared.message_hash
        if (
            str(tx.signatures[0]) != signature
            or message_fingerprint(tx.message) != expected_hash
        ):
            raise ChainUnavailable("Transaction does not match this prepared operation")
        return True

    def find_signature(self, prepared, address=None):
        if address is None:
            address = self.pda(b"bond", bytes(Pubkey.from_string(prepared.wallet)))
        page_size = 100
        max_pages = 20  # sane cap: walk at most 2000 signatures
        # Overall wall-clock bound on the whole walk, on top of the per-call
        # RPC timeout, so a slow RPC endpoint cannot stall reconciliation
        # indefinitely (20 pages x 15s would otherwise be 300s).
        deadline = time.monotonic() + 30
        before = None
        for _ in range(max_pages):
            if time.monotonic() > deadline:
                raise ChainUnavailable(
                    "Confirmation scan deadline exceeded; supply its signature"
                )
            params = {"commitment": "finalized", "limit": page_size}
            if before is not None:
                params["before"] = before
            results = self.rpc("getSignaturesForAddress", [str(address), params])
            for item in results:
                if item["err"] is None:
                    try:
                        self.confirm(prepared, item["signature"])
                        return item["signature"]
                    except ChainUnavailable:
                        continue
            if len(results) < page_size:
                break  # end of history
            before = results[-1]["signature"]
        raise ChainUnavailable(
            "No matching finalized transaction found in recent history; supply its signature"
        )
