import base64
import hashlib
import json
import struct
from types import SimpleNamespace

import pytest
from seeker.chain import Chain, ChainUnavailable, discriminator, message_fingerprint
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import Message
from solders.transaction import Transaction
from spl.token.constants import TOKEN_PROGRAM_ID


@pytest.fixture
def chain(tmp_path):
    key = Keypair()
    p = tmp_path / "verifier.json"
    p.write_text(json.dumps(list(bytes(key))))
    return Chain(
        SimpleNamespace(
            program_id="5TiDwP86k95yfQWzExrPTj5qcKxED99z8pW9fN81u5SV",
            skr_mint=str(Keypair().pubkey()),
            verifier_key_file=p,
            cluster="localnet",
            rpc_url="http://127.0.0.1:8899",
            min_bond=1_000_000,
            faucet_sol_lamports=50_000_000,
        )
    )


@pytest.fixture
def payer():
    return Keypair()


def test_deposit_withdraw_instruction_layout(chain, payer):
    ix = chain.build("deposit", str(payer.pubkey()))[-1]
    assert ix.data == discriminator("deposit_bond") + struct.pack("<Q", 1_000_000)
    assert ix.accounts[0].pubkey == payer.pubkey() and ix.accounts[0].is_signer
    assert ix.accounts[0].is_writable
    assert ix.accounts[3].pubkey == chain.pda(b"bond", bytes(payer.pubkey()))
    assert ix.accounts[3].is_writable
    assert ix.accounts[6].pubkey == TOKEN_PROGRAM_ID
    ix = chain.build("withdraw", str(payer.pubkey()))[-1]
    assert ix.data == discriminator("withdraw_bond")
    assert ix.accounts[0].pubkey == payer.pubkey() and ix.accounts[0].is_signer
    assert not ix.accounts[0].is_writable
    with pytest.raises(ChainUnavailable):
        chain.build("unknown", str(payer.pubkey()))


def test_exact_signed_message_confirmation(chain, payer):
    msg = Message.new_with_blockhash(
        chain.build("deposit", str(payer.pubkey())), payer.pubkey(), Hash.new_unique()
    )
    tx = Transaction.new_unsigned(msg)
    tx.sign([payer], msg.recent_blockhash)
    p = SimpleNamespace(message_hash=message_fingerprint(msg))
    result = {
        "meta": {"err": None},
        "transaction": [base64.b64encode(bytes(tx)).decode(), "base64"],
    }
    chain.rpc = lambda *args: result
    assert chain.confirm(p, str(tx.signatures[0]))
    p.message_hash = "bad"
    with pytest.raises(ChainUnavailable):
        chain.confirm(p, str(tx.signatures[0]))
    p.message_hash = message_fingerprint(msg)
    result["meta"]["err"] = {"InstructionError": [0, "error"]}
    with pytest.raises(ChainUnavailable):
        chain.confirm(p, str(tx.signatures[0]))
    chain.rpc = lambda *args: None
    with pytest.raises(ChainUnavailable):
        chain.confirm(p, str(tx.signatures[0]))


def test_unsigned_or_other_signer_never_confirms(chain, payer):
    msg = Message.new_with_blockhash(
        chain.build("deposit", str(payer.pubkey())), payer.pubkey(), Hash.new_unique()
    )
    tx = Transaction.new_unsigned(msg)
    p = SimpleNamespace(message_hash=message_fingerprint(msg))
    chain.rpc = lambda *args: {
        "meta": {"err": None},
        "transaction": [base64.b64encode(bytes(tx)).decode(), "base64"],
    }
    with pytest.raises(ChainUnavailable):
        chain.confirm(p, str(tx.signatures[0]))


def test_environment_binds_config_mints_verifier_and_network(chain):
    from seeker.chain import DEVNET_GENESIS

    raw = (
        discriminator("Config", "account")
        + bytes(Keypair().pubkey())  # admin (not checked)
        + bytes(chain.verifier.pubkey())
        + bytes(chain.skr)
        + struct.pack("<Q", 1_000_000)
        + bytes(chain.verifier.pubkey())  # slash_authority == verifier
    )
    assert len(raw) == 144
    account = {
        "value": {
            "owner": str(chain.program),
            "data": [base64.b64encode(raw).decode(), "base64"],
        }
    }
    chain.rpc = lambda method, params: (
        DEVNET_GENESIS if method == "getGenesisHash" else account
    )
    chain.check_environment()
    chain.settings.cluster = "devnet"
    chain.check_environment()
    account["value"]["owner"] = str(Keypair().pubkey())
    with pytest.raises(ChainUnavailable):
        chain.check_environment()
    chain.rpc = lambda *args: "mainnet"
    with pytest.raises(ChainUnavailable):
        chain.check_environment()


