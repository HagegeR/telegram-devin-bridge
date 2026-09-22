## Summary

<!-- What changed and why — one or two sentences a reviewer hasn't seen the diff can orient by. -->

## Test plan

<!-- How you verified it: pytest, a manual Telegram round-trip, doctor output, etc. -->

## Checklist

- [ ] `pytest` passes locally
- [ ] `ruff check .` is clean
- [ ] New env vars added to `.env.example` and `docs/configuration.md`
- [ ] New commands added to `_help_text()` and the README commands table
- [ ] No tokens, API keys, or real user/chat IDs in the diff
