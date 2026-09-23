# Operator UI Specification

## Purpose

The phone-first interface a biologist uses without a manual: arm/disarm, target-list editing,
capture acknowledgement, and honest visibility into faults, silence and clock trust. Self-contained
— nothing fetched from a CDN, so it works with no internet (S8).

## Requirements

### Requirement: Every operator task completes from a phone browser

The interface MUST support arming, disarming, editing a trap's target list, and acknowledging a
capture, entirely through touch input on a small viewport, with no external asset fetch (no CDN, no
remote fonts/scripts).

**Traces:** S10, S13, S8

#### Scenario: full task list completes on one page
- GIVEN the interface open on a phone-sized viewport with no internet route
- WHEN an operator arms a trap, edits its target list, and acknowledges a capture
- THEN each action completes without a full page navigation and without any request leaving the local network

### Requirement: Displayed state is reported state; captures persist (S3, S4)

The interface MUST render each trap's state, battery, clock, `targets_version` and
time-since-last-contact strictly from the registry's view — trap-reported data, never what was last
commanded. An unacknowledged capture MUST remain visible across page reloads and station restarts
until acknowledged.

**Traces:** S3, S4, S11

#### Scenario: capture from hours ago is still visible
- GIVEN a capture fired at 3am with nobody watching
- WHEN an operator opens the interface at 9am
- THEN the capture is shown, naming the trap and the tag, exactly as if seen live

#### Scenario: unreachable trap is marked, not hidden
- GIVEN a trap the registry reports unreachable
- WHEN the trap list renders
- THEN that trap is visually distinguished as unreachable rather than shown identically to a healthy one

**Verification note (Batch 2e, `verify-report-batch2d.md` PROOF 1):** the same distinction applies to
`state_may_be_stale` (a rehydrated report that may not be the trap's newest). Its data layer is pinned
end to end — `Registry.view()` and `GET /api/traps` are covered both ways. Its rendering inside
`index.html`'s `renderTrap` — the badge's actual on-screen content, placement and visibility — is
checked in CI only by a regex anchored on the source text of the ternary (`tests/test_app.py`), never by
rendering the page. Measured: three mutations invented against that regex all shipped green — an emptied
badge body, the same ternary hoisted into dead code the function never returns, and a CSS rule that hides
the markup while leaving it in the DOM (`display: none`) — because a regex over source text cannot see
content, placement or visibility, only that a matching substring exists somewhere in the file. This
project has no Node or browser test toolchain (no `package.json`, nothing in `pyproject.toml`'s `dev`
dependency group); adding one to pin a single badge was judged not worth a new, project-wide test-runtime
dependency. Decision, recorded rather than left implied: `index.html`'s actual rendered output is
unverified at this layer, for this element and any other purely presentational one. An operator-facing
regression here (the badge silently failing to render) would have to be caught by a human looking at the
page, not by this suite.

### Requirement: Clock-unset banner

When the station's clock trust state is `unset`, the interface MUST display this prominently, rather
than silently rendering timestamps that may be wrong.

**Traces:** S12

#### Scenario: unset clock shows a banner, not a plausible timestamp
- GIVEN the station's clock state is `unset`
- WHEN the interface renders
- THEN a visible banner states the clock is unset, alongside any timestamp fields
