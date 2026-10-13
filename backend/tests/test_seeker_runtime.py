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
