"""Devnet-configured fake RPC, ephemeral test keys and test mint; never uses a network."""
import base64
import hashlib
import json
import struct
from types import SimpleNamespace

import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import Transaction
from spl.token.constants import TOKEN_PROGRAM_ID

from seeker.chain import Chain, ChainUnavailable, DEVNET_GENESIS, discriminator
from seeker.config import Settings
from seeker.signing_chain import SigningChain


@pytest.fixture
def transfer(tmp_path):
    verifier, payout, admin = Keypair(), Keypair(), Keypair()
    program, mint = "5TiDwP86k95yfQWzExrPTj5qcKxED99z8pW9fN81u5SV", str(Keypair().pubkey())
    config = Pubkey.find_program_address([b"config"], Pubkey.from_string(program))[0]
    policy = Pubkey.find_program_address([b"payout", bytes(config)], Pubkey.from_string(program))[0]
    vault = Pubkey.find_program_address([b"payout-vault", bytes(policy)], Pubkey.from_string(program))[0]
    paths = []
    for name, key in (("verifier", verifier), ("payout", payout)):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(list(bytes(key))))
        paths.append(path)
    settings = Settings(program_id=program, skr_mint=mint, cluster="devnet", payout_mode="transfer",
                        verifier_pubkey=str(verifier.pubkey()), verifier_key_file=paths[0],
                        payout_signer=str(payout.pubkey()), payout_signer_key_file=paths[1], payout_vault=str(vault))
    chain = SigningChain(settings)
    config_raw = (discriminator("Config", "account") + bytes(admin.pubkey()) + bytes(verifier.pubkey())
                  + bytes(chain.skr) + struct.pack("<Q", settings.min_bond) + bytes(verifier.pubkey()))
    policy_raw = (discriminator("PayoutVault", "account") + bytes(chain.skr) + bytes(vault)
                  + bytes(payout.pubkey()) + b"\x00" + struct.pack("<QQQQ", 100, 1000, 0, 0))
    state = SimpleNamespace(accounts={str(config): config_raw, str(policy): policy_raw}, transactions={},
                            sent=[], journal=[], slot=10, height=10, land=True, fail=False, genesis=DEVNET_GENESIS)

    def rpc(method, params):
        if method == "getGenesisHash":
            return state.genesis
        if method == "getAccountInfo":
            raw = state.accounts.get(params[0])
            return {"value": None if raw is None else {"owner": program, "data": [base64.b64encode(raw).decode(), "base64"]}}
        if method == "getSlot":
            return state.slot
        if method == "getBlockHeight":
            return state.height
        if method == "getLatestBlockhash":
            return {"value": {"blockhash": str(Hash.new_unique()), "lastValidBlockHeight": state.height + 100}}
        if method == "sendTransaction":
            assert state.journal and params[0] == state.journal[-1]["transaction"], "broadcast before durable journal"
            state.sent.append(params[0])
            tx = Transaction.from_bytes(base64.b64decode(params[0]))
            tx.verify()
            sig = str(tx.signatures[0])
            if state.land and not state.fail:
                ix = tx.message.instructions[-1]
                recipient = bytes(ix.data[24:56])
                amount, version, _ = struct.unpack("<QQQ", ix.data[56:])
                raw = (discriminator("PaymentV3", "account") + recipient + bytes(chain.skr)
                       + struct.pack("<Q", amount) + bytes(ix.data[8:24]) + bytes(vault) + struct.pack("<Q", version))
                state.accounts[str(tx.message.account_keys[ix.accounts[5]])] = raw
                state.transactions[sig] = {"meta": {"err": None}, "transaction": [params[0], "base64"]}
            return sig
        if method == "getSignatureStatuses":
            return {"value": [{"err": "empty vault" if state.fail else None, "confirmationStatus": "finalized"}]}
        if method == "getTransaction":
            return state.transactions.get(params[0])
        if method == "getSignaturesForAddress":
            return [{"signature": sig, "err": None} for sig in state.transactions]
        raise AssertionError(method)
    chain.rpc = rpc
    return chain, state, str(Keypair().pubkey())


