# APlan Public Roadmap

This roadmap is written for readers, contributors, and potential collaborators.
APlan is not a live trading product. It is an auditable A-share research system
being built in public.

## Current Position

APlan has completed the first version of its research infrastructure:

- A-share universe filtering.
- Daily data ingestion and quality gates.
- Strategy plugin and unified signal format.
- Announcement and full-text evidence processing.
- Backtest and horizon validation workflows.
- Risk controls and paper trading engine.
- Markdown reports and SHA-256 audit records.

The system remains in `research_only` mode. Current strategies have not passed
the validation standard required for simulated execution.

## Phase 1: Public Research Lab

Goal: publish the system, explain the research process, and invite feedback
without presenting outputs as trade recommendations.

Deliverables:

- Public GitHub repository.
- GitHub Pages project website.
- Clear disclaimer and contribution guide.
- Weekly research notes.
- Sample reports with sensitive data removed.

Success signals:

- Readers understand the system boundary.
- Contributors can run tests locally.
- Feedback improves data quality, validation, or risk controls.

## Phase 2: Strategy Incubation

Goal: improve evidence coverage and test strategies across market regimes.

Deliverables:

- Better valuation and fundamentals coverage.
- Event-study validation for announcement categories.
- Industry and market regime filters.
- Out-of-sample validation dashboards.
- Paper trading approval checklist.

Success signals:

- A strategy passes training, validation, multi-offset, and risk checks.
- Failure cases are documented rather than hidden.
- Paper trading begins only after explicit approval.

## Phase 3: Product Shape

Goal: convert repeatable research workflows into useful tooling.

Possible directions:

- Self-hosted research dashboard.
- Data quality and audit toolkit for quant researchers.
- Private deployment service for research teams.
- Paid technical support and customization.

The project should avoid paid personalized stock advice unless the required
legal, compliance, and licensing structure is in place.

## Phase 4: Institutional Path

Goal: consider a formal quantitative research or investment company only after
the system has a credible paper trading record and a clear compliance path.

Prerequisites:

- Independent legal and regulatory review.
- Clear data licensing.
- Robust operational controls.
- Model risk management.
- Documented paper trading and live-readiness review.

The long-term ambition is not to make the system louder. It is to make it more
truthful, testable, and useful.
