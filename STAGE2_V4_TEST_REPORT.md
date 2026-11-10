# Stage 2 v4 regression test report

## Automated tests

- **15/15 regression tests passed** (`python -m unittest discover -s tests -v`).
- `main.py` and `cluster_stories.py` passed Python syntax compilation.
- SQLite schema test passed for new databases and backward-compatible migration from the previous schema, including insertion of publisher metadata.
- Google consent redirect behavior was tested with a mocked HTTP response. Live Google News redirect behavior still needs to be checked from the user's Windows machine.

## Replay against the uploaded 2026-10-18 13:46 report

Replayed the 26 candidate headlines from `clusters_2026-10-18_13-46.json` through the v4 grouping logic:

- 26 candidates → 23 groups.
- 2 multi-story groups; 21 single-story groups.
- The three Taiwan–China AI-chip smuggling / criminalization headlines grouped together as one candidate event.
- The AOL.com and The Motley Fool headlines about the same $1,000 TSM investment prediction grouped together as a candidate; verify whether the content is syndicated before treating them as independent sources.
- Different MarketBeat TSM share transactions involving different named investors remained separate.
- Publisher domains from Google News consent redirects are no longer counted as `google.com` publishers. Where available, the collector now preserves Google News RSS `<source>` metadata (`publisher_name`, `publisher_url`) so Stage 2 can attribute the outlet without pretending the article URL was resolved.

## Limitations

These tests check software behavior and headline similarity, not the truth of any story. The grouping replay is based on titles in the uploaded report, not a new live RSS collection. RSS publisher metadata may be missing for some items. Full article-text comparison and fact verification remain Stage 3 work.
