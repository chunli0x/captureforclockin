"""Worker-only private keys and signing. The API must not import this module."""
import base64
import hashlib
import json
import struct
import time
from types import SimpleNamespace

from solders.hash import Hash
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.system_program import transfer, TransferParams
from solders.transaction import Transaction, TransactionError
from spl.token.constants import TOKEN_PROGRAM_ID
from spl.token.instructions import get_associated_token_address, mint_to
from spl.token.models import MintToParams

from .chain import (Chain, ChainUnavailable, SYSTEM, RENT, MEMO_PROGRAM,
                    ata_instruction, discriminator, message_fingerprint, meta)


class SigningChain(Chain):
    def __init__(self, settings):
        self.payout_signer = self._load_key(getattr(settings, "payout_signer_key_file", None))
        self.verifier = self._load_key(getattr(settings, "verifier_key_file", None))
        self.treasury = self._load_key(getattr(settings, "treasury_key_file", None))
        if self.verifier is None:
            raise ChainUnavailable("Worker verifier key is not configured")
        public = {}
        for name, key in (("verifier_pubkey", self.verifier), ("treasury_pubkey", self.treasury)):
            configured = getattr(settings, name, None)
            if key is not None:
                if configured and configured != str(key.pubkey()):
                    raise ChainUnavailable("Worker signer does not match configured public key")
                public[name] = str(key.pubkey())
        # Derive public configuration in the worker without mutating shared settings.
        super().__init__(SimpleNamespace(**{**vars(settings), **public}))
        self.settings = settings
        if self.payout_signer is not None:
            configured = getattr(settings, "payout_signer", None)
            if configured != str(self.payout_signer.pubkey()):
                raise ChainUnavailable("Payout signer does not match configured public key")
            if self.payout_signer.pubkey() in {self.verifier.pubkey(), self.treasury_pubkey}:
                raise ChainUnavailable("Payout signer must be separate from other worker authorities")

    def check_signers(self):
        if getattr(self.settings, "payout_mode", "mint") == "transfer":
            if self.payout_signer is None:
                raise ChainUnavailable("Worker payout signer key is not configured")
            return {"verifier": "ready", "payout": "ready"}
        if self.treasury is None:
            raise ChainUnavailable("Worker treasury key is not configured")
        return {"verifier": "ready", "treasury": "ready"}

    @staticmethod
    def _load_key(path):
        if path is None:
            return None
        try:
            return Keypair.from_bytes(bytes(json.loads(path.read_text())))
        except (OSError, ValueError, TypeError):
            return None

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

    def faucet(self, wallet, *, persist, prepared=None):
        """Mint the minimum test SKR bond and drop a little SOL so the worker
        can pay the deposit fee + bond/vault rent. The treasury is the $SKR
        mint authority AND the SOL funder. Raises ChainUnavailable when the
        treasury keypair is not configured."""
        if self.treasury is None:
            raise ChainUnavailable("$SKR treasury (mint authority) is not configured")
        if prepared is not None:
            return self._resume_faucet(wallet, prepared)
        payer = self.treasury
        # Global faucet budget: keep a SOL floor in the treasury for payouts,
        # so a swarm of fresh wallets cannot drain the devnet funder.
        treasury_sol = self.rpc("getBalance", [str(payer.pubkey())])["value"]
        if treasury_sol - self.settings.faucet_sol_lamports < self.settings.faucet_treasury_sol_floor:
            raise ChainUnavailable("test $SKR faucet SOL budget exhausted")
        instructions = self._faucet_instructions(wallet)
        result = self.rpc("getLatestBlockhash", [{"commitment": "finalized"}])["value"]
        blockhash = Hash.from_string(result["blockhash"])
        tx = Transaction.new_unsigned(Message.new_with_blockhash(instructions, payer.pubkey(), blockhash))
        tx.sign([payer], blockhash)
        prepared = {"transaction": base64.b64encode(bytes(tx)).decode(),
                    "signature": str(tx.signatures[0]), "last_valid_height": result["lastValidBlockHeight"],
                    "wallet": wallet}
        persist(prepared)  # fenced commit BEFORE broadcast; quota stays reserved on failure
        self.rpc("sendTransaction", [prepared["transaction"], {
            "encoding": "base64", "preflightCommitment": "finalized",
        }])
        self._confirm_sent(prepared["signature"])
        return prepared["signature"]

    def _faucet_instructions(self, wallet):
        worker, payer = Pubkey.from_string(wallet), self.treasury
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
        return instructions

    def _resume_faucet(self, wallet, prepared):
        tx = Transaction.from_bytes(base64.b64decode(prepared["transaction"], validate=True))
        tx.verify()
        if (prepared["wallet"] != wallet or str(tx.signatures[0]) != prepared["signature"]
                or tx.message.account_keys[0] != self.treasury.pubkey()):
            raise ChainUnavailable("Journaled faucet does not match obligation")
        expected = Message.new_with_blockhash(
            self._faucet_instructions(wallet), self.treasury.pubkey(), Hash.default())
        if message_fingerprint(tx.message) != message_fingerprint(expected):
            raise ChainUnavailable("Journaled faucet does not match obligation")
        statuses = self.rpc("getSignatureStatuses", [
            [prepared["signature"]], {"searchTransactionHistory": True},
        ]).get("value") or []
        status = statuses[0] if statuses else None
        if status is not None:
            if status.get("err") is not None:
                raise ChainUnavailable("Faucet transaction failed; reserved quota retained")
            if status.get("confirmationStatus") == "finalized":
                return prepared["signature"]
        if self.height() > prepared["last_valid_height"]:
            raise ChainUnavailable("Faucet outcome unresolved; reserved quota retained")
        # Rebroadcast identical bytes only: a fresh blockhash could double mint.
        self.rpc("sendTransaction", [prepared["transaction"], {
            "encoding": "base64", "preflightCommitment": "finalized",
        }])
        self._confirm_sent(prepared["signature"])
        return prepared["signature"]

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
        if prepared is not None:
            prior = Transaction.from_bytes(base64.b64decode(prepared["transaction"], validate=True))
            if any(bytes(ix.data[:8]) == discriminator("record_payment_v3") for ix in prior.message.instructions):
                raise ChainUnavailable("Transfer attempt cannot be retried in mint mode")
        if self._account_data(self.pda(b"payment-v3", payment_id)) is not None:
            raise ChainUnavailable("Payment receipt does not match mint mode; reconcile original transfer attempt")
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

    def settle_transfer(self, wallet, amount, memo, *, persist, prepared=None, obligation_version=1):
        """Persist before broadcast; reconcile exact finalized receipts before rebuilding.

        Expiry or worker failover changes the attempt, never the receipt identity.
        A journal from a different mode/vault/obligation fails closed even if expired.
        """
        self.check_environment()
        if self.payout_signer is None:
            raise ChainUnavailable("Worker payout signer key is not configured")
        payer = self.payout_signer
        old_tx = None
        deadline = 0
        if prepared is not None:
            try:
                old_tx = Transaction.from_bytes(base64.b64decode(prepared["transaction"], validate=True))
                old_tx.verify()
                ix = old_tx.message.instructions[-1]
                if len(ix.data) != 80 or ix.data[:8] != discriminator("record_payment_v3"):
                    raise ValueError("wrong instruction")
                deadline = struct.unpack("<Q", ix.data[-8:])[0]
                _, _, old_instructions = self.transfer_payment(
                    wallet, amount, memo, obligation_version, deadline, old_tx.message.account_keys[0])
                expected_message = Message.new_with_blockhash(
                    old_instructions, old_tx.message.account_keys[0], old_tx.message.recent_blockhash)
                if (bytes(old_tx.message) != bytes(expected_message)
                        or str(old_tx.signatures[0]) != prepared["signature"]):
                    raise ValueError("wrong obligation")
            except (ValueError, TypeError, IndexError, KeyError, TransactionError) as exc:
                raise ChainUnavailable("Journaled transfer does not match obligation") from exc
        receipt, expected, instructions = self.transfer_payment(wallet, amount, memo, obligation_version, deadline)
        raw = self._account_data(receipt)
        if raw is not None:
            if raw != expected:
                raise ChainUnavailable("Payment receipt does not match obligation")
            proof = SimpleNamespace(transfer_obligation=(wallet, amount, memo, obligation_version))
            if old_tx is not None:
                try:
                    self.confirm(proof, prepared["signature"])
                    return prepared["signature"]
                except ChainUnavailable:
                    pass
            return self.find_signature(proof, address=receipt)
        # Prevent mode migration from paying a previously minted obligation again.
        payment_id = hashlib.sha256(memo.encode()).digest()[:16]
        for old_receipt in (self.pda(b"payment", payment_id), self.pda(
                b"payment-v2", bytes(self.skr), bytes(Pubkey.from_string(wallet)),
                struct.pack("<Q", amount), payment_id)):
            if self._account_data(old_receipt) is not None:
                raise ChainUnavailable("Mint receipt exists; reconcile original mint attempt")
        slot = self.rpc("getSlot", [{"commitment": "finalized"}])
        expired = prepared is not None and (self.height() > prepared["last_valid_height"] or slot > deadline)
        if prepared is None or expired:
            deadline = slot + self.settings.payout_attempt_slots
            _, _, instructions = self.transfer_payment(wallet, amount, memo, obligation_version, deadline)
            result = self.rpc("getLatestBlockhash", [{"commitment": "finalized"}])["value"]
            blockhash = Hash.from_string(result["blockhash"])
            tx = Transaction.new_unsigned(Message.new_with_blockhash(instructions, payer.pubkey(), blockhash))
            tx.sign([payer], blockhash)
            prepared = {"transaction": base64.b64encode(bytes(tx)).decode(),
                        "signature": str(tx.signatures[0]), "last_valid_height": result["lastValidBlockHeight"]}
            persist(prepared)
        elif old_tx.message.account_keys[0] != payer.pubkey():
            raise ChainUnavailable("Previous payout signer attempt must expire before rotation retry")
        self.rpc("sendTransaction", [prepared["transaction"], {
            "encoding": "base64", "preflightCommitment": "finalized",
        }])
        self._confirm_sent(prepared["signature"])
        if self._account_data(receipt) != expected:
            raise ChainUnavailable("Finalized transfer receipt does not match obligation")
        return prepared["signature"]

    def post_batch(self, batch_id: str, merkle_root: str) -> str:
        """Anchor a resolved batch's Merkle root on-chain (append-only PDA).

        ``batch_id`` is 32 hex chars (16 bytes); ``merkle_root`` is 64 hex
        chars (32 bytes). Server-signed by the verifier (slash_authority), which
        is the anchor authority. The ``init`` batch account makes it append-only:
        retries verify the exact finalized account before attempting a write.
        """
        batch_bytes = bytes.fromhex(batch_id)
        root_bytes = bytes.fromhex(merkle_root)
        batch_pda = self.pda(b"batch", batch_bytes)
        raw = self._account_data(batch_pda)
        if raw is not None:
            if raw != discriminator("Batch", "account") + root_bytes:
                raise ChainUnavailable("Batch receipt does not match immutable root")
            return str(batch_pda)  # Exact finalized receipt proves the anchor.
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

