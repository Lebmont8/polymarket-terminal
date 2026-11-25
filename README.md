# Polymarket Terminal
Inspect public Polymarket order books and keep a local record of order requests, including outcomes that are still unknown. This prototype validates prices and sizes with exact decimals and stores the journal in SQLite. Live trading is disabled.

## Included
- GTC/GTD/FOK/FAK and post-only validation
- Idempotent submit/cancel tracking and partial-fill/cancel handling
- Bounded public order-book/market reads
- Read-only adapters for separately configured paper-trading stores
- Loopback HTTP service with a generated local access token

## Verify
Python 3.11+ standard library.
~~~sh
python3 -B -m unittest -v test_terminal
python3 server.py --help
~~~
The server binds loopback only. Its internal token is generated in the chosen state directory. The API is intended behind your own authenticated UI adapter, not as a public Internet listener. No existing tokens or connection observations are distributed.

Set POLYMARKET_PAPER_ROOT to your own paper-data root if using the optional readers. No personal paper histories or financial results are included. Public-data calls happen only on explicit market requests.

Live trading remains fail-closed: no authenticated trading SDK, signer, user stream or aggregate risk integration is connected. This is not a working production exchange client or a geo-restriction workaround.

Source-inspection licence: see LICENSE. Original implementation developed with AI assistance.

## Continuous integration
A reference GitHub Actions workflow is included at ci/github-actions.yml. To activate it in your own repository, place it under .github/workflows/. Its presence as a reference does not mean hosted CI is enabled or has passed.
