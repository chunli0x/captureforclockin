"""Execute compiled SBF and SPL token CPIs in LiteSVM. No external wallet or funds."""

import json
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest
from seeker.chain import RENT, SYSTEM, Chain, discriminator, meta
from solders.account import Account
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.litesvm import LiteSVM
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction
from solders.transaction_metadata import FailedTransactionMetadata
from spl.token.constants import TOKEN_PROGRAM_ID
from spl.token.instructions import get_associated_token_address

SO = Path(__file__).resolve().parents[2] / "target/deploy/seeker_bounty.so"
pytestmark = pytest.mark.skipif(
    not SO.exists(), reason="build seeker-bounty SBF first for real program execution"
)


def token_account(mint, owner, amount):
    data = bytearray(165)
    data[:32] = bytes(mint)
    data[32:64] = bytes(owner)
    data[64:72] = struct.pack("<Q", amount)
    data[108] = 1
    return Account(10_000_000, bytes(data), TOKEN_PROGRAM_ID)


def mint_account(authority):
    data = (
        struct.pack("<I", 1)
        + bytes(authority)
        + struct.pack("<QB", 1_000_000_000, 6)
        + bytes([1])
        + bytes(36)
    )
    assert len(data) == 82
    return Account(10_000_000, data, TOKEN_PROGRAM_ID)


@pytest.fixture
def runtime(tmp_path):
    svm = LiteSVM()
    admin, worker, other, verifier = [Keypair() for _ in range(4)]
    key = tmp_path / "key.json"
    key.write_text(json.dumps(list(bytes(verifier))))
    chain = Chain(
        SimpleNamespace(
            program_id="5TiDwP86k95yfQWzExrPTj5qcKxED99z8pW9fN81u5SV",
            skr_mint=str(Keypair().pubkey()),
            verifier_key_file=key,
            min_bond=100,
            cluster="localnet",
            rpc_url="http://127.0.0.1:8899",
        )
    )
    svm.add_program(chain.program, SO.read_bytes())
    for k in (admin, worker, other, verifier):
        svm.airdrop(k.pubkey(), 10_000_000_000)
    for mint in (chain.skr,):
        svm.set_account(mint, mint_account(admin.pubkey()))
        for k in (admin, worker, other):
            svm.set_account(
                get_associated_token_address(k.pubkey(), mint),
                token_account(mint, k.pubkey(), 10_000),
            )
    cfg = (
        discriminator("Config", "account")
        + bytes(admin.pubkey())
        + bytes(verifier.pubkey())
        + bytes(chain.skr)
        + struct.pack("<Q", 100)
        + bytes(verifier.pubkey())  # slash_authority == verifier
    )
    svm.set_account(chain.config, Account(10_000_000, cfg, chain.program))
    # penalty_vault: program-owned $SKR token account (authority == config).
    svm.set_account(chain.penalty_vault, token_account(chain.skr, chain.config, 0))
    clock = svm.get_clock()
    clock.unix_timestamp = 100
    svm.set_clock(clock)
    return svm, chain, admin, worker, other, verifier


def send(rt, action, who, extra=(), modify=None):
    svm, chain, *_ = rt
    instructions = chain.build(action, str(who.pubkey()))
    if modify:
        instructions = modify(instructions)
    svm.expire_blockhash()
    msg = Message.new_with_blockhash(instructions, who.pubkey(), svm.latest_blockhash())
    tx = Transaction.new_unsigned(msg)
    tx.sign([who, *extra], msg.recent_blockhash)
    return svm.send_transaction(tx)


def send_server(rt, action, wallet, amount=None):
    """Server-signed warn/slash: the verifier is slash_authority + fee payer."""
    svm, chain, *_ = rt
    instructions = chain.build(action, str(wallet), amount=amount)
    svm.expire_blockhash()
    msg = Message.new_with_blockhash(
        instructions, chain.verifier.pubkey(), svm.latest_blockhash()
    )
    tx = Transaction.new_unsigned(msg)
    tx.sign([chain.verifier], msg.recent_blockhash)
    return svm.send_transaction(tx)


def ok(result):
    assert not isinstance(result, FailedTransactionMetadata), str(result)


def bad(result):
    assert isinstance(result, FailedTransactionMetadata), str(result)


def balance(rt, wallet, mint):
    a = rt[0].get_account(get_associated_token_address(wallet, mint))
    return struct.unpack("<Q", a.data[64:72])[0]


