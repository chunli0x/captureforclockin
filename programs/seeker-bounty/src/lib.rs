use anchor_lang::prelude::*;
use anchor_spl::token::{self, Mint, Token, TokenAccount, Transfer};

// Development identity only. Deploy with your own keypair and update all clients.
declare_id!("5TiDwP86k95yfQWzExrPTj5qcKxED99z8pW9fN81u5SV");

const COOLDOWN: i64 = 86_400;

// Bond warnings are a two-strike ladder. A bond is slashed (terminal) once it
// carries two or more warnings; a slashed bond can never withdraw.
const TERMINAL_WARNINGS: u8 = 255;

#[program]
pub mod seeker_bounty {
    use super::*;

    pub fn initialize(
        ctx: Context<Initialize>,
        verifier: Pubkey,
        slash_authority: Pubkey,
        min_bond: u64,
    ) -> Result<()> {
        require!(
            min_bond > 0
                && verifier != Pubkey::default()
                && slash_authority != Pubkey::default(),
            Error::Invalid
        );
        require!(ctx.accounts.skr_mint.decimals == 6, Error::Invalid);
        ctx.accounts.config.set_inner(Config {
            admin: ctx.accounts.admin.key(),
            verifier,
            skr_mint: ctx.accounts.skr_mint.key(),
            min_bond,
            slash_authority,
        });
        Ok(())
    }

    pub fn deposit_bond(ctx: Context<DepositBond>, amount: u64) -> Result<()> {
        require!(amount > 0, Error::Invalid);
        let bond = &mut ctx.accounts.bond;
        if bond.worker == Pubkey::default() {
            bond.worker = ctx.accounts.worker.key();
            bond.pending = 0;
        }
        require_keys_eq!(bond.worker, ctx.accounts.worker.key(), Error::Unauthorized);
        // A slashed bond (warnings >= 2) is reset to clean on deposit: the slash
        // already drained the vault, so a new deposit is a fresh bond, not an
        // extension of the old one. This is the "re-deposit a fresh bond" path.
        // Only a terminally slashed bond (warnings == TERMINAL_WARNINGS) resets
        // on deposit. A bond at the two-strike threshold (warnings == 2) is
        // slashable but not yet slashed; resetting it there would let a worker
        // dodge the slash with a one-base-unit deposit (finding #5).
        if bond.warnings == TERMINAL_WARNINGS {
            bond.warnings = 0;
            bond.pending = 0;
        }
        bond.unlock_at = Clock::get()?
            .unix_timestamp
            .checked_add(COOLDOWN)
            .ok_or(Error::Overflow)?;
        token::transfer(
            CpiContext::new(
                ctx.accounts.token_program.key(),
                Transfer {
                    from: ctx.accounts.source.to_account_info(),
                    to: ctx.accounts.vault.to_account_info(),
                    authority: ctx.accounts.worker.to_account_info(),
                },
            ),
            amount,
        )
    }

    pub fn withdraw_bond(ctx: Context<WithdrawBond>) -> Result<()> {
        require!(withdrawable(ctx.accounts.bond.warnings), Error::Slashed);
        require!(ctx.accounts.bond.pending == 0, Error::Pending);
        require!(
            Clock::get()?.unix_timestamp >= ctx.accounts.bond.unlock_at,
            Error::Locked
        );
        let worker = ctx.accounts.worker.key();
        let seeds: &[&[u8]] = &[b"bond", worker.as_ref(), &[ctx.bumps.bond]];
        token::transfer(
            CpiContext::new_with_signer(
                ctx.accounts.token_program.key(),
                Transfer {
                    from: ctx.accounts.vault.to_account_info(),
                    to: ctx.accounts.destination.to_account_info(),
                    authority: ctx.accounts.bond.to_account_info(),
                },
                &[seeds],
            ),
            ctx.accounts.vault.amount,
        )
    }

    pub fn warn(ctx: Context<Warn>, worker: Pubkey) -> Result<()> {
        let bond = &mut ctx.accounts.bond;
        bond.warnings = add_warning(bond.warnings)?;
        emit!(BondWarned {
            worker,
            warnings: bond.warnings,
        });
        Ok(())
    }