def pay(transfer, **kwargs):
    chain, state, wallet = transfer
    return chain.settle_transfer(wallet, 25, "logical-payment", persist=state.journal.append, **kwargs)


def test_transfer_wire_layout_and_finalized_receipt(transfer):
    chain, state, wallet = transfer
    assert chain.treasury is None  # Does not need mint authority or treasury key.
    assert chain.check_signers() == {"verifier": "ready", "payout": "ready"}
    signature = pay(transfer)
    tx = Transaction.from_bytes(base64.b64decode(state.sent[0]))
    ix = tx.message.instructions[-1]
    assert ix.data == (discriminator("record_payment_v3") + hashlib.sha256(b"logical-payment").digest()[:16]
                       + bytes(Pubkey.from_string(wallet)) + struct.pack("<QQQ", 25, 1, 310))
    keys = [tx.message.account_keys[i] for i in ix.accounts]
    assert keys[0] == chain.payout_signer.pubkey()
    assert keys[4] == Pubkey.from_string(chain.settings.payout_vault)
    assert keys[8] == TOKEN_PROGRAM_ID
    assert signature == str(tx.signatures[0])
    assert pay(transfer, prepared=state.journal[0]) == signature
    assert pay(transfer) == signature  # Exact receipt + matching historical transaction.
    assert len(state.sent) == len(state.journal) == 1


@pytest.mark.parametrize("offset", [0, 8, 40, 72, 80, 96, 128])
def test_every_receipt_field_is_checked(transfer, offset):
    chain, state, wallet = transfer
    receipt, raw, _ = chain.transfer_payment(wallet, 25, "logical-payment", 1, 0)
    bad = bytearray(raw)
    bad[offset] ^= 1
    state.accounts[str(receipt)] = bytes(bad)
    with pytest.raises(ChainUnavailable, match="receipt does not match"):
        pay(transfer)
    assert not state.sent and not state.journal


@pytest.mark.parametrize("expiry", ["blockhash", "deadline"])
def test_expiry_rebuilds_same_receipt_and_late_original_reconciles(transfer, expiry):
    chain, state, wallet = transfer
    state.land = False
    with pytest.raises(ChainUnavailable, match="receipt"):
        pay(transfer)
    original = state.journal[0]
    if expiry == "blockhash":
        state.height = 111
    else:
        state.slot = 311
    state.land = True
    assert pay(transfer, prepared=original)
    assert len(state.journal) == 2 and state.sent[0] != state.sent[1]
    txs = [Transaction.from_bytes(base64.b64decode(p["transaction"])) for p in state.journal]
    assert txs[0].message.account_keys[txs[0].message.instructions[-1].accounts[5]] == txs[1].message.account_keys[txs[1].message.instructions[-1].accounts[5]]
    # A lost journal update / stale worker still recognizes the same payment.
    assert pay(transfer, prepared=original) == state.journal[1]["signature"]
    assert len(state.sent) == 2


def test_original_lands_while_replacement_attempt_is_persisted(transfer):
    chain, state, wallet = transfer
    original_signature = pay(transfer)
    original = state.journal[0]
    receipt, raw, _ = chain.transfer_payment(wallet, 25, "logical-payment", 1, 0)
    del state.accounts[str(receipt)]  # Simulate an earlier finalized read missing the receipt.
    state.height = 111
    state.slot = 50
    state.land = False
    with pytest.raises(ChainUnavailable):
        pay(transfer, prepared=original)
    replacement = state.journal[-1]
    state.accounts[str(receipt)] = raw
    assert pay(transfer, prepared=replacement) == original_signature
    assert len(state.sent) == 2


