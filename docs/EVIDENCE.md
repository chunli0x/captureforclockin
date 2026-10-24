# Capture — Evidence Pack

Everything here is verifiable on Solana devnet.

**Live health check:** https://capture.magnumopus.ai/health — returns `production_ready: true`, `skr_payout: ready`.

## Repo

- Clock In ($SKR): https://github.com/chunli0x/captureforclockin
- Colosseum (test USDC): https://github.com/chunli0x/capture

## Live product loop (devnet, end-to-end)

A fresh wallet ran the full loop on-chain — get test $SKR, stake the bond, lock the bond while judging, resolve by consensus, get paid, and anchor the batch. Every transaction finalized with no error.

| Step | Signature |
|---|---|
| Faucet (get test $SKR) | `3u3qoxtQDGGbjN9U8hEt12DFD5mLYsLWV3xJSdtGi33u5doByncMBwrkMUFtAQc12sBqwANanTJp31zJKikMHWh5` |
| Deposit (stake the $SKR bond) | `34TRVVQ9hUWMRKyLJDanHnomL3v2s4oBGSWzfwSQaUkyMgG6F65EaRYPNGnioN5y85LGxUZwHesBQNa7FF3pZ5Wz` |
| Reserve (lock bond while judging) | `4Dau313QedSu6RL61mX9ipjDdDBqtSmsKUpnzsC9yzxbyKVCLfVaAN42cHD6pzM396AezmjcWRd9gtvQuhG7SMt4` |
| Resolve (consensus settles the judgment) | `55ZhbcpAq5PhTJhxYziaybP2WDBFsGiptcNKxxT4JPzULbHpD8rwnoCsY6Gh7ietybfmKZ65wTnF24XNKEkSLQEo` |
| Settle (aligned judge paid in $SKR) | `2oxxCetFocYdRT2kzdbqTfSFcvS2U5RAnpCab5mxLn1qK3Tt9S6k61ppQpNcf8GkHKaB9UvmU4soHbnKwnUYSH8y` |
| Anchor (batch Merkle root on-chain) | `3e7Pseo9XFcCx6aKq4nVAY9vU1nrX4ZpP5UoBscFmRGHnVyEy7678STriWkj7RqtkTjiijYKsCkrXdbM6dHCYch1` |

- Worker wallet: `5n83HZuFPESAZEGRSKP76kLWkakWHM4GNeG3HdVXmuD1`
- Batch id: `0592234bb822a3e0f8e650d5a9525734`
- Merkle root: `71dd4f79c5916a26ec1c076c2db14c53db6ed160c3153150113c8953a401d8bd`

## On-chain (devnet)

Two independent programs, one per token, so either build can run on its own.

| | Clock In ($SKR) | Colosseum (test USDC) |
|---|---|---|
| Program | `5TiDwP86k95yfQWzExrPTj5qcKxED99z8pW9fN81u5SV` | `8pY9dpwhe4Tbv9Ud9r13WCrNkugirsELNKA3hwfUUdpu` |
| Mint | `3aGKaVL7GpcTcTuKjkyirnVeyNG1JGFNyGGueu8mAGbM` | `TXtqUVPDj6JuDoeyFJvwuUqy5jGYTgF98kEkbeN6U8Z` |

Shared authorities:

- Deployer / upgrade authority: `2u3uauNBKnXWy65xGumNCrkzkWT3jNSn6kmedtvPDFRw`
- Verifier / slash authority: `DNBwRqu7rEWTaod4r6daUGfFqXPqxpYHgaSRKhXp5SPP`
- Treasury ($SKR mint authority): `4S7W4BJvxHu26cWZdw9cmrKRLDDpzQV2BGNo783NG8zS`

## Deployment signatures (devnet)

- $SKR config close: `EF9sYnsY1vynV6p3fqkNw3ceLPrW6ZHGgYbDHHDs7Ev9dgcmn7k8ADpCXZzDCUoVxzRmMUAutJHGyespaeb7Do2`
- $SKR initialize: `2pQWMg1jT3dDE1yMxdsNh41pe4PZi9yCTnWposjfAXbJAyihb2BmQZqsgoEhcnsfuU246NUnSs57s239LkX7wrLC`
- $SKR program upgrade: `2SULAJ9Qy1JYmJakXZEaGGiBXTnqJKz3Yz7UiaFG98PQUxNRxuuEkgHhgK2vuHmyiQkkP8F3kH2UNdroSykyHFMk`
- USDC program upgrade: `4srSq8NkBWFnoxbhnQFQ16Acvo6rBdhYpNaE9ip6cSGs24fxg4NDiR4U3FXsXK6EjA1WwpHSnVBPucq5cqN84GFr`

## Tests

- Backend: 63 (pytest)
- Anchor / Rust: 7 (cargo test)
- Android: 64 (gradle testDebugUnitTest)

## Build

```bash
git clone https://github.com/chunli0x/captureforclockin.git && cd captureforclockin

# backend
cd backend && .venv/bin/python -m pytest -q

# Anchor program (Rust unit tests + BPF build)
cargo test -p seeker-bounty --lib
anchor build

# Android (arm64 debug build)
cd android && ./gradlew :app:assembleDebug -PseekerApiBase=https://<live-host> --console=plain
```

## App

- `capture-clock-in.apk` — 46.5 MB, arm64, devnet test $SKR.
- Download: https://github.com/chunli0x/captureforclockin/releases/download/v1.0.0/capture-clock-in.apk
