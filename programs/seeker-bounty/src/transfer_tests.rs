//! Host tests execute the actual SBF program + SPL Token CPI in an isolated VM.
//! Synthetic test mint/prefunded vault only; no RPC, deployer keys or real funds.
use super::*;
use anchor_lang::solana_program::{
    instruction::Instruction, program_option::COption, program_pack::Pack,
};
use anchor_lang::{InstructionData, ToAccountMetas};
use anchor_spl::token::spl_token::state::{Account as TokenState, AccountState, Mint as MintState};
use litesvm::LiteSVM;
use solana_account::Account as VmAccount;
use solana_keypair::Keypair;
use solana_signer::Signer as _;
use solana_transaction::Transaction;
use std::{path::PathBuf, sync::OnceLock};

fn binary() -> &'static Vec<u8> {
    static BINARY: OnceLock<Vec<u8>> = OnceLock::new();
    BINARY.get_or_init(|| {
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
        let status = std::process::Command::new("cargo")
            .args([
                "build-sbf",
                "--manifest-path",
                "programs/seeker-bounty/Cargo.toml",
                "--sbf-out-dir",
                "target/transfer-test",
            ])
            .current_dir(&root)
            .status()
            .expect("cargo-build-sbf must be installed for VM tests");
        assert!(status.success(), "test-only SBF build failed");
        std::fs::read(root.join("target/transfer-test/seeker_bounty.so")).unwrap()
    })
}

struct Fixture {
    svm: LiteSVM,
    authority: Keypair,
    admin: Keypair,
    config: Pubkey,
    policy: Pubkey,
    mint: Pubkey,
    vault: Pubkey,
    recipient: Pubkey,
    destination: Pubkey,
}

impl Fixture {
    fn new() -> Self {
        let mut svm = LiteSVM::new();
        svm.add_program(ID, binary()).unwrap();
        let authority = Keypair::new();
        let admin = Keypair::new();
        svm.airdrop(&authority.pubkey(), 1_000_000_000).unwrap();
        svm.airdrop(&admin.pubkey(), 1_000_000_000).unwrap();
        let config = Pubkey::find_program_address(&[b"config"], &ID).0;
        let policy = Pubkey::find_program_address(&[b"payout", config.as_ref()], &ID).0;
        let mint = Pubkey::new_unique();
        let vault = Pubkey::find_program_address(&[b"payout-vault", policy.as_ref()], &ID).0;
        let recipient = Pubkey::new_unique();
        let destination = Pubkey::new_unique();
        let mut f = Self {
            svm,
            authority,
            admin,
            config,
            policy,
            mint,
            vault,
            recipient,
            destination,
        };
        f.put_anchor(
            config,
            &Config {
                admin: f.admin.pubkey().into(),
                verifier: Pubkey::new_unique(),
                skr_mint: mint,
                min_bond: 1,
                slash_authority: Pubkey::new_unique(),
            },
        );
        // No mint authority at all. Minting cannot be used to satisfy these tests.
        let mut data = vec![0; MintState::LEN];
        MintState::pack(
            MintState {
                mint_authority: COption::None,
                supply: 1_000,
                decimals: 6,
                is_initialized: true,
                freeze_authority: COption::None,
            },
            &mut data,
        )
        .unwrap();
        f.put(mint, token::ID, data);
        f.put_token(vault, policy, mint, 1_000);
        f.put_token(destination, recipient, mint, 0);
        f.put_anchor(
            policy,
            &PayoutVault {
                mint,
                vault,
                payout_authority: f.authority.pubkey().into(),
                paused: false,
                per_transfer_cap: 100,
                epoch_cap: 150,
                epoch: 0,
                spent: 0,
            },
        );
        f
    }

    fn put(&mut self, key: Pubkey, owner: Pubkey, data: Vec<u8>) {
        self.svm
            .set_account(
                key.into(),
                VmAccount {
                    lamports: self.svm.minimum_balance_for_rent_exemption(data.len()),
                    data,
                    owner: owner.into(),
                    executable: false,
                    rent_epoch: 0,
                },
            )
            .unwrap();
    }

    fn put_anchor<T: AccountSerialize>(&mut self, key: Pubkey, value: &T) {
        let mut data = Vec::new();
        value.try_serialize(&mut data).unwrap();
        self.put(key, ID, data);
    }