@pytest.mark.parametrize("expired", [False, True])
def test_changed_obligation_cannot_reuse_journal_even_after_expiry(transfer, expired):
    chain, state, wallet = transfer
    pay(transfer)
    if expired:
        state.height = 1000
    with pytest.raises(ChainUnavailable, match="Journaled transfer"):
        chain.settle_transfer(wallet, 26, "logical-payment", persist=state.journal.append, prepared=state.journal[0])
    assert len(state.sent) == 1


def test_signer_rotation_can_reconcile_prior_authority(transfer):
    chain, state, wallet = transfer
    signature = pay(transfer)
    replacement = Keypair()
    chain.payout_signer = replacement
    chain.settings.payout_signer = str(replacement.pubkey())
    policy = str(chain.pda(b"payout", bytes(chain.config)))
    raw = state.accounts[policy]
    state.accounts[policy] = raw[:72] + bytes(replacement.pubkey()) + raw[104:]
    assert pay(transfer, prepared=state.journal[0]) == signature
    assert pay(transfer) == signature
    assert len(state.sent) == 1


def test_persist_failure_prevents_broadcast(transfer):
    chain, state, wallet = transfer
    def fail(_):
        raise RuntimeError("lost lease")
    with pytest.raises(RuntimeError, match="lost lease"):
        chain.settle_transfer(wallet, 25, "logical-payment", persist=fail)
    assert not state.sent


def test_failed_transfer_does_not_become_a_payment(transfer):
    chain, state, wallet = transfer
    state.fail = True
    with pytest.raises(ChainUnavailable, match="failed to execute"):
        pay(transfer)
    assert len(state.journal) == 1
    receipt, _, _ = chain.transfer_payment(wallet, 25, "logical-payment", 1, 0)
    assert str(receipt) not in state.accounts


def test_environment_and_vault_fail_closed(transfer):
    chain, state, wallet = transfer
    state.genesis = "mainnet is forbidden"
    with pytest.raises(ChainUnavailable, match="not Solana devnet"):
        pay(transfer)
    state.genesis = DEVNET_GENESIS
    chain.settings.payout_vault = str(Keypair().pubkey())
    with pytest.raises(ChainUnavailable, match="not the program vault"):
        pay(transfer)
    assert not state.journal and not state.sent


def test_mode_switch_cannot_turn_a_transfer_into_a_mint(transfer):
    chain, state, wallet = transfer
    pay(transfer)
    state.height = 1000
    chain.treasury = Keypair()
    with pytest.raises(ChainUnavailable, match="cannot be retried in mint"):
        chain.settle_skr(wallet, 25, "logical-payment", persist=state.journal.append, prepared=state.journal[0])
    assert len(state.sent) == 1


def test_configuration_default_and_transfer_requirements():
    args = {"program_id": str(Keypair().pubkey()), "skr_mint": str(Keypair().pubkey())}
    assert Settings(**args).payout_mode == "mint"
    with pytest.raises(ValueError, match="payout_vault and payout_signer"):
        Settings(**args, payout_mode="transfer")
    with pytest.raises(ValueError):
        Settings(**args, payout_mode="invalid")


def test_public_chain_never_opens_payout_key(transfer, monkeypatch):
    chain, _, _ = transfer
    from pathlib import Path
    def fail(*_args, **_kwargs):
        raise AssertionError("API read private key")
    monkeypatch.setattr(Path, "read_text", fail)
    public = Chain(chain.settings)
    assert not hasattr(public, "settle_transfer")
    assert not hasattr(public, "payout_signer")