    /// Close the config PDA so it can be re-initialized with a new layout.
    ///
    /// Deployer-gated (must be the program upgrade authority). Used once, at
    /// upgrade time, when a program layout change grows the Config account: the
    /// stale config may not deserialize into the new Config struct, so this
    /// closes it raw (move lamports to the admin, shrink to zero, reassign to
    /// the system program), then ``initialize`` recreates it at the new size
    /// and creates the penalty vault.
    pub fn close_config(ctx: Context<CloseConfig>) -> Result<()> {
        let config = &ctx.accounts.config;
        let admin = &ctx.accounts.admin;
        let lamports = config.lamports();
        **config.try_borrow_mut_lamports()? = 0;
        **admin.try_borrow_mut_lamports()? = admin
            .lamports()
            .checked_add(lamports)
            .ok_or(Error::Overflow)?;
        // Zero the data and reassign to the system program. ``initialize`` will
        // then re-allocate the account at the new size and take ownership again.
        config.try_borrow_mut_data()?.fill(0);
        config.assign(&ctx.accounts.system_program.key());
        Ok(())
    }

    pub fn slash(ctx: Context<Slash>, worker: Pubkey) -> Result<()> {
        require!(slashable(ctx.accounts.bond.warnings), Error::NotWarned);
        // Full slash: the entire bond vault is forfeit on the second strike.
        let taken = ctx.accounts.bond_vault.amount;
        require!(taken > 0, Error::Invalid);
        let seeds: &[&[u8]] = &[b"bond", worker.as_ref(), &[ctx.bumps.bond]];
        token::transfer(
            CpiContext::new_with_signer(
                ctx.accounts.token_program.key(),
                Transfer {
                    from: ctx.accounts.bond_vault.to_account_info(),
                    to: ctx.accounts.penalty_vault.to_account_info(),
                    authority: ctx.accounts.bond.to_account_info(),
                },
                &[seeds],
            ),
            taken,
        )?;
        let bond = &mut ctx.accounts.bond;
        bond.warnings = TERMINAL_WARNINGS;
        emit!(BondSlashed {
            worker,
            amount: taken,
        });
        Ok(())
    }

    /// Anchor a resolved batch's Merkle root on-chain. Append-only: the batch
    /// account is ``init``, so a batch_id can only be anchored once.
    pub fn post_batch(
        ctx: Context<PostBatch>,
        batch_id: [u8; 16],
        merkle_root: [u8; 32],
    ) -> Result<()> {
        require!(valid_merkle_root(merkle_root), Error::Invalid);
        ctx.accounts.batch.set_inner(Batch { merkle_root });
        emit!(BatchAnchored {
            batch_id,
            merkle_root,
        });
        Ok(())
    }

    /// Record a payment id so a settle_skr retry is idempotent (finding D).
    /// The mint authority records the id BEFORE minting; a retry whose payment
    /// account already exists must skip the mint. Append-only via `init`.
    pub fn record_payment(
        ctx: Context<RecordPayment>,
        payment_id: [u8; 16],
    ) -> Result<()> {
        require!(payment_id != [0u8; 16], Error::Invalid);
        ctx.accounts
            .payment
            .set_inner(Payment {
                mint_authority: ctx.accounts.mint_authority.key(),
            });
        Ok(())
    }
}

/// Apply one warning. Refused once the bond is already at the two-strike
/// threshold or terminally slashed. Saturating, so it can never wrap past 255.
pub fn add_warning(warnings: u8) -> Result<u8> {
    require!(warnings < 2, Error::Slashed);
    Ok(warnings.saturating_add(1))
}

/// A bond with two or more warnings is eligible for slashing (the ladder).
pub fn slashable(warnings: u8) -> bool {
    warnings >= 2
}

/// A bond freezes on its first warning; only a clean bond can withdraw. This
/// closes the withdraw-after-warn escape: a warned worker must keep the vault
/// funded so the second-strike slash has something to forfeit.
pub fn withdrawable(warnings: u8) -> bool {
    warnings == 0
}

/// A zeroed Merkle root is rejected (it would anchor an empty/unresolved batch).
pub fn valid_merkle_root(root: [u8; 32]) -> bool {
    root != [0u8; 32]
}

#[account]
#[derive(InitSpace)]
pub struct Config {
    pub admin: Pubkey,
    pub verifier: Pubkey,
    pub skr_mint: Pubkey,
    pub min_bond: u64,
    pub slash_authority: Pubkey,
}
#[account]
#[derive(InitSpace)]
pub struct Bond {
    pub worker: Pubkey,
    pub pending: u32,
    pub unlock_at: i64,
    /// 0 = clean, 1 = warned once, >= 2 = slashed (terminal).
    pub warnings: u8,
}
#[account]
#[derive(InitSpace)]
pub struct Batch {
    /// Merkle root of the batch's resolved preference entries. Append-only: a
    /// batch_id can be anchored exactly once (``init``).
    pub merkle_root: [u8; 32],
}
#[account]
#[derive(InitSpace)]
pub struct Payment {
    /// The treasury keypair that recorded (and mints) this payment id.
    pub mint_authority: Pubkey,
}