def test_bond_deposit_withdraw_roundtrip(runtime):
    rt = runtime
    svm, c, _admin, worker, _other, _verifier = rt
    assert balance(rt, worker.pubkey(), c.skr) == 10_000
    ok(send(rt, "deposit", worker))
    # deposit escrows min_bond (100) $SKR into the bond vault.
    assert balance(rt, worker.pubkey(), c.skr) == 9_900
    # Withdraw is locked until the cooldown elapses (unlock_at = deposit + 1 day).
    bad(send(rt, "withdraw", worker))
    clock = svm.get_clock()
    clock.unix_timestamp = 86_501
    svm.set_clock(clock)
    ok(send(rt, "withdraw", worker))
    assert balance(rt, worker.pubkey(), c.skr) == 10_000


def test_warn_then_slash_ladder_drains_bond_to_penalty_vault(runtime):
    rt = runtime
    svm, c, _admin, worker, _other, _verifier = rt
    ok(send(rt, "deposit", worker))
    bond = c.pda(b"bond", bytes(worker.pubkey()))

    def warnings():
        return svm.get_account(bond).data[52]

    def penalty_balance():
        return struct.unpack("<Q", svm.get_account(c.penalty_vault).data[64:72])[0]

    assert warnings() == 0
    ok(send_server(rt, "warn", worker.pubkey()))
    assert warnings() == 1
    ok(send_server(rt, "warn", worker.pubkey()))
    assert warnings() == 2
    # Slash takes the full bond into the penalty vault; the bond is terminal.
    assert penalty_balance() == 0
    ok(send_server(rt, "slash", worker.pubkey()))
    assert warnings() == 255
    assert penalty_balance() == 100
    # A slashed bond can never withdraw.
    clock = svm.get_clock()
    clock.unix_timestamp = 86_501
    svm.set_clock(clock)
    bad(send(rt, "withdraw", worker))


def test_initializer_requires_program_upgrade_authority(runtime):
    svm, c, admin, _worker, other, verifier = runtime
    # Remove the fixture's initialized config and penalty vault and install an
    # actual loader-v3 ProgramData authority, retaining the same compiled code.
    svm.set_account(c.config, Account(0, b"", SYSTEM))
    svm.set_account(c.penalty_vault, Account(0, b"", SYSTEM))
    program = svm.get_account(c.program)
    data_address = Pubkey.from_bytes(program.data[4:36])
    data = svm.get_account(data_address)
    raw = struct.pack("<IQB", 3, 0, 1) + bytes(admin.pubkey()) + SO.read_bytes()
    svm.set_account(data_address, Account(data.lamports, raw, program.owner))

    def initialize(who):
        ix = Instruction(
            c.program,
            discriminator("initialize")
            + bytes(verifier.pubkey())
            + bytes(verifier.pubkey())  # slash_authority == verifier
            + struct.pack("<Q", 100),
            [
                meta(who.pubkey(), True, True),
                meta(c.config, writable=True),
                meta(c.skr),
                meta(c.penalty_vault, writable=True),
                meta(c.program),
                meta(data_address),
                meta(SYSTEM),
                meta(TOKEN_PROGRAM_ID),
                meta(RENT),
            ],
        )
        svm.expire_blockhash()
        msg = Message.new_with_blockhash([ix], who.pubkey(), svm.latest_blockhash())
        tx = Transaction.new_unsigned(msg)
        tx.sign([who], msg.recent_blockhash)
        return svm.send_transaction(tx)

    bad(initialize(other))
    ok(initialize(admin))
    assert svm.get_account(c.config).data[8:40] == bytes(admin.pubkey())
    bad(initialize(other))


def send_instructions(runtime, instructions, signer):
    svm = runtime[0]
    svm.expire_blockhash()
    msg = Message.new_with_blockhash(instructions, signer.pubkey(), svm.latest_blockhash())
    tx = Transaction.new_unsigned(msg)
    tx.sign([signer], msg.recent_blockhash)
    return svm.send_transaction(tx)


def liability_instruction(runtime, worker, rid, *, penalize=None, authority=None):
    _svm, c, _admin, _worker, _other, verifier = runtime
    authority = authority or verifier
    accounts = [meta(authority.pubkey(), True, True), meta(c.config),
                meta(c.pda(b"bond", bytes(worker)), writable=True),
                meta(c.pda(b"bond-vault", bytes(worker), bytes(c.skr)), writable=penalize is not None),
                meta(c.pda(b"liability", bytes(worker), rid), writable=True)]
    if penalize is None:
        accounts += [meta(SYSTEM)]
        data = discriminator("reserve_judgment") + bytes(worker) + rid
    else:
        accounts += [meta(c.penalty_vault, writable=True), meta(TOKEN_PROGRAM_ID), meta(SYSTEM)]
        data = discriminator("resolve_judgment") + bytes(worker) + rid + bytes([penalize])
    return Instruction(c.program, data, accounts)


