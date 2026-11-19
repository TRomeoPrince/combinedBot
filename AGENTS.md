# CombinedBot Shared Rules
- Data paths: ./data/XAUUSD_BARS, ./data/US30_BARS, ./data/XAUUSD_TICKS
- Evaluate signals on CLOSED bars only; execute entries at NEXT bar open.
- Include spread on every trade; same-bar SL/TP collisions default to SL first.
- Require >=100 trades and positive expectancy across >=3 of 4 broker feeds.
- Log every hypothesis and result in EXPERIMENT_LOG.md.
- Roles: Codex=Architect, Claude=Quant Engineer, Gemini=Auditor & Git Lead.