def test_backend_transfer_executes_real_cpi_and_recovers_after_confirmation_loss(transfer):
    from pathlib import Path
    from solders.account import Account
    from solders.litesvm import LiteSVM
    from solders.transaction_metadata import FailedTransactionMetadata
    from spl.token.instructions import get_associated_token_address
    from test_seeker_runtime import mint_account, token_account

    binary = Path(__file__).resolve().parents[2] / "target/transfer-test/seeker_bounty.so"
    if not binary.exists():
        pytest.skip("run cargo test -p seeker-bounty --lib first to build the test binary")
    chain, state, wallet = transfer
    svm = LiteSVM()
    state.slot = svm.get_clock().slot
    svm.add_program(chain.program, binary.read_bytes())
    svm.airdrop(chain.payout_signer.pubkey(), 1_000_000_000)
    for address, raw in state.accounts.items():
        svm.set_account(Pubkey.from_string(address), Account(10_000_000, raw, chain.program))
    # Mint has fixed supply and NO authority; transfer must work without one.
    raw = mint_account(Keypair().pubkey()).data
    mint = Account(10_000_000, bytes(36) + raw[36:], TOKEN_PROGRAM_ID)
    svm.set_account(chain.skr, mint)
    policy = chain.pda(b"payout", bytes(chain.config))
    vault = Pubkey.from_string(chain.settings.payout_vault)
    svm.set_account(vault, token_account(chain.skr, policy, 1000))
    original_rpc = chain.rpc
    confirmation_lost = True
    def rpc(method, params):
        if method == "getAccountInfo":
            account = svm.get_account(Pubkey.from_string(params[0]))
            return {"value": None if account is None else {"owner": str(account.owner),
                    "data": [base64.b64encode(account.data).decode(), "base64"]}}
        if method == "getLatestBlockhash":
            return {"value": {"blockhash": str(svm.latest_blockhash()), "lastValidBlockHeight": 100}}
        if method == "sendTransaction":
            assert params[0] == state.journal[-1]["transaction"]
            tx = Transaction.from_bytes(base64.b64decode(params[0]))
            result = svm.send_transaction(tx)
            assert not isinstance(result, FailedTransactionMetadata), str(result)
            signature = str(tx.signatures[0])
            state.sent.append(params[0])
            state.transactions[signature] = {"meta": {"err": None}, "transaction": [params[0], "base64"]}
            return signature
        if method == "getSignatureStatuses" and confirmation_lost:
            raise ChainUnavailable("confirmation lost")
        return original_rpc(method, params)
    chain.rpc = rpc
    with pytest.raises(ChainUnavailable, match="confirmation lost"):
        pay(transfer)
    ata = get_associated_token_address(Pubkey.from_string(wallet), chain.skr)
    assert struct.unpack("<Q", svm.get_account(vault).data[64:72])[0] == 975
    assert struct.unpack("<Q", svm.get_account(ata).data[64:72])[0] == 25
    assert svm.get_account(chain.skr).data == mint.data
    confirmation_lost = False
    assert pay(transfer, prepared=state.journal[0]) == state.journal[0]["signature"]
    assert len(state.sent) == len(state.journal) == 1


def test_live_attempt_rebroadcasts_identical_bytes_without_new_journal(transfer):
    chain, state, wallet = transfer
    state.land = False
    with pytest.raises(ChainUnavailable, match="receipt"):
        pay(transfer)
    state.land = True
    signature = pay(transfer, prepared=state.journal[0])
    assert signature == state.journal[0]["signature"]
    assert len(state.journal) == 1 and len(state.sent) == 2
    assert state.sent[0] == state.sent[1]


def test_mint_attempt_and_mint_receipt_cannot_be_repaid_as_transfer(transfer):
    chain, state, wallet = transfer
    chain.treasury = Keypair()
    state.land = False
    chain.settle_skr(wallet, 25, "logical-payment", persist=state.journal.append)
    state.height = 1000
    with pytest.raises(ChainUnavailable, match="Journaled transfer"):
        pay(transfer, prepared=state.journal[0])
    pid = hashlib.sha256(b"logical-payment").digest()[:16]
    receipt = chain.pda(b"payment-v2", bytes(chain.skr), bytes(Pubkey.from_string(wallet)), struct.pack("<Q", 25), pid)
    state.accounts[str(receipt)] = b"existing mint receipt"
    with pytest.raises(ChainUnavailable, match="Mint receipt exists"):
        pay(transfer)
    assert len(state.sent) == 1