def pending(runtime, worker):
    _svm, c, *_ = runtime
    data = runtime[0].get_account(c.pda(b"bond", bytes(worker))).data
    return struct.unpack("<I", data[40:44])[0]


def test_open_liabilities_lock_withdrawal_until_each_resolution(runtime):
    svm, c, _admin, worker, other, verifier = runtime
    ok(send(runtime, "deposit", worker))
    first, second = b"1" * 16, b"2" * 16
    reserve = liability_instruction(runtime, worker.pubkey(), first)
    bad(send_instructions(runtime, [liability_instruction(runtime, worker.pubkey(), first, authority=other)], other))
    assert pending(runtime, worker.pubkey()) == 0
    ok(send_instructions(runtime, [reserve], verifier))
    ok(send_instructions(runtime, [reserve], verifier))
    assert pending(runtime, worker.pubkey()) == 1
    ok(send_instructions(runtime, [liability_instruction(runtime, worker.pubkey(), second)], verifier))
    clock = svm.get_clock()
    clock.unix_timestamp = 86_501
    svm.set_clock(clock)
    bad(send(runtime, "withdraw", worker))
    release = liability_instruction(runtime, worker.pubkey(), first, penalize=False)
    bad(send_instructions(runtime, [liability_instruction(runtime, worker.pubkey(), first, penalize=False, authority=other)], other))
    ok(send_instructions(runtime, [release], verifier))
    ok(send_instructions(runtime, [release], verifier))
    assert pending(runtime, worker.pubkey()) == 1
    bad(send(runtime, "withdraw", worker))
    ok(send_instructions(runtime, [liability_instruction(runtime, worker.pubkey(), second, penalize=False)], verifier))
    assert pending(runtime, worker.pubkey()) == 0
    ok(send(runtime, "withdraw", worker))
    bad(send_instructions(runtime, [liability_instruction(runtime, worker.pubkey(), b"3" * 16)], verifier))


def test_resolution_warn_and_slash_are_atomic_and_idempotent(runtime):
    svm, c, _admin, worker, _other, verifier = runtime
    ok(send(runtime, "deposit", worker))
    ids = [bytes([n]) * 16 for n in (1, 2, 3)]
    for rid in ids:
        ok(send_instructions(runtime, [liability_instruction(runtime, worker.pubkey(), rid)], verifier))
    first = liability_instruction(runtime, worker.pubkey(), ids[0], penalize=True)
    ok(send_instructions(runtime, [first], verifier))
    ok(send_instructions(runtime, [first], verifier))
    bond = c.pda(b"bond", bytes(worker.pubkey()))
    assert svm.get_account(bond).data[52] == 1
    assert pending(runtime, worker.pubkey()) == 2
    second = liability_instruction(runtime, worker.pubkey(), ids[1], penalize=True)
    ok(send_instructions(runtime, [second], verifier))
    ok(send_instructions(runtime, [second], verifier))
    assert svm.get_account(bond).data[52] == 255
    assert struct.unpack("<Q", svm.get_account(c.penalty_vault).data[64:72])[0] == 100
    assert pending(runtime, worker.pubkey()) == 1
    # Fresh deposits cannot erase outstanding liabilities after a slash.
    bad(send(runtime, "deposit", worker))
    ok(send_instructions(runtime, [liability_instruction(runtime, worker.pubkey(), ids[2], penalize=False)], verifier))
    ok(send(runtime, "deposit", worker))
    assert svm.get_account(bond).data[52] == 0
    # Replaying the old penalty cannot slash this new deposit.
    ok(send_instructions(runtime, [second], verifier))
    assert svm.get_account(bond).data[52] == 0
    bad(send_instructions(runtime, [liability_instruction(runtime, worker.pubkey(), ids[0], penalize=False)], verifier))


def test_cancelled_submission_tombstone_blocks_late_reservation(runtime):
    _svm, _c, _admin, worker, _other, verifier = runtime
    ok(send(runtime, "deposit", worker))
    rid = b"c" * 16
    ok(send_instructions(runtime, [liability_instruction(runtime, worker.pubkey(), rid, penalize=False)], verifier))
    bad(send_instructions(runtime, [liability_instruction(runtime, worker.pubkey(), rid)], verifier))
    assert pending(runtime, worker.pubkey()) == 0


def payment_instruction(runtime, signer, recipient, amount, payment_id, *, mint=None, receipt=None, destination=None):
    c = runtime[1]
    mint = mint or c.skr
    receipt = receipt or c.pda(b"payment-v2", bytes(mint), bytes(recipient), struct.pack("<Q", amount), payment_id)
    destination = destination or get_associated_token_address(recipient, mint)
    return Instruction(c.program, discriminator("record_payment") + payment_id + bytes(recipient) + struct.pack("<Q", amount), [
        meta(signer.pubkey(), True, True), meta(c.config), meta(mint, writable=True),
        meta(receipt, writable=True), meta(destination, writable=True), meta(SYSTEM), meta(TOKEN_PROGRAM_ID),
    ])