def test_environment_rejects_stale_config(chain):
    from seeker.chain import DEVNET_GENESIS

    # A stale config (pre-slash_authority, single-mint) must be rejected.
    raw = (
        discriminator("Config", "account")
        + bytes(Keypair().pubkey())
        + bytes(chain.verifier.pubkey())
        + bytes(chain.skr)
        + struct.pack("<Q", 1_000_000)
    )
    account = {
        "value": {
            "owner": str(chain.program),
            "data": [base64.b64encode(raw).decode(), "base64"],
        }
    }
    chain.rpc = lambda method, params: (
        DEVNET_GENESIS if method == "getGenesisHash" else account
    )
    with pytest.raises(ChainUnavailable):
        chain.check_environment()


def test_warn_slash_instruction_layout(chain, payer):
    ix = chain.build("warn", str(payer.pubkey()))[-1]
    assert ix.data == discriminator("warn") + bytes(payer.pubkey())
    assert len(ix.accounts) == 3
    assert ix.accounts[0].pubkey == chain.verifier.pubkey() and ix.accounts[0].is_signer
    assert ix.accounts[1].pubkey == chain.config and not ix.accounts[1].is_writable
    assert ix.accounts[2].pubkey == chain.pda(b"bond", bytes(payer.pubkey()))
    assert ix.accounts[2].is_writable

    ix = chain.build("slash", str(payer.pubkey()))[-1]
    assert ix.data == discriminator("slash") + bytes(payer.pubkey())
    assert len(ix.accounts) == 6
    assert ix.accounts[0].pubkey == chain.verifier.pubkey() and ix.accounts[0].is_signer
    assert ix.accounts[2].pubkey == chain.pda(b"bond", bytes(payer.pubkey()))
    assert ix.accounts[3].pubkey == chain.pda(b"bond-vault", bytes(payer.pubkey()), bytes(chain.skr))
    assert ix.accounts[3].is_writable
    assert ix.accounts[4].pubkey == chain.penalty_vault and ix.accounts[4].is_writable
    assert ix.accounts[5].pubkey == TOKEN_PROGRAM_ID


def test_find_signature_paginates_beyond_first_100(chain, payer):
    msg = Message.new_with_blockhash(
        chain.build("deposit", str(payer.pubkey())), payer.pubkey(), Hash.new_unique()
    )
    tx = Transaction.new_unsigned(msg)
    tx.sign([payer], msg.recent_blockhash)
    p = SimpleNamespace(
        wallet=str(payer.pubkey()),
        message_hash=message_fingerprint(msg),
    )
    target = str(tx.signatures[0])

    decoys = [str(Keypair().sign_message(b"decoy")) for _ in range(100)]
    page1 = [{"signature": s, "err": None} for s in decoys]
    page2 = [{"signature": target, "err": None}]
    tx_lookup = {
        target: {
            "meta": {"err": None},
            "transaction": [base64.b64encode(bytes(tx)).decode(), "base64"],
        }
    }

    calls = []

    def fake_rpc(method, params):
        calls.append((method, params))
        if method == "getSignaturesForAddress":
            return page1 if params[1].get("before") is None else page2
        if method == "getTransaction":
            return tx_lookup.get(params[0])
        raise AssertionError(f"unexpected rpc method: {method}")

    chain.rpc = fake_rpc
    assert chain.find_signature(p) == target

    sig_calls = [c for c in calls if c[0] == "getSignaturesForAddress"]
    assert len(sig_calls) == 2
    assert sig_calls[0][1][1] == {"commitment": "finalized", "limit": 100}
    assert sig_calls[1][1][1]["before"] == decoys[-1]


def test_faucet_and_settle_build_signed_instructions(chain, tmp_path):
    # Regression: the treasury is a Keypair (not a Pubkey), and the SPL
    # instruction params take pubkeys while _sign_and_send signs with the
    # Keypair. This exercises both faucet and settle_skr end to end.
    treasury = Keypair()
    p = tmp_path / "treasury.json"
    p.write_text(json.dumps(list(bytes(treasury))))
    chain.treasury = treasury

    worker = Keypair()
    sent = []

    def fake_rpc(method, params):
        sent.append((method, params))
        if method == "getLatestBlockhash":
            return {"value": {"blockhash": str(Hash.new_unique())}}
        if method == "sendTransaction":
            return "sig_" + method
        if method == "getSignatureStatuses":
            # confirm success so settle_skr's _confirm_sent passes
            return {"value": [{"err": None, "confirmationStatus": "finalized"}]}
        raise AssertionError(f"unexpected rpc method: {method}")

    chain.rpc = fake_rpc
    # Faucet mints min_bond $SKR to the worker (mint authority = treasury).
    sig = chain.faucet(str(worker.pubkey()))
    assert sig == "sig_sendTransaction"
    # Settlement mints test $SKR to the judge (mint authority = treasury).
    sig = chain.settle_skr(str(worker.pubkey()), 100_000, memo="judgment-1")
    assert sig == "sig_sendTransaction"

    # Both produced exactly two sendTransaction calls (one each) and the
    # instructions serialized without a Keypair/Pubkey type mismatch.
    sends = [c for c in sent if c[0] == "sendTransaction"]
    assert len(sends) == 2
