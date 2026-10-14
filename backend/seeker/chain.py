"""Real Solana RPC + exact-message confirmation. No simulated payment path."""

import base64
import hashlib
import json
import struct
import time
from urllib.parse import urlparse
from types import SimpleNamespace

import httpx
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.system_program import transfer, TransferParams
from solders.transaction import Transaction, TransactionError
from spl.token.constants import ASSOCIATED_TOKEN_PROGRAM_ID, TOKEN_PROGRAM_ID
from spl.token.instructions import get_associated_token_address, mint_to
from spl.token.models import MintToParams

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
        self.verifier = Keypair.from_bytes(
            bytes(json.loads(settings.verifier_key_file.read_text()))
        )
        self.config = self.pda(b"config")
        self.penalty_vault = self.pda(b"penalty-vault", bytes(self.skr))
        # Devnet treasury: holds the $SKR mint authority (the faucet, the bond,
        # and the settle_skr payout — one token, three jobs). Absent means the
        # on-chain mint is unavailable and the backend fails closed instead of
        # fabricating it.
        self.treasury = self._load_key(getattr(settings, "treasury_key_file", None))

    @staticmethod
    def _load_key(path):
        if path is None:
            return None
        try:
            return Keypair.from_bytes(bytes(json.loads(path.read_text())))
        except (OSError, ValueError, TypeError):
            return None

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
            bytes(self.verifier.pubkey())
            + bytes(self.skr)
            + struct.pack("<Q", self.settings.min_bond)
            + bytes(self.verifier.pubkey())
        )
        if (
            len(raw) != 144
            or raw[:8] != discriminator("Config", "account")
            or raw[40:] != expected
        ):
            raise ChainUnavailable(
                "Configured verifier, mints or bond differ from program"
            )

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
                meta(self.verifier.pubkey(), True),
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
                meta(self.verifier.pubkey(), True),
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

    def _sign_and_send(self, instructions, payer):
        """Serialize, fully sign with ``payer``, and broadcast. Returns the
        transaction signature string. Raises ChainUnavailable on any RPC
        failure — fail closed, never fabricate a settlement."""
        result = self.rpc("getLatestBlockhash", [{"commitment": "finalized"}])["value"]
        blockhash = Hash.from_string(result["blockhash"])
        msg = Message.new_with_blockhash(instructions, payer.pubkey(), blockhash)
        tx = Transaction.new_unsigned(msg)
        tx.sign([payer], blockhash)
        return self.rpc(
            "sendTransaction",
            [
                base64.b64encode(bytes(tx)).decode(),
                {"encoding": "base64", "preflightCommitment": "finalized"},
            ],
        )

    def _confirm_sent(self, signature, deadline_s=20.0):
        """Poll a just-broadcast signature until it finalizes or fails.

        ``sendTransaction`` returning a signature only means the RPC accepted
        the submit — not that the transaction landed or executed. Reconcile
        here before any caller records a completed payment, slash, or anchor.
        Raises ChainUnavailable if it does not confirm.
        """
        deadline = time.monotonic() + deadline_s
        while True:
            r = self.rpc(
                "getSignatureStatuses",
                [[signature], {"searchTransactionHistory": True}],
            )
            statuses = r.get("value") or []
            st = statuses[0] if statuses else None
            if st is not None:
                if st.get("err") is not None:
                    raise ChainUnavailable("Transaction failed to execute")
                if st.get("confirmationStatus") == "finalized":
                    return True
            if time.monotonic() > deadline:
                raise ChainUnavailable("Transaction did not confirm in time")
            time.sleep(0.5)

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

    def issue(self, action, wallet, amount=None):
        """Server-side warn/slash: the backend is the slash_authority (the
        verifier key), so it builds, signs and broadcasts the instruction
        itself — no client signature. Returns the signature. Confirms
        execution before returning so a slash is never recorded without
        landing on-chain."""
        instructions = self.build(action, wallet, amount=amount)
        sig = self._sign_and_send(instructions, self.verifier)
        self._confirm_sent(sig)
        return sig

    def faucet(self, wallet):
        """Mint the minimum test SKR bond and drop a little SOL so the worker
        can pay the deposit fee + bond/vault rent. The treasury is the $SKR
        mint authority AND the SOL funder. Raises ChainUnavailable when the
        treasury keypair is not configured."""
        if self.treasury is None:
            raise ChainUnavailable("$SKR treasury (mint authority) is not configured")
        worker = Pubkey.from_string(wallet)
        payer = self.treasury
        # Global faucet budget: keep a SOL floor in the treasury for payouts,
        # so a swarm of fresh wallets cannot drain the devnet funder.
        treasury_sol = self.rpc("getBalance", [str(payer.pubkey())])["value"]
        if treasury_sol - self.settings.faucet_sol_lamports < self.settings.faucet_treasury_sol_floor:
            raise ChainUnavailable("test $SKR faucet SOL budget exhausted")
        instructions = [ata_instruction(payer.pubkey(), worker, self.skr)]
        instructions.append(
            mint_to(
                MintToParams(
                    program_id=TOKEN_PROGRAM_ID,
                    mint=self.skr,
                    dest=get_associated_token_address(worker, self.skr),
                    mint_authority=payer.pubkey(),
                    amount=self.settings.min_bond,
                    signers=[payer.pubkey()],
                )
            )
        )
        # The deposit is client-signed with payer=worker, so the worker needs
        # lamports to pay the transaction fee and the bond/vault rent.
        instructions.append(
            transfer(
                TransferParams(
                    from_pubkey=payer.pubkey(),
                    to_pubkey=worker,
                    lamports=self.settings.faucet_sol_lamports,
                )
            )
        )
        sig = self._sign_and_send(instructions, payer)
        self._confirm_sent(sig)
        return sig

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

    def reserve_judgment(self, wallet, resolution_id):
        worker = Pubkey.from_string(wallet)
        rid = bytes.fromhex(resolution_id)
        liability = self.pda(b"liability", bytes(worker), rid)
        raw = self._account_data(liability)
        if raw is not None:
            if (len(raw) != 59 or raw[:56] != discriminator("Liability", "account") + bytes(worker) + rid
                    or raw[56] != 0):
                raise ChainUnavailable("Invalid or already resolved liability")
            return
        ix = Instruction(self.program, discriminator("reserve_judgment") + bytes(worker) + rid, [
            meta(self.verifier.pubkey(), True, True), meta(self.config),
            meta(self.pda(b"bond", bytes(worker)), writable=True),
            meta(self.pda(b"bond-vault", bytes(worker), bytes(self.skr))),
            meta(liability, writable=True), meta(SYSTEM),
        ])
        sig = self._sign_and_send([ix], self.verifier)
        self._confirm_sent(sig)

    def resolve_judgment(self, wallet, resolution_id, penalize):
        worker = Pubkey.from_string(wallet)
        rid = bytes.fromhex(resolution_id)
        liability = self.pda(b"liability", bytes(worker), rid)
        raw = self._account_data(liability)
        if raw is not None and (len(raw) != 59 or raw[:56] != discriminator("Liability", "account") + bytes(worker) + rid):
            raise ChainUnavailable("Missing or invalid judgment liability")
        if raw is not None and raw[56]:
            if raw[57] != int(penalize):
                raise ChainUnavailable("Resolution does not match journal")
            return self.bond_warnings(wallet)
        ix = Instruction(self.program, discriminator("resolve_judgment") + bytes(worker) + rid + bytes([penalize]), [
            meta(self.verifier.pubkey(), True, True), meta(self.config),
            meta(self.pda(b"bond", bytes(worker)), writable=True),
            meta(self.pda(b"bond-vault", bytes(worker), bytes(self.skr)), writable=True),
            meta(liability, writable=True), meta(self.penalty_vault, writable=True), meta(TOKEN_PROGRAM_ID), meta(SYSTEM),
        ])
        sig = self._sign_and_send([ix], self.verifier)
        self._confirm_sent(sig)
        return self.bond_warnings(wallet)

    def settle_skr(self, wallet, amount, memo, *, persist, prepared=None):
        """Verify a receipt on retry; journal signed bytes before any broadcast.

        The program atomically mints and creates an immutable, fully bound
        receipt. An expired transaction can safely be rebuilt: concurrent or
        late submissions cannot mint twice, because they share that receipt.
        """
        if self.treasury is None:
            raise ChainUnavailable("$SKR treasury (mint authority) is not configured")
        worker, payer = Pubkey.from_string(wallet), self.treasury
        payment_id = hashlib.sha256(memo.encode()).digest()[:16]
        receipt = self.pda(b"payment-v2", bytes(self.skr), bytes(worker), struct.pack("<Q", amount), payment_id)
        instructions = [ata_instruction(payer.pubkey(), worker, self.skr), Instruction(
            self.program,
            discriminator("record_payment") + payment_id + bytes(worker) + struct.pack("<Q", amount),
            [meta(payer.pubkey(), True, True), meta(self.config), meta(self.skr, writable=True),
             meta(receipt, writable=True), meta(get_associated_token_address(worker, self.skr), writable=True),
             meta(SYSTEM), meta(TOKEN_PROGRAM_ID)],
        )]
        fingerprint = message_fingerprint(Message.new_with_blockhash(instructions, payer.pubkey(), Hash.default()))
        raw = self._account_data(receipt)
        if raw is not None:
            expected = (discriminator("Payment", "account") + bytes(payer.pubkey()) + bytes(worker)
                        + bytes(self.skr) + struct.pack("<Q", amount) + payment_id)
            if raw != expected:
                raise ChainUnavailable("Payment receipt does not match obligation")
            return self.find_signature(SimpleNamespace(message_hash=fingerprint), address=receipt)

        # Old receipts did not bind the payment. Only the actual, exactly
        # matching finalized mint transaction can prove a legacy payout.
        legacy = self.pda(b"payment", payment_id)
        if self._account_data(legacy) is not None:
            old = [instructions[0], Instruction(self.program, discriminator("record_payment") + payment_id,
                [meta(payer.pubkey(), True, True), meta(legacy, writable=True), meta(SYSTEM)]),
                mint_to(MintToParams(program_id=TOKEN_PROGRAM_ID, mint=self.skr,
                    dest=get_associated_token_address(worker, self.skr), mint_authority=payer.pubkey(),
                    amount=amount, signers=[payer.pubkey()])), Instruction(MEMO_PROGRAM, memo.encode(), [])]
            old_hash = message_fingerprint(Message.new_with_blockhash(old, payer.pubkey(), Hash.default()))
            return self.find_signature(SimpleNamespace(message_hash=old_hash), address=legacy)

        if prepared is None or self.height() > prepared["last_valid_height"]:
            result = self.rpc("getLatestBlockhash", [{"commitment": "finalized"}])["value"]
            blockhash = Hash.from_string(result["blockhash"])
            tx = Transaction.new_unsigned(Message.new_with_blockhash(instructions, payer.pubkey(), blockhash))
            tx.sign([payer], blockhash)
            prepared = {"transaction": base64.b64encode(bytes(tx)).decode(),
                        "signature": str(tx.signatures[0]), "last_valid_height": result["lastValidBlockHeight"]}
            persist(prepared)  # must commit before sendTransaction
        tx = Transaction.from_bytes(base64.b64decode(prepared["transaction"], validate=True))
        tx.verify()
        if message_fingerprint(tx.message) != fingerprint or str(tx.signatures[0]) != prepared["signature"]:
            raise ChainUnavailable("Journaled payment does not match obligation")
        self.rpc("sendTransaction", [prepared["transaction"], {
            "encoding": "base64", "preflightCommitment": "finalized",
        }])
        self._confirm_sent(prepared["signature"])
        return prepared["signature"]

    def post_batch(self, batch_id: str, merkle_root: str) -> str:
        """Anchor a resolved batch's Merkle root on-chain (append-only PDA).

        ``batch_id`` is 32 hex chars (16 bytes); ``merkle_root`` is 64 hex
        chars (32 bytes). Server-signed by the verifier (slash_authority), which
        is the anchor authority. The ``init`` batch account makes it append-only:
        re-anchoring the same batch_id fails preflight (RPC error -> closed).
        """
        batch_bytes = bytes.fromhex(batch_id)
        root_bytes = bytes.fromhex(merkle_root)
        batch_pda = self.pda(b"batch", batch_bytes)
        instructions = [
            Instruction(
                self.program,
                discriminator("post_batch") + batch_bytes + root_bytes,
                [
                    meta(self.verifier.pubkey(), True, True),  # payer (init)
                    meta(self.config),
                    meta(batch_pda, writable=True),
                    meta(SYSTEM),
                    meta(RENT),
                ],
            )
        ]
        sig = self._sign_and_send(instructions, self.verifier)
        # Finding C (blockchain-dev review): confirm before returning, so a
        # broadcast-but-failed anchor raises ChainUnavailable and leaves
        # anchored=False for the reconcile pass (migration 0013 contract).
        self._confirm_sent(sig)
        return sig

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
        if (
            str(tx.signatures[0]) != signature
            or message_fingerprint(tx.message) != prepared.message_hash
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