    fn put_token(&mut self, key: Pubkey, owner: Pubkey, mint: Pubkey, amount: u64) {
        let mut data = vec![0; TokenState::LEN];
        TokenState::pack(
            TokenState {
                mint,
                owner,
                amount,
                state: AccountState::Initialized,
                ..TokenState::default()
            },
            &mut data,
        )
        .unwrap();
        self.put(key, token::ID, data);
    }

    fn balance(&self, key: Pubkey) -> u64 {
        TokenState::unpack(&self.svm.get_account(&key.into()).unwrap().data)
            .unwrap()
            .amount
    }

    fn policy(&self) -> PayoutVault {
        PayoutVault::try_deserialize(
            &mut self
                .svm
                .get_account(&self.policy.into())
                .unwrap()
                .data
                .as_slice(),
        )
        .unwrap()
    }

    fn ix(&self, id: u8, amount: u64, version: u64, deadline: u64) -> Instruction {
        Instruction {
            program_id: ID,
            accounts: crate::accounts::RecordPaymentV3 {
                payout_authority: self.authority.pubkey().into(),
                config: self.config,
                payout: self.policy,
                mint: self.mint,
                vault: self.vault,
                payment: Pubkey::find_program_address(&[b"payment-v3", &[id; 16]], &ID).0,
                destination: self.destination,
                system_program: anchor_lang::system_program::ID,
                token_program: token::ID,
            }
            .to_account_metas(None),
            data: crate::instruction::RecordPaymentV3 {
                payment_id: [id; 16],
                recipient: self.recipient,
                amount,
                obligation_version: version,
                valid_until_slot: deadline,
            }
            .data(),
        }
    }

    fn send(&mut self, ix: Instruction) -> bool {
        self.svm.expire_blockhash(); // Distinct attempts reach the program's receipt guard.
        let tx = Transaction::new_signed_with_payer(
            &[ix],
            Some(&self.authority.pubkey()),
            &[&self.authority],
            self.svm.latest_blockhash(),
        );
        self.svm.send_transaction(tx).is_ok()
    }

    fn assert_unchanged_failure(&mut self, ix: Instruction) {
        let before = (
            self.balance(self.vault),
            self.balance(self.destination),
            self.policy().spent,
        );
        let receipt = ix.accounts[5].pubkey;
        let receipt_before = self.svm.get_account(&receipt);
        assert!(!self.send(ix));
        assert_eq!(
            before,
            (
                self.balance(self.vault),
                self.balance(self.destination),
                self.policy().spent
            )
        );
        assert_eq!(receipt_before, self.svm.get_account(&receipt));
    }
}

#[test]
fn transfer_conserves_balances_and_supply_with_no_mint_authority() {
    let mut f = Fixture::new();
    let mint_before = f.svm.get_account(&f.mint.into()).unwrap().data;
    assert!(f.send(f.ix(1, 75, 1, 100)));
    assert_eq!((f.balance(f.vault), f.balance(f.destination)), (925, 75));
    assert_eq!(mint_before, f.svm.get_account(&f.mint.into()).unwrap().data);
    assert_eq!(f.policy().spent, 75);
    let key = Pubkey::find_program_address(&[b"payment-v3", &[1; 16]], &ID).0;
    let raw = f.svm.get_account(&key.into()).unwrap().data;
    let receipt = PaymentV3::try_deserialize(&mut raw.as_slice()).unwrap();
    assert_eq!(
        (
            receipt.recipient,
            receipt.mint,
            receipt.amount,
            receipt.payment_id,
            receipt.funding_vault,
            receipt.obligation_version
        ),
        (f.recipient, f.mint, 75, [1; 16], f.vault, 1)
    );
    assert_eq!(raw.len(), 136);
}

#[test]
fn duplicate_and_conflicting_logical_ids_never_pay_twice() {
    let mut f = Fixture::new();
    assert!(f.send(f.ix(2, 40, 1, 100)));
    f.assert_unchanged_failure(f.ix(2, 40, 1, 100));
    f.assert_unchanged_failure(f.ix(2, 41, 1, 100));
    f.assert_unchanged_failure(f.ix(2, 40, 2, 100));
    let other = Pubkey::new_unique();
    f.recipient = other;
    f.put_token(f.destination, other, f.mint, 40);
    f.assert_unchanged_failure(f.ix(2, 40, 1, 100));
}

