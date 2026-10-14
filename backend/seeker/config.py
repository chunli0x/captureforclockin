from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SEEKER_", extra="ignore")
    database_url: str = "postgresql+psycopg://localhost/seeker"
    local_sqlite: bool = False
    domain: str = "localhost"
    rpc_url: str = "https://api.devnet.solana.com"
    cluster: str = "devnet"
    program_id: str
    # Required: SEEKER_SKR_MINT supplies the devnet test $SKR mint (6 decimals).
    skr_mint: str
    verifier_key_file: Path
    # Devnet treasury: holds the $SKR mint authority (the faucet, the bond, and
    # the settle_skr payout; one token, three jobs). Optional (0600,
    # gitignored). Unset -> the on-chain mint is unavailable and the backend
    # fails closed instead of fabricating it.
    treasury_key_file: Path | None = None
    session_seconds: int = 3600
    min_bond: int = Field(default=1_000_000, gt=0, le=1_000_000_000_000)
    # The devnet faucet also drops a little SOL so the worker can pay the
    # deposit transaction fee and the bond/vault rent (the deposit is
    # client-signed with payer=worker, so the worker needs lamports). 0.05 SOL.
    faucet_sol_lamports: int = Field(default=50_000_000, gt=0)
    # A wallet may tap the devnet test $SKR faucet this many times (each tap
    # mints min_bond test $SKR) before it is cut off.
    faucet_max_mints: int = Field(default=5, gt=0)
    # Global faucet budget: keep at least this much SOL in the treasury for
    # payouts. A tap is refused when it would drop the treasury below this
    # floor, so a swarm of fresh wallets cannot drain the devnet funder.
    faucet_treasury_sol_floor: int = Field(default=1_000_000_000, gt=0)

    # ---- Judgment product (RLHF preference judging) ----
    # Consensus-gated pay in test $SKR (6 decimals, not real value). All are starting
    # hypotheses (incentive spec §8): tune against real agreement data, not once.
    judge_k: int = Field(default=3, gt=0)
    calibration_n: int = Field(default=10, gt=0)
    judge_window: int = Field(default=100, gt=0)
    judge_slash_floor: float = 0.45
    judge_slash_min_judged: int = Field(default=20, gt=0)
    # Payout in $SKR base units (6 decimals). The treasury keypair is the $SKR
    # mint authority; settlement mints test $SKR to the judge. $SKR is also the
    # bond (the slashable collateral). So:
    #   judge_base_skr = 0.02 $SKR per aligned judgment (× level multiplier)
    #   judge_flat_skr = 0.005 $SKR flat "can't tell"
    judge_base_skr: int = Field(default=20_000, gt=0)
    judge_flat_skr: int = Field(default=5_000, gt=0)
    # Cap on flat "can't tell" payments a judge can earn per rolling window,
    # to bound abstention farming (finding #12): three colluding judges can
    # otherwise send "can't tell" on every fresh pair and collect flat without
    # ever resolving a task. 0 disables the cap.
    judge_flat_cap: int = Field(default=10, ge=0)
    # Liveness is a demo-time on-device motion check. When False, the gate is
    # skipped and every wallet reads as liveness-complete (used to demo the
    # judging flow while the on-device check is being stabilized).
    liveness_required: bool = True

    @model_validator(mode="after")
    def safe_environment(self):
        if self.cluster not in {"devnet", "localnet"}:
            raise ValueError("Only devnet/localnet are supported")
        if self.database_url.startswith("sqlite") and not self.local_sqlite:
            raise ValueError("SQLite requires explicit local_sqlite=true")
        if not self.domain or "\n" in self.domain:
            raise ValueError("Invalid authentication domain")
        return self
