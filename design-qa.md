# VoxQuarry design acceptance

**Findings:** no open P0/P1/P2 findings in the checked states. This accepts the selected Evidence Desk direction applied to real analysis, rather than pixel identity to fictional results.

## Target and comparison evidence

- Source visual truth: [Evidence Desk concept](docs/design/reference.png), 1488 × 1056 pixels, generated design.
- Implementation: [Chinese](docs/images/report-zh.jpg), [English](docs/images/report-en.jpg), both 1488 × 1056 pixels; CSS viewport 1488 × 1056, effective capture density 1×, no resizing/browser chrome.
- [Mobile](docs/images/report-mobile.jpg): 390 × 844 pixels and CSS viewport, effective 1×. No mobile source concept exists; this state checks usability.
- Local URL: `http://127.0.0.1:18763/`; authenticated synthetic workspace, Reports → M40 → battery topic, light theme. Account/products are fictional.
- Source and implementation were opened together in the same visual comparison input, including the final desktop/mobile captures. The review was grounded in rendered images.
- Additional focused crops were unnecessary: full-size captures made the logo, navigation, labels, buttons and quotations readable. Mobile was inspected at native width.

## Required fidelity surfaces

| Surface | Assessment |
|---|---|
| Typography | System sans stack, Inter when available, Segoe UI and Chinese system fallbacks; no external font request. The generated source has no exact font specification. Strong page/report hierarchy, 16px topic text and 18px quotations preserve its direction; compact metadata accommodates actual KPIs and audit fields. Both languages' wrapping checked. |
| Spacing and layout | 300px navigation, flexible report and 370px evidence rail. Flat separators replace the initial card-heavy build. Real metric/denominator rows intentionally add content before topics. Phone layout stacks report/evidence with a dismissible drawer. |
| Colors | Near-white surfaces, charcoal text, blue `#2854ee`, pale-blue active states and subtle dividers. Muted text contrast increased during QA. |
| Images and icons | Independent transparent bracket-and-dot brand raster checked for sharpness/halos. Actual Lucide icons with consistent strokes; no handmade CSS/SVG brand substitute. No product photography was required by the concept. |
| Copy and content | Five functional modules, one interface switch, separate answer language. Corrected the concept's duplicate switch and falsely labeled translated quotation. Replaced invented fan conclusions with reproducible mower metrics and unchanged English source text. |

## Comparison history

| Pass | Evidence | Finding and repair | Result |
|---|---|---|---|
| 1 | [v1](docs/design/report-zh-v1.jpg) | P1: compact card framing and proportions drifted from the broad flat concept. Reworked hierarchy/navigation/report grid. | blocked |
| 2 | [v2](docs/design/report-zh-v2.jpg) | P2: small report typography/navigation density. Increased hierarchy and spacing. | blocked |
| 3 | [v3](docs/design/report-zh-v3.jpg) | P2: muted text, narrow evidence rail, weak export action. Increased contrast and type sizes, widened rail, highlighted export. | blocked |
| 4 | [v4](docs/design/report-zh-v4.jpg) | P2: Chinese category remained English. Bound localized label. Functional sweep found neutral topics opened general evidence; added complete topic groups and regression coverage. | blocked |
| 5 | Final images above | Closed phone drawer cast a shadow. Hide the closed drawer from paint/accessibility; apply shadow only when open. Recaptured mobile and both desktop languages after fixes. | passed |

## Interactions and accessibility

Verified local sign-in, idempotent demo, product create/edit persistence, source links and linked-report opening, report generation, language switching, topic selection, filtered refresh and evidence paging, English Markdown download, Walmart/Home Depot collection form bounds, unconfigured-model state, independent answer language, settings and phone navigation. No live collection was submitted.

The battery topic's 10 distinct reviews produce 15 evidence fragments; the fragment pager is not a review denominator. Regression coverage verifies the distinct identities and neutral mentions. Both languages retain 40 reviews and 25/40 positive ratings.

At 1488px and 390px, no horizontal document overflow was observed. Closed phone navigation is absent from the accessibility tree. Controls have semantic roles and form labels. Console inspection found no errors. Automated screen-reader, 200% zoom and all tablet breakpoints are not certified by this pass.

**Open questions:** none blocking checked scope. External integration gaps are in [validation](docs/validation.md).

**Implementation checklist:** visual fixes complete; bilingual/mobile captures complete; neutral-topic regression complete; temporary viewport reset.

**Follow-up polish:** dedicated screen-reader/zoom audit and modal keyboard focus trapping.

final result: passed