#[test]
fn wrong_vault_mint_authority_destination_decimals_and_program_fail() {
    for account in [0, 3, 4, 6, 8] {
        let mut f = Fixture::new();
        let mut ix = f.ix(3, 40, 1, 100);
        // Authority failure reaches has_one, with a genuinely signing key.
        if account == 0 {
            let mut policy = f.policy();
            policy.payout_authority = Pubkey::new_unique();
            f.put_anchor(f.policy, &policy);
        } else {
            let wrong = Pubkey::new_unique();
            if account == 4 {
                f.put_token(wrong, f.policy, f.mint, 1000);
            }
            if account == 6 {
                f.put_token(wrong, Pubkey::new_unique(), f.mint, 0);
            }
            if account == 3 {
                let data = f.svm.get_account(&f.mint.into()).unwrap().data;
                f.put(wrong, token::ID, data);
            }
            ix.accounts[account].pubkey = wrong;
        }
        f.assert_unchanged_failure(ix);
    }
    let mut f = Fixture::new();
    let mut mint = MintState::unpack(&f.svm.get_account(&f.mint.into()).unwrap().data).unwrap();
    mint.decimals = 9;
    let mut data = vec![0; MintState::LEN];
    MintState::pack(mint, &mut data).unwrap();
    f.put(f.mint, token::ID, data);
    f.assert_unchanged_failure(f.ix(3, 40, 1, 100));
}

#[test]
fn pause_caps_epoch_rollover_and_empty_vault_are_atomic() {
    let mut f = Fixture::new();
    let mut policy = f.policy();
    policy.paused = true;
    f.put_anchor(f.policy, &policy);
    f.assert_unchanged_failure(f.ix(4, 1, 1, 100));
    policy.paused = false;
    f.put_anchor(f.policy, &policy);
    f.assert_unchanged_failure(f.ix(4, 101, 1, 100));
    assert!(f.send(f.ix(4, 100, 1, 100)));
    f.assert_unchanged_failure(f.ix(5, 51, 1, 100));
    assert!(f.send(f.ix(5, 50, 1, 100)));
    let mut clock = f.svm.get_sysvar::<Clock>();
    clock.epoch += 1;
    f.svm.set_sysvar(&clock);
    assert!(f.send(f.ix(6, 100, 1, 100)));
    assert_eq!(f.policy().spent, 100);
    f.put_token(f.vault, f.policy, f.mint, 0);
    f.assert_unchanged_failure(f.ix(7, 10, 1, 100));
}

#[test]
fn expired_attempt_and_replacement_worker_share_one_receipt() {
    let mut f = Fixture::new();
    let mut clock = f.svm.get_sysvar::<Clock>();
    clock.slot = 101;
    f.svm.set_sysvar(&clock);
    f.assert_unchanged_failure(f.ix(8, 10, 1, 100));
    // Replacement worker uses the same logical identity with a new deadline.
    assert!(f.send(f.ix(8, 10, 1, 200)));
    f.assert_unchanged_failure(f.ix(8, 10, 1, 300));
    assert_eq!(f.balance(f.destination), 10);
}

impl Fixture {
    fn program_data(&self) -> Pubkey {
        Pubkey::find_program_address(
            &[ID.as_ref()],
            &anchor_lang::solana_program::bpf_loader_upgradeable::ID,
        )
        .0
    }

    fn configure(
        &mut self,
        authority: Pubkey,
        paused: bool,
        per_transfer_cap: u64,
        epoch_cap: u64,
        admin: bool,
    ) -> bool {
        self.svm.expire_blockhash();
        let signer = if admin { &self.admin } else { &self.authority };
        let ix = Instruction {
            program_id: ID,
            accounts: crate::accounts::ConfigurePayout {
                admin: signer.pubkey().into(),
                config: self.config,
                payout: self.policy,
                program: ID,
                program_data: self.program_data(),
            }
            .to_account_metas(None),
            data: crate::instruction::ConfigurePayout {
                payout_authority: authority,
                paused,
                per_transfer_cap,
                epoch_cap,
            }
            .data(),
        };
        self.svm
            .send_transaction(Transaction::new_signed_with_payer(
                &[ix],
                Some(&signer.pubkey()),
                &[signer],
                self.svm.latest_blockhash(),
            ))
            .is_ok()
    }
}