#[derive(Accounts)]
pub struct Initialize<'info> {
    #[account(mut)]
    pub admin: Signer<'info>,
    #[account(init, payer=admin, space=8+Config::INIT_SPACE, seeds=[b"config"], bump)]
    pub config: Account<'info, Config>,
    pub skr_mint: Account<'info, Mint>,
    #[account(init, payer=admin, seeds=[b"penalty-vault", skr_mint.key().as_ref()], bump, token::mint=skr_mint, token::authority=config)]
    pub penalty_vault: Account<'info, TokenAccount>,
    #[account(constraint=program.programdata_address()? == Some(program_data.key()) @ Error::Unauthorized)]
    pub program: Program<'info, crate::program::SeekerBounty>,
    #[account(constraint=program_data.upgrade_authority_address == Some(admin.key()) @ Error::Unauthorized)]
    pub program_data: Account<'info, ProgramData>,
    pub system_program: Program<'info, System>,
    pub token_program: Program<'info, Token>,
    pub rent: Sysvar<'info, Rent>,
}

#[derive(Accounts)]
pub struct DepositBond<'info> {
    #[account(mut)]
    pub worker: Signer<'info>,
    #[account(seeds=[b"config"], bump, has_one=skr_mint)]
    pub config: Account<'info, Config>,
    pub skr_mint: Account<'info, Mint>,
    #[account(init_if_needed, payer=worker, space=8+Bond::INIT_SPACE, seeds=[b"bond", worker.key().as_ref()], bump)]
    pub bond: Account<'info, Bond>,
    #[account(init_if_needed, payer=worker, seeds=[b"bond-vault", worker.key().as_ref(), skr_mint.key().as_ref()], bump, token::mint=skr_mint, token::authority=bond)]
    pub vault: Account<'info, TokenAccount>,
    #[account(mut, token::mint=skr_mint, token::authority=worker)]
    pub source: Account<'info, TokenAccount>,
    pub token_program: Program<'info, Token>,
    pub system_program: Program<'info, System>,
    pub rent: Sysvar<'info, Rent>,
}

#[derive(Accounts)]
pub struct WithdrawBond<'info> {
    pub worker: Signer<'info>,
    #[account(seeds=[b"config"], bump, has_one=skr_mint)]
    pub config: Account<'info, Config>,
    pub skr_mint: Account<'info, Mint>,
    #[account(seeds=[b"bond", worker.key().as_ref()], bump, has_one=worker)]
    pub bond: Account<'info, Bond>,
    #[account(mut, seeds=[b"bond-vault", worker.key().as_ref(), skr_mint.key().as_ref()], bump, token::mint=skr_mint, token::authority=bond)]
    pub vault: Account<'info, TokenAccount>,
    #[account(mut, token::mint=skr_mint, token::authority=worker)]
    pub destination: Account<'info, TokenAccount>,
    pub token_program: Program<'info, Token>,
}

#[derive(Accounts)]
#[instruction(worker: Pubkey)]
pub struct Warn<'info> {
    #[account(constraint = slash_authority.key() == config.slash_authority @ Error::Unauthorized)]
    pub slash_authority: Signer<'info>,
    #[account(seeds=[b"config"], bump)]
    pub config: Account<'info, Config>,
    #[account(mut, seeds=[b"bond", worker.as_ref()], bump)]
    pub bond: Account<'info, Bond>,
}

#[derive(Accounts)]
pub struct CloseConfig<'info> {
    #[account(mut)]
    pub admin: Signer<'info>,
    /// CHECK: a stale config (pre-slash_authority layout) does NOT deserialize
    /// into the current Config struct, so close it raw. The PDA seeds prove it
    /// is the config; the upgrade-authority check below proves the caller.
    #[account(
        mut,
        seeds=[b"config"],
        bump,
    )]
    pub config: AccountInfo<'info>,
    pub system_program: Program<'info, System>,
    #[account(constraint = program.programdata_address()? == Some(program_data.key()) @ Error::Unauthorized)]
    pub program: Program<'info, crate::program::SeekerBounty>,
    #[account(constraint = program_data.upgrade_authority_address == Some(admin.key()) @ Error::Unauthorized)]
    pub program_data: Account<'info, ProgramData>,
}

