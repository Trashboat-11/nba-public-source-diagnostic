# Public NBA source check

Run with Python 3.12. Install requirements and run `streamlit run app.py`.

The button checks one known official NBA PDF and up to three recent publication slots, the current ESPN scoreboard, and the first four bytes of 56 SportsDataverse Parquet assets. Redirects are limited to allowlisted public hosts. Each run targets a 45-second request budget, uses bounded reads and up to four parallel workers, and caches results for ten minutes per app process. An in-flight request can finish after the budget. Metrics include HTTP response status, type, latency, freshness checks, CPU time, and process peak memory.

The page displays metadata only. A passing result checks reachability and basic source structure. Full dataset integrity, source corroboration, pregame eligibility, scoring, and hosted inference require separate tests.