#[test]
fn policy_admin_controls_pause_caps_and_rotation_without_resetting_spend() {
    let mut f = Fixture::new();
    assert!(f.send(f.ix(9, 40, 1, 100)));
    assert!(!f.configure(f.authority.pubkey().into(), true, 100, 150, false));
    assert!(!f.policy().paused);
    assert!(f.configure(f.authority.pubkey().into(), true, 100, 150, true));
    f.assert_unchanged_failure(f.ix(10, 10, 1, 100));
    assert!(f.configure(f.authority.pubkey().into(), false, 100, 150, true));
    assert_eq!(f.policy().spent, 40);
    let config =
        Config::try_deserialize(&mut f.svm.get_account(&f.config.into()).unwrap().data.as_slice())
            .unwrap();
    for forbidden in [
        Pubkey::default(),
        config.admin,
        config.verifier,
        config.slash_authority,
    ] {
        assert!(!f.configure(forbidden, false, 100, 150, true));
    }
    assert!(!f.configure(f.authority.pubkey().into(), false, 0, 150, true));
    assert!(!f.configure(f.authority.pubkey().into(), false, 100, 50, true));
    let replacement = Keypair::new();
    f.svm.airdrop(&replacement.pubkey(), 1_000_000_000).unwrap();
    assert!(f.configure(replacement.pubkey().into(), false, 100, 150, true));
    f.assert_unchanged_failure(f.ix(10, 10, 1, 100)); // Revoked key.
    f.authority = replacement;
    assert!(f.send(f.ix(10, 10, 1, 100)));
    assert_eq!(f.policy().spent, 50);
    f.assert_unchanged_failure(f.ix(9, 40, 1, 200)); // Rotation cannot reopen receipts.
}

#[test]
fn initialize_payout_creates_empty_paused_vault_and_rejects_upgrade_authority() {
    let mut f = Fixture::new();
    for key in [f.policy, f.vault] {
        f.svm.set_account(key.into(), VmAccount::default()).unwrap();
    }
    let program_data = f.program_data();
    let upgrade_authority = Pubkey::new_unique();
    let mut data = f.svm.get_account(&program_data.into()).unwrap();
    // UpgradeableLoaderState::ProgramData: enum u32, slot u64, Option tag, key.
    data.data[12] = 1;
    data.data[13..45].copy_from_slice(upgrade_authority.as_ref());
    f.svm.set_account(program_data.into(), data).unwrap();
    let accounts = crate::accounts::InitializePayout {
        admin: f.admin.pubkey().into(),
        config: f.config,
        mint: f.mint,
        payout: f.policy,
        vault: f.vault,
        program: ID,
        program_data,
        system_program: anchor_lang::system_program::ID,
        token_program: token::ID,
    }
    .to_account_metas(None);
    for (authority, succeeds) in [
        (upgrade_authority, false),
        (f.authority.pubkey().into(), true),
    ] {
        f.svm.expire_blockhash();
        let ix = Instruction {
            program_id: ID,
            accounts: accounts.clone(),
            data: crate::instruction::InitializePayout {
                payout_authority: authority,
                per_transfer_cap: 100,
                epoch_cap: 150,
            }
            .data(),
        };
        let result = f.svm.send_transaction(Transaction::new_signed_with_payer(
            &[ix],
            Some(&f.admin.pubkey()),
            &[&f.admin],
            f.svm.latest_blockhash(),
        ));
        assert_eq!(result.is_ok(), succeeds, "{result:?}");
    }
    assert_eq!(f.balance(f.vault), 0);
    assert!(f.policy().paused);
    assert!(!f.configure(upgrade_authority, false, 100, 150, true));
}

#[test]
fn token_account_constraints_and_spending_overflow_fail_closed() {
    for invalid in [
        "source mint",
        "source owner",
        "destination mint",
        "self transfer",
        "overflow",
    ] {
        let mut f = Fixture::new();
        match invalid {
            "source mint" => f.put_token(f.vault, f.policy, Pubkey::new_unique(), 1000),
            "source owner" => f.put_token(f.vault, Pubkey::new_unique(), f.mint, 1000),
            "destination mint" => f.put_token(f.destination, f.recipient, Pubkey::new_unique(), 0),
            "self transfer" => {
                f.destination = f.vault;
                f.recipient = f.policy;
            }
            _ => {
                let mut policy = f.policy();
                policy.spent = u64::MAX;
                policy.epoch_cap = u64::MAX;
                f.put_anchor(f.policy, &policy);
            }
        }
        f.assert_unchanged_failure(f.ix(11, 1, 1, 100));
    }
}