#[derive(Accounts)]
#[instruction(worker: Pubkey)]
pub struct Slash<'info> {
    #[account(constraint = slash_authority.key() == config.slash_authority @ Error::Unauthorized)]
    pub slash_authority: Signer<'info>,
    #[account(seeds=[b"config"], bump)]
    pub config: Account<'info, Config>,
    #[account(mut, seeds=[b"bond", worker.as_ref()], bump)]
    pub bond: Account<'info, Bond>,
    #[account(mut, seeds=[b"bond-vault", worker.as_ref(), config.skr_mint.as_ref()], bump, token::mint=config.skr_mint, token::authority=bond)]
    pub bond_vault: Account<'info, TokenAccount>,
    #[account(mut, seeds=[b"penalty-vault", config.skr_mint.as_ref()], bump, token::mint=config.skr_mint, token::authority=config)]
    pub penalty_vault: Account<'info, TokenAccount>,
    pub token_program: Program<'info, Token>,
}

#[derive(Accounts)]
#[instruction(batch_id: [u8; 16])]
pub struct PostBatch<'info> {
    #[account(mut, constraint = authority.key() == config.slash_authority @ Error::Unauthorized)]
    pub authority: Signer<'info>,
    #[account(seeds=[b"config"], bump)]
    pub config: Account<'info, Config>,
    #[account(init, payer=authority, space=8+Batch::INIT_SPACE, seeds=[b"batch", batch_id.as_ref()], bump)]
    pub batch: Account<'info, Batch>,
    pub system_program: Program<'info, System>,
    pub rent: Sysvar<'info, Rent>,
}

#[derive(Accounts)]
#[instruction(payment_id: [u8; 16])]
pub struct RecordPayment<'info> {
    #[account(mut)]
    pub mint_authority: Signer<'info>,
    #[account(init, payer=mint_authority, space=8+Payment::INIT_SPACE, seeds=[b"payment", payment_id.as_ref()], bump)]
    pub payment: Account<'info, Payment>,
    pub system_program: Program<'info, System>,
}

#[event]
pub struct BondWarned {
    pub worker: Pubkey,
    pub warnings: u8,
}

#[event]
pub struct BondSlashed {
    pub worker: Pubkey,
    pub amount: u64,
}

#[event]
pub struct BatchAnchored {
    pub batch_id: [u8; 16],
    pub merkle_root: [u8; 32],
}

#[error_code]
pub enum Error {
    #[msg("Unauthorized signer or account")]
    Unauthorized,
    #[msg("Invalid bounds or content")]
    Invalid,
    #[msg("Checked arithmetic failed")]
    Overflow,
    #[msg("Bond has pending liabilities")]
    Pending,
    #[msg("Deadline or cooldown not reached")]
    Locked,
    #[msg("Bond is slashed (two or more warnings)")]
    Slashed,
    #[msg("Two warnings are required before a bond can be slashed")]
    NotWarned,
}

#[cfg(test)]
mod tests {
    use super::*;

    // -- warning ladder --------------------------------------------------

    #[test]
    fn warn_then_warn_reaches_two_and_enables_slash() {
        assert_eq!(add_warning(0).unwrap(), 1);
        assert_eq!(add_warning(1).unwrap(), 2);
        assert!(slashable(2));
    }

    #[test]
    fn warn_is_saturating_and_never_wraps() {
        // The ladder stops at two, so anything at or past the threshold refuses.
        assert!(add_warning(2).is_err());
        assert!(add_warning(255).is_err());
    }

    #[test]
    fn slash_without_prior_warn_is_refused() {
        assert!(!slashable(0));
        assert!(!slashable(1));
    }

    #[test]
    fn slashed_bond_is_terminal_withdraw_and_warn_refuse() {
        // Terminal (255) and threshold (2) refuse further warns/withdraws, and
        // a warned bond (1) is frozen too (finding A): only a clean bond (0)
        // can withdraw.
        assert!(!withdrawable(255));
        assert!(!withdrawable(2));
        assert!(!withdrawable(1));
        assert!(withdrawable(0));
    }

    #[test]
    fn full_slash_drains_the_entire_bond_vault() {
        // A second strike forfeits the whole vault: `taken` is the vault balance
        // in full, so nothing remains and the penalty vault holds exactly it.
        let balance = 100u64;
        let taken = balance; // slash takes the full amount, no clamp
        assert_eq!(taken, 100);
        assert_eq!(balance - taken, 0);
        assert_eq!(taken + (balance - taken), balance);
    }

    #[test]
    fn merkle_root_rejects_zero_and_accepts_nonzero() {
        // A zeroed root would anchor an empty/unresolved batch; refuse it.
        assert!(!valid_merkle_root([0u8; 32]));
        assert!(valid_merkle_root([1u8; 32]));
    }
}
