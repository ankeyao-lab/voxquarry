# English launch copy

## GitHub About

A bilingual, self-hosted review research workbench: collection, product data, reproducible reports, and AI research with citations.

## Portfolio card

**VoxQuarry — From reviews to evidence**

An open-source AI product I designed and built to connect review collection, product data, deterministic analysis and evidence-backed research. Findings retain their sample scope, numerators and denominators, source versions and original quotations. It supports English and Chinese, self-hosting and bring-your-own-model research, with a model-free synthetic demo.

Product and engineering work: Python / React, data contracts, source completeness, agent tools, citation validation, access boundaries and bilingual UX.

## Launch post

**Introducing VoxQuarry: an open-source workbench for review research**

A product research report might say, “Users commonly report this problem.” I want to be able to ask: How many users? Out of which sample? Which version of the data? And where are the original reviews?

I built VoxQuarry to make those questions part of the workflow.

It brings Data Collection, Products, Reports and AI Research into one self-hosted workbench. You can import existing reviews or use public-page collection adapters. Walmart and Home Depot support bounded sampling with page, record and time limits. Interrupted collection retains valid evidence and records the missing source coverage.

Versioned rules produce metrics and topic analysis. AI research works with saved facts and original sources, retaining citations and tool records. The interface supports English and Simplified Chinese, and response language can be selected separately. Changing display language does not change a denominator or replace a source quotation.

For me, this project is an exercise in building an AI product whose evidence and limits are inspectable. That includes data modeling, API contracts, access controls, runtime isolation and the interface people actually use.

The first release is an alpha. Deterministic rules currently focus on English source reviews and outdoor power equipment categories. Chinese source text can be retained, but equivalent Chinese rule coverage is not claimed. Live providers, retailer access and the optional Docker runtime require verification in your own environment.

The repository includes 40 fictional reviews so you can explore the product and report flow without configuring a model. Feedback, reproducible bug reports, new rule packs, connector work and language contributions are welcome.

GitHub: https://github.com/ankeyao-lab/voxquarry

## Short post

I built VoxQuarry: a bilingual, self-hosted workbench for review research.

Collect or import reviews, organize products, inspect reproducible metrics, and research with source citations. Try the synthetic demo without a model key, or bring your own model. First release: alpha, English deterministic rules, explicit sample and coverage limits.

https://github.com/ankeyao-lab/voxquarry

## 60-second demo script

0–10s: Open a report. “A conclusion should come with its denominator and source.”

10–25s: Show the 40-review synthetic sample, rating metrics and topics. Select a topic and inspect the original review.

25–35s: Switch between English and Chinese. Point out that counts and evidence IDs stay the same.

35–45s: Show Products and the collection limits. Explain import and bounded sampling.

45–55s: Show research language, source access and run records. If no model is configured, show that state clearly; do not present a mock answer as a live provider result.

55–60s: Show the repository and local setup, then invite feedback.
