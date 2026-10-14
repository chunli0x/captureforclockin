# Capture — Evidence Pack

Everything here is verifiable on Solana devnet. Facts pulled live on 2026-10-14.

## Repo

- Clock In ($SKR): https://github.com/chunli0x/captureforclockin
- Colosseum (test USDC): https://github.com/chunli0x/capture

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

## Transaction signatures (devnet)

- $SKR config close: `EF9sYnsY1vynV6p3fqkNw3ceLPrW6ZHGgYbDHHDs7Ev9dgcmn7k8ADpCXZzDCUoVxzRmMUAutJHGyespaeb7Do2`
- $SKR initialize: `2pQWMg1jT3dDE1yMxdsNh41pe4PZi9yCTnWposjfAXbJAyihb2BmQZqsgoEhcnsfuU246NUnSs57s239LkX7wrLC`
- $SKR program upgrade: `2SULAJ9Qy1JYmJakXZEaGGiBXTnqJKz3Yz7UiaFG98PQUxNRxuuEkgHhgK2vuHmyiQkkP8F3kH2UNdroSykyHFMk`
- USDC program upgrade: `4srSq8NkBWFnoxbhnQFQ16Acvo6rBdhYpNaE9ip6cSGs24fxg4NDiR4U3FXsXK6EjA1WwpHSnVBPucq5cqN84GFr`

## Tests (verified 2026-10-14)

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