def test_payment_receipt_requires_mint_authority_and_proves_exact_atomic_mint(runtime):
    svm, c, admin, worker, other, _verifier = runtime
    pid = b"p" * 16
    receipt = c.pda(b"payment-v2", bytes(c.skr), bytes(worker.pubkey()), struct.pack("<Q", 25), pid)
    bad(send_instructions(runtime, [payment_instruction(runtime, other, worker.pubkey(), 25, pid)], other))
    assert svm.get_account(receipt) is None
    assert balance(runtime, worker.pubkey(), c.skr) == 10_000
    # The address is bound independently to amount, recipient and mint.
    bad(send_instructions(runtime, [payment_instruction(runtime, admin, worker.pubkey(), 26, pid, receipt=receipt)], admin))
    bad(send_instructions(runtime, [payment_instruction(runtime, admin, other.pubkey(), 25, pid, receipt=receipt)], admin))
    other_mint = Pubkey.new_unique()
    svm.set_account(other_mint, mint_account(admin.pubkey()))
    svm.set_account(get_associated_token_address(worker.pubkey(), other_mint), token_account(other_mint, worker.pubkey(), 0))
    bad(send_instructions(runtime, [payment_instruction(runtime, admin, worker.pubkey(), 25, pid, mint=other_mint)], admin))
    # Wrong destination fails without leaving a receipt behind.
    bad(send_instructions(runtime, [payment_instruction(runtime, admin, worker.pubkey(), 25, pid,
        destination=get_associated_token_address(other.pubkey(), c.skr))], admin))
    assert svm.get_account(receipt) is None
    ix = payment_instruction(runtime, admin, worker.pubkey(), 25, pid)
    ok(send_instructions(runtime, [ix], admin))
    assert balance(runtime, worker.pubkey(), c.skr) == 10_025
    assert svm.get_account(receipt).data == (discriminator("Payment", "account") + bytes(admin.pubkey())
        + bytes(worker.pubkey()) + bytes(c.skr) + struct.pack("<Q", 25) + pid)
    bad(send_instructions(runtime, [ix], admin))
    assert balance(runtime, worker.pubkey(), c.skr) == 10_025


def test_chain_retry_recovers_finalized_receipt_after_confirmation_loss(runtime):
    import base64
    from seeker.chain import ChainUnavailable
    svm, chain, admin, worker, _other, _verifier = runtime
    chain.treasury = admin
    sent, journal = {}, []
    confirmation_available = False

    def rpc(method, params):
        if method == "getAccountInfo":
            account = svm.get_account(Pubkey.from_string(params[0]))
            return {"value": None if account is None else {
                "owner": str(account.owner), "data": [base64.b64encode(account.data).decode(), "base64"],
            }}
        if method == "getLatestBlockhash":
            svm.expire_blockhash()
            return {"value": {"blockhash": str(svm.latest_blockhash()), "lastValidBlockHeight": 100}}
        if method == "sendTransaction":
            assert journal, "signed transaction must be persisted BEFORE broadcast"
            assert journal[-1]["transaction"] == params[0]
            tx = Transaction.from_bytes(base64.b64decode(params[0]))
            ok(svm.send_transaction(tx))
            sig = str(tx.signatures[0])
            sent[sig] = params[0]
            return sig
        if method == "getSignatureStatuses":
            if not confirmation_available:
                raise ChainUnavailable("confirmation transport lost")
            return {"value": [{"err": None, "confirmationStatus": "finalized"}]}
        if method == "getSignaturesForAddress":
            return [{"signature": sig, "err": None} for sig in sent]
        if method == "getTransaction":
            return {"meta": {"err": None}, "transaction": [sent[params[0]], "base64"]}
        raise AssertionError(method)

    chain.rpc = rpc
    with pytest.raises(ChainUnavailable, match="confirmation transport"):
        chain.settle_skr(str(worker.pubkey()), 25, "retry-test", persist=journal.append)
    assert balance(runtime, worker.pubkey(), chain.skr) == 10_025
    confirmation_available = True
    sig = chain.settle_skr(str(worker.pubkey()), 25, "retry-test", persist=journal.append, prepared=journal[-1])
    assert sig == journal[-1]["signature"]
    assert len(sent) == 1 and len(journal) == 1
    assert balance(runtime, worker.pubkey(), chain.skr) == 10_025
